"""What kind of move did each replay trade buy? (owner, 22 Nov 2026: "test the winning trades too")

For today's rules on the clean replay (tools/sweep.py S.BASE), every trade - winners and give-ups - is sorted by what
happened BEFORE the buy, from the recording (Polymarket by its own clock, the trigger bookmaker by the time its price
reached us; chances in the bought side's terms):
  calm          the middle of each line from 5 min to 1 min before the trigger's jump
  BOUNCE        the trigger bookmaker was 4 pt+ below its own calm level 10 s before the jump, and its move left it
                less than half the jump above the calm level: most of the "move" was a recovery from a dip
  PM FIRST      Polymarket had already made half or more of the bookmaker's net move (calm -> now) by 10 s before
                the jump: the bookmaker was confirming a move Polymarket had made
  LAG           neither: the bookmaker moved and Polymarket had not (the case the bot is built for)
and what came AFTER: how far Polymarket went beyond the target within 5 minutes.
usage: trade_kinds.py      (reads ~/swifteye/oddsfeed.db read-only, by match_id and time) -> ~/swifteye/research/trade_kinds.txt
"""
import os, sys, sqlite3, bisect, statistics as st, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

DB = os.path.expanduser('~/swifteye/oddsfeed.db')
db = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
T = S.sim(S.BASE)
rows = []
for t in T:
    c = t['c']; m, o, d, tb, t0, td = c['match_id'], c['outcome'], c['dir'], c['trigger'], c['t0'], c['tf']
    q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND bid > 0 AND ask > 0 AND ts_ms BETWEEN ? AND ? ORDER BY id", (m, o, t0 - 420_000, td + 360_000)).fetchall()
    q.sort(); qt = [x[0] for x in q]
    ch = db.execute("SELECT ts_ms, new_price FROM changes WHERE match_id=? AND book=? AND market='1x2' AND outcome=? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms", (m, tb, o, t0 - 3_600_000, td)).fetchall()
    bt = [x[0] for x in ch]; bv = [(1.0 / x[1]) if x[1] and x[1] > 1.0 else None for x in ch]
    sidep = lambda v: v if d > 0 else 1 - v
    def pm(ts):
        i = bisect.bisect_right(qt, ts) - 1
        return sidep((q[i][1] + q[i][2]) / 2) if i >= 0 and ts - q[i][0] < 120_000 else None
    def bk(ts):
        i = bisect.bisect_right(bt, ts) - 1
        return sidep(bv[i]) if i >= 0 and bv[i] is not None else None
    pc = [pm(u) for u in range(t0 - 300_000, t0 - 60_000, 2000)]; bc = [bk(u) for u in range(t0 - 300_000, t0 - 60_000, 2000)]
    pc = [x for x in pc if x is not None]; bc = [x for x in bc if x is not None]
    if len(pc) < 20 or len(bc) < 20: continue
    pm_calm, b_calm = st.median(pc), st.median(bc)
    pm_pre, b_pre, b_now = pm(t0 - 10_000), bk(t0 - 10_000), bk(td)
    if pm_pre is None or b_pre is None or b_now is None: continue
    jump = b_now - b_pre; net = b_now - b_calm
    if b_pre - b_calm <= -0.04 and net < 0.5 * jump: kind = 'BOUNCE'
    elif net > 0 and (pm_pre - pm_calm) >= 0.5 * net: kind = 'PM FIRST'
    else: kind = 'LAG'
    # after: Polymarket's best middle within 5 min v the target (pre + jump)
    after = [pm(u) for u in range(td, td + 300_000, 2000)]; after = [x for x in after if x is not None]
    best = max(after) if after else None
    rows.append(dict(kind=kind, won=t['caught'], pnl=t['pnl'], sport=c['sport'], when=time.strftime('%d %b %H:%M', time.localtime(td / 1000)), entry=t['entry'],
                     pm_calm=pm_calm, pm_pre=pm_pre, b_calm=b_calm, b_pre=b_pre, b_now=b_now, jump=jump, net=net, beyond=(best - (pm_pre + jump)) if best is not None else None))
out = []
out.append('TODAY\'S RULES, %d of %d replay trades sorted by what they bought (17-20 Nov; chances in the bought side\'s terms)\n' % (len(rows), len(T)))
out.append('  %-12s %-10s %-8s %-9s | Polymarket calm -> 10 s before | bookmaker calm -> 10 s before -> now | Polymarket in 5 min v the target | result' % ('when', 'sport', 'kind', 'outcome'))
for r in sorted(rows, key=lambda r: (r['kind'], not r['won'])):
    out.append('  %-12s %-10s %-8s %-9s | %4.0fc -> %4.0fc | %4.0f -> %4.0f -> %4.0f (jump %+4.0f, net %+4.0f) | %+5.1fc | %+6.2f $' % (
        r['when'], r['sport'], r['kind'], 'caught' if r['won'] else 'gave up', 100 * r['pm_calm'], 100 * r['pm_pre'], 100 * r['b_calm'], 100 * r['b_pre'], 100 * r['b_now'], 100 * r['jump'], 100 * r['net'],
        100 * r['beyond'] if r['beyond'] is not None else float('nan'), r['pnl']))
out.append('')
for k in ('LAG', 'PM FIRST', 'BOUNCE'):
    X = [r for r in rows if r['kind'] == k]
    if not X: out.append('  %-9s 0 trades' % k); continue
    out.append('  %-9s %2d trades | caught up %2d, gave up %2d | %+7.2f $ (%+.2f a trade)' % (k, len(X), sum(1 for r in X if r['won']), sum(1 for r in X if not r['won']), sum(r['pnl'] for r in X), sum(r['pnl'] for r in X) / len(X)))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/trade_kinds.txt'), 'w').write(txt + '\n')
