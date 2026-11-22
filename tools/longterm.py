"""Four questions on the 17-20 Nov backtest (owner, 21 Nov 2026):
 1. is holding the give-ups just gambling?   2. minimum move 8 v 10 points
 3. the long-term effect of rules that cost profit but drop trades   4. what 'wait 5 s and confirm' keeps and drops
Uses the cached signals of systems_backtest.py; $12 a trade."""
import sys, os, math, random, statistics as st
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
src = open(os.path.join(HERE, 'systems_backtest.py')).read().split("HDR = f\"")[0]
exec(compile(src, 'systems_backtest_head', 'exec'))
DAYS = (S.ROWS[-1]['tf'] - S.ROWS[0]['tf']) / 86_400_000
k = lambda t: (t['c']['match_id'], t['c']['outcome'], t['c']['t0'])
conf = lambda f: f['held5'] is True and f['oth5'] is not None and f['oth5'] >= 0.25
B0 = run(BASE)
print(f"backtest span {DAYS:.2f} days; the bot as it is: {len(B0)} trades, {sum(t['pnl'] for t in B0):+.2f}")

print("\n== 1. THE GIVE-UPS: sold after 5 min, or held to the result")
for title, p in (("the bot's rules", BASE), ("looser rules", LOOSE)):
    T = run(p); G = [t for t in T if not t['caught'] and hold_pnl(t) is not None]
    won = [hold_pnl(t) > 0 for t in G]
    sale = st.mean(t['exit'] for t in G); wr = sum(won) / len(G)
    sold = sum(t['pnl'] for t in G); held = sum(hold_pnl(t) for t in G)
    cost = st.mean((t['spent'] / t['entry']) * (S.fee(t['exit']) + 0.01) for t in G)      # the sale's fee plus about 1c of spread
    se = (wr * (1 - wr) / len({t['c']['match_id'] for t in G})) ** 0.5
    print(f"  {title:16s} {len(G):4d} give-ups in {len({t['c']['match_id'] for t in G})} games | sold at {sale * 100:4.1f}c on average | the side then WON {wr * 100:4.1f}% (+/-{se * 100:.0f}) | sold {sold:+8.2f}, held {held:+8.2f}: {(held - sold) / len(G):+5.2f} a give-up | selling costs about {cost:.2f} a give-up")
    for lo, hi in ((0, 0.35), (0.35, 0.65), (0.65, 1.01)):
        X = [t for t in G if lo <= t['exit'] < hi]
        if len(X) >= 8: print(f"       sold at {lo * 100:.0f}-{min(hi, 1) * 100:.0f}c: {len(X):4d} give-ups, sale price {st.mean(t['exit'] for t in X) * 100:4.1f}c, won {sum(hold_pnl(t) > 0 for t in X) * 100 / len(X):4.1f}%, held minus sold {sum(hold_pnl(t) - t['pnl'] for t in X):+8.2f}")
    by = defaultdict(lambda: [0, 0.0])
    for t in G: by[t['c']['_day']][0] += 1; by[t['c']['_day']][1] += hold_pnl(t) - t['pnl']
    print("       held minus sold by day: " + ', '.join(f"{d} Nov {n} give-ups {x:+.2f}" for d, (n, x) in sorted(by.items())))

print("\n== 2. MINIMUM MOVE 8 v 10 POINTS")
M0 = run(dict(BASE, min_move=0.08)); kb = {k(t) for t in B0}; km = {k(t) for t in M0}
add = [t for t in M0 if k(t) not in kb]; lost = [t for t in B0 if k(t) not in km]
f = lambda X: f"{len(X)} trades, {sum(t['pnl'] > 0 for t in X)} won, {sum(t['pnl'] for t in X):+.2f}" + (f" ({st.mean(t['pnl'] for t in X):+.2f} each, worst {min(t['pnl'] for t in X):+.2f})" if X else '')
print(f"  10 points (now): {f(B0)}\n   8 points:       {f(M0)}")
print(f"  trades ADDED by going to 8: {f(add)}; trades it displaces (locks): {f(lost)}")
by = defaultdict(list)
for t in add: by[t['c']['_day']].append(t['pnl'])
print("  added trades by day: " + ', '.join(f"{d} Nov {len(v)} trades {sum(v):+.2f}" for d, v in sorted(by.items())) + " | by sport: " + ', '.join(f"{sp} {sum(1 for t in add if t['c']['sport'] == sp)} / {sum(t['pnl'] for t in add if t['c']['sport'] == sp):+.2f}" for sp in sorted({t['c']['sport'] for t in add})))
for mm in (0.07, 0.08, 0.09, 0.10, 0.12):
    X = run(dict(BASE, min_move=mm)); print(f"     minimum move {mm * 100:.0f} points: {f(X)}")

