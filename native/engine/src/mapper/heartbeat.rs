//! A stage of the mapper run on a thread of its own while the calling thread beats: the progress of
//! the bindings, given at a fixed interval whatever the stage is doing (the relative poses of all
//! the pairs, the averaging and the union-find have no steps of their own to report from), and the
//! stop flag of the stage raised when a beat says stop.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, RecvTimeoutError};
use std::time::Duration;

/// `stage` run on a scoped thread while the calling thread calls `beat(false)` at once, then each
/// time `interval` passes without the stage ending, and `beat(true)` once when it has ended. When a
/// beat returns false, `stop` is raised (the stage reads it and ends) and `beat` is not called
/// again. What the stage returns; a panic of the stage comes out on the calling thread.
pub fn run_with_heartbeat<R: Send>(interval: Duration, stop: &AtomicBool, stage: impl FnOnce() -> R + Send,
                                   beat: &mut dyn FnMut(bool) -> bool) -> R {
    std::thread::scope(|scope| {
        let (done, ended) = mpsc::channel::<()>();
        let worker = scope.spawn(move || {
            let out = stage();
            let _ = done.send(());
            out
        });
        let mut beating = true;
        let mut call = |finished: bool, beating: &mut bool| {
            if *beating && !beat(finished) {
                *beating = false;
                stop.store(true, Ordering::SeqCst);
            }
        };
        call(false, &mut beating);
        loop {
            match ended.recv_timeout(interval) {
                Err(RecvTimeoutError::Timeout) => call(false, &mut beating),
                // ended, or panicked (the sender dropped without a message)
                Ok(()) | Err(RecvTimeoutError::Disconnected) => break,
            }
        }
        let out = match worker.join() {
            Ok(out) => out,
            Err(payload) => std::panic::resume_unwind(payload),
        };
        call(true, &mut beating);
        out
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    /// A stage that works `length` in small steps unless `stop` is raised; how long it worked.
    fn working(length: Duration, stop: &AtomicBool) -> Duration {
        let clock = Instant::now();
        while clock.elapsed() < length && !stop.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(2));
        }
        clock.elapsed()
    }

    #[test]
    fn beats_come_at_the_interval_through_a_long_stage() {
        let stop = AtomicBool::new(false);
        let mut beats: Vec<(Instant, bool)> = Vec::new();
        let start = Instant::now();
        let worked = run_with_heartbeat(Duration::from_millis(40), &stop, || working(Duration::from_millis(500), &stop),
                                        &mut |finished| {
                                            beats.push((Instant::now(), finished));
                                            true
                                        });
        assert!(worked >= Duration::from_millis(500) && !stop.load(Ordering::SeqCst));
        assert!(beats.len() >= 8, "{} beats in {:?}", beats.len(), start.elapsed());
        assert!(beats[0].0 - start < Duration::from_millis(20), "the first beat at once");
        let finished: Vec<bool> = beats.iter().map(|b| b.1).collect();
        assert_eq!(finished.iter().filter(|&&f| f).count(), 1);
        assert!(finished.last().unwrap(), "the last beat after the stage ended");
        for w in beats.windows(2) {
            assert!(w[1].0 - w[0].0 < Duration::from_millis(200), "a gap of {:?}", w[1].0 - w[0].0);
        }
    }

    #[test]
    fn a_beat_that_says_stop_ends_the_stage() {
        let stop = AtomicBool::new(false);
        let mut calls = 0;
        let worked = run_with_heartbeat(Duration::from_millis(30), &stop, || working(Duration::from_secs(10), &stop),
                                        &mut |_| {
                                            calls += 1;
                                            calls < 3
                                        });
        assert!(stop.load(Ordering::SeqCst));
        assert_eq!(calls, 3, "not called again once it said stop");
        assert!(worked < Duration::from_secs(1), "the stage ended {worked:?} after it began");
    }

    #[test]
    fn a_quick_stage_beats_twice() {
        let stop = AtomicBool::new(false);
        let mut beats = Vec::new();
        let out = run_with_heartbeat(Duration::from_secs(5), &stop, || 7, &mut |finished| {
            beats.push(finished);
            true
        });
        assert_eq!((out, beats), (7, vec![false, true]));
    }

    #[test]
    fn a_panic_of_the_stage_comes_out() {
        let stop = AtomicBool::new(false);
        let caught = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            run_with_heartbeat(Duration::from_millis(10), &stop, || -> usize { panic!("broken stage") }, &mut |_| true)
        }));
        assert!(caught.is_err());
    }
}
