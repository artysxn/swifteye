//! Bookmaker adapters. Each one polls the same public JSON the bookmaker's
//! own live page loads, and turns it into a normalised `Snapshot`.

pub mod altenar;
pub mod betby;
pub mod betcity;
pub mod betika;
pub mod betpawa;
pub mod fonbet;
pub mod kalshi;
pub mod kambi;
pub mod leon;
pub mod linebet;
pub mod m88;
pub mod marathon;
pub mod mostbet;
pub mod novibet;
pub mod olimp;
pub mod pinnacle;
pub mod polymarket;
pub mod sportybet;
pub mod thunderpick;

use crate::model::*;
use crate::App;
use anyhow::{bail, Context, Result};
use serde_json::Value;
use std::future::Future;
use std::sync::Arc;
use std::time::{Duration, Instant};

pub const UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";

/// Pre-match games: fetched less often than the live list, kept between refreshes,
/// and merged into every snapshot so a game moves smoothly from pre-match to live.
pub struct Pre {
    /// How far ahead to look (0 = pre-match off).
    pub hours: f64,
    pub every_ms: i64,
    last: Ms,
    events: Vec<EventIn>,
}

impl Pre {
    pub fn new(hours: f64, every_ms: i64) -> Self {
        Pre { hours, every_ms, last: 0, events: Vec::new() }
    }
    pub fn due(&self) -> bool {
        self.hours > 0.0 && now_ms() - self.last >= self.every_ms
    }
    /// Keep games kicking off in the window (and ones a few minutes past kick-off
    /// that the live list has not picked up yet).
    pub fn store(&mut self, mut evs: Vec<EventIn>) {
        let now = now_ms();
        let until = now + (self.hours * 3_600_000.0) as i64;
        evs.retain(|e| e.start_ms.is_some_and(|s| s >= now - 15 * 60_000 && s <= until));
        for e in &mut evs {
            e.live = false;
        }
        self.events = evs;
        self.last = now;
    }
    /// Retry soon after a failure instead of waiting a full interval.
    pub fn failed(&mut self) {
        self.last = now_ms() - self.every_ms + 5_000;
    }
    pub fn merge(&self, live: &mut Vec<EventIn>) {
        let ids: std::collections::HashSet<String> = live.iter().map(|e| e.id.clone()).collect();
        live.extend(self.events.iter().filter(|e| !ids.contains(&e.id)).cloned());
    }
}

/// Run the pre-match refresh if it is due; failures only log.
pub async fn refresh_pre<F, Fut>(pre: &mut Pre, name: &str, f: F)
where
    F: FnOnce() -> Fut,
    Fut: Future<Output = Result<Vec<EventIn>>>,
{
    if !pre.due() {
        return;
    }
    match f().await {
        Ok(evs) => pre.store(evs),
        Err(e) => {
            tracing::warn!("{name} pre-match: {e:#}");
            pre.failed();
        }
    }
}

pub trait Book: Send + 'static {
    fn name(&self) -> &'static str;
    /// Human-readable description of where the data comes from.
    fn source(&self) -> String;
    fn fetch(&mut self, http: &reqwest::Client) -> impl Future<Output = Result<Snapshot>> + Send;
}

pub fn http_client() -> reqwest::Client {
    reqwest::Client::builder()
        .user_agent(UA)
        .gzip(true)
        .brotli(true)
        .deflate(true)
        .timeout(Duration::from_secs(15))
        .connect_timeout(Duration::from_secs(8))
        .pool_idle_timeout(Duration::from_secs(90))
        .tcp_nodelay(true)
        .build()
        .expect("http client")
}

/// Send a request, read the whole body, and stamp the moment it finished arriving.
pub async fn fetch_json(req: reqwest::RequestBuilder) -> Result<(Value, Ms)> {
    let resp = req.send().await?;
    let status = resp.status();
    let body = resp.bytes().await?;
    let recv = now_ms();
    if !status.is_success() {
        let snippet = String::from_utf8_lossy(&body[..body.len().min(200)]).into_owned();
        bail!("HTTP {status}: {snippet}");
    }
    let v: Value = serde_json::from_slice(&body).with_context(|| {
        format!("bad JSON: {}", String::from_utf8_lossy(&body[..body.len().min(200)]))
    })?;
    Ok((v, recv))
}

