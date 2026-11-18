//! Do Polymarket and Kalshi settle this game the same way? Both venues write their rules as text;
//! this reads the parts an arbitrage depends on. Checked before every paper arbitrage:
//!
//! * Same game: both rules name the same originally scheduled date, and the same time when both
//!   give one (Kalshi football names only the date).
//! * Same result rule: football 90 minutes plus stoppage on both; basketball / hockey / baseball
//!   overtime (extra innings) included on Polymarket and not limited to regulation on Kalshi;
//!   tennis: a player who retires after the first ball loses on both; e-sports: the full match on
//!   both (Polymarket BO2 markets pay 50-50 on a tie, Kalshi has no tie: skipped). Rules that do
//!   not say are treated as different.
//! * The no-result cases DIFFER and are priced, not assumed away. Called off before the start
//!   (cancelled, walkover, forfeit before play, postponed past the window): Polymarket pays 50-50
//!   on two-way markets and "No" on football's Yes/No markets; Kalshi pays a "fair price"
//!   (its last fair market price; taken as its current mid) or $0.50 (tennis). A pre-match
//!   arbitrage whose value in that case is a loss of more than `VOID_MAX_LOSS` of its cost is
//!   skipped. Once the game is live that branch is gone.
//! * Postponement windows also differ (e-sports: Polymarket 14 days, Kalshi 48 hours). A game moved
//!   between the two would settle on the result at one venue and at the fair price at the other.
//!   Recorded on the position (`postpone_days`), not priced: it is rare and has no price to use.

use crate::venue::V;
use serde::Serialize;

/// A pre-match arbitrage may lose at most this share of its cost if the game is called off.
pub const VOID_MAX_LOSS: f64 = 0.02;

#[derive(Serialize, Clone, Debug, Default, PartialEq)]
pub struct Sched {
    /// (year, month, day) as written (US Eastern).
    pub ymd: Option<(i32, u32, u32)>,
    /// (hour 0-23, minute) when the rules give a time.
    pub hm: Option<(u32, u32)>,
}

const MONTHS: [&str; 12] = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

fn month(w: &str) -> Option<u32> {
    let w = w.trim_matches(|c: char| !c.is_ascii_alphabetic()).to_ascii_lowercase();
    if w.len() < 3 {
        return None;
    }
    MONTHS.iter().position(|m| w.starts_with(m)).map(|i| i as u32 + 1)
}

fn digits(w: &str) -> Option<u32> {
    let d: String = w.chars().take_while(|c| c.is_ascii_digit()).collect();
    d.parse().ok()
}

/// "originally scheduled for Wednesday, November 18, 2026 at 11:30 AM ET",
/// "initially scheduled for November 18, 2026 at 5:45AM ET", "originally scheduled for Nov 18,
/// 2026 at 4:00 AM EST", "originally scheduled for Nov 18, 2026 after 90 minutes ...".
pub fn schedule(text: &str) -> Sched {
    let low = text.to_ascii_lowercase();
    let Some(i) = low.find("scheduled for ") else { return Sched::default() };
    let words: Vec<&str> = text[i + "scheduled for ".len()..].split_whitespace().take(9).collect();
    let mut k = 0;
    // Optional weekday ("Wednesday,").
    if words.first().is_some_and(|w| w.ends_with(',') && month(w).is_none()) {
        k = 1;
    }
    let (Some(m), Some(d), Some(y)) = (words.get(k).and_then(|w| month(w)), words.get(k + 1).and_then(|w| digits(w)), words.get(k + 2).and_then(|w| digits(w))) else {
        return Sched::default();
    };
    let mut s = Sched { ymd: Some((y as i32, m, d)), hm: None };
    if words.get(k + 3).is_some_and(|w| w.eq_ignore_ascii_case("at")) {
        if let Some(t) = words.get(k + 4) {
            // "4:00" + "AM" or "5:45AM"
            let (clock, rest) = t.split_at(t.find(|c: char| c.is_ascii_alphabetic()).unwrap_or(t.len()));
            let ampm = if rest.is_empty() { words.get(k + 5).copied().unwrap_or("") } else { rest };
            let mut parts = clock.split(':');
            if let (Some(h), Some(mi)) = (parts.next().and_then(digits), parts.next().and_then(digits)) {
                let pm = ampm.to_ascii_lowercase().starts_with("pm");
                let am = ampm.to_ascii_lowercase().starts_with("am");
                if (pm || am) && (1..=12).contains(&h) {
                    s.hm = Some(((h % 12) + if pm { 12 } else { 0 }, mi));
                }
            }
        }
    }
    s
}

