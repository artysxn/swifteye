//! Real-money orders for the bots (bots.rs decides; this places). Only ever called when a bot is
//! switched to Real on the Bots page. Every order is a LIMIT order that either fills now or is
//! cancelled (Polymarket FAK/FOK, Kalshi fill_or_kill / immediate_or_cancel): nothing rests.
//!
//! Polymarket: CLOB `POST /order`, the order signed EIP-712 with the trading key (pmacct.rs
//! `private_key`) for the CTF Exchange (or the Neg-Risk exchange for neg-risk markets), body sent
//! with L2 HMAC headers. Fee rate, tick size and neg-risk flag are read from the CLOB per token.
//! Amounts: whole shares, cent prices, so USDC amounts have at most 2 decimals.
//!
//! Kalshi: `POST /trade-api/v2/portfolio/orders`, signed as kalacct.rs signs reads.
//!
//! NOT exercised against the live venues while this was written (no real orders were placed);
//! the signing is unit-tested against known vectors. Start with small caps.

use crate::kalacct::KalshiCreds;
use crate::pmacct::PmCreds;
use anyhow::{bail, Context, Result};
use k256::ecdsa::SigningKey;
use serde_json::{json, Value};
use sha3::{Digest, Keccak256};

const CLOB: &str = "https://clob.polymarket.com";
const KALSHI: &str = "https://api.elections.kalshi.com";
const CHAIN_ID: u64 = 137;
/// Exchange contracts and their EIP-712 domain versions (docs.polymarket.com, create-order, Nov 2026):
/// CTF markets: version "2", standard / neg-risk exchange; Protocol V2 markets (book `version: v2`):
/// version "3".
const EXCHANGE: &str = "0xE111180000d2663C0091e4f400237545B87B996B";
const NEG_RISK_EXCHANGE: &str = "0xe2222d279d744050d28e00520010520000310F59";
const V2_EXCHANGE: &str = "0xe3333700cA9d93003F00f0F71f8515005F6c00Aa";
const ORDER_TYPE: &str = "Order(uint256 salt,address maker,address signer,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,uint256 timestamp,bytes32 metadata,bytes32 builder)";

/// What came back from one order.
#[derive(Clone, Debug, Default, serde::Serialize)]
pub struct Fill {
    /// Shares / contracts filled.
    pub filled: f64,
    /// Average price per share / contract (0..1), fees not included.
    pub avg: f64,
    pub order_id: String,
    pub status: String,
    pub raw: Value,
    /// When the order request left (our clock, ms) and how long until the venue answered.
    pub sent_ms: crate::model::Ms,
    pub latency_ms: i64,
    /// Time spent before sending (market facts lookup), ms.
    pub prep_ms: i64,
    /// Polymarket holds some orders (live sports) before matching: how long from its first reply
    /// ("delayed") until the order's final state. 0 = matched at once.
    pub match_delay_ms: i64,
    /// A sale: how long it waited for bought shares to settle into the wallet first.
    pub settle_wait_ms: i64,
    /// Polymarket: the fee rate the venue recorded on the trades (bps), when it says.
    pub fee_bps: Option<f64>,
}

fn keccak(b: &[u8]) -> [u8; 32] {
    Keccak256::digest(b).into()
}

fn hex_bytes(s: &str) -> Result<Vec<u8>> {
    let s = s.trim().trim_start_matches("0x");
    if s.len() % 2 != 0 {
        bail!("odd-length hex");
    }
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).context("bad hex")).collect()
}

fn to_hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn signing_key(pk: &str) -> Result<SigningKey> {
    let b = hex_bytes(pk).context("trading key is not hex")?;
    if b.len() != 32 {
        bail!("trading key must be 32 bytes (64 hex characters)");
    }
    SigningKey::from_slice(&b).context("not a valid secp256k1 key")
}

/// The Ethereum address of a private key, "0x…" (checksum not applied).
pub fn signer_address(pk: &str) -> Result<String> {
    let k = signing_key(pk)?;
    let pubk = k.verifying_key().to_encoded_point(false);
    let h = keccak(&pubk.as_bytes()[1..]);
    Ok(format!("0x{}", to_hex(&h[12..])))
}

fn word_u64(v: u64) -> [u8; 32] {
    let mut w = [0u8; 32];
    w[24..].copy_from_slice(&v.to_be_bytes());
    w
}

/// A decimal string (up to 78 digits, e.g. a token id) as a 256-bit big-endian word.
fn word_dec(s: &str) -> Result<[u8; 32]> {
    let mut w = [0u8; 32];
    for ch in s.trim().chars() {
        let d = ch.to_digit(10).context("token id is not decimal")? as u16;
        let mut carry = d;
        for b in w.iter_mut().rev() {
            let v = (*b as u16) * 10 + carry;
            *b = (v & 0xff) as u8;
            carry = v >> 8;
        }
        if carry != 0 {
            bail!("number does not fit 256 bits");
        }
    }
    Ok(w)
}

