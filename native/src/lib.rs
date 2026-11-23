//! `ppg_core`: native kernels of pyarchinit-photogrammetry (Python module, PyO3).
//!
//! Every function mirrors a numpy function of the Python package (named in its docstring), which
//! stays the fallback and the reference of the tests. The work runs without the GIL on all cores.

mod convert;
mod crinex;
mod native_map;
mod native_match;
mod ppk;
mod rtklib;

use std::collections::HashMap;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::{Arc, Mutex, OnceLock};

use numpy::ndarray::{Array1, Array2, Array3};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyArray3, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3,
            PyUntypedArrayMethods};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rayon::prelude::*;

// the kernels are in the crate ppg_engine (folder engine), which knows nothing of Python
use ppg_engine::camera::{Camera, Pose};
use ppg_engine::geometry::two_view;
use ppg_engine::{bundle, camera, depth, gpu, gpu_fuse, gpu_patchmatch, gpu_sift, patchmatch, sift_cpu, tin};

/// Version of the functions offered here: raised when one is added or changes meaning (9: crx2rnx,
/// 10: the TIN kernels, 11: raw_to_rinex and convert_cancel, 12: bundle_adjust and bundle_evaluate,
/// 13: the native matching - native_features, native_preselect, native_match_pairs, the stores,
/// 14: the options of its second stage - guided, guided_max_ratio, guided_band, 15: the guided check -
/// a match of the second stage also tested against the rivals the first stage saw, `guided_check`,
/// 16: the native mapper - NativeMapper, native_mapper_defaults, 17: the variances of the free intrinsics of a
/// bundle problem - bundle_intrinsics_variances).
const API: u32 = 17;

// one matcher per GPU, each behind its own lock: several GPUs work at the same time
fn matchers() -> &'static Mutex<HashMap<i64, Arc<Mutex<gpu::Matcher>>>> {
    static M: OnceLock<Mutex<HashMap<i64, Arc<Mutex<gpu::Matcher>>>>> = OnceLock::new();
    M.get_or_init(|| Mutex::new(HashMap::new()))
}

fn matcher_for(device: i64) -> Result<Arc<Mutex<gpu::Matcher>>, String> {
    let mut all = matchers().lock().map_err(|e| e.to_string())?;
    if let Some(m) = all.get(&device) {
        return Ok(m.clone());
    }
    let m = Arc::new(Mutex::new(gpu::Matcher::new(if device < 0 { None } else { Some(device as usize) })?));
    all.insert(device, m.clone());
    Ok(m)
}

fn patchmatchers() -> &'static Mutex<HashMap<i64, Arc<gpu_patchmatch::PatchMatcher>>> {
    static M: OnceLock<Mutex<HashMap<i64, Arc<gpu_patchmatch::PatchMatcher>>>> = OnceLock::new();
    M.get_or_init(|| Mutex::new(HashMap::new()))
}

fn fusers() -> &'static Mutex<HashMap<i64, Arc<Mutex<gpu_fuse::Fuser>>>> {
    static M: OnceLock<Mutex<HashMap<i64, Arc<Mutex<gpu_fuse::Fuser>>>>> = OnceLock::new();
    M.get_or_init(|| Mutex::new(HashMap::new()))
}

fn fuser_for(device: i64) -> Result<Arc<Mutex<gpu_fuse::Fuser>>, String> {
    let mut all = fusers().lock().map_err(|e| e.to_string())?;
    if let Some(m) = all.get(&device) {
        return Ok(m.clone());
    }
    let m = Arc::new(Mutex::new(gpu_fuse::Fuser::new(if device < 0 { None } else { Some(device as usize) })?));
    all.insert(device, m.clone());
    Ok(m)
}

fn patchmatcher_for(device: i64) -> Result<Arc<gpu_patchmatch::PatchMatcher>, String> {
    let mut all = patchmatchers().lock().map_err(|e| e.to_string())?;
    if let Some(m) = all.get(&device) {
        return Ok(m.clone());
    }
    let m = Arc::new(gpu_patchmatch::PatchMatcher::new(if device < 0 { None } else { Some(device as usize) })?);
    all.insert(device, m.clone());
    Ok(m)
}

/// The GPUs wgpu finds: ``index``, ``name``, ``backend`` (Metal, Vulkan, Dx12, Gl) and
/// ``device_type`` (DiscreteGpu, IntegratedGpu, VirtualGpu, Cpu, Other) of each.
#[pyfunction]
fn gpu_adapters(py: Python<'_>) -> PyResult<Vec<Bound<'_, PyDict>>> {
    let mut out = Vec::new();
    for a in py.detach(gpu::adapters) {
        let d = PyDict::new(py);
        d.set_item("index", a.index)?;
        d.set_item("name", a.name)?;
        d.set_item("backend", a.backend)?;
        d.set_item("device_type", a.device_type)?;
        out.push(d);
    }
    Ok(out)
}

fn descriptor_bytes(a: &PyReadonlyArray2<'_, u8>, what: &str) -> PyResult<Vec<u8>> {
    if a.shape()[1] != gpu::DIM {
        return Err(PyValueError::new_err(format!("{what}: descriptors of 128 bytes expected, {} given", a.shape()[1])));
    }
    Ok(a.as_array().iter().copied().collect())
}

/// SIFT matches of a pair of photos on the GPU (:mod:`gpu`): ``a`` and ``b`` the uint8
/// descriptors (n x 128, as COLMAP's database holds them), COLMAP's ``max_ratio``,
/// ``max_distance`` and ``cross_check``; ``device`` an index of :func:`gpu_adapters` or -1 for
/// the best one; ``a_key`` / ``b_key`` (not 0) keep the descriptors on the GPU for the next pairs
/// of the same photos. Returns the (m, 2) uint32 matches, rows of ``a`` and of ``b``. The GIL is
/// released while the GPU works.
#[pyfunction]
#[pyo3(signature = (a, b, max_ratio=0.8, max_distance=0.7, cross_check=true, device=-1, a_key=0, b_key=0))]
#[allow(clippy::too_many_arguments)]
fn gpu_match<'py>(py: Python<'py>, a: PyReadonlyArray2<'py, u8>, b: PyReadonlyArray2<'py, u8>, max_ratio: f64,
                  max_distance: f64, cross_check: bool, device: i64, a_key: u64, b_key: u64)
                  -> PyResult<Bound<'py, PyArray2<u32>>> {
    let av = descriptor_bytes(&a, "a")?;
    let bv = descriptor_bytes(&b, "b")?;
    let matches = py.detach(|| -> Result<Vec<[u32; 2]>, String> {
        let m = matcher_for(device)?;
        let mut g = m.lock().map_err(|e| e.to_string())?;
        g.matches(a_key, &av, b_key, &bv, max_ratio as f32, max_distance as f32, cross_check)
    }).map_err(PyRuntimeError::new_err)?;
    let mut out = Array2::<u32>::zeros((matches.len(), 2));
    for (k, m) in matches.iter().enumerate() {
        out[[k, 0]] = m[0];
        out[[k, 1]] = m[1];
    }
    Ok(out.into_pyarray(py))
}

/// The name of the GPU a :func:`gpu_match` ``device`` resolves to (the matcher is created).
#[pyfunction]
#[pyo3(signature = (device=-1))]
fn gpu_device_name(py: Python<'_>, device: i64) -> PyResult<String> {
    py.detach(|| -> Result<String, String> {
        let m = matcher_for(device)?;
        let g = m.lock().map_err(|e| e.to_string())?;
        Ok(g.info.name.clone())
    }).map_err(PyRuntimeError::new_err)
}

/// Drop the descriptors kept on the GPUs (and the matchers).
#[pyfunction]
fn gpu_forget(py: Python<'_>) -> PyResult<()> {
    py.detach(|| {
        if let Ok(mut all) = matchers().lock() {
            for (_, m) in all.iter() {
                if let Ok(mut g) = m.lock() {
                    g.clear();
                }
            }
            all.clear();
        }
        if let Ok(mut all) = patchmatchers().lock() {
            all.clear();
        }
        if let Ok(mut all) = fusers().lock() {
            for (_, m) in all.iter() {
                if let Ok(mut g) = m.lock() {
                    g.clear();
                }
            }
            all.clear();
        }
    });
    Ok(())
}

fn matrix3(a: &PyReadonlyArray2<f64>, what: &str) -> PyResult<[[f64; 3]; 3]> {
    if a.shape() != [3, 3] {
        return Err(PyValueError::new_err(format!("{what} must be 3x3, not {:?}", a.shape())));
    }
    let v = a.as_array();
    Ok([[v[[0, 0]], v[[0, 1]], v[[0, 2]]], [v[[1, 0]], v[[1, 1]], v[[1, 2]]], [v[[2, 0]], v[[2, 1]], v[[2, 2]]]])
}

fn pose(a: &PyReadonlyArray2<f64>) -> PyResult<Pose> {
    let s = a.shape();
    if s.len() != 2 || s[1] != 4 || (s[0] != 3 && s[0] != 4) {
        return Err(PyValueError::new_err(format!("cam_from_world must be 3x4 or 4x4, not {s:?}")));
    }
    let v = a.as_array();
    let mut m = [[0.0; 4]; 3];
    for (r, row) in m.iter_mut().enumerate() {
        for (c, x) in row.iter_mut().enumerate() {
            *x = v[[r, c]];
        }
    }
    Ok(m)
}

fn check_points(a: &PyReadonlyArray2<f64>) -> PyResult<()> {
    if a.shape().len() != 2 || a.shape()[1] != 3 {
        return Err(PyValueError::new_err(format!("points must be N x 3, not {:?}", a.shape())));
    }
    Ok(())
}

fn camera(model: &str, params: &[f64], width: f64, height: f64) -> PyResult<Camera> {
    Camera::new(model, params, width, height).map_err(PyValueError::new_err)
}

/// Fusion of the depth maps of one photo (`engine.dense._combine`): `stack` k x h x w float32.
#[pyfunction]
fn combine_depths<'py>(py: Python<'py>, stack: PyReadonlyArray3<'py, f32>, min_agree: usize, tol: f64)
    -> Bound<'py, PyArray2<f32>> {
    let a = stack.as_array();
    let (k, h, w) = a.dim();
    let c = a.as_standard_layout();
    let data = c.as_slice().unwrap();
    let out = py.detach(|| depth::combine(data, k, h, w, min_agree, tol));
    Array2::from_shape_vec((h, w), out).unwrap().into_pyarray(py)
}

