//! Pinnacle member login, used only to READ odds (`books::pinnacle_member`). Never places bets.
//!
//! The owner types the username and password in Settings; they live in `pinnacle.json` next to
//! the database (never in the repo). The password is never sent back to the browser. A random
//! device id is made once and kept with them (the site sends one as `X-Device-UUID`).
//! `generation` goes up on every save: after Pinnacle refuses a sign-in, the collector waits for
//! a new save instead of trying again (repeated failed sign-ins can lock an account).

use anyhow::{Context, Result};
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::LazyLock;

const MASK: &str = "•••• saved ••••";

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct PinCreds {
    pub username: String,
    pub password: String,
    pub device_uuid: String,
    /// Pinnacle's id for this device after `POST /devices` (sign-in refuses an unregistered UUID).
    pub device_id: String,
    /// A session the owner copied from their own signed-in browser (request header `X-Session`),
    /// for when Pinnacle asks a captcha at sign-in: the owner solves it there, Swifteye only reads.
    pub session_token: String,
    /// That browser's `X-Device-UUID` (a session belongs to the device that signed in).
    pub session_uuid: String,
    /// Pinnacle asked a captcha at the last password sign-in: no new attempt (not even after a
    /// restart) until the owner saves in Settings again.
    pub captcha_asked: bool,
}

/// What the collector is doing, shown under the login in Settings.
#[derive(Clone, Debug, Default, Serialize)]
pub struct Status {
    /// "no login", "signing in", "signed in", "refused", "session ended", "guest only"
    pub state: String,
    pub detail: String,
    pub since_ms: i64,
    /// Last poll: Cloudflare's cache status and the copy's age, to prove the data is live.
    pub cache: String,
    pub age_s: Option<i64>,
    pub last_poll_ms: i64,
    pub games: usize,
    pub polls: u64,
}

static STATUS: LazyLock<Mutex<Status>> = LazyLock::new(Default::default);
static GENERATION: AtomicU64 = AtomicU64::new(1);

pub fn path_for(db: &Path) -> PathBuf {
    db.parent().map(|p| p.join("pinnacle.json")).unwrap_or_else(|| PathBuf::from("pinnacle.json"))
}

pub fn load(db: &Path) -> PinCreds {
    std::fs::read_to_string(path_for(db)).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

pub fn save(db: &Path, c: &PinCreds) -> Result<()> {
    let p = path_for(db);
    std::fs::write(&p, serde_json::to_string_pretty(c)?).with_context(|| format!("writing {}", p.display()))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o600));
    }
    Ok(())
}

pub fn has_login(c: &PinCreds) -> bool {
    !c.username.trim().is_empty() && !c.password.is_empty()
}

pub fn has_session(c: &PinCreds) -> bool {
    !c.session_token.trim().is_empty()
}

/// A random version-4 UUID, made once per login file.
pub fn new_uuid() -> String {
    let b: [u8; 16] = rand::random();
    let h = |r: std::ops::Range<usize>| b[r].iter().map(|x| format!("{x:02x}")).collect::<String>();
    let v = format!("4{}", &h(6..8)[1..]);
    let var = format!("{:x}{}", 8 + (b[8] & 0x3), &h(8..10)[1..]);
    format!("{}-{}-{}-{}-{}", h(0..4), h(4..6), v, var, h(10..16))
}

pub fn public_view(c: &PinCreds) -> Value {
    json!({
        "username": c.username,
        "password": if c.password.is_empty() { "" } else { MASK },
        "has_login": has_login(c) || has_session(c),
        "session_token": if c.session_token.is_empty() { "" } else { MASK },
        "session_uuid": c.session_uuid,
        "status": status(),
    })
}

/// Apply an edit from Settings. The masked placeholder keeps the saved password.
pub fn merge(mut c: PinCreds, edit: &Value) -> PinCreds {
    if let Some(v) = edit.get("username").and_then(|v| v.as_str()) {
        c.username = v.trim().to_string();
    }
    if let Some(v) = edit.get("password").and_then(|v| v.as_str()) {
        if v != MASK {
            c.password = v.to_string();
        }
    }
    if let Some(v) = edit.get("session_token").and_then(|v| v.as_str()) {
        if v != MASK {
            c.session_token = v.trim().to_string();
        }
    }
    if let Some(v) = edit.get("session_uuid").and_then(|v| v.as_str()) {
        c.session_uuid = v.trim().to_string();
    }
    if c.device_uuid.is_empty() {
        c.device_uuid = new_uuid();
    }
    c.captcha_asked = false; // a save is the owner's go-ahead for one more sign-in
    c
}

pub fn generation() -> u64 {
    GENERATION.load(Ordering::Relaxed)
}

/// A new save: the collector may sign in again (once).
pub fn bump() {
    GENERATION.fetch_add(1, Ordering::Relaxed);
}

pub fn status() -> Status {
    STATUS.lock().clone()
}

pub fn set_state(state: &str, detail: impl Into<String>) {
    let mut s = STATUS.lock();
    if s.state != state {
        s.since_ms = crate::model::now_ms();
    }
    s.state = state.to_string();
    s.detail = detail.into();
}

pub fn note_poll(cache: &str, age_s: Option<i64>, games: usize) {
    let mut s = STATUS.lock();
    s.cache = cache.to_string();
    s.age_s = age_s;
    s.games = games;
    s.polls += 1;
    s.last_poll_ms = crate::model::now_ms();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_saved_password_is_masked_and_kept() {
        let c = merge(PinCreds::default(), &json!({"username": " me ", "password": "pw"}));
        assert_eq!((c.username.as_str(), c.password.as_str()), ("me", "pw"));
        assert_eq!(c.device_uuid.len(), 36);
        let v = public_view(&c);
        assert_eq!(v["password"], MASK);
        assert!(!v.to_string().contains("\"pw\""), "the password never goes back to the browser");
        let c = merge(c, &json!({"session_token": " tok ", "session_uuid": " u-1 "}));
        assert_eq!((c.session_token.as_str(), c.session_uuid.as_str()), ("tok", "u-1"));
        assert_eq!(public_view(&c)["session_token"], MASK);
        assert!(!public_view(&c).to_string().contains("\"tok\""), "the session never goes back either");
        assert_eq!(merge(c.clone(), &json!({"session_token": MASK})).session_token, "tok");
        assert_eq!(merge(c.clone(), &json!({"session_token": ""})).session_token, "", "an empty box clears it");
        let c2 = merge(c.clone(), &json!({"username": "me", "password": MASK}));
        assert_eq!(c2.password, "pw");
        assert_eq!(c2.device_uuid, c.device_uuid, "the device id is made once");
        let u = new_uuid();
        assert_eq!(&u[14..15], "4");
        assert!("89ab".contains(&u[19..20]));
    }
}
