//! The two trading bots and what they share (Bots page).
//!
//! * Snipe bot (snipebot.rs): Polymarket only. Buys a price the bookmakers have already moved
//!   past, sells when Polymarket catches up. Directional risk; its own wallet.
//! * Hedge & arbitrage bot (predarb.rs): buys both sides across Polymarket and Kalshi when the
//!   pair costs under $1 after fees (plain gaps and Kalshi-lag locks), sells both early when the
//!   gap closes (auto-unwind). Locked-in risk; its own wallet.
//!
//! Wallets do not mix: each bot has its own starting balance, caps and Real switch, and its own
//! ledger (paper and real kept apart). Shared: the kill switch, a cap on dollars at risk in any
//! one game across both bots, and per-venue cash reserves for real money.
//!
//! Real money: a bot trades real money only when switched to Real here (typed confirmation),
//! with the venue keys saved. It falls back to paper by itself when its real losses today pass
//! `daily_loss`, and the kill switch stops every bot at once. Every real order is logged to
//! `bot_orders`.

use crate::model::*;
use crate::App;
use anyhow::Result;
use parking_lot::RwLock;
use rusqlite::{params, Connection};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::PathBuf;
use std::sync::{Arc, LazyLock};

pub const SNIPE: &str = "snipe";
/// Paper-only shadow of the snipe bot with 1 bookmaker instead of the picked rules' count.
pub const SNIPE_SHADOW: &str = "snipe-1book";
pub const ARB: &str = "arb";

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Common {
    /// The bot runs (paper or real).
    pub enabled: bool,
    /// Real money (false = paper).
    pub real: bool,
    /// Paper starting balance, dollars.
    pub start: f64,
    /// Most dollars in one trade / position.
    pub max_trade: f64,
    /// Most dollars in open positions at once.
    pub max_open: f64,
    /// Real money switches back to paper when today's real result is below -this.
    pub daily_loss: f64,
    /// Real money this bot may use, of the shared accounts: its open real positions may never
    /// cost more than this plus its real results so far. 0 = no real money.
    pub bankroll: f64,
    /// The paper wallet starts here (ms): earlier paper trades are history, not this wallet's.
    pub paper_since: Ms,
}