/// Poll one book forever.
pub async fn run<B: Book>(mut book: B, app: Arc<App>, _poll_ms: u64) {
    let name = book.name();
    let http = http_client();
    let mut backoff = Duration::ZERO;
    let mut next = tokio::time::Instant::now();
    loop {
        // Interval and pause are read every cycle so the control panel takes effect live.
        let (paused, poll_ms) = app.engine.lock().schedule(name);
        tokio::time::sleep_until(next).await;
        next = tokio::time::Instant::now() + Duration::from_millis(poll_ms);
        if paused {
            continue;
        }
        if !backoff.is_zero() {
            tokio::time::sleep(backoff).await;
        }
        let t0 = Instant::now();
        let res = tokio::time::timeout(Duration::from_secs(20), book.fetch(&http)).await;
        let res = match res {
            Ok(r) => r,
            Err(_) => Err(anyhow::anyhow!("timed out")),
        };
        match res {
            Ok(mut snap) => {
                let req_ms = t0.elapsed().as_millis() as i64;
                // The simulated-football filter only applies to football; real e-sports
                // titles (CS2, LoL, ...) legitimately have "esports" in league/team names.
                if !app.cfg.include_esoccer {
                    snap.events.retain(|e| e.sport != "football" || !is_esoccer(e));
                }
                snap.events.retain(|e| app.cfg.follows(&e.sport));
                let writes = app.engine.lock().apply(name, snap, req_ms);
                app.emit(writes).await;
                if !backoff.is_zero() {
                    tracing::info!("{name}: recovered");
                }
                backoff = Duration::ZERO;
            }
            Err(e) => {
                let msg = format!("{e:#}");
                app.engine.lock().poll_error(name, msg.clone());
                backoff = (backoff * 2).clamp(Duration::from_secs(2), Duration::from_secs(60));
                tracing::warn!("{name}: {msg} (retry in {}s)", backoff.as_secs());
            }
        }
    }
}

/// Sport keys used for competitive video games. Every adapter maps its own naming
/// onto these, so the matcher never mixes titles (a "Team Liquid" exists in several).
pub const ESPORTS: &[&str] = &["cs2", "lol", "dota2", "valorant", "ow", "rl", "r6", "cod", "sc2", "mlbb", "hok", "pubg"];

pub fn is_esports_sport(sport: &str) -> bool {
    sport == "esports" || ESPORTS.contains(&sport)
}

/// Traditional sports followed besides football. Every adapter maps its own naming onto these keys.
pub const SPORTS: &[&str] = &["football", "basketball", "tennis", "hockey", "baseball", "volleyball", "amfootball", "mma", "tabletennis", "handball"];

/// Sports whose main market is three-way (home / draw / away, regular time).
/// Everything else stores a two-way winner (overtime / tie-break / series included) under
/// `Market::X12` with only home and away, so a draw price never appears for them.
pub fn three_way(sport: &str) -> bool {
    matches!(sport, "football" | "handball")
}

/// Map a source's sport name ("ICE_HOCKEY", "Ice Hockey", "Hockey", "Soccer"...) to a sport key.
/// E-sports return `None` here: they need the title from `esport_key`.
pub fn sport_key(raw: &str) -> Option<&'static str> {
    let s = raw.to_lowercase().replace(['_', '-'], " ");
    Some(match s.trim() {
        "football" | "soccer" => "football",
        "basketball" => "basketball",
        "tennis" => "tennis",
        "ice hockey" | "hockey" => "hockey",
        "baseball" => "baseball",
        "volleyball" => "volleyball",
        "american football" => "amfootball",
        "mma" | "mixed martial arts" | "ufc" | "ufc/mma" => "mma",
        "table tennis" => "tabletennis",
        "handball" => "handball",
        _ => return None,
    })
}

/// Keep only the winner prices that make sense for the sport: no draw for two-way sports.
/// A two-way sport that offered a draw is quoting regular-time 1X2 (hockey), which is a
/// different market from the winner other sources quote, so the whole market is dropped.
pub fn winner_quotes(sport: &str, qs: Vec<QuoteIn>) -> Vec<QuoteIn> {
    if three_way(sport) {
        return qs;
    }
    let has_draw = qs.iter().any(|q| q.market == Market::X12 && q.outcome == Outcome::Draw);
    qs.into_iter().filter(|q| q.market != Market::X12 || !has_draw).collect()
}

