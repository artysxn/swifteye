import sqlite3, json, os, sys, time, collections
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
fee = lambda p: 0.05 * p * (1 - p)
def core(d):
    try: bm, fol, sp, dec, cx = d['move'], d['followed'], d['spread'], d['dec'], d['cx']
    except KeyError: return None
    if bm[0] < 0.08 or min(d['books']) < 2 or max(fol) > 0.05 or d.get('followed_since_start', 0) > 0.05 or max(sp) > 0.04 + 1e-9: return False
    if not all(0.02 <= x <= 0.98 for x in dec) or cx.get('fam_moved', 0) < 2 or cx.get('cons_move', 0) < 0.03 or cx.get('jumps', 0) == 0 or cx.get('follow', 0) < 0.5: return False
    g = d.get('gap_before')
    if (g is not None and g >= 0.10) or max(dec) > 0.90: return False
    e = min(bm[i] - sp[i] - 0.02 - fee(dec[i]) - fee(min(max(dec[i] + bm[i] - sp[i] - 0.02, 0), 1)) for i in (0, 1))
    return e >= 0.04
t0 = 1795215600000
by = collections.defaultdict(list); other = collections.defaultdict(set)
for ts, mid, res, data in db.execute("SELECT ts_ms, match_id, result, data FROM bot_signals WHERE ts_ms >= ? ORDER BY ts_ms", (t0,)):
    k = (mid, ts // 120000)
    if res == 'entered': other[k].add(res); continue
    d = json.loads(data)
    if core(d):
        if res in ('data_gap', 'measured_too_late'): by[k].append((ts, res, d))
        else: other[k].add(res)
names = {}
for k, v in sorted(by.items(), key=lambda x: x[1][0][0]):
    if other.get(k): continue
    mid = k[0]; ts, res, d = v[0]
    if mid not in names:
        r = db.execute("SELECT data FROM bot_signals WHERE match_id=? AND data LIKE '%home%' LIMIT 1", (mid,)).fetchone()
        names[mid] = ''
    trig = sorted(set(x[2]['trigger'] for x in v)); oc = d['outcome']
    # which source reopened in (t-14 s, t+2 s]
    opens = db.execute("SELECT book, ts_ms FROM changes WHERE match_id=? AND market='1x2' AND outcome=? AND kind='open' AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms", (mid, oc, ts - 16000, ts + 1000)).fetchall()
    closes = db.execute("SELECT book, ts_ms, kind FROM changes WHERE match_id=? AND market='1x2' AND outcome=? AND kind IN ('close','suspend') AND ts_ms BETWEEN ? AND ? ORDER BY ts_ms", (mid, oc, ts - 600000, ts)).fetchall()
    src = sorted(set(b for b, _ in opens))
    last_close = {}
    for b, t, kind in closes: last_close[b] = (t, kind)
    det = []
    for b, t in opens:
        lc = last_close.get(b)
        det.append('%s reopened %ds before%s' % (b, (ts - t) / 1000, (' (was %s for %ds)' % (lc[1], (t - lc[0]) / 1000)) if lc else ''))
    # what Polymarket did after: best bid of our side at the signal and 60/300 s later
    q = db.execute("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms BETWEEN ? AND ? ORDER BY id", (mid, oc, ts - 2000, ts + 300000)).fetchall()
    def at(t):
        c = None
        for tt, b, a in q:
            if tt <= t: c = (b, a)
            else: break
        return c
    dr = d['dir']; side = lambda x: None if x is None or x[0] is None or x[1] is None else ((x[1], x[0]) if dr > 0 else (1 - x[0], 1 - x[1]))
    a0, a60, a300 = side(at(ts + 1000)), side(at(ts + 60000)), side(at(ts + 300000))
    fmt = lambda x: '-' if x is None else '%.0f/%.0fc' % (x[0] * 100, x[1] * 100)
    print('%s %-10s game %d trig %-22s move %.1fpt, signals %d | %s' % (time.strftime('%H:%M:%S', time.localtime(ts / 1000)), d.get('sport'), mid, ','.join(trig)[:22], d['move'][0] * 100, len(v), '; '.join(det) or 'no reopen row found in the 16 s before'))
    print('      our side ask/bid at the move %s | 60 s later %s | 5 min later %s' % (fmt(a0), fmt(a60), fmt(a300)))
