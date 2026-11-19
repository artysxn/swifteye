//! Telegram bot: alerts for odds moves on watched matches, pre-match arbitrage and closed
//! paper-bot trades, plus a few chat commands. Runs inside the collector.
//!
//! Settings live in `<db dir>/telegram.json` (with the bot token; that folder is not in the
//! repository). Linking: the Telegram page shows a t.me/<bot>?start=<code> link, and the bot
//! also accepts /start from the handle typed on the page. Only the linked chat gets alerts or
//! answers to commands.

use crate::model::*;
use crate::App;
use anyhow::{anyhow, Context, Result};
use parking_lot::Mutex;
use rusqlite::params;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
pub struct OddsAlerts {
    pub enabled: bool,
    /// Only matches with the bell on (false = every match in scope).
    pub watched_only: bool,
    /// Smallest move that is sent, in implied-probability points.
    pub min_move_pts: f64,
    /// "1x2", "total", "handicap", "btts".
    pub markets: Vec<String>,
    /// "all", "books" (bookmakers only) or "predictions" (Polymarket and Kalshi only).
    pub sources: String,
    /// Seconds before the same price line may alert again.
    pub cooldown_s: u64,
    pub watch: Vec<i64>,
    /// Per-game overrides, keyed by sport / e-sports key ("football", "cs2", ...). A game with
    /// no entry follows the settings above.
    pub games: HashMap<String, GameAlerts>,
}

impl Default for OddsAlerts {
    fn default() -> Self {
        OddsAlerts { enabled: false, watched_only: true, min_move_pts: 3.0, markets: vec!["1x2".into()], sources: "all".into(), cooldown_s: 60, watch: Vec::new(), games: HashMap::new() }
    }
}

/// One game's (sport's) own alert settings. `None` = use the general setting.
#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
pub struct GameAlerts {
    /// Off silences odds-move alerts for this game, bell-watched matches included.
    pub enabled: bool,
    pub min_move_pts: Option<f64>,
    pub markets: Option<Vec<String>>,
    pub sources: Option<String>,
    pub cooldown_s: Option<u64>,
    /// Arbitrage alerts for this game.
    pub arb_enabled: bool,
    pub arb_min_profit_pct: Option<f64>,
}

impl Default for GameAlerts {
    fn default() -> Self {
        GameAlerts { enabled: true, min_move_pts: None, markets: None, sources: None, cooldown_s: None, arb_enabled: true, arb_min_profit_pct: None }
    }
}

impl OddsAlerts {
    fn game(&self, sport: &str) -> Option<&GameAlerts> {
        self.games.get(sport)
    }
    /// Smallest move that can matter for any game: lets the hot path skip tiny moves before
    /// looking the match up.
    fn move_floor(&self) -> f64 {
        self.games.values().filter(|g| g.enabled).filter_map(|g| g.min_move_pts).fold(self.min_move_pts, f64::min)
    }
}

impl ArbAlerts {
    fn profit_floor(&self, odds: &OddsAlerts) -> f64 {
        odds.games.values().filter(|g| g.arb_enabled).filter_map(|g| g.arb_min_profit_pct).fold(self.min_profit_pct, f64::min)
    }
}

fn known_game(k: &str) -> bool {
    crate::books::SPORTS.contains(&k) || crate::books::ESPORTS.contains(&k)
}

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
pub struct ArbAlerts {
    pub enabled: bool,
    pub min_profit_pct: f64,
    /// Stake used for the split in the message.
    pub stake: f64,
}

impl Default for ArbAlerts {
    fn default() -> Self {
        ArbAlerts { enabled: false, min_profit_pct: 1.0, stake: 100.0 }
    }
}

/// Suspicious-match alerts (suspect.rs); thresholds live on the Suspicious page.
#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
pub struct SuspAlerts {
    pub enabled: bool,
    /// Heavily bet lower-tier CS2 games.
    pub activity: bool,
    /// Pre-match bookmaker line moves.
    pub lines: bool,
}

impl Default for SuspAlerts {
    fn default() -> Self {
        SuspAlerts { enabled: false, activity: true, lines: true }
    }
}

/// Paper bot activity: the default (and only default-on) Telegram message.
#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
pub struct PaperAlerts {
    pub enabled: bool,
    /// Snipe trades (bought and sold within minutes): one line each when they close.
    pub trades: bool,
    /// Polymarket x Kalshi arbitrage: bought, sold early, paid out.
    pub arbs: bool,
    /// Current paper balances on Polymarket and Kalshi under every message.
    pub standings: bool,
}

impl Default for PaperAlerts {
    fn default() -> Self {
        PaperAlerts { enabled: true, trades: true, arbs: true, standings: true }
    }
}

/// The snipe bot's own messages (paper or real): what gets sent.
#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
pub struct BotAlerts {
    pub enabled: bool,
    /// A message for each buy.
    pub buys: bool,
    /// A message for each sale.
    pub sells: bool,
    /// The price chart with each sale, and the balance chart after it.
    pub charts: bool,
    /// During a real run: its start and end, an update every hour, warnings on the way to the
    /// loss stop and refused orders.
    pub run_updates: bool,
}

impl Default for BotAlerts {
    fn default() -> Self {
        BotAlerts { enabled: true, buys: true, sells: true, charts: true, run_updates: true }
    }
}

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(default)]
pub struct TgCfg {
    pub token: String,
    /// The user's Telegram handle, without @.
    pub handle: String,
    pub chat_id: Option<i64>,
    pub linked_as: Option<String>,
    pub link_code: String,
    /// Master switch for alerts (commands keep working).
    pub enabled: bool,
    pub odds: OddsAlerts,
    pub arbs: ArbAlerts,
    pub paper: PaperAlerts,
    pub suspicious: SuspAlerts,
    pub bot_alerts: BotAlerts,
    /// Clock of the times in messages: "oslo" (Norway: CET, CEST in summer) or "utc".
    pub tz: String,
}

impl Default for TgCfg {
    fn default() -> Self {
        TgCfg {
            token: String::new(),
            handle: String::new(),
            chat_id: None,
            linked_as: None,
            link_code: String::new(),
            enabled: true,
            odds: OddsAlerts::default(),
            arbs: ArbAlerts::default(),
            paper: PaperAlerts::default(),
            suspicious: SuspAlerts::default(),
            bot_alerts: BotAlerts::default(),
            tz: "oslo".into(),
        }
    }
}

#[derive(Serialize, Clone, Default)]
struct BotInfo {
    username: Option<String>,
    error: Option<String>,
    checked_ms: Ms,
}

#[derive(Serialize, Clone)]
struct Sent {
    ts_ms: Ms,
    kind: &'static str,
    text: String,
    ok: bool,
    error: Option<String>,
}

static CFG: LazyLock<Mutex<TgCfg>> = LazyLock::new(Default::default);
static PATH: LazyLock<Mutex<PathBuf>> = LazyLock::new(|| Mutex::new(PathBuf::from("telegram.json")));
static BOT: LazyLock<Mutex<BotInfo>> = LazyLock::new(Default::default);
static LOG: LazyLock<Mutex<VecDeque<Sent>>> = LazyLock::new(Default::default);
/// When /realon was asked for, and for how many hours: "/realon yes" must follow within 2 minutes.
static REALON: LazyLock<Mutex<Option<(Ms, i64)>>> = LazyLock::new(Default::default);

fn save(c: &TgCfg) {
    let p = PATH.lock().clone();
    if let Err(e) = serde_json::to_string_pretty(c).map_err(anyhow::Error::from).and_then(|s| std::fs::write(&p, s).with_context(|| p.display().to_string())) {
        tracing::warn!("telegram: saving settings: {e:#}");
    }
}

fn new_code() -> String {
    use rand::Rng;
    const A: &[u8] = b"abcdefghijkmnpqrstuvwxyz23456789";
    let mut r = rand::thread_rng();
    (0..10).map(|_| A[r.gen_range(0..A.len())] as char).collect()
}

fn load(db: &Path) {
    let p = db.parent().map(|d| d.join("telegram.json")).unwrap_or_else(|| PathBuf::from("telegram.json"));
    let mut c: TgCfg = std::fs::read_to_string(&p).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default();
    *PATH.lock() = p;
    if c.link_code.is_empty() {
        c.link_code = new_code();
        save(&c);
    }
    *CFG.lock() = c;
}

fn cfg() -> TgCfg {
    CFG.lock().clone()
}

