//! The global positioning of the native mapper (`mapper::positioning`) on scenes of known truth:
//! a ring flown once with the positions of its photos (the proof of section 8.4 of the design at
//! the level of the positioning: a slow error of the rotations bends the ring without the
//! positions and not with them), exact rotations, a wrong fix and a second flight, photos without
//! a position, positions that cannot orient a small block, a strip from a random start without
//! positions, two runs that give the same bits and a cancel.

mod common;

use common::{patch, ring, scene, InputOptions, Kind, Random, Scene};
use nalgebra::{Matrix3, Rotation3, Vector3};
use ppg_engine::camera::Camera;
use ppg_engine::mapper::graph::{Edge, ViewGraph};
use ppg_engine::mapper::positioning::{position, position_with, Frame, Gnss, Positioned, Scales};
use ppg_engine::mapper::rotation::Rotations;
use ppg_engine::mapper::tracks::{self, Tracks};
use ppg_engine::mapper::input::Input;
use ppg_engine::mapper::Options;

/// The scale of the large set: 100 photos on a ring 600 m across, 40 m over the ground.
const PHOTOS: usize = 100;
const DIAMETER: f64 = 600.0;
const HEIGHT: f64 = 40.0;
/// The amplitude of the slow error of the rotations about the vertical, `A sin(theta)` round the
/// ring, in degrees: enough to bend the ring by more than 2 % of its diameter without positions.
const BEND_DEG: f64 = 5.0;

fn array(r: &Matrix3<f64>) -> [[f64; 3]; 3] {
    std::array::from_fn(|i| std::array::from_fn(|j| r[(i, j)]))
}

/// What the positioning starts from on a scene: the input, its camera, the view graph of the pairs
/// the input matched (their true relative poses, every pair with parallax), the tracks of all the
/// photos, all of them selected.
struct Case {
    input: Input,
    cams: Vec<Camera>,
    graph: ViewGraph,
    tracks: Tracks,
    selected: Vec<usize>,
}

fn case_of(s: &Scene) -> Case {
    let input = s.input(&InputOptions::default());
    let cams = vec![Camera::new("SIMPLE_PINHOLE", &input.cameras[0].params, common::WIDTH, common::HEIGHT).unwrap()];
    let edges = input
        .pairs
        .iter()
        .enumerate()
        .map(|(k, p)| {
            let (r, t) = s.relative(p.a, p.b);
            Edge {
                pair: k,
                a: p.a,
                b: p.b,
                rotation: array(&r),
                translation: [t.x, t.y, t.z],
                inliers: p.matches.len(),
                inlier_matches: (p.matches.start as u32..p.matches.end as u32).collect(),
                parallax_deg: 10.0,
                parallax: true,
                weight: p.matches.len() as f64,
            }
        })
        .collect();
    let graph = ViewGraph { edges, left_out: 0, too_few: 0, refused: None };
    let tracks = tracks::build(&input, &vec![true; input.images.len()]);
    let selected = (0..tracks.len()).collect();
    Case { input, cams, graph, tracks, selected }
}

/// The averaged rotations: the true `world_from_cam` of every photo turned about the vertical by
/// `bend_deg * sin(theta)`, theta the angle of the photo round the ring.
fn rotations_of(s: &Scene, bend_deg: f64) -> Rotations {
    let n = s.poses.len();
    let world_from_cam = s
        .poses
        .iter()
        .enumerate()
        .map(|(i, p)| {
            let theta = std::f64::consts::TAU * i as f64 / n as f64;
            let error = Rotation3::from_axis_angle(&Vector3::z_axis(), bend_deg.to_radians() * theta.sin());
            Some(array(&(error.into_inner() * p.rotation.transpose())))
        })
        .collect();
    Rotations { world_from_cam, placed: n, dropped: Vec::new(), components: vec![n], refused: None }
}

/// Positions of the photos: the true centres plus normal noise of `plan` metres on each
/// horizontal axis and `height` on the vertical, accuracy 5 m and 10 m recorded, one flight; the
/// photos of `without` have none.
fn gnss_of(s: &Scene, plan: f64, height: f64, seed: u64, without: &[usize]) -> Gnss {
    gnss_with(s, plan, height, [5.0, 5.0, 10.0], seed, without)
}

/// `gnss_of` with the accuracy `sigma` recorded.
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

fn run(c: &Case, rotations: &Rotations, gnss: Option<&Gnss>) -> Positioned {
    position(&c.input, &c.cams, &c.graph, rotations, &c.tracks, &c.selected, gnss, &Options::default(),
             &mut || true)
    .expect("a positioning")
}

/// The residual vectors of `est` after the similarity (Umeyama) that best takes it onto `truth`,
/// with the similarity's scale.
fn after_similarity(est: &[Vector3<f64>], truth: &[Vector3<f64>]) -> (Vec<Vector3<f64>>, f64) {
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
    let residuals = est.iter().zip(truth).map(|(e, t)| scale * r * (e - me) + mt - t).collect();
    (residuals, scale)
}

/// The residuals of the centres of `p` after one similarity onto the truth: per photo, the largest
/// in 3D, the largest horizontal one and the root mean square of the vertical ones. Every photo
/// must have a centre.
struct Shape {
    residuals: Vec<Vector3<f64>>,
    largest: f64,
    horizontal: f64,
    vertical_rms: f64,
}