impl Default for Common {
    fn default() -> Self {
        Common { enabled: true, real: false, start: 10_000.0, max_trade: 250.0, max_open: 2_000.0, daily_loss: 300.0, bankroll: 0.0, paper_since: 0 }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct SnipeCfg {
    #[serde(flatten)]
    pub c: Common,
    /// Share of the balance per trade.
    pub pct: f64,
    /// Scale each stake by the expected edge: x clip(edge / 4c, 0.5, 3).
    pub edge_sizing: bool,
    /// Also paper-trade the same rules with 1 bookmaker (shadow, for comparison).
    pub shadow_one_book: bool,
}

impl Default for SnipeCfg {
    fn default() -> Self {
        SnipeCfg { c: Common::default(), pct: 0.01, edge_sizing: true, shadow_one_book: true }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct ArbCfg {
    #[serde(flatten)]
    pub c: Common,
    /// Least return of the whole position, live games / pre-match.
    pub min_live: f64,
    pub min_pre: f64,
    /// Every further pair bought must still return this.
    pub min_pair: f64,
    /// Sell both legs before the payout once the sale makes this share of the locked profit
    /// (1.0 = all of it, just sooner; 0 = never unwind).
    pub unwind_at: f64,
    /// Only games already playing (no pre-match: no called-off risk, money back sooner).
    pub live_only: bool,
}

impl Default for ArbCfg {
    fn default() -> Self {
        ArbCfg {
            c: Common { max_trade: 2_500.0, max_open: 10_000.0, daily_loss: 200.0, ..Common::default() },
            min_live: 0.01,
            min_pre: 0.02,
            min_pair: 0.005,
            unwind_at: 1.0,
            live_only: false,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    /// Stops every bot (no new trades, no exits are skipped: open positions still close).
    pub kill: bool,
    /// Most dollars at risk in one game across both bots.
    pub game_cap: f64,
    /// Real money: cash always left untouched on each venue.
    pub pm_reserve: f64,
    pub kalshi_reserve: f64,
    pub snipe: SnipeCfg,
    pub arb: ArbCfg,
    /// A timed real-money test run of the snipe bot in progress (Bots page → Test run).
    pub run: Option<Run>,
}

/// A timed real test run: the snipe bot trades real money with a small bankroll and fixed small
/// stakes until `end_ms`, then goes back to its previous settings by itself.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Run {
    pub id: Ms,
    pub start_ms: Ms,
    pub end_ms: Ms,
    pub bankroll: f64,
    pub stake: f64,
    /// The snipe settings before the run, restored after it.
    pub prev: SnipeCfg,
}

impl Default for Settings {
    fn default() -> Self {
        Settings { kill: false, game_cap: 3_000.0, pm_reserve: 50.0, kalshi_reserve: 50.0, snipe: SnipeCfg::default(), arb: ArbCfg::default(), run: None }
    }
}

static SET: LazyLock<RwLock<Settings>> = LazyLock::new(Default::default);
/// Last notable bot events (switches, trips, order failures), newest first.
static EVENTS: LazyLock<RwLock<Vec<(Ms, String)>>> = LazyLock::new(Default::default);

pub fn settings() -> Settings {
    SET.read().clone()
}

fn path(app: &App) -> PathBuf {
    app.db_path.parent().map(|d| d.join("bots.json")).unwrap_or_else(|| PathBuf::from("bots.json"))
}

fn save(app: &App, s: &Settings) -> anyhow::Result<()> {
    std::fs::write(path(app), serde_json::to_string_pretty(s)?)?;
    Ok(())
}

pub fn event(msg: impl Into<String>) {
    let m = msg.into();
    tracing::info!("bots: {m}");
    let mut e = EVENTS.write();
    e.insert(0, (now_ms(), m));
    e.truncate(50);
}

/// Load settings, create the tables. Called once at startup.
pub fn init(app: &App) -> anyhow::Result<()> {
    let s: Settings = std::fs::read_to_string(path(app)).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_default();
    *SET.write() = s;
    let c = Connection::open(&app.db_path)?;
    c.busy_timeout(std::time::Duration::from_secs(10))?;
    c.execute_batch(
        "CREATE TABLE IF NOT EXISTS bot_trades(id INTEGER PRIMARY KEY, bot TEXT NOT NULL, real INTEGER NOT NULL, ts_ms INTEGER NOT NULL,
             match_id INTEGER NOT NULL, closed_ms INTEGER, data TEXT NOT NULL);
         CREATE INDEX IF NOT EXISTS bot_trades_open ON bot_trades(bot, closed_ms);
         CREATE TABLE IF NOT EXISTS bot_tests(id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, bot TEXT NOT NULL, data TEXT NOT NULL);
         CREATE TABLE IF NOT EXISTS bot_signals(id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, match_id INTEGER, outcome TEXT, result TEXT NOT NULL, data TEXT);
         CREATE INDEX IF NOT EXISTS bot_signals_ts ON bot_signals(ts_ms);
         CREATE TABLE IF NOT EXISTS bot_runs(id INTEGER PRIMARY KEY, data TEXT NOT NULL);
         CREATE TABLE IF NOT EXISTS bot_orders(id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, bot TEXT NOT NULL, venue TEXT NOT NULL,
             ok INTEGER NOT NULL, data TEXT NOT NULL);",
    )?;
    Ok(())
}

/// Every real order, kept whatever happened.
pub fn log_order(app: &App, bot: &str, venue: &str, ok: bool, data: &Value) {
    if let Ok(c) = Connection::open(&app.db_path) {
        let _ = c.busy_timeout(std::time::Duration::from_secs(5));
        let _ = c.execute("INSERT INTO bot_orders(ts_ms, bot, venue, ok, data) VALUES (?1, ?2, ?3, ?4, ?5)", params![now_ms(), bot, venue, ok as i64, data.to_string()]);
    }
    if !ok {
        event(format!("{bot}: {venue} order failed: {}", data["error"].as_str().unwrap_or("")));
    }
}

/// Dollars at risk in one game across both bots: open arbitrage positions (both legs' cost) and
/// open snipe trades.
pub fn exposure(c: &Connection, match_id: i64) -> f64 {
    let now = now_ms();
    let arb: f64 = c
        .query_row(
            "SELECT COALESCE(SUM(json_extract(data, '$.cost')), 0) FROM pred_arbs WHERE match_id = ?1 AND json_extract(data, '$.payout_ms') > ?2 AND json_extract(data, '$.closed_ms') IS NULL",
            params![match_id, now],
            |r| r.get(0),
        )
        .unwrap_or(0.0);
    let snipe: f64 = c
        .query_row("SELECT COALESCE(SUM(json_extract(data, '$.cost')), 0) FROM bot_trades WHERE match_id = ?1 AND closed_ms IS NULL AND bot = ?2", params![match_id, SNIPE], |r| r.get(0))
        .unwrap_or(0.0);
    arb + snipe
}

/// Dollars a bot may still put into this game.
pub fn game_room(c: &Connection, match_id: i64) -> f64 {
    (settings().game_cap - exposure(c, match_id)).max(0.0)
}

/// Start of today (UTC), ms.
fn today() -> Ms {
    let now = now_ms();
    now - now.rem_euclid(86_400_000)
}

/// Snipe ledger (one bot name, paper or real): (closed P&L, open cost, today's closed P&L, trades, wins).
pub fn snipe_ledger(c: &Connection, bot: &str, real: bool) -> (f64, f64, f64, usize, usize) {
    let t0 = today();
    let since = if real { 0 } else { settings().snipe.c.paper_since };
    let mut st = match c.prepare("SELECT closed_ms, data FROM bot_trades WHERE bot = ?1 AND real = ?2 AND ts_ms >= ?3") {
        Ok(s) => s,
        Err(_) => return (0.0, 0.0, 0.0, 0, 0),
    };
    let rows: Vec<(Option<Ms>, Value)> = st
        .query_map(params![bot, real as i64, since], |r| Ok((r.get::<_, Option<Ms>>(0)?, r.get::<_, String>(1)?)))
        .map(|it| it.filter_map(|x| x.ok()).filter_map(|(c, s)| serde_json::from_str(&s).ok().map(|v| (c, v))).collect())
        .unwrap_or_default();
    let (mut closed, mut open, mut day, mut n, mut w) = (0.0, 0.0, 0.0, 0, 0);
    for (cl, v) in rows {
        match cl {
            Some(t) => {
                let p = v["pnl"].as_f64().unwrap_or(0.0);
                closed += p;
                n += 1;
                w += (p > 0.0) as usize;
                if t >= t0 {
                    day += p;
                }
            }
            None => open += v["cost"].as_f64().unwrap_or(0.0),
        }
    }
    (closed, open, day, n, w)
}

/// Arbitrage ledger (paper or real): (realized P&L, open cost, today's realized, positions).
/// A position is realized when sold (pnl) or when its payout time has passed (locked profit).
pub fn arb_ledger(c: &Connection, real: bool) -> (f64, f64, f64, usize) {
    let (now, t0) = (now_ms(), today());
    let mut st = match c.prepare("SELECT data FROM pred_arbs") {
        Ok(s) => s,
        Err(_) => return (0.0, 0.0, 0.0, 0),
    };
    let rows: Vec<Value> = st
        .query_map([], |r| r.get::<_, String>(0))
        .map(|it| it.filter_map(|x| x.ok()).filter_map(|s| serde_json::from_str(&s).ok()).collect())
        .unwrap_or_default();
    let (mut done, mut open, mut day, mut n) = (0.0, 0.0, 0.0, 0);
    let since = if real { 0 } else { settings().arb.c.paper_since };
    for v in rows.iter().filter(|v| v["real"].as_bool().unwrap_or(false) == real && v["ts"].as_i64().unwrap_or(0) >= since) {
        n += 1;
        let f = |k: &str| v[k].as_f64().unwrap_or(0.0);
        let (when, p) = if let Some(cl) = v["closed_ms"].as_i64() {
            (cl, f("pnl"))
        } else if v["payout_ms"].as_i64().unwrap_or(0) > now {
            open += f("cost");
            continue;
        } else {
            (v["payout_ms"].as_i64().unwrap_or(0), f("profit"))
        };
        done += p;
        if when >= t0 {
            day += p;
        }
    }
    (done, open, day, n)
}

/// Paper cash a bot has free.
pub fn paper_cash(c: &Connection, bot: &str) -> f64 {
    let s = settings();
    match bot {
        ARB => {
            let (done, open, _, _) = arb_ledger(c, false);
            s.arb.c.start + done - open
        }
        b => {
            let (closed, open, _, _, _) = snipe_ledger(c, b, false);
            s.snipe.c.start + closed - open
        }
    }
}

/// Real dollars this bot may still put into new positions: its bankroll plus its real results so
/// far, minus what its open real positions cost.
pub fn real_free(c: &Connection, bot: &str) -> f64 {
    let s = settings();
    match bot {
        ARB => {
            let (done, open, _, _) = arb_ledger(c, true);
            (s.arb.c.bankroll + done - open).max(0.0)
        }
        _ => {
            let (closed, open, _, _, _) = snipe_ledger(c, SNIPE, true);
            (s.snipe.c.bankroll + closed - open).max(0.0)
        }
    }
}

/// May this bot open a new position now, and is it real money? None = stopped.
pub fn mode(bot: &str) -> Option<bool> {
    let s = settings();
    if s.kill {
        return None;
    }
    let c = match bot {
        ARB => &s.arb.c,
        _ => &s.snipe.c,
    };
    if !c.enabled {
        return None;
    }
    Some(c.real && bot != SNIPE_SHADOW)
}

/// Real money switches itself off after a bad day. Call after each real close.
pub fn check_daily_loss(app: &App, bot: &str) {
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return };
    let s = settings();
    let (day, limit) = match bot {
        ARB => (arb_ledger(&c, true).2, s.arb.c.daily_loss),
        _ => (snipe_ledger(&c, SNIPE, true).2, s.snipe.c.daily_loss),
    };
    if day < -limit {
        let mut w = SET.write();
        match bot {
            ARB => w.arb.c.real = false,
            _ => w.snipe.c.real = false,
        }
        let s2 = w.clone();
        drop(w);
        let _ = save(app, &s2);
        event(format!("{bot}: real money switched OFF: today's real result ${day:.2} is past the daily limit -${limit:.0}"));
    }
}

/// Are the keys there for real money? (bot, missing pieces)
fn real_ready(app: &App, bot: &str) -> Vec<&'static str> {
    let pm = crate::pmacct::load(&app.db_path);
    let k = crate::kalacct::load(&app.db_path);
    let mut miss = Vec::new();
    if crate::exec::signer_address(&pm.private_key).is_err() {
        miss.push("Polymarket trading key");
    }
    if pm.api_key.trim().is_empty() || !crate::pmacct::secret_ok(&pm.api_secret) || pm.passphrase.trim().is_empty() {
        miss.push("Polymarket API key, secret and passphrase");
    }
    if bot == ARB && !crate::kalacct::has_key(&k) {
        miss.push("Kalshi API key");
    }
    miss
}

/// Settings edit from the Bots page (the Real switch has its own call).
pub fn update(app: &App, edit: &Value) -> anyhow::Result<Value> {
    let mut s = settings();
    let num = |v: &Value, k: &str, lo: f64, hi: f64| v.get(k).and_then(|x| x.as_f64()).map(|x| x.clamp(lo, hi));
    let boolv = |v: &Value, k: &str| v.get(k).and_then(|x| x.as_bool());
    if let Some(x) = boolv(edit, "kill") {
        if x != s.kill {
            event(if x { "KILL SWITCH ON: every bot stopped" } else { "kill switch off" });
        }
        s.kill = x;
    }
    if let Some(x) = num(edit, "game_cap", 0.0, 1e7) {
        s.game_cap = x;
    }
    if let Some(x) = num(edit, "pm_reserve", 0.0, 1e7) {
        s.pm_reserve = x;
    }
    if let Some(x) = num(edit, "kalshi_reserve", 0.0, 1e7) {
        s.kalshi_reserve = x;
    }
    let common = |c: &mut Common, v: &Value| {
        if let Some(x) = boolv(v, "enabled") {
            c.enabled = x;
        }
        if let Some(x) = num(v, "start", 10.0, 1e8) {
            c.start = x;
        }
        if let Some(x) = num(v, "max_trade", 1.0, 1e7) {
            c.max_trade = x;
        }
        if let Some(x) = num(v, "max_open", 1.0, 1e8) {
            c.max_open = x;
        }
        if let Some(x) = num(v, "daily_loss", 0.0, 1e7) {
            c.daily_loss = x;
        }
        if let Some(x) = num(v, "bankroll", 0.0, 1e8) {
            c.bankroll = x;
        }
    };
    // Start both paper wallets afresh from now (earlier paper trades stay as history).
    if edit.get("reset_paper").and_then(|x| x.as_bool()) == Some(true) {
        let now = now_ms();
        s.snipe.c.paper_since = now;
        s.arb.c.paper_since = now;
        event("paper wallets restarted");
    }
    if let Some(v) = edit.get("snipe") {
        common(&mut s.snipe.c, v);
        if let Some(x) = num(v, "pct", 0.001, 0.2) {
            s.snipe.pct = x;
        }
        if let Some(x) = boolv(v, "edge_sizing") {
            s.snipe.edge_sizing = x;
        }
        if let Some(x) = boolv(v, "shadow_one_book") {
            s.snipe.shadow_one_book = x;
        }
    }
    if let Some(v) = edit.get("arb") {
        common(&mut s.arb.c, v);
        if let Some(x) = num(v, "min_live", 0.001, 0.5) {
            s.arb.min_live = x;
        }
        if let Some(x) = num(v, "min_pre", 0.001, 0.5) {
            s.arb.min_pre = x;
        }
        if let Some(x) = num(v, "min_pair", 0.0, 0.5) {
            s.arb.min_pair = x;
        }
        if let Some(x) = num(v, "unwind_at", 0.0, 1.0) {
            s.arb.unwind_at = x;
        }
        if let Some(x) = boolv(v, "live_only") {
            s.arb.live_only = x;
        }
    }
    save(app, &s)?;
    *SET.write() = s;
    Ok(view(app))
}

/// The Real switch. Turning real ON needs the typed confirmation and the keys.
pub fn set_real(app: &App, bot: &str, real: bool, confirm: &str) -> anyhow::Result<Value> {
    if bot != SNIPE && bot != ARB {
        anyhow::bail!("unknown bot {bot}");
    }
    if real {
        if confirm.trim() != "REAL MONEY" {
            anyhow::bail!("type REAL MONEY to confirm");
        }
        let miss = real_ready(app, bot);
        if !miss.is_empty() {
            anyhow::bail!("missing: {}", miss.join(", "));
        }
        let s = settings();
        let bank = if bot == ARB { s.arb.c.bankroll } else { s.snipe.c.bankroll };
        if bank < crate::snipe::MIN_FILL {
            anyhow::bail!("set this bot's real bankroll first (how much of the accounts it may use)");
        }
    }
    let mut s = settings();
    match bot {
        ARB => s.arb.c.real = real,
        _ => s.snipe.c.real = real,
    }
    save(app, &s)?;
    *SET.write() = s;
    event(format!("{bot}: {}", if real { "switched to REAL MONEY" } else { "switched to paper" }));
    Ok(view(app))
}

// ---------------------------------------------------------------- whole-dataset simulations

struct SimSlot {
    key: String,
    running: bool,
    result: Option<Value>,
    error: Option<String>,
    computed_ms: Ms,
    prog: Arc<crate::arbsim::Progress>,
}

static SIMS: LazyLock<parking_lot::Mutex<std::collections::HashMap<String, SimSlot>>> = LazyLock::new(Default::default);

/// A whole-dataset simulation of one bot with its current settings, computed in the background and
/// cached per settings; the page polls until it is done. A finished result is served as it is and,
/// once older than 10 min (snipe) / 15 min (arbitrage, the heavy one), refreshed in the background
/// while the old one is still shown. A run is never restarted by time, only by a settings change.
/// Kinds: snipe-wf (walk-forward rules), snipe-current (today's rules on all data), snipe-shadow
/// (1 bookmaker), arb.
pub fn sim(app: &Arc<App>, kind: &str) -> Value {
    let s = settings();
    let now = now_ms();
    let (part, max_age) = match kind {
        "arb" => (serde_json::to_string(&(&s.arb, s.game_cap)).unwrap_or_default(), 900_000),
        "snipe-wf" | "snipe-current" | "snipe-shadow" => (serde_json::to_string(&s.snipe).unwrap_or_default() + &serde_json::to_string(&crate::adaptive::active_rules()).unwrap_or_default(), 600_000),
        _ => return json!({"state": "error", "error": "unknown simulation"}),
    };
    let key = format!("{kind}|{part}");
    let out = |slot: &SimSlot| {
        let p = &slot.prog;
        let state = if slot.result.is_some() { "done" } else if slot.running { "running" } else if slot.error.is_some() { "error" } else { "running" };
        json!({"state": state, "refreshing": slot.running && slot.result.is_some(),
            "progress": {"done": p.done.load(std::sync::atomic::Ordering::Relaxed), "total": p.total.load(std::sync::atomic::Ordering::Relaxed)},
            "result": slot.result, "error": slot.error, "computed_ms": slot.computed_ms})
    };
    let keep = {
        let sims = SIMS.lock();
        match sims.get(kind) {
            Some(slot) if slot.key == key => {
                if slot.running || (slot.result.is_some() && now - slot.computed_ms < max_age) || (slot.error.is_some() && now - slot.computed_ms < 60_000) {
                    return out(slot);
                }
                // Stale (or retry after an error): refresh in the background, keep showing the old result.
                Some((slot.result.clone(), slot.computed_ms))
            }
            _ => None,
        }
    };
    let prog = Arc::new(crate::arbsim::Progress { done: std::sync::atomic::AtomicUsize::new(0), total: std::sync::atomic::AtomicUsize::new(0) });
    let (old, old_ms) = keep.unwrap_or((None, 0));
    let slot = SimSlot { key: key.clone(), running: true, result: old, error: None, computed_ms: old_ms, prog: prog.clone() };
    let first = out(&slot);
    SIMS.lock().insert(kind.to_string(), slot);
    let (app2, kind2, s2) = (app.clone(), kind.to_string(), s.clone());
    tokio::task::spawn_blocking(move || {
        let r: anyhow::Result<Value> = match kind2.as_str() {
            "arb" => crate::arbsim::run(&app2.db_path, &s2.arb, s2.game_cap, &prog),
            "snipe-wf" => Ok(crate::adaptive::bot_sim(&s2.snipe, true, false)),
            "snipe-current" => Ok(crate::adaptive::bot_sim(&s2.snipe, false, false)),
            _ => Ok(crate::adaptive::bot_sim(&s2.snipe, true, true)),
        };
        let mut sims = SIMS.lock();
        if let Some(slot) = sims.get_mut(&kind2) {
            if slot.key == key {
                slot.running = false;
                match r {
                    // The move table is still being rebuilt after a restart: the next poll retries.
                    Ok(v) if v["building"].as_bool() == Some(true) => slot.key.clear(),
                    Ok(v) => {
                        slot.result = Some(v);
                        slot.error = None;
                        slot.computed_ms = now_ms();
                    }
                    Err(e) => {
                        slot.error = Some(format!("{e:#}"));
                        slot.computed_ms = now_ms();
                    }
                }
            }
        }
    });
    first
}

/// The shared real accounts: cash on each venue, and every position the bots believe they hold
/// next to what the account actually holds (both bots share one Polymarket and one Kalshi
/// account, so the account total must cover the sum of both bots).
pub async fn accounts(app: Arc<App>) -> Value {
    let pm = crate::pmacct::load(&app.db_path);
    let k = crate::kalacct::load(&app.db_path);
    let http = crate::books::http_client();
    let pm_ready = !pm.api_key.trim().is_empty() && !pm.wallet.trim().is_empty();
    let k_ready = crate::kalacct::has_key(&k);
    if !pm_ready && !k_ready {
        return json!({"keys": false});
    }
    let s = settings();
    let (pc, kc) = tokio::join!(
        async { if pm_ready { crate::exec::pm_cash(&http, &pm).await.map_err(|e| format!("{e:#}")) } else { Err("keys missing".to_string()) } },
        async { if k_ready { crate::exec::kalshi_cash(&http, &k).await.map_err(|e| format!("{e:#}")) } else { Err("keys missing".to_string()) } }
    );
    let (ph, kh) = crate::exec::holdings(&http, &pm, &k).await;
    // What the bots believe they hold on real money (open positions).
    let db = app.db_path.clone();
    let exp: Vec<(String, String, String, f64)> = tokio::task::spawn_blocking(move || {
        let mut v = Vec::new();
        let Ok(c) = crate::store::open_ro(&db) else { return v };
        if let Ok(mut st) = c.prepare("SELECT bot, data FROM bot_trades WHERE real = 1 AND closed_ms IS NULL") {
            for (bot, d) in st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))).map(|it| it.filter_map(|x| x.ok()).collect::<Vec<_>>()).unwrap_or_default() {
                if let Ok(x) = serde_json::from_str::<Value>(&d) {
                    v.push(("polymarket".into(), x["token"].as_str().unwrap_or("").into(), bot, x["contracts"].as_f64().unwrap_or(0.0)));
                }
            }
        }
        if let Ok(mut st) = c.prepare("SELECT data FROM pred_arbs WHERE json_extract(data, '$.real') = 1 AND json_extract(data, '$.closed_ms') IS NULL") {
            for d in st.query_map([], |r| r.get::<_, String>(0)).map(|it| it.filter_map(|x| x.ok()).collect::<Vec<_>>()).unwrap_or_default() {
                if let Ok(x) = serde_json::from_str::<Value>(&d) {
                    let n = x["contracts"].as_f64().unwrap_or(0.0);
                    v.push(("polymarket".into(), x["pm_token"].as_str().unwrap_or("").into(), ARB.into(), n));
                    let sign = if x["kalshi_yes"].as_bool().unwrap_or(false) { 1.0 } else { -1.0 };
                    v.push(("kalshi".into(), x["kalshi_ticker"].as_str().unwrap_or("").into(), ARB.into(), sign * n));
                }
            }
        }
        v
    })
    .await
    .unwrap_or_default();
    let mut by: std::collections::BTreeMap<(String, String), (f64, Vec<String>)> = Default::default();
    for (venue, id, bot, n) in exp {
        let e = by.entry((venue, id)).or_default();
        e.0 += n;
        if !e.1.contains(&bot) {
            e.1.push(bot);
        }
    }
    let rows: Vec<Value> = by
        .iter()
        .map(|((venue, id), (n, bots))| {
            let held = match venue.as_str() {
                "polymarket" => ph.as_ref().ok().map(|h| h.get(id).copied().unwrap_or(0.0)),
                _ => kh.as_ref().ok().map(|h| h.get(id).copied().unwrap_or(0.0)),
            };
            let status = match held {
                None => "account not readable",
                Some(h) if (h - n).abs() < 0.5 => "matches",
                Some(h) if (venue == "kalshi" && h.signum() != n.signum() && h != 0.0) || h.abs() + 0.5 < n.abs() => "MISSING on the account",
                Some(_) => "account holds more (other trades)",
            };
            json!({"venue": venue, "id": id, "bots": bots, "bots_hold": n, "account_holds": held, "status": status})
        })
        .collect();
    let others = |h: &Result<std::collections::HashMap<String, f64>>, venue: &str| h.as_ref().map(|m| m.keys().filter(|k| !by.contains_key(&(venue.to_string(), (*k).clone()))).count()).unwrap_or(0);
    json!({
        "keys": true,
        "polymarket": {"cash": pc.as_ref().ok(), "error": pc.as_ref().err(), "reserve": s.pm_reserve, "positions_error": ph.as_ref().err().map(|e| format!("{e:#}")), "other_positions": others(&ph, "polymarket")},
        "kalshi": {"cash": kc.as_ref().ok(), "error": kc.as_ref().err(), "reserve": s.kalshi_reserve, "positions_error": kh.as_ref().err().map(|e| format!("{e:#}")), "other_positions": others(&kh, "kalshi")},
        "rows": rows,
        "problems": rows.iter().filter(|r| r["status"] == "MISSING on the account").count(),
    })
}

