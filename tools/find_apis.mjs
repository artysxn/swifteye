// For each site: load its page and the scripts it links (same-origin + CDN), and list the API /
// WebSocket hosts and paths they reference. This is how each sportsbook's own odds feed was
// found (what the site's own live page loads). No challenge is ever solved: blocked pages are skipped.
//
//   node tools/find_apis.mjs host[/path] [host...]
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';
const get = async (u) => {
  const ac = new AbortController();
  const t = setTimeout(() => ac.abort(), 15000);
  try {
    const r = await fetch(u, { signal: ac.signal, redirect: 'follow', headers: { 'user-agent': UA, 'accept-language': 'en' } });
    return { status: r.status, url: r.url, body: await r.text() };
  } catch (e) {
    return { status: 0, body: '', err: String(e.cause?.code || e.name) };
  } finally {
    clearTimeout(t);
  }
};
const API_RE = /(wss?:\/\/[a-z0-9.\-]+[a-z0-9\/._\-]*|https?:\/\/[a-z0-9.\-]*(?:api|feed|line|odds|sport|swarm|ws|socket|bet|offering|data)[a-z0-9.\-]*\.[a-z]{2,}[a-z0-9\/._\-]*|["'`]\/(?:api|service-api|sportsbook|betline|line|live|feed|odds|ws)[a-z0-9\/._\-]{2,80})/gi;
for (const arg of process.argv.slice(2)) {
  const page = await get(arg.startsWith('http') ? arg : `https://${arg}`);
  console.log(`\n### ${arg} -> ${page.status} ${page.url || page.err || ''} (${page.body.length} B)`);
  if (!page.status || page.status >= 400) continue;
  const base = new URL(page.url);
  const scripts = [...page.body.matchAll(/<script[^>]+src=["']([^"']+)["']/gi)].map((m) => new URL(m[1], base).href).slice(0, 25);
  const found = new Map();
  const scan = (txt, from) => {
    for (const m of txt.matchAll(API_RE)) {
      const v = m[0].replace(/^["'`]/, '');
      if (/\.(png|svg|jpg|webp|css|woff2?|gif|ico)(\?|$)/i.test(v) || /google|facebook|yandex|gtm|analytics|sentry|hotjar|cloudflareinsights|schema\.org|w3\.org|apple\.com|intercom|livechat|onesignal/i.test(v)) continue;
      if (!found.has(v)) found.set(v, from);
    }
  };
  scan(page.body, 'html');
  for (const s of scripts) {
    const r = await get(s);
    if (r.status === 200) scan(r.body, s.split('/').pop().slice(0, 40));
  }
  console.log(`  scripts: ${scripts.length}`);
  for (const [v, from] of [...found].slice(0, 60)) console.log(`  ${v.slice(0, 140)}   <- ${from}`);
}
