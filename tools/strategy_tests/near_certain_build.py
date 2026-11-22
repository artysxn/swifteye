"""Test 2, data: near-certain sides in play. For every settled game, Polymarket every 3 minutes from the start;
wherever a side is offered at 88-98.5c, what the bookmakers say about that side at that moment."""
from sx import *
t_run = int(time.time() * 1000); out = []; G = games(); t0 = time.time(); nb = 0
for gi, (mid, sport, S, outs, win) in enumerate(G):
    cands = []; t = S; quiet = 0
    while t < S + 6 * H:
        qs = {o: pm(mid, o, t) for o in outs}
        if all(q is None for q in qs.values()):
            quiet += 1
            if quiet > 5: break
        elif any(q and q[0] >= 0.995 for q in qs.values()): break
        else:
            for o, q in qs.items():
                if q and 0.88 <= q[1] <= 0.985: cands.append((t, o, q))
        t += 3 * M
    rest(40)
    if not cands: continue
    rows = book_rows(mid, cands[0][0] - 15 * M, cands[-1][0]); nb += len(rows); rest(len(rows))
    state = {}; i = 0
    for t, o, q in cands:
        while i < len(rows) and rows[i][0] <= t:
            apply(state, *rows[i]); i += 1
        g = groups(state, outs)
        q5 = pm(mid, o, t + 5000)
        fresh = [t - v[2] for (b, oo), v in state.items() if oo == o and not v[1]]
        out.append(dict(m=mid, sport=sport, t=t, rel=(t - S) // M, o=o, won=int(o == win), bid=q[0], ask=q[1], age=(t - q[2]) / 1000, ask_size=q[4],
                        ask5=q5[1] if q5 else None, size5=q5[4] if q5 else None, fair=sorted(round(v[o], 4) for v in g.values()), newest_s=min(fresh) / 1000 if fresh else None))
    if gi % 200 == 0: print(f"{gi}/{len(G)} games, {len(out)} rows, {nb} bookmaker rows, {time.time() - t0:.0f} s", flush=True)
json.dump(out, open('t2.json', 'w'))
print(f"done: {len(out)} rows from {len({r['m'] for r in out})} of {len(G)} games, {nb} bookmaker rows, {time.time() - t0:.0f} s"); print(late_check(t_run))