pub fn esc(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

fn client() -> reqwest::Client {
    reqwest::Client::builder().timeout(Duration::from_secs(40)).connect_timeout(Duration::from_secs(10)).build().unwrap_or_default()
}

async fn call(http: &reqwest::Client, token: &str, method: &str, body: Value) -> Result<Value> {
    let r = http.post(format!("https://api.telegram.org/bot{token}/{method}")).json(&body).send().await?;
    let v: Value = r.json().await?;
    if v["ok"].as_bool() != Some(true) {
        return Err(anyhow!("{}", v["description"].as_str().unwrap_or("Telegram refused the request")));
    }
    Ok(v["result"].clone())
}

/// Send to the linked chat. Telegram caps a message at 4096 characters.
pub async fn send(kind: &'static str, text: &str) -> Result<()> {
    // Owner 2026-11-18: "only send updates on what that bot does now" -> the snipe bot's trades
    // (paper and real) and the test message; every other alert kind is dropped here.
    if !matches!(kind, "bot" | "test") {
        return Ok(());
    }
    let c = cfg();
    let chat = c.chat_id.ok_or_else(|| anyhow!("no Telegram chat linked yet"))?;
    if c.token.is_empty() {
        return Err(anyhow!("no bot token"));
    }
    let mut t = text.to_string();
    if t.len() > 4000 {
        let mut cut = 3990;
        while !t.is_char_boundary(cut) {
            cut -= 1;
        }
        t.truncate(cut);
        t.push_str("\n…");
    }
    let res = call(&client(), &c.token, "sendMessage", json!({"chat_id": chat, "text": t, "parse_mode": "HTML", "disable_web_page_preview": true})).await;
    let mut log = LOG.lock();
    log.push_front(Sent { ts_ms: now_ms(), kind, text: t, ok: res.is_ok(), error: res.as_ref().err().map(|e| format!("{e:#}")) });
    log.truncate(40);
    res.map(|_| ())
}

fn pts(x: f64) -> String {
    format!("{}{:.1}", if x > 0.0 { "+" } else { "" }, x * 100.0)
}

fn outcome_name(o: &str) -> &str {
    match o {
        "home" => "1",
        "draw" => "X",
        "away" => "2",
        "over" => "Over",
        "under" => "Under",
        "yes" => "Yes",
        "no" => "No",
        x => x,
    }
}

fn market_name(m: &str) -> &str {
    match m {
        "1x2" => "1X2",
        "total" => "Total",
        "handicap" => "Handicap",
        "btts" => "BTTS",
        x => x,
    }
}

fn names(app: &App, id: i64) -> (String, String, Option<Ms>) {
    let eng = app.engine.lock();
    eng.matcher.matches.get(&id).map(|m| (m.home.clone(), m.away.clone(), m.start_ms)).unwrap_or_else(|| (format!("match {id}"), String::new(), None))
}

fn sport_of(app: &App, id: i64) -> String {
    app.engine.lock().matcher.matches.get(&id).map(|m| m.sport.clone()).unwrap_or_default()
}

/// "in 2h 05m" (or "started"): a phone shows Telegram in its own time zone, so relative is clearer.
fn fmt_kick(ms: Option<Ms>) -> String {
    let Some(ms) = ms else { return String::new() };
    let m = (ms - now_ms()) / 60_000;
    if m <= 0 {
        "started".into()
    } else if m < 60 {
        format!("in {m}m")
    } else {
        format!("in {}h {:02}m", m / 60, m % 60)
    }
}


// ---------------------------------------------------------------- snipe bot updates

/// Every 20 s: the snipe bot's new buys and sells (paper and real). Each sale comes with a chart
/// of that market's minute prices around the trade, the buy and the sale marked (Chart.js config
/// rendered to a PNG by QuickChart, as the aim4 insights bots do; Telegram fetches the image).
async fn bot_loop(app: Arc<App>) {
    let mut last_id: i64 = 0;
    let mut closed_seen: Ms = now_ms();
    // Start after what already exists.
    if let Ok(c) = crate::store::open_ro(&app.db_path) {
        last_id = c.query_row("SELECT COALESCE(MAX(id), 0) FROM bot_trades", [], |r| r.get(0)).unwrap_or(0);
    }
    let mut menu_set = String::new();
    let mut tick: u64 = 0;
    // The real run as last seen, to notice its start and end; the hourly update's next time;
    // how far towards the loss stop a warning has gone out; the last real order looked at.
    let next_hour = |t: Ms| t - t.rem_euclid(3_600_000) + 3_600_000;
    let mut run_id: Option<Ms> = crate::bots::settings().run.map(|r| r.id);
    let mut was_open = window_now(now_ms()).0;
    let mut next_digest: Ms = next_hour(now_ms());
    let mut warned: u8 = 0;
    let mut last_order: i64 = crate::store::open_ro(&app.db_path).ok().and_then(|c| c.query_row("SELECT COALESCE(MAX(id), 0) FROM bot_orders", [], |r| r.get(0)).ok()).unwrap_or(0);
    let mut refused_told: Ms = 0;
    loop {
        tokio::time::sleep(Duration::from_secs(20)).await;
        tick += 1;
        // Keep the whole-dataset replay fresh for /projected (cached; cheap when nothing changed).
        if tick % 15 == 1 {
            let _ = crate::bots::sim(&app, "snipe-wf");
        }
        let c = cfg();
        // The command menu in Telegram (once per token).
        if !c.token.is_empty() && menu_set != c.token {
            let menu: [(&str, &str); 35] = [
                ("status", "Both wallets and whether it is trading"), ("now", "Update right now"), ("open", "Open trades"), ("today", "Today's results"),
                ("real", "The real wallet"), ("paper", "The paper wallet"), ("account", "Cash on the Polymarket account"),
                ("trades", "Last 10 closed trades"), ("last", "The last trade"), ("chart", "Chart of the last trade"), ("best", "Best trades"), ("worst", "Worst trades"),
                ("week", "Last 7 days, day by day"), ("stats", "Win rate, average win and loss"), ("sports", "Results by sport"),
                ("run", "The real run"), ("safety", "Where the run stands against each safety stop"), ("twins", "Real trades against their paper twins"),
                ("compare", "Live trades against the replay"), ("extend", "Extend the run: /extend 2h"), ("realon", "Start real money (asks you to confirm)"), ("stoprun", "End the real run now"), ("realoff", "Real money off"),
                ("rules", "When it buys and sells"), ("settings", "Wallets, stakes, hours"), ("signals", "What it checked today and why it passed"), ("hours", "Trading hours"),
                ("replay", "The replay of this setup"), ("projected", "What money turns into: /projected 24h"), ("balance", "Real balance chart"), ("balancepaper", "Paper balance chart"),
                ("health", "Feeds, prices, replay"), ("kill", "Emergency: stop opening trades"), ("unkill", "Resume trading"), ("help", "Every command"),
            ];
            let cmds = json!({"commands": menu.iter().map(|(c, d)| json!({"command": c, "description": d})).collect::<Vec<_>>()});
            if call(&client(), &c.token, "setMyCommands", cmds).await.is_ok() {
                menu_set = c.token.clone();
            }
        }
        if !c.enabled || c.chat_id.is_none() {
            continue;
        }
        let a2 = app.clone();
        let (since_id, since_close) = (last_id, closed_seen);
        let rows: Vec<(i64, Value)> = tokio::task::spawn_blocking(move || {
            let Ok(c) = crate::store::open_ro(&a2.db_path) else { return Vec::new() };
            let Ok(mut st) = c.prepare("SELECT id, data, real, closed_ms FROM bot_trades WHERE bot = 'snipe' AND (id > ?1 OR closed_ms > ?2) ORDER BY id") else { return Vec::new() };
            st.query_map(params![since_id, since_close], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?, r.get::<_, Option<Ms>>(3)?)))
                .map(|it| {
                    it.filter_map(|x| x.ok())
                        .filter_map(|(id, d, real, cl)| {
                            let mut v: Value = serde_json::from_str(&d).ok()?;
                            v["real"] = json!(real != 0);
                            v["closed_ms"] = json!(cl);
                            Some((id, v))
                        })
                        .collect()
                })
                .unwrap_or_default()
        })
        .await
        .unwrap_or_default();
        // ---- the real run: start, window, hourly update, loss warnings, end
        let st = crate::bots::settings();
        let now = now_ms();
        let tell = c.bot_alerts.enabled && c.bot_alerts.run_updates;
        match (run_id, &st.run) {
            (was, Some(r)) if was != Some(r.id) => {
                run_id = Some(r.id);
                warned = 0;
                next_digest = next_hour(now);
                if tell {
                    let _ = send("bot", &run_started_text(&app, r)).await;
                }
            }
            (Some(id), None) => {
                run_id = None;
                let rep = crate::bots::runs(&app)["past"].as_array().and_then(|a| a.iter().find(|x| x["id"].as_i64() == Some(id)).cloned());
                if let (true, Some(rep)) = (tell, rep) {
                    let _ = send("bot", &run_ended_text(&app, &rep)).await;
                }
            }
            _ => {}
        }
        let (open, closes) = window_now(now);
        if st.run.is_some() && tell {
            if open && !was_open {
                let _ = send("bot", &format!("🔔 <b>Trading window open</b>{}
💼 {}
💼 {}", closes.map(|t| format!(" until {}", hhmm(t))).unwrap_or_default(), wallet_line(&app, true), wallet_line(&app, false))).await;
                next_digest = next_hour(now);
            }
            if open && now >= next_digest {
                let _ = send("bot", &digest_text(&app, "Hourly update")).await;
            }
            if let Some(f) = crate::bots::run_guard_facts(&app) {
                let (loss, limit) = (f["loss"].as_f64().unwrap_or(0.0), f["loss_limit"].as_f64().unwrap_or(0.0));
                let level = if limit > 0.0 && loss >= 0.75 * limit { 2 } else if limit > 0.0 && loss >= 0.5 * limit { 1 } else { 0 };
                if level > warned {
                    let _ = send("bot", &format!("⚠️ <b>Real wallet is down {}</b>
The run stops itself at −{}. /safety shows every stop, /stoprun ends it now.", dollars(loss), dollars(limit))).await;
                }
                warned = warned.max(level);
            }
        }
        if now >= next_digest {
            next_digest = next_hour(now);
        }
        was_open = open;
        // ---- real orders Polymarket refused (at most one message in 5 minutes: a sale retries)
        let refused: Vec<(i64, Value)> = crate::store::open_ro(&app.db_path)
            .ok()
            .and_then(|c| {
                let mut q = c.prepare("SELECT id, data FROM bot_orders WHERE id > ?1 AND ok = 0 ORDER BY id").ok()?;
                let it = q.query_map([last_order], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))).ok()?;
                Some(it.filter_map(|x| x.ok()).filter_map(|(id, d)| Some((id, serde_json::from_str(&d).ok()?))).collect())
            })
            .unwrap_or_default();
        for (id, d) in refused {
            last_order = last_order.max(id);
            let err = d["error"].as_str().unwrap_or("");
            if err.is_empty() || err == "nothing filled" || !tell || now - refused_told < 300_000 {
                continue;
            }
            refused_told = now;
            let _ = send("bot", &format!("⚠️ <b>Polymarket refused a real {}</b>
{}", if d["side"].as_str().unwrap_or("").starts_with("buy") { "buy" } else { "sale" }, esc(&err.chars().take(200).collect::<String>()))).await;
        }
        for (id, v) in rows {
            // While a real run is going the paper wallet speaks in single lines without charts:
            // the real trades are the news.
            let compact = !is_real(&v) && st.run.is_some();
            if id > last_id {
                last_id = id;
                if !(c.bot_alerts.enabled && c.bot_alerts.buys) {
                    continue;
                }
                // With the buy: where it stands, what it expects, where it cashes out, its
                // break-even and when it gives up.
                let text = buy_text(&v, compact);
                let chart = if !compact && c.bot_alerts.charts {
                    let (a2, v2) = (app.clone(), v.clone());
                    tokio::task::spawn_blocking(move || trade_chart(&a2, &v2)).await.ok().flatten()
                } else {
                    None
                };
                let sent = match chart {
                    Some(ch) => send_chart(&ch, &text).await,
                    None => Err(anyhow!("no chart")),
                };
                if sent.is_err() {
                    let _ = send("bot", &text).await;
                }
            }
            if let Some(cl) = v["closed_ms"].as_i64() {
                // Every sale since the last look, whatever order they closed in (two trades
                // sold in one look used to drop the one that closed earlier).
                if cl > since_close {
                    closed_seen = closed_seen.max(cl);
                    if !(c.bot_alerts.enabled && c.bot_alerts.sells) {
                        continue;
                    }
                    if compact {
                        let _ = send("bot", &sell_text(&v, true)).await;
                        continue;
                    }
                    let caption = sell_text(&v, false);
                    let a2 = app.clone();
                    let v2 = v.clone();
                    let chart = if c.bot_alerts.charts { tokio::task::spawn_blocking(move || trade_chart(&a2, &v2)).await.ok().flatten() } else { None };
                    let value = bot_balance(&app, is_real(&v));
                    let sent = match chart {
                        Some(cfgj) => {
                            let (ch, cap) = with_value(cfgj, &caption, &value);
                            send_chart(&ch, &cap).await
                        }
                        None => Err(anyhow!("no chart")),
                    };
                    if sent.is_err() {
                        let _ = send("bot", &format!("{caption}\n💼 {}", esc(&value))).await;
                    }
                    let a3 = app.clone();
                    let real = is_real(&v);
                    if c.bot_alerts.charts {
                        if let Ok(Some((ch, cap))) = tokio::task::spawn_blocking(move || balance_chart(&a3, real)).await {
                            let _ = send_chart(&ch, &cap).await;
                        }
                    }
                }
            }
        }
    }
}

/// Where a snipe trade pays: the bought side's price at which the bot sells (Polymarket has made
/// `catch` of the bookmakers' move), and the give-up time. (target 0..1, give-up ms)
fn pays_at(v: &Value) -> (Option<f64>, Option<Ms>) {
    let (pre, mv, sp) = (v["pre_mid"].as_f64(), v["move"].as_f64(), v["spread"].as_f64().unwrap_or(0.0));
    let need = v["catch"].as_f64().unwrap_or(1.0) * mv.unwrap_or(0.0);
    let target = pre.zip(mv).map(|(pre, _)| if v["dir"].as_f64().unwrap_or(1.0) > 0.0 { pre + need - sp / 2.0 } else { 1.0 - pre + need - sp / 2.0 });
    let give = v["ts"].as_i64().zip(v["give_up_s"].as_i64()).map(|(t, g)| t + g * 1000);
    (target.map(|x| x.clamp(0.0, 1.0)), give)
}

/// How long a winning snipe usually takes to cash in: the median time from buy to sale of the
/// bot's trades that sold on the catch-up (paper and real, last 200); 70 s until there are 5.
fn typical_hold_ms(app: &App) -> Ms {
    let v: Vec<Ms> = crate::store::open_ro(&app.db_path)
        .ok()
        .and_then(|c| {
            let mut st = c.prepare("SELECT closed_ms - ts_ms FROM bot_trades WHERE bot = 'snipe' AND closed_ms IS NOT NULL AND json_extract(data, '$.reason') = 'caught up' ORDER BY closed_ms DESC LIMIT 200").ok()?;
            let it = st.query_map([], |r| r.get::<_, Ms>(0)).ok()?;
            Some(it.filter_map(|x| x.ok()).filter(|x| *x > 0).collect())
        })
        .unwrap_or_default();
    if v.len() < 5 {
        return 70_000;
    }
    let mut v = v;
    v.sort();
    v[v.len() / 2]
}

/// "≈HH:MM (~N min)" estimated cash-in for a trade bought at `ts`.
fn eta_text(app: &App, ts: Ms) -> String {
    let h = typical_hold_ms(app);
    let mins = (h as f64 / 60_000.0).max(0.1);
    format!("≈{} (~{} typical)", hhmm(ts + h), if mins < 1.0 { format!("{:.0} s", h as f64 / 1000.0) } else { format!("{mins:.0} min") })
}

/// Chart.js config: the market's Polymarket bid / ask each minute from 10 min before the buy to 5
/// min after the sale, the buy (green) and the sale (red) marked, in the bought side's price.
/// The look shared with the aim4 insights bot's Telegram charts (tgcharts.js): straight thin
/// lines, no fill, a light horizontal grid only, a small legend at the bottom.
fn chart_options(title: &str, y_label: &str) -> Value {
    json!({
        "title": {"display": true, "text": title, "fontSize": 16},
        "legend": {"display": true, "position": "bottom", "labels": {"boxWidth": 12, "fontSize": 11}},
        "scales": {
            "yAxes": [{"scaleLabel": {"display": true, "labelString": y_label}, "gridLines": {"color": "#eee"}}],
            "xAxes": [{"ticks": {"maxTicksLimit": 12, "maxRotation": 0}, "gridLines": {"display": false}}]
        }
    })
}

fn trade_chart(app: &App, v: &Value) -> Option<Value> {
    trade_chart_at(&app.db_path, v, typical_hold_ms(app), now_ms())
}

/// The price a sale must reach to get the money back (the buy and both fees): solves
/// p - fee(p) = cost per share. None without a cost.
fn break_even(v: &Value) -> Option<f64> {
    let (n, cost) = (v["contracts"].as_f64()?, v["cost"].as_f64()?);
    if n <= 0.0 {
        return None;
    }
    let rate = v["fee_rate"].as_f64().unwrap_or(0.05);
    let c = cost / n;
    let mut p = c;
    for _ in 0..20 {
        p = c + rate * p * (1.0 - p);
    }
    Some(p.clamp(0.0, 1.0))
}

/// One trade against the price of the side bought: the price it can be sold at (line), the
/// offer (thin line), the buy and the sale (markers) and where it sells (dashed).
fn trade_chart_at(db: &Path, v: &Value, hold: Ms, now: Ms) -> Option<Value> {
    let c = crate::store::open_ro(db).ok()?;
    let (m, o) = (v["match_id"].as_i64()?, v["outcome"].as_str()?.to_string());
    let (target, give) = pays_at(v);
    let ts = v["ts"].as_i64()?;
    let end = v["closed_ms"].as_i64().unwrap_or(now);
    // The chart runs on past the give-up time, so an open trade shows what is still to come.
    let (t0, t1) = (ts - 600_000, (end + 300_000).max(give.unwrap_or(end) + 90_000));
    let yes = v["dir"].as_f64().unwrap_or(1.0) > 0.0;
    // A time grid fine enough to show the price path (at most ~180 points), not one box a minute.
    let span = t1 - t0;
    let step: Ms = [5_000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000, 600_000].into_iter().find(|s| span / s <= 180).unwrap_or(900_000);
    let n = (span / step) as usize + 1;
    // A quote older than this says nothing about that moment: the line breaks there.
    let stale = (3 * step).max(180_000);
    let mut st = c.prepare("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND ts_ms >= ?3 AND ts_ms <= ?4 ORDER BY ts_ms").ok()?;
    let rows: Vec<(Ms, Option<f64>, Option<f64>)> = st.query_map(params![m, o, t0 - stale, t1], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()?.filter_map(|x| x.ok()).collect();
    if rows.len() < 2 {
        return None;
    }
    let cents = |x: Option<f64>| x.map(|p| (p * 1000.0).round() / 10.0);
    let (mut bid, mut ask, mut labels) = (Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n));
    let mut k = 0;
    for i in 0..n {
        let t = t0 + i as Ms * step;
        while k < rows.len() && rows[k].0 <= t {
            k += 1;
        }
        // The bought side's price: Yes as is; No = 1 - the other side of the Yes book.
        let q = (k > 0 && t <= now).then(|| rows[k - 1]).filter(|r| t - r.0 <= stale).map(|(_, b, a)| if yes { (b, a) } else { (a.map(|x| 1.0 - x), b.map(|x| 1.0 - x)) });
        bid.push(cents(q.and_then(|x| x.0)));
        ask.push(cents(q.and_then(|x| x.1)));
        let (d, _) = local(t);
        // Unique labels (an invisible mark on repeats) so a time can be pointed at exactly.
        let l = format!("{:02}:{:02}", d.hour(), d.minute());
        let dup = labels.iter().filter(|x: &&String| x.trim_end_matches('\u{200b}') == l).count();
        labels.push(format!("{l}{}", "\u{200b}".repeat(dup)));
    }
    // The first grid point at or after `t`: where the lines first show what happened at `t`.
    let idx = |t: Ms| (((t - t0) as f64 / step as f64).ceil().max(0.0) as usize).min(n - 1);
    // The bookmakers on the same time grid and the same side: the one whose move triggered the
    // trade, and the middle of all of them (each feed family once), as the bot reads them. Their
    // prices are 1 / odds, so they sit on the same cents scale as Polymarket's.
    const BOOK_AGE: Ms = 20 * 60_000;
    let trig = v["trigger"].as_str().unwrap_or("").to_string();
    let mut books: HashMap<String, Vec<(Ms, f64)>> = HashMap::new();
    if let Ok(mut bs) = c.prepare("SELECT ts_ms, book, new_price FROM changes WHERE match_id = ?1 AND market = '1x2' AND outcome = ?2 AND ts_ms >= ?3 AND ts_ms <= ?4 AND new_price IS NOT NULL AND book NOT IN ('polymarket', 'kalshi') ORDER BY ts_ms") {
        if let Ok(it) = bs.query_map(params![m, o, t0 - BOOK_AGE, t1], |r| Ok((r.get::<_, Ms>(0)?, r.get::<_, String>(1)?, r.get::<_, f64>(2)?))) {
            for (t, b, p) in it.flatten() {
                books.entry(b).or_default().push((t, 1.0 / p.max(1.0001)));
            }
        }
    }
    let side = |p: f64| if yes { p } else { 1.0 - p };
    let at = |s: &[(Ms, f64)], t: Ms| -> Option<f64> {
        let i = s.partition_point(|x| x.0 <= t);
        (i > 0 && t <= now && t - s[i - 1].0 <= BOOK_AGE).then(|| s[i - 1].1)
    };
    let (mut trig_line, mut mid_line) = (Vec::with_capacity(n), Vec::with_capacity(n));
    for i in 0..n {
        let t = t0 + i as Ms * step;
        trig_line.push(cents(books.get(&trig).and_then(|s| at(s, t)).map(side)));
        let mut fam: std::collections::BTreeMap<&str, (f64, usize)> = Default::default();
        for (b, s) in &books {
            if let Some(p) = at(s, t) {
                let e = fam.entry(crate::snipe::platform(b)).or_default();
                e.0 += p;
                e.1 += 1;
            }
        }
        let mut means: Vec<f64> = fam.values().map(|(s, k)| s / *k as f64).collect();
        means.sort_by(|a, b| a.total_cmp(b));
        mid_line.push(cents((!means.is_empty()).then(|| side(means[means.len() / 2]))));
    }
    // The bookmaker update the bot reacted to.
    let mut signal = vec![Value::Null; n];
    if let Some(mv) = v["move_ms"].as_i64() {
        if let Some(p) = books.get(&trig).and_then(|s| at(s, mv + 1_500)) {
            signal[idx(mv)] = json!(cents(Some(side(p))));
        }
    }
    let mut buy = vec![Value::Null; n];
    let mut sell = vec![Value::Null; n];
    buy[idx(ts)] = json!(cents(v["entry"].as_f64()));
    if let Some(cl) = v["closed_ms"].as_i64() {
        sell[idx(cl)] = json!(cents(v["exit"].as_f64()));
    }
    let pays = target.map(|x| format!("{:.1}", x * 100.0)).unwrap_or("?".into());
    // What the trade expects: Polymarket climbing from the buy to the sale price over a typical
    // hold, then staying there until the give-up.
    let entry = v["entry"].as_f64();
    let give_end = give.unwrap_or(ts + 300_000);
    let mut expect = vec![Value::Null; n];
    if let (Some(e), Some(x)) = (entry, target) {
        let (a, b) = (idx(ts), idx(ts + hold.max(step)));
        for (i, slot) in expect.iter_mut().enumerate().take(idx(give_end) + 1).skip(a) {
            let f = if b > a { ((i - a) as f64 / (b - a) as f64).min(1.0) } else { 1.0 };
            *slot = json!(cents(Some(e + (x - e) * f)));
        }
    }
    let be = break_even(v);
    let title = format!("{} v {} · {} {} · {} (cents) · sells at {pays}¢, gives up {}", v["home"].as_str().unwrap_or(""), v["away"].as_str().unwrap_or(""), if yes { "Yes" } else { "No" }, outcome_name(&o), if v["real"].as_bool() == Some(true) { "REAL" } else { "paper" }, give.map(hhmm).unwrap_or_default());
    let line = |label: String, data: Value, colour: &str, width: f64| json!({"label": label, "data": data, "borderColor": colour, "backgroundColor": colour, "fill": false, "pointRadius": 0, "lineTension": 0, "borderWidth": width});
    let mark = |label: &str, data: Vec<Value>, colour: &str, shape: &str| json!({"label": label, "data": data, "borderColor": colour, "backgroundColor": colour, "showLine": false, "fill": false, "pointRadius": 8, "pointHoverRadius": 8, "pointStyle": shape});
    // One rule on this chart: solid = what happened; dotted and faded = the plan or a prediction;
    // the shaded part is still to come.
    let mut target_line = line(format!("Plan: cash out at {pays}¢"), json!(vec![cents(target); n]), "rgba(46,158,91,0.55)", 1.5);
    target_line["borderDash"] = json!([6, 4]);
    let mut expect_line = line("Prediction".into(), json!(expect), "rgba(46,158,91,0.6)", 2.5);
    expect_line["borderDash"] = json!([2, 4]);
    let mut be_line = line(format!("Plan: break-even {}¢", be.map(|x| format!("{:.1}", x * 100.0)).unwrap_or("?".into())), json!(vec![cents(be); n]), "rgba(110,110,110,0.55)", 1.0);
    be_line["borderDash"] = json!([2, 4]);
    let mut opts = chart_options(&title, "¢");
    // The give-up time: there it sells at whatever the price is (no stop-loss before it).
    let mut notes = vec![json!({"type": "line", "mode": "vertical", "scaleID": "x-axis-0", "value": labels[idx(give_end)].clone(), "borderColor": "rgba(214,69,69,0.7)", "borderWidth": 1.5, "borderDash": [6, 4],
        "label": {"enabled": true, "content": format!("Plan: gives up {} · sells at any price", hhmm(give_end)), "position": "top", "xAdjust": 120, "yAdjust": 6, "backgroundColor": "rgba(214,69,69,0.8)", "fontSize": 11}})];
    // Still open: everything after now is shaded and a solid line marks now.
    if v["closed_ms"].is_null() && now < t1 {
        let i = idx(now);
        notes.insert(0, json!({"type": "box", "xScaleID": "x-axis-0", "xMin": labels[i].clone(), "xMax": labels[n - 1].clone(), "backgroundColor": "rgba(0,0,0,0.045)", "borderWidth": 0}));
        notes.push(json!({"type": "line", "mode": "vertical", "scaleID": "x-axis-0", "value": labels[i].clone(), "borderColor": "#555", "borderWidth": 1.5,
            "label": {"enabled": true, "content": "Now", "position": "bottom", "yAdjust": -6, "backgroundColor": "rgba(60,60,60,0.85)", "fontSize": 11}}));
    }
    opts["annotation"] = json!({"annotations": notes});
    Some(json!({
        "type": "line",
        // Markers first: the first dataset is drawn on top.
        "data": {"labels": labels, "datasets": [
            mark("Buy", buy, "#2e9e5b", "triangle"),
            mark("Sell", sell, "#d64545", "rectRot"),
            mark("Bookmaker move", signal, "#e07b25", "circle"),
            line("Polymarket sell".into(), json!(bid), "#2f6fdf", 2.5),
            line("Polymarket buy".into(), json!(ask), "#a9bfee", 1.0),
            line(trig.clone(), json!(trig_line), "#e07b25", 2.0),
            line("All bookmakers".into(), json!(mid_line), "#8e44ad", 1.2),
            target_line,
            expect_line,
            be_line,
        ]},
        "options": opts,
    }))
}

/// "Account $X" for a chart: real = the Polymarket account now (cash + what open real snipe
/// trades cost); paper = the snipe bot's paper wallet (start + closed results).
async fn account_value(app: &App, real: bool) -> String {
    let db = app.db_path.clone();
    let open_real = {
        let db = db.clone();
        tokio::task::spawn_blocking(move || crate::store::open_ro(&db).map(|c| crate::bots::snipe_ledger(&c, crate::bots::SNIPE, true).1).unwrap_or(0.0)).await.unwrap_or(0.0)
    };
    if real {
        let pm = crate::pmacct::load(&db);
        match crate::exec::pm_cash(&client(), &pm).await {
            Ok(cash) => format!("Polymarket account ${:.2} (cash ${cash:.2} + open ${open_real:.2})", cash + open_real),
            Err(_) => "Polymarket account: not readable".into(),
        }
    } else {
        let (bal, open) = tokio::task::spawn_blocking(move || {
            let s = crate::bots::settings();
            crate::store::open_ro(&db).map(|c| { let l = crate::bots::snipe_ledger(&c, crate::bots::SNIPE, false); (s.snipe.c.start + l.0, l.1) }).unwrap_or((0.0, 0.0))
        }).await.unwrap_or((0.0, 0.0));
        format!("Paper wallet ${bal:.2} (${open:.2} in open trades)")
    }
}

/// The bot's own balance: during a real test run, its bankroll plus the run's results;
/// otherwise its real bankroll plus its real results; for a paper trade, the paper wallet.
fn bot_balance(app: &App, real: bool) -> String {
    wallet_line(app, real)
}

/// The bot's balance over time: starts at its money (the test run's bankroll, else its real bankroll;
/// paper: the paper start) and steps by each closed trade's result. (Chart.js config, caption)
fn balance_chart(app: &App, real: bool) -> Option<(Value, String)> {
    let st = crate::bots::settings();
    let c = crate::store::open_ro(&app.db_path).ok()?;
    let now = now_ms();
    // Real during / after a test run: the run's window and bankroll; else since the start.
    let last_run: Option<Value> = if real && st.run.is_none() { crate::bots::runs(app)["past"].get(0).cloned() } else { None };
    let (start_bal, from, to, label) = if real {
        if let Some(r) = &st.run {
            (r.bankroll, r.start_ms, now, format!("Snipe bot balance · REAL test run (${:.0})", r.bankroll))
        } else if let Some(r) = &last_run {
            (r["bankroll"].as_f64().unwrap_or(40.0), r["start_ms"].as_i64().unwrap_or(0), r["end_ms"].as_i64().unwrap_or(now), format!("Snipe bot balance · last REAL test run (${:.0})", r["bankroll"].as_f64().unwrap_or(40.0)))
        } else {
            (st.snipe.c.bankroll, st.snipe.c.real_since, now, format!("Snipe bot balance · REAL (${:.0} bankroll)", st.snipe.c.bankroll))
        }
    } else {
        (st.snipe.c.start, st.snipe.c.paper_since, now, format!("Snipe bot balance · paper (${:.0} start)", st.snipe.c.start))
    };
    let mut q = c.prepare("SELECT closed_ms, json_extract(data, '$.pnl') FROM bot_trades WHERE bot = 'snipe' AND real = ?1 AND closed_ms IS NOT NULL AND ts_ms >= ?2 AND closed_ms <= ?3 ORDER BY closed_ms").ok()?;
    let pts: Vec<(Ms, f64)> = q.query_map(params![real as i64, from, to], |r| Ok((r.get::<_, Ms>(0)?, r.get::<_, Option<f64>>(1)?.unwrap_or(0.0)))).ok()?.filter_map(|x| x.ok()).collect();
    let fmt_t = |ms: Ms| dmhm(ms);
    let mut labels = vec![fmt_t(from.max(pts.first().map(|p| p.0 - 60_000).unwrap_or(now)).min(now))];
    let mut vals = vec![(start_bal * 100.0).round() / 100.0];
    let mut bal = start_bal;
    for (t, p) in &pts {
        bal += p;
        labels.push(fmt_t(*t));
        vals.push((bal * 100.0).round() / 100.0);
    }
    labels.push("now".into());
    vals.push((bal * 100.0).round() / 100.0);
    // Real money in black, paper in blue, as the insights bot draws them.
    let colour = if real { "#111111" } else { "#2f6fdf" };
    let wins = pts.iter().filter(|p| p.1 > 0.0).count();
    let caption = format!("💼 <b>Bot balance ${bal:.2}</b> ({}{:.2} from ${start_bal:.0}) · {} trades, {wins} won", if bal >= start_bal { "+$" } else { "-$" }, (bal - start_bal).abs(), pts.len());
    Some((json!({
        "type": "line",
        "data": {"labels": labels, "datasets": [
            {"label": "Wallet ($)", "data": vals, "borderColor": colour, "backgroundColor": colour, "fill": false, "pointRadius": if labels.len() <= 31 { 2 } else { 0 }, "lineTension": 0, "borderWidth": 3},
            {"label": format!("start ${start_bal:.0}"), "data": vec![start_bal; labels.len()], "borderColor": "#d0d0d0", "backgroundColor": "#d0d0d0", "fill": false, "pointRadius": 0, "lineTension": 0, "borderWidth": 1},
        ]},
        "options": chart_options(&label, "$"),
    }), caption))
}

/// Put the account value into a chart's title and its caption.
fn with_value(mut chart: Value, caption: &str, value: &str) -> (Value, String) {
    let t = chart["options"]["title"]["text"].as_str().unwrap_or("").to_string();
    chart["options"]["title"]["text"] = json!([t, value]);
    (chart, format!("{caption}\n💼 {}", esc(value)))
}

/// Render a Chart.js config with QuickChart and send it as a photo with an HTML caption.
async fn send_chart(chart: &Value, caption: &str) -> Result<()> {
    let c = cfg();
    let chat = c.chat_id.ok_or_else(|| anyhow!("no Telegram chat linked yet"))?;
    let http = client();
    let r: Value = http
        .post("https://quickchart.io/chart/create")
        .json(&json!({"chart": chart, "width": 900, "height": 600, "devicePixelRatio": 2, "backgroundColor": "white", "format": "png"}))
        .send()
        .await?
        .json()
        .await?;
    let url = r["url"].as_str().ok_or_else(|| anyhow!("chart render failed"))?;
    let res = call(&http, &c.token, "sendPhoto", json!({"chat_id": chat, "photo": url, "caption": caption, "parse_mode": "HTML"})).await;
    let mut log = LOG.lock();
    log.push_front(Sent { ts_ms: now_ms(), kind: "bot", text: caption.to_string(), ok: res.is_ok(), error: res.as_ref().err().map(|e| format!("{e:#}")) });
    log.truncate(40);
    res.map(|_| ())
}

// ---------------------------------------------------------------- public view / edits

pub fn view(app: &App) -> Value {
    let c = cfg();
    let bot = BOT.lock().clone();
    let hint = if c.token.len() > 12 { format!("{}…{}", &c.token[..6], &c.token[c.token.len() - 4..]) } else { String::new() };
    let watch: Vec<Value> = c
        .odds
        .watch
        .iter()
        .map(|id| {
            let (h, a, s) = names(app, *id);
            json!({"id": id, "home": h, "away": a, "start_ms": s})
        })
        .collect();
    let user = bot.username.clone().unwrap_or_else(|| "swifteye_bot".into());
    json!({
        "token_set": !c.token.is_empty(),
        "token_hint": hint,
        "bot": bot,
        "bot_url": format!("https://t.me/{user}"),
        "link_url": format!("https://t.me/{user}?start={}", c.link_code),
        "handle": c.handle,
        "linked": c.chat_id.is_some(),
        "linked_as": c.linked_as,
        "enabled": c.enabled,
        "odds": c.odds,
        "arbs": c.arbs,
        "paper": c.paper,
        "suspicious": c.suspicious,
        "bot_alerts": c.bot_alerts,
        "tz": c.tz,
        "watch": watch,
        "log": LOG.lock().iter().cloned().collect::<Vec<_>>(),
    })
}

fn merge(dst: &mut Value, src: &Value) {
    match (dst, src) {
        (Value::Object(d), Value::Object(s)) => {
            for (k, v) in s {
                merge(d.entry(k.clone()).or_insert(Value::Null), v);
            }
        }
        (d, s) => *d = s.clone(),
    }
}

/// Apply a partial edit from the page. `token` only changes when a new one is typed;
/// `unlink: true` forgets the chat.
pub fn edit(e: &Value) -> Result<()> {
    let mut c = CFG.lock();
    let mut v = serde_json::to_value(&*c)?;
    let mut e = e.clone();
    if let Some(o) = e.as_object_mut() {
        for k in ["chat_id", "linked_as", "link_code"] {
            o.remove(k);
        }
        if o.get("token").and_then(|t| t.as_str()).is_none_or(|t| t.trim().is_empty() || t.contains('…')) {
            o.remove("token");
        }
        if let Some(h) = o.get_mut("handle") {
            if let Some(s) = h.as_str() {
                *h = json!(s.trim().trim_start_matches('@').to_string());
            }
        }
    }
    let unlink = e["unlink"].as_bool() == Some(true);
    merge(&mut v, &e);
    let mut n: TgCfg = serde_json::from_value(v)?;
    if n.token != c.token {
        *BOT.lock() = BotInfo::default();
    }
    if unlink {
        n.chat_id = None;
        n.linked_as = None;
        n.link_code = new_code();
    }
    n.odds.min_move_pts = n.odds.min_move_pts.clamp(0.1, 100.0);
    n.odds.games.retain(|k, _| known_game(k));
    for g in n.odds.games.values_mut() {
        g.min_move_pts = g.min_move_pts.map(|x| x.clamp(0.1, 100.0));
        g.arb_min_profit_pct = g.arb_min_profit_pct.map(|x| x.clamp(-5.0, 100.0));
        g.markets = g.markets.take().map(|m| m.into_iter().filter(|x| ["1x2", "total", "handicap", "btts"].contains(&x.as_str())).collect());
        g.sources = g.sources.take().filter(|s| ["all", "books", "predictions"].contains(&s.as_str()));
    }
    *c = n;
    save(&c);
    Ok(())
}

pub fn set_watch(id: i64, on: bool) -> bool {
    let mut c = CFG.lock();
    c.odds.watch.retain(|x| *x != id);
    if on {
        c.odds.watch.push(id);
    }
    save(&c);
    on
}

pub fn watched() -> Vec<i64> {
    CFG.lock().odds.watch.clone()
}

// ---------------------------------------------------------------- tasks

pub fn spawn(app: Arc<App>) {
    load(&app.db_path);
    tokio::spawn(bot_loop(app.clone()));
    tokio::spawn(updates_loop(app.clone()));
    tokio::spawn(odds_loop(app.clone()));
    tokio::spawn(scan_loop(app));
}

async fn updates_loop(app: Arc<App>) {
    let http = client();
    let mut offset: i64 = 0;
    let mut token_used = String::new();
    loop {
        let c = cfg();
        if c.token.is_empty() {
            tokio::time::sleep(Duration::from_secs(5)).await;
            continue;
        }
        if c.token != token_used || BOT.lock().checked_ms == 0 {
            token_used = c.token.clone();
            let info = match call(&http, &c.token, "getMe", json!({})).await {
                Ok(v) => BotInfo { username: v["username"].as_str().map(String::from), error: None, checked_ms: now_ms() },
                Err(e) => BotInfo { username: None, error: Some(format!("{e:#}")), checked_ms: now_ms() },
            };
            let bad = info.error.is_some();
            *BOT.lock() = info;
            if bad {
                tokio::time::sleep(Duration::from_secs(30)).await;
                continue;
            }
            let _ = call(&http, &c.token, "setMyCommands", json!({"commands": [
                {"command": "status", "description": "What is switched on"},
                {"command": "arbs", "description": "Best pre-match arbitrage right now"},
                {"command": "watching", "description": "Matches with alerts on"},
                {"command": "pause", "description": "Stop alerts"},
                {"command": "resume", "description": "Start alerts again"},
            ]})).await;
        }
        match call(&http, &c.token, "getUpdates", json!({"offset": offset, "timeout": 25, "allowed_updates": ["message"]})).await {
            Ok(v) => {
                for u in v.as_array().into_iter().flatten() {
                    offset = offset.max(u["update_id"].as_i64().unwrap_or(0) + 1);
                    if let Err(e) = on_message(&app, &http, &u["message"]).await {
                        tracing::warn!("telegram: {e:#}");
                    }
                }
            }
            Err(e) => {
                BOT.lock().error = Some(format!("{e:#}"));
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
        }
    }
}

async fn reply(http: &reqwest::Client, token: &str, chat: i64, text: &str) -> Result<()> {
    call(http, token, "sendMessage", json!({"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": true})).await.map(|_| ())
}

fn status_text(c: &TgCfg) -> String {
    let on = |b: bool| if b { "on" } else { "off" };
    let mut games: Vec<String> = c
        .odds
        .games
        .iter()
        .filter(|(_, g)| !g.enabled || !g.arb_enabled || g.min_move_pts.is_some() || g.markets.is_some() || g.sources.is_some() || g.cooldown_s.is_some() || g.arb_min_profit_pct.is_some())
        .map(|(k, g)| {
            let mut bits: Vec<String> = Vec::new();
            if !g.enabled {
                bits.push("odds off".into());
            } else {
                if let Some(x) = g.min_move_pts {
                    bits.push(format!("≥ {x:.1} pts"));
                }
                if let Some(m) = &g.markets {
                    bits.push(if m.is_empty() { "no markets".into() } else { m.iter().map(|m| market_name(m)).collect::<Vec<_>>().join("+") });
                }
            }
            if !g.arb_enabled {
                bits.push("arb off".into());
            } else if let Some(x) = g.arb_min_profit_pct {
                bits.push(format!("arb ≥ {x:.1}%"));
            }
            format!("{}: {}", k, if bits.is_empty() { "custom".into() } else { bits.join(", ") })
        })
        .collect();
    games.sort();
    let games = if games.is_empty() { String::new() } else { format!("\nPer game: {}", games.join("; ")) };
    format!(
        "<b>Swifteye alerts: {}</b>\nOdds moves: {} ({} matches, ≥ {:.1} pts, {})\nArbitrage: {} (≥ {:.1}%)\nPaper bot trades: {}\nSuspicious: {} (heavy CS2 betting {}, pre-match line moves {}){}",
        if c.enabled { "on" } else { "paused" },
        on(c.odds.enabled),
        if c.odds.watched_only { format!("{} watched", c.odds.watch.len()) } else { "all".into() },
        c.odds.min_move_pts,
        c.odds.markets.iter().map(|m| market_name(m)).collect::<Vec<_>>().join(", "),
        on(c.arbs.enabled),
        c.arbs.min_profit_pct,
        on(c.paper.enabled),
        on(c.suspicious.enabled),
        on(c.suspicious.activity),
        on(c.suspicious.lines),
        games,
    )
}

async fn on_message(app: &App, http: &reqwest::Client, m: &Value) -> Result<()> {
    let Some(chat) = m["chat"]["id"].as_i64() else { return Ok(()) };
    let text = m["text"].as_str().unwrap_or("").trim().to_string();
    let user = m["from"]["username"].as_str().unwrap_or("").to_string();
    let c = cfg();
    let (cmd, arg) = text.split_once(' ').map(|(a, b)| (a.to_string(), b.trim().to_string())).unwrap_or((text.clone(), String::new()));
    let cmd = cmd.split('@').next().unwrap_or("").to_lowercase();
    if cmd == "/start" {
        let by_code = !arg.is_empty() && arg == c.link_code;
        let by_handle = !c.handle.is_empty() && user.eq_ignore_ascii_case(&c.handle);
        if by_code || by_handle {
            {
                let mut g = CFG.lock();
                g.chat_id = Some(chat);
                g.linked_as = Some(if user.is_empty() { format!("chat {chat}") } else { user.clone() });
                save(&g);
            }
            return reply(http, &c.token, chat, &format!("Linked to Swifteye: snipe bot updates come here.\n\n{}\n\n{HELP}", bot_status(app))).await;
        }
        if c.chat_id == Some(chat) {
            return reply(http, &c.token, chat, &bot_status(app)).await;
        }
        return reply(http, &c.token, chat, "This bot belongs to a private Swifteye install. To link it, open the Telegram page in Swifteye and press <b>Open in Telegram</b>, or enter your handle there and send /start again.").await;
    }
    if c.chat_id != Some(chat) {
        return Ok(()); // not the linked chat: stay quiet
    }
    if cmd == "/balance" || cmd == "/balancepaper" {
        let real = cmd == "/balance";
        return match balance_chart(app, real) {
            Some((ch, cap)) if send_chart(&ch, &cap).await.is_ok() => Ok(()),
            Some((_, cap)) => reply(http, &c.token, chat, &cap).await,
            None => reply(http, &c.token, chat, "No balance history yet.").await,
        };
    }
    if cmd == "/account" {
        return reply(http, &c.token, chat, &account_text(app).await).await;
    }
    if let Some(t) = toggle(&cmd, &arg) {
        return reply(http, &c.token, chat, &t).await;
    }
    if cmd == "/chart" {
        // The last closed snipe trade, charted.
        let last: Option<Value> = crate::store::open_ro(&app.db_path).ok().and_then(|c| {
            c.query_row("SELECT data, real, closed_ms FROM bot_trades WHERE bot = 'snipe' AND closed_ms IS NOT NULL ORDER BY closed_ms DESC LIMIT 1", [], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?, r.get::<_, Ms>(2)?))
            })
            .ok()
            .and_then(|(d, real, cl)| {
                let mut v: Value = serde_json::from_str(&d).ok()?;
                v["real"] = json!(real != 0);
                v["closed_ms"] = json!(cl);
                Some(v)
            })
        });
        let Some(v) = last else { return reply(http, &c.token, chat, "No closed snipe trade yet.").await };
        let cap = format!("Last snipe trade: {} v {} · {}", esc(v["home"].as_str().unwrap_or("")), esc(v["away"].as_str().unwrap_or("")), money_s(v["pnl"].as_f64().unwrap_or(0.0)));
        let value = bot_balance(app, v["real"].as_bool() == Some(true));
        return match trade_chart(app, &v) {
            Some(ch) => {
                let (ch, cap2) = with_value(ch, &cap, &value);
                if send_chart(&ch, &cap2).await.is_ok() { Ok(()) } else { reply(http, &c.token, chat, &cap2).await }
            }
            None => reply(http, &c.token, chat, &format!("{cap}\n💼 {}\n(no chart: not enough recorded prices)", esc(&value))).await,
        };
    }
    let msg = match cmd.as_str() {
        "/extend" => match arg.trim().trim_end_matches(|c: char| c.is_ascii_alphabetic()).parse::<i64>().map_err(anyhow::Error::from).and_then(|h| crate::bots::extend_run(app, h)) {
            Ok(v) => format!("⏱ Run extended. It now ends {}.", hhmm(v["end_ms"].as_i64().unwrap_or(0))),
            Err(e) => format!("Use: /extend 2h (1 to 48 hours)\n{}", esc(&e.to_string())),
        },
        "/realon" => realon(app, &arg).await,
        "/stoprun" => match crate::bots::stop_run(app, "stopped from Telegram") {
            Ok(_) => "🏁 Real run stopped. Real money is off; open real positions are still sold. The summary follows.".into(),
            Err(_) => "No real run going.".into(),
        },
        "/realoff" => {
            let done = if crate::bots::settings().run.is_some() { crate::bots::stop_run(app, "stopped from Telegram").map(|_| ()) } else { crate::bots::set_real(app, crate::bots::SNIPE, false, "").map(|_| ()) };
            match done {
                Ok(()) => "📝 Real money is off. Open real positions are still sold; paper keeps trading.".into(),
                Err(e) => format!("Could not: {}", esc(&format!("{e:#}"))),
            }
        }
        "/kill" => match crate::bots::update(app, &json!({"kill": true})) {
            Ok(_) => "🛑 <b>Kill switch on</b>
No new trades, paper or real. Open ones are still sold. /unkill resumes.".into(),
            Err(e) => format!("Could not: {e:#}"),
        },
        "/unkill" => match crate::bots::update(app, &json!({"kill": false})) {
            Ok(_) => "▶️ Kill switch off. The bot trades again.".into(),
            Err(e) => format!("Could not: {e:#}"),
        },
        "/pause" => {
            CFG.lock().enabled = false;
            save(&cfg());
            "🔕 Messages paused. The bot keeps trading. /resume brings them back.".into()
        }
        "/resume" => {
            CFG.lock().enabled = true;
            save(&cfg());
            "🔔 Messages on.".into()
        }
        other => preview(app, &format!("{other} {arg}")).unwrap_or_else(|| HELP.into()),
    };
    reply(http, &c.token, chat, &msg).await
}


/// Real money from Telegram, in two steps so one stray message cannot start it: /realon shows
/// what would start; "/realon yes" within 2 minutes starts it. It starts a test run (the Bots
/// page's real amount and real stake, a new real wallet, the run's safety stops), never a bigger
/// amount than the settings hold. Only the linked chat reaches this, and it is not part of
/// `preview`, so nothing but a Telegram message from that chat can start real money here.
async fn realon(app: &App, arg: &str) -> String {
    let s = crate::bots::settings();
    let a = arg.trim().to_lowercase();
    let now = now_ms();
    let (bank, stake) = (s.snipe.c.bankroll, s.snipe.real_stake(s.snipe.c.bankroll));
    if a == "yes" {
        let Some((t, hours)) = REALON.lock().take() else { return "Send /realon first: it shows what will start.".into() };
        if now - t > 120_000 {
            return "That took more than 2 minutes. Send /realon again.".into();
        }
        let pm = crate::pmacct::load(&app.db_path);
        let cash = crate::exec::pm_cash(&crate::books::http_client(), &pm).await.ok();
        return match crate::bots::start_run(app, hours, bank, cash, "REAL MONEY") {
            Ok(_) => format!(
                "💵 <b>Real money is ON</b>\nNew wallet {}, about {} a trade, for {hours} hours.\nIt stops by itself at -{} for the day (open positions counted) and on the run's safety stops.\n\n/realoff stops real money\n/kill stops everything\n/safety shows the stops",
                dollars(bank), dollars(stake), dollars(s.snipe.c.daily_loss)
            ),
            Err(e) => format!("Not started: {}", esc(&format!("{e:#}"))),
        };
    }
    let hours = if a.is_empty() { 8 } else { a.trim_end_matches(|c: char| c.is_ascii_alphabetic()).trim().parse::<i64>().unwrap_or(0) };
    if ![4, 8, 9, 16, 24].contains(&hours) {
        return "Use /realon (8 hours) or /realon 4, 16 or 24.".into();
    }
    if s.run.is_some() || s.snipe.c.real {
        return "Real money is already on. /realoff stops it.".into();
    }
    if s.kill {
        return "The kill switch is on. /unkill first.".into();
    }
    if !crate::adaptive::rules_ready() {
        return "The bot is still loading its rules after a restart (up to 25 minutes). Try again when /rules answers.".into();
    }
    *REALON.lock() = Some((now, hours));
    let when = match s.snipe.hours() {
        Some([a, b]) => format!("trading {a:02}:00-{b:02}:00 UTC"),
        None => "trading at all hours".into(),
    };
    format!(
        "⚠️ <b>Switch REAL money on?</b>\nNew wallet {}, about {} a trade, for {hours} hours, {when}.\n\nIt stops by itself: at -{} for the day (open positions counted), after 3 losing trades in a row, if under half its trades win once there are 10, or if orders keep failing. /realon starts it again.\n\nSend <b>/realon yes</b> within 2 minutes to start.\n/realoff stops it at any time.",
        dollars(bank), dollars(stake), dollars(s.snipe.c.daily_loss)
    )
}

fn money_s(x: f64) -> String {
    format!("{}{}", if x < 0.0 { "-" } else { "+" }, dollars(x.abs()))
}

/// "24h", "3d", "1w", "90m" (default hours) -> days.
fn parse_span(s: &str) -> Option<f64> {
    let s = s.trim().to_lowercase();
    let (num, unit) = s.split_at(s.find(|c: char| c.is_ascii_alphabetic()).unwrap_or(s.len()));
    let n: f64 = num.trim().parse().ok()?;
    let d = match unit.trim() {
        "" | "h" | "hr" | "hrs" | "hour" | "hours" => n / 24.0,
        "d" | "day" | "days" => n,
        "w" | "wk" | "week" | "weeks" => n * 7.0,
        "m" | "min" | "mins" => n / 1440.0,
        _ => return None,
    };
    (d > 0.0 && d <= 365.0).then_some(d)
}

/// The bot's money now: the test run's bankroll ± results while one runs, else its real bankroll ±
/// real results.
fn bot_value(app: &App) -> f64 {
    let st = crate::bots::settings();
    if let Some(r) = &st.run {
        return r.bankroll + crate::bots::run_report(app, r, now_ms())["pnl"].as_f64().unwrap_or(0.0);
    }
    crate::store::open_ro(&app.db_path).map(|c| st.snipe.c.bankroll + crate::bots::snipe_ledger(&c, crate::bots::SNIPE, true).0).unwrap_or(st.snipe.c.bankroll)
}

/// /projected <time> [start]: what `start` turns into over `time` at the bot's rate, from the
/// whole-dataset replay of today's settings and, once there are 10+, from its own closed trades.
fn projected(app: &App, arg: &str) -> String {
    let mut it = arg.split_whitespace();
    let Some(days) = it.next().and_then(parse_span) else { return "Use: /projected 24h [start]  (also 72h, 3d, 1w; start defaults to the bot's balance)".into() };
    let start = it.next().and_then(|x| x.trim_start_matches('$').parse::<f64>().ok()).filter(|x| *x > 0.0).unwrap_or_else(|| bot_value(app));
    let span = if days >= 1.0 { format!("{days:.1} days") } else { format!("{:.0} h", days * 24.0) };
    let mut out = format!("<b>Projection</b> · ${start:.2} over {span}\n");
    // Return on the money per day, linear (no compounding) and compounded.
    let line = |label: &str, per_day: f64, n: &str| -> String {
        let lin = start * (1.0 + per_day * days);
        let comp = start * (1.0 + per_day).powf(days);
        let half = start * (1.0 + per_day * 0.5 * days);
        format!("{label} ({n}): {:+.1}%/day\n   → <b>${lin:.2}</b> simple · ${comp:.2} compounding · ${half:.2} at half the edge\n", per_day * 100.0)
    };
    let mut any = false;
    if let Some((x, at)) = crate::bots::sim_cached("snipe-wf") {
        let (s0, pnl, d) = (x["start"].as_f64().unwrap_or(0.0), x["pnl"].as_f64().unwrap_or(0.0), x["days"].as_f64().unwrap_or(0.0));
        if s0 > 0.0 && d > 0.0 {
            let when = format!("{} {}", dmhm(at), local(at).1);
            out.push_str(&line(&format!("Replay of all data (baseline from {when})"), pnl / s0 / d, &format!("{} trades over {d:.1} days", x["trades"])));
            any = true;
        }
    }
    // The bot's own closed trades (real if any, else paper): profit per day on its money.
    if let Ok(c) = crate::store::open_ro(&app.db_path) {
        for (real, money, label) in [(true, crate::bots::settings().snipe.c.bankroll, "Its real trades"), (false, crate::bots::settings().snipe.c.start, "Its paper trades")] {
            let r: Option<(i64, f64, Ms, Ms)> = c.query_row("SELECT COUNT(*), COALESCE(SUM(json_extract(data, '$.pnl')), 0), MIN(ts_ms), MAX(closed_ms) FROM bot_trades WHERE bot = 'snipe' AND real = ?1 AND closed_ms IS NOT NULL AND ts_ms >= ?2", params![real as i64, if real { 0 } else { crate::bots::settings().snipe.c.paper_since }], |r| Ok((r.get(0)?, r.get(1)?, r.get::<_, Option<Ms>>(2)?.unwrap_or(0), r.get::<_, Option<Ms>>(3)?.unwrap_or(0)))).ok();
            if let Some((n, pnl, a, b)) = r {
                let d = ((b - a) as f64 / 86_400_000.0).max(1.0 / 24.0);
                if n >= 10 && money > 0.0 {
                    out.push_str(&line(label, pnl / money / d, &format!("{n} trades over {:.1} h", d * 24.0)));
                    any = true;
                } else if n > 0 {
                    out.push_str(&format!("{label}: {n} closed so far ({}), too few to project (10+).\n", money_s(pnl)));
                }
            }
        }
    }
    if !any {
        out.push_str("No rate yet: no saved replay and fewer than 10 closed trades.\n");
    }
    out.push_str("<i>Estimates from past data, not a promise: few days of data, real fills can be worse, and a bad day stops at the daily loss limit.</i>");
    out
}

/// /log: every real-money order the bots sent (newest first).
fn real_log(app: &App, n: i64) -> String {
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return "Database not readable.".into() };
    let rows: Vec<(Ms, String, String, bool, Value)> = c
        .prepare("SELECT ts_ms, bot, venue, ok, data FROM bot_orders WHERE COALESCE(json_extract(data, '$.intent'), 0) = 0 ORDER BY id DESC LIMIT ?1")
        .and_then(|mut st| st.query_map([n], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get::<_, i64>(3)? != 0, serde_json::from_str(&r.get::<_, String>(4)?).unwrap_or(Value::Null)))).map(|it| it.filter_map(|x| x.ok()).collect()))
        .unwrap_or_default();
    if rows.is_empty() {
        return "No real-money orders yet.".into();
    }
    let mut out = format!("<b>Real-money orders</b> (last {})\n", rows.len());
    let mut net = 0.0;
    for (ts, bot, venue, ok, d) in &rows {
        let side = d["side"].as_str().unwrap_or("?");
        let f = &d["fill"];
        let (filled, avg) = (f["filled"].as_f64().unwrap_or(0.0), f["avg"].as_f64().unwrap_or(0.0));
        let usd = filled * avg;
        if *ok {
            net += if side.starts_with("buy") { -usd } else { usd };
        }
        let t = dmhm(*ts);
        let what = if *ok && filled > 0.0 {
            format!("{} {filled:.0} @ {:.1}¢ = ${usd:.2}", if side.starts_with("buy") { "BUY" } else { "SELL" }, avg * 100.0)
        } else {
            format!("{} failed: {}", side.to_uppercase(), esc(&d["error"].as_str().unwrap_or("nothing filled").chars().take(80).collect::<String>()))
        };
        out.push_str(&format!("{} {t} · {} · {} · {what}\n", if *ok { "✅" } else { "⚠️" }, esc(bot), esc(venue)));
    }
    out.push_str(&format!("Cash flow of these orders (sales − buys, before fees): {}", money_s(net)));
    out
}

