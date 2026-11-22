"""Games with a Polymarket book worth studying: every match (some listed with a start before 17 Nov have quotes after it), with how many Polymarket quotes it has
(counted through the match_id index, capped at 1,500 - lf_build.py takes games with 1,500+). -> ~/swifteye/research/pm_counts.txt
usage: pm_counts.py"""
import os, sqlite3, time
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
now = int(time.time() * 1000)
ids = [r[0] for r in db.execute("SELECT id FROM matches")]
out = []; t0 = time.time()
for n, m in enumerate(ids):
    c = db.execute("SELECT COUNT(*) FROM (SELECT 1 FROM pm_quotes WHERE match_id=? LIMIT 1500)", (m,)).fetchone()[0]
    if c: out.append('%d|%d' % (m, c))
open(os.path.expanduser('~/swifteye/research/pm_counts.txt'), 'w').write('\n'.join(out) + '\n')
print('%d matches checked, %d with Polymarket quotes, %d with 1,500+, %.0f s' % (len(ids), len(out), sum(1 for l in out if int(l.split('|')[1]) >= 1500), time.time() - t0))
