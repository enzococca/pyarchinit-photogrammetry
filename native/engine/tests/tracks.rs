//! The tracks of the native mapper (`mapper::tracks`) and their triangulation and filter
//! (`mapper::triangulate`): the matches of the verified pairs joined into tracks, the tracks with
//! two key points of one photo dropped, the selection of the longest tracks spread over every photo,
//! the point nearest a set of rays and the observations dropped for a large reprojection error or a
//! small triangulation angle, on inputs built here and on the synthetic scenes of `common`.

mod common;

use std::collections::HashMap;

use common::{scene, InputOptions, Kind, Random, FOCAL, HEIGHT, WIDTH};
use nalgebra::{Matrix3, Vector3};
use ppg_engine::camera::{Camera, Pose};
use ppg_engine::mapper::input::{CameraIn, ImageIn, Input, PairIn};
use ppg_engine::mapper::tracks::{self, Tracks};
use ppg_engine::mapper::triangulate;
use ppg_engine::mapper::Options;

/// An input of `n` photos with the key points `keypoints[i]` and the pairs `(a, b, matches)`.
fn input_of(keypoints: &[Vec<[f32; 2]>], pairs: &[(usize, usize, Vec<[u32; 2]>)]) -> Input {
    let mut all = Vec::new();
    let mut images = Vec::new();
    for (i, kps) in keypoints.iter().enumerate() {
        let start = all.len();
        all.extend_from_slice(kps);
        images.push(ImageIn { name: format!("photo_{i:03}.jpg"), image_id: i as u32 + 1, camera: 0,
                              keypoints: start..all.len() });
    }
    let cameras = vec![CameraIn {
        camera_id: 1,
        model: "SIMPLE_PINHOLE".to_string(),
        params: vec![FOCAL, WIDTH / 2.0, HEIGHT / 2.0],
        width: WIDTH,
        height: HEIGHT,
        prior_focal: true,
    }];
    let mut matches = Vec::new();
    let mut out = Vec::new();
    for (a, b, m) in pairs {
        let start = matches.len();
        matches.extend_from_slice(m);
        out.push(PairIn { a: *a, b: *b, matches: start..matches.len(), config: 2, f: None });
    }
    let input = Input { images, cameras, keypoints: all, pairs: out, matches };
    input.check().expect("a valid input");
    input
}

fn track(t: &Tracks, i: usize) -> &[(u32, u32)] {
    &t.observations[t.offsets[i]..t.offsets[i + 1]]
}

fn count(t: &Tracks) -> usize {
    t.offsets.len() - 1
}

/// Ten key points per photo along a diagonal.
fn ten_keypoints(n: usize) -> Vec<Vec<[f32; 2]>> {
    (0..n).map(|_| (0..10).map(|k| [100.0 + 150.0 * k as f32, 80.0 + 120.0 * k as f32]).collect()).collect()
}

