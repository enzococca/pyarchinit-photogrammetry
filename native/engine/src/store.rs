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
//!
//! The match store ([`MatchStore`]) keeps the result of every pair the matching step did - its
//! matches, its verification and the route it took - in a few files with an index, in the same
//! folder style: `index.json` (`{"version": 1, "next": N, "segments": [F, ...]}`) names segment
//! files `00000001.ppgm`, each a header of 16 bytes (the magic `PPGM`, the version 1, the number
//! of pairs, the bytes of the table of contents), a table of contents (the two names of each
//! pair, the offset and length of its record) and the records. `put` buffers the records; a
//! buffer of [`SEGMENT_BYTES`] or more is sealed into a new segment file written to a temporary
//! name and renamed into place, then the index is rewritten the same way; [`MatchStore::flush`]
//! seals what is buffered and dropping the store does too. A segment in place whose index write
//! failed is remembered: the next `flush` (or seal) writes the index again, or returns that error,
//! so that `flush` never says "on disk" of a segment the index does not name. A pair put again
//! replaces its result (the latest record wins, within a segment and across them). `open` reads
//! the header and the table of every segment the index names and refuses the store at the first
//! whose magic, version, table or record bounds are wrong, naming the file and what was read.

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, MutexGuard, PoisonError};

use memmap2::Mmap;
use nalgebra::{Matrix3, Quaternion, UnitQuaternion, Vector3};
use serde_json::{json, Map, Value};

use crate::geometry::two_view::{Config, TwoView};
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

/// The codes of the system's error for a full disk: ENOSPC (28 on Linux and macOS), and
/// ERROR_HANDLE_DISK_FULL (39) and ERROR_DISK_FULL (112) on Windows.
#[cfg(not(windows))]
const DISK_FULL_CODES: &[i32] = &[28];
#[cfg(windows)]
const DISK_FULL_CODES: &[i32] = &[39, 112];

/// Whether the text of an error of the engine says that the disk is full: it carries the code of
/// a full disk as an `io::Error` of the system words it (`... (os error 28)`), which the errors of
/// the stores keep through the words the steps add (the steps carry their errors as text).
pub fn storage_full(text: &str) -> bool {
    DISK_FULL_CODES.iter().any(|code| text.contains(&format!("(os error {code})")))
}

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

// ---------------------------------------------------------------------------------------------
// The match store

/// The first four bytes of a segment file of the match store.
pub const MATCH_MAGIC: [u8; 4] = *b"PPGM";
/// The version of the segment files and of the index of the match store.
pub const MATCH_VERSION: u32 = 1;
/// The extension of a segment file.
pub const MATCH_EXTENSION: &str = "ppgm";
/// The buffered records at which `put` seals a segment: a few hundred pairs of a large project
/// (a pair of two thousand matches and their inliers is about 30 KB), so that a step of ten
/// thousand pairs is a few dozen files and a crash loses at most the pairs of one buffer.
pub const SEGMENT_BYTES: usize = 8 << 20;

const SEGMENT_HEADER: usize = 16;
const S_MAGIC: usize = 0;
const S_VERSION: usize = 4;
const S_COUNT: usize = 8;
const S_TOC_BYTES: usize = 12;
/// The fewest bytes of an entry of the table of contents: two empty names, the offset, the length.
const LEAST_TOC_ENTRY: usize = 20;

// a record: the route, the configuration, the counts, the flags of the optional parts, a reserved
// word, then the matches, the inliers (8 bytes each), the models (9 f64 each) and the pose (7 f64)
const RECORD_HEADER: usize = 24;
const R_ROUTE: usize = 0;
const R_CONFIG: usize = 4;
const R_MATCHES: usize = 8;
const R_INLIERS: usize = 12;
const R_FLAGS: usize = 16;
const HAS_E: u32 = 1;
const HAS_F: u32 = 2;
const HAS_H: u32 = 4;
const HAS_POSE: u32 = 8;
const MATRIX_BYTES: usize = 9 * 8;
const POSE_BYTES: usize = 7 * 8;