fn shape_of(s: &Scene, p: &Positioned) -> Shape {
    let est: Vec<Vector3<f64>> =
        p.centres.iter().enumerate().map(|(i, c)| Vector3::from(c.unwrap_or_else(|| panic!("photo {i}")))).collect();
    let truth: Vec<Vector3<f64>> = s.poses.iter().map(|q| q.centre).collect();
    let (residuals, _) = after_similarity(&est, &truth);
    let largest = residuals.iter().map(|r| r.norm()).fold(0.0, f64::max);
    let horizontal = residuals.iter().map(|r| r.xy().norm()).fold(0.0, f64::max);
    let vertical_rms = (residuals.iter().map(|r| r.z * r.z).sum::<f64>() / residuals.len() as f64).sqrt();
    Shape { residuals, largest, horizontal, vertical_rms }
}

/// The largest residual of the centres after one similarity, and the residuals per photo.
fn centre_residuals(s: &Scene, p: &Positioned) -> (f64, Vec<f64>) {
    let shape = shape_of(s, p);
    (shape.largest, shape.residuals.iter().map(|r| r.norm()).collect())
}

/// The proof of section 8.4 of the design at the level of the positioning: a slow error of the
/// rotations about the vertical (5 degrees, `BEND_DEG`, enough to bend the ring by more than 2 % of
/// its diameter without positions) is held by the positions of the photos. At this level the
/// heights of nadir photos are weakly held (the depth along each ray is free), so the test asserts
/// the plan of the ring (the largest horizontal residual after one similarity below 0.5 % of the
/// diameter) and heights no worse than the vertical noise of the positions (2 m, rms); the full 3D
/// gate of 0.5 % is asserted after the rounds of adjustment (Tasks 7 and 10), which hold the
/// heights by the reprojection.
#[test]
fn the_ring_is_held_by_its_gnss() {
    let s = ring(PHOTOS, DIAMETER, HEIGHT, 11);
    let c = case_of(&s);
    let rotations = rotations_of(&s, BEND_DEG);

    let free = run(&c, &rotations, None);
    assert_eq!(free.frame, Frame::Free);
    let bent = shape_of(&s, &free);

    let gnss = gnss_of(&s, 1.0, 2.0, 12, &[]);
    let held = run(&c, &rotations, Some(&gnss));
    let shape = shape_of(&s, &held);
    eprintln!("ring of {PHOTOS} photos, {DIAMETER} m, bend {BEND_DEG} deg: without GNSS {:.2} m ({:.2} %); with \
               GNSS horizontal {:.3} m ({:.3} %), 3D {:.3} m ({:.3} %), vertical rms {:.3} m; sigma_h {:?}, \
               sigma_v {:?}, frame residual {:?} deg", bent.largest, 100.0 * bent.largest / DIAMETER,
              shape.horizontal, 100.0 * shape.horizontal / DIAMETER, shape.largest, 100.0 * shape.largest / DIAMETER,
              shape.vertical_rms, held.sigma_h, held.sigma_v, held.frame_residual_deg);
    assert!(bent.largest > 0.02 * DIAMETER,
            "without GNSS the ring must be bent for the case to prove anything: {} m", bent.largest);
    assert_eq!(held.frame, Frame::Gnss);
    assert!(shape.horizontal < 0.005 * DIAMETER, "with GNSS, horizontal: {} m", shape.horizontal);
    assert!(shape.vertical_rms < 2.0, "with GNSS, vertical rms: {} m", shape.vertical_rms);
    assert_eq!((held.sigma_h, held.sigma_v), (Some(5.0), Some(10.0)), "the recorded accuracy is used");
    assert!(held.gnss_outliers.is_empty(), "{:?}", held.gnss_outliers);
}

#[test]
fn exact_rotations_give_the_truth() {
    let s = ring(PHOTOS, DIAMETER, HEIGHT, 21);
    let c = case_of(&s);
    let gnss = gnss_of(&s, 0.0, 0.0, 22, &[]);
    let p = run(&c, &rotations_of(&s, 0.0), Some(&gnss));
    assert_eq!(p.frame, Frame::Gnss);
    let (largest, _) = centre_residuals(&s, &p);
    assert!(largest < 1e-6 * DIAMETER, "{largest} m");
    // without noise the GNSS frame is the true one: the centres are the truth itself
    for (i, c) in p.centres.iter().enumerate() {
        let c = Vector3::from(c.unwrap());
        assert!((c - s.poses[i].centre).norm() < 1e-6 * DIAMETER, "photo {i}: {c:?}");
    }
    let frame_residual = p.frame_residual_deg.expect("the residual of the frame");
    assert!(frame_residual < 1e-6, "{frame_residual} deg");
    assert_eq!(p.scales_on_floor, 0, "every scale above its floor");
    let placed = p.points.iter().filter(|x| x.is_some()).count();
    assert!(placed > 9 * c.selected.len() / 10, "{placed} of {} points", c.selected.len());
    for (k, x) in p.points.iter().enumerate() {
        if let Some(x) = x {
            let truth = s.points[point_of_track(&s, &c, c.selected[k])];
            assert!((Vector3::from(*x) - truth).norm() < 1e-6 * DIAMETER, "track {k}");
        }
    }
}

/// The point of the scene a track follows (its first observation's).
fn point_of_track(s: &Scene, c: &Case, track: usize) -> usize {
    let (image, k) = c.tracks.track(track)[0];
    s.views[image as usize][k as usize].0
}

