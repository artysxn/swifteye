// One request per candidate source: reachable? JSON? a bot check (Cloudflare challenge)? No retries, no bypassing.
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36';
const P = [
  ['matchbook sports', 'https://api.matchbook.com/edge/rest/lookups/sports?per-page=50'],
  ['matchbook live events', 'https://api.matchbook.com/edge/rest/events?states=open&in-running-only=true&per-page=5&include-prices=true&price-depth=1'],
  ['betfair site', 'https://www.betfair.com/exchange/plus/'],
  ['betfair ero', 'https://ero.betfair.com/www/sports/exchange/readonly/v1/bymarket?alt=json&marketIds=1.1&types=MARKET_STATE'],
  ['188bet', 'https://www.188bet.com/en-gb/sports'],
  ['sbobet', 'https://www.sbobet.com/euro'],
  ['betway site', 'https://sports.betway.com/en/sports'],
  ['ggbet', 'https://gg.bet/en/esports'],
  ['stake graphql', 'https://stake.com/_api/graphql', '{"query":"{ __typename }"}'],
  ['bookmaker.eu', 'https://www.bookmaker.eu/'],
  ['bet365', 'https://www.bet365.com/'],
  ['circa', 'https://www.circasports.com/'],
  ['pandascore', 'https://api.pandascore.co/matches/running'],
  ['grid', 'https://api-op.grid.gg/central-data/graphql', '{"query":"{ __typename }"}'],
  ['oddin', 'https://oddin.gg/'],
  ['data.bet', 'https://data.bet/'],
  ['abios', 'https://api.abiosgaming.com/v3/series'],
  ['1xbet', 'https://1xbet.com/service-api/LiveFeed/Get1x2_VZip?sports=4&count=5&lng=en&mode=4&getEmpty=true'],
];
for (const [name, url, body] of P) {
  const t0 = Date.now();
  try {
    const r = await fetch(url, { method: body ? 'POST' : 'GET', body, headers: { 'user-agent': UA, 'accept': 'application/json, text/html;q=0.9', ...(body ? { 'content-type': 'application/json' } : {}) }, redirect: 'manual', signal: AbortSignal.timeout(10000) });
    const txt = await r.text();
    const cf = r.headers.get('cf-mitigated') || (/Just a moment|challenge-platform|cf_chl|captcha/i.test(txt) ? 'challenge' : '');
    console.log(`${name.padEnd(22)} ${r.status} ${String(r.headers.get('content-type') || '').slice(0, 24).padEnd(24)} ${String(txt.length).padStart(8)} B ${String(Date.now() - t0).padStart(5)} ms ${cf ? 'BOT CHECK ' + cf : ''} ${r.headers.get('location') ? '-> ' + r.headers.get('location').slice(0, 70) : ''} | ${txt.replace(/\s+/g, ' ').slice(0, 110)}`);
  } catch (e) { console.log(`${name.padEnd(22)} FAIL ${e.message.slice(0, 80)}`); }
}
