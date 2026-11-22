# How old is each bookmaker's price when it reaches us (our receive time minus the bookmaker's own timestamp),
# per hour, read by row-id range (sampled). Also rows per hour per bookmaker = how often each feed delivers.
import sqlite3, os, sys, time, collections
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
def id_at(target):
    lo, hi = 1, db.execute("SELECT MAX(id) FROM changes").fetchone()[0]
    while lo < hi:
        m = (lo + hi) // 2
        r = db.execute("SELECT ts_ms FROM changes WHERE id >= ? ORDER BY id LIMIT 1", (m,)).fetchone()
        if r is None or r[0] >= target: hi = m
        else: lo = m + 1
    return lo
t0, t1 = int(sys.argv[1]), int(sys.argv[2])
a, b = id_at(t0), id_at(t1)
n = b - a; step = max(1, n // 400000)
print('rows', n, 'sample every', step)
lag = collections.defaultdict(list); cnt = collections.Counter(); hasts = collections.Counter()
for ts, book, bts, kind in db.execute("SELECT ts_ms, book, book_ts_ms, kind FROM changes WHERE id >= ? AND id < ? AND id % ? = 0", (a, b, step)):
    cnt[book] += 1
    if bts: hasts[book] += 1; lag[book].append(ts - bts)
for book, c in cnt.most_common(40):
    v = sorted(lag[book]); m = len(v)
    print('%-12s rows %7d | with own timestamp %5.0f%% | %s' % (book, c * step, 100.0 * hasts[book] / c, ('age when received: median %6d ms, p90 %7d ms, over 3 s %4.0f%%' % (v[m // 2], v[int(m * 0.9)], 100.0 * sum(1 for x in v if x > 3000) / m)) if m > 20 else '-'))
