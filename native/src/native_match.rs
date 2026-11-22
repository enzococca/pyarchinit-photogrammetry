//! The bindings of the matching step of the native engine (API 13): the features step
//! (`features::extract_all`, `features::extract_pixels`) into a feature store, the generic
//! preselection of the pairs on the small store (`preselect::run`), the two-stage matching of the
//! pairs into a match store (`pairs::match_all`), the stores read and written from Python
//! (`store`), and the photos read and reduced as COLMAP reads them (`image`, for the tests).
//!
//! Every step runs without the GIL, on the GPUs and the CPU together ([`run_native`]).
//! `progress(done, total, message)` is called with the GIL taken back at the start of a step,
//! then at most every `min_interval` seconds (0.5 unless the caller says otherwise; the GIL is
//! taken only for a call that is made, never once per unit), and with the last state when the
//! step ends: the final `done == total` at once, or the state a cancel or an error left it in. The
//! calls are made one at a time under the relay's own lock, so `done` never falls as Python sees
//! it (the two phases of the preselection share one scale), and a step with nothing to count (a
//! preselection of one photo) reports nothing, so a total of zero never reaches Python. An
//! exception the callback raises sets the cancel flag of the step, which ends after the units
//! being done (what they did is on disk), and comes out again as it was (a panic of the engine
//! after it goes with it as a note, where Python has them). An error of the engine or a panic is
//! a RuntimeError with its message - an OSError with `errno.ENOSPC` when the disk is full; what the bindings check before any work - a camera model the
//! engine does not know, an option that is not one, arrays of the wrong shape - is a ValueError;
//! a photo or a pair a store does not hold is a KeyError or None, as each reader says.
//!
//! A feature store or a match store named by a step is opened when its folder holds one and made
//! otherwise, so that a step interrupted keeps what it did and the next run adds to it; the
//! caller removes the folder to start over. The readers open the store they are given and refuse
//! one that is not there or is damaged, naming the file. The one-call readers and writers
//! (`native_store_read`, `native_match_read`, ...) open the store on every call, which checks
//! every file of it: a step that goes through every item - the export of a project to COLMAP's
//! database, the import of one - holds the store open instead, through [`NativeFeatureStore`]
//! and [`NativeMatchStore`], one handle for the whole step.

use std::collections::{HashMap, HashSet};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, MutexGuard, PoisonError};
use std::time::{Duration, Instant};

use nalgebra::{Matrix3, Quaternion, UnitQuaternion, Vector3};
use numpy::ndarray::Array2;
use numpy::{IntoPyArray, PyArray2, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::exceptions::{PyKeyError, PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use ppg_engine::camera::Camera;
use ppg_engine::features::{self, FeatureOptions, FeatureReport, PhotoJob};
use ppg_engine::geometry::two_view::{self, Config, TwoView};
use ppg_engine::gpu;
use ppg_engine::gpu_sift::{self, DESC};
use ppg_engine::image::{self, Grey};
use ppg_engine::pairs;
use ppg_engine::preselect::{self, MatchOptions, PreselectOptions};
use ppg_engine::store::{ExtractParams, FeatureHeader, FeatureStore, Impl, MatchStore, PairResult, PhotoFeatures,
                        Route, INDEX_FILE};

use crate::{panic_text, set_two_view_option, sift_arrays, two_view_into_dict};

/// A photo of [`native_features`]: `(name, path, mask, limit, small_limit)`.
type PhotoArg = (String, Option<PathBuf>, Option<PathBuf>, usize, Option<usize>);

/// A camera of a photo: `(model, params, width, height, has_prior_focal)`.
type CameraArg = (String, Vec<f64>, usize, usize, bool);

/// The fewest pixels of a side the SIFT kernels take (`gpu_sift`, `sift_cpu`).
const LEAST_SIDE: usize = 8;

/// COLMAP's three tests of the descriptor matching with the defaults of its `SiftMatchingOptions`,
/// as the classic preselection matches the small photos (pycolmap's exhaustive matching of them);
/// the kernels of every GPU built for the adapter's own workgroup memory.
const MATCHING: MatchOptions =
    MatchOptions { max_ratio: 0.8, max_distance: 0.7, cross_check: true, workgroup_bytes: None };

/// COLMAP's `max_num_matches`: the matches kept for the verification when the options name none.
const MAX_NUM_MATCHES: usize = 32768;

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    m.lock().unwrap_or_else(PoisonError::into_inner)
}

/// The seconds between two calls of a progress callback unless the caller says otherwise: the
/// classic step's own figure.
const MIN_INTERVAL: f64 = 0.5;

/// What a relay has seen of its step and given to Python.
struct Seen {
    /// The latest state of the step, `done` never falling and within `total`.
    done: usize,
    total: usize,
    /// When the callback was last called; None before the first call.
    called_at: Option<Instant>,
    /// The state the callback had last.
    delivered: Option<(usize, usize)>,
}

/// The progress callback of a step relayed to Python at most every `interval` (the first call,
/// a `done == total` and the last state when the step ends always made), under one lock, so that
/// two calls never cross and `done` never falls as Python sees it; a total of zero not reported;
/// the exception of the callback kept for the caller and the step cancelled by it (the callback
/// not called again).
struct Relay<'a> {
    callback: Option<&'a Py<PyAny>>,
    message: &'static str,
    interval: Duration,
    cancel: &'a AtomicBool,
    seen: Mutex<Seen>,
    raised: Mutex<Option<PyErr>>,
}

