//! The bindings of the native mapper (API 16): `NativeMapper`, one method per stage of
//! `mapper::Mapper` (`view_graph`, `rotations`, `tracks`, `position`, `refine`, then `write_model`
//! and `report`), and `native_mapper_defaults`, the options of the mapper with their defaults.
//!
//! The arrays the mapper starts from are borrowed from numpy (no copy of numpy's own, strided views
//! read as they are) and copied once into the engine's `Input`, which the mapper keeps for its whole
//! life: an array numpy changes afterwards does not change the mapper. An array of the wrong type
//! or shape, offsets that fall or end elsewhere than their array, an index out of range (what
//! `Input::check` refuses) are a RuntimeError naming the array or the item, before any work.
//!
//! Every stage runs without the GIL on a thread of its own (`heartbeat::run_with_heartbeat`):
//! `progress(done, total, message)` is called from the calling thread, with the GIL taken back for
//! that call only, when the stage begins, then every `BEAT` while it runs (whatever the stage is
//! doing) and once when it has ended; `total` is the five stages, `done` the stages ended, the
//! message the name of the stage. An exception the callback raises raises the stop flag of the
//! mapper, which every stage reads inside its steps, and comes out as it was; the mapper is then
//! stopped: every later stage and `write_model` is a RuntimeError (`report` still answers). A
//! stage the engine refuses (out of its order, an input it cannot use) is a RuntimeError with the
//! engine's message; a failure of `refine` stops the mapper in the engine itself.

use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;
use std::sync::atomic::Ordering;
use std::time::Duration;

use numpy::ndarray::Array2;
use numpy::{Element, IntoPyArray, PyArray2, PyReadonlyArray1, PyReadonlyArray2, PyUntypedArrayMethods};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use ppg_engine::mapper::heartbeat::run_with_heartbeat;
use ppg_engine::mapper::input::{CameraIn, ImageIn, Input, PairIn};
use ppg_engine::mapper::positioning::{Frame, Gnss};
use ppg_engine::mapper::rounds::RoundReport;
use ppg_engine::mapper::{Mapper, MapperReport, Options};

use crate::panic_text;

/// The interval of the progress while a stage runs: half the 0.5 s the callers ask for at most, so
/// that a slow callback still leaves no gap longer than that.
const BEAT: Duration = Duration::from_millis(250);

/// The stages, the `total` of every progress call.
const STAGES: usize = 5;

/// A photo: `(name, image_id, camera index)`.
type ImageArg = (String, u32, usize);

/// A camera: `(camera_id, model, params, width, height, prior_focal)`.
type CameraArg = (u32, String, Vec<f64>, f64, f64, bool);

fn refused(problem: String) -> PyErr {
    PyRuntimeError::new_err(problem)
}

/// The one-dimensional array `name` of `T`, borrowed.
fn array1<'py, T: Element>(a: &Bound<'py, PyAny>, name: &str, dtype: &str) -> PyResult<PyReadonlyArray1<'py, T>> {
    a.extract::<PyReadonlyArray1<'py, T>>()
        .map_err(|e| refused(format!("{name} must be a one-dimensional {dtype} array: {}", PyErr::from(e))))
}

/// The two-dimensional array `name` of `T` with `columns` columns, borrowed.
fn array2<'py, T: Element>(a: &Bound<'py, PyAny>, name: &str, dtype: &str, columns: usize)
    -> PyResult<PyReadonlyArray2<'py, T>> {
    let a = a
        .extract::<PyReadonlyArray2<'py, T>>()
        .map_err(|e| refused(format!("{name} must be a {dtype} array of shape (n, {columns}): {}", PyErr::from(e))))?;
    if a.shape()[1] != columns {
        return Err(refused(format!("{name} must be a {dtype} array of shape (n, {columns}), not {:?}", a.shape())));
    }
    Ok(a)
}

