"""Does the wave count from TRADES (tools/game_paths.mjs + wave_count.py) match what the ORDER BOOK shows?
(companion to tools/wave_count.py, 22 Nov 2026)

For games Swifteye recorded itself (17-21 Nov; Polymarket's book every second or two, pm_quotes), the same market's
every trade is fetched from Polymarket's Data API (exact match: pm_quotes.event_id is Polymarket's EVENT id; its moneyline market is the one followed), and the
direction changes are counted on
  book     the recorded sell price (bid) of the home side, 10-second steps
  trades   side 0's sell price from the trades at the bid, 10-second steps (what wave_count.py uses on older weeks)
  minute   Polymarket's own price line, one point a minute
Waves are counted the same way as in wave_count.py (direction changes of 2 / 3 / 5 / 10c+, flat ignored).
Direction changes do not depend on which side is followed (a side's price falls exactly when the other's rises).

usage: wave_res.py [games, default 200]      (reads ~/swifteye/oddsfeed.db read-only, by match_id; fetches with node)
"""
import sys, os, json, sqlite3, random, subprocess, statistics as st
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wave_count import turns, steps, at_bid, SIZES, STEP

DB = os.path.expanduser('~/swifteye/oddsfeed.db')
N = int(sys.argv[1]) if len(sys.argv) > 1 else 200
SPORTS = {'tennis': 'tennis', 'cs2': 'esports', 'dota2': 'esports', 'lol': 'esports', 'valorant': 'esports', 'basketball': 'basketball', 'hockey': 'hockey'}
FETCH = r"""
const ids = JSON.parse(process.argv[1]); const out = {};
const get = async (u) => { for (let k = 0; k < 4; k++) { try { const r = await fetch(u, { signal: AbortSignal.timeout(30000) }); if (r.ok) return await r.json(); } catch {} await new Promise((q) => setTimeout(q, 1500)); } return null; };
for (const id of ids) {
  const e = await get('https://gamma-api.polymarket.com/events/' + id);
  const m = (e?.markets || []).find((x) => x.sportsMarketType === 'moneyline') || (e?.markets || [])[0]; if (!m || !m.conditionId) continue;
  const toks = JSON.parse(m.clobTokenIds || '[]'); const tr = []; let cur = '';
  for (let pg = 0; pg < 200; pg++) {
    const r = await get('https://data-api.polymarket.com/v2/trades?conditionId=' + m.conditionId + '&limit=500' + (cur ? '&cursor=' + cur : ''));
    if (!r || !Array.isArray(r.data)) break;
    for (const x of r.data) { const t = x.timestamp > 1e11 ? Math.floor(x.timestamp / 1000) : x.timestamp; const k = x.token_id === toks[0] ? 0 : 1; tr.push([t, k === 0 ? x.price : 1 - x.price, x.size, x.side === 'BUY' ? 1 : -1, k]); }
    if (!r.pagination?.has_more || !r.pagination?.next_cursor) break; cur = r.pagination.next_cursor;
  }
  const s = Math.floor(Date.parse(String(m.gameStartTime || '').replace(' ', 'T').replace(/\+00$/, 'Z')) / 1000);
  const h = await get('https://clob.polymarket.com/prices-history?market=' + toks[0] + '&startTs=' + (s - 600) + '&endTs=' + (s + 8 * 3600) + '&fidelity=1');
  tr.sort((a, b) => a[0] - b[0]);
  out[id] = { s, trades: tr, line: (h?.history || []).map((x) => [x.t, x.p]) };
  await new Promise((q) => setTimeout(q, 200));
}
process.stdout.write(JSON.stringify(out));
"""

db = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
ms = [r for r in db.execute("SELECT id, sport, start_ms FROM matches WHERE start_ms BETWEEN 1795046400000 AND 1795392000000") if r[1] in SPORTS]
random.seed(5); random.shuffle(ms)
pick = []
for mid, sport, start in ms:
    if len(pick) >= N: break
    q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), event_id, bid FROM pm_quotes WHERE match_id=? AND outcome='home' AND bid > 0 ORDER BY id", (mid,)).fetchall()
    q = [x for x in q if x[0] >= start]
    if len(q) < 300 or (q[-1][0] - start) < 20 * 60_000: continue
    pick.append((mid, SPORTS[sport], start // 1000, q[0][1], [(t / 1000.0, b) for t, _, b in q]))
print('%d recorded games picked; fetching their trades from Polymarket ...' % len(pick), flush=True)
CACHE = os.path.expanduser('~/swifteye/research/paths/res_check_trades.json')
got = json.load(open(CACHE)) if os.path.exists(CACHE) else {}
for i in range(0, len(pick), 25):
    if all(str(p[3]) in got for p in pick[i:i + 25]): continue
    ids = [p[3] for p in pick[i:i + 25]]
    r = subprocess.run(['node', '--input-type=module', '-e', FETCH, json.dumps(ids)], capture_output=True, text=True, timeout=1800)
    try: got.update(json.loads(r.stdout or '{}'))
    except ValueError: print('fetch failed:', r.stderr[:300])
json.dump(got, open(CACHE, 'w'))
from wave_count import v3turns
rows = []
for mid, sport, start, eid, book in pick:
    m = got.get(str(eid))
    if not m or len(m['trades']) < 30: continue
    s0 = start; end = int(book[-1][0])
    xb = steps(book, s0, end, STEP)
    for i, v in enumerate(xb):
        if v is not None and (v >= 98 or v <= 1): xb = xb[:i + 1]; end = s0 + i * STEP; break
    if end - s0 < 20 * 60: continue
    bid = [x for x in m['trades'] if at_bid(x)]
    xt = steps(bid, s0, end, STEP); xl = steps(m['line'], s0, end, 60) if m['line'] else []
    if sum(1 for v in xt if v is not None) < 30: continue
    r = dict(sport=sport, mins=(end - s0) / 60)
    xb30 = steps(book, s0, end, 30); xb60 = steps(book, s0, end, 60)
    for th in SIZES:
        r['b%d' % th] = max(0, len(turns(xb, th)) - 1); r['t%d' % th] = max(0, len(turns(xt, th)) - 1); r['l%d' % th] = max(0, len(turns(xl, th)) - 1) if xl else 0
        r['b30_%d' % th] = max(0, len(turns(xb30, th)) - 1); r['b60_%d' % th] = max(0, len(turns(xb60, th)) - 1)
    r['bv3'] = max(0, len(v3turns(xb, grid=3)) - 1); r['lv3'] = max(0, len(v3turns(xl)) - 1) if xl else 0; r['tv3'] = max(0, len(v3turns(xt, grid=3)) - 1)
    rows.append(r)
print('%d games with the book, the trades and the minute line (in play, from the listed start to decided)' % len(rows))
M = lambda X, k: sum(r[k] for r in X) / len(X)
for s in sorted({r['sport'] for r in rows}) + ['ALL']:
    X = rows if s == 'ALL' else [r for r in rows if r['sport'] == s]
    if len(X) < 10: continue
    print('  %-11s %4d games, median %3.0f min' % (s, len(X), st.median(r['mins'] for r in X)))
    for th in SIZES:
        print('     %2dc+  book every 10 s %5.1f | book every 30 s %5.1f | book every minute %5.1f | Polymarket minute line %5.1f | trades at the sell price %5.1f' % (th, M(X, 'b%d' % th), M(X, 'b30_%d' % th), M(X, 'b60_%d' % th), M(X, 'l%d' % th), M(X, 't%d' % th)))
    print('     v3    book %5.1f | minute line %5.1f | trades %5.1f' % (M(X, 'bv3'), M(X, 'lv3'), M(X, 'tv3')))
