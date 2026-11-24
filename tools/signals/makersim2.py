"""Slow resting orders against Polymarket's REAL trades, with sizes (24 Nov night).

makersim.py counted a fill whenever the book crossed our price; that assumes a taker always beats our cancel. Here a
fill needs a real taker trade from Polymarket's public history (prints_fetch.py: every trade, its size and side) that
would have reached our order:
  our quote at second u is decided from the book we saw at u - L (L = 0 / 1 / 2 / 3 s: our total reaction time);
  a real taker SALE of the token at price p fills our bid q when q >= p (they took p; ours was at least as good), a real
  taker BUY at p fills our offer q when q <= p. A fill AT a price we only share with an existing queue is not counted
  (class "queue"); only prices where we are first in line (strictly better than the best quote of the second before)
  or that the trade went THROUGH.
  OUR RECORDING HAS HOLES (a game's book can freeze for minutes or hours while it trades on): a quote is only
  decided on, a trade only counted and a fill only valued where the game's newest Polymarket row is <= 15 s old.
  The public stamp runs ~2 s behind the match, so each trade is placed on the second within [stamp - 5, stamp] whose
  best quote is nearest the trade price.
Quotes (both sides, 20 shares, one fill per side per token per minute, token 10-90c, calm = mid within 1c for 30 s):
  INSIDE k    one cent better than the best quote when the spread is k+ cents
  DEEP d      a bid d cents under the mid and an offer d cents over it
Each fill is valued at the mid 1 and 5 minutes later and at the result (a game that ends is its result, never dropped).
Also printed: the true realized spread of ALL real trades, share-weighted (the check on rspread.py).

usage: python tools/signals/makersim2.py --signals ~/swifteye/research/lab
"""
import argparse
import glob
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths as PT  # noqa: E402
from makersim import roll_range, shift, SPLIT  # noqa: E402

LATE = (0, 1, 2, 3)
OUR = 20.0


