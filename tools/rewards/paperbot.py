"""Paper bot for the liquidity-rewards lead, run on recorder 2's files (24 Nov morning). No orders are placed anywhere.

The bot it imitates: every pass (2 minutes) it looks at every watched reward market and holds the minimum two orders
(a buy of Yes under the price, a buy of No = an offer on Yes over it) in up to K markets where that pair would be
paid the most, leaving a market when others have moved in.

Honest rules:
  - a decision made on the book of pass k is LIVE only from pass k + L (L = reaction delay in passes) and is priced
    from the book it was decided on, so live orders are always L passes stale;
  - the pot share is the published formula at the order's real cent distance from the price of each pass, against
    the other makers at the HIGH bound of their score ((2 min + max) / 3 for a price of 10-90c, min outside);
    paid only for the minutes we are in (the formula itself would pay more when nobody qualifies part of the day);
  - a side FILLS when a real trade printed at or through its price while it was live (the whole order, whatever
    the trade's size), or when the book itself crossed it at the next pass;
  - after a fill the market is left (the other side is cancelled L passes later and can still fill meanwhile) and
    not entered again for 6 hours; the shares are SOLD AT THE BID of the pass after next (the raw best bid, no
    minimum size), paying a taker fee of 5% x p x (1 - p) a share; no bid at all = 10c lost a share.
    Shown beside it: the same fills valued at the mid 1 hour later and at the last mid (holding instead of selling).
    A bought pair (both sides filled) is not netted: each is sold separately (pessimistic).
  - entry: quiet (24-hour volume under VOL), DAYS+ days to the end, price 5-95c, book under 20c wide, expected
    pot share >= ENTER $ a day; exit: expected pot share < EXIT $ a day, or no price to quote.

usage: python paperbot.py [--from HH:MM] [--quick]     (reads rec2_*.json* in this folder; trades are fetched once)
"""
import json, os, sys, time, math, collections, datetime as dt
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
ARGS = sys.argv[1:]
sys.argv = [sys.argv[0]]
import snap as SN

FEE = 0.05
PRE = ARGS[ARGS.index('--prefix') + 1] if '--prefix' in ARGS else 'rec2'


def parse(e):
    e = (e or '').replace(' ', 'T').replace('Z', '+00:00')
    if e.endswith('+00'): e += ':00'
    try:
        d = dt.datetime.fromisoformat(e)
        return (d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)).timestamp()
    except Exception:
        return float('nan')


def load():
    M = json.load(open(os.path.join(HERE, PRE + '_markets.json')))
    N = len(M)
    cond = [None] * N
    for c, m in M.items(): cond[m['idx']] = c
    end = np.array([parse(M[c]['end']) for c in cond]); created = np.array([parse(M[c]['created']) for c in cond])
    lists = [json.loads(l) for l in open(os.path.join(HERE, PRE + '_list.jsonl'))]
    P = [json.loads(l) for l in open(os.path.join(HERE, PRE + '_books.jsonl'))]
    P = [p for p in P if not p.get('bad')]
    T = np.array([p['t'] for p in P], np.int64)
    B = np.full((len(P), N, 7), np.nan, np.float32)           # mid, bb, ba, qb, qa, raw bid, raw ask
    cur = np.full((N, 7), np.nan, np.float32)
    for k, p in enumerate(P):
        for r in p['rows']:
            if r[0] < N: cur[r[0]] = [np.nan if x is None else x for x in r[1:8]]
        for i in p.get('miss', []):
            if i < N: cur[i] = np.nan
        B[k] = cur
    C = np.full((len(P), N, 4), np.nan, np.float32)            # pot a day, min size, max spread, 24h volume
    li = 0; curc = np.full((N, 4), np.nan, np.float32)
    for k in range(len(P)):
        while li < len(lists) and lists[li]['t'] <= T[k] + 90:
            curc = np.full((N, 4), np.nan, np.float32)
            for r in lists[li]['m']:
                if r[0] < N: curc[r[0]] = r[1:5]
            li += 1
        C[k] = curc
    return M, cond, end, created, T, B, C


