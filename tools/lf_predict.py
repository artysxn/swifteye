"""CAN A CATCH-UP BE PICKED AT THE MOMENT OF THE JUMP? (owner, 22 Nov 2026: "how can we take more bets? if we take 4x
more bets at 60% winrate instead of 1x at 70%, we will make more money"; tools/lf_funnel.py: 32% of 8 pt+ jumps are
caught up, a caught-up bet makes ~+$1.66, a missed one ~-$2.59 -> break-even at ~61% picked right).

Patterns first, a transparent model second, judged only on days it never saw.
  events     tools/lf_build.py jumps grouped as in lf_patterns.py (8 pt+, spread 6c or less), 17-21 Nov
  label      CAUGHT UP = Polymarket's sell price reaches the decision sell price + 75% of the jump within 5 minutes
  the bet    bought at the buy price when the order lands (decision + 2 s), sold when the sell price reaches the
             decision sell price + the jump, else at 5 min; 1c off the sale, both fees, $12 (as lf_funnel.py)
  signals    only what is known at the decision (2 s after the jump): the jump's size; how many bookmaker FAMILIES had
             jumped the same way by then; Polymarket's move in the 10 s and the 60 s before (share of the jump) and
             between 1 s before and the decision; this bookmaker's record in this game (earlier jumps, the share
             Polymarket followed, its median lead); the pair's correlation; spread; price; sport; bookmaker family;
             a pause just before; the hour
  model      per signal, bands; each band's catch-up rate on the TRAIN days (17-19 Nov), smoothed; a jump's score =
             the base log-odds + the sum of each band's log-odds difference (naive Bayes). Ranked on the TEST days
             (20-21 Nov): how often the top 5 / 10 / 20 / 30 / 50% catch up and what the bets make.
usage: lf_predict.py      -> ~/swifteye/research/lf_predict.txt
"""
import os, sys, math, time, bisect, statistics as st, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lf_patterns as LP
import lf_funnel as LF

