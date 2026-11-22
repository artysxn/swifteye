//! Kalshi: game markets (one Yes contract per side, plus "Tie" for football) as a second
//! prediction-market baseline next to Polymarket.
//!
//! * Discovery (background task, every 2 min): the Sports series that price one game between
//!   two sides (football `...GAME`, NBA/NHL/MLB/NFL `...GAME`, tennis `...MATCH`, UFC `...FIGHT`,
//!   e-sports), then each series' open events. A series with nothing in the window is
//!   re-checked every 30 min only, so the hundreds of idle basketball series stay cheap.
//! * Kick-off: Kalshi has no start field. `occurrence_datetime` is the expected END; for team
//!   sports `close_time` = kick-off + 48 h, which is used when it fits, else end - typical length.
//! * Prices: with an API key (Settings → Kalshi, `kalshi.json`), the authenticated WebSocket
//!   `ticker` channel pushes every bid/ask change; REST re-seeds every 30 s and covers any
//!   contract the socket has not sent yet. Without a key: one batched `GET /markets` per poll.
//!   Price stored = decimal odds from the mid of the Yes bid/ask.

use super::*;
use crate::engine::{Depth, Write};
use crate::kalacct::KalshiCreds;
use crate::matcher::Names;
use futures::{SinkExt, StreamExt};
use parking_lot::Mutex;
use std::collections::{HashMap, HashSet};
use tokio_tungstenite::tungstenite::{client::IntoClientRequest, Message};

pub const NAME: &str = "kalshi";
const API: &str = "https://api.elections.kalshi.com/trade-api/v2";
const WS_URL: &str = "wss://api.elections.kalshi.com/trade-api/ws/v2";
const WS_PATH: &str = "/trade-api/ws/v2";
const MAX_SPREAD: f64 = 0.25;

#[derive(Clone, Debug)]
struct KGame {
    event: String,
    /// Sport key ("football", "basketball", an e-sports key, ...).
    sport: String,
    home: String,
    away: String,
    league: String,
    start_ms: Option<Ms>,
    /// (outcome, market ticker)
    tickers: Vec<(Outcome, String)>,
    /// Per market ticker: settlement rules (for the paper arbitrage).
    meta: Vec<(String, crate::venue::Meta)>,
}

/// Latest top of book per contract: (bid, ask, Kalshi's stamp in ms).
type PriceBook = HashMap<String, (Option<f64>, Option<f64>, Option<Ms>)>;

#[derive(Default)]
struct Shared {
    games: Vec<KGame>,
    book: PriceBook,
    /// The socket is connected and subscribed.
    ws_live: bool,
    /// contract ticker -> (event ticker, outcome), for recording quotes.
    tickers: HashMap<String, (String, Outcome)>,
    /// Last recorded (bid, ask, depth, when, the order book's own best bid / ask) per contract,
    /// so only changes are written (size-only changes at most every 2 s).
    recorded: HashMap<String, (Option<f64>, Option<f64>, Depth, Ms, (Option<f64>, Option<f64>))>,
    /// Order books from the `orderbook_delta` channel (needs the API key): resting Yes bids and
    /// No bids, price in dollars x10000 -> contracts. A Yes ask is 1 - a No bid.
    books: HashMap<String, KBook>,
}

#[derive(Default)]
struct KBook {
    yes: std::collections::BTreeMap<i64, f64>,
    no: std::collections::BTreeMap<i64, f64>,
}

/// A level counts as the book's best price only with at least this many contracts on it:
/// smaller ones are leftovers (0.01 of a contract) nobody can trade against.
const BOOK_MIN_QTY: f64 = 1.0;

