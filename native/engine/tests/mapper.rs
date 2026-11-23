//! The native mapper as a whole (`mapper::Mapper`: view graph, rotations, tracks, positioning,
//! rounds of adjustment, the model on disk) on scenes of known truth, from key points with 0.5 px
//! of noise and verified pairs holding a tenth of wrong matches: a strip without positions, the
//! ring flown once with the positions of its photos and rotations averaged from noisy relative
//! rotations (the claim of section 8.4 of the design after the rounds of adjustment), the accuracy
//! of the positions estimated again, a second flight with a bias, a wrong fix, photos without a
//! position, the principal point freed only on a block held by the positions, the stages in their
//! order, a cancel, two runs that write the same bytes and the bytes of COLMAP's format.
//!
//! Sizes: the ring of Task 6 (100 photos, 600 m across, 40 m high) where the claim is asserted;
//! rings of 40 and 60 photos for the order, the cancel, the bytes and the principal point.

mod common;

use std::path::{Path, PathBuf};
use std::sync::atomic::Ordering;
use std::time::Instant;

use common::{ring, scene, InputOptions, Kind, Random, Scene};
use nalgebra::{Matrix3, Rotation3, Vector3};
use ppg_engine::camera::Model;
use ppg_engine::mapper::input::Input;
use ppg_engine::mapper::model_io::{quaternion, write_colmap_binary, ModelCamera, ModelImage, ModelPoint, NO_POINT};
use ppg_engine::mapper::positioning::{Frame, Gnss};
use ppg_engine::mapper::{Mapper, MapperReport, Options};

const PHOTOS: usize = 100;
const DIAMETER: f64 = 600.0;
const HEIGHT: f64 = 40.0;
/// The tie-point limit of the tests: the plan's `max(500, tiepoint_limit // 4)` at its floor.
const PER_PHOTO: usize = 500;

fn tmp(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("mapper_{name}"));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// The input of a scene: key points with 0.5 px of noise, a tenth of the matches of every pair
/// wrong (not marked as such).
fn noisy(s: &Scene, seed: u64) -> Input {
    s.input(&InputOptions { noise_px: 0.5, outlier_share: 0.1, seed, ..InputOptions::default() })
}

/// Positions of the photos: the true centres plus normal noise of `plan` metres on each horizontal
/// axis and `height` on the vertical, the accuracy `sigma` recorded, one flight; the photos of
/// `without` have none.
fn gnss_with(s: &Scene, plan: f64, height: f64, sigma: [f64; 3], seed: u64, without: &[usize]) -> Gnss {
    let mut random = Random::new(seed);
    let mut g = Gnss { image: Vec::new(), xyz: Vec::new(), sigma: Vec::new(), flight: Vec::new() };
    for (i, p) in s.poses.iter().enumerate() {
        let noise = [plan * random.normal(), plan * random.normal(), height * random.normal()];
        if without.contains(&i) {
            continue;
        }
        g.image.push(i);
        g.xyz.push([p.centre.x + noise[0], p.centre.y + noise[1], p.centre.z + noise[2]]);
        g.sigma.push(sigma);
        g.flight.push(1);
    }
    g
}

/// The photos from `first` on as flight 2, their positions moved by `offset`.
fn second_flight(g: &mut Gnss, first: usize, offset: [f64; 3]) {
    for k in 0..g.image.len() {
        if g.image[k] >= first {
            g.flight[k] = 2;
            for a in 0..3 {
                g.xyz[k][a] += offset[a];
            }
        }
    }
}

/// A rotation of `deg` degrees about an axis drawn uniformly.
fn turn(random: &mut Random, deg: f64) -> Matrix3<f64> {
    let axis = Vector3::new(random.normal(), random.normal(), random.normal()).normalize();
    Rotation3::from_scaled_axis(axis * deg.to_radians()).into_inner()
}

fn never_cancel() -> impl FnMut() -> bool {
    || true
}

/// Every stage in order; with `noise_deg` the relative rotation of every edge of the view graph is
/// turned by that many degrees about a random axis before the rotations are averaged.
fn run(m: &mut Mapper, gnss: Option<&Gnss>, noise_deg: Option<(f64, u64)>) -> MapperReport {
    m.view_graph(&mut never_cancel()).expect("the view graph");
    if let Some((deg, seed)) = noise_deg {
        let mut graph = m.graph().expect("a view graph").clone();
        let mut random = Random::new(seed);
        for e in &mut graph.edges {
            let r = turn(&mut random, deg) * Matrix3::from_fn(|i, j| e.rotation[i][j]);
            e.rotation = std::array::from_fn(|i| std::array::from_fn(|j| r[(i, j)]));
        }
        m.set_view_graph(graph).expect("the graph replaced");
    }
    m.rotations(&mut never_cancel()).expect("the rotations");
    m.tracks(PER_PHOTO, &mut never_cancel()).expect("the tracks");
    m.position(gnss, &mut never_cancel()).expect("the positioning");
    m.refine(&mut never_cancel()).expect("the rounds of adjustment");
    m.report()
}

