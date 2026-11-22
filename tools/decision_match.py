"""Same code, same data: does the replay make the SAME decisions as the live bot? (owner, 22 Nov 2026: "the entire point
is to use the recorded data in the same way the bots would have. if it's not exact, either the data is wrong, or the
config / sim is wrong")

Window: 21 Nov 16:58 (the build the replay blocks were made with was installed) - 21:13 (the bot was stopped), Norway.
Live: every move the bot looked at (bot_signals: the measured move, followed share, spread, families ... and the
result: entered / a skip reason). Replay: the v18 blocks' candidates (tools/sweep.py with SWEEP_DATA=v18; only moves of
7 pt+, followed 30% or less, spread 10c or less are kept there). Paired by game, side, direction and trigger bookmaker,
the live log time within 3.5 s before to 1.5 s after the replay's decision time (the log is stamped with the move's own time), the side bought.
Reported: how many replay candidates the live bot also looked at, how close the measured numbers are, and where the two
reach a different decision - each such case listed with both sets of numbers.
usage: SWEEP_DATA=v18 python3 decision_match.py      -> ~/swifteye/research/decision_match.txt
"""
import os, sys, json, sqlite3, bisect, collections, statistics as st, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

A, Z = 1795280280000, 1795291980000
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
live = collections.defaultdict(list)
n_live = 0
for ts, mid, o, res, data in db.execute("SELECT ts_ms, match_id, outcome, result, data FROM bot_signals WHERE ts_ms BETWEEN ? AND ?", (A - 60_000, Z + 60_000)):
    try: d = json.loads(data) if data else {}
    except ValueError: d = {}
    if 'trigger' not in d: continue
    n_live += 1
    oo = d.get('outcome', o); dd = int(d.get('dir') or 0)
    side = oo if dd > 0 else {'home': 'away', 'away': 'home'}.get(oo, '!' + str(oo))
    live[(mid, side, d['trigger'])].append((ts, res, d))
for k in live: live[k].sort(key=lambda x: x[0])
entered = [(ts, mid, o, res, data) for ts, mid, o, res, data in db.execute("SELECT ts_ms, match_id, outcome, result, data FROM bot_signals WHERE ts_ms BETWEEN ? AND ? AND result IN ('entered', 'paper_trial')", (A, Z))]
R = [c for c in S.ROWS if A <= c['tf'] <= Z]
num = lambda v: v[-1] if isinstance(v, list) else v
out = []; P = out.append
P('SAME CODE, SAME DATA: 21 Nov 16:58-21:13. Live decisions with measurements: %d. Replay candidates (7 pt+, followed <= 30%%, spread <= 10c): %d\n' % (n_live, len(R)))
pairs = []; miss = []
for c in R:
    side = c['outcome'] if c['dir'] > 0 else {'home': 'away', 'away': 'home'}.get(c['outcome'], '!')
    L = live.get((c['match_id'], side, c['trigger']), [])
    # the bot's log is stamped with the move's own time, about 1.9 s before the replay's decision time
    i = bisect.bisect_left([x[0] for x in L], c['tf'] - 3500)
    cand = [x for x in L[i:i + 6] if c['tf'] - 3500 <= x[0] <= c['tf'] + 1500]
    if cand: pairs.append((c, cand[0]))
    else: miss.append(c)
P('Replay candidates the live bot also measured: %d of %d (%d not found in the live log)' % (len(pairs), len(R), len(miss)))
if pairs:
    dm = [100 * (num(d['move']) - c['bm'][1]) for c, (ts, res, d) in pairs if d.get('move') is not None]
    df = [100 * (num(d['followed']) - c['fol'][1]) for c, (ts, res, d) in pairs if d.get('followed') is not None]
    ds = [100 * (num(d['spread']) - c['sp'][1]) for c, (ts, res, d) in pairs if d.get('spread') is not None]
    same = lambda v, tol: sum(1 for x in v if abs(x) <= tol)
    P('  move (pt), live - replay: median %+.2f | within 0.5 pt: %d of %d' % (st.median(dm), same(dm, 0.5), len(dm)))
    P('  followed (%% of the move), live - replay: median %+.1f | within 2 points: %d of %d' % (st.median(df), same(df, 2), len(df)))
    P('  spread (c), live - replay: median %+.2f | equal: %d of %d' % (st.median(ds), same(ds, 0.05), len(ds)))
    P('  live results of the paired moves: %s' % ', '.join('%s %d' % kv for kv in collections.Counter(res for c, (ts, res, d) in pairs).most_common()))
# decisions: what today's rules decide in the replay v what the live bot decided
B = S.BASE; CI = S.CATCH.index(1.0)
P('\nDECISIONS (today\'s rules):')
rp = {id(c) for c in R if S.gate(c, B, CI)}
both = sum(1 for c, (ts, res, d) in pairs if id(c) in rp and res in ('entered', 'paper_trial'))
P('  replay would buy: %d moves | live bot entered (incl. trial): %d | both: %d' % (len(rp), len(entered), both))
diff = [(c, x) for c, x in pairs if (id(c) in rp) != (x[1] in ('entered', 'paper_trial'))]
P('  paired moves where the two decide differently: %d' % len(diff))
for c, (ts, res, d) in diff[:25]:
    P('   %s %-10s %-5s trig=%-10s | replay: %s (move %.1f, followed %.0f%%, spread %.0fc, edge %.1fc, nofill %s, blind %s) | live: %s (move %.1f, followed %.0f%%, spread %.0fc)' % (
        time.strftime('%H:%M:%S', time.localtime(c['tf'] / 1000)), c['sport'], c['outcome'], c['trigger'], 'BUY' if id(c) in rp else 'skip', 100 * c['bm'][1], 100 * c['fol'][1], 100 * c['sp'][1], 100 * c['_emin'][CI], c['nofill'], c['blind'],
        res, 100 * (num(d.get('move')) or 0), 100 * (num(d.get('followed')) or 0), 100 * (num(d.get('spread')) or 0)))
# live entries the replay did not even have as a candidate
P('\nLIVE ENTRIES in the window and their replay counterpart:')
for ts, mid, o, res, data in entered:
    d = json.loads(data) if data else {}
    near = [c for c in R if c['match_id'] == mid and abs(c['tf'] - ts) <= 15_000]
    P('   %s match %d %s %s entry %.2f | replay: %s' % (time.strftime('%H:%M:%S', time.localtime(ts / 1000)), mid, o, res, d.get('entry') or 0,
        '; '.join('%s %s dir%+d trig %s %s' % (time.strftime('%H:%M:%S', time.localtime(c['tf'] / 1000)), c['outcome'], c['dir'], c['trigger'], 'passes today\'s rules' if id(c) in rp else 'fails') for c in near) or 'NO CANDIDATE within 15 s'))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/decision_match.txt'), 'w').write(txt + '\n')