fn bot_status(app: &App) -> String {
    let s = crate::bots::settings();
    let mode = if s.kill { "🛑 stopped (kill switch)" } else if !s.snipe.c.enabled { "switched off" } else if s.snipe.c.real { "💵 real and 📝 paper" } else { "📝 paper only" };
    let mut t = format!("🤖 <b>Snipe bot</b> · {mode}\n{}\n{}\n{}", wallet_block(app, true), wallet_block(app, false), window_text().lines().next().unwrap_or(""));
    if let Some(run) = &s.run {
        let rep = crate::bots::run_report(app, run, now_ms());
        t.push_str(&format!("\n⏱ Real run until {} · {}", hhmm(run.end_ms), money_s(rep["pnl"].as_f64().unwrap_or(0.0))));
    }
    t
}

/// A moment on the clock the messages use: Norway's by default (CET; CEST from the last Sunday
/// of March to the last Sunday of October, switching at 01:00 UTC), or UTC. (time, zone name)
fn local(ms: Ms) -> (time::OffsetDateTime, &'static str) {
    let utc = time::OffsetDateTime::from_unix_timestamp(ms.div_euclid(1000)).unwrap_or(time::OffsetDateTime::UNIX_EPOCH);
    if cfg().tz == "utc" {
        return (utc, "UTC");
    }
    let last_sunday = |m: time::Month| -> Option<time::OffsetDateTime> {
        let d = time::Date::from_calendar_date(utc.year(), m, 31).ok()?;
        let d = d - time::Duration::days(d.weekday().number_days_from_sunday() as i64);
        Some(d.with_hms(1, 0, 0).ok()?.assume_utc())
    };
    let summer = matches!((last_sunday(time::Month::March), last_sunday(time::Month::October)), (Some(a), Some(b)) if utc >= a && utc < b);
    let off = time::UtcOffset::from_hms(if summer { 2 } else { 1 }, 0, 0).unwrap_or(time::UtcOffset::UTC);
    (utc.to_offset(off), if summer { "CEST" } else { "CET" })
}

