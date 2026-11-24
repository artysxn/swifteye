"""The steered resting orders taken apart: what it trades, why it makes money, how many trades, how often it wins,
and whether the test is right (24 Nov, PC; owner: "and how many trades? how often does it win money vs lose? is the
test correct?" and "you also didn't explain WHY it makes money ... on what? what games? every game?").

  python tools/signals/makeraudit.py --signals DIR [--wallet 175] [--results fair_results.json]

Needs makersteer.py build (fills + per-second states) and makerwallet.py (the fill model's calls).
The wallet replay is the one of makerwallet.py (an order exists only if the wallet can pay for it; widest spreads first;
the exit order always rests while we hold shares; what is left is paid at the result), written again here with more
switches, so the two can be held against each other. A TRADE = shares bought (or sold) by one fill and closed later,
first in first out, by an exit fill or by the result.
1 WHAT IT TRADES   which games, how many of them, when in the game, at what prices and spreads.
2 WHY              per share: what the spread pays at the fill, what the price then does against us, what is kept; for
                   the fills the rule allows and the ones it forbids.
3 TRADES           how many, how many win, the average win and loss, how long they are held, by day, by sport.
4 IS IT CORRECT    the books add up (two ways); the results are the official ones; then everything that could make it
                   look better than it is: worse prices on every fill, 2 s reaction, losing a random half of the fills,
                   losing the BEST fills to faster bots, bookmaker signals 6 s stale, no partial fills, the rule
                   turned upside down (must lose), a random rule (must equal no rule)."""
