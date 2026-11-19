//! The features step of the native engine: the photos decoded and reduced in a pool of reader
//! threads (`image::read_grey` - a JPEG at the DCT scale its reduction allows - then
//! `image::rescale_as_colmap` to the size COLMAP reduces it to), their SIFT key points and
//! descriptors extracted on the GPUs and the CPU together (`gpu_sift`, `sift_cpu`, through the
//! queue of `sched`), brought to the pixels of the full photo with COLMAP's centre (0.5 added,
//! then the factor of the reduction, the scale by the mean factor, as `sfm._extract_features_gpu`
//! does it), the points under a mask dropped, and written to the feature store (`store`), one
//! file per photo, so that a step interrupted keeps what it did. With a small store given, the
//! same decoding also gives the preselection image of each photo (`PhotoJob::small_limit`),
//! extracted with its own cap of points into that store.
//!
//! The readers run ahead of the workers from both ends of the queue (the GPUs take from the
//! front, the CPU from the back): at most `readers` decoded photos wait ready at each end, so no
//! more than `2 * readers` of them are in memory beyond those the workers hold, whatever the
//! number of photos. A photo the decoders of `image` do not read is `unsupported` (Python decodes
//! it with OpenCV and gives the pixels to [`extract_pixels`]); one that cannot be read at all -
//! damaged, missing, unreadable - is `unreadable`; one reduced below the 8 x 8 pixels the kernels
//! take is `unsupported` too. A mask (`image::read_mask`) is applied only when it is of the size
//! of the full photo, as the classic step applies it: a missing mask is no mask, one that cannot
//! be read or is of another size is logged and not applied.
//!
//! What ends the step: a worker whose kernel fails is retired by the scheduler and its photo goes
//! to the others (a lost GPU); a store that cannot be written (a full disk) is the step's failure,
//! not a device's - the first such error stops every worker after its photo and is the error of
//! the step; options the kernels refuse are refused before any work. The readers are released
//! whatever ends the step, a panic of the progress callback included: the panic propagates to
//! the caller instead of hanging the step.

use std::io;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, Sender};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError};
use std::thread;
use std::time::Instant;

use crate::gpu_sift::{self, Options, Sift, DESC};
use crate::image::{colmap_size, read_grey, read_mask, rescale_as_colmap, Grey, ReadError};
use crate::sched::{self, Worker};
use crate::sift_cpu;
use crate::store::{ExtractParams, FeatureStore, Impl, PhotoFeatures};

/// One photo to extract: its name in the project (the key of the store), its file (`None` when
/// the pixels are given to [`extract_pixels`]), its mask when it has one, the limit on the long
/// side COLMAP reduces it to (`max_image_size`, 0 for none) and, when a small store is filled,
/// the size of its preselection image.
#[derive(Clone, Debug)]
pub struct PhotoJob {
    pub name: String,
    pub path: Option<PathBuf>,
    pub mask: Option<PathBuf>,
    pub limit: usize,
    pub small_limit: Option<usize>,
}

/// The options of the step: the SIFT options (with the cap of points of the full-size
/// extraction), whether a JPEG is decoded at a DCT scale, the reader threads, the CPU worker
/// threads (0 for none while a GPU works; at least one when no GPU does).
#[derive(Clone, Debug)]
pub struct FeatureOptions {
    pub sift: Options,
    pub dct: bool,
    pub readers: usize,
    pub cpu_threads: usize,
}

impl Default for FeatureOptions {
    fn default() -> Self {
        FeatureOptions { sift: Options::default(), dct: true, readers: 2, cpu_threads: 1 }
    }
}

/// The seconds spent by phase, summed over the threads that did the work (thread-seconds, not
/// wall time).
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct PhaseSeconds {
    pub decode: f64,
    pub sift: f64,
    pub write: f64,
}

/// What the step did: the photos whose features are in the store, those no decoder of the
/// engine reads at all (`unreadable`) and those Python decodes (`unsupported`), by name; how
/// many each kind of worker did (`by_impl`: GPU, CPU); the log (workers retired, masks not
/// applied, photos skipped); the key points stored in all and the seconds by phase.
#[derive(Clone, Debug, Default)]
pub struct FeatureReport {
    pub done: usize,
    pub unreadable: Vec<String>,
    pub unsupported: Vec<String>,
    pub by_impl: (usize, usize),
    pub log: Vec<String>,
    pub points: usize,
    pub seconds: PhaseSeconds,
}