/// "14:05 CET"
fn hhmm(ms: Ms) -> String {
    let (d, z) = local(ms);
    format!("{:02}:{:02} {z}", d.hour(), d.minute())
}

/// "19/11 14:05" (no zone name: for lists and chart labels)
fn dmhm(ms: Ms) -> String {
    let (d, _) = local(ms);
    format!("{:02}/{:02} {:02}:{:02}", d.day(), u8::from(d.month()), d.hour(), d.minute())
}

fn trade_list(app: &App, open: bool, n: i64) -> String {
    let rows = snipe_rows(app, 0);
    let mut v: Vec<&Value> = rows.iter().filter(|v| v["closed_ms"].is_i64() != open).collect();
    if v.is_empty() {
        return if open { "⏳ Nothing open.".into() } else { "🧾 No closed trades yet.".into() };
    }
    v.sort_by_key(|x| std::cmp::Reverse(if open { x["ts"].as_i64() } else { x["closed_ms"].as_i64() }));
    let mut t = String::from(if open { "⏳ <b>Open trades</b>" } else { "🧾 <b>Last trades</b>" });
    for x in v.into_iter().take(n as usize) {
        if open {
            let (give, entry) = (pays_at(x).1, x["entry"].as_f64().unwrap_or(0.0));
            t.push_str(&format!("\n{} · {} · {} · {:.0} at {} ({}){}", tag(is_real(x)), game(x), bet(x), x["contracts"].as_f64().unwrap_or(0.0), cents(entry), dollars(x["cost"].as_f64().unwrap_or(0.0)), give.map(|g| format!(" · gives up {}", hhmm(g))).unwrap_or_default()));
        } else {
            t.push_str(&format!("\n{} {}", if x["pnl"].as_f64().unwrap_or(0.0) >= 0.0 { "✅" } else { "❌" }, sell_text(x, true)));
        }
    }
    t
}

