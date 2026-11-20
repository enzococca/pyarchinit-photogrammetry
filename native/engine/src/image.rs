//! The photos read as COLMAP reads them, without Python: the raster of a JPEG, PNG or TIFF with
//! the EXIF orientation ignored, as Rec. 709 grey with the weights of `sfm._REC709_BGR` rounded
//! as `cv2.transform` rounds a uint8 result; the size COLMAP reduces a photo to (`colmap_size`)
//! and its triangle filter (`rescale_as_colmap`, the port of `sfm._colmap_taps` and
//! `sfm._rescale_as_colmap`, every sum in f32 in the same order, so that the numpy port and this
//! one give the same bytes); the masks `masks.colmap_mask_folder` writes (`read_mask`). A JPEG is
//! decoded at the smallest DCT scale (1/8, 1/4, 1/2, 1) whose long side reaches the size asked
//! for, a fraction of the work of a full decode when the photo is reduced anyway. What the
//! decoders of this module do not read - 16 bits, CMYK, palettes, planar TIFFs, any other
//! format - is `ReadError::Unsupported`: Python decodes it with OpenCV. A file that is a photo by
//! its header and cannot be decoded is `Corrupt`.

use std::fmt;
use std::fs::File;
use std::io::{self, BufReader, Read, Seek, SeekFrom};
use std::path::Path;

/// A grey image, row-major, one byte per pixel.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Grey {
    pub width: usize,
    pub height: usize,
    pub pixels: Vec<u8>,
}

/// Why a file gave no image.
#[derive(Debug)]
pub enum ReadError {
    /// A format or a feature the decoders of this module do not read: Python decodes it.
    Unsupported(String),
    /// A JPEG, PNG or TIFF by its header that cannot be decoded: truncated or damaged.
    Corrupt(String),
    /// The file could not be opened or read.
    Io(io::Error),
}

impl fmt::Display for ReadError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ReadError::Unsupported(what) => write!(f, "not read here: {what}"),
            ReadError::Corrupt(what) => write!(f, "unreadable: {what}"),
            ReadError::Io(e) => write!(f, "{e}"),
        }
    }
}

impl std::error::Error for ReadError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            ReadError::Io(e) => Some(e),
            _ => None,
        }
    }
}

impl From<io::Error> for ReadError {
    fn from(e: io::Error) -> Self {
        ReadError::Io(e)
    }
}

/// OpenCV's `CV_IO_MAX_IMAGE_PIXELS`: a header announcing more is left to Python, which refuses
/// it the same way, instead of being allocated for.
const MAX_PIXELS: usize = 1 << 30;

/// The bytes a PNG or TIFF decoder may hold for an image within `MAX_PIXELS` (RGBA at 8 bits).
const MAX_BYTES: usize = MAX_PIXELS.saturating_mul(4);

/// The weights of `sfm._REC709_BGR` (COLMAP's `Bitmap::CloneAsGrey`), each rounded to f32 from
/// the Python float as numpy rounds it.
const WEIGHT_B: f32 = 0.0722_f64 as f32;
const WEIGHT_G: f32 = 0.7152_f64 as f32;
const WEIGHT_R: f32 = 0.2126_f64 as f32;

/// COLMAP's grey of one pixel as `cv2.transform(bgr, sfm._REC709_BGR)` computes it on a uint8
/// image: the sum `0.0722 B + 0.7152 G + 0.2126 R` in f32, left to right, then
/// `saturate_cast<uchar>(cvRound(v))`, which rounds half to even.
#[inline]
fn rec709(r: u8, g: u8, b: u8) -> u8 {
    let v = WEIGHT_B * f32::from(b) + WEIGHT_G * f32::from(g) + WEIGHT_R * f32::from(r);
    v.round_ties_even().clamp(0.0, 255.0) as u8
}

/// The 8-bit raster a decoder gave: grey (1 channel), RGB (3) or RGBA (4), interleaved.
struct Raster {
    width: usize,
    height: usize,
    channels: usize,
    data: Vec<u8>,
}