/// Depth map of camera i from the disparities of a rectified pair (end of `engine.dense._pair_depth`).
#[pyfunction]
#[pyo3(signature = (disp, min_valid, f, baseline, cx, cy, r1, k, out_shape))]
#[allow(clippy::too_many_arguments)]
fn disparity_to_depth<'py>(py: Python<'py>, disp: PyReadonlyArray2<'py, f32>, min_valid: f64, f: f64, baseline: f64,
                           cx: f64, cy: f64, r1: PyReadonlyArray2<'py, f64>, k: (f64, f64, f64, f64),
                           out_shape: (usize, usize)) -> PyResult<Bound<'py, PyArray2<f32>>> {
    let r1 = matrix3(&r1, "r1")?;
    let a = disp.as_array();
    let (h, w) = a.dim();
    let c = a.as_standard_layout();
    let data = c.as_slice().unwrap();
    let (oh, ow) = out_shape;
    let out = py.detach(|| depth::disparity_to_depth(data, h, w, min_valid, f, baseline, cx, cy, &r1, k, oh, ow));
    Ok(Array2::from_shape_vec((oh, ow), out).unwrap().into_pyarray(py))
}

/// Normals of a depth map (`engine.dense._normals_from_depth`), h x w x 3 float32.
#[pyfunction]
fn depth_normals<'py>(py: Python<'py>, depth: PyReadonlyArray2<'py, f32>, k: (f64, f64, f64, f64))
    -> Bound<'py, PyArray3<f32>> {
    let a = depth.as_array();
    let (h, w) = a.dim();
    let c = a.as_standard_layout();
    let data = c.as_slice().unwrap();
    let out = py.detach(|| depth::normals(data, h, w, k));
    Array3::from_shape_vec((h, w, 3), out).unwrap().into_pyarray(py)
}

/// Candidate points (`cand`, indices into `points`) a depth map agrees with
/// (`engine.confidence._agreeing`).
#[pyfunction]
#[pyo3(signature = (points, cand, depth, k, rot, t, tol, abs_tol=None))]
#[allow(clippy::too_many_arguments)]
fn depth_agree<'py>(py: Python<'py>, points: PyReadonlyArray2<'py, f64>, cand: PyReadonlyArray1<'py, i64>,
                    depth: PyReadonlyArray2<'py, f32>, k: (f64, f64, f64, f64), rot: PyReadonlyArray2<'py, f64>,
                    t: PyReadonlyArray1<'py, f64>, tol: f64, abs_tol: Option<PyReadonlyArray1<'py, f64>>)
    -> PyResult<Bound<'py, PyArray1<bool>>> {
    let rot = matrix3(&rot, "rot")?;
    let t = t.as_array();
    if t.len() != 3 {
        return Err(PyValueError::new_err("t must hold 3 values"));
    }
    let t = [t[0], t[1], t[2]];
    check_points(&points)?;
    let view = points.as_array();
    let p = view.as_standard_layout();
    let n = p.nrows();
    let pts = p.as_slice().unwrap();
    let ca = cand.as_array();
    let cc = ca.as_standard_layout();
    let cand = cc.as_slice().unwrap();
    if cand.iter().any(|&i| i < 0 || i as usize >= n) {
        return Err(PyValueError::new_err("candidate index out of range"));
    }
    let tol_array = abs_tol.as_ref().map(|a| a.as_array());
    let tol_cow = tol_array.as_ref().map(|a| a.as_standard_layout());
    let tol_slice = tol_cow.as_ref().map(|a| a.as_slice().unwrap());
    if let Some(s) = tol_slice {
        if s.len() != n {
            return Err(PyValueError::new_err("abs_tol must hold one value per point"));
        }
    }
    let d = depth.as_array();
    let (h, w) = d.dim();
    let dc = d.as_standard_layout();
    let dmap = dc.as_slice().unwrap();
    let out = py.detach(|| depth::agree(pts, cand, dmap, h, w, k, &rot, &t, tol, tol_slice));
    Ok(Array1::from_vec(out).into_pyarray(py))
}

/// Pixels, depth and in-frame flag of points in a camera (`engine.visibility.project_valid`).
#[pyfunction]
#[pyo3(signature = (points, cam_from_world, model, params, width, height, margin=2.0))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
fn project_points<'py>(py: Python<'py>, points: PyReadonlyArray2<'py, f64>, cam_from_world: PyReadonlyArray2<'py, f64>,
                       model: &str, params: Vec<f64>, width: f64, height: f64, margin: f64)
    -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<bool>>)> {
    let cam = camera(model, &params, width, height)?;
    let m = pose(&cam_from_world)?;
    check_points(&points)?;
    let view = points.as_array();
    let p = view.as_standard_layout();
    let n = p.nrows();
    let src = p.as_slice().unwrap();
    let mut px = vec![f64::NAN; 2 * n];
    let mut depth = vec![0f64; n];
    let mut ok = vec![false; n];
    py.detach(|| {
        px.par_chunks_mut(2 * 4096)
            .zip(depth.par_chunks_mut(4096))
            .zip(ok.par_chunks_mut(4096))
            .enumerate()
            .for_each(|(b, ((pb, db), ob))| {
                for j in 0..db.len() {
                    let i = b * 4096 + j;
                    let (u, v, d, good) = camera::project_valid(&cam, &m, src[3 * i], src[3 * i + 1], src[3 * i + 2], margin);
                    pb[2 * j] = u;
                    pb[2 * j + 1] = v;
                    db[j] = d;
                    ob[j] = good;
                }
            });
    });
    Ok((Array2::from_shape_vec((n, 2), px).unwrap().into_pyarray(py), Array1::from_vec(depth).into_pyarray(py),
        Array1::from_vec(ok).into_pyarray(py)))
}

/// Pixels and score of points in one photo of an orthomosaic tile (`engine.ortho._scores`): -inf
/// where the photo does not see the point or sees it further than the vertical than `min_cos`.
#[pyfunction]
#[pyo3(signature = (points, cam_from_world, model, params, width, height, centre, up, min_cos, margin=2.0))]
#[allow(clippy::too_many_arguments)]
fn ortho_scores<'py>(py: Python<'py>, points: PyReadonlyArray2<'py, f64>, cam_from_world: PyReadonlyArray2<'py, f64>,
                     model: &str, params: Vec<f64>, width: f64, height: f64, centre: [f64; 3], up: [f64; 3],
                     min_cos: f64, margin: f64) -> PyResult<(Bound<'py, PyArray2<f64>>, Bound<'py, PyArray1<f32>>)> {
    let cam = camera(model, &params, width, height)?;
    let m = pose(&cam_from_world)?;
    check_points(&points)?;
    let view = points.as_array();
    let p = view.as_standard_layout();
    let n = p.nrows();
    let src = p.as_slice().unwrap();
    let focal = cam.focal();
    let mut px = vec![f64::NAN; 2 * n];
    let mut score = vec![f32::NEG_INFINITY; n];
    py.detach(|| {
        px.par_chunks_mut(2 * 4096).zip(score.par_chunks_mut(4096)).enumerate().for_each(|(b, (pb, sb))| {
            for j in 0..sb.len() {
                let i = b * 4096 + j;
                let (x, y, z) = (src[3 * i], src[3 * i + 1], src[3 * i + 2]);
                let (u, v, depth, ok) = camera::project_valid(&cam, &m, x, y, z, margin);
                pb[2 * j] = u;
                pb[2 * j + 1] = v;
                if !ok {
                    continue;
                }
                let (rx, ry, rz) = (x - centre[0], y - centre[1], z - centre[2]);
                let len = (rx * rx + ry * ry + rz * rz).sqrt();
                let (rx, ry, rz) = (rx / len, ry / len, rz / len);
                let nadir = (-(rx * up[0] + ry * up[1] + rz * up[2])).clamp(0.0, 1.0);
                let off = (u / cam.width - 0.5).hypot(v / cam.height - 0.5);
                let gsd = depth / focal;
                let s = nadir.powi(2) * (1.0 - off) / gsd.max(1e-12);
                sb[j] = if nadir >= min_cos { s as f32 } else { f32::NEG_INFINITY };
            }
        });
    });
    Ok((Array2::from_shape_vec((n, 2), px).unwrap().into_pyarray(py), Array1::from_vec(score).into_pyarray(py)))
}

