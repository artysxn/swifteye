// Pre-match price and result of past Polymarket match markets, from the public Gamma and CLOB APIs (no account).
// For checking whether favourites are underpriced on a period OUTSIDE Swifteye's own recording.
//   price = Polymarket's own price history for side 0 at the last point between 6 h and 10 min before the listed start
// usage: node tools/prematch_history.mjs <tag id> <from YYYY-MM-DD> <to YYYY-MM-DD> <out.ndjson> [min volume $, default 300] [max markets, default 4000]
// Paced, and it waits while the bot's own Polymarket feed runs late.
import fs from 'node:fs';
const [TAG, FROM, TO, OUT, MINVOL = '300', MAX = '4000'] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function get(url) {
  for (let k = 0; k < 5; k++) {
    try {
      const r = await fetch(url, { signal: AbortSignal.timeout(20000) });
      if (r.status === 429) { await sleep(3000); continue; }
      if (!r.ok) return null;
      return await r.json();
    } catch { await sleep(1000); }
  }
  return null;
}
async function feedOk() {
  try {
    const d = await (await fetch('http://127.0.0.1:8787/bots', { signal: AbortSignal.timeout(8000) })).json();
    return (d.snipe?.status?.pm_lag_ms ?? 0) < 800;
  } catch { return true; }
}
const mk = [];
for (let off = 0; mk.length < +MAX; off += 100) {
  const url = 'https://gamma-api.polymarket.com/markets?tag_id=' + TAG + '&closed=true&sports_market_types=moneyline&end_date_min=' + FROM + 'T00:00:00Z&end_date_max=' + TO
    + 'T00:00:00Z&volume_num_min=' + MINVOL + '&limit=100&offset=' + off + '&order=endDate&ascending=true';
  const j = await get(url);
  if (!Array.isArray(j) || !j.length) break;
  for (const m of j) {
    let outs, toks, px;
    try { outs = JSON.parse(m.outcomes); toks = JSON.parse(m.clobTokenIds); px = JSON.parse(m.outcomePrices).map(Number); } catch { continue; }
    if (outs.length !== 2 || !(px[0] === 1 || px[1] === 1) || !m.gameStartTime) continue;
    mk.push({ q: m.question, tok: toks[0], w: px[0] === 1 ? 0 : 1, s: Math.floor(Date.parse(m.gameStartTime.replace(' ', 'T').replace(/\+00$/, 'Z')) / 1000), vol: Math.round(m.volumeNum || 0) });
  }
  if (off % 500 === 0) console.log('  listed ' + mk.length + ' markets (offset ' + off + ')');
  await sleep(1500);
  while (!(await feedOk())) { console.log('  bot feed late: waiting 30 s'); await sleep(30000); }
}
console.log(mk.length + ' resolved two-way match markets with $' + MINVOL + '+ traded');
const out = fs.createWriteStream(OUT);
let n = 0, got = 0;
for (const m of mk) {
  const h = await get('https://clob.polymarket.com/prices-history?market=' + m.tok + '&startTs=' + (m.s - 6 * 3600) + '&endTs=' + (m.s - 600) + '&fidelity=10');
  const pts = h?.history || [];
  if (pts.length) {
    const last = pts[pts.length - 1];
    out.write(JSON.stringify({ q: m.q, s: m.s, vol: m.vol, w: m.w, p: last.p, age: m.s - last.t, n: pts.length }) + '\n');
    got++;
  }
  if (++n % 250 === 0) {
    console.log('  ' + n + '/' + mk.length + ' price histories, ' + got + ' with a pre-match price');
    while (!(await feedOk())) await sleep(30000);
  }
  await sleep(180);
}
out.end();
console.log('saved ' + got + ' markets to ' + OUT);
