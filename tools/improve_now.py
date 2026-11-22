"""The snipe bot AS IT RUNS NOW (moves 8+ pt, lagged earlier 50%+, no Dota 2): what separates its losers from its
winners, what would raise its accuracy, and what would raise its return (owner, 21 Nov 2026).
Clean replay trades only (17-20 Nov, tools/sweep.py with the recording-gap flag) plus the real trades since 20 Nov.
  1. losers v winners: every signal known at the buy, the middle value for each group, ranked by how well it separates
     them; the same comparison on two wider clean sets, to see whether a difference is more than this handful of trades
  2. accuracy levers: one extra rule at a time on today's rules (trades kept, share won, profit, worst trade)
  3. return levers: where the costs go by price, the stake by strength of the game, a resting sale
usage: improve_now.py"""
import sys, os, json, random, statistics as st
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
sys.argv = ['x', 'lib']
import tells as T
import maker_exit as MX
S = T.S
random.seed(21)
NOW = dict(S.BASE, legacy=False, skip=('dota2',), min_move=0.08, lag=0.5)
WIDE = dict(NOW, lag=0.3, min_move=0.07, max_followed=0.10, min_edge=0.03, one_per_game=False)
LOOSE = dict(NOW, min_move=0.07, cons=0.0, fams=1, gap=None, lag=0.3, max_followed=0.15, min_edge=0.02, max_spread=0.06, cap=0.95, one_per_game=False)
orig = S.gate
fl = lambda c: (c['cx']['follow'] - c['cx']['lead']) if c['cx']['jumps'] else None
G = T.G


def run(p, extra=None):
    S.gate = (lambda c, pp, cc: orig(c, pp, cc) and extra(c)) if extra else orig
    try: return S.sim(p)
    finally: S.gate = orig


def recs(p):
    out = []
    for t in run(p):
        r = T.at_buy(t['c'], t['entry']); r.update(pnl=t['pnl'], spent=t['spent'], ret=t['pnl'] / t['spent'], win=t['pnl'] > 0, caught=t['caught'], t=t)
        out.append(r)
    return out


def auc(a, b):
    """chance that a value drawn from a is above one drawn from b (0.5 = no difference)"""
    if not a or not b: return None
    return sum((x > y) + 0.5 * (x == y) for x in a for y in b) / (len(a) * len(b))


LAB = dict(move='size of the bookmaker move (pt)', price='price paid', off50='distance of the price from 50c', followed='share of the move Polymarket had already made', spread='spread',
           room='room left after costs', gap='how far bookmakers stood above Polymarket before the move', fams='bookmaker groups quoting the game', fam_moved='bookmaker groups that moved',
           fam_share='share of the bookmaker groups that moved', cons_move="move of the bookmakers' average", cons_share="average's move as a share of the trigger's", gap_shock='gap opened by the move',
           jumps='earlier jumps in this game', follow='share of earlier jumps Polymarket made AFTER the bookmakers', lead='share it made BEFORE them', score='delay score (after minus before)',
           z='move against the usual noise', pm_trend10="Polymarket's own move our way, last 10 min", book_tr5="bookmaker's net move our way, 5 min", book_tr10="bookmaker's net move our way, 10 min",
           book_range="bookmaker's 10-min range", move_vs_range='move against the 10-min range', mins_in='minutes since the listed start', depth_best='$ on offer at the best price',
           n2='moves in this game in the last 2 min', n10='moves in this game in the last 10 min', same10='share of those in the same direction', toward='how far the OTHER bookmakers had already moved',
           ev='ahead/behind events seen', q_age="seconds since Polymarket's price last changed", q_n60='Polymarket price changes in the last 60 s', q_upd='Polymarket re-quotes between the move and the buy',
           sz_tf='$ at the best price at the buy', drift10="Polymarket's drift our way, last 10 s", drift60="Polymarket's drift our way, last 60 s", k_gap='Kalshi above Polymarket, our way',
           oth_buy='how far the other bookmakers followed by the buy', n_fam60='bookmaker groups that moved in the last 60 s')
PCT = {'price', 'off50', 'spread', 'room', 'gap', 'cons_move', 'gap_shock', 'pm_trend10', 'book_tr5', 'book_tr10', 'book_range', 'drift10', 'drift60', 'k_gap', 'move'}
fmt = lambda f, x: (f"{100 * x:6.1f}c" if f in PCT else f"{x:7.2f}")

