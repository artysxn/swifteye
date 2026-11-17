// Swifteye pages, part 3: Movers, Edges, Query, API docs, Control panel, Polymarket, Statistics, Settings.
'use strict';
(() => {
const { $, $$, esc, ic, api, P } = SE;
const PM = P.PM, G = P.G;

// ================================================================ Movers
SE.pages.movers = {
  title: 'Movers',
  render(el) {
    el.innerHTML = P.head('Movers', '<div id="vG"></div><div id="vR"></div><div id="vM"></div><div id="vB"></div>') +
      `<div class="grid g-main">${P.sec('swap_vert', 'Biggest swings', '<div id="vT"></div>', '', 'Change in implied probability from the first to the last price in the window, per source and selection.')}${P.sec('local_fire_department', 'Busiest matches', '<div id="vA"></div>', '', 'Matches with the most price moves in the window.')}</div>`;
    let mins = 15, mk = '1x2', bk = '', game = '';
    P.gameSelect($('#vG'), (v) => { game = v; load(); }, '240px');
    SE.seg($('#vR'), [[5, '5 min'], [15, '15 min'], [60, '1h'], [360, '6h'], [1440, '24h']], mins, (v) => { mins = +v; load(); });
    SE.select($('#vM'), { options: [['1x2', '1X2'], ['total', 'Total goals'], ['handicap', 'Handicap'], ['btts', 'Both score']], value: mk, onChange: (v) => { mk = v; load(); } });
    SE.select($('#vB'), { options: [['', 'All sources'], ...Object.keys(SE.BOOK_NAMES).filter((b) => b !== 'kalshi').map((b) => [b, SE.bookName(b)])], value: '', onChange: (v) => { bk = v; load(); } });
    const t = SE.table($('#vT'), [
      { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league), sort: (r) => r.home },
      { key: 'b', label: 'Source', fmt: (r) => SE.book(r.book), sort: (r) => r.book },
      { key: 's', label: 'Selection', fmt: (r) => esc(SE.outcomeLong(r.outcome, r.line)) },
      { key: 'p', label: 'Price', cls: 'r', fmt: (r) => `<span ${SE.qattr({ b: r.book, h: r.home, a: r.away, m: mk, l: r.line, o: r.outcome, p: r.p1, pv: r.p0, u: r.t1 })}>${SE.move(r.p0, r.p1)}</span>` },
      { key: 'd', label: 'Swing', cls: 'r', tip: 'Implied probability change over the window, in points.', fmt: (r) => `<b class="${r.d > 0 ? 'up' : 'down'}">${SE.fmtPts(r.d)} pts</b>`, sort: (r) => Math.abs(r.d) },
      { key: 'n', label: 'Moves', cls: 'r', fmt: (r) => r.n, sort: (r) => r.n },
      { key: 't', label: 'Last', cls: 'r', fmt: (r) => `<span class="muted">${SE.fmtAgo(r.t1)}</span>`, sort: (r) => r.t1 },
    ], { sort: 'd', limit: 300, empty: 'No price moves in this window.', onClick: (r) => SE.go('#/match/' + r.match_id) });
    const a = SE.table($('#vA'), [
      { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league) },
      { key: 'n', label: 'Moves', cls: 'r', fmt: (r) => SE.fmtInt(r.n), sort: (r) => r.n },
      { key: 'b', label: 'Sources', cls: 'r', fmt: (r) => r.books, sort: (r) => r.books },
    ], { sort: 'n', limit: 30, empty: 'Quiet.', onClick: (r) => SE.go('#/match/' + r.match_id) });
    async function load() {
      const since = Date.now() - mins * 60000;
      const where = (p) => `${p}ts_ms >= ${since} AND ${p}market = '${mk}'${bk ? ` AND ${p}book = '${bk}'` : ''}${game ? ` AND ${p}match_id = ${+game}` : ''}`;
      const [rows, act] = await Promise.all([
        SE.sql(`WITH f AS (SELECT match_id, book, line, outcome, COUNT(*) n,
              MIN(CASE WHEN old_price IS NOT NULL THEN id END) fid, MAX(CASE WHEN new_price IS NOT NULL THEN id END) lid
              FROM changes WHERE ${where('')} AND kind IN ('move','resume','suspend') GROUP BY match_id, book, line, outcome)
             SELECT f.match_id, f.book, f.line, f.outcome, f.n, a.old_price p0, b.new_price p1, b.ts_ms t1, m.home, m.away, m.league
             FROM f JOIN changes a ON a.id = f.fid JOIN changes b ON b.id = f.lid LEFT JOIN matches m ON m.id = f.match_id`, 20000),
        SE.sql(`SELECT c.match_id, COUNT(*) n, COUNT(DISTINCT c.book) books, m.home, m.away, m.league FROM changes c LEFT JOIN matches m ON m.id = c.match_id WHERE ${where('c.')} AND c.kind = 'move' GROUP BY c.match_id ORDER BY n DESC LIMIT 30`),
      ]);
      t.set(rows.filter((r) => r.p0 && r.p1).map((r) => ({ ...r, d: 1 / r.p1 - 1 / r.p0 })).filter((r) => Math.abs(r.d) >= 0.001));
      a.set(act);
    }
    load();
    SE.every(10000, load);
  },
};

