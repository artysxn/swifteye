"""Can the upgraded wave methods (detector v3, delay v5, correlation v3) improve the bot by a small adjustment?
For every replay move and every real/paper buy: from the 30 minutes BEFORE the move (nothing after it),
  - the game's wave profile against the bookmaker that triggered and against all bookmakers:
      pairs, correlation, delay between fast parts, and how much of Polymarket's wave was usually still to come
      when the bookmaker moved (`rem`)
  - what Polymarket's own line was doing at that moment: its current stroke (up / down / flat for our side), how big, how old.
usage: wave_bot_test.py build     -> ~/swifteye/research/wave_bot.json   (about 8 minutes)
       wave_bot_test.py test
"""
import sys, os, json, time, statistics as st, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
mode = sys.argv[1] if len(sys.argv) > 1 else 'test'
sys.argv = ['sweep.py', 'none']
import sweep as S, waves as W, wave_truth as WT
OUT = os.path.expanduser('~/swifteye/research/wave_bot.json')
ci = S.CATCH.index(1.0)
LOOSE = dict(S.BASE, lag=None, weak_cap=None, min_move=0.07, max_followed=0.10, min_edge=0.03, dota_spread=None, bb='all', pause=False)
key = lambda c: '%d|%s|%d|%s' % (c['match_id'], c['outcome'], c['t0'], c['trigger'])
cents = lambda a: [None if v is None else round(v * 100.0, 2) for v in a]


def feats(db, mid, oc, dr, t_move, trigger, clock):
    t1 = t_move - 15000; g = W.grid(db, mid, oc, dr, t1 - 1800000, t1, trigger, clock=clock)
    xp = cents(g['bid']); out = {}
    last20 = [v for a in (xp[-1200:], cents(g['cons'])[-1200:]) for v in a if v is not None]
    if len(last20) < 200: return None
    H = max(8.0, max(last20) - min(last20)); n = len(xp)
    for nm, arr in (('trig', cents(g['trig'])), ('all', cents(g['cons']))):
        if sum(1 for v in arr if v is not None) < 300: out[nm] = None; continue
        P, B, pr = WT.pair4(xp, arr, H, upto=n - 1); r = WT.scores3(P, B, pr, now=n)
        out[nm] = r and dict(corr=r['corr'], delay=r['delay'], pairs=r['pairs'], first=r['first'], behind=r['behind'], rem=r['rem'], np=r['np'], nb=r['nb'])
    ws = WT.waves3(xp, H, **WT.V3)
    if ws:
        w = ws[-1]; out['pm'] = dict(type=w['type'], size=w['vb'] - w['va'], age=n - 1 - w['a'], H=H)
        prev = ws[-2] if len(ws) > 1 else None; out['pm_prev'] = prev and dict(type=prev['type'], size=prev['vb'] - prev['va'])
    return out


