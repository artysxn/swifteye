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

-- SISTER markets of a Polymarket game (map winner, totals, handicap, spread ...): top of book of one
-- token per market (the other token is its mirror), probabilities 0..1. kind = Polymarket's own
-- market type. RESEARCH ONLY: no bot reads this table and nothing in it is broadcast.
CREATE TABLE IF NOT EXISTS pm_sister_quotes(
  id INTEGER PRIMARY KEY,
  ts_ms INTEGER NOT NULL,
  server_ts_ms INTEGER,
  match_id INTEGER NOT NULL,
  event_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  line REAL,
  outcome TEXT NOT NULL,
  token TEXT NOT NULL,
  bid REAL, ask REAL, bid_size REAL, ask_size REAL
);
CREATE INDEX IF NOT EXISTS pm_sister_match ON pm_sister_quotes(match_id, ts_ms);
-- Bookmaker prices for PARTS of a game (map 1, first set, first half ...): decimal odds, NULL = not
-- offered now. book_event_id = the book's id of the whole game (join book_events for the match).
-- RESEARCH ONLY, like pm_sister_quotes.
CREATE TABLE IF NOT EXISTS book_part_quotes(
  id INTEGER PRIMARY KEY,
  ts_ms INTEGER NOT NULL,
  book TEXT NOT NULL,
  book_event_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  line REAL NOT NULL,
  outcome TEXT NOT NULL,
  price REAL
);
CREATE INDEX IF NOT EXISTS book_part_event ON book_part_quotes(book, book_event_id, ts_ms);
-- Price changes of RECORDED-ONLY books (books::RECORD_ONLY): the same columns as `changes`, kept
-- apart so nothing that reads `changes` (the bots, the consensus, the replay, the research tools)
-- sees a book before its timing was tested (owner, 22 Nov 2026).
CREATE TABLE IF NOT EXISTS changes_research(
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
CREATE INDEX IF NOT EXISTS changes_research_match ON changes_research(match_id, ts_ms);
-- Bookmakers' LIVE SCORES: one row each time a book's score of a game in play changed, or its
-- detail did (1xBet platform: games / rounds / quarters of each period, the points of the tennis
-- game being played, who serves, red cards and penalties), with the clock as the book showed it.
-- ts_ms = when the poll that showed it arrived. book_event_id = the book's id of the game (join
-- book_events for the match). RESEARCH ONLY (owner, 22 Nov 2026: what caused each jump): nothing
-- reads it live.
CREATE TABLE IF NOT EXISTS book_scores(
  id INTEGER PRIMARY KEY,
  ts_ms INTEGER NOT NULL,
  book TEXT NOT NULL,
  book_event_id TEXT NOT NULL,
  score TEXT,
  detail TEXT,
  clock TEXT
);
CREATE INDEX IF NOT EXISTS book_scores_event ON book_scores(book, book_event_id, ts_ms);

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
    // When SQLite starts the write-ahead file over by itself, also cut it back to this size.
    let _ = c.execute_batch(&format!("PRAGMA journal_size_limit = {WAL_RESET_BYTES};"));
    // Order-book size columns (added Nov 2026); "duplicate column" on later starts is fine.
    for t in ["pm_quotes", "kalshi_quotes"] {
        for col in ["bid_size", "ask_size", "bid_depth", "ask_depth"] {
            let _ = c.execute(&format!("ALTER TABLE {t} ADD COLUMN {col} REAL"), []);
        }
    }
    // Kalshi's best bid / ask as its real-time order book shows them (20 Nov 2026), beside the
    // `ticker` channel's `bid` / `ask`, which arrive late.
    for col in ["book_bid", "book_ask"] {
        let _ = c.execute(&format!("ALTER TABLE kalshi_quotes ADD COLUMN {col} REAL"), []);
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

/// The write-ahead file is started over once it is bigger than this.
const WAL_RESET_BYTES: u64 = 32 << 20;
/// Above this size a try waits longer for readers (see `WalReset::wait_big`).
const WAL_BIG_BYTES: u64 = 1 << 30;

/// When and how patiently the writer starts the write-ahead file over.
#[derive(Clone, Copy)]
pub struct WalReset {
    /// Start the file over once it is bigger than this.
    pub bytes: u64,
    /// How often the writer looks at the file's size.
    pub look: Duration,
    /// The longest one try waits on each reader that is in the way. The writer stores nothing
    /// while it waits, so this stays far below the 400 ms the snipe bot allows for storing.
    pub wait: Duration,
    /// The same, once the file is past `WAL_BIG_BYTES`: something has blocked every short try.
    pub wait_big: Duration,
}

impl Default for WalReset {
    fn default() -> Self {
        WalReset { bytes: WAL_RESET_BYTES, look: Duration::from_secs(5), wait: Duration::from_millis(60), wait_big: Duration::from_millis(250) }
    }
}

/// Start the write-ahead file over when it has grown past `cfg.bytes`. Returns how long the
/// writer was held, and the file's size before.
///
/// SQLite starts the file over by itself only at a moment when no reader is using it. The snipe
/// bot reads on every bookmaker move and web pages poll, so a reader is always in flight: that
/// moment never came and the file grew without end (1.7 GB in an hour on 20 Nov 2026, about
/// 65 MB a minute). So the writer asks for it: copy what is left into the database, wait
/// briefly for the readers that began earlier, cut the file to zero. Readers are never blocked,
/// and a try that cannot finish in time changes nothing and is repeated at the next look.
fn reset_wal(c: &Connection, wal: &Path, cfg: &WalReset) -> (Duration, u64) {
    let size = std::fs::metadata(wal).map(|m| m.len()).unwrap_or(0);
    if size <= cfg.bytes {
        return (Duration::ZERO, size);
    }
    let t = Instant::now();
    let _ = c.busy_timeout(if size > WAL_BIG_BYTES { cfg.wait_big } else { cfg.wait });
    // One row: (1 when it could not finish, frames in the file, frames copied to the database).
    let _ = c.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()));
    let _ = c.busy_timeout(Duration::from_secs(5));
    (t.elapsed(), size)
}