impl Relay<'_> {
    fn new<'a>(callback: Option<&'a Py<PyAny>>, message: &'static str, interval: Duration, cancel: &'a AtomicBool)
        -> Relay<'a> {
        let seen = Seen { done: 0, total: 0, called_at: None, delivered: None };
        Relay { callback, message, interval, cancel, seen: Mutex::new(seen), raised: Mutex::new(None) }
    }

    /// The callback called with the state of `seen`, under its lock (the GIL taken for this call
    /// only); its exception kept and the step cancelled.
    fn call(&self, seen: &mut Seen, callback: &Py<PyAny>) {
        let (done, total) = (seen.done, seen.total);
        seen.called_at = Some(Instant::now());
        seen.delivered = Some((done, total));
        Python::attach(|py| {
            if let Err(e) = callback.call1(py, (done, total, self.message)) {
                *lock(&self.raised) = Some(e);
                self.cancel.store(true, Ordering::SeqCst);
            }
        });
    }

    /// A state of the step: given to Python now when it is the first, the last (`done == total`)
    /// or `interval` has passed since the last call; kept for the next call or [`Relay::finish`]
    /// otherwise.
    fn report(&self, done: usize, total: usize) {
        let Some(callback) = self.callback else { return };
        if total == 0 || self.cancel.load(Ordering::SeqCst) {
            return;
        }
        let mut seen = lock(&self.seen);
        seen.done = seen.done.max(done.min(total));
        seen.total = total;
        let due = match seen.called_at {
            None => true,
            Some(at) => seen.done == total || at.elapsed() >= self.interval,
        };
        if due {
            self.call(&mut seen, callback);
        }
    }

    /// The last state of the step given to Python when the step ends, if the callback has not
    /// had it (a step cancelled, or ended by an error, between two calls).
    fn finish(&self) {
        let Some(callback) = self.callback else { return };
        if self.cancel.load(Ordering::SeqCst) {
            return;
        }
        let mut seen = lock(&self.seen);
        if seen.total > 0 && seen.delivered != Some((seen.done, seen.total)) {
            self.call(&mut seen, callback);
        }
    }
}

/// The seconds of `min_interval` as a duration; a negative or not finite number, or one too large
/// for a duration, is a ValueError.
fn interval(min_interval: f64) -> PyResult<Duration> {
    Duration::try_from_secs_f64(min_interval)
        .map_err(|_| PyValueError::new_err(format!("min_interval must be seconds, 0 or more, not {min_interval}")))
}

/// Python's `errno.ENOSPC`, the same number on every system Python runs on.
const ENOSPC: i32 = 28;

/// The error of a step of the engine for Python: an `OSError` with `errno.ENOSPC` when its text says
/// the disk is full (`store::storage_full`: a full disk is not a failure another engine can work
/// around), a RuntimeError otherwise.
fn engine_error(problem: String) -> PyErr {
    if ppg_engine::store::storage_full(&problem) {
        PyOSError::new_err((ENOSPC, problem))
    } else {
        PyRuntimeError::new_err(problem)
    }
}

/// A step of the engine run without the GIL: `f` gets the cancel flag of the step and its progress
/// callback (`progress` relayed by a [`Relay`] at most every `interval`, with `message` as the
/// third argument of every call). The error of the step, or a panic, is a RuntimeError with the
/// message (the error of a full disk an OSError, [`engine_error`]), unless the progress callback raised: then its exception comes out as it was, the step
/// having been cancelled for it, a panic that followed attached to it as a note.
fn run_native<T: Send>(py: Python<'_>, progress: Option<&Py<PyAny>>, message: &'static str, interval: Duration,
                       f: impl FnOnce(&AtomicBool, &(dyn Fn(usize, usize) + Sync)) -> Result<T, String> + Send)
    -> PyResult<T> {
    let cancel = AtomicBool::new(false);
    let relay = Relay::new(progress, message, interval, &cancel);
    let outcome = py.detach(|| {
        let report = |done: usize, total: usize| relay.report(done, total);
        let outcome = catch_unwind(AssertUnwindSafe(|| f(&cancel, &report)));
        relay.finish();
        outcome
    });
    let raised = lock(&relay.raised).take();
    match outcome {
        Ok(Ok(value)) => raised.map_or(Ok(value), Err),
        Ok(Err(problem)) => Err(raised.unwrap_or_else(|| engine_error(problem))),
        Err(payload) => {
            let text = format!("{message}: {}", panic_text(&*payload));
            match raised {
                Some(e) => {
                    // the user's exception is the error; the panic goes with it (add_note, Python 3.11+)
                    let _ = e.value(py).call_method1("add_note", (text,));
                    Err(e)
                }
                None => Err(PyRuntimeError::new_err(text)),
            }
        }
    }
}

/// No progress to relay: the readers and writers of the stores.
const NO_PROGRESS: Duration = Duration::ZERO;

/// The feature store in `dir`: opened when the folder holds one, made otherwise.
fn feature_store(dir: &str) -> Result<FeatureStore, String> {
    let dir = Path::new(dir);
    let store = if dir.join(INDEX_FILE).is_file() { FeatureStore::open(dir) } else { FeatureStore::create(dir) };
    store.map_err(|e| e.to_string())
}

/// The feature store in `dir`, which must be there.
fn open_feature_store(dir: &str) -> Result<FeatureStore, String> {
    FeatureStore::open(Path::new(dir)).map_err(|e| e.to_string())
}

/// The match store in `dir`: opened when the folder holds one, made otherwise.
fn match_store(dir: &str) -> Result<MatchStore, String> {
    let dir = Path::new(dir);
    let store = if dir.join(INDEX_FILE).is_file() { MatchStore::open(dir) } else { MatchStore::create(dir) };
    store.map_err(|e| e.to_string())
}

/// The cameras of the photos from `{name: (model, params, width, height, has_prior_focal)}`, and
/// the names of the photos with a prior focal length; a model the engine does not know, or too few
/// parameters for it, is a ValueError naming the photo.
fn cameras(given: HashMap<String, CameraArg>) -> PyResult<(HashMap<String, Camera>, HashSet<String>)> {
    let mut cams = HashMap::with_capacity(given.len());
    let mut calibrated = HashSet::new();
    for (name, (model, params, width, height, prior)) in given {
        let cam = Camera::new(&model, &params, width as f64, height as f64)
            .map_err(|e| PyValueError::new_err(format!("{name} ({model}): {e}")))?;
        if prior {
            calibrated.insert(name.clone());
        }
        cams.insert(name, cam);
    }
    Ok((cams, calibrated))
}

/// The devices of a step from what Python gives (`_native.gpu_devices`): -1 is the best GPU of the
/// computer as the kernels choose it (`gpu::best_adapter`; a ValueError without one), an index is
/// kept as it is (one that no adapter has is the step's to log), another negative number is a
/// ValueError; each once.
fn devices(py: Python<'_>, given: Vec<i64>) -> PyResult<Vec<usize>> {
    let best = if given.contains(&-1) { py.detach(gpu::best_adapter) } else { None };
    let mut out = Vec::with_capacity(given.len());
    for d in given {
        let index = if d == -1 {
            best.ok_or_else(|| PyValueError::new_err("device -1 (the best GPU): no GPU adapter on this computer"))?
        } else {
            let not_a_device = |_| PyValueError::new_err(format!("device {d}: -1 (the best GPU) or an adapter index"));
            usize::try_from(d).map_err(not_a_device)?
        };
        if !out.contains(&index) {
            out.push(index);
        }
    }
    Ok(out)
}

