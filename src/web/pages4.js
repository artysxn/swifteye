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
      <label data-tip="Money resting in the order book (Polymarket liquidity, Kalshi liquidity in dollars).">Liquidity ≥ $<input class="input" type="number" min="0" step="1000" data-k="liq" value="${f.liq}"></label>
      <label data-tip="Traded in the last 24 hours, in dollars (Kalshi contracts valued at their last price).">24 h volume ≥ $<input class="input" type="number" min="0" step="1000" data-k="vol" value="${f.vol}"></label>
      <label data-tip="Price changes Swifteye recorded on the market in the last hour: how alive it is.">Moves / hour ≥ <input class="input" type="number" min="0" step="1" data-k="moves" value="${f.moves}"></label>
      <label data-tip="Price of the least likely outcome, in cents. Drops one-sided games whose market barely trades.">Cheapest outcome ≥ <input class="input" type="number" min="0" max="50" step="1" data-k="price" value="${f.price}">c</label></div>`;
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
    el.innerHTML = P.head(`<span style="display:inline-flex;align-items:center;gap:12px">${SE.tgLogo(30)}Telegram</span>`, '<span id="tgChip"></span>') + '<div id="tgBody"><div class="empty">Loading</div></div>';
    let s = null;
    const save = async (edit, msg = 'Saved') => {
      try { s = await api('/telegram', { body: edit }); draw(); SE.toast(msg); } catch (e) { SE.toast(e.message); }
    };
    const sw = (id, on) => `<label class="switch"><input type="checkbox" id="${id}" ${on ? 'checked' : ''}><span></span></label>`;
    const MARKETS = [['1x2', '1X2 / winner'], ['total', 'Total'], ['handicap', 'Handicap'], ['btts', 'Both score']];
    const openGames = new Set();
    const gdef = { enabled: true, min_move_pts: null, markets: null, sources: null, cooldown_s: null, arb_enabled: true, arb_min_profit_pct: null };
    const gameOf = (k) => ({ ...gdef, ...(s.odds.games?.[k] || {}) });
    const custom = (g) => g.min_move_pts != null || g.markets || g.sources || g.cooldown_s != null || g.arb_min_profit_pct != null || !g.arb_enabled;
    function gameRow(k) {
      const g = gameOf(k), o = s.odds, a = s.arbs;
      const mk = g.markets || o.markets;
      const chip = !g.enabled ? '<span class="chip bad">Off</span>' : custom(g) ? '<span class="chip ok">Custom</span>' : '<span class="chip">Default</span>';
      const sum = g.enabled ? `<span class="faint" style="margin-left:10px">≥ ${g.min_move_pts ?? o.min_move_pts} pts${g.arb_enabled ? '' : ' · arb off'}</span>` : '';
      return `<details class="tg-game" data-g="${esc(k)}" ${openGames.has(k) ? 'open' : ''} style="border-bottom:1px solid var(--line)">
        <summary style="display:flex;align-items:center;gap:12px;padding:11px 0;cursor:pointer;list-style:none">
          <span style="display:inline-flex;align-items:center;gap:8px;min-width:190px;font-weight:550">${SE.sportIcon(k)}${esc(SE.sportName(k))}</span>${chip}${sum}
          <span style="margin-left:auto" data-tip="Odds-move alerts for this game">${sw('gOn-' + k, g.enabled)}</span>
        </summary>
        <div style="padding:0 0 12px 0">
          <div class="field"><label data-tip="Empty = the general value (${o.min_move_pts} points).">Smallest move</label><div style="display:flex;gap:8px;align-items:center"><input class="input" type="number" data-gf="min_move_pts" min="0.1" step="0.5" placeholder="${o.min_move_pts}" value="${g.min_move_pts ?? ''}" style="width:90px">points</div></div>
          <div class="field"><label>Markets</label><div style="display:flex;gap:14px;flex-wrap:wrap;align-items:center"><label style="display:flex;gap:6px;align-items:center;font-weight:500"><input type="checkbox" data-gsame="markets" ${g.markets ? '' : 'checked'}>Same</label>${MARKETS.map(([m, l]) => `<label style="display:flex;gap:6px;align-items:center;font-weight:500;${g.markets ? '' : 'opacity:.45'}"><input type="checkbox" data-gm="${m}" ${mk.includes(m) ? 'checked' : ''} ${g.markets ? '' : 'disabled'}>${l}</label>`).join('')}</div></div>
          <div class="field"><label>Sources</label><div data-gseg="sources"></div></div>
          <div class="field"><label>Repeat after</label><div data-gseg="cooldown_s"></div></div>
          <div class="field"><label data-tip="Pre-match arbitrage on this game.">Arbitrage</label><div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">${sw('gArb-' + k, g.arb_enabled)}<span class="muted">Smallest return</span><input class="input" type="number" data-gf="arb_min_profit_pct" step="0.5" placeholder="${a.min_profit_pct}" value="${g.arb_min_profit_pct ?? ''}" style="width:90px">%</div></div>
          <div class="field"><label></label><div><button class="btn ghost" data-greset="${esc(k)}">${ic('restart_alt')}Use general settings</button></div></div>
        </div></details>`;
    }
    function gameGroup(title, keys) {
      const nOff = keys.filter((k) => !gameOf(k).enabled).length;
      return `<div class="sn-row" style="margin:18px 0 4px">${esc(title)} <span class="faint">${nOff ? nOff + ' off' : 'all on'}</span></div>
        <div style="display:flex;gap:8px;margin-bottom:6px"><button class="btn ghost" data-gall="${title === 'Sports' ? 'sports' : 'games'}:1">All on</button><button class="btn ghost" data-gall="${title === 'Sports' ? 'sports' : 'games'}:0">All off</button></div>${keys.map(gameRow).join('')}`;
    }
    function wireGames() {
      const gs = (k) => $(`details[data-g="${k}"]`);
      $$('details.tg-game').forEach((d) => {
        const k = d.dataset.g, g = gameOf(k);
        d.addEventListener('toggle', () => (d.open ? openGames.add(k) : openGames.delete(k)));
        // Clicks on the switch must not fold the row.
        $('summary label.switch', d).addEventListener('click', (e) => e.stopPropagation());
        $('#gOn-' + k).addEventListener('change', (e) => save({ odds: { games: { [k]: { enabled: e.target.checked } } } }, SE.sportName(k) + (e.target.checked ? ' alerts on' : ' alerts off')));
        $('#gArb-' + k).addEventListener('change', (e) => save({ odds: { games: { [k]: { arb_enabled: e.target.checked } } } }));
        $$('[data-gf]', d).forEach((i) => i.addEventListener('change', () => save({ odds: { games: { [k]: { [i.dataset.gf]: i.value === '' ? null : +i.value } } } })));
        $('[data-gsame="markets"]', d).addEventListener('change', (e) => save({ odds: { games: { [k]: { markets: e.target.checked ? null : [...s.odds.markets] } } } }));
        $$('[data-gm]', d).forEach((c) => c.addEventListener('change', () => save({ odds: { games: { [k]: { markets: $$('[data-gm]', d).filter((x) => x.checked).map((x) => x.dataset.gm) } } } })));
        SE.seg($('[data-gseg="sources"]', d), [['', 'Same'], ['all', 'All'], ['books', 'Bookmakers'], ['predictions', 'Polymarket + Kalshi']], g.sources || '', (v) => save({ odds: { games: { [k]: { sources: v || null } } } }));
        SE.seg($('[data-gseg="cooldown_s"]', d), [['', 'Same'], [30, '30 s'], [60, '1 min'], [300, '5 min'], [900, '15 min']], g.cooldown_s ?? '', (v) => save({ odds: { games: { [k]: { cooldown_s: v === '' ? null : +v } } } }));
      });
      $$('[data-greset]').forEach((b) => b.addEventListener('click', () => save({ odds: { games: { [b.dataset.greset]: { ...gdef } } } }, 'Using general settings')));
      $$('[data-gall]').forEach((b) => b.addEventListener('click', () => {
        const [grp, on] = b.dataset.gall.split(':');
        const keys = (grp === 'sports' ? SE.SPORTS : SE.GAMES).map((x) => x[0]);
        save({ odds: { games: Object.fromEntries(keys.map((k) => [k, { enabled: on === '1' }])) } }, on === '1' ? 'All on' : 'All off');
      }));
    }
    function draw() {
      const o = s.odds, a = s.arbs;
      $('#tgChip').innerHTML = s.linked ? `<span class="chip ok">${ic('check_circle')}Linked${s.linked_as ? ' as @' + esc(s.linked_as) : ''}</span>` : '<span class="chip bad">Not linked</span>';
      const bot = s.bot?.username ? `<a href="${esc(s.bot_url)}" target="_blank" rel="noopener">@${esc(s.bot.username)}</a> <span class="chip ok">Running</span>` : s.bot?.error ? `<span class="chip bad">${esc(s.bot.error)}</span>` : '<span class="muted">Checking</span>';
      $('#tgBody').innerHTML = `<div class="grid g-main"><div>
        ${P.sec('link', 'Connection', `
          <div class="field"><label>Bot</label><div>${bot}</div></div>
          <div class="field"><label data-tip="From @BotFather. Stored in telegram.json next to the database, never in the code.">Bot token</label><div style="display:flex;gap:8px"><input class="input mono" id="tgTok" placeholder="${esc(s.token_hint || 'Paste the token from @BotFather')}" autocomplete="off"><button class="btn" id="tgTokS">${ic('save')}Save</button></div></div>
          <div class="field"><label data-tip="Your own Telegram username. When you send /start to the bot from this account it links automatically.">Your handle</label><div style="display:flex;gap:8px"><input class="input" id="tgH" placeholder="@yourname" value="${s.handle ? '@' + esc(s.handle) : ''}"><button class="btn" id="tgHS">${ic('save')}Save</button></div></div>
          <div class="field"><label>Your account</label><div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">${s.linked
            ? `<span class="chip ok">@${esc(s.linked_as || '')}</span><button class="btn" id="tgTest">${ic('send')}Send test</button><button class="btn ghost" id="tgUn">${ic('link_off')}Unlink</button>`
            : `<a class="btn primary" href="${esc(s.link_url)}" target="_blank" rel="noopener">${SE.tgLogo(16)}Open in Telegram</a><span class="muted">then press Start</span>`}</div></div>
          <p class="note" style="margin-top:14px">Open in Telegram links this install to the account you press Start with. Or save your handle and send /start to the bot. Only that chat gets alerts and answers. In the chat: /status, /arbs, /watching, /pause, /resume.</p>`)}
        ${P.sec('notifications', 'Alerts', `
          <div class="field"><label>Send alerts</label><div>${sw('tgOn', s.enabled)}</div></div>
          <div class="field"><label data-tip="A message when a price moves on a watched match. Turn the bell on for a match on the Matches page or the match page.">Odds moves</label><div>${sw('tgOdds', o.enabled)}</div></div>
          <div class="field"><label>Which matches</label><div id="tgWo"></div></div>
          <div class="field"><label data-tip="Change in implied probability, in percentage points. 2.00 to 1.90 is 2.6 points.">Smallest move</label><div style="display:flex;gap:8px;align-items:center"><input class="input" type="number" id="tgMin" min="0.5" step="0.5" value="${o.min_move_pts}" style="width:90px">points</div></div>
          <div class="field"><label>Markets</label><div style="display:flex;gap:16px;flex-wrap:wrap">${[['1x2', '1X2 / winner'], ['total', 'Total'], ['handicap', 'Handicap'], ['btts', 'Both score']].map(([k, l]) => `<label style="display:flex;gap:6px;align-items:center;font-weight:500"><input type="checkbox" data-mk="${k}" ${o.markets.includes(k) ? 'checked' : ''}>${l}</label>`).join('')}</div></div>
          <div class="field"><label>Sources</label><div id="tgSrc"></div></div>
          <div class="field"><label data-tip="The same price on the same source does not alert again within this time.">Repeat after</label><div id="tgCd"></div></div>
          <div class="field"><label data-tip="Pre-match arbitrage from the Arbitrage Scout, checked every 30 s. The same one is sent again after an hour or when it gets a point better.">Arbitrage</label><div>${sw('tgArb', a.enabled)}</div></div>
          <div class="field"><label>Smallest return</label><div style="display:flex;gap:8px;align-items:center"><input class="input" type="number" id="tgArbMin" step="0.5" value="${a.min_profit_pct}" style="width:90px">%<span class="muted" style="margin-left:18px">Stake for the split $</span><input class="input" type="number" id="tgStake" min="1" step="10" value="${a.stake}" style="width:100px"></div></div>
          <div class="field"><label data-tip="Paper bot activity, checked every minute. Paper money only. On by default; the other alert types are off by default.">Paper bot activity</label><div>${sw('tgPaper', s.paper.enabled)}</div></div>
          <div class="field"><label data-tip="Each snipe trade when it closes (bought and sold within minutes).">· Snipe trades</label><div>${sw('tgPaperT', s.paper.trades)}</div></div>
          <div class="field"><label data-tip="Polymarket × Kalshi arbitrage: bought, sold early, paid out.">· Arbitrage positions</label><div>${sw('tgPaperA', s.paper.arbs)}</div></div>
          <div class="field"><label data-tip="Current paper balances on Polymarket and Kalshi, and the arbitrage total, under every message.">· Standings</label><div>${sw('tgPaperS', s.paper.standings)}</div></div>
          <div class="field"><label data-tip="Games flagged on the Suspicious page. Thresholds are set there.">Suspicious matches</label><div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">${sw('tgSus', s.suspicious?.enabled !== false)}<label style="display:flex;gap:6px;align-items:center;font-weight:500"><input type="checkbox" id="tgSusA" ${s.suspicious?.activity !== false ? 'checked' : ''}>Heavily bet CS2 games</label><label style="display:flex;gap:6px;align-items:center;font-weight:500"><input type="checkbox" id="tgSusL" ${s.suspicious?.lines !== false ? 'checked' : ''}>Pre-match line moves</label><a href="#/suspicious" class="muted">Thresholds</a></div></div>`)}
        ${P.sec('sports_esports', 'By game', `
          <p class="note" style="margin:0 0 12px">Each game can follow the settings above or use its own. Leave a box empty or on <i>Same</i> to use the general value. A game switched off is silent, bell-watched matches included.</p>
          ${gameGroup('Sports', SE.SPORTS.map((x) => x[0]))}${gameGroup('E-sports', SE.GAMES.map((x) => x[0]))}`)}
      </div><div>
        ${P.sec('notifications_active', 'Watched matches', s.watch.length ? `<div>${s.watch.map((w) => `<div class="field" style="grid-template-columns:minmax(0,1fr) auto"><a href="#/match/${w.id}">${SE.vs(w.home, w.away)} <span class="faint">${w.start_ms ? SE.fmtKick(w.start_ms) : ''}</span></a>${SE.bell(w.id)}</div>`).join('')}</div>` : `<div class="empty">${ic('notifications_off')}No matches yet. Press the bell on a match.</div>`)}
        ${P.sec('history', 'Sent', s.log.length ? s.log.map((m) => `<div class="tg-msg ${m.ok ? '' : 'err'}"><div class="faint">${SE.fmtTime(m.ts_ms)} · ${esc(m.kind)}${m.ok ? '' : ' · failed: ' + esc(m.error || '')}</div><div>${m.text.replace(/\n/g, '<br>')}</div></div>`).join('') : '<div class="empty">Nothing sent yet.</div>')}
      </div></div>`;
      SE.paintBells();
      wireGames();
      SE.seg($('#tgWo'), [['true', 'Watched only'], ['false', 'Every match in scope', 'Every match Swifteye shows. Can be a lot of messages.']], String(o.watched_only), (v) => save({ odds: { watched_only: v === 'true' } }));
      SE.seg($('#tgSrc'), [['all', 'All'], ['books', 'Bookmakers'], ['predictions', 'Polymarket + Kalshi']], o.sources, (v) => save({ odds: { sources: v } }));
      SE.seg($('#tgCd'), [[30, '30 s'], [60, '1 min'], [300, '5 min'], [900, '15 min']], o.cooldown_s, (v) => save({ odds: { cooldown_s: +v } }));
      const on = (id, fn) => $('#' + id)?.addEventListener('change', (e) => fn(e.target));
      on('tgOn', (t) => save({ enabled: t.checked }, t.checked ? 'Alerts on' : 'Alerts paused'));
      on('tgOdds', (t) => save({ odds: { enabled: t.checked } }));
      on('tgArb', (t) => save({ arbs: { enabled: t.checked } }));
      on('tgPaper', (t) => save({ paper: { enabled: t.checked } }));
      on('tgPaperT', (t) => save({ paper: { trades: t.checked } }));
      on('tgPaperA', (t) => save({ paper: { arbs: t.checked } }));
      on('tgPaperS', (t) => save({ paper: { standings: t.checked } }));
      on('tgSus', (t) => save({ suspicious: { enabled: t.checked } }));
      on('tgSusA', (t) => save({ suspicious: { activity: t.checked } }));
      on('tgSusL', (t) => save({ suspicious: { lines: t.checked } }));
      on('tgMin', (t) => save({ odds: { min_move_pts: +t.value || 3 } }));
      on('tgArbMin', (t) => save({ arbs: { min_profit_pct: +t.value || 0 } }));
      on('tgStake', (t) => save({ arbs: { stake: +t.value || 100 } }));
      $$('[data-mk]').forEach((c) => c.addEventListener('change', () => save({ odds: { markets: $$('[data-mk]').filter((x) => x.checked).map((x) => x.dataset.mk) } })));
      $('#tgTokS').addEventListener('click', () => save({ token: $('#tgTok').value.trim() }, 'Token saved'));
      $('#tgHS').addEventListener('click', () => save({ handle: $('#tgH').value.trim() }, 'Handle saved. Now send /start to the bot.'));
      $('#tgTest')?.addEventListener('click', async () => { try { await api('/telegram/test', { method: 'POST' }); SE.toast('Sent'); load(); } catch (e) { SE.toast(e.message); } });
      $('#tgUn')?.addEventListener('click', () => save({ unlink: true }, 'Unlinked'));
    }
    async function load() {
      const focused = document.activeElement?.closest?.('#tgBody input');
      s = await api('/telegram');
      if (!focused) draw();
    }
    load();
    SE.every(5000, load);
  },
};

// ================================================================ Arbitrage Scout
SE.pages.arbs = {
  title: 'Arbitrage Scout',
  render(el) {
    el.innerHTML = P.head('Arbitrage Scout', `<span class="search">${ic('search')}<input class="input" id="aQ" placeholder="Team or league"></span><div id="aSp"></div><div id="aMin"></div><label class="muted" style="display:flex;align-items:center;gap:6px">Stake $<input class="input" id="aSt" type="number" min="1" step="10" value="${SE.pref('arbStake') || 100}" style="width:90px"></label><button class="btn" id="aHb">${ic('history')}History</button>`) +
      `<div class="panel" id="aHist" hidden><div class="sec-head" style="margin-bottom:10px"><h2>${ic('history')}Past arbitrage</h2><div class="tools"><span class="muted" id="aHn"></span></div></div><div id="aHt"></div></div>` +
      `<div class="src-filter" id="aBk"></div>` +
      P.stats(P.stat('aN', 'balance', 'Arbitrage now', 'Pre-match sets of bets that pay out more than they cost, whatever the result.', true), P.stat('aB', 'trending_up', 'Best return'), P.stat('aL', 'hourglass_bottom', 'Typical lifetime', 'Median time an arbitrage stayed open before closing on its own, from the live scan every 5 s. Ones that ended at kick-off or with a restart are left out.'), P.stat('aNear', 'near_me', 'Within 2% of one', 'Sets that cost up to 2% more than they pay: one price move away.')) +
      P.sec('balance', 'Opportunities', '<div id="aT"></div>', '<span class="muted" id="aScan"></span>', 'Best price for each outcome across every source, pre-match only, scanned every 5 s. Polymarket and Kalshi legs use the best ask (No = 1 minus the Yes bid), with each venue\'s taker fee (Kalshi 7%, Polymarket sports 5%, times p × (1 − p)).') +
      `<div class="grid g-2" style="margin-top:40px">${P.sec('hourglass_bottom', 'How long they last', '<div id="aLife"></div>', '', 'Finished arbitrage from the last 14 days. Each lasted from the first scan that saw it to halfway between its last scan and the one that found it gone.')}${P.sec('history', 'Recently closed', '<div id="aH"></div>')}</div>` +
      `<p class="note" style="margin-top:18px">Prices are what each source showed in the last few minutes. Before betting: open each book and check the price is still there, the game is the same game, and the stake is accepted. Books limit or void bets they see as arbitrage, and settlement rules (overtime, retirements, pushes) differ. Nothing here checks order-book depth.</p>`;
    let min = SE.pref('arbMin') ?? 0;
    P.sportSelect($('#aSp'), () => load());
    SE.seg($('#aMin'), [[-0.02, 'Near misses', 'Also show sets up to 2% short of an arbitrage.'], [0, 'All arbitrage'], [0.01, '1%+'], [0.02, '2%+']], min, (v) => { min = +v; SE.setPref('arbMin', min); load(); });
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
      const real = all.filter((a) => a.profit > 0);
      P.set('aN', real.length);
      P.set('aB', real.length ? `<span class="up">+${(real[0].profit * 100).toFixed(2)}%</span>` : '-');
      P.set('aL', life.median_ms != null ? dur(life.median_ms) : '-', life.n ? `${life.n} finished · middle half ${dur(life.p25_ms)} to ${dur(life.p75_ms)}` : 'Learning: none finished yet');
      P.set('aNear', all.filter((a) => a.profit <= 0).length);
      $('#aScan').textContent = r.scanned_ms ? 'Scanned ' + SE.fmtAgo(r.scanned_ms) : 'First scan in a few seconds';
      t.set(all.filter((a) => a.profit >= min - 1e-9));
    }
    async function loadHist() {
      const h = await api('/arbs/history?limit=60');
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
      { key: 'l', label: 'Lasted', cls: 'r', w: 110, fmt: (x) => `${x.censored ? '≥ ' : ''}${dur(x.lasted_ms)}<div class="faint">${{ gone: 'closed', kickoff: 'kick-off', restart: 'restart' }[x.end_reason] || esc(x.end_reason || 'open')}</div>`, sort: (x) => x.lasted_ms },
    ], { sort: 'e', empty: 'No arbitrage has closed yet.', onClick: (x) => SE.go('#/match/' + x.match_id) });
    async function loadAll() {
      const h = await api('/arbs/history?limit=2000');
      let rows = h.recent;
      if (books.size) rows = rows.filter((x) => (x.books || '').split(',').every((b) => books.has(b)));
      if (!$('#aHn')) return;
      $('#aHn').textContent = `${rows.length} finished${books.size ? ' on your chosen sources' : ''}`;
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
