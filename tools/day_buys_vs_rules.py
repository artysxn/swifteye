import sqlite3, json, os, time, collections
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
rows = db.execute("SELECT id, bot, real, ts_ms, match_id, data FROM bot_trades WHERE bot IN ('snipe','snipe-trial') AND ts_ms >= 1795129200000 ORDER BY real DESC, id").fetchall()
seen = set(); out = collections.defaultdict(list)
for id_, bot, real, ts, mid, data in rows:
    d = json.loads(data)
    if d.get('move_ms') is None: continue
    k = (mid, d['outcome'], d['move_ms'])
    if k in seen: continue
    seen.add(k)
    cx = d.get('cx') or {}
    why = []
    if d.get('move', 0) < 0.08: why.append('move<8')
    if cx.get('fam_moved', 9) < 2: why.append('groups<2')
    if cx.get('cons_move', 1) < 0.03: why.append('consensus<3pt')
    if cx and (cx.get('jumps', 0) == 0 or cx.get('follow', 0) < 0.5): why.append('lagged<50%% (%.2f)' % cx.get('follow', 0))
    if d.get('edge', 1) < 0.04: why.append('room<4c (%.3f)' % d.get('edge', 0))
    if d.get('entry', 0) > 0.90: why.append('price>90')
    day = time.strftime('%d', time.localtime(ts / 1000)); hr = time.strftime('%H:%M', time.localtime(ts / 1000))
    out[day].append((id_, hr, bot, real, d.get('sport'), round(d.get('move', 0) * 100, 1), d.get('set'), round(d.get('pnl') or 0, 2), why))
for day in sorted(out):
    v = sorted(out[day])
    ok = [x for x in v if not x[8]]
    print('\n%s Nov: %d buys (real %d) | pass the rules of 21 Nov: %d (real %d) | result of all at their own stake: real $%+.2f' % (day, len(v), sum(1 for x in v if x[3]), len(ok), sum(1 for x in ok if x[3]), sum(x[7] for x in v if x[3])))
    print('  by sport all:', dict(collections.Counter(x[4] for x in v)), '| passing:', dict(collections.Counter(x[4] for x in ok)))
    print('  by hour (all / passing):', ' '.join('%s:%d/%d' % (h, sum(1 for x in v if x[1][:2] == h), sum(1 for x in ok if x[1][:2] == h)) for h in sorted(set(x[1][:2] for x in v))))
    for x in v:
        if x[8]: print('   #%d %s %s real=%d %s move %.1f set=%s pnl %+.2f | fails today: %s' % (x[0], x[1], x[2], x[3], x[4], x[5], x[6], x[7], ', '.join(x[8])))