impl Raster {
    fn grey(self) -> Grey {
        let pixels = match self.channels {
            1 => self.data,
            n => self.data.chunks_exact(n).map(|p| rec709(p[0], p[1], p[2])).collect(),
        };
        Grey { width: self.width, height: self.height, pixels }
    }
}

/// The formats read here, told by the first bytes of the file (never by its name).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Format {
    Jpeg,
    Png,
    Tiff,
}

fn format_of(head: &[u8]) -> Option<Format> {
    const TIFF: [&[u8]; 4] = [b"II*\0", b"MM\0*", b"II+\0", b"MM\0+"];
    if head.starts_with(&[0xFF, 0xD8, 0xFF]) {
        Some(Format::Jpeg)
    } else if head.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some(Format::Png)
    } else if TIFF.iter().any(|magic| head.starts_with(magic)) {
        Some(Format::Tiff)
    } else {
        None
    }
}

/// The format of the open file, which is left at its start.
fn sniff(reader: &mut BufReader<File>) -> io::Result<Option<Format>> {
    let mut head = [0u8; 8];
    let mut got = 0;
    while got < head.len() {
        match reader.read(&mut head[got..]) {
            Ok(0) => break,
            Ok(n) => got += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(e),
        }
    }
    reader.seek(SeekFrom::Start(0))?;
    Ok(format_of(&head[..got]))
}

/// An I/O error met inside a decoder: a file that ends before its image is damaged, anything
/// else is the file system's.
fn io_error(e: io::Error, what: &str) -> ReadError {
    if e.kind() == io::ErrorKind::UnexpectedEof {
        ReadError::Corrupt(format!("{what}: the file ends before the image"))
    } else {
        ReadError::Io(e)
    }
}

fn too_large(width: usize, height: usize, what: &str) -> Result<(), ReadError> {
    match width.checked_mul(height) {
        Some(n) if n <= MAX_PIXELS => Ok(()),
        _ => Err(ReadError::Unsupported(format!("{what} of {width} x {height}: more than {MAX_PIXELS} pixels"))),
    }
}

fn jpeg_error(e: jpeg_decoder::Error) -> ReadError {
    match e {
        jpeg_decoder::Error::Unsupported(feature) => ReadError::Unsupported(format!("JPEG: {feature:?}")),
        jpeg_decoder::Error::Format(what) => ReadError::Corrupt(format!("JPEG: {what}")),
        jpeg_decoder::Error::Io(e) => io_error(e, "JPEG"),
        // the decoder's own failure, not the file's: OpenCV may read it
        jpeg_decoder::Error::Internal(e) => ReadError::Unsupported(format!("JPEG: {e}")),
    }
}

/// A JPEG at the smallest DCT scale whose long side reaches `min_long_side` when `dct` (at full
/// size when `min_long_side` is 0, no limit, or `dct` is false), and the size of the full photo.
fn read_jpeg(reader: BufReader<File>, min_long_side: usize, dct: bool) -> Result<(Raster, (usize, usize)), ReadError> {
    let mut decoder = jpeg_decoder::Decoder::new(reader);
    decoder.read_info().map_err(jpeg_error)?;
    let info = decoder.info().expect("the header was read");
    let full = (usize::from(info.width), usize::from(info.height));
    let channels = match info.pixel_format {
        jpeg_decoder::PixelFormat::L8 => 1,
        jpeg_decoder::PixelFormat::RGB24 => 3,
        other => return Err(ReadError::Unsupported(format!("JPEG: {other:?} pixels"))),
    };
    too_large(full.0, full.1, "a JPEG")?;
    let (width, height) = if dct && min_long_side > 0 {
        // the decoder picks the smallest of 1/8, 1/4, 1/2 and 1 at which at least one side
        // reaches what is asked, which is the long side's reaching `min_long_side`
        let wanted = u16::try_from(min_long_side).unwrap_or(u16::MAX);
        let (w, h) = decoder.scale(wanted, wanted).map_err(jpeg_error)?;
        (usize::from(w), usize::from(h))
    } else {
        full
    };
    let data = decoder.decode().map_err(jpeg_error)?;
    if data.len() != width * height * channels {
        return Err(ReadError::Corrupt(format!("JPEG: {} bytes for {width} x {height} x {channels}", data.len())));
    }
    Ok((Raster { width, height, channels, data }, full))
}

