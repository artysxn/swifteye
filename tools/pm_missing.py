"""Polymarket games 17 Nov 05:00 - 22 Nov 01:00 Norway that we do NOT track, by sport family and Norway start hour (prime 13-22 v
off-prime). Input: ~/swifteye/research/pm_all/pm_all_events.json (tools/pm_all_fetch.mjs) + book_events (polymarket ids).
'ours' = a Gamma tag our discovery follows (books/polymarket.rs SOCCER_TAG, ESPORTS_TAG, SPORT_TAGS)."""
import json, os, sqlite3, time, collections, calendar
R = os.path.expanduser('~/swifteye/research/')
E = json.load(open(R + 'pm_all/pm_all_events.json'))
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
ours_ids = {r[0] for r in db.execute("SELECT book_event_id FROM book_events WHERE book='polymarket'")}
links = collections.Counter()
for (eid,) in db.execute("SELECT b.book_event_id FROM book_events b JOIN book_events o ON o.match_id = b.match_id AND o.book NOT IN ('polymarket','kalshi') WHERE b.book='polymarket'"):
    links[eid] += 1
OURS = {'100350': 'football', '64': 'esports', '28': 'basketball', '864': 'tennis', '100088': 'hockey', '678': 'baseball', '102883': 'volleyball', '450': 'amfootball', '100351': 'amfootball', '279': 'mma'}
GENERIC = {'1', '100639'}
LO, HI = 1794888000000, 1795305600000
def ms(s): return calendar.timegm(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S')) * 1000
hour = lambda t: int(time.strftime('%H', time.localtime(t / 1000)))
fam_name = collections.Counter()
rows = []
for e in E:
    if not e.get('start'): continue
    t = ms(e['start'])
    if not LO <= t <= HI: continue
    ids = [x.split(':', 1) for x in e['tags']]
    fam = next((OURS[i] for i, _ in ids if i in OURS), None)
    if fam is None:
        rest = [s for i, s in ids if i not in GENERIC]
        fam = 'NOT FOLLOWED: ' + (rest[0] if rest else '?')
    child = ' - ' in e['title'] and fam != 'esports'
    rows.append(dict(e, t=t, fam=fam, child=child, tracked=e['id'] in ours_ids, linked=links[e['id']] >= 2, off=not 13 <= hour(t) < 22))
out = ['POLYMARKET GAME LISTINGS v WHAT WE TRACK, start 17 Nov 05:00 - 22 Nov 01:00 Norway (%d listings)' % len(rows)]
out.append('(child = "A vs B - More Markets" style listings that repeat a game; no winner market = nothing the bot can buy)\n')
main = [r for r in rows if not r['child'] and r['ml']]
out.append('Games with a winner market: %d (child listings %d, no winner market %d)' % (len(main), sum(r['child'] for r in rows), sum(not r['child'] and not r['ml'] for r in rows)))
for lab, f in (('OFF-PRIME (22-13)', lambda r: r['off']), ('PRIME (13-22)', lambda r: not r['off'])):
    G = [r for r in main if f(r)]
    out.append('\n%s: %d games listed, %d tracked, %d tracked + 2+ bookmakers' % (lab, len(G), sum(r['tracked'] for r in G), sum(r['tracked'] and r['linked'] for r in G)))
    out.append('  %-34s %6s %8s %8s %12s %12s' % ('sport', 'listed', 'tracked', 'linked', 'median vol $', 'untracked vol'))
    by = collections.defaultdict(list)
    for r in G: by[r['fam']].append(r)
    for fam, L in sorted(by.items(), key=lambda x: -len(x[1])):
        vols = sorted(r['vol'] for r in L); un = [r['vol'] for r in L if not r['tracked']]
        out.append('  %-34s %6d %8d %8d %12.0f %12.0f' % (fam[:34], len(L), sum(r['tracked'] for r in L), sum(r['tracked'] and r['linked'] for r in L), vols[len(vols) // 2], sorted(un)[len(un) // 2] if un else 0))
out.append('\nOFF-PRIME, FOLLOWED SPORTS BUT NOT TRACKED - top series:')
c = collections.Counter((r['fam'], r['series']) for r in main if r['off'] and not r['tracked'] and not r['fam'].startswith('NOT'))
for (fam, s), n in c.most_common(25):
    v = sorted(r['vol'] for r in main if r['off'] and not r['tracked'] and r['fam'] == fam and r['series'] == s)
    out.append('  %-12s %-34s %4d games, median volume $%.0f' % (fam, (s or '')[:34], n, v[len(v) // 2]))
out.append('\nOFF-PRIME, NOT-FOLLOWED SPORTS - top series:')
c = collections.Counter((r['fam'], r['series']) for r in main if r['off'] and r['fam'].startswith('NOT'))
for (fam, s), n in c.most_common(25):
    v = sorted(r['vol'] for r in main if r['off'] and r['fam'] == fam and r['series'] == s)
    out.append('  %-30s %-28s %4d games, median volume $%.0f' % (fam[14:44], (s or '')[:28], n, v[len(v) // 2]))
out.append('\nBY HOUR (Norway): games listed / tracked / not-followed sports / untracked in followed sports')
for h in range(24):
    G = [r for r in main if hour(r['t']) == h]
    out.append('  %02d %s %5d %5d %5d %5d' % (h, '*' if 13 <= h < 22 else ' ', len(G), sum(r['tracked'] for r in G), sum(r['fam'].startswith('NOT') for r in G), sum(not r['tracked'] and not r['fam'].startswith('NOT') for r in G)))
json.dump(rows, open(R + 'pm_all/pm_missing_rows.json', 'w'))
txt = '\n'.join(out); print(txt); open(R + 'pm_missing.txt', 'w').write(txt + '\n')
