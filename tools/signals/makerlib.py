"""The wallet replay of the steered resting orders, as a class (24 Nov, PC), for makerbetter.py.

Same rules as makerwallet.py / makeraudit.py (see their headers): every simulated fill of the "one cent inside, 1 s (or
2 s) late" setting is replayed in time order across all games; an ENTRY needs its order to have been resting, i.e. the
game qualified one (two) seconds earlier (book alive, 10-90c, spread wide enough, the rule allows the side) and the
wallet could pay for it (widest spreads first); while shares are held the order that reduces the position always rests
(no money, no rule); a buy and a sale in a game net out first in first out; what is left is paid at the result.
New switches: a size per fill (confidence scaling), a mask of fills allowed as entries (model line, sports, ...), a
TIME STOP (a lot still open T minutes after it was bought is sold into the bid 1 s later as a taker, 5% p(1-p) fee),
worse prices, slower reaction, stale signals."""
import collections
import datetime as dt
import heapq
import os

import numpy as np

import makersteer as MS


class Sim:
    def __init__(self, sig, name='makersteer_flow.npz', days=None):
        Z = np.load(os.path.join(sig, name))
        self.Fm, self.FX, self.sports = Z['F'], Z['FX'], Z['sports'].tolist()
        self.ngames = int(Z['games'][0])
        F = self.Fm
        self.ci, self.gm, self.tms, self.sg, self.q, self.raw, self.spr = F[:, 0].astype(int), F[:, 1], F[:, 2], F[:, 3], F[:, 4], F[:, 13], F[:, 11]
        self.m300, self.won, self.spi, self.live = F[:, 7], F[:, 8], F[:, 9].astype(int), F[:, 10] > 0.5
        self.k300 = self.sg * (self.m300 - self.q) * 100
        self.kres = np.where(self.won >= 0, self.sg * (self.won - self.q) * 100, np.nan)
        self.fc = {k: i for i, k in enumerate(MS.FN)}
        self.has = np.isfinite(self.FX[:, 0])
        self.rowday = np.array([dt.datetime.fromtimestamp(v / 1000, dt.timezone.utc).strftime('%Y-%m-%d') for v in self.tms])
        self.days_all = sorted(set(self.rowday.tolist()))
        S = np.load(os.path.join(sig, name.replace('.npz', '_states.npz')))
        self.sgame, self.sec0, self.sn, self.soff, self.swon, self.ssport = S['game'], S['sec0'], S['n'], S['off'], S['won'].astype(float), S['sport']
        self.sflags, self.sspread, self.sbid, self.sask = S['flags'], S['spread'], S['bid'], S['ask']
        self.gidx = {int(g): i for i, g in enumerate(self.sgame)}
        self.send = self.sec0 + self.sn + 60
        self.NG = len(self.sgame)
        self.grow = np.array([self.gidx.get(int(g), -1) for g in self.gm])
        self.EV = {}
        self.set_days(days or self.days_all)

    def set_days(self, days):
        self.days = list(days)
        ok = self.live & np.isin(self.rowday, self.days)
        self.cands = {L: (lambda c: c[np.argsort(self.tms[c], kind='stable')])(np.flatnonzero((self.ci == MS.CONFIGS.index((0, 2, L))) & ok)) for L in (1, 2)}
        self.EV = {}

    def cfg(self, calm, k, L):
        return self.ci == MS.CONFIGS.index((calm, k, L))

    def col(self, k1, k2=None):
        return self.FX[:, self.fc[k1]] - (self.FX[:, self.fc[k2]] if k2 else 0)

    @staticmethod
    def rule_ok(fl, rule):
        up, dn = (fl >> 1) & 1, (fl >> 2) & 1
        if rule == 'none':
            return np.ones_like(up, bool), np.ones_like(up, bool)
        if rule == 'fam30':
            return dn == 0, up == 0
        if rule == 'against':
            return dn == 1, up == 1
        return up == 1, dn == 1

    def events(self, minsp, rule, L=1, flag_lag=0):
        key = (minsp, rule, L, flag_lag)
        if key in self.EV:
            return self.EV[key]
        ev = []
        for f in self.cands[L]:
            g_own = self.grow[f]
            if g_own < 0:
                continue
            if self.spr[f] < minsp - 0.5:
                ev.append((f, None, None, None, None, -1, g_own))
                continue
            u = int(self.tms[f] // 1000)
            idx = (u - L) - self.sec0
            gi = np.flatnonzero((idx >= 0) & (idx < self.sn))
            pos_ = self.soff[gi] + idx[gi]
            fl, sp_ = self.sflags[pos_], self.sspread[pos_]
            if flag_lag:
                fl = self.sflags[np.maximum(pos_ - flag_lag, self.soff[gi])]
            el = ((self.sflags[pos_] & 1) > 0) & (sp_ >= minsp) & (sp_ != 255)
            gi, pos_, fl, sp_ = gi[el], pos_[el], fl[el], sp_[el]
            okb, oka = self.rule_ok(fl, rule)
            g_ = np.concatenate([gi[okb], gi[oka]])
            sd = np.concatenate([np.ones(okb.sum(), np.int8), -np.ones(oka.sum(), np.int8)])
            pc = np.concatenate([self.sbid[pos_[okb]].astype(np.int16) + 1, self.sask[pos_[oka]].astype(np.int16) - 1])
            sc_ = np.concatenate([sp_[okb], sp_[oka]])
            hit = np.flatnonzero((g_ == g_own) & (sd == int(self.sg[f])))
            ev.append((f, g_, sd, pc, sc_, int(hit[0]), g_own) if len(hit) else (f, None, None, None, None, -1, g_own))
        self.EV[key] = ev
        return ev

    def taker_px(self, g0, t, side):
        """Price a position is closed at by a taker order sent at second t (it lands 1 s later): the bid for shares held (side +1), the ask to buy back (side -1)."""
        i0 = t + 1 - self.sec0[g0]
        for i in range(max(int(i0), 0), min(int(i0) + 60, int(self.sn[g0]))):
            p_ = self.soff[g0] + i
            if (self.sflags[p_] & 1) and self.sspread[p_] != 255:
                px = (self.sbid[p_] if side > 0 else self.sask[p_]) / 100.0
                if 0.0 < px < 1.0:
                    return px
        return None

    def run(self, minsp=5, rule='fam30', size=10.0, size_arr=None, capm=2, entry_mask=None, exit_any=True, w0=175.0, L=1, flag_lag=0, haircut=0.0, whole_only=False, exit_minsp=2, stop_min=None, days=None, exit_mask=None):
        ev = self.events(minsp, rule, L, flag_lag)
        dset = set(days or self.days)
        sg, q, raw, tms, spr, send, swon = self.sg, self.q, self.raw, self.tms, self.spr, self.send, self.swon
        cash, pos, heap, stops = w0, np.zeros(self.NG), [], []
        lots = collections.defaultdict(list)
        closed = []                       # pnl $, shares, seconds held, how (0 exit fill, 1 result, 2 time stop), game idx, close second, entry price, side
        n_entry = n_exit = n_stop = miss_cash = miss_cap = seq = 0
        inv = 0.0
        low = w0

        def settle(g0):
            nonlocal cash, inv
            for lot in lots[g0]:
                sh_, px_, t_, s_ = lot
                if sh_ <= 1e-9:
                    continue
                cash += sh_ * swon[g0] if s_ > 0 else sh_ * (1 - swon[g0])
                inv -= sh_ * (px_ if s_ > 0 else 1 - px_)
                closed.append(((swon[g0] - px_) * sh_ if s_ > 0 else (px_ - swon[g0]) * sh_, sh_, int(send[g0]) - t_, 1, g0, int(send[g0]), px_, s_))
                lot[0] = 0.0
            lots[g0] = []
            pos[g0] = 0.0

        def apply(g0, s_, sh, px, t):
            nonlocal cash, inv, seq
            rem = sh
            dq = lots[g0]
            while rem > 1e-9 and dq and dq[0][3] == -s_:
                lot = dq[0]
                c = min(rem, lot[0])
                closed.append(((px - lot[1]) * c if lot[3] > 0 else (lot[1] - px) * c, c, t - lot[2], 0, g0, t, lot[1], lot[3]))
                cash += c * px if s_ < 0 else c * (1 - px)
                inv -= c * (lot[1] if lot[3] > 0 else 1 - lot[1])
                lot[0] -= c
                rem -= c
                if lot[0] <= 1e-9:
                    dq.pop(0)
            if rem > 1e-9:
                cash -= rem * px if s_ > 0 else rem * (1 - px)
                inv += rem * (px if s_ > 0 else 1 - px)
                if not dq and pos[g0] == 0:
                    heapq.heappush(heap, (int(send[g0]), g0))
                lot = [rem, px, t, s_]
                dq.append(lot)
                if stop_min:
                    seq += 1
                    heapq.heappush(stops, (t + int(stop_min * 60), seq, g0, lot))
            pos[g0] += s_ * sh
        for f, g_, sd, pc, sc_, me, g_own in ev:
            if self.rowday[f] not in dset:
                continue
            u = int(tms[f] // 1000)
            while stops and stops[0][0] <= u:
                ts_, _, g0, lot = heapq.heappop(stops)
                if lot[0] <= 1e-9 or ts_ >= send[g0] - 60:
                    continue
                px = self.taker_px(g0, ts_, lot[3])
                if px is None:
                    continue
                c, fee = lot[0], 0.05 * px * (1 - px)
                cash += c * (px - fee) if lot[3] > 0 else c * ((1 - px) - fee)
                inv -= c * (lot[1] if lot[3] > 0 else 1 - lot[1])
                closed.append((((px - lot[1]) if lot[3] > 0 else (lot[1] - px)) * c - fee * c, c, ts_ - lot[2], 2, g0, ts_, lot[1], lot[3]))
                pos[g0] -= lot[3] * c
                lot[0] = 0.0
                lots[g0] = [l_ for l_ in lots[g0] if l_[0] > 1e-9]
                n_stop += 1
            while heap and heap[0][0] <= u:
                settle(heapq.heappop(heap)[1])
            low = min(low, cash + inv)
            s_ = int(sg[f])
            px = float(q[f]) + haircut * s_
            if exit_any and pos[g_own] != 0 and np.sign(pos[g_own]) == -s_:
                if spr[f] >= exit_minsp - 0.5 and (exit_mask is None or exit_mask[f]):
                    apply(g_own, s_, min(abs(pos[g_own]), float(raw[f])), px, u)
                    n_exit += 1
                continue
            sz = float(size) if size_arr is None else float(size_arr[f])
            if g_ is None or (entry_mask is not None and not entry_mask[f]) or (whole_only and raw[f] < sz) or sz < 5:
                continue
            pg, price = pos[g_], pc / 100.0
            within = np.abs(pg + sd * sz) <= max(size * capm, sz) + 1e-9
            if not within[me]:
                miss_cap += 1
                continue
            need = np.where(sd > 0, sz * price, np.maximum(sz - np.maximum(pg, 0), 0) * (1 - price))
            order = np.lexsort((need, -sc_.astype(np.int32)))
            order = order[within[order]]
            if np.cumsum(need[order])[int(np.flatnonzero(order == me)[0])] > cash + 1e-9:
                miss_cash += 1
                continue
            apply(g_own, s_, min(sz, float(raw[f])), px, u)
            n_entry += 1
        while heap:
            settle(heapq.heappop(heap)[1])
        cl = np.array(closed, float).reshape(-1, 8)
        pg = collections.defaultdict(float)
        for p_, g0 in zip(cl[:, 0], cl[:, 4].astype(int)):
            pg[g0] += p_
        return dict(final=cash, entries=n_entry, exits=n_exit, stops=n_stop, miss_cash=miss_cash, miss_cap=miss_cap, low=low, closed=cl, per_game=pg)


def paired(ra, rb, rng, n=1000):
    """$ of run b minus run a, and its 90% range resampling games."""
    gs = sorted(set(ra['per_game']) | set(rb['per_game']))
    d = np.array([rb['per_game'].get(g, 0.0) - ra['per_game'].get(g, 0.0) for g in gs])
    bs = [d[rng.integers(0, len(d), len(d))].sum() for _ in range(n)]
    return d.sum(), np.percentile(bs, 5), np.percentile(bs, 95)