#[test]
fn tracks_join_the_matches() {
    let keypoints = ten_keypoints(5);
    let mut pairs = Vec::new();
    for a in 0..5 {
        for b in a + 1..5 {
            let mut m = vec![[7, 7]];
            if (a, b) == (0, 1) {
                m.push([3, 4]);
            }
            pairs.push((a, b, m));
        }
    }
    let all = vec![true; 5];
    let t = tracks::build(&input_of(&keypoints, &pairs), &all);
    assert_eq!(t.refused, None);
    assert_eq!(t.dropped_conflicts, 0);
    assert_eq!(count(&t), 2, "{t:?}");
    assert_eq!(t.offsets.len(), count(&t) + 1);
    assert_eq!(t.offsets[count(&t)], t.observations.len());
    let five: Vec<_> = (0..count(&t)).filter(|&i| track(&t, i).len() == 5).collect();
    assert_eq!(five.len(), 1);
    assert_eq!(track(&t, five[0]), &[(0, 7), (1, 7), (2, 7), (3, 7), (4, 7)]);
    let two: Vec<_> = (0..count(&t)).filter(|&i| track(&t, i).len() == 2).collect();
    assert_eq!(track(&t, two[0]), &[(0, 3), (1, 4)]);

    // one pair missing: still one track of five
    let fewer: Vec<_> = pairs.iter().filter(|p| (p.0, p.1) != (1, 3)).cloned().collect();
    let t = tracks::build(&input_of(&keypoints, &fewer), &all);
    assert_eq!(count(&t), 2);
    assert!((0..count(&t)).any(|i| track(&t, i) == [(0, 7), (1, 7), (2, 7), (3, 7), (4, 7)]));

    // a photo that is not registered gives none of its matches
    let mut four = all.clone();
    four[2] = false;
    let t = tracks::build(&input_of(&keypoints, &pairs), &four);
    assert!((0..count(&t)).any(|i| track(&t, i) == [(0, 7), (1, 7), (3, 7), (4, 7)]), "{t:?}");
    assert!(t.observations.iter().all(|&(image, _)| image != 2));

    // key point 7 of photo 0 joined to key points 7 and 2 of photo 1 through photo 2: dropped whole
    let chain = vec![(0, 1, vec![[7, 7], [3, 4]]), (1, 2, vec![[2, 5]]), (0, 2, vec![[7, 5]])];
    let t = tracks::build(&input_of(&ten_keypoints(3), &chain), &[true; 3]);
    assert_eq!(t.dropped_conflicts, 1, "{t:?}");
    assert_eq!(count(&t), 1);
    assert_eq!(track(&t, 0), &[(0, 3), (1, 4)]);
}

#[test]
fn tracks_follow_the_points_of_a_scene() {
    let s = scene(6, Kind::Strip, 3);
    let input = s.input(&InputOptions::default());
    let t = tracks::build(&input, &[true; 6]);
    assert_eq!(t.refused, None);
    assert_eq!(t.dropped_conflicts, 0);
    assert!(count(&t) > 500, "{} tracks", count(&t));
    for i in 0..count(&t) {
        let obs = track(&t, i);
        assert!(obs.len() >= 2);
        let point = s.views[obs[0].0 as usize][obs[0].1 as usize].0;
        for &(image, k) in obs {
            assert_eq!(s.views[image as usize][k as usize].0, point, "track {i}: {obs:?}");
        }
        assert!(obs.windows(2).all(|w| w[0].0 < w[1].0), "track {i}: {obs:?}");
    }
    // the same bits on a second run
    assert_eq!(tracks::build(&input, &[true; 6]), t);
}

#[test]
fn tracks_refuse_what_they_cannot_use() {
    let keypoints = ten_keypoints(3);
    let input = input_of(&keypoints, &[(0, 1, vec![[1, 1]]), (1, 2, vec![[1, 1]])]);
    let t = tracks::build(&input, &[true; 2]);
    assert!(t.refused.as_deref().unwrap_or("").contains("registered"), "{t:?}");
    assert!(t.observations.is_empty());
    assert_eq!(t.offsets, vec![0]);

    let good = tracks::build(&input, &[true; 3]);
    assert!(tracks::select(&good, &input, 10).is_ok());
    let mut wrong = good.clone();
    wrong.observations[0] = (7, 0);
    assert!(tracks::select(&wrong, &input, 10).is_err());
    let mut wrong = good.clone();
    wrong.observations[0] = (0, 99);
    assert!(tracks::select(&wrong, &input, 10).is_err());
    let mut wrong = good.clone();
    wrong.offsets = vec![0, 5];
    assert!(tracks::select(&wrong, &input, 10).is_err());
    assert!(tracks::select(&t, &input, 10).is_err());
}

