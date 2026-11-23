//! Trade review (page `#/review`, pages7.js): the owner marks on a game's chart what the bots should
//! have seen and done, so the move detection can be checked and improved against it.
//!
//! A mark (`trade_labels` row, details in `data`) can hold:
//! * a MOVE PAIR: one line painted along a bookmaker's move and one along Polymarket's, read as the
//!   same movement. On save the recorded prices around them give the delay (each line timed where it
//!   crossed halfway, Polymarket minus bookmaker), the size of Polymarket's move against the
//!   bookmaker's, and the correlation of the two over the window, at lag 0 and at the best lag.
//! * TRADE POINTS: where to buy and sell and the most to pay (`cap`, the price of the token bought:
//!   the outcome's Yes, or its No), with the prices there taken from the recording.
//! * a link to a bot trade (`trade_id`) it judges: right, wrong, or right idea at the wrong points.
//!
//! Kinds: move (pair only), buy (missed buy), adjust (bot trade, corrected points), no_buy (bot trade
//! that should not have happened), ok (bot trade that was right).
//!
//! Every mark keeps a copy of the prices it was made on (`snapshot`, `analysis.series`), so it can
//! be studied after the prune job has removed the game's price changes. All marks are also written to
//! `<db dir>/trade_labels.json` on every change.

use crate::model::*;
use anyhow::{anyhow, bail, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use parking_lot::Mutex;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::{Arc, LazyLock};

const PM: &str = "polymarket";
pub const KINDS: &[&str] = &["move", "buy", "adjust", "no_buy", "ok", "note"];
/// Prices kept with a move pair: this long before and after the painted lines.
const PAD_MS: Ms = 60_000;
/// Lags tried for the best correlation, each way.
const MAX_LAG_S: i64 = 60;

pub fn init(db: &Path) -> Result<()> {
    let c = Connection::open(db)?;
    c.busy_timeout(std::time::Duration::from_secs(10))?;
    c.execute_batch(
        "CREATE TABLE IF NOT EXISTS trade_labels(id INTEGER PRIMARY KEY, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,
             match_id INTEGER NOT NULL, kind TEXT NOT NULL, outcome TEXT NOT NULL, data TEXT NOT NULL);
         CREATE INDEX IF NOT EXISTS trade_labels_match ON trade_labels(match_id);
         CREATE TABLE IF NOT EXISTS review_imports(id INTEGER PRIMARY KEY, mac_match_id INTEGER NOT NULL UNIQUE, imported_ms INTEGER NOT NULL,
             sport TEXT, league TEXT, home TEXT NOT NULL, away TEXT NOT NULL, start_ms INTEGER,
             trades INTEGER NOT NULL, bots TEXT, real INTEGER NOT NULL, last_ms INTEGER, pnl REAL, books TEXT);
         CREATE TABLE IF NOT EXISTS review_import_data(id INTEGER PRIMARY KEY, data TEXT NOT NULL);",
    )?;
    // The first imports (23 Nov) kept each game's prices in review_imports itself: 39 MB over 138 rows, so the
    // Bought list (summary columns only) read scattered pages and took 6 s on a cold disk cache. Moved out once.
    let old: bool = c.prepare("SELECT 1 FROM pragma_table_info('review_imports') WHERE name = 'data'")?.exists([])?;
    if old {
        let moved = c.execute_batch(
            "BEGIN;
             INSERT OR REPLACE INTO review_import_data(id, data) SELECT id, data FROM review_imports;
             ALTER TABLE review_imports DROP COLUMN data;
             COMMIT;",
        );
        match moved {
            Ok(()) => tracing::info!("trade review: imported games' prices moved to review_import_data"),
            Err(e) => {
                let _ = c.execute_batch("ROLLBACK;");
                tracing::warn!("trade review: could not move imported prices out of review_imports: {e:#}");
            }
        }
    }
    std::thread::Builder::new().name("review-replay".into()).spawn(refresh_replay)?;
    Ok(())
}

fn ro(db: &Path) -> Result<Connection> {
    crate::store::open_ro(db)
}

/// The replay paper bot's trades: the snipe bot's current rules run over every game recorded in this
/// database (adaptive.rs, the trades behind the Snipe window and the Telegram paper alerts). They are
/// the "acted on" games of a machine whose bots have not traded (this PC: the bots trade on the Mac).
/// Never built on a request: building it backtests every recorded game (about 5 s on 23 Nov), and doing
/// that inside the request made the list and every game page wait that long once a minute. A background
/// thread (`refresh_replay`) keeps it fresh; a request takes the latest copy at once.
pub fn replay_trades() -> Arc<Vec<Value>> {
    ASKED.store(now_ms(), Ordering::Relaxed);
    REPLAY.lock().1.clone()
}

static REPLAY: LazyLock<Mutex<(Ms, Arc<Vec<Value>>)>> = LazyLock::new(|| Mutex::new((0, Arc::new(Vec::new()))));
/// When the page last asked for the replay trades.
static ASKED: AtomicI64 = AtomicI64::new(0);

/// Rebuilds the replay trades every minute while the page is in use (asked in the last 10 minutes), every
/// 10 s while they are still empty and in use, and every 30 s while empty in the first 15 minutes after a
/// start (so the first visit has them).
fn refresh_replay() {
    let start = now_ms();
    loop {
        let (at, empty) = {
            let g = REPLAY.lock();
            (g.0, g.1.is_empty())
        };
        let now = now_ms();
        let in_use = now - ASKED.load(Ordering::Relaxed) < 600_000;
        let due = (in_use && (now - at > 60_000 || (empty && now - at > 10_000))) || (empty && now - start < 900_000 && now - at > 30_000);
        if due {
            let v = crate::adaptive::closed_since(0);
            *REPLAY.lock() = (now_ms(), Arc::new(v));
        }
        std::thread::sleep(std::time::Duration::from_secs(2));
    }
}

/// A replay trade as the page shows a bot trade (id "r<index>", bot "replay").
fn replay_row(i: usize, t: &Value) -> Value {
    json!({"id": format!("r{i}"), "bot": "replay", "real": false, "ts_ms": t["ts"], "closed_ms": t["exit_ts"], "data": t})
}

