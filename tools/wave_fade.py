"""BET ON THE TURN: wait until a wave looks finished, bet the opposite way, hold until the turn pays.
(owner, 22 Nov 2026: "what if we just bet on a wave to happen at all? if the market changes direction 11 times on
average a game, we wait until we THINK a wave has ended, bet the opposite, and wait until it hits. does that work?")

Patterns first: on a fairly priced market, a bet held "until it hits" wins often and small and loses rarely and big,
and the two cancel before costs. So the first number is the GROSS one (middle of the book in, middle out, no costs):
above zero only if Polymarket's price really swings back more than chance. Then the real bet.

On games Swifteye recorded itself (the order book every 2 s, real buy and sell prices), in play:
  the wave      the middle of the book has moved W cents or more from the last turn (W = 3 / 5 / 10 / 15 / 20)
  it looks over the middle has come back c cents from that wave's top (c = 0: bet the moment the wave reaches W;
                c = 1 / 2 / 3: bet once it has stopped and turned that much)
  the bet       $12 on the opposite side at its buy price (ask) 2 s later
  it hits       our side's sell price (bid) reaches the buy price + T cents (T = 2 / 3 / 5 / 10): sold there
  it never hits held to the end of the game (paid 1 or 0 when decided at 98c / 2c; sold at the last bid if the record
                ends before that) - or, in the give-up version, sold at the bid after 5 minutes
Fee 0.05 x p x (1 - p) on the buy and on a sale (none at the settlement). One bet at a time a game.

usage: wave_fade.py [games, default 400]       (first run reads ~/swifteye/oddsfeed.db read-only and caches the books
       in ~/swifteye/research/paths/book_<games>.pickle)  -> ~/swifteye/research/paths/wave_fade.txt
"""
import sys, os, sqlite3, random, pickle, statistics as st
from array import array

N = int(sys.argv[1]) if len(sys.argv) > 1 else 400
STEP = 2
STAKE = 12.0
fee = lambda p: 0.05 * p * (1 - p) if 0 < p < 1 else 0.0
SPORTS = {'tennis': 'tennis', 'cs2': 'esports', 'dota2': 'esports', 'lol': 'esports', 'valorant': 'esports', 'basketball': 'basketball', 'hockey': 'hockey', 'football': 'football'}
CACHE = os.path.expanduser('~/swifteye/research/paths/book_%d.pickle' % N)


def load():
    """Per game: sport, minutes, home bid and ask in cents every 2 s (0 = no quote), and how it ended (1 home won,
    0 away won, None = the record ends before it is decided)."""
    if os.path.exists(CACHE): return pickle.load(open(CACHE, 'rb'))
    db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
    ms = [r for r in db.execute("SELECT id, sport, start_ms FROM matches WHERE start_ms BETWEEN 1795046400000 AND 1795478400000") if r[1] in SPORTS]
    random.seed(7); random.shuffle(ms)          # the same games as tools/wave_ride.py
    G = []
    for mid, sport, start in ms:
        if len(G) >= N: break
        q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome='home' AND bid > 0 AND ask > 0 AND ask > bid ORDER BY id", (mid,)).fetchall()
        q = [x for x in q if x[0] >= start]
        if len(q) < 300: continue
        end = q[-1][0]; res = None
        for t, b, a in q:
            m = (a + b) / 2
            if m >= 0.98 or m <= 0.02: end = t; res = 1 if m >= 0.98 else 0; break
        if end - start < 20 * 60_000: continue
        bid = array('f'); ask = array('f'); k = 0; last = (0.0, 0.0)
        for u in range(start, end + 1, STEP * 1000):
            while k < len(q) and q[k][0] <= u: last = (q[k][1] * 100, q[k][2] * 100); k += 1
            bid.append(last[0]); ask.append(last[1])
        G.append(dict(id=mid, sport=SPORTS[sport], mins=(end - start) / 60000, bid=bid, ask=ask, res=res))
    pickle.dump(G, open(CACHE, 'wb'), protocol=4)
    return G


