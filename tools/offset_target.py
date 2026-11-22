"""The catch-up target from the CALM gap between the bookmaker and Polymarket (owner, 22 Nov 2026: "the catch up price
is not considering the fact that there may be an INITIAL disagreement in price ... if faze vs spirit have a 60/40 on
polymarket but 70/30 on pinnacle, and something happens that moves pinnacle to 90/10, we can expect polymarket to move
more or less 20 points up, so from 60 to 80").

The live bot (snipe.rs) measures both prices 10 s before the trigger bookmaker's jump: target = Polymarket then +
the bookmaker's move since then. When Polymarket moved FIRST (it did in most give-ups), "Polymarket then" already holds
the move and the target counts it twice.
Here, for today's rules' trades on the clean replay (tools/sweep.py S.BASE):
  calm gap    the middle of (trigger bookmaker's chance - Polymarket's middle) from 5 min to 1 min before the jump
  new target  the bookmaker's chance at the decision - the calm gap        (old: Polymarket 10 s before + the move)
  the buy     still made only if the new target leaves the bot's minimum expected gain (4c after slippage and fees,
              computed as snipe.rs does); otherwise skipped
  the sale    when Polymarket's sell price reaches the target - half the spread (snipe::caught_up), else at the
              5-minute give-up at the sell price - 1c; both rules priced the SAME simple way from the recording, so
              they compare with each other (not exactly with the replay's own fill model)
usage: offset_target.py [today|nolag|loose]      (reads ~/swifteye/oddsfeed.db read-only, by match_id and time)
"""
import os, sys, sqlite3, bisect, statistics as st, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

DB = os.path.expanduser('~/swifteye/oddsfeed.db')
fee = lambda p: 0.05 * p * (1 - p) if 0 < p < 1 else 0.0
SLIP, MIN_EDGE, GIVE = 0.02, 0.04, 300_000
db = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
RULES = sys.argv[1] if len(sys.argv) > 1 else 'today'
P = dict(S.BASE) if RULES == 'today' else dict(S.BASE, lag=None) if RULES == 'nolag' else dict(S.BASE, lag=None, min_move=0.06, weak_cap=None)
T = S.sim(P)
VERBOSE = RULES == 'today'
rows = []
for t in T:
    c = t['c']; m, o, dir_, tb = c['match_id'], c['outcome'], c['dir'], c['trigger']
    t0, td = c['t0'], c['tf']
    q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND bid > 0 AND ask > 0 AND ts_ms BETWEEN ? AND ? ORDER BY id", (m, o, t0 - 420_000, td + GIVE + 60_000)).fetchall()
    q.sort(); qt = [x[0] for x in q]
    ch = db.execute("SELECT ts_ms, new_price FROM changes WHERE match_id=? AND book=? AND market='1x2' AND outcome=? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms", (m, tb, o, t0 - 3_600_000, td)).fetchall()
    bt = [x[0] for x in ch]; bv = [(1.0 / x[1]) if x[1] and x[1] > 1.0 else None for x in ch]
    def pm(ts):
        i = bisect.bisect_right(qt, ts) - 1
        return q[i] if i >= 0 and ts - q[i][0] < 120_000 else None
    def bk(ts):
        i = bisect.bisect_right(bt, ts) - 1
        return bv[i] if i >= 0 else None
    # calm gap, in the bought side's terms (dir > 0: the outcome; dir < 0: the other side)
    gaps = []
    for ts in range(t0 - 300_000, t0 - 60_000, 2000):
        p, b = pm(ts), bk(ts)
        if p and b is not None: gaps.append((b - (p[1] + p[2]) / 2) * dir_)
    if len(gaps) < 20: continue
    gap = st.median(gaps)
    p0, pd = pm(t0 - 10_000), pm(td); b_now = bk(td)
    if not p0 or not pd or b_now is None: continue
    side = lambda p: (p[1], p[2]) if dir_ > 0 else (1 - p[2], 1 - p[1])       # (sell, buy) of the bought side
    pre_mid = sum(side(p0)) / 2; mid_now = sum(side(pd)) / 2; sp = side(pd)[1] - side(pd)[0]
    b_side = b_now if dir_ > 0 else 1 - b_now
    old_target = pre_mid + c['bm'][1]
    new_target = b_side - gap
    entry = t['entry']
    def edge(target):
        ex = target - sp / 2 - SLIP; en = entry + SLIP
        return (ex - en) - fee(en) - fee(ex)
    def outcome(target):
        need = target - sp / 2
        k = bisect.bisect_left(qt, td + 2000)
        while k < len(q) and q[k][0] <= td + GIVE:
            if side(q[k])[0] >= need - 1e-9: x = side(q[k])[0]; return x, True, (q[k][0] - td) / 60000
            k += 1
        p = pm(td + GIVE); x = max(0.0, (side(p)[0] if p else side(pd)[0]) - 0.01); return x, False, 5.0
    n = 12.0 / entry
    res = {}
    for lab, tg in (('old', old_target), ('new', new_target)):
        x, caught, mins = outcome(tg)
        res[lab] = dict(target=tg, edge=edge(tg), pnl=n * (x - entry) - n * (fee(entry) + fee(x)), caught=caught, mins=mins, buy=edge(tg) >= MIN_EDGE - 1e-9)
    rows.append(dict(t=t, sport=c['sport'], when=time.strftime('%d %b %H:%M', time.localtime(td / 1000)), entry=entry, pre_mid=pre_mid, mid_now=mid_now, gap=gap, move=c['bm'][1], replay_caught=t['caught'], **res))

