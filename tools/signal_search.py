"""Signals in the owner's sense (21 Nov 2026):
  LOSING signal   a trait of trades the bot takes NOW such that leaving those trades out makes MORE money
  WINNING signal  a trait of moves the bot does NOT take now such that taking them makes more than it loses
Both measured as the change in total profit of the whole replay (clean trades of 17-20 Nov, the bot's own locks and
one position a game, $12 a trade), not as a win rate. A signal is kept only if the change is positive overall, not
negative on any of the four days, and positive on both halves of the games (split by game number).
usage: signal_search.py"""
import sys, os, json, random, hashlib
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.argv = ['x', 'lib']
import tells as T
S = T.S
random.seed(77)
NOW = dict(S.BASE, legacy=False, skip=('dota2',), min_move=0.08, lag=0.5)
orig = S.gate
ci = 2
half = lambda m: int(hashlib.md5(str(m).encode()).hexdigest()[:6], 16) % 2
_r = {}


def rec(c):
    k = id(c)
    if k not in _r: _r[k] = T.at_buy(c)
    return _r[k]


def sim(fn):
    S.gate = lambda c, p, cc: fn(c)
    try: return S.sim(NOW)
    finally: S.gate = orig


base_ok = lambda c: orig(c, NOW, ci)
BASE = sim(base_ok)
BT = sum(t['pnl'] for t in BASE)
bg = defaultdict(float); bd = defaultdict(float); bh = defaultdict(float)
for t in BASE:
    bg[t['c']['match_id']] += t['pnl']; bd[t['c']['_day']] += t['pnl']; bh[half(t['c']['match_id'])] += t['pnl']
base_keys = {(t['c']['match_id'], t['c']['t0']) for t in BASE}


def judge(tr):
    tot = sum(t['pnl'] for t in tr)
    g = defaultdict(float); d = defaultdict(float); h = defaultdict(float)
    for t in tr:
        g[t['c']['match_id']] += t['pnl']; d[t['c']['_day']] += t['pnl']; h[half(t['c']['match_id'])] += t['pnl']
    ks = list(set(g) | set(bg)); v = sorted(sum(g.get(k, 0) - bg.get(k, 0) for k in (random.choice(ks) for _ in ks)) for _ in range(800))
    keys = {(t['c']['match_id'], t['c']['t0']) for t in tr}
    added = [t for t in tr if (t['c']['match_id'], t['c']['t0']) not in base_keys]; gone = [t for t in BASE if (t['c']['match_id'], t['c']['t0']) not in keys]
    days = [d.get(x, 0) - bd.get(x, 0) for x in ('17', '18', '19', '20')]; halves = [h.get(x, 0) - bh.get(x, 0) for x in (0, 1)]
    return dict(delta=tot - BT, tot=tot, n=len(tr), added=added, gone=gone, days=days, halves=halves, lo=v[40], hi=v[760],
                ok=(tot - BT > 0.5 and min(days) >= -0.5 and min(halves) >= -0.01 and sum(x > 0.2 for x in days) >= 2))


def q(f, op, x):
    def fn(c):
        v = rec(c)[f]
        if v is None: return False
        return v >= x if op == '>=' else v <= x if op == '<=' else v == x
    return fn


AND = lambda *fs: (lambda c: all(f(c) for f in fs))
strong = q('score', '>=', 0.2); weak = lambda c: not strong(c)
trend_ok = q('book_tr10', '>=', -0.03); trend_bad = q('book_tr10', '<=', -0.0301)
no_drift = q('drift60', '<=', 0.01); drift = q('drift60', '>=', 0.0101)
tennis = lambda c: c['sport'] == 'tennis'; cs2 = lambda c: c['sport'] == 'cs2'

print(f"TODAY'S RULES on the clean replay: {len(BASE)} trades, {sum(t['pnl'] > 0 for t in BASE)} won, ${BT:+.2f}; by day " + ' '.join(f"{bd.get(x, 0):+.1f}" for x in ('17', '18', '19', '20')))


