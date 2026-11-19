//! The feature store: the key points and descriptors of the photos of a chunk, one file per
//! photo, memory-mapped when read, so that the matching of thousands of photos holds the
//! descriptors of two photos in memory at a time and the preselection maps its small stores
//! whole. A store is a folder with an `index.json` and the feature files it names:
//!
//! - `index.json`: `{"version": 1, "next": N, "photos": {name: {"file": F, "points": P}}}`, the
//!   photos keyed by their names as the project gives them (POSIX relative paths, blanks and
//!   folders allowed), sorted, each mapped to a file name of the store's own making (a counter,
//!   `00000001.ppgf`: a photo name is never a file name, whatever characters it holds);
//! - a feature file: a header of 128 bytes - the magic `PPGF`, the format version 1, the size of
//!   the full photo, the number of points, the implementation that made them (GPU, CPU or
//!   imported from a COLMAP database), COLMAP's image id when known and the extraction
//!   parameters - then the key points (`[x, y, scale, orientation]` as f32, 16 bytes each) and
//!   the descriptors (128 bytes each), each block 16-byte aligned; little-endian throughout, as
//!   every platform of the program is.
//!
//! Key points are in the pixels of the full photo with COLMAP's centre of the first pixel at
//! (0.5, 0.5), the scale in pixels of the full photo, the orientation in radians: what a COLMAP
//! database holds. Every `put` writes a new file (the next number of the counter), then rewrites
//! the index to a temporary name renamed into place, then deletes the file the name had before:
//! the index never names a file that is not complete, no file is ever written or renamed over
//! (a reader may have it mapped, which Windows refuses), and a crash leaves at worst a file the
//! index does not name - an orphan, which `open` ignores. `put` is thread-safe (the index under
//! a lock, the file written outside it). `open` reads the header of every file the index names
//! and refuses the store at the first whose magic, version or length is wrong, naming the file
//! and what was read.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard, PoisonError};

use memmap2::Mmap;
use serde_json::{json, Map, Value};

use crate::gpu_sift::{Options, DESC};

#[cfg(target_endian = "big")]
compile_error!("the feature store writes and maps its files little-endian");

/// The first four bytes of a feature file.
pub const MAGIC: [u8; 4] = *b"PPGF";
/// The version of the file format and of the index.
pub const VERSION: u32 = 1;
/// The index of a store, in its folder.
pub const INDEX_FILE: &str = "index.json";
/// The extension of a feature file.
pub const EXTENSION: &str = "ppgf";

const HEADER_BYTES: usize = 128;
const KEYPOINT_BYTES: usize = 16;

// the offsets of the fields of the header; 72..128 is kept at zero
const O_MAGIC: usize = 0;
const O_VERSION: usize = 4;
const O_WIDTH: usize = 8;
const O_HEIGHT: usize = 12;
const O_POINTS: usize = 16;
const O_IMPL: usize = 20;
const O_IMAGE_ID: usize = 24;
const O_FLAGS: usize = 28;
const O_MAX_IMAGE_SIZE: usize = 32;
const O_REDUCED_WIDTH: usize = 36;
const O_REDUCED_HEIGHT: usize = 40;
const O_FIRST_OCTAVE: usize = 44;
const O_NUM_OCTAVES: usize = 48;
const O_OCTAVE_RESOLUTION: usize = 52;
const O_PEAK_THRESHOLD: usize = 56;
const O_EDGE_THRESHOLD: usize = 60;
const O_MAX_ORIENTATIONS: usize = 64;
const O_MAX_FEATURES: usize = 68;

const FLAG_IMAGE_ID: u32 = 1;
const FLAG_MASKED: u32 = 2;
const FLAG_UPRIGHT: u32 = 4;

/// What made the features of a photo.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Impl {
    Gpu,
    Cpu,
    /// Read from a COLMAP database (the features of the classic step).
    Imported,
}

impl Impl {
    fn code(self) -> u32 {
        match self {
            Impl::Gpu => 0,
            Impl::Cpu => 1,
            Impl::Imported => 2,
        }
    }

    fn from_code(code: u32) -> Option<Impl> {
        [Impl::Gpu, Impl::Cpu, Impl::Imported].into_iter().find(|i| i.code() == code)
    }

    /// The word of the implementation, as a report names it.
    pub fn name(self) -> &'static str {
        match self {
            Impl::Gpu => "gpu",
            Impl::Cpu => "cpu",
            Impl::Imported => "imported",
        }
    }
}

