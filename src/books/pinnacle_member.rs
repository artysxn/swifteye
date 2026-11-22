//! Pinnacle as a signed-in member: RECORDED ONLY (`books::RECORD_ONLY`) until its timing is tested.
//!
//! Why: the guest API (`pinnacle.rs`) is a Cloudflare copy kept up to ~15 min (max-age 905; a live
//! football list was 877 s old and byte-identical for a minute on 2026-11-22). Pinnacle's site sends
//! members to `api.arcadia.pinnacle.com`, which is never cached (`no-store`, cache BYPASS), and polls
//! its live markets every 3 s (`app.polling.events.markets` in `/config/app.json`).
//!
//! Sign-in, as the site does it: `POST /0.1/sessions {username, password}` with the public
//! `X-API-Key` and a device id (`X-Device-UUID`); the reply's `token` goes in `X-Session`.
//! Like the site's own requests they carry `Origin: https://www.pinnacle.com` (sign-in answers
//! 401 INVALID_ORIGIN without it). Without a valid session the member host answers 307 to the guest host, so this client never
//! follows redirects: a redirect means "not served as a member" and is reported, not used.
//!
//! Account safety: a refused sign-in (wrong password, captcha asked, trust code) is never retried
//! until the owner saves the login again; a session that ends is renewed at most 3 times an hour.
//! Nothing here answers or gets around a captcha. Read-only: no bet, balance or account calls.

use super::*;
use std::collections::{HashMap, VecDeque};
use std::path::PathBuf;

const ROOT: &str = "https://api.arcadia.pinnacle.com/0.1";
/// Device registration and sign-in go here, as on the site (it uses the member host only once signed in).
const GUEST: &str = "https://guest.api.arcadia.pinnacle.com/0.1";
/// Sign-ins allowed per hour after sessions end (the first one per saved login is always allowed).
const RENEW_PER_HOUR: usize = 3;

pub struct PinMember {
    db: PathBuf,
    sports: Vec<(i64, &'static str)>,
    token: Option<String>,
    /// The login generation whose sign-in was refused: wait for a new save.
    refused_gen: Option<u64>,
    /// The generation the current token came from (a new save signs in again).
    token_gen: u64,
    tries: VecDeque<Ms>,
    idle_until: HashMap<i64, Ms>,
}

impl PinMember {
    pub fn new(db: PathBuf, follows: impl Fn(&str) -> bool) -> Self {
        let sports = super::pinnacle::SPORT_IDS.iter().copied().filter(|(_, s)| follows(s)).collect();
        PinMember { db, sports, token: None, refused_gen: None, token_gen: 0, tries: VecDeque::new(), idle_until: HashMap::new() }
    }
}

fn empty() -> Snapshot {
    Snapshot { events: Vec::new(), recv_ms: now_ms() }
}

/// The server's own words, short (its reply never holds the password).
fn short(body: &str) -> String {
    let b: String = body.chars().filter(|c| !c.is_control()).take(240).collect();
    if b.trim().is_empty() { "no message".into() } else { b }
}

/// Minutes to add to local time to get UTC, as the browser's `getTimezoneOffset()` (Oslo winter: -60).
fn tz_offset_min() -> i64 {
    let z = std::process::Command::new("date").arg("+%z").output().ok().map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default();
    let (sign, rest) = match z.chars().next() {
        Some('-') => (-1, &z[1..]),
        Some('+') => (1, &z[1..]),
        _ => return 0,
    };
    let (h, m) = (rest.get(0..2).and_then(|x| x.parse::<i64>().ok()), rest.get(2..4).and_then(|x| x.parse::<i64>().ok()));
    match (h, m) {
        (Some(h), Some(m)) => -sign * (h * 60 + m),
        _ => 0,
    }
}

/// Register this device the way the site does on a first visit (`POST /devices` on the guest
/// host, X-API-Key + X-Device-UUID). Sign-in answers 401 BAD_UUID ("Unknown device UUID") without it.
/// Honest details: country NO, this Mac's time zone, and the same Chrome-on-Windows the bot's
/// User-Agent already says (`books::UA`).
async fn register_device(http: &reqwest::Client, c: &crate::pinacct::PinCreds) -> Result<String> {
    let body = serde_json::json!({
        "clientVersion": "8.4.42", "country": "NO", "language": "en", "osName": "Windows", "osVersion": "10",
        "tzOffset": tz_offset_min(), "uuid": c.device_uuid, "browserName": "Chrome", "browserVersion": "131.0.0.0",
    });
    let resp = http
        .post(format!("{GUEST}/devices"))
        .header("X-API-Key", super::pinnacle::KEY)
        .header("X-Device-UUID", &c.device_uuid)
        .header("Referer", "https://www.pinnacle.com/")
        .header("Origin", "https://www.pinnacle.com")
        .header("Content-Type", "application/json")
        .body(body.to_string())
        .send()
        .await?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        bail!("device registration: HTTP {status}: {}", short(&text));
    }
    let v: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
    let id = ["id", "deviceId"].iter().find_map(|k| match &v[*k] {
        Value::String(s) if !s.is_empty() => Some(s.clone()),
        Value::Number(n) => Some(n.to_string()),
        _ => None,
    });
    Ok(id.unwrap_or_else(|| "registered".into()))
}