/// The rows of a borrowed array of `N` columns, copied (packed arrays in one pass, strided views row
/// by row).
fn rows<T: Element + Copy, const N: usize>(a: &PyReadonlyArray2<'_, T>) -> Vec<[T; N]> {
    match a.as_slice() {
        Ok(flat) => flat.chunks_exact(N).map(|r| std::array::from_fn(|j| r[j])).collect(),
        Err(_) => a.as_array().rows().into_iter().map(|r| std::array::from_fn(|j| r[j])).collect(),
    }
}

fn values<T: Element + Copy>(a: &PyReadonlyArray1<'_, T>) -> Vec<T> {
    a.as_array().iter().copied().collect()
}

/// `count` ranges from `offsets` (of `count + 1` values that start at 0, never fall and end at
/// `len`, the length of the array they cut).
fn ranges(offsets: &[i64], count: usize, len: usize, name: &str, what: &str) -> PyResult<Vec<std::ops::Range<usize>>> {
    if offsets.len() != count + 1 {
        return Err(refused(format!("{name}: {} values for {count} {what} ({} expected)", offsets.len(), count + 1)));
    }
    if offsets[0] != 0 {
        return Err(refused(format!("{name} must start at 0, not {}", offsets[0])));
    }
    if let Some(k) = offsets.windows(2).position(|w| w[1] < w[0]) {
        return Err(refused(format!("{name} falls at {}: {} after {}", k + 1, offsets[k + 1], offsets[k])));
    }
    if offsets[count] as u64 != len as u64 {
        return Err(refused(format!("{name} ends at {}, its array holds {len}", offsets[count])));
    }
    Ok(offsets.windows(2).map(|w| w[0] as usize..w[1] as usize).collect())
}