/// Map any source's game name (a Polymarket series slug, a Kalshi competition, a bookmaker
/// category, a tournament name) to a sport key. `None` = not an e-sport we follow
/// (including FIFA/eFootball, which is simulated football).
pub fn esport_key(raw: &str) -> Option<&'static str> {
    let s = raw.to_lowercase();
    let has = |x: &str| s.contains(x);
    if has("fifa") || has("efootball") || has("e-football") || has("esoccer") || has("ebasketball") || has("nba 2k") || has("ea sports") || has("fc 2") || has("wild rift") || has("wild-rift") {
        return None;
    }
    Some(if has("counter-strike") || has("counter strike") || has("cs2") || has("cs:go") || has("csgo") || has("cs 2") {
        "cs2"
    } else if has("league of legends") || s == "lol" || has("lol ") || has(" lol") || has("lol:") || has("lol-") || has("lck") || has("lpl ") || has("lec ") || has("lcs") {
        "lol"
    } else if has("dota") {
        "dota2"
    } else if has("valorant") || has("vct") {
        "valorant"
    } else if has("overwatch") || s == "ow" {
        "ow"
    } else if has("rocket league") || s == "rl" {
        "rl"
    } else if has("rainbow six") || has("r6") {
        "r6"
    } else if has("call of duty") || s == "cod" || s == "codmw" {
        "cod"
    } else if has("starcraft") || s == "sc2" || s == "sc1" || s == "sc" {
        "sc2"
    } else if has("mobile legends") || s == "mlbb" {
        "mlbb"
    } else if has("honor of kings") || s == "hok" {
        "hok"
    } else if has("pubg") {
        "pubg"
    } else {
        return None;
    })
}

/// Simulated / e-sports football (FIFA "2x4 min" leagues, Volta, cyber) — not real matches.
pub fn is_esoccer(e: &EventIn) -> bool {
    let l = e.league.to_lowercase();
    let teams = format!("{} {}", e.home, e.away).to_lowercase();
    l.contains("esport")
        || l.contains("e-sport")
        || l.contains("cyber")
        || l.contains("efootball")
        || l.contains("e-football")
        || l.contains("esoccer")
        || l.contains("e-soccer")
        || l.contains("volta")
        || l.contains("fifa")
        || l.contains("ea sports")
        || l.contains(" min.")
        || l.contains("x4 min")
        || l.contains("x3 min")
        || l.contains("x5 min")
        || l.contains("x6 min")
        || (l.starts_with("fc 2") && l.as_bytes().get(5).is_some_and(|b| b.is_ascii_digit()))
        || l.contains("short football")
        || l.trim_end().ends_with('+')
        || e.home.trim_end().ends_with(" +")
        // "Division 4x4", "LFL 5x5", "Short Football 3x3": small-sided simulated formats
        || l.split(|c: char| !c.is_ascii_alphanumeric()).any(|t| {
            let b = t.as_bytes();
            b.len() == 3 && b[0].is_ascii_digit() && b[1] == b'x' && b[2].is_ascii_digit()
        })
        || teams.contains("esports")
        || teams.contains("(e)")
}

// ---- small JSON helpers -------------------------------------------------

pub fn num(v: &Value) -> Option<f64> {
    match v {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

pub fn int(v: &Value) -> Option<i64> {
    match v {
        Value::Number(n) => n.as_i64().or_else(|| n.as_f64().map(|f| f as i64)),
        Value::String(s) => s.trim().parse().ok(),
        _ => None,
    }
}

pub fn text(v: &Value) -> String {
    match v {
        Value::String(s) => s.trim().to_string(),
        Value::Number(n) => n.to_string(),
        _ => String::new(),
    }
}

pub fn id_text(v: &Value) -> String {
    text(v)
}

pub fn rfc3339_ms(s: &str) -> Option<Ms> {
    let t = time::OffsetDateTime::parse(s, &time::format_description::well_known::Rfc3339).ok()?;
    Some((t.unix_timestamp_nanos() / 1_000_000) as i64)
}

/// "YYYY-MM-DD HH:MM:SS" in a fixed UTC offset (hours).
pub fn naive_ms(s: &str, offset_h: i64) -> Option<Ms> {
    let fmt = time::macros::format_description!("[year]-[month]-[day] [hour]:[minute]:[second]");
    let t = time::PrimitiveDateTime::parse(s.trim(), &fmt).ok()?;
    Some(t.assume_utc().unix_timestamp() * 1000 - offset_h * 3600 * 1000)
}

pub fn join_league(parts: &[&str]) -> String {
    parts.iter().filter(|p| !p.is_empty()).copied().collect::<Vec<_>>().join(" / ")
}