impl KBook {
    /// The best Yes bid and Yes ask as the order book itself shows them, in real time. The
    /// `ticker` channel's bid / ask (what everything else here uses) can be seconds behind:
    /// on 20 Nov 2026 one update in ten came over 5 s after Kalshi's own stamp. Recorded beside
    /// the ticker's prices so the two can be compared before anything is switched over.
    fn best(&self) -> (Option<f64>, Option<f64>) {
        let top = |side: &std::collections::BTreeMap<i64, f64>| side.iter().rev().find(|l| *l.1 >= BOOK_MIN_QTY).map(|l| *l.0 as f64 / 10_000.0);
        (top(&self.yes), top(&self.no).map(|p| 1.0 - p))
    }
    /// (Yes bids, Yes asks = 1 - No bids) as (price, size). Levels on the wrong side of the
    /// `ticker` channel's best bid / ask are stale (an update we did not get or could not apply;
    /// seen as crossed books) and are left out, like the Polymarket feed does. The book keeps them,
    /// in case it is the ticker that is behind.
    fn levels(&self, bid: Option<f64>, ask: Option<f64>) -> (Vec<(f64, f64)>, Vec<(f64, f64)>) {
        let mut bids: Vec<(f64, f64)> = self.yes.iter().map(|(k, q)| (*k as f64 / 10_000.0, *q)).collect();
        let mut asks: Vec<(f64, f64)> = self.no.iter().map(|(k, q)| (1.0 - *k as f64 / 10_000.0, *q)).collect();
        if let Some(b) = bid {
            bids.retain(|l| l.0 <= b + 1e-9);
        }
        if let Some(a) = ask {
            asks.retain(|l| l.0 >= a - 1e-9);
        }
        (bids, asks)
    }
    fn depth(&self, bid: Option<f64>, ask: Option<f64>) -> Depth {
        if self.yes.is_empty() && self.no.is_empty() {
            return Depth::default();
        }
        let (b, a) = self.levels(bid, ask);
        Depth::from_levels(&b, &a)
    }
}

/// Kalshi price levels: cents (45) or dollars ("0.4500").
fn kprice(v: &Value) -> Option<i64> {
    let p = num(v)?;
    Some(((if p > 1.0 { p / 100.0 } else { p }) * 10_000.0).round() as i64)
}

/// [[price, qty], ...] from `yes_dollars` / `yes` style arrays.
fn klevels(v: &Value) -> std::collections::BTreeMap<i64, f64> {
    v.as_array().into_iter().flatten().filter_map(|l| Some((kprice(&l[0])?, num(&l[1])?))).filter(|l| l.1 > 0.0).collect()
}

const DEPTH_EVERY_MS: Ms = 2_000;

impl Shared {
    /// A `kalshi_quotes` row when this contract's top of book (or, at most every 2 s, the size
    /// behind it) changed.
    fn record(&mut self, ticker: &str, bid: Option<f64>, ask: Option<f64>, server_ts: Option<Ms>) -> Option<Write> {
        let (event, outcome) = self.tickers.get(ticker)?.clone();
        let depth = self.books.get(ticker).map(|b| b.depth(bid, ask)).unwrap_or_default();
        let best = self.books.get(ticker).map(|b| b.best()).unwrap_or((None, None));
        let now = now_ms();
        if let Some((b, a, d, t, k)) = self.recorded.get(ticker) {
            // A change of either price source is written at once; sizes at most every 2 s.
            let price_same = (*b, *a) == (bid, ask) && *k == best;
            if price_same && (!depth.differs(d) || now - t < DEPTH_EVERY_MS) {
                return None;
            }
        }
        self.recorded.insert(ticker.to_string(), (bid, ask, depth, now, best));
        Some(Write::KQuote { ts_ms: now, server_ts_ms: server_ts, event, outcome, bid, ask, est: false, depth, book: best })
    }

    /// Hand the paper arbitrage this contract's current book (trimmed to the ticker's best prices).
    fn publish(&self, tk: &str) {
        if let Some(b) = self.books.get(tk) {
            let (bid, ask, _) = self.book.get(tk).copied().unwrap_or((None, None, None));
            let (bids, asks) = b.levels(bid, ask);
            crate::venue::publish(crate::venue::V::Kalshi, tk, bids, asks, now_ms());
        }
    }

