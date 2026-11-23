"""Waves, delay and correlation between Polymarket and every bookmaker (owner, 23 Nov 2026).

The first step of the new detection system (theory in AGENTS.md, section 8f). Every source is read as a
late, noisy view of one true price; this module finds the WAVES (bursts of movement lasting seconds),
assembles each wave across all sources, and scores every source against Polymarket.

  lines     each source as a step series of probabilities: Polymarket's mid (spread <= MAX_SPREAD; its own
            clock or our receive clock), each bookmaker's margin-free probability (1/odds normalised over
            the outcomes it lists), None while suspended / spread too wide
  moves     a dead band ignores flicker (Polymarket's bid/ask bounce); changes of the kept level in the
            same direction, close in time, form a segment; a WAVE CANDIDATE is a segment of at least
            MIN_MOVE probability and MIN_LOGIT log-odds, at most MAX_LEN long, that still holds half of
            itself HOLD after it ends. A bookmaker reopening after a suspension at a new price counts
            from the price before the suspension (a goal)
  register  candidates of all sources assembled into waves: same direction, onset within WINDOW of the
            wave's first onset, each source at most once. Per wave: the onset of every source, who led,
            and how far Polymarket had gone (whether or not it had a candidate of its own)
  scores    per source: onset lead over Polymarket, share of waves it led, precision (Polymarket made at
            least half of its move within FOLLOW after it), recall (it showed Polymarket's waves),
            pass-through (Polymarket's move per unit of its move), catch-up share (Polymarket had already
            made half the wave when it moved), relative delay from a two-way model over all sources, and
            the Hayashi-Yoshida lead-lag correlation on increments (no common grid, so no Epps bias)

Times are ms. Probabilities 0..1; log-odds = logit(p). Directions +1 / -1 for the outcome's probability.
"""
import math
import os
import sqlite3
from collections import defaultdict

import numpy as np

PM = 'polymarket'
ANY = 'ANY BOOK'   # per wave, whichever bookmaker moved first: the ceiling of a superline
SUPER = 'superline'  # the superline as a source of its own (see superline())
# Brands on one trading desk (snipe::platform): one confirmation, one family.
FAM = {
    'linebet': '1xbet', 'melbet': '1xbet', 'betwinner': '1xbet', 'megapari': '1xbet', 'paripesa': '1xbet',
    '22bet': '1xbet', '22bit': '1xbet', '1xbit': '1xbet',
    'unibet': 'kambi', 'leovegas': 'kambi', 'paf': 'kambi',
    'fonbet': 'fonbet', 'pari': 'fonbet', 'mostbet': 'fonbet',
    'vinn': 'altenar', 'betking': 'altenar',
    'bcgame': 'betby', 'rollbit': 'betby', 'rainbet': 'betby', 'csgo500': 'betby', 'csgoempire': 'betby', 'duel': 'betby',
}
THREE_WAY = {'football', 'handball'}

# ---- parameters (the defaults the report uses; all can be changed per run)
MAX_SPREAD = 0.06        # Polymarket mid only while the spread is at most 6c
DEADBAND_PM = 0.015      # ignore Polymarket changes under 1.5 points (bid/ask bounce)
DEADBAND_BOOK = 0.004    # bookmakers step; ignore rounding noise
JOIN_MS = 8_000          # changes this close, same direction, are one segment
MIN_MOVE = 0.03          # a wave moves at least 3 points ...
MIN_LOGIT = 0.12         # ... and 0.12 log-odds (so tiny prices need a real relative move)
MAX_LEN = 20_000         # a wave is over within 20 s
HOLD = 10_000            # and still holds half of itself 10 s after it ends
WINDOW = 180_000         # sources whose onset is within 3 minutes of the first belong to the same wave
                         # (a slow board catching up 140 s late, owner's mark #1, is still the same wave)
