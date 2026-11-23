"""Polymarket leads, Kalshi lags: trade Kalshi after a Polymarket jump (python tools/signals/kalshi_lag.py --db X --signals DIR).

angles.py (study C) found Kalshi the one venue where the price 1 s after an upward Polymarket jump was still worth more
than it cost at the MID (+$3.84 per $100; Kalshi changed a median 11.8 s later). Here with Kalshi's real executable
quotes (kalshi_quotes est = 0: bid / ask / sizes / depth within 2c), Kalshi's taker fee 7% p(1-p) on both fills, and
Kalshi's own home / away flipped to the match's (matcher::names_swapped: never in football; else by name similarity).

EVENTS: Polymarket jumps in-play (mid >= 0.12 log-odds within 10 s, spread <= 4c before and at the jump, one per game /
outcome / direction per minute) on games with a Kalshi GAME market. DECISIONS: (1) 1 s after the jump, every jump; (5) 5 s
after, only jumps still at least half there (known by then). ELIGIBLE: Kalshi's mid has made < 30% of Polymarket's move.
TRADE: buy the Kalshi contract the jump's way at the ask (No = 1 - Yes bid), target = Polymarket's mid at the decision;
sell into Kalshi's bid when it gets there, else at 5 min; shares capped by Kalshi's depth on both sides; collapsed
contracts (no bid) sold at the first bid within 10 min, else 0. Dollars per $100 order.
CLEAN: per day 19-23 Nov, a filter (jump, Kalshi spread, decision time, exit) chosen on earlier days by total minus one
standard error, one position per game."""
import argparse
import datetime as dt
import difflib
import itertools
import os
import pickle
import re
import sys
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import waves as W  # noqa: E402
from booklead import held  # noqa: E402

DAY = 86_400_000
PRICES = os.environ.get('KALSHI_PRICES', 'ticker')   # 'book': the order book rebuilt from orderbook_delta (book_bid / book_ask)
logit = lambda p: np.log(np.clip(p, 0.005, 0.995) / (1 - np.clip(p, 0.005, 0.995)))
kfee = lambda p: 0.07 * p * (1 - p)


def fin(x):
    return float(x) if np.isfinite(x) else np.inf


def norm(s):
    s = re.sub(r'\(.*?\)', ' ', (s or '').lower())
    return ' '.join(w for w in re.findall(r'[a-z0-9]+', s) if w not in ('fc', 'cf', 'sc', 'the', 'club', 'esports', 'team'))


def swapped(sport, mh, ma, eh, ea):
    if sport == 'football' or not eh:
        return False
    sim = lambda a, b: difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()
    return sim(mh, ea) + sim(ma, eh) > sim(mh, eh) + sim(ma, ea)


def ktrade(buy, s_in, cap_in, sell, s_out, cap_out, taker_out=True):
    nn = min(100.0 / (buy + kfee(buy)), cap_in, cap_out)
    n1 = min(nn, s_in)
    cost = n1 * buy + (nn - n1) * (buy + 0.01) + nn * kfee(buy)
    if taker_out:
        m1 = min(nn, s_out)
        proceeds = m1 * sell + (nn - m1) * max(sell - 0.01, 0.0) - nn * kfee(sell)
    else:
        proceeds = nn * sell
    return proceeds - cost


def series(rows):
    arr = np.array([[x[0]] + [np.nan if v is None else v for v in x[1:]] for x in rows], dtype=float)
    return (arr[:, 0].astype(np.int64),) + tuple(arr[:, k] for k in range(1, arr.shape[1]))


