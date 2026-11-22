"""SEARCH THE RULES for more money, honestly (owner, 22 Nov 2026: "test more rule changes to make more or less money. current
highest is +18 over 5 days, which is nothing").

Base: the extended replay (SWEEP_DATA=v18, strict gap checks, 17 Nov 05:00 - 22 Nov 01:00), real-money sales, the bot as it
runs NOW (the lagged rule's 'followed' on Polymarket's own clock - the feed is on time), and the same with 3 s / 5 s slack
(tools/lag_shift.py's rebuild). Random combinations of every rule the replay holds + exits (catch-up target, give-up time)
+ the slack, scored by $ (flat $12 a trade) per day.
HONEST CHECK - leave one day out: for each day, the combination with the most $ on the OTHER four days is picked and
scored on the day it never saw; the sum over the five held-out days is what searching really adds. Today's rules
(exact timing) are scored on the same days for comparison. The best combinations are also run through the $40 wallet
(tools/wallet40.py's real-money sizing).
usage: SWEEP_DATA=v18 python3 rule_search.py [combinations per slack, default 2000]      -> ~/swifteye/research/rule_search.txt
"""
import os, sys, io, contextlib, random, collections, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SWEEP_DATA', 'v18'); os.environ['SHIFTS'] = '0,3000,5000'; os.environ['FROM_MS'] = '0'
N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
with contextlib.redirect_stdout(io.StringIO()):
    import lag_shift as LS
    import wallet40 as W
S = LS.S
VARIANTS = {'exact': LS.rows_with(0), '+3 s': LS.rows_with(3000), '+5 s': LS.rows_with(5000)}
days = sorted({c['_day'] for c in S.ROWS})
SPACE = dict(min_move=[0.07, 0.08, 0.09, 0.10], max_followed=[0.0, 0.05, 0.10, 0.15], fams=[1, 2, 3], cons=[0.0, 0.02, 0.03, 0.05],
             max_spread=[0.02, 0.03, 0.04, 0.06], min_edge=[0.0, 0.02, 0.04, 0.06], lag=[None, 0.3, 0.4, 0.5, 0.6], weak_cap=[None, 0.6, 0.7, 0.8],
             gap=[None, 0.05, 0.10], cap=[0.85, 0.90, 0.95], bb=['none', 'nonus', 'all'], pause=[True, False], catch=[0.5, 0.75, 1.0],
             give=[120, 300, 900, 1800], dota_spread=[0.02, None])
B = dict(S.BASE, real_sale=True)


def score(kw, rows):
    T = S.sim(dict(B, **kw), rows=rows); by = collections.defaultdict(float); n = collections.Counter()
    for t in T: by[t['c']['_day']] += t['pnl']; n[t['c']['_day']] += 1
    return [by[d] for d in days], [n[d] for d in days]


random.seed(22)
res = []          # (variant, kw, per-day $, per-day trades)
t0 = time.time()
base = {}
for v, rows in VARIANTS.items():
    base[v] = score({}, rows)
    res.append((v, {}, *base[v]))
    for i in range(N):
        kw = {k: random.choice(vals) for k, vals in SPACE.items()}
        res.append((v, kw, *score(kw, rows)))
    print('  %s: %d combinations, %.0f s' % (v, N, time.time() - t0), flush=True)
out = []; P = out.append
bx = base['exact']
P('RULE SEARCH: %d combinations (%d per slack setting) on the extended replay %s Nov, real-money sales, $12 flat a trade' % (len(res), N, '/'.join(days)))
P("today's rules, exact timing (the bot now): %d trades %+.2f $ | by day %s" % (sum(bx[1]), sum(bx[0]), ' / '.join('%+.1f' % x for x in bx[0])))
P("today's rules, +3 s slack: %+.2f $ | +5 s slack: %+.2f $\n" % (sum(base['+3 s'][0]), sum(base['+5 s'][0])))
diff = lambda kw: ', '.join('%s=%s' % (k, v) for k, v in kw.items() if S.BASE.get(k, '?') != v) or "today's rules"
P('BEST 12 BY TOTAL $ (in-sample: picked on the same days they are scored on)')
for v, kw, pd, nd in sorted(res, key=lambda r: -sum(r[2]))[:12]:
    P('  %+8.2f $ %3d trades | days %s | min day %+.1f | slack %s | %s' % (sum(pd), sum(nd), ' / '.join('%+.1f' % x for x in pd), min(pd), v, diff(kw)))
