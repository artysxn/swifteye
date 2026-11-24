"""Every game's Polymarket price second by second, in a few flat files that open instantly (24 Nov night).

  python tools/signals/paths.py build --db ~/swifteye/oddsfeed.db --out ~/swifteye/research/lab [--workers 6]

One block per game outcome (home; home and away for three-way games), on a 1 s grid from 10 minutes before the start
to the last quote:
  bid, ask         best prices, carried forward (nan = that side of the book empty)
  bsz, asz         shares at the best bid / ask (nan before sizes were recorded, 18 Nov)
  prt              a TRADE PRINT in that second: the last-trade price when it changed during the second (else nan)
  late             Polymarket feed lateness of the newest row (our receive time - Polymarket's own stamp, ms)
  age              seconds since the game's newest Polymarket row of ANY outcome. Our recording has holes (a game can
                   stop updating for minutes or hours while it trades on); a carried-forward quote older than
                   ~30 s in play is not a price. fresh(A, o, n) = age <= 30.
paths_meta.npz: key (match * 4 + k), match, k, sport, start (ms), t0 (ms of the block's first second), off, n,
won (1 / 0 / -1 unknown, from value.results). load(dir) returns the meta and memory-mapped arrays.
"""
import argparse
import os
import sqlite3
import sys
import time
from multiprocessing import Pool

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
FIELDS = ('bid', 'ask', 'bsz', 'asz', 'prt', 'late', 'age')
THREE = {'football', 'handball'}