fn png_error(e: png::DecodingError) -> ReadError {
    match e {
        png::DecodingError::IoError(e) => io_error(e, "PNG"),
        png::DecodingError::Format(what) => ReadError::Corrupt(format!("PNG: {what}")),
        png::DecodingError::Parameter(what) => ReadError::Corrupt(format!("PNG: {what}")),
        png::DecodingError::LimitsExceeded => ReadError::Unsupported("PNG: too large for this reader".to_owned()),
    }
}

/// An 8-bit PNG, grey, RGB or RGBA, as stored (a palette, a grey with alpha, fewer or more bits
/// are left to Python).
fn read_png(reader: BufReader<File>) -> Result<Raster, ReadError> {
    let mut decoder = png::Decoder::new(reader);
    decoder.set_limits(png::Limits { bytes: MAX_BYTES });
    decoder.set_transformations(png::Transformations::IDENTITY);
    decoder.set_ignore_text_chunk(true);
    let mut png = decoder.read_info().map_err(png_error)?;
    let info = png.info();
    let (width, height) = (info.width as usize, info.height as usize);
    if info.bit_depth != png::BitDepth::Eight {
        return Err(ReadError::Unsupported(format!("PNG: {} bits", info.bit_depth as u8)));
    }
    let channels = match info.color_type {
        png::ColorType::Grayscale => 1,
        png::ColorType::Rgb => 3,
        png::ColorType::Rgba => 4,
        other => return Err(ReadError::Unsupported(format!("PNG: {other:?}"))),
    };
    too_large(width, height, "a PNG")?;
    let too_large_for_png = || ReadError::Unsupported("PNG: too large for this reader".to_owned());
    let size = png.output_buffer_size().ok_or_else(too_large_for_png)?;
    let mut data = vec![0u8; size];
    let frame = png.next_frame(&mut data).map_err(png_error)?;
    data.truncate(frame.line_size * frame.height as usize);
    if data.len() != width * height * channels {
        return Err(ReadError::Corrupt(format!("PNG: {} bytes for {width} x {height} x {channels}", data.len())));
    }
    Ok(Raster { width, height, channels, data })
}

fn tiff_error(e: tiff::TiffError) -> ReadError {
    match e {
        tiff::TiffError::FormatError(what) => ReadError::Corrupt(format!("TIFF: {what}")),
        tiff::TiffError::UnsupportedError(what) => ReadError::Unsupported(format!("TIFF: {what}")),
        tiff::TiffError::IoError(e) => io_error(e, "TIFF"),
        tiff::TiffError::LimitsExceeded | tiff::TiffError::IntSizeError => {
            ReadError::Unsupported("TIFF: too large for this reader".to_owned())
        }
        tiff::TiffError::UsageError(what) => ReadError::Corrupt(format!("TIFF: {what}")),
    }
}

