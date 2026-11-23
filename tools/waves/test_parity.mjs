// node tools/waves/test_parity.mjs — src/web/waves.js against the Python (tools/waves/parity_fixture.json,
// made by tools/waves/make_parity.py). The browser's Analysis must show what the research tool measures.
import { createRequire } from 'module';
import { readFileSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const W = require(join(here, '..', '..', 'src', 'web', 'waves.js'));
const { cases } = JSON.parse(readFileSync(join(here, 'parity_fixture.json'), 'utf8'));
let passed = 0;
const ok = (name, cond, detail = '') => { if (!cond) { console.error('FAIL -', name, detail); process.exit(1); } passed++; console.log('ok -', name); };

for (const c of cases) {
  const js = W.analyse(c.G, c.outcome), py = c.python;
  ok(`${c.name}: superline has the same points`, js.superline.length === py.superline.length, `${js.superline.length} vs ${py.superline.length}`);
  const worst = Math.max(0, ...js.superline.map((r, i) => Math.abs(r[1] - py.superline[i][1])));
  ok(`${c.name}: superline values agree (worst ${worst.toExponential(1)})`, worst < 1e-9);
  ok(`${c.name}: same waves (${py.waves.length})`, js.waves.length === py.waves.length, `${js.waves.length} vs ${py.waves.length}`);
  const same = js.waves.every((w, i) => w.T === py.waves[i].T && w.dir === py.waves[i].dir && w.leader === py.waves[i].leader && (w.pm_on ?? null) === py.waves[i].pm_on &&
    JSON.stringify(Object.entries(w.on).sort()) === JSON.stringify(Object.entries(py.waves[i].on).sort()));
  ok(`${c.name}: wave onsets, leaders and Polymarket onsets agree`, same);
  if (py.lead) {
    const jl = js.lead;
    ok(`${c.name}: lead grid the same (${py.lead.n} s)`, jl && jl.t0 === py.lead.t0 && jl.n === py.lead.n, jl ? `${jl.t0}/${jl.n}` : 'none');
    const bad = py.lead.state.findIndex((v, i) => v !== jl.state[i]);
    ok(`${c.name}: lead state agrees every second`, bad < 0, `first difference at ${bad}: py ${py.lead.state[bad]} js ${jl.state[bad]}`);
    const worstE = Math.max(0, ...py.lead.e.map((v, i) => (v == null ? (Number.isNaN(jl.e[i]) ? 0 : 1) : Math.abs(v - jl.e[i]))));
    ok(`${c.name}: gap agrees (worst ${worstE.toExponential(1)})`, worstE < 1e-9);
    const same = (a, b) => a.length === b.length && a.every((e, i) => ['dir', 'on', 'alarm', 'peak', 'end'].every((k) => e[k] === b[i][k]));
    ok(`${c.name}: trend waves agree (superline ${py.lead.sl_trends.length}, Polymarket ${py.lead.pm_trends.length})`, same(jl.sl_trends, py.lead.sl_trends) && same(jl.pm_trends, py.lead.pm_trends));
  }
  const books = Object.keys(py.corr);
  const corrOk = books.every((b) => js.corr[b] && js.corr[b].lag === py.corr[b].lag && Math.abs((js.corr[b].rho ?? 0) - (py.corr[b].rho ?? 0)) < 1e-6);
  ok(`${c.name}: lead-lag and correlation agree for ${books.length} lines`, corrOk, JSON.stringify(books.filter((b) => !js.corr[b] || js.corr[b].lag !== py.corr[b].lag).slice(0, 3)));
}
console.log(`\n${passed} passed`);
