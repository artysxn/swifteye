//! Polymarket: football moneyline markets (home / draw / away, each a Yes/No token).
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
/// Above this spread the mid says little (empty / one-sided book): store as suspended.
const MAX_SPREAD: f64 = 0.25;

#[derive(Clone, Debug)]
struct PmEvent {
    id: String,
    home: String,
    away: String,
    league: String,
    start_ms: Option<Ms>,
    game_id: Option<String>,
    /// (outcome, yes-token id)
    tokens: Vec<(Outcome, String)>,
}

#[derive(Clone, Copy, Default, PartialEq)]
struct Tok {
    bid: Option<f64>,
    ask: Option<f64>,
    last: Option<f64>,
    server_ts: Ms,
}

impl Tok {
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
                match discover(&http, prematch_min).await {
                    Ok(evs) => {
                        let changed = {
                            let cur = tx.borrow();
                            cur.len() != evs.len() || cur.iter().zip(evs.iter()).any(|(a, b)| a.id != b.id || a.tokens != b.tokens)
                        };
                        if changed {
                            tracing::info!("polymarket: {} live/upcoming football games", evs.len());
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
        let before = *t;
        f(t);
        if ts > 0 {
            t.server_ts = ts;
        }
        if t.bid != before.bid || t.ask != before.ask || t.last != before.last {
            changed.push(asset.to_string());
        }
    };
    match item["event_type"].as_str().unwrap_or("") {
        "book" => {
            let asset = text(&item["asset_id"]);
            let bid = item["bids"].as_array().into_iter().flatten().filter_map(|l| num(&l["price"])).fold(None, |m: Option<f64>, p| Some(m.map_or(p, |x| x.max(p))));
            let ask = item["asks"].as_array().into_iter().flatten().filter_map(|l| num(&l["price"])).fold(None, |m: Option<f64>, p| Some(m.map_or(p, |x| x.min(p))));
            let last = num(&item["last_trade_price"]);
            upd(&asset, &mut |t| {
                t.bid = bid;
                t.ask = ask;
                if last.is_some() {
                    t.last = last;
                }
            });
        }
        "price_change" => {
            for pc in item["price_changes"].as_array().into_iter().flatten() {
                let asset = text(&pc["asset_id"]);
                let (bid, ask) = (num(&pc["best_bid"]), num(&pc["best_ask"]));
                upd(&asset, &mut |t| {
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
            sport: "football".into(),
            league: e.league.clone(),
            home: e.home.clone(),
            away: e.away.clone(),
            start_ms: e.start_ms,
            sr_id: e.game_id.clone(),
            sr_weak: true,
            score: None,
            clock: None,
            quotes,
        });
    }
    let (mut writes, match_of): (Vec<Write>, HashMap<String, i64>) = {
        let mut eng = app.engine.lock();
        let w = eng.apply(NAME, Snapshot { events: evs, recv_ms: recv }, 0);
        let m = eng.books.get(NAME).map(|b| b.events.iter().map(|(k, v)| (k.clone(), v.match_id)).collect()).unwrap_or_default();
        (w, m)
    };
    // Raw top of book for the tokens that moved.
    let tok_index: BTreeMap<&str, (&PmEvent, Outcome)> =
        events.iter().flat_map(|e| e.tokens.iter().map(move |(o, t)| (t.as_str(), (e, *o)))).collect();
    for tid in changed {
        if let (Some(t), Some((e, o))) = (toks.get(tid), tok_index.get(tid.as_str())) {
            writes.push(Write::PmQuote {
                ts_ms: recv,
                server_ts_ms: t.server_ts,
                match_id: match_of.get(&e.id).copied().unwrap_or(0),
                event_id: e.id.clone(),
                outcome: *o,
                bid: t.bid,
                ask: t.ask,
                last: t.last,
            });
        }
    }
    app.emit(writes).await;
}

async fn discover(http: &reqwest::Client, prematch_min: i64) -> Result<Vec<PmEvent>> {
    let now = now_ms();
    let fmt = |ms: Ms| {
        time::OffsetDateTime::from_unix_timestamp(ms / 1000)
            .map(|t| t.format(&time::format_description::well_known::Rfc3339).unwrap_or_default())
            .unwrap_or_default()
    };
    let from = fmt(now - 3 * 3600 * 1000);
    let to = fmt(now + prematch_min * 60 * 1000);
    let mut out = Vec::new();
    for page in 0..10 {
        let url = format!(
            "{GAMMA}/events?tag_id={SOCCER_TAG}&closed=false&limit=100&offset={}&start_time_min={from}&start_time_max={to}",
            page * 100
        );
        let (v, _) = fetch_json(http.get(url)).await?;
        let list = v.as_array().cloned().unwrap_or_default();
        for e in &list {
            if let Some(pe) = parse_event(e) {
                out.push(pe);
            }
        }
        if list.len() < 100 {
            break;
        }
    }
    out.sort_by(|a, b| a.id.cmp(&b.id));
    Ok(out)
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
        home,
        away,
        league: text(&e["seriesSlug"]),
        start_ms: e["startTime"].as_str().and_then(rfc3339_ms),
        game_id: int(&e["gameId"]).filter(|g| *g > 0).map(|g| g.to_string()),
        tokens,
    })
}
