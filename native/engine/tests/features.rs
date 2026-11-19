//! The feature store (`store`), the queue of units shared by the GPUs and the CPU (`sched`) and
//! the features step (`features`): the store round-trips key points and descriptors through its
//! files and refuses a file with another magic or version; `extract_all` on the CPU fills the
//! store from 12 photos written by the test as PNG and JPEG, with a truncated file among them
//! (unreadable) and a GIF (unsupported), the key points in the pixels of the full photo with
//! COLMAP's 0.5 centre, bit for bit what `sift_cpu::extract` gives on the same reduced image; a
//! mask drops the points under it and the small store gets its capped points; a worker that fails
//! gives its units back and the scheduler's answer is the one of the CPU alone. The fixtures are
//! written under cargo's temporary folder of the target (`CARGO_TARGET_TMPDIR`), the JPEGs by the
//! writer of `baseline_jpeg`.

use std::fs;
use std::io::BufWriter;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Mutex;

use ppg_engine::features::{extract_all, extract_pixels, FeatureOptions, PhotoJob};
use ppg_engine::gpu;
use ppg_engine::gpu_sift::{Options, Sift, DESC};
use ppg_engine::image::{colmap_size, read_grey, rescale_as_colmap};
use ppg_engine::sched::{self, Worker};
use ppg_engine::sift_cpu;
use ppg_engine::store::{ExtractParams, FeatureStore, Impl, PhotoFeatures};

mod baseline_jpeg;

/// Knuth's MMIX linear congruential generator, as in the other tests of this crate.
struct Lcg(u64);

impl Lcg {
    fn bits(&mut self) -> u64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        self.0
    }

    fn unit(&mut self) -> f32 {
        ((self.bits() >> 40) as f32) / (1u64 << 24) as f32
    }

    fn byte(&mut self) -> u8 {
        (self.bits() >> 56) as u8
    }
}

/// A grey photo of `w x h` from `seed`: value noise at four grid spacings and blobs of three
/// sizes on a grey of 128 (the image of `tests/sift.rs`, in bytes), the same at every run.
fn texture(w: usize, h: usize, seed: u64) -> Vec<u8> {
    let mut random = Lcg(seed);
    let mut img = vec![0.5f32; w * h];
    for (spacing, amplitude) in [(3usize, 0.10f32), (8, 0.12), (32, 0.12), (128, 0.14)] {
        let (gw, gh) = (w / spacing + 2, h / spacing + 2);
        let grid: Vec<f32> = (0..gw * gh).map(|_| random.unit() - 0.5).collect();
        for y in 0..h {
            let fy = y as f32 / spacing as f32;
            let (gy, ty) = (fy as usize, fy.fract());
            for x in 0..w {
                let fx = x as f32 / spacing as f32;
                let (gx, tx) = (fx as usize, fx.fract());
                let top = grid[gy * gw + gx] * (1.0 - tx) + grid[gy * gw + gx + 1] * tx;
                let bottom = grid[(gy + 1) * gw + gx] * (1.0 - tx) + grid[(gy + 1) * gw + gx + 1] * tx;
                img[y * w + x] += amplitude * (top * (1.0 - ty) + bottom * ty);
            }
        }
    }
    let margin = 20.0f32.min(w.min(h) as f32 / 4.0);
    for (sigma, count) in [(2.0f32, 12usize), (5.0, 8), (12.0, 4)] {
        for _ in 0..count {
            let cx = margin + random.unit() * (w as f32 - 2.0 * margin);
            let cy = margin + random.unit() * (h as f32 - 2.0 * margin);
            let sign = if random.unit() < 0.5 { -0.35 } else { 0.35 };
            let reach = (3.0 * sigma).ceil() as i64;
            for y in (cy as i64 - reach).max(0)..=(cy as i64 + reach).min(h as i64 - 1) {
                for x in (cx as i64 - reach).max(0)..=(cx as i64 + reach).min(w as i64 - 1) {
                    let (dx, dy) = (x as f32 - cx, y as f32 - cy);
                    img[y as usize * w + x as usize] += sign * (-(dx * dx + dy * dy) / (2.0 * sigma * sigma)).exp();
                }
            }
        }
    }
    img.iter().map(|v| (v.clamp(0.0, 1.0) * 255.0).round() as u8).collect()
}