/// Games to review. kind "bought": games a bot traded (bot_trades) or the replay paper bot traded.
/// kind "other": started games with a Polymarket market and at least one bookmaker that neither
/// traded. `q` matches team or league, `book` keeps games that bookmaker listed, `random` picks one
/// at random instead of newest first.
pub fn games(db: &Path, kind: &str, q: &str, book: &str, random: bool, limit: usize, replay: &[Value]) -> Result<Value> {
    let c = ro(db)?;
    let like = if q.trim().is_empty() { String::new() } else { format!("%{}%", q.trim()) };
    // Replay trades per game: (trades, P&L, last decision).
    let mut rep: BTreeMap<i64, (i64, f64, i64)> = BTreeMap::new();
    for t in replay {
        if let Some(m) = t["match_id"].as_i64() {
            let e = rep.entry(m).or_insert((0, 0.0, 0));
            e.0 += 1;
            e.1 += t["pnl"].as_f64().unwrap_or(0.0);
            e.2 = e.2.max(t["ts"].as_i64().unwrap_or(0));
        }
    }
    let ids = rep.keys().map(|k| k.to_string()).collect::<Vec<_>>().join(",");
    let filters = "(?1 = '' OR m.home LIKE ?1 OR m.away LIKE ?1 OR m.league LIKE ?1)
               AND (?2 = '' OR m.id IN (SELECT match_id FROM book_events WHERE book = ?2))";
    let cols = "m.id, m.sport, m.home, m.away, m.league, m.start_ms";
    // Marks: how many, their kinds, and how many trades of the game have one.
    let marks = "(SELECT COUNT(*) FROM trade_labels l WHERE l.match_id = m.id),
                 (SELECT GROUP_CONCAT(DISTINCT l.kind) FROM trade_labels l WHERE l.match_id = m.id),
                 (SELECT COUNT(DISTINCT COALESCE(json_extract(l.data, '$.trade_id'), json_extract(l.data, '$.trade.data.ts'))) FROM trade_labels l
                   WHERE l.match_id = m.id AND (json_extract(l.data, '$.trade_id') IS NOT NULL OR json_extract(l.data, '$.trade') IS NOT NULL))";
    let tail = format!("{marks}, (SELECT GROUP_CONCAT(DISTINCT be.book) FROM book_events be WHERE be.match_id = m.id)");
    let row = |r: &rusqlite::Row| -> rusqlite::Result<Value> {
        Ok(json!({
            "id": r.get::<_, i64>(0)?, "sport": r.get::<_, Option<String>>(1)?, "home": r.get::<_, String>(2)?,
            "away": r.get::<_, String>(3)?, "league": r.get::<_, Option<String>>(4)?, "start_ms": r.get::<_, Option<i64>>(5)?,
            "trades": r.get::<_, i64>(6)?, "bots": r.get::<_, Option<String>>(7)?, "real": r.get::<_, Option<i64>>(8)?.map(|x| x > 0),
            "last_ms": r.get::<_, Option<i64>>(9)?, "pnl": r.get::<_, Option<f64>>(10)?, "labels": r.get::<_, i64>(11)?,
            "kinds": r.get::<_, Option<String>>(12)?.unwrap_or_default().split(',').filter(|k| !k.is_empty()).map(String::from).collect::<Vec<_>>(),
            "marked_trades": r.get::<_, i64>(13)?,
            "books": r.get::<_, Option<String>>(14)?.unwrap_or_default().split(',').filter(|b| !b.is_empty() && *b != PM && *b != "kalshi").map(String::from).collect::<Vec<_>>(),
        }))
    };
    let mut rows: Vec<Value> = if kind == "bought" {
        let mut st = c.prepare(&format!(
            "SELECT {cols}, COUNT(t.id), GROUP_CONCAT(DISTINCT t.bot), MAX(t.real), MAX(t.ts_ms), SUM(json_extract(t.data, '$.pnl')), {tail}
             FROM bot_trades t JOIN matches m ON m.id = t.match_id WHERE {filters} GROUP BY m.id"
        ))?;
        let mut v = st.query_map(params![like, book], row)?.collect::<rusqlite::Result<Vec<_>>>()?;
        // Add the replay's trades to those games, and its other games as rows of their own.
        for g in &mut v {
            if let Some((n, pnl, last)) = g["id"].as_i64().and_then(|id| rep.get(&id)) {
                g["trades"] = json!(g["trades"].as_i64().unwrap_or(0) + n);
                g["pnl"] = json!(g["pnl"].as_f64().unwrap_or(0.0) + pnl);
                g["last_ms"] = json!(g["last_ms"].as_i64().unwrap_or(0).max(*last));
                g["bots"] = json!(format!("{},replay", g["bots"].as_str().unwrap_or("")));
            }
        }
        if !ids.is_empty() {
            let have: BTreeSet<i64> = v.iter().filter_map(|g| g["id"].as_i64()).collect();
            let mut st = c.prepare(&format!("SELECT {cols}, 0, 'replay', 0, NULL, NULL, {tail} FROM matches m WHERE m.id IN ({ids}) AND {filters}"))?;
            for mut g in st.query_map(params![like, book], row)?.collect::<rusqlite::Result<Vec<_>>>()? {
                let id = g["id"].as_i64().unwrap_or(0);
                if have.contains(&id) {
                    continue;
                }
                if let Some((n, pnl, last)) = rep.get(&id) {
                    g["trades"] = json!(n);
                    g["pnl"] = json!(pnl);
                    g["last_ms"] = json!(last);
                }
                v.push(g);
            }
        }
        // Imported games (another machine's trades): summary columns, so the list never parses their data.
        let mut st = c.prepare(&format!(
            "SELECT -m.id, m.sport, m.home, m.away, m.league, m.start_ms, m.trades, m.bots, m.real, m.last_ms, m.pnl, {marks}, m.books
             FROM review_imports m
             WHERE (?1 = '' OR m.home LIKE ?1 OR m.away LIKE ?1 OR m.league LIKE ?1) AND (?2 = '' OR (',' || m.books || ',') LIKE ('%,' || ?2 || ',%'))"
        ).replace("l.match_id = m.id", "l.match_id = -m.id"))?;
        for mut g in st.query_map(params![like, book], row)?.collect::<rusqlite::Result<Vec<_>>>()? {
            g["imported"] = json!(true);
            v.push(g);
        }
        v.sort_by_key(|g| std::cmp::Reverse(g["last_ms"].as_i64().unwrap_or(0)));
        v
    } else {
        let not_replay = if ids.is_empty() { String::new() } else { format!("AND m.id NOT IN ({ids})") };
        let order = if random { "RANDOM()" } else { "m.start_ms DESC" };
        let mut st = c.prepare(&format!(
            "SELECT {cols}, 0, NULL, NULL, NULL, NULL, {marks}, GROUP_CONCAT(DISTINCT be.book)
             FROM matches m JOIN book_events be ON be.match_id = m.id
             WHERE m.id IN (SELECT match_id FROM book_events WHERE book = '{PM}')
               AND m.id NOT IN (SELECT match_id FROM bot_trades) {not_replay}
               AND (m.start_ms IS NULL OR m.start_ms <= ?3) AND {filters}
             GROUP BY m.id
             HAVING SUM(be.book NOT IN ('{PM}', 'kalshi')) > 0
             ORDER BY {order} LIMIT ?4"
        ))?;
        let v = st.query_map(params![like, book, now_ms(), limit as i64], row)?.collect::<rusqlite::Result<Vec<_>>>()?;
        v
    };
    if random && kind == "bought" && !rows.is_empty() {
        let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos() as usize).unwrap_or(0);
        let pick = rows.swap_remove(nanos % rows.len());
        rows = vec![pick];
    }
    rows.truncate(limit);
    Ok(json!({"kind": kind, "games": rows, "replay_trades": replay.len()}))
}

