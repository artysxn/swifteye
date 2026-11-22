"""Build the dataset for sweep.py, once: every recorded bookmaker move a loose rule set could take
(the in-app replay's saved measurements, 17 Nov 10:49 - 19 Nov 19:35 UTC), plus what Polymarket REALLY showed
around it by its own clock (server_ts_ms), so no setting can be credited with a trade that only existed because
our price feed was running late.

Reads pm_quotes in merged windows, throttled (the live bot shares the database). About 4 minutes.
usage: sweep_build.py            -> ~/swifteye/research/sweep_v16.pickle
"""
import json, glob, os, sqlite3, time, bisect, pickle
from collections import defaultdict

OUT = os.path.expanduser('~/swifteye/research/sweep_v16.pickle')
DELAYS = [1000, 1500, 2000, 2500, 3000, 4000, 5000, 7000, 10000, 15000, 30000]   # ms after the bookmaker move
LOOSE = dict(fol=0.30, sp=0.10)
ex = lambda x: (x['ts'], x['px'], (x['sell'] or [None, None])[0], (x['sell'] or [None, None])[1], bool(x.get('caught'))) if x else None

rows = []
for f in sorted(glob.glob(os.path.expanduser('~/swifteye/adaptive_cache/v16-*.json'))):
    d = json.load(open(f))
    if not (isinstance(d, list) and d): continue
    for c in d[0]:
        if max(c['fol']) > LOOSE['fol'] or c['fol_start'] > LOOSE['fol'] or max(c['sp']) > LOOSE['sp'] + 1e-9: continue
        r = {k: c[k] for k in ('match_id', 'sport', 'home', 'away', 'outcome', 'dir', 'trigger', 'tf', 't0', 'books', 'bm', 'fol', 'fol_start', 'late_ms', 'sp', 'edge', 'px',
                               'buy', 'pause', 'cx', 'pm_tr', 'b_tr5', 'b_tr10', 'b_rng', 'nofill', 'dec', 'pre_mid')}
        r['blind'] = c.get('blind')          # True = the buy sits in a gap of the Polymarket recording (adaptive::Seen); None = measured before that existed
        r['exits'] = [[ex(x) for x in g] for g in c['exits']]
        r['stops'] = [[[ex(x) for x in g] for g in s] for s in c['stop_exits']]
        rows.append(r)
    del d
rows.sort(key=lambda c: c['tf'])
print(len(rows), 'moves kept')

db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
meta = {r[0]: r[1:] for r in db.execute("SELECT id, league, start_ms FROM matches")}
lg = defaultdict(set)
for mid, l in db.execute("SELECT match_id, league FROM book_events WHERE league IS NOT NULL"): lg[mid].add(l)
for c in rows:
    m = meta.get(c['match_id'], (None, None))
    c['start_ms'] = m[1]
    c['leagues'] = ' | '.join(sorted(lg.get(c['match_id'], set()) | ({m[0]} if m[0] else set())))

# One read per (game, outcome, stretch of time): windows of moves close together are merged.
by = defaultdict(list)
for c in rows:
    if not c['nofill']: by[(c['match_id'], c['outcome'])].append(c)
t_start = time.time(); nq = nrows = 0
for (m, o), cs in by.items():
    cs.sort(key=lambda c: c['t0'])
    groups, cur = [], None
    for c in cs:
        a, z = c['t0'] - 60_000, max(c['tf'], c['t0'] + 30_000) + 120_000
        if cur and a <= cur[1]: cur[1] = max(cur[1], z); cur[2].append(c)
        else: cur = [a, z, [c]]; groups.append(cur)
    for a, z, g in groups:
        q = sorted(db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms>=? AND ts_ms<=? AND bid IS NOT NULL AND ask IS NOT NULL", (m, o, a, z)).fetchall())
        nq += 1; nrows += len(q)
        time.sleep(0.03 + len(q) / 25000)
        T = [r[0] for r in q]
        at = lambda x: (lambda i: (q[i][1], q[i][2]) if i >= 0 else None)(bisect.bisect_right(T, x) - 1)
        for c in g:
            c['q_tf'] = at(c['tf']); c['q_15'] = at(c['t0'] + 1500)
            c['q_d'] = [at(c['t0'] + d) for d in DELAYS]
for c in rows:
    c.setdefault('q_tf', None); c.setdefault('q_15', None); c.setdefault('q_d', [None] * len(DELAYS))
os.makedirs(os.path.dirname(OUT), exist_ok=True)
pickle.dump(dict(rows=rows, delays=DELAYS, loose=LOOSE, built=time.time()), open(OUT, 'wb'), protocol=4)
print(f"{nq} reads, {nrows} quote rows, {time.time() - t_start:.0f} s; saved {OUT} ({os.path.getsize(OUT) / 1e6:.0f} MB)")