fn run_text(app: &App) -> String {
    let g = crate::bots::runs(app);
    let (r, live) = match (&g["current"], g["past"].get(0)) {
        (Value::Object(_), _) => (g["current"].clone(), true),
        (_, Some(p)) => (p.clone(), false),
        _ => return "⏱ No real run yet. Start one on the Bots page.".into(),
    };
    let (bank, pnl) = (r["bankroll"].as_f64().unwrap_or(0.0), r["pnl"].as_f64().unwrap_or(0.0));
    let (closed, wins, open) = (r["closed"].as_u64().unwrap_or(0), r["wins"].as_u64().unwrap_or(0), r["open"].as_u64().unwrap_or(0));
    let mut t = format!(
        "⏱ <b>Real run</b> · {}\n{} to {}\n💵 <b>{}</b> · {closed} trades · {wins} won{}\n💼 {} → {}",
        if live { "running".to_string() } else { esc(r["ended"].as_str().unwrap_or("ended")) },
        dmhm(r["start_ms"].as_i64().unwrap_or(0)), hhmm(r["end_ms"].as_i64().unwrap_or(0)),
        money_s(pnl), if open > 0 { format!(" · {open} open") } else { String::new() }, dollars(bank), dollars(bank + pnl)
    );
    let sig: Vec<String> = r["signals"].as_array().map(|a| a.iter().take(3).map(|x| format!("{} {}", x["n"], sig_name(x["result"].as_str().unwrap_or("")).to_lowercase())).collect()).unwrap_or_default();
    if !sig.is_empty() {
        t.push_str(&format!("\n📡 {}", esc(&sig.join(" · "))));
    }
    if live {
        t.push_str("\n/safety shows the stops · /stoprun ends it");
    }
    t
}

// ---------------------------------------------------------------- snipe bot: message texts

fn tag(real: bool) -> &'static str {
    if real { "💵 Real" } else { "📝 Paper" }
}

/// "62¢" / "62.5¢"
fn cents(x: f64) -> String {
    let c = x * 100.0;
    if (c - c.round()).abs() < 0.05 { format!("{c:.0}¢") } else { format!("{c:.1}¢") }
}

/// "$1,084.20"
fn dollars(x: f64) -> String {
    let whole = x.abs().trunc() as i64;
    let frac = ((x.abs() - whole as f64) * 100.0).round() as i64;
    let (whole, frac) = if frac >= 100 { (whole + 1, 0) } else { (whole, frac) };
    let s = whole.to_string();
    let mut out = String::new();
    for (i, ch) in s.chars().enumerate() {
        if i > 0 && (s.len() - i) % 3 == 0 {
            out.push(',');
        }
        out.push(ch);
    }
    format!("{}${out}.{frac:02}", if x < 0.0 { "-" } else { "" })
}

/// "45 s" / "3 min" / "1 h 05 min"
fn held(ms: Ms) -> String {
    let s = (ms / 1000).max(0);
    if s < 90 {
        format!("{s} s")
    } else if s < 3600 {
        format!("{} min", (s + 30) / 60)
    } else {
        format!("{} h {:02} min", s / 3600, (s % 3600) / 60)
    }
}

fn game(v: &Value) -> String {
    format!("{} v {}", esc(v["home"].as_str().unwrap_or("?")), esc(v["away"].as_str().unwrap_or("?")))
}

/// What was bought, in words: "Home to win", "Against Home" (bought No), "Draw", "No draw".
fn bet(v: &Value) -> String {
    let yes = v["dir"].as_f64().unwrap_or(1.0) > 0.0;
    let team = match v["outcome"].as_str().unwrap_or("") {
        "home" => v["home"].as_str().unwrap_or("home"),
        "away" => v["away"].as_str().unwrap_or("away"),
        "draw" => return if yes { "Draw".into() } else { "No draw".into() },
        o => o,
    };
    if yes { format!("{} to win", esc(team)) } else { format!("Against {}", esc(team)) }
}

fn is_real(v: &Value) -> bool {
    v["real"].as_bool() == Some(true)
}

/// A buy. `compact`: one line (the paper wallet while a real run is going).
fn buy_text(v: &Value, compact: bool) -> String {
    let (n, entry, cost) = (v["contracts"].as_f64().unwrap_or(0.0), v["entry"].as_f64().unwrap_or(0.0), v["cost"].as_f64().unwrap_or(0.0));
    if compact {
        return format!("{} bought · {} · {} · {n:.0} at {} ({})", tag(is_real(v)), game(v), bet(v), cents(entry), dollars(cost));
    }
    let mut t = format!("🟢 <b>Bought</b> · {}\n<b>{}</b>\n{} · {n:.0} shares at {} · {}", tag(is_real(v)), game(v), bet(v), cents(entry), dollars(cost));
    let (target, give) = pays_at(v);
    if let Some(x) = target {
        t.push_str(&format!("\n🎯 Sells at {} ({})", cents(x), money_s(n * (x - entry))));
        if let Some(g) = give {
            t.push_str(&format!(" · gives up {}", hhmm(g)));
        }
    }
    if let Some(be) = break_even(v) {
        t.push_str(&format!("\n⚖️ Break-even {} · no stop-loss: at the give-up it sells at any price; most it can lose {}", cents(be), dollars(cost)));
    }
    let set = if v["set"] == "wider" { "Wider set" } else { "Safe set" };
    t.push_str(&format!("\n<i>{set} · {} feeds moved · expected gain {}</i>", v["books"][1].as_u64().unwrap_or(0), cents(v["edge"].as_f64().unwrap_or(0.0))));
    t
}

/// A sale. `compact`: one line.
fn sell_text(v: &Value, compact: bool) -> String {
    let pnl = v["pnl"].as_f64().unwrap_or(0.0);
    let (entry, exit) = (v["entry"].as_f64().unwrap_or(0.0), v["exit"].as_f64().unwrap_or(0.0));
    let why = esc(v["reason"].as_str().unwrap_or(""));
    if compact {
        return format!("{} sold <b>{}</b> · {} · {} → {} · {why}", tag(is_real(v)), money_s(pnl), game(v), cents(entry), cents(exit));
    }
    let took = v["exit_ts"].as_i64().zip(v["ts"].as_i64()).map(|(a, b)| format!(" · held {}", held(a - b))).unwrap_or_default();
    format!("{} <b>Sold {}</b> · {}\n<b>{}</b>\n{} · {} → {}{took} · {why}", if pnl >= 0.0 { "✅" } else { "❌" }, money_s(pnl), tag(is_real(v)), game(v), bet(v), cents(entry), cents(exit))
}

/// (closed result, open cost, today's result, closed trades, won, open positions, wallet money)
fn wallet(app: &App, real: bool) -> (f64, f64, f64, usize, usize, i64, f64) {
    let s = crate::bots::settings();
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return (0.0, 0.0, 0.0, 0, 0, 0, 0.0) };
    let (closed, open, day, n, w) = crate::bots::snipe_ledger(&c, crate::bots::SNIPE, real);
    let since = if real { s.snipe.c.real_since } else { s.snipe.c.paper_since };
    let open_n: i64 = c.query_row("SELECT COUNT(*) FROM bot_trades WHERE bot = 'snipe' AND real = ?1 AND closed_ms IS NULL AND ts_ms >= ?2", params![real as i64, since], |r| r.get(0)).unwrap_or(0);
    (closed, open, day, n, w, open_n, if real { s.snipe.c.bankroll } else { s.snipe.c.start })
}

/// "Real wallet $41.20 (+$1.20)"
fn wallet_line(app: &App, real: bool) -> String {
    let (closed, _, _, _, _, _, money) = wallet(app, real);
    format!("{} wallet {} ({})", if real { "Real" } else { "Paper" }, dollars(money + closed), money_s(closed))
}

/// Two lines for one wallet in an update.
fn wallet_block(app: &App, real: bool) -> String {
    let (closed, open, _, n, w, open_n, money) = wallet(app, real);
    let mut t = format!("{} <b>{}</b> ({})\n      {n} trades · {w} won", tag(real), dollars(money + closed), money_s(closed));
    if open_n > 0 {
        t.push_str(&format!(" · {open_n} open ({})", dollars(open)));
    }
    t
}

/// Every snipe trade of both wallets since `since` (each wallet no earlier than its own start),
/// oldest first, with "real" and "closed_ms" added.
fn snipe_rows(app: &App, since: Ms) -> Vec<Value> {
    let s = crate::bots::settings();
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return Vec::new() };
    let Ok(mut st) = c.prepare("SELECT data, real, closed_ms FROM bot_trades WHERE bot = 'snipe' AND ts_ms >= (CASE WHEN real = 1 THEN ?1 ELSE ?2 END) ORDER BY ts_ms") else { return Vec::new() };
    st.query_map(params![since.max(s.snipe.c.real_since), since.max(s.snipe.c.paper_since)], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?, r.get::<_, Option<Ms>>(2)?)))
        .map(|it| {
            it.filter_map(|x| x.ok())
                .filter_map(|(d, real, cl)| {
                    let mut v: Value = serde_json::from_str(&d).ok()?;
                    v["real"] = json!(real != 0);
                    v["closed_ms"] = json!(cl);
                    Some(v)
                })
                .collect()
        })
        .unwrap_or_default()
}

#[derive(Default, Clone, Copy)]
struct Tally {
    closed: usize,
    wins: usize,
    pnl: f64,
    staked: f64,
    open: usize,
    win_sum: f64,
    loss_sum: f64,
}

fn tally<'a>(rows: impl Iterator<Item = &'a Value>) -> Tally {
    let mut t = Tally::default();
    for v in rows {
        match v["pnl"].as_f64().filter(|_| v["closed_ms"].is_i64()) {
            Some(p) => {
                t.closed += 1;
                t.pnl += p;
                t.staked += v["cost"].as_f64().unwrap_or(0.0);
                if p > 0.0 {
                    t.wins += 1;
                    t.win_sum += p;
                } else {
                    t.loss_sum += p;
                }
            }
            None => t.open += 1,
        }
    }
    t
}

impl Tally {
    /// "+$3.20 · 5 trades · 4 won · 1 open"
    fn line(&self) -> String {
        if self.closed + self.open == 0 {
            return "no trades".into();
        }
        let mut t = format!("<b>{}</b> · {} trades · {} won", money_s(self.pnl), self.closed, self.wins);
        if self.open > 0 {
            t.push_str(&format!(" · {} open", self.open));
        }
        t
    }
    fn roi(&self) -> String {
        if self.staked > 0.0 { format!("{:+.1}% per $ staked", self.pnl / self.staked * 100.0) } else { "-".into() }
    }
}

/// Midnight (the messages' clock) of the day `ms` falls in.
fn day_start(ms: Ms) -> Ms {
    let (d, _) = local(ms);
    ms - ms.rem_euclid(1000) - ((d.hour() as Ms * 60 + d.minute() as Ms) * 60 + d.second() as Ms) * 1000
}