/// The input of the mapper from the arrays of the constructor (see `NativeMapper::new`).
#[allow(clippy::too_many_arguments)]
fn input(images: Vec<ImageArg>, cameras: Vec<CameraArg>, keypoints: &Bound<'_, PyAny>,
         keypoint_offsets: &Bound<'_, PyAny>, pairs: &Bound<'_, PyAny>, pair_config: &Bound<'_, PyAny>,
         pair_f: &Bound<'_, PyAny>, matches: &Bound<'_, PyAny>, match_offsets: &Bound<'_, PyAny>)
    -> PyResult<Input> {
    let keypoints = rows::<f32, 2>(&array2::<f32>(keypoints, "keypoints", "float32", 2)?);
    let keypoint_offsets = values(&array1::<i64>(keypoint_offsets, "keypoint_offsets", "int64")?);
    let pairs = rows::<i64, 2>(&array2::<i64>(pairs, "pairs", "int64", 2)?);
    let pair_config = values(&array1::<i32>(pair_config, "pair_config", "int32")?);
    let pair_f = rows::<f64, 9>(&array2::<f64>(pair_f, "pair_f", "float64", 9)?);
    let matches = rows::<u32, 2>(&array2::<u32>(matches, "matches", "uint32", 2)?);
    let match_offsets = values(&array1::<i64>(match_offsets, "match_offsets", "int64")?);
    let q = pairs.len();
    if pair_config.len() != q || pair_f.len() != q {
        return Err(refused(format!("{q} pairs, {} values of pair_config and {} rows of pair_f", pair_config.len(),
                                   pair_f.len())));
    }
    let image_points = ranges(&keypoint_offsets, images.len(), keypoints.len(), "keypoint_offsets", "images")?;
    let pair_matches = ranges(&match_offsets, q, matches.len(), "match_offsets", "pairs")?;
    if let Some(p) = pairs.iter().position(|ab| ab[0] < 0 || ab[1] < 0) {
        return Err(refused(format!("pairs: pair {p} has a negative image index {:?}", pairs[p])));
    }
    let images = images
        .into_iter()
        .zip(image_points)
        .map(|((name, image_id, camera), keypoints)| ImageIn { name, image_id, camera, keypoints })
        .collect();
    let cameras = cameras
        .into_iter()
        .map(|(camera_id, model, params, width, height, prior_focal)| CameraIn {
            camera_id,
            model,
            params,
            width,
            height,
            prior_focal,
        })
        .collect();
    let pairs = pairs
        .iter()
        .zip(pair_matches)
        .zip(pair_config.iter().zip(&pair_f))
        .map(|((ab, matches), (&config, f))| PairIn {
            a: ab[0] as usize,
            b: ab[1] as usize,
            matches,
            config,
            // a row of NaN: no fundamental matrix (a row partly NaN is refused by `Input::check`)
            f: if f.iter().all(|v| v.is_nan()) { None } else { Some(*f) },
        })
        .collect();
    Ok(Input { images, cameras, keypoints, pairs, matches })
}

/// The options of the mapper from `d`, every key a field of `Options`; a key that is not one is a
/// ValueError.
fn options(d: &Bound<'_, PyDict>) -> PyResult<Options> {
    let mut o = Options::default();
    for (key, value) in d.iter() {
        let key: String = key.extract()?;
        match key.as_str() {
            "min_inliers" => o.min_inliers = value.extract()?,
            "max_epipolar_error_px" => o.max_epipolar_error_px = value.extract()?,
            "min_pair_parallax_deg" => o.min_pair_parallax_deg = value.extract()?,
            "max_relpose_trials" => o.max_relpose_trials = value.extract()?,
            "max_rotation_error_deg" => o.max_rotation_error_deg = value.extract()?,
            "max_normalized_reprojection" => o.max_normalized_reprojection = value.extract()?,
            "min_triangulation_deg" => o.min_triangulation_deg = value.extract()?,
            "positioning_huber" => o.positioning_huber = value.extract()?,
            "rounds" => o.rounds = value.extract()?,
            "ba_loss_scale_px" => o.ba_loss_scale_px = value.extract()?,
            "ba_loss_cauchy" => o.ba_loss_cauchy = value.extract()?,
            "final_all_tracks" => o.final_all_tracks = value.extract()?,
            "opencv_sensor" => o.opencv_sensor = value.extract()?,
            _ => {
                let known = options_dict(d.py(), &Options::default())?.keys();
                return Err(PyValueError::new_err(format!("{key} is not an option of the mapper (options: {known})")));
            }
        }
    }
    Ok(o)
}

fn options_dict<'py>(py: Python<'py>, o: &Options) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("min_inliers", o.min_inliers)?;
    d.set_item("max_epipolar_error_px", o.max_epipolar_error_px)?;
    d.set_item("min_pair_parallax_deg", o.min_pair_parallax_deg)?;
    d.set_item("max_relpose_trials", o.max_relpose_trials)?;
    d.set_item("max_rotation_error_deg", o.max_rotation_error_deg)?;
    d.set_item("max_normalized_reprojection", o.max_normalized_reprojection)?;
    d.set_item("min_triangulation_deg", o.min_triangulation_deg)?;
    d.set_item("positioning_huber", o.positioning_huber)?;
    d.set_item("rounds", o.rounds)?;
    d.set_item("ba_loss_scale_px", o.ba_loss_scale_px)?;
    d.set_item("ba_loss_cauchy", o.ba_loss_cauchy)?;
    d.set_item("final_all_tracks", o.final_all_tracks)?;
    d.set_item("opencv_sensor", o.opencv_sensor.clone())?;
    Ok(d)
}

/// The options of the native mapper with their defaults (the fields of the engine's `Options`):
/// ``max_normalized_reprojection`` and ``min_triangulation_deg`` are pycolmap's
/// ``GlobalPipelineOptions().mapper.max_normalized_reproj_error`` and ``.mapper.min_tri_angle_deg``.
#[pyfunction]
pub(crate) fn native_mapper_defaults(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    options_dict(py, &Options::default())
}

