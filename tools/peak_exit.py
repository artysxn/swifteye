# Sale-only test: same buys as the bot really made (real, paper and trial), sold when Polymarket's own
# spike ends (bid made a high >= entry + A, then fell B below that high) instead of waiting for the target.
import sqlite3, json, os, sys, bisect, collections
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
if db.execute("SELECT COUNT(*) FROM bot_trades WHERE real=1 AND closed_ms IS NULL").fetchone()[0]:
    print('a real position is open: not running'); sys.exit(0)
DELAY = 1500
rows = db.execute("SELECT id, bot, real, ts_ms, closed_ms, match_id, data FROM bot_trades WHERE bot IN ('snipe','snipe-trial') AND closed_ms IS NOT NULL ORDER BY real DESC, id").fetchall()
seen = set(); T = []
for id_, bot, real, ts, closed, mid, data in rows:
    d = json.loads(data)
    if d.get('move_ms') is None or d.get('entry') is None or d.get('exit') is None: continue
    k = (mid, d['outcome'], d['move_ms'])
    if k in seen: continue
    seen.add(k)
    dr = d.get('dir', 1.0); oc = d['outcome']
    q = db.execute("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms BETWEEN ? AND ? ORDER BY id", (mid, oc, ts, closed + 5000)).fetchall()
    path = [(t, (b if dr > 0 else (1.0 - a))) for t, b, a in q if b is not None and a is not None and b > 0 and a > 0]
    tg = db.execute("SELECT ts_ms, new_price FROM changes WHERE match_id=? AND book=? AND market='1x2' AND outcome=? AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms", (mid, d.get('trigger'), oc, d['move_ms'] - 600000, closed)).fetchall()
    pre = None; top = None; tpath = []
    for t, p in tg:
        if not p: continue
        pr = 1.0 / p
        if t < d['move_ms']: pre = pr
        elif t == d['move_ms']: top = pr
        if t >= d['move_ms']: tpath.append((t, pr))
    T.append(dict(id=id_, bot=bot, real=real, ts=ts, closed=closed, d=d, path=path, pre=pre, top=top, tpath=tpath, day=__import__('time').strftime('%d', __import__('time').localtime(ts / 1000))))

def pnl12(d, exit_px):  # dollars at a $12 stake, both fees
    e = d['entry']; n = 12.0 / e; r = d.get('fee_rate', 0.05)
    return n * (exit_px - e) - n * r * e * (1 - e) - n * r * exit_px * (1 - exit_px)

def gave_back(t, tr):
    if tr['pre'] is None or tr['top'] is None or abs(tr['top'] - tr['pre']) < 1e-9: return None
    cur = tr['top']
    for tt, pr in tr['tpath']:
        if tt <= t: cur = pr
        else: break
    return (tr['top'] - cur) / (tr['top'] - tr['pre'])

def run(tr, A, B, G):
    d = tr['d']; e = d['entry']; base = pnl12(d, d['exit'])
    peak = None; ts_ = [p[0] for p in tr['path']]
    for t, bid in tr['path']:
        if t >= tr['closed'] - DELAY: break
        peak = bid if peak is None else max(peak, bid)
        if peak >= e + A - 1e-9 and bid <= peak - B + 1e-9:
            if G is not None:
                g = gave_back(t, tr)
                if g is None or g < G: continue
            i = bisect.bisect_right(ts_, t + DELAY) - 1
            return pnl12(d, tr['path'][i][1]), base, t
    return base, base, None

print('buys:', len(T), '| real', sum(1 for t in T if t['real']), '| by day', dict(collections.Counter(t['day'] for t in T)))
base_all = sum(pnl12(t['d'], t['d']['exit']) for t in T)
print('as sold by the bot, at $12 a trade: $%+.2f | won %d of %d' % (base_all, sum(1 for t in T if pnl12(t['d'], t['d']['exit']) > 0), len(T)))
for G in (None, 0.25, 0.5):
    print('\n-- %s' % ('any time' if G is None else 'only once the trigger bookmaker gave back %d%%+ of its move' % int(G * 100)))
    for A in (0.0, 0.02, 0.03, 0.05):
        for B in (0.01, 0.02, 0.03):
            res = [run(t, A, B, G) for t in T]
            tot = sum(r[0] for r in res); fired = [r for r in res if r[2] is not None]
            better = sum(1 for r in fired if r[0] > r[1] + 1e-9); worse = sum(1 for r in fired if r[0] < r[1] - 1e-9)
            byday = collections.defaultdict(float)
            for t, r in zip(T, res): byday[t['day']] += r[0] - r[1]
            won = sum(1 for r in res if r[0] > 0)
            print('  high %dc+ above the buy, then %dc off the high: fires %2d (better %2d, worse %2d) | total $%+7.2f (%+6.2f) | won %2d | by day %s' % (round(A * 100), round(B * 100), len(fired), better, worse, tot, tot - base_all, won, ' '.join('%s:%+.1f' % (k, byday[k]) for k in sorted(byday))))
if len(sys.argv) > 1:
    A, B = float(sys.argv[1]), float(sys.argv[2]); G = float(sys.argv[3]) if len(sys.argv) > 3 else None
    print('\nper trade, rule A=%.2f B=%.2f G=%s' % (A, B, G))
    for t in T:
        new, base, ft = run(t, A, B, G)
        if ft is not None:
            d = t['d']; print('  #%d %s %s %s real=%d entry %.1fc -> bot sold %.1fc (%s) $%+.2f | rule sells %ds after the buy $%+.2f (%+.2f)' % (t['id'], t['day'], d.get('sport'), d.get('trigger'), t['real'], d['entry'] * 100, d['exit'] * 100, d.get('reason'), base, (ft - t['ts']) / 1000, new, new - base))