def run(g, W, c, T, give_up=None, lag=1):
    """All bets of one game. Returns [(net $, gross cents a share at the middle, hit?, minutes held)].
    Waves are followed as a zigzag of the middle: a wave grows while the price goes further; once it has come back
    c cents (c = 0: the first moment it does not go further) a wave of W+ is bet against; a smaller wave that comes back
    1c (or c) is over and the next wave starts from its top."""
    bid, ask = g['bid'], g['ask']; n = len(bid)
    mid = [(bid[i] + ask[i]) / 2 if bid[i] > 0 else None for i in range(n)]
    out = []; i = 0
    base = None; d = 0; ext = None
    while i < n:
        v = mid[i]
        if v is None: i += 1; continue
        if base is None: base = ext = v; d = 0; i += 1; continue
        if d == 0:
            if abs(v - base) >= 1.0: d = 1 if v > base else -1; ext = v
            i += 1; continue
        if (v - ext) * d > 0: ext = v; i += 1; continue
        back = abs(v - ext)
        if abs(ext - base) >= W - 1e-9 and back >= c:
            j = min(i + lag, n - 1)
            if bid[j] <= 0: i += 1; continue
            side = -d                                                       # bet the price turns
            buy = ask[j] / 100 if side > 0 else 1 - bid[j] / 100
            mid_in = mid[j] if side > 0 else 100 - mid[j]
            if not (0.03 <= buy <= 0.95): base, d, ext = ext, -d, v; i += 1; continue
            sh = STAKE / buy; cost = sh * fee(buy); target = buy * 100 + T
            hit = None; k = j + 1; stop = n if give_up is None else min(n, j + 1 + give_up // STEP)
            while k < stop:
                if bid[k] > 0:
                    sb = bid[k] if side > 0 else 100 - ask[k]                  # our side's sell price
                    if sb >= target: hit = k; break
                k += 1
            if hit is not None:
                sell = (bid[hit] if side > 0 else 100 - ask[hit]) / 100
                net = sh * (sell - buy) - cost - sh * fee(sell); mo = (mid[hit] if side > 0 else 100 - mid[hit]); end_i = hit
            elif give_up is not None and stop < n:
                e = stop
                while e > j and bid[e] <= 0: e -= 1
                sell = max(0.0, ((bid[e] if side > 0 else 100 - ask[e]) - 1) / 100)
                net = sh * (sell - buy) - cost - sh * fee(sell); mo = (mid[e] if side > 0 else 100 - mid[e]); end_i = e
            elif g['res'] is not None:
                pay = g['res'] if side > 0 else 1 - g['res']
                net = sh * (pay - buy) - cost; mo = pay * 100; end_i = n
            else:
                e = n - 1
                while e > j and bid[e] <= 0: e -= 1
                sell = max(0.0, ((bid[e] if side > 0 else 100 - ask[e]) - 1) / 100)
                net = sh * (sell - buy) - cost - sh * fee(sell); mo = (mid[e] if side > 0 else 100 - mid[e]); end_i = n
            out.append((net, mo - mid_in, hit is not None, (min(end_i, n - 1) - j) * STEP / 60, ask[j] - bid[j], buy))
            if end_i >= n: break
            i = end_i + 1; base = None                                      # look for the next wave from there
            continue
        if back >= max(c, 1.0):                                             # a smaller wave is over
            base, d, ext = ext, -d, v
        i += 1
    return out


def durations(G, sizes=(2, 3, 5, 10, 20)):
    """How long waves last: zigzag of the middle, a turn confirmed X cents back; a wave lasts from its turn to its top."""
    out = ['\nHOW LONG WAVES LAST (the middle of the book, a wave from one turn to the next top or bottom; confirmed X cents back)']
    for sport in ['ALL'] + sorted({g['sport'] for g in G}):
        X = G if sport == 'ALL' else [g for g in G if g['sport'] == sport]
        cells = []
        for th in sizes:
            L = []
            for g in X:
                bid, ask = g['bid'], g['ask']; mid = [(bid[i] + ask[i]) / 2 if bid[i] > 0 else None for i in range(len(bid))]
                d = 0; ext = None; ext_i = 0; st_i = 0; start = None
                for i, v in enumerate(mid):
                    if v is None: continue
                    if start is None: start = ext = v; st_i = ext_i = i; continue
                    if d == 0:
                        if abs(v - start) >= th: d = 1 if v > start else -1; ext, ext_i = v, i
                        continue
                    if (v - ext) * d > 0: ext, ext_i = v, i
                    elif abs(v - ext) >= th:
                        L.append(((ext_i - st_i) * STEP / 60, abs(ext - start))); start, st_i = ext, ext_i; d = -d; ext, ext_i = v, i
            if len(L) < 20: continue
            m = sorted(x[0] for x in L)
            cells.append('%2dc+: %5.1f min on average, median %4.1f, middle half %4.1f-%4.1f (%d waves, %.1f a game)' % (th, st.mean(m), m[len(m) // 2], m[len(m) // 4], m[3 * len(m) // 4], len(L), len(L) / len(X)))
        out.append('  %-11s %4d games | ' % (sport, len(X)) + ' | '.join(cells))
    return out


if __name__ == '__main__':
    G = load()
    print('%d recorded games loaded' % len(G), flush=True)
    D_ = durations(G); print('\n'.join(D_), flush=True)
    lines = ['BET ON THE TURN on %d games Swifteye recorded (17-22 Nov), in play, the book every 2 s, $%.0f a bet. "gross" = middle of the book in and out, no costs (above 0 only if the price swings back more than chance)' % (len(G), STAKE)]
    cells = []
    for gu, lab in ((None, 'HOLD UNTIL IT HITS (else to the end of the game)'), (300, 'GIVE UP AFTER 5 MINUTES')):
        lines.append('\n== ' + lab)
        for W in (3, 5, 10, 15, 20):
            for c in (0, 1, 2, 3):
                if c >= W: continue
                print('  ... wave %dc, back %dc (%s)' % (W, c, 'hold' if gu is None else 'give up'), flush=True) if c == 0 else None
                for T in (2, 3, 5, 10):
                    R = [r for g in G for r in run(g, W, c, T, gu)]
                    if len(R) < 30: continue
                    net = [r[0] for r in R]; gross = [r[1] for r in R]; hit = sum(1 for r in R if r[2])
                    miss = [r[0] for r in R if not r[2]]; th_ = sorted(r[3] for r in R if r[2])
                    cells.append((gu, W, c, T, st.mean(net), sum(net), len(R)))
                    lines.append('  wave %2dc+, looks over after %dc back, target +%2dc | %6d bets (%5.1f a game) | hit %3.0f%% after a median %4.1f min | gross %+5.2fc a share | net %+5.2f $ a bet, a miss costs %+6.2f $ | total %+8.0f $ (%+6.2f $ a game)' % (
                        W, c, T, len(R), len(R) / len(G), 100.0 * hit / len(R), th_[len(th_) // 2] if th_ else 0.0, st.mean(gross), st.mean(net), st.mean(miss) if miss else 0.0, sum(net), sum(net) / len(G)))
    best = sorted(cells, key=lambda x: -x[4])[:5]
    lines.append('\nBEST 5 SETTINGS by $ a bet: ' + ' | '.join('%s W%d c%d T%d %+.2f $ a bet on %d bets' % ('hold' if b[0] is None else 'give up 5 min', b[1], b[2], b[3], b[4], b[6]) for b in best))
    lines.append('settings with a positive total: %d of %d' % (sum(1 for x in cells if x[5] > 0), len(cells)))
    # by sport, for the best hold-until-it-hits setting
    hb = sorted([x for x in cells if x[0] is None], key=lambda x: -x[4])[0]
    lines.append('\nBY SPORT for the best hold setting (wave %dc+, %dc back, target +%dc):' % (hb[1], hb[2], hb[3]))
    for s in sorted({g['sport'] for g in G}):
        X = [g for g in G if g['sport'] == s]; R = [r for g in X for r in run(g, hb[1], hb[2], hb[3], None)]
        if R: lines.append('  %-11s %4d games %5d bets | hit %3.0f%% | gross %+5.2fc | net %+5.2f $ a bet | %+7.0f $ total' % (s, len(X), len(R), 100.0 * sum(1 for r in R if r[2]) / len(R), st.mean(r[1] for r in R), st.mean(r[0] for r in R), sum(r[0] for r in R)))
    lines += D_
    txt = '\n'.join(lines); print(txt)
    open(os.path.expanduser('~/swifteye/research/paths/wave_fade.txt'), 'w').write(txt + '\n')
