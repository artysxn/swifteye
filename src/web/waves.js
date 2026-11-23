// Swifteye: the wave analysis of tools/waves/waves.py, in the browser (Trade review's Analysis option).
// Same lines, wave candidates, register, Hayashi-Yoshida lead-lag and superline, with the same parameters;
// tools/waves/test_parity.mjs checks it against the Python on shared cases. Keep the two in step.
'use strict';
(function (root) {
const P = { MAX_SPREAD: 0.06, DEADBAND_PM: 0.015, DEADBAND_BOOK: 0.004, JOIN_MS: 8000, MIN_MOVE: 0.03, MIN_LOGIT: 0.12, MAX_LEN: 20000, HOLD: 10000, WINDOW: 180000, FOLLOW: 60000, EXEC_MS: 2200 };
const SL = { q: 0.0002, R: 0.01, jump: 0.15, jump_p: 0.03, prior: 0.6, cap: 3.0, resolve: 0.98, max_ms: 60000, bias_a: 0.05 };
const PM = 'polymarket', SUPER = 'superline';
const FAM = {
  linebet: '1xbet', melbet: '1xbet', betwinner: '1xbet', megapari: '1xbet', paripesa: '1xbet', '22bet': '1xbet', '22bit': '1xbet', '1xbit': '1xbet',
  unibet: 'kambi', leovegas: 'kambi', paf: 'kambi', fonbet: 'fonbet', pari: 'fonbet', mostbet: 'fonbet', vinn: 'altenar', betking: 'altenar',
  bcgame: 'betby', rollbit: 'betby', rainbet: 'betby', csgo500: 'betby', csgoempire: 'betby', duel: 'betby',
};
const family = (s) => FAM[s] || s;
const logit = (p) => { p = Math.min(Math.max(p, 1e-4), 1 - 1e-4); return Math.log(p / (1 - p)); };
const sig = (x) => 1 / (1 + Math.exp(-x));
const median = (a) => { const s = [...a].sort((x, y) => x - y); const n = s.length; return n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2; };

// ---- lines: [[t, p or null]], one point per change
function pmLine(rows) {
  const out = [];
  for (const [t, b, a] of rows || []) {
    let p = null;
    if (b != null && a != null && b > 0 && b < a && a < 1 && a - b <= P.MAX_SPREAD + 1e-9) p = (b + a) / 2;
    if (!out.length || out[out.length - 1][1] !== p) out.push([t, p]);
  }
  return out;
}
function bookLines(books, outcome) {
  const out = {};
  for (const [b, outs] of Object.entries(books || {})) {
    if (b === 'kalshi' || b === PM) continue;
    const names = Object.keys(outs).sort();
    if (!outs[outcome] || names.length < 2) continue;
    const ev = [];
    for (const o of names) for (const [t, pr] of outs[o]) ev.push([t, o, pr]);
    ev.sort((x, y) => x[0] - y[0]);
    const last = {}, line = [];
    let i = 0;
    while (i < ev.length) {
      const t = ev[i][0];
      while (i < ev.length && ev[i][0] === t) { last[ev[i][1]] = ev[i][2]; i++; }
      let p = null;
      if (names.every((o) => last[o] && last[o] > 1)) { const inv = names.map((o) => 1 / last[o]); p = (1 / last[outcome]) / inv.reduce((a, x) => a + x, 0); }
      if (!line.length || line[line.length - 1][1] !== p) line.push([t, p]);
    }
    if (line.filter((r) => r[1] != null).length >= 2) out[b] = line;
  }
  return out;
}
const upto = (line, t) => { let lo = 0, hi = line.length - 1, k = -1; while (lo <= hi) { const m = (lo + hi) >> 1; if (line[m][0] <= t) { k = m; lo = m + 1; } else hi = m - 1; } return k; };
function valueAt(line, t) { let i = upto(line, t); while (i >= 0 && line[i][1] == null) i--; return i >= 0 ? line[i][1] : null; }

// ---- moves and wave candidates
function increments(line, eps) {
  const inc = [];
  let lvl = null, gap = false;
  for (const [t, p] of line) {
    if (p == null) { gap = true; continue; }
    if (lvl == null) lvl = p;
    else if (Math.abs(p - lvl) >= eps) { inc.push([t, p - lvl, gap]); lvl = p; }
    gap = false;
  }
  return inc;
}
function segments(inc) {
  const segs = [];
  let cur = null;
  for (const [t, dp, re] of inc) {
    const d = dp > 0 ? 1 : -1;
    if (cur && d === cur.dir && t - cur.t1 <= P.JOIN_MS) { cur.t1 = t; cur.amp += dp; cur.n++; cur.reopen = cur.reopen || re; }
    else { if (cur) segs.push(cur); cur = { t0: t, t1: t, dir: d, amp: dp, n: 1, reopen: re }; }
  }
  if (cur) segs.push(cur);
  return segs;
}
function candidates(line, src, eps) {
  const out = [];
  for (const s of segments(increments(line, eps))) {
    if (s.t1 - s.t0 > P.MAX_LEN) continue;
    const before = valueAt(line, s.t0 - 1), after = valueAt(line, s.t1);
    if (before == null || after == null) continue;
    const amp = after - before;
    if (Math.abs(amp) < P.MIN_MOVE || Math.abs(logit(after) - logit(before)) < P.MIN_LOGIT || (amp > 0 ? 1 : -1) !== s.dir) continue;
    const later = valueAt(line, s.t1 + P.HOLD);
    if (later == null || (later - before) * s.dir < 0.5 * Math.abs(amp)) continue;
    out.push({ src, fam: family(src), t0: s.t0, t1: s.t1, dir: s.dir, amp: Math.abs(amp), p0: before, p1: after, reopen: s.reopen });
  }
  return out;
}

// ---- the register
function register(cands) {
  const waves = [];
  let open = [];
  for (const c of [...cands].sort((a, b) => a.t0 - b.t0)) {
    open = open.filter((w) => c.t0 - w.T <= P.WINDOW);
    let w = open.find((x) => x.dir === c.dir && !(c.src in x.on));
    if (!w) { w = { T: c.t0, dir: c.dir, on: {}, m: {} }; waves.push(w); open.push(w); }
    w.on[c.src] = c.t0;
    w.m[c.src] = c;
  }
  return waves;
}
function describe(w, pm) {
  const order = Object.entries(w.on).sort((a, b) => a[1] - b[1]);
  let books = Object.entries(w.m).filter(([s]) => s !== PM && s !== SUPER).map(([, c]) => c);
  if (!books.length) books = Object.entries(w.m).filter(([s]) => s !== PM).map(([, c]) => c);
  const amp = books.length ? median(books.map((c) => c.amp)) : w.m[PM].amp;
  const base = pm.length ? valueAt(pm, w.T - 1000) : null;
  const resp = {};
  if (base != null) for (const h of [0, 1000, 2000, 5000, 10000, 20000, 30000, 60000, 120000]) { const v = valueAt(pm, w.T + h); resp[h] = v == null ? null : (v - base) * w.dir / amp; }
  return { T: w.T, dir: w.dir, amp, leader: order[0][0], first_book: (order.find(([s]) => s !== PM && s !== SUPER) || [null])[0], pm_on: w.on[PM] ?? null, super_on: w.on[SUPER] ?? null,
    families: [...new Set(Object.keys(w.on).filter((s) => s !== PM && s !== SUPER).map(family))].sort(), on: { ...w.on }, pm_resp: resp, pm_base: base, members: w.m };
}

// ---- the superline (see waves.py superline)
function superline(lines, S = SL) {
  const ev = [];
  for (const [b, line] of Object.entries(lines)) for (const [t, p] of line) if (p != null) ev.push([t, family(b), logit(p)]);
  ev.sort((a, b) => a[0] - b[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) || a[2] - b[2]);
  const out = [], bias = {};
  let X = null, Pv = null, tLast = null, wave = null;
  const level = () => (wave ? wave.X0 + wave.pi * (wave.X1 - wave.X0) : X);
  for (const [t, f, y] of ev) {
    if (X == null) { X = y; Pv = S.R; tLast = t; bias[f] = 0; out.push([t, sig(X), null]); continue; }
    Pv += S.q * Math.max(0, t - tLast) / 1000;
    tLast = t;
    if (wave && t - wave.t0 > S.max_ms) { X = wave.pi >= 0.5 ? wave.X1 : wave.X0; Pv = S.R; wave = null; }
    if (!(f in bias)) { bias[f] = y - level(); out.push([t, out[out.length - 1][1], wave ? wave.pi : null]); continue; }
    const z = y - bias[f];
    if (!wave) {
      const nu = z - X;
      if (Math.abs(nu) >= S.jump && Math.abs(sig(z) - sig(X)) >= S.jump_p) wave = { X0: X, X1: z, t0: t, f0: f, votes: {}, pi: S.prior };
      else { const K = Pv / (Pv + S.R); X += K * nu; Pv *= 1 - K; bias[f] += S.bias_a * (y - X - bias[f]); }
    } else {
      if (f === wave.f0) wave.X1 = z;
      else { const Sv = S.R + Pv; const llr = ((z - wave.X0) ** 2 - (z - wave.X1) ** 2) / (2 * Sv); wave.votes[f] = Math.max(-S.cap, Math.min(S.cap, llr)); }
      const pr = Math.min(Math.max(S.prior, 1e-6), 1 - 1e-6);
      wave.pi = sig(Math.log(pr / (1 - pr)) + Object.values(wave.votes).reduce((a, x) => a + x, 0));
      if (wave.pi > S.resolve || wave.pi < 1 - S.resolve) { X = wave.pi >= 0.5 ? wave.X1 : wave.X0; Pv = S.R; wave = null; }
    }
    out.push([t, sig(level()), wave ? wave.pi : null]);
  }
  return out;
}

// ---- Hayashi-Yoshida lead-lag on log-odds increments (θ > 0: Polymarket's moves come θ after the line's)
const THETAS = (() => { const s = new Set(); for (let i = -120; i <= 120; i++) s.add(i * 1000); for (let i = -40; i <= 40; i++) s.add(i * 250); return [...s].sort((a, b) => a - b); })();
function clean(line) {
  const t = [], x = [];
  for (const [tt, p] of line) if (p != null && (!t.length || tt > t[t.length - 1])) { t.push(tt); x.push(logit(p)); }
  return t.length >= 2 ? { t, x } : null;
}
function lowerIdx(a, v) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < v) lo = m + 1; else hi = m; } return lo; }
function upperIdx(a, v) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] <= v) lo = m + 1; else hi = m; } return lo; }
function hy(book, pm) {
  const B = clean(book), Q = clean(pm);
  if (!B || !Q) return null;
  const n = Q.t.length;
  const U = THETAS.map(() => 0);
  let vb = 0, nb = 0;
  for (let i = 1; i < B.t.length; i++) {
    const db = B.x[i] - B.x[i - 1];
    if (!db) continue;
    vb += db * db;
    nb++;
    for (let k = 0; k < THETAS.length; k++) {
      const L = B.t[i - 1] + THETAS[k], R = B.t[i] + THETAS[k];
      const jb = Math.min(Math.max(upperIdx(Q.t, L) - 1, 0), n - 1), ja = Math.min(Math.max(lowerIdx(Q.t, R), 0), n - 1);
      U[k] += db * (Q.x[ja] - Q.x[jb]);
    }
  }
  if (!vb) return null;
  const lo = Math.max(B.t[0], Q.t[0]), hi = Math.min(B.t[B.t.length - 1], Q.t[n - 1]);
  let vp = 0, prev = null;
  for (let j = 0; j < n; j++) if (Q.t[j] >= lo && Q.t[j] <= hi) { if (prev != null) vp += (Q.x[j] - prev) ** 2; prev = Q.x[j]; }
  let best = 0;
  for (let k = 1; k < U.length; k++) if (Math.abs(U[k]) > Math.abs(U[best])) best = k;
  const z = THETAS.indexOf(0), den = Math.sqrt(vb * vp);
  return { U, vb, vp, n: nb, lag: THETAS[best], rho: den ? U[best] / den : null, rho0: den ? U[z] / den : null };
}

