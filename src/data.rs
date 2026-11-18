//! Data page: what is stored and how much it weighs, other datasets found on disk, snapshots
//! for moving data between machines, and merging another machine's database into this one.
//!
//! ## Merge
//! The Mac and the PC each number matches themselves, so a foreign match id means nothing
//! here. Each foreign match is mapped to a local one by, in order: a shared book event (same
//! book + event id), the same Sportradar id, or the same sport, kick-off within 45 min and
//! matching team names; otherwise it gets a new local id from the running matcher. Every table
//! is then copied with ids rewritten.
//!
//! Where both machines recorded the same source at the same time, this database's rows win:
//! a foreign price row is skipped when this database has a row for the same book event
//! (Polymarket event / Kalshi contract) within a minute of it. Each machine's own data fills the
//! hours the other was off. Rows are copied in batches of 20,000 so the live writer is never
//! locked out for long. Merging the same file twice adds nothing new.

use crate::model::*;
use crate::App;
use anyhow::{anyhow, Context, Result};
use parking_lot::Mutex;
use rusqlite::{params, Connection, OpenFlags, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock};
use std::time::Duration;

const BATCH: i64 = 20_000;
const MATCH_WINDOW_MS: Ms = 45 * 60_000;

#[derive(Serialize, Deserialize, Clone, Debug, Default)]
#[serde(default)]
pub struct DataCfg {
    /// Extra folders to scan for other datasets (besides the database folder and its import/).
    pub folders: Vec<String>,
}

#[derive(Serialize, Clone, Debug, Default)]
pub struct Job {
    pub kind: String,
    pub path: String,
    pub running: bool,
    pub stage: String,
    pub done: i64,
    pub total: i64,
    pub started_ms: Ms,
    pub finished_ms: Option<Ms>,
    pub error: Option<String>,
    pub result: Value,
}

static JOB: LazyLock<Mutex<Job>> = LazyLock::new(Default::default);
static STATS: LazyLock<Mutex<Option<(Ms, Value)>>> = LazyLock::new(Default::default);

fn dir(app: &App) -> PathBuf {
    app.db_path.parent().map(|p| if p.as_os_str().is_empty() { PathBuf::from(".") } else { p.to_path_buf() }).unwrap_or_else(|| PathBuf::from("."))
}

fn cfg_path(app: &App) -> PathBuf {
    dir(app).join("data.json")
}

