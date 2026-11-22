# Re-apply the bot's rules of 21 Nov to every recorded signal that carries its measurements, so moves refused
# for a gap in the recording or a late measurement can be judged on the other rules. One move = one game per 2 minutes.
import sqlite3, json, os, sys, time, collections
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
D0 = {'19': 1795042800000, '20': 1795129200000, '21': 1795215600000}
fee = lambda p: 0.05 * p * (1 - p)
def core(d):
    try:
        bm, fol, sp, dec, cx = d['move'], d['followed'], d['spread'], d['dec'], d['cx']
    except KeyError: return None
    why = []
    if bm[0] < 0.08: why.append('move')
    if min(d['books']) < 2: why.append('books')
    if max(fol) > 0.05 or d.get('followed_since_start', 0) > 0.05: why.append('followed')
    if max(sp) > 0.04 + 1e-9: why.append('spread')
    if not all(0.02 <= x <= 0.98 for x in dec): why.append('range')
    if cx.get('fam_moved', 0) < 2: why.append('groups')
    if cx.get('cons_move', 0) < 0.03: why.append('consensus')
    if cx.get('jumps', 0) == 0 or cx.get('follow', 0) < 0.5: why.append('no_lag_history')
    g = d.get('gap_before')
    if g is not None and g >= 0.10: why.append('already_apart')
    if max(dec) > 0.90: why.append('price90')
    e = min(bm[i] - sp[i] - 0.02 - fee(dec[i]) - fee(min(max(dec[i] + bm[i] - sp[i] - 0.02, 0), 1)) for i in (0, 1))
    if e < 0.04: why.append('room')
    return why
BOUGHT = {'entered', 'paper_trial', 'real_not_filled'}
PLUMB = {'data_gap', 'measured_too_late'}
for day in sys.argv[1:]:
    t0 = D0[day]; t1 = t0 + 86400000
    moves = {}   # (match, 2-min bucket) -> best record
    chk = collections.Counter(); raw = collections.Counter()
    for ts, mid, res, data in db.execute("SELECT ts_ms, match_id, result, data FROM bot_signals WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms", (t0, t1)):
        raw[res] += 1
        if res == 'entered': 
            k = (mid, ts // 120000); moves.setdefault(k, dict(res=set(), ok=False, sport=None, ts=ts))['res'].add(res); moves[k]['ok'] = True; continue
        d = json.loads(data); w = core(d)
        if w is None: continue
        ok = not w
        chk[(res, ok)] += 1
        if not ok and res not in BOUGHT: continue
        k = (mid, ts // 120000)
        m = moves.setdefault(k, dict(res=set(), ok=False, sport=d.get('sport'), ts=ts))
        m['res'].add(res); m['ok'] = m['ok'] or ok; m['sport'] = m['sport'] or d.get('sport')
    n = len(moves)
    bought = [m for m in moves.values() if m['res'] & BOUGHT]
    rest = [m for m in moves.values() if not (m['res'] & BOUGHT)]
    plumb = [m for m in rest if m['res'] <= PLUMB]
    ruled = [m for m in rest if not (m['res'] <= PLUMB)]
    print('\n===== %s Nov: signals %d' % (day, sum(raw.values())))
    print('moves that pass the core rules of 21 Nov (one per game per 2 min): %d' % n)
    print('  bought (real, paper or unfilled real order): %d' % len(bought), dict(collections.Counter(m['sport'] for m in bought)))
    print('  refused ONLY for a gap in the recording / measured too late: %d' % len(plumb), dict(collections.Counter(m['sport'] for m in plumb)), dict(collections.Counter(tuple(sorted(m['res'])) for m in plumb)))
    print('  refused by a later rule: %d' % len(ruled), dict(collections.Counter(m['sport'] for m in ruled)))
    print('    by rule:', dict(collections.Counter(tuple(sorted(m['res'] - PLUMB)) for m in ruled).most_common(12)))
    hrs = collections.Counter(time.strftime('%H', time.localtime(m['ts'] / 1000)) for m in plumb)
    print('  gap/late losses by hour:', ' '.join('%s:%d' % (h, hrs[h]) for h in sorted(hrs)))
    print('  check of my copy of the rules (result, passes core) for results that should never pass:', {k: v for k, v in chk.items() if k[1] and k[0] in ('move_too_small', 'already_moved', 'spread_too_wide', 'moved_with_first_book', 'too_few_feeds', 'consensus_did_not_move', 'polymarket_does_not_lag', 'gap_too_small', 'price_too_high', 'too_few_books')})