/// The `k` best photos of every point of an orthomosaic tile among all the photos given, as
/// `engine.ortho._candidates` ranks them: photo index (-1 for none), score (float32, best first,
/// ties to the earlier photo) and pixel (float32). One pass over the points, every photo scored
/// in turn, so the choice of a point never depends on the tile it belongs to.
#[pyfunction]
#[pyo3(signature = (points, poses, models, params, sizes, centres, up, min_cos, k=4, margin=2.0))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
fn ortho_candidates<'py>(py: Python<'py>, points: PyReadonlyArray2<'py, f64>, poses: PyReadonlyArray3<'py, f64>,
                         models: Vec<String>, params: Vec<Vec<f64>>, sizes: Vec<(f64, f64)>,
                         centres: PyReadonlyArray2<'py, f64>, up: [f64; 3], min_cos: f64, k: usize, margin: f64)
    -> PyResult<(Bound<'py, PyArray2<i32>>, Bound<'py, PyArray2<f32>>, Bound<'py, PyArray3<f32>>)> {
    let pa = poses.as_array();
    let m = pa.shape()[0];
    if pa.shape()[1] < 3 || pa.shape()[2] != 4 || models.len() != m || params.len() != m || sizes.len() != m
        || centres.shape() != [m, 3] {
        return Err(PyValueError::new_err("one pose (3x4), model, parameters, size and centre per photo"));
    }
    let mut cams = Vec::with_capacity(m);
    let mut mats = Vec::with_capacity(m);
    let ca = centres.as_array();
    let mut ctr = Vec::with_capacity(m);
    for j in 0..m {
        cams.push(camera(&models[j], &params[j], sizes[j].0, sizes[j].1)?);
        let mut mat = [[0.0; 4]; 3];
        for (r, row) in mat.iter_mut().enumerate() {
            for (c, x) in row.iter_mut().enumerate() {
                *x = pa[[j, r, c]];
            }
        }
        mats.push(mat);
        ctr.push([ca[[j, 0]], ca[[j, 1]], ca[[j, 2]]]);
    }
    check_points(&points)?;
    let view = points.as_array();
    let p = view.as_standard_layout();
    let n = p.nrows();
    let src = p.as_slice().unwrap();
    let k = k.max(1);
    let mut index = vec![-1i32; k * n];
    let mut score = vec![f32::NEG_INFINITY; k * n];
    let mut pix = vec![f32::NAN; 2 * k * n];
    py.detach(|| {
        let block = 2048;
        let results: Vec<(usize, Vec<i32>, Vec<f32>, Vec<f32>)> = (0..n.div_ceil(block))
            .into_par_iter()
            .map(|b| {
                let lo = b * block;
                let hi = (lo + block).min(n);
                let len = hi - lo;
                let mut bi = vec![-1i32; k * len];
                let mut bs = vec![f32::NEG_INFINITY; k * len];
                let mut bp = vec![f32::NAN; 2 * k * len];
                for i in lo..hi {
                    let o = i - lo;
                    let (x, y, z) = (src[3 * i], src[3 * i + 1], src[3 * i + 2]);
                    for j in 0..m {
                        let cam = &cams[j];
                        let (u, v, depth, ok) = camera::project_valid(cam, &mats[j], x, y, z, margin);
                        if !ok {
                            continue;
                        }
                        let c = &ctr[j];
                        let (rx, ry, rz) = (x - c[0], y - c[1], z - c[2]);
                        let len3 = (rx * rx + ry * ry + rz * rz).sqrt();
                        let (rx, ry, rz) = (rx / len3, ry / len3, rz / len3);
                        let nadir = (-(rx * up[0] + ry * up[1] + rz * up[2])).clamp(0.0, 1.0);
                        if !(nadir >= min_cos) {
                            continue;
                        }
                        let off = (u / cam.width - 0.5).hypot(v / cam.height - 0.5);
                        let gsd = depth / cam.focal();
                        let s = (nadir.powi(2) * (1.0 - off) / gsd.max(1e-12)) as f32;
                        if !s.is_finite() {
                            continue;
                        }
                        // insert keeping the best first; an equal score stays behind the earlier photo
                        let mut at = k;
                        for r in 0..k {
                            if s > bs[r * len + o] {
                                at = r;
                                break;
                            }
                        }
                        if at == k {
                            continue;
                        }
                        for r in (at + 1..k).rev() {
                            bs[r * len + o] = bs[(r - 1) * len + o];
                            bi[r * len + o] = bi[(r - 1) * len + o];
                            bp[2 * (r * len + o)] = bp[2 * ((r - 1) * len + o)];
                            bp[2 * (r * len + o) + 1] = bp[2 * ((r - 1) * len + o) + 1];
                        }
                        bs[at * len + o] = s;
                        bi[at * len + o] = j as i32;
                        bp[2 * (at * len + o)] = u as f32;
                        bp[2 * (at * len + o) + 1] = v as f32;
                    }
                }
                (lo, bi, bs, bp)
            })
            .collect();
        for (lo, bi, bs, bp) in results {
            let len = bs.len() / k;
            for r in 0..k {
                for o in 0..len {
                    index[r * n + lo + o] = bi[r * len + o];
                    score[r * n + lo + o] = bs[r * len + o];
                    pix[2 * (r * n + lo + o)] = bp[2 * (r * len + o)];
                    pix[2 * (r * n + lo + o) + 1] = bp[2 * (r * len + o) + 1];
                }
            }
        }
    });
    Ok((Array2::from_shape_vec((k, n), index).unwrap().into_pyarray(py),
        Array2::from_shape_vec((k, n), score).unwrap().into_pyarray(py),
        Array3::from_shape_vec((k, n, 2), pix).unwrap().into_pyarray(py)))
}

type SourceArgs<'py> = (PyReadonlyArray2<'py, f32>, (f64, f64, f64, f64), PyReadonlyArray2<'py, f64>,
                        PyReadonlyArray1<'py, f64>);

/// PatchMatch multi-view stereo of one photo (`engine.dense._patchmatch_depth_maps`, see
/// `patchmatch.rs`): `reference` h x w grey 0-1, `k` (fx, fy, cx, cy) with the centre of the first
/// pixel at 0, `sources` as `(image, k, r, t)` with `x_src = r x_ref + t`. Returns depth (h x w,
/// 0 where the cost is above `max_cost`), normals (h x w x 3, camera frame) and cost (h x w).
#[pyfunction]
#[pyo3(signature = (reference, k, sources, depth_range, init_depth=None, init_normals=None, iterations=4,
                    half_window=4, step=2, top_k=3, max_cost=0.6, seed=1, min_views=2))]
#[allow(clippy::too_many_arguments, clippy::type_complexity)]
fn patchmatch_depth<'py>(py: Python<'py>, reference: PyReadonlyArray2<'py, f32>, k: (f64, f64, f64, f64),
                         sources: Vec<SourceArgs<'py>>, depth_range: (f64, f64),
                         init_depth: Option<PyReadonlyArray2<'py, f32>>, init_normals: Option<PyReadonlyArray3<'py, f32>>,
                         iterations: usize, half_window: usize, step: usize, top_k: usize, max_cost: f32, seed: u64,
                         min_views: usize)
    -> PyResult<(Bound<'py, PyArray2<f32>>, Bound<'py, PyArray3<f32>>, Bound<'py, PyArray2<f32>>)> {
    let a = reference.as_array();
    let (h, w) = a.dim();
    let c = a.as_standard_layout();
    let refdata = c.as_slice().unwrap();
    if !(depth_range.0 > 0.0 && depth_range.1 > depth_range.0) {
        return Err(PyValueError::new_err("depth_range must be (near, far) with 0 < near < far"));
    }
    let views: Vec<_> = sources.iter().map(|(img, _, _, _)| img.as_array()).collect();
    let layouts: Vec<_> = views.iter().map(|v| v.as_standard_layout()).collect();
    let mut srcs = Vec::with_capacity(sources.len());
    for ((img, kk, r, t), lay) in sources.iter().zip(layouts.iter()) {
        let (sh, sw) = img.as_array().dim();
        let t = t.as_array();
        if t.len() != 3 {
            return Err(PyValueError::new_err("t must hold 3 values"));
        }
        srcs.push(patchmatch::Source { img: lay.as_slice().unwrap(), w: sw, h: sh, k: [kk.0, kk.1, kk.2, kk.3],
                                       r: matrix3(r, "r")?, t: [t[0], t[1], t[2]] });
    }
    let init_view = init_depth.as_ref().map(|d| d.as_array());
    let init_layout = init_view.as_ref().map(|v| v.as_standard_layout());
    if let Some(v) = &init_view {
        if v.dim() != (h, w) {
            return Err(PyValueError::new_err("init_depth must have the shape of the reference"));
        }
    }
    let normal_view = init_normals.as_ref().map(|d| d.as_array());
    let normal_layout = normal_view.as_ref().map(|v| v.as_standard_layout());
    if let Some(v) = &normal_view {
        if v.dim() != (h, w, 3) {
            return Err(PyValueError::new_err("init_normals must be h x w x 3"));
        }
    }
    let init = init_layout.as_ref().map(|l| l.as_slice().unwrap());
    let init_n = normal_layout.as_ref().map(|l| l.as_slice().unwrap());
    let params = patchmatch::Params { depth_min: depth_range.0, depth_max: depth_range.1, iterations, half_window,
                                      step, top_k, min_views, max_cost, seed };
    let out = py.detach(|| patchmatch::patchmatch(refdata, w, h, [k.0, k.1, k.2, k.3], &srcs, params, init,
                                                  init_n));
    Ok((Array2::from_shape_vec((h, w), out.depth).unwrap().into_pyarray(py),
        Array3::from_shape_vec((h, w, 3), out.normal).unwrap().into_pyarray(py),
        Array2::from_shape_vec((h, w), out.cost).unwrap().into_pyarray(py)))
}

/// :func:`patchmatch_depth` on the GPU (:mod:`gpu_patchmatch`): the same arguments and results,
/// ``device`` an index of :func:`gpu_adapters` or -1 for the best one; up to six sources.
#[pyfunction]
#[pyo3(signature = (reference, k, sources, depth_range, init_depth=None, init_normals=None, iterations=4,
                    half_window=4, step=2, top_k=3, max_cost=0.5, seed=7, min_views=1, device=-1))]
