"""How real is the replay's number (400 -> 1,108 in 3.2 days)? (owner, 21 Nov 2026: "Analyze the math behind it. Assume
slightly different rules (as if the rules were calibrated on different days), and figure out the significance and
reality of that number going forward.")

  1. THE MATH      what the number is made of: trades, share won, return per dollar staked, stake share, compounding;
                   how much of it comes from a few trades and from one day
  2. NEIGHBOURS    3,000 rule sets that differ a little from today's (each setting drawn from values around today's):
                   what they make, and where today's rules sit among them
  3. OTHER DAYS    the rules re-chosen on three of the four days (the best of the 3,000 there) and run on the day
                   they never saw; the same with halves of the games. This is the number an honest calibration gives.
  4. SIGNIFICANCE  can the result be told from zero: by trade, by day, and after allowing for the choosing
  5. REAL MONEY    the real trades today's rules would have taken
  6. FORWARD       a 400 wallet with the honest return in place of the replay's
usage: reality_check.py
"""
import sys, os, json, random, math, sqlite3, hashlib, statistics as st
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.argv = ['x', 'lib']
import sweep as S
S.MIN_FILL = 5.0
random.seed(2026)
ci, gi = 2, 2
NOW = dict(S.BASE, legacy=False)
DAYS = ('17', '18', '19', '20')
half = lambda m: int(hashlib.md5(str(m).encode()).hexdigest()[:6], 16) % 2
fl = lambda c: (c['cx']['follow'] - c['cx']['lead']) if c['cx']['jumps'] else None
weak = lambda c: not (fl(c) is not None and fl(c) >= 0.2)

# every setting's neighbourhood (today's value marked by its position in NOW)
GRID = dict(min_move=(0.07, 0.08, 0.09, 0.10), lag=(0.3, 0.4, 0.5, 0.6, 0.7), max_followed=(0.0, 0.03, 0.05, 0.08, 0.10), min_edge=(0.03, 0.04, 0.05), max_spread=(0.03, 0.04, 0.05),
            cons=(0.02, 0.03, 0.04), fams=(2, 3), cap=(0.85, 0.90, 0.95), gap=(0.07, 0.10, None), weak_cap=(None, 0.55, 0.60, 0.70), bb=('none', 'nonus'),
            dota=('out', 0.02, 0.03, 'all'))


def make(v):
    p = dict(NOW, **{k: x for k, x in v.items() if k != 'dota'})
    if v['dota'] == 'out': p['skip'] = ('dota2',); p['dota_spread'] = None
    elif v['dota'] == 'all': p['skip'] = (); p['dota_spread'] = None
    else: p['skip'] = (); p['dota_spread'] = v['dota']
    return p


LOOSEST = make(dict(min_move=0.07, lag=0.3, max_followed=0.10, min_edge=0.03, max_spread=0.05, cons=0.02, fams=2, cap=0.95, gap=None, weak_cap=None, bb='nonus', dota='all'))
ROWS = [c for c in S.ROWS if S.gate(c, LOOSEST, ci)]
print(f"clean moves any of the neighbouring rule sets could take: {len(ROWS)} (of {len(S.ROWS)} recorded)")


def run(p):
    return [(t['c']['_day'], t['pnl'], t['spent'], t['c']['match_id'], t) for t in S.sim(p, rows=ROWS)]


def tot(tr, days=DAYS): return sum(x[1] for x in tr if x[0] in days)