def one(args):
    db, m, sport, start = args
    c = sqlite3.connect('file:' + db + '?mode=ro', uri=True, timeout=30)
    rows = c.execute('SELECT ts_ms, server_ts_ms, outcome, bid, ask, last, bid_size, ask_size FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)).fetchall()
    out = []
    all_ts = np.array([x[0] for x in rows], np.int64)
    for k, o in enumerate(['home', 'away'] if sport in THREE else ['home']):
        r = [x for x in rows if x[2] == o]
        if len(r) < 50:
            continue
        ts = np.array([x[0] for x in r], np.int64)
        f = lambda i: np.array([np.nan if x[i] is None else x[i] for x in r], np.float64)
        srv, bid, ask, last, bs, as_ = f(1), f(3), f(4), f(5), f(6), f(7)
        t0 = max(int(ts[0]), (start or int(ts[0])) - 600_000)
        t0 -= t0 % 1000
        n = int((ts[-1] - t0) // 1000) + 1
        if n < 600 or n > 60_000:
            continue
        grid = t0 + 1000 * np.arange(n, dtype=np.int64)
        j = np.searchsorted(ts, grid + 999, side='right') - 1            # the newest row inside or before each second
        has = j >= 0
        jj = np.clip(j, 0, None)
        g = lambda v: np.where(has, v[jj], np.nan).astype(np.float32)
        chg = np.r_[False, (last[1:] != last[:-1]) & np.isfinite(last[1:])]      # the last-trade price changed on this row = a trade
        prt = np.full(n, np.nan, np.float32)
        ci = np.flatnonzero(chg & (ts >= t0))
        prt[((ts[ci] - t0) // 1000).astype(np.int64)] = last[ci]
        ja = np.searchsorted(all_ts, grid + 999, side='right') - 1
        age = np.where(ja >= 0, (grid + 999 - all_ts[np.clip(ja, 0, None)]) / 1000.0, 1e6).astype(np.float32)
        out.append((m * 4 + k, m, k, sport, start or 0, t0, n, g(bid), g(ask), g(bs), g(as_), prt, g(ts - srv), age))
    return out


def book(A, o, n, won, maxage=30.0):
    """One block's bid, ask, mid (a decided one-sided book is valued: no bid and ask <= 5c -> 0; no ask and bid >= 95c -> 1),
    fresh (the game's newest Polymarket row is at most `maxage` s old) and mark(u, h): the first FRESH mid at or after
    second u + h (looking up to 2 minutes on), else the game's result when known, else nan. u may be an array."""
    bid, ask, age = np.asarray(A['bid'][o:o + n], np.float64), np.asarray(A['ask'][o:o + n], np.float64), np.asarray(A['age'][o:o + n], np.float64)
    b2 = np.where(np.isfinite(bid), bid, np.where(np.isfinite(ask) & (ask <= 0.05), 0.0, np.nan))
    a2 = np.where(np.isfinite(ask), ask, np.where(np.isfinite(bid) & (bid >= 0.95), 1.0, np.nan))
    mid = (b2 + a2) / 2
    fresh = age <= maxage
    good = fresh & np.isfinite(mid)
    idx = np.where(good, np.arange(n), n)
    nxt = np.minimum.accumulate(idx[::-1])[::-1]         # the next second with a fresh mid
    res = float(won) if won >= 0 else np.nan

    def mark(u, h, look=120):
        j = np.asarray(u) + h
        inside = j < n
        k = nxt[np.minimum(j, n - 1)]
        ok = inside & (k < n) & (k - j <= look)
        return np.where(ok, mid[np.minimum(k, n - 1)], res)
    return bid, ask, mid, fresh, mark


def row_quality(d, game_key, t_ms):
    """For rows (key = match * 4 + k, time in ms) of another file (lab_sec): the age of the game's newest Polymarket row
    (s) and the feed's lateness (ms) at that second, from the paths files; inf where the game is not in them."""
    M, A = load(d)
    pos = {int(k): i for i, k in enumerate(M['key'])}
    age, late = np.full(len(t_ms), np.inf), np.full(len(t_ms), np.inf)
    order = np.argsort(game_key, kind='stable')
    gk = np.asarray(game_key)[order]
    starts = np.r_[0, np.flatnonzero(gk[1:] != gk[:-1]) + 1, len(gk)]
    for a_, b_ in zip(starts[:-1], starts[1:]):
        i = pos.get(int(gk[a_]))
        if i is None:
            continue
        rows = order[a_:b_]
        u = (np.asarray(t_ms)[rows] - M['t0'][i]) // 1000
        ok = (u >= 0) & (u < M['n'][i])
        o = int(M['off'][i])
        age[rows[ok]] = np.asarray(A['age'][o + u[ok]], np.float64)
        late[rows[ok]] = np.nan_to_num(np.asarray(A['late'][o + u[ok]], np.float64), nan=0.0)
    return age, late


def load(d):
    M = dict(np.load(os.path.join(d, 'paths_meta.npz')))
    A = {f: np.load(os.path.join(d, f'paths_{f}.npy'), mmap_mode='r') for f in FIELDS}
    return M, A


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['build'])
    ap.add_argument('--db', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--workers', type=int, default=6)
    a = ap.parse_args()
    import value as VL
    import waves as W
    t0 = time.time()
    gl = W.games(W.connect(a.db))
    res = VL.results(a.db, a.out)
    parts = []
    with Pool(a.workers) as pool:
        for i, r in enumerate(pool.imap_unordered(one, [(a.db, m, sp, st) for m, sp, _, _, st in gl], chunksize=4)):
            parts += r
            if (i + 1) % 500 == 0:
                print(f'  {i + 1} of {len(gl)} games, {time.time() - t0:.0f} s', flush=True)
    parts.sort(key=lambda p: p[0])
    n = np.array([p[6] for p in parts], np.int64)
    off = np.r_[0, np.cumsum(n)[:-1]]
    won = np.array([(-1 if res.get(p[1], (None, None))[p[2]] is None else res[p[1]][p[2]]) for p in parts], np.int8)
    np.savez(os.path.join(a.out, 'paths_meta.npz'), key=np.array([p[0] for p in parts], np.int64), match=np.array([p[1] for p in parts], np.int64),
             k=np.array([p[2] for p in parts], np.int8), sport=np.array([p[3] for p in parts]), start=np.array([p[4] for p in parts], np.int64),
             t0=np.array([p[5] for p in parts], np.int64), off=off, n=n, won=won)
    for fi, name in enumerate(FIELDS):
        np.save(os.path.join(a.out, f'paths_{name}.npy'), np.concatenate([p[7 + fi] for p in parts]))
    print(f'{len(parts)} game outcomes, {n.sum():,} seconds ({n.sum() / 3600:.0f} h), result known for {(won >= 0).sum()}; {time.time() - t0:.0f} s')


if __name__ == '__main__':
    main()
