"""Is a Polymarket market AHEAD of the bookmakers or BEHIND them? Measured against the FIRST bookmaker to move,
not the middle of all of them (the middle only moves once half have: against it Polymarket looks first 8 times in 10).

For one game and one outcome, over the last 30 minutes:
  each feed family's price (mean 1/odds of its books) and Polymarket's mid (its own clock), second by second
  a JUMP of a source  = it moves 3+ points within 10 s; its time = the second it had made half of the move
  an EVENT            = jumps the same way by one or more sources within 60 s of the first of them
  per event           = who was first; how many seconds Polymarket came after the first bookmaker family
                        (negative = Polymarket was first; none = Polymarket did not jump with them)
  LEVEL now           = the bookmakers' fair price (margin removed, middle of the families) minus Polymarket's mid

usage: ahead_behind.py now [sport]     every game live now: behind / ahead / level, and by how much
       (import with sys.argv[1] = 'lib' to use history / events / summary / verdict from another tool)
       ahead_behind.py validate        does the measure predict the backtest and live trades? -> research/ahead_behind.json
"""
import sys, os, sqlite3, bisect, json, time, statistics as st
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__))
MODE = sys.argv[1] if len(sys.argv) > 1 else 'now'
ARG = sys.argv[2] if len(sys.argv) > 2 else None
sys.argv = sys.argv[:1]
db = sqlite3.connect('file:' + os.path.expanduser('~/swifteye/oddsfeed.db') + '?mode=ro', uri=True)
WIN, JUMP = 31 * 60, 0.03


def fam(b):
    if b in ('linebet', 'melbet', 'betwinner', 'megapari', 'paripesa', '22bet', '22bit', '1xbit', '1xbet'): return '1xbet'
    if b in ('unibet', 'leovegas', 'paf'): return 'kambi'
    if b in ('fonbet', 'pari', 'mostbet'): return 'fonbet'
    if b in ('vinn', 'betking'): return 'altenar'
    return b


def history(m, o, t_end):
    """{source: price per second} for the WIN seconds up to t_end (ms). Sources: feed families and 'polymarket'."""
    a = t_end - WIN * 1000
    rows = db.execute("SELECT ts_ms, book, kind, new_price FROM changes WHERE match_id=? AND market='1x2' AND outcome=? AND ts_ms>=? AND ts_ms<=? AND book NOT IN ('polymarket','kalshi') ORDER BY ts_ms", (m, o, a - 1_200_000, t_end)).fetchall()
    q = sorted(db.execute("SELECT COALESCE(server_ts_ms, ts_ms), bid, ask FROM pm_quotes WHERE match_id=? AND outcome=? AND ts_ms>=? AND ts_ms<=? AND bid IS NOT NULL AND ask IS NOT NULL", (m, o, a - 120_000, t_end + 5_000)).fetchall())
    time.sleep(0.05 + (len(rows) + len(q)) / 25000)
    state, pts = {}, defaultdict(list)                      # family -> [(ts, mean implied or None)]
    for ts, b, kind, p in rows:
        f = fam(b)
        if kind in ('close', 'suspend'): state.pop(b, None)
        elif p and p > 1.0: state[b] = 1 / p
        else: continue
        v = [x for bk, x in state.items() if fam(bk) == f]
        pts[f].append((ts, sum(v) / len(v) if v else None))
    out = {}
    for f, ps in pts.items():
        T = [x[0] for x in ps]; s = []
        for k in range(WIN + 1):
            i = bisect.bisect_right(T, a + k * 1000); s.append(ps[i - 1][1] if i else None)
        out[f] = s
    qt = [x[0] for x in q]; s = []
    for k in range(WIN + 1):
        t = a + k * 1000; j = bisect.bisect_right(qt, t)
        s.append((q[j - 1][1] + q[j - 1][2]) / 2 if j and t - q[j - 1][0] <= 120_000 and q[j - 1][2] - q[j - 1][1] <= 0.06 else None)
    out['polymarket'] = s
    return out


def jumps(s):
    """[(second it had made half the move, +1 / -1, size)] for every 3+ point move within 10 s."""
    out, last = [], -99
    for i in range(12, len(s) - 9):
        if i - last < 20 or s[i] is None or s[i - 10] is None or abs(s[i] - s[i - 10]) < JUMP: continue
        b, e = s[i - 12], s[i + 8]
        if b is None or e is None or abs(e - b) < JUMP: continue
        sg = 1 if e > b else -1; m = abs(e - b)
        tc = next((j for j in range(i - 12, i + 9) if s[j] is not None and sg * (s[j] - b) >= 0.5 * m), None)
        if tc is None: continue
        out.append((tc, sg, m)); last = i
    return out