/// Why a photo gave no features.
#[derive(Clone, Debug)]
enum Problem {
    Unreadable(String),
    Unsupported(String),
}

/// Why a unit was not done: the device's fault (its worker is retired, the unit goes to the
/// others) or the step's (it ends, with this as its error).
enum Failure {
    Device(String),
    Fatal(String),
}

/// A photo as the readers leave it for the workers: reduced to the size of its extraction (and
/// of its preselection image), with its mask when applied.
struct Decoded {
    reduced: Grey,
    small: Option<Grey>,
    full: (usize, usize),
    mask: Option<Grey>,
    seconds: f64,
}

enum Slot {
    Waiting,
    Requested,
    Ready(Arc<Decoded>),
    Failed(Problem),
    Done,
}

struct Shelves {
    slots: Vec<Slot>,
    /// A unit a worker holds: not counted in the windows of the readers.
    taken: Vec<bool>,
    /// The first slot that may not be done, and one past the last.
    lo: usize,
    hi: usize,
}

/// Where the readers leave the decoded photos for the workers: a slot per unit, the requests to
/// the readers sent for the `window` next units at each end of the queue that no worker holds.
struct Pantry {
    state: Mutex<Shelves>,
    changed: Condvar,
    requests: Mutex<Option<Sender<usize>>>,
    window: usize,
    /// Set when the step ends or stops: no request is made any more and the readers decode
    /// nothing more.
    stopped: AtomicBool,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

impl Pantry {
    fn new(n: usize, window: usize, requests: Sender<usize>) -> Pantry {
        let slots = (0..n).map(|_| Slot::Waiting).collect();
        Pantry { state: Mutex::new(Shelves { slots, taken: vec![false; n], lo: 0, hi: n }), changed: Condvar::new(),
                 requests: Mutex::new(Some(requests)), window, stopped: AtomicBool::new(false) }
    }

    /// A pantry without readers: its `n` units are delivered by the caller.
    fn unfed(n: usize) -> Pantry {
        let slots = (0..n).map(|_| Slot::Waiting).collect();
        Pantry { state: Mutex::new(Shelves { slots, taken: vec![false; n], lo: 0, hi: n }), changed: Condvar::new(),
                 requests: Mutex::new(None), window: 0, stopped: AtomicBool::new(false) }
    }

    fn is_stopped(&self) -> bool {
        self.stopped.load(Ordering::SeqCst)
    }

    /// The decoding of unit `i` asked of the readers.
    fn request(&self, shelves: &mut Shelves, i: usize) {
        if let Some(tx) = &*lock(&self.requests) {
            if tx.send(i).is_ok() {
                shelves.slots[i] = Slot::Requested;
                return;
            }
        }
        shelves.slots[i] = Slot::Failed(Problem::Unreadable("no reader thread is left to decode the photo".to_owned()));
    }

    /// The requests for the next `window` units at each end that are neither done nor held;
    /// none once the step stopped.
    fn refill(&self, shelves: &mut Shelves) {
        if self.is_stopped() {
            return;
        }
        let n = shelves.slots.len();
        while shelves.lo < n && matches!(shelves.slots[shelves.lo], Slot::Done) {
            shelves.lo += 1;
        }
        while shelves.hi > shelves.lo && matches!(shelves.slots[shelves.hi - 1], Slot::Done) {
            shelves.hi -= 1;
        }
        let (lo, hi) = (shelves.lo, shelves.hi);
        let mut seen = 0;
        let mut i = lo;
        while i < hi && seen < self.window {
            if !shelves.taken[i] && !matches!(shelves.slots[i], Slot::Done) {
                if matches!(shelves.slots[i], Slot::Waiting) {
                    self.request(shelves, i);
                }
                seen += 1;
            }
            i += 1;
        }
        let mut seen = 0;
        let mut i = hi;
        while i > lo && seen < self.window {
            i -= 1;
            if !shelves.taken[i] && !matches!(shelves.slots[i], Slot::Done) {
                if matches!(shelves.slots[i], Slot::Waiting) {
                    self.request(shelves, i);
                }
                seen += 1;
            }
        }
    }

