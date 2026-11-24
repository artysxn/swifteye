"""Would arbitrage across 15-20 bookmakers pay? What the tracker's own week says (24 Nov 2026).

  python tools/arb/arb_books.py ~/swifteye/oddsfeed.db [--out ~/swifteye/research]

Source: `arb_history` (the live tracker, 17-23 Nov: pre-match, prices seen in the last 5 min, both exchange fees in)
and, for each opportunity, that game's whole recorded price history (`changes`, winner market).

1. WHO IS THE ODD ONE OUT. At the moment an opportunity opened, every other bookmaker's prices give a fair chance for
   each outcome (margin taken out per book, median over books). A leg's EDGE = its odds x that fair chance - 1.
   In an arbitrage one leg carries the edge (the mispriced one) and the other is an ordinary price that only hedges.
   Whether a bookmaker limits you depends on which of the two you place with it.
2. HOW IT OPENED AND CLOSED: did the odd leg's own price move (a book quoting something odd), or did the rest of the
   market move away from it (a slow book)? Did it close by the odd leg correcting?
3. WHICH BOOKMAKERS CARRY IT: the best set of N accounts (Polymarket always in), N = 1..20, by what it would have
   caught. An opportunity needs ALL its legs' books in the set. The tracker keeps only the best-priced combination of
   each opportunity, so a smaller set is understated a little (a second-best book could have made the same arbitrage).
4. DOLLARS, under stated assumptions (stake per opportunity, how long it must stay open, share that fails).
Nothing here sees a bet slip, a stake limit or an account restriction.
"""
import argparse
import bisect
import collections
import json
import os
import sqlite3
import statistics as st
import time

import numpy as np

PRED = {'polymarket', 'kalshi'}
# One price feed behind several brands: an edge "between" them is one book's edge, and one limit policy.
FAMILY = {'unibet': 'kambi', 'leovegas': 'kambi', 'paf': 'kambi', 'fonbet': 'fonbet', 'pari': 'fonbet',
          'melbet': '1xbet', 'linebet': '1xbet', 'betwinner': '1xbet', '22bet': '1xbet', '22bit': '1xbet', '1xbit': '1xbet', 'megapari': '1xbet', 'paripesa': '1xbet',
          'vinn': 'altenar', 'betking': 'altenar'}
# Our own knowledge of the feeds (AGENTS.md): prices that can be old at the source.
STALE_FEED = {'pinnacle': 'our Pinnacle feed is a delayed public copy (22 Nov)', 'kalshi': 'the Kalshi feed was broken until 24 Nov'}
OUTS = ('home', 'draw', 'away')


def load(db):
    c = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
    rows = c.execute('SELECT key, match_id, sport, market, legs, first_ms, last_ms, end_reason, open_profit, peak_profit, takeable FROM arb_history ORDER BY first_ms').fetchall()
    K = {}
    for key, m, sp, mk, legs, f, l, er, op, pk, tk in rows:
        k = K.setdefault(key, dict(key=key, m=m, sp=sp, mk=mk, first=f, last=l, life=0.0, open=op or 0.0, peak=0.0, legs=json.loads(legs), take=0.0, end=er, eps=0))
        k['life'] += (l - f) / 1000
        k['peak'] = max(k['peak'], pk or 0.0)
        k['last'] = max(k['last'], l)
        k['take'] = max(k['take'], tk or 0.0)
        k['end'] = er
        k['eps'] += 1
    days = (max(r[6] for r in rows) - min(r[5] for r in rows)) / 86400e3
    return c, list(K.values()), days


def history(c, m, until):
    """book -> outcome -> ([times], [prices or None]) for the winner market of one game, up to `until`."""
    H = collections.defaultdict(lambda: collections.defaultdict(lambda: ([], [])))
    for ts, book, o, price in c.execute("SELECT ts_ms, book, outcome, new_price FROM changes WHERE match_id = ? AND ts_ms <= ? AND market = '1x2' ORDER BY ts_ms", (m, until)):
        t, p = H[book][o]
        t.append(ts)
        p.append(price)
    return H


def at(series, t):
    ts, ps = series
    i = bisect.bisect_right(ts, t) - 1
    return (ps[i], ts[i]) if i >= 0 else (None, None)


