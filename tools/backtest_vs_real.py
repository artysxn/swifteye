"""How accurate is the backtest? Its trades against the REAL trades in the hours both cover.
For every real trade since 20 Nov inside the backtest's span: is the same buy in the backtest (same game, side, within
15 s), at what price, sold where, with what result. And the other way round: backtest trades with no real trade.
usage: backtest_vs_real.py"""
import sys, os, json, sqlite3, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.argv = sys.argv[:1]
import sweep as S
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
no = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone(dt.timedelta(hours=1))).strftime('%d %H:%M:%S')
lo, hi = S.ROWS[0]['tf'], S.ROWS[-1]['tf']
print(f"backtest set: {len(S.ROWS)} moves, {no(lo)} - {no(hi)} Norway time")
real = []
for i, data in db.execute("SELECT id, data FROM bot_trades WHERE real = 1 AND ts_ms >= 1795129200000 AND closed_ms IS NOT NULL ORDER BY ts_ms"):
    v = json.loads(data); v['id'] = i; real.append(v)
ci = S.CATCH.index(S.BASE['catch'])
for title, p in (("as the bot saw the feed (legacy)", dict(S.BASE, legacy=True)), ("feed on time", dict(S.BASE))):
    T = [t for t in S.sim(p) if t['c']['tf'] >= 1795129200000]
    print(f"\n== BACKTEST, the bot's rules, {title}: {len(T)} trades since 20 Nov 00:00, {sum(t['pnl'] for t in T):+.2f}")
    used = set(); n_match = 0; d_pnl = []
    print(f"  {'real trade':44s} {'real: in -> out, result':>26s} | backtest: in -> out, result, how it ended")
    for v in real:
        if not lo <= v['ts'] <= hi: continue
        m = [t for t in T if t['c']['match_id'] == v['match_id'] and t['c']['outcome'] == v['outcome'] and t['c']['dir'] == v['dir'] and abs(t['c']['tf'] - v['ts']) <= 15000]
        name = f"#{v['id']} {no(v['ts'])[3:]} {v['home'][:14]} v {v['away'][:10]}"
        rs = f"{v['entry'] * 100:5.1f} -> {v['exit'] * 100:5.1f} {v['pnl']:+6.2f} {v['reason'][:9]}"
        if m:
            t = m[0]; used.add(id(t)); n_match += 1; d_pnl.append(t['pnl'] - v['pnl'])
            print(f"  {name:44s} {rs:>26s} | {t['entry'] * 100:5.1f} -> {t['exit'] * 100:5.1f} {t['pnl']:+6.2f} {'caught up' if t['caught'] else 'gave up'}   (difference {t['pnl'] - v['pnl']:+.2f})")
        else:
            # why not: was the move in the data at all, and which rule stopped it?
            c = [c for c in S.ROWS if c['match_id'] == v['match_id'] and c['outcome'] == v['outcome'] and c['dir'] == v['dir'] and abs(c['tf'] - v['ts']) <= 15000]
            why = 'the move is not in the replay data' if not c else ('passes the rules but blocked by a lock / one-a-game' if S.gate(c[0], p, ci) and not c[0]['nofill'] else 'replay says no fill' if c[0]['nofill'] else 'a rule rejects it in the replay')
            print(f"  {name:44s} {rs:>26s} | NOT in the backtest: {why}")
    extra = [t for t in T if id(t) not in used and lo <= t['c']['tf'] <= hi]
    print(f"  matched {n_match} of {sum(1 for v in real if lo <= v['ts'] <= hi)} real trades; result differs by {sum(d_pnl):+.2f} in all ({', '.join(format(x, '+.2f') for x in d_pnl)})")
    print(f"  backtest trades with NO real trade: {len(extra)}")
    for t in extra:
        c = t['c']; print(f"     {no(c['tf'])[3:]} {c['sport']:8s} {c['home'][:14]} v {c['away'][:12]:12s} {c['trigger'][:9]:9s} in {t['entry'] * 100:5.1f} out {t['exit'] * 100:5.1f} {t['pnl']:+6.2f} {'caught up' if t['caught'] else 'gave up'}  feed late {c['late_ms'] / 1000:.1f} s")
T = S.sim(dict(S.BASE))
by = {}
for t in T: by[t['c']['_day']] = by.get(t['c']['_day'], [0, 0, 0.0]); by[t['c']['_day']][0] += 1; by[t['c']['_day']][1] += t['pnl'] > 0; by[t['c']['_day']][2] += t['pnl']
print("\n== THE WHOLE BACKTEST NOW (feed on time): " + f"{len(T)} trades, {sum(t['pnl'] > 0 for t in T) * 100 // len(T)}% won, {sum(t['pnl'] for t in T):+.2f}; by day: " + ', '.join(f"{d} Nov {n} trades {w} won {p:+.2f}" for d, (n, w, p) in sorted(by.items())))
