// Swifteye pages, part 5: Suspicious (heavily bet CS2 games, pre-match line moves) and site search.
'use strict';
(() => {
const { $, $$, esc, ic, api, P } = SE;

const money = (x) => (x == null ? '-' : x >= 1e6 ? '$' + (x / 1e6).toFixed(1) + 'M' : x >= 1e3 ? '$' + (x / 1e3).toFixed(x >= 1e4 ? 0 : 1) + 'k' : '$' + Math.round(x));
const vrs = (r) => (r > 0 ? '#' + r : '<span class="faint">unranked</span>');
const hm = (ms) => { const m = Math.round(Math.abs(ms) / 60000); return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`; };

// ================================================================ Suspicious
SE.pages.suspicious = {
  title: 'Suspicious',
  render(el) {
    el.innerHTML = P.head('Suspicious', '<div id="sDays"></div>') +
      P.stats(
        P.stat('sA', 'local_fire_department', 'Heavily bet CS2 games', 'Lower-tier CS2 games at or above the alert score in the period.', true),
        P.stat('sL', 'trending_up', 'Pre-match line moves', 'Upcoming games where the bookmakers moved a side by the threshold or more.'),
        P.stat('sB', 'stacked_bar_chart', 'Normal games compared', 'Finished lower-tier CS2 games the scores are measured against.'),
        P.stat('sV', 'military_tech', 'VRS', 'Valve Regional Standings used to skip tier-1 games.')) +
      P.sec('local_fire_department', 'Heavily bet lower-tier CS2 games', '<div id="sAT"></div>', '<div id="sAF"></div>',
        'How heavily each Polymarket CS2 game was bet compared with normal lower-tier games. Click a game for its match page, or Polymarket when Swifteye has no match for it.') +
      `<p class="note" style="margin-top:14px">Score 0-100 = average percentile against finished lower-tier games of the baseline period on: total money traded, big underdog bets, big favourite bets and the share of money in big bets (drift from the first trade to the start counts half). From your Fixed / Clean marks these activity measures separated best (AUC about 0.75); account age, past profit and bet price did not help. Two caveats from that study: the marks were made looking at activity charts, so this is partly circular, and the money in Fixed games still did not know the result. A high score means a heavily bet lower-tier game, not proof of a fix. Tier 1 (a VRS top-20 team, or both in the top 30) is skipped.</p>` +
      P.sec('trending_up', 'Pre-match line moves', '<div id="sLT"></div>', '', 'Upcoming games where the bookmakers moved a side a lot before the start. Each independent bookmaker platform\'s opening price against its price now, with its margin removed.') +
      P.sec('tune', 'Thresholds', '<div id="sS"></div>', '', 'Telegram alerts for both use these. Switch the alerts on or off on the Telegram page.');
    // days = 0 means "Upcoming": only games that have not started yet.
    let days = SE.pref('susDays') ?? 7, showT1 = false, onlyFlag = false, data = null;
    SE.seg($('#sDays'), [[1, '24 h'], [7, '7 days'], [30, '30 days'], [0, 'Upcoming']], days, (v) => { days = +v; SE.setPref('susDays', days); load(); });
    $('#sAF').innerHTML = `<label class="muted" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="sOnly">Flagged only</label><label class="muted" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="sT1">Show tier 1</label>`;
    $('#sOnly').addEventListener('change', (e) => { onlyFlag = e.target.checked; draw(); });
    $('#sT1').addEventListener('change', (e) => { showT1 = e.target.checked; draw(); });
    const status = (r) => {
      const now = Date.now();
      if (!r.start_ms) return '';
      if (r.start_ms > now) return `<span class="muted">${SE.fmtKick(r.start_ms)}</span><div class="faint">in ${hm(r.start_ms - now)}</div>`;
      if (!r.final) return `<span class="chip accent">Live / settling</span><div class="faint">${SE.fmtKick(r.start_ms)}</div>`;
      return `<span class="muted">${SE.fmtKick(r.start_ms)}</span><div class="faint">finished</div>`;
    };
    const scoreCell = (r) => {
      if (r.tier1) return '<span class="chip">Tier 1</span>';
      if (r.score == null) return `<span class="faint" data-tip="Not enough normal games to compare against yet, or no trades.">-</span>`;
      const a = data.settings.alert_score;
      return `<span class="chip ${r.score >= a ? 'bad' : r.score >= a - 20 ? 'accent' : ''}">${Math.round(r.score)}</span>`;
    };
    const tA = SE.table($('#sAT'), [
      { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league, 'cs2'), sort: (r) => r.home },
      { key: 'k', label: 'Kick-off', fmt: status, sort: (r) => r.start_ms },
      { key: 'v', label: 'VRS', w: 110, tip: 'Valve Regional Standings rank of each team.', fmt: (r) => `${vrs(r.vrs[0])} <span class="faint">v</span> ${vrs(r.vrs[1])}` },
      { key: 'vol', label: 'Traded', cls: 'r', w: 90, tip: 'Total money traded on all of the game\'s Polymarket markets.', fmt: (r) => money(r.volume), sort: (r) => r.volume },
      { key: 'dog', label: 'Big dog', cls: 'r', w: 84, tip: 'Big bets bought on the underdog (size and price limits under Thresholds).', fmt: (r) => r.big_dog ?? '-', sort: (r) => r.big_dog },
      { key: 'fav', label: 'Big fav', cls: 'r', w: 84, tip: 'Big bets bought on the favourite (50% or more).', fmt: (r) => r.big_fav ?? '-', sort: (r) => r.big_fav },
      { key: 'sh', label: 'Big share', cls: 'r', w: 92, tip: 'Share of all the money that came in big bets.', fmt: (r) => (r.big_share == null ? '-' : Math.round(r.big_share * 100) + '%'), sort: (r) => r.big_share },
      { key: 'dr', label: 'Drift', cls: 'r', w: 80, tip: 'How far the match price moved from the first trade to the start, in points.', fmt: (r) => (r.drift == null ? '-' : Math.round(r.drift * 100)), sort: (r) => r.drift },
      { key: 's', label: 'Score', cls: 'r', w: 84, tip: 'Average percentile against normal lower-tier games (0-100).', fmt: scoreCell, sort: (r) => (r.tier1 ? -1 : r.score) },
    ], { sort: 's', empty: 'No CS2 games in this period yet. The first scan of the last 14 days takes a few minutes after a start.', emptyIcon: 'local_fire_department',
      onClick: (r) => SE.go('#/susgame/' + r.event_id) });
    const outName = (r) => (r.outcome === 'home' ? r.home : r.outcome === 'away' ? r.away : r.outcome === 'draw' ? 'Draw' : r.outcome);
    const tL = SE.table($('#sLT'), [
      { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league, r.sport), sort: (r) => r.home },
      { key: 'k', label: 'Kick-off', fmt: (r) => `<span class="muted">${SE.fmtKick(r.start_ms)}</span><div class="faint">${r.start_ms > Date.now() ? 'in ' + hm(r.start_ms - Date.now()) : 'started'}</div>`, sort: (r) => r.start_ms },
      { key: 'o', label: 'Shortened', w: 150, tip: 'The side the bookmakers made more likely.', fmt: (r) => `<b>${esc(outName(r))}</b>` },
      { key: 'p', label: 'Fair odds', w: 130, tip: 'Opening and current price with the bookmaker margin removed (median platform).', fmt: (r) => `${(1 / r.open_p).toFixed(2)} <span class="faint">→</span> <b>${(1 / r.now_p).toFixed(2)}</b>` },
      { key: 'mv', label: 'Move', cls: 'r', w: 84, tip: 'Implied probability points (median platform).', fmt: (r) => `<b class="up">+${(r.move * 100).toFixed(1)}</b>`, sort: (r) => r.move },
      { key: 'a', label: 'Platforms', cls: 'r', w: 96, tip: 'Independent bookmaker platforms that moved it this way by at least half the threshold, of those pricing the game.', fmt: (r) => `${r.agree} <span class="faint">of ${r.platforms}</span>`, sort: (r) => r.agree },
      { key: 'w', label: 'Moved', w: 130, tip: 'When the move happened (median platform crossing halfway), and how long before the start.', fmt: (r) => (r.moved_ms ? `${SE.fmtTime(r.moved_ms)}<div class="faint">${hm(r.start_ms - r.moved_ms)} before start</div>` : '-'), sort: (r) => r.moved_ms },
    ], { sort: 'mv', empty: 'No big pre-match moves in this period.', emptyIcon: 'trending_up', onClick: (r) => SE.go('#/match/' + r.match_id) });
    function draw() {
      if (!data) return;
      const a = data.settings.alert_score;
      let rows = data.activity.filter((r) => showT1 || !r.tier1);
      if (onlyFlag) rows = rows.filter((r) => !r.tier1 && r.score >= a);
      tA.set(rows);
      tL.set(data.lines);
      P.set('sA', data.activity.filter((r) => !r.tier1 && r.score >= a).length, `score ${a}+ · ${data.activity.filter((r) => !r.tier1).length} lower-tier games scanned`);
      P.set('sL', data.lines.length, `${data.settings.line_move_pts}+ points, ${data.settings.line_min_platforms}+ platforms`);
      P.set('sB', data.baseline_games, data.baseline_games < 10 ? 'Scores start at 10 games' : `last ${data.settings.baseline_days} days`);
      P.set('sV', data.vrs.teams ? data.vrs.teams + ' teams' : '-', data.vrs.date ? 'standings of ' + data.vrs.date : 'loading');
    }
    function settingsForm() {
      const s = data.settings;
      const f = (k, label, tip, step = 1, suffix = '') => `<div class="field"><label data-tip="${esc(tip)}">${label}</label><div style="display:flex;gap:8px;align-items:center"><input class="input" type="number" step="${step}" data-s="${k}" value="${s[k]}" style="width:110px">${suffix}</div></div>`;
      $('#sS').innerHTML = `<div class="grid g-2"><div>
        ${f('big_bet_usd', 'Big bet', 'A bet (fills of one wallet within 60 s) of at least this many dollars.', 50, '$')}
        ${f('underdog_below', 'Underdog below', 'A bet bought below this price is on the underdog.', 0.05, 'price (0.40 = 40%)')}
        ${f('alert_score', 'Alert score', 'Games at or above this score are flagged and sent to Telegram.', 5, 'of 100')}
        ${f('baseline_days', 'Normal games from', 'Finished lower-tier games of this many days form the baseline.', 1, 'days')}
      </div><div>
        ${f('vrs_top', 'Tier 1: any team in VRS top', 'A game with a team this high is tier 1 and skipped.', 1)}
        ${f('vrs_both', 'Tier 1: both teams in VRS top', 'A game with both teams this high is tier 1 and skipped.', 1)}
        ${f('line_move_pts', 'Line move', 'Smallest pre-match move (median platform), in probability points.', 1, 'points')}
        ${f('line_min_platforms', 'Platforms agreeing', 'Independent bookmaker platforms that must move it the same way.', 1)}
        ${f('line_window_h', 'Games starting within', 'Only games starting within this many hours are checked.', 1, 'hours')}
      </div></div><div style="margin-top:14px;display:flex;gap:10px;align-items:center"><button class="btn primary" id="sSave">${ic('save')}Save</button><span class="muted" id="sMsg">Scores update within a few minutes of saving.</span></div>`;
      $('#sSave').addEventListener('click', async () => {
        const body = Object.fromEntries($$('[data-s]').map((i) => [i.dataset.s, +i.value]));
        try { await api('/suspicious/settings', { body }); $('#sMsg').textContent = 'Saved'; load(); } catch (e) { $('#sMsg').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
      });
    }
    let formDone = false;
    async function load() {
      data = await api('/suspicious?days=' + (days || 1));
      if (!days) {
        const now = Date.now();
        data.activity = data.activity.filter((r) => r.start_ms > now);
        data.lines = data.lines.filter((r) => r.start_ms > now);
      }
      draw();
      if (!formDone) { settingsForm(); formDone = true; }
    }
    load();
    SE.every(30000, load);
  },
};

// ================================================================ Suspicious: one game
SE.pages.susgame = {
  title: 'Suspicious game',
  render(el, [id]) {
    el.innerHTML = P.head('Loading', '', ['Suspicious', '#/suspicious']) + `<div class="empty">${ic('hourglass_empty')}Fetching every trade from Polymarket</div>`;
    api('/suspicious/event/' + encodeURIComponent(id)).then((d) => {
      if (!$('#page') || location.hash.indexOf('#/susgame/') !== 0) return;
      const pts = d.price.filter((p) => p[1] > 0 && p[1] < 1);
      const W = 1000, H = 260;
      const t0 = Math.min(d.start_ms, ...pts.map((p) => p[0])), t1 = Math.max(d.start_ms + 1, ...pts.map((p) => p[0]));
      const x = (t) => ((t - t0) / (t1 - t0)) * W, y = (p) => H - p * H;
      const line = pts.map((p, i) => `${i ? 'L' : 'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join('');
      const dots = d.bets.filter((b) => b.match_market && b.side === 'BUY').map((b) => {
        const home = b.outcome === d.home, p0 = home ? b.price : 1 - b.price;
        return `<circle cx="${x(b.ts).toFixed(1)}" cy="${y(p0).toFixed(1)}" r="${Math.min(14, 3 + Math.sqrt(b.cash) / 8).toFixed(1)}" fill="${home ? 'var(--up)' : 'var(--down)'}" fill-opacity=".55"><title>${esc(b.outcome)} $${Math.round(b.cash)} at ${Math.round(b.price * 100)}%</title></circle>`;
      }).join('');
      const tot = Object.values(d.by_side).reduce((a, b) => a + b, 0);
      el.innerHTML = P.head(`${esc(d.home)} <span class="faint">v</span> ${esc(d.away)}`, `<a class="btn" target="_blank" rel="noopener" href="https://polymarket.com/event/${encodeURIComponent(d.slug)}">${ic('open_in_new')}Polymarket</a>`, ['Suspicious', '#/suspicious']) +
        `<p class="muted" style="margin-top:-18px">${esc(d.league)} · ${SE.fmtDateTime(d.start_ms)} · VRS ${d.vrs.map((r) => (r > 0 ? '#' + r : 'unranked')).join(' v ')}</p>` +
        P.sec('show_chart', `Match price: ${esc(d.home)} to win`, `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:${H}px;background:var(--bg-2);border-radius:10px">
            <line x1="0" x2="${W}" y1="${H / 2}" y2="${H / 2}" stroke="var(--line-2)" stroke-dasharray="4 4"/>
            <line x1="${x(d.start_ms)}" x2="${x(d.start_ms)}" y1="0" y2="${H}" stroke="var(--accent)" stroke-dasharray="3 3"><title>Start</title></line>
            <path d="${line}" fill="none" stroke="var(--text)" stroke-width="1.5" vector-effect="non-scaling-stroke"/>${dots}</svg>
          <div class="faint" style="display:flex;justify-content:space-between;gap:12px;margin-top:6px"><span>${SE.fmtDateTime(t0)}</span><span>Dashed: 50% and the start. Dots: big bets (green ${esc(d.home)}, red ${esc(d.away)}; size = amount).</span><span>${SE.fmtDateTime(t1)}</span></div>`) +
        P.sec('payments', 'Money bought per side (match market)', Object.entries(d.by_side).map(([k, v]) => `<div class="field" style="grid-template-columns:200px minmax(0,1fr) 120px"><b>${esc(k)}</b><div style="height:8px;background:var(--bg-3);border-radius:4px;overflow:hidden"><b style="display:block;height:100%;width:${tot ? (v / tot * 100).toFixed(1) : 0}%;background:var(--accent)"></b></div><span class="r">$${SE.fmtInt(Math.round(v))}</span></div>`).join('') || '<div class="empty">No trades.</div>') +
        P.sec('list', `Big bets ($${d.settings.big_bet_usd}+, every market of the game)`, '<div id="gB"></div>');
      SE.table($('#gB'), [
        { key: 't', label: 'When', fmt: (b) => `${SE.fmtTime(b.ts)}<div class="faint">${b.ts < d.start_ms ? 'before start' : 'in game'}</div>`, sort: (b) => b.ts },
        { key: 'm', label: 'Market', fmt: (b) => esc(b.market) },
        { key: 'o', label: 'Outcome', w: 170, fmt: (b) => `<span class="chip ${b.side === 'BUY' ? 'ok' : 'bad'}">${b.side === 'BUY' ? 'Buy' : 'Sell'}</span> ${esc(b.outcome)}` },
        { key: 'p', label: 'Price', cls: 'r', w: 80, fmt: (b) => Math.round(b.price * 100) + '%', sort: (b) => b.price },
        { key: 'c', label: 'Amount', cls: 'r', w: 100, fmt: (b) => '$' + SE.fmtInt(Math.round(b.cash)), sort: (b) => b.cash },
      ], { sort: 't', dir: 1, empty: 'No big bets.' }).set(d.bets);
    }).catch((e) => { el.innerHTML = P.head('Suspicious game', '', ['Suspicious', '#/suspicious']) + `<div class="empty err">${ic('error')}${esc(e.message)}</div>`; });
  },
};

