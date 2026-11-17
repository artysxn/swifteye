//! Wall-clock correction. Every timestamp the service records is the local clock
//! plus an offset measured against public NTP servers (SNTP, RFC 4330), so lags
//! between sources are not polluted by a PC clock that runs fast or slow.

use std::sync::atomic::{AtomicI64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::net::UdpSocket;

static OFFSET_MS: AtomicI64 = AtomicI64::new(0);
static LAST_SYNC_MS: AtomicI64 = AtomicI64::new(0);
static LAST_RTT_MS: AtomicI64 = AtomicI64::new(-1);

const SERVERS: &[&str] = &["time.cloudflare.com:123", "time.google.com:123", "pool.ntp.org:123"];
const NTP_UNIX_DELTA: u64 = 2_208_988_800;

pub fn local_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// Corrected wall time in ms since epoch.
pub fn now_ms() -> i64 {
    local_ms() + OFFSET_MS.load(Ordering::Relaxed)
}

pub fn status() -> serde_json::Value {
    serde_json::json!({
        "offset_ms": OFFSET_MS.load(Ordering::Relaxed),
        "last_sync_ms": LAST_SYNC_MS.load(Ordering::Relaxed),
        "rtt_ms": LAST_RTT_MS.load(Ordering::Relaxed),
        "note": "offset = true time - this PC's clock; all recorded timestamps include it"
    })
}

fn to_ms(b: &[u8]) -> i64 {
    let secs = u32::from_be_bytes([b[0], b[1], b[2], b[3]]) as u64;
    let frac = u32::from_be_bytes([b[4], b[5], b[6], b[7]]) as u64;
    ((secs - NTP_UNIX_DELTA) * 1000 + (frac * 1000 >> 32)) as i64
}

/// One SNTP exchange -> (offset ms, round trip ms).
async fn sample(server: &str) -> anyhow::Result<(i64, i64)> {
    let sock = UdpSocket::bind("0.0.0.0:0").await?;
    sock.connect(server).await?;
    let mut pkt = [0u8; 48];
    pkt[0] = 0x23; // LI 0, version 4, mode 3 (client)
    let t0 = local_ms();
    sock.send(&pkt).await?;
    let mut buf = [0u8; 48];
    let n = tokio::time::timeout(Duration::from_secs(2), sock.recv(&mut buf)).await??;
    let t3 = local_ms();
    anyhow::ensure!(n >= 48 && (buf[0] & 0x07) == 4, "bad NTP reply");
    let t1 = to_ms(&buf[32..40]); // server receive
    let t2 = to_ms(&buf[40..48]); // server transmit
    Ok((((t1 - t0) + (t2 - t3)) / 2, (t3 - t0) - (t2 - t1)))
}

/// Take several samples and keep the one with the shortest round trip.
pub async fn sync() -> anyhow::Result<i64> {
    let mut best: Option<(i64, i64)> = None;
    for server in SERVERS {
        for _ in 0..3 {
            if let Ok((off, rtt)) = sample(server).await {
                if best.map_or(true, |(_, r)| rtt < r) {
                    best = Some((off, rtt));
                }
            }
        }
        if best.is_some_and(|(_, r)| r < 80) {
            break;
        }
    }
    let (off, rtt) = best.ok_or_else(|| anyhow::anyhow!("no NTP server answered"))?;
    OFFSET_MS.store(off, Ordering::Relaxed);
    LAST_RTT_MS.store(rtt, Ordering::Relaxed);
    LAST_SYNC_MS.store(now_ms(), Ordering::Relaxed);
    Ok(off)
}

pub fn spawn_resync() {
    tokio::spawn(async {
        loop {
            tokio::time::sleep(Duration::from_secs(600)).await;
            match sync().await {
                Ok(off) => tracing::debug!("clock offset {off} ms"),
                Err(e) => tracing::warn!("clock resync failed: {e}"),
            }
        }
    });
}