if __name__ == '__main__':
    A, W, L = recs(NOW), recs(WIDE), recs(LOOSE)
    print(f"TODAY'S RULES, clean replay: {len(A)} trades, {sum(r['win'] for r in A)} won, ${sum(r['pnl'] for r in A):+.2f} | wider clean set {len(W)} trades ({sum(r['win'] for r in W)} won) | loose clean set {len(L)} trades ({sum(r['win'] for r in L)} won)")
    print("\n1. LOSERS v WINNERS (middle value of each signal; 'separates' = chance a loser's value is above a winner's: 50% = no difference)")
    rows = []
    for f in T.NUM:
        lo = [r[f] for r in A if not r['win'] and r[f] is not None]; wi = [r[f] for r in A if r['win'] and r[f] is not None]
        if len(lo) < 5 or len(wi) < 8: continue
        a = auc(lo, wi)
        aw = auc([r[f] for r in W if not r['win'] and r[f] is not None], [r[f] for r in W if r['win'] and r[f] is not None])
        al = auc([r[f] for r in L if not r['win'] and r[f] is not None], [r[f] for r in L if r['win'] and r[f] is not None])
        rows.append((abs(a - 0.5), f, st.median(lo), st.median(wi), a, aw, al, len(lo), len(wi)))
    rows.sort(reverse=True)
    print(f"   {'signal':58s} {'losers':>8s} {'winners':>8s} | separates: today's rules / wider set / loose set | same way in all three")
    for d, f, ml, mw, a, aw, al, nl, nw in rows[:22]:
        same = aw is not None and al is not None and (a - 0.5) * (aw - 0.5) > 0 and (a - 0.5) * (al - 0.5) > 0 and abs(aw - 0.5) >= 0.04 and abs(al - 0.5) >= 0.04
        print(f"   {LAB.get(f, f):58s} {fmt(f, ml)} {fmt(f, mw)} | {100 * a:3.0f}% / {('%3.0f%%' % (100 * aw)) if aw is not None else '  - '} / {('%3.0f%%' % (100 * al)) if al is not None else '  - '} | {'YES' if same else ''}")
    print("   the losing trades themselves:")
    for r in sorted([r for r in A if not r['win']], key=lambda r: r['pnl']):
        c = r['c']
        print(f"     {c['_day']} Nov {c['sport']:7s} {(c['home'] + ' v ' + c['away'])[:30]:30s} paid {100 * r['price']:4.1f}c {r['pnl']:+6.2f} | move {100 * r['move']:4.1f} follow {r['follow']:.2f} score {r['score']:+.2f} groups {r['fam_moved']}/{r['fams']} spread {100 * r['spread']:.0f}c re-quotes {r['q_upd']} PM trend {100 * r['pm_trend10']:+.0f}c book trend {100 * r['book_tr10']:+.0f}c")

    print("\n2. ACCURACY LEVERS: one extra rule on today's rules (clean replay, bot's locks)")
    base = run(NOW)
    def line(name, tr):
        if not tr: print(f"   {name:60s} no trades"); return
        by = defaultdict(float); bb = defaultdict(float)
        for t in tr: by[t['c']['match_id']] += t['pnl']
        for t in base: bb[t['c']['match_id']] += t['pnl']
        ks = list(set(by) | set(bb)); v = sorted(sum(by.get(k, 0) - bb.get(k, 0) for k in (random.choice(ks) for _ in ks)) for _ in range(1500))
        pnl = sum(t['pnl'] for t in tr); days = [sum(t['pnl'] for t in tr if t['c']['_day'] == d) for d in ('17', '18', '19', '20')]
        print(f"   {name:60s} {len(tr):3d} trades | {100 * sum(t['pnl'] > 0 for t in tr) / len(tr):3.0f}% won | ${pnl:+7.2f} ({100 * pnl / sum(t['spent'] for t in tr):+5.1f}% a $) | worst {min(t['pnl'] for t in tr):+5.2f} | v today {pnl - sum(t['pnl'] for t in base):+6.2f} ({v[75]:+.0f} to {v[1425]:+.0f}) | days " + ' '.join(f"{x:+5.1f}" for x in days))
    qu = lambda c: (G.get(T.key(c)) or {}).get('q_upd')
    g = lambda c, k: (G.get(T.key(c)) or {}).get(k)
    line("TODAY'S RULES", base)
    for name, p, ex in (
        ("lagged earlier 70%+ (now 50%)", dict(NOW, lag=0.7), None), ("lagged earlier 100%+", dict(NOW, lag=1.0), None),
        ("delay score 0.2+ only (strong games)", NOW, lambda c: fl(c) is not None and fl(c) >= 0.2),
        ("half or more of the bookmaker groups moved", NOW, lambda c: c['cx']['fam_moved'] / max(c['cx']['fams'], 1) >= 0.5),
        ("bookmakers' average moved 5+ pt (now 3)", dict(NOW, cons=0.05), None),
        ("bookmaker's 10-min trend not against us (3 pt)", dict(NOW, book_trend=0.03), None),
        ("Polymarket's 10-min trend not against us (2 pt)", dict(NOW, pm_trend=0.02), None),
        ("price paid 30c or more", dict(NOW, floor=0.30), None), ("price paid 60c or less", dict(NOW, cap=0.60), None),
        ("spread 2c or less", dict(NOW, max_spread=0.02), None),
        ("Polymarket has not moved at all yet", dict(NOW, max_followed=0.0), None),
        ("at most 1 trade a game", dict(NOW, max_per_game=1), None),
        ("NO Polymarket re-quote between the move and the buy", NOW, lambda c: qu(c) == 0),
        ("Polymarket DID re-quote (the rest)", NOW, lambda c: qu(c) is not None and qu(c) > 0),
        ("Polymarket had not drifted our way in the last 60 s (1c)", NOW, lambda c: g(c, 'drift60') is not None and g(c, 'drift60') <= 0.01),
        ("other bookmakers had started to follow by the buy (10%+)", NOW, lambda c: g(c, 'oth_buy') is not None and g(c, 'oth_buy') >= 0.10),
        ("room after costs 6c+ (now 4c)", dict(NOW, min_edge=0.06), None),
        ("CS2 only", NOW, lambda c: c['sport'] == 'cs2'), ("tennis only", NOW, lambda c: c['sport'] == 'tennis'),
    ):
        line(name, run(p, ex))

    print("\n3. RETURN LEVERS")
    print("   a. what a trade costs, by the price paid (today's rules; fee in + fee out + 1c off the sale, as a share of the stake)")
    for lo, hi in ((0.05, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 0.92)):
        x = [t for t in base if lo <= t['entry'] < hi]
        if not x: continue
        cost = st.mean((S.fee(t['entry']) + (S.fee(t['exit']) if 0 < t['exit'] < 1 else 0) + 0.01) / t['entry'] for t in x)
        gross = sum(t['pnl'] for t in x) / sum(t['spent'] for t in x) + cost
        print(f"      paid {100 * lo:2.0f}-{100 * hi:2.0f}c: {len(x):2d} trades, {100 * sum(t['pnl'] > 0 for t in x) / len(x):3.0f}% won, costs {100 * cost:4.1f}% of the stake, made {100 * (gross - cost):+5.1f}% after them ({100 * gross:+5.1f}% before)")
    print("   b. the stake by the strength of the game ($40 start, 25% of the wallet, $12 ceiling, nothing under $5)")
    def grow(rule):
        w = 40.0; low = w; n = 0
        for t in sorted(base, key=lambda t: t['c']['tf']):
            sc = fl(t['c']); k = rule(sc)
            s = min(0.25 * w, 12.0) * k
            if s < 5.0: continue
            w += s * t['pnl'] / t['spent']; low = min(low, w); n += 1
        return w, low, n
    for nm, rule in (("full stake on every trade", lambda s: 1.0), ("half stake in weak games (today)", lambda s: 1.0 if s is not None and s >= 0.2 else 0.5), ("strong games only", lambda s: 1.0 if s is not None and s >= 0.2 else 0.0),
                     ("strong games full, weak games a quarter", lambda s: 1.0 if s is not None and s >= 0.2 else 0.25)):
        w, low, n = grow(rule); print(f"      {nm:42s} $40 -> ${w:6.2f} ({n} trades taken, lowest ${low:.2f})")
    print("   c. selling with a resting order at the target instead of hitting the bid")
    vals = [MX.resting(t) for t in base]; tot = sum(v for v, f in vals)
    print(f"      today ${sum(t['pnl'] for t in base):+.2f} -> ${tot:+.2f} ({tot - sum(t['pnl'] for t in base):+.2f}); sold at the target in {sum(f for v, f in vals)} of {len(base)}")
