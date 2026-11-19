//! The photos read without Python (`image`): one pattern written as PNG and TIFF gives one grey,
//! with the alpha dropped and a grey file read as it is; the Rec. 709 weights in the order of the
//! channels; the size COLMAP reduces to; the DCT scale a JPEG is decoded at; a truncated JPEG, a
//! GIF, a 16-bit PNG and a missing file told apart; the masks; the triangle filter on a row worked
//! by hand. The fixtures are written by the tests themselves under cargo's temporary folder of
//! the target (`CARGO_TARGET_TMPDIR`): nothing binary is committed.

use std::fs;
use std::io::BufWriter;
use std::path::{Path, PathBuf};

use ppg_engine::image::{colmap_size, read_grey, read_mask, rescale_as_colmap, Grey, ReadError};

/// The odd sizes of the pattern, on purpose.
const W: usize = 1203;
const H: usize = 805;

/// Knuth's MMIX linear congruential generator, as in the other tests of this crate.
struct Lcg(u64);

impl Lcg {
    fn byte(&mut self) -> u8 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 56) as u8
    }
}

/// An RGB pattern: red a gradient along the row, green along the column, blue the noise of the
/// generator; the first four pixels pure red, green, blue and white, to pin the weights and the
/// order of the channels.
fn pattern(w: usize, h: usize) -> Vec<u8> {
    let mut random = Lcg(7);
    let mut rgb = Vec::with_capacity(w * h * 3);
    for y in 0..h {
        for x in 0..w {
            rgb.push((x * 255 / (w - 1)) as u8);
            rgb.push((y * 255 / (h - 1)) as u8);
            rgb.push(random.byte());
        }
    }
    for (i, pixel) in [[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]].iter().enumerate() {
        rgb[i * 3..i * 3 + 3].copy_from_slice(pixel);
    }
    rgb
}

/// A smooth RGB pattern (three gradients), which a JPEG keeps nearly intact.
fn smooth(w: usize, h: usize) -> Vec<u8> {
    let mut rgb = Vec::with_capacity(w * h * 3);
    for y in 0..h {
        for x in 0..w {
            rgb.push((x * 255 / (w - 1)) as u8);
            rgb.push((y * 255 / (h - 1)) as u8);
            rgb.push(((x + y) * 255 / (w + h - 2)) as u8);
        }
    }
    rgb
}

/// Every `n`-th byte from `first`: one channel of an interleaved buffer.
fn channel(data: &[u8], n: usize, first: usize) -> Vec<u8> {
    data.iter().skip(first).step_by(n).copied().collect()
}

/// The pattern with an alpha channel of noise added.
fn with_alpha(rgb: &[u8]) -> Vec<u8> {
    let mut random = Lcg(11);
    rgb.chunks_exact(3).flat_map(|p| [p[0], p[1], p[2], random.byte()]).collect()
}

/// A fresh folder for the fixtures of one test, emptied if a failed run left it.
fn folder(name: &str) -> PathBuf {
    let dir = Path::new(env!("CARGO_TARGET_TMPDIR")).join(format!("image_{name}"));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir
}

fn write_png(path: &Path, w: usize, h: usize, color: png::ColorType, depth: png::BitDepth, data: &[u8]) {
    let mut encoder = png::Encoder::new(BufWriter::new(fs::File::create(path).unwrap()), w as u32, h as u32);
    encoder.set_color(color);
    encoder.set_depth(depth);
    encoder.write_header().unwrap().write_image_data(data).unwrap();
}

fn write_tiff<C: tiff::encoder::colortype::ColorType<Inner = u8>>(path: &Path, w: usize, h: usize, data: &[u8]) {
    let mut encoder = tiff::encoder::TiffEncoder::new(BufWriter::new(fs::File::create(path).unwrap())).unwrap();
    encoder.write_image::<C>(w as u32, h as u32, data).unwrap();
}

