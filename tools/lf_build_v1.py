"""Leader-follower dataset. For every recorded game with a Polymarket market: every jump of every bookmaker's line
(the possible LEADER) with what the bot could know at that moment about that bookmaker against Polymarket in this
game (the FOLLOWER): the pair's own delay and correlation scores from the bookmaker's earlier jumps, what Polymarket
had already done, and Polymarket's price path for the next 5 minutes (to try sale rules on).

Polymarket by its own clock (a price that reached us late still counts when it was made); bookmakers by the time
their price reached us. Our side = the outcome whose chance jumped UP; its own token's bid and ask.

usage: lf_build.py [max games]      -> ~/swifteye/research/lf_cands.pickle   (about 10 minutes for everything)
"""
import sqlite3, os, sys, time, pickle, bisect, statistics as st, math, collections

DB = os.path.expanduser('~/swifteye/oddsfeed.db')
OUT = os.path.expanduser('~/swifteye/research/lf_cands.pickle')
COUNTS = '/private/tmp/claude-501/-Users-danielvinichenko--claude-spares-spare-gVuQbw/cd21609b-6e94-423f-8770-e4a80f6afcbe/scratchpad/pm_counts.txt'
J = 0.04            # a jump: the line up this much within WIN
WIN = 10_000
W = 90_000          # Polymarket's own move is looked for this long either side of a jump
CONFIRM = 2_000     # the jump must still stand this long after it
SEND = 2_000        # an order lands this long after the decision
PATH_S, PATH_N = 2, 150
FAM = {'linebet': '1xbet', 'melbet': '1xbet', 'betwinner': '1xbet', 'megapari': '1xbet', 'paripesa': '1xbet', '22bet': '1xbet', '22bit': '1xbet', '1xbit': '1xbet',
       'unibet': 'kambi', 'leovegas': 'kambi', 'paf': 'kambi', 'fonbet': 'fonbet', 'pari': 'fonbet', 'mostbet': 'fonbet', 'vinn': 'altenar', 'betking': 'altenar'}


class Step:
    """A line that holds its value until the next change."""
    __slots__ = ('t', 'v')
    def __init__(self): self.t, self.v = [], []
    def add(self, t, v): self.t.append(t); self.v.append(v)
    def at(self, t):
        i = bisect.bisect_right(self.t, t) - 1
        return self.v[i] if i >= 0 else None
    def idx(self, t): return bisect.bisect_right(self.t, t) - 1


class Quotes:
    __slots__ = ('t', 'b', 'a')
    def __init__(self, rows):
        rows.sort(); self.t = [r[0] for r in rows]; self.b = [r[1] for r in rows]; self.a = [r[2] for r in rows]
    def at(self, t, max_age=120_000):
        i = bisect.bisect_right(self.t, t) - 1
        if i < 0 or t - self.t[i] > max_age: return None
        return self.b[i], self.a[i]
    def mid(self, t, max_age=300_000):
        q = self.at(t, max_age); return None if q is None else (q[0] + q[1]) / 2


def response(q, tj, size):
    """Polymarket's own move around a bookmaker jump at tj: (share of the jump it made over tj-W..tj+W, seconds
    from the jump to the moment that move was halfway done; None if it did not make 30% of it)."""
    a, b = q.mid(tj - W), q.mid(tj + W)
    if a is None or b is None: return None
    share = (b - a) / size
    if share < 0.3: return (max(share, 0.0), None)
    need = a + 0.5 * (b - a); i0 = bisect.bisect_left(q.t, tj - W); i1 = bisect.bisect_right(q.t, tj + W)
    for i in range(i0, i1):
        if (q.b[i] + q.a[i]) / 2 >= need - 1e-9: return (min(share, 1.5), (q.t[i] - tj) / 1000.0)
    return (min(share, 1.5), None)


