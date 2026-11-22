// Every Polymarket game event (Gamma tag 100639 "Games") starting 17 Nov 04:00Z - 22 Nov 00:00Z, open and closed;
// compact rows -> ./pm_all_events.json (run from ~/swifteye/research/pm_all). Paced 300 ms a page.
import fs from 'fs';
const G = 'https://gamma-api.polymarket.com/events';
const out = new Map(); const sleep = ms => new Promise(r => setTimeout(r, ms));
for (let t = Date.UTC(2026, 10, 17, 4); t < Date.UTC(2026, 10, 22, 0); t += 6 * 3600e3) {
  const a = new Date(t).toISOString(), b = new Date(Math.min(t + 6 * 3600e3, Date.UTC(2026, 10, 22, 0))).toISOString();
  for (const closed of ['true', 'false']) for (let off = 0; off < 3000; off += 100) {
    const u = `${G}?tag_id=100639&limit=100&offset=${off}&closed=${closed}&start_time_min=${a}&start_time_max=${b}`;
    let j; try { j = await (await fetch(u)).json(); } catch (e) { console.log('fail', u, e.message); await sleep(2000); continue; }
    for (const e of j) out.set(e.id, { id: e.id, title: e.title, start: e.startTime, series: e.seriesSlug, sport: e.sport, tags: (e.tags || []).map(x => x.id + ':' + x.slug),
      ml: (e.markets || []).some(m => m.sportsMarketType === 'moneyline'), vol: +e.volume || 0, liq: +e.liquidity || 0 });
    await sleep(300);
    if (j.length < 100) break;
  }
  console.log(a, out.size);
}
fs.writeFileSync('pm_all_events.json', JSON.stringify([...out.values()]));
console.log('total', out.size);
