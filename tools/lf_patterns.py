"""PATTERNS FIRST (owner, 21 Nov 2026: "analyze the patterns first then build a strategy around it").

What does Polymarket do after a bookmaker's line jumps and Polymarket has not yet made that move?
No strategy here: only what happens, how often, how big, how fast, and which conditions change it.

One EVENT = one game, one side, one jump: bookmakers jumping within 20 s of each other are one event (the first one
is the event; how many jumped is kept). Data: lf_build.py. Polymarket by its own clock.

For every event:
    response   how much of the jump Polymarket's SELL price had made 10 / 30 / 60 / 120 / 300 s after a buy could land
    best       the highest sell price in the 5 minutes, and when
    round trip buy at the offer when the order lands, sell at the sell price H seconds later, both fees ($12 stake)

usage: lf_patterns.py            everything, found on 17-19 Nov with 20-21 Nov beside it
       lf_patterns.py sample
"""
import os, sys, pickle, time, statistics as st, collections

STAKE = 12.0; PS = 2
fee = lambda p: 0.05 * p * (1 - p) if 0 < p < 1 else 0.0
SPLIT = 1795129200000
HZ = (10, 30, 60, 120, 300)


def load(sample=False, name='lf_cands'):
    d = pickle.load(open(os.path.expanduser('~/swifteye/research/%s%s.pickle' % (name, '_sample' if sample else '')), 'rb')); return d['cands']


def events(C, gap=20_000):
    """First jump of each cluster; `books` = how many bookmakers (and families) jumped within `gap` after it."""
    last = {}; out = []
    for c in C:
        k = (c['m'], c['o']); e = last.get(k)
        if e is not None and c['t'] - e['t'] <= gap:
            e['books'].add(c['book']); e['fams'].add(FAM.get(c['book'], c['book'])); e['maxsize'] = max(e['maxsize'], c['size']); continue
        e = dict(c); e['books'] = {c['book']}; e['fams'] = {FAM.get(c['book'], c['book'])}; e['maxsize'] = c['size']; last[k] = e; out.append(e)
    return out


FAM = {'linebet': '1xbet', 'melbet': '1xbet', 'betwinner': '1xbet', 'megapari': '1xbet', 'paripesa': '1xbet', '22bet': '1xbet', '22bit': '1xbet', '1xbit': '1xbet',
       'unibet': 'kambi', 'leovegas': 'kambi', 'paf': 'kambi', 'fonbet': 'fonbet', 'pari': 'fonbet', 'mostbet': 'fonbet', 'vinn': 'altenar', 'betking': 'altenar'}