/// How the matches of a pair were found.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Route {
    /// The first stage (the largest scales) was trusted and the second matched along the
    /// epipolar lines.
    Guided,
    /// The first stage was not trusted: every point matched against every point.
    BruteForce,
    /// The first stage found too little and the pair was not preselected: nothing matched.
    Dropped,
    /// Read from a COLMAP database (the matches of the classic step).
    Imported,
}

impl Route {
    fn code(self) -> u32 {
        match self {
            Route::Guided => 0,
            Route::BruteForce => 1,
            Route::Dropped => 2,
            Route::Imported => 3,
        }
    }

    fn from_code(code: u32) -> Option<Route> {
        [Route::Guided, Route::BruteForce, Route::Dropped, Route::Imported].into_iter().find(|r| r.code() == code)
    }

    /// The word of the route, as a report names it.
    pub fn name(self) -> &'static str {
        match self {
            Route::Guided => "guided",
            Route::BruteForce => "brute_force",
            Route::Dropped => "dropped",
            Route::Imported => "imported",
        }
    }
}

/// What the matching of a pair gave: the matches that went to the verification (pairs of key
/// point indices into the first and the second photo, capped to the step's `max_num_matches`),
/// the verification itself (`two_view::verify`: the configuration, the inliers among the
/// matches, the models) and the route taken.
#[derive(Clone, Debug, PartialEq)]
pub struct PairResult {
    pub matches: Vec<[u32; 2]>,
    pub two_view: TwoView,
    pub route: Route,
}

fn put_u64(buf: &mut Vec<u8>, v: u64) {
    buf.extend_from_slice(&v.to_le_bytes());
}

fn put_f64(buf: &mut Vec<u8>, v: f64) {
    buf.extend_from_slice(&v.to_le_bytes());
}

fn get_u64(buf: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(buf[at..at + 8].try_into().expect("eight bytes"))
}

fn get_f64(buf: &[u8], at: usize) -> f64 {
    f64::from_le_bytes(buf[at..at + 8].try_into().expect("eight bytes"))
}

fn put_matrix(buf: &mut Vec<u8>, m: &Matrix3<f64>) {
    for row in 0..3 {
        for col in 0..3 {
            put_f64(buf, m[(row, col)]);
        }
    }
}

fn get_matrix(buf: &[u8], at: usize) -> Matrix3<f64> {
    Matrix3::from_fn(|row, col| get_f64(buf, at + (row * 3 + col) * 8))
}

/// The bytes of a pair result.
fn encode_result(m: &PairResult) -> Vec<u8> {
    let tv = &m.two_view;
    let flags = if tv.e.is_some() { HAS_E } else { 0 }
        | if tv.f.is_some() { HAS_F } else { 0 }
        | if tv.h.is_some() { HAS_H } else { 0 }
        | if tv.cam2_from_cam1.is_some() { HAS_POSE } else { 0 };
    let most = RECORD_HEADER + (m.matches.len() + tv.inliers.len()) * 8 + 3 * MATRIX_BYTES + POSE_BYTES;
    let mut buf = Vec::with_capacity(most);
    buf.extend_from_slice(&m.route.code().to_le_bytes());
    buf.extend_from_slice(&(tv.config as u32).to_le_bytes());
    buf.extend_from_slice(&(m.matches.len() as u32).to_le_bytes());
    buf.extend_from_slice(&(tv.inliers.len() as u32).to_le_bytes());
    buf.extend_from_slice(&flags.to_le_bytes());
    buf.extend_from_slice(&0u32.to_le_bytes());
    buf.extend_from_slice(bytemuck::cast_slice(&m.matches));
    buf.extend_from_slice(bytemuck::cast_slice(&tv.inliers));
    for model in [&tv.e, &tv.f, &tv.h].into_iter().flatten() {
        put_matrix(&mut buf, model);
    }
    if let Some((q, t)) = &tv.cam2_from_cam1 {
        for v in [q.w, q.i, q.j, q.k, t.x, t.y, t.z] {
            put_f64(&mut buf, v);
        }
    }
    buf
}

