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
t0 = int(sys.argv[1]); a = id_at(t0); b = db.execute("SELECT MAX(id) FROM changes").fetchone()[0]
step = max(1, (b - a) // 500000)
books = sys.argv[2].split(',')
c = collections.Counter()
for ts, book in db.execute("SELECT ts_ms, book FROM changes WHERE id >= ? AND id %% ? = 0 AND book IN (%s)" % ','.join('?' * len(books)), (a, step, *books)):
    c[(time.strftime('%d %H', time.localtime(ts / 1000)), book)] += step
hrs = sorted(set(k[0] for k in c))
print('hour   ' + ' '.join('%9s' % x[:9] for x in books))
for h in hrs: print(h + '  ' + ' '.join('%9d' % c[(h, x)] for x in books))