/// The first image of an 8-bit TIFF, grey, RGB or RGBA with the samples interleaved (a planar
/// file, CMYK, YCbCr, a palette, other depths are left to Python).
fn read_tiff(reader: BufReader<File>) -> Result<Raster, ReadError> {
    let mut limits = tiff::decoder::Limits::default();
    limits.decoding_buffer_size = MAX_BYTES;
    limits.intermediate_buffer_size = MAX_BYTES;
    let mut decoder = tiff::decoder::Decoder::new(reader).map_err(tiff_error)?.with_limits(limits);
    let (w, h) = decoder.dimensions().map_err(tiff_error)?;
    let (width, height) = (w as usize, h as usize);
    let channels = match decoder.colortype().map_err(tiff_error)? {
        tiff::ColorType::Gray(8) => 1,
        tiff::ColorType::RGB(8) => 3,
        tiff::ColorType::RGBA(8) => 4,
        other => return Err(ReadError::Unsupported(format!("TIFF: {other:?}"))),
    };
    let planar = decoder.find_tag_unsigned::<u16>(tiff::tags::Tag::PlanarConfiguration).map_err(tiff_error)?;
    if planar == Some(2) {
        return Err(ReadError::Unsupported("TIFF: planar samples".to_owned()));
    }
    too_large(width, height, "a TIFF")?;
    let data = match decoder.read_image().map_err(tiff_error)? {
        tiff::decoder::DecodingResult::U8(data) => data,
        other => return Err(ReadError::Unsupported(format!("TIFF: samples of {}", sample_kind(&other)))),
    };
    if data.len() != width * height * channels {
        return Err(ReadError::Corrupt(format!("TIFF: {} bytes for {width} x {height} x {channels}", data.len())));
    }
    Ok(Raster { width, height, channels, data })
}

fn sample_kind(result: &tiff::decoder::DecodingResult) -> &'static str {
    use tiff::decoder::DecodingResult::*;
    match result {
        U8(_) => "8 bits",
        U16(_) | I16(_) | F16(_) => "16 bits",
        U32(_) | I32(_) | F32(_) => "32 bits",
        U64(_) | I64(_) | F64(_) => "64 bits",
        I8(_) => "signed 8 bits",
    }
}

/// The photo at `path` as COLMAP's grey, and the size of the full photo. A JPEG is decoded at the
/// smallest DCT scale (1/8, 1/4, 1/2, 1) whose long side is at least `min_long_side` when `dct`
/// (full size when `dct` is false or `min_long_side` is 0, which means no limit as in
/// `_extract_features_gpu.load`); a PNG or TIFF always at full size. The raster is the file's,
/// with the EXIF orientation ignored as COLMAP ignores it. `Unsupported` is for Python to decode,
/// `Corrupt` is a photo nobody reads.
pub fn read_grey(path: &Path, min_long_side: usize, dct: bool) -> Result<(Grey, (usize, usize)), ReadError> {
    let mut reader = BufReader::new(File::open(path)?);
    match sniff(&mut reader)? {
        Some(Format::Jpeg) => read_jpeg(reader, min_long_side, dct).map(|(raster, full)| (raster.grey(), full)),
        Some(Format::Png) => read_png(reader).map(whole),
        Some(Format::Tiff) => read_tiff(reader).map(whole),
        None => Err(ReadError::Unsupported("not a JPEG, PNG or TIFF".to_owned())),
    }
}

/// A raster read at full size, with that size.
fn whole(raster: Raster) -> (Grey, (usize, usize)) {
    let full = (raster.width, raster.height);
    (raster.grey(), full)
}

/// A mask as `masks.colmap_mask_folder` writes it: an 8-bit grey PNG, a pixel above 0 keeping
/// the key points on it. Anything else is `Unsupported`.
pub fn read_mask(path: &Path) -> Result<Grey, ReadError> {
    let mut reader = BufReader::new(File::open(path)?);
    if sniff(&mut reader)? != Some(Format::Png) {
        return Err(ReadError::Unsupported("a mask is an 8-bit grey PNG".to_owned()));
    }
    let raster = read_png(reader)?;
    if raster.channels != 1 {
        let channels = raster.channels;
        return Err(ReadError::Unsupported(format!("a mask is an 8-bit grey PNG, not one of {channels} channels")));
    }
    Ok(raster.grey())
}

