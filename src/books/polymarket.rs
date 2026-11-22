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
use futures::{FutureExt, SinkExt, StreamExt};
use std::collections::HashMap;
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
    // NFL and college football: their events carry no tag shared by all of American football.
    ("450", "amfootball", 5),
    ("100351", "amfootball", 5),
    // UFC: every fight of a card is listed at the card's start time, so look back a whole card.
    ("279", "mma", 8),
];
/// Above this spread the mid says little (empty / one-sided book): store as suspended.
const MAX_SPREAD: f64 = 0.25;

// ---------------------------------------------------------------- sister markets (recorded only)
// Every market of a game other than its winner market: map winners, totals, handicaps, spreads.
// They are RECORDED ONLY. They have their own connection (`run_sister`), their own token state and
// their own table (`pm_sister_quotes`). Nothing of theirs reaches the engine, the change broadcast
// the bots listen to, the venue order books the bots trade from, or the feed-delay measure.
/// Market types kept for e-sports games (all of a game's maps, its totals and its map handicap).
const SISTER_ESPORTS: &[&str] = &["child_moneyline", "totals", "map_handicap"];
/// Market types kept for basketball and American football, which list dozens of lines each:
/// only the `SISTER_LINES` line of each type priced nearest to even (a live game's totals tick
/// every possession: two lines a type made 4 million rows a day).
const SISTER_BALL: &[&str] = &["spreads", "totals"];
const SISTER_LINES: usize = 1;
/// A sister quote whose prices did not change (only the sizes did) is stored at most this often.
const SISTER_SIZE_EVERY_MS: Ms = 30_000;
/// The sister connection stops for `SISTER_PAUSE` when the winner markets' feed has been this late
/// on three checks in a row (10 s apart): that feed and the bots on it come first.
const SISTER_YIELD_MS: Ms = 500;
const SISTER_PAUSE: Duration = Duration::from_secs(600);

/// One sister market: the token recorded (its other token is the mirror image).
#[derive(Clone, Debug, PartialEq)]
struct Sister {
    token: String,
    /// Polymarket's own market type ("child_moneyline", "totals", "spreads", "map_handicap").
    kind: String,
    line: Option<f64>,
    /// The outcome the recorded token pays on, as Polymarket names it.
    outcome: String,
}

/// "Counter-Strike: A vs B - Map 2 Winner" -> 2.
fn map_no(question: &str) -> Option<f64> {
    let i = question.rfind("Map ")?;
    question[i + 4..].chars().next()?.to_digit(10).map(f64::from)
}

/// The sister markets of one Gamma event worth recording.
fn sisters(e: &Value, sport: &str) -> Vec<Sister> {
    let esports = is_esports_sport(sport);
    let kinds: &[&str] = if esports {
        SISTER_ESPORTS
    } else if matches!(sport, "basketball" | "amfootball") {
        SISTER_BALL
    } else {
        return Vec::new();
    };
    // (distance of the price from even, the market)
    let mut out: Vec<(f64, Sister)> = Vec::new();
    for m in e["markets"].as_array().into_iter().flatten() {
        let Some(kind) = m["sportsMarketType"].as_str().filter(|k| kinds.contains(k)) else { continue };
        if m["closed"].as_bool() == Some(true) || m["active"].as_bool() == Some(false) {
            continue;
        }
        let outs: Vec<String> = m["outcomes"].as_str().and_then(|s| serde_json::from_str(s).ok()).unwrap_or_default();
        let ids: Vec<String> = m["clobTokenIds"].as_str().and_then(|s| serde_json::from_str(s).ok()).unwrap_or_default();
        if outs.len() != 2 || ids.len() != 2 {
            continue;
        }
        let mid = num(&m["bestBid"]).zip(num(&m["bestAsk"])).map(|(b, a)| (a + b) / 2.0).or_else(|| num(&m["lastTradePrice"])).unwrap_or(0.5);
        // A map-winner market carries its map number only in its name ("... - Map 2 Winner").
        let line = num(&m["line"]).or_else(|| if kind == "child_moneyline" { map_no(&text(&m["question"])) } else { None });
        out.push(((mid - 0.5).abs(), Sister { token: ids[0].clone(), kind: kind.to_string(), line, outcome: outs[0].clone() }));
    }
    if !esports {
        out.sort_by(|a, b| a.0.total_cmp(&b.0));
        let mut n: HashMap<String, usize> = HashMap::new();
        out.retain(|(_, s)| {
            let c = n.entry(s.kind.clone()).or_default();
            *c += 1;
            *c <= SISTER_LINES
        });
    }
    out.into_iter().map(|x| x.1).collect()
}

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
    /// Per token: the market's fee schedule and settlement rules (for the paper arbitrage).
    meta: Vec<(String, crate::venue::Meta)>,
    /// The game's other markets, recorded only (see `Sister`).
    sister: Vec<Sister>,
}