fn mapped(
    s: &Scene,
    input_seed: u64,
    o: Options,
    gnss: Option<&Gnss>,
    noise: Option<(f64, u64)>,
) -> (Mapper, MapperReport) {
    let mut m = Mapper::new(noisy(s, input_seed), o).expect("a mapper");
    let report = run(&mut m, gnss, noise);
    (m, report)
}

/// The residual vectors of `est` after the similarity (Umeyama) that best takes it onto `truth`.
fn after_similarity(est: &[Vector3<f64>], truth: &[Vector3<f64>]) -> Vec<Vector3<f64>> {
    let n = est.len() as f64;
    let (me, mt) = (est.iter().sum::<Vector3<f64>>() / n, truth.iter().sum::<Vector3<f64>>() / n);
    let mut cov = Matrix3::zeros();
    let mut var = 0.0;
    for (e, t) in est.iter().zip(truth) {
        cov += (t - mt) * (e - me).transpose();
        var += (e - me).norm_squared();
    }
    let svd = cov.svd(true, true);
    let (u, vt) = (svd.u.unwrap(), svd.v_t.unwrap());
    let mut d = Matrix3::identity();
    if (u * vt).determinant() < 0.0 {
        d[(2, 2)] = -1.0;
    }
    let r = u * d * vt;
    let scale = (svd.singular_values.component_mul(&d.diagonal())).sum() / var;
    est.iter().zip(truth).map(|(e, t)| scale * r * (e - me) + mt - t).collect()
}

/// The residuals of the centres after one similarity onto the truth (every photo must be placed).
fn centre_residuals(s: &Scene, m: &Mapper) -> Vec<Vector3<f64>> {
    let est: Vec<Vector3<f64>> =
        m.centres().iter().enumerate().map(|(i, c)| Vector3::from(c.unwrap_or_else(|| panic!("photo {i}")))).collect();
    let truth: Vec<Vector3<f64>> = s.poses.iter().map(|q| q.centre).collect();
    after_similarity(&est, &truth)
}

/// The largest residual of the points after one similarity onto the truth, and their number.
fn point_shape(s: &Scene, m: &Mapper) -> (f64, usize) {
    let state = m.state().expect("a refined mapper");
    let (mut est, mut truth) = (Vec::new(), Vec::new());
    for (t, x) in state.points.iter().enumerate() {
        // the truth of a point is that of an observation the last filter kept (a wrong match of
        // the track may be the first)
        let kept = (state.tracks.offsets[t]..state.tracks.offsets[t + 1]).find(|&i| state.keep[i]);
        if let (Some(x), Some(i)) = (x, kept) {
            let (image, k) = state.tracks.observations[i];
            est.push(Vector3::from(*x));
            truth.push(s.points[s.views[image as usize][k as usize].0]);
        }
    }
    let largest = after_similarity(&est, &truth).iter().map(|r| r.norm()).fold(0.0, f64::max);
    (largest, est.len())
}

fn largest(residuals: &[Vector3<f64>]) -> f64 {
    residuals.iter().map(|r| r.norm()).fold(0.0, f64::max)
}

#[test]
fn the_mapper_places_a_strip_without_gnss() {
    let s = scene(24, Kind::Strip, 1);
    let (m, report) = mapped(&s, 2, Options::default(), None, None);
    let length = 23.0 * 5.0;
    let shape = largest(&centre_residuals(&s, &m));
    eprintln!("strip of 24 photos: placed {}, centres {shape:.4} m ({:.4} %), median reprojection {:.3} px, \
               tracks kept {:.3}, rounds {:?}", report.placed, 100.0 * shape / length,
              report.median_reprojection_px, report.tracks_kept_share, report.rounds);
    assert_eq!(report.placed, 24);
    assert_eq!(report.frame, Some(Frame::Free));
    assert!(shape < 0.001 * length, "{shape} m over {length} m");
    assert!(report.median_reprojection_px < 0.7, "{} px", report.median_reprojection_px);
    assert!(report.tracks_kept_share > 0.5 && report.tracks_kept_share <= 1.0, "{}", report.tracks_kept_share);
    // round 1 holds the intrinsics, round 2 frees the focal length (and the radial terms of a
    // model that has them), the principal point never without positions
    assert_eq!(report.rounds.len(), 4, "three rounds and the last adjustment with every track");
    assert!(report.rounds[0].intrinsics.is_empty());
    assert_eq!(report.rounds[1].intrinsics, vec!["focal".to_string()]);
    assert!(report.rounds.iter().all(|r| !r.intrinsics.contains(&"principal_point".to_string())));
}

