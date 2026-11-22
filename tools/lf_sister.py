"""Leader-follower pairs among SISTER markets (recorded since 21 Nov 02:32): Polymarket's map-winner market and its
match-winner market of the same game, and Fonbet's map-winner price. Same candidate format as lf_build.py, so
lf_system.py can trade them.

Pairs built (leader -> follower, the follower is what would be bought):
    match>map     Polymarket's match market jumps      -> buy the map market, same team
    map>match     Polymarket's map market jumps        -> buy the match market, same team
    fonbet>map    Fonbet's map price jumps             -> buy Polymarket's map market, same team
Which team a map token belongs to is found from the prices themselves (it rises with that team's match price).

usage: lf_sister.py     -> ~/swifteye/research/lf_sister.pickle   (about 2 minutes)
"""
import sqlite3, os, sys, time, pickle, bisect, statistics as st, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lf_build as LB
from lf_build import Step, Quotes, response, level_corr, J, WIN, W, CONFIRM, SEND, PATH_S, PATH_N

OUT = os.path.expanduser('~/swifteye/research/lf_sister.pickle')


def mid_step(q, flip=False):
    s = Step(); last = None
    for i in range(len(q.t)):
        m = (q.b[i] + q.a[i]) / 2
        if flip: m = 1 - m
        if last is None or abs(m - last) > 1e-9: s.add(q.t[i], m); last = m
    return s


def flipped(q):
    return Quotes([(q.t[i], 1 - q.a[i], 1 - q.b[i]) for i in range(len(q.t))])


def corr_sign(a, b, t0, t1, grid=10_000):
    """Do two lines rise together over [t0, t1]? Returns the correlation (-1..1) or None."""
    xs, ys = [], []
    t = t0
    while t <= t1:
        x, y = a.at(t), b.at(t)
        if x is not None and y is not None: xs.append(x); ys.append(y)
        t += grid
    if len(xs) < 30: return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    vx = sum((x - mx) ** 2 for x in xs); vy = sum((y - my) ** 2 for y in ys)
    if vx < 1e-6 or vy < 1e-6: return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (vx * vy) ** 0.5


def jumps_of(ln):
    out = []; last = -10**18
    for i in range(len(ln.t)):
        t, v = ln.t[i], ln.v[i]
        if v is None: continue
        v0 = ln.at(t - WIN)
        if v0 is not None and v - v0 >= J and t - last > 15_000: out.append((t, v0, v)); last = t
    return out


def cands(mid, sport, home, away, name, ln, q, extra):
    """Candidates for one leader line `ln` and one follower book `q` (the follower's own token's bid/ask)."""
    js = jumps_of(ln); out = []; hist = []; pending = []; pi = 0
    for t, v0, v in js:
        size = v - v0
        while pi < len(pending) and pending[pi][0] <= t: hist.append(pending[pi][1:]); pi += 1
        keep = ln.at(t + 30_000)
        if keep is not None and keep >= v0 + 0.5 * size:
            r = response(q, t, size)
            if r is not None: pending.append((t + W, r[0], r[1]))
        td = t + CONFIRM; te = td + SEND; vd = ln.at(td)
        if vd is None or vd < v0 + 0.75 * size: continue
        qd, qe = q.at(td), q.at(te)
        if qd is None or qe is None: continue
        m1, m10, m60 = q.mid(t - 1000), q.mid(t - 10_000), q.mid(t - 60_000)
        if m1 is None or m10 is None or m60 is None: continue
        if not (0.04 <= qe[1] <= 0.93) or qd[1] - qd[0] > 0.08: continue
        n = len(hist); pr = [x for x in hist if x[1] is not None]; lags = sorted(x[1] for x in pr)
        sb = (n, len(pr), sum(1 for x in lags if x > 3), sum(1 for x in lags if x < -3), st.median(lags) if lags else None, st.median([x[0] for x in hist]) if hist else None, len(pr) / n if n else None)
        corr = level_corr(ln, q, t - 1000) if len(pr) >= 2 else (None, None)
        pb_, pl_ = bytearray(PATH_N), bytearray(PATH_N)
        for k in range(PATH_N):
            u = te + k * PATH_S * 1000; qq = q.at(u, 300_000); lv = ln.at(u)
            pb_[k] = 255 if qq is None else int(round(qq[0] * 100)); pl_[k] = 255 if lv is None else min(254, max(0, int(round(lv * 100))))
        out.append(dict(m=mid, sport=sport, home=home, away=away, o=extra, book=name, t=t, base=v0, top=v, size=size, pause=0, bid_d=qd[0], ask_d=qd[1], bid_e=qe[0], ask_e=qe[1],
                        m1=m1, m10=m10, m60=m60, sb=sb, sf=sb, corr=corr[0], slide=corr[1], pb=bytes(pb_), pl=bytes(pl_), nbooks=1, moved=(m1 - m60) / size))
    return out