fn word_addr(a: &str) -> Result<[u8; 32]> {
    let b = hex_bytes(a)?;
    if b.len() != 20 {
        bail!("address must be 20 bytes: {a}");
    }
    let mut w = [0u8; 32];
    w[12..].copy_from_slice(&b);
    Ok(w)
}

fn domain_separator(name: &str, version: &str, chain: u64, contract: &str) -> Result<[u8; 32]> {
    let th = keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    let mut buf = Vec::with_capacity(32 * 5);
    buf.extend_from_slice(&th);
    buf.extend_from_slice(&keccak(name.as_bytes()));
    buf.extend_from_slice(&keccak(version.as_bytes()));
    buf.extend_from_slice(&word_u64(chain));
    buf.extend_from_slice(&word_addr(contract)?);
    Ok(keccak(&buf))
}

/// A Polymarket order, amounts in 6-decimal units.
#[derive(Clone, Debug)]
pub struct PmOrder {
    pub salt: u64,
    pub maker: String,
    pub signer: String,
    pub token_id: String,
    pub maker_amount: u64,
    pub taker_amount: u64,
    /// 0 = BUY, 1 = SELL
    pub side: u8,
    /// 0 EOA, 1 proxy, 2 Safe, 3 deposit wallet
    pub signature_type: u8,
    /// Unix ms.
    pub timestamp: u64,
}

/// (domain version, exchange) for a market.
fn exchange(neg_risk: bool, v2: bool) -> (&'static str, &'static str) {
    if v2 {
        ("3", V2_EXCHANGE)
    } else if neg_risk {
        ("2", NEG_RISK_EXCHANGE)
    } else {
        ("2", EXCHANGE)
    }
}

fn order_struct_hash(o: &PmOrder) -> Result<[u8; 32]> {
    let mut buf = Vec::with_capacity(32 * 12);
    buf.extend_from_slice(&keccak(ORDER_TYPE.as_bytes()));
    buf.extend_from_slice(&word_u64(o.salt));
    buf.extend_from_slice(&word_addr(&o.maker)?);
    buf.extend_from_slice(&word_addr(&o.signer)?);
    buf.extend_from_slice(&word_dec(&o.token_id)?);
    buf.extend_from_slice(&word_u64(o.maker_amount));
    buf.extend_from_slice(&word_u64(o.taker_amount));
    buf.extend_from_slice(&word_u64(o.side as u64));
    buf.extend_from_slice(&word_u64(o.signature_type as u64));
    buf.extend_from_slice(&word_u64(o.timestamp));
    buf.extend_from_slice(&[0u8; 32]); // metadata
    buf.extend_from_slice(&[0u8; 32]); // builder
    Ok(keccak(&buf))
}

/// The exchange domain separator for a market.
fn app_domain(neg_risk: bool, v2: bool) -> Result<[u8; 32]> {
    let (ver, contract) = exchange(neg_risk, v2);
    domain_separator("Polymarket CTF Exchange", ver, CHAIN_ID, contract)
}

/// Types 0-2: the Order signed directly under the exchange domain.
fn order_digest(o: &PmOrder, neg_risk: bool, v2: bool) -> Result<[u8; 32]> {
    let mut m = vec![0x19, 0x01];
    m.extend_from_slice(&app_domain(neg_risk, v2)?);
    m.extend_from_slice(&order_struct_hash(o)?);
    Ok(keccak(&m))
}

/// Type 3 (deposit wallet), ERC-7739: the Order is the `contents` of a TypedDataSign message
/// whose own fields are the deposit wallet's domain (name "DepositWallet", version "1", chain 137,
/// verifyingContract = the wallet, salt 0), signed under the exchange domain. The signature sent is
/// inner signature ++ exchange domain separator ++ Order hash ++ Order type string ++ its length
/// (2 bytes).
fn deposit_wallet_signature(pk: &str, o: &PmOrder, neg_risk: bool, v2: bool) -> Result<String> {
    let app = app_domain(neg_risk, v2)?;
    let contents = order_struct_hash(o)?;
    let tds_type = format!("TypedDataSign(Order contents,string name,string version,uint256 chainId,address verifyingContract,bytes32 salt){ORDER_TYPE}");
    let mut t = Vec::with_capacity(32 * 7);
    t.extend_from_slice(&keccak(tds_type.as_bytes()));
    t.extend_from_slice(&contents);
    t.extend_from_slice(&keccak(b"DepositWallet"));
    t.extend_from_slice(&keccak(b"1"));
    t.extend_from_slice(&word_u64(CHAIN_ID));
    t.extend_from_slice(&word_addr(&o.maker)?);
    t.extend_from_slice(&[0u8; 32]);
    let mut m = vec![0x19, 0x01];
    m.extend_from_slice(&app);
    m.extend_from_slice(&keccak(&t));
    let inner = hex_bytes(&sign_digest(pk, &keccak(&m))?)?;
    let mut out = inner;
    out.extend_from_slice(&app);
    out.extend_from_slice(&contents);
    out.extend_from_slice(ORDER_TYPE.as_bytes());
    out.extend_from_slice(&(ORDER_TYPE.len() as u16).to_be_bytes());
    Ok(format!("0x{}", to_hex(&out)))
}

