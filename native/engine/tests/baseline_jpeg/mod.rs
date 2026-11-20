//! A baseline JPEG writer for the fixtures of `tests/image.rs`, written from ITU-T T.81 so that the
//! tests need no encoder crate: a sequential DCT frame (SOF0) of 8-bit samples, one component
//! (grey) or three (the YCbCr of JFIF, no subsampling), the example quantisation tables of Annex K
//! (K.1, K.2) scaled to a fifth, the typical Huffman tables of Annex K (K.3 to K.6), a plain
//! floating-point DCT (A.3.3). Nothing here is tuned for size or speed, and nothing is taken from
//! the code of any encoder.

use std::fs;
use std::path::Path;

/// A Huffman table as the standard specifies it (B.2.4.2): the number of codes of each length
/// from 1 to 16 (BITS) and the symbols in the order of their codes (HUFFVAL).
struct Spec {
    counts: [u8; 16],
    symbols: &'static [u8],
}

/// Table K.3, DC luminance.
const DC_LUMINANCE: Spec = Spec {
    counts: [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0],
    symbols: &[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
};

/// Table K.4, DC chrominance.
const DC_CHROMINANCE: Spec = Spec {
    counts: [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0],
    symbols: &[0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
};

/// Table K.5, AC luminance.
const AC_LUMINANCE: Spec = Spec {
    counts: [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 125],
    symbols: &[
        0x01, 0x02, 0x03, 0x00, 0x04, 0x11, 0x05, 0x12, 0x21, 0x31, 0x41, 0x06, 0x13, 0x51, 0x61, 0x07,
        0x22, 0x71, 0x14, 0x32, 0x81, 0x91, 0xA1, 0x08, 0x23, 0x42, 0xB1, 0xC1, 0x15, 0x52, 0xD1, 0xF0,
        0x24, 0x33, 0x62, 0x72, 0x82, 0x09, 0x0A, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x25, 0x26, 0x27, 0x28,
        0x29, 0x2A, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48, 0x49,
        0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68, 0x69,
        0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x83, 0x84, 0x85, 0x86, 0x87, 0x88, 0x89,
        0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7,
        0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3, 0xC4, 0xC5,
        0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA, 0xE1, 0xE2,
        0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF1, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
        0xF9, 0xFA,
    ],
};

/// Table K.6, AC chrominance.
const AC_CHROMINANCE: Spec = Spec {
    counts: [0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 119],
    symbols: &[
        0x00, 0x01, 0x02, 0x03, 0x11, 0x04, 0x05, 0x21, 0x31, 0x06, 0x12, 0x41, 0x51, 0x07, 0x61, 0x71,
        0x13, 0x22, 0x32, 0x81, 0x08, 0x14, 0x42, 0x91, 0xA1, 0xB1, 0xC1, 0x09, 0x23, 0x33, 0x52, 0xF0,
        0x15, 0x62, 0x72, 0xD1, 0x0A, 0x16, 0x24, 0x34, 0xE1, 0x25, 0xF1, 0x17, 0x18, 0x19, 0x1A, 0x26,
        0x27, 0x28, 0x29, 0x2A, 0x35, 0x36, 0x37, 0x38, 0x39, 0x3A, 0x43, 0x44, 0x45, 0x46, 0x47, 0x48,
        0x49, 0x4A, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5A, 0x63, 0x64, 0x65, 0x66, 0x67, 0x68,
        0x69, 0x6A, 0x73, 0x74, 0x75, 0x76, 0x77, 0x78, 0x79, 0x7A, 0x82, 0x83, 0x84, 0x85, 0x86, 0x87,
        0x88, 0x89, 0x8A, 0x92, 0x93, 0x94, 0x95, 0x96, 0x97, 0x98, 0x99, 0x9A, 0xA2, 0xA3, 0xA4, 0xA5,
        0xA6, 0xA7, 0xA8, 0xA9, 0xAA, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBA, 0xC2, 0xC3,
        0xC4, 0xC5, 0xC6, 0xC7, 0xC8, 0xC9, 0xCA, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9, 0xDA,
        0xE2, 0xE3, 0xE4, 0xE5, 0xE6, 0xE7, 0xE8, 0xE9, 0xEA, 0xF2, 0xF3, 0xF4, 0xF5, 0xF6, 0xF7, 0xF8,
        0xF9, 0xFA,
    ],
};

/// Table K.1, the example luminance quantisation table, in row order.
const LUMINANCE_QUANT: [u16; 64] = [
    16, 11, 10, 16, 24, 40, 51, 61, //
    12, 12, 14, 19, 26, 58, 60, 55, //
    14, 13, 16, 24, 40, 57, 69, 56, //
    14, 17, 22, 29, 51, 87, 80, 62, //
    18, 22, 37, 56, 68, 109, 103, 77, //
    24, 35, 55, 64, 81, 104, 113, 92, //
    49, 64, 78, 87, 103, 121, 120, 101, //
    72, 92, 95, 98, 112, 100, 103, 99,
];

/// Table K.2, the example chrominance quantisation table, in row order.
const CHROMINANCE_QUANT: [u16; 64] = [
    17, 18, 24, 47, 99, 99, 99, 99, //
    18, 21, 26, 66, 99, 99, 99, 99, //
    24, 26, 56, 99, 99, 99, 99, 99, //
    47, 66, 99, 99, 99, 99, 99, 99, //
    99, 99, 99, 99, 99, 99, 99, 99, //
    99, 99, 99, 99, 99, 99, 99, 99, //
    99, 99, 99, 99, 99, 99, 99, 99, //
    99, 99, 99, 99, 99, 99, 99, 99,
];

/// A quantisation table scaled to a fifth, rounded, never below 1 (about what encoders call
/// quality 90).
fn scaled(table: &[u16; 64]) -> [u8; 64] {
    let mut out = [0u8; 64];
    for (o, &q) in out.iter_mut().zip(table) {
        *o = ((q + 2) / 5).max(1) as u8;
    }
    out
}

/// The codes of a table by the procedure of Annex C: the lengths from BITS, the codes counted up
/// in that order. Checked while built: BITS and HUFFVAL agree, no symbol twice, no code
/// overflows its length or is all ones, and every symbol the data may need is there (DC
/// categories 0..=11; AC runs 0..=15 with sizes 1..=10, EOB and ZRL).
struct Codes {
    code: [u16; 256],
    length: [u8; 256],
}

impl Codes {
    fn new(spec: &Spec, ac: bool) -> Codes {
        let total: usize = spec.counts.iter().map(|&c| usize::from(c)).sum();
        assert_eq!(total, spec.symbols.len(), "BITS and HUFFVAL disagree");
        let mut codes = Codes { code: [0; 256], length: [0; 256] };
        let (mut code, mut next) = (0u32, 0usize);
        for (len, &count) in spec.counts.iter().enumerate() {
            let len = len + 1;
            for _ in 0..count {
                let symbol = usize::from(spec.symbols[next]);
                assert!(code + 1 < (1 << len), "a code of {len} bits overflows or is all ones");
                assert_eq!(codes.length[symbol], 0, "symbol {symbol:#04x} twice");
                codes.code[symbol] = code as u16;
                codes.length[symbol] = len as u8;
                next += 1;
                code += 1;
            }
            code <<= 1;
        }
        let needed: Vec<u8> = if ac {
            let run_size = (0..16u8).flat_map(|run| (1..=10u8).map(move |size| run << 4 | size));
            [0x00, 0xF0].into_iter().chain(run_size).collect()
        } else {
            (0..=11).collect()
        };
        for symbol in needed {
            assert!(codes.length[usize::from(symbol)] > 0, "symbol {symbol:#04x} missing from the table");
        }
        codes
    }
}

/// The zigzag sequence of Figure A.6: the row-order index of the k-th coefficient. Along each
/// anti-diagonal, the even ones are walked from the bottom-left corner up and the odd ones from
/// the top-right corner down.
fn zigzag() -> [usize; 64] {
    let mut order = [0usize; 64];
    let mut k = 0;
    for d in 0..15usize {
        let (low, high) = (d.saturating_sub(7), d.min(7));
        let rows: Vec<usize> = if d % 2 == 0 { (low..=high).rev().collect() } else { (low..=high).collect() };
        for r in rows {
            order[k] = r * 8 + (d - r);
            k += 1;
        }
    }
    order
}

/// cos((2x + 1) u pi / 16) for x and u in 0..8.
fn cosines() -> [[f64; 8]; 8] {
    let mut table = [[0.0; 8]; 8];
    for (x, row) in table.iter_mut().enumerate() {
        for (u, c) in row.iter_mut().enumerate() {
            *c = ((2 * x + 1) as f64 * u as f64 * std::f64::consts::PI / 16.0).cos();
        }
    }
    table
}

/// The forward DCT of A.3.3 on one block, separable, in f64:
/// `F(v, u) = C(u) C(v) / 4 * sum over y, x of f(y, x) cos((2x + 1) u pi / 16) cos((2y + 1) v pi / 16)`.
fn fdct(f: &[f64; 64], cosines: &[[f64; 8]; 8]) -> [f64; 64] {
    let c = |k: usize| if k == 0 { std::f64::consts::FRAC_1_SQRT_2 } else { 1.0 };
    let mut rows = [0.0; 64];
    for y in 0..8 {
        for u in 0..8 {
            rows[y * 8 + u] = (0..8).map(|x| f[y * 8 + x] * cosines[x][u]).sum();
        }
    }
    let mut out = [0.0; 64];
    for v in 0..8 {
        for u in 0..8 {
            let sum: f64 = (0..8).map(|y| rows[y * 8 + u] * cosines[y][v]).sum();
            out[v * 8 + u] = 0.25 * c(u) * c(v) * sum;
        }
    }
    out
}

/// The YCbCr planes of JFIF (its 1.02 specification) from interleaved RGB.
fn ycbcr(rgb: &[u8]) -> Vec<Vec<u8>> {
    let n = rgb.len() / 3;
    let mut planes = vec![Vec::with_capacity(n), Vec::with_capacity(n), Vec::with_capacity(n)];
    for p in rgb.chunks_exact(3) {
        let (r, g, b) = (f64::from(p[0]), f64::from(p[1]), f64::from(p[2]));
        let y = 0.299 * r + 0.587 * g + 0.114 * b;
        let cb = 128.0 - 0.168736 * r - 0.331264 * g + 0.5 * b;
        let cr = 128.0 + 0.5 * r - 0.418688 * g - 0.081312 * b;
        for (plane, value) in planes.iter_mut().zip([y, cb, cr]) {
            plane.push(value.round().clamp(0.0, 255.0) as u8);
        }
    }
    planes
}

/// One 8 x 8 block of a plane, level-shifted by 128 (A.3.1), the edge samples repeated beyond the
/// image (A.1.1).
fn block(plane: &[u8], width: usize, height: usize, bx: usize, by: usize) -> [f64; 64] {
    let mut out = [0.0; 64];
    for j in 0..8 {
        let y = (by * 8 + j).min(height - 1);
        for i in 0..8 {
            let x = (bx * 8 + i).min(width - 1);
            out[j * 8 + i] = f64::from(plane[y * width + x]) - 128.0;
        }
    }
    out
}

/// The category of a value (the number of bits of its magnitude) and its bits as F.1.2.1 writes
/// them: the value itself when positive, one less when negative, in the low bits.
fn magnitude(value: i32) -> (u32, u32) {
    let size = 32 - value.unsigned_abs().leading_zeros();
    let bits = if value >= 0 { value as u32 } else { (value - 1) as u32 };
    (size, bits & ((1u32 << size) - 1))
}

/// The entropy-coded segment: bits most significant first, a 0x00 after every 0xFF byte
/// (F.1.2.3), the last byte padded with ones.
#[derive(Default)]
struct BitWriter {
    out: Vec<u8>,
    held: u32,
    count: u32,
}

impl BitWriter {
    fn put(&mut self, value: u32, bits: u32) {
        if bits == 0 {
            return;
        }
        self.held = (self.held << bits) | (value & ((1 << bits) - 1));
        self.count += bits;
        while self.count >= 8 {
            let byte = (self.held >> (self.count - 8)) as u8;
            self.out.push(byte);
            if byte == 0xFF {
                self.out.push(0x00);
            }
            self.count -= 8;
        }
        self.held &= (1 << self.count) - 1;
    }

    fn symbol(&mut self, codes: &Codes, symbol: u8) {
        let s = usize::from(symbol);
        assert!(codes.length[s] > 0, "no code for symbol {symbol:#04x}");
        self.put(u32::from(codes.code[s]), u32::from(codes.length[s]));
    }

    fn finish(&mut self) {
        if self.count > 0 {
            let pad = 8 - self.count;
            self.put((1 << pad) - 1, pad);
        }
    }
}

/// One block of quantised coefficients in zigzag order (F.1.2): the DC difference by its
/// category, then the AC coefficients as runs of zeros and sizes, ZRL for sixteen zeros, EOB when
/// the rest is zero.
fn encode_block(bits: &mut BitWriter, coefficients: &[i32; 64], previous_dc: &mut i32, dc: &Codes, ac: &Codes) {
    let difference = coefficients[0] - *previous_dc;
    *previous_dc = coefficients[0];
    let (size, value) = magnitude(difference);
    bits.symbol(dc, size as u8);
    bits.put(value, size);
    let mut run = 0u32;
    for &coefficient in &coefficients[1..] {
        if coefficient == 0 {
            run += 1;
            continue;
        }
        while run > 15 {
            bits.symbol(ac, 0xF0);
            run -= 16;
        }
        let (size, value) = magnitude(coefficient);
        bits.symbol(ac, (run << 4) as u8 | size as u8);
        bits.put(value, size);
        run = 0;
    }
    if run > 0 {
        bits.symbol(ac, 0x00);
    }
}

/// A marker segment: the marker, its length (the payload plus the two length bytes), the payload.
fn segment(out: &mut Vec<u8>, marker: u8, payload: &[u8]) {
    out.extend([0xFF, marker]);
    out.extend(((payload.len() + 2) as u16).to_be_bytes());
    out.extend(payload);
}

/// Writes `data` (row-major; grey when `channels` is 1, interleaved RGB when 3) to `path` as a
/// baseline JPEG: JFIF APP0, the quantisation tables, the frame header, the Huffman tables, one
/// scan of every component interleaved (A.2.3), the end of the image.
pub fn write(path: &Path, width: usize, height: usize, channels: usize, data: &[u8]) {
    assert!(matches!(channels, 1 | 3), "grey or RGB");
    assert!(width > 0 && width <= 65535 && height > 0 && height <= 65535, "a JPEG size");
    assert_eq!(data.len(), width * height * channels, "the data of width x height x channels");
    let planes: Vec<Vec<u8>> = if channels == 1 { vec![data.to_vec()] } else { ycbcr(data) };
    let tables = channels.min(2);
    let quant = [scaled(&LUMINANCE_QUANT), scaled(&CHROMINANCE_QUANT)];
    let dc = [Codes::new(&DC_LUMINANCE, false), Codes::new(&DC_CHROMINANCE, false)];
    let ac = [Codes::new(&AC_LUMINANCE, true), Codes::new(&AC_CHROMINANCE, true)];
    let zigzag = zigzag();

    let mut out = vec![0xFF, 0xD8];
    segment(&mut out, 0xE0, b"JFIF\0\x01\x01\x00\x00\x01\x00\x01\x00\x00");
    for (id, table) in quant.iter().enumerate().take(tables) {
        let mut payload = vec![id as u8];
        payload.extend(zigzag.iter().map(|&k| table[k]));
        segment(&mut out, 0xDB, &payload);
    }
    let mut frame = vec![8, (height >> 8) as u8, height as u8, (width >> 8) as u8, width as u8, channels as u8];
    for c in 0..channels {
        frame.extend([c as u8 + 1, 0x11, (c > 0) as u8]);
    }
    segment(&mut out, 0xC0, &frame);
    for (class, specs) in [(0u8, [&DC_LUMINANCE, &DC_CHROMINANCE]), (1, [&AC_LUMINANCE, &AC_CHROMINANCE])] {
        for (id, spec) in specs.iter().enumerate().take(tables) {
            let mut payload = vec![class << 4 | id as u8];
            payload.extend(spec.counts);
            payload.extend(spec.symbols);
            segment(&mut out, 0xC4, &payload);
        }
    }
    let mut scan = vec![channels as u8];
    for c in 0..channels {
        let table = (c > 0) as u8;
        scan.extend([c as u8 + 1, table << 4 | table]);
    }
    scan.extend([0, 63, 0]);
    segment(&mut out, 0xDA, &scan);

    let cosines = cosines();
    let mut bits = BitWriter::default();
    let mut previous = [0i32; 3];
    for by in 0..height.div_ceil(8) {
        for bx in 0..width.div_ceil(8) {
            for (c, plane) in planes.iter().enumerate() {
                let t = (c > 0) as usize;
                let coefficients = fdct(&block(plane, width, height, bx, by), &cosines);
                let mut quantised = [0i32; 64];
                for (k, &n) in zigzag.iter().enumerate() {
                    quantised[k] = (coefficients[n] / f64::from(quant[t][n])).round() as i32;
                }
                encode_block(&mut bits, &quantised, &mut previous[c], &dc[t], &ac[t]);
            }
        }
    }
    bits.finish();
    out.extend(bits.out);
    out.extend([0xFF, 0xD9]);
    fs::write(path, out).unwrap();
}