    /// Fold an `orderbook_snapshot` / `orderbook_delta` message; returns the ticker.
    fn book_msg(&mut self, kind: &str, m: &Value) -> Option<String> {
        let tk = text(&m["market_ticker"]);
        if tk.is_empty() {
            return None;
        }
        let b = self.books.entry(tk.clone()).or_default();
        if kind == "orderbook_snapshot" {
            // Current format: `yes_dollars_fp` / `no_dollars_fp` ([["0.0800", "300.00"], ...]),
            // absent when that side is empty. Older: `yes_dollars`, then cents in `yes`.
            let side = |k: &str| {
                for key in [format!("{k}_dollars_fp"), format!("{k}_dollars"), k.to_string()] {
                    if m[key.as_str()].is_array() {
                        return klevels(&m[key.as_str()]);
                    }
                }
                Default::default()
            };
            b.yes = side("yes");
            b.no = side("no");
        } else {
            let p = kprice(&m["price_dollars"]).or_else(|| kprice(&m["price"]))?;
            let d = num(&m["delta_fp"]).or_else(|| num(&m["delta"]))?;
            let side = if m["side"].as_str() == Some("no") { &mut b.no } else { &mut b.yes };
            let q = side.get(&p).copied().unwrap_or(0.0) + d;
            // Sizes come with two decimals; anything under half of 0.01 is rounding left over
            // from adding them up, and would otherwise stay as a level that is not there.
            if q > 0.005 {
                side.insert(p, q);
            } else {
                side.remove(&p);
            }
        }
        Some(tk)
    }
}

pub struct Kalshi {
    shared: Arc<Mutex<Shared>>,
    tx: tokio::sync::mpsc::Sender<Write>,
    keyed: bool,
    last_rest: Ms,
}

impl Kalshi {
    /// Starts the discovery task (and the WebSocket when a key is saved) and returns the poller.
    /// `tx` = the store writer: every change of a contract's real bid/ask goes to `kalshi_quotes`.
    pub fn new(hours: f64, follows: impl Fn(&str) -> bool, creds: KalshiCreds, tx: tokio::sync::mpsc::Sender<Write>) -> Self {
        let shared = Arc::new(Mutex::new(Shared::default()));
        let keyed = crate::kalacct::has_key(&creds);
        let sports: HashSet<&'static str> = SPORTS.iter().chain(ESPORTS.iter()).copied().filter(|s| follows(s)).collect();
        let s = shared.clone();
        tokio::spawn(async move {
            let http = http_client();
            let mut series: Vec<(String, String, &'static str)> = Vec::new();
            let mut series_at: Ms = 0;
            let mut idle_until: HashMap<String, Ms> = HashMap::new();
            loop {
                if series.is_empty() || now_ms() - series_at > 6 * 3_600_000 {
                    match game_series(&http, &sports).await {
                        Ok(x) => {
                            let mut by: HashMap<&str, usize> = HashMap::new();
                            for (_, _, sp) in &x {
                                *by.entry(sp).or_default() += 1;
                            }
                            tracing::info!("kalshi: {} game series {:?}", x.len(), by);
                            series = x;
                            series_at = now_ms();
                        }
                        Err(e) => tracing::warn!("kalshi series: {e:#}"),
                    }
                }
                let t0 = now_ms();
                let current = s.lock().games.clone();
                match discover(&http, &series, hours, &mut idle_until, &current).await {
                    Ok(found) => {
                        let n = found.len();
                        let changed = current.len() != n;
                        {
                            let mut sh = s.lock();
                            sh.tickers = found.iter().flat_map(|g| g.tickers.iter().map(move |(o, t)| (t.clone(), (g.event.clone(), *o)))).collect();
                            crate::venue::map_events(found.iter().flat_map(|g| g.tickers.iter().map(move |(o, t)| (g.event.clone(), *o, t.clone()))));
                            for g in &found {
                                for (tk, m) in &g.meta {
                                    crate::venue::set_meta(crate::venue::V::Kalshi, tk, m.clone());
                                }
                            }
                            sh.games = found;
                        }
                        if changed {
                            tracing::info!("kalshi: {n} games in the window (discovery {} s)", (now_ms() - t0) / 1000);
                        }
                    }
                    Err(e) => tracing::warn!("kalshi discovery: {e:#}"),
                }
                tokio::time::sleep(Duration::from_secs(120)).await;
            }
        });
        if keyed {
            tokio::spawn(ws_loop(shared.clone(), creds, tx.clone()));
        }
        Kalshi { shared, tx, keyed, last_rest: 0 }
    }
}

/// Which sport a Sports series prices, if it is a single game between two sides.
fn series_sport(ticker: &str, title: &str, tags: &[&str]) -> Option<&'static str> {
    let t = ticker;
    let ends = |x: &str| t.ends_with(x);
    let has = |x: &str| t.contains(x);
    let tag = |x: &str| tags.contains(&x);
    if tag("Soccer") {
        return (ends("GAME") && !has("GOALEVERY")).then_some("football");
    }
    if tag("Esports") {
        // "KXCS2GAME" = whole match winner; "...MAP" series (single maps) end differently.
        return if ends("GAME") || ends("GAME3WAY") { esport_key(title).or_else(|| esport_key(t)) } else { None };
    }
    let game = ends("GAME") && !has("SERIES") && !has("BOWLGAME") && !has("SPECIAL");
    if tag("Basketball") {
        game.then_some("basketball")
    } else if tag("Hockey") {
        game.then_some("hockey")
    } else if tag("Baseball") {
        (game && !has("NCAASB")).then_some("baseball")
    } else if tag("Football") {
        game.then_some("amfootball")
    } else if tag("Tennis") {
        ((ends("MATCH") || ends("GAME")) && !has("EXACT") && !has("SPREAD") && !has("TOTAL") && !has("PICKLEBALL")).then_some("tennis")
    } else if tag("MMA") || tag("UFC") {
        (t == "KXUFCFIGHT" || t == "KXMMAFIGHT").then_some("mma")
    } else if tag("Volleyball") {
        ends("MATCH").then_some("volleyball")
    } else if tag("Table Tennis") {
        (ends("MATCH") || ends("GAME")).then_some("tabletennis")
    } else {
        None
    }
}

