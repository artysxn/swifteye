"""Make the other days like 18 Nov ON PURPOSE (owner, 22 Nov 2026: "The feeds were late that day, which resulted in more
money ... Can we un-remove it? What can we do to make the other days more like that day?").

On 17-18 Nov our feed was ~1.5 s late, so in the lagged rule's 'followed' (consensus.rs, Polymarket by arrival time)
Polymarket moves made within ~1.5 s of a bookmaker jump counted as FOLLOWING instead of LEADING - and the extra trades
that let in made money. Here the same measure (tools/lag_clock.py's rebuild) is taken on Polymarket's OWN clock, shifted
by S seconds - as if every Polymarket price reached us S s late - on every day, and today's rules are replayed with it
(SWEEP_DATA=v18, strict gap checks, real-money sales, $12). S = 0 is the true clock; arrival time (as the bot does now)
is shown too.
usage: SWEEP_DATA=v18 python3 lag_shift.py      -> ~/swifteye/research/lag_shift.txt
"""
import os, sys, bisect, statistics as st, collections, copy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('SWEEP_DATA', 'v18')
import sweep as S
import sqlite3
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
PLAT = {'linebet': '1xbet', 'melbet': '1xbet', 'betwinner': '1xbet', 'megapari': '1xbet', 'paripesa': '1xbet', '22bet': '1xbet', '22bit': '1xbet', '1xbit': '1xbet',
        'unibet': 'kambi', 'leovegas': 'kambi', 'paf': 'kambi', 'fonbet': 'fonbet', 'pari': 'fonbet', 'mostbet': 'fonbet', 'vinn': 'altenar', 'betking': 'altenar'}
MAX_AGE = 20 * 60_000; JUMP = 0.03; STEP = 5000
SHIFTS = [int(x) for x in os.environ.get('SHIFTS', '0,1500,3000,5000,10000').split(',')]
FROM_MS = int(os.environ.get('FROM_MS', '0'))        # only moves from this time on (the daily slack test: days none of the settings were chosen on)
B = dict(S.BASE, real_sale=True); CI = S.CATCH.index(1.0)
pre = [c for c in S.ROWS if c['tf'] >= FROM_MS and S.gate(c, dict(B, lag=None, weak_cap=None), CI)]


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
        q.sort(); self.pm_own = ([x[0] for x in q], [(x[1] + x[2]) / 2 for x in q]); self.cc = {}

    def cons(self, t):
        if t in self.cc: return self.cc[t]
        acc = collections.defaultdict(list)
        for b, v in self.books.items():
            x = at(v, t)
            if x and t - x[0] <= MAX_AGE: acc[PLAT.get(b, b)].append(x[1])
        fam = [sum(v) / len(v) for v in acc.values()]
        r = st.median(fam) if fam else None; self.cc[t] = r; return r

    def pm(self, t, shift):
        if shift is None: x = at(self.pm_arr, t)
        else: x = at(self.pm_own, t - shift)          # a price made at x is seen at x + shift
        return x[1] if x and t - x[0] - (shift or 0) <= MAX_AGE else None

    def follow(self, pre_t, now, shift):
        fol, led = [], []; t = now - 1_800_000; last = t - 60_000
        while t + 60_000 <= pre_t:
            if t - last >= 60_000:
                a, b = self.cons(t - 10_000), self.cons(t)
                if a is not None and b is not None and abs(b - a) >= JUMP:
                    j = b - a; p40, p0, p60 = self.pm(t - 40_000, shift), self.pm(t, shift), self.pm(t + 60_000, shift)
                    if p40 is not None and p0 is not None and p60 is not None:
                        led.append((p0 - p40) / j); fol.append((p60 - p0) / j); last = t
            t += STEP
        return (len(fol), st.median(fol) if fol else None, st.median(led) if led else None)


by_game = collections.defaultdict(list)
for c in pre: by_game[(c['match_id'], c['outcome'])].append(c)
res = collections.defaultdict(dict)
for n, ((m, o), L) in enumerate(by_game.items()):
    g = Game(m, o, min(c['t0'] for c in L) - 31 * 60_000, max(c['t0'] for c in L) + 1500)
    for c in L:
        td2 = c['t0'] + 1500
        res[id(c)]['arrival'] = g.follow(c['t0'] - 10_000, td2, None)
        for s in SHIFTS: res[id(c)][s] = g.follow(c['t0'] - 10_000, td2, s)
    if n % 150 == 0: print('  %d of %d game sides' % (n, len(by_game)), flush=True)


def rows_with(k):
    R = []
    for c in ROWS:
        r = res.get(id(c))
        if r is None: R.append(c); continue
        c2 = copy.copy(c); cx = dict(c['cx']); n_, fo, le = r[k]
        cx['jumps'] = n_; cx['follow'] = fo if fo is not None else -9.0; cx['lead'] = le if le is not None else -9.0
        c2['cx'] = cx; R.append(c2)
    return R


ROWS = [c for c in S.ROWS if c['tf'] >= FROM_MS]
days = sorted({c['_day'] for c in ROWS})
out = ['TODAY\'S RULES WITH "FOLLOWED" MEASURED AS IF POLYMARKET\'S PRICES REACHED US S SECONDS LATE (own clock + S), every day; $12, real-money sales',
       '%-42s %-12s %s' % ('', 'all days', ' | '.join('%s Nov' % d for d in days))]
for lab, k in [('arrival time (as the bot does now)', 'arrival')] + [('own clock + %.1f s' % (s / 1000), s) for s in SHIFTS]:
    T = S.sim(B, rows=rows_with(k)); by = collections.defaultdict(list)
    for t in T: by[t['c']['_day']].append(t['pnl'])
    out.append('%-42s %3d tr %+7.2f $ | %s' % (lab, len(T), sum(t['pnl'] for t in T), ' | '.join('%2d tr %+7.2f' % (len(by[d]), sum(by[d])) for d in days)))
txt = '\n'.join(out); print(txt)
open(os.path.expanduser('~/swifteye/research/lag_shift%s.txt' % ('_new_days' if FROM_MS else '')), 'w').write(txt + '\n')