/// How the features of a photo were extracted: the size the photo was reduced to (`max_image_size`
/// the limit on its long side, 0 for none; `reduced_width` x `reduced_height` the size the SIFT
/// ran at), the options of the SIFT, whether a mask was applied. All zero for imported features.
#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct ExtractParams {
    pub max_image_size: u32,
    pub reduced_width: u32,
    pub reduced_height: u32,
    pub first_octave: i32,
    pub num_octaves: u32,
    pub octave_resolution: u32,
    pub peak_threshold: f32,
    pub edge_threshold: f32,
    pub max_orientations: u32,
    pub upright: bool,
    pub max_features: u32,
    pub masked: bool,
}

impl ExtractParams {
    /// The parameters of an extraction with the SIFT options `sift`, the sizes and the mask left
    /// at zero for the caller to fill.
    pub fn of(sift: &Options) -> ExtractParams {
        ExtractParams {
            first_octave: sift.first_octave,
            num_octaves: sift.num_octaves,
            octave_resolution: sift.octave_resolution,
            peak_threshold: sift.peak_threshold,
            edge_threshold: sift.edge_threshold,
            max_orientations: sift.max_orientations,
            upright: sift.upright,
            max_features: u32::try_from(sift.max_features).unwrap_or(u32::MAX),
            ..ExtractParams::default()
        }
    }
}

/// The header of a feature file, as [`FeatureView::header`] gives it.
#[derive(Clone, Debug, PartialEq)]
pub struct FeatureHeader {
    /// The size of the full photo.
    pub width: u32,
    pub height: u32,
    pub points: u32,
    pub implementation: Impl,
    /// COLMAP's image id in the database the features were imported from or exported to.
    pub image_id: Option<u32>,
    pub params: ExtractParams,
}

/// The features of one photo, to be put into a store: the size of the full photo, what made
/// them, the key points in the pixels of the full photo with COLMAP's centre and their
/// descriptors (128 bytes each, as many as the key points).
#[derive(Clone, Debug, PartialEq)]
pub struct PhotoFeatures {
    pub width: u32,
    pub height: u32,
    pub implementation: Impl,
    pub image_id: Option<u32>,
    pub params: ExtractParams,
    pub keypoints: Vec<[f32; 4]>,
    pub descriptors: Vec<u8>,
}

impl PhotoFeatures {
    /// The header these features are written with.
    pub fn header(&self) -> FeatureHeader {
        FeatureHeader { width: self.width, height: self.height, points: self.keypoints.len() as u32,
                        implementation: self.implementation, image_id: self.image_id, params: self.params }
    }
}

fn put_u32(buf: &mut [u8], at: usize, v: u32) {
    buf[at..at + 4].copy_from_slice(&v.to_le_bytes());
}

fn get_u32(buf: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(buf[at..at + 4].try_into().expect("four bytes"))
}

fn encode_header(h: &FeatureHeader) -> [u8; HEADER_BYTES] {
    let p = &h.params;
    let mut buf = [0u8; HEADER_BYTES];
    buf[O_MAGIC..O_MAGIC + 4].copy_from_slice(&MAGIC);
    put_u32(&mut buf, O_VERSION, VERSION);
    put_u32(&mut buf, O_WIDTH, h.width);
    put_u32(&mut buf, O_HEIGHT, h.height);
    put_u32(&mut buf, O_POINTS, h.points);
    put_u32(&mut buf, O_IMPL, h.implementation.code());
    put_u32(&mut buf, O_IMAGE_ID, h.image_id.unwrap_or(0));
    let flags = if h.image_id.is_some() { FLAG_IMAGE_ID } else { 0 }
        | if p.masked { FLAG_MASKED } else { 0 }
        | if p.upright { FLAG_UPRIGHT } else { 0 };
    put_u32(&mut buf, O_FLAGS, flags);
    put_u32(&mut buf, O_MAX_IMAGE_SIZE, p.max_image_size);
    put_u32(&mut buf, O_REDUCED_WIDTH, p.reduced_width);
    put_u32(&mut buf, O_REDUCED_HEIGHT, p.reduced_height);
    put_u32(&mut buf, O_FIRST_OCTAVE, p.first_octave as u32);
    put_u32(&mut buf, O_NUM_OCTAVES, p.num_octaves);
    put_u32(&mut buf, O_OCTAVE_RESOLUTION, p.octave_resolution);
    put_u32(&mut buf, O_PEAK_THRESHOLD, p.peak_threshold.to_bits());
    put_u32(&mut buf, O_EDGE_THRESHOLD, p.edge_threshold.to_bits());
    put_u32(&mut buf, O_MAX_ORIENTATIONS, p.max_orientations);
    put_u32(&mut buf, O_MAX_FEATURES, p.max_features);
    buf
}

