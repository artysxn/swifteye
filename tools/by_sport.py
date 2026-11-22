"""Which sports get through the rules, and what stops the others.

Part 1: the live decision log (bot_signals) by sport, for the two stretches when real money traded.
Part 2: the whole recording under TODAY'S rules, by sport: what each rule removes, and what the trades made.
        Trades are counted only where really available by Polymarket's own clock (see offpeak_rules.truly).
"""
import json, time, datetime as dt
from collections import Counter, defaultdict
import offpeak_rules as O

db, C = O.db, O.C
T0 = lambda s: int(dt.datetime.strptime(s, '%Y-%m-%d %H:%M').replace(tzinfo=dt.UTC).timestamp() * 1000)
sport = dict(db.execute("SELECT id, sport FROM matches"))

print("1. LIVE DECISION LOG BY SPORT")
for name, a, b in (('first real run, 18 Nov 22:48 - 19 Nov 05:26 UTC (old rules)', T0('2026-11-18 22:48'), T0('2026-11-19 05:26')),
                   ('second real stretch, 19 Nov 19:00 - 22:15 UTC', T0('2026-11-19 19:00'), T0('2026-11-19 22:15')),
                   ('current run, since 19 Nov 22:45 UTC (today\'s rules)', T0('2026-11-19 22:45'), int(time.time() * 1000))):
    by = defaultdict(Counter)
    for mid, res, data in db.execute("SELECT match_id, result, data FROM bot_signals WHERE ts_ms >= ? AND ts_ms < ?", (a, b)):
        if mid is None and data:
            try: mid = (json.loads(data).get('f') or json.loads(data)).get('match_id')
            except Exception: pass
        by[sport.get(mid, '?')][res] += 1
    print(f"\n {name}")
    for sp, c in sorted(by.items(), key=lambda kv: -sum(kv[1].values())):
        tot = sum(c.values())
        if tot < 15 and not c['entered']: continue
        top = ', '.join(f"{r} {n * 100 // tot}%" for r, n in c.most_common(4) if r != 'entered')
        print(f"   {sp:12s} looked at {tot:6d} | entered {c['entered']:2d} | {top}")
    time.sleep(0.1)

print("\n2. THE WHOLE RECORDING UNDER TODAY'S RULES, BY SPORT (7+ point bookmaker moves)")
hrs = (max(c['tf'] for c in C) - min(c['tf'] for c in C)) / 3600e3
print(f"   recording {hrs:.0f} h, {len(C)} rows")
P = O.BASE
ci = O.ci
rows = defaultdict(list)
for c in C: rows[c['sport']].append(c)


def moves(rs):
    seen = {}; n = 0
    for c in sorted(rs, key=lambda c: c['tf']):
        k = (c['match_id'], c['outcome'], c['dir'])
        if k not in seen or c['tf'] - seen[k] > 90_000: n += 1
        seen[k] = c['tf']
    return n


pct = lambda n, d: f"{(n * 100 / d if d else 0):3.0f}%"
print(f"   {'sport':11s} {'rows':>6s} {'moves':>6s} {'games':>5s} | each rule on its own, share of rows that pass it: "
      f"{'10+pt':>5s} {'2+grp':>5s} {'notfol':>6s} {'spread':>6s} {'room':>5s} {'lagged':>6s} | {'pass all':>8s} | trades won  P/L")
for sp, rs in sorted(rows.items(), key=lambda kv: -len(kv[1])):
    n = len(rs)
    big = sum(c['bm'][0] >= P['min_move'] for c in rs)
    grp = sum(c['cx']['fam_moved'] >= P['fams'] and c['cx']['cons_move'] >= P['cons'] for c in rs)
    nf = sum(max(c['fol']) <= P['max_followed'] and c['fol_start'] <= P['max_followed'] for c in rs)
    spd = sum(max(c['sp']) <= P['max_spread'] + 1e-9 for c in rs)
    room = sum(c['edge'][0][ci] >= P['min_edge'] and c['edge'][1][ci] >= P['min_edge'] for c in rs)
    lag = sum(c['cx']['jumps'] > 0 and c['cx']['follow'] >= 0.3 for c in rs)
    p = dict(P, basketball=True)
    ok = sum(O.passes(c, p) is None for c in rs)
    T = O.run(p, rs)
    print(f"   {sp:11s} {n:6d} {moves(rs):6d} {len({c['match_id'] for c in rs}):5d} | {'':52s} {pct(big, n):>5s} {pct(grp, n):>5s} {pct(nf, n):>6s} {pct(spd, n):>6s} {pct(room, n):>5s} {pct(lag, n):>6s} | {ok:8d} | "
          f"{len(T):3d} {sum(t['pnl'] > 0 for t in T):3d} {sum(t['pnl'] for t in T):+8.2f}")

print("\n3. THE FIRST RULE EACH ROW FAILS, BY SPORT (today's rules, in the order the bot checks them)")
for sp, rs in sorted(rows.items(), key=lambda kv: -len(kv[1])):
    if len(rs) < 40: continue
    why = Counter(O.passes(c, P) or 'PASSES' for c in rs)
    print(f"   {sp:11s}", ', '.join(f"{k} {pct(n, len(rs)).strip()}" for k, n in why.most_common(6)))

print("\n4. HOW BIG AND HOW FAST THE MOVES ARE, BY SPORT")
import statistics as st
for sp, rs in sorted(rows.items(), key=lambda kv: -len(kv[1])):
    if len(rs) < 40: continue
    mv = sorted(c['bm'][0] for c in rs); fo = sorted(max(c['fol']) for c in rs)
    px = [c['px'] for c in rs]
    print(f"   {sp:11s} bookmaker move: median {st.median(mv) * 100:4.1f} pt, top 10% over {mv[int(len(mv) * .9)] * 100:4.1f} pt | Polymarket had followed (1.5 s later): median {st.median(fo) * 100:4.0f}% of the move, "
          f"untouched in {sum(f <= 0.05 for f in fo) * 100 // len(fo)}% | bookmaker groups in the game: median {st.median(c['cx']['fams'] for c in rs):.0f}, that moved {st.median(c['cx']['fam_moved'] for c in rs):.0f}")
