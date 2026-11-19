// Swifteye pages, part 6: Bots (the snipe bot and the hedge & arbitrage bot: wallets, settings,
// the shared caps and the Real switches).
'use strict';
(() => {
const { $, esc, ic, api, P } = SE;

const money = (x, d = 2) => (x == null || !isFinite(x) ? '-' : (x < 0 ? '-$' : '$') + Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }));
const pnl = (x) => (x == null ? '<span class="faint">-</span>' : `<span class="${x > 0 ? 'up' : x < 0 ? 'down' : 'muted'}">${x > 0 ? '+' : ''}${money(x)}</span>`);
const pct = (x, d = 1) => (x == null || !isFinite(x) ? '-' : (x * 100).toFixed(d) + '%');
const sw = (id, on, tip = '') => `<label class="switch" ${tip ? `data-tip="${esc(tip)}"` : ''}><input type="checkbox" id="${id}" ${on ? 'checked' : ''}><span></span></label>`;
const numIn = (id, v, step, tip, w = 110) => `<input class="input" type="number" id="${id}" value="${v ?? ''}" step="${step}" style="width:${w}px" ${tip ? `data-tip="${esc(tip)}"` : ''}>`;
const row = (label, ctl, tip = '') => `<div class="set-row"><span ${tip ? `data-tip="${esc(tip)}"` : ''}>${label}</span><div>${ctl}</div></div>`;