/// The positions of the photos from the dict `gnss` (``image`` int64 (k,), ``xyz`` and ``sigma``
/// float64 (k, 3), ``flight`` int64 (k,)).
fn gnss(d: &Bound<'_, PyDict>) -> PyResult<Gnss> {
    let item = |key: &str| {
        d.get_item(key)?.ok_or_else(|| refused(format!("gnss: the key {key} is missing (image, xyz, sigma, flight)")))
    };
    let image = values(&array1::<i64>(&item("image")?, "gnss image", "int64")?);
    let xyz = rows::<f64, 3>(&array2::<f64>(&item("xyz")?, "gnss xyz", "float64", 3)?);
    let sigma = rows::<f64, 3>(&array2::<f64>(&item("sigma")?, "gnss sigma", "float64", 3)?);
    let flight = values(&array1::<i64>(&item("flight")?, "gnss flight", "int64")?);
    let k = image.len();
    if xyz.len() != k || sigma.len() != k || flight.len() != k {
        return Err(refused(format!("gnss: {k} images, {} xyz, {} sigma, {} flight", xyz.len(), sigma.len(),
                                   flight.len())));
    }
    if let Some(&i) = image.iter().find(|&&i| i < 0) {
        return Err(refused(format!("gnss image: negative image index {i}")));
    }
    Ok(Gnss { image: image.into_iter().map(|i| i as usize).collect(), xyz, sigma, flight })
}

fn frame_name(f: Frame) -> &'static str {
    match f {
        Frame::Gnss => "gnss",
        Frame::Similarity => "similarity",
        Frame::Free => "free",
    }
}

fn centres_array<'py>(py: Python<'py>, centres: &[Option<[f64; 3]>]) -> Bound<'py, PyArray2<f64>> {
    let mut a = Array2::from_elem((centres.len(), 3), f64::NAN);
    for (i, c) in centres.iter().enumerate() {
        if let Some(c) = c {
            for j in 0..3 {
                a[(i, j)] = c[j];
            }
        }
    }
    a.into_pyarray(py)
}

fn per_flight<'py>(py: Python<'py>, values: &[(i64, [f64; 3])]) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    for (flight, v) in values {
        d.set_item(flight, (v[0], v[1], v[2]))?;
    }
    Ok(d)
}

fn round_dict<'py>(py: Python<'py>, r: &RoundReport) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("round", r.round)?;
    d.set_item("intrinsics", r.intrinsics.clone())?;
    d.set_item("cost_initial", r.cost_initial)?;
    d.set_item("cost", r.cost)?;
    d.set_item("observations", r.observations)?;
    d.set_item("filtered", r.filtered)?;
    d.set_item("added", r.added)?;
    d.set_item("seconds", r.seconds)?;
    Ok(d)
}

fn rounds_list<'py>(py: Python<'py>, rounds: &[RoundReport]) -> PyResult<Vec<Bound<'py, PyDict>>> {
    rounds.iter().map(|r| round_dict(py, r)).collect()
}

/// The native global mapper on one input (milestone 3; the engine's `mapper::Mapper`).
///
/// ``NativeMapper(images, cameras, keypoints, keypoint_offsets, pairs, pair_config, pair_f, matches,
/// match_offsets, options)``: ``images`` a list of ``(name, image_id, camera index)``; ``cameras`` a
/// list of ``(camera_id, model, params, width, height, prior_focal)``; ``keypoints`` float32 (k, 2),
/// COLMAP pixels, those of photo ``i`` the rows ``keypoint_offsets[i]:keypoint_offsets[i + 1]``
/// (int64, n + 1); ``pairs`` int64 (q, 2), indices into ``images``; ``pair_config`` int32 (q,),
/// COLMAP's configuration; ``pair_f`` float64 (q, 9), the fundamental matrix row-major, a row of
/// NaN when there is none; ``matches`` uint32 (r, 2), key point indices within each photo, those of
/// pair ``p`` the rows ``match_offsets[p]:match_offsets[p + 1]`` (int64, q + 1); ``options`` a dict of
/// the keys of :func:`native_mapper_defaults`.
#[pyclass(name = "NativeMapper", module = "ppg_core")]
pub(crate) struct NativeMapper {
    mapper: Mapper,
    /// Why the mapper was stopped by the bindings (its progress callback raised, or a panic).
    stopped: Option<String>,
}

