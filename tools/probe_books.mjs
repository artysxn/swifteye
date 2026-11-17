// Probe bookmaker sites: does the homepage answer, what protection is in front of it, which
// sportsbook platform it runs on (from markers in the page), and does a known public odds API
// for that platform answer. Never tries to get past a challenge: a challenge is recorded as such.
//
//   node tools/probe_books.mjs [out.json]
import fs from 'node:fs';

const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const SITES = {
  // international crypto
  stake: ['stake.com'], duel: ['duel.com'], gamdom: ['gamdom.com'], bcgame: ['bc.game'],
  // Russia / CIS
  '1win': ['1win.com', '1win.pro'], '1xbet': ['1xbet.com'], mostbet: ['mostbet.com'], paripesa: ['paripesa.bet', 'paripesa.com'],
  betboom: ['betboom.ru', 'betboom.com'], winline: ['winline.ru'], pari: ['pari.ru'], ligastavok: ['ligastavok.ru'],
  betcity: ['betcity.ru'], olimpbet: ['olimpbet.kz', 'olimp.bet'], leon: ['leon.bet', 'leon.ru'], marathon: ['marathonbet.com', 'marathonbet.ru'],
  baltbet: ['baltbet.ru'], zenit: ['zenit.win', 'zenitbet.com'], battery: ['batery.ru', 'battery.bet'], sportbet: ['sportbet.one'],
  '24bet': ['24bet.com'], betm: ['bet-m.com', 'betm.ru'], betandyou: ['betandyou.com'], tennisi: ['tennisi.bet', 'tennisi.kz'],
  ggbet: ['gg.bet'], vbet: ['vbet.com'], betking: ['betking.com', 'betking.com.ua'], cosmolot: ['cosmolot.ua'],
  // Asia / China
  coinsgame: ['coins.game'], dafabet: ['dafabet.com'], '22bet': ['22bet.com'], spobet: ['spobet.com'], cmd368: ['cmd368.com'],
  '12bet': ['12bet.com'], '188bet': ['188bet.com'], w88: ['w88.com'], fun88: ['fun88.com'], bk8: ['bk8.com'], singbet: ['singbet.com'],
  '3et': ['3et.com'], penta88: ['penta88.com'], '198bet': ['198bet.com'], maxbet: ['maxbet.com'], nova88: ['nova88.com'],
  '22bit': ['22bit.com'], '1xbit': ['1xbit.com', '1xbit1.com'], bet25: ['bet25.com'], rainbet: ['rainbet.com'], dexsport: ['dexsport.io'],
};

// Platform fingerprints in page HTML / scripts.
const MARKS = {
  '1xbet-platform': [/service-api\/LiveFeed/i, /\/LineFeed\//i, /xbet/i],
  betconstruct: [/swarm/i, /betconstruct/i, /bc-sportsbook/i],
  betby: [/betby/i, /sptpub/i, /bt-renderer/i],
  kambi: [/kambicdn/i, /kambi/i],
  altenar: [/biahosted/i, /altenar/i],
  digitain: [/digitain/i],
  sbtech: [/sbtech/i],
  saba: [/saba/i, /onebook/i, /ibcbet/i],
  bti: [/bti\b|btisports/i],
  'sportradar-widgets': [/sportradar|betradar/i],
  oddin: [/oddin/i],
  imsports: [/imsports|im-sports|inplaymatrix/i],
  'pragmatic/crown': [/hga0|crown/i],
};

const timed = async (url, opts = {}) => {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), 12000);
  try {
    const r = await fetch(url, { redirect: 'follow', ...opts, signal: ac.signal, headers: { 'user-agent': UA, accept: 'text/html,application/json,*/*', 'accept-language': 'en', ...(opts.headers || {}) } });
    const body = await r.text();
    return { status: r.status, url: r.url, server: r.headers.get('server') || '', cf: !!r.headers.get('cf-ray'), body };
  } catch (e) {
    return { status: 0, err: String(e.cause?.code || e.name || e) };
  } finally {
    clearTimeout(t);
  }
};

const challenge = (r) => r.status === 403 || r.status === 503 || /Just a moment|cf-chl|challenge-platform|captcha|ddos-guard|Access Denied|qrator/i.test(r.body?.slice(0, 4000) || '');

async function probe(name, hosts) {
  const out = { name, tries: [] };
  for (const h of hosts) {
    const home = await timed(`https://${h}/`);
    const html = home.body || '';
    const plat = Object.entries(MARKS).filter(([, rs]) => rs.some((re) => re.test(html))).map(([k]) => k);
    const t = { host: h, status: home.status, final: home.url, server: home.server, cloudflare: home.cf, err: home.err, challenge: home.status ? challenge(home) : null, platforms: plat, size: html.length, title: (html.match(/<title[^>]*>([^<]*)/i) || [])[1]?.trim().slice(0, 80) };
    // 1xBet platform: the same JSON feed on every brand domain.
    const x = await timed(`https://${new URL(home.url || `https://${h}/`).host}/service-api/LiveFeed/Get1x2_VZip?sports=1&count=3&lng=en&mode=4&getEmpty=true`);
    t.xbet_feed = x.status === 200 && /"Success"\s*:\s*true/.test(x.body || '') ? 'OK' : `${x.status}${x.status && challenge(x) ? ' challenge' : ''}`;
    out.tries.push(t);
  }
  return out;
}

const res = [];
const names = Object.keys(SITES);
for (let i = 0; i < names.length; i += 6) {
  res.push(...(await Promise.all(names.slice(i, i + 6).map((n) => probe(n, SITES[n])))));
}
for (const r of res)
  for (const t of r.tries)
    console.log(`${r.name.padEnd(10)} ${t.host.padEnd(18)} ${String(t.status).padStart(3)} ${t.challenge ? 'CHALLENGE' : t.err ? t.err : 'open     '} cf=${t.cloudflare ? 'y' : 'n'} xbet=${t.xbet_feed.padEnd(12)} ${t.platforms.join(',').padEnd(24)} ${t.final || ''} | ${t.title || ''}`);
if (process.argv[2]) fs.writeFileSync(process.argv[2], JSON.stringify(res, null, 1));