/// A market's fee and rules. `feesEnabled: false` = free; a schedule without a rate = unknown.
fn market_meta(m: &Value) -> crate::venue::Meta {
    let s = &m["feeSchedule"];
    let fee = if m["feesEnabled"].as_bool() == Some(false) {
        Some(crate::venue::Fee::NONE)
    } else {
        num(&s["rate"]).map(|rate| crate::venue::Fee { rate, exponent: num(&s["exponent"]).unwrap_or(1.0) })
    };
    crate::venue::Meta { fee, rules: text(&m["description"]), title: text(&m["question"]), complement: None }
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

/// One quote waiting to be stored: a token's top of book as it stood right after one message.
struct Row {
    recv: Ms,
    tid: String,
    server_ts: Ms,
    bid: Option<f64>,
    ask: Option<f64>,
    last: Option<f64>,
    depth: crate::engine::Depth,
}

/// Token id -> (its game, the outcome it prices).
type TokIndex<'a> = HashMap<&'a str, (&'a PmEvent, Outcome)>;

/// The most messages read in one go before the games are published again (see `stream`): enough
/// to clear any backlog at once, small enough that pings and a changed game list are not held up.
const DRAIN_MAX: usize = 500;

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
    // The sister markets' own list and connection: a change there never restarts the winner feed.
    let (tx_s, rx_s) = watch::channel::<Arc<Vec<PmEvent>>>(Arc::new(Vec::new()));
    tokio::spawn(run_sister(app.clone(), rx_s));

    // Discovery loop.
    {
        let http = http.clone();
        let app = app.clone();
        tokio::spawn(async move {
            // Ball sports: the lines picked for a game stay picked (the nearest-to-even line drifts
            // with the score; re-picking would reconnect the sister feed every minute).
            let mut picked: HashMap<String, Vec<Sister>> = HashMap::new();
            loop {
                match discover(&http, prematch_min, &app.cfg).await {
                    Ok(mut evs) => {
                        for e in evs.iter_mut().filter(|e| !is_esports_sport(&e.sport) && !e.sister.is_empty()) {
                            e.sister = picked.entry(e.id.clone()).or_insert_with(|| e.sister.clone()).clone();
                        }
                        picked.retain(|id, _| evs.iter().any(|e| &e.id == id));
                        for e in &evs {
                            for (tok, m) in &e.meta {
                                crate::venue::set_meta(crate::venue::V::Pm, tok, m.clone());
                            }
                        }
                        let changed = {
                            let cur = tx.borrow();
                            cur.len() != evs.len() || cur.iter().zip(evs.iter()).any(|(a, b)| a.id != b.id || a.tokens != b.tokens)
                        };
                        let sister_changed = {
                            let cur = tx_s.borrow();
                            let flat = |v: &[PmEvent]| v.iter().flat_map(|e| e.sister.iter().map(|s| s.token.clone())).collect::<Vec<_>>();
                            flat(&cur) != flat(&evs)
                        };
                        let evs = Arc::new(evs);
                        if sister_changed {
                            tracing::info!("polymarket: {} sister markets in {} games (recorded only)", evs.iter().map(|e| e.sister.len()).sum::<usize>(), evs.iter().filter(|e| !e.sister.is_empty()).count());
                            let _ = tx_s.send(evs.clone());
                        }
                        if changed {
                            tracing::info!("polymarket: {} live/upcoming games ({} e-sports, {} other sports)", evs.len(), evs.iter().filter(|e| is_esports_sport(&e.sport)).count(), evs.iter().filter(|e| e.sport != "football" && !is_esports_sport(&e.sport)).count());
                            let _ = tx.send(evs);
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
            apply(&app, &events, &TokIndex::new(), &toks, now_ms(), Vec::new()).await;
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
    // Token -> (its game, the outcome it prices): the same for the life of this connection.
    let index: TokIndex = events.iter().flat_map(|e| e.tokens.iter().map(move |(o, t)| (t.as_str(), (e, *o)))).collect();
    let mut ping = tokio::time::interval(Duration::from_secs(10));
    let mut last_msg = std::time::Instant::now();
    loop {
        tokio::select! {
            m = ws.next() => {
                let Some(m) = m else { anyhow::bail!("closed by server") };
                // Publishing the games costs far more than reading a message: every watched game
                // is rebuilt and handed to the engine under the lock all feeds share. So everything
                // already waiting on the socket is read first, and the games are published once.
                // With one publish per message the reader kept up with about 100-150 messages a
                // second, 15-50 when the app was busy, against the 120-150 Polymarket sends in the
                // prime hours: on 20 Nov 2026 prices were handled 20-40 s after they were sent.
                let mut next = Some(m?);
                let mut rows: Vec<Row> = Vec::new();
                let mut recv = now_ms();
                let mut taken = 0;
                while let Some(m) = next.take() {
                    recv = now_ms();
                    last_msg = std::time::Instant::now();
                    crate::venue::alive(crate::venue::V::Pm, recv);
                    taken += 1;
                    let text = match m {
                        Message::Text(t) => Some(t.to_string()),
                        Message::Binary(b) => Some(String::from_utf8_lossy(&b).into_owned()),
                        Message::Ping(p) => { ws.send(Message::Pong(p)).await?; None }
                        Message::Close(_) => anyhow::bail!("closed by server"),
                        _ => None,
                    };
                    if let Some(v) = text.filter(|t| t.as_str() != "PONG" && !t.is_empty()).and_then(|t| serde_json::from_str::<Value>(&t).ok()) {
                        let mut changed: Vec<String> = Vec::new();
                        let mut stamp = 0;
                        for item in v.as_array().cloned().unwrap_or_else(|| vec![v.clone()]) {
                            stamp = stamp.max(int(&item["timestamp"]).unwrap_or(0));
                            absorb(&item, toks, &wanted, &mut changed, true);
                        }
                        // How far behind Polymarket's own clock this message was handled.
                        if stamp > 0 {
                            crate::venue::pm_lag(recv - stamp);
                        }
                        // One stored quote per token this message moved, as it stood right after
                        // it and with this message's own receive time: the record is the same as
                        // when every message was published by itself.
                        changed.sort_unstable();
                        changed.dedup();
                        for tid in changed {
                            if let Some(t) = toks.get(&tid) {
                                rows.push(Row { recv, server_ts: t.server_ts, bid: t.bid, ask: t.ask, last: t.last, depth: t.depth, tid });
                            }
                        }
                    }
                    if taken < DRAIN_MAX {
                        match ws.next().now_or_never() {
                            Some(Some(m)) => next = Some(m?),
                            Some(None) => anyhow::bail!("closed by server"),
                            None => {}
                        }
                    }
                }
                if !rows.is_empty() {
                    apply(app, events, &index, toks, recv, rows).await;
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

/// The sister markets' connection: its own socket, token state and reconnects, so the winner
/// markets' feed is never slowed or restarted by it. Every quote goes to `pm_sister_quotes` and
/// nowhere else: not to the engine, not to the venue books, not to the feed-delay measure.
async fn run_sister(app: Arc<App>, mut rx: watch::Receiver<Arc<Vec<PmEvent>>>) {
    let mut toks: HashMap<String, Tok> = HashMap::new();
    loop {
        let events = rx.borrow_and_update().clone();
        let ids: Vec<String> = events.iter().flat_map(|e| e.sister.iter().map(|s| s.token.clone())).collect();
        if ids.is_empty() {
            if rx.changed().await.is_err() {
                return;
            }
            continue;
        }
        toks.retain(|k, _| ids.contains(k));
        if let Err(e) = stream_sister(&app, &events, &ids, &mut toks, &mut rx).await {
            tracing::warn!("polymarket sister ws: {e:#}");
            tokio::time::sleep(Duration::from_secs(5)).await;
        }
    }
}

async fn stream_sister(app: &Arc<App>, events: &Arc<Vec<PmEvent>>, ids: &[String], toks: &mut HashMap<String, Tok>, rx: &mut watch::Receiver<Arc<Vec<PmEvent>>>) -> Result<()> {
    let (mut ws, _) = tokio_tungstenite::connect_async(WS_URL).await?;
    let sub = serde_json::json!({"assets_ids": ids, "type": "market", "custom_feature_enabled": true});
    ws.send(Message::Text(sub.to_string().into())).await?;
    let wanted: std::collections::HashSet<&str> = ids.iter().map(|s| s.as_str()).collect();
    let index: HashMap<&str, (&PmEvent, &Sister)> = events.iter().flat_map(|e| e.sister.iter().map(move |s| (s.token.as_str(), (e, s)))).collect();
    // Polymarket event id -> the engine's match (looked up once per game, not once per message).
    let mut match_of: HashMap<String, i64> = HashMap::new();
    // Per token: the prices last stored, and when.
    let mut stored: HashMap<String, (Option<f64>, Option<f64>, Ms)> = HashMap::new();
    let mut ping = tokio::time::interval(Duration::from_secs(10));
    let mut last_msg = std::time::Instant::now();
    let (mut late, mut msgs, mut bytes, mut rows, mut since) = (0u32, 0u64, 0u64, 0u64, std::time::Instant::now());
    loop {
        tokio::select! {
            m = ws.next() => {
                let Some(m) = m else { anyhow::bail!("closed by server") };
                let text = match m? {
                    Message::Text(t) => Some(t.to_string()),
                    Message::Binary(b) => Some(String::from_utf8_lossy(&b).into_owned()),
                    Message::Ping(p) => { ws.send(Message::Pong(p)).await?; None }
                    Message::Close(_) => anyhow::bail!("closed by server"),
                    _ => None,
                };
                last_msg = std::time::Instant::now();
                let Some(t) = text.filter(|t| t.as_str() != "PONG" && !t.is_empty()) else { continue };
                msgs += 1;
                bytes += t.len() as u64;
                let Ok(v) = serde_json::from_str::<Value>(&t) else { continue };
                let recv = now_ms();
                let mut changed: Vec<String> = Vec::new();
                for item in v.as_array().cloned().unwrap_or_else(|| vec![v.clone()]) {
                    absorb(&item, toks, &wanted, &mut changed, false);
                }
                changed.sort_unstable();
                changed.dedup();
                let mut writes = Vec::new();
                for tid in changed {
                    let (Some(t), Some((e, s))) = (toks.get(&tid), index.get(tid.as_str())) else { continue };
                    // Every price change is stored; a change of the sizes alone only now and then.
                    if stored.get(&tid).is_some_and(|l| (l.0, l.1) == (t.bid, t.ask) && recv - l.2 < SISTER_SIZE_EVERY_MS) {
                        continue;
                    }
                    let mid = match match_of.get(&e.id) {
                        Some(m) => *m,
                        None => {
                            // Not matched to a game yet: nothing to tie the quote to. Asked again next time.
                            let Some(m) = app.engine.lock().books.get(NAME).and_then(|b| b.events.get(&e.id)).map(|v| v.match_id).filter(|m| *m > 0) else { continue };
                            match_of.insert(e.id.clone(), m);
                            m
                        }
                    };
                    stored.insert(tid.clone(), (t.bid, t.ask, recv));
                    writes.push(Write::PmSister { ts_ms: recv, server_ts_ms: t.server_ts, match_id: mid, event_id: e.id.clone(), kind: s.kind.clone(), line: s.line, outcome: s.outcome.clone(), token: tid, bid: t.bid, ask: t.ask, depth: t.depth });
                }
                rows += writes.len() as u64;
                if !writes.is_empty() {
                    app.emit(writes).await;
                }
            }
            _ = ping.tick() => {
                ws.send(Message::Text("PING".into())).await?;
                if last_msg.elapsed() > Duration::from_secs(180) {
                    anyhow::bail!("no data for 180 s");
                }
                if since.elapsed() >= Duration::from_secs(300) {
                    tracing::info!("polymarket sister markets: {} tokens, {} messages, {} quotes stored, {:.2} Mbit/s in 5 min", ids.len(), msgs, rows, bytes as f64 * 8.0 / 300.0 / 1e6);
                    (msgs, bytes, rows, since) = (0, 0, 0, std::time::Instant::now());
                }
                // The winner markets' feed comes first: step aside while it runs late.
                late = if crate::venue::pm_lag_ms() > SISTER_YIELD_MS { late + 1 } else { 0 };
                if late >= 3 {
                    tracing::warn!("polymarket sister markets: the main feed is {} ms late, pausing for {} min", crate::venue::pm_lag_ms(), SISTER_PAUSE.as_secs() / 60);
                    let _ = ws.close(None).await;
                    tokio::time::sleep(SISTER_PAUSE).await;
                    return Ok(());
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
fn absorb(item: &Value, toks: &mut HashMap<String, Tok>, wanted: &std::collections::HashSet<&str>, changed: &mut Vec<String>, publish: bool) {
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
        // The full book, for the paper arbitrage (every update, not throttled).
        let lv = |m: &std::collections::BTreeMap<i64, f64>| m.iter().map(|(k, s)| (*k as f64 / 1000.0, *s)).collect::<Vec<_>>();
        if publish {
            crate::venue::publish(crate::venue::V::Pm, asset, lv(&t.bids), lv(&t.asks), now);
        }
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

/// Publish the current state of every watched game as one snapshot, and store `rows`.
async fn apply(app: &Arc<App>, events: &[PmEvent], index: &TokIndex<'_>, toks: &HashMap<String, Tok>, recv: Ms, rows: Vec<Row>) {
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
    // The engine's match for each game that has a quote to store (only those: the lock is shared
    // with every bookmaker feed).
    let (mut writes, match_of): (Vec<Write>, HashMap<&str, (i64, bool)>) = {
        let mut eng = app.engine.lock();
        let w = eng.apply(NAME, Snapshot { events: evs, recv_ms: recv }, 0);
        let mut m = HashMap::new();
        if let Some(b) = eng.books.get(NAME) {
            for r in &rows {
                if let Some((e, _)) = index.get(r.tid.as_str()) {
                    if !m.contains_key(e.id.as_str()) {
                        if let Some(v) = b.events.get(&e.id) {
                            m.insert(e.id.as_str(), (v.match_id, v.swap));
                        }
                    }
                }
            }
        }
        (w, m)
    };
    // Raw top of book for the tokens that moved.
    for r in rows {
        let Some((e, o)) = index.get(r.tid.as_str()) else { continue };
        let (match_id, swap) = match_of.get(e.id.as_str()).copied().unwrap_or((0, false));
        // Stored against the match's orientation, like every other quote.
        let outcome = match (swap, *o) {
            (true, Outcome::Home) => Outcome::Away,
            (true, Outcome::Away) => Outcome::Home,
            (_, o) => o,
        };
        if match_id > 0 {
            crate::venue::map_match(match_id, outcome, &r.tid);
        }
        writes.push(Write::PmQuote {
            ts_ms: r.recv,
            server_ts_ms: r.server_ts,
            match_id,
            event_id: e.id.clone(),
            outcome,
            bid: r.bid,
            ask: r.ask,
            last: r.last,
            depth: r.depth,
        });
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
    // A UFC fight ends in its weight class: "A vs. B (Lightweight, Main Card)".
    let teams = if sport == "mma" { teams.rsplit_once(" (").filter(|(_, tail)| tail.ends_with(')')).map(|(t, _)| t).unwrap_or(teams) } else { teams };
    let (home, away) = teams.split_once(" vs. ").or_else(|| teams.split_once(" vs "))?;
    let (home, away) = (home.trim().to_string(), away.trim().to_string());
    let (hn, an) = (Names::new(&home), Names::new(&away));
    let mut tokens = Vec::new();
    let mut meta = Vec::new();
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
                meta.push((id.clone(), market_meta(m)));
            }
        }
        break;
    }
    if tokens.len() != 2 {
        return None;
    }
    // Two-way: each side's "No" is the other side's token.
    for (tok, m) in meta.iter_mut() {
        m.complement = tokens.iter().find(|(_, t)| t != tok).map(|(_, t)| t.clone());
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
        meta,
        sister: sisters(e, sport),
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
    let mut meta = Vec::new();
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
                let mut mm = market_meta(m);
                mm.complement = ids.get(1 - yes.min(1)).cloned();
                meta.push((t.clone(), mm));
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
        meta,
        sister: Vec::new(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn event(title: &str, outcomes: [&str; 2]) -> Value {
        let outs = serde_json::to_string(&outcomes).unwrap();
        serde_json::json!({
            "id": "1", "title": title, "seriesSlug": "series", "startTime": "2026-11-22T18:00:00Z", "liquidity": "100",
            "markets": [
                {"sportsMarketType": "spreads", "outcomes": outs, "clobTokenIds": "[\"s1\", \"s2\"]"},
                {"sportsMarketType": "moneyline", "outcomes": outs, "clobTokenIds": "[\"t1\", \"t2\"]"},
            ],
        })
    }

    #[test]
    fn nfl_and_ufc_games_are_read() {
        let e = parse_two_way(&event("Vikings vs. 49ers", ["Vikings", "49ers"]), Some("amfootball")).expect("an NFL game");
        assert_eq!((e.sport.as_str(), e.home.as_str(), e.away.as_str(), e.league.as_str()), ("amfootball", "Vikings", "49ers", "series"));
        // The winner market's tokens, not the spread's.
        let mut toks: Vec<&str> = e.tokens.iter().map(|t| t.1.as_str()).collect();
        toks.sort();
        assert_eq!(toks, ["t1", "t2"]);
        // A UFC fight's weight class is not part of the fighter's name.
        let e = parse_two_way(&event("UFC Fight Night: Brian Ortega vs. Dan Hooker (Lightweight, Main Card)", ["Brian Ortega", "Dan Hooker"]), Some("mma")).expect("a UFC fight");
        assert_eq!((e.home.as_str(), e.away.as_str(), e.league.as_str()), ("Brian Ortega", "Dan Hooker", "UFC Fight Night"));
        assert_eq!(e.tokens.len(), 2);
        // "More Markets" pages repeat a game and are skipped.
        assert!(parse_two_way(&event("Vikings vs. 49ers - More Markets", ["Vikings", "49ers"]), Some("amfootball")).is_none());
    }

    #[test]
    fn sister_markets_are_picked_per_sport_and_never_the_winner_market() {
        let mk = |kind: &str, toks: &str, bid: f64, line: f64| serde_json::json!({"sportsMarketType": kind, "outcomes": "[\"A\", \"B\"]", "clobTokenIds": toks, "bestBid": bid, "bestAsk": bid + 0.02, "line": line});
        // A map winner has no line of its own: its number comes from its name.
        let mut map2 = mk("child_moneyline", "[\"m2\", \"m2b\"]", 0.4, 0.0);
        map2["line"] = Value::Null;
        map2["question"] = serde_json::json!("Counter-Strike: A vs B - Map 2 Winner");
        // E-sports: every map, the totals and the map handicap; not the winner market, not a closed map.
        let mut closed = mk("child_moneyline", "[\"m1\", \"m1b\"]", 0.99, 1.0);
        closed["closed"] = serde_json::json!(true);
        let e = serde_json::json!({"markets": [mk("moneyline", "[\"w1\", \"w2\"]", 0.5, 0.0), closed, map2, mk("totals", "[\"t\", \"tb\"]", 0.5, 2.5), mk("map_handicap", "[\"h\", \"hb\"]", 0.3, -1.5), mk("round_handicap_game", "[\"r\", \"rb\"]", 0.5, 1.0)]});
        let s = sisters(&e, "cs2");
        assert_eq!(s.iter().map(|x| x.token.as_str()).collect::<Vec<_>>(), ["m2", "t", "h"]);
        assert_eq!((s[1].kind.as_str(), s[1].line, s[1].outcome.as_str()), ("totals", Some(2.5), "A"));
        assert_eq!(s[0].line, Some(2.0));
        assert_eq!(map_no("Map Handicap: BAKS (-1.5) vs ENJOY (+1.5)"), None);
        // Basketball: of many lines, the one of each type priced nearest to even.
        let e = serde_json::json!({"markets": [mk("moneyline", "[\"w1\", \"w2\"]", 0.5, 0.0), mk("spreads", "[\"s1\", \"x\"]", 0.10, -9.5), mk("spreads", "[\"s2\", \"x\"]", 0.47, -4.5), mk("spreads", "[\"s3\", \"x\"]", 0.60, -3.5), mk("spreads", "[\"s4\", \"x\"]", 0.85, 1.5), mk("totals", "[\"t1\", \"x\"]", 0.50, 220.5), mk("points", "[\"p\", \"x\"]", 0.5, 20.5)]});
        let mut t: Vec<String> = sisters(&e, "basketball").into_iter().map(|x| x.token).collect();
        t.sort();
        assert_eq!(t, ["s2", "t1"]);
        // Sports with thin sister markets record none.
        assert!(sisters(&e, "tennis").is_empty() && sisters(&e, "football").is_empty());
    }

    /// Live check of the tags and the title parsing (needs the network):
    /// `cargo test --release live_discovery -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn live_discovery_finds_the_new_sports() {
        let rt = tokio::runtime::Builder::new_current_thread().enable_all().build().unwrap();
        let evs = rt.block_on(async { discover(&crate::books::http_client(), 4 * 24 * 60, &crate::Config::default()).await }).expect("discovery");
        let n = |s: &str| evs.iter().filter(|e| e.sport == s).count();
        println!("amfootball {} | mma {} | basketball {} | tennis {} | football {} | hockey {}", n("amfootball"), n("mma"), n("basketball"), n("tennis"), n("football"), n("hockey"));
        for sp in ["amfootball", "mma"] {
            for e in evs.iter().filter(|e| e.sport == sp).take(8) {
                println!("  {sp} | {} v {} | {} | {} tokens", e.home, e.away, e.league, e.tokens.len());
            }
        }
        assert!(n("amfootball") > 0, "no American football game found");
    }
}
