//! Bookmaker adapters. Each one polls the same public JSON the bookmaker's
//! own live page loads, and turns it into a normalised `Snapshot`.

pub mod altenar;
pub mod betby;
pub mod betcity;
pub mod betika;
pub mod betpawa;
pub mod fonbet;
pub mod hkjc;
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
pub mod pinnacle_member;
pub mod polymarket;
pub mod sbobet;
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
    /// The HTTP client this book polls with. Most share the default.
    fn client(&self) -> reqwest::Client {
        http_client()
    }
    /// How long to wait after a failure, from the error text and how many failures in a row
    /// (`streak`, 1 = the first). None = the usual wait, doubling from 2 s to 60 s.
    fn cool_down(&self, _msg: &str, _streak: u32) -> Option<Duration> {
        None
    }
    /// Prices for parts of the games of the last fetch that changed since the one before (maps,
    /// sets, halves ...). Recorded only: they bypass the engine. Most books offer none here.
    fn parts(&mut self) -> Vec<PartQuote> {
        Vec::new()
    }
    /// Extra live-score detail of the last fetch's games, by the book's event id (games and points
    /// of a tennis set, rounds of each map, quarters ...). Recorded only (`book_scores`). Most books
    /// give none here; their main score and clock are recorded anyway.
    fn score_detail(&mut self) -> std::collections::HashMap<String, String> {
        Default::default()
    }
}

/// Books that are RECORDED ONLY (owner, 22 Nov 2026: new sources are tried before any bot may use
/// them): their price changes go to `changes_research` instead of `changes` (App::emit) and are never
/// broadcast, so the bots, the consensus, the replay and every research tool reading `changes` stay
/// exactly as before. Their games still get `book_events` rows (for joins). Take a book off this list
/// only after its timing against Polymarket was tested.
pub const RECORD_ONLY: &[&str] = &["sbobet", "hkjc", "pinnacle_member"];

pub fn record_only(book: &str) -> bool {
    RECORD_ONLY.contains(&book)
}

/// Live score of one game in one poll: (the book's event id, score, clock).
pub type LiveScore = (String, Option<String>, Option<String>);

/// The `book_scores` rows of one poll: a game's score or detail that differs from what this book
/// last showed for it (a game seen for the first time gets one row too). `last` keeps only the
/// games of this poll, so it never grows past the book's live list. The clock alone changing
/// writes nothing.
pub fn score_writes(
    book: &'static str,
    ts_ms: Ms,
    live: Vec<LiveScore>,
    detail: &std::collections::HashMap<String, String>,
    last: &mut std::collections::HashMap<String, (Option<String>, Option<String>)>,
) -> Vec<crate::engine::Write> {
    let mut out = Vec::new();
    let mut seen = std::collections::HashMap::with_capacity(live.len());
    for (id, score, clock) in live {
        let now = (score, detail.get(&id).cloned());
        if last.get(&id) != Some(&now) {
            out.push(crate::engine::Write::BookScore { ts_ms, book, book_event_id: id.clone(), score: now.0.clone(), detail: now.1.clone(), clock });
        }
        seen.insert(id, now);
    }
    *last = seen;
    out
}

pub fn http_client() -> reqwest::Client {
    http_builder().build().expect("http client")
}

/// The settings every feed's client shares, for a book that needs one small change (Leon: no redirects).
pub fn http_builder() -> reqwest::ClientBuilder {
    reqwest::Client::builder()
        .user_agent(UA)
        .gzip(true)
        .brotli(true)
        .deflate(true)
        .timeout(Duration::from_secs(15))
        .connect_timeout(Duration::from_secs(8))
        .pool_idle_timeout(Duration::from_secs(90))
        .tcp_nodelay(true)
}

/// Send a request, read the whole body, and stamp the moment it finished arriving.
pub async fn fetch_json(req: reqwest::RequestBuilder) -> Result<(Value, Ms)> {
    read_json(req.send().await?).await
}

/// Read a reply already received: the whole body as JSON, stamped when it finished arriving.
pub async fn read_json(resp: reqwest::Response) -> Result<(Value, Ms)> {
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
    let http = book.client();
    let mut backoff = Duration::ZERO;
    let mut streak = 0u32;
    let mut next = tokio::time::Instant::now();
    // The live score this book last showed per game (see `score_writes`).
    let mut last_score = std::collections::HashMap::new();
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
                let live: Vec<LiveScore> = snap.events.iter().filter(|e| e.live && e.score.is_some()).map(|e| (e.id.clone(), e.score.clone(), e.clock.clone())).collect();
                let recv_ms = snap.recv_ms;
                let writes = app.engine.lock().apply(name, snap, req_ms);
                app.emit(writes).await;
                // Live scores: straight to their own table, past the engine (research only).
                let scores = score_writes(name, recv_ms, live, &book.score_detail(), &mut last_score);
                if !scores.is_empty() {
                    app.emit(scores).await;
                }
                // Parts of games (maps, sets, halves): straight to their own table, past the engine.
                let parts = book.parts();
                if !parts.is_empty() {
                    let now = crate::model::now_ms();
                    let w = parts.into_iter().map(|p| crate::engine::Write::BookPart { ts_ms: now, book: name, book_event_id: p.event, kind: p.kind, line: p.line, outcome: p.outcome, price: p.price }).collect();
                    app.emit(w).await;
                }
                if !backoff.is_zero() {
                    tracing::info!("{name}: recovered");
                }
                backoff = Duration::ZERO;
                streak = 0;
            }
            Err(e) => {
                let msg = format!("{e:#}");
                app.engine.lock().poll_error(name, msg.clone());
                streak += 1;
                backoff = book.cool_down(&msg, streak).unwrap_or_else(|| (backoff * 2).clamp(Duration::from_secs(2), Duration::from_secs(60)));
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
        // Sportradar's Simulated Reality League. Novibet writes "Ѕimulated" with a Cyrillic Ѕ,
        // so match the rest of the word and the "SRL" tag on league or team names.
        || l.contains("imulated reality")
        || has_srl_token(&l)
        || has_srl_token(&teams)
}

fn has_srl_token(s: &str) -> bool {
    s.split(|c: char| !c.is_alphanumeric()).any(|t| t == "srl")
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

#[cfg(test)]
mod esoccer_tests {
    use super::*;

    fn ev(league: &str, home: &str, away: &str) -> EventIn {
        EventIn { league: league.into(), home: home.into(), away: away.into(), ..Default::default() }
    }

    #[test]
    fn simulated_reality_league_is_filtered_even_with_a_cyrillic_s() {
        assert!(is_esoccer(&ev("\u{0405}imulated Reality League / Netherlands - Eredivisie SRL", "Ajax Amsterdam (SRL)", "ADO Den Haag SRL")));
        assert!(is_esoccer(&ev("Netherlands / Eredivisie", "Sparta Rotterdam (SRL)", "AZ Alkmaar (SRL)")));
        assert!(!is_esoccer(&ev("Netherlands / Eredivisie", "Sparta Rotterdam", "AZ Alkmaar")));
    }
}
