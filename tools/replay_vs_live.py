"""How accurate is the replay? (owner, 22 Nov 2026: "is the test good now? can the replay support multiple combinations of
rule changes and be accurate to how much we would have made throughout 17-22 nov?")

The bot's own trades (bot_trades: paper 'snipe', real, 'snipe-trial', 'snipe-1book'; 18-21 Nov) matched to the replay's
version of the same move (tools/sweep.py, SWEEP_DATA=v18 with the strict gap checks: same game and side, buy within
20 s). For each matched trade: does the replay have it, is it flagged as a recording gap / no fill, and how far are the
replay's buy price, sale price and $ (per $12) from what the bot really got. The replay's exit is taken at the trade's
own catch-up share and give-up time when the replay holds that pair, else the nearest it holds.
usage: SWEEP_DATA=v18 python3 replay_vs_live.py      -> ~/swifteye/research/replay_vs_live.txt
"""
import os, sys, json, sqlite3, bisect, collections, statistics as st, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
live = []
for bot, real, ts, mid, data in db.execute("SELECT bot, real, ts_ms, match_id, data FROM bot_trades WHERE closed_ms IS NOT NULL AND bot LIKE 'snipe%'"):
    d = json.loads(data)
    if not d.get('cost'): continue
    live.append(dict(bot=bot + (' REAL' if real else ''), real=real, ts=d.get('ts', ts), m=mid, o=d['outcome'], dir=d['dir'], entry=d['entry'], exit=d['exit'],
                     pnl12=12.0 * d['pnl'] / d['cost'], catch=d.get('catch', 1.0), give=d.get('give_up_s', 300), reason=d.get('reason'), day=time.strftime('%d', time.localtime(d.get('ts', ts) / 1000))))
# match on the side actually BOUGHT: (outcome, dir +1) buys that outcome; (outcome, dir -1) buys the other one
other = {'home': 'away', 'away': 'home'}
bought = lambda o, d: o if d > 0 else other.get(o, '!' + o)
idx = collections.defaultdict(list)
for c in S.ROWS: idx[(c['match_id'], bought(c['outcome'], c['dir']))].append(c)
for k in idx: idx[k].sort(key=lambda c: c['tf'])
out = []; P = out.append
rows = []
for t in live:
    L = idx.get((t['m'], bought(t['o'], t['dir'])), [])
    near = [c for c in L if abs(c['tf'] - t['ts']) <= 20_000]
    if not near: rows.append(dict(t=t, status='NOT IN THE REPLAY')); continue
    c = min(near, key=lambda c: abs(c['tf'] - t['ts']))
    if c['blind']: rows.append(dict(t=t, status='replay: in a recording gap', c=c)); continue
    if c['nofill']: rows.append(dict(t=t, status='replay: would not fill', c=c)); continue
    ci = min(range(len(S.CATCH)), key=lambda i: abs(S.CATCH[i] - (t['catch'] or 1.0))); gi = min(range(len(S.GIVE)), key=lambda i: abs(S.GIVE[i] - (t['give'] or 300)))
    x = c['exits'][ci][gi]
    tr = S.trade(c, x, dict(S.BASE, catch=S.CATCH[ci], real_sale=bool(t['real'])), ci) if x else None      # each trade sold as it really was: real at the bids, paper 1c under
    if tr is None: rows.append(dict(t=t, status='replay: buy not there (2c+ dearer)', c=c)); continue
    rows.append(dict(t=t, status='MATCHED', c=c, r_entry=tr[2], r_exit=tr[3], r_pnl=tr[0], r_caught=x[4]))
P('REPLAY v LIVE: %d closed bot trades (18-21 Nov) matched to the replay (v18 + strict gap checks)\n' % len(live))
for b in sorted({r['t']['bot'] for r in rows}):
    X = [r for r in rows if r['t']['bot'] == b]
    P('  %-18s %3d trades: %s' % (b, len(X), ', '.join('%s %d' % (k, v) for k, v in collections.Counter(r['status'] for r in X).most_common())))
M = [r for r in rows if r['status'] == 'MATCHED']
P('\nMATCHED TRADES (%d): the replay v what the bot really got, per $12' % len(M))
if M:
    de = [100 * (r['r_entry'] - r['t']['entry']) for r in M]; dx = [100 * (r['r_exit'] - r['t']['exit']) for r in M]; dp = [r['r_pnl'] - r['t']['pnl12'] for r in M]
    P('  buy price: replay minus real, median %+.1fc (middle half %+.1f to %+.1fc)' % (st.median(de), sorted(de)[len(de) // 4], sorted(de)[3 * len(de) // 4]))
    P('  sale price: replay minus real, median %+.1fc (middle half %+.1f to %+.1fc)' % (st.median(dx), sorted(dx)[len(dx) // 4], sorted(dx)[3 * len(dx) // 4]))
    P('  same outcome (both won or both lost): %d of %d' % (sum(1 for r in M if (r['r_pnl'] > 0) == (r['t']['pnl12'] > 0)), len(M)))
    for b in sorted({r['t']['bot'] for r in M}):
        X = [r for r in M if r['t']['bot'] == b]
        P('  %-18s %3d | real %+7.2f $ (won %d) | replay %+7.2f $ (won %d) | replay - real %+6.2f $ (%+.2f a trade)' % (b, len(X), sum(r['t']['pnl12'] for r in X), sum(1 for r in X if r['t']['pnl12'] > 0), sum(r['r_pnl'] for r in X), sum(1 for r in X if r['r_pnl'] > 0), sum(r['r_pnl'] - r['t']['pnl12'] for r in X), sum(r['r_pnl'] - r['t']['pnl12'] for r in X) / len(X)))
    P('  ALL                %3d | real %+7.2f $ | replay %+7.2f $ | replay - real %+6.2f $ (%+.2f a trade)' % (len(M), sum(r['t']['pnl12'] for r in M), sum(r['r_pnl'] for r in M), sum(dp), sum(dp) / len(M)))
    P('\n  the biggest differences:')
    for r in sorted(M, key=lambda r: -abs(r['r_pnl'] - r['t']['pnl12']))[:8]:
        t = r['t']
        P('   %s %-16s %s | real: bought %.0fc sold %.0fc (%s) %+.2f $ | replay: bought %.0fc sold %.0fc (%s) %+.2f $' % (time.strftime('%d %b %H:%M', time.localtime(t['ts'] / 1000)), t['bot'], r['c']['sport'],
            100 * t['entry'], 100 * t['exit'], t['reason'], t['pnl12'], 100 * r['r_entry'], 100 * r['r_exit'], 'caught' if r['r_caught'] else 'gave up', r['r_pnl']))
U = [r for r in rows if r['status'] != 'MATCHED']
if U:
    P('\nNOT MATCHED (%d): what those trades really made, per $12: %+.2f $ (won %d)' % (len(U), sum(r['t']['pnl12'] for r in U), sum(1 for r in U if r['t']['pnl12'] > 0)))
    for s in sorted({r['status'] for r in U}):
        X = [r for r in U if r['status'] == s]
        P('  %-38s %3d | really made %+7.2f $' % (s, len(X), sum(r['t']['pnl12'] for r in X)))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/replay_vs_live.txt'), 'w').write(txt + '\n')