/// Start a timed real test run: $`bankroll` of the Polymarket account, ~$`stake` a trade.
pub fn start_run(app: &App, hours: i64, confirm: &str) -> anyhow::Result<Value> {
    anyhow::ensure!([4, 8, 16, 24].contains(&hours), "4, 8, 16 or 24 hours");
    anyhow::ensure!(confirm.trim() == "REAL MONEY", "type REAL MONEY to confirm");
    let miss = real_ready(app, SNIPE);
    anyhow::ensure!(miss.is_empty(), "missing: {}", miss.join(", "));
    let mut s = settings();
    anyhow::ensure!(s.run.is_none(), "a test run is already going");
    anyhow::ensure!(!s.kill, "the kill switch is on");
    let now = now_ms();
    let (bankroll, stake) = (40.0, 12.0);
    s.run = Some(Run { id: now, start_ms: now, end_ms: now + hours * 3_600_000, bankroll, stake, prev: s.snipe.clone() });
    // Fixed small stakes: 30% of $40 is $12, capped at $12 (the bot's $10 minimum order fits).
    s.snipe.c.enabled = true;
    s.snipe.c.real = true;
    s.snipe.c.bankroll = bankroll;
    s.snipe.pct = 0.30;
    s.snipe.c.max_trade = stake;
    s.snipe.c.max_open = bankroll;
    s.snipe.c.daily_loss = 15.0;
    save(app, &s)?;
    *SET.write() = s;
    event(format!("snipe: {hours} h REAL test run started (${bankroll} bankroll, ~${stake} a trade)"));
    Ok(view(app))
}

