// Swifteye pages, part 4: Telegram, Arbitrage Scout, match bells, prediction-market size filters.
'use strict';
(() => {
const { $, $$, esc, ic, api, P } = SE;

SE.tgLogo = (size = 20, cls = '') => `<svg class="tg-logo ${cls}" width="${size}" height="${size}" viewBox="0 0 240 240" aria-hidden="true"><defs><linearGradient id="tgG" x1=".667" x2=".417" y1=".167" y2=".75"><stop offset="0" stop-color="#37aee2"/><stop offset="1" stop-color="#1e96c8"/></linearGradient></defs><circle cx="120" cy="120" r="120" fill="url(#tgG)"/><path fill="#c8daea" d="M98 175c-3.9 0-3.2-1.5-4.6-5.2L82 132.2 170 80"/><path fill="#a9c9dd" d="M98 175c3 0 4.3-1.4 6-3l16-15.6-20-12"/><path fill="#fff" d="M100 144.4l48.4 35.7c5.5 3 9.5 1.5 10.9-5.1l19.7-92.8c2-8.1-3.1-11.7-8.4-9.3L55.2 117.5c-7.9 3.2-7.8 7.6-1.4 9.5l29.7 9.3 68.7-43.3c3.2-2 6.2-.9 3.8 1.3"/></svg>`;

// ================================================================ bells (Telegram watch list)
SE.watch = new Set();
const paintBells = () => $$('[data-watch]').forEach((b) => {
  const on = SE.watch.has(+b.dataset.watch);
  b.classList.toggle('on', on);
  b.innerHTML = ic(on ? 'notifications_active' : 'notifications');
});
SE.bell = (id) => `<a href="#" class="bell" data-watch="${+id}" data-tip="Telegram alerts for odds moves on this match. Settings on the Telegram page.">${ic('notifications')}</a>`;
SE.loadWatch = async () => { try { SE.watch = new Set((await api('/telegram/watch')).watch); } catch {} paintBells(); };
SE.paintBells = paintBells;
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-watch]');
  if (!b) return;
  e.preventDefault();
  e.stopPropagation();
  const id = +b.dataset.watch, on = !SE.watch.has(id);
  try {
    const r = await api('/telegram/watch/' + id, { body: { on } });
    SE.watch = new Set(r.watch);
    paintBells();
    const tg = await api('/telegram').catch(() => null);
    SE.toast(on ? (tg?.linked ? 'Telegram alerts on for this match' : 'Watching. Link Telegram on the Telegram page to get alerts.') : 'Alerts off for this match');
  } catch (err) { SE.toast(err.message); }
}, true);
window.addEventListener('DOMContentLoaded', SE.loadWatch);

// ================================================================ prediction-market size ("tier") filter
const TIERS = {
  all: { liq: 0, vol: 0, moves: 0, price: 0 },
  // From the spread seen on 17 Nov 2026: Polymarket liquidity median ~$12k, top quarter ~$90k+.
  high: { liq: 50000, vol: 5000, moves: 20, price: 0 },
  mid: { liq: 5000, vol: 500, moves: 1, price: 0 },
};
const money = (x) => (x == null ? '-' : x >= 1e6 ? '$' + (x / 1e6).toFixed(1) + 'M' : x >= 1e3 ? '$' + (x / 1e3).toFixed(x >= 1e4 ? 0 : 1) + 'k' : '$' + Math.round(x));
SE.tier = {
  meta: {}, at: 0,
  get() { return { ...TIERS.all, ...(SE.pref('tierF') || {}) }; },
  set(f) { SE.setPref('tierF', f); },
  active() { const f = this.get(); return f.liq > 0 || f.vol > 0 || f.moves > 0 || f.price > 0; },
  async ensure() {
    if (Date.now() - this.at < 20000) return;
    try { this.meta = (await api('/pred/meta')).matches || {}; this.at = Date.now(); } catch {}
  },
  /** A match passes when Polymarket or Kalshi lists it and that listing meets every minimum. */
  pass(id) {
    if (!this.active()) return true;
    const f = this.get(), m = this.meta[id];
    if (!m) return false;
    return Object.values(m).some((x) => x.liquidity >= f.liq && x.volume_24h >= f.vol && x.moves_1h >= f.moves && (x.min_price ?? 0) * 100 >= f.price);
  },
  cell(id) {
    const m = this.meta[id];
    if (!m) return '<span class="faint">-</span>';
    return Object.entries(m).map(([b, x]) => `<span class="sz" data-tip="${esc(`${SE.bookName(b)}: liquidity ${money(x.liquidity)}, volume ${money(x.volume)} (24 h ${money(x.volume_24h)}), ${x.moves_1h} price moves in the last hour, cheapest outcome ${x.min_price == null ? '-' : (x.min_price * 100).toFixed(0) + 'c'}`)}">${SE.logo(b)}${money(x.volume_24h || x.volume)}</span>`).join(' ');
  },
  /** Filter bar: tier presets plus the four minimums. */
  bar(el, onChange) {
    const f = this.get();
    const preset = Object.entries(TIERS).find(([, t]) => ['liq', 'vol', 'moves', 'price'].every((k) => t[k] === f[k]))?.[0] || 'custom';
    el.innerHTML = `<div class="tierbar"><span class="muted" data-tip="Rough match tier from the Polymarket / Kalshi listing: big, busy markets are usually the top games. A match passes when either market meets every minimum.">Market size</span><div id="tSeg"></div>
      <label data-tip="Money resting in the order book (Polymarket liquidity, Kalshi liquidity in dollars).">Liquidity ≥ ${SE.num({ attr: 'data-k="liq"', value: f.liq, step: 1000, min: 0, pre: '$', w: 112 })}</label>
      <label data-tip="Traded in the last 24 hours, in dollars (Kalshi contracts valued at their last price).">24 h volume ≥ ${SE.num({ attr: 'data-k="vol"', value: f.vol, step: 1000, min: 0, pre: '$', w: 112 })}</label>
      <label data-tip="Price changes Swifteye recorded on the market in the last hour: how alive it is.">Moves / hour ≥ ${SE.num({ attr: 'data-k="moves"', value: f.moves, step: 1, min: 0, w: 76 })}</label>
      <label data-tip="Price of the least likely outcome, in cents. Drops one-sided games whose market barely trades.">Cheapest outcome ≥ ${SE.num({ attr: 'data-k="price"', value: f.price, step: 1, min: 0, max: 50, suf: '¢', w: 76 })}</label></div>`;
    const apply = (nf) => { this.set(nf); onChange(); };
    SE.seg($('#tSeg', el), [['all', 'All'], ['high', 'High tier', 'Liquidity ≥ $50k, 24 h volume ≥ $5k, 20+ price moves an hour.'], ['mid', 'Mid and up', 'Liquidity ≥ $5k, 24 h volume ≥ $500, at least one price move in the last hour.'], ...(preset === 'custom' ? [['custom', 'Custom']] : [])], preset, (v) => {
      if (!TIERS[v]) return;
      $$('input[data-k]', el).forEach((i) => { i.value = TIERS[v][i.dataset.k]; });
      apply({ ...TIERS[v] });
    });
    $$('input[data-k]', el).forEach((i) => i.addEventListener('input', SE.debounce(() => {
      const nf = { ...this.get(), [i.dataset.k]: Math.max(0, +i.value || 0) };
      $$('#tSeg button', el).forEach((b) => b.classList.remove('on'));
      apply(nf);
    }, 300)));
  },
};