if __name__ == '__main__':
    db = sqlite3.connect('file:' + LB.DB + '?mode=ro', uri=True)
    meta = {r[0]: r[1:] for r in db.execute("SELECT id, sport, home, away FROM matches")}
    ids = [r[0] for r in db.execute("SELECT DISTINCT match_id FROM pm_sister_quotes WHERE kind='child_moneyline'")]
    out = []; t0 = time.time(); stats = collections.Counter()
    for n, mid in enumerate(ids):
        if mid not in meta: continue
        sport, home, away = meta[mid]
        rows = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), outcome, bid, ask FROM pm_quotes WHERE match_id=? AND bid > 0 AND ask > 0", (mid,)).fetchall()
        MQ = {o: Quotes([(t, b, a) for t, oc, b, a in rows if oc == o]) for o in ('home', 'away')}
        if len(MQ['home'].t) < 300: stats['no match market'] += 1; continue
        ML = {o: mid_step(MQ[o]) for o in ('home', 'away') if len(MQ[o].t) >= 300}
        sis = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), token, bid, ask FROM pm_sister_quotes WHERE match_id=? AND kind='child_moneyline' AND bid > 0 AND ask > 0", (mid,)).fetchall()
        toks = collections.defaultdict(list)
        for t, tok, b, a in sis: toks[tok].append((t, b, a))
        # Fonbet map prices of this game
        fl = collections.defaultdict(Step)
        for t, line, oc, p in db.execute("SELECT b.ts_ms, b.line, b.outcome, b.price FROM book_part_quotes b JOIN book_events e ON e.book=b.book AND e.book_event_id=b.book_event_id WHERE e.match_id=? AND b.kind='map' ORDER BY b.ts_ms", (mid,)):
            fl[(line, oc)].add(t, (1.0 / p) if p and p > 1.0 else None)
        for tok, r in toks.items():
            if len(r) < 300: continue
            tq = Quotes(r); a0, a1 = tq.t[0], tq.t[-1]; tl = mid_step(tq)
            c = corr_sign(tl, ML['home'], a0, a1)
            if c is None or abs(c) < 0.3: stats['map token not matched to a team'] += 1; continue
            side = 'home' if c > 0 else 'away'; other = 'away' if side == 'home' else 'home'; stats['map tokens'] += 1
            tqn = flipped(tq); tln = mid_step(tq, flip=True)        # the other team's side of the same map market
            if side in ML and other in ML:
                # match market leads, map market follows (both teams' sides)
                out += cands(mid, sport, home, away, 'match>map', ML[side], tq, side); out += cands(mid, sport, home, away, 'match>map', ML[other], tqn, other)
                # map market leads, match market follows
                out += cands(mid, sport, home, away, 'map>match', tl, MQ[side], side); out += cands(mid, sport, home, away, 'map>match', tln, MQ[other], other)
            # Fonbet's map price for the map this token trades: the one that moves with it most
            best = None
            for (line, oc), ln in fl.items():
                if oc != 'home': continue
                cc = corr_sign(ln, ML['home'] if False else (tl if side == 'home' else tln), a0, a1)
                if cc is not None and (best is None or cc > best[0]): best = (cc, line)
            if best and best[0] >= 0.5:
                stats['map tokens with a Fonbet map price'] += 1
                for oc, fq in ((side, tq), (other, tqn)):
                    ln = fl.get((best[1], oc))
                    if ln is not None: out += cands(mid, sport, home, away, 'fonbet>map', ln, fq, oc)
        if n % 20 == 0: print(n, 'of', len(ids), 'games,', len(out), 'candidates, %.0f s' % (time.time() - t0), flush=True)
    out.sort(key=lambda c: c['t'])
    pickle.dump(dict(cands=out, built=time.time(), J=J, path_s=PATH_S), open(OUT, 'wb'), protocol=4)
    print('written: %d candidates | %s | by pair %s' % (len(out), dict(stats), dict(collections.Counter(c['book'] for c in out))))