#[allow(clippy::too_many_arguments)]
fn gpu_patchmatch_depth<'py>(py: Python<'py>, reference: PyReadonlyArray2<'py, f32>, k: (f64, f64, f64, f64),
                             sources: Vec<SourceArgs<'py>>, depth_range: (f64, f64),
                             init_depth: Option<PyReadonlyArray2<'py, f32>>, init_normals: Option<PyReadonlyArray3<'py, f32>>,
                             iterations: usize, half_window: usize, step: usize, top_k: usize, max_cost: f32, seed: u64,
                             min_views: usize, device: i64)
    -> PyResult<(Bound<'py, PyArray2<f32>>, Bound<'py, PyArray3<f32>>, Bound<'py, PyArray2<f32>>)> {
    let a = reference.as_array();
    let (h, w) = a.dim();
    let c = a.as_standard_layout();
    let refdata = c.as_slice().unwrap();
    if !(depth_range.0 > 0.0 && depth_range.1 > depth_range.0) {
        return Err(PyValueError::new_err("depth_range must be (near, far) with 0 < near < far"));
    }
    let views: Vec<_> = sources.iter().map(|(img, _, _, _)| img.as_array()).collect();
    let layouts: Vec<_> = views.iter().map(|v| v.as_standard_layout()).collect();
    let mut srcs = Vec::with_capacity(sources.len());
    for ((img, kk, r, t), lay) in sources.iter().zip(layouts.iter()) {
        let (sh, sw) = img.as_array().dim();
        let t = t.as_array();
        if t.len() != 3 {
            return Err(PyValueError::new_err("t must hold 3 values"));
        }
        srcs.push(gpu_patchmatch::Source { img: lay.as_slice().unwrap(), w: sw, h: sh, k: [kk.0, kk.1, kk.2, kk.3],
                                           r: matrix3(r, "r")?, t: [t[0], t[1], t[2]] });
    }
    let init_view = init_depth.as_ref().map(|d| d.as_array());
    let init_layout = init_view.as_ref().map(|v| v.as_standard_layout());
    if let Some(v) = &init_view {
        if v.dim() != (h, w) {
            return Err(PyValueError::new_err("init_depth must have the shape of the reference"));
        }
    }
    let normal_view = init_normals.as_ref().map(|d| d.as_array());
    let normal_layout = normal_view.as_ref().map(|v| v.as_standard_layout());
    if let Some(v) = &normal_view {
        if v.dim() != (h, w, 3) {
            return Err(PyValueError::new_err("init_normals must be h x w x 3"));
        }
    }
    let init = init_layout.as_ref().map(|l| l.as_slice().unwrap());
    let init_n = normal_layout.as_ref().map(|l| l.as_slice().unwrap());
    let opts = gpu_patchmatch::Options { depth_min: depth_range.0, depth_max: depth_range.1, iterations, half_window,
                                         step, top_k, min_views, max_cost, seed };
    let out = py.detach(|| -> Result<gpu_patchmatch::Output, String> {
        let m = patchmatcher_for(device)?;
        m.run(refdata, w, h, [k.0, k.1, k.2, k.3], &srcs, opts, init, init_n)
    }).map_err(PyRuntimeError::new_err)?;
    Ok((Array2::from_shape_vec((h, w), out.depth).unwrap().into_pyarray(py),
        Array3::from_shape_vec((h, w, 3), out.normal).unwrap().into_pyarray(py),
        Array2::from_shape_vec((h, w), out.cost).unwrap().into_pyarray(py)))
}

/// SIFT key points and descriptors of a grey image (``h x w`` float32 in [0, 1]) on the GPU, as
/// VLFeat and COLMAP compute them: ``(keypoints (n, 4) float32 - x, y, scale, orientation with the
/// centre of the first pixel at (0, 0) -, descriptors (n, 128) uint8)``. ``device`` -1 = the best
/// GPU, else the adapter index.
#[pyfunction]
#[pyo3(name = "gpu_sift")]
#[pyo3(signature = (image, max_features=8192, first_octave=-1, num_octaves=4, octave_resolution=3,
                    peak_threshold=0.02 / 3.0, edge_threshold=10.0, max_orientations=2, upright=false, device=-1))]
#[allow(clippy::too_many_arguments)]
fn gpu_sift_features<'py>(py: Python<'py>, image: PyReadonlyArray2<'py, f32>, max_features: usize, first_octave: i32,
                 num_octaves: u32, octave_resolution: u32, peak_threshold: f32, edge_threshold: f32,
                 max_orientations: u32, upright: bool, device: i64)
    -> PyResult<(Bound<'py, PyArray2<f32>>, Bound<'py, PyArray2<u8>>)> {
    let a = image.as_array();
    let (h, w) = (a.shape()[0], a.shape()[1]);
    let data: Vec<f32> = a.iter().copied().collect();
    let o = gpu_sift::Options { first_octave, num_octaves, octave_resolution, peak_threshold, edge_threshold,
                                max_orientations, upright, max_features };
    let (kp, desc) = py.detach(|| -> Result<(Vec<[f32; 4]>, Vec<u8>), String> {
        let s = gpu_sift::sift_for(device)?;
        s.extract(&data, w, h, &o)
    }).map_err(PyRuntimeError::new_err)?;
    sift_arrays(py, kp, desc)
}

/// The key points ``(n, 4)`` and descriptors ``(n, 128)`` of a SIFT extraction as numpy arrays.
pub(crate) fn sift_arrays(py: Python<'_>, kp: Vec<[f32; 4]>, desc: Vec<u8>)
    -> PyResult<(Bound<'_, PyArray2<f32>>, Bound<'_, PyArray2<u8>>)> {
    let n = kp.len();
    let flat: Vec<f32> = kp.iter().flat_map(|k| k.iter().copied()).collect();
    Ok((Array2::from_shape_vec((n, 4), flat).unwrap().into_pyarray(py),
        Array2::from_shape_vec((n, gpu_sift::DESC), desc).unwrap().into_pyarray(py)))
}

/// SIFT key points and descriptors of a grey image (``h x w`` float32 in [0, 1]) on the CPU, the
/// twin of :func:`gpu_sift` (`sift_cpu::extract`): the same points, descriptors, units (the centre
/// of the first pixel at (0, 0)) and order, on the threads of rayon, the GIL released. An image
/// smaller than 8 x 8 or a first octave below -1 is a RuntimeError, as on the GPU; so is a panic.
#[pyfunction]
#[pyo3(name = "cpu_sift")]
#[pyo3(signature = (image, max_features=8192, first_octave=-1, num_octaves=4, octave_resolution=3,
                    peak_threshold=0.02 / 3.0, edge_threshold=10.0, max_orientations=2, upright=false))]
#[allow(clippy::too_many_arguments)]
fn cpu_sift_features<'py>(py: Python<'py>, image: PyReadonlyArray2<'py, f32>, max_features: usize, first_octave: i32,
                 num_octaves: u32, octave_resolution: u32, peak_threshold: f32, edge_threshold: f32,
                 max_orientations: u32, upright: bool)
    -> PyResult<(Bound<'py, PyArray2<f32>>, Bound<'py, PyArray2<u8>>)> {
    let a = image.as_array();
    let (h, w) = (a.shape()[0], a.shape()[1]);
    let data: Vec<f32> = a.iter().copied().collect();
    let o = gpu_sift::Options { first_octave, num_octaves, octave_resolution, peak_threshold, edge_threshold,
                                max_orientations, upright, max_features };
    let (kp, desc) = py.detach(|| catch_unwind(AssertUnwindSafe(|| sift_cpu::extract(&data, w, h, &o))))
        .map_err(|payload| PyRuntimeError::new_err(format!("cpu_sift: {}", panic_text(&*payload))))?
        .map_err(PyRuntimeError::new_err)?;
    sift_arrays(py, kp, desc)
}

type FuseMapArgs<'py> = (i64, Option<PyReadonlyArray2<'py, f32>>, (f64, f64, f64, f64), PyReadonlyArray2<'py, f64>,
                         PyReadonlyArray1<'py, f64>, u32);

/// The dense points of one depth map fused with its covisible maps on the GPU (API 7): every map
/// is ``(key, depth (h x w float32) or None when the GPU holds it already, (fx, fy, cx, cy),
/// cam_from_world rotation 3x3, translation, order)``. Returns ``(xyz (n, 3) float32, normals
/// (n, 3) float32, pixel (n,) uint32 - row * w + col of the reference -, pixels (n,) uint32 fused
/// into each point, support (n,) uint32 maps agreeing on it, the reference included)``.
#[pyfunction]
#[pyo3(name = "gpu_fuse")]
#[pyo3(signature = (reference, neighbours, min_pixels=3, max_reproj=2.0, max_depth_rel=0.01, max_normal_deg=20.0,
                    support_tol=0.01, device=-1))]
#[allow(clippy::too_many_arguments)]
fn gpu_fuse_depth_maps<'py>(py: Python<'py>, reference: FuseMapArgs<'py>, neighbours: Vec<FuseMapArgs<'py>>,
                            min_pixels: u32, max_reproj: f32, max_depth_rel: f32, max_normal_deg: f32, support_tol: f32,
                            device: i64)
    -> PyResult<(Bound<'py, PyArray2<f32>>, Bound<'py, PyArray2<f32>>, Bound<'py, PyArray1<u32>>, Bound<'py, PyArray1<u32>>,
                 Bound<'py, PyArray1<u32>>)> {
    let all: Vec<&FuseMapArgs<'py>> = std::iter::once(&reference).chain(neighbours.iter()).collect();
    let views: Vec<_> = all.iter().map(|(_, d, _, _, _, _)| d.as_ref().map(|a| a.as_array())).collect();
    let layouts: Vec<_> = views.iter().map(|v| v.as_ref().map(|a| a.as_standard_layout())).collect();
    let mut maps = Vec::with_capacity(all.len());
    for ((key, _, kk, r, t, order), lay) in all.iter().zip(layouts.iter()) {
        let t = t.as_array();
        if t.len() != 3 {
            return Err(PyValueError::new_err("t must hold 3 values"));
        }
        let (depth, w, h) = match lay {
            Some(l) => { let (h, w) = l.dim(); (Some(l.as_slice().unwrap()), w, h) }
            None => (None, 0, 0),
        };
        maps.push(gpu_fuse::Map { key: *key, depth, w, h, k: [kk.0, kk.1, kk.2, kk.3], r: matrix3(r, "r")?,
                                  t: [t[0], t[1], t[2]], order: *order });
    }
    let o = gpu_fuse::Options { min_pixels, max_reproj, max_depth_rel, max_normal_deg, support_tol };
    let points = py.detach(|| -> Result<Vec<gpu_fuse::Fused>, String> {
        let f = fuser_for(device)?;
        let mut g = f.lock().map_err(|e| e.to_string())?;
        g.fuse(&maps[0], &maps[1..], &o)
    }).map_err(PyRuntimeError::new_err)?;
    let n = points.len();
    let xyz: Vec<f32> = points.iter().flat_map(|p| [p.x, p.y, p.z]).collect();
    let nrm: Vec<f32> = points.iter().flat_map(|p| [p.nx, p.ny, p.nz]).collect();
    let pixel: Vec<u32> = points.iter().map(|p| p.pixel).collect();
    let pixels: Vec<u32> = points.iter().map(|p| p.counts >> 16).collect();
    let support: Vec<u32> = points.iter().map(|p| p.counts & 0xffff).collect();
    Ok((Array2::from_shape_vec((n, 3), xyz).unwrap().into_pyarray(py),
        Array2::from_shape_vec((n, 3), nrm).unwrap().into_pyarray(py),
        Array1::from_vec(pixel).into_pyarray(py), Array1::from_vec(pixels).into_pyarray(py),
        Array1::from_vec(support).into_pyarray(py)))
}

