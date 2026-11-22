"""Test 3, data: sudden Polymarket drops in play (4+ points within 10 s) on every 4th settled game, and what the
bookmakers did around each one."""
from sx import *
import bisect
t_run = int(time.time() * 1000); out = []; t0 = time.time(); nr = 0
G = [g for g in games() if int(hashlib.md5(str(g[0]).encode()).hexdigest()[:6], 16) % 4 == 0]
for gi, (mid, sport, S, outs, win) in enumerate(G):
    q = c.execute("SELECT ts_ms, outcome, bid, ask FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (mid, S, S + 6 * H)).fetchall()
    nr += len(q); rest(len(q))
    ser = {o: ([], [], []) for o in outs}
    for ts, o, b, a in q:
        if o in ser: ser[o][0].append(ts); ser[o][1].append(b); ser[o][2].append(a)
    def at(o, t):
        T, B, A = ser[o]; i = bisect.bisect_right(T, t) - 1
        return (B[i], A[i], T[i]) if i >= 0 else None
    spikes = []
    for o in outs:
        T, B, A = ser[o]; j = 0; last = 0
        for i in range(len(T)):
            t = T[i]
            if t < last + 180_000: continue
            while j + 1 < i and T[j + 1] <= t - 10_000: j += 1
            if T[j] > t - 10_000 or t - T[j] > 120_000: continue
            m0 = (B[j] + A[j]) / 2; m1 = (B[i] + A[i]) / 2
            if m0 - m1 >= 0.04 and 0.12 <= m0 <= 0.90 and A[i] - B[i] <= 0.05 and A[j] - B[j] <= 0.05:
                spikes.append((t, o, m0, m1)); last = t
    if not spikes: continue
    spikes.sort()
    rows = book_rows(mid, spikes[0][0] - 5 * M, spikes[-1][0] + 2 * M); nr += len(rows); rest(len(rows))
    need = sorted({tt for t, *_ in spikes for tt in (t - 10_000, t + 20_000, t + 60_000)})
    state = {}; i = 0; snap = {}
    for tt in need:
        while i < len(rows) and rows[i][0] <= tt:
            apply(state, *rows[i]); i += 1
        snap[tt] = groups(state, outs)
    RT = [r[0] for r in rows]
    for t, o, m0, m1 in spikes:
        g0, g20, g60 = snap[t - 10_000], snap[t + 20_000], snap[t + 60_000]
        fair = lambda g: st.median(v[o] for v in g.values()) if len(g) >= 2 else None
        a, b = bisect.bisect_right(RT, t - 10_000), bisect.bisect_right(RT, t + 20_000)
        e = at(o, t + 20_000)
        later = {k: at(o, t + ms) for k, ms in (('1', 60_000), ('3', 180_000), ('5', 300_000), ('15', 900_000))}
        out.append(dict(m=mid, sport=sport, t=t, rel=(t - S) // M, o=o, won=int(o == win), m0=m0, m1=m1, n0=len(g0), n20=len(g20), n60=len(g60), f0=fair(g0), f20=fair(g20), f60=fair(g60),
                        susp=sum(1 for r in rows[a:b] if r[3] == 'suspend'), bmoves=sum(1 for r in rows[a:b] if r[3] == 'move'),
                        e_bid=e[0] if e else None, e_ask=e[1] if e else None, later={k: (v[0], v[1]) if v else None for k, v in later.items()}))
    if gi % 60 == 0: print(f"{gi}/{len(G)} games, {len(out)} drops, {nr} rows read, {time.time() - t0:.0f} s", flush=True)
json.dump(out, open('t3.json', 'w'))
print(f"done: {len(out)} drops in {len({r['m'] for r in out})} of {len(G)} games, {nr} rows read, {time.time() - t0:.0f} s"); print(late_check(t_run))
