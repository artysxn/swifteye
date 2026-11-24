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
//! * The socket (rebuilt 24 Nov 2026, after the recording was found to run seconds to minutes
//!   behind Kalshi's own trades at exactly the busy moments):
//!   - it is read on its OWN thread, so a busy app cannot leave Kalshi's messages waiting;
//!   - everything already waiting is read in one go, each message stamped with the time it was
//!     taken off the socket (rows used to be stamped when they were processed);
//!   - `ticker` is Kalshi's once-a-second summary; the order book (`orderbook_delta`) is every
//!     change, numbered. A missing number = a lost message: every book is then emptied and a
//!     fresh snapshot of every contract is asked for ON THE SAME CONNECTION (`get_snapshot`).
//!     It used to reconnect, with a wait that doubled on every drop and never came back down;
//!   - a book is only read (best prices, depth, the ladder the arbitrage sees) while it is IN
//!     STEP: a snapshot received on this connection and nothing lost since. A dropped
//!     connection empties every book, so an old book is never shown as a current one;
//!   - a book that disagrees with Kalshi's own ticker for 10 s gets a fresh snapshot;
//!   - how far behind Kalshi's own clock the messages arrive is measured (`status`,
//!     `GET /kalshi/feed`), and book rows keep Kalshi's time of the change (`book_ts_ms`).

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
    /// Since when a contract's order book has named other best prices than Kalshi's ticker.
    mismatch: HashMap<String, Ms>,
    /// When a fresh snapshot was last asked for a contract because of that.
    asked: HashMap<String, Ms>,
}

