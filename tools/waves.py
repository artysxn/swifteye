"""Waves: split the bookmakers' line and Polymarket's line of a game into legs (bursts up and down), pair each
bookmaker leg with Polymarket's leg of the same direction, and judge every trade the bot made by its wave.

Owner, 21 Nov 2026 (on real trade #120): "Every chart I look at has some correlation in the two lines ... The wallet
buys correctly, but skips the wave and just holds until it catches up even though there's no telling if it will."

Everything is in OUR SIDE's terms (up = good for the side bought): Polymarket = what our shares can be sold for
(bid) and bought for (ask); a bookmaker = its implied chance of our side (1/odds, margin left in).

usage: waves.py trades            every buy the bot made: its wave, where it was bought, where it peaked, the class
       waves.py couple            how the two markets move together in the hour before each buy
       waves.py exits             sale rules repriced in dollars on the same buys
       waves.py html <file>       one page with a small chart per trade, legs marked
       waves.py one <trade id>    the legs of one trade, as text
"""
import sqlite3, json, os, sys, time, bisect, statistics as st, collections

DB = os.path.expanduser('~/swifteye/oddsfeed.db')
TH_PM, TH_BK = 0.03, 0.04          # a leg ends when the line turns back by this much
PRE_S, POST_S = 600, 240           # window around a trade
DELAY_S = 2                        # a sale ordered now is filled this much later
FAM = {'linebet': '1xbet', 'melbet': '1xbet', 'betwinner': '1xbet', 'megapari': '1xbet', 'paripesa': '1xbet', '22bet': '1xbet', '22bit': '1xbet', '1xbit': '1xbet',
       'unibet': 'kambi', 'leovegas': 'kambi', 'paf': 'kambi', 'fonbet': 'fonbet', 'pari': 'fonbet', 'mostbet': 'fonbet', 'vinn': 'altenar', 'betking': 'altenar'}
fam = lambda b: FAM.get(b, b)
fee = lambda p: 0.05 * p * (1 - p) if 0 < p < 1 else 0.0


def conn():
    return sqlite3.connect('file:' + DB + '?mode=ro', uri=True)


def trades(db, since_ms=0):
    rows = db.execute("SELECT id, bot, real, ts_ms, closed_ms, match_id, data FROM bot_trades WHERE bot IN ('snipe','snipe-trial') AND closed_ms IS NOT NULL AND ts_ms >= ? ORDER BY real DESC, id", (since_ms,)).fetchall()
    seen, out = set(), []
    for id_, bot, real, ts, closed, mid, data in rows:
        d = json.loads(data)
        if d.get('move_ms') is None or d.get('entry') is None or d.get('exit') is None: continue
        k = (mid, d['outcome'], d['move_ms'])
        if k in seen: continue
        seen.add(k)
        out.append(dict(id=id_, bot=bot, real=real, ts=ts, closed=closed, mid=mid, d=d))
    return sorted(out, key=lambda t: t['ts'])


