"""Daily scorecard of the live bots: what was traded, how well the orders filled, and whether the traits that picked
winners in the replay hold on real money. Reads only the bot's own tables and each trade's own game.
  strong   the game's delay score is 0.2+ (Polymarket moves after the bookmakers there)
  trend    the trigger bookmaker's own 10-minute move was not against the trade (3 pt)
  drift    Polymarket had NOT already drifted our way in the last 60 s (1c)
usage: scorecard.py [hours back, default: since midnight Norway]   (add 'all' for everything since 20 Nov)"""
import sys, os, json, sqlite3, datetime as dt
from collections import Counter
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
ARG = sys.argv[1] if len(sys.argv) > 1 else None
sys.argv = ['x']
import tells_feats as TF
db = TF.db
NO = dt.timezone(dt.timedelta(hours=1))
now = dt.datetime.now(NO)
REAL_SINCE = 1_795_129_200_000
if ARG == 'all': since = REAL_SINCE
elif ARG: since = int((now - dt.timedelta(hours=float(ARG))).timestamp() * 1000)
else: since = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
hm = lambda ms: dt.datetime.fromtimestamp(ms / 1000, NO).strftime('%d %H:%M')
print(f"SCORECARD from {hm(since)} to {now:%d %H:%M} Norway")


def traits(d):
    cx = d.get('cx') or {}; tr = d.get('tr') or [None] * 4
    sc = (cx['follow'] - cx['lead']) if cx.get('jumps') else None
    g = TF.feats(dict(match_id=d['match_id'], outcome=d['outcome'], t0=d['move_ms'], tf=d['ts'], dir=int(d['dir']), _mv=d['move'], trigger=d['trigger'])) if 'move_ms' in d else {}
    return dict(strong=sc is not None and sc >= 0.2, trend=None if tr[2] is None else tr[2] >= -0.03, drift=None if g.get('drift60') is None else g['drift60'] <= 0.01, score=sc)


rows = [(i, bot, real, ts, cl, json.loads(d)) for i, bot, real, ts, cl, d in db.execute("SELECT id, bot, real, ts_ms, closed_ms, data FROM bot_trades WHERE ts_ms >= ? ORDER BY ts_ms", (since,))]
real = [(i, ts, cl, d) for i, bot, r, ts, cl, d in rows if r and bot == 'snipe']
done = [x for x in real if x[2] and x[3].get('pnl') is not None]
print(f"\nREAL MONEY: {len(real)} trades, {len(done)} closed, {sum(x[3]['pnl'] > 0.005 for x in done)} won, ${sum(x[3]['pnl'] for x in done):+.2f}" + (f" ({100 * sum(x[3]['pnl'] for x in done) / sum(x[3]['cost'] for x in done):+.1f}% of the money staked)" if done else ''))
T = []
for i, ts, cl, d in real:
    t = traits(d); seen = d.get('best'); slip = (d['entry'] - seen) if seen else None
    T.append((d, t, slip))
    yn = lambda v: '?' if v is None else ('yes' if v else 'no ')
    print(f"  {hm(ts)} {d['sport']:9s} {(d['home'] + ' v ' + d['away'])[:28]:28s} move {100 * d['move']:4.1f} | saw {100 * seen if seen else 0:4.0f}c paid {100 * d['entry']:4.0f}c | strong {yn(t['strong'])} trend {yn(t['trend'])} drift {yn(t['drift'])} | {(d.get('reason') or 'open')[:10]:10s} {('%+.2f' % d['pnl']) if d.get('pnl') is not None else ''}")
if done:
    print("  the three traits on these trades (won / trades, result):")
    for nm, k in (('strong game', 'strong'), ('bookmaker trend not against', 'trend'), ('no Polymarket drift our way', 'drift')):
        for lab, want in (('yes', True), ('no', False)):
            g = [(d, t) for d, t, s in T if d.get('pnl') is not None and t[k] is want]
            if g: print(f"     {nm:28s} {lab}: {sum(d['pnl'] > 0.005 for d, t in g)}/{len(g)}, ${sum(d['pnl'] for d, t in g):+.2f}")
    al = [(d, t) for d, t, s in T if d.get('pnl') is not None and t['strong'] and t['trend'] and t['drift']]
    print(f"     all three: {sum(d['pnl'] > 0.005 for d, t in al)}/{len(al)}, ${sum(d['pnl'] for d, t in al):+.2f}")
    sl = [s for d, t, s in T if s is not None]
    if sl: print(f"  fills against the price seen: {sum(abs(s) < 0.005 for s in sl)} at the price, {sum(s >= 0.005 for s in sl)} dearer (worst {100 * max(sl):+.0f}c), {sum(s <= -0.005 for s in sl)} cheaper (best {100 * min(sl):+.0f}c)")
od = [json.loads(d) | {'ok': ok} for ok, d in db.execute("SELECT ok, data FROM bot_orders WHERE ts_ms >= ? AND bot = 'snipe'", (since,))]
buys = [o for o in od if o.get('side') == 'buy' and not o.get('intent')]
print(f"  real buy orders sent: {len(buys)}, filled {sum(1 for o in buys if o['ok'])}, found nothing {sum(1 for o in buys if not o['ok'])}; sent with the feed over 1 s late: {sum(1 for o in buys if (o.get('late_ms') or 0) > 1000)}")

sig = dict(db.execute("SELECT result, count(*) FROM bot_signals WHERE ts_ms >= ? GROUP BY 1", (since,)).fetchall())
tot = sum(sig.values())
LAB = dict(move_too_small='move too small', book_reverted='bookmaker moved back', already_moved='Polymarket had already moved', no_market_price='no Polymarket price', moved_with_first_book='Polymarket moved with the first bookmaker',
           data_gap='gap in the recorded prices', measured_too_late='measured too late', spread_too_wide='spread too wide', too_few_feeds='too few bookmaker groups', consensus_did_not_move="bookmakers' average did not move",
           polymarket_does_not_lag='Polymarket does not lag in this game', weak_game_dear_price='weak game over 60c', price_too_high='price over 90c', feed_late='feed late (paper only)', feed_quiet='feed silent', entered='BOUGHT',
           real_not_filled='real order not filled', paper_trial='bought on the paper trial', paper_only_sport='paper-only sport', basketball='US basketball (paper)', after_pause='right after a pause', same_move='same move again')
print(f"\nMOVES LOOKED AT: {tot}")
for k, n in sorted(sig.items(), key=lambda x: -x[1])[:16]: print(f"  {LAB.get(k, k):42s} {n:6d}")

for bot, name in (('favs', 'PAPER FAVOURITES'), ('snipe-trial', 'PAPER TRIAL (US basketball, wide Dota 2, American football, UFC)')):
    r = [(cl, d) for i, b, rr, ts, cl, d in rows if b == bot]
    c = [d for cl, d in r if cl and d.get('pnl') is not None]
    allr = [json.loads(d) for (d,) in db.execute("SELECT data FROM bot_trades WHERE bot = ? AND closed_ms IS NOT NULL", (bot,))]
    print(f"\n{name}: {len(r)} new, {len(c)} closed in the period ({sum(d['pnl'] > 0 for d in c)} won, ${sum(d['pnl'] for d in c):+.2f}); all time {len(allr)} closed, {sum(d.get('pnl', 0) > 0 for d in allr)} won, ${sum(d.get('pnl', 0) for d in allr):+.2f}")