/// Spec 8.4 at the level of the mapper (rulings R1 and R2): the ring of Task 6, its rotations
/// averaged from relative rotations each turned by 1 degree (the drift comes from the graph), the
/// positions with 1 m of noise in plan and 2 m in height and the accuracy recorded 5 m and 10 m:
/// every photo placed, the shape of the points and of the centres within 0.5 % of the diameter
/// after one similarity, and the accuracy of the positions estimated again within 25 % of the
/// noise, on three scenes.
#[test]
fn the_mapper_holds_the_drifting_ring_with_gnss() {
    for seed in [11, 12, 13] {
        let s = ring(PHOTOS, DIAMETER, HEIGHT, seed);
        let gnss = gnss_with(&s, 1.0, 2.0, [5.0, 5.0, 10.0], seed + 100, &[]);
        let (m, report) = mapped(&s, seed + 200, Options::default(), Some(&gnss), Some((1.0, seed + 300)));
        let centres = largest(&centre_residuals(&s, &m));
        let (points, n_points) = point_shape(&s, &m);
        let (sh, sv) = (report.sigma_h.unwrap(), report.sigma_v.unwrap());
        eprintln!("seed {seed}: placed {}, centres {centres:.3} m ({:.3} %), {n_points} points {points:.3} m \
                   ({:.3} %), sigma estimated {sh:.3} / {sv:.3} m (noise 1 / 2 m), median {:.3} px, frame {:?}",
                  report.placed, 100.0 * centres / DIAMETER, 100.0 * points / DIAMETER,
                  report.median_reprojection_px, report.frame);
        assert_eq!(report.placed, PHOTOS, "seed {seed}");
        assert_eq!(report.frame, Some(Frame::Gnss), "seed {seed}");
        assert!(centres < 0.005 * DIAMETER, "seed {seed}: centres {centres} m");
        assert!(points < 0.005 * DIAMETER, "seed {seed}: points {points} m");
        assert!((sh - 1.0).abs() < 0.25, "seed {seed}: horizontal sigma {sh} m, noise 1 m");
        assert!((sv - 2.0).abs() < 0.5, "seed {seed}: vertical sigma {sv} m, noise 2 m");
    }
}

/// The world turned by 30 degrees about (1, 2, 3) (the poses, the points and the positions; the
/// pixels the same): the averaged rotations are turned onto the positions by a rotation that is
/// neither the identity nor symmetric, and the centres land on the turned truth itself (no
/// similarity). A rotation transposed anywhere (the frame, the poses, the rays) fails it.
#[test]
fn a_turned_world_is_not_a_transposed_one() {
    let mut s = ring(40, 240.0, HEIGHT, 21);
    let g = Rotation3::from_axis_angle(&nalgebra::Unit::new_normalize(Vector3::new(1.0, 2.0, 3.0)),
                                       30f64.to_radians())
        .into_inner();
    for p in &mut s.poses {
        p.rotation *= g.transpose();
        p.centre = g * p.centre;
    }
    for x in &mut s.points {
        *x = g * *x;
    }
    let gnss = gnss_with(&s, 0.3, 0.3, [1.0, 1.0, 1.0], 22, &[]);
    let (m, report) = mapped(&s, 23, Options::default(), Some(&gnss), None);
    assert_eq!(report.placed, 40);
    assert_eq!(report.frame, Some(Frame::Gnss));
    let q = Matrix3::from_fn(|i, j| report.frame_rotation.unwrap()[i][j]);
    assert!((q - q.transpose()).norm() > 0.5, "the frame rotation is nearly symmetric: {q}");
    let largest = m
        .centres()
        .iter()
        .zip(&s.poses)
        .map(|(c, p)| (Vector3::from(c.unwrap()) - p.centre).norm())
        .fold(0.0, f64::max);
    let state = m.state().unwrap();
    let turned = state
        .world_from_cam
        .iter()
        .zip(&s.poses)
        .map(|(w, p)| common::rotation_error_deg(&Matrix3::from_fn(|i, j| w.unwrap()[j][i]), &p.rotation))
        .fold(0.0, f64::max);
    eprintln!("turned world: centres {largest:.3} m from the turned truth, rotations {turned:.4} deg, {} points, \
               median {:.3} px", report.points, report.median_reprojection_px);
    assert!(largest < 0.005 * 240.0, "{largest} m");
    assert!(turned < 0.5, "a rotation {turned} degrees from the truth");
    assert!(report.points > 400 && report.median_reprojection_px < 0.7, "{report:?}");
}

