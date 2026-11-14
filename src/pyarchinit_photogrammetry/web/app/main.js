// The page of a survey: the plan (orthophoto, shaded relief, stratigraphic units, drawings, the traces of the
// sections, the layers of plugins) on a plain metric frame; the models and the point clouds in a 3D tab
// (view3d.js, loaded only when it is opened); the sections and elevations with their orthophotos and drawings in
// a tab of their own, each on the (u, v) plane of its frame; the tabs that plugins add (extras.js). A click gives
// the coordinates, the height and the attributes; distances and areas are measured on the plan, on the model or
// cloud and on a section, in the same panel. A site of several dates shows one date at a time: the selector of
// the dates switches the plan, the 3D view and the sections, the view kept (one frame for all the dates). Every
// text coming from the data is written as text, never as markup.

import {open} from './cog.js';
import {WORDS, pick, isWords} from './i18n.js';

const ol = window.ol;
const $ = (id) => document.getElementById(id);
const state = {site: null, language: 'it', map: null, entries: [], dems: [], hidden: new Set(), draw: null,
  last: null, whole: false, left: [], tab: 'plan', models: [], view3d: null, opening: null, result3d: null,
  epoch: -1, sections: [], section: null, sectionMap: null, sectionSketch: null, highlight: null, extras: [],
  sketch: null, drawMap: null};

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}

function word(key) {
  return (WORDS[state.language] || WORDS.it)[key] || key;
}

function title(text) {
  return pick(text, state.language);
}

// a value of the data as text: one given in both languages in the language of the page
function text(value) {
  if (isWords(value)) return title(value);
  return typeof value === 'object' ? JSON.stringify(value) : String(value);
}

function number(value, digits = 3) {
  return Number(value).toFixed(digits);
}

function colourOf(text, fallback) {
  return typeof text === 'string' && /^#[0-9a-fA-F]{6}$/.test(text) ? text : fallback;
}