// ================================================================ Telegram
SE.pages.telegram = {
  title: 'Telegram',
  render(el) {
    el.innerHTML = P.head(`<span style="display:inline-flex;align-items:center;gap:12px">${SE.tgLogo(30)}Telegram</span>`, '<span id="tgActs"></span>') +
      P.stats(
        P.stat('tgS1', 'power_settings_new', 'Status'),
        P.stat('tgS2', 'person', 'Chat'),
        P.stat('tgS3', 'send', 'Sent', 'Messages since the last restart.'),
        P.stat('tgS4', 'schedule', 'Last message'),
        P.stat('tgS5', 'notifications_active', 'Watching', 'Matches with the bell on.')) +
      `<div class="grid g-main" style="margin-top:40px"><div>` +
        P.sec('notifications', 'Messages', '<div id="tgList"></div>') +
        P.sec('forum', 'Ask the bot', '<div class="tp-ask" id="tgAsk"></div><div id="tgPrev"></div>', '', 'The answers its chat commands give. Nothing is sent unless you press Send.') +
        P.sec('sports_esports', 'Odds moves by sport', '<div id="tgGames"></div>', `<button class="btn sm ghost" id="tgGamesT"></button>`, 'Each sport follows the Odds moves settings unless you give it its own. A sport switched off is silent, watched matches included.') +
        P.sec('link', 'Connection', '<div id="tgConn"></div>') +
      `</div><div>` +
        P.sec('history', 'Sent', '<div class="feed" id="tgFeed"></div>') +
        P.sec('notifications_active', 'Watched matches', '<div id="tgWatch"></div>') +
      `</div></div>`;
    let s = null, lastSig = '', gamesOpen = false;
    const open = new Set(SE.pref('tgOpen') || []);
    const save = async (edit, msg = 'Saved') => {
      try { s = await api('/telegram', { body: edit }); lastSig = ''; draw(); if (msg) SE.toast(msg); } catch (e) { SE.toast(e.message, 6000); }
    };
    const sw = (id, on, tip = '') => `<label class="switch" ${tip ? `data-tip="${esc(tip)}"` : ''}><input type="checkbox" id="${id}" ${on ? 'checked' : ''}><span></span></label>`;
    const MARKETS = [['1x2', '1X2'], ['total', 'Total'], ['handicap', 'Handicap'], ['btts', 'Both score']];
    const openGames = new Set();
    const gdef = { enabled: true, min_move_pts: null, markets: null, sources: null, cooldown_s: null, arb_enabled: true, arb_min_profit_pct: null };
    const gameOf = (k) => ({ ...gdef, ...(s.odds.games?.[k] || {}) });
    const custom = (g) => g.min_move_pts != null || g.markets || g.sources || g.cooldown_s != null || g.arb_min_profit_pct != null || !g.arb_enabled;

    // ---------------------------------------------------------------- numbers + actions
    function drawTop() {
      const linked = s.linked, on = s.enabled, last = s.log[0];
      const lamp = !linked ? 'bad' : on ? 'ok pulse' : 'warn';
      P.set('tgS1', `<span class="lamp ${lamp}"></span>${!linked ? 'Not linked' : on ? 'Sending' : 'Paused'}`, s.bot?.username ? `@${esc(s.bot.username)}` : s.bot?.error ? `<span class="down">${esc(s.bot.error)}</span>` : 'no bot yet');
      P.set('tgS2', linked ? `<span style="font-size:22px">@${esc(s.linked_as || '')}</span>` : '<span class="faint">-</span>', linked ? 'linked' : 'press Start in the bot');
      P.set('tgS3', String(s.log.length), s.log.filter((m) => !m.ok).length ? `<span class="down">${s.log.filter((m) => !m.ok).length} failed</span>` : 'all delivered');
      P.set('tgS4', last ? `<span style="font-size:22px">${esc(SE.fmtAgo(last.ts_ms))}</span>` : '<span class="faint">-</span>', last ? esc(last.kind) : 'nothing yet');
      P.set('tgS5', String(s.watch.length), s.watch.length === 1 ? 'match' : 'matches');
      const acts = linked
        ? `<button class="btn" id="tgTest">${ic('send')}Test</button>` + (on ? `<button class="btn danger" id="tgPause">${ic('pause')}Pause</button>` : `<button class="btn primary" id="tgResume">${ic('play_arrow')}Resume</button>`)
        : (s.token_set ? `<a class="btn primary" href="${esc(s.link_url)}" target="_blank" rel="noopener">${SE.tgLogo(16)}Open in Telegram</a>` : '');
      const host = $('#tgActs');
      if (host.dataset.h !== acts) {
        host.dataset.h = acts;
        host.style.display = 'inline-flex'; host.style.gap = '8px';
        host.innerHTML = acts;
        $('#tgTest')?.addEventListener('click', async () => { try { await api('/telegram/test', { method: 'POST' }); SE.toast('Test sent'); load(); } catch (e) { SE.toast(e.message, 6000); } });
        $('#tgPause')?.addEventListener('click', () => save({ enabled: false }, 'Paused'));
        $('#tgResume')?.addEventListener('click', () => save({ enabled: true }, 'Resumed'));
      }
    }

    // ---------------------------------------------------------------- messages
    function drawList() {
      const o = s.odds, a = s.arbs, b = s.bot_alerts || {}, pa = s.paper, su = s.suspicious || {};
      const yes = (v) => (v ? 'on' : 'off');
      const opt = (label, ctl, tip = '') => `<div class="opt"><span ${tip ? `data-tip="${esc(tip)}"` : ''}>${label}</span>${ctl}</div>`;
      const item = (key, icon, name, on, sub, opts) =>
        `<div class="li"><span class="tile ${on ? 'on' : ''}">${ic(icon)}</span><div class="main"><div class="nm">${name}</div><div class="sb">${sub}</div></div>` +
        `<div class="end"><button class="more-b" data-open="${key}" data-tip="Options">${ic(open.has(key) ? 'expand_less' : 'tune')}</button>${sw('t-' + key, on)}</div></div>` +
        (open.has(key) ? `<div class="li-opts">${opts}</div>` : '');
      $('#tgList').innerHTML =
        item('bot', 'smart_toy', 'Snipe bot trades', b.enabled !== false, `buys ${yes(b.buys !== false)} · sales ${yes(b.sells !== false)} · charts ${yes(b.charts !== false)} · run updates ${yes(b.run_updates !== false)}`,
          opt('Buys', sw('bBuys', b.buys !== false)) + opt('Sales', sw('bSells', b.sells !== false)) + opt('Charts', sw('bCharts', b.charts !== false), 'The trade\'s price chart and the balance chart with each sale.') + opt('Run updates', sw('bRun', b.run_updates !== false), 'During a real run: its start and end, an update every hour, warnings on the way to the loss stop.')) +
        item('odds', 'swap_vert', 'Odds moves', o.enabled, `${o.watched_only ? 'watched matches' : 'every match'} · ${o.min_move_pts}+ points`,
          opt('Matches', '<div id="tgWo"></div>') +
          opt('Smallest move', SE.num({ id: 'tgMin', value: o.min_move_pts, step: 0.5, min: 0.5, suf: 'pts', w: 96 }), 'Change in implied probability, in percentage points.') +
          opt('Markets', `<span class="checks">${MARKETS.map(([k, l]) => `<label><input type="checkbox" data-mk="${k}" ${o.markets.includes(k) ? 'checked' : ''}>${l}</label>`).join('')}</span>`) +
          opt('Sources', '<div id="tgSrc"></div>') +
          opt('Repeat after', '<div id="tgCd"></div>', 'The same price on the same source does not alert again within this time.')) +
        item('arb', 'currency_exchange', 'Arbitrage', a.enabled, `${a.min_profit_pct}%+ return`,
          opt('Smallest return', SE.num({ id: 'tgArbMin', value: a.min_profit_pct, step: 0.5, suf: '%', w: 86 })) +
          opt('Stake for the split', SE.num({ id: 'tgStake', value: a.stake, step: 10, min: 1, pre: '$', w: 96 }))) +
        item('sus', 'gpp_maybe', 'Suspicious matches', su.enabled !== false, `CS2 betting ${yes(su.activity !== false)} · line moves ${yes(su.lines !== false)}`,
          opt('Heavily bet CS2 games', sw('tgSusA', su.activity !== false)) + opt('Pre-match line moves', sw('tgSusL', su.lines !== false)) + opt('Thresholds', '<a href="#/suspicious">Suspicious page</a>')) +
        item('paper', 'history_edu', 'Rule-picker paper record', pa.enabled, `trades ${yes(pa.trades)} · arbitrage ${yes(pa.arbs)}`,
          opt('Closed trades', sw('tgPaperT', pa.trades)) + opt('Paper arbitrage', sw('tgPaperA', pa.arbs)) + opt('Balances under each message', sw('tgPaperS', pa.standings)));
      const on = (id, fn) => $('#' + id)?.addEventListener('change', (e) => fn(e.target));
      on('t-bot', (t) => save({ bot_alerts: { enabled: t.checked } }));
      on('t-odds', (t) => save({ odds: { enabled: t.checked } }));
      on('t-arb', (t) => save({ arbs: { enabled: t.checked } }));
      on('t-sus', (t) => save({ suspicious: { enabled: t.checked } }));
      on('t-paper', (t) => save({ paper: { enabled: t.checked } }));
      on('bBuys', (t) => save({ bot_alerts: { buys: t.checked } }));
      on('bSells', (t) => save({ bot_alerts: { sells: t.checked } }));
      on('bCharts', (t) => save({ bot_alerts: { charts: t.checked } }));
      on('bRun', (t) => save({ bot_alerts: { run_updates: t.checked } }));
      on('tgPaperT', (t) => save({ paper: { trades: t.checked } }));
      on('tgPaperA', (t) => save({ paper: { arbs: t.checked } }));
      on('tgPaperS', (t) => save({ paper: { standings: t.checked } }));
      on('tgSusA', (t) => save({ suspicious: { activity: t.checked } }));
      on('tgSusL', (t) => save({ suspicious: { lines: t.checked } }));
      on('tgMin', (t) => save({ odds: { min_move_pts: +t.value || 3 } }));
      on('tgArbMin', (t) => save({ arbs: { min_profit_pct: +t.value || 0 } }));
      on('tgStake', (t) => save({ arbs: { stake: +t.value || 100 } }));
      $$('[data-mk]').forEach((c) => c.addEventListener('change', () => save({ odds: { markets: $$('[data-mk]').filter((x) => x.checked).map((x) => x.dataset.mk) } })));
      if ($('#tgWo')) {
        SE.seg($('#tgWo'), [['true', 'Watched'], ['false', 'All', 'Every match Swifteye shows. Can be a lot.']], String(o.watched_only), (v) => save({ odds: { watched_only: v === 'true' } }));
        SE.seg($('#tgSrc'), [['all', 'All'], ['books', 'Bookmakers'], ['predictions', 'Prediction markets']], o.sources, (v) => save({ odds: { sources: v } }));
        SE.seg($('#tgCd'), [[30, '30 s'], [60, '1 min'], [300, '5 min'], [900, '15 min']], o.cooldown_s, (v) => save({ odds: { cooldown_s: +v } }));
      }
      $$('[data-open]').forEach((bt) => bt.addEventListener('click', () => {
        const k = bt.dataset.open;
        if (open.has(k)) open.delete(k); else open.add(k);
        SE.setPref('tgOpen', [...open]);
        drawList();
      }));
    }

    // ---------------------------------------------------------------- ask the bot
    const ASK = [['/status', 'Status', 'monitor_heart'], ['/now', 'Update', 'query_stats'], ['/today', 'Today', 'today'], ['/open', 'Open', 'hourglass_top'], ['/trades', 'Trades', 'receipt_long'], ['/real', 'Real wallet', 'account_balance'], ['/paper', 'Paper wallet', 'savings'], ['/run', 'Real run', 'timer'], ['/safety', 'Safety stops', 'shield'], ['/twins', 'Twins', 'group'], ['/stats', 'Stats', 'insights'], ['/signals', 'Signals', 'bolt'], ['/rules', 'Rules', 'rule'], ['/health', 'Health', 'monitor_heart'], ['/projected 24h', 'Projection', 'trending_up'], ['/log', 'Orders', 'list_alt'], ['/help', 'All commands', 'help']];
    function drawAsk() {
      $('#tgAsk').innerHTML = `<div style="display:flex;gap:8px;flex-wrap:wrap">` + ASK.map(([c, l, i]) => `<button class="btn" data-ask="${esc(c)}" data-tip="${esc(c)}">${ic(i)}${l}</button>`).join('') +
        `<button class="btn ghost" data-push="/balance" data-tip="Sends the real balance chart to the chat.">${ic('show_chart')}Send chart</button></div>`;
      $$('[data-ask]').forEach((b) => b.addEventListener('click', async () => {
        const cmd = b.dataset.ask;
        $('#tgPrev').innerHTML = `<div class="answer"><span class="skeleton">Loading the answer</span></div>`;
        try {
          const r = await api('/telegram/preview?cmd=' + encodeURIComponent(cmd));
          $('#tgPrev').innerHTML = `<div class="answer">${r.text.replace(/\n/g, '<br>')}<div class="foot"><span class="mono">${esc(cmd)}</span><button class="btn sm" id="tgPush" ${s.linked ? '' : 'disabled'}>${ic('send')}Send to Telegram</button></div></div>`;
          $('#tgPush')?.addEventListener('click', () => push(cmd));
        } catch (e) { $('#tgPrev').innerHTML = `<div class="answer down">${esc(e.message)}</div>`; }
      }));
      $$('[data-push]').forEach((b) => b.addEventListener('click', () => push(b.dataset.push)));
    }
    async function push(cmd) {
      try { await api('/telegram/push', { body: { cmd } }); SE.toast('Sent'); load(); } catch (e) { SE.toast(e.message, 7000); }
    }

    // ---------------------------------------------------------------- sent, watched
    const opened = new Set();
    function drawFeed() {
      $('#tgFeed').innerHTML = s.log.length
        ? s.log.slice(0, 60).map((m, i) => `<div class="msg ${m.ok ? '' : 'err'} ${opened.has(m.ts_ms) ? 'open' : ''}" data-i="${m.ts_ms}"><div class="top"><span>${esc(m.kind)}</span><span>${SE.fmtTime(m.ts_ms).slice(0, 5)}</span></div><div class="body">${m.ok ? '' : `Failed: ${esc(m.error || '')}<br>`}${m.text.replace(/\n/g, '<br>')}</div></div>`).join('')
        : `<div class="empty">${ic('chat_bubble')}Nothing sent yet</div>`;
      $$('#tgFeed .msg').forEach((x) => x.addEventListener('click', () => { const k = +x.dataset.i; if (opened.has(k)) opened.delete(k); else opened.add(k); x.classList.toggle('open'); }));
      $('#tgWatch').innerHTML = s.watch.length
        ? s.watch.map((w) => `<div class="li"><span class="tile">${ic('sports_score')}</span><div class="main"><div class="nm"><a href="#/match/${w.id}">${SE.vs(w.home, w.away)}</a></div><div class="sb">${w.start_ms ? SE.fmtKick(w.start_ms) : ''}</div></div><div class="end">${SE.bell(w.id)}</div></div>`).join('')
        : `<div class="empty">${ic('notifications_off')}No watched matches</div>`;
      SE.paintBells();
    }

    // ---------------------------------------------------------------- by sport
    function gameRow(k) {
      const g = gameOf(k), o = s.odds, a = s.arbs;
      const mk = g.markets || o.markets;
      const sub = !g.enabled ? 'off' : custom(g) ? `own settings · ${g.min_move_pts ?? o.min_move_pts}+ points` : 'general settings';
      return `<details class="tg-game" data-g="${esc(k)}" ${openGames.has(k) ? 'open' : ''}>
        <summary class="li" style="cursor:pointer;list-style:none"><span class="tile ${g.enabled ? 'on' : ''}">${SE.sportIcon(k)}</span><div class="main"><div class="nm">${esc(SE.sportName(k))}</div><div class="sb">${sub}</div></div><div class="end">${sw('gOn-' + k, g.enabled)}</div></summary>
        <div class="li-opts">
          <div class="opt"><span>Smallest move</span>${SE.num({ attr: 'data-gf="min_move_pts"', value: g.min_move_pts ?? '', step: 0.5, min: 0.1, placeholder: o.min_move_pts, suf: 'pts', w: 96 })}</div>
          <div class="opt"><span>Markets</span><span class="checks"><label><input type="checkbox" data-gsame="markets" ${g.markets ? '' : 'checked'}>Same</label>${MARKETS.map(([m, l]) => `<label style="${g.markets ? '' : 'opacity:.45'}"><input type="checkbox" data-gm="${m}" ${mk.includes(m) ? 'checked' : ''} ${g.markets ? '' : 'disabled'}>${l}</label>`).join('')}</span></div>
          <div class="opt"><span>Sources</span><div data-gseg="sources"></div></div>
          <div class="opt"><span>Repeat after</span><div data-gseg="cooldown_s"></div></div>
          <div class="opt"><span>Arbitrage</span><span style="display:flex;gap:10px;align-items:center">${SE.num({ attr: 'data-gf="arb_min_profit_pct"', value: g.arb_min_profit_pct ?? '', step: 0.5, placeholder: a.min_profit_pct, suf: '%', w: 86 })}${sw('gArb-' + k, g.arb_enabled)}</span></div>
          <div class="opt"><span></span><button class="btn sm ghost" data-greset="${esc(k)}">${ic('restart_alt')}Use general settings</button></div>
        </div></details>`;
    }
    function drawGames() {
      const all = [...SE.SPORTS, ...SE.GAMES].map((x) => x[0]);
      const off = all.filter((k) => !gameOf(k).enabled).length, own = all.filter((k) => gameOf(k).enabled && custom(gameOf(k))).length;
      $('#tgGamesT').innerHTML = gamesOpen ? `${ic('expand_less')}Hide` : `${ic('expand_more')}${off ? off + ' off' : 'All on'}${own ? ` · ${own} custom` : ''}`;
      if (!gamesOpen) { $('#tgGames').innerHTML = ''; return; }
      const grp = (title, keys, id) => `<div style="display:flex;gap:8px;align-items:center;margin:14px 0 2px"><b>${esc(title)}</b><span style="margin-left:auto"></span><button class="btn sm ghost" data-gall="${id}:1">All on</button><button class="btn sm ghost" data-gall="${id}:0">All off</button></div>${keys.map(gameRow).join('')}`;
      $('#tgGames').innerHTML = grp('Sports', SE.SPORTS.map((x) => x[0]), 'sports') + grp('E-sports', SE.GAMES.map((x) => x[0]), 'games');
      $$('details.tg-game').forEach((d) => {
        const k = d.dataset.g, g = gameOf(k);
        d.addEventListener('toggle', () => (d.open ? openGames.add(k) : openGames.delete(k)));
        $('summary label.switch', d).addEventListener('click', (e) => e.stopPropagation());
        $('#gOn-' + k).addEventListener('change', (e) => save({ odds: { games: { [k]: { enabled: e.target.checked } } } }, SE.sportName(k) + (e.target.checked ? ' on' : ' off')));
        $('#gArb-' + k).addEventListener('change', (e) => save({ odds: { games: { [k]: { arb_enabled: e.target.checked } } } }));
        $$('[data-gf]', d).forEach((i) => i.addEventListener('change', () => save({ odds: { games: { [k]: { [i.dataset.gf]: i.value === '' ? null : +i.value } } } })));
        $('[data-gsame="markets"]', d).addEventListener('change', (e) => save({ odds: { games: { [k]: { markets: e.target.checked ? null : [...s.odds.markets] } } } }));
        $$('[data-gm]', d).forEach((c) => c.addEventListener('change', () => save({ odds: { games: { [k]: { markets: $$('[data-gm]', d).filter((x) => x.checked).map((x) => x.dataset.gm) } } } })));
        SE.seg($('[data-gseg="sources"]', d), [['', 'Same'], ['all', 'All'], ['books', 'Bookmakers'], ['predictions', 'Prediction markets']], g.sources || '', (v) => save({ odds: { games: { [k]: { sources: v || null } } } }));
        SE.seg($('[data-gseg="cooldown_s"]', d), [['', 'Same'], [30, '30 s'], [60, '1 min'], [300, '5 min'], [900, '15 min']], g.cooldown_s ?? '', (v) => save({ odds: { games: { [k]: { cooldown_s: v === '' ? null : +v } } } }));
      });
      $$('[data-greset]').forEach((b) => b.addEventListener('click', () => save({ odds: { games: { [b.dataset.greset]: { ...gdef } } } }, 'Using general settings')));
      $$('[data-gall]').forEach((b) => b.addEventListener('click', () => {
        const [grp2, on] = b.dataset.gall.split(':');
        const keys = (grp2 === 'sports' ? SE.SPORTS : SE.GAMES).map((x) => x[0]);
        save({ odds: { games: Object.fromEntries(keys.map((k) => [k, { enabled: on === '1' }])) } }, on === '1' ? 'All on' : 'All off');
      }));
    }
    $('#tgGamesT').addEventListener('click', () => { gamesOpen = !gamesOpen; drawGames(); });

    // ---------------------------------------------------------------- connection
    function drawConn() {
      if ($('#tgConn').contains(document.activeElement)) return;
      const row = (icon, name, sub, end) => `<div class="li"><span class="tile">${ic(icon)}</span><div class="main"><div class="nm">${name}</div>${sub ? `<div class="sb">${sub}</div>` : ''}</div><div class="end">${end}</div></div>`;
      $('#tgConn').innerHTML =
        row('smart_toy', 'Bot', s.bot?.username ? `@${esc(s.bot.username)}` : s.bot?.error ? `<span class="down">${esc(s.bot.error)}</span>` : 'none yet',
          `<input class="input mono" id="tgTok" placeholder="${esc(s.token_hint || 'Token from @BotFather')}" autocomplete="off" style="width:220px"><button class="btn" id="tgTokS">${ic('save')}</button>`) +
        row('alternate_email', 'Your handle', 'sending /start from it links the chat',
          `<input class="input" id="tgH" placeholder="@yourname" value="${s.handle ? '@' + esc(s.handle) : ''}" style="width:160px"><button class="btn" id="tgHS">${ic('save')}</button>`) +
        row('schedule', 'Times in messages', s.tz === 'utc' ? 'UTC' : 'Norway (CET, CEST in summer)', '<div id="tgTz"></div>') +
        row('chat', 'Chat', s.linked ? `@${esc(s.linked_as || '')}` : 'not linked',
          s.linked ? `<button class="btn ghost" id="tgUn">${ic('link_off')}Unlink</button>` : `<a class="btn primary" href="${esc(s.link_url)}" target="_blank" rel="noopener">${SE.tgLogo(16)}Open in Telegram</a>`);
      SE.seg($('#tgTz'), [['oslo', 'Norway'], ['utc', 'UTC']], s.tz === 'utc' ? 'utc' : 'oslo', (v) => save({ tz: v }, v === 'utc' ? 'Times in UTC' : 'Times in Norway time'));
      $('#tgTokS').addEventListener('click', () => save({ token: $('#tgTok').value.trim() }, 'Token saved'));
      $('#tgHS').addEventListener('click', () => save({ handle: $('#tgH').value.trim() }, 'Handle saved'));
      $('#tgUn')?.addEventListener('click', () => save({ unlink: true }, 'Unlinked'));
    }

    function draw() {
      drawTop();
      const busy = document.activeElement && ['INPUT', 'SELECT'].includes(document.activeElement.tagName) && el.contains(document.activeElement);
      const { log, ...rest } = s;
      const sig = JSON.stringify(rest);
      if (sig !== lastSig && !busy) { lastSig = sig; drawList(); drawGames(); drawConn(); }
      drawFeed();
    }
    async function load() {
      try { s = await api('/telegram'); draw(); } catch (e) { if (!s) SE.toast(e.message, 6000); }
    }
    drawAsk();
    load();
    SE.every(5000, load);
  },
};