/// The SIFT options of the step: COLMAP's defaults with the cap of points asked for.
fn sift_options(max_features: usize) -> gpu_sift::Options {
    gpu_sift::Options { max_features, ..gpu_sift::Options::default() }
}

/// The result of the features step as a dict: ``done``, ``unreadable`` and ``unsupported`` (the
/// names), ``gpu`` and ``cpu`` (the photos each did), ``log``.
fn features_dict<'py>(py: Python<'py>, r: &FeatureReport) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("done", r.done)?;
    d.set_item("unreadable", &r.unreadable)?;
    d.set_item("unsupported", &r.unsupported)?;
    d.set_item("gpu", r.by_impl.0)?;
    d.set_item("cpu", r.by_impl.1)?;
    d.set_item("log", &r.log)?;
    Ok(d)
}

/// The features of ``photos`` - each ``(name, path, mask, limit, small_limit)``: the name of the photo in
/// the project (the key of the store), its file, its mask or None, the limit on its long side
/// (``max_image_size``, 0 for none) and the size of its preselection image or None - extracted into the
/// feature store ``store`` with at most ``max_features`` points each and, with ``small_store``, into that
/// store at the preselection size with at most ``small_max_features`` points, on the GPUs ``devices``
/// (adapter indices, -1 the best GPU) and the CPU together (``cpu_share``: a CPU worker beside the GPUs;
/// without a GPU the CPU does every photo whatever it says); ``dct`` decodes a JPEG at the DCT scale its
/// reduction allows, ``readers`` is the number of decoding threads. ``progress(done, total, message)``
/// counts the photos: at the start, then at most every ``min_interval`` seconds, and with the last state
/// when the step ends. Returns ``done``, ``unreadable`` (files no decoder reads), ``unsupported`` (files
/// this engine does not decode, to be given as pixels to :func:`native_features_pixels`, and photos too
/// small for the kernels), ``gpu``, ``cpu`` (the photos each did) and ``log``. The GIL is released
/// (`features::extract_all`).
#[pyfunction]
#[pyo3(signature = (store, small_store, photos, max_features, small_max_features, devices, dct=true, cpu_share=true,
                    readers=2, progress=None, min_interval=MIN_INTERVAL))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn native_features<'py>(py: Python<'py>, store: &str, small_store: Option<&str>, photos: Vec<PhotoArg>,
                                   max_features: usize, small_max_features: usize, devices: Vec<i64>, dct: bool,
                                   cpu_share: bool, readers: usize, progress: Option<Py<PyAny>>, min_interval: f64)
    -> PyResult<Bound<'py, PyDict>> {
    let interval = self::interval(min_interval)?;
    let devices = self::devices(py, devices)?;
    let jobs: Vec<PhotoJob> = photos
        .into_iter()
        .map(|(name, path, mask, limit, small_limit)| PhotoJob { name, path, mask, limit, small_limit })
        .collect();
    // the CPU SIFT runs on the threads of rayon by itself: one CPU worker is its share
    let o = FeatureOptions { sift: sift_options(max_features), dct, readers, cpu_threads: usize::from(cpu_share) };
    let report = run_native(py, progress.as_ref(), "extracting features", interval, |cancel, progress| {
        let store = feature_store(store)?;
        let small = small_store.map(feature_store).transpose()?;
        let small = small.as_ref().map(|s| (s, small_max_features));
        features::extract_all(&jobs, &store, small, &o, &devices, cancel, progress)
    })?;
    features_dict(py, &report)
}

/// The sizes a photo of full size `full` is reduced to by the step: under `limit`, and under
/// `small_limit` when a small store is filled (`features::reduce`).
fn reductions(full: (usize, usize), limit: usize, small_limit: Option<usize>) -> Vec<(usize, usize)> {
    std::iter::once(limit).chain(small_limit).map(|cap| image::colmap_size(full.0, full.1, cap)).collect()
}

/// Why a photo of full size `full` reduced under `limit` (and, when a small store is filled, under
/// `small_limit`) is refused by the kernels: a reduction below [`LEAST_SIDE`] on a side, worded as
/// the features step words it; `None` when it is large enough.
fn too_small(full: (usize, usize), limit: usize, small_limit: Option<usize>) -> Option<String> {
    let small = |&(w, h): &(usize, usize)| w < LEAST_SIDE || h < LEAST_SIDE;
    let (w, h) = reductions(full, limit, small_limit).into_iter().find(small)?;
    Some(format!("reduced to {w} x {h}, below the {LEAST_SIDE} x {LEAST_SIDE} the SIFT takes"))
}

/// The pixels given for a photo of full size `full` checked against the reductions asked of it: a
/// raster smaller than one of them (which the filter would stretch into features of nothing) is
/// a ValueError naming the two sizes.
fn check_pixels(name: &str, given: (usize, usize), full: (usize, usize), limit: usize, small_limit: Option<usize>)
    -> PyResult<()> {
    let larger = |&(w, h): &(usize, usize)| given.0 < w || given.1 < h;
    match reductions(full, limit, small_limit).into_iter().find(larger) {
        Some((w, h)) => Err(PyValueError::new_err(format!(
            "{name}: the pixels given are {} x {}, the reduction asked for is {w} x {h}: give the full photo, or a \
             reduction at least that large", given.0, given.1))),
        None => Ok(()),
    }
}