#[test]
fn selection_keeps_long_tracks_spread_over_the_photo() {
    let n = 6;
    let mut random = Random::new(11);
    let mut keypoints: Vec<Vec<[f32; 2]>> = vec![Vec::new(); n];
    let mut by_pair: HashMap<(usize, usize), Vec<[u32; 2]>> = HashMap::new();
    for _ in 0..3000 {
        let length = 2 + random.below(5);
        let mut photos: Vec<usize> = (0..n).collect();
        for i in 0..length {
            let j = i + random.below(n - i);
            photos.swap(i, j);
        }
        let mut photos = photos[..length].to_vec();
        photos.sort();
        let kps: Vec<u32> = photos
            .iter()
            .map(|&p| {
                let x = random.uniform_in(0.0, WIDTH) as f32;
                let y = random.uniform_in(0.0, HEIGHT) as f32;
                keypoints[p].push([x, y]);
                keypoints[p].len() as u32 - 1
            })
            .collect();
        for w in 0..length - 1 {
            by_pair.entry((photos[w], photos[w + 1])).or_default().push([kps[w], kps[w + 1]]);
        }
    }
    let mut pairs: Vec<_> = by_pair.into_iter().map(|((a, b), m)| (a, b, m)).collect();
    pairs.sort_by_key(|p| (p.0, p.1));
    let input = input_of(&keypoints, &pairs);
    for kps in &keypoints {
        assert!(kps.len() > 1800, "{} key points", kps.len());
    }
    let t = tracks::build(&input, &vec![true; n]);
    assert_eq!(count(&t), 3000);
    let selected = tracks::select(&t, &input, 200).expect("a selection");
    assert!(selected.windows(2).all(|w| w[0] < w[1]));
    assert!(selected.len() < count(&t));
    let mut chosen = vec![false; count(&t)];
    for &s in &selected {
        chosen[s] = true;
    }
    let mut per_photo = vec![0usize; n];
    let mut cells = vec![[false; 16]; n];
    for &s in &selected {
        for &(image, k) in track(&t, s) {
            per_photo[image as usize] += 1;
            let p = input.keypoint(image as usize, k);
            let cx = ((p[0] / WIDTH * 4.0) as usize).min(3);
            let cy = ((p[1] / HEIGHT * 4.0) as usize).min(3);
            cells[image as usize][4 * cy + cx] = true;
        }
    }
    for i in 0..n {
        assert!(per_photo[i] >= 200, "photo {i}: {} tracks", per_photo[i]);
        assert!(cells[i].iter().all(|&c| c), "photo {i}: {:?}", cells[i]);
    }
    let mean = |pick: bool| {
        let lengths: Vec<usize> = (0..count(&t)).filter(|&i| chosen[i] == pick).map(|i| track(&t, i).len()).collect();
        lengths.iter().sum::<usize>() as f64 / lengths.len() as f64
    };
    assert!(mean(true) >= mean(false), "selected {} others {}", mean(true), mean(false));
    assert_eq!(tracks::select(&t, &input, 200).unwrap(), selected);
    assert!(tracks::select(&t, &input, 0).unwrap().is_empty());
}

#[test]
fn triangulation_is_exact_on_exact_rays() {
    let x = [3.25, -1.5, 0.75];
    let origins = [[0.0, 0.0, 20.0], [5.0, 0.3, 20.4], [10.0, -0.7, 19.6], [2.0, 8.0, 21.0]];
    let rays: Vec<([f64; 3], [f64; 3])> = origins
        .iter()
        .enumerate()
        .map(|(i, o)| {
            let s = 0.5 + i as f64; // directions of any length
            (*o, [s * (x[0] - o[0]), s * (x[1] - o[1]), s * (x[2] - o[2])])
        })
        .collect();
    let p = triangulate::point(&rays).expect("a point");
    for k in 0..3 {
        assert!((p[k] - x[k]).abs() < 1e-9, "{p:?}");
    }
    let p = triangulate::point(&rays[..2]).expect("a point of two rays");
    assert!((0..3).all(|k| (p[k] - x[k]).abs() < 1e-9), "{p:?}");
}

#[test]
fn parallel_rays_give_none() {
    let d = [0.1, -0.2, -1.0];
    assert_eq!(triangulate::point(&[([0.0, 0.0, 20.0], d), ([5.0, 0.0, 20.0], d)]), None);
    let opposite = [-0.1, 0.2, 1.0];
    assert_eq!(triangulate::point(&[([0.0, 0.0, 20.0], d), ([5.0, 0.0, 20.0], opposite)]), None);
    assert_eq!(triangulate::point(&[([0.0, 0.0, 20.0], d)]), None);
    assert_eq!(triangulate::point(&[]), None);
    assert_eq!(triangulate::point(&[([0.0, 0.0, 20.0], d), ([5.0, 0.0, 20.0], [0.0; 3])]), None);
}

