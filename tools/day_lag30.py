import sqlite3, json, os, time, collections
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
fee = lambda p: 0.05 * p * (1 - p)
def ok(d, th):
    bm, fol, sp, dec, cx = d['move'], d['followed'], d['spread'], d['dec'], d['cx']
    if bm[0] < 0.08 or min(d['books']) < 2 or max(fol) > 0.05 or d.get('followed_since_start', 0) > 0.05 or max(sp) > 0.04 + 1e-9: return False
    if not all(0.02 <= x <= 0.98 for x in dec) or max(dec) > 0.90 or cx.get('fam_moved', 0) < 2 or cx.get('cons_move', 0) < 0.03: return False
    if cx.get('jumps', 0) == 0 or cx.get('follow', 0) < th: return False
    g = d.get('gap_before')
    if g is not None and g >= 0.10: return False
    return min(bm[i] - sp[i] - 0.02 - fee(dec[i]) - fee(min(max(dec[i] + bm[i] - sp[i] - 0.02, 0), 1)) for i in (0, 1)) >= 0.04
t0 = 1795215600000
m50, m30 = {}, {}
for ts, mid, res, data in db.execute("SELECT ts_ms, match_id, result, data FROM bot_signals WHERE ts_ms >= ? AND result NOT IN ('entered','data_gap') ORDER BY ts_ms", (t0,)):
    d = json.loads(data)
    if 'move' not in d: continue
    k = (mid, ts // 120000)
    if ok(d, 0.5): m50.setdefault(k, (ts, d, res))
    if ok(d, 0.3): m30.setdefault(k, (ts, d, res))
extra = {k: v for k, v in m30.items() if k not in m50}
print('today: pass with lagged 50%%: %d | with lagged 30%% (yesterday): %d | extra: %d' % (len(m50), len(m30), len(extra)), dict(collections.Counter(v[1].get('sport') for v in extra.values())))
for k, (ts, d, res) in sorted(extra.items(), key=lambda x: x[1][0]):
    mid, oc, dr = k[0], d['outcome'], d['dir']
    q = db.execute("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms BETWEEN ? AND ? ORDER BY id", (mid, oc, ts - 2000, ts + 300000)).fetchall()
    def at(t):
        c = None
        for tt, b, a in q:
            if tt <= t: c = (b, a)
            else: break
        if c is None or c[0] is None or c[1] is None: return None
        return (c[1], c[0]) if dr > 0 else (1 - c[0], 1 - c[1])
    a0, a5 = at(ts + 1500), at(ts + 300000)
    best = max([(b if dr > 0 else 1 - a) for tt, b, a in q if tt > ts + 1500 and b is not None and a is not None] or [0])
    tgt = (a0[0] - d['spread'][0] / 2 - 0.01 + d['move'][0]) if a0 else None
    r = None
    if a0 and a5:
        e = a0[0]; x = (tgt - d['spread'][0] / 2 - 0.01) if (tgt and best >= tgt - d['spread'][0] / 2 - 0.01) else a5[1]
        n = 12 / e; r = n * (x - e) - n * fee(e) - n * fee(min(max(x, 0), 1))
    print('  %s %-10s game %d %s move %.1fpt lagged %.2f result %s | buy at %s, 5 min later bid %s, best bid %.0fc | about $%s at $12' % (time.strftime('%H:%M', time.localtime(ts / 1000)), d.get('sport'), mid, d['trigger'], d['move'][0] * 100, d['cx'].get('follow', 0), res, ('%.0fc' % (a0[0] * 100)) if a0 else '-', ('%.0fc' % (a5[1] * 100)) if a5 else '-', best * 100, ('%+.2f' % r) if r is not None else '?'))
