// A small reader of the Cloud Optimized GeoTIFFs this program writes for a site: tiled, with overviews,
// tiles in WebP (pictures) or deflate with the floating-point predictor (heights). It asks the server for the
// parts it needs (HTTP range requests). Where the host does not serve ranges and answers with the whole file,
// the file is taken whole if it is small enough (WHOLE_LIMIT) and refused otherwise (NoRanges): the page says so.
// Not a general GeoTIFF reader: strips, JPEG, LZW and palettes are refused with a message.

const HEAD = 65536; // read at once: a COG keeps its directories and tile indexes at the start
const SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8, 17: 8, 18: 8};
const WEBP = 50001;
const DEFLATE = [8, 32946];
export const WHOLE_LIMIT = 50 * 1024 * 1024; // the largest file taken whole from a host without ranges

// The host answered a request of a part with the whole file, and the file is too large to take it so.
export class NoRanges extends Error {
  constructor(url, size) {
    super(`${url}: the host does not serve range requests`);
    this.code = 'no-ranges';
    this.size = size; // bytes, null when the host does not say
  }
}

// The body of an answer that is the whole file, read up to ``limit`` bytes and no further.
async function wholeFile(answer, url, limit) {
  const declared = Number(answer.headers.get('Content-Length'));
  const stop = async (size) => {
    try {
      if (answer.body) await answer.body.cancel();
    } catch (error) { /* already closed */ }
    throw new NoRanges(url, size);
  };
  if (declared > limit) await stop(declared);
  if (!answer.body) {
    const all = await answer.arrayBuffer();
    if (all.byteLength > limit) throw new NoRanges(url, all.byteLength);
    return all;
  }
  const reader = answer.body.getReader();
  const parts = [];
  let size = 0;
  for (;;) {
    const {done, value} = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > limit) {
      try {
        await reader.cancel();
      } catch (error) { /* already closed */ }
      throw new NoRanges(url, declared > 0 ? declared : null);
    }
    parts.push(value);
  }
  const all = new Uint8Array(size);
  let at = 0;
  for (const part of parts) {
    all.set(part, at);
    at += part.byteLength;
  }
  return all.buffer;
}

class Bytes {
  constructor(url, limit = WHOLE_LIMIT) {
    this.url = url;
    this.limit = limit;
    this.head = null;
    this.whole = null; // the file, when the host gave it whole instead of the part asked for
  }

  async read(offset, length) {
    if (this.whole) return this.whole.slice(offset, offset + length);
    if (this.head && offset + length <= this.head.byteLength) return this.head.slice(offset, offset + length);
    const answer = await fetch(this.url, {headers: {Range: `bytes=${offset}-${offset + length - 1}`}});
    if (answer.status === 200) { // a host without ranges: the file once, whole, if it is not too large
      this.whole = await wholeFile(answer, this.url, this.limit);
      return this.whole.slice(offset, offset + length);
    }
    if (answer.status !== 206) throw new Error(`${this.url}: HTTP ${answer.status}`);
    return answer.arrayBuffer();
  }
}

class Level {
  constructor(tags, little) {
    this.tags = tags;
    this.little = little;
    this.width = tags.get(256);
    this.height = tags.get(257);
    this.tileWidth = tags.get(322);
    this.tileHeight = tags.get(323);
    this.compression = tags.get(259) || 1;
    this.predictor = tags.get(317) || 1;
    this.samples = tags.get(277) || 1;
    this.bits = tags.get(258) || 8;
    this.format = tags.get(339) || 1;
    this.mask = ((tags.get(254) || 0) & 4) !== 0;
    this.across = Math.ceil(this.width / this.tileWidth);
    this.down = Math.ceil(this.height / this.tileHeight);
  }
}

function one(value) {
  return Array.isArray(value) ? value[0] : value;
}