/// The trading window now: (open now, when it next opens or closes).
fn window_now(now: Ms) -> (bool, Option<Ms>) {
    let Some([a, b]) = crate::bots::settings().snipe.hours() else { return (true, None) };
    let day = now - now.rem_euclid(86_400_000);
    let h = |x: u8| day + x as Ms * 3_600_000;
    let inside = if a < b { now >= h(a) && now < h(b) } else { now >= h(a) || now < h(b) };
    let next = if inside {
        if a < b || now < h(b) { h(b) } else { h(b) + 86_400_000 }
    } else if now < h(a) {
        h(a)
    } else {
        h(a) + 86_400_000
    };
    (inside, Some(next))
}

fn window_text() -> String {
    let now = now_ms();
    match (crate::bots::settings().snipe.hours(), window_now(now)) {
        (Some([a, b]), (open, Some(next))) => {
            let day = now - now.rem_euclid(86_400_000);
            let span = format!("{} to {}", hhmm(day + a as Ms * 3_600_000), hhmm(day + b as Ms * 3_600_000));
            if open { format!("🕐 <b>Trading now</b> · closes {} ({} left)\nEvery day {span}", hhmm(next), held(next - now)) } else { format!("🕐 <b>Closed</b> · opens {} (in {})\nEvery day {span}", hhmm(next), held(next - now)) }
        }
        _ => "🕐 Trades all day.".into(),
    }
}

/// Signals the bot checked since `since`: (all, bought).
fn signals_since(app: &App, since: Ms) -> (i64, i64) {
    crate::store::open_ro(&app.db_path)
        .ok()
        .and_then(|c| c.query_row("SELECT COUNT(*), COALESCE(SUM(result = 'entered'), 0) FROM bot_signals WHERE ts_ms >= ?1", [since], |r| Ok((r.get(0)?, r.get(1)?))).ok())
        .unwrap_or((0, 0))
}

fn twins_line(f: &Value) -> Option<String> {
    let n = f["twin_pairs"].as_u64().unwrap_or(0);
    let gap = f["twin_gap"].as_f64()?;
    Some(format!("👥 Real vs paper twins: {n} pair{} · real {:+.1} pts", if n == 1 { "" } else { "s" }, gap * 100.0))
}

/// The update sent every hour of a real run (and the /now answer).
fn digest_text(app: &App, title: &str) -> String {
    let s = crate::bots::settings();
    let now = now_ms();
    let mut t = format!("📊 <b>{title}</b> · {}\n{}\n{}", hhmm(now), wallet_block(app, true), wallet_block(app, false));
    let (all, bought) = signals_since(app, now - 3_600_000);
    t.push_str(&format!("\n📡 Last hour: {all} moves checked · {bought} bought"));
    if let Some(f) = crate::bots::run_guard_facts(app) {
        if let Some(l) = twins_line(&f) {
            t.push_str(&format!("\n{l}"));
        }
        if let Some(r) = &s.run {
            t.push_str(&format!("\n⏱ Run ends {} · stops at −{} (now {})", hhmm(r.end_ms), dollars(f["loss_limit"].as_f64().unwrap_or(0.0)), money_s(f["pnl"].as_f64().unwrap_or(0.0))));
        }
    } else if s.snipe.c.real {
        t.push_str("\n💵 Real money is on (no end time).");
    }
    t
}

fn run_started_text(app: &App, r: &crate::bots::Run) -> String {
    let s = crate::bots::settings();
    let now = now_ms();
    let (open, next) = window_now(now);
    let when = match (open, next) {
        (true, Some(n)) => format!("Trading now until {}", hhmm(n.min(r.end_ms))),
        (false, Some(n)) => format!("Trades from {}", hhmm(n)),
        _ => "Trades all day".into(),
    };
    format!(
        "⏱ <b>Real run started</b>\n💵 Real wallet {} · up to {} a trade · stops at −{}\n📝 Paper wallet {} trades every move beside it\n🕐 {when} · ends {}\n\nAn update comes every hour. /safety shows the stops, /stoprun ends the run.",
        dollars(r.bankroll), dollars(s.snipe.real_stake(r.bankroll)), dollars(s.snipe.c.daily_loss), dollars(wallet(app, false).6 + wallet(app, false).0), hhmm(r.end_ms)
    )
}

/// The summary when a real run is over, from its stored report.
fn run_ended_text(app: &App, rep: &Value) -> String {
    let (start, to) = (rep["start_ms"].as_i64().unwrap_or(0), rep["to"].as_i64().unwrap_or_else(now_ms));
    let rows = snipe_rows(app, start);
    let in_run = |v: &&Value| v["ts"].as_i64().is_some_and(|t| t >= start && t <= to);
    let real = tally(rows.iter().filter(|v| is_real(v)).filter(in_run));
    let paper = tally(rows.iter().filter(|v| !is_real(v)).filter(in_run));
    let why = match rep["ended"].as_str().unwrap_or("ended") {
        "time up" => "time is up".to_string(),
        x => esc(x),
    };
    let mut t = format!("🏁 <b>Real run ended</b> · {why}\n💵 Real  {} ({})\n📝 Paper {} ({})", real.line(), real.roi(), paper.line(), paper.roi());
    let closed = |real_side: bool| -> Vec<Value> { rows.iter().filter(|v| is_real(v) == real_side && v["closed_ms"].is_i64()).filter(in_run).cloned().collect() };
    let gaps = crate::bots::twin_gaps(&closed(true), &closed(false));
    if !gaps.is_empty() {
        t.push_str(&format!("\n👥 Twins: {} pair{} · real {:+.1} pts against paper", gaps.len(), if gaps.len() == 1 { "" } else { "s" }, gaps.iter().sum::<f64>() / gaps.len() as f64 * 100.0));
    }
    if real.open > 0 {
        t.push_str(&format!("\n⏳ {} real position{} still open: sold as usual.", real.open, if real.open == 1 { "" } else { "s" }));
    }
    t.push_str(&format!("\n💼 {}", wallet_line(app, true)));
    t
}

// ---------------------------------------------------------------- snipe bot: command answers

fn wallet_text(app: &App, real: bool) -> String {
    let s = crate::bots::settings();
    let (closed, open, day, n, w, open_n, money) = wallet(app, real);
    let since = if real { s.snipe.c.real_since } else { s.snipe.c.paper_since };
    let mut t = format!("{} <b>{} wallet</b>\nBalance <b>{}</b> ({}{})\nToday {} · {n} trades · {w} won", if real { "💵" } else { "📝" }, if real { "Real" } else { "Paper" }, dollars(money + closed), money_s(closed), if since > 0 { format!(" since {}", dmhm(since)) } else { String::new() }, money_s(day));
    t.push_str(&if open_n > 0 { format!("\nOpen: {open_n} ({})", dollars(open)) } else { "\nNothing open".into() });
    if real {
        let free = crate::store::open_ro(&app.db_path).map(|c| crate::bots::real_free(&c, crate::bots::SNIPE)).unwrap_or(0.0);
        t.push_str(&format!("\nStake {:.0}% of the wallet, at most {} · loss stop {}\nFree to trade {}", s.snipe.r_pct * 100.0, dollars(s.snipe.r_max_trade), dollars(s.snipe.c.daily_loss), dollars(free)));
        t.push_str(&match (&s.run, s.snipe.c.real) {
            (Some(r), _) => format!("\n⏱ Real run on until {}", hhmm(r.end_ms)),
            (None, true) => "\nReal money is ON (no end time)".into(),
            (None, false) => "\nReal money is off".into(),
        });
    } else {
        t.push_str(&format!("\nStake {:.0}% of the balance, at most {} · wider set at most {}", s.snipe.pct * 100.0, dollars(s.snipe.c.max_trade), dollars(s.snipe.b_max_trade)));
    }
    t
}

fn today_text(app: &App) -> String {
    let rows = snipe_rows(app, day_start(now_ms()));
    let side = |real: bool, set: Option<&str>| tally(rows.iter().filter(|v| is_real(v) == real && set.is_none_or(|s| v["set"] == s)));
    let (r, p) = (side(true, None), side(false, None));
    let mut t = format!("📅 <b>Today</b>\n💵 Real  {}\n📝 Paper {}", r.line(), p.line());
    if p.closed + p.open > 0 {
        t.push_str(&format!("\n      safe set {} · wider set {}", money_s(side(false, Some("safe")).pnl), money_s(side(false, Some("wider")).pnl)));
    }
    let (all, _) = signals_since(app, day_start(now_ms()));
    t.push_str(&format!("\n📡 {all} moves checked\n{}", window_text().lines().next().unwrap_or("")));
    t
}

fn week_text(app: &App) -> String {
    let now = now_ms();
    let first = day_start(now) - 6 * 86_400_000;
    let rows = snipe_rows(app, first);
    let mut t = String::from("🗓 <b>Last 7 days</b>");
    let (mut tr, mut tp) = (Tally::default(), Tally::default());
    for k in 0..7 {
        let (a, b) = (first + k * 86_400_000, first + (k + 1) * 86_400_000);
        let of = |real: bool| tally(rows.iter().filter(|v| is_real(v) == real && v["closed_ms"].as_i64().is_some_and(|c| c >= a && c < b)));
        let (r, p) = (of(true), of(false));
        if r.closed + p.closed == 0 {
            continue;
        }
        tr.pnl += r.pnl;
        tr.closed += r.closed;
        tp.pnl += p.pnl;
        tp.closed += p.closed;
        let (d, _) = local(a + 43_200_000);
        t.push_str(&format!("\n{:02}/{:02}  💵 {} ({})  📝 {} ({})", d.day(), u8::from(d.month()), money_s(r.pnl), r.closed, money_s(p.pnl), p.closed));
    }
    if tr.closed + tp.closed == 0 {
        return "🗓 No closed trades in the last 7 days.".into();
    }
    t.push_str(&format!("\n<b>Total</b>  💵 {} ({})  📝 {} ({})", money_s(tr.pnl), tr.closed, money_s(tp.pnl), tp.closed));
    t
}

/// A signal result in plain words.
fn sig_name(k: &str) -> String {
    match k {
        "entered" => "Bought",
        "move_too_small" => "Move too small",
        "outside_hours" => "Outside trading hours",
        "too_few_books" | "too_few_feeds" => "Only one feed moved",
        "consensus_did_not_move" => "The market as a whole did not move",
        "polymarket_does_not_lag" => "Polymarket is not behind in this game",
        "already_moved" => "Polymarket already moved",
        "no_market_price" => "No Polymarket price",
        "no_book_price" => "No bookmaker price",
        "no_consensus" => "Too few bookmakers priced it",
        "data_gap" => "Gap in the recorded prices",
        "book_reverted" => "Bookmaker moved back",
        "basketball" => "Live basketball (skipped)",
        "price_too_high" => "Price above 90¢",
        "gap_too_small" => "Expected gain too small",
        "spread_too_wide" => "Spread too wide",
        "price_ran" => "Price moved before the buy",
        "no_cash" => "No free money for it",
        "too_thin" => "Too little for sale",
        "gone_at_match" => "Offer gone when the order matched",
        "real_not_filled" => "Real order did not fill",
        "real_order_failed" => "Real order failed",
        "polymarket_already_apart" => "Polymarket already far from the bookmakers",
        "measured_too_late" => "Decision came too late (Mac busy)",
        "moved_with_first_book" => "Polymarket moved with the first bookmaker",
        "same_move" => "Same move, already taken",
        "already_in_game" => "Real money already in this game",
        "feed_quiet" => "Polymarket price feed went quiet",
        "wider_set_daily_stop" => "Wider set stopped for today",
        "after_pause" => "Right after a pause in play",
        "price_out_of_range" => "Price too close to 0 or 100",
        "price_above_set_limit" => "Price above the wider set's 40¢",
        "weak_feed" => "Triggered by a weak feed",
        "no_live_book" => "No live order book",
        "no_offer" => "Nothing for sale",
        "no_account_cash" => "No cash on the account",
        "swingy_game" => "Game swings too much",
        "gap_usual_size" => "Gap is its usual size",
        _ => return k.replace('_', " "),
    }
    .to_string()
}

fn signals_text(app: &App) -> String {
    let since = day_start(now_ms());
    let rows: Vec<(String, i64)> = crate::store::open_ro(&app.db_path)
        .ok()
        .and_then(|c| {
            let mut st = c.prepare("SELECT result, COUNT(*) FROM bot_signals WHERE ts_ms >= ?1 GROUP BY 1 ORDER BY 2 DESC").ok()?;
            let it = st.query_map([since], |r| Ok((r.get(0)?, r.get(1)?))).ok()?;
            Some(it.filter_map(|x| x.ok()).collect())
        })
        .unwrap_or_default();
    if rows.is_empty() {
        return "📡 No moves checked today yet.".into();
    }
    let total: i64 = rows.iter().map(|x| x.1).sum();
    let mut t = format!("📡 <b>Signals today</b> · {total} moves checked");
    for (k, n) in rows.iter().take(12) {
        t.push_str(&format!("\n{} {n} · {}", if k == "entered" { "🟢" } else { "▫️" }, sig_name(k)));
    }
    t
}

fn safety_text(app: &App) -> String {
    let Some(f) = crate::bots::run_guard_facts(app) else {
        let s = crate::bots::settings();
        return format!("🛡 No real run going, so nothing to stop.\nA run stops itself after −{} on the day (open positions counted), 3 losses in a row, under half its trades winning after 10, refused orders, or real fills clearly worse than paper.", dollars(s.snipe.c.daily_loss));
    };
    let l = &f["limits"];
    let ok = |bad: bool| if bad { "⚠️" } else { "✅" };
    let (closed, wins) = (f["closed"].as_u64().unwrap_or(0), f["wins"].as_u64().unwrap_or(0));
    let (loss, limit) = (f["loss"].as_f64().unwrap_or(0.0), f["loss_limit"].as_f64().unwrap_or(0.0));
    let in_row = f["losers_in_row"].as_u64().unwrap_or(0);
    let row_max = l["losers_in_row"].as_u64().unwrap_or(3);
    let worst = f["worst_return"].as_f64().unwrap_or(0.0);
    let mut t = format!("🛡 <b>Safety stops</b> · real run\n{} Lost {} of {}\n{} Losses in a row: {in_row} of {row_max}\n{} Winners: {wins} of {closed} (needs {:.0}% once there are {})\n▫️ Worst trade {:.0}% (no stop by itself)\n{} Refused orders: {} of {}\n{} Buys that found nothing: {} (stops at {} if most)",
        ok(loss >= limit * 0.5), dollars(loss), dollars(limit),
        ok(in_row + 1 >= row_max),
        ok(closed >= 6 && (wins as f64) < 0.6 * closed as f64), l["win_share"].as_f64().unwrap_or(0.0) * 100.0, l["win_min_trades"],
        worst * 100.0,
        ok(f["refused"].as_u64().unwrap_or(0) >= 1), f["refused"], l["refused"],
        ok(f["unfilled_buys"].as_u64().unwrap_or(0) >= 3), f["unfilled_buys"], l["unfilled_buys"]);
    t.push_str(&match f["twin_gap"].as_f64() {
        Some(g) => format!("\n{} Real vs paper twins: {:+.1} pts over {} (stops at −{:.0})", ok(g <= -0.05), g * 100.0, f["twin_pairs"], l["twin_gap"].as_f64().unwrap_or(0.0) * 100.0),
        None => "\n✅ Real vs paper twins: no pairs yet".into(),
    });
    if let Some(w) = f["would_stop"].as_str() {
        t.push_str(&format!("\n🛑 Stopping now: {}", esc(w)));
    }
    t
}

fn settings_text() -> String {
    let s = crate::bots::settings();
    let sn = &s.snipe;
    format!(
        "⚙️ <b>Settings</b>\n💵 Real wallet {} · stake {:.0}%, at most {} · loss stop {}\n📝 Paper balance {} · stake {:.0}%, at most {} · most open {}\n📝 Wider set {} · stake {:.0}%, at most {} · daily stop {}\n⏳ Gives up after {} min · size by edge {}\n{}\n{}",
        dollars(sn.c.bankroll), sn.r_pct * 100.0, dollars(sn.r_max_trade), dollars(sn.c.daily_loss),
        dollars(sn.c.start), sn.pct * 100.0, dollars(sn.c.max_trade), dollars(sn.c.max_open),
        if sn.b_enabled { "on" } else { "off" }, sn.b_pct * 100.0, dollars(sn.b_max_trade), dollars(sn.b_daily_loss),
        sn.give_up_s / 60, if sn.edge_sizing { "on" } else { "off" },
        window_text().lines().last().unwrap_or(""),
        if s.kill { "🛑 Kill switch ON" } else if !sn.c.enabled { "Bot switched off" } else if sn.c.real { "💵 Real money ON" } else { "📝 Paper only" }
    )
}

