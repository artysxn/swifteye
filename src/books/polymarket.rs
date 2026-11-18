//! Polymarket: football moneyline markets (home / draw / away, each a Yes/No token) and
//! e-sports match winners (one market, one token per team).
//!
//! * Discovery: Gamma API every 60 s, games kicking off between 3 h ago and 30 min ahead.
//! * Prices: the public CLOB market WebSocket (push, server timestamps in ms).
//!
//! The "price" stored in the shared change log is decimal odds = 1 / mid(best bid, best ask)
//! of the Yes token, so it sits next to the bookmakers. Raw bid/ask/last per token with
//! Polymarket's own timestamp go to the `pm_quotes` table.

use super::*;
use crate::engine::Write;
use crate::matcher::Names;
use futures::{SinkExt, StreamExt};
use std::collections::{BTreeMap, HashMap};
use tokio::sync::watch;
use tokio_tungstenite::tungstenite::Message;

pub const NAME: &str = "polymarket";
const WS_URL: &str = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
const GAMMA: &str = "https://gamma-api.polymarket.com";
/// Gamma tag shared by every Polymarket soccer league.
const SOCCER_TAG: &str = "100350";
/// Gamma tag shared by every Polymarket e-sports title (FIFA/eFootball included, filtered out).
const ESPORTS_TAG: &str = "64";
/// Gamma tags of the two-way sports: (tag, sport key, hours a game can still be running).
const SPORT_TAGS: &[(&str, &str, i64)] = &[
    ("28", "basketball", 4),
    ("864", "tennis", 6),
    ("100088", "hockey", 4),
    ("678", "baseball", 5),
    ("102883", "volleyball", 3),
];
/// Above this spread the mid says little (empty / one-sided book): store as suspended.
const MAX_SPREAD: f64 = 0.25;

#[derive(Clone, Debug)]
struct PmEvent {
    id: String,
    /// "football" or an e-sports key (see `esport_key`).
    sport: String,
    home: String,
    away: String,
    league: String,
    start_ms: Option<Ms>,
    game_id: Option<String>,
    /// (outcome, yes-token id)
    tokens: Vec<(Outcome, String)>,
    /// Used to pick one event when Polymarket lists the same game twice.
    liquidity: f64,
}

#[derive(Clone, Default)]
struct Tok {
    bid: Option<f64>,
    ask: Option<f64>,
    last: Option<f64>,
    server_ts: Ms,
    /// Full book from `book` snapshots and `price_change` level updates: price (x1000) -> size.
    bids: std::collections::BTreeMap<i64, f64>,
    asks: std::collections::BTreeMap<i64, f64>,
    /// Depth last written, and when (size-only changes are written at most every 2 s).
    depth: crate::engine::Depth,
    depth_ms: Ms,
}

const DEPTH_EVERY_MS: Ms = 2_000;

fn pkey(p: f64) -> i64 {
    (p * 1000.0).round() as i64
}

impl Tok {
    fn depth_now(&self) -> crate::engine::Depth {
        if self.bids.is_empty() && self.asks.is_empty() {
            return crate::engine::Depth::default();
        }
        let lv = |m: &std::collections::BTreeMap<i64, f64>| m.iter().map(|(k, s)| (*k as f64 / 1000.0, *s)).collect::<Vec<_>>();
        crate::engine::Depth::from_levels(&lv(&self.bids), &lv(&self.asks))
    }
    fn mid(&self) -> Option<f64> {
        let (b, a) = (self.bid?, self.ask?);
        if a <= b || a - b > MAX_SPREAD {
            return None;
        }
        Some((a + b) / 2.0)
    }
}

pub fn source() -> String {
    "Polymarket CLOB market WebSocket (mid of Yes token); games from Gamma soccer tag".into()
}

