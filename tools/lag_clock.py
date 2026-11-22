"""Does a LATE FEED inflate the lagged rule? (owner, 22 Nov 2026: "Why were there so many trades on this day compared to the
other days? With the current rules, does it still reproduce the same trade amount?")

consensus.rs measures 'followed' / 'lead' (the lagged rule, the weak-game cap) with Polymarket's price from the `changes`
table, stamped when it REACHED US. On 17-18 Nov the feed was ~1.5 s late with longer stalls, so a Polymarket move made
together with the bookmakers is stamped after them and counts as 'followed'. Here the same measure is rebuilt in Python
(consensus.rs Series::ctx: consensus = median of the bookmaker families' prices, each family the mean of its books with a
price under 20 min old; earlier consensus jumps of 3 pt+ within 10 s in the 30 min before the move, one a minute;
followed = median share Polymarket made in the 60 s after, lead = in the 40 s before), twice:
  ARRIVAL   Polymarket from `changes` by arrival time (as the bot) - must reproduce the replay's own numbers
  OWN CLOCK Polymarket's middle from pm_quotes by Polymarket's own time
then today's rules are replayed with each (SWEEP_DATA=v18, strict gap checks, real-money sales).
usage: SWEEP_DATA=v18 python3 lag_clock.py      -> ~/swifteye/research/lag_clock.txt
"""
import os, sys, sqlite3, bisect, statistics as st, collections, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sweep as S

DB = os.path.expanduser('~/swifteye/oddsfeed.db')
db = sqlite3.connect('file:' + DB + '?mode=ro', uri=True)
PLAT = {'linebet': '1xbet', 'melbet': '1xbet', 'betwinner': '1xbet', 'megapari': '1xbet', 'paripesa': '1xbet', '22bet': '1xbet', '22bit': '1xbet', '1xbit': '1xbet',
        'unibet': 'kambi', 'leovegas': 'kambi', 'paf': 'kambi', 'fonbet': 'fonbet', 'pari': 'fonbet', 'mostbet': 'fonbet', 'vinn': 'altenar', 'betking': 'altenar'}
MAX_AGE = 20 * 60_000; JUMP = 0.03; STEP = 5000
B = dict(S.BASE, real_sale=True); CI = S.CATCH.index(1.0)
# the moves that pass every rule except the two that use 'followed' / 'lead'
pre = [c for c in S.ROWS if S.gate(c, dict(B, lag=None, weak_cap=None), CI)]
print(len(pre), 'moves to measure', flush=True)


def at(v, t):
    i = bisect.bisect_right(v[0], t) - 1
    return (v[0][i], v[1][i]) if i >= 0 else None


class Game:
    def __init__(self, m, o, lo, hi):
        rows = db.execute("SELECT book, ts_ms, new_price FROM changes WHERE match_id=? AND market='1x2' AND outcome=? AND ts_ms BETWEEN ? AND ? AND new_price IS NOT NULL ORDER BY ts_ms", (m, o, lo - MAX_AGE, hi)).fetchall()
        books = collections.defaultdict(lambda: ([], [])); pm = ([], [])
        for b, ts, p in rows:
            v = 1.0 / max(p, 1.0001)
            if b == 'polymarket': pm[0].append(ts); pm[1].append(v)
            elif b != 'kalshi': books[b][0].append(ts); books[b][1].append(v)
        self.books = dict(books); self.pm_arr = pm
        q = db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND bid > 0 AND ask > 0 AND ts_ms BETWEEN ? AND ? ORDER BY id", (m, o, lo - MAX_AGE, hi + 60_000)).fetchall()
        q.sort(); self.pm_own = ([x[0] for x in q], [(x[1] + x[2]) / 2 for x in q])

    def cons(self, t):
        acc = collections.defaultdict(list)
        for b, v in self.books.items():
            x = at(v, t)
            if x and t - x[0] <= MAX_AGE: acc[PLAT.get(b, b)].append(x[1])
        fam = [sum(v) / len(v) for v in acc.values()]
        return st.median(fam) if fam else None

    def pm(self, t, own):
        x = at(self.pm_own if own else self.pm_arr, t)
        return x[1] if x and t - x[0] <= MAX_AGE else None

    def follow(self, dir_, pre_t, now, own):
        fol, led = [], []; t = now - 1_800_000; last = t - 60_000
        while t + 60_000 <= pre_t:
            if t - last >= 60_000:
                a, b = self.cons(t - 10_000), self.cons(t)
                if a is not None and b is not None and abs(b - a) >= JUMP:
                    j = b - a; p40, p0, p60 = self.pm(t - 40_000, own), self.pm(t, own), self.pm(t + 60_000, own)
                    if p40 is not None and p0 is not None and p60 is not None:
                        led.append((p0 - p40) / j); fol.append((p60 - p0) / j); last = t
            t += STEP
        return (len(fol), st.median(fol) if fol else None, st.median(led) if led else None)