function faded(colour, alpha) {
  const n = parseInt(colour.slice(1), 16);
  return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

// ---------------------------------------------------------------------------------------------- dates
// Whether ``chunk`` is shown at the date chosen (every chunk when the site has no dates).
function shown(chunk) {
  const timeline = state.site.timeline;
  if (!timeline || timeline.length < 2 || state.epoch < 0) return true;
  return timeline[state.epoch].chunks.includes(chunk.id);
}

function shownChunks() {
  return state.site.chunks.filter(shown);
}

function dateBar() {
  const timeline = state.site.timeline || [];
  const bar = $('dates');
  if (timeline.length < 2) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  bar.replaceChildren(el('span', word('dates'), 'dates-title'));
  const slider = el('input');
  slider.type = 'range';
  slider.min = '0';
  slider.max = String(timeline.length - 1);
  slider.step = '1';
  slider.value = String(state.epoch);
  slider.id = 'date-slider';
  slider.setAttribute('aria-label', word('dates'));
  slider.addEventListener('input', () => setEpoch(Number(slider.value)));
  bar.append(slider);
  timeline.forEach((day, index) => {
    const button = el('button', `${day.date}${day.label ? ` · ${day.label}` : ''}`, 'date');
    button.type = 'button';
    button.dataset.index = String(index);
    if (index === state.epoch) button.classList.add('on');
    button.setAttribute('aria-pressed', String(index === state.epoch));
    button.addEventListener('click', () => setEpoch(index));
    bar.append(button);
  });
}

function applyVisibility() {
  for (const entry of state.entries) entry.layer.setVisible(entry.def.visible !== false && shown(entry.chunk));
  if (state.view3d) {
    state.models.forEach((model, index) => {
      state.view3d.setVisible(index, model.def.visible !== false && shown(model.chunk));
    });
  }
}

// The date ``index`` shown: plan, 3D and sections switch to it, the views stay where they are.
async function setEpoch(index) {
  const timeline = state.site.timeline || [];
  if (index < 0 || index >= timeline.length || index === state.epoch) return;
  state.epoch = index;
  document.documentElement.dataset.epoch = String(index);
  applyVisibility();
  if (state.section && !shown(state.section.chunk)) {
    // the section of the same name at the new date, else the first one there
    const same = state.sections.find((s) => shown(s.chunk) && s.section.name === state.section.section.name);
    const next = same || state.sections.find((s) => shown(s.chunk)) || null;
    if (next && state.tab === 'sections') await openSection(next, true);
    else state.section = next;
  }
  if (state.tab.startsWith('x:')) await showExtra(state.tab.slice(2));
  state.last = null;
  showWords();
}

// ---------------------------------------------------------------------------------------------- layers
// A raster of the site, or null when the host does not serve range requests and the file is too large to be
// taken whole: the layer is left out and the page says which (see showNotice).
async function raster(chunk, def) {
  const limit = Number(new URLSearchParams(window.location.search).get('whole-limit')) || undefined;
  try {
    const cog = await open(def.url, limit);
    if (cog.whole) state.whole = true;
    return cog;
  } catch (error) {
    if (error.code !== 'no-ranges') throw error;
    state.whole = true;
    state.left.push({chunk, def, size: error.size});
    return null;
  }
}

function showNotice() {
  const box = $('notice');
  box.hidden = !state.whole;
  document.documentElement.dataset.ranges = state.whole ? 'none' : 'served';
  if (!state.whole) return;
  box.replaceChildren(el('p', word('noRanges')));
  if (!state.left.length) return;
  box.append(el('p', word('noRangesLeft')));
  const list = el('ul');
  const several = state.site.chunks.length > 1;
  for (const {chunk, def, size} of state.left) {
    const name = (several ? `${chunk.label}: ` : '') + title(def.title);
    list.append(el('li', size ? `${name} (${Math.ceil(size / 1048576)} MB)` : name));
  }
  box.append(list);
}

function pictureLayer(def, projection, cog) {
  const full = cog.levels[0];
  const order = cog.levels.map((lv, i) => ({i, factor: 2 ** Math.round(Math.log2(full.width / lv.width))}))
    .sort((a, b) => b.factor - a.factor); // the coarsest first, as the map wants
  const grid = new ol.tilegrid.TileGrid({
    extent: cog.extent, origin: [cog.extent[0], cog.extent[3]],
    resolutions: order.map((o) => cog.resolution[0] * o.factor), tileSize: [full.tileWidth, full.tileHeight],
  });
  const source = new ol.source.DataTile({
    projection, tileGrid: grid, tileSize: [full.tileWidth, full.tileHeight], interpolate: true, transition: 0,
    loader: async (z, x, y) => {
      const picture = await cog.picture(order[z].i, x, y);
      if (picture) return picture;
      const blank = document.createElement('canvas');
      blank.width = full.tileWidth;
      blank.height = full.tileHeight;
      return blank;
    },
  });
  return new ol.layer.Tile({source, extent: cog.extent, visible: def.visible !== false});
}

function vectorStyle(def) {
  const groups = new Map((def.groups || []).map((g) => [g.name, g]));
  const by = def.group_by || 'layer';
  const cache = new Map();
  const wide = def.id === 'sections';
  return (feature) => {
    const p = feature.getProperties();
    const value = p[by];
    // a value in two languages is in the group of either of its texts
    const group = value === undefined || value === null ? undefined
      : isWords(value) ? Object.values(value).map(String).find((x) => groups.has(x)) : String(value);
    if (group !== undefined && state.hidden.has(`${def.url}|${group}`)) return null;
    const known = groups.get(group);
    const colour = colourOf(p.colour, colourOf(known && known.colour, colourOf(def.colour, '#ffb000')));
    const label = p.label === undefined || p.label === null ? '' : text(p.label);
    const kind = feature.getGeometry().getType();
    const key = `${colour}|${kind}|${label}`;
    if (!cache.has(key)) {
      const text = label ? new ol.style.Text({
        text: label, font: '12px system-ui, sans-serif', overflow: kind !== 'Point',
        placement: wide && kind === 'LineString' ? 'line' : 'point',
        offsetY: kind === 'Point' ? -11 : 0, fill: new ol.style.Fill({color: '#111'}),
        stroke: new ol.style.Stroke({color: 'rgba(255,255,255,0.85)', width: 3}),
      }) : undefined;
      cache.set(key, new ol.style.Style({
        stroke: new ol.style.Stroke({color: colour, width: wide ? 4 : 2}),
        fill: new ol.style.Fill({color: faded(colour, def.id === 'units' ? 0.12 : 0.2)}),
        image: new ol.style.Circle({radius: 4, fill: new ol.style.Fill({color: colour}),
          stroke: new ol.style.Stroke({color: '#fff', width: 1})}),
        text,
      }));
    }
    return cache.get(key);
  };
}

async function vectorLayer(def, projection) {
  const answer = await fetch(def.url);
  if (!answer.ok) throw new Error(`${def.url}: HTTP ${answer.status}`);
  const features = new ol.format.GeoJSON().readFeatures(await answer.json(),
    {dataProjection: projection, featureProjection: projection});
  return new ol.layer.Vector({source: new ol.source.Vector({features}), style: vectorStyle(def),
    visible: def.visible !== false, declutter: true});
}

function sketchLayer() {
  return new ol.layer.Vector({source: new ol.source.Vector(), style: new ol.style.Style({
    stroke: new ol.style.Stroke({color: '#0b6bcb', width: 2}),
    fill: new ol.style.Fill({color: 'rgba(11, 107, 203, 0.15)'}),
  })});
}

// ---------------------------------------------------------------------------------------------- the side
function check(checked, onChange) {
  const box = el('input');
  box.type = 'checkbox';
  box.checked = checked;
  box.addEventListener('change', () => onChange(box.checked));
  return box;
}

function groupRows(box, def, layer) {
  for (const group of def.groups || []) {
    const key = `${def.url}|${group.name}`;
    const sub = el('label', null, 'layer group');
    const mark = check(!state.hidden.has(key), (on) => {
      if (on) state.hidden.delete(key);
      else state.hidden.add(key);
      layer.changed();
    });
    const swatch = el('span', null, 'swatch');
    swatch.style.background = colourOf(group.colour, '#888');
    sub.append(mark, swatch, el('span', title(group.title)), el('span', group.count, 'count'));
    box.append(sub);
  }
}

function modelList(box) {
  const several = shownChunks().length > 1;
  let last = null;
  state.models.forEach((model, index) => {
    if (!shown(model.chunk)) return;
    if (several && last !== model.chunk) box.append(el('h3', model.chunk.label));
    last = model.chunk;
    const row = el('label', null, 'layer');
    row.dataset.layer = model.def.id;
    row.append(check(model.def.visible !== false, (on) => {
      model.def.visible = on;
      if (state.view3d) state.view3d.setVisible(index, on && shown(model.chunk));
    }), el('span', title(model.def.title)));
    if (model.def.faces) row.append(el('span', `${Math.round(model.def.faces / 1000)}k △`, 'count'));
    if (model.def.points) row.append(el('span', `${(model.def.points / 1e6).toFixed(2)} M`, 'count'));
    box.append(row);
    if (model.def.download) {
      const link = el('a', word('downloadCloud'), 'download');
      link.href = model.def.download;
      link.setAttribute('download', '');
      box.append(link);
    }
  });
}

function sectionList(box) {
  const items = state.sections.filter((s) => shown(s.chunk));
  if (!items.length) {
    box.append(el('p', word('noSections'), 'note'));
    return;
  }
  const several = shownChunks().length > 1;
  let last = null;
  for (const item of items) {
    if (several && last !== item.chunk) box.append(el('h3', item.chunk.label));
    last = item.chunk;
    const button = el('button', item.section.name, 'section-item');
    button.type = 'button';
    button.title = item.section.kind === 'section' ? word('section') : word('elevation');
    button.dataset.section = item.section.id;
    if (state.section === item) button.classList.add('on');
    button.addEventListener('click', () => openSection(item));
    box.append(button);
  }
  const current = state.section;
  if (!current || !shown(current.chunk)) return;
  if (current.drawing) groupRows(box, current.section.drawing, current.drawing);
  for (const extra of current.extra || []) { // the layers plugins put on this elevation
    const row = el('label', null, 'layer');
    row.dataset.layer = extra.def.id;
    row.append(check(extra.def.visible !== false, (on) => {
      extra.def.visible = on;
      extra.layer.setVisible(on);
    }));
    const swatch = el('span', null, 'swatch');
    swatch.style.background = colourOf(extra.def.colour, '#888');
    row.append(swatch, el('span', title(extra.def.title)));
    if (extra.def.count && !extra.def.groups) row.append(el('span', extra.def.count, 'count'));
    box.append(row);
    groupRows(box, extra.def, extra.layer);
  }
  const tools = el('div', null, 'buttons');
  if (current.section.trace) {
    const toPlan = el('button', word('showOnPlan'));
    toPlan.type = 'button';
    toPlan.id = 'show-on-plan';
    toPlan.addEventListener('click', () => showOnPlan(current));
    tools.append(toPlan);
  }
  if (current.section.svg) {
    const link = el('a', word('drawingSvg'), 'download');
    link.href = current.section.svg;
    link.target = '_blank';
    link.rel = 'noopener';
    tools.append(link);
  }
  box.append(tools);
}

function layerList() {
  const box = $('layers');
  box.replaceChildren();
  if (state.tab === '3d') {
    modelList(box);
    return;
  }
  if (state.tab === 'sections') {
    sectionList(box);
    return;
  }
  if (state.tab.startsWith('x:')) return;
  const several = shownChunks().length > 1;
  for (const chunk of state.site.chunks) {
    if (!shown(chunk)) continue;
    if (several) box.append(el('h3', chunk.label));
    for (const entry of state.entries.filter((e) => e.chunk === chunk)) {
      const row = el('label', null, 'layer');
      row.dataset.layer = entry.def.id;
      row.append(check(entry.def.visible !== false, (on) => {
        entry.def.visible = on;
        entry.layer.setVisible(on && shown(chunk));
      }));
      if (entry.def.colour) {
        const swatch = el('span', null, 'swatch');
        swatch.style.background = colourOf(entry.def.colour, '#888');
        row.append(swatch);
      }
      row.append(el('span', title(entry.def.title)));
      if (entry.def.count && !entry.def.groups) row.append(el('span', entry.def.count, 'count'));
      box.append(row);
      groupRows(box, entry.def, entry.layer);
    }
  }
}

function fieldName(key, fields) {
  const known = (fields || {})[key] || (state.site.fields || {})[key];
  return known ? title(known) : key;
}

function rows(table, pairs) {
  for (const [name, value] of pairs) {
    const tr = el('tr');
    tr.append(el('td', name), el('td', value));
    table.append(tr);
  }
}

function showInfo() {
  const box = $('info');
  box.replaceChildren();
  const last = state.last;
  if (!last || last.tab !== state.tab) {
    const hints = {'3d': 'hint3d', sections: 'hintSection'};
    box.append(el('p', word(hints[state.tab] || 'hint'), 'note'));
    return;
  }
  const table = el('table');
  if (last.tab === 'sections') {
    rows(table, [[word('u'), number(last.x)], [last.heights ? word('height') : word('v'), number(last.y)]]);
  } else {
    rows(table, [[word('x'), number(last.x)], [word('y'), number(last.y)],
      [word('height'), last.height === null ? word('noHeight') : number(last.height)]]);
  }
  box.append(table);
  if (last.model && state.site.chunks.length > 1) box.append(el('p', `${word('model')}: ${last.model}`, 'note'));
  for (const {properties, fields} of last.features) {
    const {label, colour, kind, section, ...others} = properties; // of the drawing, not for reading
    box.append(el('h3', (label ? text(label) : '') || others.layer || ''));
    const list = el('table');
    rows(list, Object.entries(others).filter(([, v]) => v !== null && v !== '')
      .map(([k, v]) => [fieldName(k, fields), text(v)]));
    box.append(list);
  }
}

function showWords() {
  const site = state.site;
  const extra = state.tab.startsWith('x:');
  document.documentElement.lang = state.language;
  $('description').textContent = site.description || '';
  $('layers-title').textContent = word(state.tab === 'sections' ? 'sections' : 'layers');
  $('measure-title').textContent = word('measure');
  $('info-title').textContent = word('info');
  $('distance').textContent = word('distance');
  $('area').textContent = word('area');
  $('stop').textContent = word('stop');
  $('clear').textContent = word('clear');
  $('fit').textContent = word(state.tab === '3d' ? 'home' : 'fit');
  $('fit').hidden = extra;
  $('measure-box').hidden = extra;
  $('info-box').hidden = extra;
  $('tab-plan').textContent = word('plan');
  $('tab-3d').textContent = word('model3d');
  $('tab-sections').textContent = word('sections');
  for (const x of state.extras) {
    if (x.button) x.button.textContent = title(x.title);
  }
  for (const entry of state.entries) { // labels given in both languages: drawn again in this one
    if (entry.layer instanceof ol.layer.Vector) entry.layer.changed();
  }
  $('language').textContent = word('language');
  const foot = $('foot');
  foot.replaceChildren(el('p', site.frame === 'local' ? word('local') : `${word('crs')}: ${site.crs || ''}`));
  const generator = site.generator || {};
  const libraries = state.models.length ? 'OpenLayers · three.js · 3DTilesRendererJS' : 'OpenLayers';
  foot.append(el('p', `${word('made')} ${generator.name || ''} ${generator.version || ''} · ${libraries}`));
  dateBar();
  layerList();
  showNotice();
  showInfo();
  if (state.tab === '3d') showMeasure3d(state.result3d);
  else if (!state.draw) $('measure-out').textContent = '';
}

// ---------------------------------------------------------------------------------------------- reading
async function heightAt(x, y) {
  for (const {chunk, cog} of state.dems) {
    if (!shown(chunk)) continue;
    const value = await cog.valueAt(x, y);
    if (value !== null) return value;
  }
  return null;
}

function featuresAt(map, pixel, skip) {
  const found = [];
  map.forEachFeatureAtPixel(pixel, (feature, layer) => {
    if (!layer || skip.includes(layer)) return;
    const {geometry, ...properties} = feature.getProperties();
    const extras = state.section && state.tab === 'sections' ? state.section.extra || [] : [];
    const entry = state.entries.find((e) => e.layer === layer) || extras.find((e) => e.layer === layer);
    found.push({properties, fields: entry ? entry.def.fields : null, layer, def: entry ? entry.def : null});
  }, {hitTolerance: 5});
  return found;
}

async function clicked(event) {
  if (state.draw) return;
  const [x, y] = event.coordinate;
  const features = featuresAt(state.map, event.pixel, [state.sketch, state.highlight]);
  const trace = features.find((f) => f.def && f.def.id === 'sections' && f.properties.section !== undefined);
  if (trace) { // the trace of a section: the section opens
    const item = state.sections.find((s) => s.section.id === String(trace.properties.section));
    if (item) {
      state.section = item;
      await setTab('sections');
      return;
    }
  }
  let height = null;
  try {
    height = await heightAt(x, y);
  } catch (error) {
    console.error(error);
  }
  state.last = {x, y, height, features, tab: 'plan'};
  showInfo();
}

// ---------------------------------------------------------------------------------------------- measuring
function measured(geometry) {
  if (geometry.getType() === 'Polygon') {
    const ring = new ol.geom.LineString(geometry.getLinearRing(0).getCoordinates());
    return `${word('surface')}: ${number(geometry.getArea(), 2)} m² · ${word('perimeter')}: ` +
      `${number(ring.getLength(), 2)} m`;
  }
  return `${word('length')}: ${number(geometry.getLength(), 3)} m`;
}

function measure(type) {
  endMeasure();
  if (state.tab === '3d') {
    if (!state.view3d) return;
    state.view3d.measure(type === 'Polygon' ? 'area' : 'distance');
    state.result3d = null;
    $(type === 'Polygon' ? 'area' : 'distance').classList.add('on');
    $('stop').hidden = false;
    $('measure-out').textContent = word('measureHint3d');
    return;
  }
  const onSection = state.tab === 'sections';
  const map = onSection ? state.sectionMap : state.map;
  const sketch = onSection ? state.sectionSketch : state.sketch;
  if (!map || !sketch) return;
  sketch.getSource().clear();
  const draw = new ol.interaction.Draw({source: sketch.getSource(), type});
  draw.on('drawstart', (event) => {
    sketch.getSource().clear();
    event.feature.getGeometry().on('change', (change) => {
      $('measure-out').textContent = measured(change.target);
    });
  });
  draw.on('drawend', (event) => {
    $('measure-out').textContent = measured(event.feature.getGeometry());
  });
  map.addInteraction(draw);
  state.draw = draw;
  state.drawMap = map;
  $(type === 'Polygon' ? 'area' : 'distance').classList.add('on');
  $('stop').hidden = false;
  $('measure-out').textContent = word(onSection ? 'measureHintSection' : 'measureHint');
}

function endMeasure() {
  if (state.draw && state.drawMap) state.drawMap.removeInteraction(state.draw);
  state.draw = null;
  state.drawMap = null;
  if (state.view3d && state.view3d.mode) state.result3d = state.view3d.finish();
  $('distance').classList.remove('on');
  $('area').classList.remove('on');
  $('stop').hidden = true;
}

// Esc: the measure in progress is dropped, on the plan and on a section as on the model
function cancelMeasure() {
  if (state.draw) state.draw.abortDrawing();
  if (state.view3d && state.view3d.mode) {
    state.view3d.clear();
    state.result3d = null;
  }
  endMeasure();
  $('measure-out').textContent = '';
}

function clearMeasure() {
  cancelMeasure();
  if (state.sketch) state.sketch.getSource().clear();
  if (state.sectionSketch) state.sectionSketch.getSource().clear();
  if (state.view3d) state.view3d.clear();
  state.result3d = null;
}

// ---------------------------------------------------------------------------------------------- the model
function showMeasure3d(result) {
  const out = $('measure-out');
  if (!result || result.points < 2) {
    out.textContent = state.view3d && state.view3d.mode ? word('measureHint3d') : '';
    return;
  }
  const sign = (v) => (v > 0 ? '+' : '') + number(v, 3);
  const lines = result.kind === 'area' && result.area !== undefined
    ? [`${word('area3d')}: ${number(result.area, 2)} m²`, `${word('planArea')}: ${number(result.planArea, 2)} m²`,
      `${word('perimeter')}: ${number(result.length, 2)} m`]
    : [`${word('length3d')}: ${number(result.length, 3)} m`, `${word('planLength')}: ${number(result.plan, 3)} m`,
      `${word('rise')}: ${sign(result.rise)} m`];
  out.textContent = lines.join('\n');
}

async function openModel() {
  if (state.view3d) return state.view3d;
  if (!state.opening) {
    state.opening = import('./view3d.js').then(({createView}) => createView($('view3d'), state.models));
  }
  const view = await state.opening;
  view.onPick = (point) => {
    state.last = {x: point.x, y: point.y, height: point.z, features: [], tab: '3d', model: point.chunk.label};
    showInfo();
  };
  view.onMeasure = (result, ended) => {
    state.result3d = result;
    if (ended) endMeasure();
    showMeasure3d(result);
  };
  state.view3d = view;
  applyVisibility();
  return view;
}

// ---------------------------------------------------------------------------------------------- the sections
function sectionPick(event) {
  if (state.draw || !state.section) return;
  const [u, v] = event.coordinate;
  const features = featuresAt(state.sectionMap, event.pixel, [state.sectionSketch]);
  state.last = {x: u, y: v, heights: state.section.section.heights, features, tab: 'sections'};
  showInfo();
}

// ``item`` ({chunk, section}) on the map of the sections; ``keep``: the same place of the frame stays in view
async function openSection(item, keep = false) {
  endMeasure();
  const previous = state.sectionMap ? state.sectionMap.getView() : null;
  state.section = item;
  document.documentElement.dataset.section = '';
  const sec = item.section;
  const ext = sec.extent;
  const projection = new ol.proj.Projection({code: `PPG:SEZ:${sec.id}`, units: 'm', extent: ext});
  const layers = [];
  item.drawing = null;
  if (sec.ortho) {
    const cog = await raster(item.chunk, {url: sec.ortho.url, title: sec.name});
    if (cog) layers.push(pictureLayer({visible: true}, projection, cog));
  }
  if (sec.drawing) {
    item.drawing = await vectorLayer({url: sec.drawing.url, groups: sec.drawing.groups, id: 'section'}, projection);
    layers.push(item.drawing);
  }
  item.extra = item.extra || [];
  const kept = new Map(item.extra.map((x) => [x.def.id, x.def.visible]));
  item.extra = [];
  for (const def of sec.layers || []) { // what plugins put on it, in the same (u, v)
    try {
      if (kept.has(def.id)) def.visible = kept.get(def.id);
      const layer = await vectorLayer(def, projection);
      item.extra.push({def, layer});
      layers.push(layer);
    } catch (error) {
      console.error(error);
    }
  }
  state.sectionSketch = sketchLayer();
  layers.push(state.sectionSketch);
  const pad = Math.max(ext[2] - ext[0], ext[3] - ext[1], 0.1);
  const view = new ol.View({
    projection, center: [(ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2],
    extent: [ext[0] - pad, ext[1] - pad, ext[2] + pad, ext[3] + pad],
    resolution: pad / 256, minResolution: 0.0002, maxResolution: Math.max(pad / 128, 0.001),
  });
  if (!state.sectionMap) {
    state.sectionMap = new ol.Map({
      target: 'section-map', layers,
      controls: ol.control.defaults.defaults({attribution: false, rotate: false})
        .extend([new ol.control.ScaleLine({units: 'metric'})]),
      view,
    });
    state.sectionMap.on('singleclick', sectionPick);
  } else {
    state.sectionMap.setLayers(layers);
    state.sectionMap.setView(view);
  }
  state.sectionMap.updateSize();
  if (keep && previous && previous.getCenter()) {
    view.setCenter(previous.getCenter());
    view.setResolution(previous.getResolution());
  } else {
    view.fit(ext, {padding: [24, 24, 24, 24]});
  }
  $('section-title').textContent = `${sec.name} · ${sec.kind === 'section' ? word('section') : word('elevation')}`
    + (sec.heights ? ` · ${word('vHeight')}` : '');
  state.sectionMap.once('rendercomplete', () => {
    document.documentElement.dataset.section = sec.id;
  });
  state.sectionMap.render();
  if (state.tab === 'sections') {
    state.last = null;
    showWords();
  }
}

// the trace of ``item`` on the plan, drawn over it and in view
async function showOnPlan(item) {
  await setTab('plan');
  const feature = new ol.format.GeoJSON().readFeature({type: 'Feature', geometry: item.section.trace, properties: {}});
  state.highlight.getSource().clear();
  state.highlight.getSource().addFeature(feature);
  state.map.getView().fit(feature.getGeometry().getExtent(), {padding: [80, 80, 80, 80]});
  document.documentElement.dataset.highlight = item.section.id;
}

// ---------------------------------------------------------------------------------------------- the plugins
async function showExtra(key) {
  const extra = state.extras.find((x) => x.key === key);
  const box = $('extra');
  box.replaceChildren();
  document.documentElement.dataset.extra = '';
  if (!extra) return;
  const {showPage, showTable} = await import('./extras.js');
  const several = shownChunks().length > 1;
  for (const {chunk, entry} of extra.entries) {
    if (!shown(chunk)) continue;
    const part = el('section', null, 'extra-part');
    part.append(el('h2', several ? `${title(entry.title)} · ${chunk.label}` : title(entry.title)));
    try {
      if (entry.kind === 'table') await showTable(part, entry, title, text);
      else await showPage(part, {...entry, url: isWords(entry.url) ? title(entry.url) : entry.url});
    } catch (error) {
      console.error(error);
      part.append(el('p', `${word('failed')} (${error.message})`, 'failed'));
    }
    box.append(part);
  }
  if (!box.children.length) box.append(el('p', word('nothingThisDate'), 'note'));
  document.documentElement.dataset.extra = key;
}

// ---------------------------------------------------------------------------------------------- the tabs
async function setTab(name) {
  if (name === state.tab) return;
  endMeasure();
  state.tab = name;
  const tabs = [['plan', $('tab-plan')], ['3d', $('tab-3d')], ['sections', $('tab-sections')],
    ...state.extras.map((x) => [`x:${x.key}`, x.button])];
  for (const [key, button] of tabs) {
    if (!button) continue;
    button.classList.toggle('on', key === name);
    button.setAttribute('aria-selected', String(key === name));
  }
  $('map').hidden = name !== 'plan';
  $('view3d').hidden = name !== '3d';
  $('sections').hidden = name !== 'sections';
  $('extra').hidden = !name.startsWith('x:');
  if (state.view3d) state.view3d.active = name === '3d';
  if (name === 'plan') {
    showWords();
    state.map.updateSize();
    return;
  }
  if (name === 'sections') {
    showWords();
    const wanted = state.section && shown(state.section.chunk) ? state.section
      : state.sections.find((s) => shown(s.chunk));
    if (wanted) await openSection(wanted);
    return;
  }
  if (name.startsWith('x:')) {
    showWords();
    await showExtra(name.slice(2));
    return;
  }
  showWords();
  try {
    const view = await openModel();
    view.active = state.tab === '3d';
    document.documentElement.dataset.model = 'ready';
  } catch (error) {
    console.error(error);
    document.documentElement.dataset.model = 'error';
    $('view3d').replaceChildren(el('p', `${word('failed3d')} (${error.message})`, 'failed'));
  }
}

// ---------------------------------------------------------------------------------------------- start
function extentOf(site) {
  const boxes = site.chunks.map((c) => c.extent);
  return [Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])),
    Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))];
}