/// Ruling R6 and Review Focus 2 after the rounds, judged as rulings R13 and R14 set it: a second
/// flight whose positions are off by (3, -2, 1) m is taken by its bias, and the errors of the bias
/// are calibrated against its posterior standard deviations (`MapperReport::bias_sd`: the inverse
/// of the normal matrix of the last adjustment, each group of observations over its own variance
/// factor, the components estimated jointly): over the 27 normalised errors (9 cases, 3 axes) none
/// beyond 4 and their root mean square between 0.5 and 1.5, and at most one seed with an axis beyond
/// 3. The draws are counted by seed (ruling R15): the RTK case and the 0.3 m / 0.5 m case of one
/// seed share one realisation of the noise, scaled, so they are not independent. The cases: the
/// accuracy recorded as the noise drawn (0.3 m / 0.5 m, seeds 33-38, the estimate of the accuracy
/// staying near it) and an RTK receiver (0.03 m / 0.05 m, seeds 34-36, its standard deviations
/// below the others' on every axis). In
/// every case the first flight is held at exactly zero and a fix 100 m off is the only outlier,
/// left out of the rounds, its photo placed with the others. Every case runs and prints its
/// figures before the checks.
///
/// Why against the bias's own precision: the second flight is half the ring, and a bias of half
/// the ring trades with a tilt (vertical) or a turn (horizontal) of the whole block and with the
/// bending the tie points allow, which only the positions fix: on seed 35 at 0.3 m / 0.5 m even the
/// rigid true block fitted to the positions puts the vertical bias 0.63 m off. At RTK the bending,
/// not the positions, limits the bias.
#[test]
fn a_second_flight_and_a_wrong_fix_after_the_rounds() {
    let offset = Vector3::new(3.0, -2.0, 1.0);
    let cases = (33..=38).map(|seed| (seed, 0.3, 0.5)).chain((34..=36).map(|seed| (seed, 0.03, 0.05)));
    let mut failures = Vec::new();
    let mut sds = Vec::new();
    let mut normalised_all = Vec::new();
    let mut seeds_beyond_3 = std::collections::BTreeSet::new();
    for (seed, plan, height) in cases {
        let s = ring(PHOTOS, DIAMETER, HEIGHT, 31);
        let mut gnss = gnss_with(&s, plan, height, [plan, plan, height], seed, &[]);
        second_flight(&mut gnss, PHOTOS / 2, offset.into());
        gnss.xyz[37][0] += 100.0;
        let (m, report) = mapped(&s, seed + 200, Options::default(), Some(&gnss), None);
        let residuals = centre_residuals(&s, &m);
        let shape = largest(&residuals);
        let bias = report.bias.get(1).map_or([f64::NAN; 3], |b| b.1);
        let error: [f64; 3] = std::array::from_fn(|a| bias[a] - offset[a]);
        let sd = report.bias_sd.iter().find(|(f, _)| *f == 2).map_or([f64::NAN; 3], |(_, sd)| *sd);
        let normalised: [f64; 3] = std::array::from_fn(|a| error[a] / sd[a]);
        let (sh, sv) = (report.sigma_h.unwrap_or(f64::NAN), report.sigma_v.unwrap_or(f64::NAN));
        eprintln!("seed {seed}, accuracy {plan} / {height} m: error {error:.3?} m, posterior sd {sd:.3?} m, \
                   normalised {normalised:.2?}; factors (ties, h, v) {:.3?}; outliers {:?}; centres {shape:.3} m; \
                   sigma {sh:.3} / {sv:.3} m", report.variance_factors.unwrap_or([f64::NAN; 3]),
                  report.gnss_outliers);
        sds.push((plan, sd));
        normalised_all.extend(normalised);
        if normalised.iter().any(|z| z.abs() > 3.0) {
            seeds_beyond_3.insert(seed);
        }
        let case = format!("seed {seed}, accuracy {plan} / {height} m");
        let checks = [
            (report.placed == PHOTOS, format!("placed {}", report.placed)),
            (report.gnss_outliers == vec![37], format!("outliers {:?}", report.gnss_outliers)),
            (report.bias.len() == 2 && report.bias[0] == (1, [0.0; 3]) && report.bias[1].0 == 2,
             format!("biases {:?}", report.bias)),
            (shape < 0.005 * DIAMETER, format!("centres {shape:.3} m")),
            (residuals[37].norm() < 0.005 * DIAMETER,
             format!("the photo of the wrong fix {:.3} m", residuals[37].norm())),
            // recorded as the noise: the estimate stays near it (and never above it)
            (sh <= plan && sh > 0.6 * plan.min(0.3), format!("horizontal sigma {sh}")),
            (sv <= height && sv > 0.6 * height.min(0.3), format!("vertical sigma {sv}")),
        ];
        failures.extend(checks.into_iter().filter(|(ok, _)| !ok).map(|(_, why)| format!("{case}: {why}")));
    }
    let beyond_3 = seeds_beyond_3.len();
    let beyond_4 = normalised_all.iter().filter(|z| z.abs() > 4.0).count();
    let rms = (normalised_all.iter().map(|z| z * z).sum::<f64>() / normalised_all.len() as f64).sqrt();
    eprintln!("27 normalised errors: rms {rms:.3}, seeds with an axis beyond 3 {seeds_beyond_3:?}, {beyond_4} \
               beyond 4");
    if !(beyond_3 <= 1 && beyond_4 == 0 && rms.is_finite() && (0.5..=1.5).contains(&rms)) {
        failures.push(format!("calibration: rms {rms:.3}, seeds beyond 3 {seeds_beyond_3:?}, {beyond_4} \
                               beyond 4"));
    }
    // RTK holds the bias better on every axis
    let mean = |plan: f64, a: usize| {
        let v: Vec<f64> = sds.iter().filter(|(p, _)| *p == plan).map(|(_, sd)| sd[a]).collect();
        v.iter().sum::<f64>() / v.len() as f64
    };
    for a in 0..3 {
        eprintln!("axis {a}: mean posterior sd {:.4} m at 0.3 / 0.5 m, {:.4} m at RTK", mean(0.3, a), mean(0.03, a));
        if !(mean(0.03, a) < mean(0.3, a)) {
            failures.push(format!("axis {a}: the posterior sd of RTK is not below the others'"));
        }
    }
    assert!(failures.is_empty(), "{failures:#?}");
}