async function directories(bytes) {
  const first = new DataView(await bytes.read(0, 16));
  const mark = first.getUint16(0);
  if (mark !== 0x4949 && mark !== 0x4d4d) throw new Error(`${bytes.url}: not a TIFF`);
  const little = mark === 0x4949;
  const magic = first.getUint16(2, little);
  if (magic !== 42 && magic !== 43) throw new Error(`${bytes.url}: not a TIFF`);
  const big = magic === 43;
  let offset = big ? Number(first.getBigUint64(8, little)) : first.getUint32(4, little);
  const out = [];
  while (offset && out.length < 64) {
    const count = new DataView(await bytes.read(offset, big ? 8 : 2));
    const n = big ? Number(count.getBigUint64(0, little)) : count.getUint16(0, little);
    const entry = big ? 20 : 12;
    const start = offset + (big ? 8 : 2);
    const table = new DataView(await bytes.read(start, n * entry + (big ? 8 : 4)));
    const tags = new Map();
    for (let i = 0; i < n; i++) {
      const at = i * entry;
      const tag = table.getUint16(at, little);
      const type = table.getUint16(at + 2, little);
      const items = big ? Number(table.getBigUint64(at + 4, little)) : table.getUint32(at + 4, little);
      const size = (SIZES[type] || 1) * items;
      const field = at + (big ? 12 : 8);
      const room = big ? 8 : 4;
      let view;
      if (size <= room) {
        view = new DataView(table.buffer, table.byteOffset + field, size);
      } else {
        const where = big ? Number(table.getBigUint64(field, little)) : table.getUint32(field, little);
        view = new DataView(await bytes.read(where, size));
      }
      tags.set(tag, values(view, type, items, little));
    }
    out.push({tags, little});
    offset = big ? Number(table.getBigUint64(n * entry, little)) : table.getUint32(n * entry, little);
  }
  return out;
}

function values(view, type, items, little) {
  if (type === 2) { // text
    let text = '';
    for (let i = 0; i < items; i++) {
      const c = view.getUint8(i);
      if (c) text += String.fromCharCode(c);
    }
    return text;
  }
  const out = new Array(items);
  for (let i = 0; i < items; i++) {
    switch (type) {
      case 3: out[i] = view.getUint16(i * 2, little); break;
      case 4: out[i] = view.getUint32(i * 4, little); break;
      case 12: out[i] = view.getFloat64(i * 8, little); break;
      case 16: out[i] = Number(view.getBigUint64(i * 8, little)); break;
      case 11: out[i] = view.getFloat32(i * 4, little); break;
      default: out[i] = view.getUint8(i * (SIZES[type] || 1));
    }
  }
  return out;
}

async function inflate(buffer) {
  const stream = new Blob([buffer]).stream().pipeThrough(new DecompressionStream('deflate'));
  return new Response(stream).arrayBuffer();
}

export class Cog {
  constructor(bytes, levels) {
    this.bytes = bytes;
    this.levels = levels; // the full image first, then its overviews, smaller and smaller
    this.whole = bytes.whole !== null; // read whole: the host does not serve range requests
    const full = levels[0];
    const scale = full.tags.get(33550);
    const tie = full.tags.get(33922);
    if (!scale || !tie) throw new Error(`${bytes.url}: no position in the file`);
    this.resolution = [scale[0], scale[1]];
    this.origin = [tie[3] - tie[0] * scale[0], tie[4] + tie[1] * scale[1]]; // the top left corner
    this.extent = [this.origin[0], this.origin[1] - full.height * scale[1],
      this.origin[0] + full.width * scale[0], this.origin[1]];
    const nodata = full.tags.get(42113);
    this.nodata = nodata === undefined || nodata === '' ? null : Number(nodata);
    this.cache = new Map();
  }

  // The bytes of a tile as they are in the file; null for a tile that is not there or is empty.
  async tile(level, x, y) {
    const lv = this.levels[level];
    if (!lv || x < 0 || y < 0 || x >= lv.across || y >= lv.down) return null;
    const index = y * lv.across + x;
    const size = lv.tags.get(325)[index];
    if (!size) return null;
    return this.bytes.read(lv.tags.get(324)[index], size);
  }