/// The features of one photo given as pixels - ``grey`` its raster as a uint8 array (the full photo,
/// or a reduction of it at least as large as the reductions asked for: a smaller one is a ValueError),
/// ``full_size`` ``(width, height)`` of the full photo - extracted into ``store`` (and ``small_store``) as
/// :func:`native_features` extracts a photo it reads itself, on the first GPU of ``devices`` that opens,
/// else on the CPU, with ``mask`` (a file, or None) applied as the step applies it: for a file the step
/// reported ``unsupported`` and Python decoded. Returns the dict of :func:`native_features` for this one
/// photo, with ``points`` (the key points stored, 0 when it was not): a photo too small for the kernels is
/// ``unsupported``, never an error. The GIL is released (`features::extract_pixels`).
#[pyfunction]
#[pyo3(signature = (store, small_store, name, grey, full_size, limit, small_limit, max_features, small_max_features,
                    devices, mask=None))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn native_features_pixels<'py>(py: Python<'py>, store: &str, small_store: Option<&str>, name: String,
                                          grey: PyReadonlyArray2<'py, u8>, full_size: (usize, usize), limit: usize,
                                          small_limit: Option<usize>, max_features: usize, small_max_features: usize,
                                          devices: Vec<i64>, mask: Option<PathBuf>) -> PyResult<Bound<'py, PyDict>> {
    let devices = self::devices(py, devices)?;
    let a = grey.as_array();
    let (height, width) = a.dim();
    let small_limit_wanted = small_limit.filter(|_| small_store.is_some());
    check_pixels(&name, (width, height), full_size, limit, small_limit_wanted)?;
    let pixels = Grey { width, height, pixels: a.iter().copied().collect() };
    // the mask, when given, is the one of `native_features`: read by its path, applied when its size is the photo's
    let job = PhotoJob { name: name.clone(), path: None, mask, limit, small_limit };
    // the GPU takes the photo when one opens, the CPU otherwise (the step makes a CPU worker then)
    let o = FeatureOptions { sift: sift_options(max_features), dct: true, readers: 1, cpu_threads: 0 };
    let refused = too_small(full_size, limit, small_limit_wanted);
    let outcome = run_native(py, None, "extracting features", NO_PROGRESS, |_, _| match refused {
        Some(why) => Ok(Err(why)),
        None => {
            let store = feature_store(store)?;
            let small = small_store.map(feature_store).transpose()?;
            let small = small.as_ref().map(|s| (s, small_max_features));
            let implementation = features::extract_pixels(&job, pixels, full_size, &store, small, &o, &devices)?;
            Ok(Ok((implementation, store.points(&job.name).unwrap_or(0) as usize)))
        }
    })?;
    let mut report = FeatureReport::default();
    match outcome {
        Ok((implementation, points)) => {
            report.done = 1;
            report.points = points;
            match implementation {
                Impl::Gpu => report.by_impl.0 = 1,
                Impl::Cpu | Impl::Imported => report.by_impl.1 = 1,
            }
        }
        Err(why) => {
            // the words of the step for the same refusal (`features::extract_all`)
            report.log.push(format!("{name}: not decoded here ({why})"));
            report.unsupported.push(name);
        }
    }
    let d = features_dict(py, &report)?;
    d.set_item("points", report.points)?;
    Ok(d)
}

/// The preselection of the pairs of the photos of ``small_store`` (their features at the preselection
/// size): every pair matched on the GPUs ``devices`` (adapter indices, -1 the best GPU) or, without one, on
/// the CPU, and verified with ``cameras`` - ``{name: (model, params, width, height, has_prior_focal)}``, one per photo
/// of the store (`preselect::run`). Returns ``weights``, the pairs with at least ``min_inliers`` inlier
/// matches as ``(a, b, inliers)`` with ``a < b`` and sorted, ``pairs_tried`` and ``log``.
/// ``progress(done, total, message)`` counts on one scale, twice the pairs tried: the matching to the
/// pairs tried, the verification after it; at the start, then at most every ``min_interval`` seconds, and
/// with the last state when the step ends. The GIL is released.
#[pyfunction]
#[pyo3(signature = (small_store, cameras, devices, min_inliers, progress=None, min_interval=MIN_INTERVAL))]
pub(crate) fn native_preselect<'py>(py: Python<'py>, small_store: &str, cameras: HashMap<String, CameraArg>,
                                    devices: Vec<i64>, min_inliers: usize, progress: Option<Py<PyAny>>,
                                    min_interval: f64) -> PyResult<Bound<'py, PyDict>> {
    let interval = self::interval(min_interval)?;
    let devices = self::devices(py, devices)?;
    let (cams, calibrated) = self::cameras(cameras)?;
    let o = PreselectOptions { min_inliers, matching: MATCHING, two_view: two_view::Options::default() };
    let report = run_native(py, progress.as_ref(), "preselecting pairs", interval, |cancel, progress| {
        let store = open_feature_store(small_store)?;
        preselect::run(&store, &cams, &calibrated, &devices, &o, cancel, progress)
    })?;
    let d = PyDict::new(py);
    d.set_item("weights", report.weights)?;
    d.set_item("pairs_tried", report.pairs_tried)?;
    d.set_item("log", report.log)?;
    Ok(d)
}

/// The options of the matching of the pairs from a flat dict: the fields of `pairs::Options` by their
/// names - ``stage1_points``, ``trust_inliers`` and ``drop_inliers`` must be given (their home is the
/// Python side), ``max_ratio``, ``max_distance``, ``cross_check`` and ``max_num_matches`` (None keeps every
/// match) have COLMAP's defaults, the second stage's ``guided`` (true), ``guided_max_ratio`` (``max_ratio``)
/// and ``guided_band`` (1.0, a positive multiple of the verification's ``max_error``) keep the behaviour of
/// the first versions - with the verification's under ``two_view_*`` and its RANSAC's under ``ransac_*``
/// (the keys of :func:`two_view_defaults`); a key that is not one of them is a ValueError.
fn pairs_options(d: &Bound<'_, PyDict>) -> PyResult<pairs::Options> {
    let mut o = pairs::Options { stage1_points: 0, trust_inliers: 0, drop_inliers: 0, max_ratio: 0.8,
                                 max_distance: 0.7, cross_check: true, max_num_matches: MAX_NUM_MATCHES,
                                 two_view: two_view::Options::default(), guided: true, guided_max_ratio: 0.8,
                                 guided_band: 1.0 };
    let mut guided_max_ratio: Option<f32> = None;
    const THRESHOLDS: [&str; 3] = ["stage1_points", "trust_inliers", "drop_inliers"];
    let mut given = [false; 3];
    for (key, value) in d.iter() {
        let key: String = key.extract()?;
        let known = match key.as_str() {
            "stage1_points" => {
                o.stage1_points = value.extract()?;
                given[0] = true;
                true
            }
            "trust_inliers" => {
                o.trust_inliers = value.extract()?;
                given[1] = true;
                true
            }
            "drop_inliers" => {
                o.drop_inliers = value.extract()?;
                given[2] = true;
                true
            }
            "max_ratio" => {
                o.max_ratio = value.extract()?;
                true
            }
            "max_distance" => {
                o.max_distance = value.extract()?;
                true
            }
            "cross_check" => {
                o.cross_check = value.extract()?;
                true
            }
            "max_num_matches" => {
                o.max_num_matches = value.extract::<Option<usize>>()?.unwrap_or(usize::MAX);
                true
            }
            "guided" => {
                o.guided = value.extract()?;
                true
            }
            "guided_max_ratio" => {
                guided_max_ratio = Some(value.extract()?);
                true
            }
            "guided_band" => {
                o.guided_band = value.extract()?;
                if !(o.guided_band > 0.0 && o.guided_band.is_finite()) {
                    return Err(PyValueError::new_err(format!("guided_band must be positive, not {}", o.guided_band)));
                }
                true
            }
            k if k.starts_with("ransac_") => set_two_view_option(&mut o.two_view, k, &value)?,
            k => match k.strip_prefix("two_view_") {
                Some(rest) => set_two_view_option(&mut o.two_view, rest, &value)?,
                None => false,
            },
        };
        if !known {
            return Err(PyValueError::new_err(format!("unknown matching option {key:?}")));
        }
    }
    o.guided_max_ratio = guided_max_ratio.unwrap_or(o.max_ratio);
    let missing: Vec<&str> = THRESHOLDS.iter().zip(given).filter(|&(_, g)| !g).map(|(n, _)| *n).collect();
    if !missing.is_empty() {
        return Err(PyValueError::new_err(format!("the matching options must give {}", missing.join(", "))));
    }
    Ok(o)
}