/// The `cam_from_world` rows of a camera at `centre` looking straight down.
fn looking_down(centre: [f64; 3]) -> Pose {
    let r = Matrix3::new(1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, -1.0);
    let t = -(r * Vector3::from(centre));
    std::array::from_fn(|i| [r[(i, 0)], r[(i, 1)], r[(i, 2)], t[i]])
}

/// The photos at `centres` seeing the point `x` (one key point each, its exact pixel), one track of
/// them, the poses and the cameras.
fn seen_from(centres: &[[f64; 3]], x: [f64; 3]) -> (Input, Tracks, Vec<Option<Pose>>, Vec<Camera>) {
    let poses: Vec<Option<Pose>> = centres.iter().map(|&c| Some(looking_down(c))).collect();
    let keypoints: Vec<Vec<[f32; 2]>> = poses
        .iter()
        .map(|p| {
            let (u, v, w) = ppg_engine::camera::transform(p.as_ref().unwrap(), x[0], x[1], x[2]);
            vec![[(FOCAL * u / w + WIDTH / 2.0) as f32, (FOCAL * v / w + HEIGHT / 2.0) as f32]]
        })
        .collect();
    let pairs: Vec<_> = (1..centres.len()).map(|b| (0, b, vec![[0, 0]])).collect();
    let input = input_of(&keypoints, &pairs);
    let t = tracks::build(&input, &vec![true; centres.len()]);
    let cams = vec![Camera::new("SIMPLE_PINHOLE", &input.cameras[0].params, WIDTH, HEIGHT).unwrap()];
    (input, t, poses, cams)
}

#[test]
fn the_filter_drops_far_and_flat_observations() {
    let o = Options::default();
    let x = [1.0, 2.0, 0.5];
    let (mut input, t, poses, cams) = seen_from(&[[0.0, 0.0, 20.0], [5.0, 0.0, 20.0], [10.0, 0.5, 20.0]], x);
    assert_eq!(count(&t), 1);
    let kept = triangulate::filter(&t, &[Some(x)], &input, &cams, &poses, &o).expect("a filter");
    assert_eq!(kept.keep, vec![true; 3]);
    assert_eq!((kept.by_reprojection, kept.by_angle), (0, 0));

    // 12 pixels off (0.012 of the focal length of 1000 pixels): goes; 8 pixels off: stays
    let k1 = input.images[1].keypoints.start;
    input.keypoints[k1][0] += 12.0;
    let k2 = input.images[2].keypoints.start;
    input.keypoints[k2][1] -= 8.0;
    let kept = triangulate::filter(&t, &[Some(x)], &input, &cams, &poses, &o).unwrap();
    assert_eq!(kept.keep, vec![true, false, true]);
    assert_eq!((kept.by_reprojection, kept.by_angle), (1, 0));

    // two of three far off: the one left is no point
    input.keypoints[k2][1] -= 8.0;
    let kept = triangulate::filter(&t, &[Some(x)], &input, &cams, &poses, &o).unwrap();
    assert_eq!(kept.keep, vec![false; 3]);
    assert_eq!(kept.by_reprojection, 3);

    // rays meeting at 0.5 degree at most: every observation goes; at 2 degrees they stay
    let baseline = |deg: f64| 2.0 * 19.5 * (deg.to_radians() / 2.0).tan();
    let b = baseline(0.5);
    let (input, t, poses, cams) = seen_from(&[[0.5, 2.0, 20.0], [0.5 + b / 2.0, 2.0, 20.0],
                                              [0.5 + b, 2.0, 20.0]], [0.5 + b / 2.0, 2.0, 0.5]);
    let kept = triangulate::filter(&t, &[Some([0.5 + b / 2.0, 2.0, 0.5])], &input, &cams, &poses, &o).unwrap();
    assert_eq!(kept.keep, vec![false; 3]);
    assert_eq!((kept.by_reprojection, kept.by_angle), (0, 3));
    let b = baseline(2.0);
    let (input, t, poses, cams) = seen_from(&[[0.5, 2.0, 20.0], [0.5 + b, 2.0, 20.0]], [0.5 + b / 2.0, 2.0, 0.5]);
    let kept = triangulate::filter(&t, &[Some([0.5 + b / 2.0, 2.0, 0.5])], &input, &cams, &poses, &o).unwrap();
    assert_eq!(kept.keep, vec![true; 2]);

    // a track without a point, a photo without a pose
    let (input, t, mut poses, cams) = seen_from(&[[0.0, 0.0, 20.0], [5.0, 0.0, 20.0], [10.0, 0.5, 20.0]], x);
    let kept = triangulate::filter(&t, &[None], &input, &cams, &poses, &o).unwrap();
    assert_eq!((kept.keep.clone(), kept.no_point), (vec![false; 3], 3));
    poses[0] = None;
    let kept = triangulate::filter(&t, &[Some(x)], &input, &cams, &poses, &o).unwrap();
    assert_eq!(kept.keep, vec![false, true, true]);

    // what it cannot use is refused
    assert!(triangulate::filter(&t, &[], &input, &cams, &poses, &o).is_err());
    assert!(triangulate::filter(&t, &[Some(x)], &input, &cams, &poses[..2], &o).is_err());
    assert!(triangulate::filter(&t, &[Some(x)], &input, &[], &poses, &o).is_err());
    let mut wrong = t.clone();
    wrong.observations[2] = (2, 4);
    assert!(triangulate::filter(&wrong, &[Some(x)], &input, &cams, &poses, &o).is_err());
}