/// Same scheduled game: dates equal, and times equal when both rules give one.
pub fn same_schedule(a: &Sched, b: &Sched) -> bool {
    match (a.ymd, b.ymd) {
        (Some(x), Some(y)) if x == y => match (a.hm, b.hm) {
            (Some(p), Some(q)) => p == q,
            _ => true,
        },
        _ => false,
    }
}

/// The result rule both venues share, or why they do not.
pub fn result_rule(sport: &str, pm_title: &str, pm: &str, k: &str) -> Result<&'static str, &'static str> {
    let (p, k, t) = (pm.to_lowercase(), k.to_lowercase(), pm_title.to_lowercase());
    let esports = crate::books::is_esports_sport(sport);
    if esports {
        if t.contains("map ") || (t.contains("game ") && t.contains("winner")) {
            return Err("settle_not_match_market");
        }
        if t.contains("(bo2)") || t.contains("bo2 ") {
            return Err("settle_tie_possible");
        }
        return if p.contains("win the match") && k.contains("full match") { Ok("full match") } else { Err("settle_result_differs") };
    }
    match sport {
        "football" | "handball" => {
            if p.contains("90 minutes") && k.contains("90 minutes") {
                Ok("90 minutes + stoppage")
            } else {
                Err("settle_result_differs")
            }
        }
        "basketball" | "hockey" | "amfootball" => {
            if p.contains("overtime") && !k.contains("regulation") {
                Ok("overtime included")
            } else {
                Err("settle_result_differs")
            }
        }
        "baseball" => {
            if (p.contains("extra inning") || p.contains("final score")) && !k.contains("regulation") {
                Ok("extra innings included")
            } else {
                Err("settle_result_differs")
            }
        }
        "tennis" => {
            if p.contains("retirement") && (k.contains("withdraws or forfeits after a match has started") || k.contains("retires")) {
                Ok("retirement after the first ball = loss")
            } else {
                Err("settle_result_differs")
            }
        }
        "volleyball" => {
            if !p.is_empty() && !k.is_empty() {
                Ok("match winner")
            } else {
                Err("settle_result_differs")
            }
        }
        _ => Err("settle_rules_unknown"),
    }
}

/// What one contract pays if the game is called off before it starts. `yes` = the Yes side of
/// this outcome's contract (false = its No side). `kalshi_mid` = the contract's current mid,
/// standing in for Kalshi's "fair price". None = the rules do not say.
pub fn void_pay(v: V, rules: &str, two_way: bool, yes: bool, kalshi_mid: Option<f64>) -> Option<f64> {
    let r = rules.to_lowercase();
    let pay_yes = match v {
        V::Pm => {
            if !two_way && (r.contains("will resolve to \"no\"") || r.contains("resolve to \u{201c}no\u{201d}")) {
                Some(0.0)
            } else if r.contains("50-50") {
                Some(0.5)
            } else {
                None
            }
        }
        V::Kalshi => {
            if r.contains("$0.50") || r.contains("$0.5 ") {
                Some(0.5)
            } else if r.contains("fair price") || r.contains("fair market price") {
                kalshi_mid
            } else {
                None
            }
        }
    }?;
    Some(if yes { pay_yes } else { 1.0 - pay_yes })
}

/// Days a postponed game may move and still settle on its result. Polymarket: "(14 calendar
/// days" / "14 days after", or a "does not start by <date>" deadline; Kalshi: "48 hours" /
/// "two weeks".
pub fn postpone_days(v: V, rules: &str, sched: &Sched) -> Option<u32> {
    let r = rules.to_lowercase();
    match v {
        V::Pm => {
            if r.contains("14 calendar days") || r.contains("14 days after") {
                return Some(14);
            }
            let i = r.find("does not start by ")?;
            let after = &rules[i + "does not start by ".len()..];
            let d = schedule(&format!("scheduled for {after}"));
            let ((y1, m1, d1), (y2, m2, d2)) = (sched.ymd?, d.ymd?);
            let a = time::Date::from_calendar_date(y1, time::Month::try_from(m1 as u8).ok()?, d1 as u8).ok()?;
            let b = time::Date::from_calendar_date(y2, time::Month::try_from(m2 as u8).ok()?, d2 as u8).ok()?;
            u32::try_from((b - a).whole_days()).ok()
        }
        V::Kalshi => {
            if r.contains("48 hours") {
                Some(2)
            } else if r.contains("two weeks") || r.contains("14 days") {
                Some(14)
            } else {
                None
            }
        }
    }
}

