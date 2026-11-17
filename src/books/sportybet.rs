//! SportyBet: factsCenter live list. Sportradar-fed, so events carry sr:match ids
//! and markets use Sportradar market/outcome ids.

use super::*;

pub struct SportyBet {
    /// Country site, e.g. "ng".
    pub country: String,
}

impl Book for SportyBet {
    fn name(&self) -> &'static str {
        "sportybet"
    }
    fn source(&self) -> String {
        format!("sportybet.com/api/{}/factsCenter/liveOrPrematchEvents", self.country)
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let url = format!(
            "https://www.sportybet.com/api/{}/factsCenter/liveOrPrematchEvents?sportId=sr%3Asport%3A1&marketId=1%2C18%2C16%2C29&_t={}",
            self.country,
            now_ms()
        );
        let (v, recv_ms) = fetch_json(http.get(url)).await?;
        if v["bizCode"].as_i64() != Some(10000) {
            bail!("bizCode {}: {}", v["bizCode"], v["message"]);
        }
        let mut events = Vec::new();
        for t in v["data"].as_array().into_iter().flatten() {
            for e in t["events"].as_array().into_iter().flatten() {
                if e["status"].as_i64() != Some(1) {
                    continue;
                }
                let sid = text(&e["eventId"]);
                let cat = &e["sport"]["category"];
                let mut ev = EventIn {
                    sr_id: sid.strip_prefix("sr:match:").map(str::to_string),
                    id: sid,
                    sport: "football".into(),
                    league: join_league(&[cat["name"].as_str().unwrap_or(""), cat["tournament"]["name"].as_str().unwrap_or("")]),
                    home: text(&e["homeTeamName"]),
                    away: text(&e["awayTeamName"]),
                    start_ms: int(&e["estimateStartTime"]),
                    score: e["setScore"].as_str().map(|s| s.replace(':', "-")),
                    clock: e["playedSeconds"].as_str().map(str::to_string),
                    quotes: Vec::new(),
                    sr_weak: false,
                };
                for m in e["markets"].as_array().into_iter().flatten() {
                    let mid = text(&m["id"]);
                    let spec = m["specifier"].as_str().unwrap_or("");
                    let spec_val = |key: &str| -> Option<f64> {
                        spec.split('|').find_map(|kv| kv.strip_prefix(key)?.strip_prefix('=')?.parse().ok())
                    };
                    let (market, line) = match mid.as_str() {
                        "1" => (Market::X12, None),
                        "18" => match spec_val("total") {
                            Some(l) => (Market::Total, Some(l)),
                            None => continue,
                        },
                        "16" => match spec_val("hcp") {
                            Some(l) => (Market::Handicap, Some(l)),
                            None => continue,
                        },
                        "29" => (Market::Btts, None),
                        _ => continue,
                    };
                    let market_open = m["status"].as_i64() == Some(0);
                    let ts = int(&m["lastOddsChangeTime"]);
                    for o in m["outcomes"].as_array().into_iter().flatten() {
                        let outcome = match (mid.as_str(), text(&o["id"]).as_str()) {
                            ("1", "1") => Outcome::Home,
                            ("1", "2") => Outcome::Draw,
                            ("1", "3") => Outcome::Away,
                            ("18", "12") => Outcome::Over,
                            ("18", "13") => Outcome::Under,
                            ("16", "1714") => Outcome::Home,
                            ("16", "1715") => Outcome::Away,
                            ("29", "74") => Outcome::Yes,
                            ("29", "76") => Outcome::No,
                            _ => continue,
                        };
                        let open = market_open && o["isActive"].as_i64() == Some(1);
                        let price = if open { num(&o["odds"]) } else { None };
                        ev.quotes.push(QuoteIn::new(market, line, outcome, price).ts(ts));
                    }
                }
                events.push(ev);
            }
        }
        Ok(Snapshot { events, recv_ms })
    }
}
