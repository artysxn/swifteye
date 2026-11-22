"""Shared helpers for the strategy tests on Swifteye's recording. Read-only, throttled so the live bot keeps its speed."""
import sqlite3, time, statistics as st, json, math, hashlib, os, random
from collections import defaultdict, Counter
DB = '/Users/danielvinichenko/swifteye/oddsfeed.db'
c = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
H, M = 3600_000, 60_000
fee = lambda a: 0.05 * a * (1 - a)
def fam(b):
    if b in ('linebet', 'melbet', 'betwinner', 'megapari', 'paripesa', '22bet', '22bit', '1xbit'): return '1xbet'
    if b in ('unibet', 'leovegas', 'paf'): return 'kambi'
    if b in ('fonbet', 'pari', 'mostbet'): return 'fonbet'
    if b in ('vinn', 'betking'): return 'altenar'
    return b
def power(odds):
    """Bookmaker odds -> fair probabilities (power method: each implied probability to the same power, summing to 1)."""
    pi = [1 / o for o in odds]
    if sum(pi) <= 1.0: return [p / sum(pi) for p in pi]
    lo, hi = 1.0, 6.0
    for _ in range(40):
        k = (lo + hi) / 2
        if sum(p ** k for p in pi) > 1: lo = k
        else: hi = k
    return [p ** k for p in pi]
def apply(state, ts, book, o, kind, price):
    k = (book, o)
    if kind == 'close': state.pop(k, None)
    elif kind == 'suspend':
        if k in state: state[k][1] = True
    elif kind == 'resume':
        if price: state[k] = [price, False, ts]
        elif k in state: state[k][1] = False
    elif price: state[k] = [price, False, ts]
def groups(state, outs):
    """{group: {outcome: fair probability}} from the books that quote every outcome right now."""
    per = defaultdict(dict)
    for (b, o), (od, sus, _) in state.items():
        if od and od > 1.0 and not sus: per[b][o] = od
    g = defaultdict(list)
    for b, d in per.items():
        if set(d) != set(outs): continue
        over = sum(1 / d[o] for o in outs)
        if not (1.0 <= over <= 1.30): continue
        g[fam(b)].append(dict(zip(outs, power([d[o] for o in outs]))))
    return {f: {o: st.mean(x[o] for x in v) for o in outs} for f, v in g.items()}
def book_rows(mid, t0, t1):
    return c.execute("SELECT ts_ms, book, outcome, kind, new_price FROM changes WHERE match_id=? AND market='1x2' AND ts_ms>? AND ts_ms<=? AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms", (mid, t0, t1)).fetchall()
def pm(mid, o, t):
    r = c.execute("SELECT bid, ask, ts_ms, bid_size, ask_size FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1", (mid, o, t)).fetchone()
    return r if r and r[0] is not None and r[1] is not None else None
def games():
    """Settled Polymarket games: (id, sport, start, outcomes, winner). Cached."""
    if os.path.exists('games.json'): return json.load(open('games.json'))
    out = []
    for mid, sport, S in c.execute("SELECT m.id, m.sport, m.start_ms FROM matches m WHERE m.id IN (SELECT match_id FROM book_events WHERE book='polymarket') AND m.start_ms IS NOT NULL ORDER BY m.id").fetchall():
        outs = sorted(r[0] for r in c.execute("SELECT DISTINCT outcome FROM pm_quotes WHERE match_id=? AND ts_ms >= (SELECT MAX(ts_ms)-600000 FROM pm_quotes WHERE match_id=?)", (mid, mid)))
        if len(outs) < 2: continue
        win = None
        for o in outs:
            r = c.execute("SELECT bid FROM pm_quotes WHERE match_id=? AND outcome=? AND bid IS NOT NULL ORDER BY ts_ms DESC LIMIT 1", (mid, o)).fetchone()
            if r and r[0] >= 0.97: win = o
        if win: out.append((mid, sport, S, outs, win))
    json.dump(out, open('games.json', 'w'))
    return out
_n = [0]
def rest(rows=0):
    """Pause so the live bot's reads are never queued behind ours for long."""
    _n[0] += rows
    time.sleep(0.08 + rows / 25000)
def boot(by, fn, n=400, seed=3):
    rnd = random.Random(seed); ks = list(by); vals = []
    for _ in range(n):
        v = fn([x for k in (rnd.choice(ks) for _ in ks) for x in by[k]])
        if v is not None: vals.append(v)
    vals.sort()
    return (vals[int(len(vals) * .05)], vals[int(len(vals) * .95)]) if len(vals) > 20 else (float('nan'), float('nan'))
def late_check(since_ms):
    r = c.execute("SELECT COUNT(*), SUM(result='measured_too_late'), MAX(CAST(json_extract(data,'$.measure_ms') AS INT)) FROM bot_signals WHERE ts_ms >= ? AND json_extract(data,'$.measure_ms') IS NOT NULL", (since_ms,)).fetchone()
    return f"live bot during this run: {r[0]} decisions, {r[1] or 0} late, slowest {r[2]} ms"