/// Bookmakers that listed a game Polymarket also listed (for the picker's filter).
pub fn books(db: &Path) -> Result<Value> {
    let c = ro(db)?;
    let mut st = c.prepare(&format!(
        "SELECT book, COUNT(DISTINCT match_id) n FROM book_events
         WHERE match_id IN (SELECT match_id FROM book_events WHERE book = '{PM}') AND book NOT IN ('{PM}', 'kalshi')
         GROUP BY book ORDER BY n DESC"
    ))?;
    let v = st.query_map([], |r| Ok(json!({"book": r.get::<_, String>(0)?, "games": r.get::<_, i64>(1)?})))?.collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(json!({"books": v}))
}

/// One game's prices and trades, read once: from the database, or (negative id) from an imported export of
/// another machine's trades (`review_imports`, id = -game id). Everything the page and a mark's snapshot read.
pub struct GameData {
    pub m: Value,
    pub pm: BTreeMap<String, Vec<(Ms, Option<f64>, Option<f64>)>>,
    pub books: BTreeMap<String, BTreeMap<String, Vec<(Ms, Option<f64>)>>>,
    pub research: BTreeSet<String>,
    pub trades: Vec<Value>,
    pub signals: Vec<Value>,
    pub imported: bool,
}

impl GameData {
    /// Last Polymarket (bid, ask) of the outcome at or before t.
    fn pm_at(&self, o: &str, t: Ms) -> Option<(Option<f64>, Option<f64>)> {
        let rows = self.pm.get(o)?;
        let i = rows.partition_point(|r| r.0 <= t);
        (i > 0).then(|| (rows[i - 1].1, rows[i - 1].2))
    }
    /// Each bookmaker's 1X2 at time t: raw 1/odds of the outcome and its margin-free share.
    fn books_at(&self, o: &str, t: Ms) -> Value {
        let mut out = serde_json::Map::new();
        for (b, outs) in &self.books {
            let last = |rows: &Vec<(Ms, Option<f64>)>| {
                let i = rows.partition_point(|r| r.0 <= t);
                (i > 0).then(|| rows[i - 1].1).flatten()
            };
            if !outs.values().any(|r| r.first().is_some_and(|x| x.0 <= t)) {
                continue;
            }
            let inv = |x: Option<f64>| x.filter(|p| *p > 1.0).map(|p| 1.0 / p);
            let raw = outs.get(o).and_then(|r| inv(last(r)));
            let all: Vec<Option<f64>> = outs.values().map(|r| inv(last(r))).collect();
            let fair = match (raw, all.iter().all(Option::is_some) && all.len() >= 2) {
                (Some(r), true) => Some(r / all.iter().flatten().sum::<f64>()),
                _ => None,
            };
            out.insert(b.clone(), json!({"raw": raw, "fair": fair}));
        }
        Value::Object(out)
    }
    /// Polymarket rows of the outcome from the one in force at `lo` to `hi`.
    fn pm_rows(&self, o: &str, lo: Ms, hi: Ms) -> Vec<(Ms, Option<f64>, Option<f64>)> {
        let Some(rows) = self.pm.get(o) else { return Vec::new() };
        let i = rows.partition_point(|r| r.0 <= lo).saturating_sub(1);
        rows[i..].iter().take_while(|r| r.0 <= hi).copied().collect()
    }
    /// A bookmaker's rows of the outcome from the one in force at `lo` to `hi`.
    fn book_rows(&self, book: &str, o: &str, lo: Ms, hi: Ms) -> Vec<(Ms, Option<f64>)> {
        let Some(rows) = self.books.get(book).and_then(|b| b.get(o)) else { return Vec::new() };
        let i = rows.partition_point(|r| r.0 <= lo).saturating_sub(1);
        rows[i..].iter().take_while(|r| r.0 <= hi).copied().collect()
    }
}