// ---- lead state and trend waves on a 1 s grid (see tools/waves/lead.py for the maths; same numbers)
const LEAD = { step: 1000, bias_half: 600, stale_half: 30, scale_half: 300, scale_floor: 0.03, scale_init: 0.06, clip: 1.5, z_on: 2.5, z_off: 1.0, stale_s: 180, kappa: 0.0305, pass: 0.17, horizon: 30 };
const TREND = { k: 0.002, h: 0.12 };
const ST = { LEVEL: 0, AHEAD_UP: 1, AHEAD_DOWN: 2, PM_AHEAD: 3, STALE: 4, UNKNOWN: 9 };
const lg = (p) => { p = Math.min(Math.max(p, 1e-4), 1 - 1e-4); return Math.log(p / (1 - p)); };
function grid(line, t0, t1, step) {
  const n = Math.floor((t1 - t0) / step) + 1, out = new Float64Array(n).fill(NaN);
  let j = -1;
  for (let i = 0; i < n; i++) {
    const t = t0 + i * step;
    while (j + 1 < line.length && line[j + 1][0] <= t) j++;
    if (j >= 0 && line[j][1] != null) out[i] = line[j][1];
  }
  return out;
}
function leadState(pm, sl, P = LEAD) {
  const n = pm.length, state = new Int8Array(n).fill(ST.UNKNOWN), E = new Float64Array(n).fill(NaN), Z = new Float64Array(n).fill(NaN), EXP = new Float64Array(n).fill(NaN), AGE = new Float64Array(n).fill(NaN);
  const aB = 1 - 0.5 ** (1 / P.bias_half), aSt = 1 - 0.5 ** (1 / P.stale_half), aS = 1 - 0.5 ** (1 / P.scale_half);
  const beta = P.pass * (1 - Math.exp(-P.kappa * P.horizon));
  let b = null, s = null, open = false, xa = 0, ya = 0, age = 0;
  for (let i = 0; i < n; i++) {
    if (Number.isNaN(pm[i]) || Number.isNaN(sl[i])) continue;
    const xi = lg(pm[i]), yi = lg(sl[i]), r = yi - xi;
    if (b === null) { b = r; s = P.scale_init; xa = xi; ya = yi; }
    const e = r - b, z = e / s;
    if (!open) { if (Math.abs(z) >= P.z_on) { open = true; age = 0; } else { xa = xi; ya = yi; } }
    else if (Math.abs(z) < P.z_off) { open = false; xa = xi; ya = yi; }
    let st = ST.LEVEL;
    if (open) {
      age++;
      const sg = e > 0 ? 1 : -1, cy = (yi - ya) * sg, cx = -(xi - xa) * sg;
      st = age > P.stale_s ? ST.STALE : cy >= cx ? (e > 0 ? ST.AHEAD_UP : ST.AHEAD_DOWN) : ST.PM_AHEAD;
    }
    state[i] = st; E[i] = e; Z[i] = z; AGE[i] = open ? age : 0;
    if (st === ST.AHEAD_UP || st === ST.AHEAD_DOWN) EXP[i] = 1 / (1 + Math.exp(-(xi + beta * e))) - 1 / (1 + Math.exp(-xi));
    if (st === ST.STALE) b += aSt * e; else b += aB * Math.max(-P.clip * s, Math.min(P.clip * s, e));
    if (!open) s = Math.max(P.scale_floor, s + aS * (Math.abs(e) * 1.2533 - s));
  }
  return { state, e: E, z: Z, exp: EXP, age: AGE };
}
function trends(p, P = TREND) {
  const n = p.length, eps = [];
  let u = 0, d = 0, uOn = 0, dOn = 0, cur = null, last = null;
  const x = Array.from(p, (v) => (Number.isNaN(v) ? NaN : lg(v)));
  for (let i = 0; i < n; i++) {
    const xi = x[i];
    if (Number.isNaN(xi)) continue;
    if (last === null) { last = xi; uOn = dOn = i; continue; }
    const dx = xi - last;
    last = xi;
    if (u === 0) uOn = i - 1;
    if (d === 0) dOn = i - 1;
    u = Math.max(0, u + dx - P.k);
    d = Math.max(0, d - dx - P.k);
    if (cur) {
      if ((cur.dir > 0 && xi > x[cur.peak]) || (cur.dir < 0 && xi < x[cur.peak])) cur.peak = i;
      const ended = cur.dir > 0 ? u === 0 : d === 0, flipped = cur.dir > 0 ? d >= P.h : u >= P.h;
      if (ended || flipped) { cur.end = i; eps.push(cur); cur = null; }
    }
    if (!cur) {
      if (u >= P.h) { cur = { dir: 1, on: uOn, alarm: i, peak: i, end: null }; d = 0; }
      else if (d >= P.h) { cur = { dir: -1, on: dOn, alarm: i, peak: i, end: null }; u = 0; }
    }
  }
  if (cur) { cur.end = n - 1; eps.push(cur); }
  for (const c of eps) { c.amp = x[c.on] == null || Number.isNaN(x[c.on]) ? NaN : x[c.peak] - x[c.on]; c.p_on = p[c.on]; c.p_peak = p[c.peak]; }
  return eps;
}
// Both, from two lines [[t, p]]: grid from where both exist to where the later one ends (= lead.py lead_all).
function lead(pmLine, slLine) {
  if (!pmLine.length || !slLine.length) return null;
  const t0 = Math.max(pmLine[0][0], slLine[0][0]), t1 = Math.max(pmLine[pmLine.length - 1][0], slLine[slLine.length - 1][0]);
  if (t1 <= t0) return null;
  const gp = grid(pmLine, t0, t1, LEAD.step), gs = grid(slLine, t0, t1, LEAD.step);
  return { t0, step: LEAD.step, n: gp.length, pm: gp, sl: gs, ...leadState(gp, gs), sl_trends: trends(gs), pm_trends: trends(gp) };
}