// ================================================================ Edges
SE.pages.edges = {
  title: 'Edges',
  render(el) {
    el.innerHTML = P.head('Edges', '<div id="eG"></div><div id="eMin"></div>') +
      P.stats(
        P.stat('eK1', 'balance', 'Matches compared', 'Live matches with Polymarket and at least one bookmaker pricing 1X2.'),
        P.stat('eK2', 'trending_down', 'Cheaper on Polymarket', 'Selections Polymarket prices at least 2 points below bookmaker consensus.', true),
        P.stat('eK3', 'trending_up', 'Dearer on Polymarket', 'Selections Polymarket prices at least 2 points above bookmaker consensus.'),
      ) +
      P.sec('balance', 'Polymarket against bookmaker consensus', '<div id="eT"></div>', '', G.consensus);
    let min = 0, game = '';
    P.gameSelect($('#eG'), (v) => { game = v; load(); }, '240px');
    SE.seg($('#eMin'), [[0, 'All'], [0.02, '2+ pts'], [0.04, '4+ pts'], [0.08, '8+ pts']], min, (v) => { min = +v; load(); });
    const t = SE.table($('#eT'), [
      { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league) },
      { key: 's', label: 'Score', fmt: (r) => `<b>${esc(r.score || '')}</b> <span class="faint">${esc(r.clock || '')}</span>` },
      { key: 'o', label: 'Selection', fmt: (r) => `<b>${SE.outcomeLong(r.o)}</b>` },
      { key: 'c', label: 'Consensus', cls: 'r', tip: G.consensus, fmt: (r) => SE.fmtProb(r.cons), sort: (r) => r.cons },
      { key: 'p', label: 'Polymarket', cls: 'r', tip: 'Polymarket probability from the mid price, normalised so 1, X and 2 add to 100%.', fmt: (r) => SE.fmtProb(r.pm), sort: (r) => r.pm },
      { key: 'g', label: 'Gap', cls: 'r', tip: G.gap, fmt: (r) => `<b class="${r.gap < 0 ? 'up' : 'down'}">${SE.fmtPts(r.gap)} pts</b>`, sort: (r) => Math.abs(r.gap) },
      { key: 'po', label: 'PM odds', cls: 'r', fmt: (r) => `<span ${SE.qattr({ b: PM, h: r.home, a: r.away, m: '1x2', o: r.o, p: r.pmOdds })}>${SE.fmtOdds(r.pmOdds)}</span>`, sort: (r) => r.pmOdds },
      { key: 'b', label: 'Best bookmaker', cls: 'r', fmt: (r) => P.bestCell(r.best, r, r.o), sort: (r) => r.best?.price },
      { key: 'n', label: 'Books', cls: 'r', fmt: (r) => r.n, sort: (r) => r.n },
    ], { sort: 'g', empty: 'Nothing to compare yet. This needs a live match on Polymarket and at least one bookmaker.', emptyIcon: 'balance', onClick: (r) => SE.go('#/match/' + r.id) });
    async function load() {
      const b = await api('/board?market=1x2&min_books=2');
      const rows = [];
      let matches = 0;
      for (const r of b.rows) {
        if (game && String(r.id) !== game) continue;
        const s = P.summarise(r);
        if (!s.pmFair || !s.cons) continue;
        matches++;
        for (const o of ['home', 'draw', 'away']) {
          const gap = s.pmFair[o] - s.cons[o];
          if (Math.abs(gap) >= min) rows.push({ ...r, o, cons: s.cons[o], pm: s.pmFair[o], gap, pmOdds: s.pm[o], best: s.best[o], n: s.nBooks });
        }
      }
      t.set(rows);
      P.set('eK1', matches);
      P.set('eK2', rows.filter((r) => r.gap <= -0.02).length);
      P.set('eK3', rows.filter((r) => r.gap >= 0.02).length);
    }
    load();
    SE.every(2000, load);
  },
};

// ================================================================ Query
const TEMPLATES = [
  ['Latest changes', "SELECT datetime(ts_ms/1000,'unixepoch') AS utc, book, match_id, market, line, outcome, kind, old_price, new_price\nFROM changes ORDER BY id DESC LIMIT 200"],
  ['Changes per source, last 24h', "SELECT book, COUNT(*) AS changes, SUM(kind = 'move') AS moves\nFROM changes WHERE ts_ms > (strftime('%s','now') - 86400) * 1000\nGROUP BY book ORDER BY changes DESC"],
  ['Feed delay per source', "SELECT book, COUNT(*) AS n, ROUND(AVG(ts_ms - book_ts_ms)) AS avg_ms, MIN(ts_ms - book_ts_ms) AS min_ms, MAX(ts_ms - book_ts_ms) AS max_ms\nFROM changes WHERE book_ts_ms IS NOT NULL AND kind = 'move'\nGROUP BY book ORDER BY avg_ms"],
  ['Most active matches', "SELECT m.id, m.home, m.away, m.league, COUNT(*) AS moves, COUNT(DISTINCT c.book) AS sources\nFROM changes c JOIN matches m ON m.id = c.match_id WHERE c.kind = 'move'\nGROUP BY m.id ORDER BY moves DESC LIMIT 50"],
  ['Polymarket top of book', "SELECT datetime(ts_ms/1000,'unixepoch') AS utc, match_id, outcome, bid, ask, ROUND(ask - bid, 3) AS spread, last\nFROM pm_quotes ORDER BY id DESC LIMIT 200"],
  ['Matches by day', "SELECT date(created_ms/1000,'unixepoch') AS day, COUNT(*) AS matches\nFROM matches GROUP BY day ORDER BY day DESC"],
];
SE.pages.query = {
  title: 'Query',
  render(el) {
    el.innerHTML = P.head('Query', '<div id="qTpl"></div>') +
      `<div class="grid g-main" style="grid-template-columns:minmax(0,1fr) 260px">
        <div><textarea class="editor" id="qSql" spellcheck="false"></textarea>
          <div style="display:flex;gap:8px;align-items:center;margin-top:12px"><button class="btn primary" id="qRun">${ic('play_arrow')}Run</button><div id="qLim"></div><span class="faint">Ctrl + Enter. Read only.</span><span style="margin-left:auto" class="muted" id="qInfo"></span><button class="btn" id="qCsv">${ic('download')}CSV</button></div>
          <div id="qOut" style="margin-top:22px"><div class="empty">${ic('database')}Run a query to see rows.</div></div></div>
        ${P.sec('schema', 'Tables', `<div class="note" style="font-family:var(--mono);font-size:12px;line-height:1.8">
          <b style="color:var(--text)">changes</b><br>id, ts_ms, book, book_event_id, match_id, market, line, outcome, kind, old_price, new_price, book_ts_ms<br><br>
          <b style="color:var(--text)">matches</b><br>id, sport, home, away, league, start_ms, sr_id, created_ms<br><br>
          <b style="color:var(--text)">book_events</b><br>book, book_event_id, match_id, home, away, league, start_ms, sr_id, first_seen_ms, last_seen_ms<br><br>
          <b style="color:var(--text)">pm_quotes</b><br>id, ts_ms, server_ts_ms, match_id, event_id, outcome, bid, ask, last</div>
          <div class="note" style="margin-top:16px">Times are milliseconds since 1970, UTC. kind is open, move, suspend, resume or close. Handicap lines are for the home side.</div>`)}
      </div>`;
    const ed = $('#qSql');
    ed.value = SE.pref('lastSql') || TEMPLATES[0][1];
    let last = { cols: [], rows: [] }, limit = 1000;
    SE.select($('#qTpl'), { options: TEMPLATES.map((t, i) => [String(i), t[0]]), placeholder: 'Templates', onChange: (v) => { ed.value = TEMPLATES[+v][1]; run(); } });
    SE.select($('#qLim'), { options: [['1000', '1,000 rows'], ['5000', '5,000 rows'], ['20000', '20,000 rows']], value: '1000', onChange: (v) => (limit = +v) });
    async function run() {
      const t0 = performance.now();
      SE.setPref('lastSql', ed.value);
      $('#qInfo').textContent = 'Running';
      try {
        const rows = await SE.sql(ed.value, limit);
        const cols = rows.length ? Object.keys(rows[0]) : [];
        last = { cols, rows };
        $('#qInfo').textContent = `${SE.fmtInt(rows.length)} rows in ${Math.round(performance.now() - t0)} ms`;
        SE.table($('#qOut'), cols.map((c) => ({ key: c, label: esc(c), cls: rows.some((r) => typeof r[c] === 'number') ? 'r' : '', fmt: (r) => (r[c] == null ? '<span class="faint">null</span>' : esc(r[c])), sort: (r) => r[c] })), { maxHeight: 'calc(100vh - 380px)', limit: 20000, empty: 'No rows.' }).set(rows);
      } catch (e) {
        $('#qInfo').textContent = '';
        $('#qOut').innerHTML = `<div class="empty err">${ic('error')}${esc(e.message)}</div>`;
      }
    }
    $('#qRun').addEventListener('click', run);
    ed.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); } });
    $('#qCsv').addEventListener('click', () => SE.csv('swifteye-query.csv', last.cols, last.rows.map((r) => last.cols.map((c) => r[c]))));
  },
};

