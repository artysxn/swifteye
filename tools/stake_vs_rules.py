"""Confirm or refute: 'an extra rule that cuts profit to cut risk is worse than the plain bot at a smaller stake'.
For each extra rule and each way of measuring risk, the plain bot's stake is scaled down until its risk EQUALS the
rule's risk at $12; then the two profits are compared. Risk measures: spread of a day's result; the usual and the
bad (1 in 20) biggest drop in a month; the chance a day ends under -$15 (the bot's day stop); the dollars lost on
losing trades. Months are simulated two ways: trades drawn one by one, and whole GAMES drawn together.
Repeated without 18 Nov (the day with 90% of the profit) and on each half of the games.
usage: stake_vs_rules.py"""
import sys, os, math, random, statistics as st
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
src = open(os.path.join(HERE, 'systems_backtest.py')).read().split("HDR = f\"")[0]
exec(compile(src, 'systems_backtest_head', 'exec'))
random.seed(31)
SPAN = (S.ROWS[-1]['tf'] - S.ROWS[0]['tf']) / 86_400_000


def risk(T, days, n_sim=4000):
    """Risk of a trade list at $12 a trade, played at its own pace for 30 days."""
    p = [t['pnl'] for t in T]; rate = len(T) / days
    by = defaultdict(list)
    for t in sorted(T, key=lambda t: t['c']['tf']): by[t['c']['match_id']].append(t['pnl'])
    games = list(by.values()); n_month = max(1, round(rate * 30)); per_day = max(1, round(rate))
    out = {}
    for mode in ('trades', 'games'):
        dd = []; bad_days = 0; n_days = 0
        for _ in range(n_sim):
            seq = []
            while len(seq) < n_month:
                seq += [random.choice(p)] if mode == 'trades' else random.choice(games)
            seq = seq[:n_month]
            cum = peak = d = 0.0
            for x in seq:
                cum += x; peak = max(peak, cum); d = max(d, peak - cum)
            dd.append(d)
            for i in range(0, len(seq), per_day):
                n_days += 1; bad_days += sum(seq[i:i + per_day]) <= -15
        dd.sort()
        out[mode] = dict(dd50=dd[len(dd) // 2], dd95=dd[int(len(dd) * .95)], bad_day=bad_days / n_days)
    return dict(n=len(T), rate=rate, profit=sum(p), month=sum(p) / days * 30, day_sd=st.pstdev(p) * math.sqrt(rate), lost=-sum(x for x in p if x < 0), sims=out,
                worst=min(p))


def bad_day_stake(T, days, target, mode):
    """The plain stake (as a share of $12) at which the chance of a -$15 day equals `target`: by search."""
    p = [t['pnl'] for t in T]; rate = len(T) / days; per_day = max(1, round(rate))
    by = defaultdict(list)
    for t in T: by[t['c']['match_id']].append(t['pnl'])
    games = list(by.values())
    sims = []
    for _ in range(6000):
        seq = []
        while len(seq) < per_day: seq += [random.choice(p)] if mode == 'trades' else random.choice(games)
        sims.append(sum(seq[:per_day]))
    lo, hi = 0.05, 1.5
    for _ in range(18):
        s = (lo + hi) / 2
        if sum(x * s <= -15 for x in sims) / len(sims) > target: hi = s
        else: lo = s
    return (lo + hi) / 2


def compare(title, keep, days):
    B = [t for t in run(BASE) if keep(t)]
    rb = risk(B, days)
    print(f"\n== {title}: the plain bot {rb['n']} trades, {rb['profit']:+.2f} ({rb['month']:+.0f} a month at this pace), a day's spread ${rb['day_sd']:.1f}, lost on losers ${rb['lost']:.0f}, usual / bad drop ${rb['sims']['games']['dd50']:.0f} / ${rb['sims']['games']['dd95']:.0f}, -$15 day {rb['sims']['games']['bad_day'] * 100:.1f}%")
    print(f"  {'extra rule':34s} {'trades':>6s} {'profit':>8s} | plain bot's stake with the SAME risk -> its profit (rule's profit {'':8s}) by each risk measure")
    wins = total = 0
    for name, ex in (('not AHEAD', notahead), ('follows (follow - lead 0.2+)', follows), ('trigger moved first', first), ('first and follows', AND(first, follows)), ('first, follows, not AHEAD', AND(first, follows, notahead))):
        X = [t for t in run(BASE, ex) if keep(t)]
        if len(X) < 8: print(f"  {name:34s} {len(X):6d} too few"); continue
        rx = risk(X, days); cells = []
        measures = [('day spread', rx['day_sd'] / rb['day_sd']), ('$ lost', rx['lost'] / rb['lost']),
                    ('usual drop, trades', rx['sims']['trades']['dd50'] / rb['sims']['trades']['dd50']), ('bad drop, trades', rx['sims']['trades']['dd95'] / rb['sims']['trades']['dd95']),
                    ('usual drop, games', rx['sims']['games']['dd50'] / rb['sims']['games']['dd50']), ('bad drop, games', rx['sims']['games']['dd95'] / rb['sims']['games']['dd95']),
                    ('-$15 day, games', bad_day_stake(B, days, rx['sims']['games']['bad_day'], 'games') if rx['sims']['games']['bad_day'] > 0 else None)]
        for nm, s in measures:
            if s is None: cells.append(f"{nm}: rule never has one"); total += 1; continue
            s = min(s, 1.5); pb = rb['profit'] * s; better = pb > rx['profit']; wins += better; total += 1
            cells.append(f"{nm}: ${12 * s:4.1f} -> {pb:+6.1f} {'PLAIN' if better else 'RULE '}")
        print(f"  {name:34s} {rx['n']:6d} {rx['profit']:+8.2f} | " + ' | '.join(cells))
    print(f"  the plain bot at a smaller stake wins {wins} of {total} comparisons")


compare("ALL FOUR DAYS (17-20 Nov)", lambda t: True, SPAN)
compare("WITHOUT 18 NOV", lambda t: t['c']['_day'] != '18', SPAN - 1)
compare("HALF A of the games", lambda t: half(t['c']['match_id']) == 0, SPAN)
compare("HALF B of the games", lambda t: half(t['c']['match_id']) == 1, SPAN)
