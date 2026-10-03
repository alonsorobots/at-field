//! When a near-limit warning on /health deserves a "Getting hot" pop-up (0.4.19).
//!
//! Kept free of Tauri so the decision is unit-testable: the poll loop asks
//! `should_fire` every 2 s with whatever `last_warning` /health returned.
//!   * the first observation is a BASELINE -- a warning from before the tray
//!     started is history, not news (same rule as the kill pop-up);
//!   * a newer `at` fires once; the same `at` seen again does not;
//!   * at most one pop-up per rule per `min_gap_s`, whatever the service's
//!     own cooldown says -- a mis-set cooldown must not turn into toast spam.

use std::collections::HashMap;

pub const WARN_MIN_GAP_S: f64 = 600.0;

pub struct WarnGate {
    seen_at: Option<f64>,
    last_fired: HashMap<String, f64>,
    min_gap_s: f64,
}

impl WarnGate {
    pub fn new(min_gap_s: f64) -> Self {
        WarnGate { seen_at: None, last_fired: HashMap::new(), min_gap_s }
    }

    /// `warning` is (at, rule) from /health `last_warning`, None when absent.
    pub fn should_fire(&mut self, warning: Option<(f64, &str)>, now: f64) -> bool {
        let Some(seen) = self.seen_at else {
            self.seen_at = Some(warning.map(|w| w.0).unwrap_or(0.0));
            return false;
        };
        let Some((at, rule)) = warning else { return false };
        if at <= seen {
            return false;
        }
        self.seen_at = Some(at);
        if let Some(prev) = self.last_fired.get(rule) {
            if now - prev < self.min_gap_s {
                return false;
            }
        }
        self.last_fired.insert(rule.to_string(), now);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_first_observation_is_a_baseline_not_a_pop_up() {
        let mut g = WarnGate::new(WARN_MIN_GAP_S);
        assert!(!g.should_fire(Some((100.0, "cpu-pkg-warm")), 1000.0));
        assert!(!g.should_fire(Some((100.0, "cpu-pkg-warm")), 1002.0));
    }

    #[test]
    fn a_new_warning_fires_once() {
        let mut g = WarnGate::new(WARN_MIN_GAP_S);
        assert!(!g.should_fire(None, 1000.0));
        assert!(g.should_fire(Some((1001.0, "cpu-pkg-warm")), 1002.0));
        assert!(!g.should_fire(Some((1001.0, "cpu-pkg-warm")), 1004.0));
    }

    #[test]
    fn one_pop_up_per_rule_per_gap_but_rules_are_independent() {
        let mut g = WarnGate::new(WARN_MIN_GAP_S);
        g.should_fire(None, 0.0);
        assert!(g.should_fire(Some((10.0, "cpu-pkg-warm")), 10.0));
        assert!(!g.should_fire(Some((20.0, "cpu-pkg-warm")), 20.0), "same rule inside the gap");
        assert!(g.should_fire(Some((30.0, "ram-high")), 30.0), "another rule is not held back");
        assert!(g.should_fire(Some((700.0, "cpu-pkg-warm")), 700.0), "the gap has passed");
    }

    #[test]
    fn an_older_warning_never_fires() {
        let mut g = WarnGate::new(WARN_MIN_GAP_S);
        g.should_fire(Some((500.0, "a")), 500.0);
        assert!(!g.should_fire(Some((400.0, "a")), 900.0));
    }
}