fn rules_text() -> String {
    let r = crate::adaptive::active_rules();
    let b = crate::adaptive::policy_b();
    let (fs, fw) = (crate::adaptive::Flt::live(), crate::adaptive::Flt::wider());
    format!(
        "📐 <b>When it buys</b>\nA bookmaker moves and Polymarket has not followed yet.\n\n<b>Safe set</b> (real and paper)\n• {}+ independent feeds moved {:.0}+ points\n• Polymarket made at most {:.0}% of the move\n• Polymarket followed {:.0}%+ of earlier jumps in this game\n• expected gain {}+ · spread up to {}\n• price up to 90¢ · no live basketball\n\n<b>Wider set</b> (paper only)\n• {}+ feeds moved {:.0}+ points · followed {:.0}%+ of earlier jumps\n• expected gain {}+ · price up to 40¢\n\n<b>When it sells</b>\nWhen Polymarket has caught up, or after {} min.",
        r.min_books.max(fs.fams.unwrap_or(1)), r.min_move * 100.0, r.max_followed.min(crate::adaptive::SAFE_MAX_FOLLOWED) * 100.0, fs.follow.unwrap_or(0.0) * 100.0, cents(r.min_edge), cents(r.max_spread),
        b.min_books.max(fw.fams.unwrap_or(1)), b.min_move * 100.0, fw.follow.unwrap_or(0.0) * 100.0, cents(b.min_edge),
        (if crate::bots::settings().snipe.give_up_s > 0 { crate::bots::settings().snipe.give_up_s } else { r.give_up_s }) / 60
    )
}

fn stats_text(app: &App) -> String {
    let rows = snipe_rows(app, 0);
    let mut t = String::from("📈 <b>Results so far</b> (each wallet since its start)");
    for real in [true, false] {
        let x = tally(rows.iter().filter(|v| is_real(v) == real));
        if x.closed == 0 {
            t.push_str(&format!("\n{} no closed trades", tag(real)));
            continue;
        }
        let losses = x.closed - x.wins;
        t.push_str(&format!(
            "\n{} {}\n      {:.0}% won · {}\n      average win {} · average loss {}",
            tag(real), x.line(), x.wins as f64 / x.closed as f64 * 100.0, x.roi(),
            if x.wins > 0 { money_s(x.win_sum / x.wins as f64) } else { "-".into() }, if losses > 0 { money_s(x.loss_sum / losses as f64) } else { "-".into() }
        ));
    }
    t
}

fn sports_text(app: &App) -> String {
    let rows = snipe_rows(app, 0);
    let mut by: std::collections::BTreeMap<String, (Tally, Tally)> = Default::default();
    for v in rows.iter().filter(|v| v["closed_ms"].is_i64()) {
        let e = by.entry(v["sport"].as_str().unwrap_or("other").to_string()).or_default();
        let x = if is_real(v) { &mut e.0 } else { &mut e.1 };
        x.closed += 1;
        x.pnl += v["pnl"].as_f64().unwrap_or(0.0);
    }
    if by.is_empty() {
        return "🏟 No closed trades yet.".into();
    }
    let mut v: Vec<_> = by.into_iter().collect();
    v.sort_by(|a, b| (b.1 .0.pnl + b.1 .1.pnl).partial_cmp(&(a.1 .0.pnl + a.1 .1.pnl)).unwrap_or(std::cmp::Ordering::Equal));
    let mut t = String::from("🏟 <b>By sport</b>");
    for (s, (r, p)) in v {
        t.push_str(&format!("\n{}  💵 {} ({})  📝 {} ({})", esc(&s), money_s(r.pnl), r.closed, money_s(p.pnl), p.closed));
    }
    t
}

fn extremes_text(app: &App, best: bool) -> String {
    let mut rows: Vec<Value> = snipe_rows(app, 0).into_iter().filter(|v| v["closed_ms"].is_i64()).collect();
    if rows.is_empty() {
        return "No closed trades yet.".into();
    }
    rows.sort_by(|a, b| a["pnl"].as_f64().unwrap_or(0.0).partial_cmp(&b["pnl"].as_f64().unwrap_or(0.0)).unwrap_or(std::cmp::Ordering::Equal));
    if best {
        rows.reverse();
    }
    let mut t = format!("{} <b>{} trades</b>", if best { "🏆" } else { "🥶" }, if best { "Best" } else { "Worst" });
    for v in rows.iter().take(5) {
        t.push_str(&format!("\n{}", sell_text(v, true)));
    }
    t
}

fn last_text(app: &App) -> String {
    let rows = snipe_rows(app, 0);
    match rows.iter().filter(|v| v["closed_ms"].is_i64()).max_by_key(|v| v["closed_ms"].as_i64()) {
        Some(v) => format!("{}\n💼 {}", sell_text(v, false), wallet_line(app, is_real(v))),
        None => "No closed trades yet.".into(),
    }
}

fn twins_text(app: &App) -> String {
    let since = crate::bots::settings().run.map(|r| r.start_ms).unwrap_or_else(|| day_start(now_ms()));
    let rows = snipe_rows(app, since);
    let ret = |t: &Value| t["pnl"].as_f64().unwrap_or(0.0) / t["cost"].as_f64().unwrap_or(0.0).max(1e-9);
    let mut t = String::from("👥 <b>Real against its paper twins</b>");
    let mut gaps = Vec::new();
    for r in rows.iter().filter(|v| is_real(v) && v["closed_ms"].is_i64()) {
        let Some(p) = rows.iter().find(|p| !is_real(p) && p["closed_ms"].is_i64() && p["match_id"] == r["match_id"] && p["set"] == r["set"] && (p["ts"].as_i64().unwrap_or(0) - r["ts"].as_i64().unwrap_or(0)).abs() <= 30_000) else { continue };
        gaps.push(ret(r) - ret(p));
        t.push_str(&format!(
            "\n{}\n      buy {} / {} · sale {} / {} · return {:+.1}% / {:+.1}%",
            game(r), cents(r["entry"].as_f64().unwrap_or(0.0)), cents(p["entry"].as_f64().unwrap_or(0.0)), cents(r["exit"].as_f64().unwrap_or(0.0)), cents(p["exit"].as_f64().unwrap_or(0.0)), ret(r) * 100.0, ret(p) * 100.0
        ));
    }
    if gaps.is_empty() {
        return "👥 No real trade with a closed paper twin yet.".into();
    }
    t.push_str(&format!("\n<b>Average</b>: real {:+.1} pts against paper over {} pair{} (real / paper above)", gaps.iter().sum::<f64>() / gaps.len() as f64 * 100.0, gaps.len(), if gaps.len() == 1 { "" } else { "s" }));
    t
}

/// Live trades against the replay over the last 24 hours (both wallets).
fn compare_text(app: &App) -> String {
    let s = crate::bots::settings();
    let cfg = s.run.as_ref().map(|r| r.prev.clone()).unwrap_or(s.snipe.clone());
    let mut t = String::from("🔬 <b>Live against the replay</b> · last 24 h");
    for real in [true, false] {
        let p = crate::adaptive::parity(&app.db_path, &cfg, real, now_ms() - 86_400_000);
        if p["building"] == true {
            return "🔬 The replay is still loading after a restart. Try again in a few minutes.".into();
        }
        let x = &p["summary"];
        t.push_str(&format!("\n{} {} live · {} replay · {} the same trade", tag(real), x["live_trades"], x["replay_trades"], x["both"]));
        if x["both"].as_u64().unwrap_or(0) > 0 {
            let c = |k: &str| x[k].as_f64().map(|v| format!("{:+.1}¢", v * 100.0)).unwrap_or("-".into());
            t.push_str(&format!("\n      live minus replay: buy {} · sale {} · return {}", c("entry_diff"), c("exit_diff"), x["return_diff"].as_f64().map(|v| format!("{:+.1} pts", v * 100.0)).unwrap_or("-".into())));
        }
    }
    t.push_str("\n<i>The same trade on both sides, at close prices, means the replay can be trusted.</i>");
    t
}

fn health_text(app: &App) -> String {
    let now = now_ms();
    let quiet = now - crate::venue::alive_ms(crate::venue::V::Pm);
    let c = crate::store::open_ro(&app.db_path).ok();
    let q = |sql: &str| -> Option<Ms> { c.as_ref()?.query_row(sql, [], |r| r.get::<_, Option<Ms>>(0)).ok().flatten() };
    let last_quote = q("SELECT MAX(ts_ms) FROM pm_quotes WHERE id > (SELECT MAX(id) - 500 FROM pm_quotes)");
    let last_book = q("SELECT MAX(ts_ms) FROM changes WHERE id > (SELECT MAX(id) - 500 FROM changes)");
    let ad = crate::adaptive::view(app);
    let ago = |t: Option<Ms>| t.map(|t| format!("{} ago", held(now - t))).unwrap_or("never".into());
    let ok = |good: bool| if good { "✅" } else { "⚠️" };
    let s = crate::bots::settings();
    format!(
        "🩺 <b>Health</b>\n{} Polymarket feed: last message {} ago\n{} Polymarket prices saved {}\n{} Bookmaker prices saved {}\n{} Replay: {}\n{} Bot: {}\n{}",
        ok(quiet < 15_000), held(quiet),
        ok(last_quote.is_some_and(|t| now - t < 60_000)), ago(last_quote),
        ok(last_book.is_some_and(|t| now - t < 60_000)), ago(last_book),
        ok(ad["status"] == "ready"), esc(ad["status"].as_str().filter(|x| !x.is_empty()).unwrap_or("loading")),
        ok(!s.kill && s.snipe.c.enabled), if s.kill { "kill switch on" } else if !s.snipe.c.enabled { "switched off" } else if s.snipe.c.real { "trading real money" } else { "trading paper" },
        window_text().lines().next().unwrap_or("")
    )
}

fn timing_text() -> String {
    let t = crate::timing::get();
    format!(
        "⏲ <b>Order timing</b> (measured on {} real orders)\nSigning {} ms · reply {} ms\nMatching delay on live games {:.2} s\nOrder leaves {:.1} s after the bookmaker move\nShares can be sold {:.1} s after a buy",
        t.n_pm, t.pm_prep_ms, t.pm_order_ms, t.pm_live_delay_ms as f64 / 1000.0, (1_500 + t.pm_send_lag_ms) as f64 / 1000.0, t.pm_settle_ms as f64 / 1000.0
    )
}

fn replay_text() -> String {
    let Some((x, at)) = crate::bots::sim_cached("snipe-wf").or_else(|| crate::bots::sim_cached("snipe-plan")) else { return "🧪 No replay computed yet. Open the Replay tab on the Bots page once.".into() };
    let n = x["trades"].as_f64().unwrap_or(0.0);
    format!(
        "🧪 <b>Replay of this setup</b> · {:.1} days of data\n<b>{}</b> on {} · {} trades · {:.0}% won\n{:+.1}% per $ staked · worst drop {}\n<i>Computed {}. Past data, picked rules: an upper bound, not a forecast.</i>",
        x["days"].as_f64().unwrap_or(0.0), money_s(x["pnl"].as_f64().unwrap_or(0.0)), dollars(x["start"].as_f64().unwrap_or(0.0)), x["trades"],
        if n > 0.0 { x["wins"].as_f64().unwrap_or(0.0) / n * 100.0 } else { 0.0 }, x["roi"].as_f64().unwrap_or(0.0) * 100.0, dollars(x["max_drawdown"].as_f64().unwrap_or(0.0)), dmhm(at)
    )
}

async fn account_text(app: &App) -> String {
    let s = crate::bots::settings();
    let pm = crate::pmacct::load(&app.db_path);
    let cash = crate::exec::pm_cash(&crate::books::http_client(), &pm).await;
    let (closed, open, ..) = wallet(app, true);
    match cash {
        Ok(c) => format!(
            "🏦 <b>Polymarket account</b>\nCash {}\nBot's real wallet {} ({} in open trades)\nReserve never touched {}\nThe real wallet can be at most {}",
            dollars(c), dollars(s.snipe.c.bankroll + closed), dollars(open), dollars(s.pm_reserve), dollars((c - s.pm_reserve).max(0.0))
        ),
        Err(e) => format!("🏦 Could not read the Polymarket balance: {}", esc(&format!("{e:#}").chars().take(160).collect::<String>())),
    }
}

fn alerts_text() -> String {
    let b = cfg().bot_alerts;
    let on = |x: bool| if x { "on" } else { "off" };
    format!("🔔 <b>Messages</b>\nBuys {} · sales {} · charts {} · run updates {}\nChange with /buys, /sells, /charts, /updates followed by on or off.", on(b.buys), on(b.sells), on(b.charts), on(b.run_updates))
}

/// "/buys off" and the like: Some(answer) when the command is one of the switches.
fn toggle(cmd: &str, arg: &str) -> Option<String> {
    let on = match arg.trim().to_lowercase().as_str() {
        "on" | "1" | "yes" => true,
        "off" | "0" | "no" => false,
        _ => return matches!(cmd, "/buys" | "/sells" | "/charts" | "/updates").then(|| format!("Use: {cmd} on  or  {cmd} off\n\n{}", alerts_text())),
    };
    {
        let mut g = CFG.lock();
        match cmd {
            "/buys" => g.bot_alerts.buys = on,
            "/sells" => g.bot_alerts.sells = on,
            "/charts" => g.bot_alerts.charts = on,
            "/updates" => g.bot_alerts.run_updates = on,
            _ => return None,
        }
        save(&g);
    }
    Some(alerts_text())
}

const HELP: &str = "<b>Swifteye snipe bot</b>\n\n<b>Now</b>\n/status · /now · /open · /today · /hours\n\n<b>Wallets</b>\n/real · /paper · /account · /balance · /balancepaper\n\n<b>Results</b>\n/trades · /last · /chart · /best · /worst · /week · /stats · /sports\n\n<b>Real run</b>\n/run · /safety · /twins · /compare · /extend 2h · /realon · /stoprun · /realoff\n\n<b>How it works</b>\n/rules · /settings · /signals · /replay · /projected 24h · /timing · /log · /health\n\n<b>Controls</b>\n/kill · /unkill · /pause · /resume · /alerts\n/buys, /sells, /charts, /updates on|off\n\n<i>/realon starts real money (it asks you to confirm). Raising its amount is only on the Bots page.</i>";


fn arb_text(a: &crate::arb::Arb) -> String {
    let legs = a
        .legs
        .iter()
        .map(|l| {
            let what = match l.side {
                "no" => format!("No on {}", outcome_name(l.against.unwrap_or(l.outcome))),
                "yes" => format!("Yes {}", outcome_name(l.outcome)),
                _ => outcome_name(l.outcome).to_string(),
            };
            format!("  {what} @ {:.2} on {} — stake ${:.2}", l.odds, esc(&l.book), l.stake)
        })
        .collect::<Vec<_>>()
        .join("\n");
    let line = a.line.map(|l| format!(" {l}")).unwrap_or_default();
    format!(
        "<b>Arbitrage +{:.2}%</b> · {} v {}\n{} · {}{} · kick-off {}\n{}{}{}",
        a.profit * 100.0,
        esc(&a.home),
        esc(&a.away),
        esc(&a.league),
        market_name(a.market),
        line,
        fmt_kick(a.start_ms),
        legs,
        crate::arbtrack::timing_text(a).map(|t| format!("\n⏱ {t}")).unwrap_or_default(),
        if a.warn.is_empty() { String::new() } else { format!("\n<i>{}</i>", a.warn.join(". ")) }
    )
}

