"""Does the running bot decide what the checked offline copy (live_emu.py) decides on the same data?

usage: check_live.py FROM_MS        (FROM_MS = when the bot was last started)
Compares every move the bot logged since then (7+ point moves: smaller ones are not logged)
with the offline copy's measurement and verdict for the same bookmaker update.
"""
import sys, json, sqlite3, time
from collections import Counter, defaultdict
import live_emu as E

T0 = int(sys.argv[1]); T1 = int(time.time() * 1000) - 20_000
db = sqlite3.connect(E.DB, uri=True)
emu = defaultdict(list)
C = E.Chunk(T0, T1)
last = {}
for ts, m, o, book, p in C.ev:
    k = (m, o, book); prev = last.get(k); last[k] = p
    if prev is None or abs(p - prev) < E.MIN_JUMP: continue
    d = 1.0 if p > prev else -1.0
    x = C.measure(m, o, d, book, ts)
    if isinstance(x, str) or x['bm'][0] < 0.07: continue
    x['why'] = C.gate(x, 'F')
    emu[(m, o, book, d)].append(x)
rows = db.execute("SELECT ts_ms, result, data FROM bot_signals WHERE ts_ms >= ? AND data LIKE '%\"move\"%' ORDER BY ts_ms", (T0,)).fetchall()
n = same = miss = 0; diffs = defaultdict(list); verdicts = Counter(); bad = []
PASS = ('entered', 'same_move', 'already_in_game', 'no_cash', 'too_thin', 'price_ran', 'no_live_book', 'no_offer', 'feed_quiet', 'gone_at_match', 'real_not_filled', 'real_order_failed')
for ts, res, data in rows:
    d = json.loads(data); f = d.get('f', d)
    if 'move' not in f: continue
    c = [x for x in emu.get((f['match_id'], f['outcome'], f['trigger'], f['dir']), []) if 1000 <= ts - x['t0'] <= 40_000]
    if not c: miss += 1; continue
    x = min(c, key=lambda x: abs(ts - x['t0'] - 2000 - f.get('measure_ms', 0)))
    n += 1
    diffs['bookmaker move'].append(abs(x['bm'][0] - f['move'][0])); diffs['polymarket followed'].append(abs(x['fol'][1] - f['followed'][1]))
    diffs['spread'].append(abs(x['sp'][1] - f['spread'][1]))
    if f.get('followed_since_start') is not None: diffs['moved since 1st book'].append(abs(x['fol_start'] - f['followed_since_start']))
    cx = x.get('cx') or {}
    if f.get('gap_before') is not None and cx:
        diffs['gap before the move'].append(abs((cx['cons_now'] - cx['cons_move'] * x['d'] - x['pre_mid']) * x['d'] - f['gap_before']))
    live_pass = res in PASS; emu_pass = x['why'] is None
    verdicts[(('buy' if live_pass else 'skip'), ('buy' if emu_pass else 'skip'))] += 1
    if live_pass == emu_pass: same += 1
    else: bad.append((E.U(ts).strftime('%H:%M:%S'), f['match_id'], f['trigger'], 'bot: ' + res, 'copy: ' + str(x['why'])))
print(f"{E.U(T0):%H:%M:%S} -> {E.U(T1):%H:%M:%S} UTC: the bot logged {len(rows)} moves of 7+ points; {n} matched to the offline copy, {miss} without a twin there")
for k, v in diffs.items():
    v.sort(); print(f"  {k:22s} identical on {sum(x < 0.001 for x in v)}/{len(v)}   worst difference {v[-1]:.4f}")
print(f"  same buy / skip verdict: {same}/{n}   (bot, copy): {dict(verdicts)}")
for b in bad[:12]: print('   differs:', b)
print('bot results:', Counter(r[1] for r in rows).most_common(12))
tr = db.execute("SELECT id, real, ts_ms, closed_ms, data FROM bot_trades WHERE ts_ms >= ? ORDER BY ts_ms", (T0,)).fetchall()
for id_, real, ts, cl, data in tr:
    v = json.loads(data)
    print(f"  trade #{id_} {'REAL ' if real else 'paper'} {E.U(ts):%H:%M:%S} {v['home'][:16]} v {v['away'][:14]} {v['outcome']} buy {v['entry']:.3f} x{v['contracts']:.0f}" + (f" -> {v['exit']:.3f} {v['pnl']:+.2f} {v['reason']}" if cl else ' (open)') + (f" twin of #{v['twin_of']}" if v.get('twin_of') else ''))
