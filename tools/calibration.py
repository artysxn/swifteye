"""Which market is more correct: Polymarket or each bookmaker? (owner's question, 21 Nov 2026)
From ~/swifteye/research/ev_snaps.json (tools/ev_snapshots.py): for every settled game, 7 checks from 6 h before the
start to 100 min after it, Polymarket's bid/ask, each bookmaker family's odds, and who won.

A bookmaker's price = its odds with the margin removed (power method), so it is a probability like Polymarket's mid.
  A. per source and price level: what it said, what Polymarket said on the same rows, what really happened
  B. where a bookmaker and Polymarket DISAGREE by 3+ points: who was nearer the truth
  C. accuracy head to head, per sport (squared error on the same rows; games resampled whole for the range)
  D. Polymarket's own calibration per sport
  E. as a bet: buy on Polymarket when ONE bookmaker's fair price is 2+ points above the ask after the fee, hold to the result
usage: calibration.py [snapshots.json]
"""
import json, math, random, statistics as st, sys, os
from collections import defaultdict, Counter
R = json.load(open(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser('~/swifteye/research/ev_snaps.json')))
fee = lambda a: 0.05 * a * (1 - a)


def power(odds):
    pi = [1 / o for o in odds]
    if sum(pi) <= 1.0: return [p / sum(pi) for p in pi]
    lo, hi = 1.0, 6.0
    for _ in range(40):
        k = (lo + hi) / 2
        if sum(p ** k for p in pi) > 1: lo = k
        else: hi = k
    return [p ** k for p in pi]


grp = defaultdict(list)
for r in R: grp[(r['m'], r['t'])].append(r)
for rows in grp.values():
    full = len(rows) == rows[0]['k']
    fams = set.intersection(*[set(r['raw']) for r in rows]) if full else set()
    pw = {}
    for f in fams:
        od = [r['raw'][f] for r in rows]
        if 1.0 <= sum(1 / x for x in od) <= 1.25: pw[f] = dict(zip([r['o'] for r in rows], power(od)))
    for r in rows:
        r['p'] = {f: v[r['o']] for f, v in pw.items()}
        if len(pw) >= 3: r['p']['MIDDLE of all'] = st.median(v[r['o']] for v in pw.values())
        r['mid'] = (r['bid'] + r['ask']) / 2; r['sp'] = r['ask'] - r['bid']; r['pre'] = r['rel'] < 0
U = [r for r in R if r['sp'] <= 0.06 and 0.03 <= r['mid'] <= 0.97 and r['age'] <= (1800 if r['pre'] else 120) and r['p']]
SRC = [s for s, n in Counter(f for r in U for f in r['p']).most_common() if len({r['m'] for r in U if s in r['p']}) >= 120]
print(f"{len(U)} usable rows from {len({r['m'] for r in U})} settled games, 17-21 Nov; sports: {Counter(r['sport'] for r in {r['m']: r for r in U}.values()).most_common(8)}")
print('sources with 120+ games: ' + ', '.join(f"{s} ({len({r['m'] for r in U if s in r['p']})})" for s in SRC))
random.seed(4)


def boot(rows, fn, n=400):
    by = defaultdict(list)
    for r in rows: by[r['m']].append(r)
    ks = list(by); v = []
    for _ in range(n):
        x = fn([y for k in (random.choice(ks) for _ in ks) for y in by[k]])
        if x is not None: v.append(x)
    v.sort()
    return (v[int(len(v) * .05)], v[int(len(v) * .95)]) if len(v) > 20 else (float('nan'), float('nan'))


BK = ((0.03, 0.15), (0.15, 0.25), (0.25, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 0.75), (0.75, 0.85), (0.85, 0.97))
print("\n== A. BY PRICE LEVEL: what the source said, what Polymarket said on the same rows, what happened (pre-match and in play together)")
for s in ['polymarket'] + SRC:
    rows = U if s == 'polymarket' else [r for r in U if s in r['p']]
    val = (lambda r: r['mid']) if s == 'polymarket' else (lambda r, s=s: r['p'][s])
    print(f"  {s} ({len({r['m'] for r in rows})} games)")
    for lo, hi in BK:
        x = [r for r in rows if lo <= val(r) < hi]
        g = len({r['m'] for r in x})
        if g < 40: continue
        won = st.mean(r['won'] for r in x); said = st.mean(val(r) for r in x); se = (won * (1 - won) / g) ** 0.5
        print(f"     says {lo * 100:2.0f}-{hi * 100:2.0f}%: {len(x):5d} rows, {g:4d} games | said {said * 100:5.1f}% | Polymarket said {st.mean(r['mid'] for r in x) * 100:5.1f}% | really won {won * 100:5.1f}% (+/-{se * 100:.1f}) | source off by {(won - said) * 100:+5.1f}, Polymarket off by {(won - st.mean(r['mid'] for r in x)) * 100:+5.1f}")

print("\n== B. WHERE THEY DISAGREE by 3+ points: the side the bookmaker rates HIGHER than Polymarket does")
print(f"  {'source':16s} {'rows':>5s} {'games':>5s} | bookmaker said | Polymarket said | really won | nearer the truth")
for s in SRC:
    for ph, nm in ((None, 'all'), (True, 'pre-match'), (False, 'in play')):
        x = [r for r in U if s in r['p'] and r['p'][s] - r['mid'] >= 0.03 and r['p'][s] - r['mid'] <= 0.15 and (ph is None or r['pre'] == ph)]
        g = len({r['m'] for r in x})
        if g < 40: continue
        b, p, w = st.mean(r['p'][s] for r in x), st.mean(r['mid'] for r in x), st.mean(r['won'] for r in x)
        lo, hi = boot(x, lambda z: st.mean(r['won'] for r in z) if z else None)
        print(f"  {s:16s} {len(x):5d} {g:5d} | {b * 100:13.1f}% | {p * 100:14.1f}% | {w * 100:5.1f}% ({lo * 100:.0f}-{hi * 100:.0f}) | {'bookmaker' if abs(w - b) < abs(w - p) else 'Polymarket'}  [{nm}]")