// ================================================================ API docs
const EP = [
  ['Delay', [
    ['GET', '/rankings', 'Who moves first across every source, one move at a time, plus how often Polymarket could still be bought before it reacted.', [['since, until', 'Range in ms since 1970. Default: last 24 hours.'], ['min_jump', 'Smallest move counted, in probability (0.04 = 4 points).'], ['window_s', 'How far either side of a move to look for the others.'], ['clock', '"receive" (our clock) or "source" (each feed\'s own stamp).'], ['match', 'Only this match.'], ['limit', 'Most recent events returned (summary covers all).']], '/rankings?limit=3'],
    ['GET', '/lag', 'Polymarket against each bookmaker: jumps, how many the other side matched, median and middle half of the delay.', [['since, min_jump, window_s, settle_s, clock, match, book', 'As for /rankings.']], '/lag'],
    ['GET', '/lag/events', 'Every jump behind /lag with both sides\' before and after probabilities.', [['trigger', '"book" or "polymarket".'], ['limit', 'Most recent first.']], '/lag/events?limit=5'],
    ['GET', '/lag/books', 'Bookmaker against bookmaker: the matrix on the Latency page.', [], '/lag/books'],
    ['GET', '/lag/books/events', 'Every bookmaker pair behind /lag/books.', [], '/lag/books/events?limit=5'],
    ['GET', '/clock', 'The measured PC clock offset applied to every timestamp.', [], '/clock'],
  ]],
  ['Prices', [
    ['GET', '/quotes', 'Every current price as rows: match, source, market, line, selection, price, previous price, change time, source stamp.', [], '/quotes'],
    ['GET', '/board', 'One row per live match with each source\'s prices for one market.', [['market', '1x2, total, handicap or btts.'], ['min_books', 'Only matches with at least this many sources.']], '/board?market=1x2&min_books=2'],
    ['GET', '/matches', 'Live matches, the sources listing them, and the best 1X2 across bookmakers.', [['q', 'Text filter.'], ['book', 'Only matches this source lists.'], ['all', '1 to include matches no longer live.']], '/matches'],
    ['GET', '/matches/{id}', 'Every current price for one match, per source.', [], null],
    ['GET', '/matches/{id}/changes', 'The full price history of one match, oldest first.', [['market, book, outcome, line, since, limit', 'Filters.']], null],
    ['GET', '/changes', 'The change log across all matches.', [['since, until, book, match, market, outcome, kind, line', 'Filters.'], ['order', 'asc or desc.'], ['limit', 'Up to 100,000.']], '/changes?limit=5'],
    ['GET', '/stream', 'Server-sent events. One "change" event per price change the moment it lands.', [['match, book, market', 'Filters.']], null],
  ]],
  ['Data and system', [
    ['GET', '/query', 'Any read-only SQL over the database.', [['sql', 'The statement.'], ['limit', 'Most rows returned.']], '/query?sql=' + encodeURIComponent('SELECT book, COUNT(*) AS n FROM changes GROUP BY book')],
    ['GET', '/books', 'Every source: polling, errors, last error, request time, live events and prices.', [], '/books'],
    ['GET', '/stats', 'Row counts and changes per source in the last 5 minutes.', [], '/stats'],
    ['GET', '/info', 'Version, uptime, database path and size, clock.', [], '/info'],
    ['POST', '/control/books/{name}', 'Pause, resume or re-time a source. Body: {"paused": true} or {"poll_ms": 2000}.', [], null],
    ['POST', '/matches/{into}/merge/{from}', 'Join two matches the matcher kept apart.', [], null],
    ['POST', '/events/{book}/{event_id}/unlink', 'Split a source\'s event out of a match it was wrongly joined to.', [], null],
  ]],
  ['Polymarket account', [
    ['GET', '/pm/settings', 'Saved Polymarket details, secrets masked.', [], '/pm/settings'],
    ['POST', '/pm/settings', 'Save wallet, signer, API key, secret and passphrase. Masked values are kept as they are.', [], null],
    ['GET', '/pm/account', 'Positions, value and activity (public data API) plus open orders and trades (signed CLOB requests). Read only.', [], null],
  ]],
];
SE.pages.docs = {
  title: 'API',
  render(el) {
    const base = location.origin;
    el.innerHTML = P.head('API') + `<div class="grid" style="grid-template-columns:200px minmax(0,1fr);gap:48px">
      <nav class="toc">${EP.map(([g, list]) => `<span class="g">${g}</span>${list.map(([v, p]) => `<a href="javascript:void 0" data-to="${esc(v + p)}">${esc(p)}</a>`).join('')}`).join('')}</nav>
      <div>
        <p class="note" style="font-size:13px;max-width:760px;margin:0 0 6px">Everything this site shows comes from these endpoints on <span class="mono">${esc(base)}</span>. They return JSON, need no key, and only answer on this machine. Times are milliseconds since 1970, UTC, corrected against internet time.</p>
        ${EP.map(([g, list]) => `<h2 style="font:600 17px var(--font-display);margin:36px 0 0">${g}</h2>${list.map(([v, p, d, params, example]) => `<div class="ep" id="ep-${esc(v + p)}">
          <div class="ep-head"><span class="verb ${v === 'POST' ? 'post' : ''}">${v}</span><span class="ep-path">${esc(p)}</span>
            <span style="margin-left:auto;display:flex;gap:8px">${example ? `<button class="btn" data-try="${esc(example)}">${ic('play_arrow')}Try it</button>` : ''}<button class="btn ghost" data-copy="${esc(`curl ${v === 'POST' ? '-X POST ' : ''}"${base}${example || p}"`)}" data-tip="Copy as curl">${ic('content_copy')}</button></span></div>
          <p>${esc(d)}</p>
          ${params.length ? `<table class="params">${params.map(([k, x]) => `<tr><td>${esc(k)}</td><td>${esc(x)}</td></tr>`).join('')}</table>` : ''}
          <div class="out"></div></div>`).join('')}`).join('')}
      </div></div>`;
    $$('[data-to]', el).forEach((a) => a.addEventListener('click', () => document.getElementById('ep-' + a.dataset.to)?.scrollIntoView({ behavior: 'smooth', block: 'start' })));
    $$('[data-copy]', el).forEach((b) => b.addEventListener('click', () => navigator.clipboard.writeText(b.dataset.copy).then(() => SE.toast('Copied'))));
    $$('[data-try]', el).forEach((b) => b.addEventListener('click', async () => {
      const out = $('.out', b.closest('.ep'));
      const t0 = performance.now();
      out.innerHTML = '<p class="faint">Loading</p>';
      try {
        const res = await fetch(b.dataset.try);
        const txt = JSON.stringify(await res.json(), null, 2);
        out.innerHTML = `<p class="faint" style="margin:12px 0 6px">${res.status} in ${Math.round(performance.now() - t0)} ms${txt.length > 6000 ? ', first 6,000 characters' : ''}</p><pre class="code">${esc(txt.slice(0, 6000))}</pre>`;
      } catch (e) { out.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
    }));
  },
};

// ================================================================ Control panel
SE.pages.control = {
  title: 'Control panel',
  render(el) {
    el.innerHTML = P.head('Control panel', `<button class="btn" id="cPause">${ic('pause_circle')}Pause all</button><button class="btn" id="cResume">${ic('play_circle')}Resume all</button>`) +
      P.stats(
        P.stat('cK1', 'sensors', 'Sources running', 'Sources not paused.'),
        P.stat('cK2', 'error', 'Errors since start', 'Failed requests across all sources since the service started.'),
        P.stat('cK3', 'schedule', 'Uptime', 'Time since the service started.'),
        P.stat('cK4', 'storage', 'Database', 'Size of the database file including its write-ahead log.'),
      ) + P.sec('filter_alt', 'Scope', `<label class="toggle" style="padding:0"><span class="switch"><input type="checkbox" id="cScope"><span></span></span>Only matches on Polymarket or Kalshi plus at least one bookmaker</label><p class="note" id="cScopeN" style="margin-top:8px"></p>`) +
      P.sec('tune', 'Sources', '<div id="cT"></div>', '', 'Pause a source or change how often it is asked for prices. Changes apply immediately.');
    const INTERVALS = [['500', '0.5 s'], ['1000', '1 s'], ['2000', '2 s'], ['5000', '5 s'], ['10000', '10 s'], ['30000', '30 s']];
    async function setCtl(name, body) { try { await api('/control/books/' + name, { body }); load(); } catch (e) { SE.toast(e.message); } }
    let busy = false;
    async function load() {
      if (busy || document.querySelector('.sel.open')) return;
      const [b, info] = await Promise.all([api('/books'), api('/info')]);
      const now = b.now_ms;
      P.set('cK1', `${b.books.filter((s) => !s.paused).length}<small>of ${b.books.length}</small>`);
      P.set('cK2', SE.fmtInt(b.books.reduce((a, s) => a + s.errors, 0)));
      P.set('cK3', SE.fmtDur(now - b.started_ms));
      P.set('cK4', SE.fmtBytes(info.db_bytes));
      $('#cScope').checked = !!info.prediction_only;
      $('#cScopeN').textContent = `Every source is still watched so games can be linked; only qualifying matches are recorded and shown. Games are followed from ${info.prematch_hours} hours before kick-off.`;
      $('#cT').innerHTML = `<div class="scroll"><table class="tbl"><thead><tr><th>Source</th><th>State</th><th class="c">On</th><th data-tip="How often the source is asked for prices. Polymarket pushes, so it has no interval.">Poll every</th><th class="r" data-tip="Time for the last request, send to full response.">Request</th><th class="r">Events</th><th class="r">Prices</th><th class="r">Changes</th><th class="r">Errors</th><th>Last success</th><th>Last error</th></tr></thead><tbody>${b.books.map((s) => {
        const ok = s.last_ok_ms && now - s.last_ok_ms < Math.max(15000, s.poll_ms * 5);
        const state = s.paused ? '<span class="chip">Paused</span>' : ok ? '<span class="chip ok">Healthy</span>' : s.last_error ? '<span class="chip bad">Failing</span>' : '<span class="chip">Starting</span>';
        return `<tr><td>${SE.book(s.name)}<div class="match-sub" style="max-width:260px;white-space:normal;overflow-wrap:anywhere">${esc(s.source)}</div></td><td>${state}</td>
          <td class="c"><label class="switch"><input type="checkbox" data-run="${s.name}" ${s.paused ? '' : 'checked'}><span></span></label></td>
          <td>${s.poll_ms === 0 ? '<span class="muted">Live push</span>' : `<span data-int="${s.name}" data-v="${s.poll_ms}"></span>`}</td>
          <td class="r">${s.poll_ms === 0 ? '' : s.last_request_ms != null ? s.last_request_ms + ' ms' : ''}</td><td class="r">${s.live_events}</td><td class="r">${SE.fmtInt(s.live_quotes)}</td><td class="r">${SE.fmtInt(s.changes_since_start)}</td><td class="r ${s.errors ? 'down' : 'muted'}">${s.errors}</td>
          <td class="muted">${SE.fmtAgo(s.last_ok_ms)}</td><td class="muted" style="max-width:280px;white-space:normal" ${s.last_error ? `data-tip="${esc(s.last_error)}"` : ''}>${s.last_error ? esc(s.last_error.slice(0, 80)) + ' <span class="faint">' + SE.fmtAgo(s.last_error_ms) + '</span>' : ''}</td></tr>`;
      }).join('')}</tbody></table></div>`;
      $$('[data-run]').forEach((x) => x.addEventListener('change', () => setCtl(x.dataset.run, { paused: !x.checked })));
      $$('[data-int]').forEach((x) => {
        const v = x.dataset.v;
        const opts = INTERVALS.some((o) => o[0] === v) ? INTERVALS : [...INTERVALS, [v, `${+v / 1000} s`]];
        SE.select(x, { options: opts, value: v, width: '96px', onChange: (nv) => setCtl(x.dataset.int, { poll_ms: +nv }) });
      });
    }
    const all = async (paused) => { busy = true; const b = await api('/books'); await Promise.all(b.books.map((s) => api('/control/books/' + s.name, { body: { paused } }))); busy = false; load(); };
    $('#cScope').addEventListener('change', async (e) => { await api('/control/scope', { body: { prediction_only: e.target.checked } }); SE.toast(e.target.checked ? 'Only prediction-market matches' : 'All matches'); load(); });
    $('#cPause').addEventListener('click', () => all(true));
    $('#cResume').addEventListener('click', () => all(false));
    load();
    SE.every(3000, load);
  },
};

// ================================================================ Polymarket
SE.pages.polymarket = {
  title: 'Polymarket',
  render(el) {
    el.innerHTML = P.head('Polymarket', `<span class="muted" id="pSynced"></span><button class="btn" id="pSync">${ic('sync')}Sync now</button>`) + '<div id="pBody"></div>';
    const body = $('#pBody');
    const arr = (x) => (Array.isArray(x) ? x : Array.isArray(x?.data) ? x.data : []);
    const errOf = (x) => `<div class="empty err">${ic('error')}${esc(x.error)}</div>`;
    const pick = (o, ...k) => { for (const x of k) if (o?.[x] != null) return o[x]; return null; };
    async function load() {
      const [s, games] = await Promise.all([api('/pm/settings'), api('/quotes')]);
      const pmRows = games.rows.map((a) => Object.fromEntries(games.columns.map((c, i) => [c, a[i]]))).filter((r) => r.book === PM);
      const byMatch = new Map();
      for (const r of pmRows) { if (!byMatch.has(r.match_id)) byMatch.set(r.match_id, { ...r, p: {} }); byMatch.get(r.match_id).p[r.outcome] = r; }
      let acct = null;
      if (s.has_wallet || s.has_api) acct = await api('/pm/account').catch((e) => ({ error: e.message }));
      $('#pSynced').textContent = acct?.synced_ms ? 'Synced ' + SE.fmtTime(acct.synced_ms) : '';
      const positions = arr(acct?.positions).map((p) => ({
        title: pick(p, 'title', 'slug'), outcome: pick(p, 'outcome'), size: +pick(p, 'current_size', 'size'), avgPrice: +pick(p, 'avg_price', 'avgPrice'),
        curPrice: +pick(p, 'current_price', 'curPrice'), currentValue: +pick(p, 'current_value', 'currentValue'), cashPnl: +pick(p, 'unrealized_pnl', 'cashPnl'),
        percentPnl: +pick(p, 'percent_pnl', 'percentPnl'), endDate: pick(p, 'end_date', 'endDate') || '', status: pick(p, 'status'),
      }));
      const orders = arr(acct?.orders);
      const activity = arr(acct?.activity).map((a) => {
        const ts = +pick(a, 'timestamp', 'ts');
        return { timestamp: ts > 1e12 ? ts / 1000 : ts, type: pick(a, 'type', 'activity_type') || '', title: pick(a, 'title', 'slug') || '', side: pick(a, 'side') || '', outcome: pick(a, 'outcome') || '', usdcSize: pick(a, 'usdc_size', 'usdcSize', 'amount_usdc') };
      });
      const value = pick(acct?.value?.data, 'value') ?? acct?.value?.value;
      const pnl = positions.reduce((a, p) => a + (p.cashPnl || 0), 0);
      body.innerHTML = (!s.has_wallet && !s.has_api
        ? `<div class="empty">${ic('account_balance_wallet')}Add your Polymarket wallet address and API key to see positions, orders and activity here.<div style="margin-top:16px"><a class="btn primary" href="#/settings">${ic('key')}Open settings</a></div></div>`
        : P.stats(P.stat('pV', 'account_balance', 'Portfolio value', 'Current value of all positions, from Polymarket.', true), P.stat('pP', 'inventory_2', 'Open positions'), P.stat('pL', 'show_chart', 'Unrealised P&amp;L', 'Profit or loss on open positions at current prices.'), P.stat('pO', 'receipt_long', 'Open orders', 'Needs the API key.')) +
          P.sec('inventory_2', 'Positions', '<div id="pPos"></div>') +
          `<div class="grid g-2" style="margin-top:40px">${P.sec('receipt_long', 'Open orders', s.has_api ? '<div id="pOrd"></div>' : `<div class="empty">${ic('key')}Needs the API key, secret and passphrase.</div>`)}${P.sec('history', 'Recent activity', '<div id="pAct"></div>')}</div>`) +
        P.sec('sports_soccer', 'Games tracked live', '<div id="pGames"></div>', '', 'Polymarket football games Swifteye is streaming right now.');
      if (s.has_wallet || s.has_api) {
        P.set('pV', value != null ? '$' + (+value).toLocaleString('en-US', { maximumFractionDigits: 2 }) : '-');
        P.set('pP', positions.length);
        P.set('pL', `<span class="${pnl >= 0 ? 'up' : 'down'}">${pnl >= 0 ? '+' : '-'}$${Math.abs(pnl).toFixed(2)}</span>`);
        P.set('pO', s.has_api ? orders.length : '-');
        if (acct?.positions?.error) $('#pPos').innerHTML = errOf(acct.positions);
        else SE.table($('#pPos'), [
          { key: 't', label: 'Market', cls: 'match', fmt: (p) => `<div class="match-name">${esc(p.title)}</div><div class="match-sub">${esc(p.outcome || '')}</div>` },
          { key: 's', label: 'Shares', cls: 'r', fmt: (p) => p.size.toFixed(2), sort: (p) => p.size },
          { key: 'a', label: 'Avg price', cls: 'r', fmt: (p) => (p.avgPrice * 100).toFixed(1) + 'c', sort: (p) => p.avgPrice },
          { key: 'c', label: 'Now', cls: 'r', fmt: (p) => (p.curPrice * 100).toFixed(1) + 'c', sort: (p) => p.curPrice },
          { key: 'v', label: 'Value', cls: 'r', fmt: (p) => '$' + p.currentValue.toFixed(2), sort: (p) => p.currentValue },
          { key: 'l', label: 'P&amp;L', cls: 'r', fmt: (p) => `<span class="${p.cashPnl >= 0 ? 'up' : 'down'}">${p.cashPnl >= 0 ? '+' : '-'}$${Math.abs(p.cashPnl).toFixed(2)}</span> <span class="faint">${p.percentPnl.toFixed(1)}%</span>`, sort: (p) => p.cashPnl },
          { key: 'e', label: 'Ends', fmt: (p) => `<span class="muted">${esc(String(p.endDate).slice(0, 10))}</span>${p.status === 'REDEEMABLE' ? ' <span class="chip ok">Redeemable</span>' : ''}` },
        ], { sort: 'v', empty: 'No open positions.' }).set(positions);
        if (s.has_api) {
          if (acct?.orders?.error) $('#pOrd').innerHTML = errOf(acct.orders);
          else SE.table($('#pOrd'), [
            { key: 'sd', label: 'Side', fmt: (o) => `<span class="chip ${o.side === 'BUY' ? 'ok' : 'bad'}">${esc(o.side)}</span>` },
            { key: 'o', label: 'Outcome', fmt: (o) => esc(o.outcome || String(o.asset_id || '').slice(0, 10)) },
            { key: 'p', label: 'Price', cls: 'r', fmt: (o) => (+o.price * 100).toFixed(1) + 'c' },
            { key: 's', label: 'Filled', cls: 'r', fmt: (o) => `${(+o.size_matched || 0).toFixed(0)} <span class="faint">of ${(+o.original_size).toFixed(0)}</span>` },
            { key: 't', label: 'Placed', fmt: (o) => `<span class="muted">${SE.fmtAgo((+o.created_at || 0) * 1000)}</span>` },
          ], { empty: 'No open orders.' }).set(orders);
        }
        if (acct?.activity?.error) $('#pAct').innerHTML = errOf(acct.activity);
        else SE.table($('#pAct'), [
          { key: 't', label: 'When', fmt: (a) => `<span class="muted">${SE.fmtAgo(a.timestamp * 1000)}</span>` },
          { key: 'y', label: 'Type', fmt: (a) => `<span class="chip">${esc(a.type.toLowerCase())}</span>` },
          { key: 'm', label: 'Market', cls: 'match', fmt: (a) => `<div class="match-name">${esc(a.title)}</div><div class="match-sub">${esc([a.side, a.outcome].filter(Boolean).join(' '))}</div>` },
          { key: 'u', label: 'USDC', cls: 'r', fmt: (a) => (a.usdcSize != null ? '$' + (+a.usdcSize).toFixed(2) : '') },
        ], { empty: 'No recent activity.', maxHeight: '420px' }).set(activity);
      }
      SE.table($('#pGames'), [
        { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league), sort: (r) => r.home },
        ...['home', 'draw', 'away'].map((o) => ({ key: o, label: SE.outcomeLabel(o), cls: 'r', fmt: (r) => (r.p[o]?.price ? `<span ${SE.qattr({ b: PM, h: r.home, a: r.away, m: '1x2', o, p: r.p[o].price, pv: r.p[o].prev, u: r.p[o].updated_ms, bt: r.p[o].book_ts_ms })}>${SE.fmtOdds(r.p[o].price)} <span class="faint">${SE.fmtProb(1 / r.p[o].price, 0)}</span></span>` : '<span class="faint">off</span>') })),
        { key: 'u', label: 'Updated', cls: 'r', fmt: (r) => `<span class="muted">${SE.fmtAgo(Math.max(...Object.values(r.p).map((q) => q.updated_ms)))}</span>` },
      ], { empty: 'No Polymarket football game is live or about to start.', emptyIcon: 'sports_soccer', onClick: (r) => SE.go('#/match/' + r.match_id) }).set([...byMatch.values()]);
    }
    $('#pSync').addEventListener('click', () => load().then(() => SE.toast('Polymarket synced')).catch((e) => SE.toast(e.message)));
    load().catch((e) => (body.innerHTML = `<div class="empty err">${ic('error')}${esc(e.message)}</div>`));
    SE.every(30000, () => load().catch(() => {}));
  },
};

