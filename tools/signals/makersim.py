"""Can a SLOW, SMALL resting order earn the spread? Two ways to be first in line without being fast (24 Nov night).

rspread.py: resting orders keep +0.5c a share on average, but all of it goes to the front of the queue (a level that
survives the print keeps +0.67c, a level that is taken out loses -0.85c). A late order at the back gets the bad fills.
Ways around the queue that need no speed:
  DEEP     rest a bid d cents UNDER a calm price (and an offer d cents over it) and wait for a sweep to come to it.
  INSIDE   when the spread is k+ cents wide, quote one cent better than the best bid / offer: a new level, first in line.
Both are simulated on the 1 s price paths (paths.py) with a pessimistic rule set:
  - we act on what we knew L seconds ago (L = 0 / 2 / 5): the quote standing at second u was decided from the book at
    u - L, so a quote we would have pulled stays live for L seconds and gets run over;
  - "calm" = the mid has stayed within 1c for 30 s and both sides are quoted (decided at u - L);
  - DEEP fills only when the market trades THROUGH the price (the opposite best quote reaches it) or a print is seen
    at or beyond it; INSIDE fills on any taker print on its side at or beyond its price, or when the book crosses it;
  - 5 shares, one fill per token per 5 minutes, no fee (resting), token 10-90c.
Each fill is valued at the mid 1 and 5 minutes later and at the game's result (never dropped: a game that ends is its
result). Cents a share, 90% range resampling games; choices are read on 17-20 Nov and the same lines printed for 21-23.

usage: python tools/signals/makersim.py --signals ~/swifteye/research/lab [--pre]
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths as PT  # noqa: E402

SPLIT = 1795215600000        # 21 Nov 00:00 Norway


def roll_range(mid, w=30):
    """max - min of mid over the last w seconds (nan-safe: nan anywhere in the window -> inf)."""
    n = len(mid)
    if n < w + 1:
        return np.full(n, np.inf)
    v = np.lib.stride_tricks.sliding_window_view(mid, w)
    r = v.max(1) - v.min(1)
    out = np.full(n, np.inf)
    out[w - 1:] = np.where(np.isfinite(r), r, np.inf)
    return out


def shift(a, L, fill):
    if L == 0:
        return a
    out = np.empty_like(a)
    out[:L] = fill
    out[L:] = a[:-L]
    return out


def run(M, A, pre, configs):
    """-> {config: array of fills (game, t_ms, side, price, mid60, mid300, result, sport idx, kind)}"""
    res = {c: [] for c in configs}
    sports = sorted(set(M['sport'].tolist()))
    for i in range(len(M['key'])):
        o, n = int(M['off'][i]), int(M['n'][i])
        bid, ask, prt = (np.asarray(A[f][o:o + n], np.float64) for f in ('bid', 'ask', 'prt'))
        s0 = int(max(0, (M['start'][i] - M['t0'][i]) // 1000)) if M['start'][i] else 0
        b2 = np.where(np.isfinite(bid), bid, np.where(np.isfinite(ask) & (ask <= 0.05), 0.0, np.nan))
        a2 = np.where(np.isfinite(ask), ask, np.where(np.isfinite(bid) & (bid >= 0.95), 1.0, np.nan))
        mid = (b2 + a2) / 2
        spread = ask - bid
        won = float(M['won'][i])
        t = np.arange(n)
        period = (t < s0) if pre else (t >= s0)
        calm0 = period & np.isfinite(spread) & (spread > 0) & (roll_range(mid) <= 0.0101) & (mid >= 0.10) & (mid <= 0.90)
        pb, pa = np.r_[np.nan, bid[:-1]], np.r_[np.nan, ask[:-1]]
        sell_p = np.isfinite(prt) & (prt <= pb + 1e-9)          # a taker sold (hit the bid of the second before)
        buy_p = np.isfinite(prt) & (prt >= pa - 1e-9)

        def mark(u, h):
            j = u + h
            v = np.where(j < n, mid[np.minimum(j, n - 1)], np.nan)
            if won >= 0:
                v = np.where((j >= n) | ~np.isfinite(v), won, v)
            else:
                v = np.where(np.isfinite(v), v, mid[np.minimum(j, n - 1)])
            return v
        for c in configs:
            kind, par, L = c
            cal = shift(calm0, L, False)
            if kind == 'deep':
                anchor = shift(mid, L, np.nan)
                qb = np.floor((anchor - par / 100) * 100 + 1e-9) / 100
                qa = np.ceil((anchor + par / 100) * 100 - 1e-9) / 100
            else:                                        # inside: one cent better, only when the spread (at u - L) is par+ cents
                sb, sa, ss = shift(bid, L, np.nan), shift(ask, L, np.nan), shift(spread, L, np.nan)
                wide = ss >= par / 100 - 1e-9
                cal = cal & wide
                qb, qa = sb + 0.01, sa - 0.01
            fb = cal & np.isfinite(qb) & ((ask <= qb + 1e-9) | (sell_p & (prt <= qb + 1e-9)))
            fa = cal & np.isfinite(qa) & ((bid >= qa - 1e-9) | (buy_p & (prt >= qa - 1e-9)))
            ev = np.flatnonzero(fb | fa)
            last = -10 ** 9
            for u in ev:
                if u - last < 300:
                    continue
                last = u
                side = 1 if fb[u] else -1                 # +1 we bought the token, -1 we sold it (bought the other side)
                q = qb[u] if side > 0 else qa[u]
                res[c].append((M['match'][i], M['t0'][i] + 1000 * u, side, q, float(mark(np.array([u]), 60)[0]), float(mark(np.array([u]), 300)[0]), won,
                               sports.index(str(M['sport'][i])), 1.0 if (sell_p[u] or buy_p[u]) else 0.0))
    return {c: np.array(v, float).reshape(-1, 9) for c, v in res.items()}, sports


def stat(v, g, rng):
    ok = np.isfinite(v)
    v, g = v[ok], g[ok]
    if len(v) < 20:
        return f'{"-":>22}'
    ug, inv = np.unique(g, return_inverse=True)
    sums, cnts = np.bincount(inv, v, len(ug)), np.bincount(inv, minlength=len(ug))
    bs = [(lambda q: sums[q].sum() / max(cnts[q].sum(), 1))(rng.integers(0, len(ug), len(ug))) for _ in range(400)]
    return f'{v.mean():+5.2f} [{np.percentile(bs, 5):+.2f}..{np.percentile(bs, 95):+.2f}]'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    ap.add_argument('--pre', action='store_true')
    a = ap.parse_args()
    t0 = time.time()
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    M, A = PT.load(a.signals)
    configs = [('deep', d, L) for d in (2, 3, 4, 6) for L in (0, 2, 5)] + [('inside', k, L) for k in (3, 4, 5, 7) for L in (0, 2, 5)]
    R, sports = run(M, A, a.pre, configs)
    rng = np.random.default_rng(0)
    days = (M['t0'].max() - M['t0'].min()) / 86_400_000
    say(f'{"BEFORE THE START" if a.pre else "IN PLAY"}; {len(M["key"]):,} game outcomes over {days:.1f} days ({time.time() - t0:.0f} s)')
    say('cents a share kept by OUR resting order [90% range by game]; "print" = share of fills that came from a seen trade (the rest: the book crossed our price)')
    for kind, title in (('deep', 'DEEP: a bid d cents under a calm mid, an offer d cents over'), ('inside', 'INSIDE: one cent better than the best quote when the spread is k+ cents')):
        say()
        say(title)
        say(f'   {"":<22}{"fills":>7} {"a day":>6} {"games":>6} {"print":>6}   after 1 min            | after 5 min            | at the result          || 17-20 Nov, 5 min       | 21-23 Nov, 5 min')
        for c in configs:
            if c[0] != kind:
                continue
            r = R[c]
            if len(r) < 20:
                say(f'   {("d" if kind == "deep" else "k")} = {c[1]}c, {c[2]} s late        {len(r):>7}')
                continue
            sg = r[:, 2]
            k60, k300 = sg * (r[:, 4] - r[:, 3]) * 100, sg * (r[:, 5] - r[:, 3]) * 100
            kres = np.where(r[:, 6] >= 0, sg * (r[:, 6] - r[:, 3]) * 100, np.nan)
            early = r[:, 1] < SPLIT
            say(f'   {("d" if kind == "deep" else "k")} = {c[1]}c, {c[2]} s late   {len(r):>7,} {len(r) / days:>6.0f} {len(np.unique(r[:, 0])):>6,} {r[:, 8].mean() * 100:>5.0f}%   {stat(k60, r[:, 0], rng)} | {stat(k300, r[:, 0], rng)} | {stat(kres, r[:, 0], rng)} || '
                f'{stat(k300[early], r[early, 0], rng)} | {stat(k300[~early], r[~early, 0], rng)}')
        best = [c for c in configs if c[0] == kind and c[2] == 2]
        for c in best:
            r = R[c]
            if len(r) < 200:
                continue
            sg = r[:, 2]
            k300 = sg * (r[:, 5] - r[:, 3]) * 100
            say(f'      {("d" if kind == "deep" else "k")} = {c[1]}c, 2 s late, by sport (5 min): ' + ', '.join(f'{sports[j]} {np.nanmean(k300[r[:, 7] == j]):+.2f} ({int((r[:, 7] == j).sum())})' for j in range(len(sports)) if (r[:, 7] == j).sum() >= 40))
    np.save(os.path.join(a.signals, 'makersim_pre.npy' if a.pre else 'makersim.npy'), {str(c): v for c, v in R.items()}, allow_pickle=True)
    with open(os.path.join(a.signals, 'makersim_pre.txt' if a.pre else 'makersim.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    print(f'done in {time.time() - t0:.0f} s')


if __name__ == '__main__':
    main()