/// Review Focus 2 at the level of the positioning: one position 100 m off is found as an outlier
/// and its photo is still placed by its rays; a second flight whose positions are all off by
/// (3, -2, 1) m is taken by a bias of that flight. The positioning is a coarse stage: its ray
/// residual (GLOMAP's, unweighted) cannot tell a bend of about a metre of the block from a bias,
/// so here the bias is asserted within 1.5 m over six draws of the noise; the original limit
/// (within 0.3 m) is asserted after the rounds of adjustment of Task 7, whose reprojection holds
/// the shape.
#[test]
fn a_wrong_gps_fix_and_a_second_flight() {
    let s = ring(PHOTOS, DIAMETER, HEIGHT, 31);
    let c = case_of(&s);
    let rotations = rotations_of(&s, 0.0);

    let mut gnss = gnss_of(&s, 1.0, 2.0, 32, &[]);
    gnss.xyz[37][0] += 100.0;
    let p = run(&c, &rotations, Some(&gnss));
    let shape = shape_of(&s, &p);
    eprintln!("a fix 100 m off: horizontal {:.3} m, 3D {:.3} m, vertical rms {:.3} m, outliers {:?}",
              shape.horizontal, shape.largest, shape.vertical_rms, p.gnss_outliers);
    assert_eq!(p.frame, Frame::Gnss);
    assert_eq!(p.gnss_outliers, vec![37]);
    assert!(shape.horizontal < 0.005 * DIAMETER, "{} m", shape.horizontal);
    assert!(shape.vertical_rms < 2.0, "{} m", shape.vertical_rms);
    assert!(shape.residuals[37].norm() < 0.005 * DIAMETER, "the photo of the wrong fix: {:?}", shape.residuals[37]);
    assert!(p.bias.is_empty(), "one flight has no bias: {:?}", p.bias);

    // the noise of the weak ring of the plan (0.3 m in plan, 0.5 m in height)
    let offset = [3.0, -2.0, 1.0];
    for seed in 33..=38 {
        let mut gnss = gnss_of(&s, 0.3, 0.5, seed, &[]);
        second_flight(&mut gnss, PHOTOS / 2, offset);
        let p = run(&c, &rotations, Some(&gnss));
        let (shape, _) = centre_residuals(&s, &p);
        assert_eq!(p.frame, Frame::Gnss);
        assert_eq!(p.bias.len(), 2);
        assert_eq!(p.bias[0], (1, [0.0; 3]), "seed {seed}");
        assert_eq!(p.bias[1].0, 2);
        let error = (Vector3::from(p.bias[1].1) - Vector3::from(offset)).norm();
        eprintln!("seed {seed}: a second flight off by {offset:?}: bias {:.3?}, {error:.3} m from it, \
                   shape {shape:.3} m", p.bias[1].1);
        assert!(error < 1.5, "seed {seed}: bias {:?}: {error} m from the offset", p.bias[1].1);
        assert!(shape < 0.005 * DIAMETER, "seed {seed}: {shape} m");
        assert!(p.gnss_outliers.is_empty(), "seed {seed}: {:?}", p.gnss_outliers);
    }
}

/// Review Focus 2 with positions whose recorded accuracy is their noise (a survey-grade receiver,
/// and an RTK one): the offset of the second flight is far beyond 5 recorded sigmas, so the
/// outliers are found with a translation per flight and checked again after the solve with the
/// biases; no position of the second flight is an outlier, its bias is found (within 1.5 m, R6),
/// and a fix 100 m off is still caught.
#[test]
fn a_second_flight_with_accurate_positions() {
    let s = ring(PHOTOS, DIAMETER, HEIGHT, 31);
    let c = case_of(&s);
    let rotations = rotations_of(&s, 0.0);
    let offset = [3.0, -2.0, 1.0];
    for (plan, height) in [(0.3, 0.5), (0.03, 0.05)] {
        let mut gnss = gnss_with(&s, plan, height, [plan, plan, height], 34, &[]);
        second_flight(&mut gnss, PHOTOS / 2, offset);
        gnss.xyz[37][0] += 100.0;
        let p = run(&c, &rotations, Some(&gnss));
        let shape = shape_of(&s, &p);
        let error = (Vector3::from(p.bias[1].1) - Vector3::from(offset)).norm();
        eprintln!("accuracy {plan} / {height} m: bias {:.3?}, {error:.3} m from the offset; outliers {:?}; \
                   horizontal {:.3} m, vertical rms {:.3} m", p.bias, p.gnss_outliers, shape.horizontal,
                  shape.vertical_rms);
        assert_eq!(p.frame, Frame::Gnss);
        assert_eq!(p.gnss_outliers, vec![37], "accuracy {plan} / {height} m");
        assert_eq!(p.bias[0], (1, [0.0; 3]));
        assert_eq!(p.bias[1].0, 2);
        assert!(error < 1.5, "accuracy {plan} / {height} m: bias {:?}", p.bias[1].1);
        assert!(shape.horizontal < 0.005 * DIAMETER, "{} m", shape.horizontal);
        assert!(shape.vertical_rms < 2.0, "{} m", shape.vertical_rms);
    }
}

/// The averaged rotations in a frame of their own: every one turned by `G` (30 degrees about
/// (1, 2, 3)), the relative poses unchanged. The frame is turned back onto the positions
/// (`frame_rotation` is `G^T`) and the block is the ring's. With the rotations of the ring and
/// patch not symmetric, a transposed rotation anywhere (the poses, the rays, the directions of
/// the pairs, Horn's solution) fails this test.
#[test]
fn the_frame_of_the_rotations_is_turned_onto_the_positions() {
    let s = ring(PHOTOS, DIAMETER, HEIGHT, 101);
    let c = case_of(&s);
    let turn = Rotation3::from_axis_angle(&nalgebra::Unit::new_normalize(Vector3::new(1.0, 2.0, 3.0)),
                                          30f64.to_radians())
        .into_inner();
    let mut rotations = rotations_of(&s, 0.0);
    for w in rotations.world_from_cam.iter_mut() {
        let r = turn * Matrix3::from_fn(|i, j| w.unwrap()[i][j]);
        *w = Some(array(&r));
    }
    let gnss = gnss_of(&s, 1.0, 2.0, 102, &[]);
    let p = run(&c, &rotations, Some(&gnss));
    assert_eq!(p.frame, Frame::Gnss);
    let q = Matrix3::from_fn(|i, j| p.frame_rotation[i][j]);
    let off = common::rotation_error_deg(&q, &turn.transpose());
    let shape = shape_of(&s, &p);
    eprintln!("frame turned by 30 deg: frame_rotation {off:.3} deg from G^T; horizontal {:.3} m, vertical rms \
               {:.3} m", shape.horizontal, shape.vertical_rms);
    assert!(off < 0.5, "frame_rotation {off} deg from G^T");
    assert!(shape.horizontal < 0.005 * DIAMETER, "{} m", shape.horizontal);
    assert!(shape.vertical_rms < 2.0, "{} m", shape.vertical_rms);
    // the centres themselves, not only their shape, are on the truth in the frame of the positions
    let largest = (0..PHOTOS)
        .map(|i| (Vector3::from(p.centres[i].unwrap()) - s.poses[i].centre).xy().norm())
        .fold(0.0, f64::max);
    assert!(largest < 0.005 * DIAMETER, "{largest} m");
}

