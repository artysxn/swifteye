"""Basketball and football under the snipe rules: what the trades made, split by league, and whether any
sport-specific rule set pays. Uses the replay's saved measurements (17 Nov 10:49 - 19 Nov 19:35 UTC) and keeps only
trades really available by Polymarket's own clock (offpeak_rules.truly). $12 a trade.
"""
import sys, datetime as dt, statistics as st
from collections import Counter, defaultdict
import replay_eval as R
import offpeak_rules as O

C, db = O.C, O.db
U = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC)
fee = lambda p: 0.05 * p * (1 - p)
M = {r[0]: r[1:] for r in db.execute("SELECT id, home, away, league, start_ms FROM matches")}
LG = defaultdict(set)
for mid, lg in db.execute("SELECT match_id, league FROM book_events WHERE league IS NOT NULL"): LG[mid].add(lg)


def league(c):
    m = M.get(c['match_id'], ('', '', '', None))
    s = ' | '.join(sorted(LG.get(c['match_id'], set()) | {m[2] or ''})).lower() + ' ' + (m[0] + ' ' + m[1]).lower()
    if c['sport'] == 'basketball':
        if 'nba' in s.replace('nbb', '') and 'wnba' not in s: return 'NBA'
        if 'ncaa' in s or 'college' in s or '(stud)' in s or 'cbb' in s: return 'US college'
        return 'other leagues'
    return (m[2] or '-')[:26]


def run(p, rows, catch=1.0, give=300, stake=12.0):
    ci, gi = R.CATCH.index(catch), R.GIVE.index(give)
    out, recent, busy = [], {}, {}
    for c in rows:
        if O.passes(c, p): continue
        m = c['match_id']
        if m in recent and c['tf'] - recent[m] < 120_000: continue
        recent[m] = c['tf']
        if c['nofill']: continue
        x = c['exits'][ci][gi]
        if not x: continue
        if busy.get(m, 0) > c['tf']: continue
        busy[m] = x['ts']
        f = R.fill(c, x, stake, c['edge'][1][O.ci] - R.CAP >= p['min_edge'] - 1e-9)
        if not f: continue
        ok, px = O.truly(c, f['entry'], p['max_followed'])
        if not ok: continue
        n = f['spent'] / f['entry']; ex = f['exit']
        pnl = n * (ex - px) - n * (fee(px) + (fee(ex) if 0 < ex < 1 else 0))
        out.append(dict(c=c, pnl=pnl, spent=n * px, entry=px, exit=ex, caught=x.get('caught'), held=(x['ts'] - c['tf']) / 1000))
    return out


def line(name, T):
    if not T: return f"  {name:50s} trades   0"
    tot = sum(t['pnl'] for t in T); g = len({t['c']['match_id'] for t in T})
    lo, hi = O.boot(T) if g >= 3 else (float('nan'), float('nan'))
    return (f"  {name:50s} trades {len(T):3d} in {g:3d} games | won {sum(t['pnl'] > 0 for t in T):3d} ({sum(t['pnl'] > 0 for t in T) * 100 // len(T):3d}%) | {tot:+8.2f} "
            f"(90% range {lo:+6.0f} .. {hi:+6.0f}) | {tot / sum(t['spent'] for t in T) * 100:+5.1f}% a $ | caught up {sum(bool(t['caught']) for t in T):2d} | worst {min(t['pnl'] for t in T):+6.2f}")


def show(T, k=30):
    for t in sorted(T, key=lambda t: t['c']['tf'])[:k]:
        c = t['c']; m = M.get(c['match_id'], ('?', '?', '', None))
        mins = f"{(c['tf'] - m[3]) / 60000:4.0f}'" if m[3] else '   ?'
        print(f"     {U(c['tf']):%d %b %H:%M} {league(c)[:14]:14s} {m[0][:17]:17s} v {m[1][:17]:17s} {c['outcome']:5s} {mins} into the game | move {c['bm'][0] * 100:4.1f} pt, {c['cx']['fam_moved']} groups | "
              f"{t['entry']:.2f} -> {t['exit']:.2f} in {t['held']:4.0f}s {'caught up' if t['caught'] else 'gave up  '} | {t['pnl']:+6.2f}")