/// The filter of the last adjustment (COLMAP's retriangulation): 4 pixels and 1.5 degrees beside
/// the normalised threshold. An observation 5 pixels off, which the normalised threshold keeps at a
/// focal length of 1000 pixels, goes; 3 pixels off stays; rays meeting at 1.2 degrees go.
#[test]
fn the_last_filter_drops_what_is_beyond_four_pixels_or_under_one_and_a_half_degrees() {
    let o = Options::default();
    let x = [1.0, 2.0, 0.5];
    let (mut input, t, poses, cams) = seen_from(&[[0.0, 0.0, 20.0], [5.0, 0.0, 20.0], [10.0, 0.5, 20.0]], x);
    let k1 = input.images[1].keypoints.start;
    input.keypoints[k1][0] += 5.0;
    let k2 = input.images[2].keypoints.start;
    input.keypoints[k2][1] -= 3.0;
    let kept = triangulate::filter(&t, &[Some(x)], &input, &cams, &poses, &o).unwrap();
    assert_eq!(kept.keep, vec![true; 3]);
    let kept = triangulate::filter_in_pixels(&t, &[Some(x)], &input, &cams, &poses, &o).unwrap();
    assert_eq!(kept.keep, vec![true, false, true]);
    assert_eq!((kept.by_reprojection, kept.by_angle), (1, 0));
    // the threshold is the option's
    let loose = Options { final_max_reprojection_px: 6.0, ..o.clone() };
    let kept = triangulate::filter_in_pixels(&t, &[Some(x)], &input, &cams, &poses, &loose).unwrap();
    assert_eq!(kept.keep, vec![true; 3]);

    let baseline = |deg: f64| 2.0 * 19.5 * (deg.to_radians() / 2.0).tan();
    let b = baseline(1.2);
    let at = [0.5 + b / 2.0, 2.0, 0.5];
    let (input, t, poses, cams) = seen_from(&[[0.5, 2.0, 20.0], [0.5 + b, 2.0, 20.0]], at);
    let kept = triangulate::filter(&t, &[Some(at)], &input, &cams, &poses, &o).unwrap();
    assert_eq!(kept.keep, vec![true; 2]);
    let kept = triangulate::filter_in_pixels(&t, &[Some(at)], &input, &cams, &poses, &o).unwrap();
    assert_eq!((kept.keep, kept.by_angle), (vec![false; 2], 2));
}

