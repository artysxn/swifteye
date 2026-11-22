"""A SECOND TRIGGER for slow bookmaker leads (no jump): the bookmakers' middle has moved X pt more than Polymarket over the
last 60 s (tools/wave_predict.py gap60). More bets than the jump trigger? Do they pay at real prices?
(owner, 22 Nov 2026: "how can we take more bets?")

400 games Swifteye recorded (17-22 Nov, in play), every 2 s: signals from ~/swifteye/research/paths/predict_400.pickle,
Polymarket's real buy and sell prices from book_400.pickle (the home side; the bet is on whichever side the bookmakers
moved toward). A bet: bought at the ask 2 s after the signal; sold when the sell price has made Y% of the lead
(Polymarket's middle at the signal + Y% of the gap), else at 5 min (sell price - 1c); both fees, $12. One bet at a time
a game, the next signal only after the last sale + 30 s.
usage: drift_trigger.py      -> ~/swifteye/research/drift_trigger.txt
"""
import os, sys, pickle, collections
fee = lambda p: 0.05 * p * (1 - p) if 0 < p < 1 else 0.0
isn = lambda x: x != x
D = os.path.expanduser('~/swifteye/research/paths/')
G = pickle.load(open(D + 'predict_400.pickle', 'rb')); B = {g['id']: g for g in pickle.load(open(D + 'book_400.pickle', 'rb'))}


def run(g, X, Y, spread_max, give=150):
    b = B[g['id']]; bid, ask = b['bid'], b['ask']; n = len(bid); gap = g['gap60']; mid = g['mid']
    res = []; i = 30; free = 0
    while i < n - 2:
        if i < free or isn(gap[i]) or abs(gap[i]) < X or bid[i] <= 0: i += 1; continue
        d = 1 if gap[i] > 0 else -1; j = i + 1
        if bid[j] <= 0: i += 1; continue
        sp = ask[j] - bid[j]
        if sp > spread_max: i += 1; continue
        buy = (ask[j] if d > 0 else 100 - bid[j]) / 100
        if not (0.05 <= buy <= 0.95): i += 1; continue
        m0 = mid[i] if d > 0 else 100 - mid[i]
        tgt = (m0 + Y * abs(gap[i]) - sp / 2) / 100
        k = j + 1; x = None
        while k < min(n, j + give):
            if bid[k] > 0:
                s = (bid[k] if d > 0 else 100 - ask[k]) / 100
                if s >= tgt: x = s - 0.01; break
            k += 1
        caught = x is not None
        if x is None:
            k = min(n - 1, j + give)
            while k > j and bid[k] <= 0: k -= 1
            x = ((bid[k] if d > 0 else 100 - ask[k]) - 1) / 100
        sh = 12.0 / buy
        res.append((sh * (x - buy) - sh * (fee(buy) + fee(max(x, 0.001))), caught))
        free = k + 15; i = k + 1
    return res


out = ['A DRIFT TRIGGER on %d recorded games (bookmakers ahead of Polymarket by X pt over 60 s, no jump needed); real prices, $12, give up 5 min\n' % len(G)]
for X in (4, 6, 8, 10):
    for Y in (0.5, 1.0):
        for smax, slab in ((1.5, 'spread 1c'), (3.5, 'spread up to 3c'), (99, 'any spread')):
            R = [r for g in G for r in run(g, X, Y, smax)]
            if not R: continue
            tot = sum(p for p, c in R)
            out.append('  ahead %2d pt+, sell at %3.0f%% of the lead, %-15s %5d bets (%4.1f a game) | caught %3.0f%% | won %3.0f%% | %+8.2f $ = %+5.2f a bet' % (
                X, 100 * Y, slab, len(R), len(R) / len(G), 100.0 * sum(1 for p, c in R if c) / len(R), 100.0 * sum(1 for p, c in R if p > 0) / len(R), tot, tot / len(R)))
    out.append('')
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/drift_trigger.txt'), 'w').write(txt + '\n')