/// End the test run now (or at its end time): settings back, report stored.
pub fn stop_run(app: &App, why: &str) -> anyhow::Result<Value> {
    let mut s = settings();
    let Some(r) = s.run.take() else { anyhow::bail!("no test run going") };
    s.snipe = r.prev.clone();
    s.snipe.c.real = false;
    save(app, &s)?;
    *SET.write() = s;
    let mut rep = run_report(app, &r, now_ms());
    rep["ended"] = json!(why);
    if let Ok(c) = Connection::open(&app.db_path) {
        let _ = c.execute("INSERT INTO bot_runs(id, data) VALUES (?1, ?2)", params![r.id, rep.to_string()]);
    }
    event(format!("snipe: test run ended ({why}): {} trades, {:+.2}", rep["trades"], rep["pnl"].as_f64().unwrap_or(0.0)));
    Ok(rep)
}

/// Called every 30 s: ends a run at its end time.
pub fn tick_run(app: &App) {
    if settings().run.as_ref().is_some_and(|r| now_ms() >= r.end_ms) {
        let _ = stop_run(app, "time up");
    }
}

/// What a run did: real snipe trades and the signals in its window.
pub fn run_report(app: &App, r: &Run, to: Ms) -> Value {
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return json!({}) };
    let trades: Vec<Value> = c
        .prepare("SELECT data FROM bot_trades WHERE bot = 'snipe' AND real = 1 AND ts_ms >= ?1 AND ts_ms <= ?2 ORDER BY ts_ms")
        .and_then(|mut st| st.query_map(params![r.start_ms, to], |x| x.get::<_, String>(0)).map(|it| it.filter_map(|x| x.ok()).filter_map(|s| serde_json::from_str(&s).ok()).collect()))
        .unwrap_or_default();
    let closed: Vec<&Value> = trades.iter().filter(|t| t["pnl"].is_number()).collect();
    let pnl: f64 = closed.iter().map(|t| t["pnl"].as_f64().unwrap_or(0.0)).sum();
    let signals: Vec<(String, i64)> = c
        .prepare("SELECT result, COUNT(*) FROM bot_signals WHERE ts_ms >= ?1 AND ts_ms <= ?2 GROUP BY result ORDER BY 2 DESC")
        .and_then(|mut st| st.query_map(params![r.start_ms, to], |x| Ok((x.get(0)?, x.get(1)?))).map(|it| it.filter_map(|x| x.ok()).collect()))
        .unwrap_or_default();
    json!({"id": r.id, "start_ms": r.start_ms, "end_ms": r.end_ms, "to": to, "bankroll": r.bankroll, "stake": r.stake,
        "trades": trades.len(), "closed": closed.len(), "open": trades.len() - closed.len(),
        "wins": closed.iter().filter(|t| t["pnl"].as_f64().unwrap_or(0.0) > 0.0).count(),
        "pnl": pnl, "staked": trades.iter().map(|t| t["cost"].as_f64().unwrap_or(0.0)).sum::<f64>(),
        "signals": signals.iter().map(|(k, n)| json!({"result": k, "n": n})).collect::<Vec<_>>(),
        "markets": c
            .prepare("SELECT s.match_id, s.outcome, COUNT(*), m.home, m.away FROM bot_signals s LEFT JOIN matches m ON m.id = s.match_id WHERE s.ts_ms >= ?1 AND s.ts_ms <= ?2 AND s.match_id IS NOT NULL GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 30")
            .and_then(|mut st| st.query_map(params![r.start_ms, to], |x| Ok(json!({"match_id": x.get::<_, i64>(0)?, "outcome": x.get::<_, Option<String>>(1)?, "n": x.get::<_, i64>(2)?, "home": x.get::<_, Option<String>>(3)?, "away": x.get::<_, Option<String>>(4)?}))).map(|it| it.filter_map(|x| x.ok()).collect::<Vec<_>>()))
            .unwrap_or_default(),
        "list": trades})
}

