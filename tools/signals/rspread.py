"""Who earns the spread on Polymarket's sports markets? (24 Nov night.)

A resting order pays no fee and is filled at its own price. Whether that is worth anything is one number: the REALIZED
SPREAD. For every trade print (paths.py: the last-trade price changed in that second) we know the side that took
liquidity (print at / above the ask of the second before = a buyer took; at / below the bid = a seller took) and so
what the resting side got:
  quoted half spread   how far the print was from the mid of the second before (what the taker paid for immediacy)
  realized spread      the same, measured against the mid T seconds LATER (5 s, 30 s, 1 min, 5 min) and against the
                       RESULT of the game: what the resting side really kept after the price moved on it
                       (marks use only FRESH quotes: the recording has holes, see paths.book)
realized > 0 means resting orders earn money on average; the gap to the quoted half spread is what informed takers win.
Prints are only seen when the trade price CHANGED, so repeated trades at one price are missing: the numbers are a
floor for the resting side (the missing trades are the harmless ones).

Tables: all in-play prints, by sport, by token price, by spread, by how busy the last minute was, by size of the print's
level; and the same for pre-match prints. Cents a share; ranges resample games.
usage: python tools/signals/rspread.py --signals ~/swifteye/research/lab
"""
import argparse
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths as PT  # noqa: E402

HZ = (5, 30, 60, 300)


def collect(M, A):
    """One row per print: key index, second, side (+1 a buyer took), print, mid before, spread before, mids later, result, in play."""
    rows = []
    for i in range(len(M['key'])):
        o, n = int(M['off'][i]), int(M['n'][i])
        bid, ask, mid, fresh, mark = PT.book(A, o, n, M['won'][i])
        prt = np.asarray(A['prt'][o:o + n], np.float64)
        s = np.flatnonzero(np.isfinite(prt))
        s = s[(s >= 1) & (s < n)]
        s = s[fresh[s - 1]]                              # the book of the second before must be a live one, not a carried-forward hole
        if not len(s):
            continue
        mb, sp = mid[s - 1], (ask - bid)[s - 1]
        p = prt[s]
        side = np.nan_to_num(np.where(p >= ask[s - 1] - 1e-9, 1, np.where(p <= bid[s - 1] + 1e-9, -1, np.sign(p - mb)))).astype(np.int8)
        ok = np.isfinite(mb) & np.isfinite(sp) & (sp > 0) & (side != 0) & (np.abs(p - mb) <= 0.15)
        later = [mark(s, h) for h in HZ]
        vol = np.convolve(np.isfinite(prt).astype(np.float64), np.ones(60), 'full')[:n]      # prints in the last 60 s
        t_ms = M['t0'][i] + 1000 * s
        rows.append(np.stack([np.full(len(s), i), s, side, p, mb, sp, *later, np.full(len(s), float(M['won'][i])), (t_ms >= M['start'][i]).astype(float), vol[s - 1],
                              np.asarray(A['bsz'][o:o + n], np.float64)[s - 1], np.asarray(A['asz'][o:o + n], np.float64)[s - 1], t_ms.astype(np.float64)], 1)[ok])
    return np.concatenate(rows)


def table(say, R, M, title, masks):
    say(title)
    say(f'   {"":<34}{"prints":>9} {"games":>6}  quoted half | kept after 5 s | 30 s | 1 min | 5 min | to the result  (cents a share; 5-min 90% range by game)')
    g = M['match'][R[:, 0].astype(np.int64)]
    side, p, mb = R[:, 2], R[:, 3], R[:, 4]
    for name, m in masks:
        if m.sum() < 300:
            continue
        q = side[m] * (p[m] - mb[m]) * 100
        vals = [np.nanmean(side[m] * (p[m] - R[m, 6 + k]) * 100) for k in range(len(HZ))]
        wk = R[m, 10] >= 0
        res = np.mean(side[m][wk] * (p[m][wk] - R[m, 10][wk]) * 100) if wk.sum() > 100 else float('nan')
        v5 = side[m] * (p[m] - R[m, 9]) * 100
        ug, inv = np.unique(g[m], return_inverse=True)
        fin = np.isfinite(v5)
        sums, cnts = np.bincount(inv[fin], v5[fin], len(ug)), np.bincount(inv[fin], minlength=len(ug))
        rng = np.random.default_rng(1)
        bs = [(lambda k: sums[k].sum() / max(cnts[k].sum(), 1))(rng.integers(0, len(ug), len(ug))) for _ in range(300)]
        say(f'   {name:<34}{int(m.sum()):>9,} {len(ug):>6,}  {q.mean():+6.2f}      | {vals[0]:+6.2f}        | {vals[1]:+5.2f} | {vals[2]:+5.2f} | {vals[3]:+5.2f} [{np.percentile(bs, 5):+.2f}..{np.percentile(bs, 95):+.2f}] | {res:+6.2f}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    t0 = time.time()
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    M, A = PT.load(a.signals)
    R = collect(M, A)
    np.save(os.path.join(a.signals, 'prints.npy'), R.astype(np.float64))
    sp_of = M['sport'][R[:, 0].astype(np.int64)]
    live = R[:, 11] > 0.5
    say(f'{len(R):,} trade prints with a known side in {len(np.unique(R[:, 0])):,} game outcomes ({live.sum():,} in play) ({time.time() - t0:.0f} s)')
    say('"kept" = what a resting order filled by that print still has T later; + = the resting side wins, - = the taker was right')
    allm = np.ones(len(R), bool)
    table(say, R, M, '\nIN PLAY', [('all in-play prints', live)] + [(s, live & (sp_of == s)) for s in ('tennis', 'football', 'basketball', 'cs2', 'hockey', 'valorant', 'dota2', 'amfootball')])
    table(say, R, M, '\nIN PLAY, by price of the token', [(f'{lo}-{hi}c', live & (R[:, 3] >= lo / 100) & (R[:, 3] < hi / 100)) for lo, hi in ((0, 10), (10, 30), (30, 70), (70, 90), (90, 100))])
    table(say, R, M, '\nIN PLAY, by the spread just before', [('1c or less', live & (R[:, 5] <= 0.0101)), ('2c', live & (R[:, 5] > 0.0101) & (R[:, 5] <= 0.0201)), ('3-4c', live & (R[:, 5] > 0.0201) & (R[:, 5] <= 0.0401)), ('5c+', live & (R[:, 5] > 0.0401))])
    table(say, R, M, '\nIN PLAY, by prints in the minute before', [('none (quiet)', live & (R[:, 12] <= 0)), ('1-3', live & (R[:, 12] >= 1) & (R[:, 12] <= 3)), ('4-10', live & (R[:, 12] >= 4) & (R[:, 12] <= 10)), ('over 10 (busy)', live & (R[:, 12] > 10))])
    lvl = np.where(R[:, 2] > 0, R[:, 14], R[:, 13])                             # shares resting at the level the print hit
    table(say, R, M, '\nIN PLAY, by shares resting at the level hit', [('under 50', live & (lvl < 50)), ('50-500', live & (lvl >= 50) & (lvl < 500)), ('500-5,000', live & (lvl >= 500) & (lvl < 5000)), ('5,000+', live & (lvl >= 5000))])
    table(say, R, M, '\nBEFORE THE START', [('all pre-match prints', ~live)] + [(s, ~live & (sp_of == s)) for s in ('tennis', 'football', 'basketball', 'cs2')])
    with open(os.path.join(a.signals, 'rspread.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')
    print(f'done in {time.time() - t0:.0f} s')


if __name__ == '__main__':
    main()
