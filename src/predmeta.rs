//! Size of each prediction-market listing (liquidity, volume), taken from the sources' own
//! discovery calls. Drives the "high tier vs low tier" filters on the Matches page.

use parking_lot::Mutex;
use serde::Serialize;
use std::collections::HashMap;
use std::sync::LazyLock;

/// Dollar amounts. Kalshi counts contracts; its volume is turned into dollars at the last price.
#[derive(Clone, Copy, Default, Serialize, Debug)]
pub struct Size {
    pub liquidity: f64,
    pub volume: f64,
    pub volume_24h: f64,
}

static SIZES: LazyLock<Mutex<HashMap<(String, String), Size>>> = LazyLock::new(Default::default);

pub fn set(book: &str, event: &str, s: Size) {
    SIZES.lock().insert((book.to_string(), event.to_string()), s);
}

pub fn get(book: &str, event: &str) -> Option<Size> {
    SIZES.lock().get(&(book.to_string(), event.to_string())).copied()
}