/// Drop the depth maps a GPU holds for the fusion (their keys are unique within one workspace).
#[pyfunction]
#[pyo3(signature = (device=-1))]
fn gpu_fuse_forget(py: Python<'_>, device: i64) -> PyResult<()> {
    py.detach(|| -> Result<(), String> {
        let f = fuser_for(device)?;
        let mut g = f.lock().map_err(|e| e.to_string())?;
        g.clear();
        Ok(())
    }).map_err(PyRuntimeError::new_err)
}

/// The keys of the depth maps a GPU holds for the fusion.
#[pyfunction]
#[pyo3(signature = (device=-1))]
fn gpu_fuse_cached(py: Python<'_>, device: i64) -> PyResult<Vec<i64>> {
    py.detach(|| -> Result<Vec<i64>, String> {
        let f = fuser_for(device)?;
        let g = f.lock().map_err(|e| e.to_string())?;
        Ok(g.cached())
    }).map_err(PyRuntimeError::new_err)
}

fn xy_rows(a: &PyReadonlyArray2<'_, f64>) -> PyResult<Vec<[f64; 2]>> {
    if a.shape()[1] != 2 {
        return Err(PyValueError::new_err(format!("xy must be N x 2, not {:?}", a.shape())));
    }
    let v = a.as_array();
    Ok((0..a.shape()[0]).map(|i| [v[[i, 0]], v[[i, 1]]]).collect())
}

fn triangle_rows(a: &PyReadonlyArray2<'_, i64>, n: usize) -> PyResult<Vec<[usize; 3]>> {
    if a.shape()[1] != 3 {
        return Err(PyValueError::new_err(format!("triangles must be T x 3, not {:?}", a.shape())));
    }
    let v = a.as_array();
    let mut out = Vec::with_capacity(a.shape()[0]);
    for i in 0..a.shape()[0] {
        let t = [v[[i, 0]], v[[i, 1]], v[[i, 2]]];
        if t.iter().any(|&k| k < 0 || k as usize >= n) {
            return Err(PyValueError::new_err(format!("triangle {i}: vertex index out of range")));
        }
        out.push([t[0] as usize, t[1] as usize, t[2] as usize]);
    }
    Ok(out)
}

fn heights(z: &PyReadonlyArray1<'_, f64>, n: usize) -> PyResult<Vec<f64>> {
    if z.len() != n {
        return Err(PyValueError::new_err(format!("z has {} values for {n} points", z.len())));
    }
    Ok(z.as_array().iter().copied().collect())
}

type TinTriangulation<'py> = (Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<i64>>, Bound<'py, PyArray2<f64>>,
                              Bound<'py, PyArray2<i64>>);

/// Constrained Delaunay triangulation of a TIN (`engine.tin.triangulate`, the crate spade): ``xy``
/// (n x 2, local coordinates), ``constraints`` (m x 2 point indices: break lines, boundaries).
/// Returns the counter-clockwise ``triangles`` (t x 3), the ``constrained`` edges of the
/// triangulation (k x 2, smaller index first), and the vertices made where constraints cross:
/// ``new_xy`` (j x 2, index n + row in the triangles) and ``new_on`` (j x 2, the endpoints of the
/// constraint each one lies on). Points at the same place are one vertex, the first of them.
#[pyfunction]
fn tin_triangulate<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, constraints: PyReadonlyArray2<'py, i64>)
                        -> PyResult<TinTriangulation<'py>> {
    let pts = xy_rows(&xy)?;
    let s = constraints.shape();
    if s[0] > 0 && s[1] != 2 {
        return Err(PyValueError::new_err(format!("constraints must be M x 2, not {s:?}")));
    }
    let c = constraints.as_array();
    let mut cons = Vec::with_capacity(s[0]);
    for i in 0..s[0] {
        let (a, b) = (c[[i, 0]], c[[i, 1]]);
        if a < 0 || b < 0 {
            return Err(PyValueError::new_err(format!("constraint {i}: negative index")));
        }
        cons.push([a as usize, b as usize]);
    }
    let r = py.detach(|| tin::triangulate(&pts, &cons)).map_err(PyValueError::new_err)?;
    let tri = Array2::from_shape_vec((r.triangles.len(), 3), r.triangles.concat()).unwrap();
    let con = Array2::from_shape_vec((r.constrained.len(), 2), r.constrained.concat()).unwrap();
    let nxy = Array2::from_shape_vec((r.new_xy.len(), 2), r.new_xy.concat()).unwrap();
    let non = Array2::from_shape_vec((r.new_on.len(), 2), r.new_on.concat()).unwrap();
    Ok((tri.into_pyarray(py), con.into_pyarray(py), nxy.into_pyarray(py),
        non.into_pyarray(py)))
}

/// The TIN on a grid (`engine.tin.rasterize`): ``height`` x ``width`` float32 cells of side
/// ``cell``, top-left corner (``x0``, ``y0``), each centre linear in its triangle, NaN outside.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn tin_rasterize<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                      triangles: PyReadonlyArray2<'py, i64>, x0: f64, y0: f64, cell: f64, width: usize,
                      height: usize) -> PyResult<Bound<'py, PyArray2<f32>>> {
    if !(cell > 0.0) {
        return Err(PyValueError::new_err("cell must be positive"));
    }
    if width.checked_mul(height).and_then(|n| n.checked_mul(4)).is_none() {
        return Err(PyValueError::new_err(format!("a grid of {width} x {height} cells is too large")));
    }
    let pts = xy_rows(&xy)?;
    let h = heights(&z, pts.len())?;
    let tri = triangle_rows(&triangles, pts.len())?;
    let grid = py.detach(|| tin::rasterize(&pts, &h, &tri, x0, y0, cell, width, height));
    Ok(Array2::from_shape_vec((height, width), grid).unwrap().into_pyarray(py))
}

/// Contour segments of a TIN (`engine.tin.contour_segments`): (s x 2 x 2) segments and the
/// index of their level in ``levels`` (s).
#[pyfunction]
fn tin_contours<'py>(py: Python<'py>, xy: PyReadonlyArray2<'py, f64>, z: PyReadonlyArray1<'py, f64>,
                     triangles: PyReadonlyArray2<'py, i64>, levels: PyReadonlyArray1<'py, f64>)
                     -> PyResult<(Bound<'py, PyArray3<f64>>, Bound<'py, PyArray1<i64>>)> {
    let pts = xy_rows(&xy)?;
    let h = heights(&z, pts.len())?;
    let tri = triangle_rows(&triangles, pts.len())?;
    let lv: Vec<f64> = levels.as_array().iter().copied().collect();
    let (segs, idx) = py.detach(|| tin::contours(&pts, &h, &tri, &lv));
    let flat: Vec<f64> = segs.iter().flat_map(|s| [s[0][0], s[0][1], s[1][0], s[1][1]]).collect();
    let s = Array3::from_shape_vec((segs.len(), 2, 2), flat).unwrap();
    Ok((s.into_pyarray(py), Array1::from_vec(idx).into_pyarray(py)))
}

/// Exact volumes of a TIN against the plane z = a x + b y + c (`engine.tin.prism_volume`):
/// ``(above, below, area)``.
#[pyfunction]
fn tin_volume(py: Python<'_>, xy: PyReadonlyArray2<'_, f64>, z: PyReadonlyArray1<'_, f64>,
              triangles: PyReadonlyArray2<'_, i64>, plane: (f64, f64, f64)) -> PyResult<(f64, f64, f64)> {
    let pts = xy_rows(&xy)?;
    let h = heights(&z, pts.len())?;
    let tri = triangle_rows(&triangles, pts.len())?;
    Ok(py.detach(|| tin::volume(&pts, &h, &tri, [plane.0, plane.1, plane.2])))
}

// ------------------------------------------------------------------- the bundle adjustment (API 12)
/// One entry of a problem dict, by its key.
fn entry<'py>(d: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyAny>> {
    d.get_item(key)?.ok_or_else(|| PyValueError::new_err(format!("problem: the key {key:?} is missing")))
}

fn xyz_rows(a: &PyReadonlyArray2<'_, f64>, what: &str) -> PyResult<Vec<[f64; 3]>> {
    if a.shape()[1] != 3 {
        return Err(PyValueError::new_err(format!("{what} must be N x 3, not {:?}", a.shape())));
    }
    let v = a.as_array();
    Ok((0..a.shape()[0]).map(|i| [v[[i, 0]], v[[i, 1]], v[[i, 2]]]).collect())
}

fn index_list(a: &PyReadonlyArray1<'_, i64>, what: &str) -> PyResult<Vec<usize>> {
    let negative = |k: i64| PyValueError::new_err(format!("{what}: the index {k} is negative"));
    a.as_array().iter().map(|&k| usize::try_from(k).map_err(|_| negative(k))).collect()
}

fn value_list(a: &PyReadonlyArray1<'_, f64>) -> Vec<f64> {
    a.as_array().iter().copied().collect()
}

/// A number of an options dict, or `default` without the key.
fn number(d: &Bound<'_, PyDict>, key: &str, default: f64) -> PyResult<f64> {
    match d.get_item(key)? {
        Some(v) => v.extract(),
        None => Ok(default),
    }
}

/// The observations `(image, point, xy)` of three entries of a problem dict.
fn observations(d: &Bound<'_, PyDict>, image: &str, point: &str, xy: &str) -> PyResult<Vec<bundle::Obs>> {
    let images = index_list(&entry(d, image)?.extract()?, image)?;
    let points = index_list(&entry(d, point)?.extract()?, point)?;
    let pixels = xy_rows(&entry(d, xy)?.extract()?)?;
    if points.len() != images.len() || pixels.len() != images.len() {
        return Err(PyValueError::new_err(format!("{image}, {point} and {xy} must have one entry per observation")));
    }
    let obs = images.iter().zip(&points).zip(&pixels);
    Ok(obs.map(|((&image, &point), &xy)| bundle::Obs { image, point, xy }).collect())
}