fn sign_digest(pk: &str, d: &[u8; 32]) -> Result<String> {
    let k = signing_key(pk)?;
    let (sig, rec) = k.sign_prehash_recoverable(d).context("signing failed")?;
    let mut out = sig.to_bytes().to_vec();
    out.push(27 + rec.to_byte());
    Ok(format!("0x{}", to_hex(&out)))
}

/// Per-token trading facts from the CLOB, cached 10 minutes (they rarely change and each lookup
/// costs a round trip before the order).
#[derive(Clone, Copy, Debug, serde::Serialize)]
pub struct Facts {
    pub fee_bps: u64,
    pub tick: f64,
    pub neg_risk: bool,
    /// Smallest order in shares.
    pub min_size: f64,
    /// A Protocol V2 market (signed with the V2 exchange domain).
    pub v2: bool,
}

static FACTS: std::sync::LazyLock<parking_lot::Mutex<std::collections::HashMap<String, (crate::model::Ms, Facts)>>> = std::sync::LazyLock::new(Default::default);

pub async fn token_facts(http: &reqwest::Client, token: &str) -> Result<Facts> {
    let now = crate::model::now_ms();
    if let Some((t, f)) = FACTS.lock().get(token) {
        if now - t < 600_000 {
            return Ok(*f);
        }
    }
    let get = |path: String| async move {
        let r = http.get(format!("{CLOB}{path}")).send().await?;
        let s = r.status();
        let t = r.text().await?;
        if !s.is_success() {
            bail!("{path}: HTTP {s}: {}", t.chars().take(200).collect::<String>());
        }
        Ok::<Value, anyhow::Error>(serde_json::from_str(&t).unwrap_or(Value::Null))
    };
    let (fee, book, neg) = tokio::join!(get(format!("/fee-rate?token_id={token}")), get(format!("/book?token_id={token}")), get(format!("/neg-risk?token_id={token}")));
    let book = book?;
    let f = Facts {
        fee_bps: fee.ok().and_then(|v| v["base_fee"].as_u64().or_else(|| v["fee_rate_bps"].as_u64())).unwrap_or(0),
        tick: crate::books::num(&book["tick_size"]).unwrap_or(0.01),
        neg_risk: neg.ok().and_then(|v| v["neg_risk"].as_bool()).or_else(|| book["neg_risk"].as_bool()).unwrap_or(false),
        min_size: crate::books::num(&book["min_order_size"]).unwrap_or(5.0),
        v2: book["version"].as_str().is_some_and(|v| v.eq_ignore_ascii_case("v2")),
    };
    FACTS.lock().insert(token.to_string(), (now, f));
    Ok(f)
}

/// Shares of a token the account can sell now (CLOB balance, L2 auth).
pub async fn pm_shares(http: &reqwest::Client, c: &PmCreds, token: &str) -> Result<f64> {
    let path = "/balance-allowance";
    let mut req = http.get(format!("{CLOB}{path}?asset_type=CONDITIONAL&token_id={token}&signature_type={}", c.signature_type));
    for (k, v) in crate::pmacct::l2_headers(c, "GET", path, "")? {
        req = req.header(k, v);
    }
    let v: Value = req.send().await?.json().await?;
    Ok(crate::books::num(&v["balance"]).unwrap_or(0.0) / 1e6)
}

/// Buy or sell `shares` (whole) of a Polymarket token at a limit `price`, fill-and-kill (or
/// fill-or-kill when `all_or_none`). Returns what filled. A sale right after a buy waits (up to
/// 30 s) for the bought shares to settle into the wallet: Polymarket refuses to sell them before.
pub async fn pm_order(http: &reqwest::Client, c: &PmCreds, token: &str, buy: bool, price: f64, shares: f64, all_or_none: bool) -> Result<Fill> {
    let mut waited = 0;
    if !buy {
        let t0 = crate::model::now_ms();
        let mut tries = 0;
        loop {
            let have = pm_shares(http, c, token).await.unwrap_or(0.0);
            if have + 1e-6 >= shares.floor() || crate::model::now_ms() - t0 > 30_000 {
                break;
            }
            tries += 1;
            tokio::time::sleep(std::time::Duration::from_millis(if tries < 10 { 500 } else { 1_000 })).await;
        }
        waited = crate::model::now_ms() - t0;
    }
    let mut f = pm_order_once(http, c, token, buy, price, shares, all_or_none).await?;
    f.settle_wait_ms = waited;
    Ok(f)
}