// ================================================================ Arbitrage Scout
SE.pages.arbs = {
  title: 'Arbitrage Scout',
  render(el) {
    el.innerHTML = P.head('Arbitrage Scout', `<span class="search">${ic('search')}<input class="input" id="aQ" placeholder="Team or league"></span><div id="aSp"></div><div id="aMin"></div><div id="aChk"></div><label class="muted" style="display:flex;align-items:center;gap:6px">Stake ${SE.num({ id: 'aSt', min: 1, step: 10, value: SE.pref('arbStake') || 100, pre: '$', w: 96 })}</label><button class="btn" id="aHb">${ic('history')}History</button>`) +
      `<div class="panel" id="aHist" hidden><div class="sec-head" style="margin-bottom:10px"><h2>${ic('history')}Past arbitrage</h2><div class="tools"><span class="muted" id="aHn"></span></div></div><div id="aHt"></div></div>` +
      `<div class="src-filter" id="aBk"></div>` +
      P.stats(P.stat('aN', 'balance', 'Arbitrage now', 'Pre-match sets of bets that pay out more than they cost, whatever the result.', true), P.stat('aB', 'trending_up', 'Best return'), P.stat('aL', 'hourglass_bottom', 'Typical lifetime', 'Median time an arbitrage stayed open before closing on its own, from the live scan every 5 s. Ones that ended at kick-off or with a restart are left out.'), P.stat('aNear', 'near_me', 'Within 2% of one', 'Sets that cost up to 2% more than they pay: one price move away.')) +
      P.sec('balance', 'Opportunities', '<div id="aT"></div>', '<span class="muted" id="aScan"></span>', 'Best price for each outcome across every source, pre-match only, scanned every 5 s. Polymarket and Kalshi legs use the best ask (No = 1 minus the Yes bid), with each venue\'s taker fee (Kalshi 7%, Polymarket sports 5%, times p × (1 − p)).') +
      `<div class="grid g-2" style="margin-top:40px">${P.sec('hourglass_bottom', 'How long they last', '<div id="aLife"></div>', '', 'Finished arbitrage from the last 14 days. Each lasted from the first scan that saw it to halfway between its last scan and the one that found it gone.')}${P.sec('history', 'Recently closed', '<div id="aH"></div>')}</div>` +
      `<p class="note" style="margin-top:18px">Prices are what each source showed in the last few minutes. Before betting: open each book and check the price is still there, the game is the same game, and the stake is accepted. Books limit or void bets they see as arbitrage, and settlement rules (overtime, retirements, pushes) differ. Nothing here checks order-book depth.</p>`;
    let min = SE.pref('arbMin') ?? 0;
    P.sportSelect($('#aSp'), () => load());
    SE.seg($('#aMin'), [[-0.02, 'Near misses', 'Also show sets up to 2% short of an arbitrage.'], [0, 'All arbitrage'], [0.01, '1%+'], [0.02, '2%+']], min, (v) => { min = +v; SE.setPref('arbMin', min); load(); });
    // Checked = every price agrees with other sources and any Polymarket / Kalshi leg has $10+ behind it.
    let chk = SE.pref('arbChk') ?? 1;
    SE.seg($('#aChk'), [[1, 'Checked', 'Only sets whose every price is confirmed by at least two other sources, and whose Polymarket or Kalshi legs have at least $10 on offer.'], [0, 'All', 'Also sets that no other source could confirm, or with almost nothing on offer. Treat these as "look into it", not as money.']], chk, (v) => { chk = +v; SE.setPref('arbChk', chk); load(); loadHist(); if (!$('#aHist').hidden) loadAll(); });
    const sound = (a) => a.check === 'ok' && (a.takeable == null || a.takeable >= 10);
    const take = (r) => (r.takeable == null ? '<span class="faint" data-tip="Every leg is at a bookmaker: their bet limits are not known.">limits unknown</span>' : `<b class="${r.takeable < 10 ? 'down' : ''}">$${r.takeable < 100 ? r.takeable.toFixed(0) : Math.round(r.takeable).toLocaleString()}</b>`) + (r.check !== 'ok' ? '<div class="faint" data-tip="Too few other sources price this game to confirm these prices.">not cross-checked</div>' : '');
    let life = {};
    const dur = (ms) => (ms == null ? '-' : ms < 60000 ? Math.round(ms / 1000) + 's' : ms < 3600000 ? `${Math.floor(ms / 60000)}m ${String(Math.round((ms % 60000) / 1000)).padStart(2, '0')}s` : `${Math.floor(ms / 3600000)}h ${String(Math.floor((ms % 3600000) / 60000)).padStart(2, '0')}m`);
    const left = (r) => {
      if (r.age_ms == null) return '<span class="faint">-</span>';
      if (r.left_ms != null) return `<span class="${r.left_ms < 30000 ? 'down' : ''}" data-tip="${esc(`From ${r.left_n} past arbitrage that lasted at least this long.`)}">~${dur(Math.max(0, r.left_ms))}</span>`;
      if (!life.n) return '<span class="faint" data-tip="No arbitrage has finished yet to learn from.">learning</span>';
      return `<span class="muted" data-tip="${esc('Already open longer than almost every past one, so there is nothing to estimate from.')}">overdue</span>`;
    };
    const outLabel = (l) => (l.side === 'no' ? `No ${SE.outcomeLabel(l.against)}` : l.side === 'yes' ? `Yes ${SE.outcomeLabel(l.outcome)}` : SE.outcomeLabel(l.outcome));
    const t = SE.table($('#aT'), [
      { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league, r.sport, r.match_id), sort: (r) => r.home },
      { key: 'k', label: 'Kick-off', fmt: (r) => `<span class="muted">${SE.fmtKick(r.start_ms)}</span><div class="faint">${SE.fmtIn(r.start_ms)}</div>`, sort: (r) => r.start_ms },
      { key: 'mk', label: 'Market', fmt: (r) => `${esc(SE.marketLabel(r.market))}${r.line != null ? ' <b>' + (r.market === 'handicap' && r.line > 0 ? '+' : '') + r.line + '</b>' : ''}` },
      { key: 'p', label: 'Return', cls: 'r', tip: 'Guaranteed profit on the total stake if every leg is placed at these prices.', fmt: (r) => `<b class="${r.profit > 0 ? 'up' : 'muted'}">${r.profit > 0 ? '+' : ''}${(r.profit * 100).toFixed(2)}%</b>`, sort: (r) => r.profit },
      { key: 'tk', label: 'Can take', cls: 'r', tip: 'The largest total stake, across all the bets, that the Polymarket and Kalshi legs can take at these prices. A bigger stake moves the price. Bookmaker limits are not known.', fmt: (r) => take(r), sort: (r) => (r.takeable == null ? 1e12 : r.takeable) },
      { key: 'age', label: 'Open for', cls: 'r', tip: 'Since the live scan first saw this arbitrage. ≥ when it was already open when Swifteye started.', fmt: (r) => (r.age_ms == null ? '<span class="faint">-</span>' : `${r.age_partial ? '≥ ' : ''}${dur(r.age_ms)}`), sort: (r) => r.age_ms },
      { key: 'left', label: 'Est. left', cls: 'r', tip: 'Median lifetime of past arbitrage that lasted at least this long, minus how long this one has been open. A rough guide, not a promise.', fmt: (r) => left(r), sort: (r) => r.left_ms },
      { key: 'l', label: 'Bets', fmt: (r) => `<div class="legs">${r.legs.map((l) => `<div><span class="leg-o">${outLabel(l)}</span> ${SE.book(l.book, 'sm')} <b>${l.odds.toFixed(2)}</b> <span class="muted">$${l.stake.toFixed(2)}</span> <span class="faint">${SE.fmtAgo(l.updated_ms)}</span></div>`).join('')}</div>` },
      { key: 'w', label: '', fmt: (r) => (r.warn.length ? `<span class="ic faint" data-tip="${esc(r.warn.join('. '))}">warning</span>` : '') },
      { key: 'ord', label: '', w: 96, fmt: (r) => (held.has(+r.match_id) ? `<span class="chip accent" data-tip="The paper bot already holds an arbitrage on this game (Snipe window → Paper bot → Open positions).">${ic('check')}Bought</span>` : r.profit > 0 && r.legs.every((l) => SE.PRED.includes(l.book)) ? `<button class="btn sm primary" data-ord="${+r.match_id}" data-tip="Send to the hedge & arbitrage bot: it re-prices the game from the live Polymarket and Kalshi books and takes the arbitrage if it is still there (as deep as the books pay, within the bot\'s caps). Paper or real, as the bot is set on the Bots page.">${ic('bolt')}Order</button>` : '') },
    ], { sort: 'p', empty: 'No pre-match arbitrage right now. Try Near misses.', emptyIcon: 'balance', onClick: (r) => SE.go('#/match/' + r.match_id) });
    // Order button (rows re-render every 5 s, so listen on the table).
    $('#aT').addEventListener('click', async (e) => {
      const b = e.target.closest('[data-ord]');
      if (!b) return;
      e.stopPropagation();
      b.disabled = true;
      try {
        const r = await api('/predarb/order/' + b.dataset.ord, { method: 'POST' });
        if (r.ok) { held.add(+b.dataset.ord); b.outerHTML = `<span class="chip accent">${ic('check')}Bought</span>`; const p = r.taken[0]; SE.toast(`Paper order: Yes ${(p.yes * 100).toFixed(1)}¢ on ${SE.bookName(p.yes_on)} + No ${(p.no * 100).toFixed(1)}¢, $${p.cost.toFixed(2)} staked, $${p.profit.toFixed(2)} locked`, 6000); }
        else SE.toast(r.error, 6000);
      } catch (err) { SE.toast(err.message, 6000); }
      if (b.isConnected) b.disabled = false;
    }, true);
    // Sources you can bet on: only arbitrage whose every bet is on one of them (none chosen = all).
    const books = new Set(SE.pref('arbBooks') || []);
    function drawBooks(arbs) {
      const el2 = $('#aBk');
      if (!el2) return;
      const seen = [...new Set([...arbs.flatMap((a) => a.legs.map((l) => l.book)), ...books])].sort((x, y) => (SE.PRED.includes(y) - SE.PRED.includes(x)) || SE.bookName(x).localeCompare(SE.bookName(y)));
      const key = seen.join(',') + '|' + [...books].join(',');
      if (el2.dataset.k === key) return;
      el2.dataset.k = key;
      el2.innerHTML = `<span class="muted" data-tip="Show only arbitrage where every bet is on a source you choose, such as the ones you have accounts with. None chosen = every source.">Bet on</span>` +
        `<button class="chip-btn ${books.size ? '' : 'on'}" data-b="">All sources</button>` +
        seen.map((b) => `<button class="chip-btn ${books.has(b) ? 'on' : ''}" data-b="${esc(b)}">${SE.logo(b)}${esc(SE.bookName(b))}</button>`).join('');
      $$('[data-b]', el2).forEach((btn) => btn.addEventListener('click', () => {
        const b = btn.dataset.b;
        if (!b) books.clear(); else if (books.has(b)) books.delete(b); else books.add(b);
        SE.setPref('arbBooks', [...books]);
        load();
      }));
    }
    let held = new Set();
    async function load() {
      const stake = Math.max(1, +$('#aSt').value || 100);
      try { const a = await api('/predarb'); const now = Date.now(); held = new Set(a.recent.filter((p) => (p.payout_ms || 0) > now).map((p) => +p.match_id)); } catch {}
      const r = await api(`/arbs?min_profit=${Math.min(min, -0.02)}&stake=${stake}`);
      life = r.lifetime;
      const q = $('#aQ').value.trim().toLowerCase();
      let all = r.arbs.filter((a) => P.sportOk(a.sport));
      if (q) all = all.filter((a) => `${a.home} ${a.away} ${a.league}`.toLowerCase().includes(q));
      drawBooks(r.arbs);
      if (books.size) all = all.filter((a) => a.legs.every((l) => books.has(l.book)));
      if (chk) all = all.filter(sound);
      const real = all.filter((a) => a.profit > 0);
      P.set('aN', real.length);
      P.set('aB', real.length ? `<span class="up">+${(real[0].profit * 100).toFixed(2)}%</span>` : '-');
      P.set('aL', life.median_ms != null ? dur(life.median_ms) : '-', life.n ? `${life.n} finished · middle half ${dur(life.p25_ms)} to ${dur(life.p75_ms)}` : 'Learning: none finished yet');
      P.set('aNear', all.filter((a) => a.profit <= 0).length);
      $('#aScan').textContent = r.scanned_ms ? 'Scanned ' + SE.fmtAgo(r.scanned_ms) : 'First scan in a few seconds';
      t.set(all.filter((a) => a.profit >= min - 1e-9));
    }
    async function loadHist() {
      const h = await api('/arbs/history?limit=400');
      // Finds from before the scout cross-checked its prices are shown only under "All".
      h.recent = h.recent.filter((x) => !chk || x.checked).slice(0, 60);
      const L = h.lifetime;
      const rowsOf = (list, name) => list.map(([k, n, m]) => `<tr><td>${esc(name(k))}</td><td class="r">${n}</td><td class="r"><b>${dur(m)}</b></td></tr>`).join('');
      $('#aLife').innerHTML = L.n ? `<table class="tbl compact"><thead><tr><th></th><th class="r">Finished</th><th class="r">Median life</th></tr></thead><tbody>
          <tr><td><b>All</b></td><td class="r">${L.n}</td><td class="r"><b>${dur(L.median_ms)}</b> <span class="faint">mean ${dur(L.mean_ms)}, 90% under ${dur(L.p90_ms)}</span></td></tr>
          ${rowsOf(L.by_market, SE.marketLabel)}${rowsOf(L.by_return, (k) => 'Return ' + k)}</tbody></table>
          <p class="note" style="margin-top:12px">${L.kickoff} more were still open at kick-off and are left out.</p>`
        : `<div class="empty">${ic('hourglass_empty')}Nothing has finished yet. Lifetimes appear once arbitrage found by the live scan closes.</div>`;
      $('#aH').innerHTML = h.recent.length ? `<div class="scroll" style="max-height:420px"><table class="tbl compact"><tbody>${h.recent.map((x) => `<tr class="link" data-m="${x.match_id}"><td class="match"><a class="match-link" href="#/match/${+x.match_id}">${SE.vs(x.home, x.away)}</a><div class="faint">${esc(SE.marketLabel(x.market))}${x.line != null ? ' ' + x.line : ''} · ${esc((x.books || '').split(',').map(SE.bookName).join(', '))}</div></td><td class="r"><span class="up">+${((x.peak_profit || 0) * 100).toFixed(2)}%</span></td><td class="r"><b>${x.censored ? '≥ ' : ''}${dur(x.lasted_ms)}</b><div class="faint">${{ gone: 'closed', kickoff: 'kick-off', restart: 'restart' }[x.end_reason] || esc(x.end_reason)} ${SE.fmtAgo(x.end_ms)}</div></td></tr>`).join('')}</tbody></table></div>`
        : `<div class="empty">${ic('history')}None closed yet.</div>`;
      $$('#aH tr[data-m]').forEach((tr) => tr.addEventListener('click', () => SE.go('#/match/' + tr.dataset.m)));
    }
    // Full history: every finished arbitrage the live scan saw (with the "Bet on" filter applied).
    const ht = SE.table($('#aHt'), [
      { key: 'e', label: 'Closed', w: 130, fmt: (x) => `<span class="muted">${SE.fmtDateTime(x.end_ms || x.last_ms)}</span>`, sort: (x) => x.end_ms || x.last_ms },
      { key: 'm', label: 'Match', cls: 'match', fmt: (x) => P.matchCell(x.home, x.away, x.league, x.sport, x.match_id) },
      { key: 'mk', label: 'Market', w: 120, fmt: (x) => `${esc(SE.marketLabel(x.market))}${x.line != null ? ' <b>' + x.line + '</b>' : ''}` },
      { key: 'b', label: 'Sources', w: 220, fmt: (x) => (x.books || '').split(',').filter(Boolean).map((b) => SE.book(b, 'sm')).join(' ') },
      { key: 'p', label: 'Best return', cls: 'r', w: 110, fmt: (x) => `<b class="up">+${((x.peak_profit || 0) * 100).toFixed(2)}%</b>`, sort: (x) => x.peak_profit || 0 },
      { key: 'tk', label: 'Could take', cls: 'r', w: 110, tip: 'The largest total stake its Polymarket or Kalshi legs could take when it opened. Empty for ones found before this was recorded (20 Nov 2026) and for bookmaker-only sets.', fmt: (x) => (x.takeable == null ? '<span class="faint">-</span>' : `$${Math.round(x.takeable).toLocaleString()}`), sort: (x) => x.takeable || 0 },
      { key: 'l', label: 'Lasted', cls: 'r', w: 110, fmt: (x) => `${x.censored ? '≥ ' : ''}${dur(x.lasted_ms)}<div class="faint">${{ gone: 'closed', kickoff: 'kick-off', restart: 'restart' }[x.end_reason] || esc(x.end_reason || 'open')}</div>`, sort: (x) => x.lasted_ms },
    ], { sort: 'e', empty: 'No arbitrage has closed yet.', onClick: (x) => SE.go('#/match/' + x.match_id) });
    async function loadAll() {
      const h = await api('/arbs/history?limit=2000');
      let rows = h.recent.filter((x) => !chk || x.checked);
      if (books.size) rows = rows.filter((x) => (x.books || '').split(',').every((b) => books.has(b)));
      if (!$('#aHn')) return;
      $('#aHn').textContent = `${rows.length} finished${books.size ? ' on your chosen sources' : ''}${chk ? ' · checked ones only (since 20 Nov)' : ' · including unchecked finds from before 20 Nov'}`;
      ht.set(rows);
    }
    $('#aHb').addEventListener('click', () => {
      const p = $('#aHist');
      p.hidden = !p.hidden;
      $('#aHb').classList.toggle('on', !p.hidden);
      if (!p.hidden) loadAll();
    });
    $('#aQ').addEventListener('input', SE.debounce(load, 150));
    $('#aSt').addEventListener('input', SE.debounce(() => { SE.setPref('arbStake', +$('#aSt').value || 100); load(); }, 300));
    load();
    loadHist();
    SE.every(5000, load);
    SE.every(30000, loadHist);
  },
};
})();