/// The problem of `engine/bundle.py` from the dict `bundle._to_native` builds of it: the fields of
/// `bundle.Problem` with its lists of tuples as arrays, in float64, int64 and bool.
fn bundle_problem(d: &Bound<'_, PyDict>) -> PyResult<bundle::Problem> {
    let rot: PyReadonlyArray3<f64> = entry(d, "rotations")?.extract()?;
    let s = rot.shape();
    if s[1] != 3 || s[2] != 3 {
        return Err(PyValueError::new_err(format!("rotations must be N x 3 x 3, not {s:?}")));
    }
    let r = rot.as_array();
    let rotations = (0..s[0])
        .map(|i| [[r[[i, 0, 0]], r[[i, 0, 1]], r[[i, 0, 2]]], [r[[i, 1, 0]], r[[i, 1, 1]], r[[i, 1, 2]]],
                  [r[[i, 2, 0]], r[[i, 2, 1]], r[[i, 2, 2]]]])
        .collect();
    let models: Vec<String> = entry(d, "camera_models")?.extract()?;
    let params: Vec<Vec<f64>> = entry(d, "camera_params")?.extract()?;
    let sizes: PyReadonlyArray2<f64> = entry(d, "camera_sizes")?.extract()?;
    let free: Vec<Vec<usize>> = entry(d, "free_intrinsics")?.extract()?;
    let c = models.len();
    if params.len() != c || free.len() != c || sizes.shape() != [c, 2] {
        return Err(PyValueError::new_err("camera_models, camera_params, camera_sizes (N x 2) and free_intrinsics \
                                          must have one entry per camera"));
    }
    let sv = sizes.as_array();
    let mut cameras = Vec::with_capacity(c);
    for k in 0..c {
        let (model, _) = camera::Model::from_name(&models[k]).map_err(PyValueError::new_err)?;
        cameras.push(bundle::CameraBlock { model, params: params[k].clone(), width: sv[[k, 0]], height: sv[[k, 1]],
                                           free: free[k].clone() });
    }
    let gcp_marker = index_list(&entry(d, "gcp_marker")?.extract()?, "gcp_marker")?;
    let gcp_target = xyz_rows(&entry(d, "gcp_target")?.extract()?, "gcp_target")?;
    let gcp_sigma = xyz_rows(&entry(d, "gcp_sigma")?.extract()?, "gcp_sigma")?;
    if gcp_target.len() != gcp_marker.len() || gcp_sigma.len() != gcp_marker.len() {
        return Err(PyValueError::new_err("gcp_marker, gcp_target and gcp_sigma must have one entry per control point"));
    }
    let gcps = (0..gcp_marker.len()).map(|j| (gcp_marker[j], gcp_target[j], gcp_sigma[j])).collect();
    let bar_a = index_list(&entry(d, "bar_a")?.extract()?, "bar_a")?;
    let bar_b = index_list(&entry(d, "bar_b")?.extract()?, "bar_b")?;
    let bar_distance = value_list(&entry(d, "bar_distance")?.extract()?);
    let bar_sigma = value_list(&entry(d, "bar_sigma")?.extract()?);
    if bar_b.len() != bar_a.len() || bar_distance.len() != bar_a.len() || bar_sigma.len() != bar_a.len() {
        return Err(PyValueError::new_err("bar_a, bar_b, bar_distance and bar_sigma must have one entry per scale bar"));
    }
    let bars = (0..bar_a.len()).map(|j| (bar_a[j], bar_b[j], bar_distance[j], bar_sigma[j])).collect();
    let gnss_image = index_list(&entry(d, "gnss_image")?.extract()?, "gnss_image")?;
    let gnss_target = xyz_rows(&entry(d, "gnss_target")?.extract()?, "gnss_target")?;
    let gnss_sigma = xyz_rows(&entry(d, "gnss_sigma")?.extract()?, "gnss_sigma")?;
    let flights: PyReadonlyArray1<i64> = entry(d, "gnss_flight")?.extract()?;
    let gnss_flight: Vec<i64> = flights.as_array().iter().copied().collect();
    let k = gnss_image.len();
    if gnss_target.len() != k || gnss_sigma.len() != k || gnss_flight.len() != k {
        return Err(PyValueError::new_err("gnss_image, gnss_target, gnss_sigma and gnss_flight must have one entry \
                                          per GNSS position"));
    }
    let gnss = (0..k).map(|j| (gnss_image[j], gnss_target[j], gnss_sigma[j], gnss_flight[j])).collect();
    let fixed: PyReadonlyArray1<bool> = entry(d, "fixed_images")?.extract()?;
    Ok(bundle::Problem {
        rotations,
        centres: xyz_rows(&entry(d, "centres")?.extract()?, "centres")?,
        image_camera: index_list(&entry(d, "image_camera")?.extract()?, "image_camera")?,
        cameras,
        points: xyz_rows(&entry(d, "points")?.extract()?, "points")?,
        obs: observations(d, "obs_image", "obs_point", "obs_xy")?,
        markers: xyz_rows(&entry(d, "markers")?.extract()?, "markers")?,
        mobs: observations(d, "mobs_image", "mobs_marker", "mobs_xy")?,
        gcps,
        bars,
        gnss,
        fixed_images: fixed.as_array().iter().copied().collect(),
        rolling_shutter: entry(d, "rolling_shutter")?.extract()?,
        gnss_bias: entry(d, "gnss_bias")?.extract()?,
        reference_flight: None,
        pixel_sigma: entry(d, "pixel_sigma")?.extract()?,
        marker_sigma: entry(d, "marker_sigma")?.extract()?,
        rs_sigma: value_list(&entry(d, "rs_sigma")?.extract()?),
        rs_sigma_angular: value_list(&entry(d, "rs_sigma_angular")?.extract()?),
    })
}

/// The loss of the tie points by its word and scale.
fn bundle_loss(loss: &str, scale: f64) -> PyResult<bundle::Loss> {
    match loss {
        "none" => Ok(bundle::Loss::None),
        "huber" | "cauchy" if !(scale > 0.0) => {
            Err(PyValueError::new_err(format!("loss_scale must be positive, not {scale}")))
        }
        "huber" => Ok(bundle::Loss::Huber(scale)),
        "cauchy" => Ok(bundle::Loss::Cauchy(scale)),
        other => Err(PyValueError::new_err(format!("loss must be none, huber or cauchy, not {other:?}"))),
    }
}

/// The options of the solver from a dict, the defaults of `bundle::Options` for the keys not given.
fn bundle_options(d: &Bound<'_, PyDict>) -> PyResult<bundle::Options> {
    let mut opts = bundle::Options::default();
    if let Some(v) = d.get_item("max_iterations")? {
        opts.max_iterations = v.extract()?;
    }
    let loss: String = match d.get_item("loss")? {
        Some(v) => v.extract()?,
        None => "none".to_string(),
    };
    opts.loss = bundle_loss(&loss, number(d, "loss_scale", 1.0)?)?;
    if let Some(v) = d.get_item("linear_solver")? {
        opts.linear_solver = match v.extract::<String>()?.as_str() {
            "auto" => bundle::LinearSolver::Auto,
            "dense" => bundle::LinearSolver::Dense,
            "sparse" => bundle::LinearSolver::Sparse,
            "pcg" => bundle::LinearSolver::Pcg,
            other => {
                let message = format!("linear_solver must be auto, dense, sparse or pcg, not {other:?}");
                return Err(PyValueError::new_err(message));
            }
        };
    }
    opts.function_tolerance = number(d, "function_tolerance", opts.function_tolerance)?;
    opts.gradient_tolerance = number(d, "gradient_tolerance", opts.gradient_tolerance)?;
    opts.parameter_tolerance = number(d, "parameter_tolerance", opts.parameter_tolerance)?;
    Ok(opts)
}

fn termination_word(t: bundle::Termination) -> &'static str {
    match t {
        bundle::Termination::Function => "function",
        bundle::Termination::Gradient => "gradient",
        bundle::Termination::Parameters => "parameters",
        bundle::Termination::MaxIterations => "max_iterations",
        bundle::Termination::NoProgress => "no_progress",
    }
}

fn linear_solver_word(s: bundle::LinearSolver) -> &'static str {
    match s {
        bundle::LinearSolver::Auto => "auto",
        bundle::LinearSolver::Dense => "dense",
        bundle::LinearSolver::Sparse => "sparse",
        bundle::LinearSolver::Pcg => "pcg",
    }
}

fn xyz_array<'py>(py: Python<'py>, rows: &[[f64; 3]]) -> Bound<'py, PyArray2<f64>> {
    Array2::from_shape_vec((rows.len(), 3), rows.concat()).unwrap().into_pyarray(py)
}

/// The fields of a `bundle::Solution` as a dict: `rolling_shutter` None when it was not adjusted,
/// `bias` `{flight: (3,)}`, `termination` and `linear_solver` as lower-case words.
fn solution_dict<'py>(py: Python<'py>, sol: &bundle::Solution) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    let flat: Vec<f64> = sol.rotations.iter().flat_map(|r| r.concat()).collect();
    d.set_item("rotations", Array3::from_shape_vec((sol.rotations.len(), 3, 3), flat).unwrap().into_pyarray(py))?;
    d.set_item("centres", xyz_array(py, &sol.centres))?;
    let params: Vec<_> = sol.camera_params.iter().map(|p| Array1::from_vec(p.clone()).into_pyarray(py)).collect();
    d.set_item("camera_params", params)?;
    d.set_item("points", xyz_array(py, &sol.points))?;
    d.set_item("markers", xyz_array(py, &sol.markers))?;
    match &sol.rolling_shutter {
        Some(rs) => d.set_item("rolling_shutter",
                               Array2::from_shape_vec((rs.len(), 6), rs.concat()).unwrap().into_pyarray(py))?,
        None => d.set_item("rolling_shutter", py.None())?,
    }
    let bias = PyDict::new(py);
    for (flight, b) in &sol.bias {
        bias.set_item(*flight, Array1::from_vec(b.to_vec()).into_pyarray(py))?;
    }
    d.set_item("bias", bias)?;
    d.set_item("cost_initial", sol.cost_initial)?;
    d.set_item("cost", sol.cost)?;
    d.set_item("gradient_max", sol.gradient_max)?;
    d.set_item("iterations", sol.iterations)?;
    d.set_item("termination", termination_word(sol.termination))?;
    d.set_item("linear_solver", linear_solver_word(sol.linear_solver))?;
    Ok(d)
}

