//! Kalshi: football game markets (home / tie / away, each a Yes contract), as a second
//! prediction-market baseline next to Polymarket.
//!
//! * Discovery (background task, every 2 min): the Sports series tagged Soccer whose ticker
//!   ends in GAME, then each series' open events. Games kicking off between 3 h ago and
//!   `hours` ahead are kept.
//! * Prices (every poll): one batched `GET /markets?tickers=...` per 100 contracts.
//!   Price stored = decimal odds from the mid of the Yes bid/ask.
//!
//! Public market data; no key needed. Kalshi's WebSocket needs an API key, so this polls.

use super::*;
use crate::matcher::Names;
use parking_lot::Mutex;
use std::collections::HashMap;

pub const NAME: &str = "kalshi";
const API: &str = "https://api.elections.kalshi.com/trade-api/v2";
const MAX_SPREAD: f64 = 0.25;

#[derive(Clone, Debug)]
struct KGame {
    event: String,
    /// "football" or an e-sports key.
    sport: String,
    home: String,
    away: String,
    league: String,
    start_ms: Option<Ms>,
    /// (outcome, market ticker)
    tickers: Vec<(Outcome, String)>,
}

pub struct Kalshi {
    games: Arc<Mutex<Vec<KGame>>>,
}

impl Kalshi {
    /// Starts the discovery task and returns the poller.
    pub fn new(hours: f64, esports: bool) -> Self {
        let games = Arc::new(Mutex::new(Vec::new()));
        let g = games.clone();
        tokio::spawn(async move {
            let http = http_client();
            let mut series: Vec<(String, String, &'static str)> = Vec::new();
            let mut series_at: Ms = 0;
            loop {
                if series.is_empty() || now_ms() - series_at > 6 * 3_600_000 {
                    match game_series(&http, esports).await {
                        Ok(s) => {
                            tracing::info!("kalshi: {} game series ({} e-sports)", s.len(), s.iter().filter(|x| x.2 != "football").count());
                            series = s;
                            series_at = now_ms();
                        }
                        Err(e) => tracing::warn!("kalshi series: {e:#}"),
                    }
                }
                match discover(&http, &series, hours).await {
                    Ok(found) => {
                        let n = found.len();
                        let changed = g.lock().len() != n;
                        *g.lock() = found;
                        if changed {
                            tracing::info!("kalshi: {n} games in the window");
                        }
                    }
                    Err(e) => tracing::warn!("kalshi discovery: {e:#}"),
                }
                tokio::time::sleep(Duration::from_secs(120)).await;
            }
        });
        Kalshi { games }
    }
}

/// (ticker, title, sport key) of every Kalshi series that prices one game between two sides.
async fn game_series(http: &reqwest::Client, esports: bool) -> Result<Vec<(String, String, &'static str)>> {
    let (v, _) = fetch_json(http.get(format!("{API}/series?category=Sports&limit=5000"))).await?;
    let mut out = Vec::new();
    for s in v["series"].as_array().into_iter().flatten() {
        let (ticker, title) = (text(&s["ticker"]), text(&s["title"]));
        let tags: Vec<&str> = s["tags"].as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
        if tags.contains(&"Soccer") && ticker.ends_with("GAME") && !ticker.contains("GOALEVERY") {
            out.push((ticker, title, "football"));
        } else if esports && tags.contains(&"Esports") && (ticker.ends_with("GAME") || ticker.ends_with("GAME3WAY")) {
            // "KXCS2GAME" = whole match winner; "...MAP" series (single maps) end differently and are skipped.
            if let Some(k) = esport_key(&title).or_else(|| esport_key(&ticker)) {
                out.push((ticker, title, k));
            }
        }
    }
    Ok(out)
}

async fn discover(http: &reqwest::Client, series: &[(String, String, &'static str)], hours: f64) -> Result<Vec<KGame>> {
    let now = now_ms();
    let hi = now + (hours.max(0.5) * 3_600_000.0) as i64;
    let mut out = Vec::new();
    // A few series at a time keeps us well inside Kalshi's public rate limit.
    for chunk in series.chunks(6) {
        let futs = chunk.iter().map(|(t, title, sport)| {
            let url = format!("{API}/events?series_ticker={t}&status=open&with_nested_markets=true&limit=200");
            let title = title.clone();
            let sport = *sport;
            async move { fetch_json(http.get(url)).await.map(|(v, _)| (v, title, sport)) }
        });
        for res in futures::future::join_all(futs).await {
            let Ok((v, series_title, sport)) = res else { continue };
            // A football game is over within 3 h; a best-of-five can run longer.
            let lo = now - if sport == "football" { 3 } else { 6 } * 3_600_000;
            for e in v["events"].as_array().into_iter().flatten() {
                if let Some(g) = parse_event(e, &series_title, sport) {
                    if g.start_ms.is_some_and(|s| s >= lo && s <= hi) {
                        out.push(g);
                    }
                }
            }
        }
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
    out.sort_by(|a, b| a.event.cmp(&b.event));
    Ok(out)
}

fn parse_event(e: &Value, series_title: &str, sport: &'static str) -> Option<KGame> {
    let mut title = text(&e["title"]);
    // E-sports titles carry the tournament in front: "OCS Asia Stage 3 2026: VARREL vs. T1".
    if sport != "football" {
        if let Some((_, rest)) = title.clone().rsplit_once(": ") {
            title = rest.to_string();
        }
    }
    let (home, away) = title.split_once(" vs ").or_else(|| title.split_once(" vs. ")).or_else(|| title.split_once(" v "))?;
    let (home, away) = (home.trim().to_string(), away.trim().to_string());
    let (hn, an) = (Names::new(&home), Names::new(&away));
    let mut tickers = Vec::new();
    let mut start = None;
    for m in e["markets"].as_array().into_iter().flatten() {
        if m["status"].as_str().is_some_and(|s| s != "active") {
            continue;
        }
        let sub = text(&m["yes_sub_title"]);
        let outcome = if sub.eq_ignore_ascii_case("tie") || sub.eq_ignore_ascii_case("draw") {
            Outcome::Draw
        } else {
            let n = Names::new(&sub);
            let (sh, sa) = (hn.sim(&n), an.sim(&n));
            if sh.max(sa) < 0.5 {
                continue;
            }
            if sh >= sa { Outcome::Home } else { Outcome::Away }
        };
        start = start.or_else(|| m["occurrence_datetime"].as_str().and_then(rfc3339_ms));
        if !tickers.iter().any(|(o, _)| *o == outcome) {
            tickers.push((outcome, text(&m["ticker"])));
        }
    }
    if tickers.is_empty() {
        return None;
    }
    let comp = e["product_metadata"]["competition"].as_str().unwrap_or("");
    Some(KGame {
        event: text(&e["event_ticker"]),
        sport: sport.into(),
        home,
        away,
        league: if comp.is_empty() { series_title.replace(" Game", "") } else { comp.to_string() },
        start_ms: start,
        tickers,
    })
}

impl Book for Kalshi {
    fn name(&self) -> &'static str {
        NAME
    }
    fn source(&self) -> String {
        "Kalshi football + e-sports game markets (mid of Yes bid/ask), batched REST".into()
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let games = self.games.lock().clone();
        let all: Vec<&str> = games.iter().flat_map(|g| g.tickers.iter().map(|t| t.1.as_str())).collect();
        let mut prices: HashMap<String, (Option<f64>, Option<f64>)> = HashMap::new();
        let mut recv_ms = now_ms();
        for chunk in all.chunks(100) {
            let (v, t) = fetch_json(http.get(format!("{API}/markets?tickers={}&limit=200", chunk.join(",")))).await?;
            recv_ms = recv_ms.max(t);
            for m in v["markets"].as_array().into_iter().flatten() {
                let active = m["status"].as_str() == Some("active");
                let bid = num(&m["yes_bid_dollars"]).filter(|x| *x > 0.0 && active);
                let ask = num(&m["yes_ask_dollars"]).filter(|x| *x > 0.0 && *x < 1.0 && active);
                prices.insert(text(&m["ticker"]), (bid, ask));
            }
        }
        let now = now_ms();
        let events = games
            .iter()
            .map(|g| EventIn {
                id: g.event.clone(),
                sport: g.sport.clone(),
                league: g.league.clone(),
                home: g.home.clone(),
                away: g.away.clone(),
                start_ms: g.start_ms,
                sr_id: None,
                sr_weak: false,
                live: g.start_ms.is_some_and(|s| s <= now),
                score: None,
                clock: None,
                quotes: g
                    .tickers
                    .iter()
                    .map(|(o, t)| {
                        let mid = match prices.get(t) {
                            Some((Some(b), Some(a))) if a > b && a - b <= MAX_SPREAD => Some((a + b) / 2.0),
                            _ => None,
                        };
                        QuoteIn::new(Market::X12, None, *o, mid.map(|m| ((1.0 / m) * 10_000.0).round() / 10_000.0))
                    })
                    .collect(),
            })
            .collect();
        Ok(Snapshot { events, recv_ms })
    }
}