pub async fn run(app: Arc<App>, prematch_min: i64) {
    let http = http_client();
    let (tx, mut rx) = watch::channel::<Arc<Vec<PmEvent>>>(Arc::new(Vec::new()));

    // Discovery loop.
    {
        let http = http.clone();
        let app = app.clone();
        tokio::spawn(async move {
            loop {
                match discover(&http, prematch_min, &app.cfg).await {
                    Ok(evs) => {
                        let changed = {
                            let cur = tx.borrow();
                            cur.len() != evs.len() || cur.iter().zip(evs.iter()).any(|(a, b)| a.id != b.id || a.tokens != b.tokens)
                        };
                        if changed {
                            tracing::info!("polymarket: {} live/upcoming games ({} e-sports, {} other sports)", evs.len(), evs.iter().filter(|e| is_esports_sport(&e.sport)).count(), evs.iter().filter(|e| e.sport != "football" && !is_esports_sport(&e.sport)).count());
                            let _ = tx.send(Arc::new(evs));
                        }
                    }
                    Err(e) => {
                        tracing::warn!("polymarket discovery: {e:#}");
                        app.engine.lock().poll_error(NAME, format!("discovery: {e:#}"));
                    }
                }
                tokio::time::sleep(Duration::from_secs(60)).await;
            }
        });
    }

    let mut toks: HashMap<String, Tok> = HashMap::new();
    loop {
        let events = rx.borrow_and_update().clone();
        if events.is_empty() {
            // Nothing to watch; publish an empty snapshot so finished games close, then wait.
            apply(&app, &events, &toks, now_ms(), &[]).await;
            let _ = rx.changed().await;
            continue;
        }
        let ids: Vec<String> = events.iter().flat_map(|e| e.tokens.iter().map(|t| t.1.clone())).collect();
        match stream(&app, &events, &ids, &mut toks, &mut rx).await {
            Ok(()) => {} // game list changed: resubscribe
            Err(e) => {
                tracing::warn!("polymarket ws: {e:#}");
                app.engine.lock().poll_error(NAME, format!("websocket: {e:#}"));
                tokio::time::sleep(Duration::from_secs(3)).await;
            }
        }
    }
}

async fn stream(
    app: &Arc<App>,
    events: &Arc<Vec<PmEvent>>,
    ids: &[String],
    toks: &mut HashMap<String, Tok>,
    rx: &mut watch::Receiver<Arc<Vec<PmEvent>>>,
) -> Result<()> {
    let (mut ws, _) = tokio_tungstenite::connect_async(WS_URL).await?;
    let sub = serde_json::json!({"assets_ids": ids, "type": "market", "custom_feature_enabled": true});
    ws.send(Message::Text(sub.to_string().into())).await?;
    let wanted: std::collections::HashSet<&str> = ids.iter().map(|s| s.as_str()).collect();
    let mut ping = tokio::time::interval(Duration::from_secs(10));
    let mut last_msg = std::time::Instant::now();
    loop {
        tokio::select! {
            m = ws.next() => {
                let Some(m) = m else { anyhow::bail!("closed by server") };
                let m = m?;
                let recv = now_ms();
                last_msg = std::time::Instant::now();
                let text = match m {
                    Message::Text(t) => t.to_string(),
                    Message::Binary(b) => String::from_utf8_lossy(&b).into_owned(),
                    Message::Ping(p) => { ws.send(Message::Pong(p)).await?; continue; }
                    Message::Close(_) => anyhow::bail!("closed by server"),
                    _ => continue,
                };
                if text == "PONG" || text.is_empty() { continue; }
                let Ok(v) = serde_json::from_str::<Value>(&text) else { continue };
                let mut changed: Vec<String> = Vec::new();
                for item in v.as_array().cloned().unwrap_or_else(|| vec![v.clone()]) {
                    absorb(&item, toks, &wanted, &mut changed);
                }
                if !changed.is_empty() {
                    apply(app, events, toks, recv, &changed).await;
                }
            }
            _ = ping.tick() => {
                ws.send(Message::Text("PING".into())).await?;
                if last_msg.elapsed() > Duration::from_secs(60) {
                    anyhow::bail!("no data for 60 s");
                }
            }
            r = rx.changed() => {
                r?;
                let _ = ws.close(None).await;
                return Ok(());
            }
        }
    }
}

