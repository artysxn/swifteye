"""Build the wave-model datasets: python tools/signals/build_waves.py [--db PATH] [--workers 24] [--every 30]

<db dir>/signals/waves_A.npz: every decision second with the factor 1-2 targets (wave_targets.py).
<db dir>/signals/waves_B.npz: positions entered at every `every`-th decision second, both directions, seen
every 10 s while the plain policy holds them (factor 3)."""
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
import wave_targets as WT  # noqa: E402
import waves as W  # noqa: E402

TKEYS = ('up_soon', 'dn_soon', 'rem_t', 'rem_a', 'mfe_up', 't_up', 'mfe_dn', 't_dn', 'base_up', 'base_dn', 'hold_up', 'hold_dn', 'fee_rt')


def one(args):
    path, m, sport, start, every = args
    db = W.connect(path)
    pm, bk = W.load(db, m)
    try:   # databases from before 18 Nov 2026 have no order-book sizes
        book = db.execute('SELECT ts_ms, outcome, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id = ? AND bid_size IS NOT NULL ORDER BY ts_ms', (m,)).fetchall()
    except Exception:
        book = None
    A, B = [], []
    for k, o in enumerate(['home', 'away'] if sport in W.THREE_WAY else ['home']):
        g = F.build(pm, bk, o, sport, start, sport in W.THREE_WAY, book, full=True)
        if g is None:
            continue
        T = WT.wave_targets(g)
        rows = np.flatnonzero(g['evt'] & g['ok'] & np.isfinite(T['mfe_up']) & np.isfinite(T['mfe_dn']))
        if not len(rows):
            continue
        A.append({'t': g['ts'][rows], 'X': g['X'][rows], 'game': np.full(len(rows), m, np.int32), 'outcome': np.full(len(rows), k, np.int8),
                  **{key: T[key][rows] for key in TKEYS}})
        ti, di, ei, pos, now, cont = WT.exit_rows(g, rows[::every])
        if len(ti):
            B.append({'t': g['ts'][ti], 't_entry': g['ts'][ei], 'X': g['X'][ti], 'pos': pos, 'dir': di, 'now': now, 'cont': cont,
                      'game': np.full(len(ti), m, np.int32), 'outcome': np.full(len(ti), k, np.int8)})
    return A, B


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db')
    ap.add_argument('--workers', type=int, default=24)
    ap.add_argument('--every', type=int, default=30)
    ap.add_argument('--out', help='output folder (default <db dir>/signals)')
    a = ap.parse_args()
    path = a.db or W.default_db()
    gl = W.games(W.connect(path))
    print(f'{len(gl)} games', flush=True)
    parts = {'A': [], 'B': []}
    t0 = time.time()
    with Pool(a.workers) as pool:
        for k, (A, B) in enumerate(pool.imap_unordered(one, [(path, m, s, st, a.every) for m, s, _, _, st in gl], chunksize=2)):
            parts['A'] += A
            parts['B'] += B
            if (k + 1) % 200 == 0:
                print(f'  {k + 1} games, {time.time() - t0:.0f} s', flush=True)
    starts = {m: st or 0 for m, _, _, _, st in gl}
    sports = {m: s for m, s, *_ in gl}
    od = a.out or os.path.join(os.path.dirname(path), 'signals')
    os.makedirs(od, exist_ok=True)
    for name, ps in parts.items():
        d = {key: np.concatenate([p[key] for p in ps]) for key in ps[0]}
        d['start'] = np.array([starts[g] for g in d['game']], dtype=np.int64)
        d['sport'] = np.array([sports[g] for g in d['game']])
        d['names'] = np.array(F.NAMES)
        np.savez_compressed(os.path.join(od, f'waves_{name}.npz'), **d)
        print(f"waves_{name}: {len(d['t'])} rows from {len(np.unique(d['game']))} games")
    print(f'{time.time() - t0:.0f} s; written to {od}')


if __name__ == '__main__':
    main()
