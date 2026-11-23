"""Checks on improve.py's trades before believing them (python tools/signals/improve_check.py --db X --signals DIR).

For the baseline, exit-grid and all-three trades: the token's sell price at the exit (from pm_quotes), whether the
target was hit or the position was given up at H, trades where the game was already decided at the sale (sell price
<= 3c or >= 97c: that part is the result, which we cannot call), trades sold on a stale quote (no Polymarket row for
that outcome in the last 60 s before the sale, e.g. after the recording ended), how much the biggest trades carry,
per day, per sport, and the same entries valued with the other exit (exit alone vs entries alone)."""
import argparse
import json
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import waves as W  # noqa: E402
from booklead import L, held  # noqa: E402


def prices(args):
    path, m, items = args   # items: (key, outcome name, t, d, hold_s)
    db = W.connect(path)
    rows = db.execute('SELECT ts_ms, outcome, bid, ask FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)).fetchall()
    by = defaultdict(list)
    for t, o, b, a in rows:
        by[o].append((t, np.nan if b is None else b, np.nan if a is None else a))
    out = {}
    for key, o, t, d, hold in items:
        r = by.get(o)
        if not r:
            continue
        lt = np.array([x[0] for x in r], dtype=np.int64)
        lb = np.array([x[1] for x in r], dtype=float)
        la = np.array([x[2] for x in r], dtype=float)
        e, x = t + L * 1000, t + hold * 1000
        b_e, a_e = held(lt, lb, np.array([e]))[0], held(lt, la, np.array([e]))[0]
        b_x, a_x = held(lt, lb, np.array([x]))[0], held(lt, la, np.array([x]))[0]
        k = np.searchsorted(lt, x, side='right') - 1
        out[key] = (a_e if d > 0 else 1 - b_e, b_x if d > 0 else 1 - a_x, int(x - lt[k]) if k >= 0 else 10 ** 9)
    return out


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=16)
    a = ap.parse_args()
    D = dict(np.load(os.path.join(a.signals, 'booklead.npz'), allow_pickle=False))
    I = dict(np.load(os.path.join(a.signals, 'improve.npz'), allow_pickle=False))
    for k, v in I.items():
        if not k.startswith('heads'):
            D[k] = v
    P = json.load(open(os.path.join(a.signals, 'improve_trades.json'), encoding='utf-8'))
    oname = ['home', 'away']
    want = defaultdict(list)
    for var in ('base', 'exit', 'all'):
        for i, ek, w, day in P[var]:
            hold = int(D[f'hold_{ek}'][i])
            want[int(D['game'][i])].append(((var, i, ek), oname[D['outcome'][i]], int(D['t'][i]), int(D['dir'][i]), hold))
    px = {}
    with Pool(a.workers) as pool:
        for res in pool.imap_unordered(prices, [(a.db, m, v) for m, v in want.items()], chunksize=4):
            px.update(res)
    out = []
    for var in ('base', 'exit', 'all'):
        T = P[var]
        idx = np.array([x[0] for x in T])
        eks = [x[1] for x in T]
        w = np.array([x[2] for x in T])
        days = [x[3] for x in T]
        v = np.array([D[ek][i] for i, ek in zip(idx, eks)], float)
        H = np.array([int(ek.split('_')[1]) if ek != 'plain' else 180 for ek in eks])
        hold = np.array([int(D[f'hold_{ek}'][i]) for i, ek in zip(idx, eks)])
        gave_up = hold >= H + L
        sp = np.array([px.get((var, int(i), ek), (np.nan, np.nan, 10 ** 9)) for i, ek in zip(idx, eks)], float)
        decided = (sp[:, 1] <= 0.03) | (sp[:, 1] >= 0.97)
        stale = sp[:, 2] > 60_000
        srt = np.sort(v)
        k5 = max(1, len(v) // 20)
        top = np.sort(v)[::-1]
        out.append(f'{var}: {len(v)} trades, mean {v.mean():+.2f}c, median {np.median(v):+.2f}c, 5%-trimmed {srt[k5:-k5].mean():+.2f}c, '
                   f'top 10 trades carry {top[:10].sum():+.0f}c of {v.sum():+.0f}c; |net| > 20c: {np.mean(np.abs(v) > 20) * 100:.0f}%')
        out.append(f'   given up at H {gave_up.mean() * 100:.0f}% ({v[gave_up].mean():+.2f}c), target hit {(~gave_up).mean() * 100:.0f}% ({v[~gave_up].mean() if (~gave_up).any() else float("nan"):+.2f}c); '
                   f'median hold {np.median(hold):.0f} s')
        out.append(f'   game decided at the sale (<= 3c or >= 97c): {decided.sum()} trades ({v[decided].mean() if decided.any() else float("nan"):+.2f}c); '
                   f'without them {v[~decided].mean():+.2f}c over {(~decided).sum()}')
        out.append(f'   sold on a stale quote (> 60 s old): {stale.sum()} trades ({v[stale].mean() if stale.any() else float("nan"):+.2f}c)')
        bp = sp[:, 0]
        for lo, hi in ((0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)):
            m = (bp >= lo) & (bp < hi)
            if m.any():
                out.append(f'   bought at {lo * 100:.0f}-{min(hi, 1) * 100:.0f}c: {m.sum():>4} trades {v[m].mean():+.2f}c')
        for d in sorted(set(days)):
            m = np.array([x == d for x in days])
            out.append(f'   {d}: {m.sum():>4} trades {v[m].mean():+.2f}c (total {v[m].sum():+.0f}c)')
        sp_ = D['sport'][idx]
        out.append('   by sport: ' + ', '.join(f'{s} {np.sum(sp_ == s)} {v[sp_ == s].mean():+.2f}c' for s in np.unique(sp_)))
        for other in ('target_600', 'f150_1200'):
            o = np.array([D[other][i] for i in idx], float)
            ok = np.isfinite(o)
            out.append(f'   same entries with exit {other}: {o[ok].mean():+.2f}c over {ok.sum()} (as traded {v[ok].mean():+.2f}c)')
        out.append(f'   gap at entry: median {np.median(D["room"][idx]):.1f} pt; general model OK {np.mean(D["edge"][idx] >= D["th"][idx]) * 100:.0f}%')
        out.append('')
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'improve_check.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
