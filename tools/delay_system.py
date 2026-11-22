"""A delay detector: in each game, does Polymarket move AFTER the bookmakers, and by how many seconds?
(owner's idea, 21 Nov 2026: 'detect if there is a delay in the correlation'.)

For every trade (backtest trades of 17-19 Nov from tools/sweep.py, and the live real trades since 20 Nov) it reads
that game's 31 minutes BEFORE the bookmaker move (changes and pm_quotes by match_id + time: indexed, throttled) and
measures every earlier jump of the bookmakers' middle price:
  bookmakers' middle = median over feed families of the family's mean 1/odds, on a 1-second grid
  a jump             = the middle moves 3+ points within 10 s
  Polymarket's delay = seconds from the middle making half of the jump to Polymarket's mid (its own clock) making
                       half of it, looked for from 40 s before to 60 s after. Negative = Polymarket moved first.
Only jumps whose 60 s had fully passed before the trade count, so nothing here uses the future.
Per trade: how many earlier jumps, the share where Polymarket came 2+ s AFTER, the share where it came 2+ s BEFORE,
the share with no answer, and the median delay. Compared with the bot's own cruder score (follow - lead).
usage: delay_system.py        -> ~/swifteye/research/delay_system.json
"""
import sys, os, sqlite3, bisect, json, time, statistics as st
from collections import defaultdict
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.argv = sys.argv[:1]
import sweep as S

db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
WIN, JUMP = 31 * 60, 0.03
_cache = {}