/// The size COLMAP reduces a photo of `w` x `h` to under `max_image_size = limit`, as
/// `_extract_features_gpu.load` computes it: the photo as it is when its long side is within
/// `limit` (or `limit` is 0, no limit), else both sides scaled by `limit / long side`, each
/// `max(floor(x * f + 0.5), 8)`.
pub fn colmap_size(w: usize, h: usize, limit: usize) -> (usize, usize) {
    let long = w.max(h);
    if limit == 0 || long <= limit {
        return (w, h);
    }
    let f = limit as f64 / long as f64;
    let side = |x: usize| ((x as f64 * f + 0.5).floor() as usize).max(8);
    (side(w), side(h))
}

/// The source pixels and weights of every destination pixel along one axis: OpenImageIO's
/// `resize` with the triangle filter (what COLMAP reduces the photos with), a triangle as wide
/// as the step centred on the destination pixel, normalised. The port of `sfm._colmap_taps`:
/// every operation in f32, in its order, so that the weights are the same bytes.
struct Taps {
    n: usize,
    index: Vec<usize>,
    weight: Vec<f32>,
}

impl Taps {
    fn new(dst: usize, src: usize) -> Taps {
        let ratio = dst as f32 / src as f32;
        let rad = (1.0f32 / ratio).ceil() as i64;
        let n = (2 * rad + 1) as usize;
        let inverse = 1.0f32 / dst as f32;
        let last = src as i64 - 1;
        let mut index = Vec::with_capacity(dst * n);
        let mut weight = Vec::with_capacity(dst * n);
        for x in 0..dst {
            let src_f = ((x as f32 + 0.5) * inverse) * src as f32;
            let src_i = src_f.floor() as i64;
            let frac = src_f - src_i as f32;
            let first = weight.len();
            for i in 0..n as i64 {
                let arg = ratio * ((i - rad) as f32 - (frac - 0.5));
                let distance = arg.abs();
                weight.push(if distance < 1.0 { 1.0 - distance } else { 0.0 });
                index.push((src_i - rad + i).clamp(0, last) as usize);
            }
            let mut total = weight[first];
            for &w in &weight[first + 1..] {
                total += w;
            }
            for w in &mut weight[first..] {
                *w /= total;
            }
        }
        Taps { n, index, weight }
    }

    fn at(&self, x: usize) -> (&[usize], &[f32]) {
        (&self.index[x * self.n..(x + 1) * self.n], &self.weight[x * self.n..(x + 1) * self.n])
    }
}

/// A grey image reduced to `w` x `h` as COLMAP reduces a photo before extracting its features:
/// OpenImageIO's `resize` with the triangle filter, the port of `sfm._rescale_as_colmap` - the
/// pixels scaled to [0, 1] in f32, the taps of the rows outside, of the columns inside, every
/// product and sum in f32 in that order, the result `floor(v * 255 + 0.5)` clipped to a byte -
/// so that the two ports give the same bytes. The same size gives a copy.
pub fn rescale_as_colmap(img: &Grey, w: usize, h: usize) -> Grey {
    if (w, h) == (img.width, img.height) {
        return img.clone();
    }
    if w == 0 || h == 0 || img.width == 0 || img.height == 0 {
        return Grey { width: w, height: h, pixels: vec![0; w * h] };
    }
    let scale = (1.0f64 / 255.0) as f32;
    let unit: Vec<f32> = (0..=255).map(|p| p as f32 * scale).collect();
    let columns = Taps::new(w, img.width);
    let rows = Taps::new(h, img.height);
    let mut pixels = Vec::with_capacity(w * h);
    for y in 0..h {
        let (row_index, row_weight) = rows.at(y);
        for x in 0..w {
            let (column_index, column_weight) = columns.at(x);
            let mut sum = 0.0f32;
            for (&sy, &wy) in row_index.iter().zip(row_weight) {
                let source = &img.pixels[sy * img.width..(sy + 1) * img.width];
                for (&sx, &wx) in column_index.iter().zip(column_weight) {
                    sum += (wy * wx) * unit[usize::from(source[sx])];
                }
            }
            pixels.push((sum * 255.0 + 0.5).floor().clamp(0.0, 255.0) as u8);
        }
    }
    Grey { width: w, height: h, pixels }
}