/// The matching of ``pairs`` - each ``(a, b, preselected)``, two photo names of the feature store
/// ``features`` and whether the preselection chose the pair - in the two stages of `pairs` (the largest
/// scales, then along the epipolar lines of their model; brute force where the first stage is unsure;
/// a pair not preselected with too little is dropped), every result into the match store ``matches``
/// as it is made, on the GPUs ``devices`` (adapter indices, -1 the best GPU) and the CPU threads together
/// (``cpu_share`` false leaves the pairs to the GPUs alone, when there is one; the CPU threads take their first stage
/// and brute force from the GPUs while they are there). ``cameras`` as in
/// :func:`native_preselect`, ``options`` the flat dict of the fields of `pairs::Options` (``stage1_points``,
/// ``trust_inliers``, ``drop_inliers`` given; ``max_ratio``, ``max_distance``, ``cross_check``,
/// ``max_num_matches`` COLMAP's unless given; ``guided``, ``guided_max_ratio``, ``guided_band`` of the second
/// stage; the verification's under ``two_view_*`` and ``ransac_*``).
/// Returns ``done``, ``guided``, ``brute_force``, ``dropped`` (the pairs by route), ``by_device`` (the pairs
/// whose first stage each device did, by its name, ``CPU`` for the CPU twin) and ``log``.
/// ``progress(done, total, message)`` counts the pairs: at the start, then at most every ``min_interval``
/// seconds, and with the last state when the step ends. The GIL is released (`pairs::match_all`).
#[pyfunction]
#[pyo3(signature = (features, matches, pairs, cameras, devices, options, progress=None, cpu_share=true,
                    min_interval=MIN_INTERVAL))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn native_match_pairs<'py>(py: Python<'py>, features: &str, matches: &str,
                                      pairs: Vec<(String, String, bool)>, cameras: HashMap<String, CameraArg>,
                                      devices: Vec<i64>, options: &Bound<'py, PyDict>, progress: Option<Py<PyAny>>,
                                      cpu_share: bool, min_interval: f64) -> PyResult<Bound<'py, PyDict>> {
    let interval = self::interval(min_interval)?;
    let devices = self::devices(py, devices)?;
    let (cams, calibrated) = self::cameras(cameras)?;
    let o = pairs_options(options)?;
    // one CPU worker per core beside the GPUs (and alone without one); none when the GPUs are to work alone
    let cpu_threads = if cpu_share || devices.is_empty() { None } else { Some(0) };
    let report = run_native(py, progress.as_ref(), "matching pairs", interval, |cancel, progress| {
        let store = open_feature_store(features)?;
        let out = match_store(matches)?;
        pairs::match_all(&pairs, &store, &cams, &calibrated, &out, &devices, cpu_threads, &o, cancel, progress)
    })?;
    let d = PyDict::new(py);
    d.set_item("done", report.done)?;
    d.set_item("guided", report.by_route.0)?;
    d.set_item("brute_force", report.by_route.1)?;
    d.set_item("dropped", report.by_route.2)?;
    let by_device = PyDict::new(py);
    for (name, count) in &report.stage1_by_device {
        by_device.set_item(name, count)?;
    }
    d.set_item("by_device", by_device)?;
    d.set_item("log", report.log)?;
    Ok(d)
}

/// What a reader of the feature store gives of a photo: its key points, its descriptors (empty when
/// not asked for) and the implementation that made them.
type ReadFeatures = (Vec<[f32; 4]>, Vec<u8>, Impl);

/// The key points and descriptors of a photo of a store as the readers return them: ``(keypoints (n, 4)
/// float32, descriptors (n, 128) uint8 or None when they were not read, implementation)``.
fn read_arrays<'py>(py: Python<'py>, found: Option<ReadFeatures>, name: &str, descriptors: bool)
    -> PyResult<(Bound<'py, PyArray2<f32>>, Option<Bound<'py, PyArray2<u8>>>, &'static str)> {
    let (kp, desc, implementation) = found.ok_or_else(|| PyKeyError::new_err(name.to_owned()))?;
    let n = kp.len();
    let flat: Vec<f32> = kp.iter().flat_map(|k| k.iter().copied()).collect();
    let kp = Array2::from_shape_vec((n, 4), flat).unwrap().into_pyarray(py);
    let desc = descriptors.then(|| Array2::from_shape_vec((n, DESC), desc).unwrap().into_pyarray(py));
    Ok((kp, desc, implementation.name()))
}

/// The features of a photo given from Python, checked: ``keypoints`` ``(n, 4)`` float32 in the pixels of
/// the full photo with COLMAP's centre, ``descriptors`` ``(n, 128)`` uint8, ``width`` and ``height`` of the
/// full photo, ``image_id`` COLMAP's id of the photo in the database they come from, or None. The
/// implementation is ``imported`` (the features of the classic step), the extraction parameters zero.
fn imported_features(keypoints: &PyReadonlyArray2<'_, f32>, descriptors: &PyReadonlyArray2<'_, u8>, width: u32,
                     height: u32, image_id: Option<u32>) -> PyResult<PhotoFeatures> {
    if keypoints.shape()[1] != 4 {
        return Err(PyValueError::new_err(format!("keypoints must be (n, 4), not {:?}", keypoints.shape())));
    }
    if descriptors.shape()[1] != DESC {
        let given = descriptors.shape()[1];
        return Err(PyValueError::new_err(format!("descriptors of {DESC} bytes expected, {given} given")));
    }
    let (points, rows) = (keypoints.shape()[0], descriptors.shape()[0]);
    if rows != points {
        return Err(PyValueError::new_err(format!("{points} key points and {rows} descriptors")));
    }
    let k = keypoints.as_array();
    let keypoints: Vec<[f32; 4]> = (0..points).map(|i| [k[[i, 0]], k[[i, 1]], k[[i, 2]], k[[i, 3]]]).collect();
    let descriptors: Vec<u8> = descriptors.as_array().iter().copied().collect();
    Ok(PhotoFeatures { width, height, implementation: Impl::Imported, image_id, params: ExtractParams::default(),
                       keypoints, descriptors })
}