fn invalid(what: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, what)
}

/// The header of the feature file `path` out of its first bytes: the magic and the version
/// checked, named with what was read when wrong.
fn decode_header(buf: &[u8], path: &Path) -> io::Result<FeatureHeader> {
    let name = path.display();
    if buf.len() < HEADER_BYTES {
        return Err(invalid(format!("{name}: {} bytes, no header of {HEADER_BYTES}", buf.len())));
    }
    if buf[O_MAGIC..O_MAGIC + 4] != MAGIC {
        let read = String::from_utf8_lossy(&buf[O_MAGIC..O_MAGIC + 4]).into_owned();
        return Err(invalid(format!("{name}: magic {read:?} read, PPGF expected: not a feature file")));
    }
    let version = get_u32(buf, O_VERSION);
    if version != VERSION {
        return Err(invalid(format!("{name}: feature file version {version} read, {VERSION} expected")));
    }
    let code = get_u32(buf, O_IMPL);
    let implementation =
        Impl::from_code(code).ok_or_else(|| invalid(format!("{name}: implementation {code} unknown")))?;
    let flags = get_u32(buf, O_FLAGS);
    let params = ExtractParams {
        max_image_size: get_u32(buf, O_MAX_IMAGE_SIZE),
        reduced_width: get_u32(buf, O_REDUCED_WIDTH),
        reduced_height: get_u32(buf, O_REDUCED_HEIGHT),
        first_octave: get_u32(buf, O_FIRST_OCTAVE) as i32,
        num_octaves: get_u32(buf, O_NUM_OCTAVES),
        octave_resolution: get_u32(buf, O_OCTAVE_RESOLUTION),
        peak_threshold: f32::from_bits(get_u32(buf, O_PEAK_THRESHOLD)),
        edge_threshold: f32::from_bits(get_u32(buf, O_EDGE_THRESHOLD)),
        max_orientations: get_u32(buf, O_MAX_ORIENTATIONS),
        upright: flags & FLAG_UPRIGHT != 0,
        max_features: get_u32(buf, O_MAX_FEATURES),
        masked: flags & FLAG_MASKED != 0,
    };
    Ok(FeatureHeader {
        width: get_u32(buf, O_WIDTH),
        height: get_u32(buf, O_HEIGHT),
        points: get_u32(buf, O_POINTS),
        implementation,
        image_id: if flags & FLAG_IMAGE_ID != 0 { Some(get_u32(buf, O_IMAGE_ID)) } else { None },
        params,
    })
}

/// The bytes of a feature file of `points` key points.
fn file_bytes(points: u32) -> u64 {
    HEADER_BYTES as u64 + u64::from(points) * (KEYPOINT_BYTES + DESC) as u64
}

/// The length a feature file of `header` must have, against `actual`.
fn check_length(header: &FeatureHeader, actual: u64, path: &Path) -> io::Result<()> {
    let expected = file_bytes(header.points);
    if actual != expected {
        let (name, points) = (path.display(), header.points);
        return Err(invalid(format!("{name}: {actual} bytes for {points} points, {expected} expected")));
    }
    Ok(())
}

/// The feature file `path` checked without being mapped: its header, its length and the number
/// of points the index says it has.
fn check_file(path: &Path, points: u32) -> io::Result<()> {
    let mut file = File::open(path).map_err(|e| io::Error::new(e.kind(), format!("{}: {e}", path.display())))?;
    let mut buf = vec![0u8; HEADER_BYTES];
    let mut got = 0;
    while got < HEADER_BYTES {
        match file.read(&mut buf[got..]) {
            Ok(0) => break,
            Ok(n) => got += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(io::Error::new(e.kind(), format!("{}: {e}", path.display()))),
        }
    }
    let header = decode_header(&buf[..got], path)?;
    if header.points != points {
        let name = path.display();
        return Err(invalid(format!("{name}: {} points in the file, {points} in the index", header.points)));
    }
    check_length(&header, file.metadata()?.len(), path)
}

/// The features of one photo, memory-mapped from their file: the header, the key points and the
/// descriptors as slices of the map. The map stays valid while the view lives; the store never
/// writes a file in place (a photo put again goes to a new file; the old one is deleted once the
/// index names the new, or stays an orphan when it cannot be deleted yet).
pub struct FeatureView {
    header: FeatureHeader,
    map: Mmap,
}