by_game = collections.defaultdict(list)
for c in pre: by_game[(c['match_id'], c['outcome'])].append(c)
res = {}
for n, ((m, o), L) in enumerate(by_game.items()):
    g = Game(m, o, min(c['t0'] for c in L) - 31 * 60_000, max(c['tf'] for c in L))
    for c in L:
        td2 = c['t0'] + 1500          # adaptive::make_cand measures at td2 = t0 + reaction 1 s + confirm 0.5 s (not the fill time)
        res[id(c)] = (g.follow(c['dir'], c['t0'] - 10_000, td2, False), g.follow(c['dir'], c['t0'] - 10_000, td2, True))
    if n % 100 == 0: print('  %d of %d game sides' % (n, len(by_game)), flush=True)
out = []; P = out.append
# validation: the arrival-time rebuild against the replay's own numbers
ok = [(c['cx']['follow'], res[id(c)][0][1]) for c in pre if c['cx']['jumps'] > 0 and res[id(c)][0][1] is not None]
P('LATE FEED v THE LAGGED RULE: %d moves that pass every rule except the two using "followed"\n' % len(pre))
P('VALIDATION: rebuilt with arrival times v the replay\'s own "followed": within 0.05 for %d of %d moves (median difference %+.3f)' % (sum(1 for a, b in ok if abs(a - b) <= 0.05), len(ok), st.median(b - a for a, b in ok) if ok else 0))
days = sorted({c['_day'] for c in pre})
P('\n"FOLLOWED" BY DAY, median over these moves (arrival time -> Polymarket\'s own clock), and the share passing 50%:')
for d in days:
    X = [res[id(c)] for c in pre if c['_day'] == d and res[id(c)][0][1] is not None and res[id(c)][1][1] is not None]
    if not X: continue
    P('  %s Nov: %4d moves | followed %.2f -> %.2f | lead %.2f -> %.2f | passing 50%%: %3.0f%% -> %3.0f%%' % (d, len(X), st.median(x[0][1] for x in X), st.median(x[1][1] for x in X),
        st.median(x[0][2] for x in X), st.median(x[1][2] for x in X), 100.0 * sum(1 for x in X if x[0][1] >= 0.5) / len(X), 100.0 * sum(1 for x in X if x[1][1] >= 0.5) / len(X)))
# replay today's rules with the REBUILT measure on each clock (only the clock differs between the two runs)
def with_clock(k):
    rows2 = []
    for c in S.ROWS:
        r = res.get(id(c))
        if r is None: rows2.append(c); continue
        c2 = copy.copy(c); cx = dict(c['cx']); n_, fo, le = r[k]
        cx['jumps'] = n_; cx['follow'] = fo if fo is not None else -9.0; cx['lead'] = le if le is not None else -9.0
        c2['cx'] = cx; rows2.append(c2)
    return rows2
for lab, R in (("today's rules, the replay's own 'followed' (arrival time)", S.ROWS), ("today's rules, rebuilt 'followed', ARRIVAL time", with_clock(0)), ("today's rules, rebuilt 'followed', POLYMARKET'S OWN clock", with_clock(1))):
    T = S.sim(B, rows=R); by = collections.defaultdict(list)
    for t in T: by[t['c']['_day']].append(t['pnl'])
    P('\n%s: %d trades, won %d, %+.2f $' % (lab, len(T), sum(1 for t in T if t['pnl'] > 0), sum(t['pnl'] for t in T)))
    P('   by day: ' + ' | '.join('%s Nov %d trades %+.2f $' % (d, len(by[d]), sum(by[d])) for d in sorted(by)))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/lag_clock.txt'), 'w').write(txt + '\n')
