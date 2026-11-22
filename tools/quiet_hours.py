"""How often does the bot go N hours without a trade? On the trades that were really available (Polymarket by its own clock)."""
import sqlite3, datetime as dt, time, bisect
from collections import Counter
import replay_eval as R
C = R.load(); T = R.run(C, hours=None)
db = sqlite3.connect('file:/Users/danielvinichenko/swifteye/oddsfeed.db?mode=ro', uri=True)
U = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC)
clean = []
for t in T:
    c = t['c']; d = c['dir']
    q = sorted(db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms>=? AND ts_ms<=? AND bid IS NOT NULL AND ask IS NOT NULL", (c['match_id'], c['outcome'], c['t0'] - 60_000, c['tf'] + 120_000)).fetchall())
    time.sleep(0.01)
    at = lambda x: (lambda i: q[i] if i >= 0 else None)(bisect.bisect_right([r[0] for r in q], x) - 1)
    b, b2 = at(c['tf']), at(c['t0'] + 1500)
    if not (b and b2): continue
    buy = b[2] if d > 0 else 1 - b[1]
    if ((b2[1] + b2[2]) / 2 - c['pre_mid']) * d / c['bm'][1] <= 0.05 and buy - t['entry'] <= 0.02 + 1e-9: clean.append(c['tf'])
clean.sort()
start, end = min(c['tf'] for c in C), max(c['tf'] for c in C)
print(f"{len(clean)} really-available trades between {U(start):%d %b %H:%M} and {U(end):%d %b %H:%M} UTC ({(end-start)/3600e3:.0f} hours)")
by = Counter(U(t).hour for t in clean)
print("trades by hour of day (UTC): " + ' '.join(f"{h:02d}h:{by.get(h,0)}" for h in range(24)))
prime = sum(by.get(h, 0) for h in range(12, 21))
print(f"12-21 UTC (13-22 Norway): {prime} trades | the other 15 hours of the day: {len(clean) - prime}")
gaps = sorted(((b - a) / 3600e3, a, b) for a, b in zip([start] + clean, clean + [end]))
print("longest stretches with no trade: " + ', '.join(f"{g:.1f} h ({U(a):%d %b %H:%M} to {U(b):%d %b %H:%M})" for g, a, b in gaps[::-1][:5]))
for n in (4, 7, 10):
    step = 600_000; tot = empty = 0; t = start
    while t + n * 3600_000 <= end:
        i = bisect.bisect_left(clean, t); tot += 1; empty += not (i < len(clean) and clean[i] < t + n * 3600_000); t += step
    print(f"any {n}-hour stretch: no trade in {empty/tot*100:.0f}% of them")
# the same hours as tonight's run so far: 22:45 -> 05:25 UTC
for day in (17, 18):
    a = dt.datetime(2026, 11, day, 22, 45, tzinfo=dt.UTC).timestamp() * 1000; z = a + 6.67 * 3600_000
    print(f"night of {day} Nov, 22:45 to 05:25 UTC: {sum(a <= t < z for t in clean)} really-available trades")
