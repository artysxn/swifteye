//! 1xBet platform (Linebet, Melbet, BetWinner, Megapari, ...): LiveFeed (in play) and
//! LineFeed (pre-match). Every brand serves the same `/service-api` on its own domain.
//!
//! Event types: 1/2/3 = 1X2 (group 1), 401/402 = winner incl. overtime (group 101),
//! 7/8 = handicap (P is that team's handicap), 9/10 = total over/under (P = line),
//! 180/181 = both teams to score.
//! Live and line games carry different ids, so events are keyed by team ids + kick-off.
//! LineFeed returns at most ~50 games per call, so pre-match asks one sport at a time.
//! LiveFeed returns at most 200 games per call whatever `count` says (2026-11-22: count=500 and
//! count=1000 both gave 200). One request for every sport was filled by football on a busy
//! afternoon (161 football, 2 of 18 e-sports games, 5 of 12 tennis), so live is asked in groups
//! (`live_groups`): the traded sports together on every poll, football and the minor sports each
//! on every third poll, their last reply kept in between. A group that still hits the cap splits.
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
    /// The live requests and their last replies (`live_groups`); built on the first poll.
    pub live: Live,
}

/// 1xBet's LiveFeed never returns more games than this in one reply.
const LIVE_CAP: usize = 200;
const FOOTBALL: i64 = 1;
/// Volleyball, handball, table tennis: few or no Polymarket games, asked less often.
const MINOR_SPORTS: &[i64] = &[6, 8, 10];
/// Football and the minor sports are asked on every Nth poll.
const SLOW_EVERY: u64 = 3;
/// A group's reply older than this is asked again at once (after a pause or failed requests) ...
const REFRESH_MS: Ms = 10_000;
/// ... and older than this it is no longer shown (its games close).
const STALE_MS: Ms = 30_000;

/// One LiveFeed request: its sports, when it is due, and its last reply.
pub struct LiveGroup {
    sports: Vec<i64>,
    /// Asked on the polls where `turn % every == phase`.
    every: u64,
    phase: u64,
    events: Vec<EventIn>,
    details: std::collections::HashMap<String, String>,
    /// When the last reply arrived; 0 = never.
    at: Ms,
}

#[derive(Default)]
pub struct Live {
    groups: Vec<LiveGroup>,
    turn: u64,
}

/// The live requests for a brand's sports: the traded sports together on every poll; football
/// alone (it fills a whole reply) and the minor sports on every third poll, on different polls.
fn live_groups(sports: &[i64]) -> Vec<LiveGroup> {
    let group = |sports: Vec<i64>, every: u64, phase: u64| LiveGroup { sports, every, phase, events: Vec::new(), details: Default::default(), at: 0 };
    let main: Vec<i64> = sports.iter().copied().filter(|s| *s != FOOTBALL && !MINOR_SPORTS.contains(s)).collect();
    let minor: Vec<i64> = sports.iter().copied().filter(|s| MINOR_SPORTS.contains(s)).collect();
    let mut out = Vec::new();
    if !main.is_empty() {
        out.push(group(main, 1, 0));
    }
    if sports.contains(&FOOTBALL) {
        out.push(group(vec![FOOTBALL], SLOW_EVERY, 1));
    }
    if !minor.is_empty() {
        out.push(group(minor, SLOW_EVERY, 2));
    }
    out
}

/// Only football is read beyond the winner (handicap, total, both teams to score), so only a
/// request with football in it asks for the full list.
fn live_mode(sports: &[i64]) -> u8 {
    if sports.contains(&FOOTBALL) { 4 } else { 6 }
}

/// The groups to ask on this poll: those whose turn it is, and any whose reply is missing or old.
fn due(groups: &[LiveGroup], turn: u64, now: Ms) -> Vec<usize> {
    groups.iter().enumerate().filter(|(_, g)| turn % g.every == g.phase % g.every || now - g.at >= REFRESH_MS).map(|(i, _)| i).collect()
}

