//! In-memory live state. Each poll's snapshot is diffed against the previous
//! one; every difference becomes a `Change` with the ms the response arrived.

use crate::matcher::{MatchInfo, Matcher};
use crate::model::*;
use serde::Serialize;
use std::collections::{BTreeMap, HashMap};

pub type QKey = (Market, i32, Outcome);

#[derive(Clone, Debug, Serialize)]
pub struct QuoteState {
    pub price: Option<f64>,
    /// The price before the last change (for arrows / flashes in the UI).
    pub prev: Option<f64>,
    pub updated_ms: Ms,
    pub book_ts: Option<Ms>,
}

#[derive(Clone, Debug, Serialize)]
pub struct EventMeta {
    pub id: String,
    pub sport: String,
    pub league: String,
    pub home: String,
    pub away: String,
    pub start_ms: Option<Ms>,
    pub sr_id: Option<String>,
    pub score: Option<String>,
    pub clock: Option<String>,
}

pub struct EventState {
    pub meta: EventMeta,
    pub match_id: i64,
    pub quotes: BTreeMap<QKey, QuoteState>,
    pub first_seen_ms: Ms,
    pub last_seen_ms: Ms,
    pub last_written_ms: Ms,
    pub missing_polls: u32,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct BookStatus {
    pub name: &'static str,
    pub source: String,
    pub poll_ms: u64,
    /// Paused from the control panel: the source is not polled / not applied.
    pub paused: bool,
    pub polls: u64,
    pub errors: u64,
    pub last_ok_ms: Option<Ms>,
    pub last_error_ms: Option<Ms>,
    pub last_error: Option<String>,
    /// Wall time of the last request (send -> body received).
    pub last_request_ms: Option<i64>,
    pub live_events: usize,
    pub live_quotes: usize,
    pub changes_since_start: u64,
    pub last_change_ms: Option<Ms>,
}

#[derive(Default)]
pub struct BookState {
    pub events: HashMap<String, EventState>,
    pub status: BookStatus,
}

/// Rows the store must persist.
pub enum Write {
    Change(Change),
    Event { book: &'static str, meta: EventMeta, match_id: i64, first_seen_ms: Ms, last_seen_ms: Ms },
    Match(MatchInfo),
    /// Polymarket top of book for one Yes token (probabilities, not odds).
    PmQuote {
        ts_ms: Ms,
        server_ts_ms: Ms,
        match_id: i64,
        event_id: String,
        outcome: Outcome,
        bid: Option<f64>,
        ask: Option<f64>,
        last: Option<f64>,
    },
}

#[derive(Default)]
pub struct Engine {
    pub matcher: Matcher,
    pub books: HashMap<&'static str, BookState>,
    /// (book, book event id) -> match id, restored from the DB so a restart keeps ids.
    pub known: HashMap<(String, String), i64>,
}

/// Events missing this many polls in a row (and for this long) are treated as finished.
const MISSING_POLLS: u32 = 3;
const MISSING_MS: Ms = 15_000;

impl Engine {
    pub fn register(&mut self, name: &'static str, source: String, poll_ms: u64) {
        let st = self.books.entry(name).or_default();
        st.status.name = name;
        st.status.source = source;
        st.status.poll_ms = poll_ms;
    }

    /// Pause/resume a source or change how often it is polled. Returns the new status.
    pub fn control(&mut self, book: &str, paused: Option<bool>, poll_ms: Option<u64>) -> Option<BookStatus> {
        let b = self.books.iter_mut().find(|(n, _)| **n == book)?.1;
        if let Some(p) = paused {
            b.status.paused = p;
        }
        if let Some(ms) = poll_ms {
            if b.status.poll_ms > 0 {
                b.status.poll_ms = ms.clamp(250, 60_000);
            }
        }
        Some(b.status.clone())
    }

    /// (paused, poll interval) for the poll loop.
    pub fn schedule(&self, book: &str) -> (bool, u64) {
        self.books.get(book).map_or((false, 1000), |b| (b.status.paused, b.status.poll_ms.max(250)))
    }

    pub fn poll_error(&mut self, book: &'static str, err: String) {
        if let Some(b) = self.books.get_mut(book) {
            b.status.polls += 1;
            b.status.errors += 1;
            b.status.last_error_ms = Some(now_ms());
            b.status.last_error = Some(err);
        }
    }