B = dict(O.BASE, basketball=True)
bb = [c for c in C if c['sport'] == 'basketball']
fb = [c for c in C if c['sport'] == 'football']

print("A. BASKETBALL: today's rules with basketball let in")
T = run(B, bb); print(line('all basketball', T))
for g in ('NBA', 'US college', 'other leagues'):
    rs = [c for c in bb if league(c) == g]
    print(line(f"{g} ({len({c['match_id'] for c in rs})} games with a 7+ pt move)", run(B, rs)))
print("   the trades:"); show(T)

print("\nB. BASKETBALL: one thing changed at a time")
for name, kw, rk in [
    ('sell at 50% caught up', {}, dict(catch=0.5)), ('sell at 75% caught up', {}, dict(catch=0.75)),
    ('give up after 60 s', {}, dict(give=60)), ('give up after 120 s', {}, dict(give=120)), ('give up after 15 min', {}, dict(give=900)),
    ('move 12+ points', dict(min_move=0.12), {}), ('move 15+ points', dict(min_move=0.15), {}),
    ('3+ bookmaker groups moved', dict(fams=3), {}), ('consensus moved 5+ pt', dict(cons=0.05), {}),
    ('room 6c after costs', dict(min_edge=0.06), {}), ('spread 2c at most', dict(max_spread=0.02), {}),
    ('no "lagged earlier" rule', dict(lag=False), {}),
]:
    print(line(name, run(dict(B, **kw), bb, **rk)))
mid = lambda c: 0.25 <= c['px'] <= 0.75
print(line('price 25-75c only (close games)', run(B, [c for c in bb if mid(c)])))
print(line('price outside 25-75c (one-sided games)', run(B, [c for c in bb if not mid(c)])))
late = lambda c: (M.get(c['match_id'], (0, 0, 0, None))[3] or 0) and c['tf'] - M[c['match_id']][3] > 90 * 60000
print(line('more than 90 min after the start (closing minutes)', run(B, [c for c in bb if late(c)])))
print(line('first 90 min after the start', run(B, [c for c in bb if not late(c)])))
print(line('peak hours 12-21 UTC (European leagues)', run(B, [c for c in bb if O.PEAK(c)])))
print(line('outside 12-21 UTC (US evening)', run(B, [c for c in bb if not O.PEAK(c)])))

print("\nC. FOOTBALL: today's rules pass nothing. Each rule's pass rate on its own was: 10+ pt 87%, 2+ groups 38%, not followed 40%, lagged earlier 8%")
F0 = dict(O.BASE)
for name, kw in [
    ('today\'s rules', {}),
    ('no "lagged earlier" rule', dict(lag=False)),
    ('1 bookmaker group is enough', dict(fams=1, cons=0.0)),
    ('1 group + no "lagged earlier"', dict(fams=1, cons=0.0, lag=False)),
    ('1 group + no lag rule + no gap rule', dict(fams=1, cons=0.0, lag=False, gap=None)),
    ('  ... and price cap 95c', dict(fams=1, cons=0.0, lag=False, gap=None, cap=0.95)),
    ('  ... and right after a pause too', dict(fams=1, cons=0.0, lag=False, gap=None, cap=0.95, pause=False)),
    ('2 groups, no lag rule, no gap rule', dict(lag=False, gap=None)),
]:
    T = run(dict(F0, **kw), fb); print(line(name, T))
    if name.startswith('1 group + no lag rule'): print("   the trades:"); show(T, 24)
print("   by give-up time, loosest football set above (1 group, no lag, no gap, cap 95c):")
L = dict(F0, fams=1, cons=0.0, lag=False, gap=None, cap=0.95)
for g in (60, 120, 300, 900, 1800): print(line(f"    give up after {g} s", run(L, fb, give=g)))
for o in ('home', 'draw', 'away'): print(line(f"    outcome {o}", run(L, [c for c in fb if c['outcome'] == o])))
print(line('    goals (move 15+ pt)', run(dict(L, min_move=0.15), fb)))

print("\nD. DOTA 2 under today's rules (2 of 8 won)")
d2 = [c for c in C if c['sport'] == 'dota2']; T = run(O.BASE, d2); print(line('dota2', T)); show(T)
