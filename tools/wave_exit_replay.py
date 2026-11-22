"""Sale rules built on Polymarket's own wave, tested on the replay of 17-20 Nov (same buys as sweep.py takes; only
the sale differs). The price path after each buy is read second by second by Polymarket's own clock; a sale is
filled 2 s after the rule fires, 1c under the bid. usage: wave_exit_replay.py"""
import sys, os, time, statistics as st, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.argv = ['sweep.py', 'none']
import sweep as S, waves as W
db = W.conn(); fee = W.fee; DELAY = 2
LOOSE = dict(S.BASE, lag=None, weak_cap=None, min_move=0.07, max_followed=0.10, min_edge=0.03, dota_spread=None, bb='all', pause=False)
_cache = {}
def ctx(t):
    c = t['c']; k = (c['match_id'], c['outcome'], c['t0'])
    if k not in _cache:
        dr = float(c['dir']); t0 = c['t0'] - 150000; t1 = c['tf'] + 345000
        g = W.grid(db, c['match_id'], c['outcome'], dr, t0, t1, c['trigger'], clock='server')
        im = int((c['t0'] - t0) // 1000); ib = int((c['tf'] - t0) // 1000)
        L = W.legs(g['trig'], W.TH_BK); cand = [l for l in L if l['size'] > 0 and l['i0'] - 6 <= im <= l['i1'] + 6]
        _cache[k] = dict(g=g, im=im, ib=ib, B=max(cand, key=lambda l: l['size']) if cand else None, pre=(c['pre_mid'] if dr > 0 else 1 - c['pre_mid']), mv=c['bm'][1], sp=max(c['sp']))
    return _cache[k]
def gave(x, k):
    B, s = x['B'], x['g']['trig']
    if not B or s[k] is None: return 0.0
    top = max(v for v in s[B['i0']:k + 1] if v is not None)
    return (top - s[k]) / B['size']
def run(t, rule, give=300):
    x = ctx(t); bid = x['g']['bid']; ib = x['ib']; e = t['entry']; n = t['spent'] / e
    peak = None; last = ib; end = min(ib + give, x['g']['n'] - 1 - DELAY)
    out = None
    for k in range(ib + 2, end + 1):
        b = bid[k]
        if b is None: continue
        if peak is None or b > peak: peak, last = b, k
        if rule(x, t, k, b, peak, k - last):
            out = (k, 'rule'); break
    if out is None: out = (end, 'give-up')
    kk = min(out[0] + DELAY, x['g']['n'] - 1); px = bid[kk] if bid[kk] is not None else (bid[out[0]] if bid[out[0]] is not None else None)
    if px is None: return None
    px = max(px - 0.01, 0.0)
    return n * (px - e) - n * (fee(e) + fee(px)), out[1], out[0] - ib
target = lambda k_: (lambda x, t, k, b, pk, idle: b >= x['pre'] + k_ * x['mv'] - x['sp'] / 2)
def either(*rs): return lambda *a: any(r(*a) for r in rs)
BOT = target(1.0)
RULES = collections.OrderedDict()
RULES['the bot: target = the whole move, else give up at 5 min'] = BOT
for k_ in (0.3, 0.5, 0.75): RULES['target at %d%% of the move' % (k_ * 100)] = target(k_)
for th in (0.02, 0.03, 0.05):
    RULES['whole-move target, or bid %dc off its high' % (th * 100)] = either(BOT, (lambda th: lambda x, t, k, b, pk, idle: pk - b >= th - 1e-9)(th))
    RULES['... %dc off, only once in profit 3c+' % (th * 100)] = either(BOT, (lambda th: lambda x, t, k, b, pk, idle: pk - b >= th - 1e-9 and pk >= t['entry'] + 0.03)(th))
    RULES['... %dc off, only once the bookmaker gave back a quarter' % (th * 100)] = either(BOT, (lambda th: lambda x, t, k, b, pk, idle: pk - b >= th - 1e-9 and gave(x, k) >= 0.25)(th))
for n_ in (10, 20, 30, 45, 60, 90):
    RULES['whole-move target, or no new high for %d s (bid above the buy)' % n_] = either(BOT, (lambda n_: lambda x, t, k, b, pk, idle: idle >= n_ and pk > t['entry'] + 0.01)(n_))
for n_ in (20, 45):
    RULES['... %d s, only once the bookmaker gave back a quarter' % n_] = either(BOT, (lambda n_: lambda x, t, k, b, pk, idle: idle >= n_ and pk > t['entry'] + 0.01 and gave(x, k) >= 0.25)(n_))
RULES['whole-move target, or the bookmaker gave back half'] = either(BOT, lambda x, t, k, b, pk, idle: gave(x, k) >= 0.5)
RULES['target at 50%, or no new high for 20 s'] = either(target(0.5), lambda x, t, k, b, pk, idle: idle >= 20 and pk > t['entry'] + 0.01)
day = lambda t: time.strftime('%d', time.localtime(t['c']['t0'] / 1000))
for title, P in (("TODAY'S RULES", dict(S.BASE)), ("TODAY'S RULES WITHOUT 'lagged 50%'", dict(S.BASE, lag=None, weak_cap=None)), ('LOOSER SET', LOOSE)):
    T = S.sim(P)
    base = {}
    for t in T:
        r = run(t, BOT)
        if r is not None: base[id(t)] = r
    T = [t for t in T if id(t) in base]
    b0 = sum(base[id(t)][0] for t in T)
    # hindsight: the highest bid in the 5 minutes
    hs = 0.0
    for t in T:
        x = ctx(t); v = [b for b in x['g']['bid'][x['ib'] + 2:x['ib'] + 300] if b is not None]; e = t['entry']; n = t['spent'] / e
        px = max(max(v) - 0.01, 0) if v else e; hs += n * (px - e) - n * (fee(e) + fee(px))
    print('\n== %s: %d trades | replay engine $%+.2f | this path engine, bot rule $%+.2f (won %d) | sold at the 5-min top (hindsight) $%+.2f' % (title, len(T), sum(t['pnl'] for t in T), b0, sum(1 for t in T if base[id(t)][0] > 0), hs))
    print('   %-66s %9s %8s %4s %6s %5s  %s' % ('rule', 'total', 'change', 'won', 'better', 'worse', 'change by day'))
    for name, rule in list(RULES.items())[1:]:
        res = {id(t): run(t, rule) for t in T}
        tot = sum(res[id(t)][0] for t in T)
        bd = collections.defaultdict(float)
        for t in T: bd[day(t)] += res[id(t)][0] - base[id(t)][0]
        print('   %-66s %+9.2f %+8.2f %4d %6d %5d  %s' % (name, tot, tot - b0, sum(1 for t in T if res[id(t)][0] > 0), sum(1 for t in T if res[id(t)][0] > base[id(t)][0] + 1e-9), sum(1 for t in T if res[id(t)][0] < base[id(t)][0] - 1e-9), ' '.join('%s:%+.1f' % (d, bd[d]) for d in sorted(bd))))