FOLLOW = 60_000          # Polymarket "followed" a source if it made half the move within 60 s
EXEC_MS = 2_200          # our measured buy latency: a lead shorter than this cannot be traded
RESPONSE_H = (0, 1_000, 2_000, 5_000, 10_000, 20_000, 30_000, 60_000, 120_000)
THETAS = np.unique(np.concatenate([np.arange(-120, 121, 1.0), np.arange(-10, 10.25, 0.25)])) * 1000


def family(src):
    return FAM.get(src, src)


def logit(p):
    p = min(max(p, 1e-4), 1 - 1e-4)
    return math.log(p / (1 - p))


# ---------------------------------------------------------------- data

def default_db():
    for p in (os.environ.get('SWIFTEYE_DB'), os.path.expanduser('~/swifteye/oddsfeed.db'),
              os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'run', 'oddsfeed.db')):
        if p and os.path.exists(p):
            return os.path.abspath(p)
    raise SystemExit('no database: set SWIFTEYE_DB')


def connect(path):
    db = sqlite3.connect('file:' + path.replace('\\', '/') + '?mode=ro', uri=True, timeout=30)
    db.execute('PRAGMA busy_timeout = 30000')
    return db


def games(db, since_ms=None, sport=None, ids=None):
    """Games Polymarket listed with at least one bookmaker's 1X2: [(id, sport, home, away, start_ms)]."""
    q = """SELECT m.id, m.sport, m.home, m.away, m.start_ms FROM matches m
           WHERE m.id IN (SELECT match_id FROM book_events WHERE book = 'polymarket')
             AND m.id IN (SELECT match_id FROM book_events WHERE book NOT IN ('polymarket', 'kalshi'))"""
    q += ' AND m.start_ms IS NOT NULL AND m.start_ms <= ?'   # started: their live moves are recorded
    args = [int(__import__('time').time() * 1000)]
    if since_ms:
        q += ' AND m.start_ms >= ?'
        args.append(since_ms)
    if sport:
        q += ' AND m.sport = ?'
        args.append(sport)
    rows = db.execute(q + ' ORDER BY m.start_ms', args).fetchall()
    if ids:
        ids = set(ids)
        rows = [r for r in rows if r[0] in ids]
    return rows


def load(db, m):
    """(Polymarket rows, bookmaker rows) of one game."""
    pm = db.execute('SELECT ts_ms, server_ts_ms, outcome, bid, ask FROM pm_quotes WHERE match_id = ? ORDER BY ts_ms', (m,)).fetchall()
    bk = []
    for table in ('changes', 'changes_research'):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
            continue   # changes_research only exists on newer builds
        bk += db.execute(f"SELECT ts_ms, book, outcome, new_price FROM {table} WHERE match_id = ? AND market = '1x2' AND book NOT IN ('polymarket', 'kalshi') ORDER BY ts_ms", (m,)).fetchall()
    return pm, bk


# ---------------------------------------------------------------- lines

def pm_line(rows, outcome, clock='receive'):
    """[(t, p or None)]: Polymarket's mid for `outcome`, one point per change. None while a side is missing or
    the spread is over MAX_SPREAD. clock 'source' = Polymarket's own stamps (who knew first), 'receive' = ours
    (what we could trade on)."""
    pts = []
    for ts, srv, o, bid, ask in rows:
        if o != outcome:
            continue
        t = srv if clock == 'source' and srv else ts
        p = None
        if bid is not None and ask is not None and 0 < bid < ask < 1 and ask - bid <= MAX_SPREAD + 1e-9:
            p = (bid + ask) / 2
        pts.append((t, p))
    pts.sort(key=lambda r: r[0])
    out = []
    for t, p in pts:
        if not out or out[-1][1] != p:
            out.append((t, p))
    return out


