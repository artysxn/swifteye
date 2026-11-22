"""Positive-EV test, step 1: price snapshots with results. Read-only. usage: ev_snapshots.py [OUT.json]  (about 40 s)
For every settled Polymarket game: at fixed times around the start, Polymarket's bid/ask per outcome,
each bookmaker's odds with the margin removed, and who won. One row per (game, time, outcome)."""
import sqlite3, json, time, statistics as st, sys
DB = '/Users/danielvinichenko/swifteye/oddsfeed.db'
c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
H, M = 3600_000, 60_000
def fam(b):
    if b in ('linebet', 'melbet', 'betwinner', 'megapari', 'paripesa', '22bet', '22bit', '1xbit'): return '1xbet'
    if b in ('unibet', 'leovegas', 'paf'): return 'kambi'
    if b in ('fonbet', 'pari', 'mostbet'): return 'fonbet'
    if b in ('vinn', 'betking'): return 'altenar'
    return b
PRE = [-6 * H, -3 * H, -60 * M, -10 * M]
LIVE = [20 * M, 60 * M, 100 * M]
def pmq(mid, o, t):
    r = c.execute("SELECT bid, ask, ts_ms FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1", (mid, o, t)).fetchone()
    return r if r and r[0] is not None and r[1] is not None else None
def books_at(state, outs):
    """state[(book, outcome)] = [odds, suspended] -> {family: {outcome: fair prob}}, {family: {outcome: raw odds}}"""
    per = {}
    for (b, o), (od, sus) in state.items():
        if od and od > 1.0 and not sus: per.setdefault(b, {})[o] = od
    fair, raw = {}, {}
    for b, d in per.items():
        if set(d) != set(outs): continue
        over = sum(1 / d[o] for o in outs)
        if not (1.0 <= over <= 1.25): continue
        f = fam(b)
        fair.setdefault(f, []).append({o: (1 / d[o]) / over for o in outs})
        raw.setdefault(f, []).append(d)
    fair = {f: {o: st.mean(x[o] for x in v) for o in outs} for f, v in fair.items()}
    raw = {f: {o: st.median(x[o] for x in v) for o in outs} for f, v in raw.items()}
    return fair, raw
def apply(state, book, o, kind, price):
    k = (book, o)
    if kind == 'close': state.pop(k, None)
    elif kind == 'suspend':
        if k in state: state[k][1] = True
    elif kind == 'resume':
        if price: state[k] = [price, False]
        elif k in state: state[k][1] = False
    else:
        if price: state[k] = [price, False]
games = c.execute("SELECT m.id, m.sport, m.start_ms, m.home, m.away FROM matches m WHERE m.id IN (SELECT match_id FROM book_events WHERE book='polymarket') AND m.start_ms IS NOT NULL ORDER BY m.id").fetchall()
rec0 = c.execute("SELECT MIN(ts_ms) FROM changes WHERE id = (SELECT MIN(id) FROM changes)").fetchone()[0]
out = []; t_start = time.time(); nset = 0; rows_read = 0
for gi, (mid, sport, S, home, away) in enumerate(games):
    outs = [r[0] for r in c.execute("SELECT DISTINCT outcome FROM pm_quotes WHERE match_id=? AND ts_ms >= (SELECT MAX(ts_ms)-600000 FROM pm_quotes WHERE match_id=?)", (mid, mid))]
    if len(outs) < 2: continue
    win = None
    for o in outs:
        r = c.execute("SELECT bid FROM pm_quotes WHERE match_id=? AND outcome=? AND bid IS NOT NULL ORDER BY ts_ms DESC LIMIT 1", (mid, o)).fetchone()
        if r and r[0] >= 0.97: win = o
    if not win: continue
    nset += 1
    time.sleep(0.03)          # the live bot shares the database
    # pre-match: one pass over the bookmaker rows before the start
    state = {}; snaps = []
    pre_rows = c.execute("SELECT ts_ms, book, outcome, kind, new_price FROM changes WHERE match_id=? AND market='1x2' AND ts_ms>=? AND ts_ms<=? AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms", (mid, S - 9 * H, S - 10 * M)).fetchall()
    rows_read += len(pre_rows); i = 0
    for rel in PRE:
        t = S + rel
        if t < rec0 + 5 * M: continue
        while i < len(pre_rows) and pre_rows[i][0] <= t:
            apply(state, *pre_rows[i][1:]); i += 1
        recent = sum(1 for r in pre_rows[:i] if r[0] > t - 10 * M and r[3] == 'move')
        snaps.append((rel, t, books_at(state, outs), recent))
    for rel in LIVE:
        t = S + rel; state = {}
        rows = c.execute("SELECT ts_ms, book, outcome, kind, new_price FROM changes WHERE match_id=? AND market='1x2' AND ts_ms>? AND ts_ms<=? AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms", (mid, t - 10 * M, t)).fetchall()
        rows_read += len(rows)
        for r in rows: apply(state, *r[1:])
        snaps.append((rel, t, books_at(state, outs), sum(1 for r in rows if r[3] == 'move')))
    for rel, t, (fair, raw), recent in snaps:
        if len(fair) < 1: continue
        for o in outs:
            q = pmq(mid, o, t)
            if not q: continue
            q30 = pmq(mid, o, t + 30_000); qd = pmq(mid, o, t + 30 * M); qs = pmq(mid, o, S - M) if rel < 0 else None
            out.append(dict(m=mid, sport=sport, rel=rel // M, t=t, live=recent, o=o, k=len(outs), won=int(o == win),
                            bid=q[0], ask=q[1], age=(t - q[2]) / 1000, ask30=q30[1] if q30 else None, bid30=q30[0] if q30 else None,
                            mid30m=(qd[0] + qd[1]) / 2 if qd else None, mid_start=(qs[0] + qs[1]) / 2 if qs else None,
                            fair={f: round(v[o], 5) for f, v in fair.items()}, raw={f: v[o] for f, v in raw.items()}))
    if gi % 150 == 0:
        print(f"{gi}/{len(games)} games, {nset} settled, {len(out)} rows, {rows_read} bookmaker rows read, {time.time() - t_start:.0f} s", flush=True)
json.dump(out, open(sys.argv[1] if len(sys.argv) > 1 else 'ev_snaps.json', 'w'))
print(f"done: {nset} settled games, {len(out)} rows, {rows_read} bookmaker rows read, {time.time() - t_start:.0f} s")