def events(h, upto):
    """Events fully known by second `upto`: (time of the first bookmaker, first family, families in it, Polymarket's delay or None)."""
    J = sorted((tc, sg, m, f) for f, s in h.items() for tc, sg, m in jumps(s))
    books = [x for x in J if x[3] != 'polymarket']; pmj = [x for x in J if x[3] == 'polymarket']
    out, used = [], set()
    for i, (tc, sg, m, f) in enumerate(books):
        if i in used or tc + 60 > upto or tc < 40: continue
        grp = [(tc, f)]
        for k in range(i + 1, len(books)):
            if books[k][0] - tc > 60: break
            if books[k][1] == sg and k not in used: used.add(k); grp.append((books[k][0], books[k][3]))
        d = [p[0] - tc for p in pmj if p[1] == sg and -40 <= p[0] - tc <= 60]
        out.append((tc, f, [g[1] for g in grp], min(d, key=abs) if d else None, sg, m))
    return out


def summary(ev, trigger_family=None):
    n = len(ev)
    if not n: return dict(n=0, first=None, after=None, none=None, med=None, trig=None)
    got = [e[3] for e in ev if e[3] is not None]
    part = [e for e in ev if trigger_family in e[2]] if trigger_family else []
    return dict(n=n, first=sum(x <= -2 for x in got) / n, after=sum(x >= 2 for x in got) / n, none=(n - len(got)) / n, med=st.median(got) if got else None,
                trig=(sum(e[1] == trigger_family for e in part) / len(part)) if part else None, trig_n=len(part))


def verdict(sm):
    if sm['n'] < 2 or sm['med'] is None: return 'too few events'
    if sm['med'] >= 2 and sm['after'] >= sm['first']: return f"BEHIND by {sm['med']:.0f} s"
    if sm['med'] <= -2 and sm['first'] >= sm['after']: return f"AHEAD by {-sm['med']:.0f} s"
    return 'level'


if MODE == 'now':
    sys.path.insert(0, os.path.join(HERE, 'strategy_tests')); os.chdir(os.path.join(HERE, 'strategy_tests'))
    import sx
    now = int(time.time() * 1000)
    G = db.execute("SELECT m.id, m.sport, m.start_ms, b.home, b.away FROM matches m JOIN book_events b ON b.match_id = m.id AND b.book = 'polymarket' WHERE m.start_ms BETWEEN ? AND ? ORDER BY m.sport, m.start_ms", (now - 5 * 3600_000, now + 10 * 60_000)).fetchall()
    print(f"{'game':40s} {'sport':10s} {'Polymarket':>10s} {'bookmakers':>10s} {'gap':>6s}  {'events':>6s} {'PM first':>8s} {'PM after':>8s} {'no answer':>9s}  verdict (last 30 min)")
    for mid, sport, S, home, away in G:
        if ARG and sport != ARG: continue
        outs = [r[0] for r in db.execute("SELECT DISTINCT outcome FROM pm_quotes WHERE match_id=? AND ts_ms>=?", (mid, now - 120_000))]
        if len(outs) != 2: continue
        o = sorted(outs)[0]
        q = sx.pm(mid, o, now)
        if not q or now - q[2] > 120_000 or q[0] <= 0.03 or q[1] >= 0.97: continue
        state = {}
        for r in sx.book_rows(mid, now - 20 * 60_000, now): sx.apply(state, *r)
        g = sx.groups(state, sorted(outs))
        fair = st.median(v[o] for v in g.values()) if len(g) >= 2 else None
        mid_px = (q[0] + q[1]) / 2
        sm = summary(events(history(mid, o, now), WIN))
        f = lambda x: '   -' if x is None else f"{x * 100:5.0f}%"
        print(f"{(home + ' v ' + away)[:40]:40s} {sport:10s} {mid_px * 100:9.1f}c {'-' if fair is None else format(fair * 100, '9.1f') + 'c':>10s} {'' if fair is None else format((fair - mid_px) * 100, '+5.1f'):>6s}  {sm['n']:6d} {f(sm['first']):>8s} {f(sm['after']):>8s} {f(sm['none']):>9s}  {verdict(sm)}")
    sys.exit()

