"""Step 3 of the suspicious-CS2 study: bookmaker odds BEFORE the start (Swifteye records them since 17 Nov 2026).

For every finished CS2 game linked to Polymarket: each bookmaker group's first and last price before the start (margin
removed), the median move across groups and how many moved the same way; Polymarket's own move; the tracked wallets'
money before the start; Valve ranks and the money traded (cs2_activity); who won.
Reads the live database per game with pauses. usage: bookmoves.py
"""
import os, sys, json, time, sqlite3, statistics as st, datetime as dt
from collections import defaultdict
sys.argv = sys.argv[:1]
import study as S

db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
now = int(time.time() * 1000)
G = db.execute("SELECT m.id, m.home, m.away, m.league, m.start_ms, b.book_event_id FROM matches m JOIN book_events b ON b.match_id = m.id AND b.book = 'polymarket' "
               "WHERE m.sport = 'cs2' AND m.start_ms IS NOT NULL AND m.start_ms < ?", (now - 4 * 3600 * 1000,)).fetchall()


def platform(b):
    if b in ('linebet', 'melbet', 'betwinner', 'megapari', 'paripesa', '22bet', '22bit', '1xbit'): return '1xbet'
    if b in ('unibet', 'leovegas', 'paf'): return 'kambi'
    if b in ('fonbet', 'pari', 'mostbet'): return 'fonbet'
    if b in ('vinn', 'betking'): return 'altenar'
    return b


# the tracked wallets' money before the start, by Polymarket event id and team name
money = defaultdict(lambda: defaultdict(lambda: [0.0, set()]))
for p in S.POS:
    if p['type'] == 'moneyline' and p['pre']:
        eid = str((S.M.get(p['cid']) or {}).get('eid') or '')
        x = money[eid][p['team'].lower()]; x[0] += p['cost']; x[1].add(p['w'])
act_by_eid = {}
try:
    for eid, slug in db.execute("SELECT event_id, slug FROM cs2_activity"): act_by_eid[str(eid)] = S.ACT.get(slug)
except Exception: pass

# results: the winner market of each Polymarket event (cached)
import fetch_wallets as F
RES_P = S.R + '/event_results.json'
RES = json.load(open(RES_P)) if os.path.exists(RES_P) else {}
for mid, home, away, league, start, eid in G:
    if str(eid) in RES and RES[str(eid)].get('winner') is not None: continue
    e = F.get(f'https://gamma-api.polymarket.com/events/{eid}') or {}
    w = None; vol = None
    for m in e.get('markets') or []:
        if m.get('sportsMarketType') != 'moneyline': continue
        try: pr = [float(x) for x in json.loads(m.get('outcomePrices') or '[]')]; outs = json.loads(m.get('outcomes') or '[]')
        except Exception: continue
        vol = float(m.get('volumeNum') or 0)
        if m.get('closed') and len(pr) == 2 and max(pr) >= 0.99: w = outs[pr.index(max(pr))]
    RES[str(eid)] = dict(winner=w, vol=vol, title=e.get('title'))
json.dump(RES, open(RES_P, 'w'))

def name_side(name, home, away):
    if not name: return None
    n, h, a = name.lower(), home.lower(), away.lower()
    if n == h or n in h or h in n: return 'home'
    if n == a or n in a or a in n: return 'away'
    return None