/// Ruling R1 with the accuracy recorded as the noise: the estimate of a survey-grade receiver stays
/// near its recorded accuracy (the positioning keeps the accurate positions, ruling R9). Every
/// seed runs and prints before the checks.
#[test]
fn the_estimate_of_accurate_positions_stays_near_them() {
    let mut failures = Vec::new();
    for seed in [14, 15, 16] {
        let s = ring(PHOTOS, DIAMETER, HEIGHT, seed);
        let gnss = gnss_with(&s, 0.3, 0.5, [0.3, 0.3, 0.5], seed + 100, &[]);
        let (_, report) = mapped(&s, seed + 200, Options::default(), Some(&gnss), None);
        let (sh, sv) = (report.sigma_h.unwrap_or(f64::NAN), report.sigma_v.unwrap_or(f64::NAN));
        eprintln!("seed {seed}: recorded 0.3 / 0.5 m, estimated {sh:.3} / {sv:.3} m; frame {:?}, {} outliers",
                  report.frame, report.gnss_outliers.len());
        if !((sh - 0.3).abs() < 0.075 && (sv - 0.5).abs() < 0.125) {
            failures.push(format!("seed {seed}: {sh} / {sv} m, frame {:?}, {} outliers", report.frame,
                                  report.gnss_outliers.len()));
        }
    }
    assert!(failures.is_empty(), "{failures:#?}");
}

/// Ruling R7 and Review Focus 3 after the rounds: a tenth of the photos without a position are
/// placed by their tie points within 1 m of the truth after one similarity, horizontally and
/// vertically.
#[test]
fn photos_without_gps_are_placed_within_a_metre() {
    for seed in [41, 43, 45] {
        let s = ring(PHOTOS, DIAMETER, HEIGHT, seed);
        let without: Vec<usize> = (0..PHOTOS).filter(|i| i % 10 == 3).collect();
        let gnss = gnss_with(&s, 1.0, 2.0, [5.0, 5.0, 10.0], seed + 1, &without);
        let (m, report) = mapped(&s, seed + 200, Options::default(), Some(&gnss), None);
        assert_eq!(report.placed, PHOTOS, "seed {seed}");
        let r = centre_residuals(&s, &m);
        let horizontal: Vec<f64> = without.iter().map(|&i| r[i].xy().norm()).collect();
        let vertical: Vec<f64> = without.iter().map(|&i| r[i].z.abs()).collect();
        eprintln!("seed {seed}: photos without a position: horizontal {horizontal:.3?} m, vertical {vertical:.3?} m");
        for (k, &i) in without.iter().enumerate() {
            assert!(horizontal[k] < 1.0, "seed {seed}: photo {i}: {} m horizontally", horizontal[k]);
            assert!(vertical[k] < 1.0, "seed {seed}: photo {i}: {} m vertically", vertical[k]);
        }
    }
}

/// The ring of 60 photos seen through an OPENCV sensor whose principal point is 20 px off the
/// centre of the photo, the adjustment starting at the centre (a RADIAL camera of the mapper).
fn shifted_principal_point(s: &Scene, seed: u64) -> (Input, [f64; 2]) {
    let mut input = noisy(s, seed);
    let shift = [16.0, -12.0];
    for p in &mut input.keypoints {
        p[0] += shift[0] as f32;
        p[1] += shift[1] as f32;
    }
    let cam = &mut input.cameras[0];
    cam.model = "RADIAL".into();
    cam.params = vec![common::FOCAL, s.cx, s.cy, 0.0, 0.0];
    (input, [s.cx + shift[0], s.cy + shift[1]])
}