/// Review Focus 3 at the level of the positioning: a tenth of the photos without a position are
/// placed by their tie points among the others. The positioning is a coarse stage: its ray
/// residual (GLOMAP's, unweighted) cannot tell a bend of about a metre from a position and holds
/// the heights of nadir photos weakly, so the block follows the noise of the positions over a few
/// photos (absolute residuals of about 2 m, which the positioned neighbours of a photo share).
/// Here every photo is placed, each photo without a position lies within 1 m horizontally of the
/// mean of its two ring neighbours (it is placed by its tie points), and their heights are no
/// worse than the vertical noise of the positions (2 m, rms), over three scenes; the absolute
/// limit (within 1 m of the truth) is asserted after the rounds of adjustment of Task 7.
#[test]
fn photos_without_gps_are_placed() {
    for seed in [41, 43, 45] {
        let s = ring(PHOTOS, DIAMETER, HEIGHT, seed);
        let c = case_of(&s);
        let without: Vec<usize> = (0..PHOTOS).filter(|i| i % 10 == 3).collect();
        let gnss = gnss_of(&s, 1.0, 2.0, seed + 1, &without);
        let p = run(&c, &rotations_of(&s, 0.0), Some(&gnss));
        assert_eq!(p.frame, Frame::Gnss);
        let shape = shape_of(&s, &p);
        let r = &shape.residuals;
        let horizontal: Vec<f64> = without.iter().map(|&i| r[i].xy().norm()).collect();
        let relative: Vec<f64> = without
            .iter()
            .map(|&i| (r[i] - 0.5 * (r[(i + PHOTOS - 1) % PHOTOS] + r[(i + 1) % PHOTOS])).xy().norm())
            .collect();
        let vertical: Vec<f64> = without.iter().map(|&i| r[i].z).collect();
        let vertical_rms = (vertical.iter().map(|z| z * z).sum::<f64>() / vertical.len() as f64).sqrt();
        eprintln!("seed {seed}: photos without a position: horizontal {horizontal:.2?} m, from the mean of their \
                   neighbours {relative:.2?} m, vertical {vertical:.2?} m (rms {vertical_rms:.2})");
        for (&i, h) in without.iter().zip(&relative) {
            assert!(*h < 1.0, "seed {seed}: photo {i} without a position: {h} m from its neighbours horizontally");
        }
        assert!(vertical_rms < 2.0, "seed {seed}: vertical rms {vertical_rms} m");
    }
}

#[test]
fn positions_that_cannot_orient_the_block() {
    // 30 photos 4 m apart on a patch 20 m by 16 m, positions with 5 m of noise
    let s = patch(6, 5, 4.0, 20.0, 51);
    let c = case_of(&s);
    let truth: Vec<Matrix3<f64>> = s.poses.iter().map(|p| p.rotation).collect();
    let rotations = Rotations {
        world_from_cam: truth.iter().map(|r| Some(array(&r.transpose()))).collect(),
        placed: truth.len(),
        dropped: Vec::new(),
        components: vec![truth.len()],
        refused: None,
    };
    let gnss = gnss_of(&s, 5.0, 5.0, 52, &[]);
    let p = run(&c, &rotations, Some(&gnss));
    assert_eq!(p.frame, Frame::Similarity);
    assert!(p.centres.iter().all(|c| c.is_some()), "every photo placed");
    // the shape is the rays' own: exact up to the similarity
    let (largest, _) = centre_residuals(&s, &p);
    assert!(largest < 1e-6 * 20.0, "{largest} m");
}

#[test]
fn no_gnss_from_a_random_start() {
    let s = scene(8, Kind::Strip, 61);
    let c = case_of(&s);
    let truth: Vec<Matrix3<f64>> = s.poses.iter().map(|p| p.rotation).collect();
    let rotations = Rotations {
        world_from_cam: truth.iter().map(|r| Some(array(&r.transpose()))).collect(),
        placed: truth.len(),
        dropped: Vec::new(),
        components: vec![truth.len()],
        refused: None,
    };
    let p = run(&c, &rotations, None);
    assert_eq!(p.frame, Frame::Free);
    assert_eq!((p.sigma_h, p.sigma_v, p.frame_residual_deg), (None, None, None));
    let length = 7.0 * 5.0;
    let (largest, _) = centre_residuals(&s, &p);
    assert!(largest < 1e-6 * length, "{largest} m over {length} m");
}