#[derive(Default)]
struct KBook {
    yes: std::collections::BTreeMap<i64, f64>,
    no: std::collections::BTreeMap<i64, f64>,
    /// IN STEP: a full snapshot arrived on this connection and no message was lost since. Until
    /// then, and again after a lost message or a dropped connection, the levels here are not a
    /// book: nothing is read off them and nothing is published.
    synced: bool,
    /// Kalshi's own time of the last change applied (ms).
    ts: Option<Ms>,
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
        if !self.synced {
            return (None, None);
        }
        let top = |side: &std::collections::BTreeMap<i64, f64>| side.iter().rev().find(|l| *l.1 >= BOOK_MIN_QTY).map(|l| *l.0 as f64 / 10_000.0);
        (top(&self.yes), top(&self.no).map(|p| 1.0 - p))
    }
    /// The same with no smallest size: what Kalshi's own ticker calls the best prices.
    fn best_raw(&self) -> (Option<f64>, Option<f64>) {
        if !self.synced {
            return (None, None);
        }
        let top = |side: &std::collections::BTreeMap<i64, f64>| side.keys().next_back().map(|k| *k as f64 / 10_000.0);
        (top(&self.yes), top(&self.no).map(|p| 1.0 - p))
    }
    /// (Yes bids, Yes asks = 1 - No bids) as (price, size). Levels on the wrong side of the
    /// `ticker` channel's best bid / ask are stale (an update we did not get or could not apply;
    /// seen as crossed books) and are left out, like the Polymarket feed does. The book keeps them,
    /// in case it is the ticker that is behind.
    fn levels(&self, bid: Option<f64>, ask: Option<f64>) -> (Vec<(f64, f64)>, Vec<(f64, f64)>) {
        if !self.synced {
            return (Vec::new(), Vec::new());
        }
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
        if !self.synced || (self.yes.is_empty() && self.no.is_empty()) {
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
    fn record(&mut self, ticker: &str, bid: Option<f64>, ask: Option<f64>, server_ts: Option<Ms>, now: Ms) -> Option<Write> {
        let (event, outcome) = self.tickers.get(ticker)?.clone();
        let depth = self.books.get(ticker).map(|b| b.depth(bid, ask)).unwrap_or_default();
        let best = self.books.get(ticker).map(|b| b.best()).unwrap_or((None, None));
        let book_ts = self.books.get(ticker).filter(|b| b.synced).and_then(|b| b.ts);
        if let Some((b, a, d, t, k)) = self.recorded.get(ticker) {
            // A change of either price source is written at once; sizes at most every 2 s.
            let price_same = (*b, *a) == (bid, ask) && *k == best;
            if price_same && (!depth.differs(d) || now - t < DEPTH_EVERY_MS) {
                return None;
            }
        }
        self.recorded.insert(ticker.to_string(), (bid, ask, depth, now, best));
        Some(Write::KQuote { ts_ms: now, server_ts_ms: server_ts, event, outcome, bid, ask, est: false, depth, book: best, book_ts })
    }

    /// Hand the paper arbitrage this contract's current book (trimmed to the ticker's best
    /// prices); an EMPTY book while it is not in step, so nothing trades on levels that have
    /// stopped moving.
    fn publish(&self, tk: &str, now: Ms) {
        if let Some(b) = self.books.get(tk) {
            let (bid, ask, _) = self.book.get(tk).copied().unwrap_or((None, None, None));
            let (bids, asks) = b.levels(bid, ask);
            crate::venue::publish(crate::venue::V::Kalshi, tk, bids, asks, now);
        }
    }

    /// The connection dropped or a message was lost: no order book here can be trusted until its
    /// next full snapshot. Every book is emptied and published empty, and a row is written for
    /// each contract whose book prices were on record (they are unknown from now).
    fn unsync_all(&mut self, now: Ms) -> Vec<Write> {
        let tks: Vec<String> = self.books.keys().cloned().collect();
        let mut rows = Vec::new();
        for tk in tks {
            if let Some(b) = self.books.get_mut(&tk) {
                b.yes.clear();
                b.no.clear();
                b.synced = false;
                b.ts = None;
            }
            crate::venue::publish(crate::venue::V::Kalshi, &tk, Vec::new(), Vec::new(), now);
            let (bid, ask, ts) = self.book.get(&tk).copied().unwrap_or((None, None, None));
            if let Some(w) = self.record(&tk, bid, ask, ts, now) {
                rows.push(w);
            }
        }
        self.mismatch.clear();
        rows
    }

    /// Kalshi's ticker (sent once a second) and its order book should name the same best prices
    /// within a second or two. Remember since when a contract's have not.
    fn check(&mut self, tk: &str, now: Ms) {
        let Some(b) = self.books.get(tk).filter(|b| b.synced) else {
            self.mismatch.remove(tk);
            return;
        };
        let Some(&(bid, ask, _)) = self.book.get(tk) else { return };
        let close = |x: Option<f64>, y: Option<f64>| match (x, y) {
            (Some(x), Some(y)) => (x - y).abs() < 5e-5,
            (None, None) => true,
            _ => false,
        };
        let same = |x: (Option<f64>, Option<f64>)| close(x.0, bid) && close(x.1, ask);
        if same(b.best()) || same(b.best_raw()) {
            self.mismatch.remove(tk);
        } else {
            self.mismatch.entry(tk.to_string()).or_insert(now);
        }
    }

    /// Fold an `orderbook_snapshot` / `orderbook_delta` message; returns the ticker. `stamp` =
    /// Kalshi's own time for it. A delta for a book that is not in step is dropped (None): its
    /// snapshot has not come yet, and levels built from deltas alone are not a book.
    fn book_msg(&mut self, kind: &str, m: &Value, stamp: Option<Ms>) -> Option<String> {
        let tk = text(&m["market_ticker"]);
        if tk.is_empty() {
            return None;
        }
        let b = self.books.entry(tk.clone()).or_default();
        if kind == "orderbook_snapshot" {
            b.synced = true;
            b.ts = stamp;
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
            if !b.synced {
                return None;
            }
            let p = kprice(&m["price_dollars"]).or_else(|| kprice(&m["price"]))?;
            let d = num(&m["delta_fp"]).or_else(|| num(&m["delta"]))?;
            b.ts = stamp.or(b.ts);
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
                            // Added to, never replaced: which game and side a contract belongs to
                            // does not change, and a contract that falls off the list for a round
                            // (it did, for minutes, when a series could not be read) must keep
                            // being recorded while the socket still carries it.
                            sh.tickers.extend(found.iter().flat_map(|g| g.tickers.iter().map(move |(o, t)| (t.clone(), (g.event.clone(), *o)))));
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
            // Its own thread with its own small runtime: Kalshi's messages are taken off the
            // socket the moment they arrive, whatever the rest of the app is doing. On the shared
            // runtime they waited behind every other feed and the bots, and the recording ran
            // seconds to minutes behind Kalshi's own trades at the busy moments (23 Nov 2026).
            let (sh, wtx) = (shared.clone(), tx.clone());
            let made = std::thread::Builder::new().name("kalshi-ws".into()).spawn(move || match tokio::runtime::Builder::new_current_thread().enable_all().build() {
                Ok(rt) => rt.block_on(ws_loop(sh, creds, wtx)),
                Err(e) => tracing::error!("kalshi ws: no runtime, the socket is not running: {e}"),
            });
            if let Err(e) = made {
                tracing::error!("kalshi ws: no thread, the socket is not running: {e}");
            }
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
    // A few series at a time, well inside Kalshi's rate limit (the price re-seed shares it: at 8
    // at a time Kalshi answered "too many requests" for whole lots of them, 24 Nov 2026).
    let mut failed: HashSet<String> = HashSet::new();
    for chunk in due.chunks(4) {
        let failed_before = failed.len();
        let futs = chunk.iter().map(|(t, title, sport)| {
            let url = format!("{API}/events?series_ticker={t}&status=open&with_nested_markets=true&limit=200");
            async move { (t.clone(), fetch_json(http.get(url)).await.map(|(v, _)| (v, title.clone(), *sport))) }
        });
        for (t, res) in futures::future::join_all(futs).await {
            // A series that could not be read this round keeps the games it had. (Until 24 Nov
            // 2026 it was skipped and its games fell out of the window until a later round got
            // through: the game list swung between 76 and 240 from one round to the next.)
            let Ok((v, series_title, sport)) = res else {
                failed.insert(t);
                continue;
            };
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
        // After a refusal, give Kalshi a moment before the next lot.
        tokio::time::sleep(Duration::from_millis(if failed.len() > failed_before { 2_000 } else { 300 })).await;
    }
    if !failed.is_empty() {
        tracing::warn!("kalshi discovery: {} of {} series not read this round (they keep their games)", failed.len(), due.len());
    }
    // Series not re-checked this round, or not read, keep the games they had.
    let series_of = |ev: &str| ev.split('-').next().unwrap_or("").to_string();
    out.extend(current.iter().filter(|g| {
        let s = series_of(&g.event);
        (skipped.contains(&s) || failed.contains(&s)) && g.start_ms.is_some_and(|s| s <= hi && s >= now - 6 * 3_600_000)
    }).cloned());
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

// ---------------------------------------------------------------- the socket

/// Read at most this many waiting messages before publishing what they changed.
const DRAIN_MAX: usize = 4_000;
/// Contracts per subscribe / snapshot command.
const CMD_CHUNK: usize = 200;
/// A book that names other best prices than Kalshi's ticker for this long gets a fresh snapshot,
/// and not again for `ASK_AGAIN_MS`.
const MISMATCH_MS: Ms = 10_000;
const ASK_AGAIN_MS: Ms = 300_000;
/// After a lost message the fresh snapshots must be back within this, or the connection is
/// dropped and everything subscribed again.
const RESYNC_MS: Ms = 20_000;
/// Delays kept for the percentiles.
const LAG_KEEP: usize = 4_000;

/// How the socket is doing (`status`).
#[derive(Default)]
struct Stats {
    connected_ms: Ms,
    connects: u64,
    last_error: String,
    last_error_ms: Ms,
    msgs: u64,
    tickers: u64,
    snapshots: u64,
    deltas: u64,
    deltas_ignored: u64,
    errors: u64,
    /// Times a message of the order-book stream was lost.
    gaps: u64,
    last_gap_ms: Ms,
    /// Contracts given a fresh snapshot because their book disagreed with the ticker.
    resyncs: u64,
    subscribed: usize,
    synced: usize,
    mismatched: usize,
    batch_max: usize,
    last_msg_ms: Ms,
    /// How long after Kalshi queued a message we took it off the socket, and (book changes) how
    /// long after the exchange's own time of the change: the latest `LAG_KEEP`, ms.
    behind_send: std::collections::VecDeque<i64>,
    behind_change: std::collections::VecDeque<i64>,
}

static STATS: std::sync::LazyLock<Mutex<Stats>> = std::sync::LazyLock::new(Default::default);

fn pcts(v: &std::collections::VecDeque<i64>) -> Value {
    if v.is_empty() {
        return Value::Null;
    }
    let mut x: Vec<i64> = v.iter().copied().collect();
    x.sort_unstable();
    let q = |p: f64| x[((x.len() - 1) as f64 * p).round() as usize];
    serde_json::json!({"n": x.len(), "p50": q(0.5), "p90": q(0.9), "p99": q(0.99), "max": x[x.len() - 1]})
}

/// The socket's health, for `GET /kalshi/feed` and the log. Delays are our NTP-corrected clock
/// minus Kalshi's, so they include the network and any error between the two clocks.
pub fn status() -> Value {
    let s = STATS.lock();
    let now = now_ms();
    let ago = |t: Ms| if t > 0 { Some((now - t) / 1000) } else { None };
    serde_json::json!({
        "connected": s.connected_ms > 0, "connected_for_s": ago(s.connected_ms), "connects": s.connects,
        "last_error": s.last_error, "last_error_ago_s": ago(s.last_error_ms),
        "messages": s.msgs, "ticker_messages": s.tickers, "book_snapshots": s.snapshots, "book_changes": s.deltas,
        "book_changes_dropped_not_in_step": s.deltas_ignored, "errors_from_kalshi": s.errors,
        "lost_messages": s.gaps, "last_lost_ago_s": ago(s.last_gap_ms), "snapshots_after_disagreement": s.resyncs,
        "contracts_subscribed": s.subscribed, "books_in_step": s.synced, "books_disagreeing_with_ticker": s.mismatched,
        "largest_batch": s.batch_max, "last_message_ago_ms": if s.last_msg_ms > 0 { Some(now - s.last_msg_ms) } else { None },
        "behind_kalshi_send_ms": pcts(&s.behind_send), "behind_exchange_change_ms": pcts(&s.behind_change),
    })
}

/// The order-book stream of one connection: its subscription id and the last number seen.
#[derive(Default)]
struct Sess {
    book_sid: Option<i64>,
    seq: Option<i64>,
}

/// What one batch of socket messages did.
#[derive(Default)]
struct Batch {
    rows: Vec<Write>,
    /// Contracts whose book or ticker changed: published once, after the batch.
    changed: Vec<String>,
    behind_send: Vec<i64>,
    behind_change: Vec<i64>,
    tickers: u64,
    snapshots: u64,
    deltas: u64,
    ignored: u64,
    errors: u64,
    /// A message of the order-book stream was lost: (last number before, number that came).
    gap: Option<(i64, i64)>,
}

/// One socket message, taken off the socket at `recv`.
fn absorb(sh: &mut Shared, se: &mut Sess, v: &Value, recv: Ms, b: &mut Batch) {
    let sent = int(&v["sending_ts_ms"]);
    if let Some(s) = sent {
        b.behind_send.push(recv - s);
    }
    // The messages of a connection's order-book stream are numbered 1, 2, 3...: the snapshots
    // and changes of every contract, AND Kalshi's "ok" to a command on that stream (adding
    // contracts, asking for snapshots). A missing number is a lost message, and it can belong to
    // any contract: none of the books can be trusted after it.
    // (Until 24 Nov 2026 only snapshots and changes were counted. Every "ok" then looked like a
    // lost message, so each time discovery added a game the connection was dropped and every
    // contract subscribed again.)
    if let (Some(sid), Some(seq)) = (int(&v["sid"]), int(&v["seq"])) {
        if matches!(v["type"].as_str(), Some("orderbook_snapshot" | "orderbook_delta")) && se.book_sid != Some(sid) {
            se.book_sid = Some(sid);
            se.seq = None;
        }
        if se.book_sid == Some(sid) {
            if let Some(p) = se.seq {
                if seq != p + 1 {
                    b.rows.extend(sh.unsync_all(recv));
                    if b.gap.is_none() {
                        b.gap = Some((p, seq));
                    }
                }
            }
            se.seq = Some(seq);
        }
    }
    match v["type"].as_str() {
        Some("subscribed") => {
            if v["msg"]["channel"].as_str() == Some("orderbook_delta") {
                if let Some(sid) = int(&v["msg"]["sid"]) {
                    se.book_sid = Some(sid);
                }
            }
        }
        Some("ticker") => {
            let m = &v["msg"];
            let tk = text(&m["market_ticker"]);
            let bid = prob(m, "yes_bid_dollars", "yes_bid").filter(|x| *x > 0.0);
            let ask = prob(m, "yes_ask_dollars", "yes_ask").filter(|x| *x > 0.0 && *x < 1.0);
            // `ts` is in seconds; prefer the ms stamp.
            let ts = int(&m["ts_ms"]).or_else(|| int(&m["ts"]).map(|s| if s < 10_000_000_000 { s * 1000 } else { s }));
            b.tickers += 1;
            sh.book.insert(tk.clone(), (bid, ask, ts));
            sh.check(&tk, recv);
            if let Some(w) = sh.record(&tk, bid, ask, ts, recv) {
                b.rows.push(w);
            }
            b.changed.push(tk);
        }
        Some(kind @ ("orderbook_snapshot" | "orderbook_delta")) => {
            let stamp = int(&v["msg"]["ts_ms"]);
            if kind == "orderbook_delta" {
                b.deltas += 1;
                if let Some(t) = stamp {
                    b.behind_change.push(recv - t);
                }
            } else {
                b.snapshots += 1;
            }
            match sh.book_msg(kind, &v["msg"], stamp.or(sent).or(Some(recv))) {
                Some(tk) => {
                    sh.check(&tk, recv);
                    let (bid, ask, ts) = sh.book.get(&tk).copied().unwrap_or((None, None, None));
                    if let Some(w) = sh.record(&tk, bid, ask, ts, recv) {
                        b.rows.push(w);
                    }
                    b.changed.push(tk);
                }
                None => b.ignored += 1,
            }
        }
        Some("error") => {
            b.errors += 1;
            tracing::warn!("kalshi ws error: {}", v["msg"]);
        }
        _ => {}
    }
}

/// Authenticated WebSocket, on its own thread: the `ticker` and `orderbook_delta` channels for
/// every contract in the window, new contracts added as discovery finds them.
async fn ws_loop(shared: Arc<Mutex<Shared>>, creds: KalshiCreds, tx: tokio::sync::mpsc::Sender<Write>) {
    let mut backoff = 1u64;
    loop {
        let t0 = Instant::now();
        let r = ws_session(&shared, &creds, &tx).await;
        let lived = t0.elapsed().as_secs();
        // However it ended: nothing the socket built is current any more.
        let rows = {
            let mut sh = shared.lock();
            sh.ws_live = false;
            sh.unsync_all(now_ms())
        };
        for w in rows {
            let _ = tx.try_send(w);
        }
        // A connection that held for a minute was a good one: come straight back. Only failures
        // in a row wait longer. (Until 24 Nov 2026 the wait doubled on EVERY drop and never came
        // back down: after five drops in a process's life each reconnect cost a full minute.)
        backoff = if lived >= 60 { 1 } else { (backoff * 2).min(30) };
        let why = r.err().map(|e| format!("{e:#}")).unwrap_or_else(|| "ended".into());
        tracing::warn!("kalshi ws: {why} after {lived} s (reconnect in {backoff} s)");
        {
            let mut s = STATS.lock();
            s.connected_ms = 0;
            s.synced = 0;
            s.last_error = why;
            s.last_error_ms = now_ms();
        }
        tokio::time::sleep(Duration::from_secs(backoff)).await;
    }
}

async fn ws_session(shared: &Arc<Mutex<Shared>>, creds: &KalshiCreds, wtx: &tokio::sync::mpsc::Sender<Write>) -> Result<()> {
    use futures::FutureExt;
    let mut req = WS_URL.into_client_request()?;
    for (k, v) in crate::kalacct::headers(creds, "GET", WS_PATH)? {
        req.headers_mut().insert(k, v.parse()?);
    }
    let (ws, _) = tokio::time::timeout(Duration::from_secs(15), tokio_tungstenite::connect_async(req)).await.context("connect timed out")??;
    let (mut tx, mut rx) = ws.split();
    tracing::info!("kalshi ws: connected (push prices)");
    {
        let mut s = STATS.lock();
        s.connected_ms = now_ms();
        s.connects += 1;
    }
    let mut subscribed: HashSet<String> = HashSet::new();
    let mut id = 0u64;
    let mut tick = tokio::time::interval(Duration::from_secs(5));
    let mut last_msg = now_ms();
    let mut se = Sess::default();
    // Contracts per command. `KALSHI_CHUNK` (a test hook for the probe) makes it small, so that
    // one start sends many "add contracts" commands and their numbered replies.
    let chunk_n = std::env::var("KALSHI_CHUNK").ok().and_then(|s| s.parse::<usize>().ok()).filter(|n| *n > 0).unwrap_or(CMD_CHUNK);
    // After a lost message: when the fresh snapshots must be back.
    let mut resync_by: Option<Ms> = None;
    let mut said = now_ms();
    loop {
        tokio::select! {
            _ = tick.tick() => {
                let now = now_ms();
                let want: Vec<String> = {
                    let s = shared.lock();
                    // Games that left the window (finished) are forgotten here, so "every
                    // contract" below means the ones still wanted. Kalshi drops a contract from
                    // the stream itself once its market is decided.
                    let current: HashSet<&str> = s.games.iter().flat_map(|g| g.tickers.iter().map(|t| t.1.as_str())).collect();
                    if !current.is_empty() {
                        subscribed.retain(|t| current.contains(t.as_str()));
                    }
                    s.games.iter().flat_map(|g| g.tickers.iter().map(|t| t.1.clone())).filter(|t| !subscribed.contains(t)).collect()
                };
                // A repeated subscribe adds its contracts to the connection's one order-book
                // stream (and sends their snapshots); it does not start a second stream.
                for chunk in want.chunks(chunk_n) {
                    id += 1;
                    let msg = serde_json::json!({"id": id, "cmd": "subscribe", "params": {"channels": ["ticker", "orderbook_delta"], "market_tickers": chunk}});
                    tx.send(Message::Text(msg.to_string().into())).await?;
                    subscribed.extend(chunk.iter().cloned());
                }
                // Books that have named other prices than the ticker for a while: fresh snapshots.
                let (stale, synced, mismatched) = {
                    let mut sh = shared.lock();
                    let due: Vec<String> = sh.mismatch.iter().filter(|(tk, since)| now - **since > MISMATCH_MS && sh.asked.get(*tk).is_none_or(|t| now - *t > ASK_AGAIN_MS)).map(|(tk, _)| tk.clone()).take(CMD_CHUNK).collect();
                    for tk in &due {
                        sh.asked.insert(tk.clone(), now);
                    }
                    if !subscribed.is_empty() {
                        sh.ws_live = true;
                    }
                    (due, sh.books.values().filter(|b| b.synced).count(), sh.mismatch.values().filter(|since| now - **since > MISMATCH_MS).count())
                };
                if let (false, Some(sid)) = (stale.is_empty(), se.book_sid) {
                    id += 1;
                    let msg = serde_json::json!({"id": id, "cmd": "update_subscription", "params": {"sids": [sid], "market_tickers": stale, "action": "get_snapshot"}});
                    tx.send(Message::Text(msg.to_string().into())).await?;
                }
                {
                    let mut s = STATS.lock();
                    s.subscribed = subscribed.len();
                    s.synced = synced;
                    s.mismatched = mismatched;
                    s.resyncs += stale.len() as u64;
                }
                if let Some(by) = resync_by {
                    if now > by {
                        // Contracts that closed since send no snapshot: most, not all.
                        if synced * 10 < subscribed.len() * 8 {
                            bail!("only {synced} of {} books came back after a lost message", subscribed.len());
                        }
                        resync_by = None;
                    }
                }
                if now - said > 300_000 {
                    said = now;
                    let s = status();
                    tracing::info!("kalshi ws: {} of {} books in step, {} lost-message events, behind Kalshi's send time {} ms (median) / {} ms (99 in 100)", s["books_in_step"], s["contracts_subscribed"], s["lost_messages"], s["behind_kalshi_send_ms"]["p50"], s["behind_kalshi_send_ms"]["p99"]);
                }
                // Quiet markets send nothing; a ping keeps the connection honest.
                if now - last_msg > 30_000 {
                    tx.send(Message::Ping(Vec::new().into())).await?;
                }
                if now - last_msg > 90_000 {
                    bail!("no message for 90 s");
                }
            }
            msg = rx.next() => {
                let Some(msg) = msg else { bail!("closed by server") };
                // Everything already waiting is read before anything is published: at a burst
                // (a goal, a break point) hundreds of changes arrive together, and publishing
                // after each one is what makes a reader fall behind.
                let mut next = Some(msg?);
                let mut b = Batch::default();
                let mut taken = 0usize;
                let mut recv = now_ms();
                while let Some(m) = next.take() {
                    recv = now_ms();
                    taken += 1;
                    match m {
                        Message::Text(t) => {
                            if let Ok(v) = serde_json::from_str::<Value>(&t) {
                                absorb(&mut shared.lock(), &mut se, &v, recv, &mut b);
                            }
                        }
                        Message::Ping(p) => tx.send(Message::Pong(p)).await?,
                        Message::Close(_) => bail!("closed by server"),
                        _ => {}
                    }
                    if taken < DRAIN_MAX {
                        match rx.next().now_or_never() {
                            Some(Some(m)) => next = Some(m?),
                            Some(None) => bail!("closed by server"),
                            None => {}
                        }
                    }
                }
                last_msg = recv;
                crate::venue::alive(crate::venue::V::Kalshi, recv);
                b.changed.sort_unstable();
                b.changed.dedup();
                {
                    let sh = shared.lock();
                    for tk in &b.changed {
                        sh.publish(tk, recv);
                    }
                }
                for w in b.rows.drain(..) {
                    let _ = wtx.try_send(w);
                }
                if let Some((was, got)) = b.gap {
                    // Every book was emptied when the gap was seen. Ask for all of them again,
                    // on this connection: changes for a contract are dropped until its snapshot.
                    let Some(sid) = se.book_sid else { bail!("an order-book message was lost before the stream was known ({was} -> {got})") };
                    tracing::warn!("kalshi ws: order-book message lost ({was} -> {got}): every book emptied, fresh snapshots of {} contracts asked for", subscribed.len());
                    let all: Vec<&String> = subscribed.iter().collect();
                    for chunk in all.chunks(chunk_n) {
                        id += 1;
                        let msg = serde_json::json!({"id": id, "cmd": "update_subscription", "params": {"sids": [sid], "market_tickers": chunk, "action": "get_snapshot"}});
                        tx.send(Message::Text(msg.to_string().into())).await?;
                    }
                    resync_by = Some(recv + RESYNC_MS);
                }
                let mut s = STATS.lock();
                s.msgs += taken as u64;
                s.tickers += b.tickers;
                s.snapshots += b.snapshots;
                s.deltas += b.deltas;
                s.deltas_ignored += b.ignored;
                s.errors += b.errors;
                s.batch_max = s.batch_max.max(taken);
                s.last_msg_ms = recv;
                if b.gap.is_some() {
                    s.gaps += 1;
                    s.last_gap_ms = recv;
                }
                for x in b.behind_send {
                    s.behind_send.push_back(x);
                }
                for x in b.behind_change {
                    s.behind_change.push_back(x);
                }
                while s.behind_send.len() > LAG_KEEP {
                    s.behind_send.pop_front();
                }
                while s.behind_change.len() > LAG_KEEP {
                    s.behind_change.pop_front();
                }
            }
        }
    }
}

/// `oddsfeed kalshi-probe <minutes> <out folder> <folder holding kalshi.json>`: the Kalshi feed
/// alone, exactly as the collector runs it (discovery, the socket, the REST re-seed), with
/// nothing else: no other feed, no database, no bot, no order. Every row it would store goes to
/// `<out>/kquotes.jsonl` ([our time, ticker's time, event, outcome, ticker bid, ticker ask,
/// book bid, book ask, Kalshi's time of the book change]), the socket's health every 10 s to
/// `<out>/stats.jsonl`, and the contracts to `<out>/tickers.json`. `tools/kalshi_feed_check.py`
/// then holds the recording against Kalshi's public trades.
pub async fn probe(minutes: u64, out: std::path::PathBuf, data: std::path::PathBuf) -> Result<()> {
    use std::io::Write as _;
    std::fs::create_dir_all(&out)?;
    let creds = crate::kalacct::load(&data.join("oddsfeed.db"));
    if !crate::kalacct::has_key(&creds) {
        bail!("no Kalshi key in {}", data.join("kalshi.json").display());
    }
    let (tx, mut rx) = tokio::sync::mpsc::channel::<Write>(500_000);
    let mut k = Kalshi::new(8.0, |_| true, creds, tx);
    let shared = k.shared.clone();
    let qpath = out.join("kquotes.jsonl");
    tokio::spawn(async move {
        let Ok(f) = std::fs::File::create(&qpath) else { return };
        let mut f = std::io::BufWriter::new(f);
        let mut n = 0u64;
        while let Some(w) = rx.recv().await {
            if let Write::KQuote { ts_ms, server_ts_ms, event, outcome, bid, ask, book, book_ts, .. } = w {
                let _ = writeln!(f, "{}", serde_json::json!([ts_ms, server_ts_ms, event, outcome.as_str(), bid, ask, book.0, book.1, book_ts]));
                n += 1;
                if n % 200 == 0 {
                    let _ = f.flush();
                }
            }
        }
    });
    let http = http_client();
    let end = Instant::now() + Duration::from_secs(minutes * 60);
    let mut stats = std::fs::File::create(out.join("stats.jsonl"))?;
    let mut said = Instant::now() - Duration::from_secs(60);
    println!("kalshi probe: {minutes} min, writing to {}", out.display());
    while Instant::now() < end {
        if let Err(e) = k.fetch(&http).await {
            println!("  rest: {e:#}");
        }
        if said.elapsed() >= Duration::from_secs(10) {
            said = Instant::now();
            let mut s = status();
            s["t"] = serde_json::json!(now_ms());
            writeln!(stats, "{s}")?;
            let (games, tks) = {
                let sh = shared.lock();
                (sh.games.len(), sh.tickers.iter().map(|(t, (e, o))| (t.clone(), serde_json::json!([e, o.as_str()]))).collect::<serde_json::Map<String, Value>>())
            };
            std::fs::write(out.join("tickers.json"), Value::Object(tks).to_string())?;
            println!("  {games} games | {} of {} books in step | {} msgs ({} book changes) | lost {} | reconnects {} | behind send p50 {} p99 {} ms | behind change p50 {} p99 {} ms",
                s["books_in_step"], s["contracts_subscribed"], s["messages"], s["book_changes"], s["lost_messages"], s["connects"].as_u64().unwrap_or(1).saturating_sub(1),
                s["behind_kalshi_send_ms"]["p50"], s["behind_kalshi_send_ms"]["p99"], s["behind_exchange_change_ms"]["p50"], s["behind_exchange_change_ms"]["p99"]);
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    tokio::time::sleep(Duration::from_millis(500)).await;
    Ok(())
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
                        if let Some(w) = s.record(&k, v.0, v.1, None, t) {
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
        sh.book_msg("orderbook_snapshot", &json!({"market_ticker": "T", "yes_dollars_fp": [["0.4500", "100.00"], ["0.4600", "0.01"]], "no_dollars_fp": [["0.5200", "50.00"]]}), None);
        // 0.46 holds 0.01 of a contract: not a price. Best bid 0.45, best ask 1 - 0.52 = 0.48.
        let b = sh.books["T"].best();
        assert!((b.0.unwrap() - 0.45).abs() < 1e-9 && (b.1.unwrap() - 0.48).abs() < 1e-9, "{b:?}");
        // The ticker says 0.45 / 0.48: one row.
        let w = sh.record("T", Some(0.45), Some(0.48), Some(1), 1_000).expect("first row");
        match w {
            Write::KQuote { bid, ask, book, .. } => assert_eq!((bid, ask, book.0.map(|x| (x * 100.0).round()), book.1.map(|x| (x * 100.0).round())), (Some(0.45), Some(0.48), Some(45.0), Some(48.0))),
            _ => panic!(),
        }
        // Nothing changed: no row.
        assert!(sh.record("T", Some(0.45), Some(0.48), Some(1), 1_000).is_none());
        // The market moves up in the book (the 0.48 ask is taken, a 0.50 bid arrives); the ticker
        // has not said so yet. A row is written at once, with the new book prices.
        sh.book_msg("orderbook_delta", &json!({"market_ticker": "T", "side": "no", "price_dollars": "0.5200", "delta_fp": "-50.00"}), None);
        sh.book_msg("orderbook_delta", &json!({"market_ticker": "T", "side": "no", "price_dollars": "0.4700", "delta_fp": "80.00"}), None);
        sh.book_msg("orderbook_delta", &json!({"market_ticker": "T", "side": "yes", "price_dollars": "0.5000", "delta_fp": "30.00"}), None);
        let w = sh.record("T", Some(0.45), Some(0.48), Some(1), 1_000).expect("the book moved");
        match w {
            Write::KQuote { bid, ask, book, .. } => {
                assert_eq!((bid, ask), (Some(0.45), Some(0.48)), "the ticker's prices are stored as before");
                assert!((book.0.unwrap() - 0.50).abs() < 1e-9 && (book.1.unwrap() - 0.53).abs() < 1e-9, "{book:?}");
            }
            _ => panic!(),
        }
    }

    fn snap(seq: i64, tk: &str, yes: Value, no: Value) -> Value {
        json!({"type": "orderbook_snapshot", "sid": 5, "seq": seq, "sending_ts_ms": 4_000, "msg": {"market_ticker": tk, "yes_dollars_fp": yes, "no_dollars_fp": no}})
    }

    fn delta(seq: i64, tk: &str, side: &str, price: &str, d: &str, ts: Ms) -> Value {
        json!({"type": "orderbook_delta", "sid": 5, "seq": seq, "sending_ts_ms": ts + 100, "msg": {"market_ticker": tk, "side": side, "price_dollars": price, "delta_fp": d, "ts_ms": ts}})
    }

    fn book_of(w: &Write) -> (Option<f64>, Option<f64>, Option<Ms>, Ms) {
        match w {
            Write::KQuote { book, book_ts, ts_ms, .. } => (book.0.map(|x| (x * 100.0).round()), book.1.map(|x| (x * 100.0).round()), *book_ts, *ts_ms),
            _ => panic!(),
        }
    }

    /// The order-book messages of a connection are numbered. One missing number and no book can
    /// be trusted: all are emptied (and written as unknown), changes are dropped until a
    /// contract's fresh snapshot arrives, and the caller is told to ask for the snapshots.
    #[test]
    fn a_lost_message_empties_every_book_until_its_snapshot() {
        let mut sh = shared();
        sh.tickers.insert("U".into(), ("EV".into(), Outcome::Away));
        let mut se = Sess::default();
        let mut b = Batch::default();
        absorb(&mut sh, &mut se, &snap(1, "T", json!([["0.4500", "100.00"]]), json!([["0.5200", "50.00"]])), 4_100, &mut b);
        absorb(&mut sh, &mut se, &snap(2, "U", json!([["0.5000", "10.00"]]), json!([["0.4700", "10.00"]])), 4_100, &mut b);
        absorb(&mut sh, &mut se, &delta(3, "T", "yes", "0.4600", "30.00", 5_000), 5_123, &mut b);
        assert!(b.gap.is_none());
        assert_eq!((se.book_sid, se.seq), (Some(5), Some(3)));
        // Kalshi's "ok" to a command on the stream (contracts added) takes the next number; the
        // change after it follows on. A reply on another stream (the ticker's) is not counted.
        absorb(&mut sh, &mut se, &json!({"id": 2, "sid": 5, "seq": 4, "type": "ok", "msg": {"market_tickers": ["V"]}}), 5_200, &mut b);
        absorb(&mut sh, &mut se, &json!({"id": 2, "sid": 9, "seq": 77, "type": "ok", "msg": {"market_tickers": ["V"]}}), 5_200, &mut b);
        absorb(&mut sh, &mut se, &delta(5, "T", "yes", "0.4600", "1.00", 5_250), 5_300, &mut b);
        assert!(b.gap.is_none(), "an ok is a numbered message, not a lost one");
        assert_eq!(se.seq, Some(5));
        assert_eq!(sh.books["T"].best().0.map(|x| (x * 100.0).round()), Some(46.0));
        // Each row carries the time its message was taken off the socket and Kalshi's own time
        // of the change; how far behind each message was is kept for the statistics.
        assert_eq!(book_of(b.rows.last().unwrap()), (Some(46.0), Some(48.0), Some(5_000), 5_123));
        assert_eq!((b.behind_change.clone(), b.behind_send.clone()), (vec![123, 50], vec![100, 100, 23, -50]));

        // Number 6 never comes.
        let mut b = Batch::default();
        absorb(&mut sh, &mut se, &delta(7, "U", "yes", "0.5100", "5.00", 6_000), 6_100, &mut b);
        assert_eq!(b.gap, Some((5, 7)));
        assert_eq!((sh.books["T"].best(), sh.books["U"].best()), ((None, None), (None, None)), "no book is read while out of step");
        assert!(sh.books["T"].levels(None, None).0.is_empty() && sh.books["T"].yes.is_empty());
        // Both contracts' book prices are written as unknown, at the moment the gap was seen.
        let unknown: Vec<_> = b.rows.iter().map(book_of).collect();
        assert_eq!(unknown, vec![(None, None, None, 6_100), (None, None, None, 6_100)]);
        assert_eq!(b.ignored, 1, "the change that revealed the gap is not applied to an emptied book");
        // Changes keep coming; they are dropped until the contract's own fresh snapshot.
        absorb(&mut sh, &mut se, &delta(8, "T", "yes", "0.4700", "5.00", 6_050), 6_150, &mut b);
        assert_eq!((b.ignored, sh.books["T"].yes.len()), (2, 0));
        absorb(&mut sh, &mut se, &snap(9, "T", json!([["0.4700", "40.00"]]), json!([["0.5100", "20.00"]])), 6_300, &mut b);
        absorb(&mut sh, &mut se, &delta(10, "T", "yes", "0.4800", "9.00", 6_350), 6_400, &mut b);
        assert_eq!(sh.books["T"].best().0.map(|x| (x * 100.0).round()), Some(48.0), "T is in step again");
        assert_eq!(sh.books["U"].best(), (None, None), "U waits for its own snapshot");
        assert_eq!(b.gap, Some((5, 7)), "one gap, reported once");
    }

    /// Kalshi's once-a-second ticker and its order book must name the same best prices within a
    /// second or two. A book that keeps naming others is remembered (and gets a fresh snapshot).
    #[test]
    fn a_book_that_disagrees_with_the_ticker_is_noticed() {
        let mut sh = shared();
        let mut se = Sess::default();
        let mut b = Batch::default();
        let tick = |bid: &str, ask: &str| json!({"type": "ticker", "sid": 1, "msg": {"market_ticker": "T", "yes_bid_dollars": bid, "yes_ask_dollars": ask, "ts_ms": 9_000}});
        absorb(&mut sh, &mut se, &snap(1, "T", json!([["0.4500", "100.00"], ["0.4600", "0.01"]]), json!([["0.5200", "50.00"]])), 1_000, &mut b);
        absorb(&mut sh, &mut se, &tick("0.4500", "0.4800"), 1_100, &mut b);
        assert!(sh.mismatch.is_empty());
        // The ticker counts the 0.01-contract crumb at 0.46 as the best bid; the book's own best
        // skips it. That is the same book, not a disagreement.
        absorb(&mut sh, &mut se, &tick("0.4600", "0.4800"), 1_200, &mut b);
        assert!(sh.mismatch.is_empty());
        // The ticker moves and the book does not follow.
        absorb(&mut sh, &mut se, &tick("0.5000", "0.5300"), 2_000, &mut b);
        assert_eq!(sh.mismatch.get("T"), Some(&2_000));
        absorb(&mut sh, &mut se, &tick("0.5100", "0.5300"), 9_000, &mut b);
        assert_eq!(sh.mismatch.get("T"), Some(&2_000), "since the first disagreement");
        // The book catches up: forgotten.
        absorb(&mut sh, &mut se, &delta(2, "T", "no", "0.5200", "-50.00", 9_100), 9_200, &mut b);
        absorb(&mut sh, &mut se, &delta(3, "T", "no", "0.4700", "80.00", 9_100), 9_200, &mut b);
        absorb(&mut sh, &mut se, &delta(4, "T", "yes", "0.5100", "30.00", 9_100), 9_200, &mut b);
        assert!(sh.mismatch.is_empty(), "{:?}", sh.mismatch);
        // A dropped connection forgets everything the socket built.
        absorb(&mut sh, &mut se, &tick("0.6000", "0.6300"), 9_300, &mut b);
        let rows = sh.unsync_all(9_400);
        assert!(sh.mismatch.is_empty() && !sh.books["T"].synced);
        assert_eq!(rows.iter().map(book_of).collect::<Vec<_>>(), vec![(None, None, None, 9_400)]);
    }

    /// Adding and removing sizes with two decimals leaves no level behind.
    #[test]
    fn a_level_taken_to_zero_is_gone() {
        let mut sh = shared();
        sh.book_msg("orderbook_snapshot", &json!({"market_ticker": "T", "yes_dollars_fp": [["0.3000", "0.10"]]}), None);
        for _ in 0..3 {
            sh.book_msg("orderbook_delta", &json!({"market_ticker": "T", "side": "yes", "price_dollars": "0.3000", "delta_fp": "0.20"}), None);
        }
        for d in ["-0.30", "-0.30", "-0.10"] {
            sh.book_msg("orderbook_delta", &json!({"market_ticker": "T", "side": "yes", "price_dollars": "0.3000", "delta_fp": d}), None);
        }
        assert!(sh.books["T"].yes.is_empty(), "{:?}", sh.books["T"].yes);
    }
}