# ---------------------------------------------------------------- validate on trades
if MODE == 'validate':
    sys.path.insert(0, HERE)
    import sweep as S
    cache = {}
    def feats(m, o, t0, trigger):
        k = (m, o, t0 // 60_000)
        if k not in cache: cache[k] = events(history(m, o, t0), WIN - 5)
        return summary(cache[k], fam(trigger))
    def line(name, X):
        n = len(X)
        if not n: return f"  {name:62s} {0:4d}"
        w = sum(t['pnl'] > 0.005 for t in X); p = sum(t['pnl'] for t in X)
        return f"  {name:62s} {n:4d} {w * 100 // n:4d}% {p:+9.2f} {p / n:+6.2f} {sum(bool(t['caught']) for t in X) * 100 // n:5d}%"
    def report(title, T):
        print(f"\n== {title}"); print(f"  {'':62s} {'n':>4s} {'won':>5s} {'profit':>9s} {'each':>6s} {'caught':>6s}")
        print(line('all', T)); print(line('fewer than 2 earlier events in this game', [t for t in T if t['d']['n'] < 2]))
        K = [t for t in T if t['d']['n'] >= 2]
        print('  -- verdict for the game just before the trade')
        for v in ('BEHIND', 'level', 'AHEAD', 'too few'): print(line(f"   Polymarket {v}", [t for t in K if verdict(t['d']).startswith(v)]))
        print('  -- share of earlier events where Polymarket came 2+ s after the first bookmaker')
        for nm, lo, hi in (('under 20%', -1, 0.2), ('20-50%', 0.2, 0.5), ('50% or more', 0.5, 9)): print(line(f"   {nm}", [t for t in K if lo <= t['d']['after'] < hi]))
        print('  -- share of earlier events where Polymarket was first by 2+ s')
        for nm, lo, hi in (('under 20%', -1, 0.2), ('20-50%', 0.2, 0.5), ('50% or more', 0.5, 9)): print(line(f"   {nm}", [t for t in K if lo <= t['d']['first'] < hi]))
        print('  -- share of earlier events Polymarket did not answer')
        for nm, lo, hi in (('under 30%', -1, 0.3), ('30-60%', 0.3, 0.6), ('60% or more', 0.6, 9)): print(line(f"   {nm}", [t for t in K if lo <= t['d']['none'] < hi]))
        print("  -- the trigger's family in this game's earlier events: how often it was the FIRST bookmaker")
        P = [t for t in T if t['d'].get('trig') is not None and t['d']['trig_n'] >= 2]
        for nm, lo, hi in (('rarely first (under 30%)', -1, 0.3), ('sometimes (30-60%)', 0.3, 0.6), ('usually first (60% or more)', 0.6, 9)): print(line(f"   {nm}", [t for t in P if lo <= t['d']['trig'] < hi]))
        print(line('   (trigger family in fewer than 2 earlier events)', [t for t in T if t['d'].get('trig') is None or t['d']['trig_n'] < 2]))
    t_start = time.time(); out = {}
    LOOSE = dict(S.BASE, legacy=True, min_move=0.07, cons=0.0, fams=1, lag=None, gap=None, max_followed=0.15, min_edge=0.02, max_spread=0.06, cap=0.95, one_per_game=False)
    sets = [(title, [dict(pnl=t['pnl'], caught=t['caught'], m=t['c']['match_id'], o=t['c']['outcome'], t0=t['c']['t0'], trig=t['c']['trigger']) for t in S.sim(p)])
            for title, p in (("THE BOT'S RULES, backtest 17-19 Nov", dict(S.BASE, legacy=True)), ("LOOSER RULES, backtest 17-19 Nov", LOOSE))]
    live = []
    for i, data in db.execute("SELECT id, data FROM bot_trades WHERE real = 1 AND ts_ms >= 1795129200000 AND closed_ms IS NOT NULL ORDER BY ts_ms"):
        v = json.loads(data); live.append(dict(pnl=v['pnl'], caught=v.get('reason') == 'caught up', m=v['match_id'], o=v['outcome'], t0=v['move_ms'], trig=v['trigger'], id=i))
    sets.append(("LIVE real trades since 20 Nov", live))
    for title, T in sets:
        for t in T: t['d'] = feats(t['m'], t['o'], t['t0'], t['trig'])
        report(title, T)
        out[title] = [dict(t0=t['t0'], m=t['m'], pnl=round(t['pnl'], 3), caught=bool(t['caught']), d=t['d'], id=t.get('id')) for t in T]
    print('\nlive trades one by one:')
    for t in live: print(f"  #{t['id']} events {t['d']['n']:2d}  PM first {t['d']['first']}  after {t['d']['after']}  none {t['d']['none']}  median {t['d']['med']}  trigger first {t['d'].get('trig')} of {t['d'].get('trig_n')}  -> {verdict(t['d'])}   pnl {t['pnl']:+.2f}")
    json.dump(out, open(os.path.expanduser('~/swifteye/research/ahead_behind.json'), 'w'))
    print(f"\n{len(cache)} game windows read in {time.time() - t_start:.0f} s")