/// The message of a panic of the engine.
pub(crate) fn panic_text(payload: &(dyn std::any::Any + Send)) -> String {
    if let Some(s) = payload.downcast_ref::<&str>() {
        (*s).to_string()
    } else if let Some(s) = payload.downcast_ref::<String>() {
        s.clone()
    } else {
        "the native engine panicked".to_string()
    }
}

/// The bundle adjustment of `engine/bundle.py` by the native engine (`bundle::solve`, API 12):
/// ``problem`` the dict ``bundle._to_native`` builds of a ``Problem`` (float64, int64 and bool arrays;
/// ``camera_models``, ``camera_params`` and ``free_intrinsics`` as lists), ``options`` any of
/// ``max_iterations``, ``loss`` (``none``, ``huber``, ``cauchy``) with ``loss_scale``, ``linear_solver``
/// (``auto``, ``dense``, ``sparse``, ``pcg``), ``function_tolerance``, ``gradient_tolerance``,
/// ``parameter_tolerance``. The GIL is released; ``progress(iteration, cost)`` is called after every
/// iteration with it taken back, and an exception it raises stops the adjustment and comes out as it
/// is. Returns the fields of ``bundle::Solution`` as a dict (``bias`` ``{flight: (3,)}``,
/// ``rolling_shutter`` None without it, ``termination`` and ``linear_solver`` as words). A problem that
/// is not consistent is a ValueError; an error of the solver or a panic, a RuntimeError with its message.
#[pyfunction]
#[pyo3(signature = (problem, options, progress=None))]
fn bundle_adjust<'py>(py: Python<'py>, problem: &Bound<'py, PyDict>, options: &Bound<'py, PyDict>,
                      progress: Option<Py<PyAny>>) -> PyResult<Bound<'py, PyDict>> {
    let pb = bundle_problem(problem)?;
    let opts = bundle_options(options)?;
    pb.check().map_err(PyValueError::new_err)?;
    let mut raised: Option<PyErr> = None;
    let outcome = py.detach(|| {
        let mut observe = |iteration: usize, cost: f64| -> bool {
            let Some(callback) = progress.as_ref() else { return true };
            Python::attach(|py| match callback.call1(py, (iteration, cost)) {
                Ok(_) => true,
                Err(e) => {
                    raised = Some(e);
                    false
                }
            })
        };
        catch_unwind(AssertUnwindSafe(|| bundle::solve(&pb, &opts, &mut observe)))
    });
    match outcome {
        Ok(Ok(solution)) => solution_dict(py, &solution),
        Ok(Err(message)) => Err(raised.unwrap_or_else(|| PyRuntimeError::new_err(message))),
        Err(payload) => Err(PyRuntimeError::new_err(format!("bundle adjustment: {}", panic_text(&*payload)))),
    }
}

type BundleEvaluation<'py> = (Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<f64>>, Bound<'py, PyArray1<i64>>,
                              Bound<'py, PyArray1<i64>>, Bound<'py, PyArray1<f64>>, f64);

/// A problem (as :func:`bundle_adjust` takes it) evaluated at the parameters it holds
/// (`bundle::evaluate`, API 12), with ``loss`` and ``loss_scale`` on the tie points: ``(residuals,
/// gradient, rows, cols, values, cost)`` - the residual vector of ``_Model.residuals``, the gradient of
/// the cost in the columns of ``_Layout``, the Jacobian of the residuals as COO triplets on the
/// structure of ``_Model.sparsity``, and the cost ``0.5 sum(rho(r^2))``. The GIL is released.
#[pyfunction]
#[pyo3(signature = (problem, loss="none", loss_scale=1.0))]
fn bundle_evaluate<'py>(py: Python<'py>, problem: &Bound<'py, PyDict>, loss: &str, loss_scale: f64)
                        -> PyResult<BundleEvaluation<'py>> {
    let pb = bundle_problem(problem)?;
    let loss = bundle_loss(loss, loss_scale)?;
    pb.check().map_err(PyValueError::new_err)?;
    let ev = py.detach(|| catch_unwind(AssertUnwindSafe(|| bundle::evaluate(&pb, loss))))
        .map_err(|payload| PyRuntimeError::new_err(format!("bundle evaluation: {}", panic_text(&*payload))))?;
    let rows: Vec<i64> = ev.jacobian.iter().map(|&(r, _, _)| r as i64).collect();
    let cols: Vec<i64> = ev.jacobian.iter().map(|&(_, c, _)| c as i64).collect();
    let values: Vec<f64> = ev.jacobian.iter().map(|&(_, _, v)| v).collect();
    Ok((Array1::from_vec(ev.residuals).into_pyarray(py), Array1::from_vec(ev.gradient).into_pyarray(py),
        Array1::from_vec(rows).into_pyarray(py), Array1::from_vec(cols).into_pyarray(py),
        Array1::from_vec(values).into_pyarray(py), ev.cost))
}

/// The variances of the free intrinsics of each camera of a problem (as :func:`bundle_adjust` takes it) at the
/// parameters it holds (`bundle::intrinsics_variances`, API 17): per camera, one value per entry of its
/// ``free_intrinsics``, in their order (none for a camera with none free) - the diagonal of the inverse of the normal
/// matrix at their columns, from the system reduced to the cameras, with ``loss`` and ``loss_scale`` on the tie
/// points. The accuracies of the problem are taken as they are: a caller with a variance factor puts it into them
/// first. ``reference_flight``: with ``gnss_bias``, the flight whose bias is held at zero (the frame of the
/// positions; without control points the biases of all the flights and a shift of the block would trade off). The
/// GIL is released. A problem that is not consistent is a ValueError; a normal matrix that is not positive definite
/// (a gauge left free, an intrinsic the observations do not hold) or a panic, a RuntimeError with its message.
#[pyfunction]
#[pyo3(signature = (problem, loss="none", loss_scale=1.0, reference_flight=None))]
fn bundle_intrinsics_variances(py: Python<'_>, problem: &Bound<'_, PyDict>, loss: &str, loss_scale: f64,
                               reference_flight: Option<i64>) -> PyResult<Vec<Vec<f64>>> {
    let mut pb = bundle_problem(problem)?;
    pb.reference_flight = reference_flight;
    let loss = bundle_loss(loss, loss_scale)?;
    pb.check().map_err(PyValueError::new_err)?;
    match py.detach(|| catch_unwind(AssertUnwindSafe(|| bundle::intrinsics_variances(&pb, loss)))) {
        Ok(Ok(variances)) => Ok(variances),
        Ok(Err(message)) => Err(PyRuntimeError::new_err(message)),
        Err(payload) => {
            Err(PyRuntimeError::new_err(format!("variances of the intrinsics: {}", panic_text(&*payload))))
        }
    }
}

/// Camera models the projection kernels know.
#[pyfunction]
fn supported_models() -> Vec<&'static str> {
    camera::MODELS.to_vec()
}

/// The options of the geometric verification as a flat dict: the RANSAC ones under ``ransac_*``.
fn two_view_dict<'py>(py: Python<'py>, o: &two_view::Options) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("ransac_max_error", o.ransac.max_error)?;
    d.set_item("ransac_confidence", o.ransac.confidence)?;
    d.set_item("ransac_min_num_trials", o.ransac.min_num_trials)?;
    d.set_item("ransac_max_num_trials", o.ransac.max_num_trials)?;
    d.set_item("ransac_min_inlier_ratio", o.ransac.min_inlier_ratio)?;
    d.set_item("ransac_dyn_num_trials_multiplier", o.ransac.dyn_num_trials_multiplier)?;
    d.set_item("min_num_inliers", o.min_num_inliers)?;
    d.set_item("min_e_f_inlier_ratio", o.min_e_f_inlier_ratio)?;
    d.set_item("max_h_inlier_ratio", o.max_h_inlier_ratio)?;
    d.set_item("watermark_min_inlier_ratio", o.watermark_min_inlier_ratio)?;
    d.set_item("watermark_border_size", o.watermark_border_size)?;
    d.set_item("watermark_detection_max_error", o.watermark_detection_max_error)?;
    d.set_item("detect_watermark", o.detect_watermark)?;
    d.set_item("use_sampson_refinement", o.use_sampson_refinement)?;
    d.set_item("compute_relative_pose", o.compute_relative_pose)?;
    Ok(d)
}

/// One option of the geometric verification set from `value` by its key of :func:`two_view_defaults`:
/// whether `key` is one of them (a value of the wrong type is the error of its extraction).
pub(crate) fn set_two_view_option(o: &mut two_view::Options, key: &str, value: &Bound<'_, PyAny>) -> PyResult<bool> {
    match key {
        "ransac_max_error" => o.ransac.max_error = value.extract()?,
        "ransac_confidence" => o.ransac.confidence = value.extract()?,
        "ransac_min_num_trials" => o.ransac.min_num_trials = value.extract()?,
        "ransac_max_num_trials" => o.ransac.max_num_trials = value.extract()?,
        "ransac_min_inlier_ratio" => o.ransac.min_inlier_ratio = value.extract()?,
        "ransac_dyn_num_trials_multiplier" => o.ransac.dyn_num_trials_multiplier = value.extract()?,
        "min_num_inliers" => o.min_num_inliers = value.extract()?,
        "min_e_f_inlier_ratio" => o.min_e_f_inlier_ratio = value.extract()?,
        "max_h_inlier_ratio" => o.max_h_inlier_ratio = value.extract()?,
        "watermark_min_inlier_ratio" => o.watermark_min_inlier_ratio = value.extract()?,
        "watermark_border_size" => o.watermark_border_size = value.extract()?,
        "watermark_detection_max_error" => o.watermark_detection_max_error = value.extract()?,
        "detect_watermark" => o.detect_watermark = value.extract()?,
        "use_sampson_refinement" => o.use_sampson_refinement = value.extract()?,
        "compute_relative_pose" => o.compute_relative_pose = value.extract()?,
        _ => return Ok(false),
    }
    Ok(true)
}

/// The options of the geometric verification from a flat dict (the keys of :func:`two_view_defaults`,
/// any subset); a key that is not one of them is a ValueError.
fn two_view_options(d: Option<&Bound<'_, PyDict>>) -> PyResult<two_view::Options> {
    let mut o = two_view::Options::default();
    let Some(d) = d else { return Ok(o) };
    for (key, value) in d.iter() {
        let key: String = key.extract()?;
        if !set_two_view_option(&mut o, &key, &value)? {
            return Err(PyValueError::new_err(format!("unknown two-view option {key:?}")));
        }
    }
    Ok(o)
}