    /// The decoded photo of unit `i` for the worker that took it, decoded now when the readers
    /// had not reached it; the readers are sent after the next units at both ends.
    fn fetch(&self, i: usize) -> Result<Arc<Decoded>, Problem> {
        let mut shelves = lock(&self.state);
        shelves.taken[i] = true;
        if matches!(shelves.slots[i], Slot::Waiting) {
            self.request(&mut shelves, i);
        }
        self.refill(&mut shelves);
        loop {
            match &shelves.slots[i] {
                Slot::Ready(decoded) => return Ok(decoded.clone()),
                Slot::Failed(problem) => return Err(problem.clone()),
                Slot::Done => return Err(Problem::Unreadable("the unit was done already".to_owned())),
                Slot::Waiting | Slot::Requested => {
                    shelves = self.changed.wait(shelves).unwrap_or_else(PoisonError::into_inner);
                }
            }
        }
    }

    /// Unit `i` is done: its pixels are dropped.
    fn done(&self, i: usize) {
        let mut shelves = lock(&self.state);
        shelves.slots[i] = Slot::Done;
        shelves.taken[i] = false;
        self.refill(&mut shelves);
    }

    /// Unit `i` goes back to the queue (its worker failed): its pixels are kept for the next.
    fn give_back(&self, i: usize) {
        let mut shelves = lock(&self.state);
        shelves.taken[i] = false;
        self.refill(&mut shelves);
    }

    /// What a reader made of unit `i`; nothing when the unit is done or failed already (a decode
    /// that came back after the step stopped).
    fn deliver(&self, i: usize, result: Result<Decoded, Problem>) {
        let mut shelves = lock(&self.state);
        if matches!(shelves.slots[i], Slot::Done | Slot::Failed(_)) {
            return;
        }
        shelves.slots[i] = match result {
            Ok(decoded) => Slot::Ready(Arc::new(decoded)),
            Err(problem) => Slot::Failed(problem),
        };
        self.changed.notify_all();
    }

    /// The step stops before its units are done: no more requests, and every worker waiting for
    /// a photo is released with a failure (the step ends in an error, so no unit is reported).
    fn halt(&self) {
        self.stopped.store(true, Ordering::SeqCst);
        let mut shelves = lock(&self.state);
        for slot in &mut shelves.slots {
            if matches!(slot, Slot::Waiting | Slot::Requested) {
                *slot = Slot::Failed(Problem::Unreadable("the step stopped before the photo was decoded".to_owned()));
            }
        }
        self.changed.notify_all();
    }

    /// No more requests: the readers end.
    fn close(&self) {
        self.stopped.store(true, Ordering::SeqCst);
        lock(&self.requests).take();
    }
}

/// Closes the pantry when dropped - at the end of the step, and while a panic unwinds through
/// it - so that the readers never wait for requests that will not come.
struct PantryOpen<'a>(&'a Pantry);

impl Drop for PantryOpen<'_> {
    fn drop(&mut self) {
        self.0.close();
    }
}

/// What the readers and the workers share.
struct Context<'a> {
    jobs: &'a [PhotoJob],
    pantry: Pantry,
    store: &'a FeatureStore,
    small: Option<(&'a FeatureStore, usize)>,
    o: &'a FeatureOptions,
    log: Mutex<Vec<String>>,
    /// The error that ends the step: the first one.
    fatal: Mutex<Option<String>>,
}

impl Context<'_> {
    fn note(&self, line: String) {
        lock(&self.log).push(line);
    }

    /// The step ends with `why` (the first call keeps its reason): the pantry stops, the workers
    /// drain what is left without work.
    fn halt(&self, why: String) {
        lock(&self.fatal).get_or_insert(why);
        self.pantry.halt();
    }

    fn fatal(&self) -> Option<String> {
        lock(&self.fatal).clone()
    }
}

fn problem_of(e: ReadError) -> Problem {
    match e {
        ReadError::Unsupported(what) => Problem::Unsupported(what),
        ReadError::Corrupt(what) => Problem::Unreadable(what),
        ReadError::Io(e) => Problem::Unreadable(e.to_string()),
    }
}

/// The mask of `job` when it is of the size `full` of the photo: a missing file is no mask, a
/// mask that is not read or is of another size is logged and not applied.
fn mask_of(job: &PhotoJob, full: (usize, usize), ctx: &Context<'_>) -> Option<Grey> {
    let path = job.mask.as_ref()?;
    match read_mask(path) {
        Ok(mask) if (mask.width, mask.height) == full => Some(mask),
        Ok(mask) => {
            let (name, w, h) = (&job.name, mask.width, mask.height);
            ctx.note(format!("{name}: the mask is {w} x {h}, the photo {} x {}: not applied", full.0, full.1));
            None
        }
        Err(ReadError::Io(e)) if e.kind() == io::ErrorKind::NotFound => None,
        Err(e) => {
            ctx.note(format!("{}: the mask is not read ({e}): not applied", job.name));
            None
        }
    }
}

