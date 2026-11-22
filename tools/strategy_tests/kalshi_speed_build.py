"""Kalshi speed test, data. Every sudden Polymarket rise in play (4+ points within 10 s) on games that Kalshi also
lists, and Kalshi's own quotes around it. Times are the EXCHANGES' own clocks (server_ts_ms): our Kalshi ticker
feed arrives about 1.5 s late, so our receive times would make Kalshi look slower than it is."""
from sx import *
import bisect
t_run = int(time.time() * 1000); out = []; t0 = time.time(); nr = 0; nswap = skipped = 0
ev_of = {}
for ev, mid in c.execute("SELECT e.book_event_id, e.match_id FROM book_events e WHERE e.book='kalshi' GROUP BY e.match_id HAVING e.last_seen_ms = MAX(e.last_seen_ms)"): ev_of[mid] = ev
G = [g for g in games() if g[0] in ev_of]
print(f"{len(G)} settled games are on both exchanges", flush=True)
for gi, (mid, sport, S, outs, win) in enumerate(G):
    ev = ev_of[mid]
    ref = c.execute("SELECT new_price, ts_ms FROM changes WHERE match_id=? AND book='kalshi' AND market='1x2' AND outcome='home' AND new_price > 1 ORDER BY ts_ms DESC LIMIT 400", (mid,)).fetchall()[::20]
    dh = da = 0.0; n = 0
    for price, ts in ref:
        h = c.execute("SELECT bid, ask FROM kalshi_quotes WHERE event=? AND outcome='home' AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1", (ev, ts)).fetchone()
        aw = c.execute("SELECT bid, ask FROM kalshi_quotes WHERE event=? AND outcome='away' AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1", (ev, ts)).fetchone()
        if not h or not aw or None in h or None in aw: continue
        dh += abs(1 / price - (h[0] + h[1]) / 2); da += abs(1 / price - (aw[0] + aw[1]) / 2); n += 1
    rest(60)
    if n < 3 or abs(dh - da) / n < 0.03: skipped += 1; continue
    swap = da < dh; nswap += swap
    ks = lambda o: ({'home': 'away', 'away': 'home'}.get(o, o) if swap else o)
    q = c.execute("SELECT COALESCE(server_ts_ms, ts_ms), ts_ms, outcome, bid, ask FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (mid, S, S + 6 * H)).fetchall()
    nr += len(q); rest(len(q))
    ser = {o: ([], [], [], []) for o in outs}
    for st_, ts, o, b, a in q:
        if o in ser and (not ser[o][0] or st_ >= ser[o][0][-1]):
            ser[o][0].append(st_); ser[o][1].append(b); ser[o][2].append(a); ser[o][3].append(ts)
    jumps = []
    for o in outs:
        T, B, A, R = ser[o]; j = 0; last = 0
        for i in range(len(T)):
            t = T[i]
            if t < last + 60_000: continue
            while j + 1 < i and T[j + 1] <= t - 10_000: j += 1
            if T[j] > t - 10_000 or t - T[j] > 120_000: continue
            m0 = (B[j] + A[j]) / 2; m1 = (B[i] + A[i]) / 2
            if m1 - m0 >= 0.04 and 0.10 <= m1 <= 0.90 and A[i] - B[i] <= 0.05 and A[j] - B[j] <= 0.05:
                jumps.append((t, R[i], o, m0, m1, B[i], A[i])); last = t
    if not jumps: continue
    jumps.sort()
    kq = c.execute("SELECT COALESCE(server_ts_ms, ts_ms), ts_ms, outcome, bid, ask, ask_size FROM kalshi_quotes WHERE event=? AND est=0 AND ts_ms>=? AND ts_ms<=? ORDER BY ts_ms", (ev, jumps[0][0] - 3 * M, jumps[-1][0] + 8 * M)).fetchall()
    nr += len(kq); rest(len(kq))
    kser = defaultdict(list)
    for st_, ts, o, b, a, sz in kq: kser[o].append((st_, ts, b, a, sz))
    for o in kser: kser[o].sort()
    for t, t_recv, o, m0, m1, pb, pa in jumps:
        K = kser.get(ks(o), [])
        if len(K) < 3: continue
        ST = [k[0] for k in K]
        a, b = bisect.bisect_right(ST, t - 10_000) - 1, bisect.bisect_right(ST, t + 310_000)
        if a < 0: continue
        path = [(k[0] - t, k[1] - t_recv, k[2], k[3], k[4]) for k in K[a:b]]   # (ms after the jump by Kalshi's clock, by our receive clock, bid, ask, size at ask)
        if len(path) > 300: path = path[:300]
        out.append(dict(m=mid, sport=sport, t=t, rel=(t - S) // M, o=o, won=int(o == win), m0=m0, m1=m1, pm_bid=pb, pm_ask=pa, path=path))
    if gi % 40 == 0: print(f"{gi}/{len(G)} games, {len(out)} jumps, {nr} rows read, {time.time() - t0:.0f} s", flush=True)
json.dump(out, open('k1.json', 'w'))
print(f"done: {len(out)} Polymarket jumps in {len({r['m'] for r in out})} games ({nswap} listed the other way round on Kalshi, {skipped} games where the side could not be told), {nr} rows, {time.time() - t0:.0f} s"); print(late_check(t_run))