rows = []
for mid, home, away, league, start, eid in G:
    ch = db.execute("SELECT ts_ms, book, outcome, new_price FROM changes WHERE match_id = ? AND market = '1x2' AND ts_ms < ? AND new_price > 1 AND book NOT IN ('polymarket', 'kalshi') ORDER BY ts_ms", (mid, start)).fetchall()
    time.sleep(0.04 + len(ch) / 30000)
    cur = {}; series = defaultdict(list)                       # per platform: (ts, fair home probability)
    for ts, book, oc, px in ch:
        if oc not in ('home', 'away'): continue
        k = platform(book); cur[(k, oc)] = px
        h, a = cur.get((k, 'home')), cur.get((k, 'away'))
        if h and a: series[k].append((ts, (1 / h) / (1 / h + 1 / a)))
    mv = {k: v[-1][1] - v[0][1] for k, v in series.items() if len(v) >= 2 and v[-1][0] - v[0][0] > 600_000}
    if len(mv) < 2: continue
    med = st.median(mv.values()); agree = sum(1 for x in mv.values() if x * med > 0 and abs(x) >= abs(med) / 2)
    close = st.median(v[-1][1] for v in series.values())
    # when the move happened: the time the median of the groups was first half-way
    half_t = None
    if abs(med) >= 0.03:
        ts_all = sorted({t for v in series.values() for t, _ in v})
        for t in ts_all[::max(len(ts_all) // 200, 1)]:
            cur_m = [next((p for tt, p in reversed(v) if tt <= t), v[0][1]) - v[0][1] for v in series.values() if len(v) >= 2]
            if cur_m and st.median(cur_m) * med > 0 and abs(st.median(cur_m)) >= abs(med) / 2: half_t = t; break
    q = db.execute("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = 'home' AND bid IS NOT NULL AND ask IS NOT NULL AND ts_ms < ? ORDER BY ts_ms", (mid, start)).fetchall()
    last = db.execute("SELECT bid, ask FROM pm_quotes WHERE match_id = ? AND outcome = 'home' AND ts_ms > ? ORDER BY ts_ms DESC LIMIT 1", (mid, start)).fetchone()
    time.sleep(0.03)
    won = name_side((RES.get(str(eid)) or {}).get('winner'), home, away)
    if won is None and last and last[0] is not None and last[1] is not None:
        mdl = (last[0] + last[1]) / 2; won = 'home' if mdl >= 0.97 else 'away' if mdl <= 0.03 else None
    elif last and (last[0] or 0) >= 0.97: won = 'home'
    elif last and last[1] is not None and last[1] <= 0.03: won = 'away'
    pm_move = ((q[-1][1] + q[-1][2]) / 2 - (q[0][1] + q[0][2]) / 2) if len(q) >= 2 else None
    tm = money.get(str(eid)) or {}
    def side_money(name):
        n = name.lower()
        for t, (m, ws) in tm.items():
            if t == n or t in n or n in t: return m, len(ws)
        return 0.0, 0
    mh, ma = side_money(home), side_money(away)
    a = act_by_eid.get(str(eid)) or {}
    any_money = sum(p['cost'] for p in S.POS if str((S.M.get(p['cid']) or {}).get('eid') or '') == str(eid))
    first_buy = min((p['t'] * 1000 for p in S.POS if p['type'] == 'moneyline' and p['pre'] and str((S.M.get(p['cid']) or {}).get('eid') or '') == str(eid)), default=None)
    rows.append(dict(mid=mid, home=home, away=away, league=league, start=start, n=len(mv), move=med, agree=agree, close=close, pm_move=pm_move, won=won, mh=mh[0], wh=mh[1], ma=ma[0], wa=ma[1],
                     any_money=any_money, vh=a.get('vh'), va=a.get('va'), vol=a.get('vol'), score=a.get('score'), half_t=half_t, first_buy=first_buy, hours=(start - min(v[0][0] for v in series.values())) / 3600e3))
json.dump(rows, open(S.R + '/bookmoves.json', 'w'))
print(f"{len(G)} finished CS2 games linked to Polymarket; {len(rows)} with 2+ bookmaker groups priced for 10+ min before the start; result known for {sum(r['won'] is not None for r in rows)}")

D = [r for r in rows if r['won']]
def toward(r): return 'home' if r['move'] > 0 else 'away'
print("\nA. DOES A PRE-MATCH LINE MOVE POINT TO THE WINNER? (side the bookmakers moved TOWARD; its closing bookmaker price is what you would pay)")
print(f"  {'median move of the bookmaker groups':40s} {'games':>5s} {'that side won':>13s} {'closing price':>13s} {'edge':>6s}")
for a, b in ((0, .01), (.01, .03), (.03, .05), (.05, .08), (.08, 1)):
    X = [r for r in D if a <= abs(r['move']) < b]
    if not X: continue
    w = sum(r['won'] == toward(r) for r in X) / len(X); c = sum((r['close'] if r['move'] > 0 else 1 - r['close']) for r in X) / len(X)
    print(f"  {f'{a * 100:.0f}-{b * 100:.0f} points':40s} {len(X):5d} {w * 100:12.0f}% {c * 100:12.0f}% {(w - c) * 100:+5.0f}")
print("\nB. THE GAMES WITH THE BIGGEST MOVES (3+ points)")
print(f"  {'game':44s} {'ranks':>9s} {'move':>6s} {'groups':>6s} {'PM move':>7s} {'close':>5s} {'traded $':>9s} {'score':>5s} {'tracked $ home/away':>20s} {'won':>5s}")
for r in sorted(rows, key=lambda r: -abs(r['move'])):
    if abs(r['move']) < 0.03: break
    g = f"{r['home'][:20]} v {r['away'][:20]}"; rk = f"{r['vh'] or '-'}/{r['va'] or '-'}"
    print(f"  {g:44s} {rk:>9s} {r['move'] * 100:+5.1f} {r['agree']:2d}/{r['n']:<3d} {(r['pm_move'] or 0) * 100:+6.1f} {r['close'] * 100:4.0f}% {r['vol'] or 0:9,.0f} {r['score'] or 0:5.2f} {r['mh']:9,.0f}/{r['ma']:<9,.0f} {r['won'] or '?':>5s}  tracked $ any time {r['any_money']:,.0f}"
          + (f"  wallets bought {'before' if r['first_buy'] < r['half_t'] else 'after'} the line moved" if r['first_buy'] and r['half_t'] else ''))
print("\nC. TRACKED WALLETS AGAINST THE LINE")
T = [r for r in D if max(r['mh'], r['ma']) >= 200]
print(f"  games where the tracked wallets had $200+ on a side before the start: {len(T)} of {len(D)}")
if T:
    side = lambda r: 'home' if r['mh'] >= r['ma'] else 'away'
    for nm, X in (('the line moved 2+ points TOWARD their side', [r for r in T if abs(r['move']) >= 0.02 and toward(r) == side(r)]), ('the line moved 2+ points AWAY from their side', [r for r in T if abs(r['move']) >= 0.02 and toward(r) != side(r)]),
                  ('the line moved under 2 points', [r for r in T if abs(r['move']) < 0.02])):
        if X: print(f"  {nm:48s} {len(X):3d} games, their side won {sum(r['won'] == side(r) for r in X) * 100 // len(X)}%, priced {sum((r['close'] if side(r) == 'home' else 1 - r['close']) for r in X) * 100 / len(X):.0f}% at the close")
    print(f"  median absolute line move: games with tracked money {st.median(abs(r['move']) for r in T) * 100:.1f} pts, the rest {st.median(abs(r['move']) for r in D if r not in T) * 100:.1f} pts")
print("\nD. RANKS (Valve standings) of the games with a 5+ point move against the rest")
for nm, X in (('5+ point move', [r for r in rows if abs(r['move']) >= 0.05]), ('under 2 points', [r for r in rows if abs(r['move']) < 0.02])):
    R2 = [(r['vh'], r['va']) for r in X if r['vh'] or r['va']]
    if R2:
        worst = [max(x or 999 for x in p) for p in R2]; best = [min(x or 999 for x in p) for p in R2]
        print(f"  {nm:16s} {len(X):3d} games ({len(R2)} with a rank): better team's rank median {st.median(best):.0f}, worse team's {st.median(worst):.0f}; a team ranked 80+ or unranked in {sum(w >= 80 for w in worst) * 100 // len(worst)}%; both in the top 60 in {sum(w <= 60 for w in worst) * 100 // len(worst)}%")