/// The settlement comparison stored on every paper arbitrage.
#[derive(Serialize, Clone, Debug, Default)]
pub struct Check {
    pub result_rule: &'static str,
    pub pm_schedule: Sched,
    pub kalshi_schedule: Sched,
    /// Dollars the position is worth if the game is called off before the start (pre-match only).
    pub void_value: Option<f64>,
    /// void_value - cost.
    pub void_pnl: Option<f64>,
    /// (Polymarket, Kalshi) days a postponed game may move and still count.
    pub postpone_days: (Option<u32>, Option<u32>),
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_both_schedule_styles() {
        let pm = schedule("In the upcoming EuroCup game, originally scheduled for Wednesday, November 18, 2026 at 11:30 AM ET (as of creation)");
        let k = schedule("... game originally scheduled for Nov 18, 2026 at 11:30 AM EST, then the market resolves to Yes.");
        assert_eq!(pm.ymd, Some((2026, 11, 18)));
        assert_eq!(pm.hm, Some((11, 30)));
        assert!(same_schedule(&pm, &k));
        let t = schedule("originally scheduled for November 18, 2026 at 5:45AM ET.");
        assert_eq!(t.hm, Some((5, 45)));
        let f = schedule("Coritiba vs Santos game originally scheduled for Nov 18, 2026 after 90 minutes plus stoppage time");
        assert_eq!((f.ymd, f.hm), (Some((2026, 11, 18)), None));
        assert!(same_schedule(&pm, &f));
        assert!(!same_schedule(&pm, &schedule("originally scheduled for Nov 19, 2026 at 11:30 AM EST")));
        assert!(!same_schedule(&pm, &schedule("originally scheduled for Nov 18, 2026 at 1:30 PM EST")));
    }

    #[test]
    fn prices_the_called_off_case() {
        let pm2 = "If the match is canceled (not played at all) or ends in a tie, this market will resolve to 50-50.";
        let pm3 = "If the game is canceled, does not start by Friday, this market will resolve to \"No\".";
        let k = "If the relevant match is cancelled before play begins, the market will resolve to the fair market price.";
        let kt = "all markets will resolve to $0.50. If a player withdraws";
        assert_eq!(void_pay(V::Pm, pm2, true, true, None), Some(0.5));
        assert_eq!(void_pay(V::Pm, pm3, false, true, None), Some(0.0));
        assert_eq!(void_pay(V::Pm, pm3, false, false, None), Some(1.0));
        assert_eq!(void_pay(V::Kalshi, k, true, false, Some(0.36)), Some(0.64));
        assert_eq!(void_pay(V::Kalshi, kt, true, true, Some(0.8)), Some(0.5));
        assert_eq!(void_pay(V::Kalshi, "no rule here", true, true, Some(0.8)), None);
    }

    #[test]
    fn result_rules() {
        assert!(result_rule("cs2", "Counter-Strike: A vs B (BO3) - X", "if A win the match against B", "refers to the full match").is_ok());
        assert_eq!(result_rule("cs2", "Counter-Strike: A vs B - Map 1 Winner", "win map 1", "full match"), Err("settle_not_match_market"));
        assert_eq!(result_rule("cs2", "Counter-Strike: A vs B (BO2) - X", "win the match", "full match"), Err("settle_tie_possible"));
        assert!(result_rule("football", "", "only to the outcome within the first 90 minutes of regular play", "after 90 minutes plus stoppage time").is_ok());
        assert!(result_rule("football", "", "the outcome of the game", "after 90 minutes plus stoppage time").is_err());
        assert!(result_rule("hockey", "", "including any overtime periods and shootouts", "wins the game").is_ok());
        assert!(result_rule("tennis", "", "due to the opponent's retirement, default", "If a player withdraws or forfeits after a match has started, that player will resolve to No").is_ok());
    }

    #[test]
    fn postponement_windows() {
        let s = schedule("originally scheduled for Wednesday, November 18, 2026 at 11:30 AM ET");
        assert_eq!(postpone_days(V::Pm, "If the game is canceled, does not start by Friday, November 20, 2026, 11:59 PM ET, or", &s), Some(2));
        assert_eq!(postpone_days(V::Pm, "(14 calendar days after the originally scheduled start", &s), Some(14));
        assert_eq!(postpone_days(V::Kalshi, "not started within 48 hours of its originally scheduled start", &s), Some(2));
    }
}