pub fn cfg(app: &App) -> DataCfg {
    std::fs::read_to_string(cfg_path(app)).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

pub fn save_cfg(app: &App, c: &DataCfg) -> Result<()> {
    std::fs::write(cfg_path(app), serde_json::to_string_pretty(c)?)?;
    Ok(())
}

fn rw(app: &App) -> Result<Connection> {
    let c = Connection::open_with_flags(&app.db_path, OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_URI | OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    c.busy_timeout(Duration::from_secs(10))?;
    Ok(c)
}

fn file_size(p: &Path) -> u64 {
    std::fs::metadata(p).map(|m| m.len()).unwrap_or(0)
}

fn canon(p: &Path) -> String {
    std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf()).display().to_string().trim_start_matches(r"\\?\").to_string()
}

// ---------------------------------------------------------------- what is stored

/// Per table: rows, bytes on disk (table + its indexes), time span. Cached 10 minutes.
pub fn stats(app: &App, fresh: bool) -> Result<Value> {
    if !fresh {
        if let Some((t, v)) = STATS.lock().clone() {
            if now_ms() - t < 600_000 {
                return Ok(v);
            }
        }
    }
    let c = crate::store::open_ro(&app.db_path)?;
    let tables: Vec<String> = {
        let mut st = c.prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")?;
        let it = st.query_map([], |r| r.get(0))?;
        it.filter_map(|x| x.ok()).collect()
    };
    // dbstat: bytes per table or index; indexes folded into their table.
    let owner: HashMap<String, String> = {
        let mut st = c.prepare("SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table', 'index')")?;
        let it = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))?;
        it.filter_map(|x| x.ok()).collect()
    };
    let mut bytes: HashMap<String, (i64, i64)> = HashMap::new();
    if let Ok(mut st) = c.prepare("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name") {
        if let Ok(it) = st.query_map([], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))) {
            for (name, b) in it.filter_map(|x| x.ok()) {
                let t = owner.get(&name).cloned().unwrap_or(name.clone());
                let e = bytes.entry(t).or_default();
                if name == *owner.get(&name).unwrap_or(&name) {
                    e.0 += b;
                } else {
                    e.1 += b;
                }
            }
        }
    }
    let mut rows = Vec::new();
    for t in &tables {
        let cols: Vec<String> = {
            let mut st = c.prepare(&format!("SELECT name FROM pragma_table_info('{t}')"))?;
            let it = st.query_map([], |r| r.get(0))?;
            it.filter_map(|x| x.ok()).collect()
        };
        // COUNT(*) on a few hundred MB is still only a second or two; cached anyway.
        let n: i64 = c.query_row(&format!("SELECT COUNT(*) FROM \"{t}\""), [], |r| r.get(0)).unwrap_or(0);
        let tcol = ["ts_ms", "first_ms", "start_ms", "first_seen_ms", "created_ms"].into_iter().find(|x| cols.iter().any(|c| c == x));
        let (lo, hi): (Option<i64>, Option<i64>) = match tcol {
            Some(col) => c.query_row(&format!("SELECT MIN({col}), MAX({col}) FROM \"{t}\""), [], |r| Ok((r.get(0)?, r.get(1)?))).unwrap_or((None, None)),
            None => (None, None),
        };
        let (tb, ib) = bytes.get(t).copied().unwrap_or((0, 0));
        rows.push(json!({"table": t, "rows": n, "bytes": tb + ib, "table_bytes": tb, "index_bytes": ib, "columns": cols, "time_column": tcol, "from_ms": lo, "to_ms": hi}));
    }
    rows.sort_by(|a, b| b["bytes"].as_i64().cmp(&a["bytes"].as_i64()));
    let d = dir(app);
    let db = app.db_path.clone();
    let mut files = Vec::new();
    for (name, p) in [
        ("Database", db.clone()),
        ("Write-ahead log", PathBuf::from(format!("{}-wal", db.display()))),
        ("Shared memory", PathBuf::from(format!("{}-shm", db.display()))),
    ] {
        files.push(json!({"name": name, "path": canon(&p), "bytes": file_size(&p)}));
    }
    if let Ok(rd) = std::fs::read_dir(&d) {
        for e in rd.flatten() {
            let p = e.path();
            let ext = p.extension().and_then(|x| x.to_str()).unwrap_or("");
            if ext == "json" || ext == "toml" || ext == "txt" {
                files.push(json!({"name": p.file_name().and_then(|x| x.to_str()).unwrap_or(""), "path": canon(&p), "bytes": file_size(&p)}));
            }
        }
    }
    let merges: Vec<Value> = c
        .prepare("SELECT path, bytes, finished_ms, result FROM merges ORDER BY finished_ms DESC LIMIT 20")
        .and_then(|mut st| {
            let it = st.query_map([], |r| {
                Ok(json!({"path": r.get::<_, String>(0)?, "bytes": r.get::<_, i64>(1)?, "finished_ms": r.get::<_, i64>(2)?,
                    "result": serde_json::from_str::<Value>(&r.get::<_, String>(3)?).unwrap_or(Value::Null)}))
            })?;
            Ok(it.filter_map(|x| x.ok()).collect())
        })
        .unwrap_or_default();
    let v = json!({"computed_ms": now_ms(), "db_path": canon(&db), "host": host(), "tables": rows, "files": files, "merges": merges});
    *STATS.lock() = Some((now_ms(), v.clone()));
    Ok(v)
}

fn host() -> String {
    std::env::var("COMPUTERNAME").or_else(|_| std::env::var("HOSTNAME")).unwrap_or_else(|_| "this-machine".into())
}

// ---------------------------------------------------------------- other datasets

