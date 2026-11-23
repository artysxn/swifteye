"""When the superline is ahead, how far does Polymarket follow? (python tools/signals/superline_follow.py --db X --signals DIR)

Every "superline ahead" onset (lead state turning AHEAD_UP / AHEAD_DOWN, lead.py; more_bets.npz type superline_lead,
one per game / outcome / direction per minute, gap >= 1 pt) on the Mac data, 17-23 Nov. GAP = the superline's level
minus Polymarket's mid at the onset (points, the superline's way). Then Polymarket's mid at +10 s / 30 s / 1 / 3 / 5 min:
the share of the gap it closed; how often it closed half the gap within 1 and 5 min ("superline right"); how often it
first moved 1 pt the OTHER way; the best mid our way within 5 min (a perfect exit); and the cost of a round trip there
(half spreads + 5% p(1-p) taker fee both fills). Also: the superline's own level 1 min later (does it hold or snap back).
Exploratory: all days, no model, no hindsight in the selection (the onset is known at its second)."""
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

HZ = (10, 30, 60, 180, 300)


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
        secs = t + 1000 * np.arange(0, 301)
        b, a = held(lt, lb, secs), held(lt, la, secs)
        mid = (b + a) / 2
        if not np.isfinite(mid[0]) or not np.isfinite(mid[1]):
            continue
        out.append((j, mid, (a[1] - b[1]) / 2, (a[300] - b[300]) / 2))
    return out


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--workers', type=int, default=24)
    a = ap.parse_args()
    M = dict(np.load(os.path.join(a.signals, 'more_bets.npz'), allow_pickle=False))
    names = [str(x) for x in M['names']]
    c = {k: i for i, k in enumerate(names)}
    sel = np.flatnonzero(M['type'] == 2)
    oname = ['home', 'away']
    games = defaultdict(list)
    for j in sel:
        games[int(M['game'][j])].append((int(j), oname[M['outcome'][j]], int(M['t'][j]), int(M['dir'][j])))
    res = []
    with Pool(a.workers) as pool:
        for r in pool.imap_unordered(game, [(a.db, m, v) for m, v in games.items()], chunksize=4):
            res += r
    j = np.array([x[0] for x in res])
    d = M['dir'][j].astype(float)
    mid = np.array([x[1] for x in res])
    h_in = np.array([x[2] for x in res])
    h_out = np.array([x[3] for x in res])
    gap = M['room'][j]
    mv = d[:, None] * (mid - mid[:, :1]) * 100            # PM's move our way, points, each second 0..300
    mvf = np.where(np.isfinite(mv), mv, np.nan)
    # superline's own level a minute later: the same game/outcome's waves row nearest t + 60 s is not in more_bets;
    # use the next onset-free proxy: PM-relative gap of the NEXT candidate is not reliable, so report PM only.
    out = [f'{len(res)} superline-ahead onsets (17-23 Nov, Mac data). Points = probability points; our way = the superline\'s side.', '']
    bands = ((1, 3), (3, 6), (6, 10), (10, 20), (20, 100))
    hdr = f'{"gap":>8} {"onsets":>7} {"median gap":>10} ' + ' '.join(f'{"+" + (str(h) + "s" if h < 60 else str(h // 60) + "m"):>7}' for h in HZ) + \
        f' {"half 1m":>8} {"half 5m":>8} {"wrong 1st":>9} {"best 5m":>8} {"cost":>6} {"5m - cost":>9}'
    out.append('PM\'s move our way as a share of the gap (median), then: closed half the gap within 1 / 5 min, moved 1 pt the other way first,')
    out.append('the best move our way within 5 min (mean, points ~ cents), round-trip cost (cents), and the 5-min move minus the cost:')
    out.append(hdr)
    for lo, hi in bands + ((1, 100),):
        m = (gap >= lo) & (gap < hi)
        if m.sum() < 30:
            continue
        g = gap[m]
        frac = [np.nanmedian(mvf[m, h] / g) for h in HZ]
        first_half = np.array([np.argmax(r >= gg / 2) if np.any(r >= gg / 2) else 999 for r, gg in zip(np.nan_to_num(mvf[m], nan=-99), g)])
        first_wrong = np.array([np.argmax(r <= -1) if np.any(r <= -1) else 999 for r in np.nan_to_num(mvf[m], nan=99)])
        wrong_first = np.mean(first_wrong < first_half) * 100
        best = np.nanmax(mvf[m, 1:], axis=1)
        tok = np.where(d[m] > 0, mid[m, 1], 1 - mid[m, 1])
        cost = (h_in[m] + np.nan_to_num(h_out[m], nan=0.01) + fee(tok) * 2) * 100
        m5 = np.nan_to_num(mvf[m, 300], nan=-tok * 100)
        lab = f'{lo}-{hi}' if hi < 100 else (f'{lo}+' if lo > 1 else 'all')
        out.append(f'{lab:>8} {m.sum():>7} {np.median(g):>9.1f}  ' + ' '.join(f'{f * 100:>6.0f}%' for f in frac) +
                   f' {np.mean(first_half <= 60) * 100:>7.0f}% {np.mean(first_half <= 300) * 100:>7.0f}% {wrong_first:>8.0f}% {np.nanmean(best):>+7.2f} {cost.mean():>6.2f} {np.mean(m5 - cost):>+8.2f}c')
    out.append('')
    # PM before the onset: did PM move our way in the 10 s before (it led) or not?
    pre = M['X'][j, c['pm_r10']] * d * 100 if 'pm_r10' in c else None
    if pre is not None:
        out.append('Who moved first: Polymarket\'s 10 s move before the onset, our way (log-odds x 100):')
        for lab, mm in (('PM already moving our way (> +2)', pre > 2), ('PM flat (-2..+2)', np.abs(pre) <= 2), ('PM moving against (< -2)', pre < -2)):
            if mm.sum() >= 30:
                out.append(f'  {lab:<34} {mm.sum():>6} onsets: closed half the gap within 5 min '
                           f'{np.mean([np.any(r[:301] >= gg / 2) for r, gg in zip(np.nan_to_num(mvf[mm], nan=-99), gap[mm])]) * 100:.0f}%, '
                           f'5-min move {np.nanmean(mvf[mm, 300]):+.2f} pt on a median gap of {np.median(gap[mm]):.1f}')
    text = '\n'.join(out)
    print(text)
    with open(os.path.join(a.signals, 'superline_follow.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