pub fn load_game(c: &Connection, id: i64, replay: &[Value]) -> Result<GameData> {
    if id < 0 {
        let (m, data): (Value, String) = c
            .query_row("SELECT i.id, i.sport, i.home, i.away, i.league, i.start_ms, i.mac_match_id, d.data FROM review_imports i JOIN review_import_data d ON d.id = i.id WHERE i.id = ?1", params![-id], |r| {
                Ok((json!({"id": -r.get::<_, i64>(0)?, "sport": r.get::<_, Option<String>>(1)?, "home": r.get::<_, String>(2)?, "away": r.get::<_, String>(3)?,
                           "league": r.get::<_, Option<String>>(4)?, "start_ms": r.get::<_, Option<i64>>(5)?, "imported": true, "mac_match_id": r.get::<_, i64>(6)?}), r.get::<_, String>(7)?))
            })
            .optional()?
            .ok_or_else(|| anyhow!("no imported game {}", -id))?;
        let d: Value = serde_json::from_str(&data)?;
        let mut m = m;
        // Prices in an export are known up to the moment it was made (the page carries lines that went
        // flat up to there, not beyond).
        m["until_ms"] = d["source"]["generated_ms"].clone();
        let mut pm = BTreeMap::new();
        for (o, rows) in d["pm"].as_object().into_iter().flatten() {
            pm.insert(o.clone(), rows.as_array().into_iter().flatten().map(|r| (r[0].as_i64().unwrap_or(0), r[1].as_f64(), r[2].as_f64())).collect::<Vec<_>>());
        }
        let mut books = BTreeMap::new();
        for (b, outs) in d["books"].as_object().into_iter().flatten() {
            let mut bo = BTreeMap::new();
            for (o, rows) in outs.as_object().into_iter().flatten() {
                bo.insert(o.clone(), rows.as_array().into_iter().flatten().map(|r| (r[0].as_i64().unwrap_or(0), r[1].as_f64())).collect::<Vec<_>>());
            }
            books.insert(b.clone(), bo);
        }
        return Ok(GameData { m, pm, books, research: BTreeSet::new(), trades: d["trades"].as_array().cloned().unwrap_or_default(), signals: Vec::new(), imported: true });
    }
    let m = c
        .query_row("SELECT id, sport, home, away, league, start_ms FROM matches WHERE id = ?1", params![id], |r| {
            Ok(json!({"id": r.get::<_, i64>(0)?, "sport": r.get::<_, Option<String>>(1)?, "home": r.get::<_, String>(2)?, "away": r.get::<_, String>(3)?, "league": r.get::<_, Option<String>>(4)?, "start_ms": r.get::<_, Option<i64>>(5)?}))
        })
        .optional()?
        .ok_or_else(|| anyhow!("no game {id}"))?;
    // Polymarket: one row per change of bid or ask (size-only changes dropped).
    let mut pm: BTreeMap<String, Vec<(Ms, Option<f64>, Option<f64>)>> = BTreeMap::new();
    let mut st = c.prepare("SELECT ts_ms, outcome, bid, ask FROM pm_quotes WHERE match_id = ?1 ORDER BY ts_ms")?;
    let mut rows = st.query(params![id])?;
    while let Some(r) = rows.next()? {
        let (t, o, b, a): (i64, String, Option<f64>, Option<f64>) = (r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?);
        let v = pm.entry(o).or_default();
        if v.last().is_some_and(|x| x.1 == b && x.2 == a) {
            continue;
        }
        v.push((t, b, a));
    }
    // Bookmakers: 1X2 price changes per outcome (None = suspended).
    let mut books: BTreeMap<String, BTreeMap<String, Vec<(Ms, Option<f64>)>>> = BTreeMap::new();
    let mut research = BTreeSet::new();
    for table in ["changes", "changes_research"] {
        let mut st = c.prepare(&format!("SELECT ts_ms, book, outcome, new_price FROM {table} WHERE match_id = ?1 AND market = '1x2' AND book NOT IN ('{PM}', 'kalshi') ORDER BY ts_ms"))?;
        let mut rows = st.query(params![id])?;
        while let Some(r) = rows.next()? {
            let (t, b, o, p): (i64, String, String, Option<f64>) = (r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?);
            if table == "changes_research" {
                research.insert(b.clone());
            }
            books.entry(b).or_default().entry(o).or_default().push((t, p));
        }
    }
    let mut st = c.prepare("SELECT id, bot, real, ts_ms, closed_ms, data FROM bot_trades WHERE match_id = ?1 ORDER BY ts_ms")?;
    let trades = st
        .query_map(params![id], |r| {
            let data: String = r.get(5)?;
            Ok(json!({"id": r.get::<_, i64>(0)?, "bot": r.get::<_, String>(1)?, "real": r.get::<_, i64>(2)? > 0, "ts_ms": r.get::<_, i64>(3)?, "closed_ms": r.get::<_, Option<i64>>(4)?, "data": serde_json::from_str::<Value>(&data).unwrap_or(Value::Null)}))
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut st = c.prepare("SELECT ts_ms, outcome, result FROM bot_signals WHERE match_id = ?1 ORDER BY ts_ms")?;
    let signals = st
        .query_map(params![id], |r| Ok(json!([r.get::<_, i64>(0)?, r.get::<_, Option<String>>(1)?, r.get::<_, String>(2)?])))?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    let mut g = GameData { m, pm, books, research, trades, signals, imported: false };
    for (i, t) in replay.iter().enumerate().filter(|(_, t)| t["match_id"].as_i64() == Some(id)) {
        let mut row = replay_row(i, t);
        let (o, t0) = (t["outcome"].as_str().unwrap_or(""), t["t0"].as_i64().unwrap_or(0));
        if t0 > 0 {
            if let Some((Some(b), Some(a))) = g.pm_at(o, t0 - crate::snipe::PRE_MS) {
                row["data"]["pre_mid"] = json!((b + a) / 2.0);
            }
            row["data"]["move_ms"] = json!(t0);
        }
        g.trades.push(row);
    }
    Ok(g)
}

/// Everything the chart needs for one game: Polymarket bid/ask per outcome, every bookmaker's 1X2
/// prices (recorded-only books too), the bots' trades (live, replay or imported) and skipped moves, and the marks.
pub fn game(db: &Path, id: i64, replay: &[Value]) -> Result<Value> {
    let c = ro(db)?;
    let g = load_game(&c, id, replay)?;
    let pm: BTreeMap<&String, Vec<Value>> = g.pm.iter().map(|(o, r)| (o, r.iter().map(|x| json!([x.0, x.1, x.2])).collect())).collect();
    let books: BTreeMap<&String, BTreeMap<&String, Vec<Value>>> = g.books.iter().map(|(b, outs)| (b, outs.iter().map(|(o, r)| (o, r.iter().map(|x| json!([x.0, x.1])).collect())).collect())).collect();
    // Feed families: books on one trading desk count once in the middle of all bookmakers (as the bot does).
    let families: BTreeMap<&String, &str> = g.books.keys().map(|b| (b, crate::snipe::platform(b))).collect();
    Ok(json!({
        "match": g.m, "pm": pm, "books": books, "research": g.research, "trades": g.trades, "signals": g.signals, "families": families,
        "imported": g.imported, "labels": labels_on(&c, Some(id))?,
    }))
}

/// Import an export of another machine's trades (the HTML page "Swifteye trades" or its JSON): one imported game
/// per game of that machine, with every trade's own window of Polymarket and bookmaker prices merged. A game
/// imported again is replaced in place (same id, so its marks stay attached).
pub fn import(db: &Path, body: &[u8]) -> Result<Value> {
    let text = String::from_utf8_lossy(body);
    let json_text = match text.find(r#"id="swifteye-trades">"#) {
        Some(a) => {
            let start = a + r#"id="swifteye-trades">"#.len();
            let end = text[start..].find("</script>").ok_or_else(|| anyhow!("the trades data in the page is not closed"))?;
            text[start..start + end].to_string()
        }
        None => text.to_string(),
    };
    let j: Value = serde_json::from_str(json_text.trim()).context("not a Swifteye trades export (HTML page or JSON)")?;
    let trades = j["trades"].as_array().ok_or_else(|| anyhow!("no trades in the export"))?;
    let mut by: BTreeMap<i64, Vec<&Value>> = BTreeMap::new();
    for t in trades {
        let m = t["match_id"].as_i64().or_else(|| t["record"]["match_id"].as_i64()).ok_or_else(|| anyhow!("a trade without a game"))?;
        by.entry(m).or_default().push(t);
    }
    let w = Connection::open(db)?;
    w.busy_timeout(std::time::Duration::from_secs(10))?;
    let now = now_ms();
    let (mut n_games, mut n_trades) = (0, 0);
    for (mac, ts) in &by {
        let first = ts[0];
        let mut pm: BTreeMap<String, Vec<(Ms, Option<f64>, Option<f64>)>> = BTreeMap::new();
        let mut books: BTreeMap<String, BTreeMap<String, Vec<(Ms, Option<f64>)>>> = BTreeMap::new();
        let mut rows = Vec::new();
        let (mut bots, mut real, mut last, mut pnl) = (BTreeSet::new(), false, 0i64, 0.0);
        for t in ts {
            let from = t["window"]["from_ms"].as_i64().unwrap_or(0);
            for (o, r) in t["polymarket"].as_object().into_iter().flatten() {
                pm.entry(o.clone()).or_default().extend(r.as_array().into_iter().flatten().map(|x| (from + x[0].as_i64().unwrap_or(0), x[1].as_f64(), x[2].as_f64())));
            }
            for (b, outs) in t["bookmakers"].as_object().into_iter().flatten() {
                for (o, r) in outs.as_object().into_iter().flatten() {
                    books.entry(b.clone()).or_default().entry(o.clone()).or_default().extend(r.as_array().into_iter().flatten().map(|x| (from + x[0].as_i64().unwrap_or(0), x[1].as_f64())));
                }
            }
            let mut data = t["record"].clone();
            if data.is_null() {
                data = json!({});
            }
            for k in ["outcome", "trigger", "move_ms"] {
                if data[k].is_null() && !t[k].is_null() {
                    data[k] = t[k].clone();
                }
            }
            if data["dir"].is_null() {
                data["dir"] = json!(if t["token"].as_str() == Some("no") { -1.0 } else { 1.0 });
            }
            if data["entry"].is_null() {
                data["entry"] = t["buy_price"].clone();
            }
            if data["exit"].is_null() {
                data["exit"] = t["sell_price"].clone();
            }
            if data["exit_ts"].is_null() && !t["open"].as_bool().unwrap_or(false) {
                data["exit_ts"] = t["sell_ms"].clone();
            }
            if data["pnl"].is_null() {
                data["pnl"] = t["pnl"].clone();
            }
            data["ts"] = t["buy_ms"].clone();
            let bot = t["bot"].as_str().unwrap_or("bot").to_string();
            let is_real = t["real"].as_bool().unwrap_or(false);
            bots.insert(bot.clone());
            real |= is_real;
            last = last.max(t["buy_ms"].as_i64().unwrap_or(0));
            pnl += t["pnl"].as_f64().unwrap_or(0.0);
            rows.push(json!({"id": t["id"], "bot": bot, "real": is_real, "ts_ms": t["buy_ms"], "closed_ms": if t["open"].as_bool().unwrap_or(false) { Value::Null } else { t["sell_ms"].clone() },
                             "data": data, "counts_in_real_results": t["counts_in_real_results"], "imported": true}));
        }
        // Windows of several trades overlap: sorted, repeated points dropped.
        for v in pm.values_mut() {
            v.sort_by_key(|x| x.0);
            v.dedup_by(|a, b| a.1 == b.1 && a.2 == b.2);
        }
        for outs in books.values_mut() {
            for v in outs.values_mut() {
                v.sort_by_key(|x| x.0);
                v.dedup_by(|a, b| a.1 == b.1);
            }
        }
        let data = json!({
            "pm": pm.iter().map(|(o, r)| (o.clone(), json!(r.iter().map(|x| json!([x.0, x.1, x.2])).collect::<Vec<_>>()))).collect::<serde_json::Map<_, _>>(),
            "books": books.iter().map(|(b, outs)| (b.clone(), json!(outs.iter().map(|(o, r)| (o.clone(), json!(r.iter().map(|x| json!([x.0, x.1])).collect::<Vec<_>>()))).collect::<serde_json::Map<_, _>>()))).collect::<serde_json::Map<_, _>>(),
            "trades": rows,
            "source": {"generated": j["generated"], "generated_ms": j["generated_ms"]},
        });
        // The summary row (what the list reads) and the prices (what a game page reads) in separate tables,
        // one short transaction per game (the collector's writer waits at most 5 s for the lock).
        let tx = w.unchecked_transaction()?;
        tx.execute(
            "INSERT INTO review_imports(mac_match_id, imported_ms, sport, league, home, away, start_ms, trades, bots, real, last_ms, pnl, books)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)
             ON CONFLICT(mac_match_id) DO UPDATE SET imported_ms = ?2, sport = ?3, league = ?4, home = ?5, away = ?6, start_ms = ?7, trades = ?8, bots = ?9, real = ?10, last_ms = ?11, pnl = ?12, books = ?13",
            params![mac, now, first["sport"].as_str(), first["league"].as_str(), first["home"].as_str().unwrap_or("?"), first["away"].as_str().unwrap_or("?"), first["start_ms"].as_i64(),
                    ts.len() as i64, bots.into_iter().collect::<Vec<_>>().join(","), real as i64, last, pnl, books.keys().cloned().collect::<Vec<_>>().join(",")],
        )?;
        let id: i64 = tx.query_row("SELECT id FROM review_imports WHERE mac_match_id = ?1", params![mac], |r| r.get(0))?;
        tx.execute("INSERT OR REPLACE INTO review_import_data(id, data) VALUES (?1, ?2)", params![id, data.to_string()])?;
        tx.commit()?;
        n_games += 1;
        n_trades += ts.len();
    }
    Ok(json!({"games": n_games, "trades": n_trades, "generated": j["generated"]}))
}

fn labels_on(c: &Connection, m: Option<i64>) -> Result<Vec<Value>> {
    let mut st = c.prepare("SELECT id, created_ms, updated_ms, match_id, kind, outcome, data FROM trade_labels WHERE ?1 IS NULL OR match_id = ?1 ORDER BY id")?;
    let v = st
        .query_map(params![m], |r| {
            let mut d: Value = serde_json::from_str(&r.get::<_, String>(6)?).unwrap_or_else(|_| json!({}));
            if let Some(o) = d.as_object_mut() {
                o.insert("id".into(), json!(r.get::<_, i64>(0)?));
                o.insert("created_ms".into(), json!(r.get::<_, i64>(1)?));
                o.insert("updated_ms".into(), json!(r.get::<_, i64>(2)?));
                o.insert("match_id".into(), json!(r.get::<_, i64>(3)?));
                o.insert("kind".into(), json!(r.get::<_, String>(4)?));
                o.insert("outcome".into(), json!(r.get::<_, String>(5)?));
            }
            Ok(d)
        })?
        .collect::<rusqlite::Result<Vec<_>>>()?;
    Ok(v)
}

/// Every mark, oldest first (`match_id` = one game's).
pub fn labels(db: &Path, m: Option<i64>) -> Result<Value> {
    let c = ro(db)?;
    Ok(json!({"labels": labels_on(&c, m)?}))
}

/// A painted line: from (t0, y0) to (t1, y1), y = probability of the outcome (0..1).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Seg {
    pub t0: Ms,
    pub y0: f64,
    pub t1: Ms,
    pub y1: f64,
}

fn seg(v: &Value) -> Result<Option<Seg>> {
    if v.is_null() {
        return Ok(None);
    }
    let n = |k: &str| v[k].as_f64().ok_or_else(|| anyhow!("painted line without {k}"));
    let s = Seg { t0: n("t0")? as Ms, y0: n("y0")?, t1: n("t1")? as Ms, y1: n("y1")? };
    if s.t1 <= s.t0 {
        bail!("a painted line has to run left to right");
    }
    Ok(Some(s))
}

/// A step series sampled every second from `lo` to `hi`: the value in force at each second.
fn sample(points: &[(Ms, Option<f64>)], lo: Ms, hi: Ms) -> Vec<Option<f64>> {
    let mut out = Vec::with_capacity(((hi - lo) / 1000 + 1).max(0) as usize);
    let (mut j, mut cur) = (0, None);
    let mut t = lo;
    while t <= hi {
        while j < points.len() && points[j].0 <= t {
            cur = points[j].1;
            j += 1;
        }
        out.push(cur);
        t += 1000;
    }
    out
}

/// Pearson correlation of the seconds where both have a value; None under 10 such seconds or when
/// either does not move.
fn pearson(a: &[Option<f64>], b: &[Option<f64>]) -> Option<f64> {
    let pairs: Vec<(f64, f64)> = a.iter().zip(b).filter_map(|(x, y)| Some(((*x)?, (*y)?))).collect();
    if pairs.len() < 10 {
        return None;
    }
    let n = pairs.len() as f64;
    let (mx, my) = (pairs.iter().map(|p| p.0).sum::<f64>() / n, pairs.iter().map(|p| p.1).sum::<f64>() / n);
    let (mut sxy, mut sxx, mut syy) = (0.0, 0.0, 0.0);
    for (x, y) in &pairs {
        sxy += (x - mx) * (y - my);
        sxx += (x - mx).powi(2);
        syy += (y - my).powi(2);
    }
    (sxx > 1e-12 && syy > 1e-12).then(|| sxy / (sxx * syy).sqrt())
}

/// Where a series made half of the move between its values at t0 and t1: the first second from t0
/// on (to t1 + 30 s) at or past the halfway level. Returns (value at t0, value at t1, crossing).
fn crossing(s: &[Option<f64>], lo: Ms, t0: Ms, t1: Ms) -> Option<(f64, f64, Option<Ms>)> {
    let at = |t: Ms| s.get(((t - lo) / 1000).max(0) as usize).copied().flatten();
    let (v0, v1) = (at(t0)?, at(t1)?);
    if (v1 - v0).abs() < 1e-9 {
        return Some((v0, v1, None)); // did not move (and f64::signum(0.0) is 1.0, not 0)
    }
    let half = (v0 + v1) / 2.0;
    let dir = (v1 - v0).signum();
    let mut t = t0;
    while t <= t1 + 30_000 {
        if let Some(v) = at(t) {
            if (v - half) * dir >= 0.0 {
                return Some((v0, v1, Some(t)));
            }
        }
        t += 1000;
    }
    Some((v0, v1, None))
}

/// The two painted lines read as one movement, measured on the recorded prices.
/// pm: rows (t, bid, ask); book: rows (t, price). Polymarket is its mid, the bookmaker 1/odds.
pub fn analyse(pm_rows: &[(Ms, Option<f64>, Option<f64>)], book_rows: &[(Ms, Option<f64>)], pm: Seg, bk: Seg) -> Value {
    let lo = pm.t0.min(bk.t0) - PAD_MS;
    let hi = pm.t1.max(bk.t1) + PAD_MS;
    let mid: Vec<(Ms, Option<f64>)> = pm_rows.iter().map(|(t, b, a)| (*t, b.zip(*a).map(|(b, a)| (b + a) / 2.0))).collect();
    let prob: Vec<(Ms, Option<f64>)> = book_rows.iter().map(|(t, p)| (*t, p.filter(|p| *p > 1.0).map(|p| 1.0 / p))).collect();
    let (sp, sb) = (sample(&mid, lo, hi), sample(&prob, lo, hi));
    let cp = crossing(&sp, lo, pm.t0, pm.t1);
    let cb = crossing(&sb, lo, bk.t0, bk.t1);
    let moved = |c: Option<(f64, f64, Option<Ms>)>| c.map(|(a, b, _)| b - a);
    let (mp, mb) = (moved(cp), moved(cb));
    // Best lag: Polymarket shifted by `lag` seconds against the bookmaker (positive = Polymarket later).
    let mut best: Option<(i64, f64)> = None;
    for lag in -MAX_LAG_S..=MAX_LAG_S {
        let shifted: Vec<Option<f64>> = (0..sb.len() as i64).map(|i| sp.get((i + lag) as usize).copied().flatten().filter(|_| i + lag >= 0)).collect();
        if let Some(r) = pearson(&shifted, &sb) {
            if best.map_or(true, |(_, b)| r > b) {
                best = Some((lag, r));
            }
        }
    }
    let mid_t = |s: Seg| (s.t0 + s.t1) / 2;
    // A painted line along a price that did not move (or moved the other way) measures nothing:
    // say so, so the mark can be fixed (usually the wrong bookmaker under the line).
    let mut warnings = Vec::new();
    for (name, c, s) in [("bookmaker", cb, bk), ("Polymarket", cp, pm)] {
        match c {
            None => warnings.push(format!("no {name} price at the ends of its line")),
            Some((a, b, _)) if (b - a).abs() < 0.005 => warnings.push(format!("the {name} price did not move between the ends of its line")),
            Some((a, b, _)) if (b - a) * (s.y1 - s.y0) < 0.0 => warnings.push(format!("the {name} price moved the other way to its line")),
            _ => {}
        }
    }
    json!({
        "warnings": warnings,
        "delay_painted_ms": mid_t(pm) - mid_t(bk),
        "delay_start_ms": pm.t0 - bk.t0,
        "delay_measured_ms": match (cp.and_then(|c| c.2), cb.and_then(|c| c.2)) { (Some(a), Some(b)) => Some(a - b), _ => None },
        "pm_move": mp, "book_move": mb,
        "pm_move_painted": pm.y1 - pm.y0, "book_move_painted": bk.y1 - bk.y0,
        "ratio": match (mp, mb) { (Some(p), Some(b)) if b.abs() > 1e-6 => Some(p / b), _ => None },
        "ratio_painted": if (bk.y1 - bk.y0).abs() > 1e-6 { Some((pm.y1 - pm.y0) / (bk.y1 - bk.y0)) } else { None },
        "corr_lag0": pearson(&sp, &sb),
        "best_lag_ms": best.map(|b| b.0 * 1000), "corr_best": best.map(|b| b.1),
        "window": [lo, hi],
    })
}

/// Save a mark (new, or `id` = edit). Checks it, adds the recorded prices it was made on, writes it,
/// and refreshes `<db dir>/trade_labels.json`. Returns the saved mark.
pub fn save(db: &Path, b: &Value) -> Result<Value> {
    let r = ro(db)?;
    let m = b["match_id"].as_i64().ok_or_else(|| anyhow!("match_id missing"))?;
    let gd = load_game(&r, m, &[])?;
    let game = json!({"home": gd.m["home"], "away": gd.m["away"], "league": gd.m["league"], "start_ms": gd.m["start_ms"], "sport": gd.m["sport"], "imported": gd.imported});
    let kind = b["kind"].as_str().filter(|k| KINDS.contains(k)).ok_or_else(|| anyhow!("kind must be one of {}", KINDS.join(", ")))?;
    let o = b["outcome"].as_str().filter(|o| ["home", "draw", "away"].contains(o)).ok_or_else(|| anyhow!("outcome must be home, draw or away"))?;
    let side = b["side"].as_str().unwrap_or("yes");
    if side != "yes" && side != "no" {
        bail!("side must be yes or no");
    }
    let buy = b["buy_ms"].as_i64();
    let sell = b["sell_ms"].as_i64();
    let cap = b["cap"].as_f64();
    // The plan: the price it should cash out at and when it should give up (sell at any price).
    let target = b["target"].as_f64();
    let give_up = b["give_up_ms"].as_i64();
    if target.is_some_and(|t| !(t > 0.0 && t < 1.0)) {
        bail!("the cash-out price is a price between 0 and 100 cents");
    }
    if let (Some(bu), Some(g)) = (buy, give_up) {
        if g <= bu {
            bail!("the give-up time has to come after the buy point");
        }
    }
    let trade_id = b["trade_id"].as_i64();
    let pm_seg = seg(&b["pm_seg"]).context("Polymarket line")?;
    let book_seg = seg(&b["book_seg"]).context("bookmaker line")?;
    let book = b["book_seg"]["book"].as_str().map(String::from);
    if book_seg.is_some() && book.as_deref().map_or(true, str::is_empty) {
        bail!("the bookmaker line needs its bookmaker");
    }
    // One line alone is kept (the other may come later); the measurement needs both.
    match kind {
        "move" if pm_seg.is_none() && book_seg.is_none() => bail!("a 'same move' mark needs a painted line"),
        "buy" | "adjust" if buy.is_none() || cap.is_none() => bail!("set the buy point and the price cap"),
        _ => {}
    }
    if let (Some(bu), Some(se)) = (buy, sell) {
        if se <= bu {
            bail!("the sell point has to come after the buy point");
        }
    }
    if cap.is_some_and(|c| !(c > 0.0 && c < 1.0)) {
        bail!("the price cap is a price between 0 and 100 cents");
    }
    let trade = match trade_id {
        Some(t) => Some(gd.trades.iter().find(|x| x["id"].as_i64() == Some(t)).cloned().ok_or_else(|| anyhow!("bot trade {t} is not on this game"))?),
        None => match &b["replay_trade"] {
            t if t.is_object() => {
                if t["match_id"].as_i64() != Some(m) {
                    bail!("that replay trade is not on this game");
                }
                Some(json!({"bot": "replay", "real": false, "ts_ms": t["ts"], "closed_ms": t["exit_ts"], "data": t}))
            }
            _ if matches!(kind, "adjust" | "no_buy" | "ok") => bail!("pick the bot trade this mark is about"),
            _ => None,
        },
    };

    // The prices the mark was made on.
    let token = |q: Option<(Option<f64>, Option<f64>)>, buying: bool| -> Option<f64> {
        let (bid, ask) = q?;
        match (side, buying) {
            ("yes", true) => ask,
            ("yes", false) => bid,
            (_, true) => bid.map(|x| 1.0 - x),
            (_, false) => ask.map(|x| 1.0 - x),
        }
    };
    let mut snap = serde_json::Map::new();
    if let Some(t) = buy {
        let q = gd.pm_at(o, t);
        snap.insert("buy".into(), json!({"pm_bid": q.and_then(|x| x.0), "pm_ask": q.and_then(|x| x.1), "price": token(q, true), "books": gd.books_at(o, t), "books_30s_before": gd.books_at(o, t - 30_000)}));
    }
    if let Some(t) = sell {
        let q = gd.pm_at(o, t);
        snap.insert("sell".into(), json!({"pm_bid": q.and_then(|x| x.0), "pm_ask": q.and_then(|x| x.1), "price": token(q, false)}));
    }
    if let (Some(bp), Some(sp)) = (snap.get("buy").and_then(|x| x["price"].as_f64()), snap.get("sell").and_then(|x| x["price"].as_f64())) {
        snap.insert("gain_per_share".into(), json!(sp - bp));
    }
    let analysis = match (pm_seg, book_seg, &book) {
        (Some(p), Some(k), Some(bk)) => {
            let (lo, hi) = (p.t0.min(k.t0) - PAD_MS, p.t1.max(k.t1) + PAD_MS);
            let (pr, br) = (gd.pm_rows(o, lo, hi), gd.book_rows(bk, o, lo, hi));
            if br.is_empty() {
                bail!("{bk} has no {o} price around the painted line");
            }
            let mut a = analyse(&pr, &br, p, k);
            a["series"] = json!({"pm": pr.iter().map(|x| json!([x.0, x.1, x.2])).collect::<Vec<_>>(), "book": br.iter().map(|x| json!([x.0, x.1])).collect::<Vec<_>>()});
            Some(a)
        }
        _ => None,
    };
    drop(r);

    let seg_json = |s: Option<Seg>, extra: Option<&String>| s.map(|s| {
        let mut v = json!({"t0": s.t0, "y0": s.y0, "t1": s.t1, "y1": s.y1});
        if let Some(b) = extra {
            v["book"] = json!(b);
        }
        v
    });
    let data = json!({
        "side": side, "buy_ms": buy, "sell_ms": sell, "cap": cap, "target": target, "give_up_ms": give_up, "trade_id": trade_id,
        // true: the sale was dragged by hand; false: it is where the plan (cash-out price, give-up) sells.
        "sell_set": b["sell_set"].as_bool().unwrap_or(false),
        "pm_seg": seg_json(pm_seg, None), "book_seg": seg_json(book_seg, book.as_ref()),
        "note": b["note"].as_str().unwrap_or("").chars().take(4000).collect::<String>(),
        "books_shown": b["books_shown"].as_array().cloned().unwrap_or_default(),
        "game": game, "trade": trade, "snapshot": snap, "analysis": analysis,
    });
    let now = now_ms();
    let w = Connection::open(db)?;
    w.busy_timeout(std::time::Duration::from_secs(10))?;
    let id = match b["id"].as_i64() {
        Some(id) => {
            if w.execute("UPDATE trade_labels SET updated_ms = ?2, match_id = ?3, kind = ?4, outcome = ?5, data = ?6 WHERE id = ?1", params![id, now, m, kind, o, data.to_string()])? == 0 {
                bail!("mark {id} not found");
            }
            id
        }
        None => {
            w.execute("INSERT INTO trade_labels(created_ms, updated_ms, match_id, kind, outcome, data) VALUES (?1, ?1, ?2, ?3, ?4, ?5)", params![now, m, kind, o, data.to_string()])?;
            w.last_insert_rowid()
        }
    };
    drop(w);
    mirror(db);
    let c = ro(db)?;
    labels_on(&c, Some(m))?.into_iter().find(|l| l["id"].as_i64() == Some(id)).ok_or_else(|| anyhow!("saved mark not found"))
}

pub fn delete(db: &Path, id: i64) -> Result<Value> {
    let w = Connection::open(db)?;
    w.busy_timeout(std::time::Duration::from_secs(10))?;
    let n = w.execute("DELETE FROM trade_labels WHERE id = ?1", params![id])?;
    drop(w);
    mirror(db);
    Ok(json!({"deleted": n}))
}

/// The latest `tools/waves/run.py` scores (`<db dir>/waves/scores.json`) without the per-mark details: the
/// page's Analysis shows each line's measured lead over Polymarket and correlation over all recorded games.
pub fn wave_scores(db: &Path) -> Result<Value> {
    let p = db.parent().map(|d| d.join("waves").join("scores.json")).unwrap_or_else(|| PathBuf::from("waves/scores.json"));
    let Ok(text) = std::fs::read_to_string(&p) else { return Ok(Value::Null) };
    let mut v: Value = serde_json::from_str(&text).context("waves/scores.json")?;
    if let Some(o) = v.as_object_mut() {
        o.remove("marks");
        let at = std::fs::metadata(&p).ok().and_then(|m| m.modified().ok()).and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_millis() as i64);
        o.insert("written_ms".into(), json!(at));
    }
    Ok(v)
}

