//! HTTP API. Live state comes from memory; history comes from SQLite.

use crate::engine::{EventState, QKey};
use crate::model::*;
use crate::{store, App};
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use futures::StreamExt;
use rusqlite::types::Value as SqlVal;
use serde::Deserialize;
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::convert::Infallible;
use std::sync::Arc;
use tokio_stream::wrappers::BroadcastStream;

type S = State<Arc<App>>;

pub fn router(app: Arc<App>) -> Router {
    Router::new()
        .route("/", get(|| async { asset("index.html", "text/html; charset=utf-8", include_str!("web/index.html")) }))
        .route("/assets/app.css", get(|| async { asset("app.css", "text/css; charset=utf-8", include_str!("web/app.css")) }))
        .route("/assets/app.js", get(|| async { asset("app.js", "text/javascript; charset=utf-8", include_str!("web/app.js")) }))
        .route("/assets/pages.js", get(|| async { asset("pages.js", "text/javascript; charset=utf-8", include_str!("web/pages.js")) }))
        .route("/assets/pages2.js", get(|| async { asset("pages2.js", "text/javascript; charset=utf-8", include_str!("web/pages2.js")) }))
        .route("/assets/pages3.js", get(|| async { asset("pages3.js", "text/javascript; charset=utf-8", include_str!("web/pages3.js")) }))
        .route("/assets/uplot.js", get(|| async { asset("vendor/uPlot.iife.min.js", "text/javascript; charset=utf-8", include_str!("web/vendor/uPlot.iife.min.js")) }))
        .route("/assets/uplot.css", get(|| async { asset("vendor/uPlot.min.css", "text/css; charset=utf-8", include_str!("web/vendor/uPlot.min.css")) }))
        .route("/assets/img/{name}", get(img))
        .route("/rankings", get(rankings))
        .route("/api", get(index))
        .route("/ui", get(ui))
        .route("/quotes", get(quotes))
        .route("/info", get(info))
        .route("/control/books/{name}", post(control_book))
        .route("/pm/settings", get(pm_settings_get).post(pm_settings_post))
        .route("/pm/account", get(pm_account))
        .route("/kalshi/settings", get(kalshi_settings_get).post(kalshi_settings_post))
        .route("/kalshi/account", get(kalshi_account))
        .route("/control/scope", post(control_scope))
        .route("/books", get(books))
        .route("/matches", get(matches))
        .route("/matches/{id}", get(match_detail))
        .route("/matches/{id}/changes", get(match_changes))
        .route("/matches/{into}/merge/{from}", post(merge))
        .route("/events/{book}/{event_id}/unlink", post(unlink))
        .route("/board", get(board))
        .route("/changes", get(changes))
        .route("/stream", get(stream))
        .route("/query", get(query))
        .route("/stats", get(stats))
        .route("/lag", get(lag_summary))
        .route("/lag/events", get(lag_events))
        .route("/lag/books", get(lag_books_summary))
        .route("/lag/books/events", get(lag_books_events))
        .route("/clock", get(|| async { Json(crate::clock::status()) }))
        .layer(axum::middleware::from_fn(local_cors))
        .with_state(app)
}

/// Let ui.html opened from disk (Origin "null") or from another localhost port read
/// the API. Other websites still can't read your odds data.
async fn local_cors(req: axum::extract::Request, next: axum::middleware::Next) -> Response {
    let origin = req.headers().get(axum::http::header::ORIGIN).cloned();
    let mut resp = next.run(req).await;
    if let Some(o) = origin {
        let s = o.to_str().unwrap_or("");
        let local = s == "null"
            || s.starts_with("file:")
            || s.starts_with("http://127.0.0.1")
            || s.starts_with("http://localhost")
            || s.starts_with("https://localhost");
        if local {
            resp.headers_mut().insert(axum::http::header::ACCESS_CONTROL_ALLOW_ORIGIN, o);
        }
    }
    resp
}

fn err(code: StatusCode, msg: impl Into<String>) -> Response {
    (code, Json(json!({"error": msg.into()}))).into_response()
}