def game(args):
    path, m = args
    db = W.connect(path)
    r = db.execute('SELECT sport, start_ms, home, away FROM matches WHERE id = ?', (m,)).fetchone()
    if not r or not r[1]:
        return []
    sport, start, mh, ma = r
    evs = [x for x in db.execute("SELECT book_event_id, home, away FROM book_events WHERE book = 'kalshi' AND match_id = ?", (m,)) if 'GAME' in x[0]]
    if not evs:
        return []
    pm = defaultdict(list)
    for x in db.execute('SELECT ts_ms, outcome, bid, ask FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)):
        pm[x[1]].append((x[0], x[2], x[3]))
    out = []
    for ev, eh, ea in evs[:1]:
        sw = swapped(sport, mh, ma, eh, ea)
        kq = defaultdict(list)
        cols = 'book_bid, book_ask' if PRICES == 'book' else 'bid, ask'
        for x in db.execute(f'SELECT ts_ms, outcome, {cols}, bid_size, ask_size, bid_depth, ask_depth FROM kalshi_quotes WHERE event = ? AND est = 0 ORDER BY ts_ms', (ev,)):
            o = x[1]
            if sw and o in ('home', 'away'):
                o = 'away' if o == 'home' else 'home'
            kq[o].append((x[0],) + tuple(x[2:]))
        for o in ('home', 'away'):
            if len(pm.get(o, [])) < 50 or len(kq.get(o, [])) < 50:
                continue
            lt, lb, la = series(pm[o])
            kt, kb, ka, kbs, kas, kbd, kad = series(kq[o])
            t0, t1 = max(int(lt[0]), int(kt[0]), start), min(int(lt[-1]), int(kt[-1]), start + 4 * 3_600_000)
            if t1 - t0 < 600_000:
                continue
            # data check, not a result: the two venues' prices of the same outcome must move together, else the
            # outcome mapping (Kalshi's own home / away) is wrong for this game
            g60 = np.arange(t0, t1, 60_000)
            pmm = (held(lt, lb, g60) + held(lt, la, g60)) / 2
            km = (held(kt, kb, g60) + held(kt, ka, g60)) / 2
            okc = np.isfinite(pmm) & np.isfinite(km)
            if okc.sum() < 20 or np.corrcoef(pmm[okc], km[okc])[0, 1] < 0.8:
                continue
            grid = np.arange(t0, t1, 1000)
            gb, ga = held(lt, lb, grid), held(lt, la, grid)
            mid, gsp = (gb + ga) / 2, ga - gb
            x = logit(mid)
            dx = np.full(len(x), np.nan)
            dx[10:] = x[10:] - x[:-10]
            last = {}
            for i in np.flatnonzero(np.abs(np.nan_to_num(dx)) >= 0.12):
                if i < 30 or i + 5 >= len(x) or abs(np.nan_to_num(dx[i - 1])) >= 0.12 or not (gsp[i] <= 0.04 and gsp[i - 10] <= 0.04):
                    continue
                d = 1 if dx[i] > 0 else -1
                t = int(grid[i])
                if t - last.get(d, -10 ** 15) < 60_000 or t + 1_000_000 > t1:
                    continue
                last[d] = t
                persistent = d * (x[i + 5] - x[i - 10]) >= 0.5 * abs(dx[i])
                size = abs(mid[i] - mid[i - 10])
                for lag in (1, 5):
                    if lag == 5 and not persistent:
                        continue
                    td = t + lag * 1000
                    pm_now = (held(lt, lb, np.array([td]))[0] + held(lt, la, np.array([td]))[0]) / 2
                    times = td + 1000 * np.arange(0, 901)
                    yb, ya, ybs, yas, ybd, yad = (held(kt, v, times) for v in (kb, ka, kbs, kas, kbd, kad))
                    k_pre = (held(kt, kb, np.array([t - 10_000]))[0] + held(kt, ka, np.array([t - 10_000]))[0]) / 2
                    if d > 0:
                        tb, ta, tbs, tbd, tas, tad, target = yb, ya, ybs, ybd, yas, yad, pm_now
                    else:
                        tb, ta, tbs, tbd, tas, tad, target = 1 - ya, 1 - yb, yas, yad, ybs, ybd, 1 - pm_now
                    kmid = (yb + ya) / 2
                    if not (np.isfinite(ta[0]) and np.isfinite(tb[0]) and 0.01 < ta[0] < 0.99 and np.isfinite(k_pre) and np.isfinite(pm_now)):
                        continue
                    k_followed = d * (kmid[0] - k_pre) / max(size, 1e-3)
                    rec = {'game': m, 't': t, 'day': dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime('%Y-%m-%d'), 'sport': sport, 'lag': lag,
                           'jump': float(abs(dx[i])), 'size': float(size) * 100, 'kspread': float(ta[0] - tb[0]) * 100, 'kbuy': float(ta[0]),
                           'k_followed': float(k_followed), 'room': float(target - ta[0]) * 100, 'swap': sw}
                    for h in (5, 30, 60, 300):
                        rec[f'g{h}'] = float(d * (kmid[h] - kmid[0]) * 100) if np.isfinite(kmid[h]) else np.nan
                    buy, s_in, cap_in = float(ta[0]), fin(tas[0]), max(fin(tad[0]), fin(tas[0]))
                    for lab, mk in (('usdT', False), ('usdM', True)):
                        seg = np.nan_to_num(tb[1:301], nan=-1.0)
                        hit = np.flatnonzero(seg >= (target + 0.01 - 1e-9 if mk else target - 1e-9))
                        if len(hit):
                            k = 1 + int(hit[0])
                            sell = float(target) if mk else float(tb[k])
                            rec[lab] = ktrade(buy, s_in, cap_in, sell, fin(tbs[k]), max(fin(tbd[k]), fin(tbs[k])), taker_out=not mk)
                        else:
                            f = np.flatnonzero(np.isfinite(tb[300:]))
                            if len(f):
                                j = 300 + int(f[0])
                                rec[lab] = ktrade(buy, s_in, cap_in, float(tb[j]), fin(tbs[j]), max(fin(tbd[j]), fin(tbs[j])))
                            else:
                                rec[lab] = ktrade(buy, s_in, cap_in, 0.0, np.inf, np.inf)
                    out.append(rec)
    return out


def cautious(v):
    v = np.asarray(v, float)
    return v.sum() - v.std() * np.sqrt(len(v)) if len(v) > 1 else -np.inf


def place(rows):
    busy, out = {}, []
    for r in sorted(rows, key=lambda r: r['t']):
        if r['t'] < busy.get(r['game'], -1):
            continue
        busy[r['game']] = r['t'] + 300_000
        out.append(r)
    return out


def stat(rows, k):
    v = np.array([r[k] for r in rows], float)
    v = v[np.isfinite(v)]
    return f'${v.mean():+6.2f} per $100 (n {len(v)}, win {np.mean(v > 0) * 100:.0f}%)' if len(v) else 'n 0'


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=24)
    a = ap.parse_args()
    db = W.connect(a.db)
    games = [r[0] for r in db.execute("SELECT DISTINCT match_id FROM book_events WHERE book = 'kalshi' AND match_id IN (SELECT match_id FROM book_events WHERE book = 'polymarket')")]
    print(f'{len(games)} games on both Polymarket and Kalshi', flush=True)
    R = []
    with Pool(a.workers) as pool:
        for k, r in enumerate(pool.imap_unordered(game, [(a.db, m) for m in games], chunksize=2)):
            R += r
            if (k + 1) % 200 == 0:
                print(f'  {k + 1} games, {len(R)} decisions', flush=True)
    with open(os.path.join(a.signals, f'kalshi_lag_{PRICES}.pkl'), 'wb') as f:
        pickle.dump(R, f)
    out = [f'[Kalshi prices: {PRICES}] Polymarket jump -> trade Kalshi ({len(games)} games on both; Kalshi real quotes, 7% p(1-p) fee both fills, depth), dollars per $100 order', '']
    for lag in (1, 5):
        L = [r for r in R if r['lag'] == lag]
        el = [r for r in L if r['k_followed'] < 0.3]
        out.append(f'Decision {lag} s after the jump{" (only jumps still there at +5 s)" if lag == 5 else ""}: {len(L)} jumps, Kalshi had not followed yet in {len(el) / max(len(L), 1) * 100:.0f}%')
        if not el:
            continue
        out.append(f'   Kalshi mid our way after 5 s / 30 s / 1 min / 5 min: ' + ' / '.join(f'{np.nanmean([r[f"g{h}"] for r in el]):+.2f}' for h in (5, 30, 60, 300)) +
                   f' pt; PM jump median {np.median([r["size"] for r in el]):.1f} pt; Kalshi spread median {np.median([r["kspread"] for r in el]):.1f}c')
        out.append(f'   all eligible: sell into the bid {stat(el, "usdT")}; limit sale {stat(el, "usdM")}')
        for name, pred in (('jump >= 0.25', lambda r: r['jump'] >= 0.25), ('jump >= 0.5', lambda r: r['jump'] >= 0.5), ('PM jump >= 5 pt', lambda r: r['size'] >= 5),
                           ('PM jump >= 10 pt', lambda r: r['size'] >= 10), ('Kalshi spread <= 1c', lambda r: r['kspread'] <= 1.01), ('Kalshi spread <= 2c', lambda r: r['kspread'] <= 2.01),
                           ('room >= 5c', lambda r: r['room'] >= 5), ('room >= 10c', lambda r: r['room'] >= 10), ('Kalshi 20-80c', lambda r: 0.2 <= r['kbuy'] <= 0.8),
                           ('football', lambda r: r['sport'] == 'football'), ('basketball', lambda r: r['sport'] == 'basketball'), ('tennis', lambda r: r['sport'] == 'tennis'),
                           ('esports', lambda r: r['sport'] in ('cs2', 'dota2', 'valorant', 'lol')), ('hockey', lambda r: r['sport'] == 'hockey'),
                           ('room >= 5c + spread <= 2c', lambda r: r['room'] >= 5 and r['kspread'] <= 2.01), ('room >= 10c + spread <= 2c', lambda r: r['room'] >= 10 and r['kspread'] <= 2.01)):
            sub = [r for r in el if pred(r)]
            if len(sub) >= 30:
                out.append(f'     {name:<28} sell into the bid {stat(sub, "usdT")}; limit {stat(sub, "usdM")}; Kalshi moved {np.nanmean([r["g60"] for r in sub]):+.2f} pt in 1 min')
        out.append('')
    gridK = [(f'lag {lg} room>={rm} kspread<={ks} jump>={jp}', (lambda lg, rm, ks, jp: lambda r: r['lag'] == lg and r['k_followed'] < 0.3 and r['room'] >= rm and r['kspread'] <= ks and r['jump'] >= jp)(lg, rm, ks, jp))
             for lg, rm, ks, jp in itertools.product((1, 5), (2, 5, 10), (1.01, 2.01, 4.01), (0.12, 0.25, 0.5))]
    out.append('CLEAN, day by day (filter and exit chosen on earlier days):')
    taken = []
    for day in range(19, 24):
        dd = f'2026-11-{day:02d}'
        d0 = int(dt.datetime(2026, 11, day, tzinfo=dt.timezone.utc).timestamp() * 1000)
        before = [r for r in R if r['t'] + 1_000_000 < d0]
        today = [r for r in R if r['day'] == dd]
        best = None
        for (fname, pred), vk in itertools.product(gridK, ('usdT', 'usdM')):
            tk = place([r for r in before if pred(r) and np.isfinite(r[vk])])
            if len(tk) < 30:
                continue
            sc = cautious([r[vk] for r in tk])
            if sc > 0 and (best is None or sc > best[0]):
                best = (sc, fname, pred, vk, len(tk), sum(r[vk] for r in tk))
        if best is None:
            out.append(f'   {day} Nov: nothing made money before (cautious): no trades')
            continue
        _, fname, pred, vk, nb, tb_ = best
        tk = place([r for r in today if pred(r) and np.isfinite(r[vk])])
        taken += [(r['game'], r[vk], r['day']) for r in tk]
        out.append(f'   {day} Nov: {fname}, {vk} (before: {nb} trades ${tb_:+.0f}): {len(tk)} trades ${sum(r[vk] for r in tk):+.0f}')
    if taken:
        v = np.array([x[1] for x in taken])
        g = np.array([x[0] for x in taken])
        ug, inv = np.unique(g, return_inverse=True)
        gs = np.bincount(inv, v)
        rng = np.random.default_rng(6)
        boot = [gs[rng.integers(0, len(ug), len(ug))].sum() for _ in range(4000)]
        out.append(f'   pooled: {len(v)} trades ${v.sum():+,.0f} (90% {np.quantile(boot, 0.05):+,.0f}..{np.quantile(boot, 0.95):+,.0f}), ${v.mean():+.2f} per $100, win {np.mean(v > 0) * 100:.0f}%')
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, f'kalshi_lag_{PRICES}.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