// ================================================================ Statistics
SE.pages.stats = {
  title: 'Statistics',
  render(el) {
    el.innerHTML = P.head('Statistics') +
      P.stats(P.stat('tK1', 'swap_vert', 'Price changes stored'), P.stat('tK2', 'sports_soccer', 'Matches seen'), P.stat('tK3', 'storage', 'Database size'), P.stat('tK4', 'schedule', 'Uptime')) +
      P.sec('bar_chart', 'Changes per hour', '<div class="chart" id="tChart"></div><div class="legend" id="tLegend"></div>', '<span class="faint">last 48 hours</span>') +
      `<div class="grid g-2" style="margin-top:40px">${P.sec('sensors', 'By source, last 24 hours', '<div id="tSrc"></div>')}${P.sec('calendar_month', 'By day', '<div id="tDay"></div>')}</div>`;
    let chart = null;
    (async () => {
      const day = Date.now() - 86400000;
      const [info, st, src, days, hours] = await Promise.all([
        api('/info'), api('/stats'),
        SE.sql(`SELECT book, COUNT(*) n, SUM(kind='move') moves, COUNT(DISTINCT match_id) matches, MAX(ts_ms) last FROM changes WHERE ts_ms >= ${day} GROUP BY book ORDER BY n DESC`),
        SE.sql(`SELECT date(ts_ms/1000,'unixepoch') d, COUNT(*) n, COUNT(DISTINCT match_id) matches FROM changes GROUP BY d ORDER BY d DESC LIMIT 30`),
        SE.sql(`SELECT (ts_ms/3600000)*3600000 t, book, COUNT(*) n FROM changes WHERE ts_ms >= ${Date.now() - 48 * 3600000} GROUP BY t, book ORDER BY t`, 20000),
      ]);
      P.set('tK1', SE.fmtInt(st.totals?.changes));
      P.set('tK2', SE.fmtInt(st.totals?.matches));
      P.set('tK3', SE.fmtBytes(info.db_bytes));
      P.set('tK4', SE.fmtDur(info.now_ms - info.started_ms));
      SE.table($('#tSrc'), [
        { key: 'b', label: 'Source', fmt: (r) => SE.book(r.book) },
        { key: 'n', label: 'Changes', cls: 'r', fmt: (r) => SE.fmtInt(r.n), sort: (r) => r.n },
        { key: 'm', label: 'Moves', cls: 'r', fmt: (r) => SE.fmtInt(r.moves), sort: (r) => r.moves },
        { key: 'x', label: 'Matches', cls: 'r', fmt: (r) => r.matches, sort: (r) => r.matches },
        { key: 'l', label: 'Last', cls: 'r', fmt: (r) => `<span class="muted">${SE.fmtAgo(r.last)}</span>` },
      ], { sort: 'n', empty: 'Nothing recorded in the last 24 hours.' }).set(src);
      SE.table($('#tDay'), [
        { key: 'd', label: 'Day (UTC)', fmt: (r) => esc(r.d) },
        { key: 'n', label: 'Changes', cls: 'r', fmt: (r) => SE.fmtInt(r.n) },
        { key: 'm', label: 'Matches', cls: 'r', fmt: (r) => SE.fmtInt(r.matches) },
      ], { empty: 'No data yet.' }).set(days);
      const books = [...new Set(hours.map((r) => r.book))].sort();
      const xs = [];
      for (let t = Math.floor((Date.now() - 48 * 3600000) / 3600000) * 3600000; t <= Date.now(); t += 3600000) xs.push(t);
      const idx = new Map(xs.map((t, i) => [t, i]));
      const series = books.map((b) => ({ label: SE.bookName(b), book: b, color: SE.bookColor(b), values: new Array(xs.length).fill(0) }));
      for (const r of hours) { const i = idx.get(r.t); if (i != null) series[books.indexOf(r.book)].values[i] = r.n; }
      chart = SE.chart($('#tChart'), xs, series, { step: false, yFmt: (v) => (v == null ? '' : SE.fmtInt(Math.round(v))) });
      SE.legend($('#tLegend'), series, chart);
    })().catch((e) => SE.toast(e.message));
    return () => SE.destroyChart(chart);
  },
};