/// A fresh folder for the fixtures of one test, emptied if a failed run left it.
fn folder(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("features_{name}"));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn write_png(path: &Path, w: usize, h: usize, color: png::ColorType, data: &[u8]) {
    let mut encoder = png::Encoder::new(BufWriter::new(fs::File::create(path).unwrap()), w as u32, h as u32);
    encoder.set_color(color);
    encoder.set_depth(png::BitDepth::Eight);
    encoder.write_header().unwrap().write_image_data(data).unwrap();
}

/// The grey `plane` as three equal channels.
fn rgb(plane: &[u8]) -> Vec<u8> {
    plane.iter().flat_map(|&v| [v, v, v]).collect()
}

/// The twelve photos of the tests: six PNGs (grey and RGB) and six JPEGs (one and three
/// components), of even and odd sizes, named with a blank and a folder among them, as the names
/// of a project may be. Written into `dir`; the names and their sizes.
fn twelve_photos(dir: &Path) -> Vec<(String, usize, usize)> {
    let sizes = [(320usize, 240usize), (321, 241), (300, 200), (256, 256), (333, 199), (320, 240)];
    fs::create_dir_all(dir.join("sub")).unwrap();
    let mut photos = Vec::new();
    for (i, &(w, h)) in sizes.iter().enumerate() {
        let plane = texture(w, h, 100 + i as u64);
        let png_name = match i {
            0 => "sub/IMG 0001.png".to_string(),
            _ => format!("png_{i:02}.png"),
        };
        if i % 2 == 0 {
            write_png(&dir.join(&png_name), w, h, png::ColorType::Grayscale, &plane);
        } else {
            write_png(&dir.join(&png_name), w, h, png::ColorType::Rgb, &rgb(&plane));
        }
        photos.push((png_name, w, h));
        let jpg_name = format!("jpg_{i:02}.jpg");
        let plane = texture(w, h, 200 + i as u64);
        if i % 2 == 0 {
            baseline_jpeg::write(&dir.join(&jpg_name), w, h, 3, &rgb(&plane));
        } else {
            baseline_jpeg::write(&dir.join(&jpg_name), w, h, 1, &plane);
        }
        photos.push((jpg_name, w, h));
    }
    photos
}

