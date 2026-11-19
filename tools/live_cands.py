"""Record every move the LIVE snipe bot would measure and could plausibly buy, with the Polymarket
quotes for the 17 minutes after, so entry and exit rules can be compared without re-reading the
database. Uses live_emu.py's (validated) copy of snipebot.rs measure().

Kept: bookmaker move 7+ pt, Polymarket followed <= 10 % (10 s before the trigger book), spread <= 4c,
price 2-98c, expected gain >= 2c, no data gap, not basketball, 2+ feed families moved.

usage: live_cands.py FROM_MS TO_MS OUT.pickle
"""
import sys, pickle, time, bisect
from array import array
import live_emu as E

WINDOWS = (45_000, 90_000, 180_000)     # "same move" chain lengths tried for the first-book check
PATH_MS = 3750 + 900_000 + 120_000

def run(T0, T1):
    out = []; last = {}; starts = {w: {} for w in WINDOWS}
    A = T0
    while A < T1:
        B = min(A + E.CHUNK, T1); t_ = time.time()
        C = E.Chunk(A, B); C.snap = dict(last)
        n0 = len(out)
        for ts, m, o, book, p in C.ev:
            k = (m, o, book); prev = last.get(k); last[k] = p
            if prev is None or abs(p - prev) < E.MIN_JUMP: continue
            d = 1.0 if p > prev else -1.0
            x = C.measure(m, o, d, book, ts)
            if x in ('no_book_price', 'book_reverted'): continue
            st = {}
            for w in WINDOWS:
                kk = (m, o, d > 0); e = starts[w].get(kk)
                if e is None or ts - e > w or e - ts > w or ts < e: e = starts[w][kk] = ts
                st[w] = e
            if isinstance(x, str): continue
            if x['gap'] or x['bm'][0] < 0.07 or max(x['fol']) > 0.10 or max(x['sp']) > 0.04 + 1e-9: continue
            if not all(0.02 <= v <= 0.98 for v in x['dec']) or min(x['edge']) < 0.02: continue
            if E.SPORT.get(m) == 'basketball': continue
            pre, now = ts - E.PRE, ts + E.REACTION + E.CONFIRM
            f_pre, f_now = C.fams(m, o, pre), C.fams(m, o, now)
            if sum(1 for kf, pv in f_now.items() if kf in f_pre and (pv - f_pre[kf]) * d >= 0.02) < 2: continue
            cx = C.ctx(m, o, d, pre, now)
            if not cx: continue
            mid2 = (x['q2'][0] + x['q2'][1]) / 2
            fs = {}
            for w in WINDOWS:
                q = C.quote(m, o, st[w] - E.PRE) if st[w] < ts else None
                fs[w] = (mid2 - (q[0] + q[1]) / 2) * d / x['bm'][1] if q else x['fol'][1]
            s = C.q.get((m, o))
            i0 = max(bisect.bisect_right(s[0], ts + 1000) - 1, 0); i1 = bisect.bisect_right(s[0], ts + PATH_MS)
            out.append(dict(t0=ts, m=m, o=o, d=d, book=book, sport=E.SPORT.get(m), home=E.NAMES.get(m, ('', ''))[0], away=E.NAMES.get(m, ('', ''))[1],
                            bm=x['bm'], fol=x['fol'], sp=x['sp'], dec=x['dec'], edge=x['edge'], pre_mid=x['pre_mid'], pause=x['pause'], cx=cx,
                            start={w: st[w] for w in WINDOWS}, fol_start=fs, complete=s[0][-1] >= ts + PATH_MS or B + E.AFTER > ts + PATH_MS,
                            qt=array('q', s[0][i0:i1]), qb=array('d', s[1][i0:i1]), qa=array('d', s[2][i0:i1])))
        print(f"  {E.U(A):%m-%d %H:%M}-{E.U(B):%H:%M}  kept {len(out) - n0:5d} candidates ({time.time() - t_:.0f}s)", flush=True)
        A = B
    return out

if __name__ == '__main__':
    T0, T1, path = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    c = run(T0, T1)
    pickle.dump(dict(T0=T0, T1=T1, cands=c), open(path, 'wb'))
    print(len(c), 'candidates saved')
