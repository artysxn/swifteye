"""Two questions on the trades that were REALLY available (Polymarket read by its own clock):
A. how much money can the stale prices take?   B. how fast does the opportunity disappear after a bookmaker move?"""
import sqlite3, datetime as dt, statistics as st, time, bisect
from collections import defaultdict
import replay_eval as R
C = R.load(); P = dict(R.BASE, hours=None); ci, gi = R.CATCH.index(1.0), R.GIVE.index(300)
db = sqlite3.connect('file:/Users/danielvinichenko/swifteye/oddsfeed.db?mode=ro', uri=True)
D = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime('%d %b')
def quotes(m, o, a, z):
    r = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), ts_ms, bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms>=? AND ts_ms<=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (m, o, a, z)).fetchall()
    time.sleep(0.008 + len(r) / 80000)
    return sorted(r)                      # by Polymarket's own clock
def at(q, t):
    i = bisect.bisect_right([x[0] for x in q], t) - 1
    return q[i] if i >= 0 else None

# ---------- A. scale
T = R.run(C, hours=None); clean = []
for t in T:
    c = t['c']; d = c['dir']; q = quotes(c['match_id'], c['outcome'], c['t0'] - 60_000, c['tf'] + 120_000)
    b, b2 = at(q, c['tf']), at(q, c['t0'] + 1500)
    if not (b and b2): continue
    buy = lambda r: r[3] if d > 0 else 1 - r[2]
    fol = ((b2[2] + b2[3]) / 2 - c['pre_mid']) * d / c['bm'][1]
    if fol <= 0.05 and buy(b) - t['entry'] <= 0.02 + 1e-9: clean.append((t, max(buy(b) - t['entry'], 0.0)))
days = 2.7
print(f"A. SCALE: the {len(clean)} trades that were really available, {days} days, all hours")
nd = sum(1 for t, _ in clean if not t['c']['buy'])
best = sorted(t['c']['buy'][0] * t['entry'] for t, _ in clean if t['c']['buy']); near = sorted(t['c']['buy'][1] * t['entry'] for t, _ in clean if t['c']['buy'])
print(f"   dollars on offer at the stale price: median ${best[len(best)//2]:,.0f} at the best price, ${near[len(near)//2]:,.0f} within 2c; a quarter of the trades have under ${near[len(near)//4]:,.0f} within 2c ({nd} trades have no recorded size)")
print(f"   {'stake asked':>12s} {'really bought':>14s} {'profit':>9s} {'per $':>7s} {'a day':>8s} {'orders cut short':>17s}")
sized = [(t, w) for t, w in clean if t['c']['buy'] and t['x'].get('sell')]      # only trades whose order-book size was recorded on both sides
k = len(clean) / len(sized)                                                    # scale the day figures back up to all clean trades
print(f"   (table on the {len(sized)} trades with recorded size on both the buy and the sale; day figures scaled to all {len(clean)})")
for stake in (12, 25, 50, 100, 250, 500, 1000, 2500, 10000):
    spent = pnl = 0.0; cut = 0; worst = 0.0; per = []
    for t, worse in sized:
        c = t['c']; f = R.fill(c, t['x'], float(stake), P['walk'] and c['edge'][1][ci] - R.CAP >= P['min_edge'] - 1e-9)
        if not f: continue
        n = f['spent'] / f['entry']; q = f['pnl'] - n * worse; spent += f['spent']; pnl += q; cut += f['spent'] < stake * 0.95; worst = min(worst, q); per.append(q)
    print(f"   ${stake:>11,d} ${spent/len(sized):>10,.0f} avg ${pnl*k:>8,.0f} {pnl/spent*100:>6.1f}% ${pnl*k/days:>7,.0f} {cut:>10d} of {len(sized)}   worst single trade ${worst:,.0f}")

# ---------- B. speed
steps = [-3000, -2000, -1000, 0, 500, 1000, 1750, 2750, 3750, 5000, 7000, 10000]
seen = {}; M = []
for c in C:
    if not (c['bm'][0] >= 0.10 and c['cx']['fam_moved'] >= 2 and c['cx']['cons_move'] >= 0.03 and c['sport'] != 'basketball' and not c['pause']
            and c['cx']['jumps'] > 0 and c['cx']['follow'] >= 0.3 and max(c['sp']) <= 0.04 + 1e-9): continue
    k = (c['match_id'], c['outcome'], c['dir'])
    if k in seen and c['t0'] - seen[k] < 90_000: seen[k] = c['t0']; continue
    seen[k] = c['t0']; M.append(c)
rows = []
for c in M:
    q = quotes(c['match_id'], c['outcome'], c['t0'] - 40_000, c['t0'] + 20_000); d = c['dir']
    pre = at(q, c['t0'] - 10_000)
    if not pre: continue
    pm = (pre[2] + pre[3]) / 2
    if abs(pm - c['pre_mid']) > 0.03: continue                  # our view of "before" was already stale: leave it out
    f = []
    for s in steps:
        x = at(q, c['t0'] + s); f.append((((x[2] + x[3]) / 2 - pm) * d / c['bm'][0]) if x else None)
    rows.append((c, f))
print(f"\nB. SPEED: {len(rows)} separate bookmaker moves of 10+ points (2+ groups, the other rules passed), Polymarket read by its own clock")
print("   time 0 = the moment the first bookmaker's new price reaches us. Our order lands at about +3.75 s today.")
print(f"   {'seconds':>8s} {'Polymarket has made (median)':>29s} {'still untouched (5% or less)':>30s} {'moves a day still open':>23s}")
for i, s in enumerate(steps):
    v = [f[i] for _, f in rows if f[i] is not None]
    op = sum(x <= 0.05 for x in v)
    print(f"   {s/1000:>+8.2f} {st.median(v)*100:>27.0f}% {op/len(v)*100:>28.0f}% {op/days:>22.0f}")
i0, i1 = steps.index(-2000), steps.index(3750)
for day in ('17 Nov', '18 Nov', '19 Nov'):
    x = [f for c, f in rows if D(c['t0']) == day]
    g = lambda i: sum(1 for f in x if f[i] is not None and f[i] <= 0.05) / max(sum(1 for f in x if f[i] is not None), 1) * 100
    print(f"   {day}: {len(x):3d} moves | still untouched at 0 s {g(steps.index(0)):3.0f}%, at +1.75 s {g(steps.index(1750)):3.0f}%, at +3.75 s {g(steps.index(3750)):3.0f}%, at +10 s {g(steps.index(10000)):3.0f}%")
for sp in ('tennis', 'cs2', 'dota2'):
    x = [f for c, f in rows if c['sport'] == sp]
    g = lambda i: sum(1 for f in x if f[i] is not None and f[i] <= 0.05) / max(sum(1 for f in x if f[i] is not None), 1) * 100
    print(f"   {sp:7s}: {len(x):3d} moves | still untouched at 0 s {g(steps.index(0)):3.0f}%, at +3.75 s {g(steps.index(3750)):3.0f}%, at +10 s {g(steps.index(10000)):3.0f}%")
print(f"   Polymarket moved BEFORE the bookmaker's price reached us (already 50%+ at time 0) on {sum(1 for _, f in rows if f[3] is not None and f[3] >= 0.5)/len(rows)*100:.0f}% of the moves")
