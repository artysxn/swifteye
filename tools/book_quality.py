"""ARE SOME BOOKMAKERS WORSE THAN OTHERS - do some give FALSE SIGNALS? (owner, 22 Nov 2026)
Data: tools/lf_build.py v2 (~/swifteye/research/lf_cands.pickle): every jump of 8 pt+ of every bookmaker, 17-22 Nov, in games
with a Polymarket market, where Polymarket had NOT made the move yet and the jump still stood 2 s later (the bot's decision);
Polymarket's bid and the bookmaker's own line every 2 s for 5 min after an order could land.
Per bookmaker:
  jumps      8 pt+ jumps that reached a decision
  undone 60s / 5m   the bookmaker's own line back under half its jump 60 s / 5 min later (a blip: a FALSE signal)
  alone      the other bookmakers had NOT moved (still within a quarter of the jump from the old price): it moved by itself
  last       the others ALREADY stood at the new price and did not move in the 10 s before: a late line catching up (stale)
  PM caught  Polymarket's bid reached the decision bid + 75% of the jump within 5 min (the bot's win case)
  PM +60s    Polymarket's bid change 60 s after the order lands, in cents (the jump's way)
  17-19 / 20-22   'PM caught' on the two halves of the days: a real property of the bookmaker holds on both
usage: book_quality.py      -> ~/swifteye/research/book_quality.txt
"""
import os, sys, pickle, collections, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lf_patterns import FAM
C = pickle.load(open(os.path.expanduser('~/swifteye/research/lf_cands.pickle'), 'rb'))['cands']
SPLIT = 1795129200000          # 20 Nov 00:00 Norway
J = [c for c in C if c['size'] >= 0.08]
del C


def m(c):
    s = c['size']; b100 = c['base'] * 100; s100 = s * 100
    pl = c['pl']; pb = c['pb']
    und60 = pl[30] != 255 and pl[30] < b100 + 0.5 * s100
    und5 = pl[149] != 255 and pl[149] < b100 + 0.5 * s100
    pos = None
    if c['n_others'] >= 2 and c['cons_gap'] is not None:
        lvl = c['cons_gap'] + (c['bid_d'] + c['ask_d']) / 2; pos = (lvl - c['base']) / s
    alone = pos is not None and pos < 0.25 and (c['moved_same'] or 0) == 0
    last = pos is not None and pos >= 0.75 and (c['cons_move10'] or 0) < 0.02
    tgt = c['bid_d'] * 100 + 0.75 * s100
    bids = [x for x in pb if x != 255]
    caught = bool(bids) and max(bids) >= tgt
    d60 = (pb[30] - c['bid_d'] * 100) if pb[30] != 255 else None
    return und60, und5, alone, last, caught, d60, pos is not None


def table(key, title, minimum=150):
    G = collections.defaultdict(list)
    for c in J: G[key(c)].append(c)
    out = ['\n' + title, '  %-12s %6s %6s %9s %8s %7s %7s %9s %8s %7s %7s' % ('', 'jumps', 'games', 'undone60s', 'undone5m', 'alone', 'last', 'PM caught', 'PM +60s', '17-19', '20-22')]
    rows = []
    for k, L in G.items():
        if len(L) < minimum: continue
        M = [m(c) for c in L]; P = [x for x in M if x[6]]
        cau = lambda S: 100.0 * sum(x[4] for x in S) / max(1, len(S))
        a = [mm for c, mm in zip(L, M) if c['t'] < SPLIT]; b = [mm for c, mm in zip(L, M) if c['t'] >= SPLIT]
        d60 = [x[5] for x in M if x[5] is not None]
        rows.append((cau(M), '  %-12s %6d %6d %8.0f%% %7.0f%% %6.0f%% %6.0f%% %8.0f%% %+7.2fc %6.0f%% %6.0f%%' % (k, len(L), len({c['m'] for c in L}), 100.0 * sum(x[0] for x in M) / len(M),
            100.0 * sum(x[1] for x in M) / len(M), 100.0 * sum(x[2] for x in P) / max(1, len(P)), 100.0 * sum(x[3] for x in P) / max(1, len(P)), cau(M),
            st.mean(d60) if d60 else 0, cau(a), cau(b))))
    out += [r for _, r in sorted(rows, reverse=True)]
    return out


out = ['ARE SOME BOOKMAKERS WORSE? %d jumps of 8 pt+ that reached a decision, 17-22 Nov (sorted by how often Polymarket caught up)' % len(J)]
out += table(lambda c: FAM.get(c['book'], c['book']), 'BY FAMILY (bookmakers sharing one odds feed count as one)')
out += table(lambda c: c['book'], 'BY BOOKMAKER')
# alone / last / normal, all books together: what each kind of jump is worth
K = collections.defaultdict(list)
for c in J:
    x = m(c)
    if not x[6]: continue
    K['moved alone' if x[2] else 'last to move (stale)' if x[3] else 'with others / in between'].append(x)
out.append('\nKIND OF JUMP, every bookmaker together')
for k, L in K.items():
    out.append('  %-26s %6d jumps, undone in 60 s %3.0f%%, PM caught %3.0f%%, PM +60s %+.2fc' % (k, len(L), 100.0 * sum(x[0] for x in L) / len(L), 100.0 * sum(x[4] for x in L) / len(L), st.mean([x[5] for x in L if x[5] is not None])))
txt = '\n'.join(out); print(txt); open(os.path.expanduser('~/swifteye/research/book_quality.txt'), 'w').write(txt + '\n')