async fn index() -> Json<Value> {
    Json(json!({
        "service": "oddsfeed",
        "endpoints": {
            "GET /ui": "live odds table in the browser",
            "GET /books": "per-book status: polls, errors, live events, last request time",
            "GET /matches?all=0&q=&book=": "matches (cross-book) currently live; best 1X2 across books",
            "GET /matches/{id}": "every current price for one match, per book",
            "GET /matches/{id}/changes?since=&market=&book=&limit=": "price history for one match (oldest first)",
            "GET /board?market=1x2|total|handicap|btts": "the live table: one row per match, prices per book",
            "GET /changes?since=&until=&book=&match=&market=&outcome=&kind=&limit=&order=asc|desc": "the change log",
            "GET /stream?match=&book=&market=": "server-sent events, one per price change as it happens",
            "GET /lag/books?since=&until=&min_jump=&window_s=&settle_s=&clock=&match=&book=": "book-to-book delay: per (trigger, follower) pair, how long after one book jumps the others follow",
            "GET /lag/books/events?trigger_book=&follower_book=&limit=": "each book-to-book jump behind the summary",
            "GET /query?sql=SELECT...&limit=": "read-only SQL over tables matches, book_events, changes",
            "GET /stats": "row counts and changes per book in the last 5 minutes",
            "POST /matches/{into}/merge/{from}": "fix a missed cross-book match",
            "POST /events/{book}/{event_id}/unlink": "fix a wrong cross-book match"
        },
        "notes": {
            "ts_ms": "ms since epoch when the response carrying the change finished arriving",
            "book_ts_ms": "the bookmaker's own change time, when its feed has one (kambi s-precision, sportybet/betika ms)",
            "line": "total: goal line; handicap: home team's handicap",
            "price null": "suspended or removed"
        }
    }))
}

async fn books(State(app): S) -> Json<Value> {
    let eng = app.engine.lock();
    let mut v: Vec<_> = eng.books.values().map(|b| b.status.clone()).collect();
    v.sort_by_key(|s| s.name);
    Json(json!({"started_ms": app.started_ms, "now_ms": now_ms(), "books": v}))
}

#[derive(Deserialize, Default)]
struct MatchesQ {
    all: Option<u8>,
    q: Option<String>,
    book: Option<String>,
    min_books: Option<usize>,
}

fn quote_json(k: &QKey, q: &crate::engine::QuoteState) -> Value {
    json!({
        "market": k.0, "line": line_from_key(k.1), "outcome": k.2,
        "price": q.price, "updated_ms": q.updated_ms, "book_ts_ms": q.book_ts
    })
}

fn live_events_of(app: &App, match_id: i64) -> Vec<(&'static str, EventSnapshot)> {
    let eng = app.engine.lock();
    let mut v = Vec::new();
    for (book, bs) in &eng.books {
        for es in bs.events.values() {
            if es.match_id == match_id {
                v.push((*book, EventSnapshot::from(es)));
            }
        }
    }
    v.sort_by_key(|x| x.0);
    v
}

struct EventSnapshot {
    meta: crate::engine::EventMeta,
    quotes: Vec<(QKey, crate::engine::QuoteState)>,
    last_seen_ms: Ms,
}
impl From<&EventState> for EventSnapshot {
    fn from(es: &EventState) -> Self {
        EventSnapshot {
            meta: es.meta.clone(),
            quotes: es.quotes.iter().map(|(k, q)| (*k, q.clone())).collect(),
            last_seen_ms: es.last_seen_ms,
        }
    }
}