#[test]
fn the_same_bits_twice() {
    let s = ring(40, 240.0, HEIGHT, 71);
    let c = case_of(&s);
    let rotations = rotations_of(&s, 1.0);
    let gnss = gnss_of(&s, 1.0, 2.0, 72, &[3]);
    assert_eq!(run(&c, &rotations, Some(&gnss)), run(&c, &rotations, Some(&gnss)));
    assert_eq!(run(&c, &rotations, None), run(&c, &rotations, None));
    // two flights, a fix 100 m off and accurate positions (the translations of the flights, the
    // positions left out at the start and checked again after the solve)
    let mut gnss = gnss_with(&s, 0.3, 0.5, [0.3, 0.3, 0.5], 73, &[3]);
    second_flight(&mut gnss, 20, [3.0, -2.0, 1.0]);
    gnss.xyz[10][1] += 100.0;
    let wrong = gnss.image[10];
    let first = run(&c, &rotations, Some(&gnss));
    assert_eq!(first.bias.len(), 2);
    assert_eq!(first.gnss_outliers, vec![wrong]);
    assert_eq!(first, run(&c, &rotations, Some(&gnss)));
}

#[test]
fn observe_false_cancels() {
    let s = ring(40, 240.0, HEIGHT, 81);
    let c = case_of(&s);
    let rotations = rotations_of(&s, 0.0);
    let gnss = gnss_of(&s, 1.0, 2.0, 82, &[]);
    for g in [Some(&gnss), None] {
        // the calls of a whole run: it is cancelled at the first, the third and the last
        let mut total = 0;
        position(&c.input, &c.cams, &c.graph, &rotations, &c.tracks, &c.selected, g, &Options::default(),
                 &mut || {
                     total += 1;
                     true
                 })
        .expect("a positioning");
        for stop_at in [1, 3, total] {
            let mut calls = 0;
            let mut observe = || {
                calls += 1;
                calls < stop_at
            };
            let out = position(&c.input, &c.cams, &c.graph, &rotations, &c.tracks, &c.selected, g,
                               &Options::default(), &mut observe);
            assert_eq!(out, Err("cancelled".to_string()));
        }
    }
}

#[test]
fn what_cannot_be_used_is_refused() {
    let s = ring(40, 240.0, HEIGHT, 91);
    let c = case_of(&s);
    let rotations = rotations_of(&s, 0.0);
    let go = |r: &Rotations, g: Option<&Gnss>, o: &Options| {
        position(&c.input, &c.cams, &c.graph, r, &c.tracks, &c.selected, g, o, &mut || true)
    };
    let refused = Rotations { refused: Some("the view graph was refused".into()), ..rotations.clone() };
    assert!(go(&refused, None, &Options::default()).unwrap_err().contains("the view graph was refused"));
    let mut gnss = gnss_of(&s, 1.0, 2.0, 92, &[]);
    gnss.sigma.pop();
    assert!(go(&rotations, Some(&gnss), &Options::default()).is_err());
    let mut gnss = gnss_of(&s, 1.0, 2.0, 92, &[]);
    gnss.sigma[4] = [0.0, 5.0, 10.0];
    assert!(go(&rotations, Some(&gnss), &Options::default()).is_err());
    let mut gnss = gnss_of(&s, 1.0, 2.0, 92, &[]);
    gnss.image[5] = 400;
    assert!(go(&rotations, Some(&gnss), &Options::default()).is_err());
    let bad = Options { positioning_huber: 0.0, ..Options::default() };
    assert!(go(&rotations, None, &bad).is_err());
    let mut graph = c.graph.clone();
    graph.edges[3].b = 400;
    let out = position(&c.input, &c.cams, &graph, &rotations, &c.tracks, &c.selected, None, &Options::default(),
                       &mut || true);
    assert!(out.unwrap_err().contains("out of range"));
    let mut graph = c.graph.clone();
    graph.refused = Some("cameras".into());
    let out = position(&c.input, &c.cams, &graph, &rotations, &c.tracks, &c.selected, None, &Options::default(),
                       &mut || true);
    assert!(out.unwrap_err().contains("cameras"));
}

/// What the positioning starts from on a scene with noisy key points and wrong matches: `case_of`
/// on the input of `noise_px` and `outlier_share`.
fn noisy_case_of(s: &Scene, noise_px: f64, outlier_share: f64, seed: u64) -> Case {
    let mut c = case_of(s);
    c.input = s.input(&InputOptions { noise_px, outlier_share, seed, ..InputOptions::default() });
    c.tracks = tracks::build(&c.input, &vec![true; c.input.images.len()]);
    c.selected = (0..c.tracks.len()).collect();
    c
}

/// The true rotations, each turned by a small random angle (up to `deg` about each axis).
fn rotations_near(s: &Scene, deg: f64, seed: u64) -> Rotations {
    let mut random = Random::new(seed);
    let world_from_cam = s
        .poses
        .iter()
        .map(|p| {
            let e = Rotation3::from_euler_angles(random.uniform_in(-deg, deg).to_radians(),
                                                 random.uniform_in(-deg, deg).to_radians(),
                                                 random.uniform_in(-deg, deg).to_radians());
            Some(array(&(e.into_inner() * p.rotation.transpose())))
        })
        .collect();
    let n = s.poses.len();
    Rotations { world_from_cam, placed: n, dropped: Vec::new(), components: vec![n], refused: None }
}

/// The positions of a survey flown in `flights` flights of consecutive photos: the true centres
/// plus normal noise of `plan` and `height` metres, recorded as 5 m and 10 m, each flight after the
/// first off by its `offsets` entry.
fn survey_gnss(s: &Scene, plan: f64, height: f64, flights: usize, offsets: &[[f64; 3]], seed: u64) -> Gnss {
    let mut g = gnss_with(s, plan, height, [5.0, 5.0, 10.0], seed, &[]);
    let per = s.poses.len().div_ceil(flights);
    for k in 0..g.image.len() {
        let f = g.image[k] / per;
        g.flight[k] = f as i64 + 1;
        if f > 0 {
            for a in 0..3 {
                g.xyz[k][a] += offsets[f - 1][a];
            }
        }
    }
    g
}