def book_lines(rows, outcome):
    """{book: [(t, p or None)]}: each bookmaker's margin-free probability of `outcome` (1/odds normalised over
    the outcomes it lists for the game), one point per change; None while any of its outcomes is suspended."""
    by = defaultdict(list)
    for ts, b, o, price in rows:
        by[b].append((ts, o, price))
    out = {}
    for b, rs in by.items():
        rs.sort(key=lambda r: r[0])
        outs = sorted({o for _, o, _ in rs})
        if outcome not in outs or len(outs) < 2:
            continue
        last, line, i = {}, [], 0
        while i < len(rs):
            t = rs[i][0]
            while i < len(rs) and rs[i][0] == t:   # rows of one poll: one moment
                last[rs[i][1]] = rs[i][2]
                i += 1
            if all(last.get(o) and last[o] > 1 for o in outs):
                inv = {o: 1 / last[o] for o in outs}
                p = inv[outcome] / sum(inv.values())
            else:
                p = None
            if not line or line[-1][1] != p:
                line.append((t, p))
        if sum(1 for _, p in line if p is not None) >= 2:
            out[b] = line
    return out


def value_at(line, t, ts=None):
    """The line's last real value at or before t (None if none)."""
    ts = ts if ts is not None else [r[0] for r in line]
    i = np.searchsorted(ts, t, side='right') - 1
    while i >= 0 and line[i][1] is None:
        i -= 1
    return line[i][1] if i >= 0 else None


def value_after(line, t, ts=None):
    """The first real value at or after t (None if none)."""
    ts = ts if ts is not None else [r[0] for r in line]
    i = np.searchsorted(ts, t, side='left')
    while i < len(line) and line[i][1] is None:
        i += 1
    return line[i][1] if i < len(line) else None


# ---------------------------------------------------------------- moves and wave candidates

def increments(line, eps):
    """Changes of the kept level: [(t, dp, reopened)]. Changes under eps from the kept level are ignored
    (flicker). A gap (None) is bridged: the first value after it is measured from the level before it."""
    inc, lvl, gap = [], None, False
    for t, p in line:
        if p is None:
            gap = True
            continue
        if lvl is None:
            lvl = p
        elif abs(p - lvl) >= eps:
            inc.append((t, p - lvl, gap))
            lvl = p
        gap = False
    return inc


def segments(inc, join_ms=JOIN_MS):
    """Same-direction changes no more than join_ms apart, as segments {t0, t1, dir, amp, n, reopen}."""
    segs, cur = [], None
    for t, dp, re in inc:
        d = 1 if dp > 0 else -1
        if cur and d == cur['dir'] and t - cur['t1'] <= join_ms:
            cur['t1'] = t
            cur['amp'] += dp
            cur['n'] += 1
            cur['reopen'] = cur['reopen'] or re
        else:
            if cur:
                segs.append(cur)
            cur = {'t0': t, 't1': t, 'dir': d, 'amp': dp, 'n': 1, 'reopen': re}
    if cur:
        segs.append(cur)
    return segs


def candidates(line, src, eps):
    """Wave candidates of one source: big enough, short enough, and still holding half 10 s later."""
    ts = [r[0] for r in line]
    out = []
    for s in segments(increments(line, eps)):
        if s['t1'] - s['t0'] > MAX_LEN:
            continue
        before = value_at(line, s['t0'] - 1, ts)
        after = value_at(line, s['t1'], ts)
        if before is None or after is None:
            continue
        amp = after - before
        if abs(amp) < MIN_MOVE or abs(logit(after) - logit(before)) < MIN_LOGIT or (1 if amp > 0 else -1) != s['dir']:
            continue
        later = value_at(line, s['t1'] + HOLD, ts)
        if later is None or (later - before) * s['dir'] < 0.5 * abs(amp):
            continue
        out.append({'src': src, 'fam': family(src), 't0': s['t0'], 't1': s['t1'], 'dir': s['dir'], 'amp': abs(amp),
                    'p0': before, 'p1': after, 'reopen': s['reopen']})
    return out


# ---------------------------------------------------------------- the register

