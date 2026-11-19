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
            let cmds = json!({"commands": [
                {"command": "balance", "description": "Chart of the bot's real balance ($40 test run)"},
                {"command": "balancepaper", "description": "Chart of the paper balance"},
                {"command": "projected", "description": "What money turns into: /projected 24h [start]"},
                {"command": "log", "description": "All real-money orders"},
                {"command": "status", "description": "Mode, balances, today, open trades, test run"},
                {"command": "open", "description": "Open trades"},
                {"command": "trades", "description": "Last 10 closed trades"},
                {"command": "run", "description": "The real test run"},
                {"command": "chart", "description": "Chart of the last closed trade"},
                {"command": "kill", "description": "Emergency: stop opening trades"},
                {"command": "unkill", "description": "Resume trading"},
                {"command": "pause", "description": "Updates off"},
                {"command": "resume", "description": "Updates on"}]});
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
        for (id, v) in rows {
            let tag = if v["real"].as_bool() == Some(true) { "💵 REAL" } else { "📝 paper" };
            let name = format!("{} v {}", esc(v["home"].as_str().unwrap_or("")), esc(v["away"].as_str().unwrap_or("")));
            let side = format!("{} {}", if v["dir"].as_f64().unwrap_or(1.0) > 0.0 { "Yes" } else { "No" }, outcome_name(v["outcome"].as_str().unwrap_or("")));
            let n = v["contracts"].as_f64().unwrap_or(0.0);
            let entry = v["entry"].as_f64().unwrap_or(0.0);
            if id > last_id {
                last_id = id;
                let (target, give) = pays_at(&v);
                let text = format!("🟢 <b>Snipe bought</b> · {tag}\n{name} · {side}\n{n:.0} @ {:.1}¢ = ${:.2} · trigger {} ({} books), edge {:.1}¢\n💰 Cash in {}: when Polymarket reaches {}¢ (≈{}); at the latest {} (gives up)",
                    entry * 100.0, v["cost"].as_f64().unwrap_or(0.0), esc(v["trigger"].as_str().unwrap_or("")), v["books"][1], v["edge"].as_f64().unwrap_or(0.0) * 100.0,
                    eta_text(&app, v["ts"].as_i64().unwrap_or(0)),
                    target.map(|x| format!("{:.1}", x * 100.0)).unwrap_or("?".into()),
                    target.map(|x| money_s(n * (x - entry))).unwrap_or_default(),
                    give.map(hhmm).unwrap_or_default());
                let _ = send("bot", &text).await;
            }
            if let Some(cl) = v["closed_ms"].as_i64() {
                if cl > closed_seen {
                    closed_seen = cl;
                    let pnl = v["pnl"].as_f64().unwrap_or(0.0);
                    let caption = format!("{} <b>Snipe sold</b> · {tag}\n{name} · {side}\n{n:.0}: {:.1}¢ → {:.1}¢ ({}) · <b>{}${:.2}</b>",
                        if pnl >= 0.0 { "✅" } else { "❌" }, entry * 100.0, v["exit"].as_f64().unwrap_or(0.0) * 100.0, esc(v["reason"].as_str().unwrap_or("")),
                        if pnl >= 0.0 { "+" } else { "-" }, pnl.abs());
                    let a2 = app.clone();
                    let v2 = v.clone();
                    let chart = tokio::task::spawn_blocking(move || trade_chart(&a2, &v2)).await.ok().flatten();
                    let value = bot_balance(&app, v["real"].as_bool() == Some(true));
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
                    let real = v["real"].as_bool() == Some(true);
                    if let Ok(Some((ch, cap))) = tokio::task::spawn_blocking(move || balance_chart(&a3, real)).await {
                        let _ = send_chart(&ch, &cap).await;
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
fn trade_chart(app: &App, v: &Value) -> Option<Value> {
    let c = crate::store::open_ro(&app.db_path).ok()?;
    let (m, o) = (v["match_id"].as_i64()?, v["outcome"].as_str()?.to_string());
    let (target, give) = pays_at(v);
    let end = v["closed_ms"].as_i64().unwrap_or_else(|| now_ms());
    let (t0, t1) = (v["ts"].as_i64()? - 600_000, end + 300_000);
    let yes = v["dir"].as_f64().unwrap_or(1.0) > 0.0;
    let mut st = c.prepare("SELECT ts_ms, bid, ask FROM pm_quotes WHERE match_id = ?1 AND outcome = ?2 AND ts_ms >= ?3 AND ts_ms <= ?4 ORDER BY ts_ms").ok()?;
    let rows: Vec<(Ms, Option<f64>, Option<f64>)> = st.query_map(params![m, o, t0, t1], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?))).ok()?.filter_map(|x| x.ok()).collect();
    let mut mins: std::collections::BTreeMap<Ms, (Option<f64>, Option<f64>)> = Default::default();
    for (t, b, a) in rows {
        // The bought side's price: Yes as is; No = 1 - the other side of the Yes book.
        let (b2, a2) = if yes { (b, a) } else { (a.map(|x| 1.0 - x), b.map(|x| 1.0 - x)) };
        mins.insert(t - t.rem_euclid(60_000), (b2, a2));
    }
    if mins.len() < 2 {
        return None;
    }
    let keys: Vec<Ms> = mins.keys().copied().collect();
    let idx = |t: Ms| keys.iter().rposition(|k| *k <= t).unwrap_or(0);
    let labels: Vec<String> = keys.iter().map(|k| time::OffsetDateTime::from_unix_timestamp(k / 1000).map(|d| format!("{:02}:{:02}", d.hour(), d.minute())).unwrap_or_default()).collect();
    let cents = |x: Option<f64>| x.map(|p| (p * 1000.0).round() / 10.0);
    let mut buy = vec![Value::Null; keys.len()];
    let mut sell = vec![Value::Null; keys.len()];
    buy[idx(v["ts"].as_i64()?)] = json!(cents(v["entry"].as_f64()));
    if let Some(cl) = v["closed_ms"].as_i64() {
        sell[idx(cl)] = json!(cents(v["exit"].as_f64()));
    }
    let eta = if v["closed_ms"].is_i64() { String::new() } else { format!(" · cash in {}", eta_text(app, v["ts"].as_i64().unwrap_or(0))) };
    let title = format!("{} v {} · {} {} · {} (cents) · pays at {}¢{eta}, gives up {}", v["home"].as_str().unwrap_or(""), v["away"].as_str().unwrap_or(""), if yes { "Yes" } else { "No" }, outcome_name(&o), if v["real"].as_bool() == Some(true) { "REAL" } else { "paper" },
        target.map(|x| format!("{:.1}", x * 100.0)).unwrap_or("?".into()), give.map(hhmm).unwrap_or_default());
    Some(json!({
        "type": "line",
        "data": {"labels": labels, "datasets": [
            {"label": "Bid", "data": mins.values().map(|x| cents(x.0)).collect::<Vec<_>>(), "borderColor": "#7a8a86", "fill": false, "pointRadius": 0, "borderWidth": 1.5, "steppedLine": true},
            {"label": "Ask", "data": mins.values().map(|x| cents(x.1)).collect::<Vec<_>>(), "borderColor": "#b8c4c0", "fill": false, "pointRadius": 0, "borderWidth": 1.5, "steppedLine": true},
            {"label": "Buy", "data": buy, "borderColor": "#0f9e73", "backgroundColor": "#0f9e73", "showLine": false, "pointRadius": 7, "pointStyle": "triangle"},
            {"label": "Sell", "data": sell, "borderColor": "#e0453c", "backgroundColor": "#e0453c", "showLine": false, "pointRadius": 7, "pointStyle": "rectRot"},
            {"label": format!("Pays at {}¢ (sells when caught up)", target.map(|x| format!("{:.1}", x * 100.0)).unwrap_or("?".into())), "data": vec![cents(target); keys.len()], "borderColor": "#2f6fdb", "borderDash": [6, 4], "fill": false, "pointRadius": 0, "borderWidth": 1.5},
        ]},
        "options": {"title": {"display": true, "text": title}, "legend": {"position": "bottom"}, "spanGaps": true,
            "scales": {"yAxes": [{"scaleLabel": {"display": true, "labelString": "¢"}}]}}
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

/// The bot's own balance: during a real test run, its bankroll ($40) plus the run's results;
/// otherwise its real bankroll plus its real results; for a paper trade, the paper wallet.
fn bot_balance(app: &App, real: bool) -> String {
    let st = crate::bots::settings();
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return String::new() };
    if let Some(run) = &st.run {
        let rep = crate::bots::run_report(app, run, now_ms());
        let p = rep["pnl"].as_f64().unwrap_or(0.0);
        return format!("Bot balance ${:.2} (test run: ${:.0} {})", run.bankroll + p, run.bankroll, money_s(p));
    }
    if real {
        let (closed, open, ..) = crate::bots::snipe_ledger(&c, crate::bots::SNIPE, true);
        format!("Bot balance ${:.2} (${:.0} bankroll {}, ${open:.2} in open trades)", st.snipe.c.bankroll + closed, st.snipe.c.bankroll, money_s(closed))
    } else {
        let (closed, open, ..) = crate::bots::snipe_ledger(&c, crate::bots::SNIPE, false);
        format!("Paper bot balance ${:.2} (${:.0} start {}, ${open:.2} in open trades)", st.snipe.c.start + closed, st.snipe.c.start, money_s(closed))
    }
}

/// The bot's balance over time: starts at its money (the test run's $40, else its real bankroll;
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
            (st.snipe.c.bankroll, 0, now, format!("Snipe bot balance · REAL (${:.0} bankroll)", st.snipe.c.bankroll))
        }
    } else {
        (st.snipe.c.start, st.snipe.c.paper_since, now, format!("Snipe bot balance · paper (${:.0} start)", st.snipe.c.start))
    };
    let mut q = c.prepare("SELECT closed_ms, json_extract(data, '$.pnl') FROM bot_trades WHERE bot = 'snipe' AND real = ?1 AND closed_ms IS NOT NULL AND ts_ms >= ?2 AND closed_ms <= ?3 ORDER BY closed_ms").ok()?;
    let pts: Vec<(Ms, f64)> = q.query_map(params![real as i64, from, to], |r| Ok((r.get::<_, Ms>(0)?, r.get::<_, Option<f64>>(1)?.unwrap_or(0.0)))).ok()?.filter_map(|x| x.ok()).collect();
    let fmt_t = |ms: Ms| time::OffsetDateTime::from_unix_timestamp(ms / 1000).map(|d| format!("{:02}/{:02} {:02}:{:02}", d.day(), u8::from(d.month()), d.hour(), d.minute())).unwrap_or_default();
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
    let colour = if bal >= start_bal { "#0f9e73" } else { "#e0453c" };
    let wins = pts.iter().filter(|p| p.1 > 0.0).count();
    let caption = format!("💼 <b>Bot balance ${bal:.2}</b> ({}{:.2} from ${start_bal:.0}) · {} trades, {wins} won", if bal >= start_bal { "+$" } else { "-$" }, (bal - start_bal).abs(), pts.len());
    Some((json!({
        "type": "line",
        "data": {"labels": labels, "datasets": [
            {"label": "Balance ($)", "data": vals, "borderColor": colour, "backgroundColor": "rgba(15,158,115,0.08)", "fill": true, "pointRadius": 3, "steppedLine": true, "borderWidth": 2},
            {"label": "Start", "data": vec![start_bal; labels.len()], "borderColor": "#9aa5a1", "borderDash": [6, 4], "fill": false, "pointRadius": 0, "borderWidth": 1},
        ]},
        "options": {"title": {"display": true, "text": [label, format!("${bal:.2} now")]}, "legend": {"position": "bottom"},
            "scales": {"yAxes": [{"scaleLabel": {"display": true, "labelString": "$"}}]}}
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
        .json(&json!({"chart": chart, "width": 900, "height": 500, "devicePixelRatio": 2, "backgroundColor": "white", "format": "png"}))
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
        "/status" => bot_status(app),
        "/open" => trade_list(app, true, 10),
        "/trades" => trade_list(app, false, 10),
        "/run" => run_text(app),
        "/extend" => match arg.trim().trim_end_matches(|c: char| c.is_ascii_alphabetic()).parse::<i64>().map_err(anyhow::Error::from).and_then(|h| crate::bots::extend_run(app, h)) {
            Ok(v) => format!("Test run extended; now ends {}", hhmm(v["end_ms"].as_i64().unwrap_or(0))),
            Err(e) => format!("Use: /extend 4h (1-48 h) · {}", esc(&e.to_string())),
        },
        "/projected" => projected(app, &arg),
        "/log" => real_log(app, arg.parse::<i64>().unwrap_or(20).clamp(1, 60)),
        "/kill" => match crate::bots::update(app, &json!({"kill": true})) {
            Ok(_) => "🛑 Kill switch ON: the bot opens no new trades (open ones still get sold). /unkill to resume.".into(),
            Err(e) => format!("Could not: {e:#}"),
        },
        "/unkill" => match crate::bots::update(app, &json!({"kill": false})) {
            Ok(_) => "Kill switch off: the bot trades again.".into(),
            Err(e) => format!("Could not: {e:#}"),
        },
        "/pause" => {
            CFG.lock().enabled = false;
            save(&cfg());
            "Updates paused (the bot keeps trading). /resume to get them again.".into()
        }
        "/resume" => {
            CFG.lock().enabled = true;
            save(&cfg());
            "Updates on.".into()
        }
        _ => HELP.into(),
    };
    reply(http, &c.token, chat, &msg).await
}

const HELP: &str = "<b>Snipe bot</b>\n/extend 4h — extend the running test run\n/projected 24h [start $] — what money turns into at the bot's rate (3d, 1w...)\n/log [n] — the bots' real-money orders\n/balance — chart of the bot's real balance (the $40 test run)\n/balancepaper — the paper balance chart\n/status — mode, balances, today, open trades, test run\n/open — open trades\n/trades — last 10 closed trades\n/run — the real test run (now or last)\n/chart — chart of the last closed trade\n/kill — stop opening trades (emergency)\n/unkill — resume\n/pause, /resume — updates off / on";

fn money_s(x: f64) -> String {
    format!("{}${:.2}", if x < 0.0 { "-" } else { "+" }, x.abs())
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

/// The bot's money now: the test run's $40 ± results while one runs, else its real bankroll ±
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
            let when = time::OffsetDateTime::from_unix_timestamp(at / 1000).map(|t| format!("{:02}/{:02} {:02}:{:02} UTC", t.day(), u8::from(t.month()), t.hour(), t.minute())).unwrap_or_default();
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
        .prepare("SELECT ts_ms, bot, venue, ok, data FROM bot_orders ORDER BY id DESC LIMIT ?1")
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
        let t = time::OffsetDateTime::from_unix_timestamp(ts / 1000).map(|x| format!("{:02}/{:02} {:02}:{:02}", x.day(), u8::from(x.month()), x.hour(), x.minute())).unwrap_or_default();
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
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return "Database not readable.".into() };
    let mode = if s.kill { "🛑 KILLED" } else if !s.snipe.c.enabled { "off" } else if s.snipe.c.real { "💵 REAL" } else { "📝 paper" };
    let (pc, po, pd, pn, pw) = crate::bots::snipe_ledger(&c, crate::bots::SNIPE, false);
    let (rc, ro, rd, rn, rw) = crate::bots::snipe_ledger(&c, crate::bots::SNIPE, true);
    let r = crate::adaptive::active_rules();
    let mut t = format!(
        "<b>Snipe bot</b> · {mode}\n📝 Paper: ${:.2} ({}), today {}, {pn} trades {pw} won, ${po:.2} open\n💵 Real: {} total, today {}, {rn} trades {rw} won, ${ro:.2} open, ${:.2} free of ${:.0}\nRules: {}+ books, {:.0} pt move, ≤{:.0}% followed, gain ≥{:.0}¢, give up {} min\nStake {:.0}% (max ${:.0}), daily loss stop ${:.0}",
        s.snipe.c.start + pc, money_s(pc), money_s(pd), money_s(rc), money_s(rd), crate::bots::real_free(&c, crate::bots::SNIPE), s.snipe.c.bankroll,
        r.min_books, r.min_move * 100.0, r.max_followed * 100.0, r.min_edge * 100.0, r.give_up_s / 60, s.snipe.pct * 100.0, s.snipe.c.max_trade, s.snipe.c.daily_loss
    );
    if let Some(run) = &s.run {
        let rep = crate::bots::run_report(app, run, now_ms());
        t.push_str(&format!("\n⏱ Test run until {}: {} trades, {}", hhmm(run.end_ms), rep["trades"], money_s(rep["pnl"].as_f64().unwrap_or(0.0))));
    }
    t
}

fn hhmm(ms: Ms) -> String {
    time::OffsetDateTime::from_unix_timestamp(ms / 1000).map(|d| format!("{:02}:{:02} UTC", d.hour(), d.minute())).unwrap_or_default()
}

fn trade_list(app: &App, open: bool, n: i64) -> String {
    let Ok(c) = crate::store::open_ro(&app.db_path) else { return "Database not readable.".into() };
    let sql = if open {
        "SELECT data, real FROM bot_trades WHERE bot = 'snipe' AND closed_ms IS NULL ORDER BY ts_ms DESC LIMIT ?1"
    } else {
        "SELECT data, real FROM bot_trades WHERE bot = 'snipe' AND closed_ms IS NOT NULL ORDER BY closed_ms DESC LIMIT ?1"
    };
    let rows: Vec<(Value, bool)> = c
        .prepare(sql)
        .and_then(|mut st| st.query_map([n], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))).map(|it| it.filter_map(|x| x.ok()).filter_map(|(d, real)| serde_json::from_str(&d).ok().map(|v| (v, real != 0))).collect()))
        .unwrap_or_default();
    if rows.is_empty() {
        return if open { "No open trades.".into() } else { "No closed trades yet.".into() };
    }
    rows.iter()
        .map(|(v, real)| {
            let side = format!("{} {}", if v["dir"].as_f64().unwrap_or(1.0) > 0.0 { "Yes" } else { "No" }, outcome_name(v["outcome"].as_str().unwrap_or("")));
            let head = format!("{} {} v {} · {side}", if *real { "💵" } else { "📝" }, esc(v["home"].as_str().unwrap_or("")), esc(v["away"].as_str().unwrap_or("")));
            if open {
                format!("{head}\n   {:.0} @ {:.1}¢ (${:.2})", v["contracts"].as_f64().unwrap_or(0.0), v["entry"].as_f64().unwrap_or(0.0) * 100.0, v["cost"].as_f64().unwrap_or(0.0))
            } else {
                let p = v["pnl"].as_f64().unwrap_or(0.0);
                format!("{} {head}\n   {:.1}¢ → {:.1}¢ ({}) <b>{}</b>", if p >= 0.0 { "✅" } else { "❌" }, v["entry"].as_f64().unwrap_or(0.0) * 100.0, v["exit"].as_f64().unwrap_or(0.0) * 100.0, esc(v["reason"].as_str().unwrap_or("")), money_s(p))
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
}

fn run_text(app: &App) -> String {
    let g = crate::bots::runs(app);
    let (r, live) = match (&g["current"], g["past"].get(0)) {
        (Value::Object(_), _) => (g["current"].clone(), true),
        (_, Some(p)) => (p.clone(), false),
        _ => return "No test run yet. Start one on the Bots page.".into(),
    };
    let sig = r["signals"].as_array().map(|a| a.iter().take(4).map(|x| format!("{} {}", x["result"].as_str().unwrap_or("").replace('_', " "), x["n"])).collect::<Vec<_>>().join(", ")).unwrap_or_default();
    format!(
        "<b>Test run</b> · {}\n{} → {}\nResult {} on ${:.2} staked\n{} trades: {} closed, {} won, {} open\nSignals: {}",
        if live { "running" } else { r["ended"].as_str().unwrap_or("ended") },
        hhmm(r["start_ms"].as_i64().unwrap_or(0)), hhmm(r["end_ms"].as_i64().unwrap_or(0)),
        money_s(r["pnl"].as_f64().unwrap_or(0.0)), r["staked"].as_f64().unwrap_or(0.0),
        r["trades"], r["closed"], r["wins"], r["open"], esc(&sig)
    )
}

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