/// The photo `grey` (the raster at least as large as every reduction asked for) of full size
/// `full`, reduced as the workers take it.
fn reduce(job: &PhotoJob, grey: &Grey, full: (usize, usize), mask: Option<Grey>, want_small: bool, seconds: f64)
    -> Decoded {
    let (w, h) = colmap_size(full.0, full.1, job.limit);
    let reduced = rescale_as_colmap(grey, w, h);
    let small = match job.small_limit {
        Some(limit) if want_small => {
            let (sw, sh) = colmap_size(full.0, full.1, limit);
            Some(rescale_as_colmap(grey, sw, sh))
        }
        _ => None,
    };
    Decoded { reduced, small, full, mask, seconds }
}

/// One photo read and reduced by a reader.
fn decode(job: &PhotoJob, ctx: &Context<'_>) -> Result<Decoded, Problem> {
    let started = Instant::now();
    let no_file = || Problem::Unsupported("no file: the pixels come from the caller".to_owned());
    let path = job.path.as_ref().ok_or_else(no_file)?;
    // the raster must reach the larger of the two reductions
    let min_long_side = if job.limit == 0 { 0 } else { job.limit.max(job.small_limit.unwrap_or(0)) };
    let (grey, full) = read_grey(path, min_long_side, ctx.o.dct).map_err(problem_of)?;
    let mask = mask_of(job, full, ctx);
    let decoded = reduce(job, &grey, full, mask, ctx.small.is_some(), 0.0);
    Ok(Decoded { seconds: started.elapsed().as_secs_f64(), ..decoded })
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

/// A reader thread: the units asked for, decoded in the order of the requests, until the
/// requests end or the step stops.
fn reader(requests: &Mutex<Receiver<usize>>, ctx: &Context<'_>) {
    loop {
        let next = lock(requests).recv();
        let Ok(i) = next else { return };
        if ctx.pantry.is_stopped() {
            return;
        }
        let result = catch_unwind(AssertUnwindSafe(|| decode(&ctx.jobs[i], ctx)))
            .unwrap_or_else(|payload| Err(Problem::Unreadable(format!("panicked: {}", panic_text(&*payload)))));
        ctx.pantry.deliver(i, result);
    }
}

/// The key points of the reduced image `reduced` brought to the pixels of the full photo as
/// `sfm._extract_features_gpu` brings them: COLMAP's 0.5 added, then the factor of the
/// reduction on each axis (f64, rounded to f32 as numpy rounds it), the scale by the mean of the
/// two factors; the points under `mask` (its pixel at the coordinates truncated and clamped into
/// it being 0) dropped with their descriptors.
fn to_full_photo(kp: Vec<[f32; 4]>, desc: Vec<u8>, reduced: (usize, usize), full: (usize, usize), mask: Option<&Grey>)
    -> (Vec<[f32; 4]>, Vec<u8>) {
    let (fx, fy) = (full.0 as f64 / reduced.0 as f64, full.1 as f64 / reduced.1 as f64);
    let (sx, sy, ss) = (fx as f32, fy as f32, (0.5 * (fx + fy)) as f32);
    let mut keypoints = Vec::with_capacity(kp.len());
    let mut descriptors = Vec::with_capacity(desc.len());
    for (i, k) in kp.iter().enumerate() {
        let p = [(k[0] + 0.5) * sx, (k[1] + 0.5) * sy, k[2] * ss, k[3]];
        if let Some(m) = mask {
            let cx = (p[0] as i64).clamp(0, m.width as i64 - 1) as usize;
            let cy = (p[1] as i64).clamp(0, m.height as i64 - 1) as usize;
            if m.pixels[cy * m.width + cx] == 0 {
                continue;
            }
        }
        keypoints.push(p);
        descriptors.extend_from_slice(&desc[i * DESC..(i + 1) * DESC]);
    }
    (keypoints, descriptors)
}

/// An image the kernels refuse: below 8 x 8 (a photo that small, or one reduced to it without
/// a limit).
fn too_small(image: &Grey) -> Option<Problem> {
    if image.width < 8 || image.height < 8 {
        let (w, h) = (image.width, image.height);
        Some(Problem::Unsupported(format!("reduced to {w} x {h}, below the 8 x 8 the SIFT takes")))
    } else {
        None
    }
}

enum Engine {
    Gpu { sift: Arc<Sift>, index: usize },
    Cpu,
}

/// What a worker says of a unit: the features stored, the photo skipped, or the failure that
/// ends the step (the units after it are drained without work).
enum Outcome {
    Done { implementation: Impl, points: usize, seconds: PhaseSeconds },
    Skipped(Problem),
    Fatal(String),
}

/// A worker of the step: a GPU or the CPU, extracting into the store (and the small store).
struct FeatureWorker<'a> {
    engine: Engine,
    ctx: &'a Context<'a>,
}