/// Sign in as the site does: on the guest host (the site is not "logged in" yet when it calls this).
async fn sign_in(http: &reqwest::Client, c: &crate::pinacct::PinCreds) -> Result<String> {
    let body = serde_json::json!({"username": c.username, "password": c.password, "geolocation": "", "queryFields": ""});
    let resp = http
        .post(format!("{GUEST}/sessions"))
        .header("X-API-Key", super::pinnacle::KEY)
        .header("X-Device-UUID", &c.device_uuid)
        .header("Referer", "https://www.pinnacle.com/")
        .header("Origin", "https://www.pinnacle.com")
        .header("Content-Type", "application/json")
        .body(body.to_string())
        .send()
        .await?;
    let status = resp.status();
    let text = resp.text().await.unwrap_or_default();
    if !status.is_success() {
        bail!("HTTP {status}: {}", short(&text));
    }
    let v: Value = serde_json::from_str(&text).with_context(|| format!("bad reply: {}", short(&text)))?;
    v["token"].as_str().filter(|t| !t.is_empty()).map(String::from).with_context(|| format!("no session token in the reply: {}", short(&text)))
}

/// One member GET: the member host first; when it redirects (Pinnacle does that for accounts
/// with no balance) the same path on the guest host, still with the session. `Ok(None)` = that
/// answer was Cloudflare's cached copy (HIT), i.e. no better than the guest feed.
/// `Err` with "HTTP 401/403" = session over. Returns (body, received, cache status, copy age, via guest).
async fn get(http: &reqwest::Client, uuid: &str, token: &str, path: &str) -> Result<Option<(Value, Ms, String, Option<i64>, bool)>> {
    let send = |root: &str| {
        http.get(format!("{root}{path}"))
            .header("X-API-Key", super::pinnacle::KEY)
            .header("X-Device-UUID", uuid)
            .header("X-Session", token)
            .header("Referer", "https://www.pinnacle.com/")
            .header("Origin", "https://www.pinnacle.com")
            .send()
    };
    let mut resp = send(ROOT).await?;
    let mut via_guest = false;
    if resp.status().is_redirection() {
        resp = send(GUEST).await?;
        via_guest = true;
    }
    let h = |k: &str| resp.headers().get(k).and_then(|v| v.to_str().ok()).map(String::from);
    let cache = h("cf-cache-status").unwrap_or_else(|| "none".into());
    let age = h("age").and_then(|a| a.parse().ok());
    let (v, t) = read_json(resp).await?;
    if cache.eq_ignore_ascii_case("HIT") {
        return Ok(None);
    }
    Ok(Some((v, t, cache, age, via_guest)))
}

