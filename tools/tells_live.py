"""The tells of tools/tells.py on the bot's LIVE trades (real since 20 Nov, plus paper and trial trades that are not copies).
Reads only each trade's own game around its buy. usage: tells_live.py"""
import sys, os, json, sqlite3, datetime as dt
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
import tells_feats as TF
db = TF.db
SINCE = 1_795_129_200_000
no = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.timezone(dt.timedelta(hours=1))).strftime('%d %H:%M')
rows = db.execute("SELECT id, bot, real, data FROM bot_trades WHERE ts_ms >= ? AND closed_ms IS NOT NULL ORDER BY ts_ms", (SINCE,)).fetchall()
T = []
for i, bot, real, data in rows:
    d = json.loads(data)
    if d.get('twin_of') or 'move_ms' not in d or d.get('pnl') is None: continue
    c = dict(match_id=d['match_id'], outcome=d['outcome'], t0=d['move_ms'], tf=d['ts'], dir=int(d['dir']), _mv=d['move'], trigger=d['trigger'])
    g = TF.feats(c); cx = d.get('cx') or {}
    T.append(dict(id=i, kind='REAL' if real else ('trial' if bot != 'snipe' else 'paper'), d=d, g=g, pnl=d['pnl'], cost=d.get('cost') or d.get('stake') or 1, jumps=cx.get('jumps', 0),
                  follow=cx.get('follow'), lead=cx.get('lead'), score=(cx['follow'] - cx['lead']) if cx.get('jumps') else None))
print(f"{len(T)} closed live trades since 20 Nov (real {sum(t['kind'] == 'REAL' for t in T)}, paper {sum(t['kind'] == 'paper' for t in T)}, trial {sum(t['kind'] == 'trial' for t in T)})")
r2 = lambda x: '   -' if x is None else f"{x:5.2f}"
for t in T:
    d, g = t['d'], t['g']
    print(f"  {t['kind']:5s} #{t['id']:<3d} {no(d['ts'])} {d['sport']:8s} {(d['home'] + ' v ' + d['away'])[:30]:30s} paid {100 * d['entry']:4.1f}c pnl {t['pnl']:+6.2f} {d.get('reason', '')[:10]:10s} | move {100 * d['move']:4.1f} jumps {t['jumps']:2d} follow {r2(t['follow'])} "
          f"score {r2(t['score'])} spread {100 * d['spread']:.1f}c | re-quotes before buy {g.get('q_upd')} | others followed at buy {r2(g.get('oth_buy'))} | PM drift 60s {'-' if g.get('drift60') is None else round(100 * g['drift60'], 1)}c | quote age {g.get('q_age')}")
json.dump([{k: v for k, v in t.items() if k != 'd'} | {'sport': t['d']['sport'], 'ts': t['d']['ts'], 'spread': t['d']['spread'], 'move': t['d']['move']} for t in T], open(os.path.expanduser('~/swifteye/research/tells_live.json'), 'w'))


def grp(name, fn, sel=T):
    for lab, want in (('yes', True), ('no ', False)):
        g = [t for t in sel if fn(t) is want]
        if g: print(f"    {name:44s} {lab}: {len(g):2d} trades, {sum(t['pnl'] > 0 for t in g):2d} won, ${sum(t['pnl'] for t in g):+6.2f}, {100 * sum(t['pnl'] for t in g) / sum(t['cost'] for t in g):+5.1f}% a $")


for nm, sel in (('REAL trades only', [t for t in T if t['kind'] == 'REAL']), ('all live trades (real, paper, trial)', T)):
    print(f"\n  {nm}")
    grp('10+ earlier jumps in the game', lambda t: t['jumps'] >= 10, sel)
    grp('follow 50%+', lambda t: t['follow'] is not None and t['follow'] >= 0.5, sel)
    grp('delay score 0.2+', lambda t: t['score'] is not None and t['score'] >= 0.2, sel)
    grp('no Polymarket re-quote before the buy', lambda t: None if t['g'].get('q_upd') is None else t['g']['q_upd'] == 0, sel)
    grp('other bookmakers had followed 10%+ at the buy', lambda t: None if t['g'].get('oth_buy') is None else t['g']['oth_buy'] >= 0.1, sel)
    grp('Polymarket had NOT drifted our way (60 s)', lambda t: None if t['g'].get('drift60') is None else t['g']['drift60'] <= 0.01, sel)
