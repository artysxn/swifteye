// Snipe window research: which entry rules (known at the moment of buying) make the paper model
// pay, chosen WITHOUT hindsight. Same discipline as the insights bot's research
// (aim4 tools/vision opsweepcv.mjs / pmci.mjs): pick settings on the earlier part of the data
// only, report them on the later part, and resample whole matches to see how sure the result is.
//
//   node tools/snipe_research.mjs [hours=24] [split=0.6]
//
// It asks the running service for every trade under a permissive gate (so every candidate rule
// can be applied afterwards), then grid-searches the gate per market.
const HOURS = +(process.argv[2] || 24);
const SPLIT = +(process.argv[3] || 0.6);
const BASE = 'http://127.0.0.1:8787';

// Permissive gate: every rule below is a filter on these trades. The delayed check itself
// (market made < 25% of the move) is the user's rule and stays.
const loose = 'k_need_real=false&pm_min_edge_c=0&pm_min_books=1&pm_max_spread_c=99&pm_max_age_s=86400&pm_max_followed_pct=25&pm_min_entry_c=0&k_min_edge_c=0&k_min_books=1&k_max_spread_c=99&k_max_age_s=86400&k_max_followed_pct=25&k_min_entry_c=0';
const url = `${BASE}/snipe?since=${Date.now() - HOURS * 3600e3}&all=true&${loose}`;
console.log(`fetching ${url}`);
const d = await (await fetch(url)).json();

// mulberry32 (the insights bot's fixed RNG)
let seed = 7;
const rnd = () => { seed = (seed + 0x6d2b79f5) >>> 0; let t = seed; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
const usd = (x) => `${x < 0 ? '-' : '+'}$${Math.abs(x).toFixed(0)}`;
const pct = (x) => `${(x * 100).toFixed(1)}%`;

const grid = [];
for (const books of [1, 2, 3])
  for (const spread of [0.02, 0.03, 0.04, 0.06, 0.99])
    for (const edge of [0, 0.01, 0.02, 0.03, 0.05])
      for (const age of [60e3, 300e3, 864e5])
        for (const fol of [0.1, 0.25])
          for (const entry of [0, 0.3, 0.4, 0.5]) grid.push({ books, spread, edge, age, fol, entry });
const pass = (t, g) => t.books >= g.books && t.spread <= g.spread + 1e-9 && t.edge >= g.edge && t.quote_age_ms <= g.age && t.followed <= g.fol && t.entry >= g.entry;
const stats = (ts) => {
  const n = ts.length, pnl = ts.reduce((a, t) => a + t.pnl, 0);
  return { n, pnl, roi: n ? pnl / (n * 100) : 0, win: n ? ts.filter((t) => t.pnl > 0).length / n : 0 };
};
// Whole matches resampled (trades inside one match move together).
const boot = (ts) => {
  const by = new Map();
  for (const t of ts) by.set(t.match_id, (by.get(t.match_id) || 0) + t.pnl);
  const ms = [...by.values()];
  if (!ms.length) return { loss: null, lo: 0, hi: 0 };
  const out = [];
  for (let i = 0; i < 5000; i++) { let s = 0; for (let j = 0; j < ms.length; j++) s += ms[Math.floor(rnd() * ms.length)]; out.push(s); }
  out.sort((a, b) => a - b);
  return { loss: out.filter((x) => x < 0).length / out.length, lo: out[250], hi: out[4750] };
};
const label = (g) => `entry>=${Math.round(g.entry * 100)}c books>=${g.books} spread<=${Math.round(g.spread * 100)}c edge>=${Math.round(g.edge * 100)}c age<=${g.age >= 864e5 ? 'any' : g.age / 1000 + 's'} followed<=${g.fol * 100}%`;

const chosen = {};
for (const b of d.baselines) {
  console.log(`\n=== ${b.baseline} · quotes ${JSON.stringify(b.quote_source)}`);
  for (const h of b.holds) {
    // Trades on an assumed Kalshi spread (no real price known) are data gaps, not decisions.
    const all = h.recent.filter((t) => t.src !== 'assumed').sort((a, c) => a.ts - c.ts);
    if (all.length < 20) { console.log(`give-up ${h.hold_s}s: ${all.length} trades, too few`); continue; }
    const cut = all[Math.floor(all.length * SPLIT)].ts;
    const train = all.filter((t) => t.ts < cut), test = all.filter((t) => t.ts >= cut);
    const minN = Math.max(15, Math.round(train.length * 0.05));
    let best = null;
    for (const g of grid) {
      const s = stats(train.filter((t) => pass(t, g)));
      if (s.n < minN) continue;
      // Rank by total P&L on train (a rule that makes $ on few trades ranks below one that makes more).
      if (!best || s.pnl > best.s.pnl) best = { g, s };
    }
    const base = stats(test), all0 = stats(train);
    console.log(`\ngive-up ${h.hold_s}s · ${all.length} trades (train ${train.length} before ${new Date(cut).toISOString().slice(11, 16)} UTC, test ${test.length})`);
    console.log(`  no extra rule:   train ${usd(all0.pnl)} ${pct(all0.roi)}/trade | test ${usd(base.pnl)} ${pct(base.roi)}/trade (${base.n})`);
    if (!best) { console.log('  no rule with enough trades'); continue; }
    const tt = test.filter((t) => pass(t, best.g)), st = stats(tt), bs = boot(tt);
    console.log(`  best on train:   ${label(best.g)}`);
    console.log(`                   train ${usd(best.s.pnl)} ${pct(best.s.roi)}/trade (${best.s.n}, win ${pct(best.s.win)}) | TEST ${usd(st.pnl)} ${pct(st.roi)}/trade (${st.n}, win ${pct(st.win)}), loss chance ${bs.loss == null ? '-' : pct(bs.loss)}, 90% ${usd(bs.lo)}..${usd(bs.hi)}`);
    // Each single rule on its own, train vs test, to see which ones carry the result.
    for (const [k, vals] of [['books', [1, 2, 3]], ['spread', [0.02, 0.03, 0.04, 0.99]], ['edge', [0, 0.01, 0.02, 0.03]], ['age', [60e3, 300e3, 864e5]]]) {
      const row = vals.map((v) => {
        const g = { books: 1, spread: 0.99, edge: 0, age: 864e5, fol: 0.25, entry: 0, [k]: v };
        const a = stats(train.filter((t) => pass(t, g))), c = stats(test.filter((t) => pass(t, g)));
        return `${k === 'spread' || k === 'edge' ? Math.round(v * 100) + 'c' : k === 'age' ? (v >= 864e5 ? 'any' : v / 1000 + 's') : v}: ${pct(a.roi)}/${pct(c.roi)} (${a.n}/${c.n})`;
      });
      console.log(`    ${k.padEnd(6)} train/test per trade: ${row.join(' · ')}`);
    }
    chosen[`${b.baseline}:${h.hold_s}`] = { gate: best.g, train: best.s, test: st, loss: bs.loss };
  }
}
console.log('\nJSON ' + JSON.stringify(chosen));
