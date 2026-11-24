"""Does a live SCORE reach us before Polymarket's price has moved? (24 Nov night.)

Bookmaker ODDS reach us 1.7-3.5 s late and Polymarket usually moved first. The score itself is a different feed: the
1xBet family (linebet, melbet, paripesa and their mirrors) publishes round-by-round / game-by-game / goal scores, and
esports streams run on a delay. For every score change (the earliest arrival over the family's mirrors):
  how far Polymarket's mid has moved toward the side that scored at -60 .. +180 s around the moment WE received it
  (cents, signed), the share of the 3-minute move already made by then, and what buying 1 s later at the ask makes:
  sold into the bid after 1 / 3 min (both fees), or valued at the mid after 3 min (one fee: the hold-to-result yardstick).
Events: CS2 / Valorant rounds, tennis games, football goals, basketball is left out (a basket every few seconds).
Recorded since 22 Nov 06:40, so about 31 hours.

usage: python tools/signals/scorelead.py --db ~/swifteye/oddsfeed.db --signals ~/swifteye/research/lab
"""
import argparse
import os
import re
import sqlite3
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import paths as PT  # noqa: E402

FAMILY = ('linebet', 'melbet', 'paripesa', 'betwinner', '22bit', '22bet', '1xbit', 'megapari')
PAIR = re.compile(r'(\d+)\s*[-:]\s*(\d+)')
norm = lambda s: set(re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).split())
TAUS = (-60, -30, -10, -5, 0, 1, 2, 5, 10, 20, 30, 60, 120, 180)
fee = lambda p: 0.05 * p * (1 - p)


def state(sport, sc, det):
    """(top a, top b, sub a, sub b) or None."""
    t = PAIR.search(sc or '')
    if not t:
        return None
    sub = PAIR.findall((det or '').split('|')[0])
    s = (int(sub[-1][0]), int(sub[-1][1])) if sub else (0, 0)
    return int(t.group(1)), int(t.group(2)), s[0], s[1]