def fair(H, t, skip):
    """Median over bookmakers (not `skip`, not the prediction markets) of each outcome's chance with the margin out."""
    per = collections.defaultdict(list)
    for book, outs in H.items():
        if book in PRED or FAMILY.get(book, book) in skip:
            continue
        inv = {}
        for o in OUTS:
            if o in outs:
                p, _ = at(outs[o], t)
                if p and p > 1.0:
                    inv[o] = 1.0 / p
        # A two-way game has home + away, a three-way one all three; a book showing only part is left out.
        if len(inv) >= 2 and ('draw' in inv or 'draw' not in outs or at(outs['draw'], t)[0] is None):
            s = sum(inv.values())
            if 1.0 < s < 1.25:
                for o, v in inv.items():
                    per[o].append(v / s)
    return {o: st.median(v) for o, v in per.items()}, max((len(v) for v in per.values()), default=0)


def study(c, opps, say):
    t0 = time.time()
    by_m = collections.defaultdict(list)
    for o in opps:
        by_m[o['m']].append(o)
    for n, (m, lst) in enumerate(by_m.items()):
        H = history(c, m, max(o['last'] for o in lst) + 120_000)
        start = c.execute('SELECT start_ms FROM matches WHERE id = ?', (m,)).fetchone()
        pmq = None
        for o in lst:
            t = o['first'] + 2_000
            o['to_start'] = (start[0] - o['first']) / 3_600_000 if start and start[0] else None
            # SISTER BOOKS share one price feed (Unibet / LeoVegas / Paf; Fonbet / Pari; the 1xBet brands): each is polled
            # on its own, so a leg whose sister shows the same price at that moment was really on that feed then.
            sis = []
            for l in o['legs']:
                fam = FAMILY.get(l['book'])
                if not fam or l['side'] != 'back':
                    continue
                got = [at(H[b][l['outcome']], t)[0] for b in H if b != l['book'] and FAMILY.get(b) == fam and l['outcome'] in H[b]]
                got = [x for x in got if x]
                if got:
                    sis.append(any(abs(x - l['odds']) / l['odds'] < 0.015 for x in got))
            o['sis'] = sis
            # POLYMARKET LEG against the recorded quotes: the price the leg assumed, the ask there right then, how much
            # rested at it, and how long before that Polymarket last sent anything for this game.
            for l in o['legs']:
                if l['book'] != 'polymarket':
                    continue
                if pmq is None:
                    pmq = collections.defaultdict(lambda: ([], []))
                    newest = []
                    for ts, oc, bid, ask, bsz, asz in c.execute('SELECT ts_ms, outcome, bid, ask, bid_size, ask_size FROM pm_quotes WHERE match_id = ? AND ts_ms <= ? ORDER BY ts_ms', (m, max(x['last'] for x in lst) + 120_000)):
                        pmq[oc][0].append(ts)
                        pmq[oc][1].append((bid, ask, bsz, asz))
                        newest.append(ts)
                target = l.get('against') if l['side'] == 'no' else l['outcome']
                q, qt = at(pmq[target], t) if target in pmq else (None, None)
                j = bisect.bisect_right(newest, t) - 1
                o['pm_silent'] = (t - newest[j]) / 1000 if j >= 0 else None
                if q:
                    px = (1 - q[0]) if (l['side'] == 'no' and q[0]) else (q[1] if l['side'] == 'yes' else None)
                    if px and 0 < px < 1:
                        o['pm_odds_now'] = 1 / (px + 0.05 * px * (1 - px))
                        o['pm_size'] = (q[2] if l['side'] == 'no' else q[3])
                        o['pm_quote_age'] = (t - qt) / 1000
            fams = {FAMILY.get(l['book'], l['book']) for l in o['legs']}
            f, nb = fair(H, t, fams)
            o['refs'] = nb
            edges = []
            for l in o['legs']:
                target = l.get('against') if l['side'] == 'no' else l['outcome']
                pf = f.get(target)
                if pf is None:
                    edges.append(None)
                    continue
                # A "no" leg pays when `against` does NOT happen.
                edges.append(l['odds'] * ((1 - pf) if l['side'] == 'no' else pf) - 1)
            o['edges'] = edges
            if any(e is None for e in edges) or nb < 2:
                o['odd'] = None
                continue
            i = int(np.argmax(edges))
            o['odd'] = o['legs'][i]['book']
            o['odd_edge'] = edges[i]
            o['hedge_edge'] = min(edges)
            # how it opened: the odd leg's own book moved in the 2 minutes before, or it did not (the others left it behind)
            book = o['odd']
            if book in H:
                ts_all = sorted(t for outs in H[book].values() for t in outs[0])
                j = bisect.bisect_right(ts_all, o['first'] + 2_000) - 1
                o['odd_age'] = (o['first'] - ts_all[j]) / 1000 if j >= 0 else None
                k = bisect.bisect_right(ts_all, o['last'] + 60_000) - 1
                o['odd_moved_in'] = bisect.bisect_right(ts_all, o['last']) - bisect.bisect_right(ts_all, o['first'] + 2_000)
                o['odd_closed_it'] = k >= 0 and o['last'] - 15_000 <= ts_all[k] <= o['last'] + 60_000 and any(o['last'] - 15_000 <= x <= o['last'] + 60_000 for x in ts_all)
            else:
                o['odd_age'] = o['odd_moved_in'] = None
                o['odd_closed_it'] = None
        if (n + 1) % 400 == 0:
            print(f'  {n + 1} of {len(by_m)} games, {time.time() - t0:.0f} s', flush=True)
    say(f'price history read for {len(by_m)} games in {time.time() - t0:.0f} s')
    import pickle
    pickle.dump(opps, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', 'swifteye', 'research', 'arb_study.pkl'), 'wb'))


def greedy(opps, days, value, candidates, steps=20, fixed=('polymarket',)):
    S = set(fixed)
    out = []
    for _ in range(steps):
        best = None
        for b in candidates - S:
            T = S | {b}
            v = sum(value(o) for o in opps if {l['book'] for l in o['legs']} <= T)
            if best is None or v > best[0]:
                best = (v, b)
        if best is None:
            break
        S.add(best[1])
        got = [o for o in opps if {l['book'] for l in o['legs']} <= S]
        out.append((best[1], len(got) / days, sum(value(o) for o in got) / days))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('db')
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    lines = []
    say = lambda s='': (lines.append(s), print(s, flush=True))
    c, opps, days = load(os.path.expanduser(a.db))
    say(f'{len(opps)} opportunities in {days:.1f} days ({len(opps) / days:.0f} a day), pre-match, as the live tracker saw them')
    study(c, opps, say)
    known = [o for o in opps if o.get('odd')]
    say(f'the odd leg could be named for {len(known)} ({100 * len(known) / len(opps):.0f}%): 2+ other bookmakers priced the game at that moment')

    say('\n1. WHO IS THE ODD ONE OUT (the leg whose price beats the rest of the market)')
    say(f'   {"odd leg at":<14}{"opps":>6} {"a day":>6} | odd leg edge (median) | hedge leg edge | locked profit open / peak | open time (median) | odd book moved its price while open | closed by the odd book moving')
    grp = collections.defaultdict(list)
    for o in known:
        grp[o['odd']].append(o)
    for book, v in sorted(grp.items(), key=lambda kv: -len(kv[1])):
        if len(v) < 8:
            continue
        moved = [o for o in v if o.get('odd_moved_in') is not None]
        say(f'   {book:<14}{len(v):>6} {len(v) / days:>6.1f} | {100 * st.median(o["odd_edge"] for o in v):>+10.1f}%          | {100 * st.median(o["hedge_edge"] for o in v):>+8.1f}%      | {100 * st.median(o["open"] for o in v):>+6.2f}% / {100 * st.median(o["peak"] for o in v):>+6.2f}%       | {st.median(o["life"] for o in v):>8.0f} s        | '
            f'{100 * sum(1 for o in moved if o["odd_moved_in"] > 0) / max(len(moved), 1):>5.0f}% of them                       | {100 * sum(1 for o in moved if o["odd_closed_it"]) / max(len(moved), 1):>5.0f}%')
    pm_odd = [o for o in known if o['odd'] == 'polymarket']
    with_pm = [o for o in known if any(l['book'] == 'polymarket' for l in o['legs'])]
    say(f'   opportunities with a Polymarket leg: {len(with_pm)}; Polymarket is the odd leg in {len(pm_odd)} ({100 * len(pm_odd) / max(len(with_pm), 1):.0f}%), a bookmaker in the rest')
    stale = [o for o in known if o['odd'] in STALE_FEED]
    say(f'   the odd leg is a feed we know can be old at the source ({", ".join(STALE_FEED)}): {len(stale)} ({100 * len(stale) / len(known):.0f}%)')

    say('\n2. WHAT IS LEFT AFTER THE OBVIOUS CUTS')
    cuts = [('all', lambda o: True),
            ('no Kalshi leg (not wired, feed was broken)', lambda o: all(l['book'] != 'kalshi' for l in o['legs'])),
            ('+ no Pinnacle leg (our feed of it is a delayed copy)', lambda o: all(l['book'] not in ('kalshi', 'pinnacle') for l in o['legs'])),
            ('+ open 60 s or more in all', lambda o: all(l['book'] not in ('kalshi', 'pinnacle') for l in o['legs']) and o['life'] >= 60),
            ('+ locked profit 1%+ when first seen', lambda o: all(l['book'] not in ('kalshi', 'pinnacle') for l in o['legs']) and o['life'] >= 60 and o['open'] >= 0.01),
            ('+ under 5% (above it: two different games or bets linked as one)', lambda o: all(l['book'] not in ('kalshi', 'pinnacle') for l in o['legs']) and o['life'] >= 60 and 0.01 <= o['open'] < 0.05),
            ('  of those, 2%+', lambda o: all(l['book'] not in ('kalshi', 'pinnacle') for l in o['legs']) and o['life'] >= 60 and 0.02 <= o['open'] < 0.05)]
    for name, f in cuts:
        v = [o for o in opps if f(o)]
        if not v:
            continue
        say(f'   {name:<56}{len(v):>6} = {len(v) / days:>5.1f} a day | locked when first seen: median {100 * st.median(o["open"] for o in v):+.2f}%, mean {100 * np.mean([o["open"] for o in v]):+.2f}% | $ a day at $100 an opportunity: {100 * sum(o["open"] for o in v) / days:>6.2f}')
    clean = lambda o: all(l['book'] not in ('kalshi', 'pinnacle') for l in o['legs']) and o['life'] >= 60 and 0.01 <= o['open'] < 0.05
    big = sorted((o for o in opps if o['open'] >= 0.05 and all(l['book'] not in ('kalshi', 'pinnacle') for l in o['legs'])), key=lambda o: -o['open'])
    say(f'   the {len(big)} at 5%+ carry ${100 * sum(o["open"] for o in big) / days:.0f} a day of the face value; the largest: ' + '; '.join(f'{100 * o["open"]:.0f}% {o["sp"]} ' + ' + '.join(f'{l["book"]} @{l["odds"]:.2f}' for l in o['legs']) for o in big[:5]))

    say('\n2b. ARE THE PRICES REAL? (the kept band: 1-5%, open 60 s+, no Kalshi / Pinnacle leg)')
    kept = [o for o in opps if clean(o)]
    s_all = [x for o in kept for x in o.get('sis', [])]
    say(f'   legs at a bookmaker with a sister on the same price feed, sister read at the same moment: {len(s_all)}; the sister showed the same price in {sum(s_all)} ({100 * sum(s_all) / max(len(s_all), 1):.0f}%)')
    pm = [o for o in kept if any(l['book'] == 'polymarket' for l in o['legs'])]
    have = [o for o in pm if o.get('pm_odds_now')]
    def pm_leg(o):
        return next(l for l in o['legs'] if l['book'] == 'polymarket')
    same = [o for o in have if abs(o['pm_odds_now'] - pm_leg(o)['odds']) / pm_leg(o)['odds'] < 0.01]
    worse = [o for o in have if o['pm_odds_now'] < pm_leg(o)['odds'] * 0.99]
    say(f'   with a Polymarket leg: {len(pm)}; a recorded quote at that moment for {len(have)}: the price the tracker used was still there in {len(same)} ({100 * len(same) / max(len(have), 1):.0f}%), worse by then in {len(worse)}')
    sil = [o['pm_silent'] for o in have if o.get('pm_silent') is not None]
    if sil:
        say(f'   Polymarket had last sent something for the game: median {st.median(sil):.0f} s before, 9 in 10 within {np.percentile(sil, 90):.0f} s, over 5 min before in {100 * sum(1 for x in sil if x > 300) / len(sil):.0f}%')
    sz = [o['pm_size'] * (1 / o['pm_odds_now']) for o in have if o.get('pm_size')]
    if sz:
        say(f'   dollars resting at that Polymarket price: median ${st.median(sz):.0f}, 1 in 4 under ${np.percentile(sz, 25):.0f}, 1 in 10 under ${np.percentile(sz, 10):.0f}')
    ts = [o['to_start'] for o in kept if o.get('to_start') is not None]
    if ts:
        say(f'   hours from first sight to the start of the game: median {st.median(ts):.1f}, 1 in 4 over {np.percentile(ts, 75):.1f} (the stake is out until the game ENDS)')
    by_sport = collections.Counter(o['sp'] for o in kept)
    say('   by sport: ' + ', '.join(f'{k} {v / days:.1f} a day' for k, v in by_sport.most_common(8)))
    two = sum(1 for o in kept if len(o['legs']) == 2)
    say(f'   two legs {two}, three legs {len(kept) - two}; opened and closed more than once (flicker): {sum(1 for o in kept if o["eps"] > 1)}')

    say('\n3. THE BEST SET OF N ACCOUNTS (Polymarket always in; value = locked profit when first seen, $100 an opportunity; cuts of section 2 down to 1%+ applied)')
    cand = {l['book'] for o in opps for l in o['legs']} - {'polymarket', 'kalshi', 'pinnacle'}
    val = lambda o: 100 * o['open'] if clean(o) else 0.0
    g = greedy(opps, days, val, cand)
    say(f'   {"N":>3} {"added":<13}{"opps a day":>11} {"$ a day":>9}   (the count is every opportunity the set covers; the $ only the ones passing the cuts)')
    for i, (b, n, v) in enumerate(g):
        say(f'   {i + 1:>3} {b:<13}{n:>11.1f} {v:>9.2f}')
    fam_count = collections.Counter(FAMILY.get(b, b) for b, _, _ in g[:20])
    say(f'   price feeds behind the first {min(20, len(g))}: {len(fam_count)} ({", ".join(f"{k} x{v}" if v > 1 else k for k, v in fam_count.most_common())})')

    say('\n4. PER BOOKMAKER, how often you would be betting there (opportunities passing the cuts, a day) and on which side')
    use = collections.Counter()
    odd_use = collections.Counter()
    for o in opps:
        if clean(o):
            for l in o['legs']:
                use[l['book']] += 1
            if o.get('odd'):
                odd_use[o['odd']] += 1
    for b, n in use.most_common(22):
        say(f'   {b:<13}{n / days:>6.1f} bets a day, the odd (value) leg in {odd_use[b] / days:>5.1f} of them')

    say('\n5. DOLLARS A DAY, best 15 and best 20 accounts (cuts applied), by what you assume')
    for N in (15, 20):
        if len(g) < N:
            N = len(g)
        S = {'polymarket'} | {b for b, _, _ in g[:N]}
        got = [o for o in opps if clean(o) and {l['book'] for l in o['legs']} <= S]
        base = sum(o['open'] for o in got) / days
        say(f'   best {N}: {len(got) / days:.1f} opportunities a day, mean locked {100 * np.mean([o["open"] for o in got]):.2f}%')
        for stake in (50, 100, 250):
            row = []
            for real in (1.0, 0.5, 0.25):
                for fail, loss in ((0.0, 0.0), (0.15, 0.03)):
                    # `fail` of the taken ones lose `loss` of the stake (second leg refused, first one closed at a cost)
                    row.append(stake * real * (base * (1 - fail) - len(got) / days * fail * loss))
            say(f'      ${stake:>3} an opportunity: all real ${row[0]:>7.2f} (with 15% failing at -3%: ${row[1]:>7.2f}) | half real ${row[2]:>7.2f} (${row[3]:>7.2f}) | a quarter real ${row[4]:>7.2f} (${row[5]:>7.2f})')
        tied = collections.Counter()
        say(f'      money working: about ${100 * len(got) / days:.0f} staked a day per $100 an opportunity, each stake out until its game ends')
    if a.out:
        open(os.path.join(os.path.expanduser(a.out), 'arb_books.txt'), 'w').write('\n'.join(lines) + '\n')


if __name__ == '__main__':
    main()