def quotes(mid, v, d):
    """cent prices of the pair: d = 0 -> the furthest cent that still earns; else the nearest cent at least d cents away (never past the edge)"""
    eb = np.ceil((mid - v / 100) * 100 + 1e-4) / 100
    eb = np.where((mid - eb) * 100 >= v - 1e-4, eb + 0.01, eb)
    ea = np.floor((mid + v / 100) * 100 - 1e-4) / 100
    ea = np.where((ea - mid) * 100 >= v - 1e-4, ea - 0.01, ea)
    if d <= 0: return np.round(eb, 2), np.round(ea, 2)
    b = np.maximum(np.floor((mid - d / 100) * 100 + 1e-4) / 100, eb)
    a = np.minimum(np.ceil((mid + d / 100) * 100 - 1e-4) / 100, ea)
    return np.round(b, 2), np.round(a, 2)


def pair_score(mid, bid, ask, v, n, on_b=True, on_a=True):
    db, da = (mid - bid) * 100, (ask - mid) * 100
    sb = np.where(on_b & (db >= -1e-6) & (db < v), ((v - db) / v) ** 2 * n, 0.0)
    sa = np.where(on_a & (da >= -1e-6) & (da < v), ((v - da) / v) ** 2 * n, 0.0)
    return np.where((mid >= 0.10) & (mid <= 0.90), np.maximum(np.minimum(sb, sa), np.maximum(sb, sa) / 3), np.minimum(sb, sa))


def others(mid, qb, qa, high=True):
    lo, hi = np.minimum(qb, qa), np.maximum(qb, qa)
    inr = (mid >= 0.10) & (mid <= 0.90)
    return np.where(inr, (2 * lo + hi) / 3, lo) if high else np.where(inr, hi / 3, 0.0)


def expected(k, T, B, C, end, d, vol_max, days_min, tight=0.0):
    """expected pot share ($ a day) of the minimum pair in every market on the book of pass k; nan where we would not quote"""
    mid, bb, ba, qb, qa = (B[k, :, j].astype(np.float64) for j in range(5))
    R, ms, v, vol = (C[k, :, j].astype(np.float64) for j in range(4))
    n = np.maximum(ms, 20.0)
    with np.errstate(invalid='ignore', divide='ignore'):
        ok = np.isfinite(mid) & np.isfinite(R) & (mid >= 0.05) & (mid <= 0.95) & (ba - bb <= 0.20 + 1e-9) & (vol < vol_max) & ~((end - T[k]) / 86400 < days_min)
        bid, ask = quotes(mid, v, d)
        ok &= (bid >= 0.01) & (ask <= 0.99)
        if tight > 0: ok &= (B[k, :, 6].astype(np.float64) - B[k, :, 5].astype(np.float64) <= tight + 1e-9)       # a real price: the raw book itself is at most `tight` wide
        q = pair_score(mid, bid, ask, v, n)
        rate = R * q / (q + others(mid, qb, qa))
    return np.where(ok & (q > 0), rate, np.nan), bid, ask, n


def trades_for(idxs, cond, M, t_lo, t_hi):
    path = os.path.join(HERE, PRE + '_trades.json')
    TR = json.load(open(path)) if os.path.exists(path) else {'_t': {}}
    t0 = time.time(); got = 0
    for i in idxs:
        c = cond[i]
        if TR['_t'].get(c, 0) >= t_hi - 600: continue
        yes = M[c]['yes']; rows, cur = [], None
        for _ in range(10):
            x = SN.get(f'https://data-api.polymarket.com/v2/trades?condition={c}&limit=1000' + (f'&cursor={cur}' if cur else '')); time.sleep(0.1)
            dta = x.get('data') or []
            rows += [(int(z['timestamp']), (float(z['price']) if z['token_id'] == yes else 1 - float(z['price'])), (1 if (z['side'] == 'BUY') == (z['token_id'] == yes) else -1), float(z['size'])) for z in dta]
            cur = (x.get('pagination') or {}).get('next_cursor')
            if not dta or not cur or rows[-1][0] < t_lo: break
        if 'error' in x and not rows: continue
        TR[c] = [r for r in rows if r[0] >= t_lo - 60]; TR['_t'][c] = int(time.time()); got += 1
        if got % 100 == 0: print(f'    trades of {got} markets, {time.time() - t0:.0f} s', flush=True)
    json.dump(TR, open(path, 'w'))
    return TR