print('RULES %s, %d of %d replay trades with 5 calm minutes recorded before the jump\n' % (RULES, len(rows), len(T)))
print('  %-14s %-10s %5s | Polymarket 10 s before / at the buy | calm gap (bookmaker - Polymarket) | move | OLD target -> result | NEW target -> buy? result' % ('when', 'sport', 'paid'))
for r in (rows if VERBOSE else []):
    o_, n_ = r['old'], r['new']
    fmt = lambda d: ('caught %4.1f min %+5.2f $' % (d['mins'], d['pnl'])) if d['caught'] else ('gave up %+5.2f $' % d['pnl'])
    print('  %-14s %-10s %4.0fc | %4.0fc / %4.0fc | %+5.1f pt | %4.1f pt | %4.0fc -> %-22s | %4.0fc -> %s' % (r['when'], r['sport'], 100 * r['entry'], 100 * r['pre_mid'], 100 * r['mid_now'], 100 * r['gap'], 100 * r['move'],
          100 * o_['target'], fmt(o_), 100 * n_['target'], (fmt(n_) if n_['buy'] else 'NO BUY (expected gain %+.1fc)' % (100 * n_['edge']))))
def tot(lab, only_buy):
    X = [r for r in rows if (r[lab]['buy'] or not only_buy)]
    return len(X), sum(r[lab]['pnl'] for r in X), sum(1 for r in X if r[lab]['caught']), sum(1 for r in X if r[lab]['pnl'] > 0)
for lab, ob, name in (('old', False, 'OLD target (as the bot now)'), ('new', False, 'NEW target, same buys'), ('new', True, 'NEW target, and no buy when it leaves under 4c')):
    n, p, cg, w = tot(lab, ob)
    print('\n  %-48s %2d trades | caught up %2d | won %2d | %+7.2f $' % (name, n, cg, w, p), end='')
print('\n\n  The replay\'s own result for these trades: %+.2f $ (its fill model; the two lines above use one simple model for both)' % sum(r['t']['pnl'] for r in rows))
gu = [r for r in rows if not r['replay_caught']]
print('  The replay\'s give-ups among them: %d. Under the new rule: %d not bought, %d caught up, %d gave up' % (len(gu), sum(1 for r in gu if not r['new']['buy']), sum(1 for r in gu if r['new']['buy'] and r['new']['caught']), sum(1 for r in gu if r['new']['buy'] and not r['new']['caught'])))
