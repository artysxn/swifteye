import sqlite3, json, os, time, collections
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
fee = lambda p: 0.05 * p * (1 - p)
def first_fail(d):
    bm, fol, sp, dec, cx = d['move'], d['followed'], d['spread'], d['dec'], d['cx']
    if min(d['books']) < 2: return 'one bookmaker only'
    if max(fol) > 0.05 or d.get('followed_since_start', 0) > 0.05: return 'Polymarket already moved'
    if max(sp) > 0.04 + 1e-9: return 'spread over 4c'
    if not all(0.02 <= x <= 0.98 for x in dec) or max(dec) > 0.90: return 'price over 90c or under 2c'
    if cx.get('fam_moved', 0) < 2 or cx.get('cons_move', 0) < 0.03: return 'other bookmakers did not move'
    if cx.get('jumps', 0) == 0 or cx.get('follow', 0) < 0.5: return 'game where Polymarket did not lag before'
    g = d.get('gap_before')
    if g is not None and g >= 0.10: return 'Polymarket already apart'
    e = min(bm[i] - sp[i] - 0.02 - fee(dec[i]) - fee(min(max(dec[i] + bm[i] - sp[i] - 0.02, 0), 1)) for i in (0, 1))
    if e < 0.04: return 'room under 4c'
    return None
NOW = time.time() * 1000
for day, t0 in (('20', 1795129200000), ('21', 1795215600000)):
    t1 = t0 + (NOW - 1795215600000)        # same clock window on both days
    games = collections.defaultdict(set); mv = {}
    for ts, mid, res, data in db.execute("SELECT ts_ms, match_id, result, data FROM bot_signals WHERE ts_ms >= ? AND ts_ms < ? AND result NOT IN ('entered') ORDER BY ts_ms", (t0, t1)):
        d = json.loads(data)
        if 'move' not in d: continue
        sp_ = d.get('sport') or '?'
        games[sp_].add(mid)
        if d['move'][0] < 0.08: continue
        if res == 'data_gap': continue
        k = (mid, ts // 120000)
        f = first_fail(d)
        cur = mv.get(k)
        if cur is None or (cur[1] is not None and f is None): mv[k] = (sp_, f, res)
    print('\n== %s Nov, 00:00 to the same clock time as now' % day)
    tot = collections.Counter(v[0] for v in mv.values()); ok = collections.Counter(v[0] for v in mv.values() if v[1] is None)
    for s in ('cs2', 'tennis', 'dota2', 'basketball', 'football', 'amfootball', 'valorant', 'lol'):
        if not tot[s]: continue
        why = collections.Counter(v[1] for v in mv.values() if v[0] == s and v[1])
        print('  %-10s games with a measured move %3d | moves of 8pt+ %4d | pass every core rule %2d | lost on: %s' % (s, len(games[s]), tot[s], ok[s], ', '.join('%s %d' % (a, b) for a, b in why.most_common(4))))
    print('  ALL        moves of 8pt+ %d | pass every core rule %d' % (sum(tot.values()), sum(ok.values())))