def grid(db, mid, oc, dr, t0, t1, trigger=None, clock='ours'):
    """1-second series over [t0, t1]: Polymarket bid/ask/mid of our side, the trigger bookmaker, the consensus
    (median over bookmaker groups of each group's mean), and how many groups are priced."""
    n = int((t1 - t0) // 1000) + 1
    if clock == 'server':    # Polymarket's own clock: a price that reached us late still counts at the moment it was made
        q = sorted(db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms BETWEEN ? AND ? ORDER BY id", (mid, oc, t0 - 180000, t1 + 60000)).fetchall(), key=lambda x: x[0])
    else:
        q = db.execute("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms BETWEEN ? AND ? ORDER BY id", (mid, oc, t0 - 180000, t1)).fetchall()
    bid, ask = [None] * n, [None] * n
    j, cb, ca = 0, None, None
    for i in range(n):
        t = t0 + i * 1000
        while j < len(q) and q[j][0] <= t:
            b, a = q[j][1], q[j][2]
            if b is not None and a is not None and b > 0 and a > 0:
                cb, ca = (b, a) if dr > 0 else (1 - a, 1 - b)
            j += 1
        bid[i], ask[i] = cb, ca
    ch = db.execute("SELECT ts_ms, book, kind, new_price FROM changes WHERE match_id=? AND market='1x2' AND outcome=? AND ts_ms BETWEEN ? AND ? AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms", (mid, oc, t0 - 1800000, t1)).fetchall()
    state, trig, cons, nf = {}, [None] * n, [None] * n, [0] * n
    j = 0
    for i in range(n):
        t = t0 + i * 1000
        while j < len(ch) and ch[j][0] <= t:
            _, b, kind, p = ch[j]
            state[b] = ((1.0 / p) if dr > 0 else (1 - 1.0 / p)) if p and p > 1.0 else None
            j += 1
        f = collections.defaultdict(list)
        for b, v in state.items():
            if v is not None: f[fam(b)].append(v)
        if f:
            cons[i] = st.median(sum(v) / len(v) for v in f.values()); nf[i] = len(f)
        if trigger: trig[i] = state.get(trigger)
    mid_ = [(b + a) / 2 if b is not None and a is not None else None for b, a in zip(bid, ask)]
    return dict(t0=t0, n=n, bid=bid, ask=ask, mid=mid_, trig=trig, cons=cons, nf=nf)


def legs(x, th):
    """Zigzag: turning points of a line, a turn counted once the line comes back by `th`.
    Returns legs as dicts: i0, v0 (start), i1, v1 (end), b0/b1 = the burst inside the leg (10% to 90% of its size)."""
    idx = [i for i, v in enumerate(x) if v is not None]
    if len(idx) < 3: return []
    piv = [(idx[0], x[idx[0]])]; d = 0; ei, ev = idx[0], x[idx[0]]
    for i in idx[1:]:
        v = x[i]
        if d == 0:
            if v - piv[0][1] >= th: d = 1; ei, ev = i, v
            elif piv[0][1] - v >= th: d = -1; ei, ev = i, v
            continue
        if d > 0:
            if v > ev: ei, ev = i, v
            elif ev - v >= th: piv.append((ei, ev)); d = -1; ei, ev = i, v
        else:
            if v < ev: ei, ev = i, v
            elif v - ev >= th: piv.append((ei, ev)); d = 1; ei, ev = i, v
    if d != 0: piv.append((ei, ev))
    # The first pivot is where the window opens, not a real turn: move it to the extreme before the first leg ends.
    out = []
    for (i0, v0), (i1, v1) in zip(piv, piv[1:]):
        if i1 <= i0: continue
        size = v1 - v0
        # the leg really starts at the LAST moment the line was still at its start level
        s = i0
        for k in range(i0, i1 + 1):
            if x[k] is not None and (x[k] - v0) * (1 if size > 0 else -1) <= 0.1 * abs(size): s = k
            elif x[k] is not None and (x[k] - v0) * (1 if size > 0 else -1) >= 0.5 * abs(size): break
        e = i1
        for k in range(s, i1 + 1):
            if x[k] is not None and (x[k] - v0) * (1 if size > 0 else -1) >= 0.9 * abs(size): e = k; break
        out.append(dict(i0=i0, v0=v0, i1=i1, v1=v1, size=size, b0=s, b1=e))
    return out


def pair(bk, pm, before=120, after=120):
    """Pair each bookmaker leg with the Polymarket leg of the same direction nearest in time (bursts within
    `before` s earlier to `after` s later). One Polymarket leg serves one bookmaker leg (the biggest)."""
    used, out = set(), []
    for B in sorted(bk, key=lambda l: -abs(l['size'])):
        best = None
        for k, P in enumerate(pm):
            if k in used or P['size'] * B['size'] <= 0: continue
            if P['b1'] < B['b0'] - before or P['b0'] > B['b1'] + after: continue
            gap = abs((P['b0'] + P['b1']) / 2 - (B['b0'] + B['b1']) / 2)
            if best is None or gap < best[0]: best = (gap, k)
        if best: used.add(best[1])
        out.append((B, pm[best[1]] if best else None))
    return sorted(out, key=lambda x: x[0]['b0'])


def pnl12(d, exit_px):
    e = d['entry']; n = 12.0 / e
    return n * (exit_px - e) - n * fee(e) - n * fee(exit_px)


def analyse(db, tr):
    d = tr['d']; dr = d.get('dir', 1.0); oc = d['outcome']
    t0 = tr['ts'] - PRE_S * 1000; t1 = min(tr['closed'], tr['ts'] + 900000) + POST_S * 1000
    g = grid(db, tr['mid'], oc, dr, t0, t1, d.get('trigger'))
    ib = int((tr['ts'] - t0) // 1000); im = max(0, int((d['move_ms'] - t0) // 1000)); ic = min(g['n'] - 1, int((tr['closed'] - t0) // 1000))
    Lpm, Ltr, Lco = legs(g['mid'], TH_PM), legs(g['trig'], TH_BK), legs(g['cons'], TH_BK)
    pre = d.get('pre_mid'); pre_our = pre if dr > 0 else (1 - pre if pre is not None else None)
    need = d.get('move', 0) * d.get('catch', 1.0); sp = d.get('spread', 0.0)
    target = (pre_our + need - sp / 2) if pre_our is not None else None
    # the bookmaker leg that set the trade off: the up-leg of the trigger's line around the move
    def around(L, i, slack=6):
        c = [l for l in L if l['size'] > 0 and l['i0'] - slack <= i <= l['i1'] + slack]
        return max(c, key=lambda l: l['size']) if c else None
    B = around(Ltr, im) or around(Lco, im)
    # Polymarket's wave: its up-leg that holds the buy, or the first one starting within 90 s after the move
    c = [l for l in Lpm if l['size'] > 0 and l['i1'] >= im - 5 and l['i0'] <= ib + 90]
    P = None
    if c:
        hold = [l for l in c if l['i0'] <= ib <= l['i1']]
        P = hold[0] if hold else min(c, key=lambda l: abs(l['i0'] - ib))
    r = dict(tr=tr, g=g, Lpm=Lpm, Ltr=Ltr, Lco=Lco, B=B, P=P, ib=ib, im=im, ic=ic, target=target, pre_our=pre_our)
    e = d['entry']
    bid = g['bid']
    if P is None:
        r['cls'] = 'no wave'; r['peak'] = max([b for b in bid[ib:ic + 1] if b is not None] or [e]); r['ipeak'] = ib
    else:
        hi_end = max(P['i1'], ib)
        seg = [(bid[k], k) for k in range(ib, min(hi_end, g['n'] - 1) + 1) if bid[k] is not None]
        pk, ipk = max(seg) if seg else (e, ib)
        # earliest time of that peak
        for v, k in seg:
            if v >= pk - 1e-9: ipk = k; break
        r['peak'], r['ipeak'] = pk, ipk
        if P['i1'] <= ib + 2: r['cls'] = 'bought after the wave'
        elif target is not None and pk >= target - 1e-9: r['cls'] = 'wave reached the target'
        else: r['cls'] = 'wave ended short'
    r['ratio'] = (P['size'] / B['size']) if (P and B and B['size'] > 0) else None
    r['lead'] = (P['b0'] - B['b0']) if (P and B) else None          # seconds; negative = Polymarket started first
    r['endlag'] = (P['i1'] - B['i1']) if (P and B) else None        # seconds; positive = Polymarket's wave ended later
    r['done_at_buy'] = ((g['mid'][ib] - P['v0']) / P['size']) if (P and g['mid'][ib] is not None and P['size'] > 0) else None
    r['base'] = pnl12(d, d['exit'])
    return r


def sim_exit(r, rule):
    """Reprice the trade with another sale rule; the bot's own sale stands if the rule never fires first."""
    tr, g = r['tr'], r['g']; d = tr['d']; e = d['entry']; bid = g['bid']; ib, ic = r['ib'], r['ic']
    peak = None; last_new_high = ib
    for k in range(ib + 2, ic - DELAY_S):
        b = bid[k]
        if b is None: continue
        if peak is None or b > peak: peak, last_new_high = b, k
        if rule(r, k, b, peak, k - last_new_high):
            kk = min(k + DELAY_S, g['n'] - 1); x = bid[kk] if bid[kk] is not None else b
            return pnl12(d, x), k - ib
    return r['base'], None


def gave_back(r, k, of='trig'):
    """Share of the bookmaker leg's size that the bookmaker line has given back by second k."""
    B = r['B']; x = r['g'][of]
    if not B or x[k] is None: return None
    top = max(v for v in x[B['i0']:k + 1] if v is not None)
    return (top - x[k]) / B['size'] if B['size'] > 0 else None


def fmt_t(g, i): return time.strftime('%H:%M:%S', time.localtime((g['t0'] + i * 1000) / 1000))


def cmd_one(db, tid):
    T = [t for t in trades(db) if t['id'] == tid]
    if not T: print('no such closed snipe trade'); return
    r = analyse(db, T[0]); g = r['g']; d = T[0]['d']
    print('#%d %s %s v %s | bought %s at %.1fc, bot sold %.1fc (%s) | target %.1fc' % (tid, d.get('sport'), d.get('home'), d.get('away'), fmt_t(g, r['ib']), d['entry'] * 100, d['exit'] * 100, d.get('reason'), (r['target'] or 0) * 100))
    for name, L in (('trigger bookmaker ' + str(d.get('trigger')), r['Ltr']), ('all bookmakers', r['Lco']), ('Polymarket', r['Lpm'])):
        print(' ' + name)
        for l in L: print('   %s %s -> %s  %5.1fc -> %5.1fc (%+5.1f)  burst %s-%s' % ('UP  ' if l['size'] > 0 else 'DOWN', fmt_t(g, l['i0']), fmt_t(g, l['i1']), l['v0'] * 100, l['v1'] * 100, l['size'] * 100, fmt_t(g, l['b0']), fmt_t(g, l['b1'])))
    print(' class:', r['cls'], '| wave ratio', None if r['ratio'] is None else round(r['ratio'], 2), '| Polymarket started', r['lead'], 's after the bookmaker | ended', r['endlag'], 's after | peak bid %.1fc at %s' % (r['peak'] * 100, fmt_t(g, r['ipeak'])))


def cmd_trades(db):
    R = [analyse(db, t) for t in trades(db)]
    print('%-4s %-5s %-10s %-4s %-24s %6s %6s %6s %6s %5s %6s %6s %7s %-9s %7s' % ('id', 'day', 'sport', 'real', 'class', 'entry', 'peak', 'target', 'sold', 'ratio', 'lead', 'endlag', 'peak s', 'bot', '$12'))
    for r in R:
        t = r['tr']; d = t['d']
        print('%-4d %-5s %-10s %-4s %-24s %6.1f %6.1f %6s %6.1f %5s %6s %6s %7d %-9s %+7.2f' % (t['id'], time.strftime('%d %H', time.localtime(t['ts'] / 1000)), (d.get('sport') or '')[:10], 'REAL' if t['real'] else 'pap', r['cls'], d['entry'] * 100, r['peak'] * 100,
              ('%.1f' % (r['target'] * 100)) if r['target'] else '-', d['exit'] * 100, ('%.2f' % r['ratio']) if r['ratio'] is not None else '-', r['lead'] if r['lead'] is not None else '-', r['endlag'] if r['endlag'] is not None else '-', r['ipeak'] - r['ib'], (d.get('reason') or '')[:9], r['base']))
    print()
    by = collections.defaultdict(list)
    for r in R: by[r['cls']].append(r)
    for k, v in sorted(by.items(), key=lambda x: -len(x[1])):
        won = sum(1 for r in v if r['base'] > 0); caught = sum(1 for r in v if r['tr']['d'].get('reason') == 'caught up')
        print('%-26s %2d trades | bot sold at its target %2d | won %2d | $%+7.2f at $12 | at the wave\'s top instead $%+7.2f' % (k, len(v), caught, won, sum(r['base'] for r in v), sum(pnl12(r['tr']['d'], r['peak']) for r in v)))
    rt = [r['ratio'] for r in R if r['ratio'] is not None]
    ld = [r['lead'] for r in R if r['lead'] is not None]; el = [r['endlag'] for r in R if r['endlag'] is not None]
    db_ = [r['done_at_buy'] for r in R if r['done_at_buy'] is not None]
    if rt: print('\nPolymarket\'s wave as a share of the bookmaker\'s move: median %.2f, quarter %.2f, three quarters %.2f (n %d); under half: %d, the whole move or more: %d' % (st.median(rt), sorted(rt)[len(rt) // 4], sorted(rt)[3 * len(rt) // 4], len(rt), sum(1 for x in rt if x < 0.5), sum(1 for x in rt if x >= 1.0)))
    if ld: print('Polymarket\'s wave started before the bookmaker\'s: %d of %d (median %d s); ended after the bookmaker\'s: %d of %d (median %d s)' % (sum(1 for x in ld if x < 0), len(ld), st.median(ld), sum(1 for x in el if x > 0), len(el), st.median(el)))
    if db_: print('share of Polymarket\'s wave already done when the bot bought: median %.2f' % st.median(db_))
    return R


def history(db, tr, minutes=60):
    """Legs of both markets in the hour BEFORE the buy (nothing after it), paired."""
    d = tr['d']; dr = d.get('dir', 1.0)
    t1 = d['move_ms'] - 15000; t0 = t1 - minutes * 60000
    g = grid(db, tr['mid'], d['outcome'], dr, t0, t1)
    return g, pair(legs(g['cons'], TH_BK), legs(g['mid'], TH_PM))


def cmd_couple(db):
    allp = []; per = []
    for t in trades(db):
        g, pr = history(db, t)
        m = [(B, P) for B, P in pr if P]
        allp += [(t, B, P) for B, P in pr]
        if pr:
            per.append((t, len(pr), len(m), st.median([abs(P['size'] / B['size']) for B, P in m]) if m else None, st.median([P['b0'] - B['b0'] for B, P in m]) if m else None))
    n = len(allp); m = [(t, B, P) for t, B, P in allp if P]
    print('bookmaker legs of %.0fc+ in the hour before the %d buys: %d | Polymarket made a leg the same way: %d (%.0f%%)' % (TH_BK * 100, len(per), n, len(m), 100.0 * len(m) / max(n, 1)))
    rt = sorted(abs(P['size'] / B['size']) for t, B, P in m); ld = sorted(P['b0'] - B['b0'] for t, B, P in m); el = sorted(P['i1'] - B['i1'] for t, B, P in m)
    q = lambda v, f: v[min(len(v) - 1, int(len(v) * f))]
    print('size of Polymarket\'s leg against the bookmakers\': quarter %.2f, median %.2f, three quarters %.2f' % (q(rt, .25), q(rt, .5), q(rt, .75)))
    print('Polymarket\'s leg STARTS: %.0f%% before the bookmakers\' (median of all %+d s; quarter %+d s, three quarters %+d s)' % (100.0 * sum(1 for x in ld if x < 0) / len(ld), q(ld, .5), q(ld, .25), q(ld, .75)))
    print('Polymarket\'s leg ENDS:   %.0f%% after the bookmakers\' (median %+d s; quarter %+d s, three quarters %+d s)' % (100.0 * sum(1 for x in el if x > 0) / len(el), q(el, .5), q(el, .25), q(el, .75)))
    bs = collections.defaultdict(list)
    for t, B, P in m: bs[t['d'].get('sport')].append((abs(P['size'] / B['size']), P['b0'] - B['b0']))
    for s, v in sorted(bs.items(), key=lambda x: -len(x[1])):
        print('  %-10s pairs %3d | size ratio median %.2f | Polymarket first %.0f%% | median start %+d s' % (s, len(v), st.median(x[0] for x in v), 100.0 * sum(1 for x in v if x[1] < 0) / len(v), st.median(x[1] for x in v)))
    return per


RULES = None
def rules():
    R = collections.OrderedDict()
    for k in (0.3, 0.5, 0.75):
        R['target at %d%% of the bookmaker move' % (k * 100)] = (lambda k: lambda r, i, b, pk, idle: r['pre_our'] is not None and b >= r['pre_our'] + k * r['tr']['d'].get('move', 0) - r['tr']['d'].get('spread', 0) / 2 and b > r['tr']['d']['entry'])(k)
    for th in (0.02, 0.03):
        R['wave ended: bid %dc off its high' % (th * 100)] = (lambda th: lambda r, i, b, pk, idle: pk - b >= th - 1e-9)(th)
        R['wave ended (%dc off) once the bookmaker gave back a quarter' % (th * 100)] = (lambda th: lambda r, i, b, pk, idle: pk - b >= th - 1e-9 and (gave_back(r, i) or 0) >= 0.25)(th)
        R['wave ended (%dc off) while in profit' % (th * 100)] = (lambda th: lambda r, i, b, pk, idle: pk - b >= th - 1e-9 and pk >= r['tr']['d']['entry'] + 0.03)(th)
    for n in (20, 40):
        R['burst over: no new high for %d s, bid above the buy' % n] = (lambda n: lambda r, i, b, pk, idle: idle >= n and pk > r['tr']['d']['entry'] + 0.01)(n)
        R['burst over (%d s) once the bookmaker gave back a quarter' % n] = (lambda n: lambda r, i, b, pk, idle: idle >= n and pk > r['tr']['d']['entry'] + 0.01 and (gave_back(r, i) or 0) >= 0.25)(n)
    R['bookmaker gave back half -> sell now'] = lambda r, i, b, pk, idle: (gave_back(r, i) or 0) >= 0.5
    return R


def cmd_exits(db, R=None):
    R = R or [analyse(db, t) for t in trades(db)]
    base = sum(r['base'] for r in R)
    print('\nsame %d buys, $12 each, both fees; a sale is filled %d s after the rule fires' % (len(R), DELAY_S))
    print('%-62s %4s %6s %5s %9s %9s  %s' % ('rule', 'fire', 'better', 'worse', 'total', 'change', 'change by day'))
    print('%-62s %4s %6s %5s %+9.2f %9s  won %d' % ('as the bot sold (target = the whole move, 5 min give-up)', '', '', '', base, '', sum(1 for r in R if r['base'] > 0)))
    days = sorted(set(time.strftime('%d', time.localtime(r['tr']['ts'] / 1000)) for r in R))
    for name, rule in rules().items():
        res = [sim_exit(r, rule) for r in R]
        tot = sum(x[0] for x in res); fired = [(x, r) for x, r in zip(res, R) if x[1] is not None]
        bd = {dy: sum(x[0] - r['base'] for x, r in zip(res, R) if time.strftime('%d', time.localtime(r['tr']['ts'] / 1000)) == dy) for dy in days}
        print('%-62s %4d %6d %5d %+9.2f %+9.2f  %s | won %d' % (name, len(fired), sum(1 for x, r in fired if x[0] > r['base'] + 1e-9), sum(1 for x, r in fired if x[0] < r['base'] - 1e-9), tot, tot - base, ' '.join('%s:%+.1f' % (k, v) for k, v in bd.items()), sum(1 for x in res if x[0] > 0)))
    print('%-62s %4s %6s %5s %+9.2f %+9.2f' % ('for scale: sold at the very top of the wave (hindsight)', '', '', '', sum(pnl12(r['tr']['d'], r['peak']) for r in R), sum(pnl12(r['tr']['d'], r['peak']) for r in R) - base))


# ---------------------------------------------------------------- the page
def svg(r, W=760, H=300):
    g, tr = r['g'], r['tr']; d = tr['d']
    a = max(0, r['ib'] - 300); b = min(g['n'] - 1, r['ic'] + 150)
    vals = [v for s in ('bid', 'ask', 'trig', 'cons') for v in g[s][a:b + 1] if v is not None] + [d['entry'], d['exit']] + ([r['target']] if r['target'] else [])
    lo, hi = min(vals) - 0.02, max(vals) + 0.02
    X = lambda i: 46 + (i - a) / max(b - a, 1) * (W - 60); Y = lambda v: 16 + (hi - v) / (hi - lo) * (H - 52)
    def line(s, col, w, op=1.0):
        pts, seg = [], []
        for i in range(a, b + 1):
            v = g[s][i]
            if v is None:
                if len(seg) > 1: pts.append(seg)
                seg = []
            else: seg.append('%.1f,%.1f' % (X(i), Y(v)))
        if len(seg) > 1: pts.append(seg)
        return ''.join('<polyline fill="none" stroke="%s" stroke-width="%s" opacity="%s" stroke-linejoin="round" points="%s"/>' % (col, w, op, ' '.join(p)) for p in pts)
    def marks(L, s):
        o = ''
        for l in L:
            if l['i1'] < a or l['i0'] > b or abs(l['size']) < 0.0: continue
            i0, i1 = max(l['b0'], a), min(l['b1'], b)
            v0 = g[s][i0] if g[s][i0] is not None else l['v0']; v1 = g[s][i1] if g[s][i1] is not None else l['v1']
            o += '<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="7" stroke-linecap="round" opacity="0.55"/>' % (X(i0), Y(v0), X(i1), Y(v1), '#16a34a' if l['size'] > 0 else '#dc2626')
        return o
    o = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" role="img">' % (W, H), '<rect width="%d" height="%d" fill="var(--card)"/>' % (W, H)]
    step = 0.05 if hi - lo < 0.45 else 0.10; v = (int(lo / step) + 1) * step
    while v < hi:
        o.append('<line x1="46" x2="%d" y1="%.1f" y2="%.1f" stroke="var(--grid)"/><text x="40" y="%.1f" text-anchor="end" font-size="11" fill="var(--mute)">%d</text>' % (W - 14, Y(v), Y(v), Y(v) + 4, round(v * 100))); v += step
    for i in range(a, b + 1):
        tt = g['t0'] + i * 1000
        if (tt // 1000) % 120 == 0: o.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="11" fill="var(--mute)">%s</text>' % (X(i), H - 20, time.strftime('%H:%M', time.localtime(tt / 1000))))
    if r['target']: o.append('<line x1="46" x2="%d" y1="%.1f" y2="%.1f" stroke="#16a34a" stroke-dasharray="6 4" opacity="0.7"/>' % (W - 14, Y(r['target']), Y(r['target'])))
    o.append(marks(r['Ltr'] or r['Lco'], 'trig' if r['Ltr'] else 'cons')); o.append(marks(r['Lpm'], 'mid'))
    o.append(line('cons', '#8e44ad', 1.2, 0.8)); o.append(line('trig', '#e07b25', 2)); o.append(line('ask', '#a9bfee', 1)); o.append(line('bid', '#2f6fdf', 2.4))
    xb, yb = X(r['ib']), Y(d['entry']); o.append('<path d="M%.1f %.1f l7 12 l-14 0 z" fill="#16a34a" stroke="#fff"/>' % (xb, yb - 7))
    xs, ys = X(r['ic']), Y(d['exit']); o.append('<path d="M%.1f %.1f l7 7 l-7 7 l-7 -7 z" fill="#dc2626" stroke="#fff"/>' % (xs, ys - 7))
    if r['ipeak'] > r['ib']: o.append('<circle cx="%.1f" cy="%.1f" r="6" fill="none" stroke="#111" stroke-width="2"/>' % (X(r['ipeak']), Y(r['peak'])))
    o.append('<text x="46" y="%d" font-size="11" fill="var(--mute)">blue Polymarket sell price · light blue buy price · orange %s · purple all bookmakers · green dashes = the bot\'s target · ring = top of the wave</text>' % (H - 4, d.get('trigger')))
    o.append('</svg>')
    return ''.join(o)


def cmd_html(db, path):
    R = [analyse(db, t) for t in trades(db)]
    css = """:root{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--mute:#6b7280;--grid:#e8eaee;--line:#d9dde3}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0f1114;--card:#171a1f;--ink:#e9ecf1;--mute:#98a1ae;--grid:#252a31;--line:#2c323a}}
:root[data-theme="dark"]{--bg:#0f1114;--card:#171a1f;--ink:#e9ecf1;--mute:#98a1ae;--grid:#252a31;--line:#2c323a}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.45 -apple-system,system-ui,sans-serif}main{max-width:820px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:6px 0 4px}p{margin:6px 0;color:var(--mute)}.t{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:14px 0;padding:10px 10px 4px}
.h{font-weight:600}.k{display:inline-block;padding:1px 8px;border-radius:9px;font-size:12px;font-weight:600;margin-left:6px}
.a{background:#dcfce7;color:#166534}.b{background:#fee2e2;color:#991b1b}.c{background:#fef3c7;color:#92400e}.d{background:#e5e7eb;color:#374151}
.n{font-size:13px;color:var(--mute);margin:2px 0 6px}svg{width:100%;height:auto;display:block}table{border-collapse:collapse;width:100%;font-size:14px;margin:8px 0}td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:right}td:first-child,th:first-child{text-align:left}"""
    K = {'wave reached the target': 'a', 'wave ended short': 'b', 'bought after the wave': 'c', 'no wave': 'd'}
    by = collections.defaultdict(list)
    for r in R: by[r['cls']].append(r)
    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Trade waves</title><style>%s</style></head><body><main>' % css]
    h.append('<h1>Every buy and its wave</h1><p>%d buys, 18 to 21 Nov. Thick green and red strokes are the legs the detector found on each line. Prices in cents, our side.</p>' % len(R))
    h.append('<table><tr><th>What Polymarket\'s wave did</th><th>Trades</th><th>Sold at target</th><th>Won</th><th>Result at $12</th><th>At the wave\'s top</th></tr>')
    for k, v in sorted(by.items(), key=lambda x: -len(x[1])):
        h.append('<tr><td>%s</td><td>%d</td><td>%d</td><td>%d</td><td>$%+.2f</td><td>$%+.2f</td></tr>' % (k.capitalize(), len(v), sum(1 for r in v if r['tr']['d'].get('reason') == 'caught up'), sum(1 for r in v if r['base'] > 0), sum(r['base'] for r in v), sum(pnl12(r['tr']['d'], r['peak']) for r in v)))
    h.append('</table>')
    for r in reversed(R):
        t = r['tr']; d = t['d']
        note = []
        if r['ratio'] is not None: note.append('Polymarket\'s wave was %d%% of the bookmaker\'s move' % round(r['ratio'] * 100))
        if r['lead'] is not None: note.append('it started %d s %s the bookmaker\'s' % (abs(r['lead']), 'before' if r['lead'] < 0 else 'after'))
        if r['endlag'] is not None: note.append('ended %d s %s' % (abs(r['endlag']), 'after' if r['endlag'] > 0 else 'before'))
        note.append('top %.1fc, %d s after the buy' % (r['peak'] * 100, r['ipeak'] - r['ib']))
        h.append('<div class="t"><div class="h">#%d · %s · %s v %s · %s<span class="k %s">%s</span></div>' % (t['id'], time.strftime('%d %b %H:%M', time.localtime(t['ts'] / 1000)), d.get('home'), d.get('away'), 'REAL' if t['real'] else 'paper', K.get(r['cls'], 'd'), r['cls']))
        h.append('<div class="n">Bought %.1fc, target %s, bot sold %.1fc (%s), $%+.2f at $12. %s.</div>%s</div>' % (d['entry'] * 100, ('%.1fc' % (r['target'] * 100)) if r['target'] else '-', d['exit'] * 100, d.get('reason'), r['base'], '; '.join(note), svg(r)))
    h.append('</main></body></html>')
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    open(path, 'w').write(''.join(h))
    print('written', path, len(''.join(h)) // 1024, 'KB')


if __name__ == '__main__':
    db = conn(); a = sys.argv[1:] or ['trades']
    if a[0] == 'one': cmd_one(db, int(a[1]))
    elif a[0] == 'trades': cmd_trades(db)
    elif a[0] == 'couple': cmd_couple(db)
    elif a[0] == 'exits': cmd_exits(db)
    elif a[0] == 'html': cmd_html(db, a[1])
    elif a[0] == 'all': R = cmd_trades(db); cmd_exits(db, R)