/// Runs: the one going (live report) and past ones.
pub fn runs(app: &App) -> Value {
    let cur = settings().run.map(|r| run_report(app, &r, now_ms()));
    let past: Vec<Value> = crate::store::open_ro(&app.db_path)
        .ok()
        .and_then(|c| {
            let mut st = c.prepare("SELECT data FROM bot_runs ORDER BY id DESC LIMIT 20").ok()?;
            let it = st.query_map([], |x| x.get::<_, String>(0)).ok()?;
            Some(it.filter_map(|x| x.ok()).filter_map(|s| serde_json::from_str(&s).ok()).collect())
        })
        .unwrap_or_default();
    json!({"current": cur, "past": past})
}

/// Minute chart of one market for a window: Polymarket bid / ask / last per minute, and the
/// bot's signals, buys and sells on it.
pub fn chart(app: &App, match_id: i64, outcome: &str, from: Ms, to: Ms) -> Value {
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return json!({}) };
    let rows: Vec<(Ms, Option<f64>, Option<f64>)> = c
        .prepare("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND ts_ms >= ?3 AND ts_ms <= ?4 ORDER BY ts_ms")
        .and_then(|mut st| st.query_map(params![match_id, outcome, from - 600_000, to], |x| Ok((x.get(0)?, x.get(1)?, x.get(2)?))).map(|it| it.filter_map(|x| x.ok()).collect()))
        .unwrap_or_default();
    // Last quote of each minute.
    let mut mins: std::collections::BTreeMap<Ms, (Option<f64>, Option<f64>)> = Default::default();
    for (t, b, a) in rows {
        mins.insert(t - t.rem_euclid(60_000), (b, a));
    }
    let sig: Vec<Value> = c
        .prepare("SELECT ts_ms, result FROM bot_signals WHERE match_id = ?1 AND outcome = ?2 AND ts_ms >= ?3 AND ts_ms <= ?4")
        .and_then(|mut st| st.query_map(params![match_id, outcome, from, to], |x| Ok(json!({"ts": x.get::<_, Ms>(0)?, "result": x.get::<_, String>(1)?}))).map(|it| it.filter_map(|x| x.ok()).collect()))
        .unwrap_or_default();
    let tr: Vec<Value> = c
        .prepare("SELECT data FROM bot_trades WHERE bot = 'snipe' AND match_id = ?1 AND ts_ms >= ?2 AND ts_ms <= ?3")
        .and_then(|mut st| st.query_map(params![match_id, from, to], |x| x.get::<_, String>(0)).map(|it| it.filter_map(|x| x.ok()).filter_map(|s| serde_json::from_str(&s).ok()).collect()))
        .unwrap_or_default();
    json!({"minutes": mins.iter().map(|(t, (b, a))| json!([t, b, a])).collect::<Vec<_>>(), "signals": sig, "trades": tr})
}