#[test]
fn the_principal_point_is_freed_only_on_a_block_held_by_gnss() {
    let s = ring(60, 360.0, HEIGHT, 51);
    let o = Options { opencv_sensor: vec![true], ..Options::default() };
    let gnss = gnss_with(&s, 0.3, 0.5, [0.3, 0.3, 0.5], 52, &[]);

    let (input, truth) = shifted_principal_point(&s, 53);
    let mut held = Mapper::new(input, o.clone()).unwrap();
    let report = run(&mut held, Some(&gnss), None);
    let last = &report.rounds[2];
    assert_eq!(last.intrinsics, ["focal", "radial", "principal_point", "decentring"].map(String::from).to_vec());
    let cam = &held.state().unwrap().cameras[0];
    assert_eq!(cam.model, Model::OpenCv, "widened to OPENCV");
    let (cx, cy) = (cam.params[2], cam.params[3]);
    let error = (cx - truth[0]).hypot(cy - truth[1]);
    eprintln!("with GNSS: principal point ({cx:.2}, {cy:.2}), truth ({}, {}): {error:.2} px; fx {:.2}, fy {:.2}",
              truth[0], truth[1], cam.params[0], cam.params[1]);
    assert!(error < 3.0, "{error} px from the truth");

    let (input, _) = shifted_principal_point(&s, 53);
    let mut free = Mapper::new(input, o).unwrap();
    let report = run(&mut free, None, None);
    assert!(report.rounds.iter().all(|r| !r.intrinsics.contains(&"principal_point".to_string())));
    let cam = &free.state().unwrap().cameras[0];
    assert_eq!(cam.model, Model::Radial);
    assert_eq!((cam.params[1], cam.params[2]), (s.cx, s.cy), "without GNSS the principal point is held");
}

#[test]
fn stages_out_of_order_are_refused() {
    let s = ring(40, 240.0, HEIGHT, 61);
    let mut m = Mapper::new(noisy(&s, 62), Options::default()).unwrap();
    let mut go = never_cancel();
    let dir = tmp("order");
    assert!(m.rotations(&mut go).unwrap_err().contains("view_graph"));
    assert!(m.tracks(PER_PHOTO, &mut go).unwrap_err().contains("rotations"));
    assert!(m.position(None, &mut go).unwrap_err().contains("tracks"));
    assert!(m.refine(&mut go).unwrap_err().contains("position"));
    assert!(m.write_model(&dir).is_err());
    m.view_graph(&mut go).unwrap();
    assert!(m.view_graph(&mut go).unwrap_err().contains("view_graph"));
    assert!(m.tracks(PER_PHOTO, &mut go).unwrap_err().contains("rotations"));
    m.rotations(&mut go).unwrap();
    assert!(m.set_view_graph(m.graph().unwrap().clone()).is_err(), "the graph is replaced before the rotations only");
    assert!(m.refine(&mut go).unwrap_err().contains("position"));
    m.tracks(PER_PHOTO, &mut go).unwrap();
    assert!(m.write_model(&dir).is_err());
    m.position(None, &mut go).unwrap();
    assert!(m.centres().iter().all(|c| c.is_some()), "every photo placed by the positioning");
    m.refine(&mut go).unwrap();
    assert!(m.refine(&mut go).unwrap_err().contains("refine"));
    assert_eq!(m.write_model(&dir).unwrap(), 40);
    // an input the mapper cannot use is refused at once
    let mut bad = noisy(&s, 62);
    bad.pairs[0].b = bad.pairs[0].a;
    assert!(Mapper::new(bad, Options::default()).is_err());
}

#[test]
fn observe_false_cancels_every_stage() {
    let s = ring(40, 240.0, HEIGHT, 71);
    let gnss = gnss_with(&s, 1.0, 2.0, [5.0, 5.0, 10.0], 72, &[]);
    // the calls of a whole run, then a cancel at several of them (between and inside the stages)
    let mut calls = 0usize;
    {
        let mut m = Mapper::new(noisy(&s, 73), Options::default()).unwrap();
        let mut count = || {
            calls += 1;
            true
        };
        m.view_graph(&mut count).unwrap();
        m.rotations(&mut count).unwrap();
        m.tracks(PER_PHOTO, &mut count).unwrap();
        m.position(Some(&gnss), &mut count).unwrap();
        m.refine(&mut count).unwrap();
    }
    assert!(calls > 20, "{calls} calls of observe");
    for stop_at in [1, 2, 4, calls / 3, calls / 2, calls - 5, calls] {
        let mut m = Mapper::new(noisy(&s, 73), Options::default()).unwrap();
        let mut n = 0usize;
        let mut observe = || {
            n += 1;
            n < stop_at
        };
        let out = (|| {
            m.view_graph(&mut observe)?;
            m.rotations(&mut observe)?;
            m.tracks(PER_PHOTO, &mut observe)?;
            m.position(Some(&gnss), &mut observe)?;
            m.refine(&mut observe)?;
            Ok::<(), String>(())
        })();
        assert_eq!(out, Err("cancelled".to_string()), "a cancel at call {stop_at} of {calls}");
        assert!(m.write_model(&tmp("cancelled")).is_err(), "nothing to write after a cancel");
    }
}