def simulate(T, B, C, end, cond, TR, K, d, enter, exit_, L, vol_max=300, days_min=3, k0=0, detail=False, tight=0.0):
    NP = len(T)
    held = {}            # idx -> dict(bid, ask, on_b, on_a, nxt=(eff pass, bid, ask) | (eff, None), since)
    cool = {}            # idx -> time until which we stay out
    rew_hi = rew_lo = 0.0
    fills = []           # dicts
    slot_secs = 0.0; entries = 0; hold = []
    by_hour = collections.defaultdict(lambda: [0.0, 0.0, 0.0])        # hour -> reward, fills A, slot secs
    by_mkt = collections.defaultdict(lambda: [0.0, 0.0])                # market -> reward, live seconds
    tr_idx = {}
    def tr_of(i):
        if i not in tr_idx: tr_idx[i] = sorted(TR.get(cond[i], []))
        return tr_idx[i]
    for k in range(k0, NP - 1):
        t_a, t_b = int(T[k]), int(T[k + 1])
        if t_b - t_a > 1500:                                           # a hole in the recording: nothing is assumed over it, all orders pulled
            for i, h in list(held.items()): hold.append((T[k] - h['since']) / 3600)
            held.clear(); continue
        rate, bid, ask, n = expected(k, T, B, C, end, d, vol_max, days_min, tight)
        # 1. decisions on the book of pass k (effective at pass k + L)
        for i, h in held.items():
            if h.get('leaving'): continue
            if not np.isfinite(rate[i]) or rate[i] < exit_:
                h['nxt'] = (k + L, None); h['leaving'] = True
            elif h['bid'] is None:
                continue                                             # an entry not live yet
            elif (abs(bid[i] - h['bid']) > 1e-9 or abs(ask[i] - h['ask']) > 1e-9):
                h['nxt'] = (k + L, float(bid[i]), float(ask[i]))
        free = K - len(held)
        if free > 0:
            cand = [i for i in np.argsort(-np.nan_to_num(rate, nan=-1.0))[:free + len(held) + 50] if np.isfinite(rate[i]) and rate[i] >= enter and i not in held and cool.get(i, 0) <= t_a]
            for i in cand[:free]:
                held[i] = dict(bid=None, ask=None, on_b=False, on_a=False, nxt=(k + L, float(bid[i]), float(ask[i])), since=t_a, n=float(n[i]))
                entries += 1
        # 2. apply what becomes effective now
        for i in list(held):
            h = held[i]
            if h.get('nxt') and h['nxt'][0] <= k:
                if h['nxt'][1] is None:
                    hold.append((t_a - h['since']) / 3600); del held[i]; continue
                h['bid'], h['ask'] = h['nxt'][1], h['nxt'][2]
                if not h.get('leaving'): h['on_b'] = h['on_a'] = True
                h['nxt'] = None
        # 3. the interval [t_a, t_b): rewards and fills on the live orders
        mid, qb, qa = B[k, :, 0].astype(np.float64), B[k, :, 3].astype(np.float64), B[k, :, 4].astype(np.float64)
        mid2, rb2, ra2 = B[k + 1, :, 0], B[k + 1, :, 5], B[k + 1, :, 6]
        for i in list(held):
            h = held[i]
            if h['bid'] is None or not (h['on_b'] or h['on_a']): continue
            slot_secs += t_b - t_a
            by_mkt[int(i)][1] += t_b - t_a
            by_hour[(t_a // 3600)][2] += t_b - t_a
            R, v = float(C[k, i, 0]), float(C[k, i, 2])
            if np.isfinite(mid[i]) and np.isfinite(R) and np.isfinite(v):
                q = float(pair_score(np.array(mid[i]), np.array(h['bid']), np.array(h['ask']), v, h['n'], np.array(h['on_b']), np.array(h['on_a'])))
                if q > 0:
                    oh, ol = float(others(np.array(mid[i]), np.array(qb[i]), np.array(qa[i]))), float(others(np.array(mid[i]), np.array(qb[i]), np.array(qa[i]), high=False))
                    g = R * (t_b - t_a) / 86400
                    rew_hi += g * q / (q + oh); rew_lo += g * q / (q + ol)
                    by_hour[(t_a // 3600)][0] += g * q / (q + oh)
                    by_mkt[int(i)][0] += g * q / (q + oh)
            tr = tr_of(i)
            hit_b = h['on_b'] and (any(t_a <= t[0] < t_b and t[2] < 0 and t[1] <= h['bid'] + 1e-9 for t in tr) or (np.isfinite(ra2[i]) and ra2[i] <= h['bid'] + 1e-9))
            hit_a = h['on_a'] and (any(t_a <= t[0] < t_b and t[2] > 0 and t[1] >= h['ask'] - 1e-9 for t in tr) or (np.isfinite(rb2[i]) and rb2[i] >= h['ask'] - 1e-9))
            for side, hit, px in ((1, hit_b, h['bid']), (-1, hit_a, h['ask'])):
                if not hit: continue
                if side > 0: h['on_b'] = False
                else: h['on_a'] = False
                # sold at the bid of the pass after next (we learn of the fill at pass k + 1, the sale lands a pass later)
                ks = min(k + 2, NP - 1)
                rb, ra = float(B[ks, i, 5]), float(B[ks, i, 6])
                if side > 0: out = rb if np.isfinite(rb) else px - 0.10
                else: out = ra if np.isfinite(ra) else px + 0.10
                fee = FEE * out * (1 - out)
                a_pnl = (side * (out - px) - fee) * h['n']
                m1 = [float(B[j, i, 0]) for j in range(k + 1, NP) if T[j] >= t_a + 3600 and np.isfinite(B[j, i, 0])][:1]
                ml = [float(B[j, i, 0]) for j in range(NP - 1, k, -1) if np.isfinite(B[j, i, 0])][:1]
                fills.append(dict(i=int(i), t=t_a, side=side, px=px, out=out, a=a_pnl, b1=(side * (m1[0] - px) * h['n']) if m1 else None, bl=(side * (ml[0] - px) * h['n']) if ml else None, n=h['n']))
                by_hour[(t_a // 3600)][1] += a_pnl
                cool[i] = t_a + 6 * 3600
                if not h.get('leaving'):
                    h['leaving'] = True; h['nxt'] = (k + 1 + L, None)
    hours = (T[NP - 1] - T[k0]) / 3600
    fa = sum(f['a'] for f in fills)
    res = dict(K=K, d=d, enter=enter, exit=exit_, L=L, hours=hours, slots=slot_secs / 3600 / max(hours, 1e-9), rew_hi=rew_hi, rew_lo=rew_lo, nf=len(fills), fa=fa,
               fb1=sum(f['b1'] for f in fills if f['b1'] is not None), fbl=sum(f['bl'] for f in fills if f['bl'] is not None), entries=entries,
               hold=float(np.median(hold)) if hold else float('nan'), net_day=(rew_hi + fa) / max(hours, 1e-9) * 24, markets=len({f['i'] for f in fills}))
    if detail: res['fills'] = fills; res['by_hour'] = {int(h): v for h, v in by_hour.items()}; res['by_mkt'] = dict(by_mkt)
    return res


def main():
    M, cond, end, created, T, B, C = load()
    k0 = 0
    if '--from' in ARGS:
        hh, mm = ARGS[ARGS.index('--from') + 1].split(':')
        day = dt.datetime.fromtimestamp(int(T[0]))
        t_from = day.replace(hour=int(hh), minute=int(mm), second=0).timestamp()
        k0 = int(np.searchsorted(T, t_from))
    hours = (T[-1] - T[k0]) / 3600
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    say(f'{PRE}: {len(T) - k0} passes over {hours:.1f} hours ({dt.datetime.fromtimestamp(int(T[k0])).strftime("%d %b %H:%M")} to {dt.datetime.fromtimestamp(int(T[-1])).strftime("%H:%M")}), {len(cond)} markets known')
    # which markets could ever be held (so only their trades are fetched)
    want = set()
    for k in range(k0, len(T) - 1, 1):
        for d in (0, 3.0, 2.0):
            rate = expected(k, T, B, C, end, d, 300, 3)[0]
            want |= set(np.flatnonzero(np.nan_to_num(rate, nan=0.0) >= 1.0).tolist())
    say(f'markets that ever offer $1+ a day of pot share to the minimum pair: {len(want)}')
    TR = trades_for(sorted(want), cond, M, int(T[k0]), int(T[-1]))
    say('all $ are for the whole recorded window unless marked "a day"; fills A = sold at the bid two passes later with a 5% x p(1-p) fee; B = held, valued at the mid 1 h later / at the last mid')
    step = float(np.median(np.diff(T[k0:]))) / 60
    say(f'  {"slots":>5} {"dist":>5} {"book":>6} {"in/out $":>8} {"delay":>6} | used slots | entries | hold h | pot share $ | fills | fills A $ | fills B $ 1h / last | NET $ a day: A / B last | a slot-day: A / B last | fills a slot-day')
    Ls = sorted({max(1, round(m / step)) for m in (2, 10, 30, 60)})          # reaction / waiting time before the orders go live: 2, 10, 30, 60 minutes
    grid = [(K, d, tg, e, x, L) for K in (2, 10, 50) for d in (0, 3.0) for tg in (0.0, 0.10) for (e, x) in ((3, 1),) for L in Ls]
    if '--quick' in ARGS: grid = [(2, 3.0, 0.0, 3, 1, 1), (10, 3.0, 0.0, 3, 1, 1), (50, 3.0, 0.0, 3, 1, 1), (10, 0, 0.0, 3, 1, 1), (10, 3.0, 0.10, 3, 1, 1)]
    for K, d, tg, e, x, L in grid:
        r = simulate(T, B, C, end, cond, TR, K, d, e, x, L, k0=k0, tight=tg)
        sd = max(r['slots'] * r['hours'] / 24, 1e-9); hd = max(r['hours'], 1e-9) / 24
        say(f'  {K:>5} {("edge" if d == 0 else f"{d:g}c"):>5} {("any" if tg == 0 else f"<={tg * 100:.0f}c"):>6} {f"{e:g}/{x:g}":>8} {L * step:>4.0f}min | {r["slots"]:>9.1f}  | {r["entries"]:>7} | {r["hold"]:>6.1f} | {r["rew_hi"]:>8.2f}    | {r["nf"]:>5} | {r["fa"]:>+9.2f} | {r["fb1"]:>+8.2f} / {r["fbl"]:>+8.2f}  '
            f'| {(r["rew_hi"] + r["fa"]) / hd:>+9.2f} / {(r["rew_hi"] + r["fbl"]) / hd:>+9.2f} | {(r["rew_hi"] + r["fa"]) / sd:>+8.2f} / {(r["rew_hi"] + r["fbl"]) / sd:>+8.2f}  | {r["nf"] / sd:5.1f}')
    r = simulate(T, B, C, end, cond, TR, 10, 3.0, 3, 1, 1, k0=k0, detail=True)
    say('\n10 slots, 3c, any book, enter 3 / exit 1, one pass of delay, by hour (Norway time): pot share $ | fills A $ | slots in use')
    for h in sorted(r['by_hour']):
        v = r['by_hour'][h]
        say(f'   {dt.datetime.fromtimestamp(h * 3600).strftime("%d %b %H:00")}  {v[0]:6.2f} | {v[1]:+7.2f} | {v[2] / 3600:4.1f}')
    say('its fills (time, market, side, our price, sold at, result A, held 1 h, held to the last price):')
    for f in r['fills'][:60]:
        say(f"   {dt.datetime.fromtimestamp(f['t']).strftime('%H:%M')} {'bought Yes' if f['side'] > 0 else 'bought No '} at {f['px'] * 100 if f['side'] > 0 else (1 - f['px']) * 100:4.0f}c, sold {f['out'] * 100 if f['side'] > 0 else (1 - f['out']) * 100:4.0f}c: A ${f['a']:+6.2f} | 1 h ${(f['b1'] if f['b1'] is not None else float('nan')):+6.2f} | last ${(f['bl'] if f['bl'] is not None else float('nan')):+6.2f} | {M[cond[f['i']]]['q'][:56]}")
    open(os.path.join(HERE, f'paperbot_{PRE}.txt'), 'w').write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
