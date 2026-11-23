"""The owner's drawn pattern (23 Nov): Polymarket starts moving, a bookmaker then jumps PAST it, buy while Polymarket is
still on its way, sell when it reaches the bookmaker's level.

  python tools/signals/pattern_catchup.py --db X --signals DIR [--pattern drawn|booklead]

--pattern booklead (the owner's second drawing): the BOOKMAKER leads. A family jumps >= JUMP in 10 s (moved in the
last 5 s) while Polymarket has not followed yet (moved < 0.02 log-odds that way in 10 s), and the bookmakers sit
>= GAP beyond Polymarket in the move's direction (Polymarket on the wrong side, e.g. above them before a drop).

At a decision second, known then (up; down mirrored):
  Polymarket already rising: its log-odds rose >= RISE over the last 30 s;
  a bookmaker family just jumped: its 10 s move >= JUMP and it moved >= 1 point in the last 5 s;
  the bookmakers now sit >= GAP above Polymarket (log-odds).
One instance per game / outcome / direction per minute. For each: the move a trade could capture within 3 minutes
(best sell price minus the ask paid), whether Polymarket reached the bookmakers' level, the plain-exit result (+3c
net else 180 s, fees paid), what the day's model said (walkforward_bots.py models, trained only on earlier games),
and what the old bot did within 10 s (bot_trades / bot_signals with its reason)."""
import argparse
import bisect
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict

import numpy as np
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import train_waves as TW  # noqa: E402
import waves as W  # noqa: E402