// ================================================================ Data
SE.pages.data = {
  title: 'Data',
  render(el) {
    el.innerHTML = P.head('Data', `<span class="muted" id="dAt"></span><button class="btn" id="dRe">${ic('refresh')}Recount</button>`) +
      P.stats(P.stat('dT', 'hard_drive', 'On disk', 'The database plus its write-ahead log.', true), P.stat('dC', 'swap_vert', 'Price changes'), P.stat('dP', 'candlestick_chart', 'Prediction-market quotes', 'Polymarket and Kalshi top-of-book rows.'), P.stat('dS', 'date_range', 'Recorded span')) +
      P.sec('table_chart', 'Tables', '<div id="dTab"></div>', '', "Size includes each table's indexes. Click a table to see its rows.") +
      P.sec('data_object', 'Raw rows', `<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px"><div id="dSel"></div><input class="input mono" id="dW" placeholder="Filter, e.g. book = 'unibet' AND match_id = 739" style="flex:1;min-width:260px"><button class="btn" id="dPrev">${ic('chevron_left')}Newer</button><button class="btn" id="dNext">Older${ic('chevron_right')}</button><span class="muted" id="dPg"></span></div><div id="dRaw"></div>`) +
      P.sec('merge', "Merge another machine's data", '<div id="dJob"></div><div id="dFiles"></div>', `<button class="btn" id="dScan">${ic('search')}Scan again</button><button class="btn primary" id="dExp">${ic('ios_share')}Export a snapshot</button>`,
        "Each machine numbers games itself; merging maps them onto this database's games. Where both machines recorded the same source at the same time, this database's rows are kept, so merging twice adds nothing.") +
      `<p class="note" style="margin-top:12px">To combine the Mac and the PC: press <b>Export a snapshot</b> on one machine (a consistent copy goes into its <span class="mono">export</span> folder), copy that file into the other machine's <span class="mono">import</span> folder next to its database (or any folder listed below), then press <b>Merge</b> there. Copy a snapshot rather than the live database file, which may be mid-write.</p>` +
      P.sec('folder', 'Files', '<div id="dF"></div>');
    let stats = null, table = SE.pref('dataTable') || 'changes', page = 0;
    const PAGE = 50;
    const span = (a, b) => (a && b ? `${SE.fmtDateTime(a)} <span class="faint">→</span> ${SE.fmtDateTime(b)}` : '<span class="faint">-</span>');
    async function loadStats(fresh) {
      $('#dAt').textContent = 'Counting';
      stats = await api('/data' + (fresh ? '?fresh=true' : ''));
      if (!$('#dAt')) return;
      $('#dAt').textContent = 'Counted ' + SE.fmtAgo(stats.computed_ms) + ' · ' + stats.host;
      const total = stats.files.filter((f) => ['Database', 'Write-ahead log', 'Shared memory'].includes(f.name)).reduce((a, f) => a + f.bytes, 0);
      const T = Object.fromEntries(stats.tables.map((t) => [t.table, t]));
      P.set('dT', SE.fmtBytes(total), esc(stats.db_path));
      P.set('dC', SE.fmtInt(T.changes?.rows), T.changes ? SE.fmtBytes(T.changes.bytes) : '');
      P.set('dP', SE.fmtInt((T.pm_quotes?.rows || 0) + (T.kalshi_quotes?.rows || 0)), SE.fmtBytes((T.pm_quotes?.bytes || 0) + (T.kalshi_quotes?.bytes || 0)));
      P.set('dS', T.changes?.from_ms ? SE.fmtDur(T.changes.to_ms - T.changes.from_ms) : '-', T.changes?.from_ms ? 'since ' + SE.fmtDateTime(T.changes.from_ms) : '');
      const max = Math.max(1, ...stats.tables.map((t) => t.bytes));
      SE.table($('#dTab'), [
        { key: 't', label: 'Table', fmt: (t) => `<b class="mono">${esc(t.table)}</b>`, sort: (t) => t.table },
        { key: 'r', label: 'Rows', cls: 'r', w: 120, fmt: (t) => SE.fmtInt(t.rows), sort: (t) => t.rows },
        { key: 'b', label: 'Size', w: 240, fmt: (t) => `<div style="display:flex;align-items:center;gap:8px"><div style="flex:1;height:6px;background:var(--bg-3);border-radius:3px;overflow:hidden"><b style="display:block;height:100%;width:${(t.bytes / max * 100).toFixed(1)}%;background:var(--accent)"></b></div><span style="min-width:70px;text-align:right">${SE.fmtBytes(t.bytes)}</span></div>`, sort: (t) => t.bytes },
        { key: 'i', label: 'Indexes', cls: 'r', w: 100, fmt: (t) => `<span class="muted">${SE.fmtBytes(t.index_bytes)}</span>`, sort: (t) => t.index_bytes },
        { key: 'p', label: 'Per row', cls: 'r', w: 90, tip: 'Average bytes per row, indexes included.', fmt: (t) => (t.rows ? Math.round(t.bytes / t.rows) + ' B' : '-'), sort: (t) => (t.rows ? t.bytes / t.rows : 0) },
        { key: 's', label: 'Covers', w: 330, fmt: (t) => span(t.from_ms, t.to_ms), sort: (t) => t.from_ms },
      ], { sort: 'b', onClick: (t) => { table = t.table; page = 0; SE.setPref('dataTable', table); drawSel(); loadRaw(); $('#dRaw').scrollIntoView({ behavior: 'smooth', block: 'start' }); } }).set(stats.tables);
      $('#dF').innerHTML = `<table class="tbl compact"><tbody>${stats.files.map((f) => `<tr><td>${esc(f.name)}</td><td class="mono faint" style="white-space:normal;word-break:break-all">${esc(f.path)}</td><td class="r">${SE.fmtBytes(f.bytes)}</td></tr>`).join('')}</tbody></table>`;
      drawSel();
    }
    function drawSel() {
      if (!stats || !$('#dSel')) return;
      SE.select($('#dSel'), { options: stats.tables.map((t) => [t.table, t.table]), value: table, onChange: (v) => { table = v; page = 0; SE.setPref('dataTable', table); loadRaw(); } });
    }
    async function loadRaw() {
      const w = $('#dW').value.trim();
      const sql = `SELECT * FROM "${table.replace(/"/g, '')}"${w ? ' WHERE ' + w : ''} ORDER BY rowid DESC LIMIT ${PAGE} OFFSET ${page * PAGE}`;
      $('#dPg').textContent = `rows ${page * PAGE + 1}-${(page + 1) * PAGE}, newest first`;
      let rows;
      try { rows = await SE.sql(sql, PAGE); } catch (e) { $('#dRaw').innerHTML = `<div class="empty err">${ic('error')}${esc(e.message)}</div>`; return; }
      if (!rows.length) { $('#dRaw').innerHTML = `<div class="empty">${ic('inbox')}No rows.</div>`; return; }
      const cols = Object.keys(rows[0]);
      const cell = (k, v) => {
        if (v == null) return '<span class="faint">null</span>';
        if (typeof v === 'number' && /(_ms|^ts)$/.test(k) && v > 1e12) return `<span data-tip="${v}">${SE.fmtTime(v, true)}</span>`;
        const s = String(v);
        return s.length > 80 ? `<span data-tip="${esc(s.slice(0, 600))}">${esc(s.slice(0, 80))}…</span>` : esc(s);
      };
      $('#dRaw').innerHTML = `<div class="scroll" style="max-height:520px"><table class="tbl compact raw"><thead><tr>${cols.map((c) => `<th>${esc(c)}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td class="mono">${cell(c, r[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    }
    async function loadFiles() {
      const r = await api('/data/scan');
      if (!$('#dFiles')) return;
      const folders = r.folders || [];
      $('#dFiles').innerHTML = `<div class="field" style="grid-template-columns:150px minmax(0,1fr)"><label data-tip="Besides the database folder and its import and export folders.">Also scan</label><div style="display:flex;gap:8px"><input class="input mono" id="dFold" placeholder="Folder path; several separated by ;" value="${esc(folders.join('; '))}"><button class="btn" id="dFoldS">${ic('save')}Save</button></div></div><div id="dFT" style="margin-top:12px"></div>`;
      $('#dFoldS').addEventListener('click', async () => {
        await api('/data/folders', { body: { folders: $('#dFold').value.split(';').map((x) => x.trim()).filter(Boolean) } });
        SE.toast('Saved'); loadFiles();
      });
      SE.table($('#dFT'), [
        { key: 'p', label: 'Dataset', cls: 'match', fmt: (f) => `<div class="mono" style="word-break:break-all">${esc(f.path.split(/[\\/]/).pop())}</div><div class="faint mono" style="word-break:break-all">${esc(f.folder)}</div>` },
        { key: 'b', label: 'Size', cls: 'r', w: 90, fmt: (f) => SE.fmtBytes(f.bytes), sort: (f) => f.bytes },
        { key: 's', label: 'Covers', w: 300, fmt: (f) => (f.swifteye ? span(f.info.from_ms, f.info.to_ms) + `<div class="faint">${SE.fmtInt(f.info.changes)} price changes · ${SE.fmtInt(f.info.pm_quotes + f.info.kalshi_quotes)} PM / Kalshi quotes</div>` : `<span class="faint">${esc(f.error || 'not a Swifteye database')}</span>`) },
        { key: 'm', label: 'Status', w: 150, fmt: (f) => (f.merged_ms ? `<span class="chip ok">Merged</span><div class="faint">${SE.fmtAgo(f.merged_ms)}${f.changed_since_merge ? ', file changed since' : ''}</div>` : f.swifteye ? '<span class="chip">Not merged</span>' : '') },
        { key: 'a', label: '', w: 110, fmt: (f) => (f.swifteye ? `<button class="btn ${f.merged_ms && !f.changed_since_merge ? '' : 'primary'}" data-merge="${esc(f.path)}">${ic('merge')}Merge</button>` : '') },
      ], { empty: 'No other datasets found. Put a snapshot from the other machine in the import folder next to this database, or add its folder above.', emptyIcon: 'merge' }).set(r.files);
      $$('[data-merge]').forEach((b) => b.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm(`Merge ${b.dataset.merge} into this database?\n\nRows are added; nothing here is deleted. Large files take several minutes; recording continues meanwhile.`)) return;
        try { await api('/data/merge', { body: { path: b.dataset.merge } }); pollJob(); } catch (err) { SE.toast(err.message); }
      }));
    }
    let polling = false, refreshedFor = 0;
    async function pollJob() {
      const j = await api('/data/job');
      const box = $('#dJob');
      if (!box) return;
      if (!j.kind) { box.innerHTML = ''; return; }
      const pct = j.total > 0 ? Math.min(100, (j.done / j.total) * 100) : null;
      const fmtV = (v) => (typeof v === 'object' && v ? Object.entries(v).map(([a, b]) => `${a.replace(/_/g, ' ')} ${typeof b === 'number' ? SE.fmtInt(b) : esc(b)}`).join(' · ') : SE.fmtInt(v) + ' rows');
      const res = j.result && typeof j.result === 'object' && Object.keys(j.result).length ? Object.entries(j.result).map(([k, v]) => `<div><b class="mono">${esc(k)}</b> <span class="muted">${fmtV(v)}</span></div>`).join('') : '';
      box.innerHTML = `<div class="tg-msg" style="margin-bottom:14px"><div><b>${j.kind === 'merge' ? 'Merge' : 'Export'}</b> <span class="mono faint">${esc(j.path)}</span></div>
        <div style="margin:6px 0">${j.running ? `${esc(j.stage)}${pct != null ? ` · ${pct.toFixed(0)}%` : ''}` : j.error ? `<span class="err">Failed: ${esc(j.error)}</span>` : `<span class="up">Done</span> ${SE.fmtAgo(j.finished_ms)}`}</div>
        ${pct != null && j.running ? `<div style="height:6px;background:var(--bg-3);border-radius:3px;overflow:hidden"><b style="display:block;height:100%;width:${pct}%;background:var(--accent)"></b></div>` : ''}${res}</div>`;
      if (j.running && !polling) { polling = true; setTimeout(() => { polling = false; pollJob(); }, 1500); }
      else if (!j.running && j.finished_ms && refreshedFor !== j.finished_ms) { refreshedFor = j.finished_ms; loadFiles(); loadStats(true); }
    }
    $('#dRe').addEventListener('click', () => loadStats(true));
    $('#dScan').addEventListener('click', loadFiles);
    $('#dExp').addEventListener('click', async () => {
      try { const r = await api('/data/export', { method: 'POST' }); SE.toast('Exporting to ' + r.path); pollJob(); } catch (e) { SE.toast(e.message); }
    });
    $('#dPrev').addEventListener('click', () => { page = Math.max(0, page - 1); loadRaw(); });
    $('#dNext').addEventListener('click', () => { page += 1; loadRaw(); });
    $('#dW').addEventListener('keydown', (e) => { if (e.key === 'Enter') { page = 0; loadRaw(); } });
    loadStats(false).then(loadRaw);
    loadFiles();
    pollJob();
  },
};

