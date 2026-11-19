"""Offline emulation of the LIVE snipe bot's decision path (snipebot.rs on_change -> measure -> gate)
over the recorded data, with outcomes from the recorded Polymarket quotes.

The replay (adaptive.rs) finds moves differently (one event per move, first bookmaker, confirmed with
the price 10 s later). This script follows the live code instead: every bookmaker update that jumps
4+ points from that bookmaker's previous price is a trigger.

Modes (which triggers get looked at / taken):
  L  lock a game 2 min on EVERY trigger looked at            (the bot before 19:20 UTC on 19 Nov)
  N  lock a game 2 min only after a trade                     (the build deployed 19:20-19:36 UTC)
  F  N + Polymarket must not have moved since the first bookmaker of the move (never built)
  F1 N + only the first bookmaker of a move

usage: live_emu.py FROM_MS TO_MS OUT.json [validate]
"""
import sqlite3, json, sys, time, bisect, math, datetime as dt
from array import array
from collections import defaultdict, Counter

DB = 'file:/Users/danielvinichenko/swifteye/oddsfeed.db?mode=ro'
CHUNK, LOOKBACK, AFTER = 6 * 3600_000, 55 * 60_000, 20 * 60_000
REACTION, CONFIRM, PRE = 1000, 500, 10_000
SLIP, CAP, MIN_JUMP, SAME_MOVE, MOVE_MS = 0.01, 0.02, 0.04, 120_000, 90_000
MAX_AGE, STALE = 20 * 60_000, 30 * 60_000
FILL_AFTER = 3750          # real buy lands this long after the move (1.5 s tests + 0.7 s + 0.08 s + 1.47 s live delay)
SETTLE, SALE_LAT, GIVE = 1800, 1650, 900_000
R = dict(min_move=0.10, max_followed=0.05, min_edge=0.04, max_spread=0.04)
GAP_MAX = 0.10             # skip when Polymarket already sat this far from the bookmakers before the move   # the rules live since 18 Nov 16:00 UTC
FEE = lambda p: 0.05 * p * (1 - p)
U = lambda ms: dt.datetime.fromtimestamp(ms / 1000, dt.UTC)
OUTS = ('home', 'away', 'draw')

def platform(b):
    if b in ('linebet', 'melbet', 'betwinner', 'megapari', 'paripesa', '22bet', '22bit', '1xbit'): return '1xbet'
    if b in ('unibet', 'leovegas', 'paf'): return 'kambi'
    if b in ('fonbet', 'pari', 'mostbet'): return 'fonbet'
    if b in ('vinn', 'betking'): return 'altenar'
    return b

db = sqlite3.connect(DB, uri=True)
SPORT = dict(db.execute('SELECT id, sport FROM matches'))
NAMES = {i: (h, a) for i, h, a in db.execute('SELECT id, home, away FROM matches')}

def quote_id_at(ts):
    lo, hi = 1, db.execute('SELECT MAX(id) FROM pm_quotes').fetchone()[0]
    while lo < hi:
        mid = (lo + hi) // 2
        r = db.execute('SELECT ts_ms FROM pm_quotes WHERE id >= ? ORDER BY id LIMIT 1', (mid,)).fetchone()
        if r is None or r[0] >= ts: hi = mid
        else: lo = mid + 1
    return lo

# ---- state carried across chunks
last = {}            # (m, o, book) -> last price seen by on_change
first_seen = {}      # (m, o, book) -> first row ever
seen = {k: {} for k in 'LNF1'}; seen['F'] = {}; seen['F1'] = {}
starts = {}          # (m, o, up) -> start of the current move
open_until = {k: {} for k in ('L', 'N', 'F', 'F1')}   # real money: one open position a game

