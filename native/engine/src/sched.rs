//! A queue of units of work shared by the GPUs and the CPU of a computer: one queue, the GPU
//! workers taking from its front and the CPU threads from its back, so that each device works at
//! its own pace and the split between them is decided by their speeds, not in advance (the
//! calibration pass of the design is left out on purpose: the queue needs none). A worker whose
//! unit fails is retired and its unit goes back to the queue for the others - a lost GPU gives
//! its units back - with a line in the log; with units left and no worker alive the run is an
//! error; `cancel` set stops every worker after its unit. The results come back in the order of
//! the units given, whatever the threads did, so a run is reproducible whichever device did
//! which unit.
//!
//! Test hook: with the environment variable `PPG_TEST_GPU_FAIL_AFTER` set to N (read once, at the
//! start of [`run`]), every GPU worker is retired at its N-th unit as if that unit had failed,
//! without running it, with the error text `PPG_TEST_GPU_FAIL_AFTER` in the log - the way a test
//! loses a GPU in the middle of a step on a computer whose GPU works.

use std::collections::VecDeque;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::thread;

/// A worker of one device: `run` does one unit, an `Err` retires the worker (its unit goes back
/// to the queue); `name` is what the log calls it.
pub trait Worker<U, R>: Send {
    fn name(&self) -> String;
    fn run(&mut self, unit: &U) -> Result<R, String>;
}

/// What makes a CPU worker, called once per CPU thread.
pub type Factory<'a, U, R> = Box<dyn Fn() -> Box<dyn Worker<U, R> + 'a> + Sync + 'a>;

/// The answer of a run that did every unit: the results in the order of the units, and the log
/// (one line per worker retired).
#[derive(Debug)]
pub struct Run<U, R> {
    pub results: Vec<(U, R)>,
    pub log: Vec<String>,
}

/// The environment variable of the test hook: the unit at which every GPU worker is retired.
pub const FAIL_AFTER_VAR: &str = "PPG_TEST_GPU_FAIL_AFTER";

/// The unit at which the test hook retires every GPU worker, when the variable is set to 1 or
/// more.
fn fail_after() -> Option<usize> {
    std::env::var(FAIL_AFTER_VAR).ok().and_then(|v| v.trim().parse::<usize>().ok()).filter(|&n| n > 0)
}

#[derive(Clone, Copy)]
enum Side {
    Front,
    Back,
}

struct Done<U, R> {
    results: Vec<Option<(U, R)>>,
    count: usize,
    log: Vec<String>,
}

struct Shared<U, R> {
    queue: Mutex<VecDeque<(usize, U)>>,
    done: Mutex<Done<U, R>>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The text of a panic's payload.
fn panic_text(payload: &(dyn std::any::Any + Send)) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        (*s).to_owned()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "a panic without a message".to_owned()
    }
}

/// One worker serving the queue from its `side` until the queue is empty, `cancel` is set, or a
/// unit fails (the unit goes back, the worker leaves). A panic in `run` counts as a failure of
/// the unit: the worker is retired, the run goes on.
fn serve<U, R>(worker: &mut dyn Worker<U, R>, side: Side, shared: &Shared<U, R>, total: usize, cancel: &AtomicBool,
               progress: &(dyn Fn(usize, usize) + Sync), fail_after: Option<usize>) {
    let mut served = 0;
    loop {
        if cancel.load(Ordering::SeqCst) {
            return;
        }
        let taken = {
            let mut queue = lock(&shared.queue);
            match side {
                Side::Front => queue.pop_front(),
                Side::Back => queue.pop_back(),
            }
        };
        let Some((i, unit)) = taken else { return };
        served += 1;
        let outcome = if fail_after == Some(served) {
            Err(FAIL_AFTER_VAR.to_owned())
        } else {
            match catch_unwind(AssertUnwindSafe(|| worker.run(&unit))) {
                Ok(outcome) => outcome,
                Err(payload) => Err(format!("panicked: {}", panic_text(&*payload))),
            }
        };
        match outcome {
            Ok(result) => {
                // the progress callback is called under the lock: one call at a time
                let mut done = lock(&shared.done);
                done.results[i] = Some((unit, result));
                done.count += 1;
                progress(done.count, total);
            }
            Err(problem) => {
                {
                    let mut queue = lock(&shared.queue);
                    match side {
                        Side::Front => queue.push_front((i, unit)),
                        Side::Back => queue.push_back((i, unit)),
                    }
                }
                let line = format!("{}: unit {} of {total} failed ({problem}); the worker is retired and the unit \
                                    goes back to the queue", worker.name(), i + 1);
                lock(&shared.done).log.push(line);
                return;
            }
        }
    }
}

/// Every unit of `units` done once by one of the workers: the GPU workers `gpus` (one thread
/// each, taking from the front of the queue) and `cpu_threads` CPU workers made by `cpu` (one
/// per thread, taking from the back). `progress(done, total)` is called at the start with
/// `(0, total)` and after every unit, one call at a time. The results come back in the order of
/// `units`. Errors, with the log of the run so far: `"cancelled"` when `cancel` was set and
/// units are left; "no worker left" when every worker was retired with units left; "no worker"
/// when there is none to start with.
pub fn run<'a, U: Send + 'a, R: Send + 'a>(
    units: Vec<U>, gpus: Vec<Box<dyn Worker<U, R> + 'a>>, cpu: Option<Factory<'a, U, R>>, cpu_threads: usize,
    cancel: &AtomicBool, progress: &(dyn Fn(usize, usize) + Sync),
) -> Result<Run<U, R>, (String, Vec<String>)> {
    let total = units.len();
    if total == 0 {
        return Ok(Run { results: Vec::new(), log: Vec::new() });
    }
    let cpu_threads = if cpu.is_some() { cpu_threads } else { 0 };
    if gpus.is_empty() && cpu_threads == 0 {
        return Err((format!("no worker for {total} units: no GPU worker and no CPU worker"), Vec::new()));
    }
    if cancel.load(Ordering::SeqCst) {
        return Err(("cancelled".to_owned(), Vec::new()));
    }
    let fail_after = fail_after();
    progress(0, total);
    let shared = Shared {
        queue: Mutex::new(units.into_iter().enumerate().collect()),
        done: Mutex::new(Done { results: (0..total).map(|_| None).collect(), count: 0, log: Vec::new() }),
    };
    thread::scope(|s| {
        for mut worker in gpus {
            let shared = &shared;
            s.spawn(move || serve(&mut *worker, Side::Front, shared, total, cancel, progress, fail_after));
        }
        if let Some(factory) = &cpu {
            for _ in 0..cpu_threads {
                let shared = &shared;
                s.spawn(move || {
                    let mut worker = factory();
                    serve(&mut *worker, Side::Back, shared, total, cancel, progress, None);
                });
            }
        }
    });
    let left = shared.queue.into_inner().unwrap_or_else(PoisonError::into_inner);
    let done = shared.done.into_inner().unwrap_or_else(PoisonError::into_inner);
    if !left.is_empty() {
        let reason = if cancel.load(Ordering::SeqCst) {
            "cancelled".to_owned()
        } else {
            format!("no worker left: {} of {total} units not done", left.len())
        };
        return Err((reason, done.log));
    }
    let results =
        done.results.into_iter().map(|r| r.expect("every unit has its result once the queue is empty")).collect();
    Ok(Run { results, log: done.log })
}