import argparse
import collections
import datetime as dt
import heapq
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'waves'))
import makersteer as MS  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--signals', required=True)
    ap.add_argument('--wallet', type=float, default=175.0)
    ap.add_argument('--results', default='')
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    Z = np.load(os.path.join(a.signals, 'makersteer.npz'))
    Fm, FX, sports = Z['F'], Z['FX'], Z['sports'].tolist()
    ci, gm, tms, sg, q, w, m60, m300, won, spi, inplay, spr, cls, raw = (Fm[:, j] for j in range(14))
    ci = ci.astype(int)
    k300 = sg * (m300 - q) * 100
    fc = {k: i for i, k in enumerate(MS.FN)}
    live = inplay > 0.5
    rowday = np.array([dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in tms])
    C_ = np.load(os.path.join(a.signals, 'makerwallet_calls.npz'))
    P0, sc_days = C_['PV'][:, 0], C_['days'].tolist()
    S = np.load(os.path.join(a.signals, 'makersteer_states.npz'))
    sgame, sec0, sn, soff, swon, ssport = S['game'], S['sec0'], S['n'], S['off'], S['won'].astype(float), S['sport']
    sflags, sspread, sbid, sask = S['flags'], S['spread'], S['bid'], S['ask']
    gidx = {int(g): i for i, g in enumerate(sgame)}
    send = sec0 + sn + 60
    NG = len(sgame)
    rng = np.random.default_rng(0)
    W0 = a.wallet
    cands = {L: (lambda c: c[np.argsort(tms[c], kind='stable')])(np.flatnonzero((ci == MS.CONFIGS.index((0, 2, L))) & live & np.isin(rowday, sc_days))) for L in (1, 2)}

    def rule_ok(fl, rule):
        up, dn, lu, ld = (fl >> 1) & 1, (fl >> 2) & 1, (fl >> 3) & 1, (fl >> 4) & 1
        if rule == 'none':
            return np.ones_like(up, bool), np.ones_like(up, bool)
        if rule == 'fam30':
            return dn == 0, up == 0
        if rule == 'against':                                          # upside down: only where the bookmakers moved AGAINST the side
            return dn == 1, up == 1
        return up == 1, dn == 1
    EV = {}

    def events(minsp, rule, L=1, flag_lag=0):
        key = (minsp, rule, L, flag_lag)
        if key in EV:
            return EV[key]
        ev = []
        for f in cands[L]:
            g_own = gidx.get(int(gm[f]), -1)
            if g_own < 0:
                continue
            if spr[f] < minsp - 0.5:
                ev.append((f, None, None, None, None, -1, g_own))
                continue
            u = int(tms[f] // 1000)
            idx = (u - L) - sec0
            ok = (idx >= 0) & (idx < sn)
            gi = np.flatnonzero(ok)
            pos_ = soff[gi] + idx[gi]
            fl, sp_ = sflags[pos_], sspread[pos_]
            if flag_lag:
                fl = sflags[np.maximum(pos_ - flag_lag, soff[gi])]
            el = ((sflags[pos_] & 1) > 0) & (sp_ >= minsp) & (sp_ != 255)
            gi, pos_, fl, sp_ = gi[el], pos_[el], fl[el], sp_[el]
            okb, oka = rule_ok(fl, rule)
            g_ = np.concatenate([gi[okb], gi[oka]])
            sd = np.concatenate([np.ones(okb.sum(), np.int8), -np.ones(oka.sum(), np.int8)])
            pc = np.concatenate([sbid[pos_[okb]].astype(np.int16) + 1, sask[pos_[oka]].astype(np.int16) - 1])
            sc_ = np.concatenate([sp_[okb], sp_[oka]])
            hit = np.flatnonzero((g_ == g_own) & (sd == int(sg[f])))
            ev.append((f, g_, sd, pc, sc_, int(hit[0]), g_own) if len(hit) else (f, None, None, None, None, -1, g_own))
        EV[key] = ev
        return ev

    def run(minsp=5, rule='fam30', size=10.0, capm=2, model=False, exit_any=True, w0=W0, L=1, flag_lag=0, haircut=0.0, keep_entry=None, whole_only=False, exit_minsp=2, rand_rule=None, days=None):
        ev = events(minsp, rule, L, flag_lag)
        dset = set(days or sc_days)
        cash, pos, heap = w0, np.zeros(NG), []
        lots = collections.defaultdict(collections.deque)
        closed = []                                                   # (pnl $, shares, seconds held, 0 exit fill / 1 result, game idx, close second, entry price, side)
        n_entry = n_exit = miss_cash = miss_cap = 0
        low = w0

        def settle(g0):
            nonlocal cash
            if pos[g0] != 0:
                cash += pos[g0] * swon[g0] if pos[g0] > 0 else -pos[g0] * (1 - swon[g0])
            for sh_, px_, t_, s_ in lots[g0]:
                closed.append(((swon[g0] - px_) * sh_ if s_ > 0 else (px_ - swon[g0]) * sh_, sh_, int(send[g0]) - t_, 1, g0, int(send[g0]), px_, s_))
            lots[g0].clear()
            pos[g0] = 0.0

        def apply(g0, s_, sh, px, t):
            nonlocal cash
            rem = sh
            dq = lots[g0]
            while rem > 1e-9 and dq and dq[0][3] == -s_:
                lot = dq[0]
                c = min(rem, lot[0])
                closed.append(((px - lot[1]) * c if lot[3] > 0 else (lot[1] - px) * c, c, t - lot[2], 0, g0, t, lot[1], lot[3]))
                cash += c * px if s_ < 0 else c * (1 - px)             # sell held shares / buy back and merge with the other token
                lot[0] -= c
                rem -= c
                if lot[0] <= 1e-9:
                    dq.popleft()
            if rem > 1e-9:
                cash -= rem * px if s_ > 0 else rem * (1 - px)
                if not dq and pos[g0] == 0:
                    heapq.heappush(heap, (int(send[g0]), g0))
                dq.append([rem, px, t, s_])
            pos[g0] += s_ * sh
        for f, g_, sd, pc, sc_, me, g_own in ev:
            if rowday[f] not in dset:
                continue
            u = int(tms[f] // 1000)
            while heap and heap[0][0] <= u:
                settle(heapq.heappop(heap)[1])
            low = min(low, cash + sum(l_[0] * (l_[1] if l_[3] > 0 else 1 - l_[1]) for g0 in list(lots) for l_ in lots[g0]))
            s_ = int(sg[f])
            px = float(q[f]) + haircut * s_
            if exit_any and pos[g_own] != 0 and np.sign(pos[g_own]) == -s_:
                if spr[f] >= exit_minsp - 0.5:
                    apply(g_own, s_, min(abs(pos[g_own]), float(raw[f])), px, u)
                    n_exit += 1
                continue
            if g_ is None or (keep_entry is not None and not keep_entry[f]) or (model and not (P0[f] > 0)) or (whole_only and raw[f] < size):
                continue
            if rand_rule is not None and not rand_rule[f]:
                continue
            pg, price = pos[g_], pc / 100.0
            within = np.abs(pg + sd * size) <= size * capm + 1e-9
            if not within[me]:
                miss_cap += 1
                continue
            need = np.where(sd > 0, size * price, np.maximum(size - np.maximum(pg, 0), 0) * (1 - price))
            order = np.lexsort((need, -sc_.astype(np.int32)))
            order = order[within[order]]
            if np.cumsum(need[order])[int(np.flatnonzero(order == me)[0])] > cash + 1e-9:
                miss_cash += 1
                continue
            apply(g_own, s_, min(size, float(raw[f])), px, u)
            n_entry += 1
        while heap:
            settle(heapq.heappop(heap)[1])
        return dict(final=cash, entries=n_entry, exits=n_exit, miss_cash=miss_cash, miss_cap=miss_cap, low=low, closed=np.array(closed, float).reshape(-1, 8))
    V = lambda k1, k2=None: FX[:, fc[k1]] - (FX[:, fc[k2]] if k2 else 0)
    fam30 = V('fam_up30', 'fam_dn30')
    b5 = (ci == MS.CONFIGS.index((0, 5, 1))) & live & np.isin(rowday, sc_days) & np.isfinite(FX[:, 0])
    nd = len(sc_days)
    A_ = run(model=False)
    B_ = run(model=True)
    say(f'THE STEERED RESTING ORDERS TAKEN APART. {NG} games with real trades, in play, {", ".join(x[8:] for x in sc_days)} Nov ({nd} days). Setting A = spreads of 5c+, never against the bookmakers\' last 30 s, 10 shares an order, '
        f'at most 2 orders a game, the exit order always resting, ${W0:.0f} wallet. Setting B = A + the fill model must expect a gain.')
    # ---------------- 1: what it trades
    say('')
    say('1. WHAT IT TRADES: the match-winner market of a game on Polymarket, only while the game is being played, only in seconds when the gap between the best buy and sell price is 5c or more.')
    base = (sflags & 1) > 0
    wide = base & (sspread >= 5) & (sspread != 255)
    gw = np.add.reduceat(wide.astype(np.int64), soff)
    gb = np.add.reduceat(base.astype(np.int64), soff)
    say(f'   in-play time with a live two-sided book at 10-90c: {gb.sum() / 3600:,.0f} h over the {NG} games; of it the spread is 5c+ for {gw.sum() / max(gb.sum(), 1) * 100:.0f}% ({gw.sum() / 3600:,.0f} h). '
        f'Games with at least 1 min of it: {np.mean(gw >= 60) * 100:.0f}%; with 10 min+: {np.mean(gw >= 600) * 100:.0f}%; median game {np.median(gw) / 60:.0f} min.')
    for nm, R_ in (('A', A_), ('B', B_)):
        cl = R_['closed']
        ent_g = collections.Counter(cl[:, 4].astype(int).tolist())
        say(f'   setting {nm}: {R_["entries"]:,} entry fills ({R_["entries"] / nd:.0f} a day) in {len(ent_g)} of the {NG} games ({len(ent_g) / NG * 100:.0f}%); games traded a day {len(ent_g) / nd:.0f}; '
            f'trades in a traded game: median {np.median(list(ent_g.values())):.0f}, a tenth of the games have {np.quantile(list(ent_g.values()), 0.9):.0f}+')
    eA = b5 & (fam30 >= 0)
    say('   by sport (setting A\'s entry fills; share of that sport\'s games traded; wallet result of the trades closed in it):')
    cl = A_['closed']
    for j, sp in enumerate(sports):
        gs = np.flatnonzero(ssport == j)
        if len(gs) < 5:
            continue
        mine = np.isin(cl[:, 4].astype(int), gs)
        tg_ = len(set(cl[mine, 4].astype(int).tolist()))
        say(f'      {sp:<11} {len(gs):>4} games, 5c+ spread {gw[gs].sum() / max(gb[gs].sum(), 1) * 100:3.0f}% of the time, traded {tg_ / len(gs) * 100:3.0f}% of them, {mine.sum():>5,} trades, ${cl[mine, 0].sum():>+7.2f}, won {np.mean(cl[mine, 0] > 0) * 100 if mine.any() else 0:3.0f}%')
    lm, pr_ = FX[:, fc['q_live_min']], FX[:, fc['q_price']]
    say('   when in the game (minutes since the start): ' + '; '.join(f'{lo}-{hi} min {np.mean((lm[eA] >= lo) & (lm[eA] < hi)) * 100:.0f}%' for lo, hi in ((0, 15), (15, 45), (45, 90), (90, 150), (150, 9999))))
    say('   the price of what we get: ' + '; '.join(f'{int(lo * 100)}-{int(hi * 100)}c {np.mean((pr_[eA] >= lo) & (pr_[eA] < hi)) * 100:.0f}%' for lo, hi in ((0.1, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.91))) +
        f'; the spread then: median {np.median(spr[eA]):.0f}c, a quarter {np.quantile(spr[eA], 0.75):.0f}c+; the real trade that fills us: median {np.median(raw[eA]):.0f} shares')
    # ---------------- 2: why
    say('')
    say('2. WHY IT MAKES MONEY, cents a share (10-share orders; spreads of 5c+, 1 s reaction). "The spread pays" = the middle of the book minus our price at the fill. "Then the price moves" = the middle 5 min later minus the middle at the fill, from our side.')
    w10 = np.minimum(10, raw)
    edge = spr / 2 - 1.0
    for nm, mk in (('every fill (both sides always in the book)', b5), ('fills the bookmaker rule ALLOWS', b5 & (fam30 >= 0)), ('fills it FORBIDS (bookmakers moved against that side)', b5 & (fam30 < 0)),
                   ('allowed AND the model expects a gain', b5 & (fam30 >= 0) & (P0 > 0)), ('allowed but the model says no', b5 & (fam30 >= 0) & ~(P0 > 0))):
        ok = mk & np.isfinite(k300)
        ww = w10[ok]
        e_, k_ = np.average(edge[ok], weights=ww), np.average(k300[ok], weights=ww)
        big = np.average((k300[ok] - edge[ok]) <= -5, weights=ww) * 100
        say(f'   {nm:<54}{ok.sum():>6,} fills: the spread pays {e_:+.2f}c, then the price moves {k_ - e_:+.2f}c, kept {k_:+.2f}c | the price ran 5c+ against us after {big:.0f}% of them')
    # ---------------- 3: trades
    say('')
    say('3. HOW MANY TRADES, HOW OFTEN IT WINS (a trade = shares from one fill, closed first in first out by an exit fill or by the result)')
    for nm, R_ in (('A (bookmaker rule)', A_), ('B (rule + model)', B_)):
        cl = R_['closed']
        pn, sh, hold, how = cl[:, 0], cl[:, 1], cl[:, 2], cl[:, 3]
        wn, ls = pn > 1e-9, pn < -1e-9
        say(f'   setting {nm}: ${W0:.0f} -> ${R_["final"]:,.2f} in {nd} days. {len(cl):,} trades ({len(cl) / nd:.0f} a day), {sh.sum():,.0f} shares; won {wn.mean() * 100:.0f}%, lost {ls.mean() * 100:.0f}%, flat {(~wn & ~ls).mean() * 100:.0f}%; '
            f'average win ${pn[wn].mean():+.3f} ({np.average(pn[wn] / sh[wn]) * 100:+.1f}c a share), average loss ${pn[ls].mean():+.3f} ({np.average(pn[ls] / sh[ls]) * 100:+.1f}c a share); a trade makes ${pn.mean():+.4f} on average')
        ex, rs = how == 0, how == 1
        say(f'      closed by an exit fill: {ex.mean() * 100:.0f}% of trades, won {np.mean(pn[ex] > 1e-9) * 100:.0f}%, ${pn[ex].sum():+.2f} in all, held a median {np.median(hold[ex]) / 60:.1f} min | '
            f'left to the result: {rs.mean() * 100:.0f}%, won {np.mean(pn[rs] > 1e-9) * 100:.0f}%, ${pn[rs].sum():+.2f} in all, held a median {np.median(hold[rs]) / 60:.0f} min')
        day = np.array([dt.datetime.fromtimestamp(int(t), dt.timezone.utc).strftime('%d') for t in cl[:, 5]])
        run_ = W0
        parts = []
        for d_ in sorted(set(day)):
            m_ = day == d_
            run_ += pn[m_].sum()
            parts.append(f'{d_} Nov {m_.sum()} trades, won {np.mean(pn[m_] > 1e-9) * 100:.0f}%, ${pn[m_].sum():+.2f} -> ${run_:.2f}')
        say('      by the day a trade closed: ' + '; '.join(parts))
        gg = collections.defaultdict(float)
        for p_, g0 in zip(pn, cl[:, 4].astype(int)):
            gg[g0] += p_
        gv = np.sort(np.array(list(gg.values())))[::-1]
        say(f'      by game: {len(gv)} games, {np.mean(gv > 0) * 100:.0f}% up; best game ${gv[0]:+.2f}, worst ${gv[-1]:+.2f}; the 10 best games make ${gv[:10].sum():+.2f} of the ${gv.sum():+.2f}; without them ${gv[10:].sum():+.2f}; '
            f'median game ${np.median(gv):+.3f}; checks: trades sum to ${pn.sum():+.2f}, wallet change ${R_["final"] - W0:+.2f}')
    # ---------------- 4: is it correct
    say('')
    say('4. IS THE TEST CORRECT?')
    r = run(rule='none', exit_any=False, w0=1e9, capm=10 ** 6, size=20.0)
    direct = sum(min(20.0, raw[f]) * sg[f] * (swon[g_own] - q[f]) for f, g_, sd, pc, sc_, me, g_own in events(5, 'none') if g_ is not None)
    say(f'   a. the books add up: with endless money, no cap, no exit and no rule the wallet replay makes ${r["final"] - 1e9:+,.2f}; adding up every such fill by hand (shares x (result - price)) gives ${direct:+,.2f}. '
        f'In section 3 the trades add up to the wallet change to the cent.')
    if a.results and os.path.exists(a.results):
        R = json.load(open(a.results))
        offv = np.array([(R.get(str(int(m)), {}).get('home', -1) if (R.get(str(int(m)), {}).get('home', 0) or 0) + (R.get(str(int(m)), {}).get('away', 0) or 0) == 1 else -1) for m in sgame], float)
        say(f'   b. the results used (read from the last recorded quotes) against Polymarket\'s official results: {int((offv == swon).sum())} of {int((offv >= 0).sum())} games with a clean official result agree ({int((offv < 0).sum())} without one).')
    say(f'   c. what could make it look better than it is, on both settings (wallet after {nd} days, from ${W0:.0f}; A / B):')

    def both(name, **kw):
        ra, rb = run(model=False, **kw), run(model=True, **kw)
        say(f'      {name:<78} ${ra["final"]:>7,.2f} ({ra["entries"]:,} entries) / ${rb["final"]:>7,.2f} ({rb["entries"]:,})')
        return ra, rb
    both('as tested')
    both('every fill, in and out, half a cent worse', haircut=0.005)
    both('every fill 1c worse', haircut=0.01)
    both('every fill 2c worse', haircut=0.02)
    both('2 s reaction instead of 1 s', L=2)
    both('bookmaker signals 6 s stale (in case a trade was really earlier than placed)', flag_lag=6)
    both('no part fills: only real trades at least as big as our order', whole_only=True)
    both('the exit order only in spreads of 3c+', exit_minsp=3)
    both('held to the result instead (no exit order)', exit_any=False)
    both('2 s reaction AND every fill half a cent worse', L=2, haircut=0.005)
    both('2 s reaction AND every fill 1c worse', L=2, haircut=0.01)
    both('2 s, half a cent worse, signals 6 s stale, no part fills (all at once)', L=2, haircut=0.005, flag_lag=6, whole_only=True)
    for frac in (0.5, 0.25):
        fa, fb = [], []
        for sd_ in range(12):
            km = np.random.default_rng(100 + sd_).random(len(Fm)) < frac
            fa.append(run(model=False, keep_entry=km)['final'])
            fb.append(run(model=True, keep_entry=km)['final'])
        say(f'      {"we only get a random " + str(int(frac * 100)) + "% of the entry fills (12 draws: median, lowest .. highest)":<78} ${np.median(fa):>7,.2f} (${min(fa):,.0f}..${max(fa):,.0f}) / ${np.median(fb):>7,.2f} (${min(fb):,.0f}..${max(fb):,.0f})')
    b2 = (ci == MS.CONFIGS.index((0, 2, 1))) & live & np.isin(rowday, sc_days) & np.isfinite(FX[:, 0]) & (spr >= 4.5)      # the rows the wallet replay itself uses
    for name, base_mask in (('A', b2 & (fam30 >= 0)), ('B', b2 & (fam30 >= 0) & (P0 > 0))):
        res = []
        for frac in (0.1, 0.2, 0.3):
            v = np.where(base_mask & np.isfinite(k300), k300, -np.inf)
            cut = np.quantile(k300[base_mask & np.isfinite(k300)], 1 - frac)
            km = ~(base_mask & (v >= cut))
            res.append(run(model=(name == 'B'), keep_entry=km)['final'])
        say(f'      setting {name}: faster bots take our BEST entry fills (by what they made 5 min later, hindsight): best 10% gone ${res[0]:,.2f}, 20% gone ${res[1]:,.2f}, 30% gone ${res[2]:,.2f}')
    ra, rb = both('PLACEBO, the rule upside down (only where the bookmakers moved AGAINST the side): must lose', rule='against')
    rn = run(rule='none', model=False)
    share = (b5 & (fam30 >= 0)).sum() / max(b5.sum(), 1)
    rr = [run(rule='none', model=False, rand_rule=np.random.default_rng(200 + s_).random(len(Fm)) < share)['final'] for s_ in range(12)]
    say(f'      PLACEBO, a random rule that allows the same share of fills ({share * 100:.0f}%): median ${np.median(rr):,.2f} (${min(rr):,.0f}..${max(rr):,.0f}); no rule at all ${rn["final"]:,.2f}; the real rule ${A_["final"]:,.2f}')
    say('      first half / second half of the days, each from a fresh wallet (A, B): ' + ' | '.join(
        f'{d_[0][8:]}-{d_[-1][8:]} Nov ${run(model=False, days=d_)["final"]:,.2f}, ${run(model=True, days=d_)["final"]:,.2f}' for d_ in ([x for x in sc_days if x <= '2026-11-20'], [x for x in sc_days if x >= '2026-11-21'])))
    cl = B_['closed']
    per_sh = cl[:, 0].sum() / max(cl[:, 1].sum(), 1) * 100
    cla = A_['closed']
    say(f'      how thin it is: setting A makes {cla[:, 0].sum() / max(cla[:, 1].sum(), 1) * 100:+.2f}c per share traded, setting B {per_sh:+.2f}c; a fill half a cent worse on the way in and on the way out takes 1c off that.')
    say('   d. what the test CANNOT show: whether other bots step in front of an order placed inside the spread (it assumes we are first at our price); whether our own order changes who trades; anything outside this one week; football (not in the sample).')
    with open(os.path.join(a.signals, 'makeraudit.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
