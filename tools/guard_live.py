"""What would the late-feed guard have done to REAL money? Every real buy order since 20 Nov (filled or not), with the
bot's feed delay at that moment (logged late_ms where present, and measured from the game's own quotes: receive time
minus Polymarket's clock, median from 20 s before to 5 s after). Plus the buys the guard blocked while it was on.
usage: guard_live.py"""
import sqlite3, json, os, statistics as st, datetime as dt, bisect
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
SINCE = 1_795_129_200_000
no = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone(dt.timedelta(hours=1))).strftime('%d %H:%M:%S')


def lag(m, t):
    q = db.execute("SELECT ts_ms - server_ts_ms FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=? AND server_ts_ms IS NOT NULL", (m, t - 20_000, t + 5_000)).fetchall()
    return st.median(r[0] for r in q) if len(q) >= 3 else None


trades = [(i, ts, json.loads(d)) for i, ts, d in db.execute("SELECT id, ts_ms, data FROM bot_trades WHERE real=1 AND ts_ms>=? ORDER BY ts_ms", (SINCE,))]
orders = [(i, ts, ok, json.loads(d)) for i, ts, ok, d in db.execute("SELECT id, ts_ms, ok, data FROM bot_orders WHERE ts_ms>=? AND bot='snipe' ORDER BY ts_ms", (SINCE,))]
intents = [(ts, d) for i, ts, ok, d in orders if d.get('intent')]
buys = [(i, ts, ok, d) for i, ts, ok, d in orders if d.get('side') == 'buy' and not d.get('intent')]
print(f"real buy orders since 20 Nov: {len(buys)} ({sum(1 for b in buys if b[2])} filled)")
rows = []
for i, ts, ok, d in buys:
    it = [x for x in intents if x[1].get('token') == d.get('token') and 0 <= ts - x[0] < 20_000]
    m = it[-1][1].get('match_id') if it else None
    tr = [t for t in trades if t[2].get('token') == d.get('token') and abs(t[1] - ts) < 5000]
    if m is None and tr: m = tr[0][2]['match_id']
    l = lag(m, ts) if m else None
    pnl = tr[0][2].get('pnl') if tr else None
    name = (tr[0][2]['home'] + ' v ' + tr[0][2]['away'])[:30] if tr else (it[-1][1].get('home', '') + ' v ' + it[-1][1].get('away', ''))[:30] if it else '?'
    rows.append(dict(ts=ts, ok=bool(ok), logged=d.get('late_ms'), lag=l, pnl=pnl, name=name, err=d.get('error')))
    print(f"  {no(ts)} {name:30s} {'FILLED' if ok else 'no fill':7s} logged delay {str(d.get('late_ms')):>6s} ms | measured {('%.0f' % l) if l is not None else '-':>6s} ms | pnl {('%+.2f' % pnl) if pnl is not None else '   - '}")
late = lambda r: (r['logged'] if r['logged'] is not None else (r['lag'] or 0)) > 1000
for nm, g in (('feed over 1 s late (the guard would block)', [r for r in rows if late(r)]), ('feed on time', [r for r in rows if not late(r)])):
    f = [r for r in g if r['ok'] and r['pnl'] is not None]
    print(f"  {nm}: {len(g)} buy orders, {sum(r['ok'] for r in g)} filled; filled trades: {sum(r['pnl'] > 0 for r in f)} won of {len(f)}, ${sum(r['pnl'] for r in f):+.2f}")
print("\nbuys the guard BLOCKED while it was on (20 Nov 12:55-20:27 Norway): what Polymarket's own clock shows afterwards")
for ts, data in db.execute("SELECT ts_ms, data FROM bot_signals WHERE ts_ms>=? AND result='feed_late' ORDER BY ts_ms", (SINCE,)):
    d = json.loads(data); m, o, dr = d['match_id'], d['outcome'], d.get('dir', 1.0)
    q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask, ts_ms FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=? AND outcome=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY 1", (m, ts - 60_000, ts + 400_000, o)).fetchall()
    qt = [r[0] for r in q]; i = bisect.bisect_right(qt, ts)
    seen = d['dec'][1] if isinstance(d.get('dec'), list) else None
    def our(r): return (r[1], r[2]) if dr > 0 else (1 - r[2], 1 - r[1])
    true_ask = our(q[i - 1])[1] if i else None
    quiet = (qt[i] - qt[i - 1]) / 1000 if 0 < i < len(qt) else None
    hi = max((our(r)[0] for r in q[i:] if r[0] <= ts + 300_000), default=None)
    last = our(q[bisect.bisect_right(qt, ts + 300_000) - 1])[0] if q else None
    print(f"  {no(ts)} {d.get('sport', ''):8s} match {m}: price the bot saw {seen}, Polymarket's real ask then {true_ask}, limit {None if seen is None else round(seen + 0.02, 3)} | feed delay {d.get('late_ms')} ms | "
          f"best bid in the next 5 min {hi}, bid at 5 min {last} | this market silent around the buy for {quiet} s")
