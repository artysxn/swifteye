"""Same bet, two prices: Polymarket's esports side markets against each other and against the match winner (24 Nov).

At certain moments two different Polymarket markets pay on exactly the same event:
  DECIDER    the last map of a series at level score: "match winner X" = "map N winner X".
  TOTALS     the leader is one map from winning, map k is being played: "over k.5 maps" = "the trailing team wins map k".
  HANDICAP   the same moment, leader exactly 2 (or 3) maps ahead of what the other side needs: "leader -1.5 (-2.5)" =
             "the leader wins map k".
If token X can be BOUGHT for less than its twin Y can be SOLD (selling Y = buying Y's other side at 1 - bid), the pair
costs under $1 and pays exactly $1: a locked profit, whoever wins. Both fills pay the taker fee.

Data: pm_sister_quotes (child_moneyline = map winners with the map number in `line`, totals, map_handicap; one token a
market, the first-listed side or Over) and pm_quotes (match winner, home token), recorded 21 -> 23 Nov. Map results are
read from the map markets themselves (bid 97c+ / ask 3c- that stays). Quotes are carried forward on a 1 s grid; a pair
is only looked at while BOTH markets printed a row in the last 120 s and outside the stretches where the whole side
feed was silent (> 15 s without any row: the recorder pauses when the main feed runs late).

Prints per episode (profit >= 1c a share after fees for 3+ s in a row): when, how long, the profit, the shares on offer.
usage: python tools/signals/identity.py --db ~/swifteye/oddsfeed.db [--out DIR]
"""
import argparse
import os
import re
import sqlite3
import time

import numpy as np

norm = lambda s: set(re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).split())
fee = lambda p: 0.05 * p * (1 - p)


def ffill(ts, vals, grid):
    """vals (n, k) at times ts -> the latest row at or before each grid second (nan before the first), and its age in s."""
    j = np.searchsorted(ts, grid, side='right') - 1
    out = np.where((j >= 0)[:, None], vals[np.clip(j, 0, None)], np.nan)
    age = np.where(j >= 0, (grid - ts[np.clip(j, 0, None)]) / 1000.0, np.inf)
    return out, age