/// Fold one WebSocket event into token state; remember which tokens changed.
fn absorb(item: &Value, toks: &mut HashMap<String, Tok>, wanted: &std::collections::HashSet<&str>, changed: &mut Vec<String>) {
    let ts = int(&item["timestamp"]).unwrap_or(0);
    let mut upd = |asset: &str, f: &mut dyn FnMut(&mut Tok)| {
        if !wanted.contains(asset) {
            return;
        }
        let t = toks.entry(asset.to_string()).or_default();
        let before = (t.bid, t.ask, t.last);
        f(t);
        if ts > 0 {
            t.server_ts = ts;
        }
        // Levels inside the spread are stale (an update we missed): the feed's best prices win.
        if let Some(b) = t.bid {
            let k = pkey(b);
            t.bids.retain(|p, _| *p <= k);
        }
        if let Some(a) = t.ask {
            let k = pkey(a);
            t.asks.retain(|p, _| *p >= k);
        }
        let now = now_ms();
        let d = t.depth_now();
        let price_moved = (t.bid, t.ask, t.last) != before;
        let size_moved = d.differs(&t.depth) && now - t.depth_ms >= DEPTH_EVERY_MS;
        if price_moved || size_moved {
            t.depth = d;
            t.depth_ms = now;
            changed.push(asset.to_string());
        }
    };
    let levels = |v: &Value| -> std::collections::BTreeMap<i64, f64> {
        v.as_array().into_iter().flatten().filter_map(|l| Some((pkey(num(&l["price"])?), num(&l["size"])?))).filter(|l| l.1 > 0.0).collect()
    };
    match item["event_type"].as_str().unwrap_or("") {
        "book" => {
            let asset = text(&item["asset_id"]);
            let (bids, asks) = (levels(&item["bids"]), levels(&item["asks"]));
            let bid = bids.keys().next_back().map(|k| *k as f64 / 1000.0);
            let ask = asks.keys().next().map(|k| *k as f64 / 1000.0);
            let last = num(&item["last_trade_price"]);
            upd(&asset, &mut |t| {
                t.bid = bid;
                t.ask = ask;
                t.bids = bids.clone();
                t.asks = asks.clone();
                if last.is_some() {
                    t.last = last;
                }
            });
        }
        "price_change" => {
            for pc in item["price_changes"].as_array().into_iter().flatten() {
                let asset = text(&pc["asset_id"]);
                let (bid, ask) = (num(&pc["best_bid"]), num(&pc["best_ask"]));
                // One level's new total size (0 = gone); side BUY = bids, SELL = asks.
                let level = num(&pc["price"]).zip(num(&pc["size"]));
                let side = pc["side"].as_str().unwrap_or("").to_ascii_uppercase();
                upd(&asset, &mut |t| {
                    if let Some((p, s)) = level {
                        let book = match side.as_str() {
                            "BUY" => Some(&mut t.bids),
                            "SELL" => Some(&mut t.asks),
                            _ => None,
                        };
                        if let Some(book) = book {
                            if s > 0.0 {
                                book.insert(pkey(p), s);
                            } else {
                                book.remove(&pkey(p));
                            }
                        }
                    }
                    if pc.get("best_bid").is_some() {
                        t.bid = bid.filter(|x| *x > 0.0);
                    }
                    if pc.get("best_ask").is_some() {
                        t.ask = ask.filter(|x| *x > 0.0 && *x < 1.0);
                    }
                });
            }
        }
        "best_bid_ask" => {
            let asset = text(&item["asset_id"]);
            let (bid, ask) = (num(&item["best_bid"]), num(&item["best_ask"]));
            upd(&asset, &mut |t| {
                t.bid = bid.filter(|x| *x > 0.0);
                t.ask = ask.filter(|x| *x > 0.0 && *x < 1.0);
            });
        }
        "last_trade_price" => {
            let asset = text(&item["asset_id"]);
            let p = num(&item["price"]);
            upd(&asset, &mut |t| t.last = p);
        }
        _ => {}
    }
}

