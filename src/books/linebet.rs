//! 1xBet platform (Linebet, Melbet, BetWinner, Megapari, ...): LiveFeed (in play) and
//! LineFeed (pre-match). Every brand serves the same `/service-api` on its own domain.
//!
//! Event types: 1/2/3 = 1X2 (group 1), 401/402 = winner incl. overtime (group 101),
//! 7/8 = handicap (P is that team's handicap), 9/10 = total over/under (P = line),
//! 180/181 = both teams to score.
//! Live and line games carry different ids, so events are keyed by team ids + kick-off.
//! LineFeed returns at most ~50 games per call, so pre-match asks one sport at a time.
//! E-sports (sport 40) carry the title in the league name ("CS 2. European Pro League").

use super::*;

pub struct Linebet {
    pub name: &'static str,
    pub host: String,
    /// Brand-specific query extras (country / partner), may be empty.
    pub extra: String,
    /// 1xBet sport ids to ask for.
    pub sports: Vec<i64>,
    pub pre: Pre,
    /// Score detail of the last live fetch's games, by event id (`details`). Recorded only.
    pub details: std::collections::HashMap<String, String>,
}

/// (1xBet sport id, our sport key); 40 = e-sports, titled per league.
pub const SPORT_IDS: &[(i64, &str)] = &[
    (1, "football"),
    (2, "hockey"),
    (3, "basketball"),
    (4, "tennis"),
    (5, "baseball"),
    (6, "volleyball"),
    (8, "handball"),
    (10, "tabletennis"),
    (13, "amfootball"),
    (40, "esports"),
];

pub fn sport_ids(follows: impl Fn(&str) -> bool) -> Vec<i64> {
    SPORT_IDS.iter().filter(|(_, s)| follows(s)).map(|x| x.0).collect()
}

impl Linebet {
    fn url(&self, feed: &str, sports: &str) -> String {
        // Since ~2026-11-22 LineFeed answers 406 NotAcceptable to `noFilterBlockEvent`; LiveFeed still takes it.
        let block = if feed == "LiveFeed" { "&noFilterBlockEvent=true" } else { "" };
        format!(
            "https://{}/service-api/{feed}/Get1x2_VZip?sports={sports}&count=500&lng=en&mode=4{}&getEmpty=true{block}",
            self.host, self.extra
        )
    }
}

impl Book for Linebet {
    fn name(&self) -> &'static str {
        self.name
    }
    fn source(&self) -> String {
        format!("{}/service-api LiveFeed + LineFeed (1xBet platform, sports {:?})", self.host, self.sports)
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let urls: Vec<String> = self.sports.iter().map(|s| self.url("LineFeed", &s.to_string())).collect();
        refresh_pre(&mut self.pre, self.name, || async move {
            let res = futures::future::join_all(urls.into_iter().map(|u| fetch_json(http.get(u)))).await;
            let mut out = Vec::new();
            let mut last_err = None;
            for r in res {
                match r.and_then(|(v, _)| parse(&v, false)) {
                    Ok(evs) => out.extend(evs),
                    Err(e) => last_err = Some(e),
                }
            }
            match (out.is_empty(), last_err) {
                (true, Some(e)) => Err(e),
                _ => Ok(out),
            }
        })
        .await;
        let ids: Vec<String> = self.sports.iter().map(|s| s.to_string()).collect();
        let (v, recv_ms) = fetch_json(http.get(self.url("LiveFeed", &ids.join(",")))).await?;
        let mut events = parse(&v, true)?;
        self.details = details(&v);
        self.pre.merge(&mut events);
        Ok(Snapshot { events, recv_ms })
    }
    fn score_detail(&mut self) -> std::collections::HashMap<String, String> {
        std::mem::take(&mut self.details)
    }
}

/// The game's id: both teams' ids and the start hour (the same game keeps it across the live and
/// pre-match feeds), else the feed's own id.
fn event_id(g: &Value) -> String {
    match (int(&g["O1I"]), int(&g["O2I"]), int(&g["S"])) {
        (Some(a), Some(b), Some(s)) => format!("{a}-{b}-{}", s / 3600),
        _ => id_text(&g["I"]),
    }
}

/// A score part as text: numbers and texts as they are ("40", "A"), missing = 0.
fn part(v: &Value) -> String {
    match v {
        Value::Number(n) => n.to_string(),
        Value::String(s) if !s.is_empty() => s.clone(),
        _ => "0".into(),
    }
}

