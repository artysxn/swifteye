//! SQLite persistence. One writer thread batches everything into a
//! transaction every ~200 ms; readers open their own read-only connections.

use crate::engine::{Engine, Write};
use crate::matcher::Matcher;
use crate::model::*;
use anyhow::Result;
use rusqlite::{params, Connection, OpenFlags};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};
use tokio::sync::mpsc;

const SCHEMA: &str = r#"
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS matches(
  id INTEGER PRIMARY KEY,
  sport TEXT NOT NULL,
  home TEXT NOT NULL,
  away TEXT NOT NULL,
  league TEXT,
  start_ms INTEGER,
  sr_id TEXT,
  created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS book_events(
  book TEXT NOT NULL,
  book_event_id TEXT NOT NULL,
  match_id INTEGER NOT NULL,
  sport TEXT, league TEXT, home TEXT, away TEXT,
  start_ms INTEGER,
  sr_id TEXT,
  first_seen_ms INTEGER NOT NULL,
  last_seen_ms INTEGER NOT NULL,
  PRIMARY KEY(book, book_event_id)
);
CREATE INDEX IF NOT EXISTS book_events_match ON book_events(match_id);
-- Every price change ever seen. ts_ms = when the response carrying it arrived.
CREATE TABLE IF NOT EXISTS changes(
  id INTEGER PRIMARY KEY,
  ts_ms INTEGER NOT NULL,
  book TEXT NOT NULL,
  book_event_id TEXT NOT NULL,
  match_id INTEGER NOT NULL,
  market TEXT NOT NULL,
  line REAL,
  outcome TEXT NOT NULL,
  kind TEXT NOT NULL,
  old_price REAL,
  new_price REAL,
  book_ts_ms INTEGER
);
CREATE INDEX IF NOT EXISTS changes_ts ON changes(ts_ms);
CREATE INDEX IF NOT EXISTS changes_match ON changes(match_id, ts_ms);
CREATE INDEX IF NOT EXISTS changes_book ON changes(book, ts_ms);
-- Polymarket top of book per Yes token. Prices are probabilities (0..1).
-- ts_ms = our corrected receive time, server_ts_ms = Polymarket's own message time.
CREATE TABLE IF NOT EXISTS pm_quotes(
  id INTEGER PRIMARY KEY,
  ts_ms INTEGER NOT NULL,
  server_ts_ms INTEGER,
  match_id INTEGER NOT NULL,
  event_id TEXT NOT NULL,
  outcome TEXT NOT NULL,
  bid REAL, ask REAL, last REAL
);
CREATE INDEX IF NOT EXISTS pm_quotes_match ON pm_quotes(match_id, ts_ms);

-- Kalshi top of book per contract (event ticker + outcome), probabilities 0..1.
-- est = 1: rebuilt from Kalshi's 1-minute candles (bid/ask closes) for earlier hours.
CREATE TABLE IF NOT EXISTS kalshi_quotes(
  id INTEGER PRIMARY KEY,
  ts_ms INTEGER NOT NULL,
  server_ts_ms INTEGER,
  event TEXT NOT NULL,
  outcome TEXT NOT NULL,
  bid REAL, ask REAL,
  est INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS kalshi_quotes_ev ON kalshi_quotes(event, outcome, ts_ms);

-- Pre-match arbitrage opportunities (match + market + line) as the live scan saw them come
-- and go. end_reason: gone | kickoff (the match started) | restart. censored = 1: already
-- open in the first scan after a start, so its real start is unknown.
CREATE TABLE IF NOT EXISTS pred_arbs(
  ts_ms INTEGER NOT NULL,
  match_id INTEGER NOT NULL,
  data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pred_arbs_ts ON pred_arbs(ts_ms);
CREATE TABLE IF NOT EXISTS arb_history(
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL,
  match_id INTEGER NOT NULL,
  sport TEXT, home TEXT, away TEXT, league TEXT,
  market TEXT NOT NULL,
  line REAL,
  books TEXT,
  legs TEXT,
  first_ms INTEGER NOT NULL,
  last_ms INTEGER NOT NULL,
  end_ms INTEGER,
  end_reason TEXT,
  open_profit REAL,
  peak_profit REAL,
  censored INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS arb_history_end ON arb_history(end_ms);

-- Suspicious-match detection (suspect.rs). One row per Polymarket CS2 game: how heavily it was
-- bet (from Polymarket's trade history) and its score against normal lower-tier games.
CREATE TABLE IF NOT EXISTS cs2_activity(
  event_id TEXT PRIMARY KEY,
  slug TEXT, title TEXT, home TEXT, away TEXT, league TEXT,
  start_ms INTEGER,
  vrs_home INTEGER, vrs_away INTEGER, tier1 INTEGER NOT NULL DEFAULT 0,
  volume REAL, trades INTEGER, bets INTEGER, big_dog INTEGER, big_fav INTEGER, big_share REAL, drift REAL,
  score REAL,
  final INTEGER NOT NULL DEFAULT 0,
  updated_ms INTEGER
);
CREATE INDEX IF NOT EXISTS cs2_activity_start ON cs2_activity(start_ms);
-- Snipe paper bot: every 4-hourly re-choice of its rules, per market (adaptive.rs).
CREATE TABLE IF NOT EXISTS adaptive_runs(
  ts_ms INTEGER NOT NULL,
  base TEXT NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY(ts_ms, base)
);
-- Other machines' databases merged into this one (data.rs).
CREATE TABLE IF NOT EXISTS merges(
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL,
  bytes INTEGER,
  finished_ms INTEGER,
  result TEXT
);
-- Pre-match bookmaker line moves that crossed the threshold (latest state per match + outcome).
CREATE TABLE IF NOT EXISTS line_moves(
  match_id INTEGER NOT NULL,
  outcome TEXT NOT NULL,
  sport TEXT, home TEXT, away TEXT, league TEXT, start_ms INTEGER,
  open_p REAL, now_p REAL, move REAL,
  platforms INTEGER, agree INTEGER,
  moved_ms INTEGER,
  first_ms INTEGER, updated_ms INTEGER,
  PRIMARY KEY(match_id, outcome)
);
"#;

pub fn open(path: &Path) -> Result<Connection> {
    let c = Connection::open(path)?;
    c.busy_timeout(Duration::from_secs(5))?;
    c.execute_batch(SCHEMA)?;
    // Order-book size columns (added Nov 2026); "duplicate column" on later starts is fine.
    for t in ["pm_quotes", "kalshi_quotes"] {
        for col in ["bid_size", "ask_size", "bid_depth", "ask_depth"] {
            let _ = c.execute(&format!("ALTER TABLE {t} ADD COLUMN {col} REAL"), []);
        }
    }
    Ok(c)
}

pub fn open_ro(path: &Path) -> Result<Connection> {
    let c = Connection::open_with_flags(path, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    c.busy_timeout(Duration::from_secs(5))?;
    // Reads straight from the OS page cache (no read call per page, shared by every reader) and
    // a bigger private cache: the replay build reads gigabytes.
    let _ = c.execute_batch("PRAGMA mmap_size = 8589934592; PRAGMA cache_size = -32768; PRAGMA temp_store = MEMORY;");
    Ok(c)
}

/// Reload recent matches and book-event links so ids survive a restart.
pub fn restore(path: &Path, engine: &mut Engine) -> Result<()> {
    let c = open(path)?;
    let max: i64 = c.query_row("SELECT COALESCE(MAX(id),0) FROM matches", [], |r| r.get(0))?;
    engine.matcher.set_next_id(max);
    let since = now_ms() - 12 * 3600 * 1000;
    let mut st = c.prepare(
        "SELECT m.id, m.sport, m.home, m.away, m.league, m.start_ms, m.sr_id, m.created_ms, MAX(e.last_seen_ms)
         FROM matches m JOIN book_events e ON e.match_id = m.id
         GROUP BY m.id HAVING MAX(e.last_seen_ms) > ?1",
    )?;
    let rows = st.query_map([since], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            EventIn {
                sport: r.get(1)?,
                home: r.get(2)?,
                away: r.get(3)?,
                league: r.get::<_, Option<String>>(4)?.unwrap_or_default(),
                start_ms: r.get(5)?,
                sr_id: r.get(6)?,
                ..Default::default()
            },
            r.get::<_, i64>(7)?,
            r.get::<_, i64>(8)?,
        ))
    })?;
    for row in rows {
        let (id, ev, created, last) = row?;
        let mut m = Matcher::new_match_info(id, &ev, created);
        m.last_seen_ms = last;
        engine.matcher.restore(m);
    }
    let mut st = c.prepare("SELECT book, book_event_id, match_id FROM book_events WHERE last_seen_ms > ?1")?;
    let rows = st.query_map([since], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?, r.get::<_, i64>(2)?)))?;
    for row in rows {
        let (b, e, m) = row?;
        engine.known.insert((b, e), m);
    }
    Ok(())
}

pub fn spawn_writer(path: PathBuf, mut rx: mpsc::Receiver<Write>) -> std::thread::JoinHandle<()> {
    std::thread::Builder::new()
        .name("sqlite-writer".into())
        .spawn(move || {
            let mut c = match open(&path) {
                Ok(c) => c,
                Err(e) => {
                    tracing::error!("cannot open db {}: {e}", path.display());
                    return;
                }
            };
            let mut buf: Vec<Write> = Vec::with_capacity(4096);
            loop {
                // Block for the first item, then drain whatever else is queued for a short window.
                match rx.blocking_recv() {
                    Some(w) => buf.push(w),
                    None => break,
                }
                let deadline = Instant::now() + Duration::from_millis(200);
                while buf.len() < 20_000 {
                    match rx.try_recv() {
                        Ok(w) => buf.push(w),
                        Err(mpsc::error::TryRecvError::Empty) => {
                            if Instant::now() >= deadline {
                                break;
                            }
                            std::thread::sleep(Duration::from_millis(10));
                        }
                        Err(mpsc::error::TryRecvError::Disconnected) => break,
                    }
                }
                if let Err(e) = flush(&mut c, &mut buf) {
                    tracing::error!("db write failed: {e}");
                    buf.clear();
                }
            }
            let _ = flush(&mut c, &mut buf);
        })
        .expect("spawn writer")
}

fn flush(c: &mut Connection, buf: &mut Vec<Write>) -> Result<()> {
    if buf.is_empty() {
        return Ok(());
    }
    let tx = c.transaction()?;
    {
        let mut ins_change = tx.prepare_cached(
            "INSERT INTO changes(ts_ms,book,book_event_id,match_id,market,line,outcome,kind,old_price,new_price,book_ts_ms)
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
        )?;
        let mut up_event = tx.prepare_cached(
            "INSERT INTO book_events(book,book_event_id,match_id,sport,league,home,away,start_ms,sr_id,first_seen_ms,last_seen_ms)
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)
             ON CONFLICT(book,book_event_id) DO UPDATE SET match_id=excluded.match_id, league=excluded.league,
               home=excluded.home, away=excluded.away, start_ms=excluded.start_ms,
               sr_id=COALESCE(excluded.sr_id, book_events.sr_id), last_seen_ms=MAX(book_events.last_seen_ms, excluded.last_seen_ms)",
        )?;
        let mut up_match = tx.prepare_cached(
            "INSERT INTO matches(id,sport,home,away,league,start_ms,sr_id,created_ms) VALUES(?1,?2,?3,?4,?5,?6,?7,?8)
             ON CONFLICT(id) DO UPDATE SET sr_id=COALESCE(matches.sr_id, excluded.sr_id)",
        )?;
        let mut ins_pm = tx.prepare_cached(
            "INSERT INTO pm_quotes(ts_ms,server_ts_ms,match_id,event_id,outcome,bid,ask,last,bid_size,ask_size,bid_depth,ask_depth) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)",
        )?;
        let mut ins_k = tx.prepare_cached(
            "INSERT INTO kalshi_quotes(ts_ms,server_ts_ms,event,outcome,bid,ask,est,bid_size,ask_size,bid_depth,ask_depth) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
        )?;
        for w in buf.drain(..) {
            match w {
                Write::PmQuote { ts_ms, server_ts_ms, match_id, event_id, outcome, bid, ask, last, depth: d } => {
                    ins_pm.execute(params![ts_ms, server_ts_ms, match_id, event_id, outcome.as_str(), bid, ask, last, d.bid_size, d.ask_size, d.bid_depth, d.ask_depth])?;
                }
                Write::KQuote { ts_ms, server_ts_ms, event, outcome, bid, ask, est, depth: d } => {
                    ins_k.execute(params![ts_ms, server_ts_ms, event, outcome.as_str(), bid, ask, est as i64, d.bid_size, d.ask_size, d.bid_depth, d.ask_depth])?;
                }
                Write::Change(ch) => {
                    ins_change.execute(params![
                        ch.ts_ms,
                        ch.book,
                        ch.book_event_id,
                        ch.match_id,
                        ch.market.as_str(),
                        ch.line,
                        ch.outcome.as_str(),
                        ch.kind.as_str(),
                        ch.old_price,
                        ch.new_price,
                        ch.book_ts_ms
                    ])?;
                }
                Write::Event { book, meta, match_id, first_seen_ms, last_seen_ms } => {
                    up_event.execute(params![
                        book,
                        meta.id,
                        match_id,
                        meta.sport,
                        meta.league,
                        meta.home,
                        meta.away,
                        meta.start_ms,
                        meta.sr_id,
                        first_seen_ms,
                        last_seen_ms
                    ])?;
                }
                Write::Match(m) => {
                    up_match.execute(params![m.id, m.sport, m.home, m.away, m.league, m.start_ms, m.sr_id, m.created_ms])?;
                }
            }
        }
    }
    tx.commit()?;
    Ok(())
}