/// .db files in the database folder, its import/ folder and the configured
/// folders, with what they hold (read-only peek).
pub fn scan(app: &App) -> Vec<Value> {
    let d = dir(app);
    // Not export/: those are this machine's own snapshots, made for the other machine.
    let mut folders = vec![d.clone(), d.join("import")];
    folders.extend(cfg(app).folders.iter().map(PathBuf::from));
    let me = canon(&app.db_path);
    let merged: HashMap<String, (i64, i64)> = crate::store::open_ro(&app.db_path)
        .ok()
        .and_then(|c| {
            let mut st = c.prepare("SELECT path, bytes, MAX(finished_ms) FROM merges GROUP BY path").ok()?;
            let it = st.query_map([], |r| Ok((r.get::<_, String>(0)?, (r.get::<_, i64>(1)?, r.get::<_, i64>(2)?)))).ok()?;
            Some(it.filter_map(|x| x.ok()).collect())
        })
        .unwrap_or_default();
    let mut seen = HashSet::new();
    let mut out = Vec::new();
    for f in folders {
        let Ok(rd) = std::fs::read_dir(&f) else { continue };
        for e in rd.flatten() {
            let p = e.path();
            let ext = p.extension().and_then(|x| x.to_str()).unwrap_or("").to_ascii_lowercase();
            if !(ext == "db" || ext == "sqlite" || ext == "sqlite3") {
                continue;
            }
            let path = canon(&p);
            if path == me || !seen.insert(path.clone()) {
                continue;
            }
            let bytes = file_size(&p);
            let modified = e.metadata().ok().and_then(|m| m.modified().ok()).and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_millis() as i64);
            let mut row = json!({"path": path, "folder": canon(&f), "bytes": bytes, "modified_ms": modified});
            match peek(&p) {
                Ok(info) => {
                    row["swifteye"] = json!(true);
                    row["info"] = info;
                }
                Err(e) => {
                    row["swifteye"] = json!(false);
                    row["error"] = json!(format!("{e:#}"));
                }
            }
            if let Some((b, t)) = merged.get(&path) {
                row["merged_ms"] = json!(t);
                row["changed_since_merge"] = json!(*b as u64 != bytes);
            }
            out.push(row);
        }
    }
    out
}

fn open_foreign(p: &Path) -> Result<Connection> {
    let c = Connection::open_with_flags(p, OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX)?;
    c.busy_timeout(Duration::from_secs(5))?;
    Ok(c)
}

/// Rows and time span of a foreign database's main tables.
fn peek(p: &Path) -> Result<Value> {
    let c = open_foreign(p)?;
    let has = |t: &str| c.query_row("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1", [t], |_| Ok(())).optional().ok().flatten().is_some();
    if !has("changes") || !has("matches") {
        return Err(anyhow!("not a Swifteye database (no changes / matches table)"));
    }
    let (lo, hi): (Option<i64>, Option<i64>) = c.query_row("SELECT MIN(ts_ms), MAX(ts_ms) FROM changes", [], |r| Ok((r.get(0)?, r.get(1)?)))?;
    let approx = |t: &str| -> i64 { if has(t) { c.query_row(&format!("SELECT COALESCE(MAX(rowid), 0) FROM \"{t}\""), [], |r| r.get(0)).unwrap_or(0) } else { 0 } };
    Ok(json!({"from_ms": lo, "to_ms": hi, "changes": approx("changes"), "pm_quotes": approx("pm_quotes"), "kalshi_quotes": approx("kalshi_quotes"), "matches": approx("matches")}))
}

// ---------------------------------------------------------------- jobs

pub fn job() -> Job {
    JOB.lock().clone()
}

fn start_job(kind: &str, path: &str) -> Result<()> {
    let mut j = JOB.lock();
    if j.running {
        return Err(anyhow!("a {} is already running", j.kind));
    }
    *j = Job { kind: kind.into(), path: path.into(), running: true, stage: "starting".into(), started_ms: now_ms(), ..Default::default() };
    Ok(())
}

fn progress(stage: &str, done: i64, total: i64) {
    let mut j = JOB.lock();
    j.stage = stage.into();
    j.done = done;
    j.total = total;
}

