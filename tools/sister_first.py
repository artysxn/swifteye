"""First read of the sister-market recording (pm_sister_quotes, since 21 Nov 02:32): does a game's MAP-winner market
on Polymarket lag its MATCH-winner market, or lead it? Polymarket's own clock for both; no bookmaker needed.
A jump = the mid moves 3+ points within 10 s. For every jump in one market: how much of it the other market had
already made, and how long its old price stayed buyable (ask within 1c of the ask before the jump).
usage: sister_first.py"""
import sqlite3, os, bisect, statistics as st, datetime as dt
from collections import defaultdict, Counter
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
NO = dt.timezone(dt.timedelta(hours=1))
kinds = db.execute("SELECT kind, count(*), count(DISTINCT match_id), count(DISTINCT token), min(ts_ms), max(ts_ms) FROM pm_sister_quotes GROUP BY kind").fetchall()
for k in kinds: print(f"  {k[0]:16s} {k[1]:7d} rows, {k[2]:3d} games, {k[3]:3d} markets, {dt.datetime.fromtimestamp(k[4] / 1000, NO):%H:%M} to {dt.datetime.fromtimestamp(k[5] / 1000, NO):%H:%M}")
games = db.execute("SELECT s.match_id, m.sport, m.home, m.away, count(*), count(DISTINCT s.token) FROM pm_sister_quotes s JOIN matches m ON m.id = s.match_id WHERE s.kind = 'child_moneyline' GROUP BY 1 ORDER BY 5 DESC").fetchall()
print(f"games with a map-winner market: {len(games)}: " + ', '.join(f"{g[1]} {g[2][:14]} v {g[3][:14]} ({g[4]} rows)" for g in games[:8]))


def series(rows):
    """[(t, bid, ask)] sorted, mids"""
    rows = [(t, b, a) for t, b, a in rows if b is not None and a is not None and 0 < b < a < 1 and a - b <= 0.15]
    return rows


def at(s, ts, t):
    i = bisect.bisect_right(ts, t); return s[i - 1] if i else None


def jumps(s, thr=0.03, win=10_000):
    ts = [x[0] for x in s]; out = []; last = -10 ** 15
    for i, (t, b, a) in enumerate(s):
        if t - last < 60_000: continue
        p = at(s, ts, t - win)
        if not p or t - p[0] > 120_000: continue
        d = (b + a) / 2 - (p[1] + p[2]) / 2
        if abs(d) >= thr: out.append((t, 1 if d > 0 else -1, abs(d), p)); last = t
    return out


res = defaultdict(list)
for m, sport, home, away, n, ntok in games:
    lo, hi = db.execute("SELECT min(ts_ms), max(ts_ms) FROM pm_sister_quotes WHERE match_id=? AND kind='child_moneyline'", (m,)).fetchone()
    main = series(db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=? AND outcome='home' ORDER BY 1", (m, lo - 60_000, hi + 60_000)).fetchall())
    if len(main) < 50: continue
    mt = [x[0] for x in main]
    for (tok, line, oname) in db.execute("SELECT DISTINCT token, line, outcome FROM pm_sister_quotes WHERE match_id=? AND kind='child_moneyline'", (m,)).fetchall():
        # the table names the side by the team: the same side as the match market's 'home', or the other one (then the price is turned round)
        nm = (oname or '').lower(); same_side = nm[:6] == home.lower()[:6]; other = nm[:6] == away.lower()[:6]
        if not (same_side or other): continue
        raw = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_sister_quotes WHERE match_id=? AND token=? ORDER BY 1", (m, tok)).fetchall()
        sub = series(raw if same_side else [(t, None if a is None else 1 - a, None if b is None else 1 - b) for t, b, a in raw])
        if len(sub) < 50: continue
        sts = [x[0] for x in sub]
        for lead_name, A, At, B, Bt in (('match market jumps -> map market', main, mt, sub, sts), ('map market jumps -> match market', sub, sts, main, mt)):
            for t, d, size, pre in jumps(A):
                b0 = at(B, Bt, t - 10_000); b1 = at(B, Bt, t)
                if not b0 or not b1 or t - b1[0] > 120_000: continue
                mid = lambda x: (x[1] + x[2]) / 2
                already = (mid(b1) - mid(b0)) * d
                later = {}
                for T in (2, 5, 10, 30, 60):
                    x = at(B, Bt, t + T * 1000); later[T] = (mid(x) - mid(b0)) * d if x else None
                # the other market's price on OUR side (the side that just rose): how long the old ask stays within 1c
                ask0 = b0[2] if d > 0 else 1 - b0[1]
                stay = None
                for T in (0.5, 1, 1.5, 2, 3, 5, 10):
                    x = at(B, Bt, t + int(T * 1000)); a = (x[2] if d > 0 else 1 - x[1]) if x else None
                    if a is not None and a <= ask0 + 0.01: stay = T
                    else: break
                res[lead_name].append(dict(sport=sport, size=size, already=already, later=later, stay=stay, game=f"{home} v {away}", line=line, spread=b1[2] - b1[1]))
for name, R in res.items():
    if not R: continue
    print(f"\n{name}: {len(R)} jumps in {len({r['game'] for r in R})} games ({dict(Counter(r['sport'] for r in R))}); jump size middle {100 * st.median(r['size'] for r in R):.1f} pt; the other market's spread middle {100 * st.median(r['spread'] for r in R):.1f}c")
    moved = [r for r in R if r['later'].get(60) is not None and r['later'][60] >= 0.02]
    print(f"   the other market moved 2+ pt the same way within 60 s in {len(moved)} of {len(R)}")
    if not moved: continue
    print(f"   of those {len(moved)}: it had ALREADY moved 2+ pt when the jump was seen in {sum(r['already'] >= 0.02 for r in moved)}; share of its 60-s move done at the jump (middle) {100 * st.median(max(min(r['already'] / r['later'][60], 1.5), -0.5) for r in moved):.0f}%")
    for T in (2, 5, 10, 30):
        x = [r['later'][T] / r['later'][60] for r in moved if r['later'].get(T) is not None]
        print(f"      {T:2d} s after the jump it had made {100 * st.median(x):3.0f}% of that move (middle)")
    late = [r for r in moved if r['already'] < 0.01]
    print(f"   it had NOT moved yet (under 1 pt) at the jump in {len(late)}; its old price was still buyable 1.5 s later in {sum(1 for r in late if r['stay'] is not None and r['stay'] >= 1.5)}, 3 s later in {sum(1 for r in late if r['stay'] is not None and r['stay'] >= 3)}")