fn mirror_path(db: &Path) -> PathBuf {
    db.parent().map(|d| d.join("trade_labels.json")).unwrap_or_else(|| PathBuf::from("trade_labels.json"))
}

/// All marks as one JSON file next to the database (read it without the app; survives a new db).
fn mirror(db: &Path) {
    let res = labels(db, None).and_then(|v| Ok(std::fs::write(mirror_path(db), serde_json::to_vec_pretty(&v["labels"])?)?));
    if let Err(e) = res {
        tracing::warn!("trade review: could not write {}: {e:#}", mirror_path(db).display());
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_polymarket_move_eight_seconds_after_the_bookmaker_measures_eight_seconds() {
        // Bookmaker: 2.0 -> 1.6 at t = 100 s (50% -> 62.5%). Polymarket mid: 0.50 -> 0.62 at t = 108 s.
        let s = 1_000_000;
        let book = vec![(s, Some(2.0)), (s + 100_000, Some(1.6))];
        let pm = vec![(s, Some(0.49), Some(0.51)), (s + 108_000, Some(0.61), Some(0.63))];
        let bk = Seg { t0: s + 95_000, y0: 0.5, t1: s + 105_000, y1: 0.625 };
        let p = Seg { t0: s + 103_000, y0: 0.5, t1: s + 113_000, y1: 0.62 };
        let a = analyse(&pm, &book, p, bk);
        assert_eq!(a["delay_measured_ms"], json!(8_000));
        assert_eq!(a["delay_painted_ms"], json!(8_000));
        assert!((a["ratio"].as_f64().unwrap() - 0.12 / 0.125).abs() < 1e-9);
        assert_eq!(a["best_lag_ms"], json!(8_000), "shifting Polymarket 8 s back lines the two up");
        assert!(a["corr_best"].as_f64().unwrap() > 0.99);
        assert!(a["corr_lag0"].as_f64().unwrap() < a["corr_best"].as_f64().unwrap());
    }

    #[test]
    fn a_bookmaker_that_did_not_move_gives_no_delay_and_a_warning() {
        let s = 1_000_000;
        let book = vec![(s, Some(1.22))];
        let pm = vec![(s, Some(0.74), Some(0.76)), (s + 100_000, Some(0.15), Some(0.17))];
        let a = analyse(&pm, &book, Seg { t0: s + 95_000, y0: 0.75, t1: s + 105_000, y1: 0.16 }, Seg { t0: s + 140_000, y0: 0.84, t1: s + 150_000, y1: 0.1 });
        assert_eq!(a["delay_measured_ms"], Value::Null);
        assert_eq!(a["ratio"], Value::Null);
        assert!(a["warnings"][0].as_str().unwrap().contains("bookmaker price did not move"));
    }

    #[test]
    fn sampling_holds_the_last_value_and_pearson_needs_movement() {
        let s = sample(&[(1_000, Some(1.0)), (3_500, None), (5_000, Some(2.0))], 0, 6_000);
        assert_eq!(s, vec![None, Some(1.0), Some(1.0), Some(1.0), None, Some(2.0), Some(2.0)]);
        let flat = vec![Some(1.0); 20];
        assert_eq!(pearson(&flat, &flat), None);
    }
}