/// Two runs on two flights with a wrong fix and photos without positions write the same bytes.
#[test]
fn the_same_bits_twice() {
    let s = ring(40, 240.0, HEIGHT, 81);
    let mut gnss = gnss_with(&s, 0.3, 0.5, [0.3, 0.3, 0.5], 82, &[3]);
    second_flight(&mut gnss, 20, [3.0, -2.0, 1.0]);
    gnss.xyz[10][1] += 100.0;
    let mut files = Vec::new();
    for run_index in 0..2 {
        let (mut m, report) = mapped(&s, 83, Options::default(), Some(&gnss), Some((1.0, 84)));
        assert_eq!(report.placed, 40);
        let dir = tmp(&format!("bits_{run_index}"));
        assert_eq!(m.write_model(&dir).unwrap(), 40);
        let names = ["cameras.bin", "frames.bin", "images.bin", "points3D.bin", "rigs.bin"];
        files.push(names.map(|n| std::fs::read(dir.join(n)).unwrap()));
        m.refine(&mut never_cancel()).unwrap_err();
    }
    for (k, (a, b)) in files[0].iter().zip(&files[1]).enumerate() {
        assert!(a == b, "file {k} differs between the two runs");
    }
}

/// The model the mapper writes, read back: every registered photo with all its key points, points
/// with their tracks, ids those of the input.
#[test]
fn the_model_holds_every_key_point() {
    let s = ring(40, 240.0, HEIGHT, 91);
    let input = noisy(&s, 92);
    let gnss = gnss_with(&s, 1.0, 2.0, [5.0, 5.0, 10.0], 93, &[]);
    let mut m = Mapper::new(input.clone(), Options::default()).unwrap();
    let report = run(&mut m, Some(&gnss), None);
    let dir = tmp("model");
    assert_eq!(m.write_model(&dir).unwrap(), 40);
    let images = std::fs::read(dir.join("images.bin")).unwrap();
    let points = std::fs::read(dir.join("points3D.bin")).unwrap();
    let count = |b: &[u8]| u64::from_le_bytes(b[0..8].try_into().unwrap());
    assert_eq!(count(&images), 40);
    assert_eq!(count(&points) as usize, report.points);
    // the first image: its id, its name and every key point of the photo
    let id = u32::from_le_bytes(images[8..12].try_into().unwrap());
    assert_eq!(id, input.images[0].image_id);
    let name_at = 8 + 4 + 7 * 8 + 4;
    let name = Scene::name(0);
    assert_eq!(&images[name_at..name_at + name.len()], name.as_bytes());
    let n2d_at = name_at + name.len() + 1;
    let n2d = u64::from_le_bytes(images[n2d_at..n2d_at + 8].try_into().unwrap()) as usize;
    assert_eq!(n2d, input.images[0].keypoints.len());
    let first = &images[n2d_at + 8..n2d_at + 8 + 24];
    let kp = input.keypoints[input.images[0].keypoints.start];
    assert_eq!(f64::from_le_bytes(first[0..8].try_into().unwrap()), kp[0] as f64);
    assert_eq!(f64::from_le_bytes(first[8..16].try_into().unwrap()), kp[1] as f64);
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// The bytes pycolmap 4.2.1 writes for a RADIAL camera 3 of 200 x 100 pixels with the parameters
/// (100, 50, 50, 0.01, 0) and an image 7, "a.jpg", at the identity pose with one point 2D at (1, 2)
/// and no point 3D, each added with its trivial rig and frame (recorded once with pycolmap).
#[test]
fn the_bytes_are_pycolmaps() {
    let dir = tmp("pycolmap_bytes");
    let camera =
        ModelCamera { camera_id: 3, model: Model::Radial, width: 200, height: 100,
                      params: vec![100.0, 50.0, 50.0, 0.01, 0.0] };
    let image = ModelImage {
        image_id: 7,
        camera_id: 3,
        name: "a.jpg".into(),
        rotation: quaternion(&[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]),
        translation: [0.0; 3],
        points2d: vec![([1.0, 2.0], NO_POINT)],
    };
    write_colmap_binary(&dir, &[camera], &[image], &[]).unwrap();
    let read = |name: &str| hex(&std::fs::read(dir.join(name)).unwrap());
    assert_eq!(read("rigs.bin"), "010000000000000003000000010000000000000003000000");
    assert_eq!(
        read("cameras.bin"),
        concat!(
            "01000000000000000300000003000000c800000000000000640000000000000000000000000059400000000000004940",
            "00000000000049407b14ae47e17a843f0000000000000000",
        )
    );
    assert_eq!(
        read("frames.bin"),
        concat!(
            "01000000000000000700000003000000000000000000f03f000000000000000000000000000000000000000000000000",
            "0000000000000000000000000000000000000000000000000100000000000000030000000700000000000000",
        )
    );
    assert_eq!(
        read("images.bin"),
        concat!(
            "010000000000000007000000000000000000f03f00000000000000000000000000000000000000000000000000000000",
            "000000000000000000000000000000000000000003000000612e6a7067000100000000000000000000000000f03f0000",
            "000000000040ffffffffffffffff",
        )
    );
    assert_eq!(read("points3D.bin"), "0000000000000000");
}

#[test]
fn a_model_that_does_not_fit_is_refused() {
    let dir = tmp("refused");
    let camera =
        ModelCamera { camera_id: 1, model: Model::SimplePinhole, width: 10, height: 10, params: vec![1.0; 3] };
    let image = ModelImage {
        image_id: 1,
        camera_id: 1,
        name: "a".into(),
        rotation: [1.0, 0.0, 0.0, 0.0],
        translation: [0.0; 3],
        points2d: vec![([1.0, 1.0], 1)],
    };
    let point = ModelPoint { point_id: 1, xyz: [0.0; 3], color: [0; 3], error: 0.0, track: vec![(1, 0)] };
    write_colmap_binary(&dir, &[camera.clone()], &[image.clone()], &[point.clone()]).unwrap();
    let other = ModelImage { camera_id: 2, ..image.clone() };
    assert!(write_colmap_binary(&dir, &[camera.clone()], &[other], &[]).is_err());
    let beyond = ModelPoint { track: vec![(1, 1)], ..point.clone() };
    assert!(write_colmap_binary(&dir, &[camera.clone()], &[image.clone()], &[beyond]).is_err());
    assert!(write_colmap_binary(&dir, &[camera.clone(), camera], &[image], &[point]).is_err());
}



/// The stop flag of the mapper (`Mapper::stop_flag`, set by the bindings from another thread when
/// the progress callback raises) ends `view_graph`, `rotations` and `tracks` from inside their
/// steps, `observe` saying go on: each stage set going with the flag up is cancelled, and runs once
/// the flag is down again.
#[test]
fn the_stop_flag_cancels_the_stages_from_inside() {
    let s = ring(40, 240.0, HEIGHT, 101);
    for stopped in 0..3 {
        let mut m = Mapper::new(noisy(&s, 102), Options::default()).unwrap();
        let stop = m.stop_flag();
        let mut go = never_cancel();
        for stage in 0..=stopped {
            let up = stage == stopped;
            stop.store(up, Ordering::SeqCst);
            let out = match stage {
                0 => m.view_graph(&mut go).map(|_| ()),
                1 => m.rotations(&mut go).map(|_| ()),
                _ => m.tracks(PER_PHOTO, &mut go).map(|_| ()),
            };
            if up {
                assert_eq!(out, Err("cancelled".to_string()), "stage {stage} with the flag up");
            } else {
                out.unwrap();
            }
        }
        stop.store(false, Ordering::SeqCst);
        match stopped {
            0 => m.view_graph(&mut go).map(|_| ()),
            1 => m.rotations(&mut go).map(|_| ()),
            _ => m.tracks(PER_PHOTO, &mut go).map(|_| ()),
        }
        .unwrap();
    }
}

/// The flag raised while the relative poses are being estimated ends the view graph long before
/// it would have ended (the pairs not yet begun are skipped).
#[test]
fn the_stop_flag_ends_the_view_graph_early() {
    let s = ring(120, 240.0, HEIGHT, 111);
    let input = noisy(&s, 112);
    let clock = Instant::now();
    Mapper::new(input.clone(), Options::default()).unwrap().view_graph(&mut never_cancel()).unwrap();
    let whole = clock.elapsed();
    let mut m = Mapper::new(input, Options::default()).unwrap();
    let stop = m.stop_flag();
    let raiser = {
        let stop = stop.clone();
        std::thread::spawn(move || {
            std::thread::sleep(whole / 10);
            stop.store(true, Ordering::SeqCst);
        })
    };
    let clock = Instant::now();
    let out = m.view_graph(&mut never_cancel()).map(|_| ());
    let stopped = clock.elapsed();
    raiser.join().unwrap();
    assert_eq!(out, Err("cancelled".to_string()));
    assert!(stopped < whole * 6 / 10, "stopped after {stopped:?}, the whole stage takes {whole:?}");
}

/// The positioning gives what it found (`Positioned`): the frame, the accuracy of the positions.
#[test]
fn the_positioning_is_returned() {
    let s = ring(40, 240.0, HEIGHT, 121);
    let gnss = gnss_with(&s, 1.0, 2.0, [5.0, 5.0, 10.0], 122, &[]);
    let mut m = Mapper::new(noisy(&s, 123), Options::default()).unwrap();
    let mut go = never_cancel();
    m.view_graph(&mut go).unwrap();
    m.rotations(&mut go).unwrap();
    m.tracks(PER_PHOTO, &mut go).unwrap();
    let p = m.position(Some(&gnss), &mut go).unwrap();
    assert_eq!(p.frame, Frame::Gnss);
    assert_eq!((p.sigma_h, p.sigma_v), (Some(5.0), Some(10.0)));
    assert_eq!(p.centres.len(), 40);
}
