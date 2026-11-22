// The ENTIRE Polymarket history of past match markets, from the moment each market opened to the moment it closed,
// from the public Gamma, CLOB and Data APIs (no account). For studying waves inside games on weeks outside
// Swifteye's own recording (owner, 22 Nov 2026: "you should have the entire price trend on polymarket, at least").
// Output, gzip, one line a resolved two-way match market:
//   {q, tag, cond: conditionId, s: listed start, created, closed (unix s), vol: $ traded, w: winning side (0/1),
//    line: [[unix s, side 0's price]] Polymarket's price line, one point a minute, whole life,
//    trades: [[unix s, side 0's price, shares, 1 = buy / -1 = sell, side traded 0/1, wallet]] every trade, whole life}
// Prices are always side 0's (a trade on side 1 at p is side 0 at 1 - p).
// usage: node tools/game_paths.mjs <out dir> [from YYYY-MM-DD] [to YYYY-MM-DD] [name:tag ...]
import fs from 'node:fs';
import zlib from 'node:zlib';
const [OUT, FROM = '2026-10-05', TO = '2026-11-17', ...PAIRS] = process.argv.slice(2);
const TAGS = PAIRS.length ? PAIRS : ['tennis:864', 'esports:64', 'basketball:28', 'nba:745', 'hockey:100088', 'nhl:899', 'ufc:279'];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function get(url) {
  for (let k = 0; k < 5; k++) {
    try {
      const r = await fetch(url, { signal: AbortSignal.timeout(30000) });
      if (r.status === 429) { await sleep(3000 * (k + 1)); continue; }
      if (!r.ok) { await sleep(1000); continue; }
      return await r.json();
    } catch { await sleep(1000); }
  }
  return null;
}
const ts = (x) => (x ? Math.floor(Date.parse(String(x).replace(' ', 'T').replace(/\+00$/, 'Z')) / 1000) : null);
const r3 = (x) => Math.round(x * 1000) / 1000;
fs.mkdirSync(OUT, { recursive: true });
const seen = new Set();
for (const pair of TAGS) {
  const [name, tag] = pair.split(':');
  const mk = [];
  // Gamma returns at most about 2,000 markets for one query, so list three days at a time.
  const end = Date.parse(TO + 'T00:00:00Z');
  for (let d0 = Date.parse(FROM + 'T00:00:00Z'); d0 < end; d0 += 3 * 86400e3) {
    const d1 = Math.min(d0 + 3 * 86400e3, end);
    for (let off = 0; ; off += 100) {
      const j = await get('https://gamma-api.polymarket.com/markets?tag_id=' + tag + '&closed=true&sports_market_types=moneyline&end_date_min=' + new Date(d0).toISOString().slice(0, 19)
        + 'Z&end_date_max=' + new Date(d1).toISOString().slice(0, 19) + 'Z&volume_num_min=300&limit=100&offset=' + off + '&order=endDate&ascending=true');
      if (!Array.isArray(j) || !j.length) break;
      for (const m of j) {
        let outs, toks, px;
        try { outs = JSON.parse(m.outcomes); toks = JSON.parse(m.clobTokenIds); px = JSON.parse(m.outcomePrices).map(Number); } catch { continue; }
        if (outs.length !== 2 || !(px[0] === 1 || px[1] === 1) || !m.gameStartTime || seen.has(toks[0])) continue;
        seen.add(toks[0]);
        mk.push({ q: m.question, tag: name, cond: m.conditionId, toks, w: px[0] === 1 ? 0 : 1, s: ts(m.gameStartTime), created: ts(m.createdAt), closed: ts(m.closedTime), vol: Math.round(m.volumeNum || 0) });
      }
      await sleep(1000);
    }
  }
  console.log(name + ': ' + mk.length + ' markets listed, fetching their histories');
  const gz = zlib.createGzip(); const file = fs.createWriteStream(OUT + '/paths_' + name + '.ndjson.gz'); gz.pipe(file);
  let n = 0, got = 0, ntr = 0;
  const one = async (m) => {
    const a = m.created || m.s - 7 * 86400, b = m.closed || m.s + 6 * 3600;
    const h = await get('https://clob.polymarket.com/prices-history?market=' + m.toks[0] + '&startTs=' + a + '&endTs=' + b + '&fidelity=1');
    const line = (h?.history || []).map((x) => [x.t, r3(x.p)]);
    const trades = []; let cur = '', complete = false;
    for (let pg = 0; pg < 400; pg++) {
      const r = await get('https://data-api.polymarket.com/v2/trades?conditionId=' + m.cond + '&limit=500' + (cur ? '&cursor=' + cur : ''));
      if (!r || !Array.isArray(r.data)) break;
      for (const x of r.data) {
        const t = x.timestamp > 1e11 ? Math.floor(x.timestamp / 1000) : x.timestamp, k = x.token_id === m.toks[0] ? 0 : 1;
        trades.push([t, r3(k === 0 ? x.price : 1 - x.price), Math.round(x.size * 100) / 100, x.side === 'BUY' ? 1 : -1, k, x.proxy_wallet]);
      }
      const p = r.pagination || {};
      if (!p.has_more || !p.next_cursor) { complete = true; break; }
      cur = p.next_cursor;
    }
    trades.sort((x, y) => x[0] - y[0]);
    if (line.length >= 10 || trades.length >= 10) {
      const { toks, ...rest } = m;
      gz.write(JSON.stringify({ ...rest, tok0: toks[0], complete, line, trades }) + '\n'); got++; ntr += trades.length;
    }
  };
  for (let i = 0; i < mk.length; i += 3) {             // three markets at a time, paced
    await Promise.all(mk.slice(i, i + 3).map(one));
    n += Math.min(3, mk.length - i);
    if (n % 300 < 3) console.log('  ' + name + ' ' + n + '/' + mk.length + ', ' + got + ' saved, ' + ntr + ' trades');
    await sleep(200);
  }
  await new Promise((r) => { file.on('finish', r); gz.end(); });
  console.log(name + ' done: ' + got + ' markets saved, ' + ntr + ' trades');
}
console.log('ALL DONE');
