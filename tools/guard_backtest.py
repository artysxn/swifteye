"""Does the late-feed guard give or take? Every backtest trade of 17-19 Nov (tools/sweep.py, the bot as it really
saw the feed: legacy=True), with the guard's own measure at the moment of the decision: the smallest delay among the
last 8 Polymarket messages received before it. Light on the database: primary-key probes only.
"""
import sys, os, time, json, sqlite3, datetime as dt
T0 = time.time()
sys.path.insert(0, os.path.expanduser('~/swifteye-src/tools')); sys.argv = sys.argv[:1]
import sweep as S

db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
LO, HI = db.execute("SELECT MIN(rowid), MAX(rowid) FROM pm_quotes").fetchone()
ts_of = lambda r: db.execute("SELECT ts_ms FROM pm_quotes WHERE rowid = ?", (r,)).fetchone()


def rowid_at(t):
    """Largest rowid whose receive time is <= t (rows are appended in receive order)."""
    lo, hi = LO, HI
    while lo < hi:
        mid = (lo + hi + 1) // 2; x = ts_of(mid)
        if x is None: mid2 = db.execute("SELECT MIN(rowid) FROM pm_quotes WHERE rowid >= ?", (mid,)).fetchone()[0]; x = ts_of(mid2)
        if x[0] <= t: lo = mid
        else: hi = mid - 1
    return lo


def guard_lag(t):
    r = rowid_at(t)
    rows = db.execute("SELECT ts_ms, server_ts_ms FROM pm_quotes WHERE rowid BETWEEN ? AND ? AND server_ts_ms IS NOT NULL", (r - 400, r)).fetchall()
    msg = {}
    for ts, sv in rows:
        if ts <= t: msg[ts] = max(msg.get(ts, 0), sv)
    last = sorted(msg.items())[-8:]
    if len(last) < 8 or t - last[-1][0] > 30_000: return None
    return min(ts - sv for ts, sv in last)


SETS = {
    'the bot\'s rules (as it saw the feed)': dict(S.BASE, legacy=True),
    'looser rules, for a bigger sample': dict(S.BASE, legacy=True, min_move=0.07, cons=0.0, fams=1, lag=None, gap=None, max_followed=0.15, min_edge=0.02, max_spread=0.06, cap=0.95, one_per_game=False),
}
cache = {}
out = {}
for name, p in SETS.items():
    T = S.sim(p)
    if len(T) > 700: T = T[::(len(T) // 700 + 1)]
    for i, t in enumerate(T):
        tf = t['c']['tf']
        if tf not in cache: cache[tf] = guard_lag(tf)
        t['lag'] = cache[tf]
        if i % 60 == 59: time.sleep(0.15)
        if time.time() - T0 > 95: print('time budget reached, stopping early at', i, 'of', len(T)); T = T[:i]; break
    K = [t for t in T if t['lag'] is not None]
    print(f"\n== {name}: {len(T)} trades, {sum(t['pnl'] > 0 for t in T) * 100 // max(len(T), 1)}% won, {sum(t['pnl'] for t in T):+.2f}; feed delay known for {len(K)}")
    print(f"   {'guard threshold':18s} {'blocked':>7s} {'won':>5s} {'profit of blocked':>18s} {'per trade':>10s} | {'kept':>5s} {'won':>5s} {'profit kept':>12s} {'per trade':>10s}")
    for th in (500, 1000, 2000, 5000):
        B = [t for t in K if t['lag'] > th]; R = [t for t in K if t['lag'] <= th]
        f = lambda X: (len(X), (f"{sum(t['pnl'] > 0 for t in X) * 100 // len(X)}%" if X else '-'), sum(t['pnl'] for t in X), (sum(t['pnl'] for t in X) / len(X) if X else 0.0))
        b, r = f(B), f(R)
        print(f"   over {th / 1000:.1f} s late    {b[0]:7d} {b[1]:>5s} {b[2]:+18.2f} {b[3]:+10.2f} | {r[0]:5d} {r[1]:>5s} {r[2]:+12.2f} {r[3]:+10.2f}")
    if name.startswith('the bot'):
        print('   trades with the feed over 1 s late:')
        for t in K:
            if t['lag'] > 1000:
                c = t['c']; print(f"     {dt.datetime.fromtimestamp(c['tf'] / 1000, dt.UTC).strftime('%d %b %H:%M')} {c['sport']:10s} {c['home'][:16]} v {c['away'][:16]} | {t['lag'] / 1000:.1f} s late | {'caught up' if t['caught'] else 'gave up'} | {t['pnl']:+.2f}")
        lags = sorted(t['lag'] for t in K)
        if lags: print(f"   feed delay at the decision: median {lags[len(lags) // 2]} ms, 90th percentile {lags[int(len(lags) * .9)]} ms, worst {lags[-1]} ms")
    out[name] = [(t['c']['tf'], t['c']['sport'], t['lag'], round(t['pnl'], 3), bool(t['caught'])) for t in T]
json.dump(out, open(os.path.expanduser('~/swifteye/research/guard_backtest.json'), 'w'))
print(f"\n{time.time() - T0:.0f} s, {len(cache)} decision times looked up")