/// Odds moves on watched matches, batched into one message every few seconds.
async fn odds_loop(app: Arc<App>) {
    let mut rx = app.live.subscribe();
    let mut last: HashMap<(i64, &'static str, &'static str, i64, &'static str), Ms> = HashMap::new();
    let mut buf: Vec<String> = Vec::new();
    let mut flush_at: Option<tokio::time::Instant> = None;
    loop {
        let wait = flush_at.map(|t| t.saturating_duration_since(tokio::time::Instant::now())).unwrap_or(Duration::from_secs(3600));
        tokio::select! {
            r = rx.recv() => {
                let ch = match r {
                    Ok(c) => c,
                    Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                    Err(_) => return,
                };
                let c = cfg();
                if !c.enabled || !c.odds.enabled || c.chat_id.is_none() || ch.kind != ChangeKind::Move {
                    continue;
                }
                if c.odds.watched_only && !c.odds.watch.contains(&ch.match_id) {
                    continue;
                }
                let (Some(o), Some(n)) = (ch.old_price.filter(|p| *p > 1.0), ch.new_price.filter(|p| *p > 1.0)) else { continue };
                let d = 1.0 / n - 1.0 / o;
                // Cheap cut before the match lookup: smaller than every game's threshold.
                if d.abs() * 100.0 < c.odds.move_floor() {
                    continue;
                }
                let sport = sport_of(&app, ch.match_id);
                let g = c.odds.game(&sport);
                if g.is_some_and(|g| !g.enabled) {
                    continue;
                }
                let markets = g.and_then(|g| g.markets.as_ref()).unwrap_or(&c.odds.markets);
                if !markets.iter().any(|m| m == ch.market.as_str()) {
                    continue;
                }
                let pred = ch.book == "polymarket" || ch.book == "kalshi";
                let sources = g.and_then(|g| g.sources.as_deref()).unwrap_or(&c.odds.sources);
                if (sources == "books" && pred) || (sources == "predictions" && !pred) {
                    continue;
                }
                if d.abs() * 100.0 < g.and_then(|g| g.min_move_pts).unwrap_or(c.odds.min_move_pts) {
                    continue;
                }
                let cooldown = g.and_then(|g| g.cooldown_s).unwrap_or(c.odds.cooldown_s);
                let key = (ch.match_id, ch.book, ch.market.as_str(), (ch.line.unwrap_or(0.0) * 100.0) as i64, ch.outcome.as_str());
                if last.get(&key).is_some_and(|t| ch.ts_ms - t < cooldown as i64 * 1000) {
                    continue;
                }
                last.insert(key, ch.ts_ms);
                let (h, a, _) = names(&app, ch.match_id);
                let line = ch.line.map(|l| format!(" {l}")).unwrap_or_default();
                buf.push(format!(
                    "{} <b>{} v {}</b> · {} · {}{} {}: {:.2} → {:.2} ({} pts)",
                    if d > 0.0 { "▲" } else { "▼" }, esc(&h), esc(&a), esc(ch.book), market_name(ch.market.as_str()), line, outcome_name(ch.outcome.as_str()), o, n, pts(d)
                ));
                flush_at.get_or_insert(tokio::time::Instant::now() + Duration::from_secs(4));
            }
            _ = tokio::time::sleep(wait) => {
                flush_at = None;
                if buf.is_empty() { continue; }
                let text = format!("<b>Odds moves</b>\n{}", buf.join("\n"));
                buf.clear();
                if let Err(e) = send("odds", &text).await {
                    tracing::warn!("telegram odds alert: {e:#}");
                }
                if last.len() > 20_000 {
                    let cut = now_ms() - 3_600_000;
                    last.retain(|_, t| *t > cut);
                }
            }
        }
    }
}

/// Arbitrage (every 30 s) and closed paper-bot trades (every 60 s).
async fn scan_loop(app: Arc<App>) {
    let mut sent_arbs: HashMap<String, (Ms, f64)> = HashMap::new();
    let mut paper_seen: Ms = now_ms();
    let mut arb_seen: Ms = now_ms();
    let mut tick: u64 = 0;
    loop {
        tokio::time::sleep(Duration::from_secs(30)).await;
        tick += 1;
        let c = cfg();
        if !c.enabled || c.chat_id.is_none() {
            continue;
        }
        if c.arbs.enabled {
            let a2 = app.clone();
            let (min, stake) = (c.arbs.profit_floor(&c.odds) / 100.0, c.arbs.stake);
            let arbs = tokio::task::spawn_blocking(move || crate::arb::scan(&a2, min, stake)).await.unwrap_or_default();
            let now = now_ms();
            let fresh: Vec<&crate::arb::Arb> = arbs
                .iter()
                .filter(|a| {
                    let g = c.odds.game(&a.sport);
                    g.is_none_or(|g| g.arb_enabled) && a.profit * 100.0 >= g.and_then(|g| g.arb_min_profit_pct).unwrap_or(c.arbs.min_profit_pct)
                })
                .filter(|a| match sent_arbs.get(&a.key) {
                    None => true,
                    // Again after an hour, or when it got a point better.
                    Some((t, p)) => now - t > 3_600_000 || a.profit > p + 0.01,
                })
                .take(5)
                .collect();
            if !fresh.is_empty() {
                let text = fresh.iter().map(|a| arb_text(a)).collect::<Vec<_>>().join("\n\n");
                for a in &fresh {
                    sent_arbs.insert(a.key.clone(), (now, a.profit));
                }
                if let Err(e) = send("arb", &text).await {
                    tracing::warn!("telegram arb alert: {e:#}");
                }
            }
            sent_arbs.retain(|_, (t, _)| now - *t < 6 * 3_600_000);
        }
        if c.paper.enabled && tick % 2 == 0 {
            // Closed trades of the adaptive paper bot (rules re-chosen every 4 hours).
            let trades = if c.paper.trades { tokio::task::spawn_blocking(move || crate::adaptive::closed_since(paper_seen)).await.unwrap_or_default() } else { Vec::new() };
            let mut lines = Vec::new();
            if c.paper.arbs {
                let a2 = app.clone();
                let since = arb_seen;
                let (evs, upto) = tokio::task::spawn_blocking(move || crate::predarb::events_since(&a2, since)).await.unwrap_or_default();
                arb_seen = arb_seen.max(upto);
                lines.extend(evs);
            }
            for t in &trades {
                paper_seen = paper_seen.max(t["exit_ts"].as_i64().unwrap_or(0));
                let pnl = t["pnl"].as_f64().unwrap_or(0.0);
                lines.push(format!(
                    "{} {} · {} v {} · {} {} bought {:.2}, sold {:.2} ({}) · <b>{}${:.2}</b>",
                    if pnl >= 0.0 { "✅" } else { "❌" },
                    esc(t["base"].as_str().unwrap_or("")),
                    esc(t["home"].as_str().unwrap_or("")),
                    esc(t["away"].as_str().unwrap_or("")),
                    if t["dir"].as_i64() == Some(1) { "Yes" } else { "No" },
                    outcome_name(t["outcome"].as_str().unwrap_or("")),
                    t["entry"].as_f64().unwrap_or(0.0),
                    t["exit"].as_f64().unwrap_or(0.0),
                    t["reason"].as_str().unwrap_or(""),
                    if pnl >= 0.0 { "+" } else { "-" },
                    pnl.abs()
                ));
            }
            if !lines.is_empty() {
                let mut text = format!("<b>Paper bot</b>\n{}", lines.join("\n"));
                if c.paper.standings {
                    let a2 = app.clone();
                    if let Ok(st) = tokio::task::spawn_blocking(move || standings(&a2)).await {
                        text.push_str(&format!("\n\n{st}"));
                    }
                }
                let _ = send("paper", &text).await;
            }
        }
    }
}

/// A suspicious-match alert ("activity" or "lines"), when that kind is switched on.
pub async fn alert_suspicious(kind: &str, text: &str) {
    let c = cfg();
    let on = c.enabled && c.chat_id.is_some() && c.suspicious.enabled && match kind {
        "activity" => c.suspicious.activity,
        "lines" => c.suspicious.lines,
        _ => false,
    };
    if on {
        if let Err(e) = send("suspicious", text).await {
            tracing::warn!("telegram suspicious alert: {e:#}");
        }
    }
}

/// "Send test" on the page.
/// What the bot would answer to a read-only command (the Telegram page shows it; nothing is sent).
pub fn preview(app: &App, cmd: &str) -> Option<String> {
    let (cmd, arg) = cmd.trim().split_once(' ').map(|(a, b)| (a, b.trim())).unwrap_or((cmd.trim(), ""));
    Some(match cmd {
        "/status" => bot_status(app),
        "/now" => digest_text(app, "Update"),
        "/open" => trade_list(app, true, 20),
        "/trades" => trade_list(app, false, arg.parse::<i64>().unwrap_or(10).clamp(1, 30)),
        "/last" => last_text(app),
        "/best" => extremes_text(app, true),
        "/worst" => extremes_text(app, false),
        "/today" => today_text(app),
        "/week" => week_text(app),
        "/stats" => stats_text(app),
        "/sports" => sports_text(app),
        "/real" => wallet_text(app, true),
        "/paper" => wallet_text(app, false),
        "/run" => run_text(app),
        "/safety" => safety_text(app),
        "/twins" => twins_text(app),
        "/compare" => compare_text(app),
        "/rules" => rules_text(),
        "/settings" => settings_text(),
        "/signals" => signals_text(app),
        "/hours" => window_text(),
        "/replay" => replay_text(),
        "/timing" => timing_text(),
        "/health" => health_text(app),
        "/alerts" => alerts_text(),
        "/projected" => projected(app, if arg.is_empty() { "24h" } else { arg }),
        "/log" => real_log(app, arg.parse::<i64>().unwrap_or(20).clamp(1, 60)),
        "/help" | "/commands" => HELP.into(),
        _ => return None,
    })
}

/// Send a command's answer to the linked chat from the page ("/balance" and "/balancepaper" send
/// the balance chart).
pub async fn push(app: &Arc<App>, cmd: &str) -> Result<()> {
    let cmd = cmd.trim().to_string();
    if cmd == "/balance" || cmd == "/balancepaper" {
        let (a, real) = (app.clone(), cmd == "/balance");
        let (ch, cap) = tokio::task::spawn_blocking(move || balance_chart(&a, real)).await?.ok_or_else(|| anyhow!("no balance history yet"))?;
        return send_chart(&ch, &cap).await;
    }
    let (a, c2) = (app.clone(), cmd.clone());
    let text = tokio::task::spawn_blocking(move || preview(&a, &c2)).await?.ok_or_else(|| anyhow!("unknown command {cmd}"))?;
    send("bot", &text).await
}

pub async fn test(app: &App) -> Result<()> {
    send("test", &format!("Test from Swifteye. {}", status_text(&cfg()))).await?;
    let _ = app;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn per_game_edits_merge_and_reset() {
        *PATH.lock() = std::env::temp_dir().join("oddsfeed-tg-test.json");
        edit(&json!({"odds": {"games": {"cs2": {"enabled": false}, "football": {"min_move_pts": 1.5, "markets": ["total", "bogus"]}, "nonsense": {"enabled": false}}}})).unwrap();
        let o = cfg().odds;
        assert!(!o.games["cs2"].enabled);
        assert_eq!(o.games["football"].min_move_pts, Some(1.5));
        assert_eq!(o.games["football"].markets, Some(vec!["total".to_string()]));
        assert!(!o.games.contains_key("nonsense"));
        assert_eq!(o.move_floor(), 1.5);
        // A later partial edit keeps the rest; null goes back to the general value.
        edit(&json!({"odds": {"games": {"football": {"min_move_pts": null, "markets": null}}}})).unwrap();
        let o = cfg().odds;
        assert_eq!(o.games["football"].min_move_pts, None);
        assert_eq!(o.games["football"].markets, None);
        assert!(!o.games["cs2"].enabled);
        assert_eq!(o.move_floor(), o.min_move_pts);
        let _ = std::fs::remove_file(PATH.lock().clone());
    }
}

/// Current paper standings: each market's backtest balance (snipe) and the arbitrage totals.
fn standings(app: &App) -> String {
    let v = crate::adaptive::view(app);
    let a = crate::predarb::view(app);
    let mut out = String::from("<b>Standings</b>");
    for b in v["bases"].as_array().cloned().unwrap_or_default() {
        let bal = b["balance"].as_f64().unwrap_or(0.0);
        let start = v["bank"]["start"].as_f64().unwrap_or(0.0);
        out.push_str(&format!("\n{}: ${:.2} ({}${:.2})", if b["base"] == "kalshi" { "Kalshi" } else { "Polymarket" }, bal, if bal >= start { "+" } else { "-" }, (bal - start).abs()));
    }
    out.push_str(&format!("\nArbitrage: {} taken, ${:.2} locked", a["trades"].as_u64().unwrap_or(0), a["locked_profit"].as_f64().unwrap_or(0.0)));
    out
}

#[cfg(test)]
mod bot_text_tests {
    use super::*;

    #[test]
    fn money_and_time_words() {
        assert_eq!(dollars(1084.2), "$1,084.20");
        assert_eq!(dollars(12.0), "$12.00");
        assert_eq!((money_s(1668.07), money_s(-5.67), money_s(0.0)), ("+$1,668.07".to_string(), "-$5.67".to_string(), "+$0.00".to_string()));
        assert_eq!(dollars(-0.999), "-$1.00");
        assert_eq!(dollars(1_234_567.891), "$1,234,567.89");
        assert_eq!((cents(0.62), cents(0.625), cents(0.0)), ("62¢".to_string(), "62.5¢".to_string(), "0¢".to_string()));
        assert_eq!((held(45_000), held(185_000), held(3_900_000)), ("45 s".to_string(), "3 min".to_string(), "1 h 05 min".to_string()));
    }

    #[test]
    fn trade_texts() {
        let v = json!({"home": "A <b>", "away": "B", "outcome": "home", "dir": -1.0, "contracts": 19.0, "entry": 0.62, "cost": 12.0, "exit": 0.75, "pnl": 2.4, "reason": "caught up",
            "real": true, "set": "safe", "books": [2, 2], "edge": 0.09, "pre_mid": 0.4, "move": 0.14, "spread": 0.02, "catch": 1.0, "ts": 1_000_000, "give_up_s": 900, "exit_ts": 1_060_000, "closed_ms": 1_060_000});
        let b = buy_text(&v, false);
        assert!(b.contains("Bought") && b.contains("Real") && b.contains("A &lt;b&gt; v B") && b.contains("Against A") && b.contains("19 shares at 62¢") && b.contains("$12.00") && b.contains("Sells at"));
        let s = sell_text(&v, false);
        assert!(s.contains("Sold +$2.40") && s.contains("62¢ → 75¢") && s.contains("held 60 s") && s.contains("caught up"));
        assert!(!buy_text(&v, true).contains('\n') && !sell_text(&v, true).contains('\n'));
        let t = tally([v.clone(), json!({"cost": 5.0, "real": true})].iter());
        assert_eq!((t.closed, t.wins, t.open), (1, 1, 1));
        assert!(t.line().contains("+$2.40") && t.line().contains("1 open"));
    }

    /// Not a unit test: writes the chart of one recorded trade as JSON (SWIFTEYE_DB, TRADE_ID, OUT),
    /// to render and look at.
    #[test]
    #[ignore]
    fn dump_trade_chart() {
        let db = PathBuf::from(std::env::var("SWIFTEYE_DB").expect("SWIFTEYE_DB"));
        let id: i64 = std::env::var("TRADE_ID").expect("TRADE_ID").parse().unwrap();
        let c = crate::store::open_ro(&db).unwrap();
        let (d, real, cl): (String, i64, Option<Ms>) = c.query_row("SELECT data, real, closed_ms FROM bot_trades WHERE id = ?1", [id], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).unwrap();
        let mut v: Value = serde_json::from_str(&d).unwrap();
        v["real"] = json!(real != 0);
        v["closed_ms"] = json!(cl);
        // As an open trade (the chart sent with the buy) when OPEN is set.
        if std::env::var("OPEN").is_ok() {
            v["closed_ms"] = Value::Null;
        }
        let now = std::env::var("NOW").ok().and_then(|x| x.parse().ok()).unwrap_or_else(now_ms);
        let ch = trade_chart_at(&db, &v, 60_000, now).expect("chart");
        std::fs::write(std::env::var("OUT").expect("OUT"), serde_json::to_vec(&ch).unwrap()).unwrap();
    }

    #[test]
    fn every_listed_command_is_in_the_help() {
        for c in ["/status", "/now", "/open", "/today", "/hours", "/real", "/paper", "/account", "/balance", "/trades", "/last", "/chart", "/best", "/worst", "/week", "/stats", "/sports", "/run", "/safety", "/twins", "/compare", "/extend", "/realon", "/stoprun", "/realoff", "/rules", "/settings", "/signals", "/replay", "/projected", "/timing", "/log", "/health", "/kill", "/unkill", "/pause", "/resume", "/alerts", "/buys", "/sells", "/charts", "/updates"] {
            assert!(HELP.contains(c), "{c} missing from the help");
        }
    }
}

