"""Build the signal dataset over every recorded game: python tools/signals/build.py [--db PATH] [--workers 24]

Writes <db dir>/signals/dataset.npz: the decision seconds (a line changed) of every (game, outcome) with
features X, labels y (up, down), nets, trade lengths, and game / time / sport for walk-forward splits."""
import argparse
import os
import sys
import time
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import features as F  # noqa: E402
import waves as W  # noqa: E402


def one(args):
    path, m, sport, start, lab = args
    F.TP, F.STOP, F.HOR = lab   # the label's barriers (set here: workers import features afresh)
    db = W.connect(path)
    pm, bk = W.load(db, m)
    book = db.execute('SELECT ts_ms, outcome, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND bid_size IS NOT NULL ORDER BY ts_ms', (m,)).fetchall()
    out = []
    for k, o in enumerate(['home', 'away'] if sport in W.THREE_WAY else ['home']):
        r = F.build(pm, bk, o, sport, start, sport in W.THREE_WAY, book)
        if r is None:
            continue
        ts, X, y, net, evt, hold = r
        e = np.where(evt)[0]
        if not len(e):
            continue
        out.append((m, k, ts[e], X[e], y[e], net[e], hold[e]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db')
    ap.add_argument('--workers', type=int, default=24)
    ap.add_argument('--tp', type=float, default=F.TP, help='take-profit, net per share after fees')
    ap.add_argument('--stop', type=float, default=F.STOP, help='stop: fall of the sell price from the buy')
    ap.add_argument('--horizon', type=int, default=F.HOR, help='s')
    ap.add_argument('--out', default='dataset.npz')
    a = ap.parse_args()
    path = a.db or W.default_db()
    gl = W.games(W.connect(path))
    print(f'{len(gl)} games', flush=True)
    parts = {k: [] for k in ('t', 'X', 'y', 'net', 'hold', 'game', 'outcome', 'start')}
    t0 = time.time()
    with Pool(a.workers) as pool:
        for k, res in enumerate(pool.imap_unordered(one, [(path, m, s, st, (a.tp, a.stop, a.horizon)) for m, s, _, _, st in gl], chunksize=2)):
            for m, o, ts, X, y, net, hold in res:
                parts['t'].append(ts)
                parts['X'].append(X)
                parts['y'].append(y)
                parts['net'].append(net)
                parts['hold'].append(hold)
                parts['game'].append(np.full(len(ts), m, dtype=np.int32))
                parts['outcome'].append(np.full(len(ts), o, dtype=np.int8))
            if (k + 1) % 100 == 0:
                print(f'  {k + 1} games, {sum(len(x) for x in parts["t"])} rows, {time.time() - t0:.0f} s', flush=True)
    starts = {m: st for m, _, _, _, st in gl}
    data = {k: np.concatenate(v) for k, v in parts.items() if v}
    data['start'] = np.array([starts[g] or 0 for g in data['game']], dtype=np.int64)
    sports = {m: s for m, s, *_ in gl}
    data['sport'] = np.array([sports[g] for g in data['game']])
    data['names'] = np.array(F.NAMES)
    data['label'] = np.array([a.tp, a.stop, a.horizon], dtype=float)
    od = os.path.join(os.path.dirname(path), 'signals')
    os.makedirs(od, exist_ok=True)
    np.savez_compressed(os.path.join(od, a.out), **data)
    y = data['y']
    print(f"{len(y)} rows from {len(np.unique(data['game']))} games, {data['X'].shape[1]} features; wins up {y[:, 0].mean():.3f} down {y[:, 1].mean():.3f}; {time.time() - t0:.0f} s")
    print(f"written to {od}")


if __name__ == '__main__':
    main()
