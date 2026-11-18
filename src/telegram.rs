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
        OddsAlerts { enabled: true, watched_only: true, min_move_pts: 3.0, markets: vec!["1x2".into()], sources: "all".into(), cooldown_s: 60, watch: Vec::new(), games: HashMap::new() }
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
        ArbAlerts { enabled: true, min_profit_pct: 1.0, stake: 100.0 }
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
        SuspAlerts { enabled: true, activity: true, lines: true }
    }
}

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
#[serde(default)]
pub struct PaperAlerts {
    pub enabled: bool,
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
            return reply(http, &c.token, chat, &format!("Linked to Swifteye. Alerts come here.\n\n{}\n\nChange them on the Telegram page in Swifteye, or use /pause and /resume.", status_text(&cfg()))).await;
        }
        if c.chat_id == Some(chat) {
            return reply(http, &c.token, chat, &status_text(&c)).await;
        }
        return reply(http, &c.token, chat, "This bot belongs to a private Swifteye install. To link it, open the Telegram page in Swifteye and press <b>Open in Telegram</b>, or enter your handle there and send /start again.").await;
    }
    if c.chat_id != Some(chat) {
        return Ok(()); // not the linked chat: stay quiet
    }
    let msg = match cmd.as_str() {
        "/status" => status_text(&c),
        "/pause" => {
            CFG.lock().enabled = false;
            save(&cfg());
            "Alerts paused. /resume to start them again.".into()
        }
        "/resume" => {
            CFG.lock().enabled = true;
            save(&cfg());
            "Alerts on.".into()
        }
        "/watching" => {
            if c.odds.watch.is_empty() {
                "No matches watched. Press the bell on a match in Swifteye.".into()
            } else {
                c.odds.watch.iter().map(|id| {
                    let (h, a, s) = names(app, *id);
                    format!("• {} v {} <i>{}</i>", esc(&h), esc(&a), fmt_kick(s))
                }).collect::<Vec<_>>().join("\n")
            }
        }
        "/arbs" => {
            let arbs = crate::arb::scan(app, 0.0, c.arbs.stake);
            if arbs.is_empty() { "No pre-match arbitrage right now.".into() } else { arbs.iter().take(5).map(arb_text).collect::<Vec<_>>().join("\n\n") }
        }
        _ => "Commands: /status, /arbs, /watching, /pause, /resume".into(),
    };
    reply(http, &c.token, chat, &msg).await
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
            let trades = tokio::task::spawn_blocking(move || crate::adaptive::closed_since(paper_seen)).await.unwrap_or_default();
            let mut lines = Vec::new();
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
                let _ = send("paper", &format!("<b>Paper bot</b> (adaptive rules, share of the balance a trade)\n{}", lines.join("\n"))).await;
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
