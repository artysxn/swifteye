// Swifteye pages, part 6: Bots. A control panel for the snipe bot (state, money over time, open
// trades, today's signals, controls), with the replay, the real-money setup and the arbitrage
// bot on their own tabs.
'use strict';
(() => {
const { $, $$, esc, ic, api, P } = SE;

const money = (x, d = 2) => (x == null || !isFinite(x) ? '-' : (x < 0 ? '-$' : '$') + Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }));
const signed = (x, d = 2) => (x == null || !isFinite(x) ? '-' : (x > 0 ? '+' : x < 0 ? '-' : '') + '$' + Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }));
const pnl = (x) => (x == null ? '<span class="faint">-</span>' : `<span class="${x > 0 ? 'up' : x < 0 ? 'down' : 'muted'}">${signed(x)}</span>`);
const pct = (x, d = 1) => (x == null || !isFinite(x) ? '-' : (x * 100).toFixed(d) + '%');
const cents = (x) => (x == null ? '…' : (x * 100).toFixed(1) + '¢');
const sw = (id, on, tip = '') => `<label class="switch" ${tip ? `data-tip="${esc(tip)}"` : ''}><input type="checkbox" id="${id}" ${on ? 'checked' : ''}><span></span></label>`;
const numIn = (id, v, step, tip, w = 110, pre = '', suf = '') => SE.num({ id, value: v ?? '', step, tip, w, pre, suf });
const row = (label, ctl, tip = '') => `<div class="set-row"><span ${tip ? `data-tip="${esc(tip)}"` : ''}>${label}</span><div>${ctl}</div></div>`;
const st = (ok, text) => `<span class="${ok === true ? 'st-ok' : ok === false ? 'st-bad' : 'st-off'}">${esc(text)}</span>`;
const dur = (ms) => {
  ms = Math.max(0, ms);
  const m = Math.floor(ms / 60000), h = Math.floor(m / 60);
  if (h >= 1) return `${h} h ${m % 60} min`;
  if (m >= 1) return `${m} min ${Math.floor(ms / 1000) % 60} s`;
  return `${Math.floor(ms / 1000)} s`;
};
// Coarse, for text that is redrawn often: minutes only.
const durMin = (ms) => { const m = Math.max(0, Math.round(ms / 60000)), h = Math.floor(m / 60); return h >= 1 ? `${h} h ${m % 60} min` : m >= 1 ? `${m} min` : 'under a minute'; };
const utcHM = (ms) => new Date(ms).toISOString().slice(11, 16) + ' UTC';
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const setName = (s) => (s === 'wider' ? 'wider set' : 'safe set');
const betName = (t) => `${t.dir > 0 ? 'Yes' : 'No'} ${t.outcome || ''}`;

// What the bot decided about a move, in plain words.
const SIG = {
  entered: 'Bought', move_too_small: 'Move too small', outside_hours: 'Outside trading hours', too_few_books: 'Only one feed moved', too_few_feeds: 'Only one feed moved',
  consensus_did_not_move: 'The market as a whole did not move', polymarket_does_not_lag: 'Polymarket is not behind in this game', polymarket_leads: 'Polymarket moved first',
  already_moved: 'Polymarket already moved', no_market_price: 'No Polymarket price', no_book_price: 'No bookmaker price', no_consensus: 'Too few bookmakers priced it',
  data_gap: 'Gap in the recorded prices', book_reverted: 'Bookmaker moved back', basketball: 'Live basketball (skipped)', price_too_high: 'Price above 90¢',
  price_above_set_limit: 'Price above the wider set\'s 40¢', gap_too_small: 'Expected gain too small', spread_too_wide: 'Spread too wide', price_out_of_range: 'Price too close to 0 or 100',
  price_ran: 'Price moved before the buy', no_cash: 'No free money for it', too_thin: 'Too little for sale', after_pause: 'Right after a pause in play', weak_feed: 'Triggered by a weak feed',
  wider_set_daily_stop: 'Wider set stopped for today', real_not_filled: 'Order did not fill', real_order_failed: 'Order failed', no_account_cash: 'No cash on the account',
  already_in_game: 'Real money already in this game', feed_quiet: 'Polymarket price feed went quiet',
  gone_at_match: 'Offer gone when the order matched', no_live_book: 'No live order book', no_offer: 'Nothing for sale', gap_usual_size: 'Gap is its usual size', swingy_game: 'Game swings too much',
};
const sigName = (k) => SIG[k] || k.replace(/_/g, ' ');

/**
 * An inline confirmation box (no browser dialogs). opts: { danger, title, body (html), type (text
 * the user must type), extra (html), ok (button label), onOk(box) }.
 */
function confirmBox(host, opts) {
  host.hidden = false;
  host.className = 'ask-box' + (opts.danger ? ' danger' : '');
  host.innerHTML = `<div class="q">${ic(opts.icon || 'help')}${opts.title}</div>${opts.text ? `<div class="a">${opts.text}</div>` : '<div style="height:10px"></div>'}<div class="row">` +
    (opts.extra || '') + (opts.type ? `<input class="input" data-c="t" placeholder="Type ${esc(opts.type)}" autocomplete="off" spellcheck="false">` : '') +
    `<button class="btn ${opts.danger ? 'danger' : 'primary'}" data-c="ok" ${opts.type ? 'disabled' : ''}>${opts.ok}</button><button class="btn ghost" data-c="no">Cancel</button></div>`;
  const close = () => { host.hidden = true; host.innerHTML = ''; };
  const ok = $('[data-c="ok"]', host), t = $('[data-c="t"]', host);
  if (t) {
    t.addEventListener('input', () => { ok.disabled = t.value.trim() !== opts.type; });
    t.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !ok.disabled) ok.click(); if (e.key === 'Escape') close(); });
    setTimeout(() => t.focus(), 0);
  }
  $('[data-c="no"]', host).addEventListener('click', close);
  ok.addEventListener('click', async () => { ok.disabled = true; try { await opts.onOk(host); } finally { close(); } });
  host.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  return close;
}