def register(cands, window=WINDOW):
    """Waves: candidates of all sources with the same direction, onset within `window` of the wave's first
    onset, each source at most once (a second move of a source in the window opens a new wave)."""
    waves, open_ = [], []
    for c in sorted(cands, key=lambda c: c['t0']):
        open_ = [w for w in open_ if c['t0'] - w['T'] <= window]
        w = next((w for w in open_ if w['dir'] == c['dir'] and c['src'] not in w['on']), None)
        if w is None:
            w = {'T': c['t0'], 'dir': c['dir'], 'on': {}, 'm': {}}
            waves.append(w)
            open_.append(w)
        w['on'][c['src']] = c['t0']
        w['m'][c['src']] = c
    return waves


def describe(w, pm, pm_ts):
    """Per wave: who led, the bookmakers' size of the move, and Polymarket's response measured on its own line
    from just before the wave's FIRST onset (any source, Polymarket included): completion at RESPONSE_H after."""
    order = sorted(w['on'].items(), key=lambda x: x[1])
    books = [c for s, c in w['m'].items() if s not in (PM, SUPER)] or [c for s, c in w['m'].items() if s != PM]
    amp = float(np.median([c['amp'] for c in books])) if books else w['m'][PM]['amp']
    base = value_at(pm, w['T'] - 1_000, pm_ts) if pm else None
    resp = {}
    if base is not None:
        for h in RESPONSE_H:
            v = value_at(pm, w['T'] + h, pm_ts)
            resp[h] = None if v is None else (v - base) * w['dir'] / amp
    first_book = next((s for s, _ in order if s not in (PM, SUPER)), None)
    return {'T': w['T'], 'dir': w['dir'], 'amp': amp, 'leader': order[0][0], 'first_book': first_book,
            'pm_on': w['on'].get(PM), 'super_on': w['on'].get(SUPER), 'families': sorted({family(s) for s in w['on'] if s not in (PM, SUPER)}),
            'on': dict(w['on']), 'pm_resp': resp, 'pm_base': base}


def game_waves(pm_rows, bk_rows, outcome, clock='receive'):
    """Everything one (game, outcome) gives: the lines, the waves, and per-source wave rows."""
    pm = pm_line(pm_rows, outcome, clock)
    books = book_lines(bk_rows, outcome)
    sl = [(t, p) for t, p, _ in superline(books)] if books else []
    cands = []
    if pm:
        cands += candidates(pm, PM, DEADBAND_PM)
    for b, line in books.items():
        cands += candidates(line, b, DEADBAND_BOOK)
    if sl:
        cands += candidates(sl, SUPER, DEADBAND_BOOK)
    waves = register(cands)
    pm_ts = [r[0] for r in pm]
    desc = [describe(w, pm, pm_ts) for w in waves]
    # Per source in each wave: its onset, its move, and what Polymarket did around it.
    rows = []
    for wi, (w, d) in enumerate(zip(waves, desc)):
        books_in = [(c['t0'], s) for s, c in w['m'].items() if s not in (PM, SUPER)]
        fastest = min(books_in)[1] if books_in else None
        for s0, c in list(w['m'].items()) + ([(ANY, w['m'][fastest])] if fastest else []):
            s = s0
            if s == PM:
                continue
            t = c['t0']
            pm_before = value_at(pm, t - 1_000, pm_ts) if pm else None
            pm_after = value_at(pm, t + FOLLOW, pm_ts) if pm else None
            pm_at_wave = d['pm_base']
            # How much of the wave Polymarket had made when this source moved (from the wave's start).
            done = None if pm_before is None or pm_at_wave is None else (pm_before - pm_at_wave) * w['dir'] / c['amp']
            gain = None if pm_before is None or pm_after is None else (pm_after - pm_before) * w['dir']
            rows.append({'src': s, 'fam': family(s) if s != ANY else ANY, 'wave': wi, 'by': c['src'], 't': t, 'amp': c['amp'], 'dir': w['dir'], 'reopen': c['reopen'],
                         'pm_on': d['pm_on'], 'lead': None if d['pm_on'] is None else d['pm_on'] - t,
                         'led_pm': d['pm_on'] is None or d['pm_on'] > t, 'first': d['leader'] == c['src'],
                         'pm_done_before': done, 'pm_gain': gain, 'followed': None if gain is None else gain >= 0.5 * c['amp']})
    return {'pm': pm, 'books': books, 'superline': sl, 'waves': desc, 'rows': rows}


