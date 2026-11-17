//! Polymarket account sync (read-only): positions, portfolio value, open orders, recent trades.
//!
//! Credentials live in `polymarket.json` next to the database, are edited from the
//! Settings page, and are never sent back to the browser in full. Nothing here places
//! or cancels orders.
//!
//! * Positions, value, activity: public Data API v2 (v1 was retired), keyed by the profile (proxy) wallet.
//! * Open orders and trades: CLOB L2 auth. HMAC-SHA256 over `timestamp + method + path [+ body]`
//!   with the base64url-decoded API secret, sent as POLY_ADDRESS / POLY_SIGNATURE /
//!   POLY_TIMESTAMP / POLY_API_KEY / POLY_PASSPHRASE.

use anyhow::{bail, Context, Result};
use base64::engine::general_purpose::{URL_SAFE, URL_SAFE_NO_PAD};
use base64::Engine as _;
use hmac::{Hmac, Mac};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::Sha256;
use std::path::{Path, PathBuf};

const CLOB: &str = "https://clob.polymarket.com";
const DATA: &str = "https://data-api.polymarket.com";

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct PmCreds {
    /// Polymarket profile / proxy wallet (holds the positions). 0x…
    pub wallet: String,
    /// Address that created the API key (EOA). Defaults to `wallet` when empty.
    pub signer: String,
    pub api_key: String,
    pub api_secret: String,
    pub passphrase: String,
}

pub fn path_for(db: &Path) -> PathBuf {
    db.parent().map(|p| p.join("polymarket.json")).unwrap_or_else(|| PathBuf::from("polymarket.json"))
}

pub fn load(db: &Path) -> PmCreds {
    std::fs::read_to_string(path_for(db)).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

pub fn save(db: &Path, c: &PmCreds) -> Result<()> {
    let p = path_for(db);
    std::fs::write(&p, serde_json::to_string_pretty(c)?).with_context(|| format!("writing {}", p.display()))?;
    Ok(())
}

fn mask(s: &str) -> String {
    if s.is_empty() {
        String::new()
    } else if s.len() <= 8 {
        "••••".into()
    } else {
        format!("{}••••{}", &s[..4], &s[s.len() - 4..])
    }
}

/// What the Settings page may see.
pub fn public_view(c: &PmCreds) -> Value {
    json!({
        "wallet": c.wallet,
        "signer": c.signer,
        "api_key": mask(&c.api_key),
        "api_secret": mask(&c.api_secret),
        "passphrase": mask(&c.passphrase),
        "has_wallet": !c.wallet.is_empty(),
        "has_api": !c.api_key.is_empty() && !c.api_secret.is_empty() && !c.passphrase.is_empty(),
    })
}

/// Apply an edit from the Settings page. Masked values (unchanged fields) are ignored.
pub fn merge(mut c: PmCreds, edit: &Value) -> PmCreds {
    let take = |field: &str, cur: &mut String| {
        if let Some(v) = edit.get(field).and_then(|v| v.as_str()) {
            let v = v.trim();
            if !v.contains('•') {
                *cur = v.to_string();
            }
        }
    };
    take("wallet", &mut c.wallet);
    take("signer", &mut c.signer);
    take("api_key", &mut c.api_key);
    take("api_secret", &mut c.api_secret);
    take("passphrase", &mut c.passphrase);
    c
}

fn l2_headers(c: &PmCreds, method: &str, path: &str, body: &str) -> Result<Vec<(&'static str, String)>> {
    let ts = (crate::clock::now_ms() / 1000).to_string();
    let secret = URL_SAFE
        .decode(c.api_secret.trim())
        .or_else(|_| URL_SAFE_NO_PAD.decode(c.api_secret.trim().trim_end_matches('=')))
        .or_else(|_| base64::engine::general_purpose::STANDARD.decode(c.api_secret.trim()))
        .context("API secret is not valid base64")?;
    let mut mac = Hmac::<Sha256>::new_from_slice(&secret)?;
    mac.update(format!("{ts}{method}{path}{body}").as_bytes());
    let sig = URL_SAFE.encode(mac.finalize().into_bytes());
    let addr = if c.signer.trim().is_empty() { c.wallet.trim() } else { c.signer.trim() };
    Ok(vec![
        ("POLY_ADDRESS", addr.to_string()),
        ("POLY_SIGNATURE", sig),
        ("POLY_TIMESTAMP", ts),
        ("POLY_API_KEY", c.api_key.trim().to_string()),
        ("POLY_PASSPHRASE", c.passphrase.trim().to_string()),
    ])
}

async fn get(http: &reqwest::Client, url: String, headers: Vec<(&'static str, String)>) -> Result<Value> {
    let mut req = http.get(&url);
    for (k, v) in headers {
        req = req.header(k, v);
    }
    let resp = req.send().await?;
    let status = resp.status();
    let text = resp.text().await?;
    if !status.is_success() {
        bail!("HTTP {status}: {}", text.chars().take(200).collect::<String>());
    }
    Ok(serde_json::from_str(&text).unwrap_or(Value::String(text)))
}

/// Everything the Polymarket page shows. Each part fails on its own.
pub async fn snapshot(http: &reqwest::Client, c: &PmCreds) -> Value {
    let mut out = json!({ "synced_ms": crate::clock::now_ms(), "has_wallet": !c.wallet.is_empty(),
        "has_api": !c.api_key.is_empty() && !c.api_secret.is_empty() && !c.passphrase.is_empty() });
    let w = c.wallet.trim().to_lowercase();
    if !w.is_empty() {
        out["positions"] = match get(http, format!("{DATA}/v2/positions?user={w}&limit=500"), vec![]).await {
            Ok(v) => v,
            Err(e) => json!({ "error": format!("{e:#}") }),
        };
        out["value"] = match get(http, format!("{DATA}/v2/value?user={w}"), vec![]).await {
            Ok(v) => v,
            Err(e) => json!({ "error": format!("{e:#}") }),
        };
        out["activity"] = match get(http, format!("{DATA}/v2/activity?user={w}&limit=50"), vec![]).await {
            Ok(v) => v,
            Err(e) => json!({ "error": format!("{e:#}") }),
        };
    }
    if out["has_api"].as_bool() == Some(true) {
        for (key, path) in [("orders", "/data/orders"), ("trades", "/data/trades")] {
            out[key] = match l2_headers(c, "GET", path, "") {
                Ok(h) => match get(http, format!("{CLOB}{path}"), h).await {
                    Ok(v) => v,
                    Err(e) => json!({ "error": format!("{e:#}") }),
                },
                Err(e) => json!({ "error": format!("{e:#}") }),
            };
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn masked_fields_are_not_overwritten() {
        let c = PmCreds { api_key: "abcd1234efgh".into(), ..Default::default() };
        let edited = merge(c, &json!({ "api_key": "abcd••••efgh", "wallet": " 0xabc " }));
        assert_eq!(edited.api_key, "abcd1234efgh");
        assert_eq!(edited.wallet, "0xabc");
    }

    #[test]
    fn signature_is_base64url_hmac() {
        let c = PmCreds { wallet: "0x1".into(), api_key: "k".into(), api_secret: URL_SAFE.encode(b"secret"), passphrase: "p".into(), ..Default::default() };
        let h = l2_headers(&c, "GET", "/data/orders", "").unwrap();
        let sig = &h.iter().find(|x| x.0 == "POLY_SIGNATURE").unwrap().1;
        assert!(URL_SAFE.decode(sig).unwrap().len() == 32);
    }
}