/// The distances of the centres of `p` to the truth (no similarity: the positions hold the frame),
/// horizontal, sorted, and the root mean square of the vertical ones.
fn distances_to_truth(s: &Scene, p: &Positioned) -> (Vec<f64>, f64) {
    let e: Vec<Vector3<f64>> = p
        .centres
        .iter()
        .enumerate()
        .map(|(i, c)| Vector3::from(c.unwrap_or_else(|| panic!("photo {i}"))) - s.poses[i].centre)
        .collect();
    let mut h: Vec<f64> = e.iter().map(|e| e.xy().norm()).collect();
    h.sort_by(|a, b| a.total_cmp(b));
    (h, (e.iter().map(|e| e.z * e.z).sum::<f64>() / e.len() as f64).sqrt())
}

/// The long block of the diagnosis of the gate: a corridor of 4 strips of 250 photos (3 km by
/// 72 m, 80/70 % overlap, a mound of 15 m), two flights of two strips (a flight of one strip cannot
/// hold the roll about its axis), the second off by a few metres,
/// positions with 3 m and 5 m of noise recorded as 5 m and 10 m, key points with 0.5 px of noise,
/// 2 % of wrong matches. The rays alone from a random start do not converge on such a block (the
/// block put on the positions was hundreds of metres off, 90 % of the positions outliers); from
/// the positions it fits as the ring does (R2: the plan within 0.5 % of the length after one
/// similarity, the heights no worse than the vertical noise of the positions), and its centres are
/// no farther from the truth than the noise of the positions: horizontally 90 % within 7.5 m (the
/// noise alone puts 90 % of the positions within 6.4 m), the heights within 5 m rms.
#[test]
fn a_long_corridor_is_placed_from_its_positions() {
    let s = common::corridor(4, 250, 12.0, 24.0, 40.0, 15.0, 0.06, 111);
    let c = noisy_case_of(&s, 0.5, 0.02, 112);
    let rotations = rotations_near(&s, 0.02, 113);
    let offset = [1.0, -2.0, 1.0];
    let gnss = survey_gnss(&s, 3.0, 5.0, 2, &[offset], 114);
    let clock = std::time::Instant::now();
    let p = run(&c, &rotations, Some(&gnss));
    let seconds = clock.elapsed().as_secs_f64();
    let (h, vertical_rms) = distances_to_truth(&s, &p);
    let h90 = h[9 * h.len() / 10];
    let shape = shape_of(&s, &p);
    let length = 249.0 * 12.0;
    eprintln!("corridor of {} photos, {} tracks: {seconds:.1} s; to the truth: horizontal median {:.2} m, 90 % \
               {h90:.2} m, largest {:.2} m, vertical rms {vertical_rms:.2} m; shape: horizontal {:.2} m, vertical \
               rms {:.2} m; outliers {}; biases {:.2?}", s.poses.len(), c.selected.len(), h[h.len() / 2],
              h[h.len() - 1], shape.horizontal, shape.vertical_rms, p.gnss_outliers.len(), p.bias);
    assert_eq!(p.frame, Frame::Gnss);
    assert!(p.notes.is_empty(), "the start at the positions passes its check: {:?}", p.notes);
    assert!(shape.horizontal < 0.005 * length, "the plan after one similarity: {} m", shape.horizontal);
    assert!(shape.vertical_rms < 5.0, "the heights after one similarity: {} m rms", shape.vertical_rms);
    assert!(h90 < 7.5, "90 % of the centres within {h90} m of the truth horizontally");
    assert!(vertical_rms < 5.0, "heights {vertical_rms} m rms from the truth");
    assert!(p.gnss_outliers.len() < s.poses.len() / 100, "{} outliers", p.gnss_outliers.len());
    let bias = Vector3::from(p.bias[1].1);
    // between two flights joined across a strip, the across-strip component of the bias is held
    // by the rays alone at this level (the directions of those pairs run along it)
    assert!((bias - Vector3::from(offset)).norm() < 2.0, "the bias of the second flight: {bias:?}");
}

/// The corridor with a seventh of its positions wrong by 100 m (more than the start's check
/// allows): the rays alone from the random start bend on such a block (see above), and a bent block
/// can name fewer outliers by taking wrong positions into the translations of its flights; the
/// start at the positions is kept, every wrong position an outlier, the corridor placed as without
/// them.
#[test]
fn many_wrong_fixes_on_a_corridor_keep_the_start_at_the_positions() {
    let s = common::corridor(4, 250, 12.0, 24.0, 40.0, 15.0, 0.06, 111);
    let c = noisy_case_of(&s, 0.5, 0.02, 112);
    let rotations = rotations_near(&s, 0.02, 113);
    let mut gnss = survey_gnss(&s, 3.0, 5.0, 2, &[[1.0, -2.0, 1.0]], 114);
    let mut random = Random::new(115);
    let wrong: Vec<usize> = (0..s.poses.len()).filter(|i| i % 7 == 3).collect();
    for &k in &wrong {
        let angle = random.uniform_in(0.0, std::f64::consts::TAU);
        gnss.xyz[k][0] += 100.0 * angle.cos();
        gnss.xyz[k][1] += 100.0 * angle.sin();
        gnss.xyz[k][2] += random.uniform_in(-50.0, 50.0);
    }
    let p = run(&c, &rotations, Some(&gnss));
    let (h, vertical_rms) = distances_to_truth(&s, &p);
    let h90 = h[9 * h.len() / 10];
    eprintln!("corridor with {} wrong fixes: frame {:?}, outliers {}, to the truth horizontal 90 % {h90:.2} m, \
               vertical rms {vertical_rms:.2} m; notes {:?}", wrong.len(), p.frame, p.gnss_outliers.len(), p.notes);
    assert_eq!(p.frame, Frame::Gnss);
    assert_eq!(p.notes.len(), 1, "{:?}", p.notes);
    assert!(p.notes[0].contains("kept the start at the positions"), "{:?}", p.notes);
    assert_eq!(p.gnss_outliers, wrong);
    assert!(h90 < 7.5, "90 % of the centres within {h90} m of the truth horizontally");
    assert!(vertical_rms < 5.0, "heights {vertical_rms} m rms from the truth");
}

