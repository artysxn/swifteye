// Swifteye core: API, live stream, router, formatting, tooltips, selects, charts, tables.
'use strict';

const SE = (window.SE = {});
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
SE.$ = $;
SE.$$ = $$;

// ---------------------------------------------------------------- preferences
const PREF_DEFAULTS = { theme: 'dark', odds: 'decimal', tz: 'local', minJump: 0.04, windowS: 90, baseline: 'polymarket' };
SE.pref = (k) => {
  try {
    const v = localStorage.getItem('se.' + k);
    return v === null ? PREF_DEFAULTS[k] : JSON.parse(v);
  } catch {
    return PREF_DEFAULTS[k];
  }
};
SE.setPref = (k, v) => {
  try { localStorage.setItem('se.' + k, JSON.stringify(v)); } catch {}
  if (k === 'theme') document.documentElement.dataset.theme = v;
};
document.documentElement.dataset.theme = SE.pref('theme');

// ---------------------------------------------------------------- helpers
SE.esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
SE.attr = (o) => SE.esc(JSON.stringify(o));
SE.ic = (name, cls = '') => `<span class="ic ${cls}">${name}</span>`;
SE.api = async (path, opts = {}) => {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? 'POST' : 'GET'),
    headers: opts.body ? { 'Content-Type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
};
SE.sql = async (sql, limit = 5000) => (await SE.api('/query?limit=' + limit + '&sql=' + encodeURIComponent(sql))).rows;
SE.toast = (msg, ms = 3000) => {
  const t = $('#toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(SE._toast);
  SE._toast = setTimeout(() => t.classList.remove('show'), ms);
};
SE.debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
SE.throttle = (fn, ms) => {
  let last = 0, t = null;
  return (...a) => {
    const now = Date.now();
    if (now - last >= ms) { last = now; fn(...a); }
    else if (!t) t = setTimeout(() => { t = null; last = Date.now(); fn(...a); }, ms - (now - last));
  };
};

// ---------------------------------------------------------------- formatting
SE.fmtOdds = (p) => {
  if (p == null || !isFinite(p)) return '';
  const f = SE.pref('odds');
  if (f === 'prob') return (100 / p).toFixed(1) + '%';
  if (f === 'american') return p >= 2 ? '+' + Math.round((p - 1) * 100) : String(Math.round(-100 / (p - 1)));
  return p >= 100 ? p.toFixed(0) : p >= 10 ? p.toFixed(1) : p.toFixed(2);
};
SE.fmtProb = (x, d = 1) => (x == null || !isFinite(x) ? '' : (x * 100).toFixed(d) + '%');
SE.fmtPts = (x) => (x == null || !isFinite(x) ? '' : (x > 0 ? '+' : '') + (x * 100).toFixed(1));
SE.fmtInt = (n) => (n == null ? '' : Number(n).toLocaleString('en-US'));
SE.fmtPct = (x, d = 0) => (x == null || !isFinite(x) ? '' : (x * 100).toFixed(d) + '%');
SE.fmtBytes = (b) => {
  if (b == null) return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (b >= 1024 && i < u.length - 1) { b /= 1024; i++; }
  return b.toFixed(i ? 1 : 0) + ' ' + u[i];
};
SE.fmtDelay = (ms) => {
  if (ms == null || !isFinite(ms)) return '';
  const a = Math.abs(ms);
  const s = a < 1000 ? a.toFixed(0) + ' ms' : a < 60000 ? (a / 1000).toFixed(1) + ' s' : (a / 60000).toFixed(1) + ' min';
  return (ms < 0 ? '-' : ms > 0 ? '+' : '') + s;
};
SE.fmtSpan = (ms) => (ms == null || !isFinite(ms) ? '' : SE.fmtDelay(Math.abs(ms)).replace('+', ''));
/** "31.7 s later" / "4.6 s earlier" / "same time" */
SE.fmtRel = (ms) => {
  if (ms == null || !isFinite(ms)) return '';
  if (Math.abs(ms) < 50) return 'same time';
  return SE.fmtSpan(ms) + (ms > 0 ? ' later' : ' earlier');
};
SE.fmtDur = (ms) => {
  if (ms == null) return '';
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : m ? `${m}m ${s % 60}s` : `${s}s`;
};
SE.fmtAgo = (ms) => {
  if (!ms) return '';
  const d = Date.now() - ms;
  if (d < 1500) return 'now';
  if (d < 60000) return Math.round(d / 1000) + 's ago';
  if (d < 3600000) return Math.round(d / 60000) + 'm ago';
  if (d < 86400000) return Math.round(d / 3600000) + 'h ago';
  return Math.round(d / 86400000) + 'd ago';
};
const pad = (n, w = 2) => String(n).padStart(w, '0');
SE.fmtTime = (ms, withMs = false) => {
  if (!ms) return '';
  const d = new Date(ms);
  const utc = SE.pref('tz') === 'utc';
  const h = utc ? d.getUTCHours() : d.getHours(), m = utc ? d.getUTCMinutes() : d.getMinutes(), s = utc ? d.getUTCSeconds() : d.getSeconds();
  return `${pad(h)}:${pad(m)}:${pad(s)}${withMs ? '.' + pad(d.getMilliseconds(), 3) : ''}`;
};
SE.fmtDateTime = (ms) => {
  if (!ms) return '';
  const d = new Date(ms);
  const utc = SE.pref('tz') === 'utc';
  const mo = utc ? d.getUTCMonth() : d.getMonth(), day = utc ? d.getUTCDate() : d.getDate();
  return `${day} ${'Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(' ')[mo]} ${SE.fmtTime(ms)}`;
};
/** Kick-off: "14:30" today, "Wed 14:30" otherwise. */
SE.fmtKick = (ms) => {
  if (!ms) return '';
  const d = new Date(ms), n = new Date();
  const same = d.toDateString() === n.toDateString();
  return (same ? '' : 'Sun Mon Tue Wed Thu Fri Sat'.split(' ')[SE.pref('tz') === 'utc' ? d.getUTCDay() : d.getDay()] + ' ') + SE.fmtTime(ms).slice(0, 5);
};
/** "Starts in 2h 10m" style countdown. */
SE.fmtIn = (ms) => {
  if (!ms) return '';
  const d = ms - Date.now();
  if (d <= 0) return 'kicking off';
  const m = Math.round(d / 60000);
  return m < 60 ? `in ${m}m` : `in ${Math.floor(m / 60)}h ${m % 60}m`;
};
/** Status cell: score and clock when live, kick-off when not. */
SE.status = (r) => (r.live
  ? `<b>${SE.esc(r.score || '')}</b> <span class="faint">${SE.esc(r.clock || '')}</span>`
  : `<span class="chip" data-tip="Not started. Odds before kick-off are tracked up to 8 hours ahead.">Pre-match</span> <span class="muted">${SE.fmtKick(r.start_ms)}</span>`);
SE.PRED = ['polymarket', 'kalshi'];
SE.vs = (home, away) => `${SE.esc(home)} <span class="faint">v</span> ${SE.esc(away)}`;

const OUTCOME = { home: '1', draw: 'X', away: '2', over: 'Over', under: 'Under', yes: 'Yes', no: 'No' };
const OUTCOME_LONG = { home: 'Home win', draw: 'Draw', away: 'Away win', over: 'Over', under: 'Under', yes: 'Both score', no: 'Not both score' };
const fmtLine = (l) => (l > 0 ? '+' + l : String(l));
// Handicap lines carry a sign (and flip for the away side); goal lines do not.
const lineFor = (o, line) => (line == null || line === '' ? '' : ' ' + (o === 'home' ? fmtLine(line) : o === 'away' ? fmtLine(-line) : String(line)));
SE.outcomeLabel = (o, line) => (OUTCOME[o] || o) + lineFor(o, line);
SE.outcomeLong = (o, line) => (OUTCOME_LONG[o] || o) + lineFor(o, line);
SE.marketLabel = (m) => ({ '1x2': '1X2', total: 'Total goals', handicap: 'Handicap', btts: 'Both score' }[m] || m);

// ---------------------------------------------------------------- sources
SE.BOOK_COLORS = {
  polymarket: '#50e4b2', sportybet: '#ff6b5b', betika: '#c6e83a', betpawa: '#9aa5a1',
  linebet: '#2f9e5b', fonbet: '#ff3b4f', unibet: '#7d8cff', vinn: '#f5b83d', kalshi: '#28d07f', thunderpick: '#8b5cf6', novibet: '#1e88e5',
};
SE.BOOK_NAMES = { polymarket: 'Polymarket', sportybet: 'SportyBet', betika: 'Betika', betpawa: 'betPawa', linebet: 'Linebet', fonbet: 'Fonbet', unibet: 'Unibet', vinn: 'VINN', kalshi: 'Kalshi', thunderpick: 'Thunderpick', novibet: 'Novibet' };
SE.bookColor = (b) => SE.BOOK_COLORS[b] || '#8e8e93';
SE.bookName = (b) => SE.BOOK_NAMES[b] || b;
SE.logo = (b) => `<img src="/assets/img/${SE.esc(b)}.png" alt="" onerror="this.style.visibility='hidden'">`;
SE.book = (b, cls = '') => `<span class="src ${cls}" data-tipsrc="${SE.esc(b)}">${SE.logo(b)}${SE.esc(SE.bookName(b))}</span>`;
SE.logos = (books) => `<span class="logos">${books.map((b) => `<span data-tipsrc="${SE.esc(b)}">${SE.logo(b)}</span>`).join('')}</span>`;

SE.move = (oldP, newP) => {
  if (newP == null) return `<span class="price-move faint">${SE.fmtOdds(oldP)} ${SE.ic('arrow_forward')} off</span>`;
  if (oldP == null) return `<span class="price-move">${SE.fmtOdds(newP)}</span>`;
  const up = newP < oldP; // shorter odds = more likely
  return `<span class="price-move"><span class="faint">${SE.fmtOdds(oldP)}</span>${SE.ic(up ? 'arrow_upward' : 'arrow_downward', up ? 'up' : 'down')}<b>${SE.fmtOdds(newP)}</b></span>`;
};

SE.median = (a) => {
  const v = a.filter((x) => x != null && isFinite(x)).sort((x, y) => x - y);
  if (!v.length) return null;
  const m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
};
SE.quantile = (a, q) => {
  const v = a.filter((x) => x != null && isFinite(x)).sort((x, y) => x - y);
  return v.length ? v[Math.min(v.length - 1, Math.max(0, Math.round((v.length - 1) * q)))] : null;
};
SE.csv = (name, cols, rows) => {
  const q = (v) => (v == null ? '' : /[",\n]/.test(String(v)) ? '"' + String(v).replace(/"/g, '""') + '"' : String(v));
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([[cols.map(q).join(','), ...rows.map((r) => r.map(q).join(','))].join('\n')], { type: 'text/csv' }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
};

// ---------------------------------------------------------------- shared context (rankings, feed delay, status)
// Refreshed every minute so any tooltip can say how fast a source is.
SE.ctx = { rank: new Map(), feed: new Map(), books: new Map(), opp: null, loaded: 0 };
SE.loadCtx = async () => {
  const since = Date.now() - 24 * 3600000;
  const [r, b, f] = await Promise.all([
    SE.api(`/rankings?since=${since}&min_jump=${SE.pref('minJump')}&window_s=${SE.pref('windowS')}&limit=0`).catch(() => null),
    SE.api('/books').catch(() => null),
    SE.sql(`SELECT book, ts_ms - book_ts_ms AS d FROM changes WHERE book_ts_ms IS NOT NULL AND kind = 'move' AND ts_ms >= ${since} ORDER BY id DESC LIMIT 30000`, 30000).catch(() => []),
  ]);
  if (r) {
    const srcs = r.rankings.sources;
    SE.ctx.rank = new Map(srcs.map((s, i) => [s.source, { ...s, pos: i + 1, of: srcs.length }]));
    SE.ctx.opp = r.rankings.opportunity;
  }
  if (b) SE.ctx.books = new Map(b.books.map((x) => [x.name, x]));
  const by = new Map();
  for (const row of f) { if (!by.has(row.book)) by.set(row.book, []); by.get(row.book).push(row.d); }
  SE.ctx.feed = new Map([...by].map(([k, v]) => [k, { med: SE.median(v), p90: SE.quantile(v, 0.9), n: v.length }]));
  SE.ctx.loaded = Date.now();
};

// ---------------------------------------------------------------- tooltips
const tip = { el: null, target: null, timer: null, x: 0, y: 0 };
const kv = (rows) => `<div class="kv">${rows.filter((r) => r && r[1] !== '' && r[1] != null).map(([k, v]) => `<span>${k}</span><b>${v}</b>`).join('')}</div>`;
SE.kv = kv;

function srcTip(b) {
  const r = SE.ctx.rank.get(b), f = SE.ctx.feed.get(b), st = SE.ctx.books.get(b);
  const lines = [];
  if (r) {
    lines.push(['Speed rank', r.median_behind_first_ms != null ? `#${r.pos} of ${r.of}` : '']);
    lines.push(['Moves first', r.moved ? `${SE.fmtPct(r.first_share)} of ${r.moved}` : '']);
    lines.push(['Behind first mover', r.median_behind_first_ms != null ? SE.fmtSpan(r.median_behind_first_ms) : '']);
    if (b !== 'polymarket') lines.push(['Polymarket', r.median_vs_pm_ms != null ? SE.fmtRel(r.median_vs_pm_ms) : '']);
    if (b !== 'polymarket' && r.ahead_of_pm_share != null) lines.push(['Ahead of Polymarket', `${SE.fmtPct(r.ahead_of_pm_share)} of ${r.vs_pm_events}`]);
  }
  if (f) lines.push(['Feed delay', `${SE.fmtSpan(f.med)} median`]);
  if (b === 'kalshi' && r) lines.push(['Polymarket', r.median_vs_pm_ms != null ? SE.fmtRel(r.median_vs_pm_ms) : '']);
  if (b !== 'kalshi' && r && r.median_vs_kalshi_ms != null) lines.push(['Kalshi', SE.fmtRel(r.median_vs_kalshi_ms)]);
  if (st) {
    lines.push(['Events', st.live_events]);
    lines.push(['Updates', st.paused ? 'Paused' : st.poll_ms ? `every ${SE.fmtSpan(st.poll_ms)}` : 'Live push']);
  }
  return `<h4>${SE.logo(b)}${SE.esc(SE.bookName(b))}</h4>${lines.length ? kv(lines) : '<p>No moves measured yet.</p>'}<hr><p>Last 24 hours, 1X2 moves of ${Math.round(SE.pref('minJump') * 100)}+ points.</p>`;
}

/** Tooltip for one price: q = {b, h, a, m, l, o, p, pv, u, bt} */
function quoteTip(q) {
  const r = SE.ctx.rank.get(q.b);
  const move = q.p && q.pv ? 1 / q.p - 1 / q.pv : null;
  return `<h4>${SE.logo(q.b)}${SE.esc(SE.bookName(q.b))}</h4>
    <p>${SE.vs(q.h, q.a)}</p>
    ${kv([
      ['Market', SE.esc(SE.marketLabel(q.m))],
      ['Selection', SE.esc(SE.outcomeLong(q.o, q.l))],
      ['Price', q.p ? `${SE.fmtOdds(q.p)} <span class="faint">${SE.fmtProb(1 / q.p)}</span>` : 'Suspended'],
      ['Previous', q.pv ? `${SE.fmtOdds(q.pv)} <span class="${move > 0 ? 'up' : 'down'}">${SE.fmtPts(move)} pts</span>` : ''],
      ['Changed', q.u ? `${SE.fmtTime(q.u, true)} <span class="faint">${SE.fmtAgo(q.u)}</span>` : ''],
      ['Feed delay', q.bt && q.u ? SE.fmtSpan(q.u - q.bt) : ''],
    ])}
    ${r ? `<hr>${kv([
      ['Speed rank', r.median_behind_first_ms != null ? `#${r.pos} of ${r.of}` : ''],
      ['Behind first mover', r.median_behind_first_ms != null ? SE.fmtSpan(r.median_behind_first_ms) : ''],
      q.b !== 'polymarket' ? ['Polymarket', r.median_vs_pm_ms != null ? SE.fmtRel(r.median_vs_pm_ms) : ''] : null,
    ])}` : ''}`;
}
SE.qattr = (q) => `data-tipq="${SE.attr(q)}"`;

function tipHtml(el) {
  if (el.dataset.tipsrc) return srcTip(el.dataset.tipsrc);
  if (el.dataset.tipq) { try { return quoteTip(JSON.parse(el.dataset.tipq)); } catch { return ''; } }
  const t = el.dataset.tip || '';
  return t.startsWith('<') ? t : `<p>${SE.esc(t)}</p>`;
}
function placeTip() {
  const el = tip.el, pad = 14;
  const w = el.offsetWidth, h = el.offsetHeight;
  let x = tip.x + pad, y = tip.y + pad;
  if (x + w > window.innerWidth - 8) x = tip.x - w - pad;
  if (y + h > window.innerHeight - 8) y = tip.y - h - pad;
  el.style.setProperty('--x', Math.max(8, x) + 'px');
  el.style.setProperty('--y', Math.max(8, y) + 'px');
}
SE.tipShow = (html, x, y) => {
  const el = tip.el;
  el.innerHTML = html;
  tip.x = x; tip.y = y;
  if (!el.classList.contains('show')) {
    // Appear at the cursor; only glide once visible (otherwise it flies in from its last spot).
    el.style.transition = 'none';
    placeTip();
    void el.offsetWidth;
    el.style.transition = '';
  } else placeTip();
  el.classList.add('show');
};
SE.tipHide = () => { tip.el?.classList.remove('show'); };
function initTips() {
  tip.el = $('#tip');
  document.addEventListener('mousemove', (e) => {
    tip.x = e.clientX; tip.y = e.clientY;
    if (tip.target && tip.el.classList.contains('show')) placeTip();
  }, { passive: true });
  document.addEventListener('mouseover', (e) => {
    const el = e.target.closest('[data-tip],[data-tipsrc],[data-tipq]');
    if (el === tip.target) return;
    tip.target = el;
    clearTimeout(tip.timer);
    if (!el) { SE.tipHide(); return; }
    tip.timer = setTimeout(() => {
      if (tip.target !== el) return;
      const h = tipHtml(el);
      if (h) SE.tipShow(h, tip.x, tip.y);
    }, 110);
  });
  document.addEventListener('scroll', () => { tip.target = null; SE.tipHide(); }, true);
}

// ---------------------------------------------------------------- select
/** Styled dropdown. opts: { options: [[value, label, sub?]], value, onChange, search?, placeholder?, width? } */
SE.select = (el, opts) => {
  let options = opts.options || [], value = opts.value;
  el.classList.add('sel');
  el.innerHTML = `<button type="button" class="sel-btn" style="${opts.width ? 'width:' + opts.width : ''}"><span class="v"></span>${SE.ic('expand_more')}</button>`;
  const btn = $('.sel-btn', el);
  const label = () => {
    const o = options.find((x) => String(x[0]) === String(value));
    $('.v', btn).textContent = o ? o[1] : opts.placeholder || 'Select';
  };
  let pop = null, hi = -1, shown = [];
  const outside = (e) => { if (!pop?.contains(e.target) && !el.contains(e.target)) close(); };
  const onKey = (e) => {
    if (!pop) return;
    if (e.key === 'Escape') { close(); btn.focus(); }
    else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      hi = Math.max(0, Math.min(shown.length - 1, hi + (e.key === 'ArrowDown' ? 1 : -1)));
      $$('.sel-opt', pop).forEach((x, i) => x.classList.toggle('hi', i === hi));
      $$('.sel-opt', pop)[hi]?.scrollIntoView({ block: 'nearest' });
    } else if (e.key === 'Enter' && shown[hi]) { e.preventDefault(); pick(shown[hi][0]); }
  };
  function close() {
    if (!pop) return;
    const p = pop;
    pop = null;
    el.classList.remove('open');
    p.classList.remove('show');
    setTimeout(() => p.remove(), 160);
    document.removeEventListener('mousedown', outside, true);
    document.removeEventListener('keydown', onKey);
    window.removeEventListener('resize', close);
    $('#main').removeEventListener('scroll', close);
  }
  function pick(v) { value = v; label(); close(); opts.onChange?.(v); }
  function render(q = '') {
    const ql = q.trim().toLowerCase();
    shown = options.filter((o) => !ql || String(o[1]).toLowerCase().includes(ql));
    const list = $('.sel-list', pop);
    list.innerHTML = shown.length
      ? shown.map((o, i) => `<div class="sel-opt ${String(o[0]) === String(value) ? 'on' : ''} ${i === hi ? 'hi' : ''}" data-i="${i}">${SE.esc(o[1])}${o[2] ? `<small>${SE.esc(o[2])}</small>` : ''}${SE.ic('check')}</div>`).join('')
      : '<div class="sel-empty">No matches</div>';
    $$('.sel-opt', list).forEach((d) => {
      d.addEventListener('mousedown', (e) => { e.preventDefault(); pick(shown[+d.dataset.i][0]); });
      d.addEventListener('mousemove', () => { if (hi === +d.dataset.i) return; hi = +d.dataset.i; $$('.sel-opt', list).forEach((x) => x.classList.toggle('hi', x === d)); });
    });
  }
  function open() {
    if (pop) { close(); return; }
    const search = opts.search ?? options.length > 10;
    pop = document.createElement('div');
    pop.className = 'sel-pop';
    pop.innerHTML = `${search ? '<input placeholder="Search">' : ''}<div class="sel-list"></div>`;
    document.body.appendChild(pop);
    const r = btn.getBoundingClientRect();
    pop.style.minWidth = Math.max(220, r.width) + 'px';
    hi = Math.max(0, options.findIndex((o) => String(o[0]) === String(value)));
    render();
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 12)) + 'px';
    const below = window.innerHeight - r.bottom;
    if (below < 280 && r.top > below) { pop.style.bottom = window.innerHeight - r.top + 6 + 'px'; pop.style.transformOrigin = 'bottom left'; }
    else pop.style.top = r.bottom + 6 + 'px';
    el.classList.add('open');
    requestAnimationFrame(() => pop?.classList.add('show'));
    const inp = $('input', pop);
    if (inp) { inp.focus(); inp.addEventListener('input', () => { hi = 0; render(inp.value); }); }
    $('.sel-opt.on', pop)?.scrollIntoView({ block: 'nearest' });
    document.addEventListener('mousedown', outside, true);
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', close);
    $('#main').addEventListener('scroll', close);
  }
  btn.addEventListener('click', open);
  btn.addEventListener('keydown', (e) => { if (['ArrowDown', 'Enter', ' '].includes(e.key) && !pop) { e.preventDefault(); open(); } });
  label();
  return { set(o, v) { options = o; if (v !== undefined) value = v; label(); }, value: () => value };
};

// ---------------------------------------------------------------- live stream
const listeners = new Set();
SE.onChange = (fn) => { listeners.add(fn); return () => listeners.delete(fn); };
SE.live = { connected: false, perMin: [], total: 0 };
function connect() {
  const es = new EventSource('/stream');
  es.addEventListener('change', (e) => {
    let c;
    try { c = JSON.parse(e.data); } catch { return; }
    SE.live.total++;
    SE.live.perMin.push(Date.now());
    if (SE.live.perMin.length > 20000) SE.live.perMin.splice(0, 10000);
    for (const fn of listeners) { try { fn(c); } catch (err) { console.error(err); } }
  });
  es.onopen = () => (SE.live.connected = true);
  es.onerror = () => (SE.live.connected = false);
}
SE.ratePerMin = () => {
  const cut = Date.now() - 60000;
  SE.live.perMin = SE.live.perMin.filter((t) => t > cut);
  return SE.live.perMin.length;
};

// ---------------------------------------------------------------- charts (uPlot)
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
/** series: [{ label, color, values, book?, width?, dash? }] */
SE.chart = (el, xs, series, opts = {}) => {
  el.innerHTML = '';
  const yFmt = opts.yFmt || ((v) => (v == null ? '' : v.toFixed(2)));
  const axis = { stroke: cssVar('--text-3'), grid: { stroke: cssVar('--grid'), width: 1 }, ticks: { show: false }, font: '11px ' + cssVar('--font') };
  const u = new uPlot(
    {
      width: el.clientWidth || 600,
      height: opts.height || el.clientHeight || 300,
      ms: 1,
      cursor: { drag: { x: true, y: false }, points: { size: 7 } },
      legend: { show: false },
      scales: { x: { time: true }, y: opts.yRange ? { range: opts.yRange } : {} },
      axes: [
        { ...axis, values: (u, ticks) => {
          const span = (u.scales.x.max ?? 0) - (u.scales.x.min ?? 0);
          return ticks.map((t) => (span > 2 * 86400000 ? SE.fmtDateTime(t).split(' ').slice(0, 2).join(' ') : span > 3 * 3600000 ? SE.fmtTime(t).slice(0, 5) : SE.fmtTime(t)));
        } },
        { ...axis, size: 58, values: (u, ticks) => ticks.map(yFmt) },
      ],
      series: [
        {},
        ...series.map((s) => ({
          label: s.label, stroke: s.color, width: s.width || 1.6, dash: s.dash, spanGaps: true,
          paths: opts.step === false ? undefined : uPlot.paths.stepped({ align: 1 }), points: { show: false },
        })),
      ],
      hooks: {
        setCursor: [(u) => {
          const i = u.cursor.idx;
          if (i == null || u.cursor.left < 0) { SE.tipHide(); return; }
          const rows = series.map((s, k) => ({ s, v: u.data[k + 1][i], k })).filter((r) => r.v != null && u.series[r.k + 1].show);
          if (!rows.length) { SE.tipHide(); return; }
          rows.sort((a, b) => b.v - a.v);
          const rect = u.over.getBoundingClientRect();
          SE.tipShow(`<div class="t">${SE.fmtTime(u.data[0][i], true)}</div>` + rows.map((r) =>
            `<div class="row"><span class="src sm">${r.s.book ? SE.logo(r.s.book) : `<i style="width:10px;height:3px;background:${r.s.color};display:inline-block;border-radius:2px"></i>`}${SE.esc(r.s.label)}</span><b>${yFmt(r.v)}</b></div>`).join(''),
          rect.left + u.cursor.left, rect.top + u.cursor.top);
        }],
      },
    },
    [xs, ...series.map((s) => s.values)],
    el
  );
  u.over.addEventListener('mouseleave', SE.tipHide);
  const ro = new ResizeObserver(() => u.setSize({ width: el.clientWidth, height: opts.height || el.clientHeight || 300 }));
  ro.observe(el);
  u._ro = ro;
  return u;
};
SE.destroyChart = (u) => { if (u) { u._ro?.disconnect(); u.destroy(); } };
SE.alignSeries = (lists) => {
  const xs = [...new Set(lists.flatMap((l) => l.map((p) => p[0])))].sort((a, b) => a - b);
  const values = lists.map((l) => {
    const vals = new Array(xs.length).fill(null);
    let j = 0, cur = null;
    for (let i = 0; i < xs.length; i++) {
      while (j < l.length && l[j][0] <= xs[i]) { cur = l[j][1]; j++; }
      vals[i] = cur;
    }
    return vals;
  });
  return { xs, values };
};
SE.legend = (el, series, u) => {
  el.innerHTML = series.map((s, i) => `<label data-i="${i + 1}" ${s.book ? `data-tipsrc="${SE.esc(s.book)}"` : ''}><i style="background:${s.color}"></i>${s.book ? SE.logo(s.book) : ''}${SE.esc(s.label)}</label>`).join('');
  $$('label', el).forEach((lab) => lab.addEventListener('click', () => {
    const i = +lab.dataset.i, show = !u.series[i].show;
    u.setSeries(i, { show });
    lab.classList.toggle('off', !show);
  }));
};

// ---------------------------------------------------------------- tables
/** cols: [{ key, label, tip, fmt(row), sort(row), cls }]  opts: { sort, dir, onClick, rowTip(row), empty, emptyIcon, limit, maxHeight, cls } */
SE.table = (el, cols, opts = {}) => {
  let rows = [];
  let sortKey = opts.sort || null, dir = opts.dir || -1;
  el.innerHTML = `<div class="scroll" style="${opts.maxHeight ? 'max-height:' + opts.maxHeight : ''}"><table class="tbl ${opts.cls || ''}"><thead><tr>${cols
    .map((c) => `<th class="${c.sort ? 'sortable' : ''} ${c.cls || ''}" data-k="${c.key}" ${c.tip ? `data-tip="${SE.esc(c.tip)}"` : ''}>${c.label}</th>`).join('')}</tr></thead><tbody></tbody></table></div>`;
  const tbody = $('tbody', el);
  $$('th.sortable', el).forEach((th) => th.addEventListener('click', () => {
    if (sortKey === th.dataset.k) dir = -dir; else { sortKey = th.dataset.k; dir = -1; }
    draw();
  }));
  function draw() {
    let r = rows;
    const c = cols.find((x) => x.key === sortKey);
    if (c?.sort) r = [...rows].sort((a, b) => { const x = c.sort(a), y = c.sort(b); if (x == null) return 1; if (y == null) return -1; return (x < y ? -1 : x > y ? 1 : 0) * dir; });
    $$('th', el).forEach((th) => { const col = cols.find((x) => x.key === th.dataset.k); th.innerHTML = col.label + (th.dataset.k === sortKey ? ' ' + SE.ic(dir < 0 ? 'arrow_downward' : 'arrow_upward') : ''); });
    if (!r.length) { tbody.innerHTML = `<tr><td colspan="${cols.length}"><div class="empty">${SE.ic(opts.emptyIcon || 'inbox')}${opts.empty || 'Nothing here yet.'}</div></td></tr>`; return; }
    tbody.innerHTML = r.slice(0, opts.limit || 2000).map((row) => `<tr class="${opts.onClick ? 'link' : ''}" data-i="${rows.indexOf(row)}" ${opts.rowTip ? `data-tip="${SE.esc(opts.rowTip(row))}"` : ''}>${cols.map((c) => `<td class="${c.cls || ''}">${c.fmt ? c.fmt(row) : SE.esc(row[c.key])}</td>`).join('')}</tr>`).join('');
    if (opts.onClick) $$('tr.link', tbody).forEach((tr) => tr.addEventListener('click', (e) => { if (!e.target.closest('a')) opts.onClick(rows[+tr.dataset.i]); }));
  }
  return { set(r) { rows = r || []; draw(); }, rows: () => rows };
};

/** Segmented control. options: [[value, label, tooltip?]] */
SE.seg = (el, options, value, onChange) => {
  el.classList.add('seg');
  el.innerHTML = options.map(([v, l, t]) => `<button data-v="${SE.esc(v)}" class="${String(v) === String(value) ? 'on' : ''}" ${t ? `data-tip="${SE.esc(t)}"` : ''}>${l}</button>`).join('');
  $$('button', el).forEach((b) => b.addEventListener('click', () => {
    $$('button', el).forEach((x) => x.classList.toggle('on', x === b));
    onChange(b.dataset.v);
  }));
};

// ---------------------------------------------------------------- router
SE.pages = {};
let cleanup = null, timers = [];
SE.every = (ms, fn) => { const t = setInterval(fn, ms); timers.push(t); return t; };
SE.crumbs = () => {};
function route() {
  const hash = location.hash.replace(/^#\/?/, '') || 'dashboard';
  const [name, ...rest] = hash.split('/');
  const key = SE.pages[name] ? name : 'dashboard';
  const page = SE.pages[key];
  if (typeof cleanup === 'function') { try { cleanup(); } catch {} }
  timers.forEach(clearInterval);
  timers = [];
  SE.tipHide();
  $$('.nav a[data-route]').forEach((a) => a.classList.toggle('on', a.dataset.route === (key === 'match' ? 'live' : key)));
  const el = $('#page');
  $('#main').scrollTop = 0;
  el.innerHTML = '';
  document.title = page.title + ' | Swifteye';
  try { cleanup = page.render(el, rest.map(decodeURIComponent)); }
  catch (e) { console.error(e); el.innerHTML = `<div class="empty err">${SE.ic('error')}${SE.esc(e.message)}</div>`; }
}
SE.go = (h) => (location.hash = h);

async function tickNav() { try { $('#navLive').textContent = (await SE.api('/matches')).count || ''; } catch {} }
async function tickMe() {
  try {
    const s = await SE.api('/pm/settings');
    $('#meDot').className = 'dot ' + (s.has_api ? 'ok' : s.has_wallet ? 'warn' : '');
    $('#meSub').textContent = s.has_api ? 'Polymarket linked' : s.has_wallet ? 'Wallet only' : 'Polymarket not linked';
  } catch {}
}
SE.refreshMe = tickMe;

window.addEventListener('hashchange', route);
window.addEventListener('DOMContentLoaded', () => {
  initTips();
  connect();
  route();
  tickNav();
  setInterval(tickNav, 15000);
  tickMe();
  SE.loadCtx();
  setInterval(SE.loadCtx, 60000);
});