def show(name, j, kind):
    grp = j['gone'] if kind == 'drop' else j['added']
    other = j['added'] if kind == 'drop' else j['gone']
    side = f"; lets in {len(other)} other ({sum(t['pnl'] for t in other):+.2f})" if kind == 'drop' and other else (f"; pushes out {len(other)} ({sum(t['pnl'] for t in other):+.2f})" if other else '')
    print(f"   {'KEEP ' if j['ok'] else '     '}{name:66s} {len(grp):3d} trades, {sum(t['pnl'] > 0 for t in grp):2d} won, their result ${sum(t['pnl'] for t in grp):+7.2f}{side} | profit {j['delta']:+7.2f} (90% range {j['lo']:+.0f} to {j['hi']:+.0f}) | days " + ' '.join(f"{x:+5.1f}" for x in j['days']) + " | halves " + ' '.join(f"{x:+5.1f}" for x in j['halves']))


print("\nA. LOSING SIGNALS: trades the bot takes now, left out. 'their result' = what those trades made (negative = dropping them pays)")
DROPS = [
    ("weak game (delay score under 0.2)", weak), ("bookmaker's 10-min trend against us (3 pt+)", trend_bad), ("Polymarket had drifted our way (1c+ in 60 s)", drift),
    ("5+ bookmaker groups had moved in the last 60 s", q('n_fam60', '>=', 5)), ("4+ bookmaker groups had moved in the last 60 s", q('n_fam60', '>=', 4)),
    ("price paid over 60c", q('price', '>=', 0.6001)), ("price paid over 70c", q('price', '>=', 0.7001)), ("price paid 50-70c", AND(q('price', '>=', 0.50), q('price', '<=', 0.70))),
    ("price paid under 30c", q('price', '<=', 0.30)), ("spread 3c or more", q('spread', '>=', 0.0299)), ("under half of the bookmaker groups moved", q('fam_share', '<=', 0.4999)),
    ("tennis", tennis), ("CS2", cs2), ("tennis in a weak game", AND(tennis, weak)), ("tennis, price over 60c", AND(tennis, q('price', '>=', 0.6001))),
    ("tennis, bookmaker trend against", AND(tennis, trend_bad)), ("tennis, Polymarket had drifted our way", AND(tennis, drift)),
    ("weak game AND bookmaker trend against", AND(weak, trend_bad)), ("weak game AND Polymarket drifted our way", AND(weak, drift)), ("trend against AND Polymarket drifted", AND(trend_bad, drift)),
    ("weak game, price over 60c", AND(weak, q('price', '>=', 0.6001))), ("bookmaker trend against, price over 60c", AND(trend_bad, q('price', '>=', 0.6001))),
    ("3+ Polymarket re-quotes before the buy", q('q_upd', '>=', 3)), ("move under 10 pt", q('move', '<=', 0.0999)), ("move 15 pt or more", q('move', '>=', 0.15)),
    ("lagged earlier under 70%", q('follow', '<=', 0.6999)), ("Polymarket moved first in over 80% of earlier jumps", q('lead', '>=', 0.8)),
    ("first hour of the game", q('mins_in', '<=', 60)), ("after 2.5 hours of the game", q('mins_in', '>=', 150)),
    ("the previous move in this game went the other way", lambda c: rec(c)['flip'] is True), ("3+ moves in this game in the last 2 min", q('n2', '>=', 3)),
    ("bookmakers were 8c+ below Polymarket before the move", q('gap', '<=', -0.08)), ("move 4+ times the usual noise", q('z', '>=', 4)),
    ("bookmakers' average moved under half of the trigger's move", q('cons_share', '<=', 0.4999)), ("trigger is one of the weak feeds", lambda c: c['_weak']),
    ("the other bookmakers had already moved most of the way", q('toward', '>=', 0.75)), ("$200+ on offer at the best price", q('depth_best', '>=', 200)),
    ("7+ bookmaker groups quote the game", q('fams', '>=', 7)), ("room after costs under 6c", q('room', '<=', 0.0599)),
]
out = []
for name, cond in DROPS:
    j = judge(sim(lambda c, cond=cond: base_ok(c) and not cond(c)))
    if j['gone']: out.append((j['delta'], name, j))