def events(c, m, sport, home, away):
    """[(ts, +1 home scored / -1 away, kind)] from the earliest arrival of each new state over the family's mirrors."""
    seen = {}                                            # state -> earliest ts
    for book, ev, bh, ba in c.execute(f"SELECT book, book_event_id, home, away FROM book_events WHERE match_id = ? AND book IN ({','.join('?' * len(FAMILY))})", (m, *FAMILY)).fetchall():
        swap = len(norm(bh) & norm(away)) + len(norm(ba) & norm(home)) > len(norm(bh) & norm(home)) + len(norm(ba) & norm(away))
        for ts, sc, det in c.execute('SELECT ts_ms, score, detail FROM book_scores WHERE book = ? AND book_event_id = ? ORDER BY ts_ms', (book, ev)):
            st = state(sport, sc, det)
            if st is None:
                continue
            if swap:
                st = (st[1], st[0], st[3], st[2])
            if st not in seen or ts < seen[st]:
                seen[st] = ts
    order = sorted(seen.items(), key=lambda kv: kv[1])
    out, prev = [], None
    for st, ts in order:
        if prev is not None:
            da, db, dsa, dsb = st[0] - prev[0], st[1] - prev[1], st[2] - prev[2], st[3] - prev[3]
            if sport == 'football':
                if (da, db) in ((1, 0), (0, 1)):
                    out.append((ts, 1 if da else -1, 'goal'))
            elif (da, db) == (0, 0) and (dsa, dsb) in ((1, 0), (0, 1)):      # a round / a game inside the running map / set
                out.append((ts, 1 if dsa else -1, 'round' if sport != 'tennis' else 'game'))
        if prev is None or sum(st) >= sum(prev):          # ignore a mirror stepping back to an older state
            prev = st
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--signals', required=True)
    a = ap.parse_args()
    t0 = time.time()
    out = []
    say = lambda s='': (out.append(s), print(s, flush=True))
    M, A = PT.load(a.signals)
    c = sqlite3.connect('file:' + a.db + '?mode=ro', uri=True, timeout=30)
    info = {m: (h, aw) for m, h, aw in c.execute('SELECT id, home, away FROM matches')}
    R = []                                               # sport, match, dir, mid moves at TAUS (signed, cents), ask(+1), bid(+60), bid(+180), mid(+180), mid before
    for i in np.flatnonzero(M['k'] == 0):
        m, sport = int(M['match'][i]), str(M['sport'][i])
        if sport not in ('cs2', 'valorant', 'tennis', 'football', 'dota2', 'lol', 'hockey'):
            continue
        if M['t0'][i] + 1000 * M['n'][i] < 1795300000000:   # scores start on 22 Nov
            continue
        h, aw = info.get(m, ('', ''))
        ev = events(c, m, sport, h, aw)
        if not ev:
            continue
        o, n, tz = int(M['off'][i]), int(M['n'][i]), int(M['t0'][i])
        bid, ask = np.asarray(A['bid'][o:o + n], np.float64), np.asarray(A['ask'][o:o + n], np.float64)
        b2 = np.where(np.isfinite(bid), bid, np.where(np.isfinite(ask) & (ask <= 0.05), 0.0, np.nan))
        a2 = np.where(np.isfinite(ask), ask, np.where(np.isfinite(bid) & (bid >= 0.95), 1.0, np.nan))
        mid = (b2 + a2) / 2
        for ts, d, kind in ev:
            s = int((ts - tz) // 1000)
            if s < 70 or s + 185 >= n or ts < M['start'][i]:
                continue
            base = mid[s - 10]
            if not np.isfinite(base) or base < 0.08 or base > 0.92:
                continue
            mv = [d * (mid[s + t] - base) * 100 for t in TAUS]
            # the token bought: home if home scored, else the other side (1 - home)
            buy = (ask[s + 1] if d > 0 else 1 - bid[s + 1])
            sell60 = (bid[s + 60] if d > 0 else 1 - ask[s + 60])
            sell180 = (bid[s + 180] if d > 0 else 1 - ask[s + 180])
            m180 = (mid[s + 180] if d > 0 else 1 - mid[s + 180])
            R.append((sport, m, d, *mv, buy, sell60, sell180, m180, base, (ask - bid)[s - 10] * 100))
    if not R:
        say('no score events on games with Polymarket prices')
        return
    sp = np.array([r[0] for r in R])
    V = np.array([r[1:] for r in R], float)
    nT = len(TAUS)
    say(f'{len(R):,} score events on {len(np.unique(V[:, 0])):,} games ({time.time() - t0:.0f} s)')
    say('Polymarket\'s mid toward the side that scored, cents, from 10 s before we got the score (token 8-92c):')
    say(f'   {"":<22}{"events":>7}{"games":>6}  ' + ' '.join(f'{t:>+5}s' for t in TAUS) + '   done by +1 s')
    for s_ in ('cs2', 'valorant', 'dota2', 'tennis', 'football', 'hockey'):
        k = sp == s_
        if k.sum() < 30:
            continue
        mv = np.nanmean(V[k, 2:2 + nT], 0)
        say(f'   {s_:<22}{int(k.sum()):>7,}{len(np.unique(V[k, 0])):>6}  ' + ' '.join(f'{v:>+6.2f}' for v in mv) + f'   {100 * mv[TAUS.index(1)] / mv[-1] if abs(mv[-1]) > 0.05 else float("nan"):>5.0f}%')
    say()
    say('Buying the side that scored 1 s after the score arrives, cents a share [90% range by game]:')
    say(f'   {"":<22}{"events":>7}  sold into the bid at +1 min | at +3 min | valued at the mid at +3 min (one fee) | spread before')
    rng = np.random.default_rng(0)

    def rngm(v, g):
        ok = np.isfinite(v)
        v, g = v[ok], g[ok]
        ug, inv = np.unique(g, return_inverse=True)
        sums, cnts = np.bincount(inv, v, len(ug)), np.bincount(inv, minlength=len(ug))
        bs = [(lambda q: sums[q].sum() / max(cnts[q].sum(), 1))(rng.integers(0, len(ug), len(ug))) for _ in range(500)]
        return f'{v.mean():+5.2f} [{np.percentile(bs, 5):+.2f}..{np.percentile(bs, 95):+.2f}]'
    for s_ in ('cs2', 'valorant', 'dota2', 'tennis', 'football', 'hockey'):
        for label, kk in ((s_, sp == s_), (s_ + ', spread <= 2c', (sp == s_) & (V[:, 2 + nT + 5] <= 2.01))):
            if kk.sum() < 30:
                continue
            buy, s60, s180, m180 = V[kk, 2 + nT], V[kk, 3 + nT], V[kk, 4 + nT], V[kk, 5 + nT]
            g = V[kk, 0]
            rt60 = (s60 - buy - fee(buy) - fee(np.nan_to_num(s60, nan=0.5))) * 100
            rt180 = (s180 - buy - fee(buy) - fee(np.nan_to_num(s180, nan=0.5))) * 100
            one = (m180 - buy - fee(buy)) * 100
            say(f'   {label:<22}{int(kk.sum()):>7,}  {rngm(rt60, g)} | {rngm(rt180, g)} | {rngm(one, g)} | {np.nanmedian(V[kk, 2 + nT + 5]):.0f}c')
    np.save(os.path.join(a.signals, 'scorelead.npy'), V)
    with open(os.path.join(a.signals, 'scorelead.txt'), 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
