"""Test 1, data: pre-match line moves. For every settled game, the bookmakers' fair price minute by minute before the
start; whenever it moved 1.5+ points within 15 minutes, what Polymarket had done and what it did next."""
from sx import *
t_run = int(time.time() * 1000); out = []; G = games(); t0 = time.time(); nb = 0; W = 15 * M
for gi, (mid, sport, S, outs, win) in enumerate(G):
    rows = book_rows(mid, S - 9 * H, S - 2 * M); nb += len(rows); rest(len(rows))
    if len(rows) < 20: continue
    state = {}; i = 0; series = {}; cur = None; dirty = True
    t = (rows[0][0] // M + 1) * M
    while t <= S - 2 * M:
        while i < len(rows) and rows[i][0] <= t:
            apply(state, *rows[i]); i += 1; dirty = True
        if dirty: cur = groups(state, outs); dirty = False
        series[t] = cur; t += M
    armed = 0
    for t in sorted(series):
        if t < armed or t - W not in series: continue
        a, b = series[t - W], series[t]
        common = [g for g in a if g in b]
        if len(common) < 3: continue
        best = None
        for o in outs:
            d = [b[g][o] - a[g][o] for g in common]; mv = st.median(d)
            if best is None or mv > best[0]: best = (mv, o, sum(1 for x in d if x >= 0.01), st.median(a[g][o] for g in common), st.median(b[g][o] for g in common))
        mv, o, nmoved, fa, fb = best
        if mv < 0.015 or nmoved < 2: continue
        armed = t + 30 * M
        q0, q1, qe = pm(mid, o, t - W), pm(mid, o, t), pm(mid, o, t + 30_000)
        if not (q0 and q1 and qe): continue
        later = {}
        for name, tt in (('5', t + 5 * M), ('15', t + 15 * M), ('60', t + 60 * M), ('start', S - M)):
            if tt <= S - M:
                q = pm(mid, o, tt); later[name] = (q[0], q[1]) if q else None
        out.append(dict(m=mid, sport=sport, t=t, to_start=(S - t) / H, o=o, won=int(o == win), move=mv, n=len(common), nmoved=nmoved, fa=fa, fb=fb,
                        pm0=(q0[0] + q0[1]) / 2, pm1=(q1[0] + q1[1]) / 2, bid=qe[0], ask=qe[1], later=later))
    if gi % 200 == 0: print(f"{gi}/{len(G)} games, {len(out)} moves, {nb} bookmaker rows, {time.time() - t0:.0f} s", flush=True)
json.dump(out, open('t1.json', 'w'))
print(f"done: {len(out)} moves in {len({r['m'] for r in out})} of {len(G)} games, {nb} bookmaker rows, {time.time() - t0:.0f} s"); print(late_check(t_run))