/// The pair result of the bytes `buf`; `what` names the record in an error.
fn decode_result(buf: &[u8], what: &str) -> io::Result<PairResult> {
    if buf.len() < RECORD_HEADER {
        return Err(invalid(format!("{what}: {} bytes, no record header of {RECORD_HEADER}", buf.len())));
    }
    let route = get_u32(buf, R_ROUTE);
    let route = Route::from_code(route).ok_or_else(|| invalid(format!("{what}: route {route} unknown")))?;
    let config = get_u32(buf, R_CONFIG);
    let config = Config::from_u32(config).ok_or_else(|| invalid(format!("{what}: configuration {config} unknown")))?;
    let n_matches = get_u32(buf, R_MATCHES) as usize;
    let n_inliers = get_u32(buf, R_INLIERS) as usize;
    let flags = get_u32(buf, R_FLAGS);
    let models = [HAS_E, HAS_F, HAS_H].iter().filter(|&&f| flags & f != 0).count();
    let expected = RECORD_HEADER + (n_matches + n_inliers) * 8 + models * MATRIX_BYTES
        + if flags & HAS_POSE != 0 { POSE_BYTES } else { 0 };
    if buf.len() != expected {
        return Err(invalid(format!("{what}: {} bytes, {expected} expected for its counts", buf.len())));
    }
    let mut at = RECORD_HEADER;
    let pairs = |at: usize, n: usize| -> Vec<[u32; 2]> {
        buf[at..at + n * 8].chunks_exact(8).map(|c| [get_u32(c, 0), get_u32(c, 4)]).collect()
    };
    let matches = pairs(at, n_matches);
    at += n_matches * 8;
    let inliers = pairs(at, n_inliers);
    at += n_inliers * 8;
    let mut model = |flag: u32| -> Option<Matrix3<f64>> {
        if flags & flag == 0 {
            return None;
        }
        let m = get_matrix(buf, at);
        at += MATRIX_BYTES;
        Some(m)
    };
    let (e, f, h) = (model(HAS_E), model(HAS_F), model(HAS_H));
    let cam2_from_cam1 = (flags & HAS_POSE != 0).then(|| {
        let v: Vec<f64> = (0..7).map(|k| get_f64(buf, at + k * 8)).collect();
        // the quaternion as written (unit already): no renormalisation, so a result read back is
        // the one written, bit for bit
        (UnitQuaternion::new_unchecked(Quaternion::new(v[0], v[1], v[2], v[3])), Vector3::new(v[4], v[5], v[6]))
    });
    Ok(PairResult { matches, two_view: TwoView { config, inliers, e, f, h, cam2_from_cam1 }, route })
}

/// An entry of the table of contents of a segment: the pair and where its record is in the file.
#[derive(Clone, Debug)]
struct TocEntry {
    a: String,
    b: String,
    offset: u64,
    bytes: u32,
}

fn toc_entry_bytes(e: &TocEntry) -> usize {
    4 + e.a.len() + 4 + e.b.len() + 8 + 4
}

fn encode_toc(entries: &[TocEntry]) -> Vec<u8> {
    let mut buf = Vec::with_capacity(entries.iter().map(toc_entry_bytes).sum());
    for e in entries {
        buf.extend_from_slice(&(e.a.len() as u32).to_le_bytes());
        buf.extend_from_slice(e.a.as_bytes());
        buf.extend_from_slice(&(e.b.len() as u32).to_le_bytes());
        buf.extend_from_slice(e.b.as_bytes());
        put_u64(&mut buf, e.offset);
        buf.extend_from_slice(&e.bytes.to_le_bytes());
    }
    buf
}