  // A tile of a picture (WebP), decoded by the browser; null where there is none.
  async picture(level, x, y) {
    const lv = this.levels[level];
    if (lv && lv.compression !== WEBP) throw new Error(`${this.bytes.url}: tiles that are not WebP`);
    const data = await this.tile(level, x, y);
    return data ? createImageBitmap(new Blob([data], {type: 'image/webp'})) : null;
  }

  // A tile of numbers (float32, one band) as a Float32Array of tileWidth x tileHeight; null where there is none.
  async numbers(level, x, y) {
    const lv = this.levels[level];
    const key = `${level}/${x}/${y}`;
    if (this.cache.has(key)) return this.cache.get(key);
    let data = await this.tile(level, x, y);
    let out = null;
    if (data) {
      if (lv.bits !== 32 || lv.format !== 3 || lv.samples !== 1) {
        throw new Error(`${this.bytes.url}: not one band of float32`);
      }
      if (DEFLATE.includes(lv.compression)) data = await inflate(data);
      else if (lv.compression !== 1) throw new Error(`${this.bytes.url}: a compression this page cannot read`);
      const w = lv.tileWidth;
      const h = lv.tileHeight;
      out = new Float32Array(w * h);
      if (lv.predictor === 3) { // floating-point predictor: bytes differenced along the row, most significant first
        const row = new Uint8Array(data);
        const view = new DataView(new ArrayBuffer(4));
        for (let r = 0; r < h; r++) {
          const at = r * w * 4;
          for (let i = 1; i < w * 4; i++) row[at + i] = (row[at + i] + row[at + i - 1]) & 255;
          for (let c = 0; c < w; c++) {
            for (let b = 0; b < 4; b++) view.setUint8(b, row[at + b * w + c]);
            out[r * w + c] = view.getFloat32(0, false);
          }
        }
      } else if (lv.predictor === 1) {
        const view = new DataView(data);
        for (let i = 0; i < w * h; i++) out[i] = view.getFloat32(i * 4, lv.little);
      } else {
        throw new Error(`${this.bytes.url}: a predictor this page cannot read`);
      }
    }
    if (this.cache.size >= 16) this.cache.delete(this.cache.keys().next().value);
    this.cache.set(key, out);
    return out;
  }

  // The value of the full image at a point of the map; null outside it and where the image is empty.
  async valueAt(x, y) {
    const lv = this.levels[0];
    const col = Math.floor((x - this.origin[0]) / this.resolution[0]);
    const row = Math.floor((this.origin[1] - y) / this.resolution[1]);
    if (col < 0 || row < 0 || col >= lv.width || row >= lv.height) return null;
    const tile = await this.numbers(0, Math.floor(col / lv.tileWidth), Math.floor(row / lv.tileHeight));
    if (!tile) return null;
    const value = tile[(row % lv.tileHeight) * lv.tileWidth + (col % lv.tileWidth)];
    if (!Number.isFinite(value) || (this.nodata !== null && value === Math.fround(this.nodata))) return null;
    return value;
  }
}

// ``limit``: the largest file taken whole where the host does not serve ranges (NoRanges beyond it); the
// ``whole`` of the reader says whether that happened.
export async function open(url, limit = WHOLE_LIMIT) {
  const bytes = new Bytes(url, limit);
  bytes.head = await bytes.read(0, HEAD);
  const found = await directories(bytes);
  const levels = [];
  for (const {tags, little} of found) {
    for (const tag of [254, 256, 257, 258, 259, 277, 317, 322, 323, 339]) {
      if (tags.has(tag)) tags.set(tag, one(tags.get(tag)));
    }
    if (!tags.has(322) || !tags.has(324)) throw new Error(`${url}: a TIFF that is not tiled`);
    const level = new Level(tags, little);
    if (!level.mask) levels.push(level);
  }
  if (!levels.length) throw new Error(`${url}: an empty TIFF`);
  levels.sort((a, b) => b.width - a.width);
  return new Cog(bytes, levels);
}