/// A reply at the cap is cut off: ask its sports in two requests from now on (one sport cannot split).
fn split_group(groups: &mut Vec<LiveGroup>, i: usize) {
    if groups[i].sports.len() < 2 {
        return;
    }
    let half = groups[i].sports.len() / 2;
    let tail = groups[i].sports.split_off(half);
    let (every, phase) = (groups[i].every, groups[i].phase);
    groups[i].at = 0;
    groups[i].events.clear();
    groups[i].details.clear();
    groups.push(LiveGroup { sports: tail, every, phase, events: Vec::new(), details: Default::default(), at: 0 });
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
    /// `mode` 4 = every line of the list (what football needs: handicaps, totals, both-to-score);
    /// 6 = the winner prices only, the same games, names and scores in a reply ~26% smaller
    /// (checked 2026-11-22 against mode 4 at the same moment: identical winner lines in 116 of 116
    /// games). The feed refuses anything it does not know with 406: other `count` values, another
    /// order of the sports, extra parameters. Keep the parameter order.
    fn url(&self, feed: &str, sports: &str, mode: u8) -> String {
        // Since ~2026-11-22 LineFeed answers 406 NotAcceptable to `noFilterBlockEvent`; LiveFeed still takes it.
        let block = if feed == "LiveFeed" { "&noFilterBlockEvent=true" } else { "" };
        format!(
            "https://{}/service-api/{feed}/Get1x2_VZip?sports={sports}&count=500&lng=en&mode={mode}{}&getEmpty=true{block}",
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
        let urls: Vec<String> = self.sports.iter().map(|s| self.url("LineFeed", &s.to_string(), 4)).collect();
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
        if self.live.groups.is_empty() {
            self.live.groups = live_groups(&self.sports);
        }
        let turn = self.live.turn;
        self.live.turn += 1;
        let asked = due(&self.live.groups, turn, now_ms());
        let urls: Vec<String> = asked
            .iter()
            .map(|&i| {
                let g = &self.live.groups[i];
                self.url("LiveFeed", &g.sports.iter().map(|s| s.to_string()).collect::<Vec<_>>().join(","), live_mode(&g.sports))
            })
            .collect();
        let res = futures::future::join_all(urls.into_iter().map(|u| fetch_json(http.get(u)))).await;
        let mut recv_ms = 0;
        let mut first_err = None;
        let mut capped = Vec::new();
        for (&i, r) in asked.iter().zip(res) {
            match r.and_then(|(v, t)| Ok((parse(&v, true)?, details(&v), v["Value"].as_array().map_or(0, |a| a.len()), t))) {
                Ok((evs, det, games, t)) => {
                    let g = &mut self.live.groups[i];
                    (g.events, g.details, g.at) = (evs, det, t);
                    recv_ms = recv_ms.max(t);
                    if games >= LIVE_CAP && g.sports.len() > 1 {
                        capped.push(i);
                    }
                }
                // The every-poll group failing fails the poll (as one request did before); a slow
                // group keeps its last reply until it is STALE_MS old.
                Err(e) if self.live.groups[i].every == 1 || first_err.is_none() => first_err = Some((self.live.groups[i].every == 1, e)),
                Err(_) => {}
            }
        }
        match first_err {
            Some((true, e)) => return Err(e),
            Some((false, e)) if recv_ms == 0 => return Err(e),
            _ => {}
        }
        for i in capped {
            tracing::warn!("{}: live list hit the {LIVE_CAP}-game cap for sports {:?}; asking them in two requests", self.name, self.live.groups[i].sports);
            split_group(&mut self.live.groups, i);
        }
        let now = now_ms();
        let mut events = Vec::new();
        self.details.clear();
        for g in self.live.groups.iter().filter(|g| now - g.at < STALE_MS) {
            events.extend(g.events.iter().cloned());
            self.details.extend(g.details.iter().map(|(k, v)| (k.clone(), v.clone())));
        }
        self.pre.merge(&mut events);
        Ok(Snapshot { events, recv_ms: if recv_ms > 0 { recv_ms } else { now } })
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
    fn only_a_request_with_football_asks_for_every_line() {
        assert_eq!(live_mode(&[1]), 4);
        assert_eq!(live_mode(&[2, 3, 4, 5, 13, 40]), 6);
        assert_eq!(live_mode(&[6]), 6);
        let b = Linebet { name: "linebet", host: "linebet.com".into(), extra: "&country=137&partner=189".into(), sports: vec![1, 40], pre: Pre::new(8.0, 15_000), details: Default::default(), live: Default::default() };
        assert_eq!(b.url("LiveFeed", "2,3,4,5,13,40", 6), "https://linebet.com/service-api/LiveFeed/Get1x2_VZip?sports=2,3,4,5,13,40&count=500&lng=en&mode=6&country=137&partner=189&getEmpty=true&noFilterBlockEvent=true");
        assert_eq!(b.url("LineFeed", "1", 4), "https://linebet.com/service-api/LineFeed/Get1x2_VZip?sports=1&count=500&lng=en&mode=4&country=137&partner=189&getEmpty=true");
    }

    #[test]
    fn live_is_asked_in_groups_so_football_cannot_crowd_out_the_traded_sports() {
        let all: Vec<i64> = SPORT_IDS.iter().map(|x| x.0).collect();
        let g = live_groups(&all);
        let shape: Vec<(Vec<i64>, u64, u64)> = g.iter().map(|g| (g.sports.clone(), g.every, g.phase)).collect();
        assert_eq!(shape, vec![(vec![2, 3, 4, 5, 13, 40], 1, 0), (vec![1], 3, 1), (vec![6, 8, 10], 3, 2)]);
        // A brand that follows only e-sports and football: two requests, no empty one.
        let g = live_groups(&[1, 40]);
        assert_eq!(g.iter().map(|g| g.sports.clone()).collect::<Vec<_>>(), vec![vec![40], vec![1]]);
        assert!(live_groups(&[]).is_empty());
    }

    #[test]
    fn slow_groups_take_turns_and_an_old_reply_is_asked_again_at_once() {
        let mut g = live_groups(&SPORT_IDS.iter().map(|x| x.0).collect::<Vec<_>>());
        let now = 1_000_000;
        // First poll: nothing has a reply yet, so everything is asked.
        assert_eq!(due(&g, 0, now), vec![0, 1, 2]);
        for x in g.iter_mut() {
            x.at = now;
        }
        // Then: the traded sports on every poll, football on turns 1, 4, .., the minor sports on 2, 5, ..
        assert_eq!(due(&g, 3, now + 1_000), vec![0]);
        assert_eq!(due(&g, 4, now + 2_000), vec![0, 1]);
        assert_eq!(due(&g, 5, now + 3_000), vec![0, 2]);
        // Football's reply is 10 s old (requests failed, or the feed was paused): asked on any turn.
        g[1].at = now - REFRESH_MS;
        assert_eq!(due(&g, 3, now), vec![0, 1]);
    }

    #[test]
    fn a_group_at_the_cap_splits_but_one_sport_cannot() {
        let mut g = live_groups(&[2, 3, 4, 40, 1]);
        g[0].at = 5;
        split_group(&mut g, 0);
        assert_eq!(g.iter().map(|g| (g.sports.clone(), g.every, g.at)).collect::<Vec<_>>(), vec![(vec![2, 3], 1, 0), (vec![1], 3, 0), (vec![4, 40], 1, 0)]);
        split_group(&mut g, 1); // football alone stays as it is
        assert_eq!(g.len(), 3);
    }

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