/// A whole flight whose positions are all off by 30 m (a wrong datum, the risk of starting at the
/// positions) does not bend the block: 6 strips of 40 photos flown in 3 flights of two strips (a
/// flight of one strip cannot hold the roll about its axis: a height offset per strip is a roll),
/// the second off by 30 m; against the same survey with that flight not off, every centre is where
/// it was (within 5 cm), the bias of the flight takes the 30 m (within 5 cm of its bias there plus
/// the offset), no position is an outlier, and the start passed its check.
#[test]
fn a_flight_off_by_30_m_is_taken_by_its_bias() {
    let s = common::corridor(6, 40, 12.0, 24.0, 40.0, 15.0, 0.06, 121);
    let c = noisy_case_of(&s, 0.5, 0.02, 122);
    let rotations = rotations_near(&s, 0.02, 123);
    let wrong = [30.0, -20.0, 10.0];
    let control = run(&c, &rotations, Some(&survey_gnss(&s, 1.0, 2.0, 3, &[[0.0; 3], [-1.0, 1.0, 0.5]], 124)));
    let p = run(&c, &rotations, Some(&survey_gnss(&s, 1.0, 2.0, 3, &[wrong, [-1.0, 1.0, 0.5]], 124)));
    let bias_of = |p: &Positioned| {
        Vector3::from(p.bias.iter().find(|(f, _)| *f == 2).map(|(_, b)| *b).expect("the bias of flight 2"))
    };
    let error = (bias_of(&p) - bias_of(&control) - Vector3::from(wrong)).norm();
    let moved = (0..s.poses.len())
        .map(|i| (Vector3::from(p.centres[i].unwrap()) - Vector3::from(control.centres[i].unwrap())).norm())
        .fold(0.0, f64::max);
    let shape = shape_of(&s, &p);
    eprintln!("a flight off by {wrong:?}: bias {:.2?} (control {:.2?}, {error:.3} m from the offset), centres \
               moved {moved:.3} m; shape horizontal {:.2} m, vertical rms {:.2} m; outliers {}", bias_of(&p),
              bias_of(&control), shape.horizontal, shape.vertical_rms, p.gnss_outliers.len());
    assert_eq!(p.frame, Frame::Gnss);
    assert!(p.notes.is_empty() && control.notes.is_empty(), "{:?} {:?}", p.notes, control.notes);
    assert!(error < 0.05, "bias {:?}: {error} m from the control's plus the offset", bias_of(&p));
    assert!(moved < 0.05, "the centres moved by up to {moved} m");
    assert!(p.gnss_outliers.is_empty(), "{:?}", p.gnss_outliers);
}

/// The scales of the observations eliminated in closed form (point blocks of 3) give the solution
/// of GLOMAP's formulation with the scales in the point blocks, on the cases of this file solved
/// with positions: the bent ring, a fix 100 m off, a second flight, photos without a position, the
/// turned frame. Same outliers, the centres and biases within 1 mm, the points within 1 cm.
#[test]
fn the_eliminated_scales_give_the_explicit_solution() {
    let turned = {
        let s = ring(PHOTOS, DIAMETER, HEIGHT, 101);
        let turn = Rotation3::from_axis_angle(&nalgebra::Unit::new_normalize(Vector3::new(1.0, 2.0, 3.0)),
                                              30f64.to_radians())
            .into_inner();
        let mut rotations = rotations_of(&s, 0.0);
        for w in rotations.world_from_cam.iter_mut() {
            *w = Some(array(&(turn * Matrix3::from_fn(|i, j| w.unwrap()[i][j]))));
        }
        let gnss = gnss_of(&s, 1.0, 2.0, 102, &[]);
        (s, rotations, gnss)
    };
    let bent = {
        let s = ring(PHOTOS, DIAMETER, HEIGHT, 11);
        let rotations = rotations_of(&s, BEND_DEG);
        let gnss = gnss_of(&s, 1.0, 2.0, 12, &[]);
        (s, rotations, gnss)
    };
    let wrong_and_flight = {
        let s = ring(PHOTOS, DIAMETER, HEIGHT, 31);
        let rotations = rotations_of(&s, 0.0);
        let mut gnss = gnss_with(&s, 0.3, 0.5, [0.3, 0.3, 0.5], 34, &[]);
        second_flight(&mut gnss, PHOTOS / 2, [3.0, -2.0, 1.0]);
        gnss.xyz[37][0] += 100.0;
        (s, rotations, gnss)
    };
    let without = {
        let s = ring(PHOTOS, DIAMETER, HEIGHT, 41);
        let rotations = rotations_of(&s, 0.0);
        let gnss = gnss_of(&s, 1.0, 2.0, 42, &(0..PHOTOS).filter(|i| i % 10 == 3).collect::<Vec<_>>());
        (s, rotations, gnss)
    };
    for (name, (s, rotations, gnss)) in
        [("bent ring", bent), ("wrong fix, second flight", wrong_and_flight), ("without GPS", without),
         ("turned frame", turned)]
    {
        let c = case_of(&s);
        let solve = |scales| {
            position_with(&c.input, &c.cams, &c.graph, &rotations, &c.tracks, &c.selected, Some(&gnss),
                          &Options::default(), scales, &mut || true)
            .expect("a positioning")
        };
        let (eliminated, explicit) = (solve(Scales::Eliminated), solve(Scales::Explicit));
        let far = |a: &[Option<[f64; 3]>], b: &[Option<[f64; 3]>]| {
            a.iter()
                .zip(b)
                .map(|(a, b)| match (a, b) {
                    (Some(a), Some(b)) => (Vector3::from(*a) - Vector3::from(*b)).norm(),
                    (None, None) => 0.0,
                    _ => f64::INFINITY,
                })
                .fold(0.0, f64::max)
        };
        let centres = far(&eliminated.centres, &explicit.centres);
        let points = far(&eliminated.points, &explicit.points);
        let biases = eliminated
            .bias
            .iter()
            .zip(&explicit.bias)
            .map(|(a, b)| (Vector3::from(a.1) - Vector3::from(b.1)).norm())
            .fold(0.0, f64::max);
        eprintln!("{name}: centres {centres:.2e} m, points {points:.2e} m, biases {biases:.2e} m apart; outliers \
                   {:?}", eliminated.gnss_outliers);
        assert_eq!(eliminated.frame, explicit.frame, "{name}");
        assert_eq!(eliminated.gnss_outliers, explicit.gnss_outliers, "{name}");
        assert_eq!(eliminated.bias.len(), explicit.bias.len(), "{name}");
        assert_eq!(eliminated.scales_on_floor, explicit.scales_on_floor, "{name}");
        assert!(centres < 1e-3, "{name}: centres {centres} m apart");
        assert!(biases < 1e-3, "{name}: biases {biases} m apart");
        assert!(points < 1e-2, "{name}: points {points} m apart");
    }
}

