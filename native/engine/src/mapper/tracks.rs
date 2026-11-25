//! The tracks of the mapper (step 3 of the design): the inlier matches of the verified pairs
//! between registered photos joined by union-find into tracks, a track holding two key points of
//! one photo dropped whole, and the tie-point limit (`select`): per photo its longest tracks
//! spread over a grid of 4 x 4 cells.
//!
//! The matches are those of `Input` (the inliers of the verified pairs, as COLMAP's database holds
//! them), whether or not the view graph found parallax in their pair: a pair taken from one place
//! still sees the same points, and the filter of the triangulation removes the flat ones.
//!
//! Everything runs in a fixed order (the union keeps the smaller node as the root, the tracks are
//! numbered by their smallest node, every sort breaks ties by index), so that two runs give the
//! same bits.

use std::sync::atomic::{AtomicBool, Ordering};

use super::input::Input;

/// The tracks: track `t` is `observations[offsets[t]..offsets[t + 1]]`, each observation
/// `(photo, key point)` (the index of the photo in `Input::images`, the index of the key point among
/// those of the photo), in the order of the photos. Tracks are numbered in the order of their first
/// observation (photo, then key point). `dropped_conflicts` counts the tracks dropped because they
/// held two key points of one photo; `refused` gives the reason when nothing was built because
/// the input could not be used (the tracks then empty).
#[derive(Clone, Debug, PartialEq)]
pub struct Tracks {
    pub observations: Vec<(u32, u32)>,
    pub offsets: Vec<usize>,
    pub dropped_conflicts: usize,
    pub refused: Option<String>,
}

impl Tracks {
    fn refused(reason: String) -> Tracks {
        Tracks { observations: Vec::new(), offsets: vec![0], dropped_conflicts: 0, refused: Some(reason) }
    }