async fn pm_order_once(http: &reqwest::Client, c: &PmCreds, token: &str, buy: bool, price: f64, shares: f64, all_or_none: bool) -> Result<Fill> {
    let signer = signer_address(&c.private_key).context("Polymarket trading key")?;
    if c.api_key.trim().is_empty() || c.api_secret.trim().is_empty() || c.passphrase.trim().is_empty() {
        bail!("Polymarket API key / secret / passphrase missing (Settings)");
    }
    let wallet = if c.wallet.trim().is_empty() { signer.clone() } else { c.wallet.trim().to_lowercase() };
    // Who makes the order: the EOA (0), its proxy / Safe with the EOA as signer (1, 2), or the
    // deposit wallet as both (3).
    let sig_type = if wallet == signer { 0 } else { c.signature_type };
    let (maker, order_signer) = match sig_type {
        0 => (signer.clone(), signer.clone()),
        3 => (wallet.clone(), wallet.clone()),
        _ => (wallet.clone(), signer.clone()),
    };
    let t_prep = crate::model::now_ms();
    let Facts { tick, neg_risk: neg, v2, .. } = token_facts(http, token).await?;
    let prep_ms = crate::model::now_ms() - t_prep;
    let px = if buy { (price / tick).floor() * tick } else { (price / tick).ceil() * tick };
    let px = (px * 100.0).round() / 100.0; // cent prices: amounts keep 2 decimals
    // Buys: whole shares. Sales: hundredths of a share, so everything bought can be sold (a buy
    // filled under its limit returns a fraction more shares than ordered).
    let n = if buy { shares.floor() } else { (shares * 100.0 + 1e-6).floor() / 100.0 };
    if n < 1.0 || !(0.01..=0.99).contains(&px) {
        bail!("order too small or price out of range ({n} @ {px})");
    }
    // Dollars: cents on a buy; a sale of hundredths at a cent price needs 4 decimals.
    let usdc = if buy { ((n * px) * 100.0).round() / 100.0 } else { ((n * px) * 1e4).round() / 1e4 };
    let (maker_amount, taker_amount) = if buy { ((usdc * 1e6).round() as u64, (n * 1e6).round() as u64) } else { ((n * 1e6).round() as u64, (usdc * 1e6).round() as u64) };
    let o = PmOrder {
        salt: rand::random::<u32>() as u64,
        maker,
        signer: order_signer,
        token_id: token.to_string(),
        maker_amount,
        taker_amount,
        side: if buy { 0 } else { 1 },
        signature_type: sig_type,
        timestamp: crate::model::now_ms() as u64,
    };
    let sig = if sig_type == 3 { deposit_wallet_signature(&c.private_key, &o, neg, v2)? } else { sign_digest(&c.private_key, &order_digest(&o, neg, v2)?)? };
    let zero32 = format!("0x{}", "0".repeat(64));
    let body = json!({
        "deferExec": false,
        "order": {
            "builder": zero32, "expiration": "0", "maker": o.maker, "makerAmount": o.maker_amount.to_string(),
            "metadata": zero32, "salt": o.salt, "side": if buy { "BUY" } else { "SELL" }, "signature": sig,
            "signatureType": o.signature_type, "signer": o.signer, "takerAmount": o.taker_amount.to_string(),
            "timestamp": o.timestamp.to_string(), "tokenId": o.token_id,
        },
        "orderType": if all_or_none { "FOK" } else { "FAK" },
        "owner": c.api_key.trim(),
        "postOnly": false,
    })
    .to_string();
    let mut req = http.post(format!("{CLOB}/order")).header("Content-Type", "application/json").body(body.clone());
    for (k, v) in crate::pmacct::l2_headers(c, "POST", "/order", &body)? {
        req = req.header(k, v);
    }
    let sent_ms = crate::model::now_ms();
    let r = req.send().await?;
    let status = r.status();
    let text = r.text().await?;
    let latency_ms = crate::model::now_ms() - sent_ms;
    let v: Value = serde_json::from_str(&text).unwrap_or(json!({"text": text}));
    if !status.is_success() || v["success"].as_bool() == Some(false) {
        bail!("Polymarket order refused (HTTP {status}): {}", v["errorMsg"].as_str().or(v["error"].as_str()).unwrap_or(&text.chars().take(200).collect::<String>()));
    }
    // Buy: making = USDC paid, taking = shares got. Sell: making = shares sold, taking = USDC got.
    let making = crate::books::num(&v["makingAmount"]).unwrap_or(0.0);
    let taking = crate::books::num(&v["takingAmount"]).unwrap_or(0.0);
    let (mut filled, usd) = if buy { (taking, making) } else { (making, taking) };
    let mut avg = if filled > 0.0 { usd / filled } else { 0.0 };
    let mut status: String = v["status"].as_str().unwrap_or("").into();
    let id: String = v["orderID"].as_str().unwrap_or("").into();
    let mut match_delay_ms = 0;
    let mut fee_seen: Option<f64> = None;
    // Live sports: Polymarket answers "delayed" and matches a few seconds later. Wait for the
    // order's final state (up to 15 s) and take the fill from it.
    if status.eq_ignore_ascii_case("delayed") && !id.is_empty() {
        let t0 = crate::model::now_ms();
        let path = format!("/data/order/{id}");
        loop {
            tokio::time::sleep(std::time::Duration::from_millis(400)).await;
            let mut req = http.get(format!("{CLOB}{path}"));
            for (k, h) in crate::pmacct::l2_headers(c, "GET", &path, "")? {
                req = req.header(k, h);
            }
            let o: Value = match req.send().await {
                Ok(r) => r.json().await.unwrap_or(Value::Null),
                Err(_) => Value::Null,
            };
            let st = o["status"].as_str().unwrap_or("").to_uppercase();
            let done = matches!(st.as_str(), "MATCHED" | "CANCELED" | "CANCELLED" | "UNMATCHED" | "INVALID");
            if done || crate::model::now_ms() - t0 > 15_000 {
                filled = crate::books::num(&o["size_matched"]).unwrap_or(filled);
                if filled > 0.0 && avg == 0.0 {
                    // The order record only has the limit price: take the real execution price
                    // from its trades (each maker's price, flipped when the maker traded the
                    // complementary token).
                    let (mut n, mut usd) = (0.0, 0.0);
                    for tid in o["associate_trades"].as_array().into_iter().flatten().filter_map(|x| x.as_str()) {
                        let tpath = "/data/trades";
                        let mut req = http.get(format!("{CLOB}{tpath}?id={tid}"));
                        for (k, h) in crate::pmacct::l2_headers(c, "GET", tpath, "")? {
                            req = req.header(k, h);
                        }
                        let Ok(r) = req.send().await else { continue };
                        let tv: Value = r.json().await.unwrap_or(Value::Null);
                        let t = tv["data"].as_array().and_then(|a| a.first().cloned()).unwrap_or(tv);
                        if fee_seen.is_none() {
                            fee_seen = crate::books::num(&t["fee_rate_bps"]);
                        }
                        for m in t["maker_orders"].as_array().into_iter().flatten() {
                            let (Some(q), Some(p)) = (crate::books::num(&m["matched_amount"]), crate::books::num(&m["price"])) else { continue };
                            let p = if m["asset_id"].as_str() == Some(token) { p } else { 1.0 - p };
                            n += q;
                            usd += q * p;
                        }
                    }
                    avg = if n > 0.0 { usd / n } else { crate::books::num(&o["price"]).unwrap_or(px) };
                }
                status = if done { st.to_lowercase() } else { format!("still {} after 15 s", if st.is_empty() { "unknown" } else { &st }) };
                match_delay_ms = crate::model::now_ms() - t0;
                break;
            }
        }
    }
    Ok(Fill { filled, avg, order_id: id, status, raw: v, sent_ms, latency_ms, prep_ms, match_delay_ms, settle_wait_ms: 0, fee_bps: fee_seen })
}

