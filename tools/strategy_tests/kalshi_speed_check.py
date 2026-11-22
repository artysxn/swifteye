"""Is the old Kalshi price really still there? The recorded bid/ask come from Kalshi's ticker (late and sparse);
the recorded SIZE at that price comes from the real-time order book. Redo the test by OUR clock and ask for size."""
from sx import *
kfee = lambda a: 0.07 * a * (1 - a)
R = json.load(open('k1.json'))
def at(path, dt, clock):
    best = None
    for p in path:
        if p[clock] <= dt and p[2] is not None and p[3] is not None and (best is None or p[clock] >= best[clock]): best = p
    return best
E = []
for r in R:
    r['rp'] = sorted(r['path'], key=lambda p: p[1])
    k0 = at(r['rp'], -10_000, 1)
    if not k0 or k0[3] - k0[2] > 0.06: continue
    r['k0'] = (k0[2] + k0[3]) / 2; r['move'] = r['m1'] - r['m0']
    if abs(r['k0'] - r['m0']) <= 0.05: E.append(r)
days = (max(r['t'] for r in E) - min(r['t'] for r in E)) / 86400e3
d = sorted(p[1] - p[0] for r in E for p in r['path'])
print(f"Kalshi ticker delay inside these windows (our receive time minus Kalshi's stamp): median {d[len(d)//2]/1000:.1f} s, 90% under {d[len(d)*9//10]/1000:.1f} s, 99% under {d[len(d)*99//100]/1000:.1f} s")
g = sorted(b[1] - a[1] for r in E for a, b in zip(r['rp'], r['rp'][1:]) if (a[2], a[3]) != (b[2], b[3]))
print(f"time between two Kalshi price changes as we receive them: median {g[len(g)//2]/1000:.1f} s, 90% under {g[len(g)*9//10]/1000:.1f} s")
def sim(L, min_usd, thr=0.03):
    T = []; lock = {}
    for r in sorted(E, key=lambda r: r['t']):
        if r['t'] - lock.get(r['m'], 0) < 120_000: continue
        k = at(r['rp'], L, 1)
        if not k: continue
        bid, ask, size = k[2], k[3], (k[4] or 0)
        fol = ((bid + ask) / 2 - r['k0']) / r['move']; edge = r['m1'] - ask - kfee(ask)
        if fol > 0.2 or edge < thr or ask - bid > 0.04 or ask > 0.90 or size * ask < min_usd: continue
        lock[r['m']] = r['t']; ex = None
        for p in r['rp']:
            if p[1] >= L + 1500 and p[2] is not None and p[2] - r['k0'] >= r['move'] - 0.01: ex = p[2]; break
        if ex is None:
            q = at(r['rp'], L + 300_000, 1); ex = q[2] if q else bid
        cost = ask + kfee(ask); usd = min(size * ask, 100.0)
        T.append(dict(m=r['m'], sport=r['sport'], sell=(ex - kfee(ex) - cost) / cost, hold=(r['won'] - cost) / cost, usd=usd, cap=size * ask, edge=edge))
    return T
byg = lambda x: (lambda dd: ([dd[r['m']].append(r) for r in x], dd)[1])(defaultdict(list))
print("\nBUY THE OLD KALSHI PRICE, judged by our own clock, only when the real-time order book still shows size at that price")
for min_usd in (0, 20, 100):
    for L in (1000, 3000, 6000, 12000):
        T = sim(L, min_usd)
        if len(T) < 8: print(f"  ${min_usd}+ resting, {L/1000:.0f} s after: trades {len(T)}"); continue
        c1 = boot(byg(T), lambda z: st.mean(r['sell'] for r in z) if z else None)
        tot = sum(r['sell'] * r['usd'] for r in T)
        print(f"  ${min_usd:3d}+ resting, {L/1000:4.0f} s after the jump: trades {len(T):4d} ({len(T)/days:5.1f} a day) | {sum(r['sell']>0 for r in T)/len(T)*100:3.0f}% won | {st.mean(r['sell'] for r in T)*100:+6.1f}% a $ ({c1[0]*100:+4.0f}..{c1[1]*100:+4.0f}) | held to the result {st.mean(r['hold'] for r in T)*100:+6.1f}% | $ resting, median {st.median(r['cap'] for r in T):6,.0f} | profit at up to $100 a trade: ${tot/days:6.1f} a day")
T = sim(3000, 20)
print("\n  by sport ($20+ resting, 3 s):", {s: (len(x), f"{st.mean(r['sell'] for r in x)*100:+.0f}%", f"${sum(r['sell']*r['usd'] for r in x)/days:.0f}/day") for s, x in ((s, [r for r in T if r['sport'] == s]) for s in ('basketball', 'cs2', 'football', 'hockey', 'dota2')) if len(x) >= 3})
z = [r for r in sim(3000, 0) if r['cap'] < 1]
print(f"  trades at 3 s where the book shows under $1 at the old price (the price was already gone): {len(z)} of {len(sim(3000, 0))}")