def history(m, o, t0):
    """(grid start s, bookmakers' middle per second, Polymarket mid per second) for the 31 min before t0 (ms)."""
    a = t0 - WIN * 1000
    rows = db.execute("SELECT ts_ms, book, kind, new_price FROM changes WHERE match_id=? AND market='1x2' AND outcome=? AND ts_ms>=? AND ts_ms<=? AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms", (m, o, a - 1_200_000, t0)).fetchall()
    q = sorted(db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms>=? AND ts_ms<=? AND bid IS NOT NULL AND ask IS NOT NULL", (m, o, a - 120_000, t0 + 5_000)).fetchall())
    time.sleep(0.05 + (len(rows) + len(q)) / 25000)
    state, pts = {}, []
    for ts, b, kind, p in rows:
        if kind in ('close', 'suspend'): state.pop(b, None)
        elif p and p > 1.0: state[b] = 1 / p
        else: continue
        fam = defaultdict(list)
        for bk, v in state.items(): fam[S.platform(bk)].append(v)
        if len(fam) >= 2:
            ms = sorted(sum(x) / len(x) for x in fam.values()); pts.append((ts, ms[len(ms) // 2]))
        else: pts.append((ts, None))
    ct = [x[0] for x in pts]; qt = [x[0] for x in q]
    cons, pm = [], []
    for s in range(WIN + 1):
        t = a + s * 1000
        i = bisect.bisect_right(ct, t); cons.append(pts[i - 1][1] if i else None)
        j = bisect.bisect_right(qt, t)
        pm.append((q[j - 1][1] + q[j - 1][2]) / 2 if j and t - q[j - 1][0] <= 120_000 and q[j - 1][2] - q[j - 1][1] <= 0.06 else None)
    return cons, pm


def delays(cons, pm):
    """Polymarket's delay (s) behind each earlier jump of the bookmakers' middle; None = it never made half of it."""
    out, last = [], -99
    for i in range(52, len(cons) - 68):          # room for 40 s before and 60 s after, all before the trade
        if i - last < 30 or cons[i] is None or cons[i - 10] is None: continue
        if abs(cons[i] - cons[i - 10]) < JUMP: continue
        b, e = cons[i - 12], cons[min(i + 8, len(cons) - 1)]
        if b is None or e is None or abs(e - b) < JUMP: continue
        sg = 1 if e > b else -1; m = abs(e - b)
        tc = next((j for j in range(i - 12, i + 9) if cons[j] is not None and sg * (cons[j] - b) >= 0.5 * m), None)
        if tc is None or tc < 41 or tc + 60 >= len(cons): continue
        base = pm[tc - 40]
        if base is None: continue
        last = i
        tp = next((j for j in range(tc - 39, tc + 61) if pm[j] is not None and sg * (pm[j] - base) >= 0.5 * m), None)
        out.append(None if tp is None else tp - tc)
    return out


def feats(m, o, t0):
    k = (m, o, t0 // 60_000)
    if k not in _cache:
        d = delays(*history(m, o, t0))
        got = [x for x in d if x is not None]
        _cache[k] = dict(n=len(d), after=sum(x >= 2 for x in got) / len(d) if d else None, before=sum(x <= -2 for x in got) / len(d) if d else None,
                         none=(len(d) - len(got)) / len(d) if d else None, med=st.median(got) if got else None)
    return _cache[k]


def line(name, X):
    n = len(X)
    if not n: return f"  {name:60s} {0:4d}"
    w = sum(t['pnl'] > 0.005 for t in X); p = sum(t['pnl'] for t in X); caught = sum(bool(t['caught']) for t in X)
    return f"  {name:60s} {n:4d} {w * 100 // n:4d}% {p:+9.2f} {p / n:+6.2f} {caught * 100 // n:5d}%"


def report(title, T):
    print(f"\n== {title}"); print(f"  {'':60s} {'n':>4s} {'won':>5s} {'profit':>9s} {'each':>6s} {'caught':>6s}")
    print(line('all', T)); print(line('no earlier bookmaker jump in the 30 min before', [t for t in T if t['d']['n'] == 0]))
    K = [t for t in T if t['d']['n'] > 0]
    print('  -- on how many of the earlier jumps did Polymarket come 2+ s AFTER the bookmakers')
    for nm, lo, hi in (('none of them', -1, 1e-9), ('some, under half', 1e-9, 0.5), ('half or more', 0.5, 9)): print(line(f"   {nm}", [t for t in K if lo <= t['d']['after'] < hi]))
    print('  -- on how many did Polymarket move 2+ s BEFORE the bookmakers')
    for nm, lo, hi in (('none of them', -1, 1e-9), ('some, under half', 1e-9, 0.5), ('half or more', 0.5, 9)): print(line(f"   {nm}", [t for t in K if lo <= t['d']['before'] < hi]))
    print('  -- on how many did Polymarket not answer at all within 60 s')
    for nm, lo, hi in (('under half', -1, 0.5), ('half or more', 0.5, 9)): print(line(f"   {nm}", [t for t in K if lo <= t['d']['none'] < hi]))
    print('  -- median delay of Polymarket behind the bookmakers on the earlier jumps')
    M = [t for t in K if t['d']['med'] is not None]
    for nm, lo, hi in (('Polymarket first by 2+ s', -99, -1.5), ('together (within 2 s)', -1.5, 2), ('Polymarket 2-10 s behind', 2, 10), ('Polymarket over 10 s behind', 10, 99)): print(line(f"   {nm}", [t for t in M if lo <= t['d']['med'] < hi]))
    print('  -- score: share AFTER minus share BEFORE')
    for nm, lo, hi in (('under 0', -9, -1e-9), ('0 to 0.3', -1e-9, 0.3), ('0.3 or more', 0.3, 9)): print(line(f"   {nm}", [t for t in K if lo <= t['d']['after'] - t['d']['before'] < hi]))
    print("  -- the bot's own score for comparison: follow - lead")
    for nm, lo, hi in (('under 0.2', -9, 0.2), ('0.2 or more', 0.2, 9)): print(line(f"   {nm}", [t for t in T if t['cx']['jumps'] > 0 and lo <= t['cx']['follow'] - t['cx']['lead'] < hi]))


t_start = time.time(); out = {}
LOOSE = dict(S.BASE, legacy=True, min_move=0.07, cons=0.0, fams=1, lag=None, gap=None, max_followed=0.15, min_edge=0.02, max_spread=0.06, cap=0.95, one_per_game=False)
sets = []
for title, p in (("THE BOT'S RULES, backtest 17-19 Nov (as it saw the feed)", dict(S.BASE, legacy=True)), ("LOOSER RULES, backtest 17-19 Nov", LOOSE)):
    T = [dict(pnl=t['pnl'], caught=t['caught'], cx=t['c']['cx'], m=t['c']['match_id'], o=t['c']['outcome'], t0=t['c']['t0'], late=t['c']['late_ms']) for t in S.sim(p)]
    sets.append((title, T))
live = []
for i, data in db.execute("SELECT id, data FROM bot_trades WHERE real = 1 AND ts_ms >= 1795129200000 AND closed_ms IS NOT NULL ORDER BY ts_ms"):
    v = json.loads(data); live.append(dict(pnl=v['pnl'], caught=v.get('reason') == 'caught up', cx=v['cx'], m=v['match_id'], o=v['outcome'], t0=v['move_ms'], late=0, id=i))
sets.append(("LIVE real trades since 20 Nov (feed on time)", live))
for title, T in sets:
    for t in T: t['d'] = feats(t['m'], t['o'], t['t0'])
    report(title, T)
    out[title] = [dict(t0=t['t0'], m=t['m'], pnl=round(t['pnl'], 3), caught=bool(t['caught']), late=t['late'], d=t['d'], follow=t['cx']['follow'], lead=t['cx']['lead'], id=t.get('id')) for t in T]
print('\nlive trades one by one:')
for t in live: print(f"  #{t['id']} jumps {t['d']['n']:2d} after {t['d']['after'] if t['d']['after'] is None else round(t['d']['after'], 2)} before {t['d']['before'] if t['d']['before'] is None else round(t['d']['before'], 2)} none {t['d']['none'] if t['d']['none'] is None else round(t['d']['none'], 2)} median {t['d']['med']}  bot follow-lead {t['cx']['follow'] - t['cx']['lead']:+.2f}  pnl {t['pnl']:+.2f}")
json.dump(out, open(os.path.expanduser('~/swifteye/research/delay_system.json'), 'w'))
print(f"\n{len(_cache)} game windows read in {time.time() - t_start:.0f} s")
