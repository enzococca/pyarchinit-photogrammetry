//! `ppg_core`: native kernels of pyarchinit-photogrammetry (Python module, PyO3).
//!
//! Every function mirrors a numpy function of the Python package (named in its docstring), which
//! stays the fallback and the reference of the tests. The work runs without the GIL on all cores.

mod camera;
mod depth;

use numpy::ndarray::{Array1, Array2, Array3};
use numpy::{IntoPyArray, PyArray1, PyArray2, PyArray3, PyReadonlyArray1, PyReadonlyArray2, PyReadonlyArray3,
            PyUntypedArrayMethods};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use rayon::prelude::*;

use camera::{Camera, Pose};

/// Version of the functions offered here: raised when one is added or changes meaning.
const API: u32 = 1;

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
    let out = py.allow_threads(|| depth::combine(data, k, h, w, min_agree, tol));
    Array2::from_shape_vec((h, w), out).unwrap().into_pyarray_bound(py)
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
    let out = py.allow_threads(|| depth::disparity_to_depth(data, h, w, min_valid, f, baseline, cx, cy, &r1, k, oh, ow));
    Ok(Array2::from_shape_vec((oh, ow), out).unwrap().into_pyarray_bound(py))
}

/// Normals of a depth map (`engine.dense._normals_from_depth`), h x w x 3 float32.
#[pyfunction]
fn depth_normals<'py>(py: Python<'py>, depth: PyReadonlyArray2<'py, f32>, k: (f64, f64, f64, f64))
    -> Bound<'py, PyArray3<f32>> {
    let a = depth.as_array();
    let (h, w) = a.dim();
    let c = a.as_standard_layout();
    let data = c.as_slice().unwrap();
    let out = py.allow_threads(|| depth::normals(data, h, w, k));
    Array3::from_shape_vec((h, w, 3), out).unwrap().into_pyarray_bound(py)
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
    let out = py.allow_threads(|| depth::agree(pts, cand, dmap, h, w, k, &rot, &t, tol, tol_slice));
    Ok(Array1::from_vec(out).into_pyarray_bound(py))
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
    py.allow_threads(|| {
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
    Ok((Array2::from_shape_vec((n, 2), px).unwrap().into_pyarray_bound(py), Array1::from_vec(depth).into_pyarray_bound(py),
        Array1::from_vec(ok).into_pyarray_bound(py)))
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
    py.allow_threads(|| {
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
    Ok((Array2::from_shape_vec((n, 2), px).unwrap().into_pyarray_bound(py), Array1::from_vec(score).into_pyarray_bound(py)))
}

/// Camera models the projection kernels know.
#[pyfunction]
fn supported_models() -> Vec<&'static str> {
    camera::MODELS.to_vec()
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
    m.add_function(wrap_pyfunction!(supported_models, m)?)?;
    Ok(())
}
