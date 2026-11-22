// Records the order-book top of Polymarket's SISTER markets (map winner, totals, map handicap) and the match
// winner beside them, for the esports games the bot is following. A separate small process: it only READS the
// bot's database (which games, which Polymarket event) and talks to Polymarket's public feeds. It places nothing.
//
// Why (21 Nov 2026): the snipe works where a price is slow to follow news. The match-winner market follows within
// seconds; the thinner sister markets may keep old orders longer. The main recording holds match-winner prices only.
//
// usage:  node tools/sister_recorder.mjs [sports, default cs2,dota2,valorant,lol] [hours to run, default 48]
// output: ~/swifteye/research/sister/<UTC date>.jsonl   one line per change of a market's best bid / ask:
//           {t: our clock ms, s: Polymarket's stamp ms, a: market key, b: bid, bs: size at the bid, k: ask, ks: size at the ask}
//           and per trade print: {t, s, a, tp: price, tz: size, td: side}
//         ~/swifteye/research/sister/meta.json          market key -> game, kind of market, outcome it prices
//         ~/swifteye/research/sister/recorder.log       what it follows and how much it downloads
// It watches the bot's own Polymarket feed and pauses itself for 10 minutes whenever that feed runs late.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';

const HOME = process.env.HOME;
const DB = `${HOME}/swifteye/oddsfeed.db`;
const OUT = `${HOME}/swifteye/research/sister`;
const SPORTS = (process.argv[2] || 'cs2,dota2,valorant,lol').split(',');
const STOP_AT = Date.now() + (+process.argv[3] || 48) * 3600e3;
const KINDS = new Set(['moneyline', 'child_moneyline', 'totals', 'map_handicap']);
const WS_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market';
const GAMMA = 'https://gamma-api.polymarket.com';
fs.mkdirSync(OUT, { recursive: true });

const log = (m) => fs.appendFileSync(`${OUT}/recorder.log`, `${new Date().toISOString()} ${m}\n`);
let meta = {};
try { meta = JSON.parse(fs.readFileSync(`${OUT}/meta.json`, 'utf8')); } catch { /* first run */ }
let nextKey = Object.keys(meta).length;
const keyOf = {};                       // token id -> market key
for (const [k, v] of Object.entries(meta)) keyOf[v.token] = k;

let out = null, outDay = '';
function write(row) {
  const day = new Date().toISOString().slice(0, 10);
  if (day !== outDay) { out?.end(); out = fs.createWriteStream(`${OUT}/${day}.jsonl`, { flags: 'a' }); outDay = day; }
  out.write(JSON.stringify(row) + '\n');
}

