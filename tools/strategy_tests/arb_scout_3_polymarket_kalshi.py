"""The Polymarket-vs-Kalshi arbitrages of 5%+: rebuilt from the recorded quotes of both exchanges, with Kalshi's
sides checked against its own normalised prices."""
import sqlite3, json, time, datetime as dt, bisect
c = sqlite3.connect('file:/Users/danielvinichenko/swifteye/oddsfeed.db?mode=ro', uri=True)
HM = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime('%d %b %H:%M:%S')
pmfee = lambda p: 0.05 * p * (1 - p); kfee = lambda p: 0.07 * p * (1 - p)
now = int(time.time() * 1000)
rows = c.execute("SELECT id, match_id, sport, home, away, first_ms, COALESCE(end_ms, last_ms), peak_profit, legs FROM arb_history WHERE first_ms >= ? AND peak_profit >= 0.05 AND books IN ('kalshi,polymarket','polymarket,kalshi') AND end_reason != 'restart' ORDER BY match_id, first_ms", (now - 72 * 3600_000,)).fetchall()
print(len(rows), "records")
by = {}
for id_, m, sport, home, away, first, end, peak, legs in rows:
    g = by.setdefault(m, dict(sport=sport, game=f"{home} v {away}", spans=[]))
    g['spans'].append((first, end, peak, json.loads(legs)))
for m, g in by.items():
    ev = c.execute("SELECT book_event_id FROM book_events WHERE book='kalshi' AND match_id=? ORDER BY last_seen_ms DESC LIMIT 1", (m,)).fetchone()[0]
    start = c.execute("SELECT start_ms FROM matches WHERE id=?", (m,)).fetchone()[0]
    a, z = min(s[0] for s in g['spans']) - 600_000, max(s[1] for s in g['spans']) + 600_000
    # which way round is Kalshi? compare its raw 'home' quote with its normalised price in `changes`
    ref = c.execute("SELECT new_price, ts_ms FROM changes WHERE match_id=? AND book='kalshi' AND market='1x2' AND outcome='home' AND new_price>1 AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 200", (m, z)).fetchall()[::10]
    dh = da = 0.0; n = 0
    for price, ts in ref:
        h = c.execute("SELECT bid, ask FROM kalshi_quotes WHERE event=? AND outcome='home' AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1", (ev, ts)).fetchone()
        w = c.execute("SELECT bid, ask FROM kalshi_quotes WHERE event=? AND outcome='away' AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1", (ev, ts)).fetchone()
        if h and w and None not in h and None not in w: dh += abs(1 / price - (h[0] + h[1]) / 2); da += abs(1 / price - (w[0] + w[1]) / 2); n += 1
    swap = da < dh if n >= 2 else None
    ks = lambda o: ({'home': 'away', 'away': 'home'}[o] if swap else o)
    pm = {o: c.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask, ask_size FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms BETWEEN ? AND ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (m, o, a - 3600_000, z)).fetchall() for o in ('home', 'away')}
    kq = {o: c.execute("SELECT ts_ms, bid, ask, ask_size FROM kalshi_quotes WHERE event=? AND outcome=? AND est=0 AND ts_ms BETWEEN ? AND ? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (ev, ks(o), a - 3600_000, z)).fetchall() for o in ('home', 'away')}
    def at(q, t):
        i = bisect.bisect_right([x[0] for x in q], t) - 1
        return q[i] if i >= 0 else None
    print(f"\n{g['game']} ({g['sport']}), starts {HM(start) if start else '?'} | Kalshi listed the other way round: {swap} (checked on {n} points)")
    for first, end, peak, legs in g['spans']:
        best = None; secs = 0; t = first
        while t <= end:
            for A_, B_ in (('home', 'away'), ('away', 'home')):          # buy A on Polymarket, B on Kalshi
                p, k = at(pm[A_], t), at(kq[B_], t)
                if p and k:
                    cost = p[2] + pmfee(p[2]) + k[2] + kfee(k[2]); prof = 1 - cost
                    if best is None or prof > best[0]: best = (prof, t, A_, p, B_, k)
                    if prof > 0.005: secs += 5
            t += 5000
        stored = ' + '.join(f"{l['book']} {l.get('side')} {l['outcome']} @{l['odds']:.2f}" for l in legs)
        if best:
            prof, t, A_, p, B_, k = best
            print(f"  scout: {HM(first)} for {(end-first)/1000:.0f} s, peak {peak*100:.1f}% ({stored})")
            print(f"     rebuilt from the quotes: best {prof*100:+.1f}% at {HM(t)}: Polymarket {A_} ask {p[2]:.3f} (size {p[3] or 0:.0f}) + Kalshi {B_} ask {k[2]:.3f} (size {k[3] or 0:.0f}, quote {max(0,(t-k[0])/1000):.0f} s old); above 0.5% for {secs} s")