/// A baseline JPEG of quality 90.
fn write_jpeg(path: &Path, w: usize, h: usize, rgb: &[u8]) {
    let encoder = jpeg_encoder::Encoder::new_file(path, 90).unwrap();
    encoder.encode(rgb, w as u16, h as u16, jpeg_encoder::ColorType::Rgb).unwrap();
}

/// The mean and the largest absolute difference of two images of the same size.
fn difference(a: &[u8], b: &[u8]) -> (f64, u8) {
    assert_eq!(a.len(), b.len(), "images of different sizes");
    let gaps = a.iter().zip(b).map(|(&p, &q)| p.abs_diff(q));
    let largest = gaps.clone().max().unwrap_or(0);
    (gaps.map(f64::from).sum::<f64>() / a.len().max(1) as f64, largest)
}

fn size_of(img: &Grey) -> (usize, usize) {
    assert_eq!(img.pixels.len(), img.width * img.height, "the pixel count is not width x height");
    (img.width, img.height)
}

#[test]
fn png_and_tiff_give_the_same_grey() {
    let dir = folder("png_tiff");
    let rgb = pattern(W, H);
    let (png_path, tiff_path) = (dir.join("pattern.png"), dir.join("pattern.tif"));
    write_png(&png_path, W, H, png::ColorType::Rgb, png::BitDepth::Eight, &rgb);
    write_tiff::<tiff::encoder::colortype::RGB8>(&tiff_path, W, H, &rgb);

    let (from_png, full_png) = read_grey(&png_path, 0, false).unwrap();
    let (from_tiff, full_tiff) = read_grey(&tiff_path, 0, false).unwrap();
    assert_eq!(size_of(&from_png), (W, H));
    assert_eq!(size_of(&from_tiff), (W, H));
    assert_eq!((full_png, full_tiff), ((W, H), (W, H)));
    assert!(from_png.pixels == from_tiff.pixels, "the grey of the PNG and of the TIFF differ");
    assert_eq!(&from_png.pixels[..4], &[54, 182, 18, 255], "pure red, green, blue and white");

    // a DCT scale is a JPEG's: the others are read at full size whatever is asked
    let (reduced, full) = read_grey(&png_path, 300, true).unwrap();
    assert_eq!((size_of(&reduced), full), ((W, H), (W, H)));
    assert!(reduced.pixels == from_png.pixels);
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn alpha_is_dropped_and_a_grey_file_is_read_as_it_is() {
    let dir = folder("alpha_grey");
    let rgb = pattern(W, H);
    let rgba = with_alpha(&rgb);
    let plane = channel(&rgb, 3, 2);
    write_png(&dir.join("rgb.png"), W, H, png::ColorType::Rgb, png::BitDepth::Eight, &rgb);
    write_png(&dir.join("rgba.png"), W, H, png::ColorType::Rgba, png::BitDepth::Eight, &rgba);
    write_png(&dir.join("grey.png"), W, H, png::ColorType::Grayscale, png::BitDepth::Eight, &plane);
    write_tiff::<tiff::encoder::colortype::RGBA8>(&dir.join("rgba.tif"), W, H, &rgba);
    write_tiff::<tiff::encoder::colortype::Gray8>(&dir.join("grey.tif"), W, H, &plane);

    let expected = read_grey(&dir.join("rgb.png"), 0, false).unwrap().0;
    for name in ["rgba.png", "rgba.tif"] {
        let (img, full) = read_grey(&dir.join(name), 0, false).unwrap();
        assert_eq!((size_of(&img), full), ((W, H), (W, H)), "{name}");
        assert!(img.pixels == expected.pixels, "{name}: the alpha changed the grey");
    }
    for name in ["grey.png", "grey.tif"] {
        let (img, full) = read_grey(&dir.join(name), 0, false).unwrap();
        assert_eq!((size_of(&img), full), ((W, H), (W, H)), "{name}");
        assert!(img.pixels == plane, "{name}: a grey file is not read as it is");
    }
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn colmap_size_rounds_as_colmap() {
    // 805 x 600 / 1203 = 401.496, plus 0.5 and floored: 401
    assert_eq!(colmap_size(1203, 805, 600), (600, 401));
    assert_eq!(colmap_size(805, 1203, 600), (401, 600));
    // the short side never below 8: 20 x 600 / 3000 = 4
    assert_eq!(colmap_size(20, 3000, 600), (8, 600));
    // within the limit, and without a limit, the photo as it is
    assert_eq!(colmap_size(500, 300, 600), (500, 300));
    assert_eq!(colmap_size(600, 400, 600), (600, 400));
    assert_eq!(colmap_size(1203, 805, 0), (1203, 805));
}

#[test]
fn dct_scale_is_the_smallest_above_the_target() {
    let dir = folder("dct");
    let (w, h) = (1600, 1200);
    let rgb = smooth(w, h);
    let (jpg, png_path) = (dir.join("smooth.jpg"), dir.join("smooth.png"));
    write_jpeg(&jpg, w, h, &rgb);
    write_png(&png_path, w, h, png::ColorType::Rgb, png::BitDepth::Eight, &rgb);

    let at = |min_long_side: usize, dct: bool| {
        let (img, full) = read_grey(&jpg, min_long_side, dct).unwrap();
        assert_eq!(full, (w, h), "the full size of the photo");
        (size_of(&img), img)
    };
    assert_eq!(at(500, true).0, (800, 600), "1/2");
    assert_eq!(at(150, true).0, (200, 150), "1/8");
    assert_eq!(at(200, true).0, (200, 150), "1/8 still reaches 200");
    assert_eq!(at(201, true).0, (400, 300), "1/4");
    assert_eq!(at(1600, true).0, (1600, 1200), "full size reaches 1600");
    assert_eq!(at(5000, true).0, (1600, 1200), "nothing above the full size");
    assert_eq!(at(0, true).0, (1600, 1200), "no limit: full size");
    let (size, full) = at(500, false);
    assert_eq!(size, (1600, 1200), "without the DCT scales");

    // the JPEG keeps the smooth pattern (the colour path of the decoder is COLMAP's grey too)
    let exact = read_grey(&png_path, 0, false).unwrap().0;
    let (mean, largest) = difference(&full.pixels, &exact.pixels);
    assert!(mean < 1.0 && largest <= 8, "JPEG against PNG: mean {mean:.3}, largest {largest}");
    // and the DCT scale of 1/2 is close to COLMAP's reduction of the full decode
    let reduced = rescale_as_colmap(&full, 800, 600);
    let (mean, largest) = difference(&at(500, true).1.pixels, &reduced.pixels);
    assert!(mean < 1.0 && largest <= 8, "DCT 1/2 against the triangle filter: mean {mean:.3}, largest {largest}");
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_truncated_jpeg_is_corrupt_and_a_gif_is_unsupported() {
    let dir = folder("corrupt");
    let jpg = dir.join("pattern.jpg");
    write_jpeg(&jpg, W, H, &pattern(W, H));
    let bytes = fs::read(&jpg).unwrap();
    let half = dir.join("half.jpg");
    fs::write(&half, &bytes[..bytes.len() / 2]).unwrap();
    let gif = dir.join("pattern.gif");
    fs::write(&gif, b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff\x2c").unwrap();

    assert!(matches!(read_grey(&half, 0, false), Err(ReadError::Corrupt(_))), "half a JPEG at full size");
    assert!(matches!(read_grey(&half, 300, true), Err(ReadError::Corrupt(_))), "half a JPEG at a DCT scale");
    assert!(matches!(read_grey(&gif, 0, false), Err(ReadError::Unsupported(_))), "a GIF");
    assert!(matches!(read_mask(&gif), Err(ReadError::Unsupported(_))), "a GIF as a mask");
    let (whole, full) = read_grey(&jpg, 0, false).unwrap();
    assert_eq!((size_of(&whole), full), ((W, H), (W, H)), "the whole JPEG reads");
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_16_bit_png_and_a_missing_file_are_told_apart() {
    let dir = folder("sixteen");
    let deep = dir.join("deep.png");
    write_png(&deep, 4, 4, png::ColorType::Grayscale, png::BitDepth::Sixteen, &[0x12; 4 * 4 * 2]);
    let empty = dir.join("empty.png");
    fs::write(&empty, b"").unwrap();

    assert!(matches!(read_grey(&deep, 0, false), Err(ReadError::Unsupported(_))), "16 bits");
    assert!(matches!(read_mask(&deep), Err(ReadError::Unsupported(_))), "16 bits as a mask");
    assert!(matches!(read_grey(&empty, 0, false), Err(ReadError::Unsupported(_))), "an empty file");
    assert!(matches!(read_grey(&dir.join("missing.jpg"), 0, false), Err(ReadError::Io(_))), "a missing file");
    assert!(matches!(read_mask(&dir.join("missing.png")), Err(ReadError::Io(_))), "a missing mask");
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn a_mask_is_an_8_bit_grey_png() {
    let dir = folder("mask");
    let mut random = Lcg(3);
    let values: Vec<u8> = (0..W * H).map(|_| if random.byte() < 64 { 0 } else { 255 }).collect();
    let mask = dir.join("photo.jpg.png");
    write_png(&mask, W, H, png::ColorType::Grayscale, png::BitDepth::Eight, &values);
    let colour = dir.join("colour.png");
    write_png(&colour, 4, 4, png::ColorType::Rgb, png::BitDepth::Eight, &[200; 4 * 4 * 3]);
    let jpg = dir.join("mask.jpg");
    write_jpeg(&jpg, 16, 16, &smooth(16, 16));

    let read = read_mask(&mask).unwrap();
    assert_eq!(size_of(&read), (W, H));
    assert!(read.pixels == values, "the mask is not read as written");
    assert!(matches!(read_mask(&colour), Err(ReadError::Unsupported(_))), "a colour PNG");
    assert!(matches!(read_mask(&jpg), Err(ReadError::Unsupported(_))), "a JPEG");
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn the_reduction_is_the_triangle_filter_of_colmap() {
    // one row of four pixels to two: OpenImageIO's triangle of radius 2 (ceil(1 / 0.5)) centred
    // on the destination pixel has the weights 1/8, 3/8, 3/8, 1/8 on the source pixels -1..2
    // (clamped to the row) of the first and 1..4 of the second
    let row = Grey { width: 4, height: 1, pixels: vec![0, 255, 0, 0] };
    let half = rescale_as_colmap(&row, 2, 1);
    assert_eq!((half.width, half.height), (2, 1));
    assert_eq!(half.pixels, vec![96, 32], "3/8 of 255 is 95.625, 1/8 is 31.875");

    // the same size is a copy; a flat image stays flat (the weights add up to one)
    let flat = Grey { width: 50, height: 30, pixels: vec![200; 50 * 30] };
    assert_eq!(rescale_as_colmap(&flat, 50, 30), flat);
    let smaller = rescale_as_colmap(&flat, 25, 15);
    assert_eq!(size_of(&smaller), (25, 15));
    assert!(smaller.pixels.iter().all(|&p| p == 200), "a flat image changed");

    // the pattern to the size COLMAP reduces it to
    let rgb = pattern(W, H);
    let grey = Grey { width: W, height: H, pixels: channel(&rgb, 3, 0) };
    let (w, h) = colmap_size(W, H, 600);
    let reduced = rescale_as_colmap(&grey, w, h);
    assert_eq!(size_of(&reduced), (600, 401));
    // the red gradient along the row survives the reduction: the left edge dark, the right bright
    let middle = &reduced.pixels[200 * 600..201 * 600];
    assert!(middle[0] < 8 && middle[599] > 247, "left {}, right {}", middle[0], middle[599]);
}