SPLIT = LP.SPLIT
C = LP.load()
# jumps of every bookmaker by (game, side), to count the families that jumped by the decision (no look-ahead)
by_side = collections.defaultdict(list)
for c in C: by_side[(c['m'], c['o'])].append((c['t'], LP.FAM.get(c['book'], c['book'])))
for k in by_side: by_side[k].sort()
rows = []
for e, w in LF.rows:
    if e['size'] < 0.08 - 1e-9: continue
    L = by_side[(e['m'], e['o'])]; t = e['t']
    i0 = bisect.bisect_left(L, (t - 20_000, '')); i1 = bisect.bisect_right(L, (t + 2000, '~'))
    fams = {f for tt, f in L[i0:i1] if tt >= t - 20_000}
    s = e['sb'] if e['sb'][0] >= 3 else e['sf']
    f = dict(size=100 * e['size'], fams=len(fams), pre60=e['pre'], pre10=e['pre10'], now=((e['bid_d'] + e['ask_d']) / 2 - e['m1']) / e['size'],
             jumps=e['sb'][0], follow=(s[6] if s[1] >= 2 else None), lead=(s[4] if s[1] >= 2 else None), corr=e['corr'], spread=100 * (e['ask_d'] - e['bid_d']),
             price=100 * e['ask_e'], sport=e['sport'], fam=LP.FAM.get(e['book'], e['book']), pause=1 if e['pause'] else 0, hour=int(time.strftime('%H', time.localtime(e['t'] / 1000))) // 4)
    rows.append(dict(f=f, y=1 if w['c75'] is not None else 0, pnl=w['pnl'], train=e['t'] < SPLIT, day=time.strftime('%d', time.localtime(e['t'] / 1000))))
CUTS = dict(size=[10, 12, 15, 20, 30], fams=[2, 3, 4, 6], pre60=[-0.25, -0.05, 0.05, 0.25, 0.6], pre10=[-0.1, 0.0, 0.1, 0.3], now=[-0.1, 0.05, 0.2, 0.5],
            jumps=[1, 3, 8, 20], follow=[0.3, 0.5, 0.7, 0.9], lead=[-10, -3, 3, 10], corr=[50, 80, 92], spread=[1.5, 2.5, 4.5], price=[20, 35, 50, 65, 80])
CAT = ('sport', 'fam', 'pause', 'hour')


def band(k, v):
    if v is None: return 'none'
    if k in CAT: return str(v)
    return str(bisect.bisect_right(CUTS[k], v))


train = [r for r in rows if r['train']]; test = [r for r in rows if not r['train']]
base = sum(r['y'] for r in train) / len(train); lo = lambda p: math.log(p / (1 - p))
W = {}
for k in list(CUTS) + list(CAT):
    cnt = collections.defaultdict(lambda: [0, 0])
    for r in train: b = band(k, r['f'][k]); cnt[b][0] += r['y']; cnt[b][1] += 1
    W[k] = {b: lo((y + 20 * base) / (n + 20)) - lo(base) for b, (y, n) in cnt.items()}     # smoothed toward the base rate
score = lambda r: lo(base) + sum(W[k].get(band(k, r['f'][k]), 0.0) for k in W)
for r in rows: r['s'] = score(r)
out = []; P = out.append
P('CAN A CATCH-UP BE PICKED AT THE JUMP? 8 pt+ jump events: train 17-19 Nov %d (caught up %.0f%%), test 20-21 Nov %d (caught up %.0f%%)\n' % (
    len(train), 100 * base, len(test), 100.0 * sum(r['y'] for r in test) / len(test)))
P('THE PATTERNS (train days): catch-up rate by band, biggest effects first (band: share caught up, events)')
eff = []
for k in W:
    cnt = collections.defaultdict(lambda: [0, 0])
    for r in train: b = band(k, r['f'][k]); cnt[b][0] += r['y']; cnt[b][1] += 1
    rates = [(b, y / n, n) for b, (y, n) in cnt.items() if n >= 40]
    if len(rates) >= 2: eff.append((max(x[1] for x in rates) - min(x[1] for x in rates), k, sorted(rates, key=lambda x: x[0])))
for d, k, rates in sorted(eff, reverse=True):
    lab = lambda b: b if (k in CAT or b == 'none') else ('<%g' % CUTS[k][0] if b == '0' else ('%g+' % CUTS[k][-1] if int(b) == len(CUTS[k]) else '%g-%g' % (CUTS[k][int(b) - 1], CUTS[k][int(b)])))
    P('  %-8s spread of rates %2.0f pt | %s' % (k, 100 * d, '  '.join('%s: %2.0f%% (%d)' % (lab(b), 100 * r, n) for b, r, n in rates)))
P('')
for name, X in (('TRAIN (17-19 Nov, the model was fitted here)', train), ('TEST (20-21 Nov, never seen)', test)):
    X = sorted(X, key=lambda r: -r['s'])
    P('%s - the jumps ranked by the model, best first; $12 a bet, bought at the landing, sold at the target or at 5 min' % name)
    for share in (0.02, 0.05, 0.10, 0.20, 0.30, 0.50, 1.0):
        Y = X[:max(1, int(share * len(X)))]
        P('  top %3.0f%% %5d bets | caught up %3.0f%% | won %3.0f%% | %+9.2f $ = %+5.2f a bet | by day %s' % (100 * share, len(Y), 100.0 * sum(r['y'] for r in Y) / len(Y), 100.0 * sum(1 for r in Y if r['pnl'] > 0) / len(Y), sum(r['pnl'] for r in Y), sum(r['pnl'] for r in Y) / len(Y),
            ' '.join('%s:%+.0f' % (d, sum(r['pnl'] for r in Y if r['day'] == d)) for d in sorted({r['day'] for r in Y}))))
    P('')
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/lf_predict.txt'), 'w').write(txt + '\n')

# ---- a second model that ranks by EXPECTED $ directly (catch-up likely is not the same as profitable)
base_pnl = sum(r['pnl'] for r in train) / len(train)
WD = {}
for k in list(CUTS) + list(CAT):
    acc = collections.defaultdict(lambda: [0.0, 0])
    for r in train: b = band(k, r['f'][k]); acc[b][0] += r['pnl']; acc[b][1] += 1
    WD[k] = {b: (s + 30 * base_pnl) / (n + 30) - base_pnl for b, (s, n) in acc.items()}
for r in rows: r['sd'] = base_pnl + sum(WD[k].get(band(k, r['f'][k]), 0.0) for k in WD)
out2 = []
for name, X in (('TRAIN', train), ('TEST (never seen)', test)):
    X = sorted(X, key=lambda r: -r['sd'])
    out2.append('EXPECTED-$ MODEL, %s: jumps ranked by expected $ (bands fitted on the train days)' % name)
    for share in (0.01, 0.02, 0.05, 0.10, 0.20, 0.30):
        Y = X[:max(1, int(share * len(X)))]
        out2.append('  top %3.0f%% %5d bets | caught up %3.0f%% | won %3.0f%% | %+9.2f $ = %+5.2f a bet | by day %s' % (100 * share, len(Y), 100.0 * sum(r['y'] for r in Y) / len(Y), 100.0 * sum(1 for r in Y if r['pnl'] > 0) / len(Y), sum(r['pnl'] for r in Y), sum(r['pnl'] for r in Y) / len(Y),
            ' '.join('%s:%+.0f' % (d, sum(r['pnl'] for r in Y if r['day'] == d)) for d in sorted({r['day'] for r in Y}))))
    Y = [r for r in X if r['sd'] > 0]
    out2.append('  every jump the model expects to pay (> $0): %d bets | caught up %.0f%% | won %.0f%% | %+.2f $ = %+.2f a bet' % (len(Y), 100.0 * sum(r['y'] for r in Y) / max(1, len(Y)), 100.0 * sum(1 for r in Y if r['pnl'] > 0) / max(1, len(Y)), sum(r['pnl'] for r in Y), sum(r['pnl'] for r in Y) / max(1, len(Y))))
    out2.append('')
# ---- the bot's own kind of rule on the same jumps, for comparison
for name, X in (('TRAIN', train), ('TEST', test)):
    for lab, fn in (('8 pt+, 2+ families by the decision, followed 50%+, spread 4c-, no pause, 90c-', lambda f: f['fams'] >= 2 and f['follow'] is not None and f['follow'] >= 0.5 and f['spread'] <= 4.5 and not f['pause'] and f['price'] <= 90),
                    ('... and Polymarket still in the 10 s before (under 10% of the jump)', lambda f: f['fams'] >= 2 and f['follow'] is not None and f['follow'] >= 0.5 and f['spread'] <= 4.5 and not f['pause'] and f['price'] <= 90 and abs(f['pre10']) < 0.1)):
        Y = [r for r in X if fn(r['f'])]
        out2.append('BOT-LIKE RULE, %-5s %-70s %4d bets | caught up %3.0f%% | won %3.0f%% | %+8.2f $ = %+5.2f a bet' % (name, lab, len(Y), 100.0 * sum(r['y'] for r in Y) / max(1, len(Y)), 100.0 * sum(1 for r in Y if r['pnl'] > 0) / max(1, len(Y)), sum(r['pnl'] for r in Y), sum(r['pnl'] for r in Y) / max(1, len(Y))))
txt2 = '\n'.join(out2); print(txt2)
open(os.path.expanduser('~/swifteye/research/lf_predict.txt'), 'a').write('\n' + txt2 + '\n')
