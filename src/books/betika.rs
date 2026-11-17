//! Betika (Kenya): live.betika.com for in-play, api.betika.com for pre-match. 1X2.
//! Times are East Africa Time (UTC+3). The live and pre-match systems number matches
//! differently, so events are keyed by `parent_match_id` (the Sportradar id), which both share.

use super::*;

pub struct Betika {
    pub pre: Pre,
}

impl Book for Betika {
    fn name(&self) -> &'static str {
        "betika"
    }
    fn source(&self) -> String {
        "live.betika.com (in play) + api.betika.com (pre-match), football 1X2".into()
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        refresh_pre(&mut self.pre, "betika", || async {
            let mut out = Vec::new();
            for page in 1..=4 {
                let url = format!("https://api.betika.com/v1/uo/matches?page={page}&limit=500&sport_id=14&sort_id=2&period_id=-1&sub_type_id=1");
                let (v, _) = fetch_json(http.get(url)).await?;
                let list = v["data"].as_array().cloned().unwrap_or_default();
                let n = list.len();
                out.extend(list.iter().filter_map(|e| parse(e, false)));
                if n < 500 {
                    break;
                }
            }
            Ok(out)
        })
        .await;
        let url = "https://live.betika.com/v1/uo/matches?page=1&limit=1000&sub_type_id=1&sport=14&sort=1";
        let (v, recv_ms) = fetch_json(http.get(url)).await?;
        let mut events: Vec<EventIn> = v["data"].as_array().into_iter().flatten().filter_map(|e| parse(e, true)).collect();
        self.pre.merge(&mut events);
        Ok(Snapshot { events, recv_ms })
    }
}

fn parse(e: &Value, live: bool) -> Option<EventIn> {
    if e["sport_id"].as_i64().is_some_and(|s| s != 14) {
        return None;
    }
    // Live rows carry activity flags; pre-match rows are open unless the odd is missing.
    let open = !live || (e["market_active"].as_i64() == Some(1) && e["odd_active"].as_i64() == Some(1));
    let q = |field: &str, seq: &str, outcome| {
        let price = if open { num(&e[field]) } else { None };
        QuoteIn::new(Market::X12, None, outcome, price).ts(int(&e[seq]))
    };
    let sr = int(&e["parent_match_id"]).filter(|&x| x > 0).map(|x| x.to_string());
    Some(EventIn {
        id: sr.clone().map(|s| format!("sr{s}")).unwrap_or_else(|| id_text(&e["match_id"])),
        sport: "football".into(),
        league: join_league(&[e["category"].as_str().unwrap_or(""), e["competition_name"].as_str().unwrap_or("")]),
        home: text(&e["home_team"]),
        away: text(&e["away_team"]),
        start_ms: e["start_time"].as_str().and_then(|s| naive_ms(s, 3)),
        sr_id: sr,
        score: e["current_score"].as_str().map(|s| s.replace(':', "-")),
        clock: e["match_time"].as_str().map(str::to_string),
        sr_weak: false,
        live,
        quotes: vec![
            q("home_odd", "home_seq", Outcome::Home),
            q("neutral_odd", "neutral_seq", Outcome::Draw),
            q("away_odd", "away_seq", Outcome::Away),
        ],
    })
}