// ---------------------------------------------------------------- which markets
function games() {
  const sql = `SELECT m.id AS id, m.sport AS sport, m.start_ms AS start, b.book_event_id AS ev, b.home AS home, b.away AS away
    FROM matches m JOIN book_events b ON b.match_id = m.id AND b.book = 'polymarket'
    WHERE m.sport IN (${SPORTS.map((s) => `'${s.replace(/[^a-z0-9]/g, '')}'`).join(',')})
      AND m.start_ms BETWEEN ${Date.now() - 6 * 3600e3} AND ${Date.now() + 90 * 60e3}`;
  try { return JSON.parse(execFileSync('sqlite3', ['-readonly', '-json', DB, sql], { encoding: 'utf8', timeout: 20000 }) || '[]'); } catch (e) { log(`games: ${e.message.slice(0, 120)}`); return null; }
}
let want = new Set();
async function discover() {
  const gs = games();
  if (!gs) return;
  const now = new Set();
  for (const g of gs) {
    let ev;
    try { ev = await (await fetch(`${GAMMA}/events/${g.ev}`, { signal: AbortSignal.timeout(15000) })).json(); } catch { continue; }
    for (const m of ev.markets || []) {
      if (m.closed || !KINDS.has(m.sportsMarketType)) continue;
      let toks, outs;
      try { toks = JSON.parse(m.clobTokenIds); outs = JSON.parse(m.outcomes); } catch { continue; }
      const tok = toks[0];              // the other token is this one's mirror (bid of one + ask of the other = 1)
      if (!keyOf[tok]) {
        const k = 'm' + nextKey++;
        keyOf[tok] = k;
        meta[k] = { token: tok, match_id: g.id, sport: g.sport, event: g.ev, home: g.home, away: g.away, kind: m.sportsMarketType, question: m.question, outcome: outs[0], other: outs[1], line: m.line ?? null, start: g.start };
      }
      now.add(tok);
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  fs.writeFileSync(`${OUT}/meta.json`, JSON.stringify(meta));
  const changed = now.size !== want.size || [...now].some((t) => !want.has(t));
  if (changed) { want = now; log(`following ${gs.length} games, ${want.size} markets`); if (Date.now() > pausedUntil) connect(); }
}

// ---------------------------------------------------------------- the feed
let ws = null, pinger = null, books = {}, last = {}, bytes = 0, msgs = 0, rows = 0;
function emit(tok, stamp) {
  const bk = books[tok], k = keyOf[tok];
  if (!bk || !k) return;
  let b = null, bs = 0, a = null, as = 0;
  for (const [p, s] of bk.bids) if (s > 0 && (b === null || p > b)) { b = p; bs = s; }
  for (const [p, s] of bk.asks) if (s > 0 && (a === null || p < a)) { a = p; as = s; }
  const sig = `${b}|${bs}|${a}|${as}`;
  if (last[tok] === sig) return;
  last[tok] = sig; rows++;
  write({ t: Date.now(), s: +stamp || null, a: k, b, bs, k: a, ks: as });
}
function onEvent(e) {
  const stamp = e.timestamp;
  if (e.event_type === 'book' && want.has(e.asset_id)) {
    books[e.asset_id] = { bids: new Map((e.bids || []).map((x) => [+x.price, +x.size])), asks: new Map((e.asks || []).map((x) => [+x.price, +x.size])) };
    emit(e.asset_id, stamp);
  } else if (e.event_type === 'price_change') {
    const touched = new Set();
    for (const c of e.price_changes || e.changes || []) {
      const tok = c.asset_id || e.asset_id, bk = books[tok];
      if (!bk || !want.has(tok)) continue;
      const side = c.side === 'BUY' ? bk.bids : bk.asks, p = +c.price, s = +c.size;
      if (s > 0) side.set(p, s); else side.delete(p);
      touched.add(tok);
    }
    for (const tok of touched) emit(tok, stamp);
  } else if (e.event_type === 'last_trade_price' && want.has(e.asset_id)) {
    write({ t: Date.now(), s: +stamp || null, a: keyOf[e.asset_id], tp: +e.price, tz: +e.size, td: e.side });
  }
}
function connect() {
  try { ws?.close(); } catch { /* already closed */ }
  ws = null;                             // so the closed socket's handler does not reconnect by itself
  clearInterval(pinger);
  if (!want.size) return;
  books = {}; last = {};
  const mine = (ws = new WebSocket(WS_URL));
  mine.onopen = () => {
    mine.send(JSON.stringify({ assets_ids: [...want], type: 'market', custom_feature_enabled: true }));
    pinger = setInterval(() => { if (mine.readyState === 1) mine.send('PING'); }, 10000);
  };
  mine.onmessage = (m) => {
    const txt = typeof m.data === 'string' ? m.data : '';
    bytes += txt.length; msgs++;
    if (!txt || txt === 'PONG') return;
    let d;
    try { d = JSON.parse(txt); } catch { return; }
    for (const e of Array.isArray(d) ? d : [d]) onEvent(e);
  };
  mine.onclose = () => { if (ws === mine) setTimeout(() => { if (ws === mine) connect(); }, 3000); };
  mine.onerror = () => { try { mine.close(); } catch { /* ignore */ } };
}

setInterval(() => {
  log(`5 min: ${msgs} messages, ${rows} rows, ${(bytes * 8 / 300 / 1e6).toFixed(2)} Mbit/s, ${want.size} markets`);
  bytes = 0; msgs = 0; rows = 0;
  if (Date.now() > STOP_AT) { log('time limit reached: stopping'); process.exit(0); }
}, 300000);
setInterval(discover, 300000);

// Safety valve: the bot's own Polymarket feed comes first. If it runs late (the link is full), stop downloading
// for 10 minutes and try again.
let late = 0, pausedUntil = 0;
setInterval(async () => {
  let lag = 0;
  try { lag = (await (await fetch('http://127.0.0.1:8787/bots', { signal: AbortSignal.timeout(8000) })).json()).snipe?.status?.pm_lag_ms ?? 0; } catch { return; }
  late = lag > 500 ? late + 1 : 0;
  if (late >= 2 && Date.now() > pausedUntil) {
    pausedUntil = Date.now() + 600000; late = 0;
    log(`the bot's feed is ${lag} ms late: pausing for 10 min`);
    const keep = want; want = new Set(); connect(); want = keep;       // closes the socket, keeps the list
    setTimeout(() => { log('resuming'); connect(); }, 600000);
  }
}, 30000);
log(`started: ${SPORTS.join(',')}`);
discover();