/// The table of contents of a segment out of its bytes: `count` entries, each record after the
/// table and within `file_len`. A count the table cannot hold is refused before anything is
/// allocated for it.
fn decode_toc(buf: &[u8], count: usize, file_len: u64, path: &Path) -> io::Result<Vec<TocEntry>> {
    let name = path.display();
    if count.checked_mul(LEAST_TOC_ENTRY).is_none_or(|least| least > buf.len()) {
        return Err(invalid(format!("{name}: {count} pairs in a table of contents of {} bytes", buf.len())));
    }
    let records_from = (SEGMENT_HEADER + buf.len()) as u64;
    let mut at = 0;
    let mut entries = Vec::with_capacity(count);
    let text = |at: usize, len: usize| -> io::Result<String> {
        let short = || invalid(format!("{name}: the table of contents is cut short"));
        let bytes = buf.get(at..at + len).ok_or_else(short)?;
        String::from_utf8(bytes.to_vec()).map_err(|_| invalid(format!("{name}: a photo name is not UTF-8")))
    };
    let word = |at: usize| -> io::Result<u32> {
        buf.get(at..at + 4)
            .map(|b| get_u32(b, 0))
            .ok_or_else(|| invalid(format!("{name}: the table of contents is cut short")))
    };
    for _ in 0..count {
        let len_a = word(at)? as usize;
        let a = text(at + 4, len_a)?;
        at += 4 + len_a;
        let len_b = word(at)? as usize;
        let b = text(at + 4, len_b)?;
        at += 4 + len_b;
        let offset = buf
            .get(at..at + 8)
            .map(|b| get_u64(b, 0))
            .ok_or_else(|| invalid(format!("{name}: the table of contents is cut short")))?;
        let bytes = word(at + 8)?;
        at += 12;
        let end = offset.checked_add(u64::from(bytes));
        if offset < records_from || end.is_none_or(|end| end > file_len) {
            let what = format!("{name}: the record of ({a:?}, {b:?}) at {offset} of {bytes} bytes is outside the \
                                records of the file ({records_from} to {file_len})");
            return Err(invalid(what));
        }
        entries.push(TocEntry { a, b, offset, bytes });
    }
    if at != buf.len() {
        return Err(invalid(format!("{name}: {} bytes after the table of contents", buf.len() - at)));
    }
    Ok(entries)
}

/// The header and the table of contents of the segment `path`, checked: the magic, the version,
/// the length of the table and every record within the file.
fn read_toc(path: &Path) -> io::Result<Vec<TocEntry>> {
    let describe = |e: io::Error| io::Error::new(e.kind(), format!("{}: {e}", path.display()));
    let name = path.display();
    let mut file = File::open(path).map_err(describe)?;
    let file_len = file.metadata().map_err(describe)?.len();
    let mut header = [0u8; SEGMENT_HEADER];
    let mut got = 0;
    while got < SEGMENT_HEADER {
        match file.read(&mut header[got..]) {
            Ok(0) => break,
            Ok(n) => got += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(describe(e)),
        }
    }
    if got < SEGMENT_HEADER {
        return Err(invalid(format!("{name}: {got} bytes, no header of {SEGMENT_HEADER}")));
    }
    if header[S_MAGIC..S_MAGIC + 4] != MATCH_MAGIC {
        let read = String::from_utf8_lossy(&header[S_MAGIC..S_MAGIC + 4]).into_owned();
        return Err(invalid(format!("{name}: magic {read:?} read, PPGM expected: not a segment of a match store")));
    }
    let version = get_u32(&header, S_VERSION);
    if version != MATCH_VERSION {
        return Err(invalid(format!("{name}: segment version {version} read, {MATCH_VERSION} expected")));
    }
    let count = get_u32(&header, S_COUNT) as usize;
    let toc_bytes = get_u32(&header, S_TOC_BYTES) as usize;
    if (SEGMENT_HEADER + toc_bytes) as u64 > file_len {
        let what = format!("{name}: a table of contents of {toc_bytes} bytes in a file of {file_len}");
        return Err(invalid(what));
    }
    let mut toc = vec![0u8; toc_bytes];
    file.read_exact(&mut toc).map_err(describe)?;
    decode_toc(&toc, count, file_len, path)
}

/// Where the record of a pair is: in the segment `segment` of the index (an index into its
/// list), or still buffered (`None`), at `offset` (from the start of the file, or of the
/// buffer) for `bytes`.
#[derive(Clone, Copy, Debug)]
struct Located {
    segment: Option<usize>,
    offset: u64,
    bytes: u32,
}

/// The index of a match store and the records not yet sealed into a segment.
struct MatchIndex {
    next: u64,
    segments: Vec<String>,
    pairs: BTreeMap<(String, String), Located>,
    /// The entries of the segment being filled, their offsets into `data`.
    pending: Vec<TocEntry>,
    data: Vec<u8>,
    /// The index on disk does not name every segment in place: its last write failed, and the
    /// next seal writes it again whether or not anything is buffered.
    index_stale: bool,
}

impl MatchIndex {
    fn to_json(&self) -> Value {
        json!({"version": MATCH_VERSION, "next": self.next, "segments": self.segments})
    }