/// Score detail of every game in a live feed, by event id: each period's score ("3-6 1-4": games of
/// each set, rounds of each map, points of each quarter), the points of the tennis game being
/// played ("pts 40-15"), who serves ("srv 2"), and in football red cards and penalties.
/// Statistics that change all the time (attacks, possession, the clock) are left out.
fn details(v: &Value) -> std::collections::HashMap<String, String> {
    let mut out = std::collections::HashMap::new();
    for g in v["Value"].as_array().into_iter().flatten() {
        let Some(sport) = sport_of(g) else { continue };
        let sc = &g["SC"];
        let mut bits = Vec::new();
        let periods: Vec<String> =
            sc["PS"].as_array().into_iter().flatten().map(|p| &p["Value"]).filter(|x| x.get("S1").is_some() || x.get("S2").is_some()).map(|x| format!("{}-{}", part(&x["S1"]), part(&x["S2"]))).collect();
        if !periods.is_empty() {
            bits.push(periods.join(" "));
        }
        if sc["SS"].is_object() {
            bits.push(format!("pts {}-{}", part(&sc["SS"]["S1"]), part(&sc["SS"]["S2"])));
        }
        if sport == "tennis" {
            if let Some(p) = int(&sc["P"]) {
                bits.push(format!("srv {p}"));
            }
        }
        if sport == "football" {
            for st in sc["ST"].as_array().into_iter().flatten().flat_map(|k| k["Value"].as_array().into_iter().flatten()) {
                let lab = match int(&st["ID"]) {
                    Some(71) => "rc",
                    Some(72) => "pen",
                    _ => continue,
                };
                bits.push(format!("{lab} {}-{}", part(&st["S1"]), part(&st["S2"])));
            }
        }
        if !bits.is_empty() {
            out.insert(event_id(g), bits.join(" | "));
        }
    }
    out
}

fn sport_of(g: &Value) -> Option<&'static str> {
    let si = g["SI"].as_i64()?;
    let key = SPORT_IDS.iter().find(|x| x.0 == si)?.1;
    if key == "esports" {
        // "CS 2. Berserk League", "Dota 2. Ultras Dota Pro"
        return esport_key(&text(&g["LE"]).replace("CS 2", "CS2"));
    }
    Some(key)
}

