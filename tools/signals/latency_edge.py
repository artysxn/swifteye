"""How much of a bookmaker-leads move is left after our delay? (python tools/signals/latency_edge.py --db X --signals DIR)

For every bookmaker-leads instance with a gap >= 4 pt (booklead.npz; t = when OUR collector received the bookmaker's
jump), entering L seconds later (L = 0 / 0.25 / 0.5 / 1 / 2 / 5): Polymarket's mid at the entry, the mid 5 min after
the signal, the half-spread + fee paid on the way in and out. GROSS = how far the mid moved our way after the entry
(cents a share, the part a model can predict); COST = half spreads + fees; NET = gross - cost. L = 0 is the best a
bot could do with this collector's feeds (the feeds themselves lag the bookmakers by 1.7-3.5 s: docs above).
Exploratory measurement over all days, not a strategy test."""
import argparse
import os
import sys
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import waves as W  # noqa: E402
from booklead import fee, held  # noqa: E402

LS = (0.0, 0.25, 0.5, 1.0, 2.0, 5.0)
H = 300


def game(args):
    path, m, insts = args
    db = W.connect(path)
    by = defaultdict(list)
    for t, o, b, a in db.execute('SELECT ts_ms, outcome, bid, ask FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)):
        by[o].append((t, np.nan if b is None else b, np.nan if a is None else a))
    out = []
    for j, o, t, d in insts:
        r = by.get(o)
        if not r:
            continue
        lt = np.array([x[0] for x in r], np.int64)
        lb = np.array([x[1] for x in r], float)
        la = np.array([x[2] for x in r], float)
        times = np.array([t + int(L * 1000) for L in LS] + [t - 1000, t + H * 1000])
        b, a = held(lt, lb, times), held(lt, la, times)
        mid, half = (b + a) / 2, (a - b) / 2
        if not np.all(np.isfinite(mid)):
            continue
        out.append((j, d, mid[:len(LS)], half[:len(LS)], mid[-2], mid[-1], half[-1]))
    return out


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=24)
    a = ap.parse_args()
    D = dict(np.load(os.path.join(a.signals, 'booklead.npz'), allow_pickle=False))
    oname = ['home', 'away']
    games = defaultdict(list)
    for j in np.flatnonzero(D['room'] >= 4):
        games[int(D['game'][j])].append((int(j), oname[D['outcome'][j]], int(D['t'][j]), int(D['dir'][j])))
    res = []
    with Pool(a.workers) as pool:
        for r in pool.imap_unordered(game, [(a.db, m, v) for m, v in games.items()], chunksize=4):
            res += r
    j = np.array([x[0] for x in res])
    d = np.array([x[1] for x in res], float)
    mid = np.array([x[2] for x in res])
    half = np.array([x[3] for x in res])
    pre = np.array([x[4] for x in res])
    m5 = np.array([x[5] for x in res])
    h5 = np.array([x[6] for x in res])
    room = D['room'][j]
    out = [f'{len(res)} bookmaker-leads instances with gap >= 4 pt; mid 5 min after the signal as the exit (no target), cents a share', '']
    for lab, sel in (('gap >= 4', room >= 4), ('gap >= 8', room >= 8), ('gap >= 15', room >= 15)):
        out.append(f'{lab} ({sel.sum()} instances). Polymarket had already moved {np.mean(d[sel] * (mid[sel, 0] - pre[sel])) * 100:+.2f}c our way between 1 s before and the signal:')
        out.append(f'   {"delay":>6} {"moved already":>14} {"gross left":>11} {"cost":>6} {"net":>7}')
        for k, L in enumerate(LS):
            tok = np.where(d[sel] > 0, mid[sel, k], 1 - mid[sel, k])
            gone = d[sel] * (mid[sel, k] - mid[sel, 0]) * 100
            gross = d[sel] * (m5[sel] - mid[sel, k]) * 100
            cost = (half[sel, k] + h5[sel] + fee(tok) + fee(np.clip(tok + gross / 100, 0.01, 0.99))) * 100
            out.append(f'   {L:>5.2f}s {gone.mean():>+13.2f}c {gross.mean():>+10.2f}c {cost.mean():>5.2f}c {(gross - cost).mean():>+6.2f}c')
        out.append('')
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'latency_edge.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