pub fn spawn_writer(path: PathBuf, rx: mpsc::Receiver<Write>) -> std::thread::JoinHandle<()> {
    spawn_writer_with(path, rx, WalReset::default())
}

pub fn spawn_writer_with(path: PathBuf, mut rx: mpsc::Receiver<Write>, wal_cfg: WalReset) -> std::thread::JoinHandle<()> {
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
            let wal = PathBuf::from(format!("{}-wal", path.display()));
            let mut looked = Instant::now();
            // Time the last reset try held the writer: taken off the next collecting window, so
            // what arrived meanwhile is stored no later than it would have been.
            let mut held = Duration::ZERO;
            let mut warned: Option<Instant> = None;
            let mut buf: Vec<Write> = Vec::with_capacity(4096);
            loop {
                // Block for the first item, then drain whatever else is queued for a short window.
                match rx.blocking_recv() {
                    Some(w) => buf.push(w),
                    None => break,
                }
                let deadline = Instant::now() + Duration::from_millis(200).saturating_sub(std::mem::take(&mut held));
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
                if looked.elapsed() >= wal_cfg.look {
                    looked = Instant::now();
                    let (took, before) = reset_wal(&c, &wal, &wal_cfg);
                    held = took;
                    let after = std::fs::metadata(&wal).map(|m| m.len()).unwrap_or(0);
                    if before > WAL_BIG_BYTES && after < before {
                        tracing::info!("write-ahead file started over (it had reached {} MB)", before >> 20);
                    } else if after > WAL_BIG_BYTES && warned.map_or(true, |w| w.elapsed() > Duration::from_secs(600)) {
                        warned = Some(Instant::now());
                        tracing::warn!("write-ahead file is {} MB and cannot be started over yet: a long read is in the way", after >> 20);
                    }
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
        let mut ins_sister = tx.prepare_cached(
            "INSERT INTO pm_sister_quotes(ts_ms,server_ts_ms,match_id,event_id,kind,line,outcome,token,bid,ask,bid_size,ask_size) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)",
        )?;
        let mut ins_part = tx.prepare_cached("INSERT INTO book_part_quotes(ts_ms,book,book_event_id,kind,line,outcome,price) VALUES(?1,?2,?3,?4,?5,?6,?7)")?;
        let mut ins_research = tx.prepare_cached(
            "INSERT INTO changes_research(ts_ms,book,book_event_id,match_id,market,line,outcome,kind,old_price,new_price,book_ts_ms)
             VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11)",
        )?;
        let mut ins_score = tx.prepare_cached("INSERT INTO book_scores(ts_ms,book,book_event_id,score,detail,clock) VALUES(?1,?2,?3,?4,?5,?6)")?;
        let mut ins_k = tx.prepare_cached(
            "INSERT INTO kalshi_quotes(ts_ms,server_ts_ms,event,outcome,bid,ask,est,bid_size,ask_size,bid_depth,ask_depth,book_bid,book_ask) VALUES(?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13)",
        )?;
        for w in buf.drain(..) {
            match w {
                Write::PmQuote { ts_ms, server_ts_ms, match_id, event_id, outcome, bid, ask, last, depth: d } => {
                    ins_pm.execute(params![ts_ms, server_ts_ms, match_id, event_id, outcome.as_str(), bid, ask, last, d.bid_size, d.ask_size, d.bid_depth, d.ask_depth])?;
                }
                Write::PmSister { ts_ms, server_ts_ms, match_id, event_id, kind, line, outcome, token, bid, ask, depth: d } => {
                    ins_sister.execute(params![ts_ms, server_ts_ms, match_id, event_id, kind, line, outcome, token, bid, ask, d.bid_size, d.ask_size])?;
                }
                Write::BookPart { ts_ms, book, book_event_id, kind, line, outcome, price } => {
                    ins_part.execute(params![ts_ms, book, book_event_id, kind, line, outcome.as_str(), price])?;
                }
                Write::ChangeResearch(ch) => {
                    ins_research.execute(params![
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
                Write::BookScore { ts_ms, book, book_event_id, score, detail, clock } => {
                    ins_score.execute(params![ts_ms, book, book_event_id, score, detail, clock])?;
                }
                Write::KQuote { ts_ms, server_ts_ms, event, outcome, bid, ask, est, depth: d, book } => {
                    ins_k.execute(params![ts_ms, server_ts_ms, event, outcome.as_str(), bid, ask, est as i64, d.bid_size, d.ask_size, d.bid_depth, d.ask_depth, book.0, book.1])?;
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::Arc;

    fn change(i: i64) -> Write {
        Write::Change(Change {
            ts_ms: 1_700_000_000_000 + i,
            book: "testbook",
            book_event_id: format!("e{}", i % 97),
            match_id: i % 97,
            market: Market::X12,
            line: None,
            outcome: Outcome::Home,
            kind: ChangeKind::Move,
            old_price: Some(1.9),
            new_price: Some(2.0),
            book_ts_ms: None,
        })
    }

    /// Writes for `secs` seconds while three readers overlap without a pause (the snipe bot's
    /// load). Returns the write-ahead file's biggest and last size.
    fn run(name: &str, cfg: WalReset, secs: u64) -> (u64, u64) {
        let dir = std::env::temp_dir().join(format!("swifteye-wal-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let db = dir.join("t.db");
        drop(open(&db).unwrap());
        let (tx, rx) = mpsc::channel::<Write>(100_000);
        let writer = spawn_writer_with(db.clone(), rx, cfg);
        let stop = Arc::new(AtomicBool::new(false));
        let readers: Vec<_> = (0..3u64)
            .map(|k| {
                let (db, stop) = (db.clone(), stop.clone());
                std::thread::spawn(move || {
                    let c = open_ro(&db).unwrap();
                    std::thread::sleep(Duration::from_millis(10 * k));
                    while !stop.load(Ordering::Relaxed) {
                        // A read that stays open 30 ms: with three of them staggered, one is
                        // always in flight.
                        let _ = c.execute_batch("BEGIN");
                        let _: i64 = c.query_row("SELECT COUNT(*) FROM changes WHERE match_id = 5", [], |r| r.get(0)).unwrap_or(0);
                        std::thread::sleep(Duration::from_millis(30));
                        let _ = c.execute_batch("COMMIT");
                    }
                })
            })
            .collect();
        let wal = PathBuf::from(format!("{}-wal", db.display()));
        let (mut top, t, mut i) = (0u64, Instant::now(), 0i64);
        while t.elapsed() < Duration::from_secs(secs) {
            for _ in 0..400 {
                tx.blocking_send(change(i)).unwrap();
                i += 1;
            }
            std::thread::sleep(Duration::from_millis(20));
            top = top.max(std::fs::metadata(&wal).map(|m| m.len()).unwrap_or(0));
        }
        std::thread::sleep(Duration::from_millis(600));
        let last = std::fs::metadata(&wal).map(|m| m.len()).unwrap_or(0);
        let rows: i64 = open_ro(&db).unwrap().query_row("SELECT COUNT(*) FROM changes", [], |r| r.get(0)).unwrap();
        assert_eq!(rows, i, "every write is stored");
        stop.store(true, Ordering::Relaxed);
        for r in readers {
            r.join().unwrap();
        }
        drop(tx);
        writer.join().unwrap();
        let _ = std::fs::remove_dir_all(&dir);
        (top, last)
    }

    /// With readers always in flight the write-ahead file only grows; with the writer's reset it
    /// stays near the size it is reset at.
    #[test]
    fn wal_is_started_over_under_constant_readers() {
        let never = WalReset { bytes: u64::MAX, ..WalReset::default() };
        let (top_off, last_off) = run("off", never, 6);
        let on = WalReset { bytes: 256 << 10, look: Duration::from_millis(200), ..WalReset::default() };
        let (top_on, last_on) = run("on", on, 6);
        println!("write-ahead file, no reset: top {} KB, end {} KB | with reset: top {} KB, end {} KB", top_off >> 10, last_off >> 10, top_on >> 10, last_on >> 10);
        assert!(last_off > 4 << 20, "the load must reproduce the growth (got {} KB)", last_off >> 10);
        assert!(top_on < 6 << 20, "the reset keeps the file small (top {} KB)", top_on >> 10);
        assert!(top_on * 4 < top_off);
    }
}