/// Buy or sell `count` Kalshi contracts on `ticker`, side "yes"/"no", at a limit price (0..1)
/// for that side; fill-or-kill when `all_or_none`, else immediate-or-cancel.
pub async fn kalshi_order(http: &reqwest::Client, c: &KalshiCreds, ticker: &str, buy: bool, yes_side: bool, price: f64, count: f64, all_or_none: bool) -> Result<Fill> {
    if !crate::kalacct::has_key(c) {
        bail!("Kalshi API key missing (Kalshi page)");
    }
    let cents = (price * 100.0).round().clamp(1.0, 99.0) as i64;
    let n = count.floor() as i64;
    if n < 1 {
        bail!("order too small");
    }
    let path = "/trade-api/v2/portfolio/orders";
    let mut body = json!({
        "ticker": ticker, "action": if buy { "buy" } else { "sell" }, "side": if yes_side { "yes" } else { "no" },
        "count": n, "type": "limit", "time_in_force": if all_or_none { "fill_or_kill" } else { "immediate_or_cancel" },
        "client_order_id": format!("swifteye-{}-{}", crate::clock::now_ms(), rand::random::<u32>()),
    });
    body[if yes_side { "yes_price" } else { "no_price" }] = json!(cents);
    let body = body.to_string();
    let mut req = http.post(format!("{KALSHI}{path}")).header("Content-Type", "application/json").body(body);
    for (k, v) in crate::kalacct::headers(c, "POST", path)? {
        req = req.header(k, v);
    }
    let sent_ms = crate::model::now_ms();
    let r = req.send().await?;
    let status = r.status();
    let text = r.text().await?;
    let latency_ms = crate::model::now_ms() - sent_ms;
    let v: Value = serde_json::from_str(&text).unwrap_or(json!({"text": text}));
    if !status.is_success() {
        bail!("Kalshi order refused (HTTP {status}): {}", text.chars().take(300).collect::<String>());
    }
    let o = &v["order"];
    let filled = crate::books::num(&o["fill_count_fp"]).or_else(|| crate::books::num(&o["fill_count"])).unwrap_or_else(|| {
        // Older shape: count - remaining.
        crate::books::num(&o["count"]).unwrap_or(0.0) - crate::books::num(&o["remaining_count"]).unwrap_or(0.0)
    });
    let paid = crate::books::num(&o["taker_fill_cost_dollars"]).or_else(|| crate::books::num(&o["taker_fill_cost"]).map(|c| c / 100.0));
    let avg = match paid {
        Some(p) if filled > 0.0 => p / filled,
        _ => cents as f64 / 100.0,
    };
    Ok(Fill { filled: filled.max(0.0), avg, order_id: o["order_id"].as_str().unwrap_or("").into(), status: o["status"].as_str().unwrap_or("").into(), raw: v, sent_ms, latency_ms, prep_ms: 0, match_delay_ms: 0, settle_wait_ms: 0, fee_bps: None })
}

