"""Do the bot's own trades come from the 'worse' bookmakers, and does blocking them make money? (owner, 22 Nov 2026: "are certain
bookmakers worse than others (and may give false signals)?"; the pattern: tools/book_quality.py).
The bot as it runs (v18 replay, 'followed' on Polymarket's clock + 5 s, 15-min give-up, real sales, $12 flat). Each move is
joined to its jump in lf_cands.pickle (same game, side, bookmaker, within 3 s) for 'last to move' / 'moved alone'.
Blocks tried: the trigger in a family whose jumps are mostly late (pinnacle, m88, betika, altenar), the wider middle group too
(duel, betcity, olimp, thunderpick, mostbet), and any trigger that was last to move. Leave one day out over the choices."""
import os, sys, io, contextlib, pickle, collections, bisect
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lf_patterns import FAM
C = pickle.load(open(os.path.expanduser('~/swifteye/research/lf_cands.pickle'), 'rb'))['cands']
LK = collections.defaultdict(list)
for c in C:
    if c['size'] < 0.06: continue
    pos = None
    if c['n_others'] >= 2 and c['cons_gap'] is not None:
        pos = (c['cons_gap'] + (c['bid_d'] + c['ask_d']) / 2 - c['base']) / c['size']
    kind = None if pos is None else 'alone' if pos < 0.25 and (c['moved_same'] or 0) == 0 else 'last' if pos >= 0.75 and (c['cons_move10'] or 0) < 0.02 else 'with'
    LK[(c['m'], c['book'])].append((c['t'], kind))
del C
for k in LK: LK[k].sort()
os.environ.setdefault('SWEEP_DATA', 'v18'); os.environ['SHIFTS'] = '5000'; os.environ['FROM_MS'] = '0'
with contextlib.redirect_stdout(io.StringIO()):
    import lag_shift as LS
S = LS.S; ROWS = LS.rows_with(5000)
B = dict(S.BASE, real_sale=True, give=900)
found = 0
for c in ROWS:
    L = LK.get((c['match_id'], c['trigger'])); c['_kind'] = None
    if L:
        i = bisect.bisect_left(L, (c['t0'] - 3000, ''))
        best = None
        for t, k in L[i:i + 6]:
            if abs(t - c['t0']) <= 3000 and (best is None or abs(t - c['t0']) < best[0]): best = (abs(t - c['t0']), k)
        if best: c['_kind'] = best[1] or 'unknown'; found += 1
fam = lambda c: FAM.get(c['trigger'], c['trigger'])
days = sorted({c['_day'] for c in ROWS})
T = S.sim(B, rows=ROWS)
out = ['THE BOT\'S OWN TRADES BY TRIGGER BOOKMAKER (v18 replay, the bot as it runs: 5 s slack, 15-min give-up, real sales, $12 flat). %d trades, %+.2f $' % (len(T), sum(t['pnl'] for t in T)),
       '(moves joined to their bookmaker jump: %d of %d)\n' % (found, len(ROWS))]
for lab, key in (('TRIGGER FAMILY', fam), ('KIND OF TRIGGER JUMP', lambda c: c['_kind'] or 'not joined')):
    out.append(lab)
    G = collections.defaultdict(list)
    for t in T: G[key(t['c'])].append(t)
    for k, L in sorted(G.items(), key=lambda x: -len(x[1])):
        out.append('  %-22s %3d trades, won %3d (%3.0f%%), %+7.2f $, %+5.1fc per $ staked' % (k, len(L), sum(t['pnl'] > 0 for t in L), 100.0 * sum(t['pnl'] > 0 for t in L) / len(L), sum(t['pnl'] for t in L), 100 * sum(t['pnl'] for t in L) / (12.0 * len(L))))
    out.append('')
BAD = {'pinnacle', 'm88', 'betika', 'altenar'}; MID = BAD | {'duel', 'betcity', 'olimp', 'thunderpick', 'fonbet'}
CH = [("today's rules", lambda c: True), ('no late-line families', lambda c: fam(c) not in BAD), ('no late or middle families', lambda c: fam(c) not in MID),
      ('no trigger that was last to move', lambda c: c['_kind'] != 'last'), ('no trigger that moved alone', lambda c: c['_kind'] != 'alone')]
res = []
out.append('BLOCKS (the blocked moves are simply not candidates; locks and the rest as today)')
for name, ok in CH:
    Tb = S.sim(B, rows=[c for c in ROWS if ok(c)]); by = collections.defaultdict(float)
    for t in Tb: by[t['c']['_day']] += t['pnl']
    res.append((name, [by[d] for d in days]))
    out.append('  %-34s %3d trades, won %3d, %+7.2f $ | by day %s' % (name, len(Tb), sum(t['pnl'] > 0 for t in Tb), sum(t['pnl'] for t in Tb), ' / '.join('%+.1f' % by[d] for d in days)))
tot = 0.0
for i, d in enumerate(days):
    pick = max(res, key=lambda r: sum(r[1]) - r[1][i]); tot += pick[1][i]
out.append('  leave one day out over these: %+.2f $ v today\'s rules %+.2f $' % (tot, sum(res[0][1])))
# the wider pool: every move passing the rules except the lagged rule (more trades, the same question)
OPEN = dict(B, lag=None)
To = S.sim(OPEN, rows=ROWS)
out.append('\nWIDER POOL (lagged rule off, to see more trades): %d trades %+.2f $' % (len(To), sum(t['pnl'] for t in To)))
G = collections.defaultdict(list)
for t in To: G[(fam(t['c']) in BAD and 'late-line families') or (fam(t['c']) in MID and 'middle families') or 'the rest'].append(t)
for k, L in G.items():
    out.append('  %-22s %3d trades, won %3d (%3.0f%%), %+7.2f $' % (k, len(L), sum(t['pnl'] > 0 for t in L), 100.0 * sum(t['pnl'] > 0 for t in L) / len(L), sum(t['pnl'] for t in L)))
G = collections.defaultdict(list)
for t in To: G[t['c']['_kind'] or 'not joined'].append(t)
for k, L in G.items():
    out.append('  trigger %-14s %3d trades, won %3d (%3.0f%%), %+7.2f $' % (k, len(L), sum(t['pnl'] > 0 for t in L), 100.0 * sum(t['pnl'] > 0 for t in L) / len(L), sum(t['pnl'] for t in L)))
txt = '\n'.join(out); print(txt); open(os.path.expanduser('~/swifteye/research/book_block.txt'), 'w').write(txt + '\n')
