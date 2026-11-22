"""Were the backtest's buys real? Our Polymarket feed sometimes ran seconds late (18 Nov 2026: median 2.5 s in the
prime hours). The backtest sees Polymarket by OUR receive time. Here every backtest trade is looked at again by
Polymarket's own clock (server_ts_ms): had the price really not moved yet when the order would have landed?"""
import sqlite3, datetime as dt, statistics as st, sys, time
from collections import defaultdict
import replay_eval as R
C = R.load(); T = R.run(C, hours=None)
db = sqlite3.connect('file:/Users/danielvinichenko/swifteye/oddsfeed.db?mode=ro', uri=True)
D = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime('%d %b')
fee = lambda p: 0.05 * p * (1 - p)
out = []
for t in T:
    c = t['c']; m, o, d, t0, tf = c['match_id'], c['outcome'], c['dir'], c['t0'], c['tf']
    rows = db.execute("SELECT ts_ms, COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms>=? AND ts_ms<=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (m, o, t0 - 60_000, tf + 120_000)).fetchall()
    time.sleep(0.01)
    buy = lambda r: r[3] if d > 0 else 1 - r[2]
    mid = lambda r: ((r[2] + r[3]) / 2 - c['pre_mid']) * d
    rec = lambda x: max((r for r in rows if r[0] <= x), key=lambda r: r[0], default=None)
    tru = lambda x: max((r for r in rows if r[1] <= x), key=lambda r: r[1], default=None)
    a, b = rec(tf), tru(tf); a2, b2 = rec(t0 + 1500), tru(t0 + 1500)
    if not (a and b and a2 and b2): continue
    late = [r[0] - r[1] for r in rows if t0 - 5000 <= r[1] <= tf]         # how late quotes stamped around the move reached us
    out.append(dict(t=t, day=D(tf), rec_px=buy(a), true_px=buy(b), fol_rec=mid(a2) / c['bm'][1], fol_true=mid(b2) / c['bm'][1], late=max(late) if late else 0))
print(f"{len(out)} backtest trades checked against Polymarket's own clock\n")
def pnl(x, px):
    t = x['t']; n = t['spent'] / t['entry']; ex = t['exit']
    return n * (ex - px) - n * (fee(px) + (fee(ex) if 0 < ex < 1 else 0))
print(f"{'':8s} {'trades':>7s} {'as backtested':>14s} | {'feed was 2+ s late at the buy':>30s} | {'really still behind (5% or less)':>33s} | {'...and still buyable within 2c':>31s} {'their result':>13s}")
tot = [0, 0.0, 0, 0.0]
for day in ('17 Nov', '18 Nov', '19 Nov'):
    x = [r for r in out if r['day'] == day]
    if not x: continue
    late2 = [r for r in x if r['late'] >= 2000]
    behind = [r for r in x if r['fol_true'] <= 0.05]
    real = [r for r in behind if r['true_px'] - r['rec_px'] <= 0.02 + 1e-9]
    p0 = sum(r['t']['pnl'] for r in x); p1 = sum(pnl(r, max(r['true_px'], r['rec_px'])) for r in real)
    tot[0] += len(x); tot[1] += p0; tot[2] += len(real); tot[3] += p1
    print(f"{day:8s} {len(x):7d} {p0:+14.2f} | {len(late2):24d} ({len(late2)/len(x)*100:3.0f}%) | {len(behind):27d} ({len(behind)/len(x)*100:3.0f}%) | {len(real):31d} {p1:+13.2f}")
print(f"{'all':8s} {tot[0]:7d} {tot[1]:+14.2f} | {'':30s} | {'':33s} | {tot[2]:31d} {tot[3]:+13.2f}")
ok = [r for r in out if r['late'] < 2000]; bad = [r for r in out if r['late'] >= 2000]
for name, x in (("feed on time at the buy (under 2 s late)", ok), ("feed 2+ s late at the buy", bad)):
    if x: print(f"\n{name}: {len(x)} trades, backtested {sum(r['t']['pnl'] for r in x):+.2f} ({st.mean(r['t']['pnl']/r['t']['spent'] for r in x)*100:+.0f}% a trade, {sum(r['t']['pnl']>0 for r in x)/len(x)*100:.0f}% won); "
                f"by Polymarket's clock the price had already moved {st.median((r['true_px']-r['rec_px'])*100 for r in x):+.1f}c at the buy (median), and it had followed {st.median(r['fol_true'] for r in x)*100:.0f}% of the move")
d = sorted(r['late'] for r in out)
print(f"\nhow late Polymarket quotes reached us around each trade: median {d[len(d)//2]/1000:.1f} s, 75% under {d[len(d)*3//4]/1000:.1f} s, 90% under {d[len(d)*9//10]/1000:.1f} s")