impl FeatureWorker<'_> {
    fn implementation(&self) -> Impl {
        match self.engine {
            Engine::Gpu { .. } => Impl::Gpu,
            Engine::Cpu => Impl::Cpu,
        }
    }

    /// The features of `image` (the photo reduced to `reduced`) extracted with `sift`, brought
    /// to the full photo and written to `store` under the name of `job`: the points kept and
    /// the seconds of the SIFT and of the write. A kernel that fails is the device's failure; a
    /// store that cannot be written is the step's.
    fn extract_into(&self, job: &PhotoJob, image: &Grey, decoded: &Decoded, limit: usize, sift: &Options,
                    store: &FeatureStore) -> Result<(usize, PhaseSeconds), Failure> {
        let (w, h) = (image.width, image.height);
        let pixels: Vec<f32> = image.pixels.iter().map(|&p| f32::from(p) / 255.0).collect();
        let started = Instant::now();
        let extracted = match &self.engine {
            Engine::Gpu { sift: kernel, .. } => kernel.extract(&pixels, w, h, sift),
            Engine::Cpu => sift_cpu::extract(&pixels, w, h, sift),
        };
        let (kp, desc) = extracted.map_err(Failure::Device)?;
        let sift_seconds = started.elapsed().as_secs_f64();
        let (keypoints, descriptors) = to_full_photo(kp, desc, (w, h), decoded.full, decoded.mask.as_ref());
        let params = ExtractParams { max_image_size: limit as u32, reduced_width: w as u32, reduced_height: h as u32,
                                     masked: decoded.mask.is_some(), ..ExtractParams::of(sift) };
        let points = keypoints.len();
        let (width, height) = (decoded.full.0 as u32, decoded.full.1 as u32);
        let features = PhotoFeatures { width, height, implementation: self.implementation(), image_id: None, params,
                                       keypoints, descriptors };
        let started = Instant::now();
        let not_written = |e: io::Error| Failure::Fatal(format!("{}: not written to the store: {e}", job.name));
        store.put(&job.name, &features).map_err(not_written)?;
        Ok((points, PhaseSeconds { decode: 0.0, sift: sift_seconds, write: started.elapsed().as_secs_f64() }))
    }

    /// The photo of `job` through the kernels and into the stores: done, or skipped when it is
    /// too small for them.
    fn process(&self, job: &PhotoJob, decoded: &Decoded) -> Result<Outcome, Failure> {
        if let Some(problem) = too_small(&decoded.reduced).or_else(|| decoded.small.as_ref().and_then(too_small)) {
            return Ok(Outcome::Skipped(problem));
        }
        let o = self.ctx.o;
        let (points, mut seconds) =
            self.extract_into(job, &decoded.reduced, decoded, job.limit, &o.sift, self.ctx.store)?;
        seconds.decode = decoded.seconds;
        let small = (self.ctx.small, &decoded.small, job.small_limit);
        if let (Some((small_store, cap)), Some(small), Some(limit)) = small {
            let options = Options { max_features: cap, ..o.sift };
            let (_, more) = self.extract_into(job, small, decoded, limit, &options, small_store)?;
            seconds.sift += more.sift;
            seconds.write += more.write;
        }
        Ok(Outcome::Done { implementation: self.implementation(), points, seconds })
    }
}