base = run(NOW)
assert abs(tot(base) - sum(t['pnl'] for t in S.sim(NOW))) < 1e-6
B = tot(base); n = len(base); spent = sum(x[2] for x in base)
rets = [x[1] / x[2] for x in base]
print(f"\n1. THE MATH (today's rules, clean replay, $12 a trade): {n} trades in 3.2 days = {n / 3.2:.1f} a day; {sum(r > 0 for r in rets)} won ({100 * sum(r > 0 for r in rets) / n:.0f}%)")
wins = [r for r in rets if r > 0]; loss = [r for r in rets if r <= 0]
print(f"   a winning trade makes {100 * st.mean(wins):+.1f}% of its stake on average, a losing one {100 * st.mean(loss):+.1f}%; all together {100 * B / spent:+.1f}% of the money staked (${B:+.2f} on ${spent:.0f})")
print(f"   needed to break even at those sizes: {100 * -st.mean(loss) / (st.mean(wins) - st.mean(loss)):.0f}% won")
srt = sorted(base, key=lambda x: -x[1])
print(f"   the 5 best trades make ${sum(x[1] for x in srt[:5]):+.2f} of the ${B:+.2f} ({100 * sum(x[1] for x in srt[:5]) / B:.0f}%); without them the other {n - 5} make ${B - sum(x[1] for x in srt[:5]):+.2f} ({100 * (B - sum(x[1] for x in srt[:5])) / sum(x[2] for x in srt[5:]):+.1f}% a $)")
print("   by day: " + ' | '.join(f"{d} Nov {sum(1 for x in base if x[0] == d)} trades ${tot(base, (d,)):+.2f}" for d in DAYS) + f" | 18 Nov is {100 * tot(base, ('18',)) / B:.0f}% of it")
# compounding
w = 400.0; g = []
for d, pnl, sp, m, t in sorted(base, key=lambda x: x[4]['c']['tf']):
    f = 0.25 * (0.5 if weak(t['c']) else 1.0); g.append(1 + f * pnl / sp); w *= g[-1]
print(f"   400 wallet, 25% a trade (half in weak games), if every order filled in full: x{w / 400:.2f} = {w:.0f}. Each trade moves the wallet by {100 * (st.geometric_mean(g) - 1):+.2f}% on average; {n} of those compound to x{w / 400:.2f}")
print(f"   so the figure is: (trades a day) x (share of the wallet staked) x (return per $ staked), compounded. Halve the return per $ and x{w / 400:.2f} becomes x{math.exp(sum(math.log(1 + (x - 1) / 2) for x in g)):.2f}; a quarter of it gives x{math.exp(sum(math.log(1 + (x - 1) / 4) for x in g)):.2f}")

# ---------------------------------------------------------------- 2. neighbours
N = 3000
sets = []
seen = set()
while len(sets) < N:
    v = {k: random.choice(vals) for k, vals in GRID.items()}
    key = tuple(sorted((k, str(x)) for k, x in v.items()))
    if key in seen: continue
    seen.add(key); sets.append(v)
R = []
for v in sets:
    tr = run(make(v))
    R.append(dict(v=v, n=len(tr), tot=tot(tr), spent=sum(x[2] for x in tr), day={d: tot(tr, (d,)) for d in DAYS}, nday={d: sum(1 for x in tr if x[0] == d) for d in DAYS},
                  half={h: sum(x[1] for x in tr if half(x[3]) == h) for h in (0, 1)}, won=sum(x[1] > 0 for x in tr)))
ok = [r for r in R if r['n'] >= 10]
tots = sorted(r['tot'] for r in ok); per = sorted(r['tot'] / r['spent'] for r in ok)
q = lambda v, x: v[min(len(v) - 1, int(len(v) * x))]
print(f"\n2. NEIGHBOURS: {len(ok)} rule sets that differ a little from today's (10+ trades each)")
print(f"   profit at $12 a trade: 1 in 10 under ${q(tots, .1):+.0f}, a quarter under ${q(tots, .25):+.0f}, middle ${q(tots, .5):+.0f}, a quarter over ${q(tots, .75):+.0f}, 1 in 10 over ${q(tots, .9):+.0f}; lost money: {100 * sum(t < 0 for t in tots) / len(tots):.0f}%")
print(f"   return per $ staked: 1 in 10 under {100 * q(per, .1):+.1f}%, middle {100 * q(per, .5):+.1f}%, 1 in 10 over {100 * q(per, .9):+.1f}%")
print(f"   TODAY'S RULES: ${B:+.2f}, {100 * B / spent:+.1f}% a $: better than {100 * sum(t < B for t in tots) / len(tots):.0f}% of their neighbours (by profit), {100 * sum(x < B / spent for x in per) / len(per):.0f}% (by return per $)")
print(f"   trades taken: middle {q(sorted(r['n'] for r in ok), .5)}, share won middle {100 * q(sorted(r['won'] / r['n'] for r in ok), .5):.0f}%; positive on all four days: {100 * sum(all(r['day'][d] > 0 for d in DAYS) for r in ok) / len(ok):.0f}% of the neighbours")
print("   which single setting matters (middle profit of the neighbours with each value):")
for k, vals in GRID.items():
    print(f"      {k:13s} " + '  '.join(f"{str(x):>5s}: {st.median(r['tot'] for r in ok if r['v'][k] == x):+5.0f}" for x in vals))