#[test]
fn the_filter_keeps_the_points_of_a_scene() {
    let s = scene(5, Kind::Strip, 5);
    let input = s.input(&InputOptions { noise_px: 0.5, ..InputOptions::default() });
    let t = tracks::build(&input, &[true; 5]);
    let poses: Vec<Option<Pose>> = s
        .poses
        .iter()
        .map(|p| {
            let t = -(p.rotation * p.centre);
            Some(std::array::from_fn(|i| [p.rotation[(i, 0)], p.rotation[(i, 1)], p.rotation[(i, 2)], t[i]]))
        })
        .collect();
    let cams = vec![Camera::new("SIMPLE_PINHOLE", &input.cameras[0].params, WIDTH, HEIGHT).unwrap()];
    let points: Vec<Option<[f64; 3]>> = (0..count(&t))
        .map(|i| {
            let rays: Vec<([f64; 3], [f64; 3])> = track(&t, i)
                .iter()
                .map(|&(image, k)| {
                    let p = s.poses[image as usize].clone();
                    let px = input.keypoint(image as usize, k);
                    let (u, v) = cams[0].normalised(px[0], px[1]);
                    let d = p.rotation.transpose() * Vector3::new(u, v, 1.0);
                    (p.centre.into(), d.into())
                })
                .collect();
            triangulate::point(&rays)
        })
        .collect();
    let mut worst: f64 = 0.0;
    for (i, p) in points.iter().enumerate() {
        let p = p.expect("a point");
        let truth = s.points[s.views[track(&t, i)[0].0 as usize][track(&t, i)[0].1 as usize].0];
        worst = worst.max((Vector3::from(p) - truth).norm());
    }
    assert!(worst < 0.5, "worst point {worst} m off");
    let kept = triangulate::filter(&t, &points, &input, &cams, &poses, &Options::default()).unwrap();
    assert_eq!(kept.by_reprojection, 0);
    let share = kept.keep.iter().filter(|&&k| k).count() as f64 / kept.keep.len() as f64;
    assert!(share > 0.9, "kept {share}");
}

#[test]
fn selection_spreads_before_it_takes_the_longest() {
    // photo 0: sixteen tracks of three photos in its first cell, sixteen of two photos one in each
    // cell; photos 1 and 2 see all of theirs in their first cell
    let corner = |k: usize| [10.0 + k as f32, 10.0 + k as f32];
    let mut keypoints: Vec<Vec<[f32; 2]>> = vec![Vec::new(); 3];
    let (mut p01, mut p12) = (Vec::new(), Vec::new());
    for k in 0..16u32 {
        keypoints[0].push(corner(k as usize));
        keypoints[1].push(corner(k as usize));
        keypoints[2].push(corner(k as usize));
        p01.push([k, k]);
        p12.push([k, k]);
    }
    for c in 0..16 {
        let (x, y) = ((c % 4) as f64 + 0.5, (c / 4) as f64 + 0.5);
        keypoints[0].push([(x * WIDTH / 4.0) as f32, (y * HEIGHT / 4.0) as f32]);
        keypoints[1].push(corner(16 + c));
        p01.push([16 + c as u32, 16 + c as u32]);
    }
    let input = input_of(&keypoints, &[(0, 1, p01), (1, 2, p12)]);
    let t = tracks::build(&input, &[true; 3]);
    assert_eq!(count(&t), 32);
    let selected = tracks::select(&t, &input, 16).unwrap();
    let mut cells = [false; 16];
    for &s in &selected {
        let (image, k) = track(&t, s)[0];
        assert_eq!(image, 0);
        let p = input.keypoint(0, k);
        cells[4 * ((p[1] / HEIGHT * 4.0) as usize).min(3) + ((p[0] / WIDTH * 4.0) as usize).min(3)] = true;
    }
    assert!(cells.iter().all(|&c| c), "{cells:?}");
    // the first cell of photo 0 gives its longest track, the others their one
    assert!(selected.contains(&0));
    assert_eq!(selected.len(), 31, "{selected:?}");
}