/// Run a read-only SQL statement and return rows as JSON objects.
pub fn query_json(path: &Path, sql: &str, args: &[rusqlite::types::Value], limit: usize) -> Result<Vec<serde_json::Value>> {
    let c = open_ro(path)?;
    let mut st = c.prepare(sql)?;
    if !st.readonly() {
        anyhow::bail!("only read-only statements are allowed");
    }
    let cols: Vec<String> = st.column_names().iter().map(|s| s.to_string()).collect();
    let mut rows = st.query(rusqlite::params_from_iter(args.iter()))?;
    let mut out = Vec::new();
    while let Some(r) = rows.next()? {
        let mut obj = serde_json::Map::new();
        for (i, name) in cols.iter().enumerate() {
            use rusqlite::types::ValueRef;
            let v = match r.get_ref(i)? {
                ValueRef::Null => serde_json::Value::Null,
                ValueRef::Integer(n) => n.into(),
                ValueRef::Real(f) => serde_json::Number::from_f64(f).map(Into::into).unwrap_or(serde_json::Value::Null),
                ValueRef::Text(t) => String::from_utf8_lossy(t).into_owned().into(),
                ValueRef::Blob(b) => format!("<{} bytes>", b.len()).into(),
            };
            obj.insert(name.clone(), v);
        }
        out.push(serde_json::Value::Object(obj));
        if out.len() >= limit {
            break;
        }
    }
    Ok(out)
}