/// (ticker, title, sport key) of every followed Kalshi series that prices one game.
async fn game_series(http: &reqwest::Client, sports: &HashSet<&'static str>) -> Result<Vec<(String, String, &'static str)>> {
    let (v, _) = fetch_json(http.get(format!("{API}/series?category=Sports&limit=5000"))).await?;
    let mut out = Vec::new();
    for s in v["series"].as_array().into_iter().flatten() {
        let (ticker, title) = (text(&s["ticker"]), text(&s["title"]));
        let tags: Vec<&str> = s["tags"].as_array().into_iter().flatten().filter_map(|x| x.as_str()).collect();
        if let Some(sp) = series_sport(&ticker, &title, &tags) {
            if sports.contains(sp) {
                out.push((ticker, title, sp));
            }
        }
    }
    Ok(out)
}

async fn discover(
    http: &reqwest::Client,
    series: &[(String, String, &'static str)],
    hours: f64,
    idle_until: &mut HashMap<String, Ms>,
    current: &[KGame],
) -> Result<Vec<KGame>> {
    let now = now_ms();
    let hi = now + (hours.max(0.5) * 3_600_000.0) as i64;
    let mut out = Vec::new();
    let mut skipped: HashSet<String> = HashSet::new();
    let mut due = Vec::new();
    for x in series {
        if idle_until.get(&x.0).map_or(true, |u| now >= *u) {
            due.push(x);
        } else {
            skipped.insert(x.0.clone());
        }
    }
    // A few series at a time keeps us well inside Kalshi's rate limit.
    for chunk in due.chunks(8) {
        let futs = chunk.iter().map(|(t, title, sport)| {
            let url = format!("{API}/events?series_ticker={t}&status=open&with_nested_markets=true&limit=200");
            async move { (t.clone(), fetch_json(http.get(url)).await.map(|(v, _)| (v, title.clone(), *sport))) }
        });
        for (t, res) in futures::future::join_all(futs).await {
            let Ok((v, series_title, sport)) = res else { continue };
            // A football game is over within 3 h; a best-of-five or a tennis match can run longer.
            let lo = now - if sport == "football" { 3 } else { 6 } * 3_600_000;
            let before = out.len();
            for e in v["events"].as_array().into_iter().flatten() {
                if let Some(g) = parse_event(e, &series_title, sport) {
                    crate::predmeta::set(NAME, &g.event, event_size(e));
                    if g.start_ms.is_some_and(|s| s >= lo && s <= hi) {
                        out.push(g);
                    }
                }
            }
            if out.len() == before {
                idle_until.insert(t, now + 30 * 60_000);
            } else {
                idle_until.remove(&t);
            }
        }
        tokio::time::sleep(Duration::from_millis(200)).await;
    }
    // Series not re-checked this round keep the games they had.
    let series_of = |ev: &str| ev.split('-').next().unwrap_or("").to_string();
    out.extend(current.iter().filter(|g| skipped.contains(&series_of(&g.event)) && g.start_ms.is_some_and(|s| s <= hi && s >= now - 6 * 3_600_000)).cloned());
    out.sort_by(|a, b| a.event.cmp(&b.event));
    out.dedup_by(|a, b| a.event == b.event);
    Ok(out)
}

/// Liquidity and volume of an event, summed over its contracts, in dollars (Kalshi counts
/// contracts; volume is valued at each contract's last price).
fn event_size(e: &Value) -> crate::predmeta::Size {
    let mut s = crate::predmeta::Size::default();
    for m in e["markets"].as_array().into_iter().flatten() {
        let px = prob(m, "last_price_dollars", "last_price").unwrap_or(0.5);
        // Kalshi's liquidity fields are often zero now; open interest at the last price stands in.
        let liq = num(&m["liquidity_dollars"]).or_else(|| num(&m["liquidity"]).map(|c| c / 100.0)).filter(|x| *x > 0.0);
        s.liquidity += liq.or_else(|| num(&m["open_interest_fp"]).or_else(|| num(&m["open_interest"])).map(|n| n * px)).unwrap_or(0.0);
        s.volume += num(&m["volume_fp"]).or_else(|| num(&m["volume"])).unwrap_or(0.0) * px;
        s.volume_24h += num(&m["volume_24h_fp"]).or_else(|| num(&m["volume_24h"])).unwrap_or(0.0) * px;
    }
    s
}

/// Typical length of a game, to turn Kalshi's expected end into a kick-off.
fn typical_ms(sport: &str) -> i64 {
    let h = match sport {
        "amfootball" => 3.5,
        "tennis" | "tabletennis" | "volleyball" => 2.0,
        "mma" => 0.5,
        _ => 3.0,
    };
    (h * 3_600_000.0) as i64
}

fn kickoff(m: &Value, sport: &str) -> Option<Ms> {
    let end = m["occurrence_datetime"].as_str().or(m["expected_expiration_time"].as_str()).and_then(rfc3339_ms)?;
    if let Some(close) = m["close_time"].as_str().and_then(rfc3339_ms) {
        // Team sports: the market closes 48 h after the scheduled start.
        let k = close - 48 * 3_600_000;
        if k <= end && end - k <= 8 * 3_600_000 {
            return Some(k);
        }
    }
    Some(end - typical_ms(sport))
}

fn parse_event(e: &Value, series_title: &str, sport: &'static str) -> Option<KGame> {
    let mut title = text(&e["title"]);
    // Prefixes: "OCS Asia Stage 3 2026: VARREL vs. T1", "Fight Night: A vs B".
    if sport != "football" {
        if let Some((_, rest)) = title.clone().rsplit_once(": ") {
            title = rest.to_string();
        }
    }
    let (home, away) = title.split_once(" vs ").or_else(|| title.split_once(" vs. ")).or_else(|| title.split_once(" v "))?;
    let (home, away) = (home.trim().to_string(), away.trim().to_string());
    let (hn, an) = (Names::new(&home), Names::new(&away));
    let mut tickers = Vec::new();
    let mut meta = Vec::new();
    let mut start = None;
    for m in e["markets"].as_array().into_iter().flatten() {
        if m["status"].as_str().is_some_and(|s| s != "active") {
            continue;
        }
        let sub = text(&m["yes_sub_title"]);
        let outcome = if sub.eq_ignore_ascii_case("tie") || sub.eq_ignore_ascii_case("draw") {
            if !three_way(sport) {
                continue;
            }
            Outcome::Draw
        } else {
            let n = Names::new(&sub);
            let (sh, sa) = (hn.sim(&n), an.sim(&n));
            if sh.max(sa) < 0.5 {
                continue;
            }
            if sh >= sa { Outcome::Home } else { Outcome::Away }
        };
        start = start.or_else(|| kickoff(m, sport));
        if !tickers.iter().any(|(o, _)| *o == outcome) {
            tickers.push((outcome, text(&m["ticker"])));
            let rules = format!("{}\n{}", text(&m["rules_primary"]), text(&m["rules_secondary"]));
            meta.push((text(&m["ticker"]), crate::venue::Meta { fee: Some(crate::venue::Fee { rate: 0.07, exponent: 1.0 }), rules, title: text(&m["title"]), complement: None }));
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
        meta,
    })
}

/// Dollar string ("0.4500") or cents number (45) to a 0..1 probability.
fn prob(m: &Value, dollars: &str, cents: &str) -> Option<f64> {
    num(&m[dollars]).or_else(|| num(&m[cents]).map(|c| c / 100.0))
}

async fn rest_prices(http: &reqwest::Client, tickers: &[&str]) -> Result<(PriceBook, Ms)> {
    let mut book = PriceBook::new();
    let mut recv_ms = 0;
    for chunk in tickers.chunks(100) {
        let (v, t) = fetch_json(http.get(format!("{API}/markets?tickers={}&limit=200", chunk.join(",")))).await?;
        recv_ms = recv_ms.max(t);
        for m in v["markets"].as_array().into_iter().flatten() {
            let active = m["status"].as_str() == Some("active");
            let bid = prob(m, "yes_bid_dollars", "yes_bid").filter(|x| *x > 0.0 && active);
            let ask = prob(m, "yes_ask_dollars", "yes_ask").filter(|x| *x > 0.0 && *x < 1.0 && active);
            book.insert(text(&m["ticker"]), (bid, ask, None));
        }
    }
    Ok((book, if recv_ms > 0 { recv_ms } else { now_ms() }))
}

/// Authenticated WebSocket: subscribe the `ticker` channel for every contract in the window,
/// add new contracts as discovery finds them, reconnect with backoff.
async fn ws_loop(shared: Arc<Mutex<Shared>>, creds: KalshiCreds, tx: tokio::sync::mpsc::Sender<Write>) {
    let mut backoff = 2u64;
    loop {
        match ws_session(&shared, &creds, &tx).await {
            Ok(()) => backoff = 2,
            Err(e) => {
                tracing::warn!("kalshi ws: {e:#} (reconnect in {backoff}s)");
                backoff = (backoff * 2).min(60);
            }
        }
        shared.lock().ws_live = false;
        tokio::time::sleep(Duration::from_secs(backoff)).await;
    }
}

async fn ws_session(shared: &Arc<Mutex<Shared>>, creds: &KalshiCreds, wtx: &tokio::sync::mpsc::Sender<Write>) -> Result<()> {
    let mut req = WS_URL.into_client_request()?;
    for (k, v) in crate::kalacct::headers(creds, "GET", WS_PATH)? {
        req.headers_mut().insert(k, v.parse()?);
    }
    let (ws, _) = tokio::time::timeout(Duration::from_secs(15), tokio_tungstenite::connect_async(req)).await.context("connect timed out")??;
    let (mut tx, mut rx) = ws.split();
    tracing::info!("kalshi ws: connected (push prices)");
    let mut subscribed: HashSet<String> = HashSet::new();
    let mut id = 0u64;
    let mut tick = tokio::time::interval(Duration::from_secs(5));
    let mut last_msg = now_ms();
    // Order-book messages must arrive in `seq` order per subscription; a gap means a missed
    // delta, and the only cure is fresh snapshots (reconnect).
    let mut seqs: HashMap<i64, i64> = HashMap::new();
    loop {
        tokio::select! {
            _ = tick.tick() => {
                let want: Vec<String> = {
                    let s = shared.lock();
                    s.games.iter().flat_map(|g| g.tickers.iter().map(|t| t.1.clone())).filter(|t| !subscribed.contains(t)).collect()
                };
                for chunk in want.chunks(200) {
                    id += 1;
                    let msg = serde_json::json!({"id": id, "cmd": "subscribe", "params": {"channels": ["ticker", "orderbook_delta"], "market_tickers": chunk}});
                    tx.send(Message::Text(msg.to_string().into())).await?;
                    subscribed.extend(chunk.iter().cloned());
                }
                if !subscribed.is_empty() {
                    shared.lock().ws_live = true;
                }
                // Quiet markets send nothing; a ping keeps the connection honest.
                if now_ms() - last_msg > 30_000 {
                    tx.send(Message::Ping(Vec::new().into())).await?;
                }
                if now_ms() - last_msg > 90_000 {
                    bail!("no message for 90 s");
                }
            }
            msg = rx.next() => {
                let Some(msg) = msg else { bail!("closed by server") };
                last_msg = now_ms();
                crate::venue::alive(crate::venue::V::Kalshi, last_msg);
                match msg? {
                    Message::Text(t) => {
                        let Ok(v) = serde_json::from_str::<Value>(&t) else { continue };
                        match v["type"].as_str() {
                            Some("ticker") => {
                                let m = &v["msg"];
                                let tk = text(&m["market_ticker"]);
                                let bid = prob(m, "yes_bid_dollars", "yes_bid").filter(|x| *x > 0.0);
                                let ask = prob(m, "yes_ask_dollars", "yes_ask").filter(|x| *x > 0.0 && *x < 1.0);
                                // `ts` is in seconds; prefer a ms stamp when the feed has one.
                                let ts = int(&m["ts_ms"]).or_else(|| int(&m["ts"]).map(|s| if s < 10_000_000_000 { s * 1000 } else { s }));
                                let w = {
                                    let mut sh = shared.lock();
                                    sh.book.insert(tk.clone(), (bid, ask, ts));
                                    sh.publish(&tk);
                                    sh.record(&tk, bid, ask, ts)
                                };
                                if let Some(w) = w {
                                    let _ = wtx.try_send(w);
                                }
                            }
                            Some(kind @ ("orderbook_snapshot" | "orderbook_delta")) => {
                                if let (Some(sid), Some(seq)) = (int(&v["sid"]), int(&v["seq"])) {
                                    let prev = seqs.insert(sid, seq);
                                    if kind == "orderbook_delta" && prev.is_some_and(|p| seq != p + 1) {
                                        bail!("order-book sequence gap on subscription {sid} ({} -> {seq}); reconnecting for fresh snapshots", prev.unwrap_or(0));
                                    }
                                }
                                let w = {
                                    let mut sh = shared.lock();
                                    match sh.book_msg(kind, &v["msg"]) {
                                        Some(tk) => {
                                            sh.publish(&tk);
                                            let (bid, ask, ts) = sh.book.get(&tk).copied().unwrap_or((None, None, None));
                                            sh.record(&tk, bid, ask, ts)
                                        }
                                        None => None,
                                    }
                                };
                                if let Some(w) = w {
                                    let _ = wtx.try_send(w);
                                }
                            }
                            Some("error") => tracing::warn!("kalshi ws error: {}", v["msg"]),
                            _ => {}
                        }
                    }
                    Message::Ping(p) => tx.send(Message::Pong(p)).await?,
                    Message::Close(_) => bail!("closed by server"),
                    _ => {}
                }
            }
        }
    }
}

impl super::Book for Kalshi {
    fn name(&self) -> &'static str {
        NAME
    }
    fn source(&self) -> String {
        if self.keyed {
            "Kalshi game markets (mid of Yes bid/ask): authenticated WebSocket push + REST re-seed".into()
        } else {
            "Kalshi game markets (mid of Yes bid/ask), batched REST (add an API key for push)".into()
        }
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let (games, ws_live) = {
            let s = self.shared.lock();
            (s.games.clone(), s.ws_live)
        };
        let now = now_ms();
        let mut recv_ms = now;
        let all: Vec<&str> = games.iter().flat_map(|g| g.tickers.iter().map(|t| t.1.as_str())).collect();
        // REST when there is no socket, every 30 s as a re-seed, and for contracts the socket
        // has not reported yet (the ticker channel only sends on change).
        let rest_for: Vec<&str> = if !ws_live || now - self.last_rest > 30_000 {
            all
        } else {
            let s = self.shared.lock();
            all.into_iter().filter(|t| !s.book.contains_key(*t)).collect()
        };
        if !rest_for.is_empty() {
            let (book, t) = rest_prices(http, &rest_for).await?;
            recv_ms = t;
            self.last_rest = now;
            let mut rows = Vec::new();
            {
                let mut s = self.shared.lock();
                for (k, v) in book {
                    // An older REST read must not overwrite a price the socket pushed just now.
                    let fresh_push = ws_live && s.book.get(&k).is_some_and(|old| old.2.is_some_and(|ts| ts > t - 2_000));
                    if !fresh_push {
                        if let Some(w) = s.record(&k, v.0, v.1, None) {
                            rows.push(w);
                        }
                        s.book.insert(k, v);
                    }
                }
            }
            for w in rows {
                let _ = self.tx.try_send(w);
            }
        }
        let book = self.shared.lock().book.clone();
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
                        let (mid, ts) = match book.get(t) {
                            Some((Some(b), Some(a), ts)) if a > b && a - b <= MAX_SPREAD => (Some((a + b) / 2.0), *ts),
                            Some((_, _, ts)) => (None, *ts),
                            None => (None, None),
                        };
                        QuoteIn::new(Market::X12, None, *o, mid.map(|m| ((1.0 / m) * 10_000.0).round() / 10_000.0)).ts(ts)
                    })
                    .collect(),
            })
            .collect();
        Ok(Snapshot { events, recv_ms })
    }
}