# ---------------------------------------------------------------- 3. calibrated on other days
print("\n3. RULES CHOSEN ON OTHER DAYS: the best of the 3,000 on three days (or on one half of the games), run on what it never saw")
oos = 0.0; oos_top = 0.0; today_sum = 0.0
for d in DAYS:
    tr_days = [x for x in DAYS if x != d]
    cand = [r for r in R if sum(r['nday'][x] for x in tr_days) >= 8]
    cand.sort(key=lambda r: -sum(r['day'][x] for x in tr_days))
    best = cand[0]; top = cand[:30]
    oos += best['day'][d]; oos_top += st.mean(r['day'][d] for r in top); today_sum += tot(base, (d,))
    print(f"   chosen without {d} Nov: made ${sum(best['day'][x] for x in tr_days):+6.2f} on the other three, then ${best['day'][d]:+6.2f} on {d} Nov ({best['nday'][d]} trades) | the 30 best choices average ${st.mean(r['day'][d] for r in top):+6.2f} | today's rules that day ${tot(base, (d,)):+6.2f} | the middle neighbour ${st.median(r['day'][d] for r in ok):+6.2f}")
inbest = max(r['tot'] for r in ok)
print(f"   ALL FOUR UNSEEN DAYS TOGETHER: ${oos:+.2f} (the 30 best choices: ${oos_top:+.2f}) against ${B:+.2f} for today's rules judged on the days they were chosen on, and ${inbest:+.2f} for the best neighbour with hindsight")
ho = 0.0; ho_top = 0.0
for h in (0, 1):
    cand = sorted([r for r in R if r['n'] >= 10], key=lambda r: -r['half'][1 - h]); best = cand[0]
    ho += best['half'][h]; ho_top += st.mean(r['half'][h] for r in cand[:30])
    print(f"   chosen on half {2 - h} of the games: ${best['half'][1 - h]:+6.2f} there, then ${best['half'][h]:+6.2f} on the half it never saw (30 best: ${st.mean(r['half'][h] for r in cand[:30]):+6.2f}; today's rules ${sum(x[1] for x in base if half(x[3]) == h):+6.2f})")
print(f"   BOTH UNSEEN HALVES TOGETHER: ${ho:+.2f} (30 best: ${ho_top:+.2f})")
shr_day = oos_top / B; shr_half = ho_top / B
print(f"   so an honest calibration keeps about {100 * shr_day:.0f}% (by days) to {100 * shr_half:.0f}% (by games) of the figure reported for today's rules")

# ---------------------------------------------------------------- 4. significance
print("\n4. SIGNIFICANCE")
pn = [x[1] for x in base]; m = st.mean(pn); sd = st.stdev(pn)
print(f"   by trade: ${m:+.2f} a trade, spread ${sd:.2f}, {m / (sd / math.sqrt(n)):.1f} standard errors above zero. But the rules were picked to look good on these very trades, so this overstates it.")
dd = [tot(base, (d,)) for d in DAYS]
print(f"   by day (the days are the real unit: a good or bad day moves many trades together): {', '.join(f'{x:+.1f}' for x in dd)}; mean ${st.mean(dd):+.1f} a day, {st.mean(dd) / (st.stdev(dd) / 2):.1f} standard errors above zero with only 4 days; without 18 Nov ${st.mean([x for x in dd if x != max(dd)]):+.1f} a day")
by = defaultdict(float)
for x in base: by[x[3]] += x[1]
ks = list(by); bs = sorted(sum(by[random.choice(ks)] for _ in ks) for _ in range(4000))
print(f"   resampling whole games: ${bs[200]:+.0f} to ${bs[3800]:+.0f} (90%), below zero in {100 * sum(b < 0 for b in bs) / 4000:.1f}% of resamples, again before allowing for the choosing")
json.dump(dict(B=B, spent=spent, n=n, oos=oos, oos_top=oos_top, ho=ho, ho_top=ho_top, median_neighbour=q(tots, .5), median_per=q(per, .5), shr_day=shr_day, shr_half=shr_half), open(os.path.expanduser('~/swifteye/research/reality_check.json'), 'w'))