impl NativeMapper {
    /// The stage `f` (number `index`, named `message`) of the mapper run without the GIL beside its
    /// progress (see the module).
    fn stage<T: Send>(&mut self, py: Python<'_>, progress: Option<Py<PyAny>>, index: usize, message: &'static str,
                      f: impl FnOnce(&mut Mapper) -> Result<T, String> + Send) -> PyResult<T> {
        if let Some(why) = &self.stopped {
            return Err(refused(format!("the mapper was stopped ({why}): {message} cannot run")));
        }
        let stop = self.mapper.stop_flag();
        stop.store(false, Ordering::SeqCst);
        let mapper = &mut self.mapper;
        let mut raised: Option<PyErr> = None;
        let outcome = py.detach(|| {
            let mut beat = |finished: bool| -> bool {
                let Some(callback) = &progress else { return true };
                let done = index + usize::from(finished);
                Python::attach(|py| match callback.call1(py, (done, STAGES, message)) {
                    Ok(_) => true,
                    Err(e) => {
                        raised = Some(e);
                        false
                    }
                })
            };
            catch_unwind(AssertUnwindSafe(|| run_with_heartbeat(BEAT, &stop, || f(mapper), &mut beat)))
        });
        if let Some(e) = raised {
            self.stopped = Some(format!("its progress callback raised in {message}"));
            if let Err(payload) = &outcome {
                // the user's exception is the error; the panic goes with it (add_note, Python 3.11+)
                let _ = e.value(py).call_method1("add_note", (format!("{message}: {}", panic_text(&**payload)),));
            }
            return Err(e);
        }
        match outcome {
            Ok(Ok(value)) => Ok(value),
            Ok(Err(problem)) => Err(refused(format!("{message}: {problem}"))),
            Err(payload) => {
                let text = format!("{message}: {}", panic_text(&*payload));
                self.stopped = Some(format!("the engine panicked in {message}"));
                Err(refused(text))
            }
        }
    }

    fn camera_id(&self, index: usize) -> u32 {
        self.mapper.input().cameras[index].camera_id
    }
}

