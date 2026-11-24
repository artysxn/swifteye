"""Who is right about the WINNER when Polymarket and the bookmakers disagree on the level? (23 Nov, Mac.)

Every earlier test asked "does Polymarket follow within minutes" and paid a round trip (3.3-4.5c). This one asks
whether a level gap is VALUE: hold to the result, so one fill, one fee (1.75c at 50c, under 1c at 90c+).

Rows: lab_sec.npz (in-play decision seconds, lab.py build). gap = mean over bookmaker families of (their margin-free
log-odds - Polymarket's mid log-odds) = X['fam_gap_mean']. Winner per game from Polymarket's last quotes (bid 97c+ =
won; ask 3c- = lost; else the other token of a two-way game; else the game is left out and counted).

1. HOW MUCH OF A GAP IS REAL: beta in  (later price - Polymarket now) = beta x (bookmakers - Polymarket now),
   prices in probability, later = 10 min, 30 min, the result (a game that ended sooner is valued at its result, never
   dropped). beta 0 = Polymarket was right, 1 = the bookmakers were. Games weigh the same; ranges resample games.
2. THE BET: per game the first second (after 10 live minutes) where the gap has stood at g or more for 2 minutes
   (CALM: Polymarket and the superline within 0.05 log-odds over the last minute, Polymarket within 0.08 over 3, not stale,
   2+ families quoting, spread <= 3c, token 5-95c): buy $100 toward the bookmakers at the ask + fee, hold to the
   result. Next to it: a resting buy at the bid with no fee (IF it fills: an upper bound), and the same moments bet
   AGAINST the bookmakers (the control: both lose the cost if the gap means nothing).

usage: python tools/signals/value.py --db ~/swifteye/oddsfeed.db --signals ~/swifteye/research/lab
"""
import argparse
import os
import sqlite3
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import mine as MN  # noqa: E402

C = MN.C
sig = lambda v: 1 / (1 + np.exp(-v))
WINNERS_DIR = None        # set to the signals folder to reuse results.json


def outcome_index(G, T):
    """lab_sec has no outcome column: a game's rows are one block per outcome (home, then away for three-way games), each
    in time order, so a step back in time inside a game starts the next outcome. Returns 0 / 1 per row."""
    k = np.zeros(len(G), np.int64)
    new_game = np.r_[True, G[1:] != G[:-1]]
    back = np.r_[False, (T[1:] < T[:-1]) & ~new_game[1:]]
    run = np.cumsum(back)
    k = run - np.maximum.accumulate(np.where(new_game, run, 0))
    return k


def last_quotes(c, m, depth=600):
    """{outcome: (last bid seen, last ask seen)} from the game's newest rows (one indexed read per game)."""
    q = {}
    for o, b, a in c.execute('SELECT outcome, bid, ask FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms DESC LIMIT ?', (m, depth)):
        cb, ca = q.get(o, (None, None))
        q[o] = (b if cb is None else cb, a if ca is None else ca)
    return q


def verdict(q, o):
    """1 = this outcome won, 0 = lost, None = not decided in the recording."""
    def one(ba):
        b, a = ba
        if b is not None and b >= 0.97:
            return 1
        if a is not None and a <= 0.03:
            return 0
        return None
    v = one(q.get(o, (None, None)))
    if v is None:
        others = [one(q[x]) for x in q if x != o]
        if any(x == 1 for x in others):
            v = 0                                       # another outcome won
        elif len(q) == 2 and others and others[0] == 0:
            v = 1                                       # two-way game and the other token lost
    return v


def results(db, signals):
    """{match: (home won, away won)} (1 / 0 / None) for every Polymarket game; cached in <signals>/results.json."""
    import json
    path = os.path.join(signals, 'results.json')
    if os.path.exists(path):
        return {int(k): tuple(v) for k, v in json.load(open(path)).items()}
    c = sqlite3.connect('file:' + db + '?mode=ro', uri=True, timeout=30)
    res = {}
    for (m,) in c.execute("SELECT DISTINCT match_id FROM book_events WHERE book = 'polymarket'").fetchall():
        q = last_quotes(c, m)
        if q:
            res[m] = (verdict(q, 'home'), verdict(q, 'away'))
    json.dump({str(k): list(v) for k, v in res.items()}, open(path, 'w'))
    return res


def winners(db, keys):
    """{key: 1 won / 0 lost} for the outcome each key stands for (key = match * 4 + k; k 0 = home, 1 = away)."""
    out, left = {}, 0
    res = results(db, WINNERS_DIR) if WINNERS_DIR else None
    c = None if res is not None else sqlite3.connect('file:' + db + '?mode=ro', uri=True, timeout=30)
    cache = {}
    for key in keys:
        m, k = int(key) // 4, int(key) % 4
        if res is not None:
            v = res.get(m, (None, None))[k]
        else:
            if m not in cache:
                cache[m] = last_quotes(c, m)
            v = verdict(cache[m], 'home' if k == 0 else 'away')
        if v is None:
            left += 1
        else:
            out[int(key)] = v
    return out, left