impl Worker<usize, Outcome> for FeatureWorker<'_> {
    fn name(&self) -> String {
        match &self.engine {
            Engine::Gpu { sift, index } => format!("GPU {index} ({})", sift.info.name),
            Engine::Cpu => "CPU".to_owned(),
        }
    }

    fn run(&mut self, unit: &usize) -> Result<Outcome, String> {
        let i = *unit;
        let ctx = self.ctx;
        let pantry = &ctx.pantry;
        if let Some(why) = ctx.fatal() {
            // the step is ending: the units left are drained without work
            pantry.done(i);
            return Ok(Outcome::Fatal(why));
        }
        let decoded = match pantry.fetch(i) {
            Ok(decoded) => decoded,
            Err(problem) => {
                pantry.done(i);
                return Ok(Outcome::Skipped(problem));
            }
        };
        match self.process(&ctx.jobs[i], &decoded) {
            Ok(outcome) => {
                pantry.done(i);
                Ok(outcome)
            }
            Err(Failure::Device(why)) => {
                pantry.give_back(i);
                Err(why)
            }
            Err(Failure::Fatal(why)) => {
                pantry.done(i);
                ctx.halt(why.clone());
                Ok(Outcome::Fatal(why))
            }
        }
    }
}

/// The GPU workers of the adapters `devices`; an adapter that opens no SIFT is logged and left
/// to the others.
fn gpu_workers<'a>(devices: &[usize], ctx: &'a Context<'a>, log: &mut Vec<String>)
    -> Vec<Box<dyn Worker<usize, Outcome> + 'a>> {
    let mut gpus: Vec<Box<dyn Worker<usize, Outcome> + 'a>> = Vec::new();
    for &index in devices {
        match gpu_sift::sift_for(index as i64) {
            Ok(sift) => gpus.push(Box::new(FeatureWorker { engine: Engine::Gpu { sift, index }, ctx })),
            Err(problem) => log.push(format!("GPU {index} not opened ({problem}): the other workers do its share")),
        }
    }
    gpus
}

/// The options the kernels refuse, refused before any work (a worker failing on them would be
/// retired for the step's mistake): the kernels' own check of the first octave.
fn check_options(o: &FeatureOptions) -> Result<(), String> {
    if o.sift.first_octave < -1 {
        return Err("first_octave: -1, 0 or positive".to_owned());
    }
    Ok(())
}

/// The error of a step, with the lines of its log after it.
fn failed(reason: String, lines: Vec<String>) -> String {
    if lines.is_empty() {
        reason
    } else {
        format!("{reason}\n{}", lines.join("\n"))
    }
}

/// The units run through the queue with the GPU workers `gpus` and the CPU's: the outcomes in
/// the order of the units, with the log (`log` first, then the scheduler's lines).
fn run_units<'a>(ctx: &'a Context<'a>, units: Vec<usize>, gpus: Vec<Box<dyn Worker<usize, Outcome> + 'a>>,
                 mut log: Vec<String>, cancel: &AtomicBool, progress: &(dyn Fn(usize, usize) + Sync))
    -> Result<(Vec<Outcome>, Vec<String>), String> {
    let cpu_threads = if gpus.is_empty() { ctx.o.cpu_threads.max(1) } else { ctx.o.cpu_threads };
    let cpu: Option<sched::Factory<'a, usize, Outcome>> = if cpu_threads > 0 {
        let make = move || Box::new(FeatureWorker { engine: Engine::Cpu, ctx }) as Box<dyn Worker<usize, Outcome> + 'a>;
        Some(Box::new(make))
    } else {
        None
    };
    match sched::run(units, gpus, cpu, cpu_threads, cancel, progress) {
        Ok(run) => {
            log.extend(run.log);
            Ok((run.results.into_iter().map(|(_, outcome)| outcome).collect(), log))
        }
        Err((reason, lines)) => {
            log.extend(lines);
            log.extend(std::mem::take(&mut *lock(&ctx.log)));
            Err(failed(reason, log))
        }
    }
}

