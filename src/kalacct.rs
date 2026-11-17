//! Kalshi account sync (read-only): balance, positions, resting orders, recent fills.
//!
//! Credentials (API key id + RSA private key PEM) live in `kalshi.json` next to the
//! database and are edited from Settings; the private key is never sent back to the browser.
//! Requests are signed the way Kalshi documents: RSA-PSS (SHA-256) over
//! `timestamp_ms + METHOD + path`, sent as KALSHI-ACCESS-KEY / -SIGNATURE / -TIMESTAMP.
//! Nothing here places or cancels orders.

use anyhow::{bail, Context, Result};
use base64::Engine as _;
use rsa::pkcs1::DecodeRsaPrivateKey;
use rsa::pkcs8::DecodePrivateKey;
use rsa::pss::BlindedSigningKey;
use rsa::signature::{RandomizedSigner, SignatureEncoding};
use rsa::RsaPrivateKey;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::Sha256;
use std::path::{Path, PathBuf};

const HOST: &str = "https://api.elections.kalshi.com";
const BASE: &str = "/trade-api/v2";

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
#[serde(default)]
pub struct KalshiCreds {
    pub key_id: String,
    /// PEM, PKCS#1 ("BEGIN RSA PRIVATE KEY") or PKCS#8 ("BEGIN PRIVATE KEY").
    pub private_key: String,
}

pub fn path_for(db: &Path) -> PathBuf {
    db.parent().map(|p| p.join("kalshi.json")).unwrap_or_else(|| PathBuf::from("kalshi.json"))
}

pub fn load(db: &Path) -> KalshiCreds {
    std::fs::read_to_string(path_for(db)).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

pub fn save(db: &Path, c: &KalshiCreds) -> Result<()> {
    let p = path_for(db);
    std::fs::write(&p, serde_json::to_string_pretty(c)?).with_context(|| format!("writing {}", p.display()))
}

pub fn public_view(c: &KalshiCreds) -> Value {
    let key_ok = !c.private_key.trim().is_empty() && parse_key(&c.private_key).is_ok();
    json!({
        "key_id": c.key_id,
        "private_key": if c.private_key.trim().is_empty() { "" } else { "•••• saved ••••" },
        "has_key": !c.key_id.trim().is_empty() && key_ok,
        "key_valid": key_ok,
    })
}

/// Apply an edit from Settings. The masked placeholder keeps the saved key.
pub fn merge(mut c: KalshiCreds, edit: &Value) -> KalshiCreds {
    if let Some(v) = edit.get("key_id").and_then(|v| v.as_str()) {
        c.key_id = v.trim().to_string();
    }
    if let Some(v) = edit.get("private_key").and_then(|v| v.as_str()) {
        if !v.contains('•') {
            c.private_key = v.trim().to_string();
        }
    }
    c
}

fn parse_key(pem: &str) -> Result<RsaPrivateKey> {
    let pem = pem.trim();
    RsaPrivateKey::from_pkcs8_pem(pem)
        .or_else(|_| RsaPrivateKey::from_pkcs1_pem(pem))
        .context("private key is not a PEM RSA key (PKCS#1 or PKCS#8)")
}

fn headers(c: &KalshiCreds, method: &str, path: &str) -> Result<Vec<(&'static str, String)>> {
    let key = parse_key(&c.private_key)?;
    let ts = crate::clock::now_ms().to_string();
    let signer = BlindedSigningKey::<Sha256>::new(key);
    let sig = signer.sign_with_rng(&mut rand::thread_rng(), format!("{ts}{method}{path}").as_bytes());
    Ok(vec![
        ("KALSHI-ACCESS-KEY", c.key_id.trim().to_string()),
        ("KALSHI-ACCESS-SIGNATURE", base64::engine::general_purpose::STANDARD.encode(sig.to_bytes())),
        ("KALSHI-ACCESS-TIMESTAMP", ts),
    ])
}

async fn get(http: &reqwest::Client, c: &KalshiCreds, path_q: &str) -> Result<Value> {
    let path = path_q.split('?').next().unwrap_or(path_q);
    let mut req = http.get(format!("{HOST}{BASE}{path_q}"));
    for (k, v) in headers(c, "GET", &format!("{BASE}{path}"))? {
        req = req.header(k, v);
    }
    let resp = req.send().await?;
    let status = resp.status();
    let body = resp.text().await?;
    if !status.is_success() {
        bail!("HTTP {status}: {}", body.chars().take(200).collect::<String>());
    }
    Ok(serde_json::from_str(&body)?)
}

pub async fn snapshot(http: &reqwest::Client, c: &KalshiCreds) -> Value {
    let mut out = json!({ "synced_ms": crate::clock::now_ms(), "has_key": !c.key_id.trim().is_empty() && !c.private_key.trim().is_empty() });
    if out["has_key"].as_bool() != Some(true) {
        return out;
    }
    for (k, p) in [
        ("balance", "/portfolio/balance"),
        ("positions", "/portfolio/positions?limit=200&count_filter=position"),
        ("orders", "/portfolio/orders?status=resting&limit=200"),
        ("fills", "/portfolio/fills?limit=50"),
    ] {
        out[k] = match get(http, c, p).await {
            Ok(v) => v,
            Err(e) => json!({ "error": format!("{e:#}") }),
        };
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signs_with_a_generated_key() {
        use rsa::pkcs8::EncodePrivateKey;
        let key = RsaPrivateKey::new(&mut rand::thread_rng(), 1024).unwrap();
        let pem = key.to_pkcs8_pem(rsa::pkcs8::LineEnding::LF).unwrap().to_string();
        let c = KalshiCreds { key_id: "abc".into(), private_key: pem };
        let h = headers(&c, "GET", "/trade-api/v2/portfolio/balance").unwrap();
        assert_eq!(h[0].1, "abc");
        assert!(base64::engine::general_purpose::STANDARD.decode(&h[1].1).unwrap().len() == 128);
        assert!(public_view(&c)["has_key"].as_bool().unwrap());
    }

    #[test]
    fn masked_key_is_kept() {
        let c = KalshiCreds { key_id: "a".into(), private_key: "PEM".into() };
        let e = merge(c, &json!({"private_key": "•••• saved ••••", "key_id": " b "}));
        assert_eq!(e.private_key, "PEM");
        assert_eq!(e.key_id, "b");
    }
}