def wls_beta(d, g, w):
    return float((w * d * g).sum() / max((w * g * g).sum(), 1e-12))


def boot(games, fn, n=400, seed=0):
    """90% range of fn(mask-free index array) resampling whole games."""
    rng = np.random.default_rng(seed)
    ug, inv = np.unique(games, return_inverse=True)
    order = np.argsort(inv, kind='stable')
    starts = np.searchsorted(inv[order], np.arange(len(ug) + 1))
    vals = []
    for _ in range(n):
        pick = rng.integers(0, len(ug), len(ug))
        idx = np.concatenate([order[starts[i]:starts[i + 1]] for i in pick])
        vals.append(fn(idx))
    return np.percentile(vals, [5, 95])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--fresh', action='store_true', help='only rows where our Polymarket recording was live: newest row <= 15 s old, feed <= 1 s late')
    a = ap.parse_args()
    t0 = time.time()
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    S = np.load(os.path.join(a.signals, 'lab_sec.npz'))
    T, G, SP = S['t'], S['game'].astype(np.int64), S['sport']
    G = G * 4 + outcome_index(G, T)                                            # one key per game and outcome
    cols = ['x', 'spread', 'fam_gap_mean', 'fam_n', 'pm_wave', 'sl_wave', 'pm_r60', 'sl_r60', 'lead_stale', 'min_from_start', 'pm_r180', 'three_way']
    X = S['X']
    V = {k: X[:, C[k]].astype(np.float64) for k in cols}
    del X
    global WINNERS_DIR
    WINNERS_DIR = a.signals
    win, left = winners(a.db, np.unique(G))
    y = np.array([win.get(int(g), -1) for g in G], np.float64)
    ok = (y >= 0) & np.isfinite(V['fam_gap_mean']) & (V['fam_n'] >= 1)
    if a.fresh:
        import paths as PT
        age, late = PT.row_quality(a.signals, G, T)
        live_rec = (age <= 15) & (late <= 1000)
        print(f'recording live (newest Polymarket row <= 15 s old, feed <= 1 s late) on {live_rec.mean() * 100:.0f}% of rows; by day: ' + ', '.join(f'{d_}: {live_rec[(T // 86_400_000) == d_].mean() * 100:.0f}%' for d_ in np.unique(T // 86_400_000)), flush=True)
        ok &= live_rec
    say(f'{len(T):,} in-play rows, {len(np.unique(G)):,} game outcomes; winner known for {len(win):,}, not decided in the recording {left:,} ({time.time() - t0:.0f} s)')
    order = np.lexsort((T, G))
    T, G, SP, y, ok = T[order], G[order], SP[order], y[order], ok[order]
    V = {k: v[order] for k, v in V.items()}
    p = sig(V['x'])
    pb = sig(V['x'] + V['fam_gap_mean'])
    gap_p = pb - p
    # the price H later (same game), the result when the game ended sooner
    first = np.r_[0, np.flatnonzero(G[1:] != G[:-1]) + 1]
    last_ = np.r_[first[1:], len(G)]
    end_of = np.repeat(last_, last_ - first)

    gi = np.unique(G, return_inverse=True)[1]                                  # rows are sorted by (game, t)
    cnt = np.bincount(gi)
    K = gi.astype(np.int64) * 10 ** 13 + T                                     # one sorted key: game, then time

    def later(H):
        j = np.searchsorted(K, K + H, side='left')
        inside = j < end_of
        return np.where(inside, p[np.minimum(j, len(p) - 1)], y)
    calm = ok & (np.abs(V['pm_r60']) < 0.05) & (np.abs(V['pm_r180']) < 0.08) & (np.abs(V['sl_r60']) < 0.05) & (V['lead_stale'] < 0.5) \
        & (V['fam_n'] >= 2) & (V['spread'] <= 3) & (p > 0.05) & (p < 0.95)

    say()
    say('1. HOW MUCH OF A GAP IS REAL (beta: 0 = Polymarket was right, 1 = the bookmakers were; 90% range by game)')
    for name, mask in (('every in-play second', ok), ('calm seconds only', calm), ('calm, gap 3+ pt', calm & (np.abs(gap_p) >= 0.03)), ('calm, gap 5+ pt', calm & (np.abs(gap_p) >= 0.05)),
                       ('calm, gap 3+ pt, tennis', calm & (np.abs(gap_p) >= 0.03) & (SP == 'tennis')), ('calm, gap 3+ pt, football', calm & (np.abs(gap_p) >= 0.03) & (SP == 'football')),
                       ('calm, gap 3+ pt, basketball', calm & (np.abs(gap_p) >= 0.03) & (SP == 'basketball')), ('calm, gap 3+ pt, cs2', calm & (np.abs(gap_p) >= 0.03) & (SP == 'cs2'))):
        idx = np.flatnonzero(mask)
        if len(idx) < 500:
            say(f'  {name:<30} too few rows ({len(idx)})')
            continue
        n_in = np.bincount(gi[idx], minlength=len(cnt)).astype(float)
        w = 1.0 / n_in[gi[idx]]
        line = f'  {name:<30} {len(idx):>9,} rows / {int((n_in > 0).sum()):>5,} games, median gap {np.median(np.abs(gap_p[idx])) * 100:4.1f} pt:'
        for H, hn in ((600_000, '10 min'), (1_800_000, '30 min'), (None, 'result')):
            fut = later(H)[idx] if H else y[idx]
            d, g = fut - p[idx], gap_p[idx]
            b = wls_beta(d, g, w)
            lo, hi = boot(G[idx], lambda k: wls_beta(d[k], g[k], w[k]), n=300)
            line += f'  {hn} {b:+.2f} ({lo:+.2f}..{hi:+.2f})'
        say(line)

    say()
    say('2. THE BET: first calm second per game (after 10 live minutes) where the gap stood at g+ for 2 min; $100, held to the result')
    say('   $ per $100 [90% range by game], win rate | resting buy at the bid, no fee, IF filled | bet AGAINST the bookmakers (control)')
    live_ok = V['min_from_start'] * 60 >= 10
    sgn = np.sign(V['fam_gap_mean'])
    for g_min in (0.02, 0.03, 0.05, 0.08):
        cand = calm & live_ok & (np.abs(gap_p) >= g_min)
        # stood for 2 minutes: every row of the game in the last 120 s has the same sign and at least half the size, and one row is 60+ s old
        j0 = np.searchsorted(K, K - 120_000, side='left')
        weak = (np.abs(gap_p) < g_min / 2).astype(np.int64)
        cs_weak = np.r_[0, np.cumsum(weak)]
        pos, neg = np.r_[0, np.cumsum(sgn > 0)], np.r_[0, np.cumsum(sgn < 0)]
        i1 = np.arange(len(T)) + 1
        same = np.where(sgn > 0, neg[i1] - neg[j0] == 0, pos[i1] - pos[j0] == 0)
        strong = cs_weak[i1] - cs_weak[j0] == 0
        old = (T - T[np.minimum(j0, len(T) - 1)] >= 60_000) & (j0 >= first[gi])
        cand &= same & strong & old
        idx = np.flatnonzero(cand)
        if not len(idx):
            say(f'  gap {g_min * 100:.0f}+ pt: no bets')
            continue
        keep = idx[np.r_[True, G[idx][1:] != G[idx][:-1]]]                     # the first one per game
        side = sgn[keep]
        ps = np.where(side > 0, p[keep], 1 - p[keep])                          # mid of the token bought
        ys = np.where(side > 0, y[keep], 1 - y[keep])
        hs = V['spread'][keep] / 200.0
        rows = []
        for label, paid, fee, yy in (('toward', ps + hs, 0.05 * (ps + hs) * (1 - ps - hs), ys), ('resting', ps - hs, 0.0, ys),
                                     ('against', (1 - ps) + hs, 0.05 * (1 - ps + hs) * (ps - hs), 1 - ys)):
            pay = 100 * (yy - paid - fee) / paid
            lo, hi = boot(G[keep], lambda k: pay[k].mean(), n=1000)
            rows.append(f'${pay.mean():+.2f} [{lo:+.2f}..{hi:+.2f}], win {yy.mean() * 100:.0f}%')
        say(f'  gap {g_min * 100:.0f}+ pt: {len(keep):>5,} games, token {np.median(ps) * 100:.0f}c (median) | toward {rows[0]} | resting {rows[1]} | against {rows[2]}')
        if g_min == 0.03:
            for s_ in ('tennis', 'football', 'basketball', 'cs2', 'hockey'):
                k = np.flatnonzero(SP[keep] == s_)
                if len(k) >= 30:
                    paid = ps[k] + hs[k]
                    pay = 100 * (ys[k] - paid - 0.05 * paid * (1 - paid)) / paid
                    lo, hi = boot(G[keep][k], lambda q: pay[q].mean(), n=1000)
                    say(f'      {s_:<11} {len(k):>5,} games: toward ${pay.mean():+.2f} [{lo:+.2f}..{hi:+.2f}], win {ys[k].mean() * 100:.0f}% at {np.mean(paid) * 100:.0f}c paid')
    with open(os.path.join(a.signals, 'value_fresh.txt' if a.fresh else 'value.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    print(f'done in {time.time() - t0:.0f} s')


if __name__ == '__main__':
    main()