# ---------------------------------------------------------------- Hayashi-Yoshida lead-lag

def _clean(line):
    t = np.array([r[0] for r in line if r[1] is not None], dtype=float)
    x = np.array([logit(r[1]) for r in line if r[1] is not None], dtype=float)
    if len(t) < 2:
        return None, None
    keep = np.concatenate([[True], np.diff(t) > 0])
    return t[keep], x[keep]


def hy_contrast(book, pm, thetas=THETAS):
    """Hayashi-Yoshida lead-lag contrast on log-odds increments (Hoffmann, Rosenbaum & Yoshida 2013):
      U(θ) = Σ_i ΔB(I_i) · Σ_{j: J_j - θ overlaps I_i} ΔP(J_j)
    The inner sum telescopes: PM's value at its first tick at or after the shifted interval's end, minus its
    value at its last tick at or before the shifted interval's start. θ > 0: Polymarket's moves line up with
    the bookmaker's θ later (the bookmaker leads). Returns (U, ΣΔB², ΣΔP² over the overlap) or None."""
    tb, xb = _clean(book)
    tp, xp = _clean(pm)
    if tb is None or tp is None:
        return None
    a0, a1, db = tb[:-1], tb[1:], np.diff(xb)
    nz = db != 0
    if not nz.any():
        return None
    a0, a1, db = a0[nz], a1[nz], db[nz]
    L = a0[:, None] + thetas[None, :]
    R = a1[:, None] + thetas[None, :]
    jb = np.clip(np.searchsorted(tp, L, side='right') - 1, 0, len(tp) - 1)
    ja = np.clip(np.searchsorted(tp, R, side='left'), 0, len(tp) - 1)
    dp = xp[ja] - xp[jb]
    U = (db[:, None] * dp).sum(axis=0)
    lo, hi = max(tb[0], tp[0]), min(tb[-1], tp[-1])
    inside = (tp >= lo) & (tp <= hi)
    vp = float((np.diff(xp[inside]) ** 2).sum()) if inside.sum() > 1 else 0.0
    return U, float((db ** 2).sum()), vp


# ---------------------------------------------------------------- relative delay: two-way model

def two_way_delays(waves, iters=30):
    """t_s(w) = T_w + δ_s + ε: median polish over waves with 2+ sources, δ of Polymarket fixed at 0.
    Returns {source: δ_s in ms} (negative = earlier than Polymarket). Grouped medians (pandas), so a few
    hundred thousand onsets take well under a second."""
    import pandas as pd
    recs = [(k, s, t) for k, w in enumerate(x for x in waves if len(x['on']) >= 2) for s, t in w['on'].items()]
    if not recs:
        return {}
    df = pd.DataFrame(recs, columns=['w', 's', 't'])
    df['t'] = df['t'].astype(float)
    df['d'] = 0.0
    delta = pd.Series(0.0, index=sorted(df['s'].unique()))
    for _ in range(iters):
        df['d'] = df['s'].map(delta)
        T = (df['t'] - df['d']).groupby(df['w']).median()
        delta = (df['t'] - df['w'].map(T)).groupby(df['s']).median()
        if PM in delta.index:
            delta = delta - delta[PM]
    return {s: float(v) for s, v in delta.items()}


# ---------------------------------------------------------------- the superline