// ================================================================ site search
(function search() {
  const inp = () => $('#gSearch'), pop = () => $('#gPop');
  let items = [], sel = -1, seq = 0;
  const pages = () => $$('.nav a[data-route]').map((a) => ({ label: a.textContent.trim(), href: a.getAttribute('href') }));
  function render() {
    const p = pop();
    if (!items.length) { p.classList.remove('show'); return; }
    let group = '';
    p.innerHTML = items.map((it, i) => {
      const head = it.group !== group ? `<div class="sp-group">${(group = it.group)}</div>` : '';
      return `${head}<a href="${esc(it.href)}" class="sp-item ${i === sel ? 'on' : ''}" data-i="${i}">${it.html}</a>`;
    }).join('');
    p.classList.add('show');
    $$('.sp-item', p).forEach((a) => a.addEventListener('mousedown', (e) => { e.preventDefault(); open(items[+a.dataset.i]); }));
  }
  function open(it) {
    if (!it) return;
    if (it.query != null) SE.pendingQuery = it.query;
    pop().classList.remove('show');
    inp().blur();
    if (location.hash === it.href) window.dispatchEvent(new HashChangeEvent('hashchange')); else location.hash = it.href;
  }
  const run = SE.debounce(async () => {
    const q = inp().value.trim();
    const my = ++seq;
    if (!q) { items = []; render(); return; }
    const ql = q.toLowerCase();
    const pg = pages().filter((p) => p.label.toLowerCase().includes(ql)).slice(0, 5).map((p) => ({ group: 'Pages', href: p.href, html: `${ic('arrow_forward')}${esc(p.label)}` }));
    items = [...pg];
    sel = items.length ? 0 : -1;
    render();
    if (q.length < 2) return;
    let r;
    try { r = await api('/search?q=' + encodeURIComponent(q)); } catch { return; }
    if (my !== seq) return;
    const ms = r.matches.map((m) => ({
      group: 'Matches', href: '#/match/' + m.id,
      html: `${SE.sportIcon(m.sport)}<span class="sp-main">${SE.vs(m.home, m.away)}<span class="faint"> · ${esc(m.league || '')}</span></span><span class="sp-side">${m.live ? '<span class="chip ok">Live</span>' : m.start_ms ? `<span class="faint">${SE.fmtKick(m.start_ms)}</span>` : ''}</span>`,
    }));
    const ls = r.leagues.map((l) => ({ group: 'Leagues', href: '#/live', query: l.league, html: `${SE.sportIcon(l.sport)}<span class="sp-main">${esc(l.league)}</span><span class="sp-side faint">${l.matches} matches</span>` }));
    items = [...pg, ...ms, ...ls];
    sel = items.length ? Math.min(Math.max(sel, 0), items.length - 1) : -1;
    render();
  }, 150);
  window.addEventListener('DOMContentLoaded', () => {
    const i = inp();
    if (!i) return;
    i.addEventListener('input', run);
    i.addEventListener('focus', () => { if (i.value.trim()) run(); });
    i.addEventListener('blur', () => setTimeout(() => pop().classList.remove('show'), 120));
    i.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); if (items.length) { sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; render(); } }
      else if (e.key === 'Enter') { e.preventDefault(); open(items[sel] || items[0]); }
      else if (e.key === 'Escape') { i.value = ''; items = []; render(); i.blur(); }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && !(e.target instanceof Element && e.target.closest('input, textarea, select, [contenteditable]'))) { e.preventDefault(); i.focus(); i.select(); }
    });
  });
})();
})();