impl Book for PinMember {
    fn name(&self) -> &'static str {
        "pinnacle_member"
    }
    fn source(&self) -> String {
        "Pinnacle signed-in member API (uncached live lists; recorded only)".into()
    }
    fn client(&self) -> reqwest::Client {
        http_builder().redirect(reqwest::redirect::Policy::none()).build().expect("http client")
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        use crate::pinacct as acct;
        let c = acct::load(&self.db);
        let pasted = acct::has_session(&c);
        if !pasted && !acct::has_login(&c) {
            self.token = None;
            acct::set_state("no login", "Type your Pinnacle login, or paste a session from your browser, in Settings.");
            return Ok(empty());
        }
        // A pasted browser session is used with that browser's device id.
        let uuid = if pasted && !c.session_uuid.is_empty() { c.session_uuid.clone() } else { c.device_uuid.clone() };
        let gen = acct::generation();
        if self.refused_gen == Some(gen) {
            return Ok(empty());
        }
        if self.token.is_some() && self.token_gen != gen {
            self.token = None; // a new save: sign in with the new login
        }
        if self.token.is_none() && pasted {
            self.token = Some(c.session_token.clone());
            self.token_gen = gen;
            acct::set_state("signed in", "using the session copied from your browser");
        }
        if self.token.is_none() && c.captcha_asked {
            acct::set_state("refused", "Pinnacle asks for a captcha at sign-in, which Swifteye does not answer. Sign in on pinnacle.com in your own browser and paste that session in the two boxes below.");
            return Ok(empty());
        }
        if self.token.is_none() {
            let now = now_ms();
            self.tries.retain(|t| now - *t < 3_600_000);
            let first_for_this_login = self.token_gen != gen;
            if !first_for_this_login && self.tries.len() >= RENEW_PER_HOUR {
                acct::set_state("session ended", "Pinnacle ended the session 3 times this hour; waiting. Save the login again to retry now.");
                return Ok(empty());
            }
            self.tries.push_back(now);
            self.token_gen = gen;
            let mut c = c.clone();
            if c.device_id.is_empty() {
                acct::set_state("signing in", "registering this device");
                match register_device(http, &c).await {
                    Ok(id) => {
                        tracing::info!("pinnacle_member: device registered");
                        c.device_id = id;
                        if let Err(e) = acct::save(&self.db, &c) {
                            tracing::warn!("pinnacle_member: saving the device id: {e:#}");
                        }
                    }
                    Err(e) => {
                        let msg = format!("{e:#}");
                        tracing::warn!("pinnacle_member: {msg}");
                        acct::set_state("refused", format!("Pinnacle refused to register this device. Press Save and sign in to try again. ({msg})"));
                        self.refused_gen = Some(gen);
                        return Ok(empty());
                    }
                }
            }
            acct::set_state("signing in", "");
            match sign_in(http, &c).await {
                Ok(t) => {
                    tracing::info!("pinnacle_member: signed in");
                    acct::set_state("signed in", "");
                    self.token = Some(t);
                }
                Err(e) => {
                    let msg = format!("{e:#}");
                    let low = msg.to_ascii_lowercase();
                    let why = if low.contains("captcha") {
                        "Pinnacle asks for a captcha at sign-in, which Swifteye does not answer. Sign in on pinnacle.com in your own browser and paste that session in the two boxes below."
                    } else if low.contains("trust") {
                        "Pinnacle asked for a trust code (a new device). Sign in once on pinnacle.com in your own browser, then try again."
                    } else {
                        "Pinnacle refused the sign-in. Check the login, then press Save and sign in again."
                    };
                    tracing::warn!("pinnacle_member: sign-in refused: {msg}");
                    if low.contains("captcha") {
                        let mut c = c.clone();
                        c.captcha_asked = true;
                        let _ = acct::save(&self.db, &c);
                    }
                    acct::set_state("refused", format!("{why} ({msg})"));
                    self.refused_gen = Some(gen);
                    return Ok(empty());
                }
            }
        }
        let token = self.token.clone().unwrap_or_default();
        let now = now_ms();
        let due: Vec<(i64, &'static str)> = self.sports.iter().copied().filter(|(id, _)| self.idle_until.get(id).map_or(true, |t| now >= *t)).collect();
        let mut events = Vec::new();
        let mut recv_ms = 0;
        let (mut cache, mut age) = (String::new(), None);
        for (id, sport) in due {
            let (mu, mk) = (format!("/sports/{id}/matchups/live"), format!("/sports/{id}/markets/live/straight"));
            let (a, b) = futures::join!(get(http, &uuid, &token, &mu), get(http, &uuid, &token, &mk));
            let (a, b) = match (a, b) {
                (Ok(Some(a)), Ok(Some(b))) => (a, b),
                (Ok(None), _) | (_, Ok(None)) => {
                    self.token = None;
                    self.refused_gen = Some(gen);
                    acct::set_state("guest only", "Pinnacle sends this account to the guest server, and even with the session it answers with Cloudflare's cached copy (the same as the free feed). Pinnacle's own site does this for accounts with no balance. Stopped: nothing to gain.");
                    return Ok(empty());
                }
                (Err(e), _) | (_, Err(e)) => {
                    let msg = format!("{e:#}");
                    if (msg.contains("HTTP 401") || msg.contains("HTTP 403")) && pasted {
                        self.token = None;
                        self.refused_gen = Some(gen);
                        acct::set_state("session ended", format!("The browser session ended. Sign in on pinnacle.com again and paste the new X-Session. ({})", short(&msg)));
                        return Ok(empty());
                    }
                    if msg.contains("HTTP 401") || msg.contains("HTTP 403") {
                        self.token = None;
                        acct::set_state("session ended", format!("Signing in again on the next poll ({})", short(&msg)));
                        return Ok(empty());
                    }
                    return Err(e);
                }
            };
            recv_ms = recv_ms.max(a.1).max(b.1);
            cache = if b.4 { format!("{} via guest host", b.2) } else { b.2.clone() };
            age = b.3;
            let evs = super::pinnacle::parse(&a.0, &b.0, sport, true);
            if evs.is_empty() {
                self.idle_until.insert(id, now + 30_000);
            } else {
                self.idle_until.remove(&id);
            }
            events.extend(evs);
        }
        if !cache.is_empty() {
            acct::note_poll(&cache, age, events.len());
        }
        Ok(Snapshot { events, recv_ms: if recv_ms > 0 { recv_ms } else { now_ms() } })
    }
}