/// The verification of a pair (`two_view::TwoView`) into the dict `d`: ``config`` (COLMAP's number),
/// ``inliers`` ``(m, 2)`` uint32 in the order of the matches, ``F``, ``E``, ``H`` as 3 x 3 float64 or None
/// when not estimated, and ``qvec`` (w, x, y, z) with ``tvec`` of cam2_from_cam1, None without a pose.
pub(crate) fn two_view_into_dict(py: Python<'_>, d: &Bound<'_, PyDict>, out: &two_view::TwoView) -> PyResult<()> {
    d.set_item("config", out.config as u32)?;
    let flat: Vec<u32> = out.inliers.iter().flat_map(|p| [p[0], p[1]]).collect();
    d.set_item("inliers", Array2::from_shape_vec((out.inliers.len(), 2), flat).unwrap().into_pyarray(py))?;
    for (name, matrix) in [("F", &out.f), ("E", &out.e), ("H", &out.h)] {
        match matrix {
            Some(mm) => {
                let rows: Vec<f64> = (0..3).flat_map(|r| (0..3).map(move |c| mm[(r, c)])).collect();
                d.set_item(name, Array2::from_shape_vec((3, 3), rows).unwrap().into_pyarray(py))?;
            }
            None => d.set_item(name, py.None())?,
        }
    }
    match &out.cam2_from_cam1 {
        Some((q, t)) => {
            d.set_item("qvec", Array1::from_vec(vec![q.w, q.i, q.j, q.k]).into_pyarray(py))?;
            d.set_item("tvec", Array1::from_vec(vec![t.x, t.y, t.z]).into_pyarray(py))?;
        }
        None => {
            d.set_item("qvec", py.None())?;
            d.set_item("tvec", py.None())?;
        }
    }
    Ok(())
}

/// The defaults of the geometric verification of a pair (`two_view::Options`): the fields of COLMAP's
/// ``TwoViewGeometryOptions`` the native engine reads, as a flat dict with the RANSAC ones under
/// ``ransac_*`` (``tests/test_native.py`` maps each name to its pycolmap attribute and compares them).
#[pyfunction]
fn two_view_defaults(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    two_view_dict(py, &two_view::Options::default())
}

/// Key points as the engine takes them: an ``(n, k)`` float32 array with ``x`` and ``y`` in the first
/// two columns, the only ones the verification reads (the four of the feature store, or the six of a
/// COLMAP database, whose third and fourth columns are the affine shape, not a scale and an
/// orientation: they are carried along and ignored).
fn keypoint_rows(a: &PyReadonlyArray2<'_, f32>, what: &str) -> PyResult<Vec<[f32; 4]>> {
    let s = a.shape();
    if s[1] < 2 {
        return Err(PyValueError::new_err(format!("{what} must be (n, >= 2), not {s:?}")));
    }
    let v = a.as_array();
    Ok((0..s[0])
        .map(|i| {
            let at = |j: usize| if j < s[1] { v[[i, j]] } else { 0.0 };
            [at(0), at(1), at(2), at(3)]
        })
        .collect())
}

/// The geometric verification of a pair by the native engine (`two_view::verify`): ``model1``,
/// ``params1``, ``size1`` (``(width, height)``) the camera of the first photo and ``kp1`` its key
/// points (:func:`keypoint_rows`, COLMAP pixels), the same for the second; ``matches`` ``(n, 2)`` uint32
/// pairs of indices into them; ``quality`` one float32 per match (the ratio of the two best descriptor
/// distances, lower is better, orders the PROSAC sampling; empty, the matches are taken in their
/// order); ``calibrated`` whether both cameras have a prior focal length (then E is estimated too);
/// ``seed`` the seed of the RANSAC (``two_view::pair_seed`` of the two names in the engine); ``options``
/// any of the keys of :func:`two_view_defaults`. Returns ``config`` (COLMAP's number), ``inliers``
/// ``(m, 2)`` uint32 in the order of ``matches``, ``F``, ``E``, ``H`` as 3 x 3 float64 or None when not
/// estimated, and ``qvec`` (w, x, y, z) with ``tvec`` of cam2_from_cam1, None unless
/// ``compute_relative_pose`` found one. An index outside the key points, lengths that disagree or a
/// camera model the engine does not know are a ValueError; a panic of the engine, a RuntimeError.
#[pyfunction]
#[pyo3(signature = (model1, params1, size1, kp1, model2, params2, size2, kp2, matches, quality, calibrated, seed,
                    options=None))]
#[allow(clippy::too_many_arguments)]
fn verify_pair<'py>(py: Python<'py>, model1: &str, params1: PyReadonlyArray1<'py, f64>, size1: (f64, f64),
                    kp1: PyReadonlyArray2<'py, f32>, model2: &str, params2: PyReadonlyArray1<'py, f64>,
                    size2: (f64, f64), kp2: PyReadonlyArray2<'py, f32>, matches: PyReadonlyArray2<'py, u32>,
                    quality: PyReadonlyArray1<'py, f32>, calibrated: bool, seed: u64,
                    options: Option<&Bound<'py, PyDict>>) -> PyResult<Bound<'py, PyDict>> {
    let cam1 = camera(model1, &params1.as_array().to_vec(), size1.0, size1.1)?;
    let cam2 = camera(model2, &params2.as_array().to_vec(), size2.0, size2.1)?;
    let k1 = keypoint_rows(&kp1, "kp1")?;
    let k2 = keypoint_rows(&kp2, "kp2")?;
    if matches.shape()[1] != 2 {
        return Err(PyValueError::new_err(format!("matches must be (n, 2), not {:?}", matches.shape())));
    }
    let m = matches.as_array();
    let pairs: Vec<[u32; 2]> = (0..m.shape()[0]).map(|i| [m[[i, 0]], m[[i, 1]]]).collect();
    if let Some(bad) = pairs.iter().find(|p| p[0] as usize >= k1.len() || p[1] as usize >= k2.len()) {
        return Err(PyValueError::new_err(format!("match {bad:?} is outside the key points ({}, {})", k1.len(),
                                                 k2.len())));
    }
    let q = quality.as_array().to_vec();
    if !q.is_empty() && q.len() != pairs.len() {
        return Err(PyValueError::new_err(format!("{} qualities for {} matches", q.len(), pairs.len())));
    }
    let o = two_view_options(options)?;
    let out = py.detach(|| {
        catch_unwind(AssertUnwindSafe(|| two_view::verify(&cam1, &k1, &cam2, &k2, &pairs, &q, calibrated, &o, seed)))
    })
    .map_err(|payload| PyRuntimeError::new_err(format!("verify_pair: {}", panic_text(&*payload))))?;
    let d = PyDict::new(py);
    two_view_into_dict(py, &d, &out)?;
    Ok(d)
}

#[pymodule]
fn ppg_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("API", API)?;
    m.add_function(wrap_pyfunction!(combine_depths, m)?)?;
    m.add_function(wrap_pyfunction!(disparity_to_depth, m)?)?;
    m.add_function(wrap_pyfunction!(depth_normals, m)?)?;
    m.add_function(wrap_pyfunction!(depth_agree, m)?)?;
    m.add_function(wrap_pyfunction!(project_points, m)?)?;
    m.add_function(wrap_pyfunction!(ortho_scores, m)?)?;
    m.add_function(wrap_pyfunction!(ortho_candidates, m)?)?;
    m.add_function(wrap_pyfunction!(patchmatch_depth, m)?)?;
    m.add_function(wrap_pyfunction!(supported_models, m)?)?;
    m.add_function(wrap_pyfunction!(gpu_adapters, m)?)?;
    m.add_function(wrap_pyfunction!(gpu_sift_features, m)?)?;
    m.add_function(wrap_pyfunction!(cpu_sift_features, m)?)?;
    m.add_function(wrap_pyfunction!(gpu_fuse_depth_maps, m)?)?;
    m.add_function(wrap_pyfunction!(gpu_fuse_cached, m)?)?;
    m.add_function(wrap_pyfunction!(gpu_fuse_forget, m)?)?;
    m.add_function(wrap_pyfunction!(gpu_match, m)?)?;
    m.add_function(wrap_pyfunction!(gpu_device_name, m)?)?;
    m.add_function(wrap_pyfunction!(gpu_forget, m)?)?;
    m.add_function(wrap_pyfunction!(gpu_patchmatch_depth, m)?)?;
    m.add_function(wrap_pyfunction!(ppk::ppk_solve, m)?)?;
    m.add_function(wrap_pyfunction!(crinex::crx2rnx, m)?)?;
    m.add_function(wrap_pyfunction!(convert::raw_to_rinex, m)?)?;
    m.add_function(wrap_pyfunction!(convert::convert_cancel, m)?)?;
    m.add_function(wrap_pyfunction!(tin_triangulate, m)?)?;
    m.add_function(wrap_pyfunction!(tin_rasterize, m)?)?;
    m.add_function(wrap_pyfunction!(tin_contours, m)?)?;
    m.add_function(wrap_pyfunction!(tin_volume, m)?)?;
    m.add_function(wrap_pyfunction!(bundle_adjust, m)?)?;
    m.add_function(wrap_pyfunction!(bundle_evaluate, m)?)?;
    m.add_function(wrap_pyfunction!(bundle_intrinsics_variances, m)?)?;
    m.add_function(wrap_pyfunction!(two_view_defaults, m)?)?;
    m.add_function(wrap_pyfunction!(verify_pair, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::native_features, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::native_features_pixels, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::native_preselect, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::native_match_pairs, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::native_store_read, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::native_store_write, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::native_store_names, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::native_store_header, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::native_match_read, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::native_match_pairs_list, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::read_grey, m)?)?;
    m.add_function(wrap_pyfunction!(native_match::rescale_as_colmap, m)?)?;
    m.add_class::<native_match::NativeFeatureStore>()?;
    m.add_class::<native_match::NativeMatchStore>()?;
    m.add_function(wrap_pyfunction!(native_map::native_mapper_defaults, m)?)?;
    m.add_class::<native_map::NativeMapper>()?;
    Ok(())
}