    fn from_json(value: &Value, path: &Path) -> io::Result<MatchIndex> {
        let name = path.display();
        let version = value["version"].as_u64().ok_or_else(|| invalid(format!("{name}: no version")))?;
        if version != u64::from(MATCH_VERSION) {
            return Err(invalid(format!("{name}: index version {version}, {MATCH_VERSION} expected")));
        }
        let next = value["next"].as_u64().ok_or_else(|| invalid(format!("{name}: no next file number")))?;
        let listed = value["segments"].as_array().ok_or_else(|| invalid(format!("{name}: no segments")))?;
        let segments = listed
            .iter()
            .map(|v| v.as_str().map(str::to_owned).ok_or_else(|| invalid(format!("{name}: a segment without a name"))))
            .collect::<io::Result<Vec<String>>>()?;
        Ok(MatchIndex { next, segments, pairs: BTreeMap::new(), pending: Vec::new(), data: Vec::new(),
                        index_stale: false })
    }

    /// The index written to `dir`, whole: a temporary file renamed into place.
    fn write(&self, dir: &Path) -> io::Result<()> {
        let bytes = serde_json::to_vec_pretty(&self.to_json()).map_err(|e| invalid(e.to_string()))?;
        write_whole(&dir.join(format!("{INDEX_FILE}.tmp")), &dir.join(INDEX_FILE), &bytes)
    }

    /// The index written, and remembered as stale when the write failed, so that the next seal
    /// writes it again.
    fn write_index(&mut self, dir: &Path) -> io::Result<()> {
        let written = self.write(dir);
        self.index_stale = written.is_err();
        written
    }

    /// The buffered records sealed into a new segment file of `dir` (written whole to a
    /// temporary name, renamed into place), the index rewritten; with nothing buffered, only the
    /// index when its last write failed, else nothing.
    fn seal(&mut self, dir: &Path) -> io::Result<()> {
        if self.pending.is_empty() {
            return if self.index_stale { self.write_index(dir) } else { Ok(()) };
        }
        let file = format!("{:08}.{MATCH_EXTENSION}", self.next);
        let toc_bytes: usize = self.pending.iter().map(toc_entry_bytes).sum();
        let base = (SEGMENT_HEADER + toc_bytes) as u64;
        let entries: Vec<TocEntry> =
            self.pending.iter().map(|e| TocEntry { offset: e.offset + base, ..e.clone() }).collect();
        let mut bytes = Vec::with_capacity(base as usize + self.data.len());
        bytes.extend_from_slice(&MATCH_MAGIC);
        bytes.extend_from_slice(&MATCH_VERSION.to_le_bytes());
        bytes.extend_from_slice(&(entries.len() as u32).to_le_bytes());
        bytes.extend_from_slice(&(toc_bytes as u32).to_le_bytes());
        bytes.extend_from_slice(&encode_toc(&entries));
        bytes.extend_from_slice(&self.data);
        write_whole(&dir.join(format!("{file}.tmp")), &dir.join(&file), &bytes)?;
        // the file is in place: the index knows it from now on, on disk once written below (a
        // failure there leaves the file an orphan, and the index stale, until the next seal
        // writes the index and names it)
        self.next += 1;
        self.segments.push(file);
        let segment = self.segments.len() - 1;
        for e in &entries {
            let located = Located { segment: Some(segment), offset: e.offset, bytes: e.bytes };
            self.pairs.insert((e.a.clone(), e.b.clone()), located);
        }
        self.pending.clear();
        self.data.clear();
        self.write_index(dir)
    }
}

/// A match store: the folder, and its index and buffered records under a lock.
pub struct MatchStore {
    dir: PathBuf,
    index: Mutex<MatchIndex>,
}

impl MatchStore {
    /// A new store in `dir` (created when missing). A folder that holds a store already is
    /// refused (`AlreadyExists`): the caller removes it first when it means to start over.
    pub fn create(dir: &Path) -> io::Result<MatchStore> {
        fs::create_dir_all(dir)?;
        if dir.join(INDEX_FILE).exists() {
            let what = format!("{}: a match store is there already", dir.display());
            return Err(io::Error::new(io::ErrorKind::AlreadyExists, what));
        }
        let index = MatchIndex { next: 1, segments: Vec::new(), pairs: BTreeMap::new(), pending: Vec::new(),
                                 data: Vec::new(), index_stale: false };
        index.write(dir)?;
        Ok(MatchStore { dir: dir.to_path_buf(), index: Mutex::new(index) })
    }