def decided_at(ts, bid, ask):
    """(winner 1 home / 0 away / None, time it was decided for good) from one market's rows."""
    keep = np.flatnonzero(np.isfinite(bid) | np.isfinite(ask))       # an emptied book at the very end says nothing
    if not len(keep):
        return None, None
    ts, bid, ask = ts[:keep[-1] + 1], bid[:keep[-1] + 1], ask[:keep[-1] + 1]
    b, a = np.nan_to_num(bid, nan=-1.0), np.nan_to_num(ask, nan=2.0)
    won, lost = b >= 0.97, a <= 0.03
    last = len(ts) - 1
    if won[last] or (not np.isfinite(ask[last]) and b[last] >= 0.9):
        back = np.flatnonzero(~((b >= 0.9) | (bid != bid)))
        i = (back[-1] + 1) if len(back) else 0
        return 1, int(ts[min(i, last)])
    if lost[last] or (not np.isfinite(bid[last]) and a[last] <= 0.1):
        back = np.flatnonzero(~((a <= 0.1) | (ask != ask)))
        i = (back[-1] + 1) if len(back) else 0
        return 0, int(ts[min(i, last)])
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', default=None)
    ap.add_argument('--min', type=float, default=0.01)
    ap.add_argument('--maxage', type=float, default=120.0, help='both markets must have printed a row within this many seconds')
    ap.add_argument('--minshares', type=float, default=0.0)
    a = ap.parse_args()
    t0 = time.time()
    c = sqlite3.connect('file:' + a.db + '?mode=ro', uri=True, timeout=30)
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    # stretches where the whole side feed was silent
    allts = np.array([r[0] for r in c.execute('SELECT ts_ms FROM pm_sister_quotes ORDER BY id')], np.int64)
    allts.sort()
    gaps = np.flatnonzero(np.diff(allts) > 15_000)
    holes = [(int(allts[i]), int(allts[i + 1])) for i in gaps]
    say(f'side feed: {len(allts):,} rows, {len(holes)} silent stretches over 15 s ({sum(b - a_ for a_, b in holes) / 3.6e6:.1f} h of {(allts[-1] - allts[0]) / 3.6e6:.1f} h) ({time.time() - t0:.0f} s)')
    matches = [r[0] for r in c.execute("SELECT DISTINCT match_id FROM pm_sister_quotes WHERE kind = 'child_moneyline'")]
    episodes, windows, secs_checked = [], 0, 0
    kinds = {}
    for m in matches:
        info = c.execute('SELECT sport, home, away, start_ms FROM matches WHERE id = ?', (m,)).fetchone()
        if not info:
            continue
        sport, home, away, start = info
        rows = c.execute('SELECT ts_ms, kind, line, outcome, bid, ask, bid_size, ask_size FROM pm_sister_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)).fetchall()
        mk = {}
        for ts, kind, line, o, b, a_, bs, as_ in rows:
            mk.setdefault((kind, line, o), []).append((ts, b, a_, bs, as_))
        mk = {k: np.array([[np.nan if v is None else v for v in r] for r in v], float) for k, v in mk.items()}
        maps = {int(k[1]): (k[2], v) for k, v in mk.items() if k[0] == 'child_moneyline' and k[1] is not None}
        if not maps:
            continue
        # which side is the recorded token: the collector's home or away
        name = next(iter(maps.values()))[0]
        flip = len(norm(name) & norm(away)) > len(norm(name) & norm(home))

        def tok(arr):
            """Rows (ts, bid, ask, bid size, ask size) turned to the HOME side."""
            if not flip:
                return arr
            o = arr.copy()
            o[:, 1], o[:, 2], o[:, 3], o[:, 4] = 1 - arr[:, 2], 1 - arr[:, 1], arr[:, 4], arr[:, 3]
            return o
        maps = {k: tok(v[1]) for k, v in maps.items()}
        n_maps = max(maps)
        totals = {k[1]: v for k, v in mk.items() if k[0] == 'totals'}
        best_of = 5 if (n_maps >= 4 or any(l in totals for l in (3.5, 4.5))) else 3 if (n_maps >= 2 or 2.5 in totals) else 1
        need = (best_of + 1) // 2
        hcap = {}
        for k, v in mk.items():
            if k[0] == 'map_handicap':
                is_home = len(norm(k[2]) & norm(home)) >= len(norm(k[2]) & norm(away))
                hcap[(abs(k[1]), is_home)] = v          # token = "<that team> -line"
        pmr = c.execute("SELECT ts_ms, bid, ask, bid_size, ask_size FROM pm_quotes WHERE match_id = ? AND outcome = 'home' ORDER BY ts_ms", (m,)).fetchall()
        main_ = np.array([[np.nan if v is None else v for v in r] for r in pmr], float) if pmr else None
        # walk the series
        h = aw = 0
        t_from = start or int(min(v[0, 0] for v in maps.values()))
        for k in range(1, best_of + 1):
            if k not in maps or h >= need or aw >= need:
                break
            arr = maps[k]
            w, t_dec = decided_at(arr[:, 0], arr[:, 1], arr[:, 2])
            t_to = t_dec if t_dec is not None else int(arr[-1, 0])
            pairs = []                                   # (label, X rows, Y rows, Y inverted?)  X = Y (or X = 1 - Y)
            if h == aw == need - 1 and main_ is not None:
                pairs.append(('decider: match = map', main_, arr, False))
            lead_home = h == need - 1 and aw < need - 1
            lead_away = aw == need - 1 and h < need - 1
            if lead_home or lead_away:
                t_wins = aw if lead_home else h
                if (k + 0.5) in totals:                  # over k.5 = the trailing side wins map k
                    pairs.append((f'over {k}.5 = trailer wins map {k}', totals[k + 0.5], arr, lead_home))   # trailer = away when home leads: 1 - home token
                gap_ = need - t_wins
                if gap_ in (2, 3) and (gap_ - 0.5, lead_home) in hcap:
                    pairs.append((f'leader -{gap_ - 0.5} = leader wins map {k}', hcap[(gap_ - 0.5, lead_home)], arr, lead_away))  # leader away: 1 - home token
            if t_to - t_from >= 120_000:
                grid = np.arange(t_from + 30_000, t_to - 5_000, 1000, dtype=np.int64)
                in_hole = np.zeros(len(grid), bool)
                for a_, b_ in holes:
                    if b_ > grid[0] and a_ < grid[-1]:
                        in_hole |= (grid >= a_) & (grid <= b_ + 60_000)
                for label, Xr, Yr, inv in pairs:
                    Xv, Xage = ffill(Xr[:, 0].astype(np.int64), Xr[:, 1:], grid)
                    Yv, Yage = ffill(Yr[:, 0].astype(np.int64), Yr[:, 1:], grid)
                    if inv:
                        Yv = np.stack([1 - Yv[:, 1], 1 - Yv[:, 0], Yv[:, 3], Yv[:, 2]], 1)
                    live = (Xage <= a.maxage) & (Yage <= a.maxage) & ~in_hole & (Xv[:, 1] > Xv[:, 0]) & (Yv[:, 1] > Yv[:, 0])
                    # buy X at its ask + buy the other side of Y at 1 - bid(Y); and the reverse
                    for way, (ba, bb, sa, sb) in (('buy 1st, sell 2nd', (Xv[:, 1], Yv[:, 0], Xv[:, 3], Yv[:, 2])), ('buy 2nd, sell 1st', (Yv[:, 1], Xv[:, 0], Yv[:, 3], Xv[:, 2]))):
                        prof = bb - ba - fee(ba) - fee(1 - bb)
                        good = live & np.isfinite(prof) & (prof >= a.min) & (ba > 0.02) & (bb < 0.98)
                        d = np.diff(np.r_[0, good.astype(np.int8), 0])
                        for s_, e_ in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
                            if e_ - s_ >= 3 and (a.minshares <= 0 or np.nanmin(np.minimum(sa[s_ + 2:e_], sb[s_ + 2:e_])) >= a.minshares):
                                seg = slice(s_ + 2, e_)                  # we act 2 s after it appears
                                shares = float(np.nanmin(np.minimum(sa[seg], sb[seg]))) if np.isfinite(np.minimum(sa[seg], sb[seg])).any() else float('nan')
                                episodes.append((m, sport, label, way, int(grid[s_]), int(e_ - s_), float(np.nanmedian(prof[seg])), float(np.nanmin(prof[seg])), shares, float(np.nanmedian(ba[seg])), float(np.nanmedian(bb[seg]))))
                    windows += 1
                    secs_checked += int(live.sum())
                    kinds[label.split(' = ')[0].split(' ')[0]] = kinds.get(label.split(' = ')[0].split(' ')[0], 0) + 1
            if w is None:
                break
            h, aw = h + (w == 1), aw + (w == 0)
            t_from = t_dec
    say(f'{len(matches)} esports games with map markets; {windows} twin windows checked ({kinds}), {secs_checked / 3600:.1f} h of two-sided quotes ({time.time() - t0:.0f} s)')
    say(f'EPISODES (locked profit >= {a.min * 100:.0f}c a share after both fees, standing 3+ s; entered 2 s after it appears): {len(episodes)}')
    tot = 0.0
    for e in sorted(episodes, key=lambda e: e[4]):
        m, sport, label, way, t_, dur, pmed, pmin, sh, ba, bb = e
        usd = (0 if not np.isfinite(sh) else min(sh, 100 / max(ba + 1 - bb, 0.01))) * pmin
        tot += usd
        say(f'  {time.strftime("%d %b %H:%M:%S", time.gmtime(t_ / 1000))} UTC  game {m} {sport:<8} {label:<34} {way}: {dur:>4} s, locked {pmed * 100:4.1f}c (worst {pmin * 100:4.1f}c) a share, buy at {ba * 100:.0f}c sell at {bb * 100:.0f}c, {sh:.0f} shares on offer -> ${usd:.2f} on a $100 cap')
    say(f'TOTAL on a $100 cap per episode, at the worst price of each episode: ${tot:.2f} over {len(set(e[0] for e in episodes))} games')
    if a.out:
        with open(os.path.join(a.out, f'identity_age{int(a.maxage)}.txt'), 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