// ================================================================ Settings
SE.pages.settings = {
  title: 'Settings',
  render(el) {
    el.innerHTML = P.head('Settings') + `<div class="grid g-main">
      <div>
        ${P.sec('account_balance_wallet', 'Polymarket', `
          <div class="field"><label for="fW" data-tip="The address shown on your Polymarket profile. Positions are looked up by it.">Wallet address</label><input class="input mono" id="fW" placeholder="0x on your Polymarket profile"></div>
          <div class="field"><label for="fS" data-tip="Only needed if your API key was created by a different address than the wallet.">Signer address</label><input class="input mono" id="fS" placeholder="Leave empty if the same"></div>
          <div class="field"><label for="fK">API key</label><input class="input mono" id="fK" autocomplete="off"></div>
          <div class="field"><label for="fX">API secret</label><input class="input mono" id="fX" type="password" autocomplete="new-password"></div>
          <div class="field"><label for="fP">Passphrase</label><input class="input mono" id="fP" type="password" autocomplete="new-password"></div>
          <div style="display:flex;gap:8px;margin-top:16px;align-items:center"><button class="btn primary" id="fSave">${ic('save')}Save</button><button class="btn" id="fTest">${ic('sync')}Test connection</button><span class="muted" id="fMsg"></span></div>
          <p class="note" style="margin-top:16px">Stored on this machine in polymarket.json next to the database. Saved secrets show masked; leave a masked field as it is to keep it. Swifteye only reads your account and never places or cancels orders.</p>`)}
        ${P.sec('account_balance', 'Kalshi', `
          <div class="field"><label for="kK" data-tip="From Kalshi: Account, API keys. Read-only access is enough.">API key ID</label><input class="input mono" id="kK" autocomplete="off"></div>
          <div class="field" style="align-items:start"><label for="kP" data-tip="The private key file Kalshi gave you when creating the key (PEM). Paste its whole text.">Private key</label><textarea class="input mono" id="kP" style="height:90px;padding-top:8px;resize:vertical" placeholder="-----BEGIN RSA PRIVATE KEY-----"></textarea></div>
          <div style="display:flex;gap:8px;margin-top:16px;align-items:center"><button class="btn primary" id="kSave">${ic('save')}Save</button><button class="btn" id="kTest">${ic('sync')}Test connection</button><span class="muted" id="kMsg"></span></div>
          <p class="note" style="margin-top:16px">Stored on this machine in kalshi.json next to the database. The private key is never shown again; leave the masked text to keep it. Swifteye only reads your Kalshi account.</p>`)}
        ${P.sec('palette', 'Display', `
          <div class="field"><label>Theme</label><div id="sTheme"></div></div>
          <div class="field"><label>Odds</label><div id="sOdds"></div></div>
          <div class="field"><label>Times</label><div id="sTz"></div></div>`)}
        ${P.sec('timer', 'Delay analysis', `
          <div class="field"><label data-tip="${esc(G.jump)}">Smallest jump</label><div id="sJump"></div></div>
          <div class="field"><label data-tip="How far either side of a move to look for other sources making it.">Search window</label><div id="sWin"></div></div>`)}
      </div>
      <div>${P.sec('dns', 'Service', '<div id="sInfo"></div>')}</div></div>`;
    SE.seg($('#sTheme'), [['dark', 'Dark'], ['light', 'Light']], SE.pref('theme'), (v) => SE.setPref('theme', v));
    SE.seg($('#sOdds'), [['decimal', 'Decimal'], ['prob', 'Probability'], ['american', 'American']], SE.pref('odds'), (v) => SE.setPref('odds', v));
    SE.seg($('#sTz'), [['local', 'Local'], ['utc', 'UTC']], SE.pref('tz'), (v) => SE.setPref('tz', v));
    SE.seg($('#sJump'), [[0.02, '2 pts'], [0.04, '4 pts'], [0.06, '6 pts'], [0.1, '10 pts']], SE.pref('minJump'), (v) => { SE.setPref('minJump', +v); SE.loadCtx(); });
    SE.seg($('#sWin'), [[30, '30 s'], [60, '60 s'], [90, '90 s'], [180, '3 min']], SE.pref('windowS'), (v) => { SE.setPref('windowS', +v); SE.loadCtx(); });
    const fields = { wallet: '#fW', signer: '#fS', api_key: '#fK', api_secret: '#fX', passphrase: '#fP' };
    const fill = (s) => { if (!$('#fW')) return; for (const [k, sel] of Object.entries(fields)) $(sel).value = s[k] || ''; };
    api('/pm/settings').then(fill);
    $('#fSave').addEventListener('click', async () => {
      try { fill(await api('/pm/settings', { body: Object.fromEntries(Object.entries(fields).map(([k, sel]) => [k, $(sel).value])) })); $('#fMsg').textContent = 'Saved'; SE.refreshMe?.(); }
      catch (e) { $('#fMsg').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
    });
    $('#fTest').addEventListener('click', async () => {
      $('#fMsg').textContent = 'Checking';
      try {
        const a = await api('/pm/account');
        const n = (x) => (Array.isArray(x) ? x : x?.data || []).length;
        const parts = [];
        if (a.positions) parts.push(a.positions.error ? 'positions failed' : `${n(a.positions)} positions`);
        if (a.orders) parts.push(a.orders.error ? `orders failed: ${a.orders.error}` : `${n(a.orders)} open orders`);
        $('#fMsg').textContent = parts.length ? parts.join(', ') : 'Nothing configured yet';
      } catch (e) { $('#fMsg').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
    });
    const kfill = (s) => { if (!$('#kK')) return; $('#kK').value = s.key_id || ''; $('#kP').value = s.private_key || ''; };
    api('/kalshi/settings').then(kfill);
    $('#kSave').addEventListener('click', async () => {
      try { const s = await api('/kalshi/settings', { body: { key_id: $('#kK').value, private_key: $('#kP').value } }); kfill(s); $('#kMsg').textContent = s.private_key && !s.key_valid ? 'Saved, but the key is not a valid RSA PEM' : 'Saved'; }
      catch (e) { $('#kMsg').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
    });
    $('#kTest').addEventListener('click', async () => {
      $('#kMsg').textContent = 'Checking';
      try { const a = await api('/kalshi/account'); $('#kMsg').textContent = !a.has_key ? 'Nothing saved yet' : a.balance?.error ? 'Failed: ' + a.balance.error : `Connected. Balance $${((a.balance?.balance ?? 0) / 100).toFixed(2)}`; }
      catch (e) { $('#kMsg').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
    });
    api('/info').then((i) => {
      const row = (k, v) => `<div class="field" style="grid-template-columns:130px minmax(0,1fr)"><label>${k}</label><span class="muted" style="word-break:break-all">${v}</span></div>`;
      if (!$('#sInfo')) return;
      $('#sInfo').innerHTML = row('Version', esc(i.version)) + row('Listening on', `<span class="mono">${esc(i.bind)}</span>`) + row('Database', `<span class="mono">${esc(i.db_path)}</span>`) + row('Size', SE.fmtBytes(i.db_bytes)) +
        row('Clock offset', `<span data-tip="This PC's clock compared with internet time. Every timestamp Swifteye records is corrected by this amount.">${SE.fmtDelay(i.clock.offset_ms)}</span>`) + row('Simulated football', i.include_esoccer ? 'Included' : 'Filtered out') + row('Running since', SE.fmtDateTime(i.started_ms));
    });
  },
};

// ================================================================ Kalshi
SE.pages.kalshi = {
  title: 'Kalshi',
  render(el) {
    el.innerHTML = P.head('Kalshi', `<span class="muted" id="qSynced"></span><button class="btn" id="qSync">${ic('sync')}Sync now</button>`) + '<div id="qBody"></div>';
    const body = $('#qBody');
    const usd = (c) => (c == null ? '-' : '$' + (+c / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
    async function load() {
      const [s, games] = await Promise.all([api('/kalshi/settings'), api('/quotes')]);
      const rows = games.rows.map((a) => Object.fromEntries(games.columns.map((c, i) => [c, a[i]]))).filter((r) => r.book === 'kalshi');
      const byMatch = new Map();
      for (const r of rows) { if (!byMatch.has(r.match_id)) byMatch.set(r.match_id, { ...r, p: {} }); byMatch.get(r.match_id).p[r.outcome] = r; }
      let a = null;
      if (s.has_key) a = await api('/kalshi/account').catch((e) => ({ error: e.message }));
      $('#qSynced').textContent = a?.synced_ms ? 'Synced ' + SE.fmtTime(a.synced_ms) : '';
      const pos = a?.positions?.market_positions || [], ord = a?.orders?.orders || [], fills = a?.fills?.fills || [];
      body.innerHTML = (!s.has_key
        ? `<div class="empty">${ic('account_balance')}Add your Kalshi API key ID and private key in Settings to see balance, positions, orders and fills here.<div style="margin-top:16px"><a class="btn primary" href="#/settings">${ic('key')}Open settings</a></div></div>`
        : P.stats(P.stat('qBal', 'account_balance', 'Cash balance', 'Available balance on Kalshi.', true), P.stat('qPos', 'inventory_2', 'Open positions'), P.stat('qOrd', 'receipt_long', 'Resting orders'), P.stat('qFil', 'history', 'Recent fills')) +
          (a?.balance?.error ? `<div class="empty err">${ic('error')}${esc(a.balance.error)}</div>` : '') +
          `<div class="grid g-2" style="margin-top:40px">${P.sec('inventory_2', 'Positions', '<div id="qP"></div>')}${P.sec('receipt_long', 'Resting orders', '<div id="qO"></div>')}</div>` +
          P.sec('history', 'Recent fills', '<div id="qF"></div>')) +
        P.sec('sports_soccer', 'Games tracked', '<div id="qG"></div>', '', 'Kalshi football games Swifteye is pricing every second, live and up to 8 hours before kick-off.');
      if (s.has_key) {
        P.set('qBal', usd(a?.balance?.balance));
        P.set('qPos', pos.length);
        P.set('qOrd', ord.length);
        P.set('qFil', fills.length);
        SE.table($('#qP'), [
          { key: 't', label: 'Market', fmt: (p) => `<span class="mono">${esc(p.ticker)}</span>` },
          { key: 'p', label: 'Contracts', cls: 'r', fmt: (p) => p.position ?? p.position_fp ?? '', sort: (p) => +(p.position ?? 0) },
          { key: 'e', label: 'Exposure', cls: 'r', fmt: (p) => usd(p.market_exposure), sort: (p) => +(p.market_exposure ?? 0) },
          { key: 'r', label: 'Realised', cls: 'r', fmt: (p) => usd(p.realized_pnl) },
        ], { empty: 'No open positions.' }).set(pos);
        SE.table($('#qO'), [
          { key: 't', label: 'Market', fmt: (o) => `<span class="mono">${esc(o.ticker)}</span>` },
          { key: 's', label: 'Side', fmt: (o) => `<span class="chip ${o.action === 'buy' ? 'ok' : 'bad'}">${esc(o.action || '')} ${esc(o.side || '')}</span>` },
          { key: 'p', label: 'Price', cls: 'r', fmt: (o) => (o.yes_price != null ? o.yes_price + 'c' : esc(o.yes_price_dollars || '')) },
          { key: 'n', label: 'Left', cls: 'r', fmt: (o) => o.remaining_count ?? '' },
        ], { empty: 'No resting orders.' }).set(ord);
        SE.table($('#qF'), [
          { key: 'w', label: 'When', fmt: (f) => `<span class="muted">${SE.fmtAgo(Date.parse(f.created_time))}</span>` },
          { key: 't', label: 'Market', fmt: (f) => `<span class="mono">${esc(f.ticker)}</span>` },
          { key: 's', label: 'Side', fmt: (f) => `${esc(f.action || '')} ${esc(f.side || '')}` },
          { key: 'c', label: 'Contracts', cls: 'r', fmt: (f) => f.count ?? '' },
          { key: 'p', label: 'Price', cls: 'r', fmt: (f) => (f.yes_price != null ? f.yes_price + 'c' : '') },
        ], { empty: 'No recent fills.' }).set(fills);
      }
      SE.table($('#qG'), [
        { key: 'm', label: 'Match', cls: 'match', fmt: (r) => P.matchCell(r.home, r.away, r.league), sort: (r) => r.home },
        { key: 's', label: 'Status', fmt: (r) => SE.status(r), sort: (r) => r.start_ms },
        ...['home', 'draw', 'away'].map((o) => ({ key: o, label: SE.outcomeLabel(o), cls: 'r', fmt: (r) => (r.p[o]?.price ? `<span ${SE.qattr({ b: 'kalshi', h: r.home, a: r.away, m: '1x2', o, p: r.p[o].price, pv: r.p[o].prev, u: r.p[o].updated_ms })}>${SE.fmtOdds(r.p[o].price)} <span class="faint">${SE.fmtProb(1 / r.p[o].price, 0)}</span></span>` : '<span class="faint">off</span>') })),
      ], { empty: 'No Kalshi football game in scope right now. Kalshi mostly lists the big leagues; games appear here once a bookmaker lists the same match.', emptyIcon: 'sports_soccer', onClick: (r) => SE.go('#/match/' + r.match_id) }).set([...byMatch.values()]);
    }
    $('#qSync').addEventListener('click', () => load().then(() => SE.toast('Kalshi synced')).catch((e) => SE.toast(e.message)));
    load().catch((e) => (body.innerHTML = `<div class="empty err">${ic('error')}${esc(e.message)}</div>`));
    SE.every(30000, () => load().catch(() => {}));
  },
};
})();