    /// The number of tracks.
    pub fn len(&self) -> usize {
        self.offsets.len().saturating_sub(1)
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// The observations of track `t`.
    pub fn track(&self, t: usize) -> &[(u32, u32)] {
        &self.observations[self.offsets[t]..self.offsets[t + 1]]
    }

    /// `Err` naming the first thing `select` and `triangulate::filter` could not use: tracks that
    /// were refused, offsets that do not start at 0, go back or end elsewhere than at the end of the
    /// observations, an observation whose photo or key point is out of the range of `input`.
    pub fn check(&self, input: &Input) -> Result<(), String> {
        if let Some(reason) = &self.refused {
            return Err(format!("the tracks were refused: {reason}"));
        }
        if self.offsets.first() != Some(&0) || self.offsets.last() != Some(&self.observations.len()) {
            return Err(format!("track offsets {:?}..{:?} do not span the {} observations", self.offsets.first(),
                               self.offsets.last(), self.observations.len()));
        }
        if let Some(t) = self.offsets.windows(2).position(|w| w[0] > w[1]) {
            return Err(format!("track {t}: offsets {} > {}", self.offsets[t], self.offsets[t + 1]));
        }
        for (i, &(image, k)) in self.observations.iter().enumerate() {
            let Some(im) = input.images.get(image as usize) else {
                return Err(format!("observation {i}: image {image} out of range ({} images)", input.images.len()));
            };
            if k as usize >= im.keypoints.len() {
                return Err(format!("observation {i}: key point {k} beyond the {} of image {image}",
                                   im.keypoints.len()));
            }
        }
        Ok(())
    }
}

/// The root of `x`, halving the path on the way.
fn find(parent: &mut [u32], mut x: u32) -> u32 {
    while parent[x as usize] != x {
        let up = parent[parent[x as usize] as usize];
        parent[x as usize] = up;
        x = up;
    }
    x
}

/// The tracks of the inlier matches of the pairs whose two photos are `registered` (one flag per
/// photo of `input`). A track that holds two key points of one photo is dropped whole and counted;
/// a track of one observation cannot arise (every match joins two photos).
///
/// `registered` of another length than the photos, or an input whose pairs, matches or key points
/// are out of range (see `Input::check`), give empty tracks with the reason in `refused`.
pub fn build(input: &Input, registered: &[bool]) -> Tracks {
    build_until(input, registered, &AtomicBool::new(false))
}

/// `build`, ended early when `stop` is raised (by another thread, checked at every pair): the
/// tracks are then refused with the reason `"cancelled"`.
pub fn build_until(input: &Input, registered: &[bool], stop: &AtomicBool) -> Tracks {
    let n = input.images.len();
    if registered.len() != n {
        return Tracks::refused(format!("{} registered flags for {n} images", registered.len()));
    }
    // a node per key point of every photo, the photos one after the other
    let mut base = Vec::with_capacity(n + 1);
    let mut total = 0usize;
    for image in &input.images {
        base.push(total);
        total += image.keypoints.len();
    }
    base.push(total);
    if total >= u32::MAX as usize {
        return Tracks::refused(format!("{total} key points: {} at most are supported", u32::MAX - 1));
    }
    let mut parent: Vec<u32> = (0..total as u32).collect();
    let mut used = vec![false; total];
    for (p, pair) in input.pairs.iter().enumerate() {
        if stop.load(Ordering::Relaxed) {
            return Tracks::refused("cancelled".into());
        }
        if pair.a >= n || pair.b >= n || pair.a == pair.b {
            return Tracks::refused(format!("pair {p}: images {} and {} ({n} images)", pair.a, pair.b));
        }
        if !(registered[pair.a] && registered[pair.b]) {
            continue;
        }
        let Some(matches) = input.matches.get(pair.matches.clone()) else {
            return Tracks::refused(format!("pair {p}: matches {:?} out of range ({} matches)", pair.matches,
                                           input.matches.len()));
        };
        let (na, nb) = (input.images[pair.a].keypoints.len(), input.images[pair.b].keypoints.len());
        for &[ka, kb] in matches {
            if ka as usize >= na || kb as usize >= nb {
                return Tracks::refused(format!("pair {p}: match [{ka}, {kb}] beyond the {na} and {nb} key points \
                                                of images {} and {}", pair.a, pair.b));
            }
            let (x, y) = ((base[pair.a] + ka as usize) as u32, (base[pair.b] + kb as usize) as u32);
            used[x as usize] = true;
            used[y as usize] = true;
            let (rx, ry) = (find(&mut parent, x), find(&mut parent, y));
            if rx != ry {
                // the smaller node is the root: the root of a set is its smallest node
                let (low, high) = (rx.min(ry), rx.max(ry));
                parent[high as usize] = low;
            }
        }
    }
    // number the sets by their root, met first when the nodes are walked in order
    const NONE: u32 = u32::MAX;
    let mut track_of = vec![NONE; total];
    let mut sizes: Vec<usize> = Vec::new();
    let mut roots = Vec::new();
    for x in 0..total {
        if !used[x] {
            continue;
        }
        let r = find(&mut parent, x as u32) as usize;
        if track_of[r] == NONE {
            track_of[r] = sizes.len() as u32;
            sizes.push(0);
        }
        roots.push(r as u32);
        sizes[track_of[r] as usize] += 1;
    }
    drop(parent);
    let mut start = Vec::with_capacity(sizes.len() + 1);
    let mut sum = 0;
    for &s in &sizes {
        start.push(sum);
        sum += s;
    }
    start.push(sum);
    // the observations of every set, in the order of the nodes (photo, then key point)
    let mut grouped = vec![(0u32, 0u32); sum];
    let mut fill = start.clone();
    let mut image = 0;
    let mut used_index = 0;
    for x in 0..total {
        if !used[x] {
            continue;
        }
        while x >= base[image + 1] {
            image += 1;
        }
        let t = track_of[roots[used_index] as usize] as usize;
        used_index += 1;
        grouped[fill[t]] = (image as u32, (x - base[image]) as u32);
        fill[t] += 1;
    }
    let mut observations = Vec::with_capacity(sum);
    let mut offsets = vec![0];
    let mut dropped_conflicts = 0;
    for t in 0..sizes.len() {
        let obs = &grouped[start[t]..start[t + 1]];
        if obs.windows(2).any(|w| w[0].0 == w[1].0) {
            dropped_conflicts += 1;
            continue;
        }
        observations.extend_from_slice(obs);
        offsets.push(observations.len());
    }
    Tracks { observations, offsets, dropped_conflicts, refused: None }
}

/// The cells of the grid of `select` across and down a photo.
pub const GRID: usize = 4;

/// The tie-point limit: per photo, its tracks ordered by length (the longest first, ties by the
/// index of the track) and dealt into the cells of a `GRID` x `GRID` grid of the photo by the pixel
/// of their key point; the photo takes one track of each cell in turn (round robin over the cells,
/// each cell in the order of length) until it has `per_photo` of them or none is left. A track is
/// kept when one of its photos takes it. The indices of the kept tracks are returned in increasing
/// order.
///
/// `Err` when the tracks do not fit `input` (`Tracks::check`).
pub fn select(t: &Tracks, input: &Input, per_photo: usize) -> Result<Vec<usize>, String> {
    t.check(input)?;
    let n = input.images.len();
    let count = t.len();
    // the tracks of every photo with their cell, counted then filled (no vector per photo)
    let mut start = vec![0usize; n + 1];
    for &(image, _) in &t.observations {
        start[image as usize + 1] += 1;
    }
    for i in 0..n {
        start[i + 1] += start[i];
    }
    let mut fill = start.clone();
    let mut entries = vec![(0usize, 0u8); t.observations.len()];
    for track in 0..count {
        for &(image, k) in t.track(track) {
            let image = image as usize;
            let camera = input.cameras.get(input.images[image].camera).ok_or_else(|| {
                format!("image {image}: camera {} out of range", input.images[image].camera)
            })?;
            let p = input.keypoint(image, k);
            let cell = |v: f64, size: f64| {
                let c = (v / size * GRID as f64).floor();
                if c.is_finite() { c.clamp(0.0, (GRID - 1) as f64) as usize } else { 0 }
            };
            let c = cell(p[1], camera.height) * GRID + cell(p[0], camera.width);
            entries[fill[image]] = (track, c as u8);
            fill[image] += 1;
        }
    }
    let length = |track: usize| t.offsets[track + 1] - t.offsets[track];
    let mut kept = vec![false; count];
    let mut cells: Vec<Vec<usize>> = vec![Vec::new(); GRID * GRID];
    for i in 0..n {
        let mine = &mut entries[start[i]..start[i + 1]];
        mine.sort_unstable_by(|a, b| length(b.0).cmp(&length(a.0)).then(a.0.cmp(&b.0)));
        for c in cells.iter_mut() {
            c.clear();
        }
        for &(track, c) in mine.iter() {
            cells[c as usize].push(track);
        }
        let mut taken = 0;
        let mut round = 0;
        while taken < per_photo {
            let mut any = false;
            for c in &cells {
                if let Some(&track) = c.get(round) {
                    any = true;
                    if taken < per_photo {
                        kept[track] = true;
                        taken += 1;
                    }
                }
            }
            if !any {
                break;
            }
            round += 1;
        }
    }
    Ok((0..count).filter(|&track| kept[track]).collect())
}
