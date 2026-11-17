//! Betcity (betcity.ru): the live feed its own site loads, `https://ad.betcity.ru/d/on_air/bets`.
//!
//! * `reply.sports[id_sp].chmps[id_ch].evts[id_ev]`: `name_ht` / `name_at` (Russian; e-sports teams
//!   are already in Latin script), `date_ev` (unix s), `sc_ev` score.
//! * Main result: `main["69"].data[id_ev].blocks.Wm` = `P1` / `X` / `P2` with `kf` (decimal) and
//!   `md` = Betcity's own change time (unix s) → used as the book timestamp.
//! * Sports by id: 1 football, 2 tennis, 3 basketball, 7 hockey, 8 handball, 12 volleyball,
//!   46 table tennis, 73 e-sports (game in the championship name: "CS2. RUSH:Brno ...").
//! * Names are not translated, so non-e-sports games link to other books only when the
//!   transliterated name is close enough (the matcher is strict, so it fails safe: no link).

use super::*;

const URL: &str = "https://ad.betcity.ru/d/on_air/bets?rev=8&ver=124&csn=ooca9s";

pub struct Betcity;

fn sport_of(id: i64) -> Option<&'static str> {
    Some(match id {
        1 => "football",
        2 => "tennis",
        3 => "basketball",
        7 => "hockey",
        8 => "handball",
        12 => "volleyball",
        46 => "tabletennis",
        73 => "esports",
        _ => return None,
    })
}

impl Book for Betcity {
    fn name(&self) -> &'static str {
        "betcity"
    }
    fn source(&self) -> String {
        "ad.betcity.ru /d/on_air/bets (all live, main result with Betcity's change stamps)".into()
    }
    async fn fetch(&mut self, http: &reqwest::Client) -> Result<Snapshot> {
        let (v, recv_ms) = fetch_json(http.get(URL).header("Referer", "https://betcity.ru/")).await?;
        let mut out = Vec::new();
        for (_, s) in v["reply"]["sports"].as_object().into_iter().flatten() {
            let Some(group) = s["id_sp"].as_i64().and_then(sport_of) else { continue };
            for (_, ch) in s["chmps"].as_object().into_iter().flatten() {
                let league = text(&ch["name_ch"]);
                let sport: &str = if group == "esports" {
                    match esport_key(&league) {
                        Some(x) => x,
                        None => continue,
                    }
                } else {
                    group
                };
                for (id, e) in ch["evts"].as_object().into_iter().flatten() {
                    let (home, away) = (text(&e["name_ht"]), text(&e["name_at"]));
                    if home.is_empty() || away.is_empty() {
                        continue;
                    }
                    let main = &e["main"]["69"];
                    let open = main["is_open"].as_i64() != Some(0);
                    let wm = &main["data"][id.as_str()]["blocks"]["Wm"];
                    let mut quotes = Vec::new();
                    for (k, o) in [("P1", Outcome::Home), ("X", Outcome::Draw), ("P2", Outcome::Away)] {
                        let q = &wm[k];
                        if q.is_null() {
                            continue;
                        }
                        let ts = int(&q["md"]).map(|s| s * 1000);
                        quotes.push(QuoteIn::new(Market::X12, None, o, if open { num(&q["kf"]) } else { None }).ts(ts));
                    }
                    out.push(EventIn {
                        id: id.clone(),
                        sport: sport.into(),
                        league: league.clone(),
                        home,
                        away,
                        start_ms: int(&e["date_ev"]).map(|s| s * 1000),
                        sr_id: None,
                        sr_weak: false,
                        live: true,
                        score: e["sc_ev"].as_str().map(|s| s.replace(':', "-")),
                        clock: e["min"].as_i64().map(|m| format!("{m}'")),
                        quotes: winner_quotes(sport, quotes),
                    });
                }
            }
        }
        Ok(Snapshot { events: out, recv_ms })
    }
}