/// The last finished simulation of a kind, if any (Telegram /projected).
pub fn sim_cached(kind: &str) -> Option<Value> {
    SIMS.lock().get(kind).and_then(|x| x.result.clone())
}

/// Everything the Bots page shows.
pub fn view(app: &App) -> Value {
    let s = settings();
    let c = crate::store::open_ro(&app.db_path).ok();
    let snipe_side = |bot: &str, real: bool| -> Value {
        let Some(c) = &c else { return Value::Null };
        let (closed, open, day, n, w) = snipe_ledger(c, bot, real);
        let start = s.snipe.c.start;
        let free = if real && bot == SNIPE { json!(real_free(c, SNIPE)) } else { Value::Null };
        json!({"closed_pnl": closed, "open_cost": open, "today": day, "trades": n, "wins": w, "free": free,
            "balance": if real { Value::Null } else { json!(start + closed) }, "cash": if real { Value::Null } else { json!(start + closed - open) }})
    };
    let arb_side = |real: bool| -> Value {
        let Some(c) = &c else { return Value::Null };
        let (done, open, day, n) = arb_ledger(c, real);
        let free = if real { json!(real_free(c, ARB)) } else { Value::Null };
        json!({"realized": done, "open_cost": open, "today": day, "positions": n, "free": free,
            "balance": if real { Value::Null } else { json!(s.arb.c.start + done) }, "cash": if real { Value::Null } else { json!(s.arb.c.start + done - open) }})
    };
    let trades = |bot: &str| -> Vec<Value> {
        let Some(c) = &c else { return vec![] };
        let Ok(mut st) = c.prepare("SELECT data, real, closed_ms FROM bot_trades WHERE bot = ?1 ORDER BY ts_ms DESC LIMIT 100") else { return vec![] };
        st.query_map([bot], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?, r.get::<_, Option<Ms>>(2)?)))
            .map(|it| {
                it.filter_map(|x| x.ok())
                    .filter_map(|(d, real, cl)| {
                        let mut v: Value = serde_json::from_str(&d).ok()?;
                        v["real"] = json!(real != 0);
                        v["open"] = json!(cl.is_none());
                        Some(v)
                    })
                    .collect()
            })
            .unwrap_or_default()
    };
    let orders: Vec<Value> = c
        .as_ref()
        .and_then(|c| {
            let mut st = c.prepare("SELECT ts_ms, bot, venue, ok, data FROM bot_orders ORDER BY id DESC LIMIT 50").ok()?;
            let it = st
                .query_map([], |r| Ok(json!({"ts": r.get::<_, Ms>(0)?, "bot": r.get::<_, String>(1)?, "venue": r.get::<_, String>(2)?, "ok": r.get::<_, i64>(3)? != 0, "data": serde_json::from_str::<Value>(&r.get::<_, String>(4)?).unwrap_or(Value::Null)})))
                .ok()?;
            Some(it.filter_map(|x| x.ok()).collect())
        })
        .unwrap_or_default();
    let pm = crate::pmacct::load(&app.db_path);
    let rules = crate::adaptive::active_rules();
    json!({
        "settings": s,
        "events": EVENTS.read().iter().map(|(t, m)| json!({"ts": t, "msg": m})).collect::<Vec<_>>(),
        "keys": {
            "pm_trading_address": crate::exec::signer_address(&pm.private_key).ok(),
            "pm_wallet": pm.wallet,
            "pm_api": !pm.api_key.trim().is_empty() && !pm.api_secret.trim().is_empty() && !pm.passphrase.trim().is_empty(),
            "pm_secret_ok": crate::pmacct::secret_ok(&pm.api_secret),
            "pm_passphrase": !pm.passphrase.trim().is_empty(),
            "kalshi": crate::kalacct::has_key(&crate::kalacct::load(&app.db_path)),
            "missing_snipe": real_ready(app, SNIPE),
            "missing_arb": real_ready(app, ARB),
        },
        "snipe": {
            "paper": snipe_side(SNIPE, false), "real": snipe_side(SNIPE, true), "shadow": snipe_side(SNIPE_SHADOW, false),
            "rules": rules, "trades": trades(SNIPE), "shadow_trades": trades(SNIPE_SHADOW), "status": crate::snipebot::status(),
        },
        "arb": {
            "paper": arb_side(false), "real": arb_side(true), "scan": crate::predarb::view(app),
        },
        "orders": orders,
    })
}
