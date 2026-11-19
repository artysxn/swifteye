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
    /// Real money this bot may use, of the shared accounts: its own wallet inside the account.
    /// Its open real positions may never cost more than this plus its real results since
    /// `real_since`. 0 = no real money.
    pub bankroll: f64,
    /// The paper wallet starts here (ms): earlier paper trades are history, not this wallet's.
    pub paper_since: Ms,
    /// The real wallet starts here (ms): set when its amount is set or a test run starts.
    pub real_since: Ms,
}

impl Default for Common {
    fn default() -> Self {
        Common { enabled: true, real: false, start: 10_000.0, max_trade: 250.0, max_open: 2_000.0, daily_loss: 300.0, bankroll: 0.0, paper_since: 0, real_since: 0 }
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
    /// The wider rule set (more trades, lower win rate) trades too, with its own small stake:
    /// share of the balance, most dollars a trade, and its own daily loss stop (0 = none).
    pub b_enabled: bool,
    pub b_pct: f64,
    pub b_max_trade: f64,
    pub b_daily_loss: f64,
    /// The real wallet's own stakes, never the paper wallet's: share of the real balance per
    /// trade and most dollars a trade. (Its most open at once is the wallet itself, `bankroll`;
    /// its loss stop is `daily_loss`.)
    pub r_pct: f64,
    pub r_max_trade: f64,
    /// Give up after this many seconds (0 = what the Adaptive rules say).
    pub give_up_s: i64,
    /// Trade only between these UTC hours [from, to); equal = all day.
    pub hours_utc: [u8; 2],
}

impl SnipeCfg {
    /// The stake of a real safe-set trade on a real balance of `balance` at edge factor 1.
    pub fn real_stake(&self, balance: f64) -> f64 {
        (self.r_pct * balance).min(self.r_max_trade)
    }
    pub fn hours(&self) -> Option<[u8; 2]> {
        (self.hours_utc[0] != self.hours_utc[1]).then_some(self.hours_utc)
    }
}

impl Default for SnipeCfg {
    fn default() -> Self {
        SnipeCfg { c: Common::default(), pct: 0.01, edge_sizing: true, shadow_one_book: true, b_enabled: true, b_pct: 0.06, b_max_trade: 12.0, b_daily_loss: 15.0, r_pct: 0.30, r_max_trade: 12.0, give_up_s: 900, hours_utc: [12, 21] }
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
    let sp = saved_path(&app.db_path);
    if let Some(v) = std::fs::read_to_string(&sp).ok().and_then(|t| serde_json::from_str::<Value>(&t).ok()) {
        *SAVED.lock() = Some((sp, v));
    }
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

/// The same for the snipe bot's real or paper side alone: a paper position (the paper twin of a
/// real trade, the wider set) never takes a real trade's room in a game, and the other way round.
pub fn snipe_game_room(c: &Connection, match_id: i64, real: bool) -> f64 {
    let open: f64 = c
        .query_row(
            "SELECT COALESCE(SUM(json_extract(data, '$.cost')), 0) FROM bot_trades WHERE match_id = ?1 AND closed_ms IS NULL AND bot = ?2 AND real = ?3",
            params![match_id, SNIPE, real as i64],
            |r| r.get(0),
        )
        .unwrap_or(0.0);
    (settings().game_cap - open).max(0.0)
}

/// Start of today (UTC), ms.
fn today() -> Ms {
    let now = now_ms();
    now - now.rem_euclid(86_400_000)
}

/// Snipe ledger (one bot name, paper or real): (closed P&L, open cost, today's closed P&L, trades, wins).
pub fn snipe_ledger(c: &Connection, bot: &str, real: bool) -> (f64, f64, f64, usize, usize) {
    let t0 = today();
    // Each wallet counts its own trades from its own start: real money begins at its amount
    // whatever earlier real trades did.
    let since = if real { settings().snipe.c.real_since } else { settings().snipe.c.paper_since };
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
        if let Some(r) = s.run.as_mut() {
            r.prev.c.paper_since = now;
        }
        event("paper wallets restarted");
    }
    if let Some(v) = edit.get("snipe") {
        // During a test run the wallet settings belong to the run: edits go to the settings
        // that come back when it ends.
        // The real wallet's amount: a new amount starts the wallet afresh (not during a test run,
        // which keeps its results). The cap at the account's cash is checked by the caller.
        let (old_bank, in_run, now) = (s.snipe.c.bankroll, s.run.is_some(), now_ms());
        let w: &mut SnipeCfg = match s.run.as_mut() {
            Some(r) => &mut r.prev,
            None => &mut s.snipe,
        };
        common(&mut w.c, v);
        if let Some(x) = num(v, "pct", 0.001, 0.5) {
            w.pct = x;
        }
        let both: Vec<&mut SnipeCfg> = match s.run.as_mut() {
            Some(r) => vec![&mut r.prev, &mut s.snipe],
            None => vec![&mut s.snipe],
        };
        for w in both {
            if let Some(x) = num(v, "bankroll", 0.0, 1e8) {
                w.c.bankroll = x;
                if (x - old_bank).abs() > 1e-9 && !in_run {
                    w.c.real_since = now;
                }
            }
            if let Some(x) = num(v, "daily_loss", 0.0, 1e7) {
                w.c.daily_loss = x;
            }
            if let Some(x) = num(v, "r_pct", 0.01, 1.0) {
                w.r_pct = x;
            }
            if let Some(x) = num(v, "r_max_trade", 1.0, 1e7) {
                w.r_max_trade = x;
            }
            if let Some(x) = boolv(v, "b_enabled") {
                w.b_enabled = x;
            }
            if let Some(x) = num(v, "b_pct", 0.0, 0.5) {
                w.b_pct = x;
            }
            if let Some(x) = num(v, "b_max_trade", 0.0, 1e7) {
                w.b_max_trade = x;
            }
            if let Some(x) = num(v, "b_daily_loss", 0.0, 1e7) {
                w.b_daily_loss = x;
            }
            if let Some(x) = num(v, "give_up_s", 0.0, 7200.0) {
                w.give_up_s = x as i64;
            }
            if let Some(h) = v.get("hours_utc").and_then(|x| x.as_array()).filter(|a| a.len() == 2) {
                w.hours_utc = [h[0].as_u64().unwrap_or(0).min(23) as u8, h[1].as_u64().unwrap_or(0).min(24) as u8];
            }
        }
        if let Some(x) = boolv(v, "edge_sizing") {
            s.snipe.edge_sizing = x;
        }
        if let Some(x) = boolv(v, "shadow_one_book") {
            s.snipe.shadow_one_book = x;
        }
    }
    // During a test run both copies of the settings stay the same but for the Real switch: the
    // run changes nothing about the paper wallet.
    let bank = s.snipe.c.bankroll;
    if let Some(r) = s.run.as_mut() {
        r.bankroll = bank;
        r.stake = r.prev.real_stake(bank);
        let p = r.prev.clone();
        s.snipe.c.start = p.c.start;
        s.snipe.c.max_trade = p.c.max_trade;
        s.snipe.c.max_open = p.c.max_open;
        s.snipe.pct = p.pct;
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
        k if k.starts_with("arb") => (serde_json::to_string(&(&s.arb, s.game_cap)).unwrap_or_default(), 900_000),
        k if k.starts_with("snipe") => (serde_json::to_string(&s.snipe).unwrap_or_default() + &serde_json::to_string(&crate::adaptive::active_rules()).unwrap_or_default(), 600_000),
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
            // Research: only gaps after the Kalshi book fix, optionally pre-match too.
            k if k.starts_with("arb-post") => {
                let mut a = s2.arb.clone();
                a.live_only = !k.contains("-all");
                if let Some(m) = k.split("min=").nth(1).and_then(|x| x.parse::<f64>().ok()) {
                    a.min_live = m;
                    a.min_pre = m;
                }
                crate::arbsim::run_since(&app2.db_path, &a, s2.game_cap, &prog, crate::arbsim::KALSHI_FIX_MS, k.contains("-kfirst"))
            }
            "snipe-wf" => Ok(crate::adaptive::bot_sim(&s2.snipe, true, false, crate::adaptive::Flt::live())),
            // The settings that apply outside a test run (the run's own are fixed small stakes).
            "snipe-plan" => Ok(crate::adaptive::bot_sim(s2.run.as_ref().map(|r| &r.prev).unwrap_or(&s2.snipe), true, false, crate::adaptive::Flt::live())),
            "snipe-plan-safe" => {
                let mut c = s2.run.as_ref().map(|r| r.prev.clone()).unwrap_or(s2.snipe.clone());
                c.b_enabled = false;
                Ok(crate::adaptive::bot_sim(&c, true, false, crate::adaptive::Flt::live()))
            }
            "snipe-nocap" => Ok(crate::adaptive::bot_sim(&s2.snipe, true, false, crate::adaptive::Flt { cap: false, ..crate::adaptive::Flt::live() })),
            "snipe-current" => Ok(crate::adaptive::bot_sim(&s2.snipe, false, false, crate::adaptive::Flt::live())),
            // What-if wallet: snipe-w:start=1000,max=300,open=1000,pct=0.2,bmax=60,bpct=0.06,bloss=75
            // (paper replay only; nothing is saved to the settings).
            k if k.starts_with("snipe-w:") => {
                let mut c = s2.run.as_ref().map(|r| r.prev.clone()).unwrap_or(s2.snipe.clone());
                for kv in k[8..].split(',') {
                    let (key, v) = kv.split_once('=').unwrap_or((kv, ""));
                    let Ok(x) = v.parse::<f64>() else { continue };
                    match key {
                        "start" => c.c.start = x,
                        "max" => c.c.max_trade = x,
                        "open" => c.c.max_open = x,
                        "pct" => c.pct = x,
                        "bmax" => c.b_max_trade = x,
                        "bpct" => c.b_pct = x,
                        "bloss" => c.b_daily_loss = x,
                        "wider" => c.b_enabled = x != 0.0,
                        _ => {}
                    }
                }
                Ok(crate::adaptive::bot_sim(&c, true, false, crate::adaptive::Flt::live()))
            }
            k if k.starts_with("snipe-x:") => Ok(crate::adaptive::bot_sim(&s2.snipe, true, false, parse_flt(&k[8..]))),
            _ => Ok(crate::adaptive::bot_sim(&s2.snipe, true, true, crate::adaptive::Flt::live())),
        };
        let mut sims = SIMS.lock();
        if let Some(slot) = sims.get_mut(&kind2) {
            if slot.key == key {
                slot.running = false;
                match r {
                    // The move table is still being rebuilt after a restart: the next poll retries.
                    Ok(v) if v["building"].as_bool() == Some(true) => slot.key.clear(),
                    Ok(v) => {
                        save_sim(&app2.db_path, &kind2, &v, &key);
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

/// The default real wallet.
pub const RUN_BANKROLL: f64 = 40.0;

/// A real wallet must fit the account: at most the Polymarket cash (as just read) less the reserve.
pub fn check_bankroll(bankroll: f64, cash: Option<f64>) -> anyhow::Result<()> {
    let reserve = settings().pm_reserve;
    check_bankroll_with(bankroll, cash, reserve)
}

fn check_bankroll_with(bankroll: f64, cash: Option<f64>, reserve: f64) -> anyhow::Result<()> {
    anyhow::ensure!(bankroll >= crate::snipe::MIN_FILL, "at least ${:.0} (the smallest order)", crate::snipe::MIN_FILL);
    let Some(cash) = cash else { anyhow::bail!("could not read the Polymarket balance, so the amount cannot be checked against it") };
    let most = (cash - reserve).max(0.0);
    anyhow::ensure!(bankroll <= most + 1e-9, "at most ${most:.2}: the account holds ${cash:.2} and ${reserve:.0} stays in reserve");
    Ok(())
}

/// Change the real wallet's amount during a test run (the run keeps its results).
pub fn set_run_bankroll(app: &App, bankroll: f64, cash: Option<f64>) -> anyhow::Result<Value> {
    anyhow::ensure!(settings().run.is_some(), "no test run going");
    check_bankroll(bankroll, cash)?;
    update(app, &json!({"snipe": {"bankroll": bankroll}}))?;
    event(format!("snipe: real wallet set to ${bankroll:.0}"));
    Ok(view(app))
}

/// Start a timed real test run: $`bankroll` of the Polymarket account (at most its cash),
/// 30% of it a trade.
pub fn start_run(app: &App, hours: i64, bankroll: f64, cash: Option<f64>, confirm: &str) -> anyhow::Result<Value> {
    anyhow::ensure!([4, 8, 9, 16, 24].contains(&hours), "4, 8, 9, 16 or 24 hours");
    anyhow::ensure!(confirm.trim() == "REAL MONEY", "type REAL MONEY to confirm");
    let miss = real_ready(app, SNIPE);
    anyhow::ensure!(miss.is_empty(), "missing: {}", miss.join(", "));
    let mut s = settings();
    anyhow::ensure!(s.run.is_none(), "a test run is already going");
    anyhow::ensure!(!s.kill, "the kill switch is on");
    let now = now_ms();
    check_bankroll(bankroll, cash)?;
    // The real wallet's own stakes (never the paper wallet's) must reach the smallest order.
    let stake = s.snipe.real_stake(bankroll);
    anyhow::ensure!(stake >= crate::snipe::MIN_FILL, "a real trade would be ${stake:.2}, under the $10 smallest order: raise the real stake or the amount");
    // Started outside the trading hours, the run's hours count from when the window opens (the
    // bot takes no trade before that anyway).
    let begin = match s.snipe.hours() {
        Some([a, b]) => {
            let day = now - now.rem_euclid(86_400_000);
            let (open, close) = (day + a as Ms * 3_600_000, day + b as Ms * 3_600_000);
            if a < b && now < open {
                open
            } else if a < b && now >= close {
                open + 86_400_000
            } else {
                now
            }
        }
        None => now,
    };
    // The real wallet starts afresh at its amount; the paper wallet and its stakes are untouched
    // and keep trading beside it.
    s.snipe.c.bankroll = bankroll;
    s.snipe.c.real_since = now;
    s.snipe.c.enabled = true;
    s.run = Some(Run { id: now, start_ms: now, end_ms: begin + hours * 3_600_000, bankroll, stake, prev: s.snipe.clone() });
    s.snipe.c.real = true;
    save(app, &s)?;
    *SET.write() = s;
    event(format!("snipe: {hours} h REAL test run started (${bankroll} bankroll, ~${stake} a trade{})", if begin > now { ", trading from the window's opening" } else { "" }));
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

/// Extend the running test run by `hours` (1..=48), total at most 7 days.
pub fn extend_run(app: &App, hours: i64) -> anyhow::Result<Value> {
    anyhow::ensure!((1..=48).contains(&hours), "1 to 48 hours");
    let mut s = settings();
    let Some(r) = s.run.as_mut() else { anyhow::bail!("no test run going") };
    anyhow::ensure!(r.end_ms + hours * 3_600_000 - r.start_ms <= 7 * 86_400_000, "a run can last at most 7 days");
    r.end_ms += hours * 3_600_000;
    let end = r.end_ms;
    save(app, &s)?;
    *SET.write() = s;
    event(format!("snipe: test run extended by {hours} h"));
    Ok(json!({"end_ms": end}))
}

/// Called every 30 s: ends a run at its end time, or early when a safety trigger fires (the
/// returned text goes to Telegram). Open real positions are still sold by their own watchers.
pub fn tick_run(app: &App) -> Option<String> {
    let r = settings().run?;
    if now_ms() >= r.end_ms {
        let _ = stop_run(app, "time up");
        return None;
    }
    let why = run_guard(app, &r)?;
    let rep = stop_run(app, &format!("safety stop: {why}")).ok()?;
    Some(format!(
        "🛑 Real test run stopped: {why}.\nReal money is off; the paper wallet keeps trading. {} real trades, {} won, {:+.2}.",
        rep["closed"], rep["wins"], rep["pnl"].as_f64().unwrap_or(0.0)
    ))
}

/// Safety triggers: the run ends early when real money does worse than the replay says it
/// should (replay, safe set: ~19 of 20 trades win, a loser costs about half its stake, the paper
/// twin of a trade fills within a cent or two).
const GUARD_WIN_SHARE: f64 = 0.60;
const GUARD_WIN_MIN_TRADES: usize = 4;
const GUARD_ONE_TRADE_LOSS: f64 = 0.80;
/// Orders Polymarket refused (an error, not just "nothing filled": a sale that waits for a bid
/// retries every second and a buy whose price ran is normal).
const GUARD_REFUSED_ORDERS: usize = 3;
/// Buys that found nothing to fill: a problem only when they are most of the attempts.
const GUARD_UNFILLED_BUYS: usize = 4;
const GUARD_TWIN_GAP: f64 = 0.10;

/// Why the running real test should stop now (None = all as expected).
fn run_guard(app: &App, r: &Run) -> Option<String> {
    let c = crate::store::open_ro(&app.db_path).ok()?;
    let rows = |real: bool| -> Vec<Value> {
        c.prepare("SELECT data FROM bot_trades WHERE bot = 'snipe' AND real = ?1 AND ts_ms >= ?2 AND closed_ms IS NOT NULL ORDER BY closed_ms")
            .and_then(|mut st| st.query_map(params![real as i64, r.start_ms], |x| x.get::<_, String>(0)).map(|it| it.filter_map(|x| x.ok()).filter_map(|s| serde_json::from_str(&s).ok()).collect()))
            .unwrap_or_default()
    };
    let real = rows(true);
    let count = |sql: &str| -> usize { c.query_row(sql, params![r.start_ms], |x| x.get::<_, i64>(0)).unwrap_or(0) as usize };
    let refused = count("SELECT COUNT(*) FROM bot_orders WHERE bot = 'snipe' AND ok = 0 AND ts_ms >= ?1 AND COALESCE(json_extract(data, '$.error'), '') NOT IN ('', 'nothing filled')");
    let unfilled_buys = count("SELECT COUNT(*) FROM bot_orders WHERE bot = 'snipe' AND ok = 0 AND ts_ms >= ?1 AND json_extract(data, '$.side') = 'buy' AND json_extract(data, '$.error') = 'nothing filled'");
    let filled_buys = count("SELECT COUNT(*) FROM bot_trades WHERE bot = 'snipe' AND real = 1 AND ts_ms >= ?1");
    run_guard_on(&real, &rows(false), (refused, unfilled_buys, filled_buys), settings().snipe.c.daily_loss)
}

/// The triggers on closed real trades (oldest first), the paper trades of the same hours, and
/// the real orders: (refused by Polymarket, buys that found nothing to fill, buys that filled).
fn run_guard_on(real: &[Value], paper: &[Value], orders: (usize, usize, usize), loss_limit: f64) -> Option<String> {
    let (refused, unfilled_buys, filled_buys) = orders;
    let pnl = |t: &Value| t["pnl"].as_f64().unwrap_or(0.0);
    let ret = |t: &Value| pnl(t) / t["cost"].as_f64().unwrap_or(0.0).max(1e-9);
    let total: f64 = real.iter().map(pnl).sum();
    if total <= -loss_limit {
        return Some(format!("real money is down ${:.2}, past the ${loss_limit:.0} limit", -total));
    }
    // A loss here is more than 5% of the stake: a give-up a cent under the buy price is a scratch.
    if real.len() >= 2 && real[real.len() - 2..].iter().all(|t| ret(t) < -0.05) {
        return Some("two losing real trades in a row (the replay expects about 1 loser in 20)".into());
    }
    let wins = real.iter().filter(|t| pnl(t) > 0.0).count();
    if real.len() >= GUARD_WIN_MIN_TRADES && (wins as f64) < GUARD_WIN_SHARE * real.len() as f64 {
        return Some(format!("only {wins} of {} real trades won (the replay expects about 19 of 20)", real.len()));
    }
    if let Some(t) = real.iter().find(|t| ret(t) <= -GUARD_ONE_TRADE_LOSS) {
        return Some(format!("one real trade lost {:.0}% of its stake ({})", -ret(t) * 100.0, t["home"].as_str().unwrap_or("")));
    }
    if refused >= GUARD_REFUSED_ORDERS {
        return Some(format!("Polymarket refused {refused} real orders"));
    }
    if unfilled_buys >= GUARD_UNFILLED_BUYS && unfilled_buys > filled_buys {
        return Some(format!("{unfilled_buys} of {} real buys found nothing to fill (the price runs before real orders match)", unfilled_buys + filled_buys));
    }
    // Real against its paper twin (the same move, bought within 30 s): real fills clearly worse
    // than paper means the paper model flatters the bot.
    let gaps: Vec<f64> = real
        .iter()
        .filter_map(|t| {
            let (m, ts) = (t["match_id"].as_i64()?, t["ts"].as_i64()?);
            let twin = paper.iter().find(|p| p["match_id"].as_i64() == Some(m) && p["set"] == t["set"] && (p["ts"].as_i64().unwrap_or(0) - ts).abs() <= 30_000)?;
            Some(ret(t) - ret(twin))
        })
        .collect();
    if gaps.len() >= 2 {
        let mean = gaps.iter().sum::<f64>() / gaps.len() as f64;
        if mean <= -GUARD_TWIN_GAP {
            return Some(format!("real trades return {:.0} points less than their paper twins over {} trades", -mean * 100.0, gaps.len()));
        }
    }
    None
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

/// Live trades against the replay over the last `hours` (see `adaptive::parity`).
pub fn parity(app: &Arc<App>, real: bool, hours: f64) -> Value {
    let s = settings();
    let cfg = s.run.as_ref().map(|r| r.prev.clone()).unwrap_or(s.snipe.clone());
    crate::adaptive::parity(&app.db_path, &cfg, real, now_ms() - (hours.clamp(0.1, 24.0 * 30.0) * 3_600_000.0) as Ms)
}

/// Finished simulations saved next to the database (`sims.json`), so a baseline survives restarts:
/// {kind: {"result": .., "computed_ms": .., "settings": ..}}.
static SAVED: LazyLock<parking_lot::Mutex<Option<(PathBuf, Value)>>> = LazyLock::new(Default::default);

fn saved_path(db: &std::path::Path) -> PathBuf {
    db.parent().map(|d| d.join("sims.json")).unwrap_or_else(|| PathBuf::from("sims.json"))
}

fn save_sim(db: &std::path::Path, kind: &str, result: &Value, settings: &str) {
    let p = saved_path(db);
    let mut all: Value = std::fs::read_to_string(&p).ok().and_then(|t| serde_json::from_str(&t).ok()).unwrap_or_else(|| json!({}));
    all[kind] = json!({"result": result, "computed_ms": now_ms(), "settings": settings});
    let _ = std::fs::write(&p, serde_json::to_string(&all).unwrap_or_default());
    *SAVED.lock() = Some((p, all));
}

/// "cap=1,pm=0.03,book=0.05,rev=0.5,noise=1,stop=0.5" -> filters (missing = off; cap defaults on).
fn parse_flt(s: &str) -> crate::adaptive::Flt {
    let mut f = crate::adaptive::Flt { cap: true, ..Default::default() };
    for kv in s.split(',') {
        let (k, v) = kv.split_once('=').unwrap_or((kv, ""));
        let x = v.parse::<f64>().ok();
        match k {
            "cap" => f.cap = v != "0",
            "pm" => f.pm_trend = x,
            "book" => f.book_trend = x,
            "rev" => f.rev = x,
            "noise" => f.noise = x,
            "stop" => f.stop = x,
            "revert" => f.revert = v != "0",
            "cons" => f.cons = x,
            "shock" => f.shock = x,
            "follow" => f.follow = x,
            "lead" => f.lead = x,
            "z" => f.z = x,
            "fams" => f.fams = x.map(|v| v as usize),
            "nobb" => f.no_basketball = v != "0",
            "maxpx" => f.max_px = x,
            "skipfeeds" => f.skip_feeds = v != "0",
            "picked" => f = crate::adaptive::Flt::picked(),
            _ => {}
        }
    }
    f
}

/// The newest simulation of a kind: this run's, else the saved baseline. (result, computed ms)
pub fn sim_cached(kind: &str) -> Option<(Value, Ms)> {
    if let Some(x) = SIMS.lock().get(kind).and_then(|x| x.result.clone().map(|r| (r, x.computed_ms))) {
        return Some(x);
    }
    let g = SAVED.lock();
    let (_, all) = g.as_ref()?;
    Some((all[kind]["result"].clone(), all[kind]["computed_ms"].as_i64().unwrap_or(0))).filter(|x| x.0.is_object())
}

/// When the current rules went live (two rule sets, consensus checks): trades before it ran on
/// other rules and are no evidence for these.
pub const RULES_SINCE_MS: Ms = 1_795_065_960_000;

/// The control panel of the snipe bot, paper or real: its money over time (every closed trade),
/// the open trades with the price they could be sold at now, today's signals, and its state.
pub fn panel(app: &App, real: bool) -> Value {
    let s = settings();
    let now = now_ms();
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return json!({"error": "database not readable"}) };
    // Real: the test run's own money while one runs, else the bot's real bankroll since the start.
    let (start, since, scope) = match (&s.run, real) {
        (Some(r), true) => (r.bankroll, r.start_ms, "run"),
        (_, true) => (s.snipe.c.bankroll, s.snipe.c.real_since, "real"),
        (_, false) => (s.snipe.c.start, s.snipe.c.paper_since, "paper"),
    };
    let rows: Vec<(Ms, Option<Ms>, Value)> = c
        .prepare("SELECT ts_ms, closed_ms, data FROM bot_trades WHERE bot = ?1 AND real = ?2 AND ts_ms >= ?3 ORDER BY ts_ms")
        .and_then(|mut st| {
            st.query_map(params![SNIPE, real as i64, since], |r| Ok((r.get::<_, Ms>(0)?, r.get::<_, Option<Ms>>(1)?, r.get::<_, String>(2)?)))
                .map(|it| it.filter_map(|x| x.ok()).filter_map(|(t, cl, d)| serde_json::from_str(&d).ok().map(|v| (t, cl, v))).collect())
        })
        .unwrap_or_default();
    let day0 = today();
    let (mut closed, mut today_pnl, mut open_cost) = (0.0, 0.0, 0.0);
    let mut trades = Vec::new();
    for (ts, cl, v) in &rows {
        let (dir, entry) = (v["dir"].as_f64().unwrap_or(1.0), v["entry"].as_f64().unwrap_or(0.0));
        let n = v["contracts"].as_f64().unwrap_or(0.0);
        // Where it sells: Polymarket has made `catch` of the bookmakers' move.
        let need = v["catch"].as_f64().unwrap_or(1.0) * v["move"].as_f64().unwrap_or(0.0);
        let target = v["pre_mid"].as_f64().map(|pre| (if dir > 0.0 { pre } else { 1.0 - pre } + need - v["spread"].as_f64().unwrap_or(0.0) / 2.0).clamp(0.0, 1.0));
        let mut t = json!({
            "ts": ts, "closed_ms": cl, "match_id": v["match_id"], "home": v["home"], "away": v["away"], "sport": v["sport"], "outcome": v["outcome"], "dir": dir,
            "set": v["set"].as_str().unwrap_or("safe"), "contracts": n, "entry": entry, "cost": v["cost"], "exit": v["exit"], "pnl": v["pnl"], "reason": v["reason"],
            "trigger": v["trigger"], "target": target, "give_up_ms": v["give_up_s"].as_i64().map(|g| ts + g * 1000),
            // Trades from before the current rules (19 Nov 05:26 UTC) carry no rule-set tag.
            "old": v["set"].is_null(),
        });
        match cl {
            Some(t_close) => {
                let p = v["pnl"].as_f64().unwrap_or(0.0);
                closed += p;
                if *t_close >= day0 {
                    today_pnl += p;
                }
            }
            None => {
                open_cost += v["cost"].as_f64().unwrap_or(0.0);
                // What it would fetch now: the best bid of the side held.
                let o = v["outcome"].as_str().and_then(crate::venue::outcome_of);
                let bid = o.and_then(|o| crate::venue::pm_contract(v["match_id"].as_i64().unwrap_or(0), o)).and_then(|(_, l, _)| if dir > 0.0 { l.bids.first().map(|x| x.0) } else { l.asks.first().map(|x| 1.0 - x.0) });
                t["bid_now"] = json!(bid);
                t["value_now"] = json!(bid.map(|b| n * b));
            }
        }
        trades.push(t);
    }
    // Today's decisions (UTC day).
    let signals: Vec<Value> = c
        .prepare("SELECT result, COUNT(*) FROM bot_signals WHERE ts_ms >= ?1 GROUP BY result ORDER BY 2 DESC")
        .and_then(|mut st| st.query_map([day0], |r| Ok(json!({"result": r.get::<_, String>(0)?, "n": r.get::<_, i64>(1)?}))).map(|it| it.filter_map(|x| x.ok()).collect()))
        .unwrap_or_default();
    // The trading window.
    let window = match s.snipe.hours() {
        Some([a, b]) if a < b => {
            let d0 = now - now.rem_euclid(86_400_000);
            let (open, close) = (d0 + a as Ms * 3_600_000, d0 + b as Ms * 3_600_000);
            let inside = now >= open && now < close;
            json!({"hours": [a, b], "open_now": inside, "next_open_ms": if now < open { open } else { open + 86_400_000 }, "closes_ms": if inside { json!(close) } else { Value::Null }})
        }
        _ => json!({"hours": Value::Null, "open_now": true}),
    };
    let wider_today: f64 = rows.iter().filter(|(_, cl, v)| cl.is_some_and(|t| t >= day0) && v["set"].as_str() == Some("wider")).map(|(_, _, v)| v["pnl"].as_f64().unwrap_or(0.0)).sum();
    json!({
        "real": real, "scope": scope, "start": start, "since": since, "now": now, "rules_since": RULES_SINCE_MS,
        "balance": start + closed, "closed_pnl": closed, "today": today_pnl, "open_cost": open_cost, "wider_today": wider_today,
        "trades": trades, "signals": signals, "window": window,
        "run": s.run.as_ref().map(|r| json!({"start_ms": r.start_ms, "end_ms": r.end_ms, "bankroll": r.bankroll, "stake": r.stake})),
        "mode": {"kill": s.kill, "enabled": s.snipe.c.enabled, "real": s.snipe.c.real},
        "rules": crate::adaptive::active_rules(),
    })
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

#[cfg(test)]
mod tests {
    use super::*;

    fn t(m: i64, ts: i64, cost: f64, pnl: f64) -> Value {
        json!({"match_id": m, "ts": ts, "cost": cost, "pnl": pnl, "set": "safe", "home": "A"})
    }

    #[test]
    fn run_guard_triggers() {
        let win = |m| t(m, m * 100_000, 15.0, 3.0);
        // As expected: wins, one ordinary loser, a scratch.
        assert_eq!(run_guard_on(&[win(1), win(2), t(3, 300_000, 15.0, -7.0), win(4), t(5, 500_000, 15.0, -0.3), win(6), win(7)], &[], (0, 0, 0), 15.0), None);
        // The loss limit.
        assert!(run_guard_on(&[t(1, 0, 15.0, -9.0), win(2), t(3, 0, 15.0, -9.5)], &[], (0, 0, 0), 15.0).unwrap().contains("limit"));
        // Two real losers in a row.
        assert!(run_guard_on(&[win(1), t(2, 0, 15.0, -3.0), t(3, 0, 15.0, -4.0)], &[], (0, 0, 0), 15.0).unwrap().contains("in a row"));
        // Too few winners after four trades.
        assert!(run_guard_on(&[t(1, 0, 15.0, -2.0), win(2), t(3, 0, 15.0, -2.0), win(4), t(5, 0, 15.0, -0.1)], &[], (0, 0, 0), 15.0).unwrap().contains("won"));
        // Nearly a whole stake on one trade.
        assert!(run_guard_on(&[win(1), t(2, 0, 15.0, -13.0)], &[], (0, 0, 0), 15.0).unwrap().contains("lost 87%"));
        // Orders that fail.
        assert!(run_guard_on(&[win(1)], &[], (3, 0, 0), 15.0).unwrap().contains("orders"));
        // Buys that find nothing: fine while most fill, a stop once they are most of the attempts.
        assert_eq!(run_guard_on(&[win(1)], &[], (0, 4, 6), 15.0), None);
        assert_eq!(run_guard_on(&[win(1)], &[], (2, 3, 1), 15.0), None);
        assert!(run_guard_on(&[win(1)], &[], (0, 5, 2), 15.0).unwrap().contains("nothing to fill"));
        // Real clearly worse than its paper twins (same game, bought within 30 s).
        let real = [t(1, 100_000, 15.0, 0.5), t(2, 200_000, 15.0, 0.6)];
        let paper = [t(1, 101_000, 100.0, 18.0), t(2, 202_000, 100.0, 16.0)];
        assert!(run_guard_on(&real, &paper, (0, 0, 0), 15.0).unwrap().contains("paper twins"));
        // The bankroll rules: $35 at least, never more than the account's cash less the reserve.
        assert!(check_bankroll_with(40.0, Some(192.74), 5.0).is_ok());
        assert!(check_bankroll_with(5.0, Some(192.74), 5.0).is_err());
        assert!(check_bankroll_with(190.0, Some(192.74), 5.0).is_err());
        assert!(check_bankroll_with(40.0, None, 5.0).is_err());
        // The real wallet's own stakes: 30% of $40, at most $12; never the paper wallet's $100.
        let c = SnipeCfg { c: Common { max_trade: 100.0, ..Default::default() }, pct: 0.2, ..Default::default() };
        assert_eq!((c.real_stake(40.0), c.real_stake(30.0), c.real_stake(1000.0)), (12.0, 9.0, 12.0));
        // Twins that agree: nothing.
        let paper = [t(1, 101_000, 100.0, 4.0), t(2, 202_000, 100.0, 5.0)];
        assert_eq!(run_guard_on(&real, &paper, (0, 0, 0), 15.0), None);
    }
}
