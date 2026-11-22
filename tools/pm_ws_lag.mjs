// As pm_ws_lag.mjs, but on the sports games the collector follows (football tag 100350, e-sports tag 64,
// started in the last 3 hours = in play now), and split by message type. Public data only.
const secs = +(process.argv[2] || 40);
const now = Date.now(), iso = ms => new Date(ms).toISOString().slice(0, 19) + 'Z';
const ids = [];
for (const [tag, pages] of [['100350', 3], ['64', 1]]) for (let p = 0; p < pages; p++) {
  const u = `https://gamma-api.polymarket.com/events?tag_id=${tag}&closed=false&limit=100&offset=${p * 100}&start_time_min=${iso(now - 3 * 3600e3)}&start_time_max=${iso(now)}`;
  const v = await (await fetch(u)).json(); if (!Array.isArray(v) || !v.length) break;
  for (const e of v) for (const m of e.markets || []) { if (m.sportsMarketType && m.sportsMarketType !== 'moneyline') continue; try { ids.push(...JSON.parse(m.clobTokenIds || '[]')); } catch {} }
}
const ws = new WebSocket('wss://ws-subscriptions-clob.polymarket.com/ws/market');
const lag = {}; let msgs = 0; const t0 = Date.now();
ws.onopen = () => { ws.send(JSON.stringify({ assets_ids: ids, type: 'market', custom_feature_enabled: true })); setInterval(() => ws.send('PING'), 10000); };
ws.onmessage = (e) => {
  const t = Date.now(); if (e.data === 'PONG') return; let v; try { v = JSON.parse(e.data); } catch { return; }
  msgs++; if (t - t0 < 4000) return;
  for (const it of Array.isArray(v) ? v : [v]) { const ts = +it.timestamp; if (ts > 0) (lag[it.event_type || '?'] ||= []).push(t - ts); }
};
ws.onclose = () => console.log('closed by server after', ((Date.now() - t0) / 1000).toFixed(0), 's');
setTimeout(() => {
  console.log(`${ids.length} tokens of games in play, ${msgs} messages in ${secs}s (${(msgs / secs).toFixed(0)}/s)`);
  const all = [];
  for (const [k, a] of Object.entries(lag)) { all.push(...a); a.sort((x, y) => x - y); const n = a.length, q = p => a[Math.min(n - 1, Math.floor(n * p))];
    console.log(`  ${k.padEnd(18)} ${String(n).padStart(6)}  median ${q(.5)} ms, p90 ${q(.9)} ms, max ${q(1)} ms, over 1 s ${(100 * a.filter(x => x > 1000).length / n).toFixed(1)}%`); }
  all.sort((x, y) => x - y); const n = all.length, q = p => all[Math.min(n - 1, Math.floor(n * p))];
  console.log(`  ALL: ${n} timed, median ${q(.5)} ms, p90 ${q(.9)} ms, p99 ${q(.99)} ms, over 1 s ${(100 * all.filter(x => x > 1000).length / Math.max(1, n)).toFixed(1)}%`);
  process.exit(0);
}, secs * 1000);