def level_corr(line, q, t, span=1_800_000, grid=10_000, maxlag=120_000):
    """Correlation score: how alike the bookmaker's line and Polymarket's line were over the last half hour, at the
    best slide in time. Returns (0-100, slide in seconds; + = the bookmaker ahead)."""
    n = span // grid; ts = [t - (n - k) * grid for k in range(n + 1)]
    x = [line.at(u) for u in ts]; y = [q.mid(u) for u in ts]
    lags = range(-maxlag // grid, maxlag // grid + 1); best = (None, 0)
    for L in lags:
        sx = sy = sxx = syy = sxy = 0.0; m = 0
        for k in range(len(ts)):
            kb = k - L                                   # the bookmaker L steps earlier against Polymarket now
            if kb < 0 or kb >= len(ts): continue
            a, b = x[kb], y[k]
            if a is None or b is None: continue
            sx += a; sy += b; sxx += a * a; syy += b * b; sxy += a * b; m += 1
        if m < 60: continue
        vx, vy = sxx / m - (sx / m) ** 2, syy / m - (sy / m) ** 2
        if vx < 1e-5 or vy < 1e-5: continue
        c = (sxy / m - sx * sy / m / m) / math.sqrt(vx * vy)
        if best[0] is None or c > best[0]: best = (c, L * grid / 1000.0)
    return (None, None) if best[0] is None else (100.0 * max(best[0], 0.0), best[1])


def game(db, mid, sport, home, away):
    ch = db.execute("SELECT ts_ms, book, outcome, new_price FROM changes WHERE match_id=? AND market='1x2' AND outcome IN ('home','away') AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms", (mid,)).fetchall()
    if not ch: return []
    lines = {}
    for t, b, o, p in ch:
        k = (b, o)
        if k not in lines: lines[k] = Step()
        lines[k].add(t, (1.0 / p) if p and p > 1.0 else None)
    # jumps per line: the price is J+ above where it stood WIN before (after a pause: above the last price before it)
    jumps = []
    for (b, o), ln in lines.items():
        last = -10**18; lastval = None; lastval_t = None; susp_t = None
        for i in range(len(ln.t)):
            t, v = ln.t[i], ln.v[i]
            if v is None:
                if susp_t is None: susp_t = t
                continue
            v0 = ln.at(t - WIN); pause = 0
            if v0 is None and lastval is not None and susp_t is not None: v0 = lastval; pause = int((t - susp_t) / 1000)   # reopened after a pause
            if v0 is not None and v - v0 >= J and t - last > 15_000:
                jumps.append([t, b, o, v0, v, pause]); last = t
            lastval, lastval_t, susp_t = v, t, None
    if not jumps: return []
    rows = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), outcome, bid, ask FROM pm_quotes WHERE match_id=? AND bid > 0 AND ask > 0", (mid,)).fetchall()
    Q = {}
    for o in ('home', 'away'):
        r = [(t, b, a) for t, oc, b, a in rows if oc == o]
        if len(r) >= 300: Q[o] = Quotes(r)
    if not Q: return []
    jumps.sort()
    # every jump's own outcome on Polymarket (known W after it), then each jump's view of its bookmaker's earlier ones
    resp = []
    for t, b, o, v0, v, pause in jumps:
        q = Q.get(o); ln = lines[(b, o)]
        keep = ln.at(t + 30_000); stood = keep is not None and keep >= v0 + 0.5 * (v - v0)
        resp.append(response(q, t, v - v0) if (q and stood) else None)
    hist = collections.defaultdict(list)      # book -> [(time known, share, lag)]
    fhist = collections.defaultdict(list)     # family -> same
    out = []
    order = sorted(range(len(jumps)), key=lambda i: jumps[i][0])
    pending = []                              # (time known, book, share, lag) not yet visible
    pi = 0
    for i in order:
        t, b, o, v0, v, pause = jumps[i]; size = v - v0
        while pi < len(pending) and pending[pi][0] <= t:
            _, pb, sh, lg = pending[pi]; hist[pb].append((sh, lg)); fhist[FAM.get(pb, pb)].append((sh, lg)); pi += 1
        if resp[i] is not None:
            pending.append((t + W, b, resp[i][0], resp[i][1])); pending.sort(key=lambda x: x[0])
        q = Q.get(o)
        if q is None: continue
        ln = lines[(b, o)]; td = t + CONFIRM; te = td + SEND
        vd = ln.at(td)
        if vd is None or vd < v0 + 0.75 * size: continue                     # the jump did not stand: no decision
        qd, qe = q.at(td), q.at(te)
        if qd is None or qe is None: continue
        m1, m10, m60 = q.mid(t - 1000), q.mid(t - 10_000), q.mid(t - 60_000)
        if m1 is None or m10 is None or m60 is None: continue
        if not (0.04 <= qe[1] <= 0.93) or qd[1] - qd[0] > 0.08: continue
        if (m1 - m60) / size >= 0.6 or ((qd[0] + qd[1]) / 2 - m60) / size >= 0.7: continue      # Polymarket has made the move already: never a buy
        def stats(h):
            n = len(h); pr = [x for x in h if x[1] is not None]
            if not n: return (0, 0, 0, 0, None, None, None)
            lags = sorted(x[1] for x in pr)
            return (n, len(pr), sum(1 for x in lags if x > 3), sum(1 for x in lags if x < -3), st.median(lags) if lags else None, st.median([x[0] for x in h]), len(pr) / n)
        sb, sf = stats(hist[b]), stats(fhist[FAM.get(b, b)])
        corr = level_corr(ln, q, t - 1000) if sb[1] >= 2 or sf[1] >= 3 else (None, None)
        # the next 5 minutes: our bid every PATH_S s from the moment the order lands, and the bookmaker's line
        pb_, pl_ = bytearray(PATH_N), bytearray(PATH_N)
        for k in range(PATH_N):
            u = te + k * PATH_S * 1000; qq = q.at(u, 300_000); lv = ln.at(u)
            pb_[k] = 255 if qq is None else int(round(qq[0] * 100)); pl_[k] = 255 if lv is None else min(254, int(round(lv * 100)))
        out.append(dict(m=mid, sport=sport, home=home, away=away, o=o, book=b, t=t, base=v0, top=v, size=size, pause=pause,
                        bid_d=qd[0], ask_d=qd[1], bid_e=qe[0], ask_e=qe[1], m1=m1, m10=m10, m60=m60,
                        sb=sb, sf=sf, corr=corr[0], slide=corr[1], pb=bytes(pb_), pl=bytes(pl_), nbooks=len(set(k[0] for k in lines))))
    return out


if __name__ == '__main__':
    lim = int(sys.argv[1]) if len(sys.argv) > 1 else None
    ids = [(int(a), int(b)) for a, b in (l.strip().split('|') for l in open(COUNTS)) if int(b) >= 1500]
    if lim: ids = ids[:: max(1, len(ids) // lim)][:lim]
    db = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
    meta = {r[0]: r[1:] for r in db.execute("SELECT id, sport, home, away FROM matches")}
    out = []; t0 = time.time(); ng = 0
    for n, (mid, cnt) in enumerate(ids):
        mt = meta.get(mid)
        if not mt: continue
        c = game(db, mid, *mt)
        if c: out += c; ng += 1
        if n % 100 == 0: print('%d of %d games, %d with jumps, %d candidates, %.0f s' % (n, len(ids), ng, len(out), time.time() - t0), flush=True)
    out.sort(key=lambda c: c['t'])
    pickle.dump(dict(cands=out, built=time.time(), J=J, path_s=PATH_S), open(OUT if not lim else OUT.replace('.pickle', '_sample.pickle'), 'wb'), protocol=4)
    print('written: %d candidates from %d games in %.0f s' % (len(out), ng, time.time() - t0))