// ---- one (game, outcome): lines, superline, waves, per-source rows, lead-lag of every line
// (opts.corr = false skips the lead-lag, the slow part on a long game: the page runs hy() per line itself)
function analyse(G, outcome, opts = {}) {
  const pm = pmLine((G.pm || {})[outcome]);
  const books = bookLines(G.books, outcome);
  const sl = Object.keys(books).length ? superline(books) : [];
  const slLine = sl.map(([t, p]) => [t, p]);
  let cands = [];
  if (pm.length) cands = cands.concat(candidates(pm, PM, P.DEADBAND_PM));
  for (const [b, line] of Object.entries(books)) cands = cands.concat(candidates(line, b, P.DEADBAND_BOOK));
  if (slLine.length) cands = cands.concat(candidates(slLine, SUPER, P.DEADBAND_BOOK));
  const waves = register(cands).map((w) => describe(w, pm));
  const corr = {};
  if (pm.length && opts.corr !== false) {
    for (const [b, line] of Object.entries(books)) { const h = hy(line, pm); if (h) corr[b] = { lag: h.lag, rho: h.rho, rho0: h.rho0, n: h.n }; }
    if (slLine.length) { const h = hy(slLine, pm); if (h) corr[SUPER] = { lag: h.lag, rho: h.rho, rho0: h.rho0, n: h.n }; }
  }
  return { pm, books, superline: sl, waves, corr, lead: lead(pm, slLine) };
}

const api = { P, SL, PM, SUPER, family, logit, pmLine, bookLines, valueAt, increments, segments, candidates, register, describe, superline, hy, analyse, THETAS, LEAD, TREND, ST, grid, leadState, trends, lead };
if (typeof module !== 'undefined' && module.exports) module.exports = api;
if (root && root.SE) root.SE.waves = api;
})(typeof window !== 'undefined' ? window : globalThis);