/// The header of a photo of a feature store as a dict: ``width`` and ``height`` of the full photo,
/// ``points``, ``impl`` (``gpu``, ``cpu`` or ``imported``), ``image_id`` (COLMAP's, or None),
/// ``max_image_size`` (the limit the photo was reduced under, 0 for none or imported) and ``masked``.
fn header_dict<'py>(py: Python<'py>, h: &FeatureHeader) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("width", h.width)?;
    d.set_item("height", h.height)?;
    d.set_item("points", h.points)?;
    d.set_item("impl", h.implementation.name())?;
    d.set_item("image_id", h.image_id)?;
    d.set_item("max_image_size", h.params.max_image_size)?;
    d.set_item("masked", h.params.masked)?;
    Ok(d)
}

/// The result of a pair of a match store as a dict: ``matches`` ``(n, 2)`` uint32 pairs of key point
/// indices into the two photos, ``inliers`` ``(m, 2)`` among them, ``config`` (COLMAP's number), ``F``,
/// ``E``, ``H`` (3 x 3 float64 or None), ``qvec`` and ``tvec`` (the relative pose or None) and ``route``
/// (``guided``, ``brute_force``, ``dropped`` or ``imported``).
fn pair_dict<'py>(py: Python<'py>, r: &PairResult) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    let flat: Vec<u32> = r.matches.iter().flat_map(|m| [m[0], m[1]]).collect();
    d.set_item("matches", Array2::from_shape_vec((r.matches.len(), 2), flat).unwrap().into_pyarray(py))?;
    two_view_into_dict(py, &d, &r.two_view)?;
    d.set_item("route", r.route.name())?;
    Ok(d)
}

/// ``(n, 2)`` uint32 pairs of key point indices from Python; another shape is a ValueError naming ``what``.
fn index_pairs(a: &PyReadonlyArray2<'_, u32>, what: &str) -> PyResult<Vec<[u32; 2]>> {
    if a.shape()[1] != 2 {
        return Err(PyValueError::new_err(format!("{what} must be (n, 2), not {:?}", a.shape())));
    }
    let v = a.as_array();
    Ok((0..a.shape()[0]).map(|i| [v[[i, 0]], v[[i, 1]]]).collect())
}

/// A 3 x 3 float64 matrix from Python, or None; another shape is a ValueError naming ``what``.
fn matrix(m: Option<PyReadonlyArray2<'_, f64>>, what: &str) -> PyResult<Option<Matrix3<f64>>> {
    let Some(m) = m else { return Ok(None) };
    if m.shape() != [3, 3] {
        return Err(PyValueError::new_err(format!("{what} must be 3 x 3, not {:?}", m.shape())));
    }
    let v = m.as_array();
    Ok(Some(Matrix3::from_fn(|r, c| v[[r, c]])))
}

/// The result of a pair given from Python (`Route::Imported`, the matches of the classic step): ``matches``
/// and ``inliers`` ``(n, 2)`` uint32, ``config`` COLMAP's number (0 to 7: the ones the verification gives),
/// the models and the pose as :func:`pair_dict` gives them (``qvec`` and ``tvec`` together or neither).
#[allow(clippy::too_many_arguments)]
fn imported_result(a: &str, b: &str, matches: &PyReadonlyArray2<'_, u32>, inliers: &PyReadonlyArray2<'_, u32>,
                   config: u32, f: Option<PyReadonlyArray2<'_, f64>>, e: Option<PyReadonlyArray2<'_, f64>>,
                   h: Option<PyReadonlyArray2<'_, f64>>, qvec: Option<[f64; 4]>, tvec: Option<[f64; 3]>)
    -> PyResult<PairResult> {
    let config = Config::from_u32(config).ok_or_else(|| {
        PyValueError::new_err(format!("({a}, {b}): configuration {config} is not one the verification gives (0 to 7)"))
    })?;
    let cam2_from_cam1 = match (qvec, tvec) {
        (Some(q), Some(t)) => {
            let rotation = UnitQuaternion::from_quaternion(Quaternion::new(q[0], q[1], q[2], q[3]));
            Some((rotation, Vector3::new(t[0], t[1], t[2])))
        }
        (None, None) => None,
        _ => return Err(PyValueError::new_err(format!("({a}, {b}): qvec and tvec go together"))),
    };
    let two_view = TwoView { config, inliers: index_pairs(inliers, "inliers")?, e: matrix(e, "E")?, f: matrix(f, "F")?,
                             h: matrix(h, "H")?, cam2_from_cam1 };
    Ok(PairResult { matches: index_pairs(matches, "matches")?, two_view, route: Route::Imported })
}

/// The features of the photo ``name`` of the feature store ``store``: ``(keypoints (n, 4) float32 - x, y,
/// scale, orientation in the pixels of the full photo with COLMAP's centre -, descriptors (n, 128) uint8,
/// implementation)``, the last ``gpu``, ``cpu`` or ``imported``. A photo the store does not hold is a
/// KeyError; a store that is not there or is damaged, a RuntimeError naming the file.
#[pyfunction]
pub(crate) fn native_store_read<'py>(py: Python<'py>, store: &str, name: &str)
    -> PyResult<(Bound<'py, PyArray2<f32>>, Bound<'py, PyArray2<u8>>, &'static str)> {
    let found = run_native(py, None, "reading the feature store", NO_PROGRESS, |_, _| {
        let store = open_feature_store(store)?;
        let view = store.get(name);
        Ok(view.map(|v| (v.keypoints().to_vec(), v.descriptors().to_vec(), v.header().implementation)))
    })?;
    let (kp, desc, implementation) = found.ok_or_else(|| PyKeyError::new_err(name.to_owned()))?;
    let (kp, desc) = sift_arrays(py, kp, desc)?;
    Ok((kp, desc, implementation.name()))
}