print("\n== 4. WAIT 5 s AND CONFIRM: it does not add trades, it SPLITS the bot's own trades")
C = run(dict(BASE, delay=5000), conf); kc = {(t['c']['match_id'], t['c']['outcome'], t['c']['t0']) for t in C}
keep = [t for t in B0 if k(t) in kc]; drop = [t for t in B0 if k(t) not in kc]
print(f"  the bot's {len(B0)} trades: {f(B0)}\n  confirmed (kept):   {f(keep)}\n  not confirmed:      {f(drop)}")
print(f"  (as a rule it trades {len(C)} times for {sum(t['pnl'] for t in C):+.2f}: the buy is 1 s later and a freed game can take another trade)")

print("\n== 3. LONG-TERM EFFECT, per system (trades treated as independent draws at the backtest's rate; 20,000 simulated 30-day months at $12 a trade)")
random.seed(21)
def month(T):
    rate = len(T) / DAYS; p = [t['pnl'] for t in T]; out = []; dd_all = []; ruin = 0
    for _ in range(20000):
        n = sum(1 for _ in range(int(rate * 30 * 2)) if random.random() < 0.5)       # about rate x 30 trades, with spread
        cum = peak = dd = 0.0; hit = False
        for _ in range(n):
            cum += random.choice(p); peak = max(peak, cum); dd = max(dd, peak - cum)
            if cum <= -40: hit = True
        out.append(cum); dd_all.append(dd); ruin += hit
    out.sort(); dd_all.sort()
    return st.mean(out), out[1000], sum(x < 0 for x in out) / len(out), dd_all[10000], dd_all[19000], ruin / 20000
print(f"  {'system':40s} {'trades/day':>10s} {'each':>6s} {'sd':>5s} {'losers':>6s} {'lost $':>7s} {'avg loss':>8s} | 30 days: {'profit':>7s} {'bad month':>9s} {'P(loss)':>7s} {'usual drop':>10s} {'bad drop':>8s} {'-$40 hit':>8s} | growth a day at 30% stakes, best stake")
for name, T in (('the bot as it is', B0), ('+ not AHEAD', run(BASE, notahead)), ('+ follows (follow - lead 0.2+)', run(BASE, follows)), ('+ trigger moved first', run(BASE, first)),
                ('+ first and follows', run(BASE, AND(first, follows))), ('move 8+ points', M0), ('move 8+, follows', run(dict(BASE, min_move=0.08), follows)),
                ('wait 5 s and confirm', C), ('give-ups held', run(BASE, None, True)), ('+ follows, give-ups held', run(BASE, follows, True))):
    p = [t['pnl'] for t in T]; r = [t['pnl'] / t['spent'] for t in T]; L = [x for x in p if x < 0]
    mean, p5, ploss, dd50, dd95, ruin = month(T)
    g = lambda fr: (len(T) / DAYS) * st.mean(math.log(max(1e-9, 1 + fr * x)) for x in r)
    best = max((g(fr / 100), fr) for fr in range(1, 100))
    print(f"  {name:40s} {len(T) / DAYS:10.1f} {st.mean(p):+6.2f} {st.pstdev(p):5.2f} {len(L):6d} {sum(L):+7.2f} {st.mean(L) if L else 0:+8.2f} | {mean:+16.0f} {p5:+9.0f} {ploss * 100:6.1f}% {dd50:10.0f} {dd95:8.0f} {ruin * 100:7.1f}% | {(math.exp(g(0.30)) - 1) * 100:+5.1f}% a day; best at {best[1]}% of the bankroll ({(math.exp(best[0]) - 1) * 100:+.1f}% a day)")