class Chunk:
    def __init__(self, A, B):
        self.A, self.B = A, B
        self.snap = dict(last)
        self.ser = {}            # (m, o, book) -> (ts, prob)
        self.books = defaultdict(list)   # (m, o) -> [book]
        self.opens = defaultdict(list); self.susp = defaultdict(list)
        self.q = {}              # (m, o) -> (ts, bid, ask)
        ev = []
        cur = db.execute("SELECT ts_ms, book, match_id, outcome, kind, new_price FROM changes INDEXED BY changes_ts WHERE ts_ms >= ? AND ts_ms < ? AND market = '1x2'", (A - LOOKBACK, B + 5000))
        for ts, book, m, o, kind, p in cur:
            if o not in OUTS or m <= 0: continue
            k = (m, o, book)
            if ts >= A and k not in first_seen: first_seen[k] = ts
            if kind == 'open': self.opens[k].append(ts)
            elif kind == 'suspend': self.susp[k].append(ts)
            if p is None: continue
            s = self.ser.get(k)
            if s is None:
                s = self.ser[k] = (array('q'), array('d'))
                self.books[(m, o)].append(book)
            s[0].append(ts); s[1].append(1.0 / max(p, 1.0001))
            if A <= ts < B and book != 'polymarket' and book != 'kalshi' and p > 1.0:
                ev.append((ts, m, o, book, 1.0 / p))
        self.ev = ev
        lo, hi = quote_id_at(A - LOOKBACK), quote_id_at(B + AFTER)
        nan = float('nan')
        for ts, m, o, b, a in db.execute('SELECT ts_ms, match_id, outcome, bid, ask FROM pm_quotes WHERE id >= ? AND id < ?', (lo, hi)):
            s = self.q.get((m, o))
            if s is None: s = self.q[(m, o)] = (array('q'), array('d'), array('d'))
            if s[0] and ts < s[0][-1]: ts = s[0][-1]      # keep the arrays sorted (rare out-of-order rows)
            s[0].append(ts); s[1].append(nan if b is None else b); s[2].append(nan if a is None else a)

    def book_prob(self, book, m, o, t):
        s = self.ser.get((m, o, book))
        if s:
            i = bisect.bisect_right(s[0], t)
            if i: return s[1][i - 1]
        return self.snap.get((m, o, book))

    def quote(self, m, o, t):
        s = self.q.get((m, o))
        if not s: return None
        i = bisect.bisect_right(s[0], t)
        if not i or t - s[0][i - 1] > STALE: return None
        b, a = s[1][i - 1], s[2][i - 1]
        if b != b or a != a or not (b > 0.0 and a < 1.0 and a > b): return None
        return (b, a)

    def reopened(self, book, m, o, frm, to):
        k = (m, o, book)
        return any(frm < t <= to and first_seen.get(k, t) < t for t in self.opens.get(k, ()))

    # ---- consensus.rs
    def fams(self, m, o, t):
        acc = {}
        for book in self.books.get((m, o), ()):
            if book == 'polymarket' or book == 'kalshi': continue
            ts, pr = self.ser[(m, o, book)]
            i = bisect.bisect_right(ts, t)
            if i and t - ts[i - 1] <= MAX_AGE:
                f = platform(book); e = acc.get(f)
                if e: e[0] += pr[i - 1]; e[1] += 1
                else: acc[f] = [pr[i - 1], 1]
        return {k: v[0] / v[1] for k, v in acc.items()}
    def cons(self, m, o, t):
        v = sorted(self.fams(m, o, t).values())
        return v[len(v) // 2] if v else None
    def pm_at(self, m, o, t):
        s = self.ser.get((m, o, 'polymarket'))
        if not s: return None
        i = bisect.bisect_right(s[0], t)
        return s[1][i - 1] if i and t - s[0][i - 1] <= MAX_AGE else None
    def ctx(self, m, o, d, pre, now):
        f_pre, f_now = self.fams(m, o, pre), self.fams(m, o, now)
        if not f_pre or not f_now: return None
        med = lambda f: sorted(f.values())[len(f) // 2]
        c_pre, c_now = med(f_pre), med(f_now)
        fam_moved = sum(1 for k, p in f_now.items() if k in f_pre and (p - f_pre[k]) * d >= 0.02)
        fol = []
        t = now - 1_800_000; lst = t - 60_000
        while t + 60_000 <= pre:
            if t - lst >= 60_000:
                a, b = self.cons(m, o, t - 10_000), self.cons(m, o, t)
                if a is not None and b is not None and abs(b - a) >= 0.03:
                    p40, p0, p60 = self.pm_at(m, o, t - 40_000), self.pm_at(m, o, t), self.pm_at(m, o, t + 60_000)
                    if p40 is not None and p0 is not None and p60 is not None:
                        fol.append((p60 - p0) / (b - a)); lst = t
            t += 5000
        fol.sort()
        return dict(fams=len(f_now), fam_moved=fam_moved, cons_move=(c_now - c_pre) * d, cons_now=c_now, jumps=len(fol), follow=fol[len(fol) // 2] if fol else -9.0)

    # ---- snipebot.rs measure()
    def measure(self, m, o, d, book, t0):
        td, td2, pre = t0 + REACTION, t0 + REACTION + CONFIRM, t0 - PRE
        b0, b1, b2 = self.book_prob(book, m, o, pre), self.book_prob(book, m, o, td), self.book_prob(book, m, o, td2)
        if b0 is None or b1 is None or b2 is None: return 'no_book_price'
        bm = [(b1 - b0) * d, (b2 - b0) * d]
        if bm[0] < 0.01 or bm[1] < 0.01: return 'book_reverted'
        k = (m, o, d > 0); e = starts.get(k)
        if e is None or t0 - e > MOVE_MS or e - t0 > MOVE_MS or t0 < e: e = starts[k] = t0
        q0, q1, q2 = self.quote(m, o, pre), self.quote(m, o, td), self.quote(m, o, td2)
        if not (q0 and q1 and q2): return 'no_market_price'
        pre_mid = (q0[0] + q0[1]) / 2
        fol = [((q1[0] + q1[1]) / 2 - pre_mid) * d / bm[0], ((q2[0] + q2[1]) / 2 - pre_mid) * d / bm[1]]
        fol_start = fol[1]
        if e < t0:
            qs = self.quote(m, o, e - PRE)
            if qs: fol_start = ((q2[0] + q2[1]) / 2 - (qs[0] + qs[1]) / 2) * d / bm[1]
        dec = [(q1[1] if d > 0 else 1 - q1[0]) + SLIP, (q2[1] if d > 0 else 1 - q2[0]) + SLIP]
        sp = [q1[1] - q1[0], q2[1] - q2[0]]
        def edge(i):
            target = min(max(pre_mid + d * bm[i], 0.0), 1.0)
            exp = target - sp[i] / 2 - SLIP if d > 0 else 1 - (target + sp[i] / 2) - SLIP
            return exp - dec[i] - FEE(dec[i]) - FEE(min(max(exp, 0.0), 1.0))
        return dict(m=m, o=o, d=d, book=book, t0=t0, start=e, bm=bm, fol=fol, fol_start=fol_start, dec=dec, sp=sp, pre_mid=pre_mid,
                    edge=[edge(0), edge(1)], q2=q2,
                    pause=any(t0 - 120_000 <= t <= t0 for t in self.susp.get((m, o, book), ())),
                    gap=self.reopened(book, m, o, pre, td2) or self.reopened('polymarket', m, o, pre, td2))

    def gate(self, f, first_check=None):
        """Why not (None = buy). Cheap checks first; same pass/fail as snipebot::gate + Flt::live (hours aside)."""
        if f['gap']: return 'data_gap'
        if f['bm'][0] < R['min_move']: return 'move_too_small'
        if max(f['fol']) > R['max_followed']: return 'already_moved'
        if first_check == 'F' and f['fol_start'] > R['max_followed']: return 'moved_with_first_book'
        if first_check == 'F1' and f['start'] != f['t0']: return 'not_first_book'
        if max(f['sp']) > R['max_spread'] + 1e-9: return 'spread_too_wide'
        if not all(0.02 <= x <= 0.98 for x in f['dec']): return 'price_out_of_range'
        if SPORT.get(f['m']) == 'basketball': return 'basketball'
        if max(f['dec']) > 0.90: return 'price_too_high'
        if min(f['edge']) < R['min_edge']: return 'gap_too_small'
        if f['pause']: return 'after_pause'
        cx = f.get('cx')
        if cx is None:
            cx = f['cx'] = self.ctx(f['m'], f['o'], f['d'], f['t0'] - PRE, f['t0'] + REACTION + CONFIRM) or {}
        if not cx: return 'no_consensus'
        if cx['fam_moved'] < 2: return 'too_few_feeds'
        if cx['cons_move'] < 0.03: return 'consensus_did_not_move'
        if cx['jumps'] == 0 or cx['follow'] < 0.3: return 'polymarket_does_not_lag'
        if (cx['cons_now'] - cx['cons_move'] * f['d'] - f['pre_mid']) * f['d'] >= GAP_MAX: return 'polymarket_already_apart'
        return None

    # ---- snipebot.rs enter() + monitor(), real-money timing
    def trade(self, f, fill_after=FILL_AFTER):
        m, o, d, t0 = f['m'], f['o'], f['d'], f['t0']
        sell = lambda q: q[0] if d > 0 else 1 - q[1]
        buy = lambda q: q[1] if d > 0 else 1 - q[0]
        qd = self.quote(m, o, t0 + 1750)
        if not qd: return dict(res='no_live_book')
        best = buy(qd)
        if best + SLIP > f['dec'][1] + CAP: return dict(res='price_ran')
        room = f['edge'][1] - (best + SLIP - f['dec'][1]) - R['min_edge']
        max_px = best + min(max(math.floor(room * 100 + 1e-6) / 100, 0.0), 0.02)
        tb = t0 + fill_after
        qf = self.quote(m, o, tb)
        if not qf or buy(qf) > max_px + 1e-9: return dict(res='not_filled', limit=max_px, ask=buy(qf) if qf else None)
        entry = buy(qf); cost = entry + FEE(entry)
        need = f['bm'][0] - f['sp'][0] / 2
        give_end = tb + GIVE
        s = self.q.get((m, o)); data_end = s[0][-1] if s else 0
        t = tb + SETTLE
        while True:
            if t + SALE_LAT > min(data_end, self.B + AFTER - 1000) and t < give_end + 60_000 and data_end >= self.B + AFTER - 5000:
                return dict(res='beyond_chunk')
            q = self.quote(m, o, t)
            if not q:
                if t > give_end:
                    # no book any more: settle on the last recorded price, as the bot does
                    i = bisect.bisect_right(s[0], t) if s else 0
                    b, a = (s[1][i - 1], s[2][i - 1]) if i else (float('nan'), float('nan'))
                    won = True if b == b and b >= 0.97 else False if a == a and a <= 0.03 else None
                    if won is None: return dict(res='unsettled', entry=entry)
                    pay = 1.0 if (d > 0) == won else 0.0
                    return dict(res='settled', entry=entry, exit=pay, ret=(pay - cost) / cost, held=(t - tb) / 1000, tb=tb, tx=t)
                t += 1000
                if t > give_end + 4 * 3600_000: return dict(res='unsettled', entry=entry)
                continue
            px = q[0] if d > 0 else q[1]
            caught = (px - f['pre_mid']) * d >= need or sell(q) >= 0.99
            if not caught and t < give_end:
                t += 1000; continue
            q2 = self.quote(m, o, t + SALE_LAT)
            if q2 and sell(q2) >= sell(q) - CAP - 1e-9:
                ex = sell(q2)
                return dict(res='caught up' if caught else 'gave up', entry=entry, exit=ex, ret=(ex - FEE(ex) - cost) / cost,
                            held=(t + SALE_LAT - tb) / 1000, tb=tb, tx=t + SALE_LAT)
            t += 1000

def run(T0, T1, validate=False):
    out = {k: [] for k in ('L', 'N', 'F', 'F1')}
    reasons = {k: Counter() for k in out}
    looked = Counter(); ntrig = 0
    val = []
    A = T0
    while A < T1:
        B = min(A + CHUNK, T1)
        t_ = time.time()
        C = Chunk(A, B)
        nt = 0
        for ts, m, o, book, p in C.ev:
            k = (m, o, book); prev = last.get(k); last[k] = p
            if prev is None or abs(p - prev) < MIN_JUMP: continue
            d = 1.0 if p > prev else -1.0
            nt += 1
            f = None
            def feat():
                nonlocal f
                if f is None: f = C.measure(m, o, d, book, ts)
                return f
            for mode in ('L', 'N', 'F', 'F1'):
                sm = seen[mode]
                if mode == 'L':
                    if m in sm and ts - sm[m] < SAME_MOVE: continue
                    sm[m] = ts
                elif m in sm and abs(ts - sm[m]) < SAME_MOVE: continue
                looked[mode] += 1
                x = feat()
                if isinstance(x, str): reasons[mode][x] += 1; continue
                why = C.gate(x, mode if mode in ('F', 'F1') else None)
                if validate and mode == 'N':
                    val.append(dict(ts=ts, m=m, o=o, book=book, d=d, why=why, bm=x['bm'], fol=x['fol'], sp=x['sp'], dec=x['dec'], cx=x.get('cx')))
                if why: reasons[mode][why] += 1; continue
                if mode != 'L': sm[m] = ts
                h = U(ts).hour
                rec = dict(t0=ts, m=m, o=o, d=d, book=book, sport=SPORT.get(m), home=NAMES.get(m, ('', ''))[0], bm=x['bm'][0], fol=x['fol'][1],
                           fol_start=x['fol_start'], first=x['start'] == ts, late_s=(ts - x['start']) / 1000, edge=x['edge'][1], dec=x['dec'][1], cx=x['cx'], in_hours=12 <= h < 21)
                if open_until[mode].get(m, 0) > ts:
                    rec['res'] = 'already_in_game'
                else:
                    r = C.trade(x); rec.update(r)
                    r5 = C.trade(x, 4900); rec['ret_slow'] = r5.get('ret'); rec['res_slow'] = r5['res']
                    if 'tx' in r: open_until[mode][m] = r['tx']
                reasons[mode]['ENTER:' + rec['res']] += 1
                out[mode].append(rec)
        ntrig += nt
        print(f"  {U(A):%m-%d %H:%M}-{U(B):%H:%M}  {len(C.ev):8d} bookmaker updates, {nt:6d} jumps of 4+ pt, trades so far " +
              ' '.join(f"{k}:{len(v)}" for k, v in out.items()) + f"   ({time.time() - t_:.0f}s)", flush=True)
        A = B
    return out, reasons, looked, ntrig, val

if __name__ == '__main__':
    T0, T1, path = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    out, reasons, looked, ntrig, val = run(T0, T1, len(sys.argv) > 4)
    json.dump(dict(out=out, reasons={k: dict(v) for k, v in reasons.items()}, looked=dict(looked), ntrig=ntrig, val=val, T0=T0, T1=T1), open(path, 'w'))
    print('jumps', ntrig, 'looked at', dict(looked))
    for k, v in reasons.items(): print(k, v.most_common(14))