/// The features of the photo ``name`` written to the feature store ``store`` (made when the folder holds
/// none), replacing what it held for that name: ``keypoints`` ``(n, 4)`` float32 in the pixels of the full
/// photo with COLMAP's centre, ``descriptors`` ``(n, 128)`` uint8, ``width`` and ``height`` of the full photo,
/// ``image_id`` COLMAP's id of the photo in the database they come from, or None. The implementation
/// recorded is ``imported`` (the features of the classic step), the extraction parameters zero.
#[pyfunction]
#[pyo3(signature = (store, name, keypoints, descriptors, width, height, image_id=None))]
#[allow(clippy::too_many_arguments)]
pub(crate) fn native_store_write<'py>(py: Python<'py>, store: &str, name: &str, keypoints: PyReadonlyArray2<'py, f32>,
                                      descriptors: PyReadonlyArray2<'py, u8>, width: u32, height: u32,
                                      image_id: Option<u32>) -> PyResult<()> {
    let f = imported_features(&keypoints, &descriptors, width, height, image_id)?;
    run_native(py, None, "writing the feature store", NO_PROGRESS, |_, _| {
        let store = feature_store(store)?;
        store.put(name, &f).map_err(|e| e.to_string())
    })
}

/// The names of the photos of the feature store ``store``, sorted.
#[pyfunction]
pub(crate) fn native_store_names(py: Python<'_>, store: &str) -> PyResult<Vec<String>> {
    run_native(py, None, "reading the feature store", NO_PROGRESS, |_, _| Ok(open_feature_store(store)?.names()))
}

/// The header of the photo ``name`` of the feature store ``store``: ``width`` and ``height`` of the full
/// photo, ``points``, ``impl`` (``gpu``, ``cpu`` or ``imported``), ``image_id`` (COLMAP's, or None),
/// ``max_image_size`` (the limit the photo was reduced under, 0 for none or imported) and ``masked``. A
/// photo the store does not hold is a KeyError.
#[pyfunction]
pub(crate) fn native_store_header<'py>(py: Python<'py>, store: &str, name: &str) -> PyResult<Bound<'py, PyDict>> {
    let header: Option<FeatureHeader> = run_native(py, None, "reading the feature store", NO_PROGRESS, |_, _| {
        Ok(open_feature_store(store)?.get(name).map(|v| v.header().clone()))
    })?;
    header_dict(py, &header.ok_or_else(|| PyKeyError::new_err(name.to_owned()))?)
}

/// The result of the pair ``(a, b)`` of the match store ``matches`` (the two names in the order they were
/// matched), or None when the store has no such pair: ``matches`` ``(n, 2)`` uint32 pairs of key point
/// indices into the two photos, ``inliers`` ``(m, 2)`` among them, ``config`` (COLMAP's number), ``F``,
/// ``E``, ``H`` (3 x 3 float64 or None), ``qvec`` and ``tvec`` (the relative pose or None) and ``route``
/// (``guided``, ``brute_force``, ``dropped`` or ``imported``).
#[pyfunction]
pub(crate) fn native_match_read<'py>(py: Python<'py>, matches: &str, a: &str, b: &str)
    -> PyResult<Option<Bound<'py, PyDict>>> {
    let found = run_native(py, None, "reading the match store", NO_PROGRESS, |_, _| {
        let store = MatchStore::open(Path::new(matches)).map_err(|e| e.to_string())?;
        Ok(store.get(a, b))
    })?;
    found.map(|r| pair_dict(py, &r)).transpose()
}

/// The pairs of the match store ``matches``, each as its two names in the order they were matched,
/// sorted.
#[pyfunction]
pub(crate) fn native_match_pairs_list(py: Python<'_>, matches: &str) -> PyResult<Vec<(String, String)>> {
    run_native(py, None, "reading the match store", NO_PROGRESS, |_, _| {
        Ok(MatchStore::open(Path::new(matches)).map_err(|e| e.to_string())?.pairs())
    })
}

/// A feature store held open from Python - ``NativeFeatureStore(dir)``, opened when the folder holds one
/// and made otherwise - for a step that goes through every photo of it: the names, the header and the
/// features of a photo read, and features written as :func:`native_store_write` writes them, through one
/// handle, where the one-call readers open the store (and check every file of it) on every call. What one
/// handle writes another on the same folder reads. A store that is not there or is damaged is a
/// RuntimeError naming the file; a photo the store does not hold is a KeyError.
#[pyclass(name = "NativeFeatureStore", module = "ppg_core", frozen)]
pub(crate) struct NativeFeatureStore {
    store: FeatureStore,
}

#[pymethods]
impl NativeFeatureStore {
    #[new]
    fn new(py: Python<'_>, dir: &str) -> PyResult<Self> {
        let store = run_native(py, None, "opening the feature store", NO_PROGRESS, |_, _| feature_store(dir))?;
        Ok(NativeFeatureStore { store })
    }

    /// The names of the photos of the store, sorted.
    fn names(&self) -> Vec<String> {
        self.store.names()
    }

    /// The header of the photo ``name`` (the dict of :func:`native_store_header`).
    fn header<'py>(&self, py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyDict>> {
        let header = run_native(py, None, "reading the feature store", NO_PROGRESS, |_, _| {
            Ok(self.store.get(name).map(|v| v.header().clone()))
        })?;
        header_dict(py, &header.ok_or_else(|| PyKeyError::new_err(name.to_owned()))?)
    }

    /// The features of the photo ``name``: ``(keypoints (n, 4) float32, descriptors (n, 128) uint8 - or None
    /// when ``descriptors`` is false: the export of the key points alone does not copy them -,
    /// implementation)``, as :func:`native_store_read` gives them.
    #[pyo3(signature = (name, descriptors=true))]
    fn read<'py>(&self, py: Python<'py>, name: &str, descriptors: bool)
        -> PyResult<(Bound<'py, PyArray2<f32>>, Option<Bound<'py, PyArray2<u8>>>, &'static str)> {
        let found = run_native(py, None, "reading the feature store", NO_PROGRESS, |_, _| {
            Ok(self.store.get(name).map(|v| {
                let desc = if descriptors { v.descriptors().to_vec() } else { Vec::new() };
                (v.keypoints().to_vec(), desc, v.header().implementation)
            }))
        })?;
        read_arrays(py, found, name, descriptors)
    }

    /// The features of the photo ``name`` written, replacing what the store held for it, as
    /// :func:`native_store_write` writes them (``imported``, with COLMAP's ``image_id`` or None).
    #[pyo3(signature = (name, keypoints, descriptors, width, height, image_id=None))]
    #[allow(clippy::too_many_arguments)]
    fn write<'py>(&self, py: Python<'py>, name: &str, keypoints: PyReadonlyArray2<'py, f32>,
                  descriptors: PyReadonlyArray2<'py, u8>, width: u32, height: u32, image_id: Option<u32>)
        -> PyResult<()> {
        let f = imported_features(&keypoints, &descriptors, width, height, image_id)?;
        run_native(py, None, "writing the feature store", NO_PROGRESS, |_, _| {
            self.store.put(name, &f).map_err(|e| e.to_string())
        })
    }
}