def align(ts, side, p, bid, ask, t0s, n):
    """Second (index) each trade most likely happened: within [stamp - 5, stamp], the one whose touch is nearest its price."""
    u = np.empty(len(ts), np.int64)
    for j in range(len(ts)):
        best, bu = 9.0, ts[j] - 2 - t0s
        for c in (2, 1, 3, 0, 4, 5):                     # try the usual lag first
            k = ts[j] - c - t0s
            if 1 <= k < n:
                touch = bid[k - 1] if side[j] < 0 else ask[k - 1]
                dlt = abs(touch - p[j]) if np.isfinite(touch) else 8.0
                if dlt < best - 1e-9:
                    best, bu = dlt, k
        u[j] = bu
    return u


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    t0 = time.time()
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    M, A = PT.load(a.signals)
    idx = {int(M['match'][i]): i for i in range(len(M['key'])) if M['k'][i] == 0}
    configs = [('inside', k) for k in (2, 3, 4, 5, 7)] + [('deep', d) for d in (1, 2, 3, 4, 6)]
    F = []                                               # fills: cfg idx, L, match, t_ms, side, q, shares, mid60, mid300, result, sport idx, in play, spread then, class (0 first in line, 1 traded through), minutes to start
    T = []                                               # every real trade: match, side, p, size, mid before, mid 60, mid 300, result, in play, spread before, sport idx
    sports = sorted(set(M['sport'].tolist()))
    games = 0
    for f in sorted(glob.glob(os.path.join(a.signals, 'pm_trades', '*.json'))):
        g = json.load(open(f))
        m = g['match']
        if m not in idx or not g['trades']:
            continue
        i = idx[m]
        o, n, t0s = int(M['off'][i]), int(M['n'][i]), int(M['t0'][i]) // 1000
        won = float(M['won'][i])
        bid, ask, mid, fresh, mark = PT.book(A, o, n, won, maxage=15.0)
        spread = ask - bid
        s0 = int(max(0, (M['start'][i] - M['t0'][i]) // 1000)) if M['start'][i] else 0
        tr = np.array([(t[0], t[1], t[2], t[3], 1 if (t[4] or '').upper() == 'BUY' else -1) for t in g['trades']], float)
        ts = tr[:, 0].astype(np.int64)
        keep = (ts >= t0s + 40) & (ts < t0s + n - 2)
        tr, ts = tr[keep], ts[keep]
        if not len(tr):
            continue
        games += 1
        home = tr[:, 1] > 0.5
        side = np.where(home, tr[:, 4], -tr[:, 4]).astype(np.int64)        # on the home token: +1 the taker bought it, -1 sold it
        p = np.where(home, tr[:, 2], 1 - tr[:, 2])
        size = tr[:, 3]
        order = np.argsort(ts, kind='stable')
        ts, side, p, size = ts[order], side[order], p[order], size[order]
        u = align(ts, side, p, bid, ask, t0s, n)
        okb = fresh[u] & fresh[u - 1]                                    # only trades that land on a live book of ours
        ts, side, p, size, u = ts[okb], side[okb], p[okb], size[okb], u[okb]
        if not len(u):
            continue

        sp_i = sports.index(str(M['sport'][i]))
        live_tr = u >= s0
        T.append(np.stack([np.full(len(u), m), side, p, size, mid[u - 1], mark(u, 60), mark(u, 300), np.full(len(u), won), live_tr.astype(float), spread[u - 1] * 100, np.full(len(u), sp_i)], 1))
        calm0 = fresh & np.isfinite(spread) & (spread > 0) & (roll_range(mid) <= 0.0101) & (mid >= 0.10) & (mid <= 0.90)
        pb, pa = np.r_[np.nan, bid[:-1]], np.r_[np.nan, ask[:-1]]         # the best quotes of the second before
        for ci, (kind, par) in enumerate(configs):
            for L in LATE:
                cal = shift(calm0, L, False)
                if kind == 'inside':
                    ok = cal & (shift(spread, L, np.nan) >= par / 100 - 1e-9)
                    qb = np.where(ok, shift(bid, L, np.nan) + 0.01, np.nan)
                    qa = np.where(ok, shift(ask, L, np.nan) - 0.01, np.nan)
                else:
                    anchor = shift(mid, L, np.nan)
                    qb = np.where(cal, np.floor((anchor - par / 100) * 100 + 1e-9) / 100, np.nan)
                    qa = np.where(cal, np.ceil((anchor + par / 100) * 100 - 1e-9) / 100, np.nan)
                for sgn, qarr, prev in ((1, qb, pb), (-1, qa, pa)):          # +1 our bid (takers selling), -1 our offer (takers buying)
                    q = qarr[u]
                    tk = side == -sgn
                    reach = tk & np.isfinite(q) & (sgn * (q - p) >= -1e-9)        # the trade price was no better for the taker than ours
                    first = sgn * (q - prev[u]) > 1e-9                            # strictly better than the best quote of the second before: first in line
                    through = sgn * (q - p) > 1e-9                                # traded beyond our price: our level was taken whole
                    last = -10 ** 9
                    for j in np.flatnonzero(reach & (first | through)):
                        k = int(u[j])
                        if k - last < 60:
                            continue
                        last = k
                        F.append((ci, L, m, (t0s + k) * 1000, sgn, q[j], min(OUR, size[j]), float(mark(np.array([k]), 60)[0]), float(mark(np.array([k]), 300)[0]), won, sp_i, float(k >= s0),
                                  spread[max(k - L, 0)] * 100, 0.0 if first[j] else 1.0, (s0 - k) / 60.0))
    F, T = np.array(F, float).reshape(-1, 15), np.concatenate(T)
    np.save(os.path.join(a.signals, 'makersim2_fills.npy'), F)
    np.save(os.path.join(a.signals, 'makersim2_trades.npy'), T)
    rng = np.random.default_rng(0)

    def stat(v, g, w=None):
        ok = np.isfinite(v)
        v, g = v[ok], g[ok]
        w = np.ones(len(v)) if w is None else w[ok]
        if len(v) < 15:
            return f'{"-":>22}'
        ug, inv = np.unique(g, return_inverse=True)
        sums, cnts = np.bincount(inv, v * w, len(ug)), np.bincount(inv, w, len(ug))
        bs = [(lambda q: sums[q].sum() / max(cnts[q].sum(), 1e-9))(rng.integers(0, len(ug), len(ug))) for _ in range(400)]
        return f'{(v * w).sum() / w.sum():+5.2f} [{np.percentile(bs, 5):+.2f}..{np.percentile(bs, 95):+.2f}]'
    say(f'{games} games with public trade history, {len(T):,} real trades, {T[:, 3].sum():,.0f} shares ({time.time() - t0:.0f} s)')
    say()
    say('THE TRUE REALIZED SPREAD of all real trades (what the resting side kept), cents a share [90% range by game]')
    say(f'   {"":<26}{"trades":>9}  per TRADE: quoted half | kept 5 min           || per SHARE: quoted half | kept 5 min            | at the result')
    sd, pp, sz, mb = T[:, 1], T[:, 2], T[:, 3], T[:, 4]
    okq = np.isfinite(mb) & (np.abs(pp - mb) <= 0.15)
    for name, mk in (('in play, all', T[:, 8] > 0.5), ('in play, spread <= 1c', (T[:, 8] > 0.5) & (T[:, 9] <= 1.01)), ('in play, spread 2c', (T[:, 8] > 0.5) & (T[:, 9] > 1.01) & (T[:, 9] <= 2.01)),
                     ('in play, spread 3-4c', (T[:, 8] > 0.5) & (T[:, 9] > 2.01) & (T[:, 9] <= 4.01)), ('in play, spread 5c+', (T[:, 8] > 0.5) & (T[:, 9] > 4.01)),
                     ('in play, under 20 shares', (T[:, 8] > 0.5) & (sz < 20)), ('in play, 20-200 shares', (T[:, 8] > 0.5) & (sz >= 20) & (sz < 200)), ('in play, 200+ shares', (T[:, 8] > 0.5) & (sz >= 200)),
                     ('before the start, all', T[:, 8] < 0.5), ('before, spread 3c+', (T[:, 8] < 0.5) & (T[:, 9] > 2.01))):
        k = mk & okq
        if k.sum() < 100:
            continue
        q_, k5 = sd[k] * (pp[k] - mb[k]) * 100, sd[k] * (pp[k] - T[k, 6]) * 100
        kr = np.where(T[k, 7] >= 0, sd[k] * (pp[k] - T[k, 7]) * 100, np.nan)
        say(f'   {name:<26}{int(k.sum()):>9,}  {q_.mean():+6.2f}     | {stat(k5, T[k, 0])} || {np.average(q_, weights=sz[k]):+6.2f}     | {stat(k5, T[k, 0], sz[k])} | {stat(kr, T[k, 0], sz[k])}')
    days_frac = games / max(1, sum(1 for i in range(len(M['key'])) if M['k'][i] == 0)) * 6.2
    for live, title in ((1.0, 'IN PLAY'), (0.0, 'BEFORE THE START')):
        say()
        say(f'OUR RESTING ORDERS, {title}: cents a share kept, share-weighted [90% range by game]; fills a day scaled to every recorded game')
        say(f'   {"":<22}{"fills":>6} {"a day":>6} {"games":>6} {"first":>6}   after 1 min            | after 5 min            | at the result          || 17-20 Nov, 5 min       | 21-23 Nov, 5 min')
        for ci, (kind, par) in enumerate(configs):
            for L in LATE:
                r = F[(F[:, 0] == ci) & (F[:, 1] == L) & (F[:, 11] == live)]
                if len(r) < 15:
                    continue
                sg, w = r[:, 4], r[:, 6]
                k60, k300 = sg * (r[:, 7] - r[:, 5]) * 100, sg * (r[:, 8] - r[:, 5]) * 100
                kres = np.where(r[:, 9] >= 0, sg * (r[:, 9] - r[:, 5]) * 100, np.nan)
                early = r[:, 3] < SPLIT
                say(f'   {kind} {par}c, {L} s late     {len(r):>6,} {len(r) / days_frac:>6.0f} {len(np.unique(r[:, 2])):>6,} {(r[:, 13] == 0).mean() * 100:>5.0f}%   {stat(k60, r[:, 2], w)} | {stat(k300, r[:, 2], w)} | {stat(kres, r[:, 2], w)} || '
                    f'{stat(k300[early], r[early, 2], w[early])} | {stat(k300[~early], r[~early, 2], w[~early])}')
        for ci, (kind, par) in enumerate(configs):
            r = F[(F[:, 0] == ci) & (F[:, 1] == 2) & (F[:, 11] == live)]
            if len(r) < 150:
                continue
            k300 = r[:, 4] * (r[:, 8] - r[:, 5]) * 100
            say(f'      {kind} {par}c, 2 s late, by sport (5 min): ' + ', '.join(f'{sports[j]} {np.average(k300[(r[:, 10] == j) & np.isfinite(k300)], weights=r[(r[:, 10] == j) & np.isfinite(k300), 6]):+.2f} ({int((r[:, 10] == j).sum())})'
                                                                              for j in range(len(sports)) if ((r[:, 10] == j) & np.isfinite(k300)).sum() >= 25))
    with open(os.path.join(a.signals, 'makersim2.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    print(f'done in {time.time() - t0:.0f} s')


if __name__ == '__main__':
    main()