if mode == 'build':
    db = W.conn(); cand = [c for c in S.ROWS if S.gate(c, LOOSE, ci)]; prof = {}; cache = {}; t_start = time.time()
    print('replay moves:', len(cand), flush=True)
    for n, c in enumerate(cand):
        ck = (c['match_id'], c['outcome'], c['dir'], c['t0'] // 30000, c['trigger'])
        if ck not in cache: cache[ck] = feats(db, c['match_id'], c['outcome'], float(c['dir']), c['t0'], c['trigger'], 'server')
        prof[key(c)] = cache[ck]
        if n % 200 == 0: print(n, 'of', len(cand), '%.0f s' % (time.time() - t_start), flush=True)
    live = {}
    for tr in W.trades(db):
        d = tr['d']; live[str(tr['id'])] = feats(db, tr['mid'], d['outcome'], d.get('dir', 1.0), d['move_ms'], d.get('trigger'), 'ours')
    json.dump(dict(replay=prof, live=live), open(OUT, 'w')); print('written', OUT, len(prof), len(live), '%.0f s' % (time.time() - t_start)); sys.exit(0)

D = json.load(open(OUT)); prof = D['replay']
for c in S.ROWS: c['_w'] = prof.get(key(c))
def tot(T): return sum(t['pnl'] for t in T)
def line(name, T, T0=None):
    dp = collections.defaultdict(float); dn = collections.Counter()
    for t in T: dy = time.strftime('%d', time.localtime(t['c']['t0'] / 1000)); dp[dy] += t['pnl']; dn[dy] += 1
    print('  %-66s trades %3d | won %3d (%3.0f%%) | $%+8.2f%s | by day %s' % (name, len(T), sum(1 for t in T if t['pnl'] > 0), 100.0 * sum(1 for t in T if t['pnl'] > 0) / max(len(T), 1), tot(T), (' (%+.2f)' % (tot(T) - tot(T0))) if T0 is not None else '', ' '.join('%s:%+.1f/%d' % (d, dp[d], dn[d]) for d in sorted(dn))))
def P_(c, nm): return c['_w'] and c['_w'].get(nm)
def flt(nm, pred, pairs=3): return [c for c in S.ROWS if P_(c, nm) and P_(c, nm)['pairs'] >= pairs and P_(c, nm)['rem'] is not None and pred(P_(c, nm))]
def pm(pred): return [c for c in S.ROWS if c['_w'] and c['_w'].get('pm') and pred(c['_w']['pm'])]
for title, P in (("TODAY'S RULES", dict(S.BASE)), ("TODAY'S RULES WITHOUT the bot's own 'lagged 50%' and weak-game rules", dict(S.BASE, lag=None, weak_cap=None)), ('LOOSER SET (7pt+, followed 10%, room 3c, every sport)', LOOSE)):
    print('\n== ' + title); T0 = S.sim(P); line('all moves', T0)
    for nm, lab in (('trig', 'against the TRIGGER bookmaker'), ('all', 'against ALL bookmakers')):
        print('  the game\'s waves in the 30 min before, ' + lab)
        line('   3+ pairs known', S.sim(P, flt(nm, lambda r: True)))
        line('   usually 60%+ of Polymarket\'s wave still to come', S.sim(P, flt(nm, lambda r: r['rem'] >= 0.6)))
        line('   30 to 60% still to come', S.sim(P, flt(nm, lambda r: 0.3 <= r['rem'] < 0.6)))
        line('   under 30% still to come (Polymarket had mostly moved)', S.sim(P, flt(nm, lambda r: r['rem'] < 0.3)))
        line('   bookmaker first by 3 s+ (fast parts)', S.sim(P, flt(nm, lambda r: r['delay'] > 3)))
        line('   Polymarket first by 3 s+', S.sim(P, flt(nm, lambda r: r['delay'] < -3)))
        line('   correlation 70+', S.sim(P, flt(nm, lambda r: r['corr'] >= 70)))
        line('   correlation under 50', S.sim(P, flt(nm, lambda r: r['corr'] < 50)))
        line('   RULE: skip when under 30% is usually still to come', S.sim(P, [c for c in S.ROWS if not (P_(c, nm) and P_(c, nm)['pairs'] >= 3 and P_(c, nm)['rem'] is not None and P_(c, nm)['rem'] < 0.3)]), T0)
    print('  what Polymarket\'s own line was doing when the bookmaker jumped (its current stroke, our side)')
    line('   rising already (an up stroke of 3c+)', S.sim(P, pm(lambda w: w['type'] == 'up' and w['size'] >= 3)))
    line('   flat', S.sim(P, pm(lambda w: w['type'] == 'flat')))
    line('   falling (a down stroke of 3c+)', S.sim(P, pm(lambda w: w['type'] == 'down' and w['size'] <= -3)))
    line('   RULE: skip when Polymarket is already rising', S.sim(P, [c for c in S.ROWS if not (c['_w'] and c['_w'].get('pm') and c['_w']['pm']['type'] == 'up' and c['_w']['pm']['size'] >= 3)]), T0)
    line('   RULE: skip when Polymarket is falling', S.sim(P, [c for c in S.ROWS if not (c['_w'] and c['_w'].get('pm') and c['_w']['pm']['type'] == 'down' and c['_w']['pm']['size'] <= -3)]), T0)
# the bot's own buys
db = W.conn(); L = D['live']; rows = []
for tr in W.trades(db):
    f = L.get(str(tr['id'])); d = tr['d']
    rows.append(dict(id=tr['id'], day=time.strftime('%d', time.localtime(tr['ts'] / 1000)), pnl=W.pnl12(d, d['exit']), real=tr['real'], f=f))
print('\n== THE BOT\'S OWN %d BUYS ($%+.2f at $12)' % (len(rows), sum(r['pnl'] for r in rows)))
def b(name, pred):
    g = [r for r in rows if r['f'] and pred(r['f'])]
    if g: print('  %-66s trades %3d | won %3d | $%+8.2f | real only %d trades $%+.2f' % (name, len(g), sum(1 for r in g if r['pnl'] > 0), sum(r['pnl'] for r in g), sum(1 for r in g if r['real']), sum(r['pnl'] for r in g if r['real'])))
ok = lambda f, nm: f.get(nm) and f[nm]['pairs'] >= 3 and f[nm]['rem'] is not None
for nm, lab in (('trig', 'trigger bookmaker'), ('all', 'all bookmakers')):
    b('%s: usually 60%%+ still to come' % lab, lambda f: ok(f, nm) and f[nm]['rem'] >= 0.6); b('%s: 30 to 60%%' % lab, lambda f: ok(f, nm) and 0.3 <= f[nm]['rem'] < 0.6); b('%s: under 30%% still to come' % lab, lambda f: ok(f, nm) and f[nm]['rem'] < 0.3)
    b('%s: fewer than 3 pairs known' % lab, lambda f: not ok(f, nm))
b('Polymarket already rising (up stroke 3c+)', lambda f: f.get('pm') and f['pm']['type'] == 'up' and f['pm']['size'] >= 3); b('Polymarket flat', lambda f: f.get('pm') and f['pm']['type'] == 'flat'); b('Polymarket falling (down stroke 3c+)', lambda f: f.get('pm') and f['pm']['type'] == 'down' and f['pm']['size'] <= -3)