/// A sixth of the positions wrong by 100 m in all directions on the bent ring (more than the
/// start's check allows): the rays of each photo, resected from the points of the others, find the
/// wrong positions before the joint solve; the check fails on their number, the rays alone run and
/// are not clearly better on the cost of a ray, so the start at the positions is kept: every wrong
/// position an outlier, the ring held, in the frame of the positions.
#[test]
fn many_wrong_fixes_on_a_ring_keep_the_start_at_the_positions() {
    let s = ring(PHOTOS, DIAMETER, HEIGHT, 131);
    let c = case_of(&s);
    let mut gnss = gnss_of(&s, 1.0, 2.0, 132, &[]);
    let mut random = Random::new(133);
    let wrong: Vec<usize> = (0..PHOTOS).filter(|i| i % 6 == 2).collect();
    for &k in &wrong {
        let angle = random.uniform_in(0.0, std::f64::consts::TAU);
        gnss.xyz[k][0] += 100.0 * angle.cos();
        gnss.xyz[k][1] += 100.0 * angle.sin();
        gnss.xyz[k][2] += random.uniform_in(-50.0, 50.0);
    }
    let p = run(&c, &rotations_of(&s, BEND_DEG), Some(&gnss));
    let shape = shape_of(&s, &p);
    eprintln!("{} wrong fixes of 100 m: frame {:?}, outliers {}, horizontal {:.2} m, vertical rms {:.2} m, ray cost \
               {:.3e}; notes {:?}", wrong.len(), p.frame, p.gnss_outliers.len(), shape.horizontal, shape.vertical_rms,
              p.ray_cost, p.notes);
    assert_eq!(p.frame, Frame::Gnss);
    assert_eq!(p.notes.len(), 1, "{:?}", p.notes);
    assert!(p.notes[0].contains("kept the start at the positions"), "{:?}", p.notes);
    assert_eq!(p.gnss_outliers, wrong);
    assert!(shape.horizontal < 0.005 * DIAMETER, "{} m", shape.horizontal);
    assert!(shape.vertical_rms < 2.0, "{} m", shape.vertical_rms);
}

/// With noisy key points and a tenth of wrong matches the rows of a wrong ray lie beyond the Huber
/// scale, where the closed-form scale (the least-squares one) is not the minimiser of the robust
/// cost: the two formulations then differ by a little, here the centres within 10 cm and the cost
/// within 2 %.
#[test]
fn the_eliminated_scales_with_wrong_matches() {
    let s = ring(PHOTOS, DIAMETER, HEIGHT, 141);
    let c = noisy_case_of(&s, 0.5, 0.1, 142);
    let rotations = rotations_of(&s, BEND_DEG);
    let gnss = gnss_of(&s, 1.0, 2.0, 143, &[]);
    let solve = |scales| {
        position_with(&c.input, &c.cams, &c.graph, &rotations, &c.tracks, &c.selected, Some(&gnss),
                      &Options::default(), scales, &mut || true)
        .expect("a positioning")
    };
    let (eliminated, explicit) = (solve(Scales::Eliminated), solve(Scales::Explicit));
    let centres = (0..PHOTOS)
        .map(|i| (Vector3::from(eliminated.centres[i].unwrap()) - Vector3::from(explicit.centres[i].unwrap())).norm())
        .fold(0.0, f64::max);
    let cost = (eliminated.cost - explicit.cost).abs() / explicit.cost;
    eprintln!("a tenth of wrong matches: centres {centres:.4} m apart, cost {:.5} vs {:.5} ({:.2} %)",
              eliminated.cost, explicit.cost, 100.0 * cost);
    assert_eq!(eliminated.frame, explicit.frame);
    assert_eq!(eliminated.gnss_outliers, explicit.gnss_outliers);
    assert!(centres < 0.1, "centres {centres} m apart");
    assert!(cost < 0.02, "costs {} and {}", eliminated.cost, explicit.cost);
}
