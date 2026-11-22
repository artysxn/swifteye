"""THE CATCH-UP FUNNEL over every bookmaker jump (owner, 22 Nov 2026: "why is there so few catchups despite the graphs
in theory having multiple? how can we take more bets?").

Data: tools/lf_build.py (every jump of 4 pt+ of every bookmaker that Polymarket had not yet made, 17-21 Nov, with
Polymarket's sell price every 2 s for 5 minutes after an order could land), grouped into EVENTS as in lf_patterns.py
(bookmakers jumping within 20 s of each other = one event). Timing as the bot: decision 2 s after the jump (the jump
must still stand), the order lands 2 s later.
Each event is followed down the funnel:
  1 every event                    (spread 6c or less)
  2 the jump is 8 pt+              (the bot's minimum)
  3 POLYMARKET CATCHES UP          its sell price reaches the decision sell price + 75% of the jump within 5 minutes
                                   (what the charts show as 'a catch-up')
  4 ... AFTER our order lands      at the landing it had made under 50% of the jump (the rest still to come)
  5 ... AND IT PAYS                bought at the buy price at the landing, sold when the sell price reaches the
                                   decision sell price + the jump (else at 5 min), 1c off the sale, both fees: > 0
Then: where the catch-ups that do NOT pay lose (made before we could buy / spread / fee / too small), per game, and
what an ideal bot - one that knew which events reach stage 5 - would make, against what all stage-2 events make.
usage: lf_funnel.py      -> ~/swifteye/research/lf_funnel.txt
"""
import os, sys, statistics as st, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lf_patterns as LP

PS = 2
fee = lambda p: 0.05 * p * (1 - p) if 0 < p < 1 else 0.0
C = LP.load(); E = [LP.measure(e) for e in LP.events(C)]
E = [e for e in E if e.get('best') is not None and e['spread'] <= 0.06 + 1e-9]
games = len({e['m'] for e in E})


def walk(e):
    pb = e['pb']; size = e['size']; bd = e['bid_d']; a = e['ask_e']
    path = [(k * PS, pb[k] / 100.0) for k in range(len(pb)) if pb[k] != 255]
    if not path: return None
    c75 = next((s for s, v in path if v >= bd + 0.75 * size - 1e-9), None)
    made_at_landing = (e['bid_e'] - bd) / size
    tgt = bd + size
    hit = next(((s, v) for s, v in path if v >= tgt - 1e-9), None)
    x = (hit[1] if hit else path[-1][1]) - 0.01
    n = 12.0 / a; pnl = n * (x - a) - n * (fee(a) + fee(max(x, 0.001)))
    return dict(c75=c75, made=made_at_landing, pnl=pnl, hit=hit is not None, gross=(x + 0.01 - a), spread=e['ask_e'] - e['bid_e'])


rows = []
for e in E:
    w = walk(e)
    if w: rows.append((e, w))
out = []; P = out.append
P('THE CATCH-UP FUNNEL: %d bookmaker jump events in %d games (17-21 Nov, spread 6c or less)\n' % (len(rows), games))
s1 = rows
s2 = [(e, w) for e, w in s1 if e['size'] >= 0.08 - 1e-9]
s3 = [(e, w) for e, w in s2 if w['c75'] is not None]
s4 = [(e, w) for e, w in s3 if w['made'] < 0.5]
s5 = [(e, w) for e, w in s4 if w['pnl'] > 0]
for lab, X in (('1 every event', s1), ('2 the jump is 8 pt+', s2), ('3 Polymarket catches up 75%+ within 5 min', s3), ('4 ... with half or more still to come when our order lands', s4), ('5 ... and buying then pays', s5)):
    g = len({e['m'] for e, w in X})
    P('  %-62s %6d events (%4.1f a game with any) in %4d games' % (lab, len(X), len(X) / max(1, g), g))
P('')
# where stage-3 catch-ups that do not pay are lost
lostA = [(e, w) for e, w in s3 if w['made'] >= 0.5]
lostB = [(e, w) for e, w in s4 if w['pnl'] <= 0]
P('CATCH-UPS (stage 3: %d) THAT DO NOT PAY:' % len(s3))
P('  %5d (%3.0f%%) half or more was made BEFORE our order could land (2 s after the decision, 4 s after the jump)' % (len(lostA), 100.0 * len(lostA) / max(1, len(s3))))
P('  %5d (%3.0f%%) still to come at the landing, but the buy and sale cost more than was left:' % (len(lostB), 100.0 * len(lostB) / max(1, len(s3))))
if lostB:
    P('        median spread at the landing %.1fc, median gross (sale - buy) %+.1fc, median jump %.1f pt' % (100 * st.median(w['spread'] for e, w in lostB), 100 * st.median(w['gross'] for e, w in lostB), 100 * st.median(e['size'] for e, w in lostB)))
P('  %5d (%3.0f%%) pay (stage 5)' % (len(s5), 100.0 * len(s5) / max(1, len(s3))))
P('')
P('MONEY ($12 a bet, bought at the landing, sold at the target or at 5 min):')
for lab, X in (('every 8 pt+ event (stage 2)', s2), ('the catch-ups (stage 3) - known only afterwards', s3), ('still to come at the landing (stage 4) - known only afterwards', s4)):
    P('  %-62s %6d bets | won %3.0f%% | %+9.2f $ = %+5.2f a bet' % (lab, len(X), 100.0 * sum(1 for e, w in X if w['pnl'] > 0) / max(1, len(X)), sum(w['pnl'] for e, w in X), sum(w['pnl'] for e, w in X) / max(1, len(X))))
P('')
P('BY SPORT, stage 2 -> 3 -> 4 -> 5 (events), and stage 4 as a bet:')
for sp in sorted({e['sport'] for e, w in s2}, key=lambda s: -sum(1 for e, w in s2 if e['sport'] == s)):
    f = lambda X: [x for x in X if x[0]['sport'] == sp]
    a, b, c, d = f(s2), f(s3), f(s4), f(s5)
    if len(a) < 100: continue
    P('  %-11s %6d -> %5d (%3.0f%%) -> %5d (%3.0f%%) -> %5d (%3.0f%%) | stage 4 as a bet: won %3.0f%%, %+.2f $ a bet' % (sp, len(a), len(b), 100.0 * len(b) / len(a), len(c), 100.0 * len(c) / len(a), len(d), 100.0 * len(d) / len(a),
        100.0 * sum(1 for e, w in c if w['pnl'] > 0) / max(1, len(c)), sum(w['pnl'] for e, w in c) / max(1, len(c))))
P('')
P('HOW FAST CATCH-UPS HAPPEN (stage 3): seconds from the order\'s landing to 75%% of the jump: median %d s, middle half %d-%d s; made before the landing (time 0): %d%%' % (
    st.median(w['c75'] for e, w in s3), sorted(w['c75'] for e, w in s3)[len(s3) // 4], sorted(w['c75'] for e, w in s3)[3 * len(s3) // 4], 100.0 * sum(1 for e, w in s3 if w['c75'] == 0) / max(1, len(s3))))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/lf_funnel.txt'), 'w').write(txt + '\n')