impl FeatureView {
    fn open(path: &Path) -> io::Result<FeatureView> {
        let describe = |e: io::Error| io::Error::new(e.kind(), format!("{}: {e}", path.display()));
        let file = File::open(path).map_err(describe)?;
        // the file is read-only here and replaced only by a rename, never written in place
        let map = unsafe { Mmap::map(&file) }.map_err(describe)?;
        let header = decode_header(&map, path)?;
        check_length(&header, map.len() as u64, path)?;
        Ok(FeatureView { header, map })
    }

    pub fn header(&self) -> &FeatureHeader {
        &self.header
    }

    /// The key points `[x, y, scale, orientation]` in the pixels of the full photo, COLMAP's
    /// centre.
    pub fn keypoints(&self) -> &[[f32; 4]] {
        let n = self.header.points as usize;
        bytemuck::try_cast_slice(&self.map[HEADER_BYTES..HEADER_BYTES + n * KEYPOINT_BYTES])
            .expect("a file is mapped at a page boundary and the key points start 128 bytes in")
    }

    /// The descriptors, 128 bytes each, in the order of the key points.
    pub fn descriptors(&self) -> &[u8] {
        let n = self.header.points as usize;
        let start = HEADER_BYTES + n * KEYPOINT_BYTES;
        &self.map[start..start + n * DESC]
    }
}

#[derive(Clone, Debug)]
struct Entry {
    file: String,
    points: u32,
}

/// The index of a store: the photos by name, and the number of the next feature file.
struct Index {
    next: u64,
    photos: BTreeMap<String, Entry>,
}

impl Index {
    fn to_json(&self) -> Value {
        let photos: Map<String, Value> = self
            .photos
            .iter()
            .map(|(name, e)| (name.clone(), json!({"file": e.file, "points": e.points})))
            .collect();
        json!({"version": VERSION, "next": self.next, "photos": photos})
    }

    fn from_json(value: &Value, path: &Path) -> io::Result<Index> {
        let name = path.display();
        let version = value["version"].as_u64().ok_or_else(|| invalid(format!("{name}: no version")))?;
        if version != u64::from(VERSION) {
            return Err(invalid(format!("{name}: index version {version}, {VERSION} expected")));
        }
        let next = value["next"].as_u64().ok_or_else(|| invalid(format!("{name}: no next file number")))?;
        let listed = value["photos"].as_object().ok_or_else(|| invalid(format!("{name}: no photos")))?;
        let mut photos = BTreeMap::new();
        for (photo, entry) in listed {
            let file = entry["file"].as_str().ok_or_else(|| invalid(format!("{name}: {photo:?} has no file")))?;
            let points = entry["points"].as_u64().ok_or_else(|| invalid(format!("{name}: {photo:?} has no points")))?;
            let points = u32::try_from(points).map_err(|_| invalid(format!("{name}: {photo:?} has {points} points")))?;
            photos.insert(photo.clone(), Entry { file: file.to_owned(), points });
        }
        Ok(Index { next, photos })
    }

    /// The index written to `dir`, whole: a temporary file renamed into place. It is rewritten
    /// after every put, so a step of N photos writes O(N^2) bytes of JSON - tens of megabytes
    /// for a thousand photos, gigabytes for ten thousand; a journal appended to would be the
    /// next step, when a project of that size asks for it.
    fn write(&self, dir: &Path) -> io::Result<()> {
        let bytes = serde_json::to_vec_pretty(&self.to_json()).map_err(|e| invalid(e.to_string()))?;
        write_whole(&dir.join(format!("{INDEX_FILE}.tmp")), &dir.join(INDEX_FILE), &bytes)
    }
}

/// `bytes` written to the new file `path`.
fn write_file(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let describe = |e: io::Error| io::Error::new(e.kind(), format!("{}: {e}", path.display()));
    let mut file = File::create(path).map_err(describe)?;
    file.write_all(bytes).map_err(describe)
}

/// `bytes` written to `tmp`, then renamed to `path`.
fn write_whole(tmp: &Path, path: &Path, bytes: &[u8]) -> io::Result<()> {
    write_file(tmp, bytes)?;
    fs::rename(tmp, path).map_err(|e| io::Error::new(e.kind(), format!("{}: {e}", path.display())))
}

/// A feature store: the folder, and its index under a lock.
pub struct FeatureStore {
    dir: PathBuf,
    index: Mutex<Index>,
}