fn finish(r: Result<Value>) {
    let mut j = JOB.lock();
    j.running = false;
    j.finished_ms = Some(now_ms());
    match r {
        Ok(v) => {
            j.stage = "done".into();
            j.result = v;
        }
        Err(e) => {
            j.stage = "failed".into();
            j.error = Some(format!("{e:#}"));
        }
    }
    *STATS.lock() = None;
}

/// A consistent copy of this database (VACUUM INTO) in export/, to carry to the other machine.
pub fn export(app: Arc<App>) -> Result<String> {
    let d = dir(&app).join("export");
    std::fs::create_dir_all(&d)?;
    let t = time::OffsetDateTime::now_utc();
    let stamp = format!("{:04}{:02}{:02}-{:02}{:02}", t.year(), t.month() as u8, t.day(), t.hour(), t.minute());
    let out = d.join(format!("swifteye-{}-{stamp}.db", host().to_lowercase()));
    let path = canon(&d).to_string() + std::path::MAIN_SEPARATOR_STR + out.file_name().and_then(|x| x.to_str()).unwrap_or("export.db");
    start_job("export", &path)?;
    let p2 = path.clone();
    tokio::task::spawn_blocking(move || {
        progress("copying (a consistent snapshot; large databases take a few minutes)", 0, 0);
        let r = (|| -> Result<Value> {
            let c = crate::store::open_ro(&app.db_path)?;
            c.execute("VACUUM INTO ?1", [&p2])?;
            Ok(json!({"path": p2, "bytes": file_size(Path::new(&p2))}))
        })();
        finish(r);
    });
    Ok(path)
}

pub fn merge(app: Arc<App>, path: String) -> Result<()> {
    let p = PathBuf::from(&path);
    if canon(&p) == canon(&app.db_path) {
        return Err(anyhow!("that is this database"));
    }
    peek(&p)?;
    start_job("merge", &path)?;
    tokio::task::spawn_blocking(move || finish(merge_run(&app, &p)));
    Ok(())
}

/// Column list of a table in a schema ("main" or "f").
fn columns(c: &Connection, schema: &str, t: &str) -> Vec<String> {
    c.prepare(&format!("SELECT name FROM pragma_table_info('{t}', '{schema}')"))
        .and_then(|mut st| {
            let it = st.query_map([], |r| r.get::<_, String>(0))?;
            Ok(it.filter_map(|x| x.ok()).collect())
        })
        .unwrap_or_default()
}

/// For each local column (except the skipped ones): the foreign expression, or NULL when the
/// foreign table is older and lacks it. `over` replaces a column's expression.
fn select_list(local: &[String], foreign: &[String], alias: &str, skip: &[&str], over: &[(&str, &str)]) -> (String, String) {
    let mut cols = Vec::new();
    let mut exprs = Vec::new();
    for c in local {
        if skip.contains(&c.as_str()) {
            continue;
        }
        cols.push(c.clone());
        exprs.push(match over.iter().find(|o| o.0 == c) {
            Some(o) => o.1.to_string(),
            None if foreign.contains(c) => format!("{alias}.{c}"),
            None => "NULL".into(),
        });
    }
    (cols.join(", "), exprs.join(", "))
}