print("\n== C. ACCURACY HEAD TO HEAD on the same rows (squared error x 1000, lower = better; + = the bookmaker is MORE accurate than Polymarket)")
sports = [s for s, n in Counter(r['sport'] for r in {r['m']: r for r in U}.values()).most_common() if n >= 60]
br = lambda x, f: st.mean((f(r) - r['won']) ** 2 for r in x) * 1000
print(f"  {'source':16s} " + ' '.join(f"{sp + ' (games, diff, range)':>34s}" for sp in ['ALL'] + sports))
for s in SRC:
    out = f"  {s:16s} "
    for sp in [None] + sports:
        x = [r for r in U if s in r['p'] and (sp is None or r['sport'] == sp)]
        g = len({r['m'] for r in x})
        if g < 60: out += f"{'-':>34s} "; continue
        d = br(x, lambda r: r['mid']) - br(x, lambda r: r['p'][s])
        lo, hi = boot(x, lambda z: (br(z, lambda r: r['mid']) - br(z, lambda r: r['p'][s])) if z else None, 300)
        out += f"{f'{g:4d}  {d:+5.1f}  ({lo:+5.1f} to {hi:+5.1f})':>34s} "
    print(out)
print("  pre-match rows only:")
for s in SRC:
    x = [r for r in U if s in r['p'] and r['pre']]
    if len({r['m'] for r in x}) < 60: continue
    d = br(x, lambda r: r['mid']) - br(x, lambda r: r['p'][s]); lo, hi = boot(x, lambda z: (br(z, lambda r: r['mid']) - br(z, lambda r: r['p'][s])) if z else None, 300)
    print(f"  {s:16s} {len({r['m'] for r in x}):4d} games  Polymarket {br(x, lambda r: r['mid']):6.1f}  bookmaker {br(x, lambda r: r['p'][s]):6.1f}  diff {d:+5.1f} ({lo:+5.1f} to {hi:+5.1f})")

print("\n== D. POLYMARKET'S OWN CALIBRATION PER SPORT (its mid price against what happened)")
for sp in sports:
    rows = [r for r in U if r['sport'] == sp]
    print(f"  {sp} ({len({r['m'] for r in rows})} games)")
    for lo, hi in ((0.03, 0.2), (0.2, 0.35), (0.35, 0.5), (0.5, 0.65), (0.65, 0.8), (0.8, 0.97)):
        x = [r for r in rows if lo <= r['mid'] < hi]; g = len({r['m'] for r in x})
        if g < 30: continue
        won = st.mean(r['won'] for r in x); said = st.mean(r['mid'] for r in x)
        print(f"     price {lo * 100:2.0f}-{hi * 100:2.0f}c: {g:4d} games | said {said * 100:5.1f}% | won {won * 100:5.1f}% (+/-{(won * (1 - won) / g) ** 0.5 * 100:.1f}) | off by {(won - said) * 100:+5.1f}")

print("\n== E. AS A BET: one bet a game, at the first check where the source's fair price is 2+ points above Polymarket's ask after the fee (up to 15); held to the result")
roi = lambda x: st.mean((r['won'] - r['ask30'] - fee(r['ask30'])) / (r['ask30'] + fee(r['ask30'])) for r in x)
def bets(rows, s, thr=0.02):
    by = defaultdict(list)
    for r in rows:
        if s in r['p'] and r.get('ask30') is not None and 0.03 <= r['ask30'] <= 0.97: by[(r['t'], r['m'])].append(r)
    got = {}
    for (t, m) in sorted(by):
        if m in got: continue
        c = [(r['p'][s] - r['ask30'] - fee(r['ask30']), r) for r in by[(t, m)]]
        c = [x for x in c if thr <= x[0] <= 0.15]
        if c: got[m] = max(c, key=lambda x: x[0])[1]
    return list(got.values())
print(f"  {'source':16s} {'sport':11s} {'bets':>5s} {'paid':>6s} {'won':>6s} {'needed':>7s} {'return per $':>13s}  range")
for s in SRC:
    for sp in [None] + sports:
        b = bets([r for r in U if sp is None or r['sport'] == sp], s)
        if len(b) < 30: continue
        lo, hi = boot(b, lambda z: roi(z) if z else None)
        print(f"  {s:16s} {sp or 'ALL':11s} {len(b):5d} {st.mean(r['ask30'] for r in b) * 100:5.1f}c {st.mean(r['won'] for r in b) * 100:5.1f}% {st.mean(r['ask30'] + fee(r['ask30']) for r in b) * 100:6.1f}% {roi(b) * 100:+12.1f}%  ({lo * 100:+.0f} to {hi * 100:+.0f})")
b = []
for m in {r['m'] for r in U}:
    x = [r for r in U if r['m'] == m and r.get('ask30') is not None and 0.03 <= r['ask30'] <= 0.97]
    if x: b.append(min(x, key=lambda r: (r['t'], r['o'])))
print(f"  comparison: the first row of every game, no signal: {len(b)} bets, return {roi(b) * 100:+.1f}% a $")
