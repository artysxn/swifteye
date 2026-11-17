//! Novibet: live soccer market view (plain GET). 1X2 and goal totals.

use super::*;

pub struct Novibet;

impl Book for Novibet {
    fn name(&self) -> &'static str {
        "novibet"
    }
    fn source(&self) -> String {
        "novibet.com/spt/feed/marketviews/location/v2/4324/4390 (live soccer)".into()
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let url = "https://www.novibet.com/spt/feed/marketviews/location/v2/4324/4390/?lang=en-US&timeZ=UTC&oddsR=1&usrGrp=null";
        let (v, recv_ms) = fetch_json(http.get(url).header("Referer", "https://www.novibet.com/live-betting")).await?;
        let mut events = Vec::new();
        for view in v.as_array().into_iter().flatten() {
            for bv in view["betViews"].as_array().into_iter().flatten() {
                for c in bv["competitions"].as_array().into_iter().flatten() {
                    for e in c["events"].as_array().into_iter().flatten() {
                        if e["eventSysname"].as_str() != Some("SOCCER_MATCH") {
                            continue;
                        }
                        let cap = &e["additionalCaptions"];
                        let ld = &e["liveData"];
                        let mut ev = EventIn {
                            id: id_text(&e["betContextId"]),
                            sport: "football".into(),
                            league: join_league(&[c["regionCaption"].as_str().unwrap_or(""), c["caption"].as_str().unwrap_or("")]),
                            home: text(&cap["competitor1"]),
                            away: text(&cap["competitor2"]),
                            start_ms: None,
                            sr_id: None,
                            sr_weak: false,
                            live: true,
                            score: match (ld["homeGoals"].as_i64(), ld["awayGoals"].as_i64()) {
                                (Some(h), Some(a)) => Some(format!("{h}-{a}")),
                                _ => None,
                            },
                            clock: ld["elapsed"].as_str().map(|s| s.chars().take(5).collect()),
                            quotes: Vec::new(),
                        };
                        for m in e["markets"].as_array().into_iter().flatten() {
                            let kind = m["betTypeSysname"].as_str().unwrap_or("");
                            for b in m["betItems"].as_array().into_iter().flatten() {
                                let price = if b["isAvailable"].as_bool() == Some(true) { num(&b["price"]) } else { None };
                                let code = b["code"].as_str().unwrap_or("");
                                let q = match (kind, code) {
                                    ("SOCCER_MATCH_RESULT", "1") => QuoteIn::new(Market::X12, None, Outcome::Home, price),
                                    ("SOCCER_MATCH_RESULT", "X") => QuoteIn::new(Market::X12, None, Outcome::Draw, price),
                                    ("SOCCER_MATCH_RESULT", "2") => QuoteIn::new(Market::X12, None, Outcome::Away, price),
                                    ("SOCCER_UNDER_OVER", c) if c.starts_with('O') || c.starts_with('U') => {
                                        let line = b["instanceCaption"].as_str().or(b["caption"].as_str()).and_then(|s| s.split_whitespace().last()?.parse::<f64>().ok());
                                        match line {
                                            Some(l) => QuoteIn::new(Market::Total, Some(l), if c.starts_with('O') { Outcome::Over } else { Outcome::Under }, price),
                                            None => continue,
                                        }
                                    }
                                    _ => continue,
                                };
                                ev.quotes.push(q);
                            }
                        }
                        events.push(ev);
                    }
                }
            }
        }
        Ok(Snapshot { events, recv_ms })
    }
}
