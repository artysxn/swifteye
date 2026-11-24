"""The "superline 4+ points above the cost" bet, taken apart (python -I tools/signals/fair_sl.py DIR; needs fair.npz).

fair.py showed it at +$20.34 per $100 over 146 bets before kick-off, with a range from -17 to +61: results alone cannot
say whether that is information or luck. Two things can:
  the closing price  if the superline knows something, Polymarket's own price should move TOWARD it before kick-off.
                     For each bet: Polymarket's mid at the last snapshot before kick-off (3 min, else 10 min) minus
                     the price paid (cents), and what selling there into the bid would have made. A price move has a
                     tenth of the noise of a result.
  dose and response  a bigger gap should give a bigger edge, in both halves of the week, in more than one sport.
One bet a game: the first snapshot before kick-off (120 / 60 / 30 / 10 min) where superline - (ask + fee) >= threshold."""
import datetime as dt
import os
import sys

import numpy as np

Z = np.load(os.path.join(sys.argv[1], 'fair.npz'))
A, SP = Z['A'], Z['sport']
C = {k: i for i, k in enumerate(Z['cols'].tolist())}
col = lambda k: A[:, C[k]]
G, side, off, t, start, y = col('game').astype(np.int64), col('side').astype(int), col('off'), col('t'), col('start'), col('y')
bid, ask, cons, sl, nf = col('pm_bid'), col('pm_ask'), col('cons'), col('sl'), col('n_fam')
mid = (bid + ask) / 2
fee = lambda p: 0.05 * p * (1 - p)
cost = ask + fee(ask)
pay = 100 * (y - cost) / cost
day = np.array([dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%d') for v in start])
key = {(int(g), int(s), int(o)): i for i, (g, s, o) in enumerate(zip(G, side, off))}
rng = np.random.default_rng(2)
out = []
say = lambda s: (out.append(s), print(s, flush=True))


def ci(v):
    v = np.asarray(v, float)
    v = v[np.isfinite(v)]
    if len(v) < 8:
        return 'n/a'
    b = [v[rng.integers(0, len(v), len(v))].mean() for _ in range(4000)]
    return f'{v.mean():+.2f} ({np.quantile(b, .05):+.2f}..{np.quantile(b, .95):+.2f})'


def close_of(i):
    for o in (-3, -10):
        if off[i] < o and (int(G[i]), int(side[i]), o) in key:
            return key[(int(G[i]), int(side[i]), o)]
    return None


def bets(edge, offs=(-120, -60, -30, -10), mask=None):
    order = np.argsort(t, kind='stable')
    seen, idx = set(), []
    for i in order:
        if off[i] in offs and np.isfinite(edge[i]) and edge[i] >= 0 and (mask is None or mask[i]) and G[i] not in seen:
            seen.add(G[i])
            idx.append(i)
    return np.array(idx, np.int64)


def line(name, idx):
    if len(idx) < 12:
        return say(f'   {name:<40} {len(idx)} bets: too few')
    cl = [close_of(i) for i in idx]
    clv = np.array([(mid[c] - ask[i]) * 100 if c is not None else np.nan for i, c in zip(idx, cl)])
    sell = np.array([100 * (bid[c] - fee(bid[c]) - cost[i]) / cost[i] if c is not None else np.nan for i, c in zip(idx, cl)])
    say(f'   {name:<40} {len(idx):>4} bets at {ask[idx].mean() * 100:.0f}c: won {y[idx].mean() * 100:.0f}% (needed {cost[idx].mean() * 100:.0f}%), to the result ${ci(pay[idx])} per $100 | '
        f'Polymarket then moved {ci(clv)}c our way by kick-off | sold before kick-off ${ci(sell)} per $100')


e_sl = sl - cost
say('1. THE CONTROL: any side bought 120 / 60 / 30 / 10 min before kick-off with no signal (one a game)')
line('no signal', bets(np.where(np.isfinite(sl), 0.0, np.nan)))
say('')
say('2. DOSE AND RESPONSE: the superline at least N points above the cost (ask + fee)')
for th in (0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10):
    line(f'superline {int(th * 100)}+ pt above the cost', bets(e_sl - th))
say('   the mirror (superline BELOW the price = the side looks overpriced):')
for th in (0.04, 0.06):
    line(f'superline {int(th * 100)}+ pt below the bid', bets(bid - sl - th))
B = bets(e_sl - 0.04)
say('')
say(f'3. THE 4+ POINT BETS TAKEN APART ({len(B)} bets)')
inb = np.zeros(len(A), bool)
inb[B] = True
for name, m in [(s, SP == s) for s in ('tennis', 'basketball', 'football', 'cs2', 'hockey', 'dota2')] + [
        ('price under 30c', ask < .3), ('price 30-55c', (ask >= .3) & (ask < .55)), ('price 55c and over', ask >= .55),
        ('first seen 2 h before', off == -120), ('first seen 1 h before', off == -60), ('first seen 30 or 10 min before', off >= -30),
        ('gap 4-6 pt', e_sl < .06), ('gap 6-10 pt', (e_sl >= .06) & (e_sl < .10)), ('gap 10+ pt', e_sl >= .10),
        ('games of 17-20 Nov', np.isin(day, ['17', '18', '19', '20'])), ('games of 21-23 Nov', np.isin(day, ['21', '22', '23'])),
        ('1-2 bookmaker families', nf <= 2), ('3-5 families', (nf >= 3) & (nf <= 5)), ('6+ families', nf >= 6),
        ('the bookmakers\' middle also above the cost', cons >= cost), ('the middle NOT above the cost', cons < cost),
        ('bookmakers moved up 1+ pt in the last hour', col('cons_chg60') >= .01), ('Polymarket fell 1+ pt in the last hour', col('pm_chg60') <= -.01),
        ('neither moved in the last hour', (np.abs(col('cons_chg60')) < .01) & (np.abs(col('pm_chg60')) < .01))]:
    line(name, np.flatnonzero(inb & m))
v = np.sort(pay[B])[::-1]
say(f'   to the result: median bet ${np.median(v):+.2f}; without the 5 best ${v[5:].mean():+.2f} per $100, without the 10 best ${v[10:].mean():+.2f}; the superline was {np.median((sl - mid)[B]) * 100:.1f} pt above Polymarket\'s mid (median), '
    f'the bookmakers\' middle {np.nanmedian((cons - mid)[B]) * 100:+.1f} pt')
say('')
say('4. THE SAME RULE WITH OTHER LINES (4+ pt above the cost, before kick-off)')
line('bookmakers\' middle', bets(cons - cost - 0.04))
line('highest bookmaker family', bets(col('fam_max') - cost - 0.04))
line('superline AND the middle', bets(np.minimum(sl, cons) - cost - 0.04))
line('superline but not the middle', bets(e_sl - 0.04, mask=cons < cost))
with open(os.path.join(sys.argv[1], 'fair_sl.txt'), 'w', encoding='utf-8') as fh:
    fh.write('\n'.join(out) + '\n')