    /// The store in `dir`: its index read, the header and the table of contents of every segment
    /// it names checked; an error names the first file that is wrong and what was read in it.
    pub fn open(dir: &Path) -> io::Result<MatchStore> {
        let path = dir.join(INDEX_FILE);
        let bytes = fs::read(&path).map_err(|e| io::Error::new(e.kind(), format!("{}: {e}", path.display())))?;
        let value: Value = serde_json::from_slice(&bytes).map_err(|e| invalid(format!("{}: {e}", path.display())))?;
        let mut index = MatchIndex::from_json(&value, &path)?;
        for (segment, file) in index.segments.iter().enumerate() {
            for e in read_toc(&dir.join(file))? {
                // in the order of the files and of the entries: the latest record of a pair wins
                index.pairs.insert((e.a, e.b), Located { segment: Some(segment), offset: e.offset, bytes: e.bytes });
            }
        }
        Ok(MatchStore { dir: dir.to_path_buf(), index: Mutex::new(index) })
    }

    /// The folder of the store.
    pub fn dir(&self) -> &Path {
        &self.dir
    }

    fn index(&self) -> MutexGuard<'_, MatchIndex> {
        self.index.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// The result of the pair `(a, b)` - the two names in that order; the other order is another
    /// pair - put into the store, replacing what it held for the pair. Thread-safe. The record is
    /// buffered; a buffer of [`SEGMENT_BYTES`] or more is sealed into a segment file at once
    /// (the error of that write is this call's).
    pub fn put(&self, a: &str, b: &str, m: &PairResult) -> io::Result<()> {
        let record = encode_result(m);
        let mut index = self.index();
        let offset = index.data.len() as u64;
        let too_long = |_| {
            io::Error::new(io::ErrorKind::InvalidInput, format!("({a}, {b}): a record of {} bytes", record.len()))
        };
        let bytes = u32::try_from(record.len()).map_err(too_long)?;
        index.data.extend_from_slice(&record);
        index.pending.push(TocEntry { a: a.to_owned(), b: b.to_owned(), offset, bytes });
        index.pairs.insert((a.to_owned(), b.to_owned()), Located { segment: None, offset, bytes });
        if index.data.len() >= SEGMENT_BYTES {
            index.seal(&self.dir)?;
        }
        Ok(())
    }

    /// The buffered records sealed into a segment file and the index rewritten (again, when its
    /// last write failed): everything put so far is on disk and named by the index once this
    /// returns `Ok`. Dropping the store does the same, without a word on a failure.
    pub fn flush(&self) -> io::Result<()> {
        self.index().seal(&self.dir)
    }

    /// The result of the pair `(a, b)`, as put; `None` when the store has no such pair (or its
    /// record cannot be read any more: `open` checked every table of contents against its file).
    pub fn get(&self, a: &str, b: &str) -> Option<PairResult> {
        let index = self.index();
        let located = *index.pairs.get(&(a.to_owned(), b.to_owned()))?;
        let what = format!("({a}, {b})");
        let (offset, bytes) = (located.offset as usize, located.bytes as usize);
        match located.segment {
            None => decode_result(&index.data[offset..offset + bytes], &what).ok(),
            Some(segment) => {
                let path = self.dir.join(&index.segments[segment]);
                let mut file = File::open(&path).ok()?;
                file.seek(SeekFrom::Start(located.offset)).ok()?;
                let mut buf = vec![0u8; bytes];
                file.read_exact(&mut buf).ok()?;
                decode_result(&buf, &format!("{}: {what}", path.display())).ok()
            }
        }
    }

    /// The pairs of the store, each as its two names in the order they were put, sorted.
    pub fn pairs(&self) -> Vec<(String, String)> {
        self.index().pairs.keys().cloned().collect()
    }

    /// How many pairs the store holds.
    pub fn len(&self) -> usize {
        self.index().pairs.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

impl Drop for MatchStore {
    fn drop(&mut self) {
        let _ = self.flush();
    }
}
