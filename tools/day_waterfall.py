import sqlite3, json, os, time, collections, sys
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
fee = lambda p: 0.05 * p * (1 - p)
ORDER = ['only one bookmaker prices the game', 'Polymarket had already moved', 'spread over 4c', 'price over 90c or under 2c', 'other bookmakers did not move', 'Polymarket did not lag in this game before', 'Polymarket already apart', 'room under 4c after fees']
def first_fail(d):
    bm, fol, sp, dec, cx = d['move'], d['followed'], d['spread'], d['dec'], d['cx']
    if min(d['books']) < 2: return ORDER[0]
    if max(fol) > 0.05 or d.get('followed_since_start', 0) > 0.05: return ORDER[1]
    if max(sp) > 0.04 + 1e-9: return ORDER[2]
    if not all(0.02 <= x <= 0.98 for x in dec) or max(dec) > 0.90: return ORDER[3]
    if cx.get('fam_moved', 0) < 2 or cx.get('cons_move', 0) < 0.03: return ORDER[4]
    if cx.get('jumps', 0) == 0 or cx.get('follow', 0) < 0.5: return ORDER[5]
    g = d.get('gap_before')
    if g is not None and g >= 0.10: return ORDER[6]
    e = min(bm[i] - sp[i] - 0.02 - fee(dec[i]) - fee(min(max(dec[i] + bm[i] - sp[i] - 0.02, 0), 1)) for i in (0, 1))
    return ORDER[7] if e < 0.04 else None
for name, t0, t1 in (('LAST 20 HOURS (21 Nov 01:12 - 21:12)', 1795219920000, 1795291920000), ('same 20 hours a day earlier', 1795133520000, 1795205520000)):
    woke = set(); mv = {}; pre = collections.Counter()
    for ts, mid, res, data in db.execute("SELECT ts_ms, match_id, result, data FROM bot_signals WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms", (t0, t1)):
        k = (mid, ts // 120000); woke.add(k)
        if res == 'entered': mv[k] = ('?', None, {'entered'}); continue
        d = json.loads(data)
        if 'move' not in d: pre[res] += 1; continue
        if d['move'][0] < 0.08: continue
        f = 'bookmaker reopened after a pause (gap)' if res == 'data_gap' else first_fail(d)
        cur = mv.get(k)
        if cur is None: mv[k] = (d.get('sport'), f, {res})
        elif cur[1] is not None and f is None: mv[k] = (d.get('sport') or cur[0], None, cur[2] | {res})
        elif (cur[1] is None) == (f is None): cur[2].add(res)
    print('\n== %s' % name)
    print('bookmaker moves that woke the bot (one per game per 2 min): %d' % len(woke))
    print('  of these measured as 8 points or more: %d' % len(mv))
    c = collections.Counter(v[1] for v in mv.values() if v[1])
    for r in ['bookmaker reopened after a pause (gap)'] + ORDER:
        if c[r]: print('    - %-46s %5d' % (r, c[r]))
    ok = [v for v in mv.values() if v[1] is None]
    B = {'entered', 'paper_trial', 'real_not_filled'}
    late = collections.Counter()
    for v in ok:
        if not (v[2] & B): late[', '.join(sorted(v[2] - {'measured_too_late'})) or 'measured_too_late'] += 1
    print('  pass every core rule: %d' % len(ok))
    for r, n in late.most_common(): print('    - refused by a last rule: %-30s %3d' % (r, n))
    print('    = approved: %d (order sent and filled, real or paper: %d; real order not filled: %d; paper-only trial: %d)' % (sum(1 for v in ok if v[2] & B), sum(1 for v in ok if 'entered' in v[2]), sum(1 for v in ok if 'real_not_filled' in v[2] and 'entered' not in v[2]), sum(1 for v in ok if 'paper_trial' in v[2] and 'entered' not in v[2])))
