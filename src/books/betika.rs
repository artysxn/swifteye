//! Betika (Kenya): live.betika.com match list, 1X2. Times are East Africa Time (UTC+3).
//! `parent_match_id` is the Sportradar match id.

use super::*;

pub struct Betika;

impl Book for Betika {
    fn name(&self) -> &'static str {
        "betika"
    }
    fn source(&self) -> String {
        "live.betika.com/v1/uo/matches (football, 1X2)".into()
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let url = "https://live.betika.com/v1/uo/matches?page=1&limit=1000&sub_type_id=1&sport=14&sort=1";
        let (v, recv_ms) = fetch_json(http.get(url)).await?;
        let mut events = Vec::new();
        for e in v["data"].as_array().into_iter().flatten() {
            if e["sport_id"].as_i64().is_some_and(|s| s != 14) {
                continue;
            }
            let open = e["market_active"].as_i64() == Some(1) && e["odd_active"].as_i64() == Some(1);
            let q = |field: &str, seq: &str, outcome| {
                let price = if open { num(&e[field]) } else { None };
                QuoteIn::new(Market::X12, None, outcome, price).ts(int(&e[seq]))
            };
            let sr = int(&e["parent_match_id"]).filter(|&x| x > 0).map(|x| x.to_string());
            events.push(EventIn {
                id: id_text(&e["match_id"]),
                sport: "football".into(),
                league: join_league(&[e["category"].as_str().unwrap_or(""), e["competition_name"].as_str().unwrap_or("")]),
                home: text(&e["home_team"]),
                away: text(&e["away_team"]),
                start_ms: e["start_time"].as_str().and_then(|s| naive_ms(s, 3)),
                sr_id: sr,
                score: e["current_score"].as_str().map(|s| s.replace(':', "-")),
                clock: e["match_time"].as_str().map(str::to_string),
                sr_weak: false,
                quotes: vec![
                    q("home_odd", "home_seq", Outcome::Home),
                    q("neutral_odd", "neutral_seq", Outcome::Draw),
                    q("away_odd", "away_seq", Outcome::Away),
                ],
            });
        }
        Ok(Snapshot { events, recv_ms })
    }
}
