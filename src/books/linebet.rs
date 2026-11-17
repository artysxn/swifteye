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
        format!(
            "https://{}/service-api/{feed}/Get1x2_VZip?sports={sports}&count=500&lng=en&mode=4{}&getEmpty=true&noFilterBlockEvent=true",
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
        self.pre.merge(&mut events);
        Ok(Snapshot { events, recv_ms })
    }
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
        let id = match (int(&g["O1I"]), int(&g["O2I"]), start) {
            (Some(a), Some(b), Some(s)) => format!("{a}-{b}-{}", s / 3600),
            _ => id_text(&g["I"]),
        };
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