    pub fn apply(&mut self, book: &'static str, snap: Snapshot, request_ms: i64) -> Vec<Write> {
        let now = snap.recv_ms;
        let mut out = Vec::new();
        let Engine { matcher, books, known } = self;
        let bs = books.entry(book).or_default();
        bs.status.polls += 1;
        bs.status.last_ok_ms = Some(now);
        bs.status.last_request_ms = Some(request_ms);

        let mut seen: Vec<String> = Vec::with_capacity(snap.events.len());
        for ev in snap.events {
            if ev.id.is_empty() || ev.home.is_empty() || ev.away.is_empty() {
                continue;
            }
            seen.push(ev.id.clone());
            let meta = EventMeta {
                id: ev.id.clone(),
                sport: ev.sport.clone(),
                league: ev.league.clone(),
                home: ev.home.clone(),
                away: ev.away.clone(),
                start_ms: ev.start_ms,
                sr_id: ev.sr_id.clone(),
                score: ev.score.clone(),
                clock: ev.clock.clone(),
            };

            let es = match bs.events.get_mut(&ev.id) {
                Some(es) => es,
                None => {
                    let match_id = match known.get(&(book.to_string(), ev.id.clone())) {
                        Some(&id) if matcher.matches.contains_key(&id) => {
                            matcher.attach(id, book, now);
                            id
                        }
                        _ => {
                            let (id, created) = matcher.assign(book, &ev, now);
                            if created {
                                out.push(Write::Match(matcher.matches[&id].clone()));
                            }
                            id
                        }
                    };
                    known.insert((book.to_string(), ev.id.clone()), match_id);
                    out.push(Write::Event {
                        book,
                        meta: meta.clone(),
                        match_id,
                        first_seen_ms: now,
                        last_seen_ms: now,
                    });
                    bs.events.insert(
                        ev.id.clone(),
                        EventState {
                            meta: meta.clone(),
                            match_id,
                            quotes: BTreeMap::new(),
                            first_seen_ms: now,
                            last_seen_ms: now,
                            last_written_ms: now,
                            missing_polls: 0,
                        },
                    );
                    bs.events.get_mut(&ev.id).unwrap()
                }
            };
            es.meta = meta;
            es.last_seen_ms = now;
            es.missing_polls = 0;
            matcher.touch(es.match_id, now);
            if now - es.last_written_ms > 60_000 {
                es.last_written_ms = now;
                out.push(Write::Event {
                    book,
                    meta: es.meta.clone(),
                    match_id: es.match_id,
                    first_seen_ms: es.first_seen_ms,
                    last_seen_ms: now,
                });
            }

            // Diff quotes. Duplicate keys in one snapshot: last one wins.
            let mut incoming: BTreeMap<QKey, &QuoteIn> = BTreeMap::new();
            for q in &ev.quotes {
                incoming.insert((q.market, line_key(q.line), q.outcome), q);
            }
            for (k, q) in &incoming {
                let prev = es.quotes.get(k);
                let kind = match (prev, prev.and_then(|p| p.price), q.price) {
                    (None, _, Some(_)) => Some(ChangeKind::Open),
                    (None, _, None) => None, // first sighting while suspended: just remember it
                    (Some(_), Some(a), Some(b)) if (a - b).abs() > 1e-9 => Some(ChangeKind::Move),
                    (Some(_), Some(_), None) => Some(ChangeKind::Suspend),
                    (Some(_), None, Some(_)) => Some(ChangeKind::Resume),
                    _ => None,
                };
                if let Some(kind) = kind {
                    out.push(Write::Change(Change {
                        ts_ms: now,
                        book,
                        book_event_id: ev.id.clone(),
                        match_id: es.match_id,
                        market: k.0,
                        line: line_from_key(k.1),
                        outcome: k.2,
                        kind,
                        old_price: prev.and_then(|p| p.price),
                        new_price: q.price,
                        book_ts_ms: q.book_ts,
                    }));
                }
                let updated_ms = if kind.is_some() || prev.is_none() { now } else { prev.unwrap().updated_ms };
                let prev_price = match (kind, prev) {
                    (Some(_), Some(p)) => p.price.or(p.prev),
                    (None, Some(p)) => p.prev,
                    _ => None,
                };
                es.quotes.insert(*k, QuoteState { price: q.price, prev: prev_price, updated_ms, book_ts: q.book_ts });
            }
            // Lines the book stopped listing.
            let gone: Vec<QKey> = es.quotes.keys().filter(|k| !incoming.contains_key(k)).copied().collect();
            for k in gone {
                let prev = es.quotes.remove(&k).unwrap();
                if prev.price.is_some() {
                    out.push(Write::Change(Change {
                        ts_ms: now,
                        book,
                        book_event_id: ev.id.clone(),
                        match_id: es.match_id,
                        market: k.0,
                        line: line_from_key(k.1),
                        outcome: k.2,
                        kind: ChangeKind::Close,
                        old_price: prev.price,
                        new_price: None,
                        book_ts_ms: None,
                    }));
                }
            }
        }

        // Events that dropped out of the live list.
        seen.sort();
        let mut finished = Vec::new();
        for (id, es) in bs.events.iter_mut() {
            if seen.binary_search(id).is_ok() {
                continue;
            }
            es.missing_polls += 1;
            if es.missing_polls >= MISSING_POLLS && now - es.last_seen_ms >= MISSING_MS {
                finished.push(id.clone());
            }
        }
        for id in finished {
            let es = bs.events.remove(&id).unwrap();
            for (k, q) in es.quotes {
                if q.price.is_some() {
                    out.push(Write::Change(Change {
                        ts_ms: now,
                        book,
                        book_event_id: id.clone(),
                        match_id: es.match_id,
                        market: k.0,
                        line: line_from_key(k.1),
                        outcome: k.2,
                        kind: ChangeKind::Close,
                        old_price: q.price,
                        new_price: None,
                        book_ts_ms: None,
                    }));
                }
            }
            matcher.detach(es.match_id, book);
            out.push(Write::Event {
                book,
                meta: es.meta,
                match_id: es.match_id,
                first_seen_ms: es.first_seen_ms,
                last_seen_ms: es.last_seen_ms,
            });
        }

        let n_changes = out.iter().filter(|w| matches!(w, Write::Change(_))).count() as u64;
        bs.status.changes_since_start += n_changes;
        if n_changes > 0 {
            bs.status.last_change_ms = Some(now);
        }
        bs.status.live_events = bs.events.len();
        bs.status.live_quotes = bs.events.values().map(|e| e.quotes.values().filter(|q| q.price.is_some()).count()).sum();
        matcher.prune(now);
        out
    }