# Parameters of the superline (the same numbers in src/web/waves.js).
SL = {
    'q': 0.0002,        # drift of the true level, log-odds² per second (calm periods)
    'R': 0.01,          # a family's reading noise around the level after its bias, log-odds²
    'jump': 0.15,       # a reading this far (log-odds) from the line ...
    'jump_p': 0.03,     # ... and 3 points away opens a wave
    'prior': 0.6,       # belief in a wave right after the first family moves (before any other reads)
    'cap': 3.0,         # one family's evidence is capped at ±3 nats (about ×20)
    'resolve': 0.98,    # a wave is settled when its posterior passes 98% (or falls under 2%) ...
    'max_ms': 60_000,   # ... or after 60 s, to the likelier side
    'bias_a': 0.05,     # how fast a family's offset (margin, optimism) is learned, calm readings only
}


def _sig(x):
    return 1 / (1 + math.exp(-x))


def superline(lines, P=SL):
    """The fastest credible line from all bookmakers: [(t, p, wave_posterior or None)].

    Every bookmaker's margin-free probability (log-odds) is a reading of one level; brands of one trading
    desk are one family. Calm: a scalar Kalman filter follows the level and learns each family's offset.
    When a family's reading lands far from the line, a WAVE opens with two hypotheses, the old level X0 and
    that family's new level X1. The line moves at once to X0 + prior·(X1 − X0) (the quickest reaction).
    Every other family then votes with its LATEST reading only (sister brands and repeats replace, never
    add): the log-likelihood ratio of X1 against X0, capped. The posterior π = σ(logit(prior) + Σ votes) and
    the line sits at X0 + π·(X1 − X0); at π > resolve or < 1 − resolve, or after max_ms, it settles.
    (A sequential probability ratio test per wave, Wald 1945, with one vote per independent desk.)"""
    ev = sorted((t, family(b), logit(p)) for b, line in lines.items() for t, p in line if p is not None)
    out, bias = [], {}
    X = Pv = t_last = None
    wave = None
    for t, f, y in ev:
        if X is None:
            X, Pv, t_last, bias[f] = y, P['R'], t, 0.0
            out.append((t, _sig(X), None))
            continue
        Pv += P['q'] * max(0, t - t_last) / 1000
        t_last = t
        if wave and t - wave['t0'] > P['max_ms']:
            X = wave['X1'] if wave['pi'] >= 0.5 else wave['X0']
            Pv, wave = P['R'], None
        if f not in bias:                      # a family's first reading: only its offset
            bias[f] = y - (X if wave is None else wave['X0'] + wave['pi'] * (wave['X1'] - wave['X0']))
            out.append((t, out[-1][1], None if wave is None else wave['pi']))
            continue
        z = y - bias[f]
        if wave is None:
            nu = z - X
            if abs(nu) >= P['jump'] and abs(_sig(z) - _sig(X)) >= P['jump_p']:
                wave = {'X0': X, 'X1': z, 't0': t, 'f0': f, 'votes': {}, 'pi': P['prior']}
            else:
                K = Pv / (Pv + P['R'])
                X += K * nu
                Pv *= 1 - K
                bias[f] += P['bias_a'] * (y - X - bias[f])
        else:
            if f == wave['f0']:
                wave['X1'] = z                  # the family that opened it refines its level
            else:
                S = P['R'] + Pv
                llr = ((z - wave['X0']) ** 2 - (z - wave['X1']) ** 2) / (2 * S)
                wave['votes'][f] = max(-P['cap'], min(P['cap'], llr))
            pr = min(max(P['prior'], 1e-6), 1 - 1e-6)
            wave['pi'] = _sig(math.log(pr / (1 - pr)) + sum(wave['votes'].values()))
            if wave['pi'] > P['resolve'] or wave['pi'] < 1 - P['resolve']:
                X = wave['X1'] if wave['pi'] >= 0.5 else wave['X0']
                Pv, wave = P['R'], None
        level = X if wave is None else wave['X0'] + wave['pi'] * (wave['X1'] - wave['X0'])
        out.append((t, _sig(level), None if wave is None else wave['pi']))
    return out