/// Publish the current state of every watched game as one snapshot.
async fn apply(app: &Arc<App>, events: &[PmEvent], toks: &HashMap<String, Tok>, recv: Ms, changed: &[String]) {
    if app.engine.lock().schedule(NAME).0 {
        return; // paused from the control panel
    }
    let mut evs = Vec::with_capacity(events.len());
    for e in events {
        let mut quotes = Vec::new();
        for (o, tid) in &e.tokens {
            let Some(t) = toks.get(tid) else { continue };
            let price = t.mid().map(|m| ((1.0 / m) * 10_000.0).round() / 10_000.0);
            quotes.push(QuoteIn::new(Market::X12, None, *o, price).ts((t.server_ts > 0).then_some(t.server_ts)));
        }
        evs.push(EventIn {
            id: e.id.clone(),
            sport: e.sport.clone(),
            league: e.league.clone(),
            home: e.home.clone(),
            away: e.away.clone(),
            start_ms: e.start_ms,
            // Polymarket's e-sports gameId is its own counter, not Sportradar's.
            sr_id: if e.sport == "football" { e.game_id.clone() } else { None },
            sr_weak: true,
            live: e.start_ms.is_some_and(|s| s <= now_ms()),
            score: None,
            clock: None,
            quotes,
        });
    }
    let (mut writes, match_of): (Vec<Write>, HashMap<String, (i64, bool)>) = {
        let mut eng = app.engine.lock();
        let w = eng.apply(NAME, Snapshot { events: evs, recv_ms: recv }, 0);
        let m = eng.books.get(NAME).map(|b| b.events.iter().map(|(k, v)| (k.clone(), (v.match_id, v.swap))).collect()).unwrap_or_default();
        (w, m)
    };
    // Raw top of book for the tokens that moved.
    let tok_index: BTreeMap<&str, (&PmEvent, Outcome)> =
        events.iter().flat_map(|e| e.tokens.iter().map(move |(o, t)| (t.as_str(), (e, *o)))).collect();
    for tid in changed {
        if let (Some(t), Some((e, o))) = (toks.get(tid), tok_index.get(tid.as_str())) {
            let (match_id, swap) = match_of.get(&e.id).copied().unwrap_or((0, false));
            // Stored against the match's orientation, like every other quote.
            let outcome = match (swap, *o) {
                (true, Outcome::Home) => Outcome::Away,
                (true, Outcome::Away) => Outcome::Home,
                (_, o) => o,
            };
            writes.push(Write::PmQuote {
                ts_ms: recv,
                server_ts_ms: t.server_ts,
                match_id,
                event_id: e.id.clone(),
                outcome,
                bid: t.bid,
                ask: t.ask,
                last: t.last,
                depth: t.depth,
            });
        }
    }
    app.emit(writes).await;
}