// ---------------------------------------------------------------- API credentials from the key

/// Polymarket's L1 proof: an EIP-712 "ClobAuth" message signed by the trading key.
fn clob_auth_headers(pk: &str, ts: &str) -> Result<Vec<(&'static str, String)>> {
    let addr = signer_address(pk)?;
    let dt = keccak(b"EIP712Domain(string name,string version,uint256 chainId)");
    let mut d = Vec::new();
    d.extend_from_slice(&dt);
    d.extend_from_slice(&keccak(b"ClobAuthDomain"));
    d.extend_from_slice(&keccak(b"1"));
    d.extend_from_slice(&word_u64(CHAIN_ID));
    let ds = keccak(&d);
    let th = keccak(b"ClobAuth(address address,string timestamp,uint256 nonce,string message)");
    let mut m = Vec::new();
    m.extend_from_slice(&th);
    m.extend_from_slice(&word_addr(&addr)?);
    m.extend_from_slice(&keccak(ts.as_bytes()));
    m.extend_from_slice(&word_u64(0));
    m.extend_from_slice(&keccak(b"This message attests that I control the given wallet"));
    let sh = keccak(&m);
    let mut x = vec![0x19, 0x01];
    x.extend_from_slice(&ds);
    x.extend_from_slice(&sh);
    let sig = sign_digest(pk, &keccak(&x))?;
    Ok(vec![("POLY_ADDRESS", addr), ("POLY_SIGNATURE", sig), ("POLY_TIMESTAMP", ts.to_string()), ("POLY_NONCE", "0".to_string())])
}

/// The API key, secret and passphrase belonging to the trading key: the existing ones if the key
/// already has them (derive), else new ones (create). Returns (key, secret, passphrase).
pub async fn pm_api_from_key(http: &reqwest::Client, pk: &str) -> Result<(String, String, String)> {
    let parse = |v: &Value| -> Option<(String, String, String)> {
        Some((v["apiKey"].as_str()?.to_string(), v["secret"].as_str()?.to_string(), v["passphrase"].as_str()?.to_string()))
    };
    let ts = (crate::model::now_ms() / 1000).to_string();
    let mut req = http.get(format!("{CLOB}/auth/derive-api-key"));
    for (k, v) in clob_auth_headers(pk, &ts)? {
        req = req.header(k, v);
    }
    let r = req.send().await?;
    if r.status().is_success() {
        if let Some(x) = parse(&r.json::<Value>().await?) {
            return Ok(x);
        }
    }
    let ts = (crate::model::now_ms() / 1000).to_string();
    let mut req = http.post(format!("{CLOB}/auth/api-key"));
    for (k, v) in clob_auth_headers(pk, &ts)? {
        req = req.header(k, v);
    }
    let r = req.send().await?;
    let st = r.status();
    let t = r.text().await?;
    if !st.is_success() {
        bail!("Polymarket refused (HTTP {st}): {}", t.chars().take(200).collect::<String>());
    }
    parse(&serde_json::from_str(&t)?).context("no credentials in Polymarket's reply")
}