function firstLanguage(site) {
  const offered = (site.languages || ['it']).filter((x) => WORDS[x]);
  let asked = null;
  try {
    asked = window.localStorage.getItem('ppg-site-language');
  } catch (error) { /* no storage: the language of the browser */ }
  const wanted = [asked, (navigator.language || '').slice(0, 2)];
  return wanted.find((x) => offered.includes(x)) || offered[0] || 'it';
}

// the sections of the chunks and the tabs of the plugins (one tab for an id of a plugin, over the chunks)
function collect(site) {
  for (const chunk of site.chunks) {
    for (const section of chunk.sections || []) state.sections.push({chunk, section});
    for (const entry of chunk.plugins || []) {
      if (entry.kind !== 'table' && entry.kind !== 'html') continue;
      const key = `${entry.plugin}:${entry.id}`;
      let extra = state.extras.find((x) => x.key === key);
      if (!extra) {
        extra = {key, title: entry.title, entries: [], button: null};
        state.extras.push(extra);
      }
      extra.entries.push({chunk, entry});
    }
  }
}

async function start() {
  const answer = await fetch('site.json');
  if (!answer.ok) throw new Error(`site.json: HTTP ${answer.status}`);
  const site = await answer.json();
  if (site.format !== 'ppg-site') throw new Error('site.json: not a site of this program');
  state.site = site;
  state.language = firstLanguage(site);
  if (site.timeline && site.timeline.length > 1) {
    state.epoch = site.timeline.length - 1; // the latest survey first
    document.documentElement.dataset.epoch = String(state.epoch);
  }
  document.title = site.title || document.title;
  $('title').textContent = site.title || '';
  collect(site);

  const extent = extentOf(site);
  const projection = new ol.proj.Projection({code: 'PPG:PLAN', units: 'm', extent});
  const order = {ortho: 0, hillshade: 1, units: 2, drawings: 3, sections: 4};
  const layers = [];
  for (const chunk of site.chunks) {
    for (const def of [...chunk.layers].sort((a, b) => (order[a.id] ?? 9) - (order[b.id] ?? 9))) {
      if (def.type === 'tiles' || def.type === 'points') { // the model and the cloud: in the 3D tab
        state.models.push({chunk, def});
        continue;
      }
      const cog = def.type === 'vector' ? null : await raster(chunk, def);
      if (def.type !== 'vector' && !cog) continue; // left out: the notice names it
      if (def.type === 'heights') {
        state.dems.push({chunk, cog});
        continue;
      }
      const layer = def.type === 'image' ? pictureLayer(def, projection, cog) : await vectorLayer(def, projection);
      layer.setVisible(def.visible !== false && shown(chunk));
      state.entries.push({chunk, def, layer});
      layers.push(layer);
    }
  }
  // pictures under the drawings, whatever the chunk
  layers.sort((a, b) => (a instanceof ol.layer.Vector) - (b instanceof ol.layer.Vector));
  state.sketch = sketchLayer();
  state.highlight = new ol.layer.Vector({source: new ol.source.Vector(), style: new ol.style.Style({
    stroke: new ol.style.Stroke({color: '#ffde00', width: 8}),
  })});
  const pad = Math.max(extent[2] - extent[0], extent[3] - extent[1]);
  state.map = new ol.Map({
    target: 'map',
    layers: [...layers, state.highlight, state.sketch],
    controls: ol.control.defaults.defaults({attribution: false}).extend([new ol.control.ScaleLine({units: 'metric'})]),
    view: new ol.View({
      projection, center: [(extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2],
      extent: [extent[0] - pad, extent[1] - pad, extent[2] + pad, extent[3] + pad],
      resolution: pad / 256, minResolution: 0.0002, maxResolution: Math.max(pad / 128, 0.001),
    }),
  });
  const fit = () => state.map.getView().fit(extent, {padding: [24, 24, 24, 24]});
  fit();
  state.map.on('singleclick', clicked);
  $('fit').addEventListener('click', () => {
    if (state.tab === '3d' && state.view3d) state.view3d.home();
    else if (state.tab === 'sections' && state.section && state.sectionMap) {
      state.sectionMap.getView().fit(state.section.section.extent, {padding: [24, 24, 24, 24]});
    } else fit();
  });
  $('distance').addEventListener('click', () => measure('LineString'));
  $('area').addEventListener('click', () => measure('Polygon'));
  $('stop').addEventListener('click', () => {
    endMeasure();
    if (state.tab === '3d') showMeasure3d(state.result3d);
  });
  $('clear').addEventListener('click', clearMeasure);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') cancelMeasure();
  });
  $('tab-3d').hidden = !state.models.length;
  $('tab-sections').hidden = !state.sections.length;
  for (const extra of state.extras) {
    const button = el('button', null, 'tab');
    button.type = 'button';
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-selected', 'false');
    button.dataset.tab = `x:${extra.key}`;
    button.addEventListener('click', () => setTab(`x:${extra.key}`));
    $('tabs').append(button);
    extra.button = button;
  }
  if (state.models.length || state.sections.length || state.extras.length) {
    $('tabs').hidden = false;
    $('tab-plan').addEventListener('click', () => setTab('plan'));
    $('tab-3d').addEventListener('click', () => setTab('3d'));
    $('tab-sections').addEventListener('click', () => setTab('sections'));
  }
  const languages = (site.languages || []).filter((x) => WORDS[x]);
  if (languages.length > 1) {
    $('language').hidden = false;
    $('language').addEventListener('click', () => {
      state.language = languages[(languages.indexOf(state.language) + 1) % languages.length];
      try {
        window.localStorage.setItem('ppg-site-language', state.language);
      } catch (error) { /* not kept: asked again the next time */ }
      showWords();
      if (state.tab === 'sections' && state.section) openSection(state.section, true);
      if (state.tab.startsWith('x:')) showExtra(state.tab.slice(2));
    });
  }
  showWords();
  window.ppgSite = {map: state.map, site, state, heightAt, measured, setTab, openModel, openSection, setEpoch,
    showOnPlan};
  const nothingOnPlan = !state.entries.length && !state.dems.length;
  if (nothingOnPlan && (state.models.length || state.sections.length)) {
    await setTab(state.models.length ? '3d' : 'sections'); // a survey without a plan opens on what it has
    document.documentElement.dataset.state = 'ready';
    return;
  }
  state.map.once('rendercomplete', () => {
    document.documentElement.dataset.state = 'ready';
  });
}

start().catch((error) => {
  console.error(error);
  document.documentElement.dataset.state = 'error';
  const box = $('map');
  box.replaceChildren(el('p', `${word('failed')} (${error.message})`, 'failed'));
});