async fn matches(State(app): S, Query(p): Query<MatchesQ>) -> Json<Value> {
    let eng = app.engine.lock();
    let needle = p.q.as_deref().map(|s| s.to_lowercase());
    // match id -> (book, &EventState)
    let mut by_match: BTreeMap<i64, Vec<(&'static str, &EventState)>> = BTreeMap::new();
    for (book, bs) in &eng.books {
        for es in bs.events.values() {
            by_match.entry(es.match_id).or_default().push((*book, es));
        }
    }
    let mut rows = Vec::new();
    let ids: Vec<i64> = if p.all == Some(1) {
        eng.matcher.matches.keys().copied().collect()
    } else {
        by_match.keys().copied().collect()
    };
    for id in ids {
        let Some(m) = eng.matcher.matches.get(&id) else { continue };
        if !eng.visible(id) && p.all != Some(1) {
            continue;
        }
        let evs = by_match.get(&id).cloned().unwrap_or_default();
        if let Some(b) = &p.book {
            if !evs.iter().any(|(bk, _)| bk == b) {
                continue;
            }
        }
        if evs.len() < p.min_books.unwrap_or(0) {
            continue;
        }
        if let Some(n) = &needle {
            let hay = format!("{} {} {}", m.home, m.away, m.league).to_lowercase();
            let hay2: String = evs.iter().map(|(_, e)| format!(" {} {}", e.meta.home, e.meta.away)).collect::<String>().to_lowercase();
            if !hay.contains(n.as_str()) && !hay2.contains(n.as_str()) {
                continue;
            }
        }
        let mut best: BTreeMap<&str, (f64, &str)> = BTreeMap::new();
        for (book, es) in &evs {
            for (k, q) in &es.quotes {
                if k.0 != Market::X12 {
                    continue;
                }
                if let Some(pr) = q.price {
                    let e = best.entry(k.2.as_str()).or_insert((0.0, book));
                    if pr > e.0 {
                        *e = (pr, book);
                    }
                }
            }
        }
        let best_json: BTreeMap<&str, Value> = best.iter().map(|(o, (pr, b))| (*o, json!({"price": pr, "book": b}))).collect();
        let overround = if best.len() == 3 { Some(best.values().map(|(p, _)| 1.0 / p).sum::<f64>()) } else { None };
        let score = evs.iter().find_map(|(_, e)| e.meta.score.clone());
        let clock = evs.iter().find_map(|(_, e)| e.meta.clock.clone());
        rows.push(json!({
            "id": m.id, "sport": m.sport, "home": m.home, "away": m.away, "league": m.league,
            "start_ms": m.start_ms, "sr_id": m.sr_id, "score": score, "clock": clock,
            "live": evs.iter().any(|(_, e)| e.meta.live),
            "books": evs.iter().map(|(b, e)| json!({"book": b, "event_id": e.meta.id, "home": e.meta.home, "away": e.meta.away, "league": e.meta.league})).collect::<Vec<_>>(),
            "best_1x2": best_json,
            "best_1x2_book_sum": overround,
        }));
    }
    rows.sort_by(|a, b| {
        let na = a["books"].as_array().map_or(0, |x| x.len());
        let nb = b["books"].as_array().map_or(0, |x| x.len());
        nb.cmp(&na).then(a["start_ms"].as_i64().cmp(&b["start_ms"].as_i64()))
    });
    Json(json!({"now_ms": now_ms(), "count": rows.len(), "matches": rows}))
}

async fn match_detail(State(app): S, Path(id): Path<i64>) -> Response {
    let info = app.engine.lock().matcher.matches.get(&id).cloned();
    let evs = live_events_of(&app, id);
    if info.is_none() && evs.is_empty() {
        return err(StatusCode::NOT_FOUND, "match not in memory; use /changes?match= for history");
    }
    let books: Vec<Value> = evs
        .iter()
        .map(|(b, e)| {
            json!({
                "book": b, "event": e.meta, "last_seen_ms": e.last_seen_ms,
                "quotes": e.quotes.iter().map(|(k, q)| quote_json(k, q)).collect::<Vec<_>>()
            })
        })
        .collect();
    Json(json!({"now_ms": now_ms(), "match": info, "books": books})).into_response()
}

#[derive(Deserialize, Default)]
struct ChangesQ {
    since: Option<i64>,
    until: Option<i64>,
    book: Option<String>,
    #[serde(rename = "match")]
    match_id: Option<i64>,
    market: Option<String>,
    outcome: Option<String>,
    kind: Option<String>,
    line: Option<f64>,
    limit: Option<usize>,
    order: Option<String>,
}

async fn run_changes(app: Arc<App>, p: ChangesQ) -> Response {
    let mut sql = String::from(
        "SELECT c.id, c.ts_ms, c.book, c.book_event_id, c.match_id, c.market, c.line, c.outcome, c.kind, c.old_price, c.new_price, c.book_ts_ms FROM changes c WHERE 1=1",
    );
    let mut args: Vec<SqlVal> = Vec::new();
    let mut add = |cond: &str, v: SqlVal| {
        args.push(v);
        sql.push_str(&format!(" AND {cond} ?{}", args.len()));
    };
    if let Some(x) = p.since {
        add("c.ts_ms >=", SqlVal::Integer(x));
    }
    if let Some(x) = p.until {
        add("c.ts_ms <", SqlVal::Integer(x));
    }
    if let Some(x) = p.book {
        add("c.book =", SqlVal::Text(x));
    }
    if let Some(x) = p.match_id {
        add("c.match_id =", SqlVal::Integer(x));
    }
    if let Some(x) = p.market {
        add("c.market =", SqlVal::Text(x));
    }
    if let Some(x) = p.outcome {
        add("c.outcome =", SqlVal::Text(x));
    }
    if let Some(x) = p.kind {
        add("c.kind =", SqlVal::Text(x));
    }
    if let Some(x) = p.line {
        add("c.line =", SqlVal::Real(x));
    }
    let asc = match p.order.as_deref() {
        Some("asc") => true,
        Some("desc") => false,
        _ => p.since.is_some(),
    };
    let limit = p.limit.unwrap_or(500).min(100_000);
    sql.push_str(if asc { " ORDER BY c.id ASC" } else { " ORDER BY c.id DESC" });
    sql.push_str(&format!(" LIMIT {limit}"));
    let path = app.db_path.clone();
    match tokio::task::spawn_blocking(move || store::query_json(&path, &sql, &args, limit)).await {
        Ok(Ok(rows)) => Json(json!({"count": rows.len(), "changes": rows})).into_response(),
        Ok(Err(e)) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn changes(State(app): S, Query(p): Query<ChangesQ>) -> Response {
    run_changes(app, p).await
}

async fn match_changes(State(app): S, Path(id): Path<i64>, Query(mut p): Query<ChangesQ>) -> Response {
    p.match_id = Some(id);
    if p.order.is_none() {
        p.order = Some("asc".into());
    }
    if p.limit.is_none() {
        p.limit = Some(10_000);
    }
    run_changes(app, p).await
}

#[derive(Deserialize, Default)]
struct BoardQ {
    market: Option<String>,
    min_books: Option<usize>,
}

/// The live table: one row per match, each book's current prices for one market.
async fn board(State(app): S, Query(p): Query<BoardQ>) -> Response {
    let market = match Market::parse(p.market.as_deref().unwrap_or("1x2")) {
        Some(m) => m,
        None => return err(StatusCode::BAD_REQUEST, "market must be 1x2, total, handicap or btts"),
    };
    let eng = app.engine.lock();
    let mut by_match: BTreeMap<i64, Vec<(&'static str, &EventState)>> = BTreeMap::new();
    for (book, bs) in &eng.books {
        for es in bs.events.values() {
            by_match.entry(es.match_id).or_default().push((*book, es));
        }
    }
    let mut rows = Vec::new();
    for (id, mut evs) in by_match {
        if evs.len() < p.min_books.unwrap_or(1) {
            continue;
        }
        if !eng.visible(id) {
            continue;
        }
        evs.sort_by_key(|x| x.0);
        let Some(m) = eng.matcher.matches.get(&id) else { continue };
        let mut per_book = serde_json::Map::new();
        let mut last_change: Ms = 0;
        for (book, es) in &evs {
            // line -> outcome -> price
            let mut lines: BTreeMap<String, serde_json::Map<String, Value>> = BTreeMap::new();
            for (k, q) in &es.quotes {
                if k.0 != market {
                    continue;
                }
                last_change = last_change.max(q.updated_ms);
                let lk = match line_from_key(k.1) {
                    Some(l) => format!("{l}"),
                    None => "-".into(),
                };
                lines.entry(lk).or_default().insert(k.2.as_str().into(), json!(q.price));
            }
            let v = if market == Market::X12 || market == Market::Btts {
                lines.remove("-").map(Value::Object).unwrap_or(Value::Null)
            } else {
                Value::Object(lines.into_iter().map(|(k, v)| (k, Value::Object(v))).collect())
            };
            per_book.insert(book.to_string(), v);
        }
        rows.push(json!({
            "id": id, "home": m.home, "away": m.away, "league": m.league, "start_ms": m.start_ms,
            "score": evs.iter().find_map(|(_, e)| e.meta.score.clone()),
            "clock": evs.iter().find_map(|(_, e)| e.meta.clock.clone()),
            "last_change_ms": last_change,
            "live": evs.iter().any(|(_, e)| e.meta.live),
            "books": per_book,
        }));
    }
    rows.sort_by(|a, b| {
        let na = a["books"].as_object().map_or(0, |x| x.len());
        let nb = b["books"].as_object().map_or(0, |x| x.len());
        nb.cmp(&na).then(a["home"].as_str().cmp(&b["home"].as_str()))
    });
    let books: Vec<&str> = eng.books.keys().copied().collect();
    Json(json!({"now_ms": now_ms(), "market": market, "books": books, "rows": rows})).into_response()
}

#[derive(Deserialize, Default)]
struct StreamQ {
    #[serde(rename = "match")]
    match_id: Option<i64>,
    book: Option<String>,
    market: Option<String>,
}

async fn stream(State(app): S, Query(p): Query<StreamQ>) -> Sse<impl futures::Stream<Item = Result<Event, Infallible>>> {
    let rx = app.live.subscribe();
    let s = BroadcastStream::new(rx).filter_map(move |r| {
        let out = match r {
            Ok(c) => {
                let keep = p.match_id.map_or(true, |m| m == c.match_id)
                    && p.book.as_deref().map_or(true, |b| b == c.book)
                    && p.market.as_deref().map_or(true, |m| m == c.market.as_str());
                if keep {
                    Some(Ok(Event::default().event("change").data(serde_json::to_string(&*c).unwrap_or_default())))
                } else {
                    None
                }
            }
            // Slow client fell behind; tell it how many it missed instead of dropping silently.
            Err(tokio_stream::wrappers::errors::BroadcastStreamRecvError::Lagged(n)) => {
                Some(Ok(Event::default().event("lagged").data(n.to_string())))
            }
        };
        futures::future::ready(out)
    });
    Sse::new(s).keep_alive(KeepAlive::default())
}

#[derive(Deserialize)]
struct SqlQ {
    sql: String,
    limit: Option<usize>,
}

async fn query(State(app): S, Query(p): Query<SqlQ>) -> Response {
    let limit = p.limit.unwrap_or(1000).min(100_000);
    let path = app.db_path.clone();
    match tokio::task::spawn_blocking(move || store::query_json(&path, &p.sql, &[], limit)).await {
        Ok(Ok(rows)) => Json(json!({"count": rows.len(), "rows": rows})).into_response(),
        Ok(Err(e)) => err(StatusCode::BAD_REQUEST, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn stats(State(app): S) -> Response {
    let path = app.db_path.clone();
    let since = now_ms() - 5 * 60 * 1000;
    let res = tokio::task::spawn_blocking(move || -> anyhow::Result<Value> {
        let totals = store::query_json(
            &path,
            "SELECT (SELECT COUNT(*) FROM changes) AS changes, (SELECT COUNT(*) FROM matches) AS matches, (SELECT COUNT(*) FROM book_events) AS book_events",
            &[],
            1,
        )?;
        let per_book = store::query_json(
            &path,
            "SELECT book, COUNT(*) AS changes_5m, SUM(kind='move') AS moves_5m FROM changes WHERE ts_ms >= ?1 GROUP BY book ORDER BY 2 DESC",
            &[SqlVal::Integer(since)],
            100,
        )?;
        Ok(json!({"totals": totals.first(), "last_5_minutes": per_book}))
    })
    .await;
    match res {
        Ok(Ok(v)) => Json(v).into_response(),
        Ok(Err(e)) => err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn merge(State(app): S, Path((into, from)): Path<(i64, i64)>) -> Response {
    let res = app.engine.lock().merge(into, from);
    match res {
        Ok(writes) => {
            let n = writes.len();
            app.emit(writes).await;
            Json(json!({"ok": true, "moved_events": n})).into_response()
        }
        Err(e) => err(StatusCode::BAD_REQUEST, e),
    }
}

async fn unlink(State(app): S, Path((book, event_id)): Path<(String, String)>) -> Response {
    let res = app.engine.lock().unlink(&book, &event_id);
    match res {
        Ok((id, writes)) => {
            app.emit(writes).await;
            Json(json!({"ok": true, "new_match_id": id})).into_response()
        }
        Err(e) => err(StatusCode::BAD_REQUEST, e),
    }
}

#[derive(Deserialize, Default)]
struct LagQ {
    /// ms since epoch; default 24 h ago
    since: Option<i64>,
    until: Option<i64>,
    /// minimum implied-probability move that counts as a jump (default 0.04)
    min_jump: Option<f64>,
    /// how far either side of a jump to look for the other side's reaction (default 90)
    window_s: Option<i64>,
    /// how long a move may take to complete (default 10)
    settle_s: Option<i64>,
    /// "receive" (default: when we saw it) or "source" (each feed's own timestamp where it has one)
    clock: Option<String>,
    #[serde(rename = "match")]
    match_id: Option<i64>,
    book: Option<String>,
    trigger: Option<String>,
    trigger_book: Option<String>,
    follower_book: Option<String>,
    limit: Option<usize>,
}

impl LagQ {
    fn params(&self) -> crate::lag::Params {
        let now = now_ms();
        crate::lag::Params {
            since: self.since.unwrap_or(now - 24 * 3600 * 1000),
            until: self.until.unwrap_or(now + 1),
            min_jump: self.min_jump.unwrap_or(0.04).max(0.005),
            window_ms: self.window_s.unwrap_or(90).clamp(5, 900) * 1000,
            settle_ms: self.settle_s.unwrap_or(10).clamp(1, 120) * 1000,
            source_clock: self.clock.as_deref() == Some("source"),
            match_id: self.match_id,
            book: self.book.clone(),
        }
    }
}

async fn lag_run(app: &Arc<App>, q: &LagQ) -> Result<Vec<crate::lag::LagEvent>, Response> {
    let path = app.db_path.clone();
    let p = q.params();
    match tokio::task::spawn_blocking(move || crate::lag::compute(&path, &p)).await {
        Ok(Ok(v)) => Ok(v),
        Ok(Err(e)) => Err(err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}"))),
        Err(e) => Err(err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string())),
    }
}

/// Per book: how long after a bookmaker jump Polymarket follows, and vice versa.
async fn lag_summary(State(app): S, Query(q): Query<LagQ>) -> Response {
    let evs = match lag_run(&app, &q).await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let p = q.params();
    let matches: std::collections::BTreeSet<i64> = evs.iter().map(|e| e.match_id).collect();
    Json(json!({
        "since": p.since, "until": p.until, "min_jump": p.min_jump,
        "window_ms": p.window_ms, "settle_ms": p.settle_ms,
        "clock": if p.source_clock { "source" } else { "receive" },
        "matches": matches.len(),
        "sign": "delay_ms = polymarket time - book time; positive means Polymarket was later",
        "summary": crate::lag::summarize(&evs),
    }))
    .into_response()
}

async fn lag_events(State(app): S, Query(q): Query<LagQ>) -> Response {
    let mut evs = match lag_run(&app, &q).await {
        Ok(v) => v,
        Err(r) => return r,
    };
    if let Some(t) = &q.trigger {
        evs.retain(|e| e.trigger == t.as_str());
    }
    let n = evs.len();
    let limit = q.limit.unwrap_or(1000);
    if evs.len() > limit {
        evs.drain(..evs.len() - limit);
    }
    Json(json!({"count": n, "events": evs})).into_response()
}

async fn lag_books_run(app: &Arc<App>, q: &LagQ) -> Result<Vec<crate::lag::BookLagEvent>, Response> {
    let path = app.db_path.clone();
    let p = q.params();
    match tokio::task::spawn_blocking(move || crate::lag::compute_books(&path, &p)).await {
        Ok(Ok(v)) => Ok(v),
        Ok(Err(e)) => Err(err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}"))),
        Err(e) => Err(err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string())),
    }
}

/// Per (trigger, follower) book pair: how long after one book jumps the other follows.
async fn lag_books_summary(State(app): S, Query(q): Query<LagQ>) -> Response {
    let evs = match lag_books_run(&app, &q).await {
        Ok(v) => v,
        Err(r) => return r,
    };
    let p = q.params();
    let matches: std::collections::BTreeSet<i64> = evs.iter().map(|e| e.match_id).collect();
    Json(json!({
        "since": p.since, "until": p.until, "min_jump": p.min_jump,
        "window_ms": p.window_ms, "settle_ms": p.settle_ms,
        "clock": if p.source_clock { "source" } else { "receive" },
        "matches": matches.len(),
        "sign": "delay_ms = follower time - trigger time; positive means the follower was later",
        "summary": crate::lag::summarize_books(&evs),
    }))
    .into_response()
}

async fn lag_books_events(State(app): S, Query(q): Query<LagQ>) -> Response {
    let mut evs = match lag_books_run(&app, &q).await {
        Ok(v) => v,
        Err(r) => return r,
    };
    if let Some(t) = &q.trigger_book {
        evs.retain(|e| e.trigger_book == *t);
    }
    if let Some(f) = &q.follower_book {
        evs.retain(|e| e.follower_book == *f);
    }
    let n = evs.len();
    let limit = q.limit.unwrap_or(1000);
    if evs.len() > limit {
        evs.drain(..evs.len() - limit);
    }
    Json(json!({"count": n, "events": evs})).into_response()
}

/// Web files are compiled in. Set ODDSFEED_WEB_DIR=<repo>/src/web to serve them from disk while editing.
fn asset(name: &'static str, ct: &'static str, body: &'static str) -> Response {
    let headers = [(axum::http::header::CONTENT_TYPE, ct), (axum::http::header::CACHE_CONTROL, "no-cache")];
    if let Ok(dir) = std::env::var("ODDSFEED_WEB_DIR") {
        if let Ok(text) = std::fs::read_to_string(std::path::Path::new(&dir).join(name)) {
            return (headers, text).into_response();
        }
    }
    (headers, body).into_response()
}

/// Every current quote as flat rows: the spreadsheet view.
async fn quotes(State(app): S) -> Json<Value> {
    let eng = app.engine.lock();
    let mut rows = Vec::new();
    for (book, bs) in &eng.books {
        for es in bs.events.values() {
            if !eng.visible(es.match_id) {
                continue;
            }
            let m = eng.matcher.matches.get(&es.match_id);
            for (k, q) in &es.quotes {
                rows.push(json!([
                    es.match_id,
                    m.map(|m| m.home.as_str()).unwrap_or(&es.meta.home),
                    m.map(|m| m.away.as_str()).unwrap_or(&es.meta.away),
                    es.meta.league,
                    es.meta.score,
                    es.meta.clock,
                    book,
                    k.0,
                    line_from_key(k.1),
                    k.2,
                    q.price,
                    q.prev,
                    q.updated_ms,
                    q.book_ts,
                    es.meta.live,
                    m.and_then(|m| m.start_ms).or(es.meta.start_ms),
                ]));
            }
        }
    }
    Json(json!({
        "now_ms": now_ms(),
        "columns": ["match_id","home","away","league","score","clock","book","market","line","outcome","price","prev","updated_ms","book_ts_ms","live","start_ms"],
        "rows": rows,
    }))
}

/// Service facts for Settings / Dashboard: uptime, database size, clock, config.
async fn info(State(app): S) -> Json<Value> {
    let db = &app.db_path;
    let size: u64 = ["", "-wal", "-shm"]
        .iter()
        .filter_map(|s| std::fs::metadata(format!("{}{}", db.display(), s)).ok())
        .map(|m| m.len())
        .sum();
    Json(json!({
        "version": env!("CARGO_PKG_VERSION"),
        "started_ms": app.started_ms,
        "now_ms": now_ms(),
        "db_path": std::fs::canonicalize(db).unwrap_or(db.clone()).display().to_string().trim_start_matches(r"\\?\").to_string(),
        "db_bytes": size,
        "bind": app.cfg.bind,
        "include_esoccer": app.cfg.include_esoccer,
        "prediction_only": app.engine.lock().prediction_only,
        "prematch_hours": app.cfg.prematch_hours,
        "clock": crate::clock::status(),
    }))
}

#[derive(Deserialize)]
struct ControlQ {
    paused: Option<bool>,
    poll_ms: Option<u64>,
}

async fn control_book(State(app): S, Path(name): Path<String>, Json(b): Json<ControlQ>) -> Response {
    match app.engine.lock().control(&name, b.paused, b.poll_ms) {
        Some(st) => Json(json!({"ok": true, "status": st})).into_response(),
        None => err(StatusCode::NOT_FOUND, "unknown source"),
    }
}

async fn pm_settings_get(State(app): S) -> Json<Value> {
    Json(crate::pmacct::public_view(&crate::pmacct::load(&app.db_path)))
}

async fn pm_settings_post(State(app): S, Json(edit): Json<Value>) -> Response {
    let c = crate::pmacct::merge(crate::pmacct::load(&app.db_path), &edit);
    match crate::pmacct::save(&app.db_path, &c) {
        Ok(()) => Json(crate::pmacct::public_view(&c)).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")),
    }
}

async fn pm_account(State(app): S) -> Json<Value> {
    let c = crate::pmacct::load(&app.db_path);
    let http = crate::books::http_client();
    Json(crate::pmacct::snapshot(&http, &c).await)
}

#[derive(Deserialize)]
struct ScopeQ {
    prediction_only: bool,
}

/// Switch between "only matches on Polymarket/Kalshi plus a bookmaker" and "everything".
async fn control_scope(State(app): S, Json(b): Json<ScopeQ>) -> Json<Value> {
    let mut eng = app.engine.lock();
    eng.prediction_only = b.prediction_only;
    eng.qualified.clear();
    Json(json!({"ok": true, "prediction_only": eng.prediction_only}))
}

async fn kalshi_settings_get(State(app): S) -> Json<Value> {
    Json(crate::kalacct::public_view(&crate::kalacct::load(&app.db_path)))
}

async fn kalshi_settings_post(State(app): S, Json(edit): Json<Value>) -> Response {
    let c = crate::kalacct::merge(crate::kalacct::load(&app.db_path), &edit);
    match crate::kalacct::save(&app.db_path, &c) {
        Ok(()) => Json(crate::kalacct::public_view(&c)).into_response(),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")),
    }
}

async fn kalshi_account(State(app): S) -> Json<Value> {
    let c = crate::kalacct::load(&app.db_path);
    let http = crate::books::http_client();
    Json(crate::kalacct::snapshot(&http, &c).await)
}

/// Logo and source icons, compiled in.
async fn img(Path(name): Path<String>) -> Response {
    let bytes: Option<&'static [u8]> = match name.as_str() {
        "logo.png" => Some(include_bytes!("web/img/logo.png")),
        "favicon.png" => Some(include_bytes!("web/img/favicon.png")),
        "polymarket.png" => Some(include_bytes!("web/img/polymarket.png")),
        "unibet.png" => Some(include_bytes!("web/img/unibet.png")),
        "sportybet.png" => Some(include_bytes!("web/img/sportybet.png")),
        "betpawa.png" => Some(include_bytes!("web/img/betpawa.png")),
        "betika.png" => Some(include_bytes!("web/img/betika.png")),
        "linebet.png" => Some(include_bytes!("web/img/linebet.png")),
        "fonbet.png" => Some(include_bytes!("web/img/fonbet.png")),
        "vinn.png" => Some(include_bytes!("web/img/vinn.png")),
        "kalshi.png" => Some(include_bytes!("web/img/kalshi.png")),
        _ => None,
    };
    match bytes {
        Some(b) => ([(axum::http::header::CONTENT_TYPE, "image/png"), (axum::http::header::CACHE_CONTROL, "max-age=86400")], b).into_response(),
        None => err(StatusCode::NOT_FOUND, "no such image"),
    }
}

/// Who moves first across all sources, plus how often Polymarket can still be bought before it reacts.
async fn rankings(State(app): S, Query(q): Query<LagQ>) -> Response {
    let path = app.db_path.clone();
    let p = q.params();
    let limit = q.limit.unwrap_or(400);
    match tokio::task::spawn_blocking(move || crate::rank::compute(&path, &p)).await {
        Ok(Ok(mut r)) => {
            let n = r.events.len();
            if n > limit {
                r.events.drain(..n - limit);
            }
            Json(json!({"since": q.params().since, "event_count": n, "rankings": r})).into_response()
        }
        Ok(Err(e)) => err(StatusCode::INTERNAL_SERVER_ERROR, format!("{e:#}")),
        Err(e) => err(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()),
    }
}

async fn ui() -> Html<&'static str> {
    Html(include_str!("ui.html"))
}