// ---------------------------------------------------------------- one account, two bots

/// Free cash on one venue account as this process knows it: the last balance read, minus what was
/// bought since (the balance is re-read when older than 5 s).
pub struct Wallet {
    pub cash: f64,
    pub at: crate::model::Ms,
    pub spent: f64,
}

static PM_WALLET: std::sync::LazyLock<tokio::sync::Mutex<Wallet>> = std::sync::LazyLock::new(|| tokio::sync::Mutex::new(Wallet { cash: 0.0, at: 0, spent: 0.0 }));
static K_WALLET: std::sync::LazyLock<tokio::sync::Mutex<Wallet>> = std::sync::LazyLock::new(|| tokio::sync::Mutex::new(Wallet { cash: 0.0, at: 0, spent: 0.0 }));

/// USDC free on the Polymarket account (CLOB balance-allowance, L2 auth).
pub async fn pm_cash(http: &reqwest::Client, c: &PmCreds) -> Result<f64> {
    let path = "/balance-allowance";
    let mut req = http.get(format!("{CLOB}{path}?asset_type=COLLATERAL&signature_type={}", c.signature_type));
    for (k, v) in crate::pmacct::l2_headers(c, "GET", path, "")? {
        req = req.header(k, v);
    }
    let r = req.send().await?;
    let st = r.status();
    let t = r.text().await?;
    if !st.is_success() {
        bail!("Polymarket balance: HTTP {st}: {}", t.chars().take(200).collect::<String>());
    }
    let v: Value = serde_json::from_str(&t)?;
    Ok(crate::books::num(&v["balance"]).context("no balance in the reply")? / 1e6)
}

/// Dollars free on the Kalshi account.
pub async fn kalshi_cash(http: &reqwest::Client, c: &KalshiCreds) -> Result<f64> {
    let path = "/trade-api/v2/portfolio/balance";
    let mut req = http.get(format!("{KALSHI}{path}"));
    for (k, v) in crate::kalacct::headers(c, "GET", path)? {
        req = req.header(k, v);
    }
    let r = req.send().await?;
    let st = r.status();
    let t = r.text().await?;
    if !st.is_success() {
        bail!("Kalshi balance: HTTP {st}: {}", t.chars().take(200).collect::<String>());
    }
    let v: Value = serde_json::from_str(&t)?;
    crate::books::num(&v["balance_dollars"]).or_else(|| crate::books::num(&v["balance"]).map(|c| c / 100.0)).context("no balance in the reply")
}

/// Both bots trade the same accounts. Every real BUY goes through this: one order at a time per
/// venue (across both bots), and only when the account's free cash, after the venue reserve and
/// what was just bought, covers `amount`. Hold the returned guard while the order is sent, then
/// add what it cost to `guard.spent`.
pub async fn claim(venue: crate::venue::V, db: &std::path::Path, http: &reqwest::Client, amount: f64) -> Result<tokio::sync::MutexGuard<'static, Wallet>> {
    let s = crate::bots::settings();
    let (lock, reserve) = match venue {
        crate::venue::V::Pm => (&*PM_WALLET, s.pm_reserve),
        crate::venue::V::Kalshi => (&*K_WALLET, s.kalshi_reserve),
    };
    let mut w = lock.lock().await;
    let now = crate::model::now_ms();
    if now - w.at > 5_000 {
        w.cash = match venue {
            crate::venue::V::Pm => pm_cash(http, &crate::pmacct::load(db)).await?,
            crate::venue::V::Kalshi => kalshi_cash(http, &crate::kalacct::load(db)).await?,
        };
        w.at = now;
        w.spent = 0.0;
    }
    let free = w.cash - w.spent - reserve;
    if free < amount {
        bail!("{:?} account: ${free:.2} free after the ${reserve:.0} reserve, ${amount:.2} needed", venue);
    }
    Ok(w)
}