RISE, JUMP, GAP = 0.08, 0.12, 0.12


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    ap.add_argument('--pattern', default='drawn', choices=['drawn', 'booklead'])
    a = ap.parse_args()
    A = dict(np.load(os.path.join(a.signals, 'waves_A.npz'), allow_pickle=False))
    names = [str(x) for x in A['names']]
    c = {k: i for i, k in enumerate(names)}
    X = A['X']
    if a.pattern == 'drawn':
        up = (X[:, c['pm_r30']] >= RISE) & (X[:, c['fam_r10_max']] >= JUMP) & (X[:, c['fam_up5']] >= 1) & (X[:, c['fam_gap_max']] >= GAP)
        dn = (X[:, c['pm_r30']] <= -RISE) & (X[:, c['fam_r10_min']] <= -JUMP) & (X[:, c['fam_dn5']] >= 1) & (X[:, c['fam_gap_min']] <= -GAP)
    else:   # the bookmaker leads; Polymarket has not followed and sits on the wrong side of the bookmakers
        up = (X[:, c['fam_r10_max']] >= JUMP) & (X[:, c['fam_up5']] >= 1) & (X[:, c['pm_r10']] < 0.02) & (X[:, c['fam_gap_mean']] >= GAP)
        dn = (X[:, c['fam_r10_min']] <= -JUMP) & (X[:, c['fam_dn5']] >= 1) & (X[:, c['pm_r10']] > -0.02) & (X[:, c['fam_gap_mean']] <= -GAP)
    inst = []
    for d, mask in ((1, up), (-1, dn)):
        last = {}
        for i in np.flatnonzero(mask):
            k = (int(A['game'][i]), int(A['outcome'][i]), d)
            if A['t'][i] - last.get(k, -10 ** 15) < 60_000:
                continue
            last[k] = int(A['t'][i])
            inst.append((i, d))
    print(f'{len(inst)} instances of the pattern ({sum(1 for _, d in inst if d > 0)} up, {sum(1 for _, d in inst if d < 0)} down) in {len(np.unique(A["game"]))} games')
    # The model of each game's day (walk-forward: trained only on games that kicked off before that day).
    edge = np.full(len(inst), np.nan)
    by_day = defaultdict(list)
    for j, (i, d) in enumerate(inst):
        by_day[dt.datetime.fromtimestamp(A['start'][i] / 1000, dt.timezone.utc).strftime('%Y%m%d')].append(j)
    th = {}
    for day, js in by_day.items():
        p = os.path.join(a.signals, 'wf', day, 'wave_model.pt')
        if not os.path.exists(p):
            continue
        ck = torch.load(p, weights_only=False)
        net = TW.Body(len(ck['names']), ck['hidden'], ck['A']['net.6.weight'].shape[0])
        net.load_state_dict(ck['A'])
        net.eval()
        rows = np.array([inst[j][0] for j in js])
        z = np.clip((X[rows] - ck['mu']) / ck['sd'], -10, 10).astype(np.float32)
        with torch.no_grad():
            out = net(torch.from_numpy(z)).numpy()
        for k, j in enumerate(js):
            i, d = inst[j]
            q = np.sort(out[k, 2:5] if d > 0 else out[k, 5:8]) * 3
            edge[j] = q[1] - A['fee_rt'][i]
            th[j] = ck['theta'].get('up' if d > 0 else 'down')
    # Outcomes.
    res = []
    for j, (i, d) in enumerate(inst):
        x = X[i, c['x']]
        gap = (X[i, c['fam_gap_max']] if d > 0 else -X[i, c['fam_gap_min']]) if a.pattern == 'drawn' else d * X[i, c['fam_gap_mean']]
        p0 = 1 / (1 + np.exp(-x))
        p_book = 1 / (1 + np.exp(-(x + d * gap)))
        room = abs(p_book - p0) * 100                       # points between Polymarket's mid and the bookmakers
        mfe = A['mfe_up'][i] if d > 0 else A['mfe_dn'][i]   # best sell within 3 min minus the ask paid (cents)
        spread = X[i, c['spread']]
        reached = mfe >= room - spread / 2                  # the best sell price got to the bookmakers' level
        base = A['base_up'][i] if d > 0 else A['base_dn'][i]
        res.append({'i': int(i), 'd': d, 'game': int(A['game'][i]), 'outcome': int(A['outcome'][i]), 't': int(A['t'][i]), 'room': room, 'mfe': float(mfe),
                    'reached': bool(reached), 'base': float(base), 'fee': float(A['fee_rt'][i]), 'spread': float(spread),
                    'edge': float(edge[j]), 'th': th.get(j), 'sport': str(A['sport'][i])})
    res = [r for r in res if np.isfinite(r['base']) and np.isfinite(r['mfe'])]
    # The old bot within 10 s: trades and skipped signals (with reasons).
    db = W.connect(a.db)
    sig = defaultdict(list)
    for t_, m, o, rr in db.execute('SELECT ts_ms, match_id, outcome, result FROM bot_signals'):
        sig[m].append((t_, o, rr))
    trd = defaultdict(list)
    for t_, m, data in db.execute('SELECT ts_ms, match_id, data FROM bot_trades'):
        trd[m].append((t_, json.loads(data).get('outcome')))
    for v in sig.values():
        v.sort()
    oname = ['home', 'away']
    for r in res:
        o = oname[r['outcome']] if r['outcome'] < 2 else None
        s = sig.get(r['game'], [])
        k = bisect.bisect_left(s, (r['t'] - 10_000,))
        near = [x for x in s[k:k + 50] if abs(x[0] - r['t']) <= 10_000 and (o is None or x[1] == o)]
        r['bot_traded'] = any(abs(t_ - r['t']) <= 10_000 for t_, oo in trd.get(r['game'], []))
        r['bot_reasons'] = sorted({x[2] for x in near})

    def stat(name, rs):
        if not rs:
            return f'  {name:<58} none'
        b = np.array([r['base'] for r in rs])
        return (f"  {name:<58}{len(rs):>6}  reached the bookmakers {100 * np.mean([r['reached'] for r in rs]):3.0f}%  room {np.median([r['room'] for r in rs]):4.1f} pt  "
                f"best within 3 min {np.median([r['mfe'] for r in rs]):+5.1f}c  plain exit {b.mean():+5.2f}c (win {100 * np.mean(b > 0):.0f}%)")

    lines = [(f'The pattern, as drawn: Polymarket rising >= {RISE} log-odds in 30 s, a bookmaker family jumping >= {JUMP} in 10 s, and the bookmakers >= {GAP} above Polymarket.'
              if a.pattern == 'drawn' else f'Bookmaker leads: a family jumping >= {JUMP} in 10 s, Polymarket not following yet, the bookmakers (mean) >= {GAP} beyond Polymarket that way.'),
             f'Per instance: room to the bookmakers (median, points), the best sell within 3 min minus the ask paid (median, cents, before fees), the plain-exit result (+3c net else 180 s, fees paid).',
             stat('all instances', res),
             stat('  of which up', [r for r in res if r['d'] > 0]),
             stat('  of which down', [r for r in res if r['d'] < 0]),
             stat('  room 5+ points to the bookmakers', [r for r in res if r['room'] >= 5]),
             stat('  room 8+ points', [r for r in res if r['room'] >= 8]),
             '', 'Does the MODEL take them (that day\'s model, never trained on the game):',
             stat('model takes it (edge over its validation threshold)', [r for r in res if r['th'] is not None and r['edge'] >= r['th']]),
             stat('model sees a positive edge (move > fees)', [r for r in res if r['edge'] > 0]),
             stat('model says no', [r for r in res if not r['edge'] > 0]),
             '', 'Does the OLD BOT take them (within 10 s):',
             stat('old bot traded', [r for r in res if r['bot_traded']]),
             stat('old bot saw a signal and skipped it', [r for r in res if not r['bot_traded'] and r['bot_reasons']]),
             stat('old bot saw nothing', [r for r in res if not r['bot_traded'] and not r['bot_reasons']]),
             '  why it skipped: ' + ', '.join(f'{k} {v}' for k, v in Counter(x for r in res if not r['bot_traded'] for x in r['bot_reasons']).most_common(8)),
             '', 'By sport:'] + [stat(f'  {sp}', [r for r in res if r['sport'] == sp]) for sp in sorted({r['sport'] for r in res}) if sum(1 for r in res if r['sport'] == sp) >= 30]
    text = '\n'.join(lines)
    print(text)
    with open(os.path.join(a.signals, f'pattern_{a.pattern}.txt'), 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    with open(os.path.join(a.signals, f'pattern_{a.pattern}.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, default=float)


if __name__ == '__main__':
    main()