fn parse(v: &Value, live: bool) -> Result<Vec<EventIn>> {
    if v["Success"].as_bool() == Some(false) {
        bail!("feed error: {}", v["Error"]);
    }
    let mut events = Vec::new();
    for g in v["Value"].as_array().into_iter().flatten() {
        let Some(sport) = sport_of(g) else { continue };
        let sc = &g["SC"];
        let s1 = sc["FS"]["S1"].as_i64().unwrap_or(0);
        let s2 = sc["FS"]["S2"].as_i64().unwrap_or(0);
        let start = int(&g["S"]);
        let id = event_id(g);
        let mut ev = EventIn {
            id,
            sport: sport.into(),
            league: text(&g["LE"]).chars().take(200).collect::<String>().replace(". ", " / "),
            home: text(if g["O1E"].is_string() { &g["O1E"] } else { &g["O1"] }),
            away: text(if g["O2E"].is_string() { &g["O2E"] } else { &g["O2"] }),
            start_ms: start.map(|s| s * 1000),
            sr_id: None,
            score: live.then(|| format!("{s1}-{s2}")),
            clock: if live && sport == "football" { int(&sc["TS"]).map(|s| format!("{}'", s / 60)) } else { None },
            quotes: Vec::new(),
            sr_weak: false,
            live,
        };
        if ev.league.is_empty() {
            ev.league = text(&g["L"]);
        }
        if ev.home.is_empty() || ev.away.is_empty() {
            continue;
        }
        let game_blocked = g["B"].as_bool() == Some(true);
        let football = sport == "football";
        // Main events plus the extra lines in AE; dedupe happens in the engine.
        let mains = g["E"].as_array().into_iter().flatten();
        let extra = g["AE"].as_array().into_iter().flatten().flat_map(|ae| ae["ME"].as_array().into_iter().flatten());
        let all: Vec<&Value> = mains.chain(extra).collect();
        // Two-way sports: the overtime-inclusive winner (401/402) when the book has it,
        // otherwise the plain 1/3 (tennis, e-sports have no draw).
        let has_ot = all.iter().any(|o| matches!(o["T"].as_i64(), Some(401) | Some(402)));
        for o in all {
            let t = o["T"].as_i64().unwrap_or(0);
            let grp = o["G"].as_i64();
            let p = num(&o["P"]);
            let (market, line, outcome) = match t {
                1 | 2 | 3 if three_way(sport) || !has_ot => match t {
                    1 => (Market::X12, None, Outcome::Home),
                    2 => (Market::X12, None, Outcome::Draw),
                    _ => (Market::X12, None, Outcome::Away),
                },
                401 if !three_way(sport) => (Market::X12, None, Outcome::Home),
                402 if !three_way(sport) => (Market::X12, None, Outcome::Away),
                7 if football => (Market::Handicap, Some(p.unwrap_or(0.0)), Outcome::Home),
                8 if football => (Market::Handicap, Some(-p.unwrap_or(0.0)), Outcome::Away),
                9 | 10 if football => match p {
                    Some(l) => (Market::Total, Some(l), if t == 9 { Outcome::Over } else { Outcome::Under }),
                    None => continue,
                },
                180 if football => (Market::Btts, None, Outcome::Yes),
                181 if football => (Market::Btts, None, Outcome::No),
                _ => continue,
            };
            // Only full-time markets: groups 1 (1X2), 2 (handicap), 17 (total), 19 (BTTS), 101 (winner incl. OT).
            if !matches!(grp, Some(1) | Some(2) | Some(17) | Some(19) | Some(101)) {
                continue;
            }
            let blocked = game_blocked || o["B"].as_bool() == Some(true);
            ev.quotes.push(QuoteIn::new(market, line, outcome, if blocked { None } else { num(&o["C"]) }));
        }
        ev.quotes = winner_quotes(sport, ev.quotes);
        events.push(ev);
    }
    Ok(events)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn score_detail_of_a_tennis_game_a_map_and_a_football_game() {
        let v = serde_json::json!({"Value": [
            {"SI": 4, "O1I": 11, "O2I": 12, "S": 7200, "LE": "Challenger. Kobe",
             "SC": {"FS": {"S2": 1}, "PS": [{"Key": 1, "Value": {"S1": 3, "S2": 6, "NF": "1st set"}}, {"Key": 2, "Value": {"S1": 1, "S2": 4}}],
                    "CP": 2, "P": 2, "SS": {"S1": "40", "S2": "A"}, "ST": [{"Key": 0, "Value": [{"ID": 1, "S1": "1", "S2": "6", "N": "Aces"}]}]}},
            {"SI": 40, "O1I": 21, "O2I": 22, "S": 7200, "LE": "CS 2. Winners series",
             "SC": {"FS": {}, "PS": [{"Key": 1, "Value": {"S1": 3, "S2": 1, "NF": "1st map"}}, {"Key": 2, "Value": {"NF": "2nd map"}}]}},
            {"SI": 1, "O1I": 31, "O2I": 32, "S": 7200, "LE": "K League 1",
             "SC": {"FS": {"S1": 1}, "PS": [{"Key": 1, "Value": {"S1": 1, "NF": "1st half"}}],
                    "ST": [{"Key": 0, "Value": [{"ID": 45, "S1": "42", "S2": "37", "N": "Attacks"}, {"ID": 71, "S1": "0", "S2": "1", "N": "Red card"}, {"ID": 72, "S1": "0", "S2": "0", "N": "Penalty"}]}]}},
            {"SI": 3, "O1I": 41, "O2I": 42, "S": 7200, "LE": "NBA", "SC": {"FS": {}, "PS": []}}
        ]});
        let d = details(&v);
        assert_eq!(d.get("11-12-2").map(String::as_str), Some("3-6 1-4 | pts 40-A | srv 2"));
        assert_eq!(d.get("21-22-2").map(String::as_str), Some("3-1"));
        assert_eq!(d.get("31-32-2").map(String::as_str), Some("1-0 | rc 0-1 | pen 0-0"));
        assert!(!d.contains_key("41-42-2"), "a game with no score detail has no entry");
    }

    #[test]
    fn a_score_row_only_when_the_score_or_detail_changes() {
        let mut last = std::collections::HashMap::new();
        let det = |s: &str| std::collections::HashMap::from([("g".to_string(), s.to_string())]);
        let row = |sc: &str, clk: &str| vec![("g".to_string(), Some(sc.to_string()), Some(clk.to_string()))];
        assert_eq!(score_writes("linebet", 1, row("0-0", "10'"), &det("0-0"), &mut last).len(), 1, "first sight");
        assert_eq!(score_writes("linebet", 2, row("0-0", "11'"), &det("0-0"), &mut last).len(), 0, "only the clock moved");
        assert_eq!(score_writes("linebet", 3, row("0-0", "12'"), &det("0-0 | rc 1-0"), &mut last).len(), 1, "a red card");
        assert_eq!(score_writes("linebet", 4, row("1-0", "13'"), &det("1-0 | rc 1-0"), &mut last).len(), 1, "a goal");
        assert_eq!(score_writes("linebet", 5, vec![], &det(""), &mut last).len(), 0);
        assert!(last.is_empty(), "games gone from the live list are forgotten");
    }
}