P('\nBEST 8 WITH EVERY DAY >= 0 (in-sample)')
for v, kw, pd, nd in sorted([r for r in res if min(r[2]) >= 0], key=lambda r: -sum(r[2]))[:8]:
    P('  %+8.2f $ %3d trades | days %s | slack %s | %s' % (sum(pd), sum(nd), ' / '.join('%+.1f' % x for x in pd), v, diff(kw)))
# leave one day out
P('\nHONEST CHECK - LEAVE ONE DAY OUT: the combination best on the other 4 days, scored on the day it never saw')
tot_cv = 0.0; tot_base = 0.0
for i, d in enumerate(days):
    pick = max(res, key=lambda r: sum(r[2]) - r[2][i])
    tot_cv += pick[2][i]; tot_base += bx[0][i]
    P('  %s Nov held out: picked (%s, %s) made %+.2f $ on the other days and %+.2f $ (%d trades) on %s Nov | today\'s rules that day %+.2f $' % (
        d, pick[0], diff(pick[1])[:110], sum(pick[2]) - pick[2][i], pick[2][i], pick[3][i], d, bx[0][i]))
P('  SUM over the held-out days: searching %+.2f $ v today\'s rules %+.2f $' % (tot_cv, tot_base))
# $40 wallets for the top 3
P('\n$40 WALLETS (real-money sizing) for the top 3 by total, and today\'s rules:')
for v, kw, pd, nd in [('exact', {}, *bx)] + sorted(res, key=lambda r: -sum(r[2]))[:3]:
    tr, bal, low, peak, dd, sk = W.wallet(VARIANTS[v] if not kw.get('catch') and not kw.get('give') else VARIANTS[v]) if False else (None,) * 6
    p = dict(B, **kw)
    # wallet40.wallet uses today's exits; rebuild with this combination's rules and exits
    ci = S.CATCH.index(p.get('catch', 1.0)); gi = S.GIVE.index(p.get('give', 300))
    bal = 40.0; openp = []; recent, busy = {}, {}; n_tr = 0; won = 0; low = peak = 40.0; dd = 0.0
    for c in VARIANTS[v]:
        if not S.gate(c, p, ci): continue
        m = c['match_id']; tf = c['tf']
        if p['lock'] and m in recent and tf - recent[m] < p['lock'] * 1000: continue
        recent[m] = tf
        if c['nofill']: continue
        x = c['exits'][ci][gi]
        if not x or (p['one_per_game'] and busy.get(m, 0) > tf): continue
        for o in [o for o in openp if o[0] <= tf]:
            bal += o[2]; openp.remove(o); low = min(low, bal); peak = max(peak, bal); dd = max(dd, peak - bal)
        cx = c['cx']; weak = cx['jumps'] == 0 or cx['follow'] - cx['lead'] < 0.2
        stake = min(min(0.25 * bal, 12.0) * (0.5 if weak else 1.0), bal - sum(o[1] for o in openp))
        if stake / max(c['_px'], 0.01) < 5: continue
        t = S.trade(c, x, dict(p, stake=stake), ci)
        if t is None: continue
        busy[m] = x[0]; openp.append((x[0], t[1], t[0])); n_tr += 1; won += t[0] > 0
    for o in sorted(openp):
        bal += o[2]; low = min(low, bal); peak = max(peak, bal); dd = max(dd, peak - bal)
    P('  %-70s $40 -> $%.2f | %d trades, won %d | worst drop $%.2f' % ((v + ': ' + diff(kw))[:70], bal, n_tr, won, dd))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/rule_search.txt'), 'w').write(txt + '\n')
json.dump([dict(slack=v, rules=kw, by_day=pd, trades=nd) for v, kw, pd, nd in sorted(res, key=lambda r: -sum(r[2]))[:200]], open(os.path.expanduser('~/swifteye/research/rule_search_top200.json'), 'w'))
