"""Test 4, data: Kalshi as the place to bet. Same checks as the Polymarket value test, with Kalshi's bid/ask."""
from sx import *
t_run = int(time.time() * 1000); out = []; t0 = time.time(); nb = 0
def kq(ev, o, t):
    r = c.execute("SELECT bid, ask, ts_ms, ask_size FROM kalshi_quotes WHERE event=? AND outcome=? AND ts_ms<=? AND est=0 ORDER BY ts_ms DESC LIMIT 1", (ev, o, t)).fetchone()
    return r if r and r[0] is not None and r[1] is not None else None
evs = c.execute("SELECT e.book_event_id, e.match_id, m.sport, m.start_ms FROM book_events e JOIN matches m ON m.id=e.match_id WHERE e.book='kalshi' AND m.start_ms IS NOT NULL GROUP BY e.match_id HAVING e.last_seen_ms = MAX(e.last_seen_ms)").fetchall()
PRE = [-6 * H, -3 * H, -60 * M, -10 * M]; LIVE = [20 * M, 60 * M, 100 * M]; nset = 0; skipped = 0; nswap = 0
for gi, (ev, mid, sport, S) in enumerate(evs):
    outs = sorted(r[0] for r in c.execute("SELECT DISTINCT outcome FROM kalshi_quotes WHERE event=?", (ev,)))
    if len(outs) < 2: continue
    # kalshi_quotes keeps Kalshi's own home/away; `changes` (book kalshi) uses the game's. Compare the two to learn
    # whether this event is listed the other way round.
    ref = c.execute("SELECT new_price, ts_ms FROM changes WHERE match_id=? AND book='kalshi' AND market='1x2' AND outcome='home' AND new_price > 1 ORDER BY ts_ms DESC LIMIT 400", (mid,)).fetchall()[::20]
    dh = da = 0.0; nref = 0
    for price, ts in ref:
        h = c.execute("SELECT bid, ask FROM kalshi_quotes WHERE event=? AND outcome='home' AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1", (ev, ts)).fetchone()
        aw = c.execute("SELECT bid, ask FROM kalshi_quotes WHERE event=? AND outcome='away' AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1", (ev, ts)).fetchone()
        if not h or not aw or None in h or None in aw: continue
        dh += abs(1 / price - (h[0] + h[1]) / 2); da += abs(1 / price - (aw[0] + aw[1]) / 2); nref += 1
    if nref < 3 or abs(dh - da) / nref < 0.03: skipped += 1; continue
    swap = da < dh
    ks = lambda o: ({'home': 'away', 'away': 'home'}.get(o, o) if swap else o)
    nswap += swap
    win = None
    for o in outs:
        r = c.execute("SELECT bid FROM kalshi_quotes WHERE event=? AND outcome=? AND bid IS NOT NULL AND est=0 ORDER BY ts_ms DESC LIMIT 1", (ev, ks(o))).fetchone()
        if r and r[0] >= 0.97: win = o
    if not win: continue
    nset += 1
    rows = book_rows(mid, S - 9 * H, S - 10 * M); nb += len(rows); rest(len(rows)); state = {}; i = 0; snaps = []
    for rel in PRE:
        t = S + rel
        while i < len(rows) and rows[i][0] <= t:
            apply(state, *rows[i]); i += 1
        snaps.append((rel, t, groups(state, outs), 0))
    for rel in LIVE:
        t = S + rel; state = {}; rows = book_rows(mid, t - 10 * M, t); nb += len(rows); rest(len(rows))
        for r in rows: apply(state, *r)
        snaps.append((rel, t, groups(state, outs), sum(1 for r in rows if r[3] == 'move')))
    for rel, t, g, recent in snaps:
        if len(g) < 3: continue
        for o in outs:
            q = kq(ev, ks(o), t)
            if not q: continue
            q30 = kq(ev, ks(o), t + 30_000); p = pm(mid, o, t)
            out.append(dict(m=mid, sport=sport, rel=rel // M, t=t, live=recent, o=o, k=len(outs), won=int(o == win), bid=q[0], ask=q[1], age=(t - q[2]) / 1000,
                            ask30=q30[1] if q30 else None, size=q[3], fair=st.median(v[o] for v in g.values()), nf=len(g), pm_bid=p[0] if p else None, pm_ask=p[1] if p else None))
    if gi % 250 == 0: print(f"{gi}/{len(evs)} Kalshi games, {nset} settled, {len(out)} rows, {time.time() - t0:.0f} s", flush=True)
json.dump(out, open('t4.json', 'w'))
print(f"listed the other way round: {nswap} of {nset}; side could not be told on {skipped} games")
print(f"done: {nset} settled Kalshi games, {len(out)} rows from {len({r['m'] for r in out})} games with 3+ bookmaker groups, {nb} bookmaker rows, {time.time() - t0:.0f} s"); print(late_check(t_run))