SE.pages.bots = {
  title: 'Bots',
  render(el) {
    let tab = SE.pref('botTab') || 'panel';
    if (!['panel', 'replay', 'setup', 'arb'].includes(tab)) tab = 'panel';
    el.innerHTML = P.head('Bots', `<div id="btTabs"></div><div id="bpMode" data-for="panel"></div><div id="bpRange" data-for="panel"></div><span id="bpActs"></span>`) +
      `<div id="bpAsk" hidden></div>` +
      // ---- control panel
      `<div data-tab="panel">` +
        `<div id="bpStats"></div>` +
        `<div class="grid g-main" style="margin-top:40px">` +
          P.sec('show_chart', '<span id="bpChartTitle">Balance</span><span id="bpChartTag"></span>', '<div class="chart-wrap" id="bpChart"></div>', '', 'Steps at every sale. Green dot = won, red = lost. Shaded band = trading hours. With paper and real together, each line is the profit since the start of the period.') +
          `<div>` + P.sec('hourglass_top', 'Open<span id="bpOpenTag"></span>', '<div id="bpOpen"></div>') + P.sec('bolt', 'Signals today', '<div id="bpSig"></div>', '<span class="muted" id="bpSigN"></span>', 'Every bookmaker move of 4+ points the bot checked today and what it decided. The same for paper and real.') + `</div>` +
        `</div>` +
        P.sec('insights', 'Projection<span id="bpProjTag"></span>', '<div id="bpProj"></div>', '<div id="bpProjSrc"></div><div id="bpProjH"></div>', 'Thousands of simulated futures from the chosen record: trades arrive at its rate, each with a return drawn from it, stakes as the settings say, daily stops applied. An estimate, not a promise.') +
        P.sec('receipt_long', 'Trades<span id="bpTradesTag"></span>', '<div id="bpTrades"></div>') +
        P.sec('tune', 'Settings', '<div class="set-grid" id="bpCtl"></div>', `<button class="btn primary" id="bpSave" hidden>${ic('save')}Save</button>`, 'Safe set: 2+ independent feeds moved 10+ points, the market as a whole moved, Polymarket lagged earlier in this game, price 90¢ or less, no live basketball. Wider set: the same with looser numbers, prices of 40¢ or less. Both sell when Polymarket catches up, or at the give-up time.') +
      `</div>` +
      // ---- replay
      `<div data-tab="replay" hidden>` +
        P.sec('science', 'Replay', '<div id="btSim"></div>', '<div id="btSimTab"></div>', 'The bot replayed over every recorded move, with the settings saved now.') +
        P.sec('compare_arrows', 'Live against replay', '<div id="btPar"></div>', '<div id="btParTab"></div><div id="btParH"></div>', 'Every trade the live bot made next to the replay\'s trade on the same move. If the two agree, the replay can be trusted. Trades only one side made show why.') +
      `</div>` +
      // ---- real-money setup
      `<div data-tab="setup" hidden>` +
        P.sec('key', 'Keys', '<div id="btKeys"></div>', '', 'Keys stay on this computer and are never shown in full.') +
        P.sec('account_balance', 'Accounts', '<div id="btAcc"></div>', '', 'Every real position the bots hold next to what the account holds.') +
        P.sec('shield', 'Shared limits', '<div id="btShared"></div>') +
        P.sec('timer', 'Test runs', '<div id="btRun"></div>') +
        P.sec('speed', 'Test orders', '<div id="btDiagC" hidden></div><div id="btDiag"></div>', `<button class="btn sm" id="tArb" data-tip="One real arbitrage pair at the smallest size, sold straight back.">${ic('science')}Arbitrage</button><button class="btn sm" id="tSn" data-tip="One real smallest Polymarket buy, sold straight back.">${ic('science')}Snipe</button>`, 'Real round trips at the smallest size, timed at every step.') +
        P.sec('list_alt', 'Real orders', '<div id="btOrd"></div>') +
        P.sec('notifications', 'Events', '<div id="btEv"></div>') +
      `</div>` +
      // ---- arbitrage bot
      `<div data-tab="arb" hidden>` +
        P.sec('balance', 'Arbitrage bot', '<div id="btArb"></div>', '<span id="btArbChip"></span>', 'Buys both sides across Polymarket and Kalshi when the pair costs under $1 after fees.') +
        P.sec('receipt_long', 'Positions', '<div id="btArT"></div>') +
      `</div>`;
    let d = null, pd = {}, runs = null;
    let view = null;                       // 'paper' | 'real' | 'both' (the wallets shown)
    let range = SE.pref('botRange') || 'all';
    let dirty = false;

    // ================================================================ tabs
    const loaded = {};
    function showTab(v) {
      tab = v;
      SE.setPref('botTab', v);
      $$('[data-tab]', el).forEach((x) => { x.hidden = x.dataset.tab !== v; });
      $$('[data-for]', el).forEach((x) => { x.hidden = x.dataset.for !== v; });
      if (v === 'replay' && !loaded.replay) { loaded.replay = 1; loadSim(true); loadParity(); }
      if (v === 'setup') { loadAcc(); loadDiag(); loadRun(); }
      if (v === 'panel') { chartSig = ''; drawPanel(); }
      draw();
    }
    SE.seg($('#btTabs'), [['panel', SE.ic('space_dashboard') + 'Panel'], ['replay', SE.ic('science') + 'Replay'], ['setup', SE.ic('key') + 'Setup'], ['arb', SE.ic('balance') + 'Arbitrage']], tab, showTab);

    const save = async (edit, msg = 'Saved') => {
      try { d = await api('/bots/settings', { body: edit }); SE.toast(msg); dirty = false; await load(); if (loaded.replay) loadSim(true); } catch (e) { SE.toast(e.message, 6000); }
    };

    // ================================================================ control panel: which money
    // pd = { paper?: panel, real?: panel } for the wallets shown; view = 'paper' | 'real' | 'both'.
    const wallets = () => (view === 'both' ? ['paper', 'real'] : [view || 'paper']);
    const anyP = () => wallets().map((w) => pd[w]).find(Boolean) || null;
    const tag = (w, sm = false) => (w === 'real'
      ? `<span class="wtag real ${sm ? 'sm' : ''}">${ic('payments')}${sm ? 'Real' : d?.settings.run ? 'Real money · test run' : 'Real money'}</span>`
      : `<span class="wtag paper ${sm ? 'sm' : ''}">${ic('edit_note')}${sm ? 'Paper' : 'Paper money'}</span>`);
    const tags = () => wallets().map((w) => tag(w)).join('');
    const allTrades = () => wallets().flatMap((w) => (pd[w]?.trades || []).map((t) => ({ ...t, wallet: w })));

    // ================================================================ control panel: header and state
    function drawState() {
      if (!d) return;
      const s = d.settings, sn = s.snipe, run = s.run, w = anyP()?.window || {};
      const miss = d.keys.missing_snipe || [];
      let lamp, word, foot;
      if (s.kill) { lamp = 'bad'; word = 'Paused'; foot = ''; }
      else if (!sn.enabled) { lamp = ''; word = 'Off'; foot = ''; }
      else if (w.open_now === false) { lamp = 'warn'; word = 'Waiting'; foot = `opens ${utcHM(w.next_open_ms)}`; }
      else { lamp = 'ok pulse'; word = 'Trading'; foot = w.closes_ms ? `until ${utcHM(w.closes_ms)}` : ''; }
      if (run && !s.kill) foot = `run ends ${utcHM(run.end_ms)}`;
      const money2 = sn.real || run ? 'real' : 'paper';
      const status = `<span class="hstat" data-tip="${esc(s.kill ? 'Kill switch on: no new trades. Open trades still sell.' : `Trades with ${money2 === 'real' ? 'real money' : 'paper money'} inside its hours.`)}"><span class="lamp ${lamp}"></span>${word}${foot ? `<span class="faint">${foot}</span>` : ''}${s.kill || !sn.enabled ? '' : tag(money2, true)}</span>`;
      const needs = miss.length ? `disabled data-tip="Needs: ${esc(miss.join(', '))}"` : '';
      const acts = status + (s.kill
        ? `<button class="btn primary" data-a="resume">${ic('play_arrow')}Resume</button>`
        : !sn.enabled
          ? `<button class="btn primary" data-a="on">${ic('power_settings_new')}Switch on</button>`
          : (run
            ? `<button class="btn" data-a="amount">${ic('payments')}Amount</button><button class="btn" data-a="extend">${ic('more_time')}Extend</button><button class="btn danger" data-a="stoprun">${ic('stop_circle')}End test run</button>`
            : sn.real
              ? `<button class="btn" data-a="paper">${ic('undo')}Back to paper</button>`
              : `<button class="btn" data-a="run" ${needs}>${ic('timer')}Real test run</button>`) +
            `<button class="btn danger" data-a="pause" data-tip="Stops every bot. Open trades still sell.">${ic('pause')}Pause</button>`);
      const host = $('#bpActs');
      if (!host || host.dataset.h === acts) return;
      host.dataset.h = acts;
      host.style.display = 'inline-flex';
      host.style.alignItems = 'center';
      host.style.gap = '8px';
      host.innerHTML = acts;
      $$('[data-a]', host).forEach((b) => b.addEventListener('click', () => ACT[b.dataset.a]?.()));
    }
    const ACT = {
      resume: () => save({ kill: false }, 'Resumed'),
      on: () => save({ snipe: { enabled: true } }, 'Snipe bot on'),
      pause: () => ask({ danger: true, icon: 'pause_circle', title: 'Pause every bot?', text: 'Open trades still sell.', ok: 'Pause', onOk: () => save({ kill: true }, 'Paused') }),
      paper: async () => { try { await api('/bots/real/snipe', { body: { real: false, confirm: '' } }); SE.toast('Back on paper'); load(); } catch (e) { SE.toast(e.message, 6000); } },
      real: () => ask({ danger: true, icon: 'payments', title: 'Trade real money?', text: `Real wallet ${money(d.settings.snipe.bankroll, 0)}, up to ${money(d.settings.snipe.r_max_trade, 0)} a trade, stops after −${money(d.settings.snipe.daily_loss, 0)}. No end time and no extra safety stops: the test run has both.`, type: 'REAL MONEY', ok: 'Switch to real',
        onOk: async () => { try { await api('/bots/real/snipe', { body: { real: true, confirm: 'REAL MONEY' } }); SE.toast('Real money on', 6000); setView('real'); } catch (e) { SE.toast(e.message, 7000); } } }),
      run: () => {
        let hours = 9;
        ask({ danger: true, icon: 'timer', title: 'Start a real test run?', text: `The real wallet with its own stakes: ${pct(d.settings.snipe.r_pct, 0)} a trade, at most ${money(d.settings.snipe.r_max_trade, 0)}. It stops itself after −${money(d.settings.snipe.daily_loss, 0)}, two losses in a row, or fills clearly worse than paper. The paper wallet trades every move beside it.`,
          extra: `<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">${SE.num({ id: 'bpRunB', value: d.settings.snipe.bankroll || 40, step: 5, min: 10, pre: '$', w: 110, tip: 'The real wallet for this run. At most the cash on your Polymarket account.' })}<div id="bpRunH"></div></div>`, type: 'REAL MONEY', ok: 'Start',
          onOk: async () => { try { await api('/bots/run/start', { body: { hours, bankroll: +$('#bpRunB').value || d.settings.snipe.bankroll || 40, confirm: 'REAL MONEY' } }); SE.toast(`${hours} h test run started`, 6000); setView('real'); } catch (e) { SE.toast(e.message, 7000); } } });
        SE.seg($('#bpRunH'), [[4, '4 h'], [8, '8 h'], [9, '9 h', 'One whole trading window (12:00 to 21:00 UTC).'], [16, '16 h'], [24, '24 h']], hours, (v) => { hours = +v; });
      },
      extend: () => {
        let h = 4;
        ask({ icon: 'more_time', title: 'Extend the test run', extra: '<div id="bpExtH"></div>', ok: 'Extend',
          onOk: async () => { try { await api('/bots/run/extend', { body: { hours: h } }); SE.toast(`Extended by ${h} h`); load(); } catch (e) { SE.toast(e.message, 6000); } } });
        SE.seg($('#bpExtH'), [[1, '1 h'], [2, '2 h'], [4, '4 h'], [8, '8 h']], h, (v) => { h = +v; });
      },
      amount: () => ask({ icon: 'payments', title: 'Real wallet', text: 'At most the cash on your Polymarket account. The run keeps its results.',
        extra: SE.num({ id: 'bpRunB2', value: d.settings.snipe.bankroll, step: 5, min: 10, pre: '$', w: 110 }), ok: 'Save',
        onOk: async () => { try { await api('/bots/run/bankroll', { body: { bankroll: +$('#bpRunB2').value } }); SE.toast('Amount saved'); load(); } catch (e) { SE.toast(e.message, 7000); } } }),
      stoprun: () => ask({ danger: true, icon: 'stop_circle', title: 'End the test run now?', text: 'Back to paper. Open trades still sell.', ok: 'End run',
        onOk: async () => { try { await api('/bots/run/stop', { method: 'POST' }); SE.toast('Test run ended'); load(); } catch (e) { SE.toast(e.message, 6000); } } }),
    };
    const ask = (o) => confirmBox($('#bpAsk'), o);

    // ================================================================ control panel: numbers (one row per wallet)
    const statH = (icon, label, value, foot, tip = '') => `<div class="stat" ${tip ? `data-tip="${esc(tip)}"` : ''}><div class="stat-label">${ic(icon)}${label}</div><div class="stat-value">${value}</div><div class="stat-foot">${foot}</div></div>`;
    const pctOf = (x, base) => (base > 0 ? `${x >= 0 ? '+' : '−'}${Math.abs(x / base * 100).toFixed(1)}%` : '');
    const longFor = (ms) => {
      const h = ms / 3600000;
      if (h < 1) return `${Math.max(1, Math.round(ms / 60000))} min`;
      if (h < 48) return `${Math.floor(h)} h ${Math.round((h % 1) * 60)} min`;
      return `${(h / 24).toFixed(1)} days`;
    };
    // When the wallet started: its own start (paper wallet, test run), else its first trade.
    const startedAt = (p) => (p.since > 0 ? p.since : p.trades.length ? Math.min(...p.trades.map((t) => t.ts)) : null);
    function drawNumbers() {
      const host = $('#bpStats');
      if (!host) return;
      host.innerHTML = wallets().map((w, i) => {
        const p = pd[w];
        if (!p) return '';
        const day0 = p.now - (p.now % 86400000);
        const closed = p.trades.filter((t) => t.closed_ms), today = closed.filter((t) => t.closed_ms >= day0), open = p.trades.filter((t) => !t.closed_ms);
        const ch = p.balance - p.start;
        // Evidence for the current rules: only trades made under them.
        const cur = closed.filter((t) => !t.old), old = closed.filter((t) => t.old);
        const won = cur.filter((t) => t.pnl > 0).length;
        const staked = cur.reduce((a, t) => a + (t.cost || 0), 0), gained = cur.reduce((a, t) => a + (t.pnl || 0), 0);
        const oldPnl = old.reduce((a, t) => a + (t.pnl || 0), 0);
        const t0 = startedAt(p);
        const cls = (x) => (x > 0 ? 'up' : x < 0 ? 'down' : '');
        return `<div class="wl-head" style="${i ? 'margin-top:22px' : ''}">${tag(w)}</div>` + `<div class="stats">` +
          statH('account_balance_wallet', 'Balance', money(p.balance), `from ${money(p.start)} · <span class="${cls(ch)}">${pctOf(ch, p.start)}</span>`, w === 'real' ? (p.scope === 'run' ? 'The test run\'s money and its results.' : 'The real bankroll and its results.') : 'The paper starting balance and its results.') +
          statH('payments', 'Profit', pnl(ch), old.length ? `${signed(oldPnl)} of it on old rules` : `<span class="${cls(ch)}">${pctOf(ch, p.start)}</span> of ${money(p.start, 0)}`, old.length ? `${old.length} trade${old.length === 1 ? '' : 's'} (${signed(oldPnl)}) ran on the rules before 19 Nov 05:26 UTC. They count in the money, not in the win rate or the projection.` : '') +
          statH('schedule', 'Running', t0 ? longFor(p.now - t0) : '<span class="faint">-</span>', t0 ? `since ${SE.fmtDateTime(t0)}` : 'no trades yet', 'Clock time since this wallet started (it trades only inside its hours).') +
          statH('emoji_events', 'Won', cur.length ? `${Math.round(100 * won / cur.length)}%` : '<span class="faint">-</span>', cur.length ? `${won} of ${cur.length} · ${pct(staked ? gained / staked : 0)} per $` : old.length ? `none yet on these rules (${old.length} old)` : 'no closed trades', 'Trades on the current rules only.') +
          statH('today', 'Today', pnl(p.today), `${today.length} trade${today.length === 1 ? '' : 's'} · UTC day`) +
          statH('hourglass_top', 'Open', String(open.length), open.length ? `${money(p.open_cost)} in play` : 'none') + `</div>`;
      }).join('');
      for (const id of ['bpChartTag', 'bpOpenTag', 'bpTradesTag']) if ($('#' + id)) $('#' + id).innerHTML = tags();
    }

    // ================================================================ control panel: balance chart
    let chart = null, chartSig = '', chartMeta = [];
    const WCOL = { paper: () => css('--text-2'), real: () => css('--warn') };
    function drawChart() {
      const host = $('#bpChart');
      const ws = wallets().filter((w) => pd[w]);
      if (!host || !ws.length) return;
      const now = Math.max(...ws.map((w) => pd[w].now));
      const day0 = now - (now % 86400000);
      const both = ws.length > 1;
      const closedOf = (w) => pd[w].trades.filter((t) => t.closed_ms).sort((a, b) => a.closed_ms - b.closed_ms).map((t) => ({ ...t, wallet: w }));
      const all = ws.flatMap(closedOf).sort((a, b) => a.closed_ms - b.closed_ms);
      const first = all.length ? Math.min(...all.map((t) => t.ts)) : now - 3600000;
      let from = range === 'today' ? day0 : range === 'week' ? now - 7 * 86400000 : Math.min(first - 600000, ...ws.map((w) => (pd[w].since > 0 ? pd[w].since : Infinity)));
      if (range === 'all' && !all.length) from = Math.max(from, now - 6 * 3600000);
      if (!isFinite(from)) from = now - 6 * 3600000;
      // Value per wallet at each sale: balance (one wallet) or profit since the period start (both).
      const base = {}, cur = {};
      for (const w of ws) {
        let b = pd[w].start;
        for (const t of closedOf(w)) if (t.closed_ms < from) b += t.pnl || 0;
        base[w] = b; cur[w] = b;
      }
      const val = (w) => (both ? cur[w] - base[w] : cur[w]);
      const xs = [from], line = Object.fromEntries(ws.map((w) => [w, [val(w)]])), win = [null], loss = [null], meta = [null];
      let n = 0;
      for (const t of all) {
        if (t.closed_ms < from) continue;
        cur[t.wallet] += t.pnl || 0; n++;
        xs.push(Math.max(t.closed_ms, xs[xs.length - 1] + 1));
        for (const w of ws) line[w].push(val(w));
        win.push(t.pnl > 0 ? val(t.wallet) : null); loss.push(t.pnl > 0 ? null : val(t.wallet)); meta.push(t);
      }
      xs.push(Math.max(now, xs[xs.length - 1] + 1));
      for (const w of ws) line[w].push(val(w));
      win.push(null); loss.push(null); meta.push(null);
      const sig = [ws.join(), range, all.length, all.length ? all[all.length - 1].closed_ms : 0, ...ws.map((w) => pd[w].start), Math.floor(now / 300000), host.clientWidth, document.documentElement.dataset.theme].join('|');
      if (sig === chartSig && chart) return;
      chartSig = sig; chartMeta = meta;
      if (chart) { chart._ro?.disconnect(); chart.destroy(); chart = null; }
      host.innerHTML = '';
      const ref = both ? 0 : base[ws[0]];
      const vals = ws.flatMap((w) => line[w]);
      const lo = Math.min(...vals, ref), hi = Math.max(...vals, ref), pad = Math.max((hi - lo) * 0.18, Math.abs(ref) * 0.01, 0.5);
      const axis = { stroke: css('--text-3'), grid: { stroke: css('--grid'), width: 1 }, ticks: { show: false }, font: '11px ' + css('--font') };
      const hours = anyP()?.window?.hours;
      const fmtY = (v) => (both ? signed(v, hi - lo < 20 ? 2 : 0) : money(v, hi - lo < 20 ? 2 : 0));
      const one = ws[0], up = !both && cur[one] >= base[one];
      chart = new uPlot({
        width: host.clientWidth || 600, height: 260, ms: 1, legend: { show: false },
        cursor: { drag: { x: false, y: false }, points: { size: 9 } },
        scales: { x: { time: true }, y: { range: [lo - pad, hi + pad] } },
        axes: [
          { ...axis, values: (u, ticks) => { const span = (u.scales.x.max ?? 0) - (u.scales.x.min ?? 0); return ticks.map((t) => (span > 2 * 86400000 ? SE.fmtDateTime(t).split(' ').slice(0, 2).join(' ') : SE.fmtTime(t).slice(0, 5))); } },
          { ...axis, size: 70, values: (u, ticks) => ticks.map(fmtY) },
        ],
        series: [
          {},
          ...ws.map((w) => ({ stroke: both ? WCOL[w]() : css(up ? '--up' : '--down'), width: 2, paths: uPlot.paths.stepped({ align: 1 }), points: { show: false } })),
          { stroke: css('--text-3'), width: 1, dash: [4, 4], points: { show: false } },
          { stroke: css('--up'), width: 0, paths: () => null, points: { show: true, size: 8, fill: css('--up'), stroke: css('--bg'), width: 1.5 } },
          { stroke: css('--down'), width: 0, paths: () => null, points: { show: true, size: 8, fill: css('--down'), stroke: css('--bg'), width: 1.5 } },
        ],
        hooks: {
          drawClear: [(u) => {
            const { top, height } = u.bbox, c = u.ctx, x0 = u.scales.x.min, x1 = u.scales.x.max;
            if (x0 == null || x1 == null) return;
            // Where the current rules start.
            const rs = anyP()?.rules_since;
            if (rs && rs > x0 && rs < x1) {
              const x = Math.round(u.valToPos(rs, 'x', true)), dpr = devicePixelRatio || 1;
              c.save();
              c.strokeStyle = css('--accent'); c.lineWidth = dpr; c.setLineDash([4 * dpr, 4 * dpr]);
              c.beginPath(); c.moveTo(x + 0.5, top); c.lineTo(x + 0.5, top + height); c.stroke();
              c.setLineDash([]); c.font = `${11 * dpr}px ${css('--font')}`; c.fillStyle = css('--accent'); c.textBaseline = 'top'; c.textAlign = 'left';
              c.fillText('current rules', x + 5 * dpr, top + 4 * dpr);
              c.restore();
            }
            if (!hours) return;
            c.save(); c.fillStyle = 'rgba(128,128,128,0.07)';
            for (let day = Math.floor(x0 / 86400000) * 86400000; day < x1; day += 86400000) {
              const a = Math.max(day + hours[0] * 3600000, x0), b = Math.min(day + hours[1] * 3600000, x1);
              if (b > a) c.fillRect(u.valToPos(a, 'x', true), top, u.valToPos(b, 'x', true) - u.valToPos(a, 'x', true), height);
            }
            c.restore();
          }],
          drawAxes: [(u) => {
            if (both) return;
            const c = u.ctx, yb = u.valToPos(ref, 'y', true), ys = line[one];
            c.save();
            for (let i = 0; i < xs.length - 1; i++) {
              if (ys[i] === ref) continue;
              const x0 = u.valToPos(xs[i], 'x', true), x1 = u.valToPos(xs[i + 1], 'x', true), y = u.valToPos(ys[i], 'y', true);
              c.fillStyle = css(ys[i] > ref ? '--up-soft' : '--down-soft');
              c.fillRect(x0, Math.min(y, yb), x1 - x0, Math.abs(y - yb));
            }
            c.restore();
          }],
          setCursor: [(u) => {
            const i = u.cursor.idx;
            if (i == null || u.cursor.left < 0) { SE.tipHide(); return; }
            const t = chartMeta[i], rect = u.over.getBoundingClientRect();
            const rows = ws.map((w, k) => `<div class="row"><span>${both ? tag(w, true) : 'Balance'}</span><b>${fmtY(u.data[k + 1][i])}</b></div>`).join('');
            SE.tipShow((t
              ? `<div class="t">${SE.fmtDateTime(t.closed_ms)}${both ? ' · ' + tag(t.wallet, true) : ''}</div><div class="row"><b>${esc(t.home || '')} v ${esc(t.away || '')}</b></div><div class="row"><span>${esc(betName(t))} · ${cents(t.entry)} → ${cents(t.exit)}</span><b class="${t.pnl > 0 ? 'up' : 'down'}">${signed(t.pnl)}</b></div>`
              : `<div class="t">${SE.fmtDateTime(u.data[0][i])}</div>`) + rows, rect.left + u.cursor.left, rect.top + u.cursor.top);
          }],
        },
      }, [xs, ...ws.map((w) => line[w]), xs.map(() => ref), win, loss], host);
      chart.over.addEventListener('mouseleave', SE.tipHide);
      const ro = new ResizeObserver(() => { if (host.clientWidth) chart?.setSize({ width: host.clientWidth, height: 260 }); });
      ro.observe(host);
      chart._ro = ro;
      $('#bpChartTitle').textContent = both ? 'Profit' : 'Balance';
      if (!n) host.insertAdjacentHTML('beforeend', `<div class="over">No trades ${range === 'today' ? 'today' : range === 'week' ? 'in 7 days' : 'yet'}</div>`);
    }

    // ================================================================ control panel: lists
    function drawOpen() {
      const host = $('#bpOpen');
      if (!host || !anyP()) return;
      const open = allTrades().filter((t) => !t.closed_ms);
      if (!open.length) { host.innerHTML = `<div class="empty">${ic('hourglass_empty')}Nothing open</div>`; return; }
      host.innerHTML = open.map((t) => {
        const e = t.entry, tgt = t.target, bid = t.bid_now;
        const p = bid != null && tgt != null && tgt > e ? Math.max(-0.33, Math.min(1, (bid - e) / (tgt - e))) : 0;
        const x = 25 + 75 * p;
        const bar = x >= 25 ? `left:25%;width:${x - 25}%;background:var(--up)` : `left:${x}%;width:${25 - x}%;background:var(--down)`;
        const now = t.value_now != null ? t.value_now - t.cost : null;
        return `<div class="li"><span class="tile on">${ic(t.dir > 0 ? 'trending_up' : 'trending_down')}</span><div class="main">` +
          `<div class="nm"><a href="#/match/${+t.match_id}">${esc(t.home || '')} v ${esc(t.away || '')}</a></div>` +
          `<div class="sb">${view === 'both' ? tag(t.wallet, true) + ' ' : ''}${esc(betName(t))} · ${cents(e)} → ${cents(tgt)} · <span data-until="${t.give_up_ms || ''}"></span></div>` +
          `<div class="prog" data-tip="Bought ${cents(e)} · now ${cents(bid)} · sells at ${cents(tgt)}"><i style="${bar}"></i><u style="left:25%"></u></div></div>` +
          `<div class="end">${pnl(now)}</div></div>`;
      }).join('');
      tick();
    }
    function drawSignals() {
      const host = $('#bpSig');
      const p = anyP();
      if (!host || !p) return;
      const rows = p.signals || [], total = rows.reduce((a, x) => a + x.n, 0);
      $('#bpSigN').textContent = total ? total.toLocaleString() : '';
      if (!total) { host.innerHTML = `<div class="empty">${ic('bolt')}No signals yet today</div>`; return; }
      const bought = rows.find((x) => x.result === 'entered')?.n || 0;
      const top = rows.filter((x) => x.result !== 'entered').slice(0, 6), max = Math.max(...rows.map((x) => x.n), 1);
      const bar = (label, n, hit) => `<div class="mbar ${hit ? 'hit' : ''}"><span class="t">${esc(label)}</span><span class="b"><i style="width:${Math.max(2, 100 * n / max)}%"></i></span><b>${n.toLocaleString()}</b></div>`;
      host.innerHTML = bar('Bought', bought, true) + top.map((x) => bar(sigName(x.result), x.n)).join('');
    }
    let tradeTbl = null;
    function drawTrades() {
      if (!anyP() || !$('#bpTrades')) return;
      const now = Date.now();
      if (!tradeTbl) tradeTbl = SE.table($('#bpTrades'), [
        ...(view === 'both' ? [{ key: 'w', label: 'Wallet', w: 90, fmt: (t) => tag(t.wallet, true), sort: (t) => t.wallet }] : []),
        { key: 't', label: 'When', w: 130, fmt: (t) => `<span class="muted">${SE.fmtDateTime(t.ts)}</span>`, sort: (t) => t.ts },
        { key: 'm', label: 'Match', cls: 'match', fmt: (t) => P.matchCell(t.home, t.away, '', t.sport, t.match_id) },
        { key: 'o', label: 'Bet', w: 120, fmt: (t) => esc(betName(t)) },
        { key: 'k', label: 'Rules', w: 90, fmt: (t) => (t.old ? '<span class="faint" data-tip="Before the current rules (19 Nov 05:26 UTC)">old rules</span>' : `<span class="muted">${t.set === 'wider' ? 'wider set' : 'safe set'}</span>`) },
        { key: 's', label: 'Stake', cls: 'r', w: 90, fmt: (t) => money(t.cost), sort: (t) => t.cost },
        { key: 'e', label: 'Price', cls: 'r', w: 130, fmt: (t) => `${cents(t.entry)} → ${t.closed_ms ? cents(t.exit) : cents(t.bid_now)}` },
        { key: 'h', label: 'Held', cls: 'r', w: 80, fmt: (t) => (t.closed_ms ? durMin(t.closed_ms - t.ts) : '<span class="up">open</span>'), sort: (t) => (t.closed_ms || now) - t.ts },
        { key: 'p', label: 'Result', cls: 'r', w: 100, fmt: (t) => (t.closed_ms ? pnl(t.pnl) : pnl(t.value_now != null ? t.value_now - t.cost : null)), sort: (t) => t.pnl ?? 0 },
      ], { sort: 't', limit: 300, empty: 'No trades yet', emptyIcon: 'receipt_long', rowTip: (t) => (t.closed_ms ? (t.reason || '') : 'open') });
      tradeTbl.set(allTrades());
    }

    // ================================================================ control panel: projection
    // Monte Carlo: trades arrive at the measured rate; each one's return per $ is drawn from the
    // chosen record (the wallet's own closed trades, or the replay of the current setup); stakes
    // follow the settings on the balance of that path; the daily stops apply.
    let projSrc = SE.pref('botProjSrc') || 'actual', projH = +(SE.pref('botProjH') || 7), projSig = '', projChart = null, replayCache = null, replayWait = null;
    SE.seg($('#bpProjSrc'), [['actual', ic('history') + 'Its results', 'The closed trades of the wallet shown.'], ['replay', ic('science') + 'Replay', 'The replay of the current setup on all recorded data.']], projSrc, (v) => { projSrc = v; SE.setPref('botProjSrc', v); projSig = ''; drawProj(); });
    SE.seg($('#bpProjH'), [[1, '1 day'], [3, '3 days'], [7, '1 week'], [30, '1 month']], projH, (v) => { projH = +v; SE.setPref('botProjH', v); projSig = ''; drawProj(); });
    const projWallet = () => (view === 'paper' ? 'paper' : 'real');
    async function replayRecord() {
      if (replayCache && Date.now() - replayCache.at < 600000) return replayCache;
      try {
        const r = await api('/bots/sim?kind=snipe-plan');
        if (r.result?.list) { replayCache = { at: Date.now(), list: r.result.list, days: r.result.days || 1 }; return replayCache; }
        clearTimeout(replayWait);
        replayWait = setTimeout(() => { projSig = ''; drawProj(); }, 1500);
      } catch (e) { /* try again on the next refresh */ }
      return null;
    }
    function poisson(m) {
      if (m <= 0) return 0;
      if (m > 40) return Math.max(0, Math.round(m + Math.sqrt(m) * gauss()));
      const L = Math.exp(-m);
      let k = 0, p = 1;
      do { k++; p *= Math.random(); } while (p > L);
      return k - 1;
    }
    function gauss() { let u = 0, v = 0; while (!u) u = Math.random(); while (!v) v = Math.random(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }
    function simulate(samples, perDay, start, cfg, days) {
      const PATHS = 3000;
      const step = days <= 1 ? 1 / 24 : days <= 7 ? 0.25 : 1;
      const steps = Math.round(days / step);
      const safe = samples.filter((s) => s.set !== 'wider'), wide = samples.filter((s) => s.set === 'wider');
      const shareWide = cfg.wider && wide.length ? wide.length / samples.length : 0;
      const pool = shareWide ? samples : safe.length ? safe : samples;
      const cols = Array.from({ length: steps + 1 }, () => new Float64Array(PATHS));
      for (let p = 0; p < PATHS; p++) {
        let bal = start, day = -1, dayPnl = 0, wideDay = 0;
        cols[0][p] = bal;
        for (let k = 1; k <= steps; k++) {
          const t0 = (k - 1) * step, dIdx = Math.floor(t0 + 1e-9);
          if (dIdx !== day) { day = dIdx; dayPnl = 0; wideDay = 0; }
          const n = poisson(perDay * step);
          for (let j = 0; j < n; j++) {
            if (cfg.stop > 0 && dayPnl <= -cfg.stop) break;
            const s = pool[(Math.random() * pool.length) | 0];
            const wider = s.set === 'wider';
            if (wider && (!cfg.wider || (cfg.bstop > 0 && wideDay <= -cfg.bstop))) continue;
            const stake = wider ? Math.min(cfg.bpct * bal, cfg.bmax) : Math.min(cfg.pct * bal * cfg.k, cfg.max);
            if (stake < 10 || stake > bal) continue;
            const g = stake * s.r;
            bal += g; dayPnl += g;
            if (wider) wideDay += g;
          }
          cols[k][p] = bal;
        }
      }
      const q = (a, f) => a[Math.min(a.length - 1, Math.max(0, Math.round(f * (a.length - 1))))];
      return {
        step, steps, n: PATHS,
        rows: cols.map((c) => {
          const a = Float64Array.from(c).sort();
          let m = 0;
          for (const v of a) m += v;
          return { min: a[0], p10: q(a, 0.1), p50: q(a, 0.5), mean: m / a.length, p90: q(a, 0.9), max: a[a.length - 1], below: ((i) => (i < 0 ? 1 : i / a.length))(a.findIndex((v) => v >= start)) };
        }),
      };
    }
    async function drawProj() {
      const host = $('#bpProj');
      if (!host || !d || tab !== 'panel') return;
      const w = projWallet(), p = pd[w] || anyP();
      $('#bpProjTag').innerHTML = tag(w);
      if (!p) return;
      const s = d.settings, sn = s.snipe, run = s.run;
      const set = w === 'paper' && run ? run.prev : sn;
      const cfg = { pct: set.pct, max: set.max_trade, k: sn.edge_sizing ? 1.5 : 1, wider: sn.b_enabled && !(w === 'real' && run), bpct: sn.b_pct, bmax: sn.b_max_trade, bstop: sn.b_daily_loss, stop: w === 'real' ? set.daily_loss : 0 };
      let samples, perDay, label;
      if (projSrc === 'replay') {
        const rec = await replayRecord();
        if (!rec) { host.innerHTML = `<div class="empty"><span class="skeleton">Replaying the current setup</span></div>`; return; }
        samples = rec.list.filter((t) => t.spent > 0).map((t) => ({ r: t.pnl / t.spent, set: t.set }));
        perDay = rec.list.length / Math.max(rec.days, 0.25);
        label = `replay: ${rec.list.length} trades over ${rec.days.toFixed(1)} days`;
      } else {
        const all = wallets().flatMap((x) => (pd[x]?.trades || [])).filter((t) => t.closed_ms && t.cost > 0);
        const tr = all.filter((t) => !t.old), nOld = all.length - tr.length;
        if (!tr.length) { projSig = ''; host.innerHTML = `<div class="empty">${ic('insights')}No closed trades on the current rules yet${nOld ? ` (${nOld} on the old rules are left out)` : ''}. Use Replay meanwhile.</div>`; return; }
        samples = tr.map((t) => ({ r: t.pnl / t.cost, set: t.set }));
        const span = (Date.now() - Math.max(Math.min(...tr.map((t) => t.ts)), (anyP()?.rules_since || 0))) / 86400000;
        perDay = tr.length / Math.max(span, 1);
        label = `${view === 'both' ? 'paper + real' : w}: ${tr.length} closed trade${tr.length === 1 ? '' : 's'} on the current rules${nOld ? ` (${nOld} old left out)` : ''}`;
      }
      if (!samples.length) { host.innerHTML = `<div class="empty">${ic('insights')}No closed trades to project from yet</div>`; return; }
      const sig = [projSrc, projH, w, samples.length, samples.reduce((a, x) => a + x.r, 0).toFixed(6), p.balance.toFixed(2), JSON.stringify(cfg), host.clientWidth, document.documentElement.dataset.theme].join('|');
      if (sig === projSig) return;
      projSig = sig;
      const start = p.balance;
      const res = simulate(samples, perDay, start, cfg, projH);
      const end = res.rows[res.rows.length - 1];
      const span = { 1: '1 day', 3: '3 days', 7: '1 week', 30: '1 month' }[projH] || `${projH} days`;
      const pc = (v) => { const x = start > 0 ? (v / start - 1) * 100 : 0; return `${x >= 0 ? '+' : '−'}${Math.abs(x) >= 100 ? Math.round(Math.abs(x)).toLocaleString() : Math.abs(x).toFixed(1)}%`; };
      const card = (icon, name, v, tip) => statH(icon, name, money(v), `<span class="${v > start ? 'up' : v < start ? 'down' : ''}">${pc(v)}</span> in ${span}`, `${tip} ${signed(v - start)} from ${money(start)}.`);
      const endAt = Date.now() + projH * 86400000;
      host.innerHTML = `<div class="stats">` +
        statH('flag', 'Start', money(start), `now → ${SE.fmtDateTime(endAt).split(' ').slice(0, 2).join(' ')}`, `Today's balance of the ${w === 'real' ? 'real' : 'paper'} wallet. Each scenario is where it ends ${span} later.`) +
        card('south', 'Worst case', end.min, `The worst of ${res.n.toLocaleString()} simulated runs, from ${money(start)}.`) +
        card('trending_down', 'Worst 10%', end.p10, '1 run in 10 ends at or below this.') +
        card('functions', 'Average', end.mean, `The mean of all runs. Median ${money(end.p50)}. ${Math.round(end.below * 100)}% of runs end below the start.`) +
        card('trending_up', 'Best 10%', end.p90, '1 run in 10 ends at or above this.') +
        card('north', 'Best case', end.max, `The best of ${res.n.toLocaleString()} simulated runs.`) +
        `</div><div class="chart-wrap" id="bpProjChart" style="margin-top:16px"></div><div class="proj-foot"><span>${money(start)} today, ${span} ahead · based on ${esc(label)} · ${perDay.toFixed(1)} trades a day · ${res.n.toLocaleString()} runs</span><span>${Math.round(end.below * 100)}% of runs end below ${money(start)}</span></div>`;
      const ch = $('#bpProjChart');
      const t0 = Date.now(), xs = res.rows.map((_, i) => t0 + i * res.step * 86400000);
      const col = (k) => res.rows.map((r) => r[k]);
      const all = res.rows.flatMap((r) => [r.min, r.max]);
      const lo = Math.min(...all, start), hi = Math.max(...all, start), pad = Math.max((hi - lo) * 0.12, 1);
      const axis = { stroke: css('--text-3'), grid: { stroke: css('--grid'), width: 1 }, ticks: { show: false }, font: '11px ' + css('--font') };
      SE.destroyChart(projChart);
      const names = { min: 'Worst case', p10: 'Worst 10%', p50: 'Median', mean: 'Average', p90: 'Best 10%', max: 'Best case' };
      const keys = ['max', 'p90', 'mean', 'p50', 'p10', 'min'];
      projChart = new uPlot({
        width: ch.clientWidth || 600, height: 260, ms: 1, legend: { show: false },
        cursor: { drag: { x: false, y: false }, points: { size: 7 } },
        scales: { x: { time: true }, y: { range: [Math.max(0, lo - pad), hi + pad] } },
        axes: [
          { ...axis, values: (u, ticks) => ticks.map((t) => (projH > 2 ? SE.fmtDateTime(t).split(' ').slice(0, 2).join(' ') : SE.fmtTime(t).slice(0, 5))) },
          { ...axis, size: 70, values: (u, ticks) => ticks.map((v) => money(v, 0)) },
        ],
        series: [
          {},
          { stroke: css('--text-3'), width: 1, dash: [3, 4], points: { show: false } },
          { stroke: css('--text-2'), width: 1.2, points: { show: false } },
          { stroke: css('--accent'), width: 1.5, dash: [6, 4], points: { show: false } },
          { stroke: css('--accent'), width: 2.2, points: { show: false } },
          { stroke: css('--text-2'), width: 1.2, points: { show: false } },
          { stroke: css('--text-3'), width: 1, dash: [3, 4], points: { show: false } },
          { stroke: css('--text-3'), width: 1, dash: [2, 3], points: { show: false } },
        ],
        hooks: {
          drawAxes: [(u) => {
            // The middle 80% of runs, shaded.
            const c = u.ctx;
            c.save();
            c.fillStyle = css('--accent-soft');
            c.beginPath();
            res.rows.forEach((r, i) => { const x = u.valToPos(xs[i], 'x', true), y = u.valToPos(r.p90, 'y', true); if (i) c.lineTo(x, y); else c.moveTo(x, y); });
            for (let i = res.rows.length - 1; i >= 0; i--) c.lineTo(u.valToPos(xs[i], 'x', true), u.valToPos(res.rows[i].p10, 'y', true));
            c.closePath();
            c.fill();
            c.restore();
          }],
          setCursor: [(u) => {
            const i = u.cursor.idx;
            if (i == null || u.cursor.left < 0) { SE.tipHide(); return; }
            const rect = u.over.getBoundingClientRect(), r = res.rows[i];
            SE.tipShow(`<div class="t">${SE.fmtDateTime(xs[i])}</div>` + keys.map((k) => `<div class="row"><span>${names[k]}</span><b class="${r[k] > start ? 'up' : r[k] < start ? 'down' : ''}">${money(r[k])}</b></div>`).join(''), rect.left + u.cursor.left, rect.top + u.cursor.top);
          }],
        },
      }, [xs, ...keys.map(col), xs.map(() => start)], ch);
      projChart.over.addEventListener('mouseleave', SE.tipHide);
      const ro = new ResizeObserver(() => { if (ch.clientWidth) projChart?.setSize({ width: ch.clientWidth, height: 260 }); });
      ro.observe(ch);
      projChart._ro = ro;
    }

    // ================================================================ control panel: settings
    function drawControls() {
      const host = $('#bpCtl');
      if (!host || !d || dirty || host.contains(document.activeElement)) return;
      const s = d.settings, run = s.run, sn = s.snipe, w = run ? run.prev : sn;
      const f = (icon, label, ctl, tip) => `<div class="field"><label ${tip ? `data-tip="${esc(tip)}"` : ''}>${ic(icon)}${label}</label><div class="in">${ctl}</div></div>`;
      const n = (k, v, step = 1, pre = '', suf = '', w = 92) => SE.num({ attr: `data-k="${k}"`, value: v, step, pre, suf, w });
      // Two wallets, nothing shared: paper (left), real (middle); the rules both follow (right).
      host.innerHTML =
        `<div>` +
          f('savings', 'Paper balance', n('start', w.start, 50, '$'), 'The paper wallet. Changing it starts the paper wallet afresh.') +
          f('percent', 'Paper stake', n('pct', +(w.pct * 100).toFixed(1), 1, '', '%'), 'Share of the paper balance on a safe-set trade.') +
          f('payments', 'Paper max per trade', n('max_trade', w.max_trade, 5, '$')) +
          f('stacks', 'Paper max open', n('max_open', w.max_open, 10, '$')) +
          f('percent', 'Wider stake', n('b_pct', +(sn.b_pct * 100).toFixed(1), 1, '', '%'), 'The wider set trades on the paper wallet.') +
          f('payments', 'Wider max', n('b_max_trade', sn.b_max_trade, 1, '$')) +
        `</div><div>` +
          f('account_balance', 'Real wallet', n('bankroll', sn.bankroll, 5, '$'), 'The real bot\'s own money inside your Polymarket account. Never more than the cash on the account. Changing it starts the real wallet afresh.') +
          f('percent', 'Real stake', n('r_pct', +(sn.r_pct * 100).toFixed(1), 1, '', '%'), 'Share of the real wallet on a trade. Its own number, never the paper stake.') +
          f('payments', 'Real max per trade', n('r_max_trade', sn.r_max_trade, 1, '$'), 'Most real dollars in one trade.') +
          f('shield', 'Real loss stop', n('daily_loss', sn.daily_loss, 5, '$'), 'Real money goes back to paper once it has lost this much.') +
          f('payments', 'Real money', sw('ctlReal', sn.real, 'Asks for a typed confirmation.')) +
        `</div><div>` +
          f('tune', 'Size by edge', sw('ctlEdge', sn.edge_sizing), 'Bigger stake when the expected gain is bigger (½× to 3×), up to each wallet\'s max per trade.') +
          f('call_split', 'Wider set', sw('ctlB', sn.b_enabled), 'More trades at a lower win rate, small stakes. Paper only.') +
          f('block', 'Wider daily stop', n('b_daily_loss', sn.b_daily_loss, 5, '$'), 'The wider set stops for the day after losing this much. 0 = never.') +
          f('timer_off', 'Give up after', n('give', Math.round((sn.give_up_s || 900) / 60), 1, '', 'min'), 'An unsold trade is sold at this point.') +
          f('schedule', 'Hours (UTC)', n('h0', sn.hours_utc?.[0] ?? 12, 1, '', ':00', 76) + '<span class="faint">to</span>' + n('h1', sn.hours_utc?.[1] ?? 21, 1, '', ':00', 76), 'Trades only between these hours. Same number twice = all day.') +
        `</div>`;
      const mark = () => { dirty = true; $('#bpSave').hidden = false; };
      $$('input[type=number]', host).forEach((i) => i.addEventListener('input', mark));
      ['ctlEdge', 'ctlB'].forEach((id) => $('#' + id).addEventListener('change', mark));
      $('#ctlReal').addEventListener('change', (e) => { e.target.checked = sn.real; if (sn.real) ACT.paper(); else if (run) SE.toast('A test run is going'); else ACT.real(); });
    }
    $('#bpSave').addEventListener('click', async () => {
      const g = (k) => +$(`#bpCtl [data-k="${k}"]`).value;
      await save({ snipe: {
        pct: g('pct') / 100, max_trade: g('max_trade'), edge_sizing: $('#ctlEdge').checked,
        b_enabled: $('#ctlB').checked, b_pct: g('b_pct') / 100, b_max_trade: g('b_max_trade'), b_daily_loss: g('b_daily_loss'),
        daily_loss: g('daily_loss'), max_open: g('max_open'), bankroll: g('bankroll'), start: g('start'), r_pct: g('r_pct') / 100, r_max_trade: g('r_max_trade'),
        hours_utc: [g('h0'), g('h1')], give_up_s: Math.round(g('give') * 60),
      } }, 'Saved');
      $('#bpSave').hidden = true;
      drawControls();
    });

    function drawPanel() {
      if (!d) return;
      drawState();
      if (tab !== 'panel' || !anyP()) return;
      drawNumbers(); drawChart(); drawOpen(); drawSignals(); drawTrades(); drawControls(); drawProj();
    }
    function tick() {
      $$('[data-until]', el).forEach((x) => { const t = +x.dataset.until; x.textContent = t ? `${durMin(t - Date.now())} left` : ''; });
    }
    function setView(v) { view = v; modeSeg = ''; SE.setPref('botView', v); chartSig = ''; projSig = ''; tradeTbl = null; pd = {}; load(); }
    SE.seg($('#bpRange'), [['today', 'Today'], ['week', '7 days'], ['all', 'All']], range, (v) => { range = v; SE.setPref('botRange', v); chartSig = ''; drawChart(); });

    // ================================================================ replay
    let simKind = SE.pref('botSim') || 'snipe-plan', simTimer = null, simChart = null, simSeq = 0;
    const SIMS = {
      'snipe-plan': ['This setup', 'Both rule sets with the stakes saved now, the rules picked before each 4-hour window. What the bot would have made.'],
      'snipe-plan-safe': ['Safe set only', 'The same without the wider set.'],
      'snipe-current': ['Today\'s rules', 'Today\'s rules over all the data. In-sample: the rules were picked on this same data, so this flatters them.'],
      'arb': ['Arbitrage bot', 'Every recorded Polymarket and Kalshi quote of every game both listed, traded as the arbitrage bot trades.'],
    };
    if (!SIMS[simKind]) simKind = 'snipe-plan';
    SE.seg($('#btSimTab'), Object.entries(SIMS).map(([k, v]) => [k, v[0], v[1]]), simKind, (v) => { simKind = v; SE.setPref('botSim', v); loadSim(true); });
    function simLoading(r) {
      const p = r?.progress || {};
      const known = p.total > 0;
      const share = known ? Math.min(1, p.done / p.total) : 0;
      const what = simKind === 'arb' ? (known ? `Replaying quotes: game ${p.done.toLocaleString()} of ${p.total.toLocaleString()}` : 'Loading every recorded quote') : 'Replaying every recorded move with these settings';
      SE.destroyChart(simChart); simChart = null;
      $('#btSim').innerHTML = `<div class="sim-load"><div class="lbl"><span class="dot"></span>${esc(what)}${known ? ` · ${Math.round(share * 100)}%` : ''}</div>` +
        `<div class="sim-bar ${known ? '' : 'indet'}"><i style="${known ? `width:${(share * 100).toFixed(1)}%` : ''}"></i></div>` +
        `<div class="sk-row"><div class="sk"></div><div class="sk"></div><div class="sk"></div><div class="sk"></div><div class="sk"></div></div><div class="sk chart"></div></div>`;
    }
    function simDraw(r) {
      SE.destroyChart(simChart); simChart = null;
      const x = r.result;
      if (!x) { $('#btSim').innerHTML = `<div class="empty">${esc(r.error || 'No result')}</div>`; return; }
      if (x.building) { $('#btSim').innerHTML = '<div class="empty">The recorded moves are still being measured after a restart. Try again in a few minutes.</div>'; return; }
      const days = x.days || 0;
      const notes = [];
      if (simKind === 'arb') {
        notes.push(`${x.unwound ?? 0} of ${x.trades} sold early, median hold ${Math.round(x.median_hold_min ?? 0)} min. ${x.leg_failures ?? 0} second legs failed: ${money(x.leg_failure_loss ?? 0)}.`);
      } else {
        notes.push(`${x.normal_exits ?? 0} of ${x.trades} sold on the catch-up, the rest at the give-up time. Fills use the recorded order book where it was recorded.`);
        const L = x.list || [], per = (set) => { const v = L.filter((t) => t.set === set); return v.length ? `${set} set ${v.length} trades, ${Math.round(100 * v.filter((t) => t.pnl > 0).length / v.length)}% won, ${signed(v.reduce((a, t) => a + t.pnl, 0))}` : ''; };
        const sets = [per('safe'), per('wider')].filter(Boolean).join('; ');
        if (sets) notes.push(sets + '.');
        if (simKind === 'snipe-current') notes.push('In-sample: today\'s rules were chosen on this data.');
      }
      $('#btSim').innerHTML =
        `<div class="stats" style="margin:0 0 12px">` +
        `<div class="stat key"><div class="stat-label">${ic('payments')}Profit</div><div class="stat-value">${pnl(x.pnl)}</div><div class="stat-foot">${money(x.start, 0)} → ${money(x.end_balance)}</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('percent')}Return per $ staked</div><div class="stat-value">${pct(x.roi)}</div><div class="stat-foot">${money(x.staked, 0)} staked in total</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('swap_horiz')}Trades</div><div class="stat-value">${(x.trades || 0).toLocaleString()}</div><div class="stat-foot">${x.trades ? Math.round(100 * x.wins / x.trades) : 0}% won</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('trending_down')}Worst drop</div><div class="stat-value">${money(-(x.max_drawdown || 0))}</div><div class="stat-foot">peak to trough</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('calendar_today')}Per day</div><div class="stat-value">${pnl(x.per_day)}</div><div class="stat-foot">over ${days.toFixed(1)} days of data</div></div>` +
        `</div><div id="btSimChart" style="height:240px"></div>` +
        `<p class="note" style="margin:10px 0 6px">${notes.map(esc).join(' ')} A replay is an estimate from past data, not a promise.</p>` +
        `<div id="btSimDays"></div>` +
        `<p class="faint" style="font-size:12px;margin:6px 0 0">Computed ${SE.fmtDateTime(r.computed_ms)} with the settings saved then.</p>`;
      const xs = (x.curve || []).map((c) => c[0]);
      if (xs.length > 1) simChart = SE.chart($('#btSimChart'), xs, [{ label: 'Balance', color: css('--accent') || '#50e4b2', values: x.curve.map((c) => c[1]), width: 2, linear: true }], { height: 240, yFmt: (v) => (v == null ? '' : money(v, 0)) });
      else $('#btSimChart').innerHTML = `<div class="empty">${ic('show_chart')}No trades with these settings.</div>`;
      SE.table($('#btSimDays'), [
        { key: 'd', label: 'Day (UTC)', w: 130, fmt: (r2) => `<span class="muted">${SE.fmtDateTime(r2.day).split(' ').slice(0, 2).join(' ')}</span>`, sort: (r2) => r2.day },
        { key: 'n', label: 'Trades', cls: 'r', w: 90, fmt: (r2) => r2.trades.toLocaleString(), sort: (r2) => r2.trades },
        { key: 's', label: 'Staked', cls: 'r', w: 120, fmt: (r2) => money(r2.staked, 0), sort: (r2) => r2.staked },
        { key: 'p', label: 'Profit', cls: 'r', w: 120, fmt: (r2) => pnl(r2.pnl), sort: (r2) => r2.pnl },
        { key: 'r', label: 'Return per $', cls: 'r', w: 120, fmt: (r2) => pct(r2.staked ? r2.pnl / r2.staked : null), sort: (r2) => (r2.staked ? r2.pnl / r2.staked : 0) },
      ], { sort: 'd', empty: 'No trades.' }).set(x.daily || []);
    }
    async function loadSim(fresh) {
      clearTimeout(simTimer);
      const seq = ++simSeq;
      if (fresh) simLoading(null);
      let r;
      try { r = await api('/bots/sim?kind=' + simKind); } catch (e) { if ($('#btSim')) $('#btSim').innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
      if (seq !== simSeq || !$('#btSim')) return;
      if (r.state === 'running' || (!r.result && !r.error)) { simLoading(r); simTimer = setTimeout(() => loadSim(false), 900); return; }
      simDraw(r);
    }

    // ---- live against replay: the same hours, trade by trade
    let parReal = SE.pref('botParReal') === '1', parH = +SE.pref('botParH') || 24, parSeq = 0;
    SE.seg($('#btParTab'), [['0', 'Paper'], ['1', 'Real']], parReal ? '1' : '0', (v) => { parReal = v === '1'; SE.setPref('botParReal', v); loadParity(); });
    SE.seg($('#btParH'), [['24', '24 hours'], ['168', '7 days']], String(parH), (v) => { parH = +v; SE.setPref('botParH', v); loadParity(); });
    async function loadParity() {
      const host = $('#btPar'), seq = ++parSeq;
      if (!host) return;
      host.innerHTML = `<div class="empty"><span class="skeleton">Comparing live trades with the replay</span></div>`;
      let r;
      try { r = await api(`/bots/parity?real=${parReal ? 1 : 0}&hours=${parH}`); } catch (e) { host.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
      if (seq !== parSeq || !$('#btPar')) return;
      if (r.building) { host.innerHTML = '<div class="empty">The recorded moves are still being measured after a restart. Try again in a few minutes.</div>'; return; }
      const s = r.summary || {};
      const cents = (x) => (x == null || !isFinite(x) ? '-' : (x * 100).toFixed(1) + '¢');
      const gap = (x, f) => (x == null ? '<span class="faint">-</span>' : `<span class="${Math.abs(x) < 0.005 ? 'muted' : x > 0 ? 'up' : 'down'}">${x > 0 ? '+' : ''}${f(x)}</span>`);
      const ret = (x) => (x == null ? '<span class="faint">-</span>' : `<span class="${x > 0 ? 'up' : x < 0 ? 'down' : 'muted'}">${x > 0 ? '+' : ''}${pct(x)}</span>`);
      const rows = [].concat(
        (r.both || []).map((b) => ({ t: b.live.ts, home: b.live.home, o: b.live.outcome, set: b.live.set, who: 'Both', le: b.live.entry, re: b.replay.entry, lx: b.live.exit, rx: b.replay.exit, lr: b.live.return, rr: b.replay.return, why: '' })),
        (r.only_live || []).map((b) => ({ t: b.ts, home: b.home, o: b.outcome, set: b.set, who: 'Live only', le: b.entry, lx: b.exit, lr: b.return, why: 'Replay: ' + sigName(b.replay_says || '') })),
        (r.only_replay || []).map((b) => ({ t: b.t, home: b.home, o: b.outcome, set: b.set, who: 'Replay only', re: b.entry, rx: b.exit, rr: b.return, why: 'Live: ' + (b.live_says || []).map(sigName).join(', ') })));
      host.innerHTML =
        `<div class="stats" style="margin:0 0 12px">` +
        `<div class="stat key"><div class="stat-label">${ic('done_all')}Same trade both</div><div class="stat-value">${s.both || 0}</div><div class="stat-foot">of ${s.live_trades || 0} live, ${s.replay_trades || 0} replay</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('person')}Live only</div><div class="stat-value">${s.only_live || 0}</div><div class="stat-foot">replay passed</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('science')}Replay only</div><div class="stat-value">${s.only_replay || 0}</div><div class="stat-foot">live passed</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('shopping_cart')}Buy price gap</div><div class="stat-value">${gap(s.entry_diff, cents)}</div><div class="stat-foot">live minus replay</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('sell')}Sale price gap</div><div class="stat-value">${gap(s.exit_diff, cents)}</div><div class="stat-foot">live minus replay</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('percent')}Return gap</div><div class="stat-value">${gap(s.return_diff, (x) => pct(x))}</div><div class="stat-foot">live minus replay</div></div>` +
        `</div><div id="btParTbl"></div>`;
      SE.table($('#btParTbl'), [
        { key: 't', label: 'Time', w: 120, fmt: (x) => `<span class="muted">${SE.fmtDateTime(x.t)}</span>`, sort: (x) => x.t },
        { key: 'g', label: 'Game', fmt: (x) => `${esc(x.home || '')} <span class="faint">${esc(x.o || '')}${x.set ? ' · ' + esc(x.set) : ''}</span>`, sort: (x) => x.home || '' },
        { key: 'w', label: 'Traded by', w: 100, fmt: (x) => (x.who === 'Both' ? 'Both' : `<span class="muted">${x.who}</span>`), sort: (x) => x.who },
        { key: 'e', label: 'Buy live / replay', cls: 'r', w: 140, fmt: (x) => `${cents(x.le)} / ${cents(x.re)}`, sort: (x) => x.le ?? x.re ?? 0 },
        { key: 'x', label: 'Sale live / replay', cls: 'r', w: 140, fmt: (x) => `${cents(x.lx)} / ${cents(x.rx)}`, sort: (x) => x.lx ?? x.rx ?? 0 },
        { key: 'r', label: 'Return live / replay', cls: 'r', w: 160, fmt: (x) => `${ret(x.lr)} / ${ret(x.rr)}`, sort: (x) => x.lr ?? x.rr ?? 0 },
        { key: 'y', label: 'Why not both', fmt: (x) => `<span class="muted">${esc(x.why)}</span>`, sort: (x) => x.why },
      ], { sort: 't', empty: 'No trades in this period.' }).set(rows);
    }

    // ================================================================ setup + arbitrage (drawn from /bots)
    function common(prefix, c, missing, realFree) {
      return row('Running', sw(prefix + 'On', c.enabled, 'Off = no new trades. Open positions still close.')) +
        row('Real money', sw(prefix + 'Real', c.real, missing.length ? 'Needs: ' + missing.join(', ') : 'Asks for a typed confirmation.') + (missing.length && !c.real ? `<div class="faint" style="font-size:12px;margin-top:4px">needs ${esc(missing.join(', '))}</div>` : '')) +
        row('Real bankroll', numIn(prefix + 'Bank', c.bankroll, 10, 'How much of the shared accounts this bot may use for real money. 0 = no real money.', 110, '$') + (realFree != null ? `<div class="faint" style="font-size:12px;margin-top:4px">${money(realFree)} free now</div>` : '')) +
        row('Paper starting balance', numIn(prefix + 'Start', c.start, 100, 'Changing it re-bases the paper wallet.', 110, '$')) +
        row('Most per trade', numIn(prefix + 'MaxT', c.max_trade, 10, '', 110, '$')) +
        row('Most open at once', numIn(prefix + 'MaxO', c.max_open, 100, '', 110, '$')) +
        row('Daily loss limit (real)', numIn(prefix + 'Loss', c.daily_loss, 10, 'Real money switches back to paper when today\'s real result is below minus this.', 110, '$'));
    }
    const commonEdit = (prefix) => ({ bankroll: +$('#' + prefix + 'Bank').value, start: +$('#' + prefix + 'Start').value, max_trade: +$('#' + prefix + 'MaxT').value, max_open: +$('#' + prefix + 'MaxO').value, daily_loss: +$('#' + prefix + 'Loss').value });

    function drawSetup() {
      if (!d || tab !== 'setup' || !$('#btKeys')) return;
      if ($('[data-tab="setup"]', el).contains(document.activeElement) && ['INPUT', 'SELECT'].includes(document.activeElement.tagName)) return;
      const s = d.settings, k = d.keys;
      $('#btShared').innerHTML =
        row('Most at risk in one game (both bots)', numIn('shCap', s.game_cap, 100, 'Open snipe trades plus open arbitrage positions on the same game.', 120, '$')) +
        row('Cash kept free on Polymarket (real)', numIn('shPm', s.pm_reserve, 10, '', 110, '$')) +
        row('Cash kept free on Kalshi (real)', numIn('shK', s.kalshi_reserve, 10, '', 110, '$')) +
        `<div style="margin-top:10px"><button class="btn" id="shSave">${ic('save')}Save shared limits</button></div>`;
      $('#shSave').onclick = () => save({ game_cap: +$('#shCap').value, pm_reserve: +$('#shPm').value, kalshi_reserve: +$('#shK').value });
      $('#btKeys').innerHTML =
        row('Polymarket API key, secret, passphrase', (k.pm_api && k.pm_secret_ok ? st(true, 'Saved') : st(false, !k.pm_secret_ok ? 'The API secret is not a Polymarket secret' : 'Passphrase missing')) +
          (k.pm_trading_address ? ` <button class="btn sm" id="pkApi" data-tip="Asks Polymarket for the API key, secret and passphrase that belong to your trading key (it signs a 'this is my wallet' message; no order, no money) and saves them.">${ic('key')}Get them from the trading key</button>` : ''), 'They let the bots read your balance and place orders. The trading key can fetch them.') +
        row('Polymarket wallet (funder)', k.pm_wallet ? `<span class="mono">${esc(k.pm_wallet)}</span>` : '<a class="btn sm" href="#/settings">Add in Settings</a>') +
        row('Polymarket trading key', `${k.pm_trading_address ? `${st(true, 'Saved')} <span class="mono faint">signs as ${esc(k.pm_trading_address)}</span>` : st(false, 'Missing')}<div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap"><input class="input" id="pkIn" type="password" placeholder="Private key of the signer (hex)" style="width:300px" autocomplete="off"><select class="input" id="pkType" style="width:230px" data-tip="Google / email sign-ups now get a deposit wallet. Older email logins: Polymarket proxy. Browser wallet: Safe. Plain wallet: EOA."><option value="3">Deposit wallet (Google / email)</option><option value="1">Polymarket proxy (older email login)</option><option value="2">Browser-wallet Safe</option><option value="0">Plain wallet (EOA)</option></select><button class="btn sm" id="pkSave">Save</button></div>`, 'Signs Polymarket orders. It never leaves this computer and is never shown again.') +
        row('Kalshi API key', k.kalshi ? st(true, 'Saved') : '<a class="btn sm" href="#/kalshi">Add on the Kalshi page</a>');
      if ($('#pkApi')) $('#pkApi').onclick = async () => {
        try { const v = await api('/pm/api-from-key', { method: 'POST' }); SE.toast(v.has_api && v.api_secret_ok ? 'API key, secret and passphrase saved' : 'Saved, but something is still missing', 6000); load(); loadAcc(); } catch (e) { SE.toast(e.message, 8000); }
      };
      $('#pkSave').onclick = async () => {
        const v = $('#pkIn').value.trim();
        if (!v) { SE.toast('Paste the key first'); return; }
        try { await api('/pm/settings', { body: { private_key: v, signature_type: +$('#pkType').value } }); $('#pkIn').value = ''; SE.toast('Trading key saved'); load(); } catch (e) { SE.toast(e.message, 6000); }
      };
      SE.table($('#btOrd'), [
        { key: 't', label: 'When', w: 140, fmt: (o) => `<span class="muted">${SE.fmtDateTime(o.ts)}</span>`, sort: (o) => o.ts },
        { key: 'b', label: 'Bot', w: 110, fmt: (o) => esc(o.bot) },
        { key: 'v', label: 'Venue', w: 110, fmt: (o) => SE.book(o.venue, 'sm') },
        { key: 'k', label: 'Result', w: 80, fmt: (o) => st(o.ok, o.ok ? 'ok' : 'failed') },
        { key: 'd', label: 'Order', fmt: (o) => { const f = o.data?.fill || {}; return `${esc((o.data?.side || '').toUpperCase())} ${f.filled != null ? (+f.filled).toFixed(2) : '-'} @ ${f.avg ? cents(f.avg) : '-'}${o.data?.error ? ` <span class="down">${esc(String(o.data.error).slice(0, 120))}</span>` : ''}`; } },
      ], { sort: 't', empty: 'No real orders yet. The bots trade paper until switched to real money.' }).set(d.orders || []);
      $('#btEv').innerHTML = (d.events || []).length ? d.events.map((e) => `<div class="set-row"><span class="mono faint">${SE.fmtDateTime(e.ts)}</span><div>${esc(e.msg)}</div></div>`).join('') : '<div class="empty">No events since the last restart.</div>';
    }

    function drawArb() {
      if (!d || tab !== 'arb' || !$('#btArb')) return;
      if ($('[data-tab="arb"]', el).contains(document.activeElement) && document.activeElement.tagName === 'INPUT') return;
      const s = d.settings, ar = d.arb, k = d.keys, sc = ar.scan || {};
      $('#btArbChip').innerHTML = s.kill ? st(false, 'Paused: kill switch') : !s.arb.enabled ? st(null, 'Switched off') : s.arb.real ? st(false, 'REAL MONEY') : st(true, 'Paper');
      const rej = Object.entries(sc.rejected || {}).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([kk, v]) => `${kk.replace(/_/g, ' ')} ${v}`).join(', ');
      $('#btArb').innerHTML =
        `<p class="note" style="margin:0 0 12px">Switched off since 18 Nov: in the replay its second leg failed too often. Buying the Kalshi side first looks better on the newest data but is unproven.</p>` +
        `<div class="stats" style="margin:0 0 10px">` +
        `<div class="stat"><div class="stat-label">Paper balance</div><div class="stat-value">${money(ar.paper?.balance)}</div><div class="stat-foot">${pnl(ar.paper?.realized)} realized · today ${pnl(ar.paper?.today)}</div></div>` +
        `<div class="stat"><div class="stat-label">Cash free</div><div class="stat-value">${money(ar.paper?.cash)}</div><div class="stat-foot">open ${money(ar.paper?.open_cost)}</div></div>` +
        `<div class="stat"><div class="stat-label">Scans</div><div class="stat-value">${(sc.scans ?? 0).toLocaleString()}</div><div class="stat-foot">${sc.watching ?? 0} games on both · gaps ${sc.raw_gaps ?? 0}</div></div>` +
        `</div>` + (rej ? `<p class="note" style="margin:0 0 8px">Gaps not taken: ${esc(rej)}.</p>` : '') +
        common('ar', s.arb, k.missing_arb, ar.real?.free) +
        row('Least return, live games', numIn('arLive', +(s.arb.min_live * 100).toFixed(2), 0.1, '', 96, '', '%')) +
        row('Least return, pre-match', numIn('arPre', +(s.arb.min_pre * 100).toFixed(2), 0.1, '', 96, '', '%')) +
        row('Least return of the last pair', numIn('arPair', +(s.arb.min_pair * 100).toFixed(2), 0.1, 'How deep into the books an order may walk.', 96, '', '%')) +
        row('Sell both early at', numIn('arUnw', Math.round(s.arb.unwind_at * 100), 5, '100 = sell both legs as soon as they fetch the full locked profit. 0 = never.', 96, '', '%')) +
        row('Live games only', sw('arLiveOnly', s.arb.live_only, 'Skip pre-match gaps.')) +
        `<div id="arConfirm" hidden style="margin-top:10px"></div><div style="margin-top:10px"><button class="btn primary" id="arSave">${ic('save')}Save arbitrage settings</button></div>`;
      $('#arOn').onchange = (e) => save({ arb: { enabled: e.target.checked } });
      $('#arReal').onchange = (e) => {
        const on = e.target.checked;
        const go = async (confirm) => { try { d = await api('/bots/real/arb', { body: { real: on, confirm } }); SE.toast(on ? 'Arbitrage bot: REAL MONEY' : 'Arbitrage bot: paper', 5000); } catch (err) { SE.toast(err.message, 7000); } load(); };
        if (!on) { go(''); return; }
        e.target.checked = false;
        confirmBox($('#arConfirm'), { danger: true, title: 'Switch the arbitrage bot to real money?', body: 'It will place real orders on Polymarket and Kalshi with your saved keys, within its caps. In the replay this bot lost money.', type: 'REAL MONEY', ok: 'Switch to real money', onOk: () => go('REAL MONEY') });
      };
      $('#arSave').onclick = () => save({ arb: { ...commonEdit('ar'), min_live: +$('#arLive').value / 100, min_pre: +$('#arPre').value / 100, min_pair: +$('#arPair').value / 100, unwind_at: +$('#arUnw').value / 100, live_only: $('#arLiveOnly').checked } });
      const now = Date.now();
      SE.table($('#btArT'), [
        { key: 't', label: 'Opened', w: 150, fmt: (p) => `<span class="muted">${SE.fmtDateTime(p.ts)}</span>${p.real ? ' <span class="st-bad">real</span>' : ''}`, sort: (p) => p.ts },
        { key: 'm', label: 'Match', cls: 'match', fmt: (p) => P.matchCell(p.home, p.away, '', p.sport, p.match_id) },
        { key: 'l', label: 'Legs', w: 250, fmt: (p) => `${Math.round(p.contracts)} × Yes ${esc(p.outcome)} ${(p.yes * 100).toFixed(1)}¢ on ${SE.bookName(p.yes_on)} + No ${(p.no * 100).toFixed(1)}¢` },
        { key: 's', label: 'Staked', cls: 'r', w: 100, fmt: (p) => money(p.cost) },
        { key: 'p', label: 'Result', cls: 'r', w: 140, fmt: (p) => (p.closed_ms ? `${pnl(p.pnl)}<div class="faint">${esc(p.close_reason || 'sold')}</div>` : (p.payout_ms || 0) > now ? `${pnl(p.profit)}<div class="faint">locked, open</div>` : `${pnl(p.profit)}<div class="faint">paid out</div>`), sort: (p) => p.pnl ?? p.profit },
        { key: 'x', label: '', w: 80, fmt: (p) => (!p.closed_ms && (p.payout_ms || 0) > now ? `<button class="btn sm" data-sell="${p.ts}/${p.match_id}">${ic('sell')}Sell</button>` : '') },
      ], { sort: 't', empty: 'No arbitrage positions yet.' }).set(sc.recent || []);
      if (!$('#btArT').dataset.wired) {
        $('#btArT').dataset.wired = '1';
        $('#btArT').addEventListener('click', async (e) => {
          const b = e.target.closest('[data-sell]');
          if (!b) return;
          e.stopPropagation();
          b.disabled = true;
          try { const x = await api('/predarb/sell/' + b.dataset.sell, { method: 'POST' }); SE.toast(x.ok ? `Sold: ${signed(x.position.pnl)}` : x.error, 6000); } catch (err) { SE.toast(err.message, 6000); }
          load();
        }, true);
      }
    }

    function draw() { drawPanel(); drawSetup(); drawArb(); }

    async function loadAcc() {
      if (!$('#btAcc') || tab !== 'setup') return;
      let a;
      try { a = await api('/bots/accounts'); } catch (e) { $('#btAcc').innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
      if (!$('#btAcc')) return;
      if (!a.keys) { $('#btAcc').innerHTML = '<div class="empty">No account keys saved: the bots are paper only.</div>'; return; }
      const v = (x, name) => `<div class="stat"><div class="stat-label">${SE.book(name, 'sm')}</div><div class="stat-value">${x.cash != null ? money(x.cash) : '<span class="faint">-</span>'}</div><div class="stat-foot">${x.error ? esc(x.error) : `free for the bots ${money(Math.max(0, (x.cash || 0) - x.reserve))} after the ${money(x.reserve, 0)} reserve`}${x.other_positions ? ` · ${x.other_positions} other positions` : ''}</div></div>`;
      $('#btAcc').innerHTML = `<div class="stats" style="margin:0 0 10px">${v(a.polymarket, 'polymarket')}${v(a.kalshi, 'kalshi')}</div>` +
        (a.problems ? `<p class="note down" style="margin:0 0 8px">${a.problems} position(s) the bots hold are MISSING on the account: check them by hand.</p>` : '') + '<div id="btAccT"></div>';
      SE.table($('#btAccT'), [
        { key: 'v', label: 'Venue', w: 110, fmt: (r) => SE.book(r.venue, 'sm') },
        { key: 'i', label: 'Contract', fmt: (r) => `<span class="mono faint" style="font-size:12px">${esc(r.id.length > 24 ? r.id.slice(0, 10) + '…' + r.id.slice(-8) : r.id)}</span>` },
        { key: 'b', label: 'Bots', w: 130, fmt: (r) => esc(r.bots.join(', ')) },
        { key: 'n', label: 'Bots hold', cls: 'r', w: 100, fmt: (r) => r.bots_hold.toFixed(0) },
        { key: 'h', label: 'Account holds', cls: 'r', w: 120, fmt: (r) => (r.account_holds == null ? '-' : r.account_holds.toFixed(0)) },
        { key: 's', label: 'Check', w: 220, fmt: (r) => st(r.status === 'matches' ? true : r.status.startsWith('MISSING') ? false : null, r.status) },
      ], { empty: 'No open real-money positions.' }).set(a.rows || []);
    }

    // ---- test orders
    let testing = false;
    async function loadDiag() {
      if (!$('#btDiag') || testing || tab !== 'setup') return;
      let g;
      try { g = await api('/bots/tests'); } catch (e) { $('#btDiag').innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
      if (!$('#btDiag') || testing) return;
      const m = g.summary || {};
      const ms = (x) => (x == null ? '<span class="faint">-</span>' : `${Math.round(x)}<small>ms</small>`);
      const c = (x) => (x == null ? '<span class="faint">-</span>' : `${(x * 100).toFixed(1)}<small>¢</small>`);
      $('#btDiag').innerHTML = `<div class="stats" style="margin:0 0 12px">` +
        `<div class="stat" data-tip="Median time from sending a Polymarket order to its reply."><div class="stat-label">${SE.book('polymarket', 'sm')} order latency</div><div class="stat-value">${ms(m.pm_latency_ms)}</div><div class="stat-foot">slippage ${c(m.pm_slippage)} median</div></div>` +
        `<div class="stat" data-tip="Median time from sending a Kalshi order to its reply."><div class="stat-label">${SE.book('kalshi', 'sm')} order latency</div><div class="stat-value">${ms(m.kalshi_latency_ms)}</div><div class="stat-foot">slippage ${c(m.kalshi_slippage)} median</div></div>` +
        `<div class="stat" data-tip="Polymarket holds orders on live sports games before matching them. Makers can move their prices during it, which is what a snipe has to beat."><div class="stat-label">${SE.book('polymarket', 'sm')} matching delay</div><div class="stat-value">${ms(m.pm_match_delay_ms)}</div><div class="stat-foot">live sports · shares settle in ${ms(m.pm_settle_ms)}</div></div>` +
        `<div class="stat"><div class="stat-label">Tests</div><div class="stat-value">${m.count ?? 0}</div><div class="stat-foot">${m.ok ?? 0} complete · cost ${pnl(m.net_total)}</div></div>` +
        `</div><div id="btDiagT"></div>`;
      const left = (t) => {
        if (t.sold_back) return 0;
        let b = 0, s2 = 0;
        for (const l of t.legs || []) if (l.venue === 'polymarket') { if (l.side === 'buy') b += l.filled || 0; else s2 += l.filled || 0; }
        return Math.floor(b - s2);
      };
      const legs = (t) => (t.legs || []).map((l) => `<div style="font-size:12.5px">${SE.book(l.venue, 'sm')} ${esc(l.side)} ${l.filled != null ? Math.round(l.filled) : 0}/${Math.round(l.wanted || 0)}${l.avg ? ` @ ${(l.avg * 100).toFixed(1)}¢` : ''} <span class="faint">(saw ${(l.expected * 100).toFixed(1)}¢${l.latency_ms != null ? `, ${l.latency_ms} ms${l.match_delay_ms ? ` + ${l.match_delay_ms} ms matching delay` : ''}` : ''})</span>${l.error ? ` <span class="down">${esc(l.error.slice(0, 120))}</span>` : ''}</div>`).join('');
      SE.table($('#btDiagT'), [
        { key: 't', label: 'When', w: 130, fmt: (t) => `<span class="muted">${SE.fmtDateTime(t.ts)}</span>`, sort: (t) => t.ts },
        { key: 'k', label: 'Test', w: 130, fmt: (t) => `${esc(t.kind || '')}<div>${st(!!t.ok, t.ok ? 'complete' : 'incomplete')}</div>` },
        { key: 'l', label: 'Orders', fmt: legs },
        { key: 'n', label: 'Net', cls: 'r', w: 90, fmt: (t) => pnl(t.net), sort: (t) => t.net ?? 0 },
        { key: 'x', label: '', w: 130, fmt: (t) => (left(t) >= 1 ? `<button class="btn sm" data-sb="${t.id}" data-n="${left(t)}" data-tip="This test left ${left(t)} Polymarket shares on the account. Sells them into the current bid (2¢ under).">${ic('sell')}Sell back ${left(t)}</button>` : '') },
      ], { sort: 't', empty: 'No test orders yet. Each button places one real round trip at the smallest size.' }).set((g.tests || []).map((t) => ({ ...t, error: undefined })));
      if (!$('#btDiagT').dataset.wired) {
        $('#btDiagT').dataset.wired = '1';
        $('#btDiagT').addEventListener('click', (e) => {
          const b = e.target.closest('[data-sb]');
          if (!b) return;
          e.stopPropagation();
          confirmBox($('#btDiagC'), { danger: true, title: `Sell ${b.dataset.n} test shares back now?`, body: 'A real order into the current bid, 2¢ under.', ok: 'Sell them',
            onOk: async () => { try { const r = await api('/bots/test-sellback/' + b.dataset.sb, { method: 'POST' }); const f = (r.sold || [])[0]; SE.toast(f ? `Sold ${Math.round(f.filled || 0)} of ${Math.round(f.wanted || 0)}${f.avg ? ' at ' + (f.avg * 100).toFixed(1) + '¢' : ''}${f.error ? ': ' + f.error : ''}` : 'Nothing left to sell', 7000); } catch (err) { SE.toast(err.message, 7000); } loadDiag(); loadAcc(); } });
        }, true);
      }
      const errs = (g.tests || []).filter((t) => t.error).slice(0, 1);
      if (errs.length) $('#btDiag').insertAdjacentHTML('beforeend', `<p class="note" style="margin-top:8px">Last problem: ${esc(errs[0].error)}</p>`);
    }
    const runTest = (bot) => confirmBox($('#btDiagC'), {
      danger: true, title: bot === 'arb' ? 'Place a real test arbitrage pair?' : 'Place a real test buy?',
      body: `${bot === 'arb' ? 'An arbitrage pair (Polymarket then Kalshi), sold straight back' : 'One Polymarket buy, sold straight back'}, at the smallest size the venue allows (about $1 to $5). It costs the spread and the fees.`,
      type: 'TEST', ok: 'Place it',
      onOk: async () => {
        testing = true;
        $('#btDiag').innerHTML = `<div class="sim-load"><div class="lbl"><span class="dot"></span>Placing the real test orders and timing them</div><div class="sim-bar indet"><i></i></div></div>`;
        try { const r = await api('/bots/test/' + bot, { body: { confirm: 'TEST' } }); SE.toast(r.ok ? `Test done: net ${signed(r.net || 0)}` : `Test incomplete: ${r.error}`, 7000); } catch (e) { SE.toast(e.message, 7000); }
        testing = false;
        loadDiag(); loadAcc();
      },
    });
    $('#tArb').onclick = () => runTest('arb');
    $('#tSn').onclick = () => runTest('snipe');

    // ---- real test runs (history and the per-market chart)
    let runChart = null, runSel = null;
    async function loadRun() {
      if (!$('#btRun') || tab !== 'setup') return;
      let g;
      try { g = await api('/bots/runs'); } catch (e) { return; }
      if (!$('#btRun')) return;
      runs = g;
      const cur = g.current, last = cur || (g.past || [])[0];
      const rep = (r, live) => `<div class="stats" style="margin:0 0 10px">` +
        `<div class="stat key"><div class="stat-label">${ic('payments')}${live ? 'Result so far' : 'Last run'}</div><div class="stat-value">${pnl(r.pnl)}</div><div class="stat-foot">on ${money(r.staked)} staked · $${r.bankroll} bankroll</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('swap_horiz')}Trades</div><div class="stat-value">${r.trades}</div><div class="stat-foot">${r.closed} closed, ${r.wins} won, ${r.open} open</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('bolt')}Signals</div><div class="stat-value">${(r.signals || []).reduce((a, x) => a + x.n, 0)}</div><div class="stat-foot">${esc((r.signals || []).slice(0, 3).map((x) => `${sigName(x.result)} ${x.n}`).join(' · '))}</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('schedule')}Window</div><div class="stat-value" style="font-size:18px">${SE.fmtDateTime(r.start_ms)}</div><div class="stat-foot">to ${SE.fmtDateTime(r.end_ms)}${r.ended ? ' · ' + esc(r.ended) : ''}</div></div></div>`;
      const markets = last ? [...(last.list || []).map((t) => ({ match_id: t.match_id, outcome: t.outcome, home: t.home, away: t.away, n: 'trade' })), ...(last.markets || [])] : [];
      const seen = new Set();
      const mk = markets.filter((m) => { const k = m.match_id + m.outcome; if (seen.has(k)) return false; seen.add(k); return true; });
      $('#btRun').innerHTML = (last ? rep(last, !!cur) : '<p class="note" style="margin:0 0 8px">No test run yet. Start one from the control panel.</p>') +
        (mk.length ? `<div style="display:flex;gap:8px;align-items:center;margin:4px 0 8px"><span class="muted">Market</span><select class="input" id="runMk" style="max-width:520px">${mk.map((m) => `<option value="${m.match_id}|${esc(m.outcome || '')}">${esc((m.home || 'Match ' + m.match_id) + (m.away ? ' v ' + m.away : ''))} · ${esc(m.outcome || '')} · ${m.n === 'trade' ? 'traded' : m.n + ' signals'}</option>`).join('')}</select></div><div id="runChart" style="height:260px"></div><p class="faint" style="font-size:12px;margin:6px 0 0">Lines: Polymarket bid / ask at each minute. Dots: grey = signal checked, green = buy, red = sell.</p>` : '') +
        ((g.past || []).length ? `<div id="runPast" style="margin-top:12px"></div>` : '');
      if (mk.length) {
        if (runSel && mk.some((m) => m.match_id + '|' + m.outcome === runSel)) $('#runMk').value = runSel;
        $('#runMk').onchange = () => { runSel = $('#runMk').value; drawRunChart(last); };
        drawRunChart(last);
      }
      if ((g.past || []).length) SE.table($('#runPast'), [
        { key: 't', label: 'Run', w: 170, fmt: (r) => `<span class="muted">${SE.fmtDateTime(r.start_ms)}</span>`, sort: (r) => r.start_ms },
        { key: 'h', label: 'Hours', cls: 'r', w: 70, fmt: (r) => Math.round((r.end_ms - r.start_ms) / 3600000) },
        { key: 'n', label: 'Trades', cls: 'r', w: 110, fmt: (r) => `${r.trades} (${r.wins} won)` },
        { key: 's', label: 'Staked', cls: 'r', w: 100, fmt: (r) => money(r.staked) },
        { key: 'p', label: 'Result', cls: 'r', w: 100, fmt: (r) => pnl(r.pnl) },
        { key: 'e', label: 'Ended', fmt: (r) => `<span class="muted">${esc(r.ended || '')}</span>` },
      ], { sort: 't' }).set(g.past);
    }
    async function drawRunChart(r) {
      const host = $('#runChart');
      if (!host || !r) return;
      const [m, o] = ($('#runMk')?.value || '').split('|');
      let x;
      try { x = await api(`/bots/run/chart?m=${m}&o=${encodeURIComponent(o)}&from=${r.start_ms}&to=${r.end_ms}`); } catch (e) { host.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
      const mins = x.minutes || [];
      if (!mins.length) { host.innerHTML = '<div class="empty">No Polymarket prices recorded for this market in the window.</div>'; return; }
      const xs = mins.map((q) => q[0]);
      const at = (t) => { const i = xs.findIndex((q) => q > t); return i < 0 ? xs.length - 1 : Math.max(0, i - 1); };
      const blank = () => xs.map(() => null);
      const sig = blank(), buy = blank(), sell = blank();
      for (const s2 of x.signals || []) { const i = at(s2.ts); const q = mins[i]; sig[i] = q[1] != null && q[2] != null ? (q[1] + q[2]) / 2 : (q[1] ?? q[2]); }
      for (const t of x.trades || []) {
        const yes = t.dir > 0;
        buy[at(t.ts)] = yes ? t.entry : 1 - t.entry;
        if (t.exit_ts) sell[at(t.exit_ts)] = yes ? t.exit : 1 - t.exit;
      }
      SE.destroyChart(runChart);
      runChart = SE.chart(host, xs, [
        { label: 'Bid', color: css('--text-3') || '#888', values: mins.map((q) => q[1]), width: 1.2 },
        { label: 'Ask', color: css('--text-2') || '#aaa', values: mins.map((q) => q[2]), width: 1.2 },
        { label: 'Signal', color: css('--text-3') || '#888', values: sig, points: true },
        { label: 'Buy', color: css('--up') || '#50e4b2', values: buy, points: true },
        { label: 'Sell', color: css('--down') || '#ff5f57', values: sell, points: true },
      ], { height: 260, yFmt: (v) => (v == null ? '' : (v * 100).toFixed(0) + '¢') });
    }

    // ================================================================ loading
    let modeSeg = '';
    async function load() {
      try {
        const nd = await api('/bots');
        d = nd;
        if (view == null) view = d.settings.snipe.real || d.settings.run ? 'real' : ['paper', 'real', 'both'].includes(SE.pref('botView')) ? SE.pref('botView') : 'paper';
        if (modeSeg !== view) { modeSeg = view; SE.seg($('#bpMode'), [['paper', ic('edit_note') + 'Paper', 'Paper money: the simulated wallet.'], ['real', ic('payments') + 'Real', 'Real money on your Polymarket account.'], ['both', 'Both', 'Paper and real side by side.']], view, setView); }
        if (tab === 'panel') {
          const ws = wallets();
          const got = await Promise.all(ws.map((w) => api('/bots/panel?real=' + (w === 'real' ? 1 : 0)).catch(() => null)));
          ws.forEach((w, i) => { if (got[i]) pd[w] = got[i]; });
        }
        draw();
      } catch (e) { if (!d) SE.toast(e.message, 6000); }
    }
    load();
    showTab(tab);
    SE.every(5000, load);
    SE.every(30000, () => { loadAcc(); loadRun(); });
    // Countdowns tick every second without reloading.
    SE.every(15000, tick);
    return () => { if (chart) { chart._ro?.disconnect(); chart.destroy(); } SE.destroyChart(simChart); SE.destroyChart(runChart); SE.destroyChart(projChart); clearTimeout(replayWait); SE.tipHide(); };
  },
};
})();