/// (outcome, contract ticker) for a Kalshi event JSON (`/events/{ticker}` with its markets),
/// named the same way discovery names them. Used to rebuild past quotes.
pub(crate) fn event_contracts(e: &Value, sport: &'static str) -> Vec<(Outcome, String)> {
    parse_event(e, "", sport).map(|g| g.tickers).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn shared() -> Shared {
        let mut sh = Shared::default();
        sh.tickers.insert("T".into(), ("EV".into(), Outcome::Home));
        sh
    }

    /// The order book's best prices are read off the book, skip leftover crumbs, and a move of
    /// the book is written even while the late ticker still shows the old price.
    #[test]
    fn book_best_is_recorded_beside_the_ticker() {
        let mut sh = shared();
        sh.book_msg("orderbook_snapshot", &json!({"market_ticker": "T", "yes_dollars_fp": [["0.4500", "100.00"], ["0.4600", "0.01"]], "no_dollars_fp": [["0.5200", "50.00"]]}));
        // 0.46 holds 0.01 of a contract: not a price. Best bid 0.45, best ask 1 - 0.52 = 0.48.
        let b = sh.books["T"].best();
        assert!((b.0.unwrap() - 0.45).abs() < 1e-9 && (b.1.unwrap() - 0.48).abs() < 1e-9, "{b:?}");
        // The ticker says 0.45 / 0.48: one row.
        let w = sh.record("T", Some(0.45), Some(0.48), Some(1)).expect("first row");
        match w {
            Write::KQuote { bid, ask, book, .. } => assert_eq!((bid, ask, book.0.map(|x| (x * 100.0).round()), book.1.map(|x| (x * 100.0).round())), (Some(0.45), Some(0.48), Some(45.0), Some(48.0))),
            _ => panic!(),
        }
        // Nothing changed: no row.
        assert!(sh.record("T", Some(0.45), Some(0.48), Some(1)).is_none());
        // The market moves up in the book (the 0.48 ask is taken, a 0.50 bid arrives); the ticker
        // has not said so yet. A row is written at once, with the new book prices.
        sh.book_msg("orderbook_delta", &json!({"market_ticker": "T", "side": "no", "price_dollars": "0.5200", "delta_fp": "-50.00"}));
        sh.book_msg("orderbook_delta", &json!({"market_ticker": "T", "side": "no", "price_dollars": "0.4700", "delta_fp": "80.00"}));
        sh.book_msg("orderbook_delta", &json!({"market_ticker": "T", "side": "yes", "price_dollars": "0.5000", "delta_fp": "30.00"}));
        let w = sh.record("T", Some(0.45), Some(0.48), Some(1)).expect("the book moved");
        match w {
            Write::KQuote { bid, ask, book, .. } => {
                assert_eq!((bid, ask), (Some(0.45), Some(0.48)), "the ticker's prices are stored as before");
                assert!((book.0.unwrap() - 0.50).abs() < 1e-9 && (book.1.unwrap() - 0.53).abs() < 1e-9, "{book:?}");
            }
            _ => panic!(),
        }
    }

    /// Adding and removing sizes with two decimals leaves no level behind.
    #[test]
    fn a_level_taken_to_zero_is_gone() {
        let mut sh = shared();
        sh.book_msg("orderbook_snapshot", &json!({"market_ticker": "T", "yes_dollars_fp": [["0.3000", "0.10"]]}));
        for _ in 0..3 {
            sh.book_msg("orderbook_delta", &json!({"market_ticker": "T", "side": "yes", "price_dollars": "0.3000", "delta_fp": "0.20"}));
        }
        for d in ["-0.30", "-0.30", "-0.10"] {
            sh.book_msg("orderbook_delta", &json!({"market_ticker": "T", "side": "yes", "price_dollars": "0.3000", "delta_fp": d}));
        }
        assert!(sh.books["T"].yes.is_empty(), "{:?}", sh.books["T"].yes);
    }
}