/// The features of `photos` extracted into `store` (and, with `small` - a store and its cap of
/// points - the preselection features of the photos with a `small_limit` into that store) on
/// the GPUs `devices` (adapter indices) and the CPU together. `progress(done, total)` after
/// every photo; `cancel` set stops the step after the photos being done, with an error
/// `"cancelled"`. An error, with the log of the run, on a cancel, when the queue is left with
/// photos and no worker (every GPU lost and no CPU thread), when a store cannot be written (the
/// first such photo names the error, the others are not attempted) or when the options are ones
/// the kernels refuse; the photos done before it are in the store either way. A panic of
/// `progress` propagates (the readers are released first).
pub fn extract_all(photos: &[PhotoJob], store: &FeatureStore, small: Option<(&FeatureStore, usize)>, o: &FeatureOptions,
                   devices: &[usize], cancel: &AtomicBool, progress: &(dyn Fn(usize, usize) + Sync))
    -> Result<FeatureReport, String> {
    if photos.is_empty() {
        return Ok(FeatureReport::default());
    }
    check_options(o)?;
    let readers = o.readers.max(1);
    let (tx, rx) = mpsc::channel();
    let requests = Mutex::new(rx);
    let pantry = Pantry::new(photos.len(), readers, tx);
    let ctx = Context { jobs: photos, pantry, store, small, o, log: Mutex::new(Vec::new()), fatal: Mutex::new(None) };
    // the GPUs are opened before any reader thread exists: a panic of the driver here (wgpu's
    // handler of an uncaptured error panics) leaves nothing behind it
    let mut log = Vec::new();
    let gpus = gpu_workers(devices, &ctx, &mut log);
    let run = thread::scope(|s| {
        // closed when this closure ends, by its return or by a panic unwinding through it: the
        // readers then leave, and the scope joins them instead of waiting for ever
        let _open = PantryOpen(&ctx.pantry);
        for _ in 0..readers {
            s.spawn(|| reader(&requests, &ctx));
        }
        run_units(&ctx, (0..photos.len()).collect(), gpus, log, cancel, progress)
    });
    let (outcomes, log) = run?;
    if let Some(why) = ctx.fatal() {
        let mut lines = std::mem::take(&mut *lock(&ctx.log));
        lines.extend(log);
        return Err(failed(why, lines));
    }
    let mut report = FeatureReport { log: std::mem::take(&mut *lock(&ctx.log)), ..FeatureReport::default() };
    for (job, outcome) in photos.iter().zip(outcomes) {
        match outcome {
            Outcome::Done { implementation, points, seconds } => {
                report.done += 1;
                report.points += points;
                match implementation {
                    Impl::Gpu => report.by_impl.0 += 1,
                    Impl::Cpu | Impl::Imported => report.by_impl.1 += 1,
                }
                report.seconds.decode += seconds.decode;
                report.seconds.sift += seconds.sift;
                report.seconds.write += seconds.write;
            }
            Outcome::Skipped(Problem::Unreadable(why)) => {
                report.log.push(format!("{}: unreadable ({why})", job.name));
                report.unreadable.push(job.name.clone());
            }
            Outcome::Skipped(Problem::Unsupported(why)) => {
                report.log.push(format!("{}: not decoded here ({why})", job.name));
                report.unsupported.push(job.name.clone());
            }
            Outcome::Fatal(why) => return Err(failed(why, log)),
        }
    }
    report.log.extend(log);
    Ok(report)
}

/// The features of one photo given as pixels - `grey` its raster (the full photo, or a
/// reduction of it at least as large as the reductions asked for), `full` the size of the full
/// photo - extracted into `store` (and the small store) as [`extract_all`] extracts a photo it
/// reads itself: on the first GPU of `devices` that opens, else on the CPU. The implementation
/// that did it.
pub fn extract_pixels(job: &PhotoJob, grey: Grey, full: (usize, usize), store: &FeatureStore,
                      small: Option<(&FeatureStore, usize)>, o: &FeatureOptions, devices: &[usize])
    -> Result<Impl, String> {
    check_options(o)?;
    let jobs = std::slice::from_ref(job);
    let ctx = Context { jobs, pantry: Pantry::unfed(1), store, small, o, log: Mutex::new(Vec::new()),
                        fatal: Mutex::new(None) };
    let started = Instant::now();
    let mask = mask_of(job, full, &ctx);
    let decoded = reduce(job, &grey, full, mask, small.is_some(), started.elapsed().as_secs_f64());
    drop(grey);
    ctx.pantry.deliver(0, Ok(decoded));
    let mut log = Vec::new();
    let gpus = gpu_workers(devices, &ctx, &mut log);
    let (outcomes, _) = run_units(&ctx, vec![0], gpus, log, &AtomicBool::new(false), &|_, _| {})?;
    match outcomes.into_iter().next() {
        Some(Outcome::Done { implementation, .. }) => Ok(implementation),
        Some(Outcome::Skipped(Problem::Unreadable(why))) | Some(Outcome::Skipped(Problem::Unsupported(why))) => {
            Err(format!("{}: {why}", job.name))
        }
        Some(Outcome::Fatal(why)) => Err(why),
        None => Err(format!("{}: no outcome", job.name)),
    }
}