#[pymethods]
impl NativeMapper {
    #[new]
    #[pyo3(signature = (images, cameras, keypoints, keypoint_offsets, pairs, pair_config, pair_f, matches,
                        match_offsets, options=None))]
    #[allow(clippy::too_many_arguments)]
    fn new(py: Python<'_>, images: Vec<ImageArg>, cameras: Vec<CameraArg>, keypoints: &Bound<'_, PyAny>,
           keypoint_offsets: &Bound<'_, PyAny>, pairs: &Bound<'_, PyAny>, pair_config: &Bound<'_, PyAny>,
           pair_f: &Bound<'_, PyAny>, matches: &Bound<'_, PyAny>, match_offsets: &Bound<'_, PyAny>,
           options: Option<&Bound<'_, PyDict>>) -> PyResult<Self> {
        let o = match options {
            Some(d) => self::options(d)?,
            None => Options::default(),
        };
        let input = input(images, cameras, keypoints, keypoint_offsets, pairs, pair_config, pair_f, matches,
                          match_offsets)?;
        // `Input::check` reads every match: without the GIL
        let mapper = py
            .detach(|| catch_unwind(AssertUnwindSafe(|| Mapper::new(input, o))))
            .map_err(|payload| refused(format!("the mapper: {}", panic_text(&*payload))))?
            .map_err(refused)?;
        Ok(NativeMapper { mapper, stopped: None })
    }

    /// The view graph (the focal lengths that were guessed calibrated, the relative pose of every
    /// pair): ``edges``, ``left_out`` (pairs whose estimation failed), ``too_few`` (pairs with too few
    /// matches), ``focals`` (``(camera_id, before, after)`` of every camera calibrated).
    #[pyo3(signature = (progress=None))]
    fn view_graph<'py>(&mut self, py: Python<'py>, progress: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyDict>> {
        let (edges, left_out, too_few) = self.stage(py, progress, 0, "view graph", |m| {
            m.view_graph(&mut || true).map(|g| (g.edges.len(), g.left_out, g.too_few))
        })?;
        let d = PyDict::new(py);
        d.set_item("edges", edges)?;
        d.set_item("left_out", left_out)?;
        d.set_item("too_few", too_few)?;
        let focals: Vec<(u32, f64, f64)> =
            self.mapper.report().focals.iter().map(|f| (self.camera_id(f.camera), f.before, f.after)).collect();
        d.set_item("focals", focals)?;
        Ok(d)
    }

    /// The rotations averaged: ``registered`` (the photos placed), ``dropped`` (the edges dropped),
    /// ``components`` (the sizes of the connected parts, largest first).
    #[pyo3(signature = (progress=None))]
    fn rotations<'py>(&mut self, py: Python<'py>, progress: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyDict>> {
        let (registered, dropped, components) = self.stage(py, progress, 1, "rotations", |m| {
            m.rotations(&mut || true).map(|r| (r.placed, r.dropped.len(), r.components.clone()))
        })?;
        let d = PyDict::new(py);
        d.set_item("registered", registered)?;
        d.set_item("dropped", dropped)?;
        d.set_item("components", components)?;
        Ok(d)
    }

    /// The tracks of the photos placed and the ``per_photo`` longest of each photo: ``tracks``,
    /// ``selected``, ``observations`` (of all the tracks), ``dropped_conflicts``.
    #[pyo3(signature = (per_photo, progress=None))]
    fn tracks<'py>(&mut self, py: Python<'py>, per_photo: usize, progress: Option<Py<PyAny>>)
        -> PyResult<Bound<'py, PyDict>> {
        let (tracks, observations, dropped) = self.stage(py, progress, 2, "tracks", |m| {
            m.tracks(per_photo, &mut || true).map(|t| (t.len(), t.observations.len(), t.dropped_conflicts))
        })?;
        let d = PyDict::new(py);
        d.set_item("tracks", tracks)?;
        d.set_item("selected", self.mapper.report().selected)?;
        d.set_item("observations", observations)?;
        d.set_item("dropped_conflicts", dropped)?;
        Ok(d)
    }

    /// The global positioning, with the positions of the photos ``gnss`` (a dict of ``image`` int64
    /// (k,), ``xyz`` and ``sigma`` float64 (k, 3), ``flight`` int64 (k,)) or None: ``centres`` float64
    /// (n, 3), a row of NaN for a photo not placed; ``frame`` (``"gnss"``, ``"similarity"``,
    /// ``"free"``); ``frame_residual_deg``, ``sigma_h``, ``sigma_v`` (the largest recorded accuracy
    /// of the positions used; None in a free frame); ``bias`` ``{flight: (x, y, z)}``; ``bias_sd``
    /// (empty here: the deviations of the biases are estimated by ``refine``, see ``report``);
    /// ``gnss_outliers`` (photo indices).
    #[pyo3(signature = (gnss=None, progress=None))]
    fn position<'py>(&mut self, py: Python<'py>, gnss: Option<&Bound<'py, PyDict>>, progress: Option<Py<PyAny>>)
        -> PyResult<Bound<'py, PyDict>> {
        let gnss = gnss.map(self::gnss).transpose()?;
        let p = self.stage(py, progress, 3, "positioning", |m| m.position(gnss.as_ref(), &mut || true))?;
        let d = PyDict::new(py);
        d.set_item("centres", centres_array(py, &p.centres))?;
        d.set_item("frame", frame_name(p.frame))?;
        d.set_item("frame_residual_deg", p.frame_residual_deg)?;
        d.set_item("sigma_h", p.sigma_h)?;
        d.set_item("sigma_v", p.sigma_v)?;
        d.set_item("bias", per_flight(py, &p.bias)?)?;
        d.set_item("bias_sd", PyDict::new(py))?;
        d.set_item("gnss_outliers", p.gnss_outliers.clone())?;
        Ok(d)
    }

    /// The rounds of adjustment: ``rounds``, a dict per round (``round``, ``intrinsics``,
    /// ``cost_initial``, ``cost``, ``observations``, ``filtered``, ``added``, ``seconds``).
    #[pyo3(signature = (progress=None))]
    fn refine<'py>(&mut self, py: Python<'py>, progress: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyDict>> {
        let rounds = self.stage(py, progress, 4, "refinement", |m| m.refine(&mut || true).map(<[_]>::to_vec))?;
        let d = PyDict::new(py);
        d.set_item("rounds", rounds_list(py, &rounds)?)?;
        Ok(d)
    }

    /// The model written into the folder ``folder`` (which exists) in COLMAP's binary format; the
    /// number of photos written. Before ``refine`` has run, or once the mapper stopped, a
    /// RuntimeError; an error of the disk an OSError.
    fn write_model(&self, py: Python<'_>, folder: &str) -> PyResult<usize> {
        if let Some(why) = &self.stopped {
            return Err(refused(format!("the mapper was stopped ({why}): nothing to write")));
        }
        let mapper = &self.mapper;
        py.detach(|| mapper.write_model(Path::new(folder))).map_err(|e| {
            if e.kind() == std::io::ErrorKind::Other {
                refused(e.to_string())
            } else {
                PyErr::from(e)
            }
        })
    }

    /// What the stages did (the engine's `MapperReport`) in plain types.
    fn report<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let r: MapperReport = self.mapper.report();
        let d = PyDict::new(py);
        d.set_item("photos", r.photos)?;
        d.set_item("edges", r.edges)?;
        d.set_item("pairs_left_out", r.pairs_left_out)?;
        d.set_item("pairs_too_few", r.pairs_too_few)?;
        let focals: Vec<(u32, f64, f64)> =
            r.focals.iter().map(|f| (self.camera_id(f.camera), f.before, f.after)).collect();
        d.set_item("focals", focals)?;
        d.set_item("rotations_placed", r.rotations_placed)?;
        d.set_item("edges_dropped", r.edges_dropped)?;
        d.set_item("components", r.components.clone())?;
        d.set_item("tracks", r.tracks)?;
        d.set_item("selected", r.selected)?;
        d.set_item("dropped_conflicts", r.dropped_conflicts)?;
        d.set_item("frame", r.frame.map(frame_name))?;
        d.set_item("frame_rotation", r.frame_rotation.map(|m| m.map(|row| row.to_vec()).to_vec()))?;
        d.set_item("frame_residual_deg", r.frame_residual_deg)?;
        d.set_item("gnss_outliers", r.gnss_outliers.clone())?;
        d.set_item("placed", r.placed)?;
        d.set_item("points", r.points)?;
        d.set_item("observations", r.observations)?;
        d.set_item("tracks_kept_share", r.tracks_kept_share)?;
        d.set_item("median_reprojection_px", r.median_reprojection_px)?;
        d.set_item("sigma_h", r.sigma_h)?;
        d.set_item("sigma_v", r.sigma_v)?;
        d.set_item("bias", per_flight(py, &r.bias)?)?;
        d.set_item("bias_sd", per_flight(py, &r.bias_sd)?)?;
        d.set_item("variance_factors", r.variance_factors.map(|v| (v[0], v[1], v[2])))?;
        d.set_item("rounds", rounds_list(py, &r.rounds)?)?;
        d.set_item("notes", r.notes.clone())?;
        d.set_item("stopped", self.stopped.clone())?;
        Ok(d)
    }
}