/// Positions the accounts actually hold: Polymarket token -> shares, Kalshi ticker -> contracts
/// (+ Yes, - No).
pub async fn holdings(http: &reqwest::Client, pm: &PmCreds, k: &KalshiCreds) -> (Result<std::collections::HashMap<String, f64>>, Result<std::collections::HashMap<String, f64>>) {
    let pmh = async {
        let w = pm.wallet.trim().to_lowercase();
        if w.is_empty() {
            bail!("no Polymarket wallet saved");
        }
        let v: Value = http.get(format!("https://data-api.polymarket.com/v2/positions?user={w}&limit=500&sizeThreshold=0.01")).send().await?.json().await?;
        let rows = v.as_array().cloned().or_else(|| v["positions"].as_array().cloned()).unwrap_or_default();
        Ok(rows.iter().filter_map(|r| Some((r["asset"].as_str()?.to_string(), crate::books::num(&r["size"])?))).collect())
    };
    let kh = async {
        if !crate::kalacct::has_key(k) {
            bail!("no Kalshi key saved");
        }
        let path = "/trade-api/v2/portfolio/positions";
        let mut req = http.get(format!("{KALSHI}{path}?limit=1000&count_filter=position"));
        for (a, b) in crate::kalacct::headers(k, "GET", path)? {
            req = req.header(a, b);
        }
        let v: Value = req.send().await?.json().await?;
        Ok(v["market_positions"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|r| Some((r["ticker"].as_str()?.to_string(), crate::books::num(&r["position_fp"]).or_else(|| crate::books::num(&r["position"]))?)))
            .filter(|x| x.1 != 0.0)
            .collect())
    };
    tokio::join!(pmh, kh)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keccak_and_address_known_vectors() {
        assert_eq!(to_hex(&keccak(b"")), "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470");
        // Private key 1 -> the well-known address 0x7E5F…5Bdf.
        let pk = format!("{:064x}", 1);
        assert_eq!(signer_address(&pk).unwrap(), "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf");
    }

    #[test]
    fn eip712_domain_matches_the_spec_example() {
        // EIP-712 "Ether Mail" example domain separator.
        let d = domain_separator("Ether Mail", "1", 1, "0xCcCCccccCCCCcCCCCCCcCcCccCcCCCcCcccccccC").unwrap();
        assert_eq!(to_hex(&d), "f2cee375fa42b42143804025fc449deafd50cc031ca257e0b194a650a912090f");
    }

    #[test]
    fn deposit_wallet_signature_layout() {
        let pk = format!("{:064x}", 11);
        let w = "0x277892b3097439c4d0779163bc143455762f97c7";
        let o = PmOrder { salt: 5, maker: w.into(), signer: w.into(), token_id: "42".into(), maker_amount: 1_000_000, taker_amount: 2_000_000, side: 0, signature_type: 3, timestamp: 1_795_000_000_000 };
        let s = hex_bytes(&deposit_wallet_signature(&pk, &o, false, false).unwrap()).unwrap();
        // 65 inner + 32 app domain + 32 contents + type string + 2-byte length.
        assert_eq!(s.len(), 65 + 32 + 32 + ORDER_TYPE.len() + 2);
        assert_eq!(&s[65..97], &app_domain(false, false).unwrap());
        assert_eq!(&s[97..129], &order_struct_hash(&o).unwrap());
        let n = s.len();
        assert_eq!(u16::from_be_bytes([s[n - 2], s[n - 1]]) as usize, ORDER_TYPE.len());
    }

    #[test]
    fn clob_auth_signature_recovers_to_the_signer() {
        let pk = format!("{:064x}", 9);
        let h = clob_auth_headers(&pk, "1700000000").unwrap();
        assert_eq!(h[0].1, signer_address(&pk).unwrap());
        assert_eq!(hex_bytes(&h[1].1).unwrap().len(), 65);
    }

    #[test]
    fn big_decimal_words() {
        assert_eq!(word_dec("255").unwrap()[31], 255);
        assert_eq!(word_dec("256").unwrap()[30..], [1, 0]);
        let w = word_dec("29498536440894343989369031289293084556573417799065103660169559734904616858853").unwrap();
        assert_ne!(w, [0u8; 32]);
    }

    #[test]
    fn signatures_recover_to_the_signer() {
        use k256::ecdsa::{RecoveryId, Signature, VerifyingKey};
        let pk = format!("{:064x}", 7);
        let o = PmOrder { salt: 1, maker: signer_address(&pk).unwrap(), signer: signer_address(&pk).unwrap(), token_id: "123".into(), maker_amount: 5_000_000, taker_amount: 10_000_000, side: 0, signature_type: 0, timestamp: 1_795_000_000_000 };
        let d = order_digest(&o, false, false).unwrap();
        let s = hex_bytes(&sign_digest(&pk, &d).unwrap()).unwrap();
        assert_eq!(s.len(), 65);
        let sig = Signature::from_slice(&s[..64]).unwrap();
        let vk = VerifyingKey::recover_from_prehash(&d, &sig, RecoveryId::from_byte(s[64] - 27).unwrap()).unwrap();
        assert_eq!(vk, *signing_key(&pk).unwrap().verifying_key());
    }
}