fn merge_run(app: &Arc<App>, p: &Path) -> Result<Value> {
    let c = rw(app)?;
    let uri = format!("file:{}?mode=ro", p.display().to_string().replace('\\', "/").replace('?', "%3f").replace('#', "%23"));
    c.execute("ATTACH DATABASE ?1 AS f", [&uri]).context("opening the other database")?;
    let ftables: HashSet<String> = {
        let mut st = c.prepare("SELECT name FROM f.sqlite_master WHERE type = 'table'")?;
        let it = st.query_map([], |r| r.get::<_, String>(0))?;
        it.filter_map(|x| x.ok()).collect()
    };
    let mut res = serde_json::Map::new();

    // ---- 1. match ids
    progress("matching games", 0, 0);
    let fm: Vec<(i64, String, String, String, Option<String>, Option<i64>, Option<String>, i64)> = {
        let mut st = c.prepare("SELECT id, sport, home, away, league, start_ms, sr_id, created_ms FROM f.matches")?;
        let it = st.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?, r.get(6)?, r.get(7)?)))?;
        it.filter_map(|x| x.ok()).collect()
    };
    let mut map: HashMap<i64, i64> = HashMap::new();
    // a) shared book events: same book + event id is the same game.
    {
        let mut st = c.prepare(
            "SELECT fe.match_id, le.match_id, COUNT(*) n FROM f.book_events fe JOIN main.book_events le ON le.book = fe.book AND le.book_event_id = fe.book_event_id
             GROUP BY 1, 2 ORDER BY n DESC",
        )?;
        let it = st.query_map([], |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)))?;
        for (f, l) in it.filter_map(|x| x.ok()) {
            map.entry(f).or_insert(l);
        }
    }
    let by_events = map.len();
    // b) Sportradar id, c) sport + kick-off + names.
    let (lo, hi) = fm.iter().filter_map(|m| m.5).fold((i64::MAX, i64::MIN), |(a, b), s| (a.min(s), b.max(s)));
    let local: Vec<(i64, String, String, String, Option<i64>, Option<String>)> = {
        let mut st = c.prepare("SELECT id, sport, home, away, start_ms, sr_id FROM main.matches WHERE start_ms BETWEEN ?1 AND ?2 OR sr_id IS NOT NULL")?;
        let it = st.query_map(params![lo.saturating_sub(MATCH_WINDOW_MS), hi.saturating_add(MATCH_WINDOW_MS)], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?, r.get(4)?, r.get(5)?)))?;
        it.filter_map(|x| x.ok()).collect()
    };
    let by_sr: HashMap<&str, i64> = local.iter().filter_map(|m| m.5.as_deref().map(|s| (s, m.0))).collect();
    let mut by_sport: HashMap<&str, Vec<(i64, i64, crate::matcher::Names, crate::matcher::Names)>> = HashMap::new();
    for m in &local {
        if let Some(s) = m.4 {
            by_sport.entry(m.1.as_str()).or_default().push((m.0, s, crate::matcher::Names::new(&m.2), crate::matcher::Names::new(&m.3)));
        }
    }
    let (mut by_sr_n, mut by_names, mut new_n) = (0usize, 0usize, 0usize);
    let mut new_rows: Vec<(i64, &(i64, String, String, String, Option<String>, Option<i64>, Option<String>, i64))> = Vec::new();
    for m in &fm {
        if map.contains_key(&m.0) {
            continue;
        }
        if let Some(l) = m.6.as_deref().and_then(|s| by_sr.get(s)) {
            map.insert(m.0, *l);
            by_sr_n += 1;
            continue;
        }
        let hit = m.5.and_then(|start| {
            let (h, a) = (crate::matcher::Names::new(&m.2), crate::matcher::Names::new(&m.3));
            by_sport.get(m.1.as_str())?.iter().filter(|l| (l.1 - start).abs() <= MATCH_WINDOW_MS).find(|l| {
                let straight = l.2.sim(&h).min(l.3.sim(&a));
                let swapped = if m.1 == "football" { 0.0 } else { l.2.sim(&a).min(l.3.sim(&h)) };
                straight.max(swapped) >= 0.75
            }).map(|l| l.0)
        });
        match hit {
            Some(l) => {
                map.insert(m.0, l);
                by_names += 1;
            }
            None => {
                let id = app.engine.lock().matcher.alloc_id();
                map.insert(m.0, id);
                new_rows.push((id, m));
                new_n += 1;
            }
        }
    }
    map.insert(0, 0);
    {
        let tx = c.unchecked_transaction()?;
        {
            let mut ins = tx.prepare("INSERT OR IGNORE INTO main.matches(id, sport, home, away, league, start_ms, sr_id, created_ms) VALUES (?1,?2,?3,?4,?5,?6,?7,?8)")?;
            for (id, m) in &new_rows {
                ins.execute(params![id, m.1, m.2, m.3, m.4, m.5, m.6, m.7])?;
            }
        }
        tx.execute_batch("DROP TABLE IF EXISTS temp.idmap; CREATE TEMP TABLE idmap(fid INTEGER PRIMARY KEY, lid INTEGER NOT NULL);")?;
        {
            let mut ins = tx.prepare("INSERT INTO temp.idmap(fid, lid) VALUES (?1, ?2)")?;
            for (f, l) in &map {
                ins.execute(params![f, l])?;
            }
        }
        tx.commit()?;
    }
    res.insert("matches".into(), json!({"foreign": fm.len(), "same_book_event": by_events, "same_sportradar_id": by_sr_n, "same_teams_and_time": by_names, "new": new_n}));

    // ---- 2. book events (a local link wins)
    if ftables.contains("book_events") {
        progress("book events", 0, 0);
        let (cols, exprs) = select_list(&columns(&c, "main", "book_events"), &columns(&c, "f", "book_events"), "e", &[], &[("match_id", "m.lid")]);
        let n = c.execute(&format!("INSERT OR IGNORE INTO main.book_events({cols}) SELECT {exprs} FROM f.book_events e JOIN temp.idmap m ON m.fid = e.match_id"), [])?;
        res.insert("book_events".into(), json!(n));
    }

    // ---- 3. price tables: skip what this database already recorded around the same minute
    let (flo, fhi): (Option<i64>, Option<i64>) = c.query_row("SELECT MIN(ts_ms), MAX(ts_ms) FROM f.changes", [], |r| Ok((r.get(0)?, r.get(1)?)))?;
    let (flo, fhi) = (flo.unwrap_or(0) - 120_000, fhi.unwrap_or(0) + 120_000);
    // (table, coverage key columns, extra coverage filter, match id column to remap)
    let jobs: [(&str, &[&str], &str, Option<&str>); 3] = [
        ("changes", &["book", "book_event_id"], "", Some("match_id")),
        ("pm_quotes", &["event_id", "outcome"], "", Some("match_id")),
        ("kalshi_quotes", &["event", "outcome"], "est = 0", None),
    ];
    for (t, keys, filt, mcol) in jobs {
        if !ftables.contains(t) {
            continue;
        }
        progress(&format!("{t}: what is already here"), 0, 0);
        let k = keys.join(", ");
        let w = if filt.is_empty() { String::new() } else { format!("AND {filt}") };
        c.execute_batch(&format!(
            "DROP TABLE IF EXISTS temp.cov; CREATE TEMP TABLE cov AS SELECT DISTINCT {k}, ts_ms / 60000 AS m FROM main.{t} WHERE ts_ms BETWEEN {flo} AND {fhi} {w};
             CREATE INDEX temp.cov_i ON cov({k}, m);"
        ))?;
        let lcols = columns(&c, "main", t);
        let fcols = columns(&c, "f", t);
        let over: Vec<(&str, &str)> = mcol.map(|m| vec![(m, "mm.lid")]).unwrap_or_default();
        let (cols, exprs) = select_list(&lcols, &fcols, "x", &["id"], &over);
        let join = mcol.map(|m| format!("JOIN temp.idmap mm ON mm.fid = x.{m}")).unwrap_or_default();
        let cov_match = keys.iter().map(|kk| format!("v.{kk} = x.{kk}")).collect::<Vec<_>>().join(" AND ");
        // Kalshi candle rows (est = 1): copy when not already there.
        let est_clause = if t == "kalshi_quotes" {
            " AND (x.est = 0 AND NOT EXISTS (SELECT 1 FROM temp.cov v WHERE COV AND v.m BETWEEN x.ts_ms / 60000 - 1 AND x.ts_ms / 60000 + 1)
                  OR x.est = 1 AND NOT EXISTS (SELECT 1 FROM main.kalshi_quotes y WHERE y.event = x.event AND y.outcome = x.outcome AND y.ts_ms = x.ts_ms AND y.est = 1))"
                .replace("COV", &cov_match)
        } else {
            format!(" AND NOT EXISTS (SELECT 1 FROM temp.cov v WHERE {cov_match} AND v.m BETWEEN x.ts_ms / 60000 - 1 AND x.ts_ms / 60000 + 1)")
        };
        let sql = format!("INSERT INTO main.{t}({cols}) SELECT {exprs} FROM f.{t} x {join} WHERE x.rowid > ?1 AND x.rowid <= ?2{est_clause}");
        let (rmin, rmax): (i64, i64) = c.query_row(&format!("SELECT COALESCE(MIN(rowid), 0), COALESCE(MAX(rowid), 0) FROM f.{t}"), [], |r| Ok((r.get(0)?, r.get(1)?)))?;
        let mut st = c.prepare(&sql)?;
        let (mut added, mut a) = (0usize, rmin - 1);
        while rmax > 0 && a < rmax {
            let b = (a + BATCH).min(rmax);
            added += st.execute(params![a, b])?;
            progress(&format!("{t}: copying"), b - rmin + 1, rmax - rmin + 1);
            a = b;
            std::thread::sleep(Duration::from_millis(15)); // let the live writer in
        }
        let total = if rmax > 0 { rmax - rmin + 1 } else { 0 };
        res.insert(t.into(), json!({"foreign_rows": total.max(0), "added": added, "skipped_already_recorded": (total.max(0) as usize).saturating_sub(added)}));
    }
    c.execute_batch("DROP TABLE IF EXISTS temp.cov;")?;

    // ---- 4. derived tables
    if ftables.contains("arb_history") {
        progress("arbitrage history", 0, 0);
        let (cols, exprs) = select_list(
            &columns(&c, "main", "arb_history"),
            &columns(&c, "f", "arb_history"),
            "x",
            &["id"],
            &[("match_id", "m.lid"), ("key", "m.lid || substr(x.key, instr(x.key, '|'))")],
        );
        let n = c.execute(
            &format!(
                "INSERT INTO main.arb_history({cols}) SELECT {exprs} FROM f.arb_history x JOIN temp.idmap m ON m.fid = x.match_id
                 WHERE NOT EXISTS (SELECT 1 FROM main.arb_history y WHERE y.match_id = m.lid AND y.market = x.market AND y.first_ms = x.first_ms)"
            ),
            [],
        )?;
        res.insert("arb_history".into(), json!(n));
    }
    if ftables.contains("cs2_activity") {
        progress("CS2 activity", 0, 0);
        let lc = columns(&c, "main", "cs2_activity");
        let (cols, exprs) = select_list(&lc, &columns(&c, "f", "cs2_activity"), "x", &[], &[]);
        let set = lc.iter().filter(|x| *x != "event_id").map(|x| format!("{x} = excluded.{x}")).collect::<Vec<_>>().join(", ");
        // The fuller record (more trades seen) wins.
        let n = c.execute(
            &format!("INSERT INTO main.cs2_activity({cols}) SELECT {exprs} FROM f.cs2_activity x WHERE true ON CONFLICT(event_id) DO UPDATE SET {set} WHERE COALESCE(excluded.trades, 0) > COALESCE(cs2_activity.trades, 0)"),
            [],
        )?;
        res.insert("cs2_activity".into(), json!(n));
    }
    if ftables.contains("line_moves") {
        let (cols, exprs) = select_list(&columns(&c, "main", "line_moves"), &columns(&c, "f", "line_moves"), "x", &[], &[("match_id", "m.lid")]);
        let n = c.execute(&format!("INSERT OR IGNORE INTO main.line_moves({cols}) SELECT {exprs} FROM f.line_moves x JOIN temp.idmap m ON m.fid = x.match_id"), [])?;
        res.insert("line_moves".into(), json!(n));
    }
    c.execute_batch("DROP TABLE IF EXISTS temp.idmap;")?;
    c.execute("DETACH DATABASE f", [])?;
    let result = Value::Object(res);
    c.execute(
        "INSERT INTO merges(path, bytes, finished_ms, result) VALUES (?1, ?2, ?3, ?4)",
        params![canon(p), file_size(p) as i64, now_ms(), result.to_string()],
    )?;
    Ok(result)
}
