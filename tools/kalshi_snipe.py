"""Kalshi, re-tested on its real order-book prices (recorded since 20 Nov 2026 03:12 UTC), with both feeds checked
for gaps (owner, 21 Nov: "investigate Kalshi ... maybe a new bot for kalshi?").

Two ideas, the same machinery:
  A. POLYMARKET JUMPS -> BUY ON KALSHI   Polymarket's mid for a side rises 4+ points within 10 s in play; Kalshi's book
     has made 20% of that or less; buy Kalshi's ask D seconds later, sell when Kalshi's bid has caught up or after 5 min.
  B. KALSHI JUMPS -> BUY ON POLYMARKET   the mirror image (does Kalshi ever lead enough to be a trigger?).
Everything on OUR receive clock, as a live bot would see it. A signal counts only when
  - the signal venue's feed was on time (its quotes under 0.5 s late) and
  - the pooled stream of the venue we buy on shows no silent stretch over 2 s from 5 s before the signal to the buy
    (a silent stretch = our recording had a hole there: the "old price" is then just a price we stopped seeing).
The buy is made at the ask our feed shows 0.3 s AFTER the order lands (what really rested then), needs $20+ at that
price, a spread of 4c or less, 3c+ of room after both fees; one position a game, 2-minute lock.
Fees: Kalshi 7% x p x (1-p) a contract each way, Polymarket 5% x p x (1-p).
usage: kalshi_snipe.py build | report
"""
import sys, os, json, time, bisect, sqlite3, random, statistics as st, datetime as dt
from collections import defaultdict, Counter
MODE = sys.argv[1] if len(sys.argv) > 1 else 'all'
OUT = os.path.expanduser('~/swifteye/research/kalshi_snipe.json')
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
T_FROM = 1_795_144_320_000          # 20 Nov 03:12 UTC: Kalshi's own best book prices are recorded from here
H, M = 3_600_000, 60_000
NO = dt.timezone(dt.timedelta(hours=1))
DELAYS = (300, 500, 1000, 2000, 3000, 5000)
random.seed(8)