async fn discover(http: &reqwest::Client, prematch_min: i64, cfg: &crate::Config) -> Result<Vec<PmEvent>> {
    let now = now_ms();
    let fmt = |ms: Ms| {
        time::OffsetDateTime::from_unix_timestamp(ms / 1000)
            .map(|t| t.format(&time::format_description::well_known::Rfc3339).unwrap_or_default())
            .unwrap_or_default()
    };
    let to = fmt(now + prematch_min * 60 * 1000);
    let mut out = Vec::new();
    // (tag, how far back to look): a football game is over within 3 h, a BO5 can run longer.
    // (tag, sport key, how far back to look): a football game is over within 3 h, a BO5 can run longer.
    let mut tags = Vec::new();
    if cfg.follows("football") {
        tags.push((SOCCER_TAG, "football", 3i64));
    }
    if cfg.esports {
        tags.push((ESPORTS_TAG, "esports", 6));
    }
    tags.extend(SPORT_TAGS.iter().copied().filter(|t| cfg.follows(t.1)));
    for (tag, sport, back_h) in tags {
        let from = fmt(now - back_h * 3600 * 1000);
        for page in 0..10 {
            let url = format!(
                "{GAMMA}/events?tag_id={tag}&closed=false&limit=100&offset={}&start_time_min={from}&start_time_max={to}",
                page * 100
            );
            let (v, _) = match fetch_json(http.get(url)).await {
                Ok(x) => x,
                // A failing side page must not take the football games down with it.
                Err(e) if tag != SOCCER_TAG => {
                    tracing::warn!("polymarket {sport} discovery: {e:#}");
                    break;
                }
                Err(e) => return Err(e),
            };
            let list = v.as_array().cloned().unwrap_or_default();
            for e in &list {
                let pe = match sport {
                    "football" => parse_event(e),
                    "esports" => parse_two_way(e, None),
                    s => parse_two_way(e, Some(s)),
                };
                if let Some(pe) = pe {
                    crate::predmeta::set(
                        NAME,
                        &pe.id,
                        crate::predmeta::Size {
                            liquidity: num(&e["liquidity"]).unwrap_or(0.0),
                            volume: num(&e["volume"]).unwrap_or(0.0),
                            volume_24h: num(&e["volume24hr"]).unwrap_or(0.0),
                        },
                    );
                    out.push(pe);
                }
            }
            if list.len() < 100 {
                break;
            }
        }
    }
    // Polymarket sometimes lists one game as two events; keep the more liquid one so
    // the bookmakers do not get split between two matches.
    out.sort_by(|a, b| b.liquidity.partial_cmp(&a.liquidity).unwrap_or(std::cmp::Ordering::Equal));
    let mut seen = std::collections::HashSet::new();
    out.retain(|e| seen.insert((e.sport.clone(), e.home.to_lowercase(), e.away.to_lowercase(), e.start_ms.map(|s| s / 3_600_000))));
    out.sort_by(|a, b| a.id.cmp(&b.id));
    Ok(out)
}

/// Two-way games: one "moneyline" market with two tokens (one per team / player), no draw.
/// E-sports title: "Counter-Strike: A vs B (BO3) - Tournament" (`sport` = None, read from the title).
/// Others: "Magic vs. Celtics" (NBA, NHL), "ATP Finals, Group X: Alexander Zverev vs Ben Shelton".
fn parse_two_way(e: &Value, sport: Option<&'static str>) -> Option<PmEvent> {
    if e["closed"].as_bool() == Some(true) {
        return None;
    }
    let title = text(&e["title"]);
    let slug = text(&e["seriesSlug"]);
    let game = title.split_once(": ").map(|(g, _)| g.to_string()).unwrap_or_default();
    let sport = match sport {
        Some(s) => s,
        None => esport_key(&slug).or_else(|| esport_key(&game))?,
    };
    let esports = is_esports_sport(sport);
    let rest = title.split_once(": ").map(|(_, r)| r).unwrap_or(&title);
    // Child events of traditional sports ("A vs. B - More Markets") repeat the game.
    if !esports && rest.contains(" - ") {
        return None;
    }
    let (teams, tournament) = match rest.split_once(" - ") {
        Some((t, l)) => (t, l.trim().to_string()),
        None => (rest, if esports { String::new() } else { game.clone() }),
    };
    // Drop the "(BO3)" suffix.
    let teams = teams.rsplit_once(" (BO").map(|(t, _)| t).unwrap_or(teams);
    let (home, away) = teams.split_once(" vs. ").or_else(|| teams.split_once(" vs "))?;
    let (home, away) = (home.trim().to_string(), away.trim().to_string());
    let (hn, an) = (Names::new(&home), Names::new(&away));
    let mut tokens = Vec::new();
    for m in e["markets"].as_array().into_iter().flatten() {
        if m["sportsMarketType"].as_str() != Some("moneyline") || m["closed"].as_bool() == Some(true) {
            continue;
        }
        let outs: Vec<String> = m["outcomes"].as_str().and_then(|s| serde_json::from_str(s).ok()).unwrap_or_default();
        let ids: Vec<String> = m["clobTokenIds"].as_str().and_then(|s| serde_json::from_str(s).ok()).unwrap_or_default();
        for (name, id) in outs.iter().zip(ids.iter()) {
            let n = Names::new(name);
            let (sh, sa) = (hn.sim(&n), an.sim(&n));
            if sh.max(sa) < 0.5 {
                continue;
            }
            let outcome = if sh >= sa { Outcome::Home } else { Outcome::Away };
            if !tokens.iter().any(|(o, _)| *o == outcome) {
                tokens.push((outcome, id.clone()));
            }
        }
        break;
    }
    if tokens.len() != 2 {
        return None;
    }
    tokens.sort_by_key(|t| t.0);
    Some(PmEvent {
        id: text(&e["id"]),
        sport: sport.into(),
        home,
        away,
        league: if tournament.is_empty() { slug } else { tournament },
        start_ms: e["startTime"].as_str().and_then(rfc3339_ms),
        game_id: None,
        liquidity: num(&e["liquidity"]).unwrap_or(0.0),
        tokens,
    })
}

