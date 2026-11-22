"""The moves that pass EVERY rule but were not traded because the order would not fill (tools/rule_blocks.py funnel:
127 'no fill' + 23 'offer gone' of 262). Why, and would a FASTER order have made money?

For each such move (clean replay, today's rules, 17-20 Nov), Polymarket's recorded book (its own clock, the bought
side; sizes where recorded) around the trigger's jump t0 and the decision tf:
  reason      RAN = the buy price at the order's landing was 2c+ above the decision price; THIN = under $10 on offer
              within 2c; GAP = no quote within 5 s of the landing (a recording gap); else OTHER
  caught      did Polymarket's sell price reach the bot's target (price 10 s before + the move - half the spread)
              within 5 minutes?
  if faster   bought at the ask at t0 + 1 s, t0 + 2 s, or at the decision tf, sold at the target or at 5 min (sell
              price - 1c), both fees, $12 - the same simple pricing as tools/quick_exit.py
The bot's own 36 trades are priced the same way for comparison.
usage: nofill_check.py      (reads ~/swifteye/oddsfeed.db read-only, by match_id and time) -> ~/swifteye/research/nofill_check.txt
"""
import os, sys, sqlite3, bisect, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

DB = os.path.expanduser('~/swifteye/oddsfeed.db')
db = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
fee = lambda p: 0.05 * p * (1 - p) if 0 < p < 1 else 0.0
B = S.BASE; CI = S.CATCH.index(1.0); GI = S.GIVE.index(B['give'])
FILL_MS = 2000          # the order lands about 2 s after the decision (send + match)


def book(c):
    q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask, bid_size, ask_size, bid_depth, ask_depth FROM pm_quotes WHERE match_id=? AND outcome=? AND bid > 0 AND ask > 0 AND ts_ms BETWEEN ? AND ? ORDER BY id",
                   (c['match_id'], c['outcome'], c['t0'] - 60_000, c['tf'] + 330_000)).fetchall()
    q.sort(); d = c['dir']
    # bought side: (t, sell, buy, size at buy, depth at buy)
    return [(t, b if d > 0 else 1 - a, a if d > 0 else 1 - b, (as_ if d > 0 else bs), (ad if d > 0 else bd)) for t, b, a, bs, as_, bd, ad in q]


def at(Q, ts, tol=5000):
    i = bisect.bisect_right([x[0] for x in Q], ts) - 1
    return Q[i] if i >= 0 and ts - Q[i][0] <= tol else None


def sell(Q, c, t_buy, entry):
    pre = c['pre_mid'] if c['dir'] > 0 else 1 - c['pre_mid']
    need = pre + c['bm'][1] - c['_sp'] / 2
    ts = [x[0] for x in Q]; k = bisect.bisect_left(ts, t_buy + 2000)
    while k < len(Q) and Q[k][0] <= t_buy + 300_000:
        if Q[k][1] >= need: x = Q[k][1] - 0.01; break
        k += 1
    else:
        e = at(Q, t_buy + 300_000, 120_000); x = (e[1] - 0.01) if e else entry
        n = 12.0 / entry; return n * (x - entry) - n * (fee(entry) + fee(max(x, 0.001))), False
    n = 12.0 / entry; return n * (x - entry) - n * (fee(entry) + fee(max(x, 0.001))), True


pas = [c for c in S.ROWS if S.gate(c, B, CI)]
T = S.sim(B); traded = {(t['c']['match_id'], t['c']['t0']) for t in T}
tr_by_game = collections.defaultdict(list)
for t in T: tr_by_game[t['c']['match_id']].append((t['c']['tf'], t['xts']))
lost = []
seen_ev = {}
for c in sorted(pas, key=lambda c: c['tf']):
    m = c['match_id']
    if (m, c['t0']) in traded: continue
    if any(abs(c['tf'] - x[0]) <= 20_000 or x[0] < c['tf'] <= x[1] for x in tr_by_game.get(m, [])): continue
    x = c['exits'][CI][GI]
    if not c['nofill'] and x and S.trade(c, x, B, CI) is not None: continue
    # one per event (moves in a game within 60 s)
    if m in seen_ev and c['tf'] - seen_ev[m] <= 60_000: continue
    seen_ev[m] = c['tf']; lost.append(c)
out = []; P = out.append
rows = []
for c in lost:
    Q = book(c)
    if len(Q) < 5: rows.append(dict(c=c, reason='GAP (nothing recorded)')); continue
    dq, lq = at(Q, c['tf']), at(Q, c['tf'] + FILL_MS)
    if lq is None: reason = 'GAP'
    elif dq and lq[2] > dq[2] + 0.02 - 1e-9: reason = 'RAN'
    elif lq[3] is not None and lq[3] * lq[2] < 10: reason = 'THIN'
    else: reason = 'OTHER'
    r = dict(c=c, reason=reason)
    for lab, ts in (('t0+1s', c['t0'] + 1000), ('t0+2s', c['t0'] + 2000), ('decision', c['tf'])):
        e = at(Q, ts)
        if e and 0.03 <= e[2] <= 0.95: r[lab] = sell(Q, c, ts, e[2])
    rows.append(r)
P('MOVES THAT PASSED EVERY RULE BUT WOULD NOT FILL (or the offer was gone): %d separate events (one per game within 60 s)\n' % len(rows))
for k, v in collections.Counter(r['reason'] for r in rows).most_common(): P('  %-28s %3d' % (k, v))
P('\nIF THE ORDER HAD BEEN FASTER (bought at the ask then, sold at the bot\'s target or at 5 min; simple pricing):')
for lab in ('t0+1s', 't0+2s', 'decision'):
    X = [r[lab] for r in rows if lab in r]
    if X: P('  bought %-9s %3d events priced | caught up %3d | won %3d (%3.0f%%) | %+7.2f $ = %+5.2f a trade' % (lab, len(X), sum(1 for p, cg in X if cg), sum(1 for p, cg in X if p > 0), 100.0 * sum(1 for p, cg in X if p > 0) / len(X), sum(p for p, cg in X), sum(p for p, cg in X) / len(X)))
    for rs in ('RAN', 'THIN', 'OTHER'):
        Y = [r[lab] for r in rows if lab in r and r['reason'] == rs]
        if Y: P('      %-6s %3d | won %3d | %+7.2f $' % (rs, len(Y), sum(1 for p, cg in Y if p > 0), sum(p for p, cg in Y)))
# the bot's own trades priced the same way, for scale
own = []
for t in T:
    Q = book(t['c'])
    if len(Q) >= 5: own.append(sell(Q, t['c'], t['c']['tf'], t['entry']))
P('\n  for scale, the bot\'s %d trades priced the same way: won %d, %+.2f $' % (len(own), sum(1 for p, cg in own if p > 0), sum(p for p, cg in own)))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/nofill_check.txt'), 'w').write(txt + '\n')
