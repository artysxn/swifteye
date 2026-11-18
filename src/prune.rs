//! Housekeeping: delete the price history of games no prediction market listed.
//!
//! Every 4 hours (00, 04, 08, 12, 16 and 20 UTC, 10 minutes past, after the paper bot's
//! re-choice) and once 2 minutes after start. A game's `changes` rows are deleted when no
//! Polymarket or Kalshi event was ever linked to it and no source has listed it for 3 hours (it is
//! over, and too late for a prediction market to list it). Kalshi-only games stay: the paper bot
//! trades Kalshi too. Games flagged on the Suspicious page (`line_moves`) stay, so their charts
//! still open. The small `matches` / `book_events` rows stay, so links and merges still know them.
//!
//! Deletes run in batches of 20,000 rows with short pauses, so the live writer is never held up.
//! Freed pages are reused by new data; the file itself only shrinks on a VACUUM.

use crate::model::*;
use crate::App;
use parking_lot::Mutex;
use rusqlite::{params_from_iter, Connection};
use serde_json::{json, Value};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

const EVERY_MS: Ms = 4 * 3_600_000;
const OFFSET_MS: Ms = 10 * 60_000;
const IDLE_MS: Ms = 3 * 3_600_000;
const BATCH: i64 = 20_000;
const IDS_PER_PASS: usize = 300;

static LAST: LazyLock<Mutex<Value>> = LazyLock::new(|| Mutex::new(Value::Null));

/// The last cleanup and the next one (for the Data page).
pub fn status() -> Value {
    let now = now_ms();
    let next = (now - OFFSET_MS).div_euclid(EVERY_MS) * EVERY_MS + EVERY_MS + OFFSET_MS;
    json!({"last": LAST.lock().clone(), "next_ms": next, "rule": "price history of games never listed on Polymarket or Kalshi, 3 hours after the last source listed them"})
}

pub fn spawn(app: Arc<App>) {
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(120)).await;
        loop {
            let db = app.db_path.clone();
            let started = now_ms();
            let r = tokio::task::spawn_blocking(move || run(&db, now_ms())).await;
            let v = match r {
                Ok(Ok((games, rows))) => {
                    tracing::info!("cleanup: deleted {rows} price rows of {games} games no prediction market listed");
                    json!({"started_ms": started, "finished_ms": now_ms(), "games": games, "rows": rows})
                }
                Ok(Err(e)) => {
                    tracing::warn!("cleanup: {e:#}");
                    json!({"started_ms": started, "finished_ms": now_ms(), "error": format!("{e:#}")})
                }
                Err(e) => json!({"started_ms": started, "error": e.to_string()}),
            };
            *LAST.lock() = v;
            let now = now_ms();
            let next = (now - OFFSET_MS).div_euclid(EVERY_MS) * EVERY_MS + EVERY_MS + OFFSET_MS;
            tokio::time::sleep(Duration::from_millis((next - now).max(60_000) as u64)).await;
        }
    });
}

/// Delete; returns (games with rows deleted, rows deleted).
fn run(db: &std::path::Path, now: Ms) -> anyhow::Result<(usize, i64)> {
    let c = Connection::open(db)?;
    c.busy_timeout(Duration::from_secs(10))?;
    let ids: Vec<i64> = {
        let mut st = c.prepare(
            "SELECT match_id FROM book_events GROUP BY match_id
             HAVING SUM(book IN ('polymarket', 'kalshi')) = 0 AND MAX(last_seen_ms) < ?1
                AND match_id NOT IN (SELECT match_id FROM line_moves)",
        )?;
        let it = st.query_map([now - IDLE_MS], |r| r.get(0))?;
        it.filter_map(|x| x.ok()).collect()
    };
    let (mut games, mut rows) = (0usize, 0i64);
    for chunk in ids.chunks(IDS_PER_PASS) {
        let list = vec!["?"; chunk.len()].join(",");
        // Games in this pass that still have rows (index lookup, cheap).
        let have: i64 = c.query_row(&format!("SELECT COUNT(DISTINCT match_id) FROM changes WHERE match_id IN ({list})"), params_from_iter(chunk.iter()), |r| r.get(0))?;
        if have == 0 {
            continue;
        }
        games += have as usize;
        let sql = format!("DELETE FROM changes WHERE id IN (SELECT id FROM changes WHERE match_id IN ({list}) LIMIT {BATCH})");
        loop {
            let n = c.execute(&sql, params_from_iter(chunk.iter()))? as i64;
            if n == 0 {
                break;
            }
            rows += n;
            std::thread::sleep(Duration::from_millis(150));
        }
    }
    Ok((games, rows))
}