/// A match store held open from Python - ``NativeMatchStore(dir)``, opened when the folder holds one and
/// made otherwise - for a step that goes through every pair of it: the pairs listed and read, and pairs
/// written (`Route::Imported`, the matches of a COLMAP database) through one handle. What is written is
/// buffered and sealed into a segment file by :meth:`flush` (or when the buffer is full, or when the
/// handle is dropped): a reader that opens the store on its own sees a pair once it is flushed, this
/// handle at once. A store that is not there or is damaged is a RuntimeError naming the file.
#[pyclass(name = "NativeMatchStore", module = "ppg_core", frozen)]
pub(crate) struct NativeMatchStore {
    store: MatchStore,
}

#[pymethods]
impl NativeMatchStore {
    #[new]
    fn new(py: Python<'_>, dir: &str) -> PyResult<Self> {
        let store = run_native(py, None, "opening the match store", NO_PROGRESS, |_, _| match_store(dir))?;
        Ok(NativeMatchStore { store })
    }

    /// The pairs of the store, each as its two names in the order they were put, sorted.
    fn pairs(&self) -> Vec<(String, String)> {
        self.store.pairs()
    }

    /// The result of the pair ``(a, b)`` (the dict of :func:`native_match_read`), or None when the store has
    /// no such pair.
    fn read<'py>(&self, py: Python<'py>, a: &str, b: &str) -> PyResult<Option<Bound<'py, PyDict>>> {
        let found = run_native(py, None, "reading the match store", NO_PROGRESS, |_, _| Ok(self.store.get(a, b)))?;
        found.map(|r| pair_dict(py, &r)).transpose()
    }

    /// The result of the pair ``(a, b)`` written as ``imported``, replacing what the store held for it:
    /// ``matches`` and ``inliers`` ``(n, 2)`` uint32, ``config`` COLMAP's number (0 to 7), ``F``, ``E``, ``H``
    /// 3 x 3 float64 or None, ``qvec`` (w, x, y, z) with ``tvec`` or neither.
    #[pyo3(signature = (a, b, matches, inliers, config, F=None, E=None, H=None, qvec=None, tvec=None))]
    #[allow(clippy::too_many_arguments, non_snake_case)]
    fn write<'py>(&self, py: Python<'py>, a: &str, b: &str, matches: PyReadonlyArray2<'py, u32>,
                  inliers: PyReadonlyArray2<'py, u32>, config: u32, F: Option<PyReadonlyArray2<'py, f64>>,
                  E: Option<PyReadonlyArray2<'py, f64>>, H: Option<PyReadonlyArray2<'py, f64>>,
                  qvec: Option<[f64; 4]>, tvec: Option<[f64; 3]>) -> PyResult<()> {
        let r = imported_result(a, b, &matches, &inliers, config, F, E, H, qvec, tvec)?;
        run_native(py, None, "writing the match store", NO_PROGRESS, |_, _| {
            self.store.put(a, b, &r).map_err(|e| e.to_string())
        })
    }

    /// What was written sealed into a segment file and named by the index: on disk once this returns.
    fn flush(&self, py: Python<'_>) -> PyResult<()> {
        run_native(py, None, "writing the match store", NO_PROGRESS, |_, _| self.store.flush().map_err(|e| e.to_string()))
    }
}

fn grey_array(py: Python<'_>, g: Grey) -> Bound<'_, PyArray2<u8>> {
    Array2::from_shape_vec((g.height, g.width), g.pixels).unwrap().into_pyarray(py)
}

/// The photo at ``path`` as COLMAP's grey (`image::read_grey`: Rec. 709 of the raster with the EXIF
/// orientation ignored, rounded as ``cv2.transform(..., sfm._REC709_BGR)`` rounds it), ``(grey (h, w)
/// uint8, (width, height) of the full photo)``: a JPEG decoded at the smallest DCT scale whose long side
/// reaches ``min_long_side`` when ``dct`` (at full size when it is false or ``min_long_side`` is 0), a PNG
/// or TIFF at full size. A file this engine does not decode, a damaged one or one that cannot be read is
/// a RuntimeError naming it: the features step reports the first as ``unsupported`` and the others as
/// ``unreadable``.
#[pyfunction]
pub(crate) fn read_grey<'py>(py: Python<'py>, path: &str, min_long_side: usize, dct: bool)
    -> PyResult<(Bound<'py, PyArray2<u8>>, (usize, usize))> {
    let (grey, full) = run_native(py, None, "reading a photo", NO_PROGRESS, |_, _| {
        image::read_grey(Path::new(path), min_long_side, dct).map_err(|e| format!("{path}: {e}"))
    })?;
    Ok((grey_array(py, grey), full))
}

/// A grey image ``(h, w)`` uint8 reduced to ``w`` x ``h`` as COLMAP reduces a photo before extracting its
/// features (`image::rescale_as_colmap`, the port of ``sfm._rescale_as_colmap``: OpenImageIO's triangle
/// filter, the same sums in single precision in the same order, so the two give the same bytes).
#[pyfunction]
pub(crate) fn rescale_as_colmap<'py>(py: Python<'py>, grey: PyReadonlyArray2<'py, u8>, w: usize, h: usize)
    -> PyResult<Bound<'py, PyArray2<u8>>> {
    let a = grey.as_array();
    let (height, width) = a.dim();
    let img = Grey { width, height, pixels: a.iter().copied().collect() };
    let out = run_native(py, None, "reducing a photo", NO_PROGRESS, |_, _| Ok(image::rescale_as_colmap(&img, w, h)))?;
    Ok(grey_array(py, out))
}