fn parse_event(e: &Value) -> Option<PmEvent> {
    let title = text(&e["title"]);
    // Child events ("X vs. Y - Exact Score", "- More Markets") repeat the game; skip them.
    if title.contains(" - ") || e["closed"].as_bool() == Some(true) {
        return None;
    }
    let (home, away) = title.split_once(" vs. ").or_else(|| title.split_once(" vs "))?;
    let (home, away) = (home.trim().to_string(), away.trim().to_string());
    let (hn, an) = (Names::new(&home), Names::new(&away));
    let mut tokens = Vec::new();
    for m in e["markets"].as_array().into_iter().flatten() {
        if m["sportsMarketType"].as_str() != Some("moneyline") || m["closed"].as_bool() == Some(true) {
            continue;
        }
        let item = text(&m["groupItemTitle"]);
        let outcome = if item.starts_with("Draw") {
            Outcome::Draw
        } else {
            let n = Names::new(&item);
            let (sh, sa) = (hn.sim(&n), an.sim(&n));
            if sh.max(sa) < 0.5 {
                continue;
            }
            if sh >= sa {
                Outcome::Home
            } else {
                Outcome::Away
            }
        };
        let outs: Vec<String> = m["outcomes"].as_str().and_then(|s| serde_json::from_str(s).ok()).unwrap_or_default();
        let ids: Vec<String> = m["clobTokenIds"].as_str().and_then(|s| serde_json::from_str(s).ok()).unwrap_or_default();
        let yes = outs.iter().position(|o| o.eq_ignore_ascii_case("yes")).unwrap_or(0);
        if let Some(t) = ids.get(yes) {
            if !tokens.iter().any(|(o, _)| *o == outcome) {
                tokens.push((outcome, t.clone()));
            }
        }
    }
    if tokens.is_empty() {
        return None;
    }
    tokens.sort_by_key(|t| t.0);
    Some(PmEvent {
        id: text(&e["id"]),
        sport: "football".into(),
        home,
        away,
        league: text(&e["seriesSlug"]),
        start_ms: e["startTime"].as_str().and_then(rfc3339_ms),
        game_id: int(&e["gameId"]).filter(|g| *g > 0).map(|g| g.to_string()),
        liquidity: num(&e["liquidity"]).unwrap_or(0.0),
        tokens,
    })
}
