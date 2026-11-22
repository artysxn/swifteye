"""Patterns, step 3: BEFORE costs, does Polymarket's sell price move the bookmaker's way after a jump it has not made?
Move of the sell price from the decision to H seconds after a buy could land, in cents; and what a round trip costs."""
import os, sys, time, statistics as st, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lf_patterns as LP
C = LP.load(); E = [LP.measure(e) for e in LP.events(C)]
E = [e for e in E if e.get('best') is not None and e['spread'] <= 0.06 + 1e-9]
def mv(e, h): return None if e['r'][h] is None else 100 * e['r'][h] * e['size']
def cost(e):   # cents a share: the spread crossed once in and once out, 1c on the sale, both fees
    a = e['ask_e']; b = e['bid_e']; return 100 * ((a - b) + 0.01 + LP.fee(a) + LP.fee(b))
def s_(e): return e['sb'] if e['sb'][0] >= 3 else e['sf']
def row(name, S):
    S = [e for e in S if mv(e, 60) is not None and mv(e, 300) is not None]
    if len(S) < 30: return
    m = lambda h: st.mean(mv(e, h) for e in S if mv(e, h) is not None)
    up3 = 100.0 * sum(1 for e in S if mv(e, 60) >= 3) / len(S); dn3 = 100.0 * sum(1 for e in S if mv(e, 60) <= -3) / len(S)
    print('  %-46s %6d | jump %4.1fc | sell price moved: 10 s %+5.2fc  30 s %+5.2fc  60 s %+5.2fc  120 s %+5.2fc  300 s %+5.2fc | up 3c+ at 60 s %2.0f%%, down 3c+ %2.0f%% | a round trip costs %4.1fc' % (
        name, len(S), 100 * st.mean(e['size'] for e in S), m(10), m(30), m(60), m(120), m(300), up3, dn3, st.mean(cost(e) for e in S)))
for lab, part in (('17-19 Nov', lambda e: e['t'] < LP.SPLIT), ('20-21 Nov', lambda e: e['t'] >= LP.SPLIT)):
    S = [e for e in E if part(e)]; print('\n' + lab)
    row('ALL EVENTS', S)
    row('4+ bookmaker groups jumped', [e for e in S if len(e['fams']) >= 4])
    row('1 group only', [e for e in S if len(e['fams']) == 1])
    row('correlation 92+', [e for e in S if e['corr'] is not None and e['corr'] >= 92])
    row('20+ earlier jumps in the game', [e for e in S if e['sb'][0] >= 20])
    row('bookmaker usually AHEAD (delay 3 s+)', [e for e in S if s_(e)[1] >= 2 and s_(e)[4] is not None and s_(e)[4] > 3])
    row('Polymarket usually ahead (delay under -3 s)', [e for e in S if s_(e)[1] >= 2 and s_(e)[4] is not None and s_(e)[4] < -3])
    row('Polymarket went the OTHER way in the last minute', [e for e in S if e['pre'] <= -0.25])
    row('Polymarket flat in the last minute', [e for e in S if -0.05 < e['pre'] < 0.05])
    row('tennis', [e for e in S if e['sport'] == 'tennis']); row('cs2', [e for e in S if e['sport'] == 'cs2']); row('basketball', [e for e in S if e['sport'] == 'basketball']); row('dota2', [e for e in S if e['sport'] == 'dota2']); row('football', [e for e in S if e['sport'] == 'football'])
    row('jump 8-12pt', [e for e in S if 0.08 <= e['size'] < 0.12]); row('jump 12pt+', [e for e in S if e['size'] >= 0.12])
    row('4+ groups, corr 92+, cs2 or tennis, no pause', [e for e in S if len(e['fams']) >= 4 and e['corr'] is not None and e['corr'] >= 92 and e['sport'] in ('cs2', 'tennis') and e['pause'] == 0])
    row('  ... and jump 8pt+', [e for e in S if len(e['fams']) >= 4 and e['corr'] is not None and e['corr'] >= 92 and e['sport'] in ('cs2', 'tennis') and e['pause'] == 0 and e['size'] >= 0.08])
    row('  ... and price 60c+, spread 1c', [e for e in S if len(e['fams']) >= 4 and e['corr'] is not None and e['corr'] >= 92 and e['sport'] in ('cs2', 'tennis') and e['pause'] == 0 and e['ask_e'] >= 0.6 and e['spread'] <= 0.011])
    row('the bookmaker kept going (known only afterwards)', [e for e in S if e['l30'] is not None and e['l30'] >= 1.1])
    row('the bookmaker took it back (known only afterwards)', [e for e in S if e['l30'] is not None and e['l30'] <= 0.25])