/// A truncated JPEG and a GIF beside the photos: their names.
fn two_bad_files(dir: &Path) -> (String, String) {
    let bytes = fs::read(dir.join("jpg_00.jpg")).unwrap();
    fs::write(dir.join("half.jpg"), &bytes[..bytes.len() / 2]).unwrap();
    fs::write(dir.join("pattern.gif"), b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff\x2c").unwrap();
    ("half.jpg".to_string(), "pattern.gif".to_string())
}

fn job(dir: &Path, name: &str, limit: usize) -> PhotoJob {
    PhotoJob { name: name.to_string(), path: Some(dir.join(name)), mask: None, limit, small_limit: None }
}

/// The SIFT options of the tests: a cap that bites on the textured photos.
fn sift_options() -> Options {
    Options { max_features: 600, ..Options::default() }
}

fn cpu_options() -> FeatureOptions {
    FeatureOptions { sift: sift_options(), dct: true, readers: 2, cpu_threads: 2 }
}

/// What the features step must store for the photo at `path` reduced to `limit`, computed here
/// step by step: the photo as `image::read_grey` reads it, reduced as COLMAP reduces it, the
/// points of `sift_cpu::extract`, brought to the pixels of the full photo as
/// `sfm._extract_features_gpu` brings them (the 0.5 centre added, then the factor of the
/// reduction, the scale by the mean factor).
fn reference(path: &Path, limit: usize, o: &Options) -> (Vec<[f32; 4]>, Vec<u8>, (usize, usize), (usize, usize)) {
    let (grey, full) = read_grey(path, limit, true).unwrap();
    let (w, h) = colmap_size(full.0, full.1, limit);
    let reduced = rescale_as_colmap(&grey, w, h);
    let image: Vec<f32> = reduced.pixels.iter().map(|&p| p as f32 / 255.0).collect();
    let (kp, desc) = sift_cpu::extract(&image, w, h, o).unwrap();
    let sx = (full.0 as f64 / w as f64) as f32;
    let sy = (full.1 as f64 / h as f64) as f32;
    let ss = (0.5 * (full.0 as f64 / w as f64 + full.1 as f64 / h as f64)) as f32;
    let kp = kp.iter().map(|k| [(k[0] + 0.5) * sx, (k[1] + 0.5) * sy, k[2] * ss, k[3]]).collect();
    (kp, desc, full, (w, h))
}

/// Random features of `n` points for the store tests.
fn features(random: &mut Lcg, n: usize, implementation: Impl, image_id: Option<u32>) -> PhotoFeatures {
    let keypoints = (0..n)
        .map(|_| [random.unit() * 4000.0, random.unit() * 3000.0, random.unit() * 20.0, random.unit() * 6.28])
        .collect();
    let descriptors = (0..n * DESC).map(|_| random.byte()).collect();
    let params = ExtractParams { max_image_size: 2016, reduced_width: 2016, reduced_height: 1512, masked: false,
                                 ..ExtractParams::of(&sift_options()) };
    PhotoFeatures { width: 4000, height: 3000, implementation, image_id, params, keypoints, descriptors }
}

#[test]
fn the_store_round_trips() {
    let dir = folder("store");
    let store = FeatureStore::create(&dir).unwrap();
    assert!(store.names().is_empty() && store.get("none").is_none());
    let mut random = Lcg(5);
    let a = features(&mut random, 300, Impl::Cpu, None);
    let b = features(&mut random, 0, Impl::Gpu, Some(7));
    let c = features(&mut random, 1000, Impl::Imported, Some(2));
    // names as a project has them: a folder, a blank, an upper-case extension
    store.put("sub/IMG 0002.JPG", &a).unwrap();
    store.put("b.jpg", &b).unwrap();
    store.put("a.jpg", &c).unwrap();
    assert_eq!(store.names(), ["a.jpg", "b.jpg", "sub/IMG 0002.JPG"], "sorted");
    // a photo put again replaces its features, under the same name only
    let a2 = features(&mut random, 120, Impl::Cpu, None);
    store.put("sub/IMG 0002.JPG", &a2).unwrap();
    assert_eq!(store.names().len(), 3);
    drop(store);

    let again = FeatureStore::open(&dir).unwrap();
    assert_eq!(again.names(), ["a.jpg", "b.jpg", "sub/IMG 0002.JPG"]);
    for (name, want) in [("sub/IMG 0002.JPG", &a2), ("b.jpg", &b), ("a.jpg", &c)] {
        let view = again.get(name).unwrap_or_else(|| panic!("{name} is not in the store"));
        assert!(view.keypoints() == want.keypoints.as_slice(), "{name}: the key points differ");
        assert!(view.descriptors() == want.descriptors.as_slice(), "{name}: the descriptors differ");
        let header = view.header();
        assert_eq!((header.width, header.height, header.points as usize), (4000, 3000, want.keypoints.len()), "{name}");
        assert_eq!(header.implementation, want.implementation, "{name}");
        assert_eq!(header.image_id, want.image_id, "{name}");
        assert_eq!(header.params, want.params, "{name}");
    }
    assert_eq!(again.get("sub/IMG 0002.JPG").unwrap().header().implementation, Impl::Cpu);
    assert!(again.get("missing.jpg").is_none());
    // removed: gone from the names and from the folder
    assert!(again.remove("b.jpg").unwrap());
    assert!(!again.remove("b.jpg").unwrap());
    assert_eq!(again.names(), ["a.jpg", "sub/IMG 0002.JPG"]);
    let reopened = FeatureStore::open(&dir).unwrap();
    assert_eq!(reopened.names(), ["a.jpg", "sub/IMG 0002.JPG"]);
    // the store is its index and its feature files, nothing else: one file per photo
    let files: Vec<String> =
        fs::read_dir(&dir).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).collect();
    assert_eq!(files.len(), 3, "{files:?}");
    assert!(files.contains(&"index.json".to_string()), "{files:?}");
    // a store is created once
    assert!(FeatureStore::create(&dir).is_err(), "created over an existing store");
    assert!(FeatureStore::open(&dir.join("nowhere")).is_err());

    // a file whose magic or version is wrong is refused by name, with what was read
    let file_of = |name: &str| {
        let index: serde_json::Value = serde_json::from_slice(&fs::read(dir.join("index.json")).unwrap()).unwrap();
        dir.join(index["photos"][name]["file"].as_str().expect("the file of the photo"))
    };
    let path = file_of("a.jpg");
    let good = fs::read(&path).unwrap();
    let mut bad = good.clone();
    bad[..4].copy_from_slice(b"PLY\n");
    fs::write(&path, &bad).unwrap();
    let refused = FeatureStore::open(&dir).err().expect("a wrong magic is refused").to_string();
    let file_name = path.file_name().unwrap().to_string_lossy().into_owned();
    assert!(refused.contains(&file_name) && refused.contains("PPGF"), "{refused}");
    let mut bad = good.clone();
    bad[4..8].copy_from_slice(&9u32.to_le_bytes());
    fs::write(&path, &bad).unwrap();
    let refused = FeatureStore::open(&dir).err().expect("a wrong version is refused").to_string();
    assert!(refused.contains(&file_name) && refused.contains('9'), "{refused}");
    // and a file cut short
    fs::write(&path, &good[..good.len() - 100]).unwrap();
    let refused = FeatureStore::open(&dir).err().expect("a short file is refused").to_string();
    assert!(refused.contains(&file_name), "{refused}");
    fs::write(&path, &good).unwrap();
    assert!(FeatureStore::open(&dir).is_ok());
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn extract_all_on_the_cpu_fills_the_store() {
    let dir = folder("cpu");
    let photos = twelve_photos(&dir);
    let (half, gif) = two_bad_files(&dir);
    let limit = 160;
    let mut jobs: Vec<PhotoJob> = photos.iter().map(|(name, _, _)| job(&dir, name, limit)).collect();
    jobs.insert(3, job(&dir, &half, limit));
    jobs.push(job(&dir, &gif, limit));
    assert_eq!(jobs.len(), 14);
    let store = FeatureStore::create(&dir.join("store")).unwrap();
    let o = cpu_options();
    let calls = Mutex::new(Vec::new());
    let progress = |done: usize, total: usize| calls.lock().unwrap().push((done, total));
    let report = extract_all(&jobs, &store, None, &o, &[], &AtomicBool::new(false), &progress).unwrap();
    assert_eq!(report.done, 12, "{:?}", report.log);
    assert_eq!(report.unreadable, [half.clone()]);
    assert_eq!(report.unsupported, [gif.clone()]);
    assert_eq!(report.by_impl, (0, 12));
    let calls = calls.into_inner().unwrap();
    assert_eq!(calls.first(), Some(&(0, 14)), "{calls:?}");
    assert_eq!(calls.last(), Some(&(14, 14)), "{calls:?}");
    assert!(calls.windows(2).all(|p| p[0].0 < p[1].0), "{calls:?}");
    let mut names: Vec<String> = photos.iter().map(|(n, _, _)| n.clone()).collect();
    names.sort();
    assert_eq!(store.names(), names);

    let reopened = FeatureStore::open(&dir.join("store")).unwrap();
    let mut total = 0;
    for (name, w, h) in &photos {
        let view = reopened.get(name).unwrap_or_else(|| panic!("{name} is not in the store"));
        let header = view.header();
        assert_eq!((header.width as usize, header.height as usize), (*w, *h), "{name}");
        assert_eq!(header.implementation, Impl::Cpu, "{name}");
        assert_eq!(header.image_id, None, "{name}");
        let (kp, desc, full, reduced) = reference(&dir.join(name), limit, &o.sift);
        assert_eq!(full, (*w, *h), "{name}");
        assert_eq!((header.params.reduced_width as usize, header.params.reduced_height as usize), reduced, "{name}");
        assert_eq!(header.params.max_image_size as usize, limit, "{name}");
        assert_eq!(header.params.max_features as usize, o.sift.max_features, "{name}");
        assert!(!header.params.masked, "{name}");
        assert!(!kp.is_empty(), "{name}: no key point in the reference");
        let stored = view.keypoints().len();
        assert_eq!(stored, kp.len(), "{name}: {stored} points stored, {} in the reference", kp.len());
        assert!(view.keypoints() == kp.as_slice(), "{name}: the key points are not the reference's (centre, factor)");
        assert!(view.descriptors() == desc.as_slice(), "{name}: the descriptors differ");
        for (i, k) in view.keypoints().iter().enumerate() {
            let inside = k[0] >= 0.0 && k[0] < *w as f32 && k[1] >= 0.0 && k[1] < *h as f32;
            assert!(inside && k[2] > 0.0, "{name}: key point {i} outside the full photo: {k:?}");
        }
        total += kp.len();
    }
    assert_eq!(report.points, total);
    let seconds = report.seconds;
    assert!(seconds.decode > 0.0 && seconds.sift > 0.0 && seconds.write > 0.0, "{seconds:?}");
    assert!(reopened.get(&half).is_none() && reopened.get(&gif).is_none());
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_mask_drops_the_points_under_it() {
    let dir = folder("mask");
    let (w, h) = (320usize, 240usize);
    let plane = texture(w, h, 77);
    write_png(&dir.join("photo.png"), w, h, png::ColorType::Grayscale, &plane);
    // black on the left half, white on the right; and a mask of another size
    let mask: Vec<u8> = (0..w * h).map(|i| if i % w < w / 2 { 0 } else { 255 }).collect();
    write_png(&dir.join("photo.png.png"), w, h, png::ColorType::Grayscale, &mask);
    write_png(&dir.join("wrong.png"), w / 2, h / 2, png::ColorType::Grayscale, &vec![0u8; w * h / 4]);
    let limit = 160;
    let o = FeatureOptions { cpu_threads: 1, ..cpu_options() };
    let cancel = AtomicBool::new(false);
    let quiet = |_: usize, _: usize| {};

    let plain = FeatureStore::create(&dir.join("plain")).unwrap();
    extract_all(&[job(&dir, "photo.png", limit)], &plain, None, &o, &[], &cancel, &quiet).unwrap();
    let all = plain.get("photo.png").unwrap();
    let left = all.keypoints().iter().filter(|k| k[0] < (w / 2) as f32).count();
    let right_count = all.keypoints().len() - left;
    assert!(left > 20 && right_count > 20, "{left} points on the left, {right_count} on the right");

    let masked = FeatureStore::create(&dir.join("masked")).unwrap();
    let small_store = FeatureStore::create(&dir.join("small")).unwrap();
    let mut with_mask = job(&dir, "photo.png", limit);
    with_mask.mask = Some(dir.join("photo.png.png"));
    with_mask.small_limit = Some(80);
    let report = extract_all(&[with_mask], &masked, Some((&small_store, 50)), &o, &[], &cancel, &quiet).unwrap();
    assert_eq!((report.done, report.by_impl), (1, (0, 1)), "{:?}", report.log);
    let kept = masked.get("photo.png").unwrap();
    assert!(kept.header().params.masked);
    assert!(kept.keypoints().iter().all(|k| k[0] >= (w / 2) as f32), "a point under the mask was kept");
    // the points kept are the unmasked extraction's points on the right, in their order
    let on_the_right = |k: &[f32; 4]| (k[0] as i64).clamp(0, w as i64 - 1) >= (w / 2) as i64;
    let right: Vec<[f32; 4]> = all.keypoints().iter().copied().filter(on_the_right).collect();
    assert!(kept.keypoints() == right.as_slice(), "{} kept, {} expected", kept.keypoints().len(), right.len());
    // the small store: the preselection image of 80 pixels, at most 50 points, in the pixels of
    // the full photo, under the same mask
    let small = small_store.get("photo.png").unwrap();
    let header = small.header();
    assert_eq!((header.width as usize, header.height as usize, header.params.max_image_size as usize), (w, h, 80));
    assert_eq!((header.params.reduced_width, header.params.reduced_height, header.params.max_features), (80, 60, 50));
    assert!(!small.keypoints().is_empty() && small.keypoints().len() <= 50, "{} small points", small.keypoints().len());
    let inside_right_half = |k: &[f32; 4]| k[0] >= (w / 2) as f32 && k[0] < w as f32 && k[1] >= 0.0 && k[1] < h as f32;
    assert!(small.keypoints().iter().all(inside_right_half));
    assert_eq!(small.descriptors().len(), small.keypoints().len() * DESC);

    // a mask of another size than the photo is not applied, and the log says so
    let ignored = FeatureStore::create(&dir.join("ignored")).unwrap();
    let mut wrong = job(&dir, "photo.png", limit);
    wrong.mask = Some(dir.join("wrong.png"));
    let report = extract_all(&[wrong], &ignored, None, &o, &[], &cancel, &quiet).unwrap();
    let view = ignored.get("photo.png").unwrap();
    assert!(view.keypoints() == all.keypoints() && !view.header().params.masked);
    assert!(report.log.iter().any(|line| line.contains("mask") && line.contains("160 x 120")), "{:?}", report.log);

    // the same photo given as pixels (Python's decoding): the same features, in both stores
    let (grey, full) = read_grey(&dir.join("photo.png"), 0, false).unwrap();
    let mut as_pixels = job(&dir, "photo.png", limit);
    as_pixels.path = None;
    as_pixels.mask = Some(dir.join("photo.png.png"));
    as_pixels.small_limit = Some(80);
    let from_pixels = FeatureStore::create(&dir.join("pixels")).unwrap();
    let small_from_pixels = FeatureStore::create(&dir.join("small_pixels")).unwrap();
    let who = extract_pixels(&as_pixels, grey, full, &from_pixels, Some((&small_from_pixels, 50)), &o, &[]).unwrap();
    assert_eq!(who, Impl::Cpu);
    let again = from_pixels.get("photo.png").unwrap();
    assert!(again.keypoints() == kept.keypoints() && again.descriptors() == kept.descriptors());
    assert_eq!(again.header(), kept.header());
    let small_again = small_from_pixels.get("photo.png").unwrap();
    assert!(small_again.keypoints() == small.keypoints() && small_again.header() == small.header());

    // cancelled before the start: an error, nothing in the store
    let untouched = FeatureStore::create(&dir.join("untouched")).unwrap();
    let stop = AtomicBool::new(true);
    let refused = extract_all(&[job(&dir, "photo.png", limit)], &untouched, None, &o, &[], &stop, &quiet);
    assert!(refused.as_ref().is_err_and(|why| why.starts_with("cancelled")), "{refused:?}");
    assert!(untouched.is_empty());
    fs::remove_dir_all(&dir).unwrap();
}

/// A worker of the tests: `unit * 10`, failing on purpose at its `fail_at`-th unit. A CPU fake
/// made `after_gpu` holds its first unit until the GPU fake has run twice, so that the GPU
/// reaches its third unit before the CPU can drain the queue of trivial units (the scenarios
/// are then the same at every run, whatever the threads do).
struct Fake {
    name: &'static str,
    fail_at: Option<usize>,
    served: usize,
    after_gpu: bool,
}

/// Every call of `run` of every fake, and the units the GPU fake did.
static CALLS: AtomicUsize = AtomicUsize::new(0);
static GPU_RUNS: AtomicUsize = AtomicUsize::new(0);

/// Held while the test hook's variable is set and by every test that runs GPU workers through
/// the scheduler: the tests of this file run in parallel threads of one process, and the
/// variable is read at the start of every `sched::run`.
static TURN: Mutex<()> = Mutex::new(());

impl Worker<u32, u32> for Fake {
    fn name(&self) -> String {
        self.name.to_string()
    }

    fn run(&mut self, unit: &u32) -> Result<u32, String> {
        if self.after_gpu && self.served == 0 {
            let started = std::time::Instant::now();
            while GPU_RUNS.load(Ordering::SeqCst) < 2 {
                assert!(started.elapsed() < std::time::Duration::from_secs(10), "the GPU fake never ran twice");
                std::thread::yield_now();
            }
        }
        self.served += 1;
        CALLS.fetch_add(1, Ordering::SeqCst);
        if self.fail_at == Some(self.served) {
            return Err(format!("broken on purpose at unit {unit}"));
        }
        if self.name.starts_with("GPU") {
            GPU_RUNS.fetch_add(1, Ordering::SeqCst);
        }
        Ok(unit * 10)
    }
}

fn fake(name: &'static str, fail_at: Option<usize>) -> Box<dyn Worker<u32, u32>> {
    Box::new(Fake { name, fail_at, served: 0, after_gpu: false })
}

/// The CPU fakes of the scenarios with a GPU: each holds its first unit until the GPU has run
/// twice.
fn cpu_after_gpu() -> Option<sched::Factory<'static, u32, u32>> {
    GPU_RUNS.store(0, Ordering::SeqCst);
    Some(Box::new(|| Box::new(Fake { name: "CPU", fail_at: None, served: 0, after_gpu: true })))
}

#[test]
fn a_worker_that_fails_gives_its_units_back() {
    let units: Vec<u32> = (1..=20).collect();
    let want: Vec<(u32, u32)> = units.iter().map(|&u| (u, u * 10)).collect();
    let calls = Mutex::new(Vec::new());
    let progress = |done: usize, total: usize| calls.lock().unwrap().push((done, total));
    let never = AtomicBool::new(false);
    let cpu = || -> Option<sched::Factory<'static, u32, u32>> { Some(Box::new(|| fake("CPU", None))) };

    // the CPU alone: every unit once, in the order given
    let alone = sched::run(units.clone(), vec![], cpu(), 2, &never, &progress).unwrap();
    assert_eq!(alone.results, want);
    assert!(alone.log.is_empty(), "{:?}", alone.log);
    let calls_alone = std::mem::take(&mut *calls.lock().unwrap());
    assert_eq!((calls_alone.first(), calls_alone.last()), (Some(&(0, 20)), Some(&(20, 20))), "{calls_alone:?}");

    // a GPU that fails at its third unit beside the CPU: the same answer, one line in the log
    CALLS.store(0, Ordering::SeqCst);
    let failing = vec![fake("GPU 0 (fake)", Some(3))];
    let shared = sched::run(units.clone(), failing, cpu_after_gpu(), 1, &never, &progress).unwrap();
    assert_eq!(shared.results, want);
    assert_eq!(shared.log.len(), 1, "{:?}", shared.log);
    assert!(shared.log[0].contains("GPU 0 (fake)") && shared.log[0].contains("broken on purpose"), "{:?}", shared.log);
    // every unit was run once, plus the one the GPU failed on
    assert_eq!(CALLS.load(Ordering::SeqCst), 21);
    let calls_shared = std::mem::take(&mut *calls.lock().unwrap());
    assert_eq!(calls_shared.last(), Some(&(20, 20)), "{calls_shared:?}");

    // the failing worker alone: an error, the units that were done are in the log's count
    let failing = vec![fake("GPU 0 (fake)", Some(3))];
    let (reason, log) = sched::run(units.clone(), failing, None, 0, &never, &progress).err().unwrap();
    assert!(reason.contains("no worker") && reason.contains("18"), "{reason}");
    assert_eq!(log.len(), 1, "{log:?}");
    // no worker at all
    let (reason, _) = sched::run::<u32, u32>(units.clone(), vec![], None, 0, &never, &progress).err().unwrap();
    assert!(reason.contains("no worker"), "{reason}");

    // cancelled before the start: nothing run
    CALLS.store(0, Ordering::SeqCst);
    let cancelled = AtomicBool::new(true);
    let sound = vec![fake("GPU 0 (fake)", None)];
    let (reason, _) = sched::run(units.clone(), sound, cpu(), 2, &cancelled, &progress).err().unwrap();
    assert_eq!(reason, "cancelled");
    assert_eq!(CALLS.load(Ordering::SeqCst), 0);
    // no units: nothing to do, no error
    let empty = sched::run(Vec::<u32>::new(), vec![fake("GPU 0 (fake)", None)], cpu(), 1, &never, &progress).unwrap();
    assert!(empty.results.is_empty() && empty.log.is_empty());

    // the test hook of the lost GPU: PPG_TEST_GPU_FAIL_AFTER=3 retires every GPU worker at its
    // third unit, without running it, as a GPU that fails would be retired
    CALLS.store(0, Ordering::SeqCst);
    let turn = TURN.lock().unwrap_or_else(|p| p.into_inner());
    std::env::set_var(sched::FAIL_AFTER_VAR, "3");
    let hooked = sched::run(units.clone(), vec![fake("GPU 0 (fake)", None)], cpu_after_gpu(), 1, &never, &progress);
    std::env::remove_var(sched::FAIL_AFTER_VAR);
    drop(turn);
    let hooked = hooked.unwrap();
    assert_eq!(hooked.results, want);
    assert_eq!(hooked.log.len(), 1, "{:?}", hooked.log);
    let line = &hooked.log[0];
    assert!(line.contains(sched::FAIL_AFTER_VAR) && line.contains("GPU 0 (fake)"), "{line}");
    // the GPU ran two units, the CPU the other eighteen
    assert_eq!(CALLS.load(Ordering::SeqCst), 20);
}

#[test]
fn extract_all_on_a_gpu_shares_the_photos_with_the_cpu() {
    let Some(adapter) = gpu::adapters().into_iter().find(|a| a.device_type != "Cpu") else {
        eprintln!("no GPU adapter: the shared extraction is not run");
        return;
    };
    if let Err(problem) = Sift::new(Some(adapter.index)) {
        // an adapter that opens no device (a GL one of a runner) is reported and left out
        eprintln!("adapter {} ({}) skipped: {problem}", adapter.index, adapter.name);
        return;
    }
    let _turn = TURN.lock().unwrap_or_else(|p| p.into_inner());
    let dir = folder("gpu");
    let photos = twelve_photos(&dir);
    let limit = 160;
    let jobs: Vec<PhotoJob> = photos.iter().map(|(name, _, _)| job(&dir, name, limit)).collect();
    let o = FeatureOptions { cpu_threads: 1, ..cpu_options() };
    let quiet = |_: usize, _: usize| {};
    let cancel = AtomicBool::new(false);
    // the GPU alone
    let on_gpu = FeatureStore::create(&dir.join("gpu")).unwrap();
    let only = FeatureOptions { cpu_threads: 0, ..o.clone() };
    let report = extract_all(&jobs, &on_gpu, None, &only, &[adapter.index], &cancel, &quiet).unwrap();
    assert_eq!((report.done, report.by_impl), (12, (12, 0)), "{:?}", report.log);
    // the GPU and the CPU together: every photo once, by one of them
    let shared = FeatureStore::create(&dir.join("shared")).unwrap();
    let report = extract_all(&jobs, &shared, None, &o, &[adapter.index], &cancel, &quiet).unwrap();
    assert_eq!(report.done, 12, "{:?}", report.log);
    assert_eq!(report.by_impl.0 + report.by_impl.1, 12, "{:?}", report.by_impl);
    let (on_gpu_count, on_cpu_count) = report.by_impl;
    let who = format!("adapter {} ({})", adapter.index, adapter.name);
    eprintln!("{who}: {on_gpu_count} photos on the GPU, {on_cpu_count} on the CPU");
    // the GPU's points are the CPU's (the twins of tests/sift.rs: within 0.5 px and 10 % of scale)
    for (name, w, h) in &photos {
        let view = on_gpu.get(name).unwrap();
        assert_eq!(view.header().implementation, Impl::Gpu, "{name}");
        let (cpu, _, _, _) = reference(&dir.join(name), limit, &o.sift);
        let twins = view.keypoints().iter().filter(|p| cpu.iter().any(|q| {
            (p[0] - q[0]).hypot(p[1] - q[1]) < 0.5 && (p[2] / q[2]).ln().abs() < 0.1
        })).count();
        let share = twins as f64 / view.keypoints().len().max(1) as f64;
        assert!(share > 0.95, "{name}: {share:.3} of the GPU's points have a CPU twin");
        let inside = |k: &[f32; 4]| k[0] >= 0.0 && k[0] < *w as f32 && k[1] >= 0.0 && k[1] < *h as f32;
        assert!(view.keypoints().iter().all(inside), "{name}");
        let other = shared.get(name).unwrap();
        assert!(!other.keypoints().is_empty(), "{name}: no point in the shared store");
    }
    fs::remove_dir_all(&dir).unwrap();
}
