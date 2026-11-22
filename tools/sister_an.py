"""Do Polymarket's sister markets (map winner, totals, map handicap) follow the match-winner market late?
Reads the recorder's files (tools/sister_recorder.mjs -> ~/swifteye/research/sister/). No database, no network.

For every jump of a game's MATCH-WINNER market (its mid moves 3+ cents within 10 s) and every sister market of
that game that was open and tight (spread 6c or less) just before:
  did the sister move the same way within 2 min (half or more of ... its own later move of 2c+)?
  how many seconds after the match market had made half of its jump did the sister make half of its own?
  was the OLD ask of the side that rose still there 1 s, 2 s, 5 s, 10 s after the match market's jump, and how
  many dollars were resting at it?  (that is what a snipe could buy)
  what a $12 buy at that old ask, 2 s after the jump, would have made sold at the bid 60 s later, fees included
usage: sister_an.py [min jump in cents, default 3]
"""
import json, glob, os, sys, bisect, statistics as st
from collections import defaultdict
D = os.path.expanduser('~/swifteye/research/sister')
JUMP = (float(sys.argv[1]) if len(sys.argv) > 1 else 3) / 100
fee = lambda p: 0.05 * p * (1 - p)
meta = json.load(open(D + '/meta.json'))
S = defaultdict(list)          # market key -> [(our ms, bid, bid size, ask, ask size)]
for f in sorted(glob.glob(D + '/*.jsonl')):
    for l in open(f):
        try: r = json.loads(l)
        except Exception: continue
        if 'b' in r: S[r['a']].append((r['t'], r['b'], r['bs'], r['k'], r['ks']))
T = {k: [x[0] for x in v] for k, v in S.items()}
def at(k, t):
    i = bisect.bisect_right(T[k], t)
    return S[k][i - 1] if i else None
def mid(q): return None if not q or q[1] is None or q[3] is None else (q[1] + q[3]) / 2
games = defaultdict(dict)
for k, m in meta.items():
    if k in S: games[m['match_id']].setdefault(m['kind'], []).append(k)
print(f"{sum(len(v) for v in S.values()):,} rows, {len(S)} markets, {len(games)} games; span {(max(max(t) for t in T.values()) - min(min(t) for t in T.values())) / 3600e3:.1f} h")
res = defaultdict(list); nj = 0
for g, kinds in games.items():
    for mk in kinds.get('moneyline', []):
        rows = S[mk]; last = 0
        for i in range(len(rows)):
            t = rows[i][0]
            if t - last < 30_000: continue
            q0, q1 = at(mk, t - 10_000), rows[i]
            m0, m1 = mid(q0), mid(q1)
            if m0 is None or m1 is None or abs(m1 - m0) < JUMP or q1[3] - q1[1] > 0.06 or q0[3] - q0[1] > 0.06 or not 0.08 <= m1 <= 0.92: continue
            if t - q0[0] > 120_000: continue
            sg = 1 if m1 > m0 else -1; size = abs(m1 - m0); last = t; nj += 1
            # the moment the match market had made half of the jump
            j = bisect.bisect_right(T[mk], t - 10_000)
            th = next((rows[x][0] for x in range(j, i + 1) if mid(rows[x]) is not None and sg * (mid(rows[x]) - m0) >= 0.5 * size), t)
            for kind, ks in kinds.items():
                if kind == 'moneyline': continue
                for sk in ks:
                    same = meta[sk]['outcome'] == meta[mk]['outcome']
                    if kind != 'child_moneyline': same = None      # totals / handicap: direction learnt from the sister's own move
                    b0 = at(sk, th - 3_000)
                    if not b0 or mid(b0) is None or b0[3] - b0[1] > 0.06 or th - b0[0] > 600_000: continue
                    s0 = mid(b0); later = mid(at(sk, th + 120_000))
                    if later is None: continue
                    mv = later - s0
                    d = (sg if same else -sg) if same is not None else (1 if mv > 0 else -1)
                    moved = d * mv >= 0.02
                    delay = None
                    if moved:
                        a = bisect.bisect_right(T[sk], th - 3_000)
                        delay = next(((S[sk][x][0] - th) / 1000 for x in range(a, len(S[sk])) if mid(S[sk][x]) is not None and d * (mid(S[sk][x]) - s0) >= 0.5 * abs(mv)), None)
                    # the old price of the side that rose: its ask if the sister's first outcome rose, else 1 - bid
                    old = b0[3] if d > 0 else 1 - b0[1]
                    still = {}
                    for w in (1, 2, 5, 10):
                        q = at(sk, th + w * 1000)
                        px, sz = (q[3], q[4]) if d > 0 else (None if q[1] is None else 1 - q[1], q[2])
                        still[w] = (px is not None and px <= old + 0.005, (sz or 0) * (px or 0))
                    pnl = None
                    q2, q60 = at(sk, th + 2_000), at(sk, th + 62_000)
                    if still[2][0] and q60:
                        e = old; x = q60[1] if d > 0 else (None if q60[3] is None else 1 - q60[3])
                        if x is not None: pnl = 12 / e * (x - fee(x) - e - fee(e))
                    res[kind].append(dict(moved=moved, delay=delay, still=still, pnl=pnl, size=size, sister_move=d * mv))
print(f"{nj} jumps of {JUMP * 100:.0f}c+ in the match-winner markets")
for kind, R in res.items():
    n = len(R); M = [r for r in R if r['moved']]
    if not n: continue
    print(f"\n== {kind}: {n} sister-market readings beside a jump; {len(M)} ({len(M) * 100 // n}%) moved the same way within 2 min (average {st.mean(r['sister_move'] for r in M) * 100 if M else 0:.1f}c for a match jump of {st.mean(r['size'] for r in M) * 100 if M else 0:.1f}c)")
    dl = sorted(r['delay'] for r in M if r['delay'] is not None)
    if dl: print(f"   delay behind the match market: median {st.median(dl):.1f} s; first quarter {dl[len(dl) // 4]:.1f} s, last quarter {dl[len(dl) * 3 // 4]:.1f} s; before it or together (under 1 s) {sum(x < 1 for x in dl) * 100 // len(dl)}%, 1-5 s {sum(1 <= x < 5 for x in dl) * 100 // len(dl)}%, 5-30 s {sum(5 <= x < 30 for x in dl) * 100 // len(dl)}%, over 30 s {sum(x >= 30 for x in dl) * 100 // len(dl)}%")
    for w in (1, 2, 5, 10):
        ok = [r for r in M if r['still'][w][0]]
        print(f"   old price still there {w:2d} s after the jump: {len(ok)} of {len(M)} ({len(ok) * 100 // max(1, len(M))}%), median ${st.median(r['still'][w][1] for r in ok) if ok else 0:.0f} resting at it")
    P = [r['pnl'] for r in M if r['pnl'] is not None]
    if P: print(f"   $12 at the old price 2 s after the jump, sold 60 s later (moved ones only): {len(P)} trades, {sum(x > 0 for x in P) * 100 // len(P)}% won, {sum(P):+.2f} ({st.mean(P):+.2f} each)")
    P = [r['pnl'] for r in R if r['pnl'] is not None]
    if P: print(f"   the same on EVERY reading (not knowing whether it will move): {len(P)} trades, {sum(x > 0 for x in P) * 100 // len(P)}% won, {sum(P):+.2f} ({st.mean(P):+.2f} each)")