def measure(e):
    """Adds to an event: bid path facts and round trips."""
    pb = e['pb']; a = e['ask_e']; n = STAKE / a; size = e['size']
    def bid(sec):
        k = min(sec // PS, len(pb) - 1)
        while k >= 0 and pb[k] == 255: k -= 1
        return pb[k] / 100.0 if k >= 0 else None
    e['r'] = {}; e['rt'] = {}
    for h in HZ:
        b = bid(h)
        e['r'][h] = None if b is None else (b - e['bid_d']) / size             # share of the jump the sell price made after the decision
        e['rt'][h] = None if b is None else n * ((b - 0.01) - a) - n * (fee(a) + fee(b))
    v = [(pb[k], k) for k in range(1, len(pb)) if pb[k] != 255]
    if v:
        top = max(x[0] for x in v); e['best'] = top / 100.0; e['best_s'] = next(k for x, k in v if x == top) * PS
        e['best_rt'] = n * ((e['best'] - 0.01) - a) - n * (fee(a) + fee(e['best'])); e['best_share'] = (e['best'] - e['bid_d']) / size
        low = min(x[0] for x in v); e['worst_rt'] = n * ((low / 100.0 - 0.01) - a) - n * (fee(a) + fee(low / 100.0))
    else:
        e['best'] = None
    # the leader 30 s and 120 s later: still up?
    pl = e['pl']
    def lead(sec):
        k = min(sec // PS, len(pl) - 1); return None if pl[k] == 255 else (pl[k] / 100.0 - e['base']) / size
    e['l30'], e['l120'] = lead(30), lead(120)
    e['spread'] = e['ask_d'] - e['bid_d']; e['pre'] = (e['m1'] - e['m60']) / size; e['pre10'] = (e['m1'] - e['m10']) / size
    return e


def row(name, E, w=44):
    E = [e for e in E if e.get('best') is not None]
    if len(E) < 15: print('  %-*s %5d  (too few)' % (w, name, len(E))); return
    med = lambda v: st.median(v) if v else float('nan')
    r60 = [e['r'][60] for e in E if e['r'][60] is not None]
    rt = {h: [e['rt'][h] for e in E if e['rt'][h] is not None] for h in HZ}
    print('  %-*s %5d | Polymarket made of the jump after 60 s: median %4.0f%%, half or more %3.0f%% | best sell in 5 min: median %4.0f%% of the jump after %3.0f s | round trip $ a trade: 30 s %+5.2f  60 s %+5.2f  120 s %+5.2f  300 s %+5.2f | at the best moment %+5.2f | won at 60 s %3.0f%%' % (
        w, name, len(E), 100 * med(r60), 100.0 * sum(1 for x in r60 if x >= 0.5) / max(len(r60), 1), 100 * med([e['best_share'] for e in E]), med([e['best_s'] for e in E]),
        st.mean(rt[30]), st.mean(rt[60]), st.mean(rt[120]), st.mean(rt[300]), st.mean([e['best_rt'] for e in E]), 100.0 * sum(1 for x in rt[60] if x > 0) / max(len(rt[60]), 1)))


def table(title, E, key, cuts=None, fmt='%g', top=None):
    print('\n' + title)
    for part, lab in ((lambda e: e['t'] < SPLIT, '17-19 Nov'), (lambda e: e['t'] >= SPLIT, '20-21 Nov')):
        S = [e for e in E if part(e)]
        print(' ' + lab)
        if cuts:
            for lo, hi in cuts:
                row((fmt + ' to ' + fmt) % (lo, hi), [e for e in S if key(e) is not None and lo <= key(e) < hi])
            row('not known', [e for e in S if key(e) is None])
        else:
            g = collections.defaultdict(list)
            for e in S: g[key(e)].append(e)
            for k, v in sorted(g.items(), key=lambda x: -len(x[1]))[:top or 12]: row(str(k), v)


if __name__ == '__main__':
    sample = len(sys.argv) > 1 and sys.argv[1] == 'sample'
    C = load(sample); E = [measure(e) for e in events(C)]; E = [e for e in E if e.get('best') is not None and e['spread'] <= 0.06 + 1e-9]
    print('bookmaker jumps of 4pt+ that Polymarket had not yet made: %d jumps, %d separate events in %d games' % (len(C), len(E), len(set(e['m'] for e in E))))
    print('\nALL EVENTS'); row('17-19 Nov', [e for e in E if e['t'] < SPLIT]); row('20-21 Nov', [e for e in E if e['t'] >= SPLIT])
    sb = lambda i: (lambda e: (e['sb'] if e['sb'][0] >= 3 else e['sf'])[i] if (e['sb'] if e['sb'][0] >= 3 else e['sf'])[1] >= 2 else None)
    table('1. SIZE OF THE JUMP (points)', E, lambda e: 100 * e['size'], [(4, 6), (6, 8), (8, 12), (12, 20), (20, 100)])
    table('2. HOW MANY BOOKMAKER GROUPS JUMPED within 20 s', E, lambda e: len(e['fams']), [(1, 2), (2, 3), (3, 4), (4, 99)])
    table('3. WHAT POLYMARKET DID IN THE MINUTE BEFORE (share of the jump; minus = it went the other way)', E, lambda e: e['pre'], [(-9, -0.25), (-0.25, -0.05), (-0.05, 0.05), (0.05, 0.25), (0.25, 0.6)], fmt='%.2f')
    table('4. DELAY SCORE of the bookmaker in this game (s; + = it was ahead of Polymarket on its earlier jumps)', E, sb(4), [(-999, -20), (-20, -3), (-3, 3.01), (3.01, 20), (20, 999)])
    table('5. FIRST SHARE (how often that bookmaker was first)', E, lambda e: (lambda s: s[2] / s[1] if s[1] >= 2 else None)(e['sb'] if e['sb'][0] >= 3 else e['sf']), [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 1.01)], fmt='%.1f')
    table('6. FOLLOW (share of that bookmaker\'s earlier jumps Polymarket also made)', E, sb(6), [(0, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)], fmt='%.1f')
    table('7. RESPONSE (how much of its earlier jumps Polymarket usually made)', E, sb(5), [(0, 0.3), (0.3, 0.6), (0.6, 0.9), (0.9, 9)], fmt='%.1f')
    table('8. CORRELATION SCORE of the pair over the last half hour', E, lambda e: e['corr'], [(0, 50), (50, 80), (80, 92), (92, 101)])
    table('9. EARLIER JUMPS of that bookmaker in the game (how much is known)', E, lambda e: e['sb'][0], [(0, 1), (1, 3), (3, 8), (8, 20), (20, 9999)])
    table('10. SPORT', E, lambda e: e['sport'])
    table('11. BOOKMAKER that jumped first', E, lambda e: e['book'], top=14)
    table('12. PRICE of the side (offer, cents)', E, lambda e: 100 * e['ask_e'], [(4, 20), (20, 40), (40, 60), (60, 80), (80, 94)])
    table('13. SPREAD on Polymarket (cents)', E, lambda e: round(100 * e['spread']), [(0, 2), (2, 3), (3, 5), (5, 7)])
    table('14. JUMP RIGHT AFTER A PAUSE of that bookmaker (seconds paused; 0 = no pause)', E, lambda e: e['pause'], [(0, 1), (1, 60), (60, 100000)])
    table('15. WHAT THE BOOKMAKER DID NEXT (share of its jump still there after 30 s) - known only afterwards', E, lambda e: e['l30'], [(-9, 0.25), (0.25, 0.75), (0.75, 1.1), (1.1, 9)], fmt='%.2f')
    table('16. HOUR OF THE DAY (Norway)', E, lambda e: int(time.strftime('%H', time.localtime(e['t'] / 1000))) // 4 * 4, [(0, 4), (4, 8), (8, 12), (12, 16), (16, 20), (20, 24)])
