"""Polymarket games we track, 17 Nov 05:00 - 22 Nov 01:00: by Norway start hour and sport, how many have bookmakers linked
(owner, 22 Nov 2026: more trades outside 13-22 -> which games at those hours do we miss?). book_events only (small table)."""
import sqlite3, os, time, collections, json
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
LO, HI = 1794888000000, 1795305600000          # 17 Nov 05:00 - 22 Nov 01:00 Norway
pm = db.execute("SELECT book_event_id, match_id, sport, league, home, away, start_ms FROM book_events WHERE book='polymarket' AND start_ms BETWEEN ? AND ?", (LO, HI)).fetchall()
nb = collections.Counter()
for (m,) in db.execute("SELECT match_id FROM book_events WHERE book NOT IN ('polymarket','kalshi') AND start_ms BETWEEN ? AND ?", (LO - 86400000, HI + 86400000)):
    nb[m] += 1
hour = lambda ms: int(time.strftime('%H', time.localtime(ms / 1000)))
prime = lambda ms: 13 <= hour(ms) < 22
rows = [dict(id=e, m=m, sport=s, league=l or '', home=h, away=a, start=st, books=nb[m]) for e, m, s, l, h, a, st in pm]
json.dump(rows, open(os.path.expanduser('~/swifteye/research/pm_cover_ours.json'), 'w'))
out = ['POLYMARKET GAMES WE TRACKED, start 17 Nov 05:00 - 22 Nov 01:00 (%d games). linked = 2+ bookmakers linked (enough for a trade)' % len(rows)]
for lab, f in (('PRIME 13-22', lambda r: prime(r['start'])), ('OFF-PRIME', lambda r: not prime(r['start']))):
    R = [r for r in rows if f(r)]
    out.append('\n%s: %d games, %d with 2+ bookmakers, %d with none' % (lab, len(R), sum(r['books'] >= 2 for r in R), sum(r['books'] == 0 for r in R)))
    for sp, n in collections.Counter(r['sport'] for r in R).most_common():
        S = [r for r in R if r['sport'] == sp]
        out.append('  %-12s %4d games, linked %4d (%3.0f%%), none %4d' % (sp, n, sum(r['books'] >= 2 for r in S), 100.0 * sum(r['books'] >= 2 for r in S) / n, sum(r['books'] == 0 for r in S)))
out.append('\nOFF-PRIME, NOT LINKED (fewer than 2 bookmakers), top leagues:')
for (sp, lg), n in collections.Counter((r['sport'], r['league']) for r in rows if not prime(r['start']) and r['books'] < 2).most_common(30):
    out.append('  %-12s %-40s %4d' % (sp, lg[:40], n))
out.append('\nBY HOUR (Norway): games tracked / linked')
for h in range(24):
    R = [r for r in rows if hour(r['start']) == h]
    out.append('  %02d %s %4d / %4d' % (h, '*' if 13 <= h < 22 else ' ', len(R), sum(r['books'] >= 2 for r in R)))
txt = '\n'.join(out); print(txt); open(os.path.expanduser('~/swifteye/research/pm_cover.txt'), 'w').write(txt + '\n')