for dl, name, j in sorted(out, key=lambda x: -x[0])[:18]: show(name, j, 'drop')
kept_drops = [(n, j) for d, n, j in out if j['ok']]

print("\nB. WINNING SIGNALS: moves the bot does not take now, taken. 'their result' = what the added trades made")
RELAX = [("moves of 7 to 8 pt", dict(min_move=0.07)), ("Polymarket already followed 5-15%", dict(max_followed=0.15)), ("lagged earlier 30-50%", dict(lag=0.3)), ("lagged earlier 0-50%", dict(lag=0.0)),
         ("no earlier-lag record needed", dict(lag=None)), ("bookmakers' average moved under 3 pt", dict(cons=0.0)), ("only one bookmaker group moved", dict(fams=1)), ("spread 4-6c", dict(max_spread=0.06)),
         ("room after costs 2-4c", dict(min_edge=0.02)), ("price paid 90-95c", dict(cap=0.95)), ("basketball outside the US", dict(bb='nonus')), ("all basketball", dict(bb='all')),
         ("Dota 2", dict(skip=())), ("right after a pause in play", dict(pause=False)), ("bookmakers already 10+ pt above Polymarket", dict(gap=None))]
QUAL = [("(all of them)", None), ("in a strong game", strong), ("bookmaker trend not against", trend_ok), ("Polymarket had not drifted our way", no_drift), ("other bookmakers already following", q('oth_buy', '>=', 0.10)),
        ("3 or fewer groups moved in the last 60 s", q('n_fam60', '<=', 3)), ("price paid 60c or less", q('price', '<=', 0.60)), ("spread 2c or less", q('spread', '<=', 0.0201)),
        ("half+ of the bookmaker groups moved", q('fam_share', '>=', 0.5)), ("CS2", cs2), ("tennis", tennis), ("no Polymarket re-quote before the buy", q('q_upd', '==', 0)), ("move 12 pt+", q('move', '>=', 0.12)),
        ("lagged earlier 70%+", q('follow', '>=', 0.7)), ("strong game AND trend not against", AND(strong, trend_ok)), ("strong game AND no drift", AND(strong, no_drift)), ("trend not against AND no drift", AND(trend_ok, no_drift))]
res = []
for rn, rp in RELAX:
    P2 = dict(NOW, **rp); pool_ok = lambda c, P2=P2: orig(c, P2, ci)
    for qn, qc in QUAL:
        j = judge(sim(lambda c, pool_ok=pool_ok, qc=qc: base_ok(c) or (pool_ok(c) and (qc is None or qc(c)))))
        if j['added']: res.append((j['delta'], f"{rn} {qn}", j, rn, qn))
print("  each kind of move the rules refuse, all of them:")
for dl, name, j, rn, qn in sorted([r for r in res if r[4] == "(all of them)"], key=lambda x: -x[0]): show(rn, j, 'add')
print("  the best with one more condition (at least 4 added trades):")
for dl, name, j, rn, qn in sorted([r for r in res if r[4] != "(all of them)" and len(r[2]['added']) >= 4], key=lambda x: -x[0])[:22]: show(name, j, 'add')
kept_adds = [(n, j) for d, n, j, rn, qn in res if j['ok'] and len(j['added']) >= 4]
print(f"\n  tested: {len(out)} losing signals, {len(res)} winning signals. Passed every check (positive overall, no day worse, both halves better): {len(kept_drops)} losing, {len(kept_adds)} winning.")
print("  measured 21 Nov 2026: a RANDOM group of 4-5 refused moves passes those checks in 8-9% of draws, so about that share of the tests pass by luck: a pass is a candidate, not a proof.")
json.dump(dict(drops=[n for n, j in kept_drops], adds=[n for n, j in kept_adds]), open(os.path.expanduser('~/swifteye/research/signal_search.json'), 'w'))