impl FeatureStore {
    /// A new store in `dir` (created when missing). A folder that holds a store already is
    /// refused (`AlreadyExists`): the caller removes it first when it means to start over.
    pub fn create(dir: &Path) -> io::Result<FeatureStore> {
        fs::create_dir_all(dir)?;
        if dir.join(INDEX_FILE).exists() {
            let what = format!("{}: a feature store is there already", dir.display());
            return Err(io::Error::new(io::ErrorKind::AlreadyExists, what));
        }
        let index = Index { next: 1, photos: BTreeMap::new() };
        index.write(dir)?;
        Ok(FeatureStore { dir: dir.to_path_buf(), index: Mutex::new(index) })
    }

    /// The store in `dir`: its index read, every feature file it names checked (header and
    /// length); an error names the first file that is wrong and what was read in it.
    pub fn open(dir: &Path) -> io::Result<FeatureStore> {
        let path = dir.join(INDEX_FILE);
        let bytes = fs::read(&path).map_err(|e| io::Error::new(e.kind(), format!("{}: {e}", path.display())))?;
        let value: Value = serde_json::from_slice(&bytes).map_err(|e| invalid(format!("{}: {e}", path.display())))?;
        let index = Index::from_json(&value, &path)?;
        for entry in index.photos.values() {
            check_file(&dir.join(&entry.file), entry.points)?;
        }
        Ok(FeatureStore { dir: dir.to_path_buf(), index: Mutex::new(index) })
    }

    /// The folder of the store.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    fn index(&self) -> MutexGuard<'_, Index> {
        self.index.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The features of the photo `name` written to the store, replacing what it held for that
    /// name. Thread-safe: the file is written outside the lock of the index. Every put goes to a
    /// new file: no file is written over (a view may have it mapped), two puts of one name share
    /// no path, and a crash before the index is written leaves an orphan, never a file that
    /// disagrees with the index.
    pub fn put(&self, name: &str, f: &PhotoFeatures) -> io::Result<()> {
        if f.descriptors.len() != f.keypoints.len() * DESC {
            let (points, bytes) = (f.keypoints.len(), f.descriptors.len());
            let what = format!("{name}: {points} key points and {bytes} bytes of descriptors");
            return Err(io::Error::new(io::ErrorKind::InvalidInput, what));
        }
        let header = f.header();
        let file = {
            let mut index = self.index();
            let file = format!("{:08}.{EXTENSION}", index.next);
            index.next += 1;
            file
        };
        let mut bytes = Vec::with_capacity(file_bytes(header.points) as usize);
        bytes.extend_from_slice(&encode_header(&header));
        bytes.extend_from_slice(bytemuck::cast_slice(&f.keypoints));
        bytes.extend_from_slice(&f.descriptors);
        write_file(&self.dir.join(&file), &bytes)?;
        let before = {
            let mut index = self.index();
            let before = index.photos.insert(name.to_owned(), Entry { file, points: header.points });
            index.write(&self.dir)?;
            before
        };
        if let Some(old) = before {
            // the file the name had: gone, or left an orphan when it cannot go yet (mapped, on
            // Windows); `open` ignores files the index does not name
            let _ = fs::remove_file(self.dir.join(&old.file));
        }
        Ok(())
    }

    /// The features of the photo `name`, memory-mapped; `None` when the store has no such photo
    /// (or its file cannot be mapped any more: `open` checked every file when the store was
    /// opened).
    pub fn get(&self, name: &str) -> Option<FeatureView> {
        let entry = self.index().photos.get(name).cloned()?;
        FeatureView::open(&self.dir.join(&entry.file)).ok()
    }

    /// The number of key points of the photo `name` as the index records it, without mapping
    /// its file.
    pub fn points(&self, name: &str) -> Option<u32> {
        self.index().photos.get(name).map(|e| e.points)
    }

    /// The names of the photos of the store, sorted.
    pub fn names(&self) -> Vec<String> {
        self.index().photos.keys().cloned().collect()
    }

    /// How many photos the store holds.
    pub fn len(&self) -> usize {
        self.index().photos.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    /// The photo `name` removed from the store, its file deleted: whether it was there.
    pub fn remove(&self, name: &str) -> io::Result<bool> {
        let entry = {
            let mut index = self.index();
            let entry = index.photos.remove(name);
            if entry.is_some() {
                index.write(&self.dir)?;
            }
            entry
        };
        let Some(entry) = entry else { return Ok(false) };
        match fs::remove_file(self.dir.join(&entry.file)) {
            Ok(()) => Ok(true),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(true),
            Err(e) => Err(e),
        }
    }
}
