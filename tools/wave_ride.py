"""If we RODE the waves, which wave size works? (owner, 22 Nov 2026: "if we were to attempt to ride the waves, which
number here is best?")

Patterns first, then the cost. On games Swifteye recorded itself (Polymarket's order book every second or two, real
buy and sell prices), waves are found as in tools/wave_count.py: a turn is CONFIRMED once the middle of the book has
come back X cents from the last top / bottom. For every confirmed wave:
  further     how much further the middle went after the confirmation, before the next turn was confirmed
              (a ride keeps: further - X, the price given back before the exit's confirmation)
  ride        buy the side that is rising at its ask 2 s after the confirmation; sell it 1c under its bid 2 s after
              the opposite turn is confirmed; the fee 0.05 x p x (1 - p) on both; $12 a ride (as the bot)
A down wave is ridden on the other side (its ask = 1 - this side's bid). In play only (from the listed start until
the price is decided at 98c / 2c).

usage: wave_ride.py [games, default 400] [step seconds, default 2]
       -> prints the table, writes ~/swifteye/research/paths/wave_ride_<step>s.txt
"""
import sys, os, sqlite3, random, statistics as st

DB = os.path.expanduser('~/swifteye/research/../oddsfeed.db')
N = int(sys.argv[1]) if len(sys.argv) > 1 else 400
STEP = int(sys.argv[2]) if len(sys.argv) > 2 else 2
SIZES = (2, 3, 5, 8, 10, 15, 20)
STAKE = 12.0
fee = lambda p: 0.05 * p * (1 - p) if 0 < p < 1 else 0.0
SPORTS = {'tennis': 'tennis', 'cs2': 'esports', 'dota2': 'esports', 'lol': 'esports', 'valorant': 'esports', 'basketball': 'basketball', 'hockey': 'hockey', 'football': 'football'}


def book_steps(q, a, b, step):
    """(bid, ask) in cents every `step` ms from a to b, the last quote held."""
    out = []; k = 0; last = None
    for u in range(a, b + 1, step):
        while k < len(q) and q[k][0] <= u: last = (q[k][1] * 100, q[k][2] * 100); k += 1
        out.append(last)
    return out


def turns(x, th):
    """Confirmed turns of the middle x (list of cents or None): (direction of the new wave, index confirmed, extreme index)."""
    out = []; d = 0; ext = None; ext_i = 0; start = None
    for i, v in enumerate(x):
        if v is None: continue
        if start is None: start = ext = v; ext_i = i; continue
        if d == 0:
            if abs(v - start) >= th: d = 1 if v > start else -1; ext, ext_i = v, i; out.append((d, i, ext_i))
            continue
        if (v - ext) * d > 0: ext, ext_i = v, i
        elif abs(v - ext) >= th:
            d = -d; out.append((d, i, ext_i)); ext, ext_i = v, i
    return out


def ride(bk, d, i, j, lag):
    """Buy the rising side at index i + lag, sell at index j + lag (or the last quote). Returns $ on STAKE or None."""
    n = len(bk); a = bk[min(i + lag, n - 1)]; b = bk[min(j + lag, n - 1)]
    if a is None or b is None: return None
    if d > 0: buy, sell = a[1] / 100, (b[0] - 1) / 100          # this side: ask in, bid - 1c out
    else: buy, sell = 1 - a[0] / 100, (100 - b[1] - 1) / 100    # the other side: its ask = 1 - bid, its bid = 1 - ask
    if not (0.02 <= buy <= 0.97): return None
    sh = STAKE / buy
    return sh * (max(sell, 0.0) - buy) - sh * (fee(buy) + fee(max(sell, 0.001))), (a[1] - a[0])


if __name__ == '__main__':
    db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
    ms = [r for r in db.execute("SELECT id, sport, start_ms FROM matches WHERE start_ms BETWEEN 1795046400000 AND 1795478400000") if r[1] in SPORTS]
    random.seed(7); random.shuffle(ms)
    G = []
    for mid, sport, start in ms:
        if len(G) >= N: break
        q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome='home' AND bid > 0 AND ask > 0 AND ask > bid ORDER BY id", (mid,)).fetchall()
        q = [x for x in q if x[0] >= start]
        if len(q) < 300: continue
        end = q[-1][0]
        for t, b, a in q:
            if (a + b) / 2 >= 0.98 or (a + b) / 2 <= 0.02: end = t; break
        if end - start < 20 * 60_000: continue
        bk = book_steps(q, start, end, STEP * 1000)
        x = [None if v is None else (v[0] + v[1]) / 2 for v in bk]
        G.append((SPORTS[sport], (end - start) / 60000, bk, x))
    lag = max(1, round(2 / STEP))
    out = ['RIDING THE WAVES on %d games Swifteye recorded (17-22 Nov), in play, the book every %d s; a ride = $%.0f in at the ask %d s after the turn is confirmed, out 1c under the bid %d s after the opposite turn, both fees' % (len(G), STEP, STAKE, lag * STEP, lag * STEP)]
    for sport in ['ALL'] + sorted({g[0] for g in G}):
        X = G if sport == 'ALL' else [g for g in G if g[0] == sport]
        if len(X) < 15: continue
        out.append('\n%s: %d games, median %d min played' % (sport, len(X), st.median(g[1] for g in X)))
        for th in SIZES:
            fur = []; res = []; tight = []
            for _, _, bk, x in X:
                T = turns(x, th)
                for k in range(len(T) - 1):
                    d, i, _ = T[k]; d2, j, e = T[k + 1]
                    ext = x[e]; at = x[i]
                    if ext is None or at is None: continue
                    fur.append((ext - at) * d)
                    r = ride(bk, d, i, j, lag)
                    if r: res.append(r[0]); (tight.append(r[0]) if r[1] <= 3 else None)
            if len(res) < 20: out.append('  %2dc waves: too few' % th); continue
            out.append('  %2dc waves: %5.1f rides a game | after you can see it, the wave goes %4.1fc further (median %4.1fc), you give back %2dc before the exit is seen | ride: won %3.0f%%, %+5.2f $ a ride (%+5.2f with a spread of 3c or less, %d rides), %+6.2f $ a game' % (
                th, len(res) / len(X), st.mean(fur), st.median(fur), th, 100.0 * sum(1 for v in res if v > 0) / len(res), st.mean(res), st.mean(tight) if tight else float('nan'), len(tight), sum(res) / len(X)))
    txt = '\n'.join(out); print(txt)
    open(os.path.expanduser('~/swifteye/research/paths/wave_ride_%ds.txt' % STEP), 'w').write(txt + '\n')