    /// Move every book event of match `from` onto match `into`.
    pub fn merge(&mut self, into: i64, from: i64) -> Result<Vec<Write>, String> {
        if into == from {
            return Err("same match".into());
        }
        let (Some(a), Some(b)) = (self.matcher.matches.get(&into), self.matcher.matches.get(&from)) else {
            return Err("unknown match id (only matches still in memory can be merged)".into());
        };
        if let Some(dup) = a.books.intersection(&b.books).next() {
            return Err(format!("both matches have a live {dup} event; unlink one first"));
        }
        let now = now_ms();
        let mut out = Vec::new();
        for (book, bs) in self.books.iter_mut() {
            for es in bs.events.values_mut() {
                if es.match_id == from {
                    es.match_id = into;
                    self.known.insert((book.to_string(), es.meta.id.clone()), into);
                    self.matcher.detach(from, book);
                    self.matcher.attach(into, book, now);
                    out.push(Write::Event {
                        book,
                        meta: es.meta.clone(),
                        match_id: into,
                        first_seen_ms: es.first_seen_ms,
                        last_seen_ms: es.last_seen_ms,
                    });
                }
            }
        }
        Ok(out)
    }

    /// Split one book event off into a match of its own.
    pub fn unlink(&mut self, book: &str, event_id: &str) -> Result<(i64, Vec<Write>), String> {
        let now = now_ms();
        let Some((&bname, bs)) = self.books.iter_mut().find(|(n, _)| **n == book) else {
            return Err("unknown book".into());
        };
        let Some(es) = bs.events.get_mut(event_id) else {
            return Err("event not live".into());
        };
        let ev = EventIn {
            id: es.meta.id.clone(),
            sport: es.meta.sport.clone(),
            league: es.meta.league.clone(),
            home: es.meta.home.clone(),
            away: es.meta.away.clone(),
            start_ms: es.meta.start_ms,
            sr_id: None,
            ..Default::default()
        };
        self.matcher.detach(es.match_id, bname);
        let id = self.matcher.alloc_id();
        let mut info = Matcher::new_match_info(id, &ev, now);
        info.books.insert(bname);
        self.matcher.restore(info);
        es.match_id = id;
        self.known.insert((book.to_string(), event_id.to_string()), id);
        let out = vec![
            Write::Match(self.matcher.matches[&id].clone()),
            Write::Event {
                book: bname,
                meta: es.meta.clone(),
                match_id: id,
                first_seen_ms: es.first_seen_ms,
                last_seen_ms: es.last_seen_ms,
            },
        ];
        Ok((id, out))
    }
}