def build():
    t_now = int(time.time() * 1000)
    games = db.execute("""SELECT m.id, m.sport, m.home, m.away, m.start_ms, e.book_event_id FROM matches m JOIN book_events e ON e.match_id = m.id AND e.book = 'kalshi'
                          WHERE m.start_ms IS NOT NULL AND m.start_ms < ? AND m.start_ms > ? AND m.id IN (SELECT match_id FROM book_events WHERE book = 'polymarket')
                          GROUP BY m.id HAVING e.last_seen_ms = MAX(e.last_seen_ms) ORDER BY m.start_ms""", (t_now - 20 * M, T_FROM - 5 * H)).fetchall()
    print(f"{len(games)} games listed on both exchanges with play inside the recording", flush=True)
    out = []; pm_all = []; k_all = []; t0 = time.time(); nr = 0
    for gi, (mid, sport, home, away, S, ev) in enumerate(games):
        lo, hi = max(S, T_FROM), min(S + 5 * H, t_now)
        if hi - lo < 10 * M: continue
        pm = db.execute("SELECT ts_ms, COALESCE(server_ts_ms, ts_ms), outcome, bid, ask, ask_size FROM pm_quotes WHERE match_id=? AND ts_ms>=? AND ts_ms<=? AND bid IS NOT NULL AND ask IS NOT NULL ORDER BY ts_ms", (mid, lo, hi)).fetchall()
        kq = db.execute("SELECT ts_ms, outcome, book_bid, book_ask, ask_size FROM kalshi_quotes WHERE event=? AND ts_ms>=? AND ts_ms<=? AND book_bid IS NOT NULL AND book_ask IS NOT NULL ORDER BY ts_ms", (ev, lo, hi)).fetchall()
        nr += len(pm) + len(kq); time.sleep(0.01 + (len(pm) + len(kq)) / 400_000)
        if len(pm) < 50 or len(kq) < 50: continue
        P = defaultdict(list); K = defaultdict(list)
        for t, sv, o, b, a, sz in pm: P[o].append((t, b, a, sz, t - sv))
        for t, o, b, a, sz in kq: K[o].append((t, b, a, sz, 0))
        pm_all += [r[0] for r in pm[::3]]; k_all += [r[0] for r in kq[::3]]
        # Kalshi names the sides in its own order: match them by price
        def mid_at(s, t):
            i = bisect.bisect_right([x[0] for x in s], t); return (s[i - 1][1] + s[i - 1][2]) / 2 if i else None
        ks = {}
        if 'home' in P and 'home' in K and 'away' in K:
            smp = [P['home'][i][0] for i in range(0, len(P['home']), max(1, len(P['home']) // 40))]
            d1 = [abs(mid_at(P['home'], t) - x) for t in smp for x in [mid_at(K['home'], t)] if x is not None]
            d2 = [abs(mid_at(P['home'], t) - x) for t in smp for x in [mid_at(K['away'], t)] if x is not None]
            if len(d1) < 5 or len(d2) < 5 or abs(st.mean(d1) - st.mean(d2)) < 0.03: continue
            swap = st.mean(d2) < st.mean(d1)
            ks = {'home': 'away' if swap else 'home', 'away': 'home' if swap else 'away', 'draw': 'draw'}
        else:
            continue
        out.append(dict(m=mid, sport=sport, home=home, away=away, start=S, P={o: P[o] for o in P}, K={ks[o]: K[o] for o in K if o in ks}))
        if gi % 60 == 0: print(f"  {gi}/{len(games)} games, {len(out)} usable, {nr} rows, {time.time() - t0:.0f} s", flush=True)
    pm_all.sort(); k_all.sort()
    json.dump(dict(games=out, pm_all=pm_all, k_all=k_all), open(OUT, 'w'))
    print(f"done: {len(out)} games, {nr} rows, {time.time() - t0:.0f} s", flush=True)


def signals(sig, feed_late_ok=False):
    """jumps in one side's series: (t, mid 10 s before, mid now)"""
    T = [x[0] for x in sig]; out = []; last = -10 ** 15; j = 0
    for i, (t, b, a, sz, lag) in enumerate(sig):
        if t < last + 60_000: continue
        while j + 1 < i and T[j + 1] <= t - 10_000: j += 1
        if T[j] > t - 10_000 or t - T[j] > 120_000: continue
        m0 = (sig[j][1] + sig[j][2]) / 2; m1 = (b + a) / 2
        if m1 - m0 >= 0.04 and 0.10 <= m1 <= 0.90 and a - b <= 0.05 and sig[j][2] - sig[j][1] <= 0.05:
            if lag > 500 and not feed_late_ok: last = t; continue
            out.append((t, m0, m1)); last = t
    return out


def flowing(pool, a, b):
    """no silent stretch over 2 s in the pooled stream between a and b"""
    i = bisect.bisect_left(pool, a - 2000); prev = None
    while i < len(pool) and pool[i] <= b + 2000:
        if prev is not None and pool[i] - prev > 2000: return False
        prev = pool[i]; i += 1
    return prev is not None and prev >= b - 2000 and i > 0 and (bisect.bisect_right(pool, a) > 0 and a - pool[bisect.bisect_right(pool, a) - 1] <= 2000)


def trade(tgt, t, m0, m1, d, fee_rate, need=20.0, check=True):
    """Buy the target venue's ask at t + d; None when the rules refuse."""
    T = [x[0] for x in tgt]
    def at(tt):
        i = bisect.bisect_right(T, tt); return tgt[i - 1] if i else None
    pre = at(t - 10_000); now = at(t + d); real = at(t + d + 300)
    if not pre or not now or not real or t - pre[0] > 180_000: return None
    kpre = (pre[1] + pre[2]) / 2; move = m1 - m0
    if abs(kpre - m0) > 0.05: return None                      # the two venues agreed before
    if ((now[1] + now[2]) / 2 - kpre) / move > 0.20: return None   # already followed
    ask = real[2]; spread = real[2] - real[1]
    if spread > 0.04 + 1e-9 or not (0.05 <= ask <= 0.90): return None
    if check and (real[3] is None or real[3] * ask < need): return None
    fee = lambda p: fee_rate * p * (1 - p)
    target = kpre + move
    if target - spread / 2 - ask - fee(ask) - fee(min(max(target, 0.01), 0.99)) < 0.03: return None
    ex = None; caught = False
    i = bisect.bisect_right(T, t + d + 300)
    while i < len(tgt) and tgt[i][0] <= t + d + 300_000:
        if tgt[i][1] >= target - 1e-9:
            x = at(tgt[i][0] + 500); ex = min(x[1], tgt[i][1]) if x else tgt[i][1]; caught = True; break
        i += 1
    if ex is None:
        x = at(t + d + 300_000)
        if not x or t + d + 300_000 - x[0] > 300_000: return None
        ex = x[1]
    return dict(buy=ask, ex=ex, caught=caught, ret=(ex - ask - fee(ask) - (fee(ex) if 0 < ex < 1 else 0)) / (ask + fee(ask)), end=t + d + 300_000)


def report():
    D = json.load(open(OUT)); G = D['games']; pm_all = D['pm_all']; k_all = D['k_all']
    span = (max(k_all) - min(k_all)) / 86_400_000
    print(f"{len(G)} games on both exchanges with play in the recording ({span:.1f} days); by sport {dict(Counter(g['sport'] for g in G).most_common())}")
    for name, sig_key, tgt_key, pool_sig, pool_tgt, fee in (("A. POLYMARKET JUMPS -> BUY ON KALSHI", 'P', 'K', pm_all, k_all, 0.07), ("B. KALSHI JUMPS -> BUY ON POLYMARKET", 'K', 'P', k_all, pm_all, 0.05)):
        print(f"\n{name}")
        S = []
        for g in G:
            for o, sig in g[sig_key].items():
                if o not in g[tgt_key] or len(g[tgt_key][o]) < 20: continue
                for t, m0, m1 in signals(sig, feed_late_ok=(sig_key == 'K')): S.append((g, o, t, m0, m1))
        S.sort(key=lambda x: x[2])
        ok = [s for s in S if flowing(pool_tgt, s[2] - 5000, s[2] + 5300)]
        print(f"   jumps of 4+ points in 10 s: {len(S)} ({len(S) / span:.0f} a day); with the other venue's feed flowing around them: {len(ok)}")
        for label, pool, check in (("as the rules say (feeds flowing, $20+ resting)", ok, True), ("WITHOUT the gap check (what a careless test shows)", S, True), ("without the size check", ok, False)):
            print(f"   -- {label}")
            for d in DELAYS:
                tr = []; busy = {}; lock = {}
                for g, o, t, m0, m1 in pool:
                    if busy.get(g['m'], 0) > t or t - lock.get(g['m'], -10 ** 15) < 120_000: continue
                    x = trade(g[tgt_key][o], t, m0, m1, d, fee, check=check)
                    if not x: continue
                    lock[g['m']] = t; busy[g['m']] = x['end'] if not x['caught'] else t + d + 60_000
                    x.update(m=g['m'], sport=g['sport'], t=t); tr.append(x)
                if len(tr) < 8: print(f"      order lands {d / 1000:.1f} s after: {len(tr)} trades, too few"); continue
                by = defaultdict(list)
                for x in tr: by[x['m']].append(x['ret'])
                ks = list(by); bs = sorted(st.mean([r for k in (random.choice(ks) for _ in ks) for r in by[k]]) for _ in range(800))
                sp = ' '.join(f"{s}:{len(v)}/{100 * st.mean(v):+.0f}%" for s, v in sorted(((s, [x['ret'] for x in tr if x['sport'] == s]) for s in {x['sport'] for x in tr}), key=lambda z: -len(z[1])) if len(v) >= 5)
                print(f"      order lands {d / 1000:.1f} s after: {len(tr):4d} trades ({len(tr) / span:4.0f} a day), {100 * sum(x['ret'] > 0 for x in tr) / len(tr):3.0f}% won, caught up {100 * sum(x['caught'] for x in tr) / len(tr):3.0f}%, {100 * st.mean(x['ret'] for x in tr):+5.1f}% a $ (90% range {100 * bs[40]:+.1f} to {100 * bs[760]:+.1f}) | {sp}")


if __name__ == '__main__':
    if MODE in ('build', 'all'): build()
    if MODE in ('report', 'all'): report()