SE.pages.bots = {
  title: 'Bots',
  render(el) {
    el.innerHTML = P.head('Bots', '<span id="btKill"></span>') +
      '<div id="btBanner"></div>' +
      P.stats(
        P.stat('btS', 'my_location', 'Snipe bot (paper)', 'Paper balance of the snipe bot: its starting balance plus closed results.', true),
        P.stat('btA', 'balance', 'Hedge & arbitrage bot (paper)', 'Paper balance of the hedge & arbitrage bot: its starting balance plus sold and paid-out results.'),
        P.stat('btR', 'payments', 'Real money today', 'Both bots\' real-money results today (UTC). Zero while both are on paper.'),
        P.stat('btX', 'shield', 'Shared caps', 'Dollars at risk in one game across both bots, and cash kept free on each venue for real money.')) +
      P.sec('timer', 'Real test run', '<div id="btRun"></div>', '<span id="btRunTools"></span>', 'A timed REAL-money run of the snipe bot: $40 of the Polymarket account, about $12 a trade. It ends by itself, puts the settings back and reports. Every signal, buy and sell is charted on the minute prices.') +
      '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px">' +
      P.sec('my_location', 'Snipe bot', '<div id="btSnipe"></div>', '<span id="btSnipeChip"></span>', 'Buys Polymarket prices the bookmakers have already moved past and sells when Polymarket catches up. Polymarket only. Its rules are the Adaptive bot\'s current pick (Snipe window → Paper bot).') +
      P.sec('balance', 'Hedge & arbitrage bot', '<div id="btArb"></div>', '<span id="btArbChip"></span>', 'Buys both sides across Polymarket and Kalshi when the pair costs under $1 after both fees, including Kalshi-lag locks. Sells both early once the gap closes (auto-unwind).') +
      '</div>' +
      P.sec('science', 'Whole-dataset simulation', '<div id="btSim"></div>', '<div id="btSimTab"></div>', 'Each bot replayed over every move and quote recorded, with its settings as saved now. Re-runs by itself when a setting changes.') +
      P.sec('shield', 'Shared between the bots', '<div id="btShared"></div>', '', 'The wallets are separate; these limits apply to both.') +
      P.sec('key', 'Real-money keys', '<div id="btKeys"></div>', '', 'What real money needs. Keys stay on this computer and are never shown in full.') +
      P.sec('account_balance', 'Shared accounts', '<div id="btAcc"></div>', '', 'Both bots trade the same Polymarket and Kalshi accounts. Real buys go one at a time per venue and only with free cash (balance minus the reserve and what was just bought). Below: every position the bots hold on real money next to what the account really holds.') +
      P.sec('receipt_long', 'Snipe trades', '<div id="btSnT"></div>', '<div id="btSnTab"></div>') +
      P.sec('receipt_long', 'Arbitrage positions', '<div id="btArT"></div>') +
      P.sec('speed', 'Test runs & diagnostics', '<div id="btDiag"></div>', `<button class="btn sm" id="tArb" data-tip="One REAL arbitrage pair at the smallest size (Polymarket minimum, at least $1), Polymarket leg first then Kalshi, sold straight back. Costs the spread and fees on a few dollars.">${ic('science')}Test arbitrage</button><button class="btn sm" id="tSn" data-tip="One REAL smallest Polymarket buy on the tightest market, sold straight back. Costs the spread and fees on about a dollar.">${ic('science')}Test snipe</button>`, 'Real round trips at the smallest stakes, timed at every step: book age and socket silence at the decision, order preparation, request latency, the gap between the two legs, slippage against the price seen, and the net cost.') +
      P.sec('list_alt', 'Real orders', '<div id="btOrd"></div>', '', 'Every real order either bot sent, whatever happened.') +
      P.sec('notifications', 'Bot events', '<div id="btEv"></div>');
    let d = null, snTab = 'snipe';
    SE.seg($('#btSnTab'), [['snipe', 'Snipe bot'], ['shadow', '1-bookmaker shadow']], snTab, (v) => { snTab = v; drawTrades(); });

    // ---- whole-dataset simulation
    let simKind = SE.pref('botSim') || 'snipe-wf', simTimer = null, simChart = null, simSeq = 0;
    const SIMS = {
      'snipe-wf': ['Snipe bot', 'The snipe bot over every recorded Polymarket move: each 4-hour window traded with the rules picked before it (defaults before the first pick), with this bot\'s sizing and caps. What it would have made.'],
      'snipe-current': ['Snipe, today\'s rules', 'Today\'s rules over all the data. In-sample: the rules were picked on this same data, so this flatters them.'],
      'snipe-shadow': ['1-bookmaker shadow', 'Walk-forward rules with 1 bookmaker instead of the picked count.'],
      'arb': ['Hedge & arbitrage', 'Every recorded Polymarket and Kalshi quote of every game both listed, traded as the bot trades: both fees, the 250 ms re-check, best-level sizes, one position per game, auto-unwind, this bot\'s caps and cash.'],
    };
    SE.seg($('#btSimTab'), Object.entries(SIMS).map(([k, v]) => [k, v[0], v[1]]), simKind, (v) => { simKind = v; SE.setPref('botSim', v); loadSim(true); });
    const accent = () => getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#50e4b2';
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
      const days = x.days || 0;
      const notes = [];
      if (simKind === 'arb') {
        notes.push(`${x.unwound ?? 0} of ${x.trades} sold early by auto-unwind, median hold ${Math.round(x.median_hold_min ?? 0)} min; ${x.live_trades ?? 0} on live games.`);
        if (x.pre_fix_trades) notes.push(`${x.pre_fix_trades} trades are from before the Kalshi order-book fix (18 Nov ~16:00 UTC): their Kalshi sizes were wrong, so treat their size as a ceiling.`);
        if (x.size_unknown) notes.push(`${x.size_unknown} gaps had no recorded size (older data) and are left out.`);
        if (x.timing) notes.push(`Orders fill as measured by the test runs: Polymarket ${x.timing.pm_order_ms} ms + ${x.timing.pm_live_delay_ms} ms matching delay on live games, shares usable after ${x.timing.pm_settle_ms} ms, Kalshi ${x.timing.kalshi_order_ms} ms${x.timing.n_kalshi ? '' : ' (not measured yet: assumed)'}.`);
        notes.push(`${x.leg_failures ?? 0} leg failures (Kalshi gone after the Polymarket leg filled, shares sold back): ${money(x.leg_failure_loss ?? 0)}; ${x.gone_before_fill ?? 0} gaps gone before Polymarket matched (nothing bought).`);
        notes.push('Not replayable: the settlement-rule check (rule texts were not stored back then) and book levels behind the best price, so this is the best-price-only version of the bot.');
      } else {
        notes.push(`${x.normal_exits ?? 0} of ${x.trades} sold on the catch-up, the rest at the give-up time. Fills use the recorded order book where it was recorded.`);
        if (simKind === 'snipe-current') notes.push('In-sample: today\'s rules were chosen on this data.');
      }
      $('#btSim').innerHTML =
        `<div class="stats" style="margin:0 0 12px">` +
        `<div class="stat key"><div class="stat-label">${ic('payments')}Profit</div><div class="stat-value">${pnl(x.pnl)}</div><div class="stat-foot">${money(x.start, 0)} → ${money(x.end_balance)}</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('percent')}Return per $ staked</div><div class="stat-value">${pct(x.roi)}</div><div class="stat-foot">${money(x.staked, 0)} staked in total</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('swap_horiz')}Trades</div><div class="stat-value">${(x.trades || 0).toLocaleString()}</div><div class="stat-foot">${x.trades ? Math.round(100 * x.wins / x.trades) : 0}% won</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('trending_down')}Worst drop</div><div class="stat-value">${money(-(x.max_drawdown || 0))}</div><div class="stat-foot">peak to trough</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('calendar_today')}Per day</div><div class="stat-value">${pnl(x.per_day)}</div><div class="stat-foot">over ${days.toFixed(1)} days of data</div></div>` +
        `</div><div id="btSimChart" style="height:220px"></div>` +
        `<p class="note" style="margin:10px 0 6px">${notes.map(esc).join(' ')}</p>` +
        `<div id="btSimDays"></div>` +
        `<p class="faint" style="font-size:12px;margin:6px 0 0">Computed ${SE.fmtDateTime(r.computed_ms)} with the settings saved then.</p>`;
      const xs = (x.curve || []).map((c) => c[0]);
      if (xs.length > 1) simChart = SE.chart($('#btSimChart'), xs, [{ label: 'Balance', color: accent(), values: x.curve.map((c) => c[1]), width: 2, linear: true }], { height: 220, yFmt: (v) => (v == null ? '' : money(v, 0)) });
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

    const save = async (edit, msg = 'Saved') => {
      try { d = await api('/bots/settings', { body: edit }); draw(); SE.toast(msg); loadSim(true); } catch (e) { SE.toast(e.message, 6000); }
    };
    const setReal = async (bot, on) => {
      const name = bot === 'arb' ? 'hedge & arbitrage' : 'snipe';
      let confirm = '';
      if (on) {
        confirm = window.prompt(`Switch the ${name} bot to REAL MONEY?\n\nIt will place real orders with your saved keys, within its caps (most per trade, most open, daily loss limit). It goes back to paper by itself after a bad day.\n\nType REAL MONEY to confirm.`) || '';
        if (confirm.trim() !== 'REAL MONEY') { SE.toast('Not switched: confirmation not typed'); draw(); return; }
      }
      try { d = await api('/bots/real/' + bot, { body: { real: on, confirm } }); draw(); SE.toast(on ? `${name} bot: REAL MONEY` : `${name} bot: paper`, 5000); } catch (e) { SE.toast(e.message, 7000); draw(); }
    };

    function chip(c, kill) {
      if (kill) return '<span class="chip bad">Stopped: kill switch</span>';
      if (!c.enabled) return '<span class="chip">Off</span>';
      return c.real ? '<span class="chip bad" style="font-weight:700">REAL MONEY</span>' : '<span class="chip ok">Paper</span>';
    }
    function common(prefix, c, missing, realFree) {
      return row('Running', sw(prefix + 'On', c.enabled, 'Off = no new trades. Open positions still close.')) +
        row('Real money', sw(prefix + 'Real', c.real, missing.length ? 'Needs: ' + missing.join(', ') : 'Asks for a typed confirmation.') + (missing.length && !c.real ? `<div class="faint" style="font-size:12px;margin-top:4px">needs ${esc(missing.join(', '))}</div>` : '')) +
        row('Real bankroll', numIn(prefix + 'Bank', c.bankroll, 10, 'How much of the shared accounts this bot may use for real money: its open real positions can never cost more than this plus its real results so far. 0 = no real money.') + (realFree != null ? `<div class="faint" style="font-size:12px;margin-top:4px">${money(realFree)} free now</div>` : '')) +
        row('Paper starting balance', numIn(prefix + 'Start', c.start, 100, 'Changing it re-bases the paper wallet.')) +
        row('Most per trade', numIn(prefix + 'MaxT', c.max_trade, 10)) +
        row('Most open at once', numIn(prefix + 'MaxO', c.max_open, 100)) +
        row('Daily loss limit (real)', numIn(prefix + 'Loss', c.daily_loss, 10, 'Real money switches back to paper when today\'s real result is below minus this.'));
    }
    const commonEdit = (prefix) => ({ bankroll: +$('#' + prefix + 'Bank').value, start: +$('#' + prefix + 'Start').value, max_trade: +$('#' + prefix + 'MaxT').value, max_open: +$('#' + prefix + 'MaxO').value, daily_loss: +$('#' + prefix + 'Loss').value });

    function draw() {
      if (!d || !$('#btSnipe')) return;
      const s = d.settings, sn = d.snipe, ar = d.arb, k = d.keys;
      $('#btKill').innerHTML = `<button class="btn ${s.kill ? 'primary' : ''}" id="btKillB" data-tip="Stops every bot at once. Open positions still close.">${ic(s.kill ? 'play_arrow' : 'block')}${s.kill ? 'Resume bots' : 'Kill switch'}</button>`;
      $('#btKillB').onclick = () => save({ kill: !s.kill }, s.kill ? 'Bots resumed' : 'Kill switch ON');
      const realOn = (s.snipe.real || s.arb.real) && !s.kill;
      $('#btBanner').innerHTML = realOn ? `<div class="note" style="border:1px solid var(--down);background:var(--down-soft);padding:10px 14px;border-radius:10px;margin-bottom:14px"><b class="down">Real money is ON</b> for ${[s.snipe.real && 'the snipe bot', s.arb.real && 'the hedge & arbitrage bot'].filter(Boolean).join(' and ')}. Orders are placed with your keys.</div>` : '';
      P.set('btS', money(sn.paper?.balance), `${pnl(sn.paper?.closed_pnl)} closed · ${sn.paper?.trades ?? 0} trades, ${sn.paper?.trades ? Math.round(100 * sn.paper.wins / sn.paper.trades) : 0}% won · today ${pnl(sn.paper?.today)}`);
      P.set('btA', money(ar.paper?.balance), `${pnl(ar.paper?.realized)} realized · ${ar.paper?.positions ?? 0} positions · today ${pnl(ar.paper?.today)}`);
      P.set('btR', pnl((sn.real?.today || 0) + (ar.real?.today || 0)), `snipe ${pnl(sn.real?.today)} · arbitrage ${pnl(ar.real?.today)}`);
      P.set('btX', money(s.game_cap, 0), `per game · reserves ${money(s.pm_reserve, 0)} Polymarket / ${money(s.kalshi_reserve, 0)} Kalshi`);
      $('#btSnipeChip').innerHTML = chip(s.snipe, s.kill);
      $('#btArbChip').innerHTML = chip(s.arb, s.kill);

      const r = sn.rules || {};
      const sh = sn.shadow || {};
      const st = sn.status || {};
      $('#btSnipe').innerHTML =
        `<div class="stats" style="margin:0 0 10px">` +
        `<div class="stat"><div class="stat-label">Cash free</div><div class="stat-value">${money(sn.paper?.cash)}</div><div class="stat-foot">open ${money(sn.paper?.open_cost)}</div></div>` +
        `<div class="stat" data-tip="Same moves, same rules, but 1 bookmaker is enough (paper only): the live A/B test of that rule."><div class="stat-label">1-bookmaker shadow</div><div class="stat-value">${pnl(sh.closed_pnl)}</div><div class="stat-foot">${sh.trades ?? 0} trades, ${sh.trades ? Math.round(100 * sh.wins / sh.trades) : 0}% won</div></div>` +
        `<div class="stat" data-tip="Bookmaker moves of 4+ points seen and checked live."><div class="stat-label">Moves checked</div><div class="stat-value">${st.candidates ?? 0}</div><div class="stat-foot">entered ${Object.entries(st.entered || {}).map(([b, n]) => `${esc(b)} ${n}`).join(', ') || 'none yet'}</div></div>` +
        `</div>` +
        `<p class="note" style="margin:0 0 8px">Rules now: ${r.min_books ?? '-'}+ bookmakers, ${pct(r.min_move, 0)} move, Polymarket ≤ ${pct(r.max_followed, 0)} followed, gain ≥ ${Math.round((r.min_edge || 0) * 100)}¢, spread ≤ ${Math.round((r.max_spread || 0) * 100)}¢; sell at ${pct(r.catch, 0)} caught up, give up after ${Math.round((r.give_up_s || 0) / 60)} min${r.skip_after_pause ? '; skip after a pause' : ''}.</p>` +
        common('sn', s.snipe, k.missing_snipe, sn.real?.free) +
        row('Share of balance per trade', numIn('snPct', +(s.snipe.pct * 100).toFixed(2), 0.1, 'Percent of the bot\'s balance staked on each trade.') + ' %') +
        row('Size by expected edge', sw('snEdge', s.snipe.edge_sizing, 'Stake x clip(expected gain / 4¢, 0.5, 3). In the replay: +19.4% vs +17.9% a $.')) +
        row('Run the 1-bookmaker shadow', sw('snShadow', s.snipe.shadow_one_book, 'Paper-only comparison bot.')) +
        `<div style="margin-top:10px"><button class="btn primary" id="snSave">${ic('save')}Save snipe settings</button></div>`;
      $('#snOn').onchange = (e) => save({ snipe: { enabled: e.target.checked } });
      $('#snReal').onchange = (e) => setReal('snipe', e.target.checked);
      $('#snSave').onclick = () => save({ snipe: { ...commonEdit('sn'), pct: +$('#snPct').value / 100, edge_sizing: $('#snEdge').checked, shadow_one_book: $('#snShadow').checked } });

      const sc = ar.scan || {};
      const rej = Object.entries(sc.rejected || {}).sort((a, b) => b[1] - a[1]).slice(0, 6).map(([kk, v]) => `${kk.replace(/_/g, ' ')} ${v}`).join(', ');
      $('#btArb').innerHTML =
        `<div class="stats" style="margin:0 0 10px">` +
        `<div class="stat"><div class="stat-label">Cash free</div><div class="stat-value">${money(ar.paper?.cash)}</div><div class="stat-foot">open ${money(ar.paper?.open_cost)}</div></div>` +
        `<div class="stat" data-tip="Positions sold early because both legs fetched their locked profit before the payout."><div class="stat-label">Auto-unwound</div><div class="stat-value">${sc.unwound ?? 0}</div><div class="stat-foot">since the last restart</div></div>` +
        `<div class="stat" data-tip="Scans run on every book change on either venue (at most every 150 ms), else every 2 s."><div class="stat-label">Scans</div><div class="stat-value">${(sc.scans ?? 0).toLocaleString()}</div><div class="stat-foot">${sc.watching ?? 0} games on both · gaps ${sc.raw_gaps ?? 0}</div></div>` +
        `</div>` +
        (rej ? `<p class="note" style="margin:0 0 8px">Gaps not taken: ${esc(rej)}.</p>` : '') +
        common('ar', s.arb, k.missing_arb, ar.real?.free) +
        row('Least return, live games', numIn('arLive', +(s.arb.min_live * 100).toFixed(2), 0.1) + ' %') +
        row('Least return, pre-match', numIn('arPre', +(s.arb.min_pre * 100).toFixed(2), 0.1, 'Pre-match money waits ~5x longer per % and carries the called-off risk.') + ' %') +
        row('Least return of the last pair', numIn('arPair', +(s.arb.min_pair * 100).toFixed(2), 0.1, 'How deep into the books an order may walk.') + ' %') +
        row('Auto-unwind at', numIn('arUnw', Math.round(s.arb.unwind_at * 100), 5, '100 = sell both legs as soon as they fetch the full locked profit (just sooner than the payout). 0 = never.') + ' % of the locked profit') +
        row('Live games only', sw('arLiveOnly', s.arb.live_only, 'Skip pre-match gaps.')) +
        `<div style="margin-top:10px"><button class="btn primary" id="arSave">${ic('save')}Save arbitrage settings</button></div>`;
      $('#arOn').onchange = (e) => save({ arb: { enabled: e.target.checked } });
      $('#arReal').onchange = (e) => setReal('arb', e.target.checked);
      $('#arSave').onclick = () => save({ arb: { ...commonEdit('ar'), min_live: +$('#arLive').value / 100, min_pre: +$('#arPre').value / 100, min_pair: +$('#arPair').value / 100, unwind_at: +$('#arUnw').value / 100, live_only: $('#arLiveOnly').checked } });

      $('#btShared').innerHTML =
        row('Most at risk in one game (both bots)', numIn('shCap', s.game_cap, 100, 'Open snipe trades plus open arbitrage positions on the same game.')) +
        row('Cash kept free on Polymarket (real)', numIn('shPm', s.pm_reserve, 10)) +
        row('Cash kept free on Kalshi (real)', numIn('shK', s.kalshi_reserve, 10)) +
        `<div style="margin-top:10px"><button class="btn" id="shSave">${ic('save')}Save shared limits</button></div>`;
      $('#shSave').onclick = () => save({ game_cap: +$('#shCap').value, pm_reserve: +$('#shPm').value, kalshi_reserve: +$('#shK').value });

      $('#btKeys').innerHTML =
        row('Polymarket API key, secret, passphrase', (k.pm_api && k.pm_secret_ok ? '<span class="chip ok">Saved</span>' : `<span class="chip bad">${!k.pm_secret_ok ? 'API secret is not a Polymarket secret' : 'Passphrase missing'}</span>`) +
          (k.pm_trading_address ? ` <button class="btn sm" id="pkApi" data-tip="Asks Polymarket for the API key, secret and passphrase that belong to your trading key (it signs a 'this is my wallet' message; no order, no money) and saves them.">${ic('key')}Get them from the trading key</button>` : ''), 'Polymarket\'s API credentials: a key (an id), a secret (base64, about 44 characters) and a passphrase. They let the bots read your balance and place orders. The trading key can fetch them.') +
        row('Polymarket wallet (funder)', k.pm_wallet ? `<span class="mono">${esc(k.pm_wallet)}</span>` : '<a class="btn sm" href="#/settings">Add in Settings</a>') +
        row('Polymarket trading key', `${k.pm_trading_address ? `<span class="chip ok">Saved</span> <span class="mono faint">signs as ${esc(k.pm_trading_address)}</span>` : '<span class="chip bad">Missing</span>'}<div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap"><input class="input" id="pkIn" type="password" placeholder="Private key of the signer (hex)" style="width:300px" autocomplete="off"><select class="input" id="pkType" style="width:170px" data-tip="Google / email sign-ups now get a deposit wallet. Older email logins: Polymarket proxy. Browser wallet: Safe. Plain wallet: EOA."><option value="3">Deposit wallet (Google / email)</option><option value="1">Polymarket proxy (older email login)</option><option value="2">Browser-wallet Safe</option><option value="0">Plain wallet (EOA)</option></select><button class="btn sm" id="pkSave">Save</button></div>`, 'Signs Polymarket orders. It never leaves this computer and is never shown again.') +
        row('Kalshi API key', k.kalshi ? '<span class="chip ok">Saved</span>' : '<a class="btn sm" href="#/kalshi">Add on the Kalshi page</a>');
      if ($('#pkApi')) $('#pkApi').onclick = async () => {
        try { const v = await api('/pm/api-from-key', { method: 'POST' }); SE.toast(v.has_api && v.api_secret_ok ? 'API key, secret and passphrase saved' : 'Saved, but something is still missing', 6000); load(); loadAcc(); } catch (e) { SE.toast(e.message, 8000); }
      };
      $('#pkSave').onclick = async () => {
        const v = $('#pkIn').value.trim();
        if (!v) { SE.toast('Paste the key first'); return; }
        try { await api('/pm/settings', { body: { private_key: v, signature_type: +$('#pkType').value } }); $('#pkIn').value = ''; SE.toast('Trading key saved'); load(); } catch (e) { SE.toast(e.message, 6000); }
      };

      drawTrades();
      const now = Date.now();
      SE.table($('#btArT'), [
        { key: 't', label: 'Opened', w: 130, fmt: (p) => `<span class="muted">${SE.fmtDateTime(p.ts)}</span>${p.real ? ' <span class="chip bad">REAL</span>' : ''}`, sort: (p) => p.ts },
        { key: 'm', label: 'Match', cls: 'match', fmt: (p) => P.matchCell(p.home, p.away, '', p.sport, p.match_id) + (p.live ? ' <span class="chip">live</span>' : '') },
        { key: 'l', label: 'Legs', w: 250, fmt: (p) => `${Math.round(p.contracts)} × Yes ${esc(p.outcome)} ${(p.yes * 100).toFixed(1)}¢ on ${SE.bookName(p.yes_on)} + No ${(p.no * 100).toFixed(1)}¢${p.levels ? `<div class="faint">${p.levels[0]} + ${p.levels[1]} book levels</div>` : ''}` },
        { key: 's', label: 'Staked', cls: 'r', w: 100, fmt: (p) => `${money(p.cost)}${p.fees != null ? `<div class="faint">fees ${money(p.fees)}</div>` : ''}` },
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
          try { const x = await api('/predarb/sell/' + b.dataset.sell, { method: 'POST' }); SE.toast(x.ok ? `Sold: ${x.position.pnl >= 0 ? '+' : '-'}${money(Math.abs(x.position.pnl))}` : x.error, 6000); } catch (err) { SE.toast(err.message, 6000); }
          load();
        }, true);
      }
      SE.table($('#btOrd'), [
        { key: 't', label: 'When', w: 140, fmt: (o) => `<span class="muted">${SE.fmtDateTime(o.ts)}</span>`, sort: (o) => o.ts },
        { key: 'b', label: 'Bot', w: 90, fmt: (o) => esc(o.bot) },
        { key: 'v', label: 'Venue', w: 110, fmt: (o) => SE.book(o.venue, 'sm') },
        { key: 'k', label: 'Result', w: 90, fmt: (o) => (o.ok ? '<span class="chip ok">ok</span>' : '<span class="chip bad">failed</span>') },
        { key: 'd', label: 'Order', fmt: (o) => `<span class="mono faint" style="font-size:12px">${esc(JSON.stringify(o.data).slice(0, 220))}</span>` },
      ], { sort: 't', empty: 'No real orders yet. Both bots trade paper until switched to Real.' }).set(d.orders || []);
      $('#btEv').innerHTML = (d.events || []).length ? d.events.map((e) => `<div class="set-row"><span class="mono faint">${SE.fmtDateTime(e.ts)}</span><div>${esc(e.msg)}</div></div>`).join('') : '<div class="empty">No events since the last restart.</div>';
    }
    function drawTrades() {
      if (!d || !$('#btSnT')) return;
      const rows = snTab === 'shadow' ? d.snipe.shadow_trades : d.snipe.trades;
      SE.table($('#btSnT'), [
        { key: 't', label: 'When', w: 130, fmt: (t) => `<span class="muted">${SE.fmtDateTime(t.ts)}</span>${t.real ? ' <span class="chip bad">REAL</span>' : ''}`, sort: (t) => t.ts },
        { key: 'm', label: 'Match', cls: 'match', fmt: (t) => P.matchCell(t.home, t.away, '', t.sport, t.match_id) },
        { key: 'o', label: 'Bet', w: 120, fmt: (t) => `${t.dir > 0 ? 'Yes' : 'No'} <b>${esc(t.outcome)}</b><div class="faint">${esc(t.trigger)} · ${t.books?.[1] ?? '-'} books</div>` },
        { key: 's', label: 'Stake', cls: 'r', w: 100, fmt: (t) => `${money(t.cost)}${t.edge_x && t.edge_x !== 1 ? `<div class="faint">x${t.edge_x.toFixed(2)} edge</div>` : ''}`, sort: (t) => t.cost },
        { key: 'e', label: 'Entry → exit', cls: 'r', w: 130, fmt: (t) => `${(t.entry * 100).toFixed(1)}¢ → ${t.exit != null ? (t.exit * 100).toFixed(1) + '¢' : '…'}` },
        { key: 'x', label: 'Exit', w: 100, fmt: (t) => (t.open ? '<span class="chip accent">open</span>' : `<span class="muted">${esc(t.reason || '')}</span>`) },
        { key: 'p', label: 'P&amp;L', cls: 'r', w: 100, fmt: (t) => pnl(t.pnl), sort: (t) => t.pnl ?? 0 },
      ], { sort: 't', empty: snTab === 'shadow' ? 'No shadow trades yet.' : 'No live snipe trades yet. The bot trades bookmaker moves as they happen.' }).set(rows || []);
    }
    async function load() {
      try { d = await api('/bots'); draw(); } catch (e) { if ($('#btSnipe')) $('#btSnipe').innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
    }
    async function loadAcc() {
      if (!$('#btAcc')) return;
      let a;
      try { a = await api('/bots/accounts'); } catch (e) { $('#btAcc').innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
      if (!$('#btAcc')) return;
      if (!a.keys) { $('#btAcc').innerHTML = '<div class="empty">No account keys saved: both bots are paper only.</div>'; return; }
      const v = (x, name) => `<div class="stat"><div class="stat-label">${SE.book(name, 'sm')}</div><div class="stat-value">${x.cash != null ? money(x.cash) : '<span class="faint">-</span>'}</div><div class="stat-foot">${x.error ? esc(x.error) : `free for the bots ${money(Math.max(0, (x.cash || 0) - x.reserve))} after the ${money(x.reserve, 0)} reserve`}${x.other_positions ? ` · ${x.other_positions} other positions` : ''}</div></div>`;
      $('#btAcc').innerHTML = `<div class="stats" style="margin:0 0 10px">${v(a.polymarket, 'polymarket')}${v(a.kalshi, 'kalshi')}</div>` +
        (a.problems ? `<p class="note down" style="margin:0 0 8px">${a.problems} position(s) the bots hold are MISSING on the account: check them by hand.</p>` : '') + '<div id="btAccT"></div>';
      SE.table($('#btAccT'), [
        { key: 'v', label: 'Venue', w: 110, fmt: (r) => SE.book(r.venue, 'sm') },
        { key: 'i', label: 'Contract', fmt: (r) => `<span class="mono faint" style="font-size:12px">${esc(r.id.length > 24 ? r.id.slice(0, 10) + '…' + r.id.slice(-8) : r.id)}</span>` },
        { key: 'b', label: 'Bots', w: 130, fmt: (r) => esc(r.bots.join(', ')) },
        { key: 'n', label: 'Bots hold', cls: 'r', w: 100, fmt: (r) => r.bots_hold.toFixed(0) },
        { key: 'h', label: 'Account holds', cls: 'r', w: 120, fmt: (r) => (r.account_holds == null ? '-' : r.account_holds.toFixed(0)) },
        { key: 's', label: 'Check', w: 220, fmt: (r) => `<span class="chip ${r.status === 'matches' ? 'ok' : r.status.startsWith('MISSING') ? 'bad' : ''}">${esc(r.status)}</span>` },
      ], { empty: 'No open real-money positions.' }).set(a.rows || []);
    }
    // ---- test runs & diagnostics
    let testing = false;
    async function loadDiag() {
      if (!$('#btDiag') || testing) return;
      let g;
      try { g = await api('/bots/tests'); } catch (e) { $('#btDiag').innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
      if (!$('#btDiag') || testing) return;
      const m = g.summary || {};
      const ms = (x) => (x == null ? '<span class="faint">-</span>' : `${Math.round(x)}<small>ms</small>`);
      const c = (x) => (x == null ? '<span class="faint">-</span>' : `${(x * 100).toFixed(1)}<small>¢</small>`);
      $('#btDiag').innerHTML = `<div class="stats" style="margin:0 0 12px">` +
        `<div class="stat" data-tip="Median time from sending a Polymarket order to its reply."><div class="stat-label">${SE.book('polymarket', 'sm')} order latency</div><div class="stat-value">${ms(m.pm_latency_ms)}</div><div class="stat-foot">slippage ${c(m.pm_slippage)} median</div></div>` +
        `<div class="stat" data-tip="Median time from sending a Kalshi order to its reply."><div class="stat-label">${SE.book('kalshi', 'sm')} order latency</div><div class="stat-value">${ms(m.kalshi_latency_ms)}</div><div class="stat-foot">slippage ${c(m.kalshi_slippage)} median</div></div>` +
        `<div class="stat" data-tip="Polymarket holds orders on live sports games before matching them: the median wait from its first reply to the match. Makers can move their prices during it, which is what a snipe has to beat."><div class="stat-label">${SE.book('polymarket', 'sm')} matching delay</div><div class="stat-value">${ms(m.pm_match_delay_ms)}</div><div class="stat-foot">live sports · shares settle in ${ms(m.pm_settle_ms)}</div></div>` +
        `<div class="stat" data-tip="Arbitrage: time between the Polymarket leg's reply and the Kalshi order leaving: how long one leg is unhedged."><div class="stat-label">Gap between legs</div><div class="stat-value">${ms(m.leg_gap_ms)}</div><div class="stat-foot">cash check ${ms(m.claim_ms)}</div></div>` +
        `<div class="stat"><div class="stat-label">Tests</div><div class="stat-value">${m.count ?? 0}</div><div class="stat-foot">${m.ok ?? 0} complete · cost ${pnl(m.net_total)}</div></div>` +
        `</div><div id="btDiagT"></div>`;
      const left = (t) => {
        if (t.sold_back) return 0;
        let b = 0, s2 = 0;
        for (const l of t.legs || []) if (l.venue === 'polymarket') { if (l.side === 'buy') b += l.filled || 0; else s2 += l.filled || 0; }
        return Math.floor(b - s2);
      };
      const legs = (t) => (t.legs || []).map((l) => `<div style="font-size:12.5px">${SE.book(l.venue, 'sm')} ${esc(l.side)} ${l.filled != null ? Math.round(l.filled) : 0}/${Math.round(l.wanted || 0)}${l.avg ? ` @ ${(l.avg * 100).toFixed(1)}¢` : ''} <span class="faint">(saw ${(l.expected * 100).toFixed(1)}¢${l.latency_ms != null ? `, ${l.latency_ms} ms${l.match_delay_ms ? ` + ${l.match_delay_ms} ms matching delay` : ''}${l.settle_wait_ms ? `, waited ${l.settle_wait_ms} ms for shares to settle` : ''}${l.venue_fee_bps != null ? `, fee ${l.venue_fee_bps} bps` : ''}, prep ${l.prep_ms ?? 0} ms` : ''})</span>${l.error ? ` <span class="down">${esc(l.error.slice(0, 120))}</span>` : ''}</div>`).join('');
      SE.table($('#btDiagT'), [
        { key: 't', label: 'When', w: 130, fmt: (t) => `<span class="muted">${SE.fmtDateTime(t.ts)}</span>`, sort: (t) => t.ts },
        { key: 'k', label: 'Test', w: 110, fmt: (t) => `${esc(t.kind || '')}${t.live ? ' <span class="chip">live</span>' : ''}<div>${t.ok ? '<span class="chip ok">complete</span>' : '<span class="chip bad">incomplete</span>'}</div>` },
        { key: 'm', label: 'Market at the decision', w: 200, fmt: (t) => (t.market ? `<div class="faint" style="font-size:12px">${t.kind === 'arb' ? `pair ${(t.market.pair_cost * 100).toFixed(1)}¢ · books ${t.market.pm_book_age_ms} / ${t.market.kalshi_book_age_ms} ms old` : `spread ${(t.market.spread * 100).toFixed(1)}¢ · book ${t.market.book_age_ms} ms old`} · min ${t.market.min_size} shares</div>` : '') },
        { key: 'l', label: 'Orders', fmt: legs },
        { key: 'g', label: 'Leg gap', cls: 'r', w: 90, fmt: (t) => (t.leg_gap_ms != null ? `${t.leg_gap_ms} ms` : '-') },
        { key: 'n', label: 'Net', cls: 'r', w: 90, fmt: (t) => pnl(t.net), sort: (t) => t.net ?? 0 },
        { key: 'x', label: '', w: 110, fmt: (t) => (left(t) >= 1 ? `<button class="btn sm" data-sb="${t.id}" data-tip="This test left ${left(t)} Polymarket shares on the account. Sells them into the current bid (2¢ under).">${ic('sell')}Sell back ${left(t)}</button>` : '') },
      ], { sort: 't', empty: 'No test runs yet. Each button places one real round trip at the smallest size.' }).set((g.tests || []).map((t) => ({ ...t, error: undefined })));
      if (!$('#btDiagT').dataset.wired) {
        $('#btDiagT').dataset.wired = '1';
        $('#btDiagT').addEventListener('click', async (e) => {
          const b = e.target.closest('[data-sb]');
          if (!b) return;
          e.stopPropagation();
          if (!window.confirm('Sell these test shares back now, into the current bid (2¢ under)? This is a real order.')) return;
          b.disabled = true;
          try { const r = await api('/bots/test-sellback/' + b.dataset.sb, { method: 'POST' }); const f = (r.sold || [])[0]; SE.toast(f ? `Sold ${Math.round(f.filled || 0)} of ${Math.round(f.wanted || 0)}${f.avg ? ' at ' + (f.avg * 100).toFixed(1) + '¢' : ''}${f.error ? ': ' + f.error : ''}` : 'Nothing left to sell', 7000); } catch (err) { SE.toast(err.message, 7000); }
          loadDiag(); loadAcc();
        }, true);
      }
      const errs = (g.tests || []).filter((t) => t.error).slice(0, 1);
      if (errs.length) $('#btDiag').insertAdjacentHTML('beforeend', `<p class="note" style="margin-top:8px">Last problem: ${esc(errs[0].error)}</p>`);
    }
    const runTest = async (bot) => {
      const what = bot === 'arb' ? 'an arbitrage pair (Polymarket then Kalshi), sold straight back' : 'one Polymarket buy, sold straight back';
      const conf = window.prompt(`REAL MONEY test: ${what}, at the smallest size the venue allows (about $1-$5). It costs the spread and fees.\n\nType TEST to place it.`) || '';
      if (conf.trim() !== 'TEST') { SE.toast('Not run: confirmation not typed'); return; }
      testing = true;
      $('#btDiag').innerHTML = `<div class="sim-load"><div class="lbl"><span class="dot"></span>Placing the real test orders and timing them</div><div class="sim-bar indet"><i></i></div><div class="sk-row"><div class="sk"></div><div class="sk"></div><div class="sk"></div><div class="sk"></div></div></div>`;
      try { const r = await api('/bots/test/' + bot, { body: { confirm: 'TEST' } }); SE.toast(r.ok ? `Test done: net ${r.net >= 0 ? '+' : '-'}${money(Math.abs(r.net || 0))}` : `Test incomplete: ${r.error}`, 7000); } catch (e) { SE.toast(e.message, 7000); }
      testing = false;
      loadDiag();
      loadAcc();
    };
    $('#tArb').onclick = () => runTest('arb');
    $('#tSn').onclick = () => runTest('snipe');

    // ---- real test run
    let runChart = null, runSel = null;
    async function loadRun() {
      if (!$('#btRun')) return;
      let g;
      try { g = await api('/bots/runs'); } catch (e) { return; }
      if (!$('#btRun')) return;
      const cur = g.current, last = cur || (g.past || [])[0];
      $('#btRunTools').innerHTML = cur
        ? `<span class="chip bad" style="font-weight:700">REAL · ends ${SE.fmtDateTime(cur.end_ms)}</span> <button class="btn sm" id="runExtend">Extend…</button> <button class="btn sm" id="runStop">${ic('stop')}Stop now</button>`
        : [4, 8, 16, 24].map((h) => `<button class="btn sm" data-run="${h}">${ic('play_arrow')}${h} h</button>`).join(' ');
      const rep = (r, live) => `<div class="stats" style="margin:0 0 10px">` +
        `<div class="stat key"><div class="stat-label">${ic('payments')}${live ? 'Result so far' : 'Result'}</div><div class="stat-value">${pnl(r.pnl)}</div><div class="stat-foot">on ${money(r.staked)} staked · $${r.bankroll} bankroll</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('swap_horiz')}Trades</div><div class="stat-value">${r.trades}</div><div class="stat-foot">${r.closed} closed, ${r.wins} won, ${r.open} open</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('bolt')}Signals</div><div class="stat-value">${(r.signals || []).reduce((a, x) => a + x.n, 0)}</div><div class="stat-foot">${esc((r.signals || []).slice(0, 4).map((x) => `${x.result.replace(/_/g, ' ')} ${x.n}`).join(' · '))}</div></div>` +
        `<div class="stat"><div class="stat-label">${ic('schedule')}Window</div><div class="stat-value" style="font-size:18px">${SE.fmtDateTime(r.start_ms)}</div><div class="stat-foot">to ${SE.fmtDateTime(r.end_ms)}${r.ended ? ' · ' + esc(r.ended) : ''}</div></div></div>`;
      const markets = last ? [...(last.list || []).map((t) => ({ match_id: t.match_id, outcome: t.outcome, home: t.home, away: t.away, n: 'trade' })), ...(last.markets || [])] : [];
      const seen = new Set();
      const mk = markets.filter((m) => { const k = m.match_id + m.outcome; if (seen.has(k)) return false; seen.add(k); return true; });
      $('#btRun').innerHTML = (last ? rep(last, !!cur) : '<p class="note" style="margin:0 0 8px">No test run yet. Start one above: it trades real money ($40, about $12 a trade) for the hours chosen.</p>') +
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
        { key: 'n', label: 'Trades', cls: 'r', w: 90, fmt: (r) => `${r.trades} (${r.wins} won)` },
        { key: 's', label: 'Staked', cls: 'r', w: 100, fmt: (r) => money(r.staked) },
        { key: 'p', label: 'Result', cls: 'r', w: 100, fmt: (r) => pnl(r.pnl) },
        { key: 'e', label: 'Ended', fmt: (r) => `<span class="muted">${esc(r.ended || '')}</span>` },
      ], { sort: 't' }).set(g.past);
      if ($('#runExtend')) $('#runExtend').onclick = async () => {
        const h = Math.round(+(window.prompt('Extend the real test run by how many hours? (1-48)', '4') || 0));
        if (!h) return;
        try { await api('/bots/run/extend', { body: { hours: h } }); SE.toast(`Extended by ${h} h`); } catch (e) { SE.toast(e.message, 6000); }
        load(); loadRun();
      };
      if ($('#runStop')) $('#runStop').onclick = async () => {
        if (!window.confirm('Stop the real test run now? Open trades are still sold by the bot.')) return;
        try { await api('/bots/run/stop', { method: 'POST' }); SE.toast('Test run stopped'); } catch (e) { SE.toast(e.message, 6000); }
        load(); loadRun();
      };
      document.querySelectorAll('[data-run]').forEach((b) => {
        b.onclick = async () => {
          const h = +b.dataset.run;
          const c = window.prompt(`Start a ${h}-hour REAL MONEY test run of the snipe bot?\n\n$40 of your Polymarket account, about $12 a trade, at most $15 lost before it stops. It ends by itself after ${h} hours and reports.\n\nType REAL MONEY to start.`) || '';
          if (c.trim() !== 'REAL MONEY') { SE.toast('Not started'); return; }
          try { await api('/bots/run/start', { body: { hours: h, confirm: c } }); SE.toast(`${h} h real test run started`, 6000); } catch (e) { SE.toast(e.message, 7000); }
          load(); loadRun();
        };
      });
    }
    async function drawRunChart(r) {
      const el = $('#runChart');
      if (!el || !r) return;
      const [m, o] = ($('#runMk')?.value || '').split('|');
      let d;
      try { d = await api(`/bots/run/chart?m=${m}&o=${encodeURIComponent(o)}&from=${r.start_ms}&to=${r.end_ms}`); } catch (e) { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return; }
      const mins = d.minutes || [];
      if (!mins.length) { el.innerHTML = '<div class="empty">No Polymarket prices recorded for this market in the window.</div>'; return; }
      const xs = mins.map((x) => x[0]);
      const at = (t) => { const i = xs.findIndex((x) => x > t); return i < 0 ? xs.length - 1 : Math.max(0, i - 1); };
      const blank = () => xs.map(() => null);
      const sig = blank(), buy = blank(), sell = blank();
      for (const s2 of d.signals || []) { const i = at(s2.ts); const q = mins[i]; sig[i] = q[1] != null && q[2] != null ? (q[1] + q[2]) / 2 : (q[1] ?? q[2]); }
      for (const t of d.trades || []) {
        const yes = t.dir > 0;
        buy[at(t.ts)] = yes ? t.entry : 1 - t.entry;
        if (t.exit_ts) sell[at(t.exit_ts)] = yes ? t.exit : 1 - t.exit;
      }
      const css = (n, f) => getComputedStyle(document.documentElement).getPropertyValue(n).trim() || f;
      SE.destroyChart(runChart);
      runChart = SE.chart(el, xs, [
        { label: 'Bid', color: css('--text-3', '#888'), values: mins.map((x) => x[1]), width: 1.2 },
        { label: 'Ask', color: css('--text-2', '#aaa'), values: mins.map((x) => x[2]), width: 1.2 },
        { label: 'Signal', color: css('--text-3', '#888'), values: sig, points: true },
        { label: 'Buy', color: css('--up', '#50e4b2'), values: buy, points: true },
        { label: 'Sell', color: css('--down', '#ff5f57'), values: sell, points: true },
      ], { height: 260, yFmt: (v) => (v == null ? '' : (v * 100).toFixed(0) + '¢') });
    }

    load();
    loadRun();
    SE.every(30000, loadRun);
    loadSim(true);
    loadAcc();
    loadDiag();
    SE.every(30000, loadAcc);
    SE.every(5000, () => { if (!document.activeElement || !['INPUT', 'SELECT'].includes(document.activeElement.tagName)) load(); });
  },
};
})();
