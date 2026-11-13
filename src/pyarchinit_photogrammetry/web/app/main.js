// The plan of a survey: orthophoto, shaded relief, stratigraphic units and drawings from site.json, on a plain
// metric frame. A click gives the coordinates, the height of the DEM and the attributes; distances and areas
// are measured on the plan. The models of the chunks are in a 3D tab (view3d.js, loaded only when it is opened),
// measured in the same frame and in the same panel. Every text coming from the data is written as text, never
// as markup.

import {open} from './cog.js';
import {WORDS, pick} from './i18n.js';

const ol = window.ol;
const $ = (id) => document.getElementById(id);
const state = {site: null, language: 'it', map: null, entries: [], dems: [], hidden: new Set(), draw: null,
  last: null, whole: false, left: [], tab: 'plan', models: [], view3d: null, opening: null, result3d: null};

function el(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (cls) node.className = cls;
  return node;
}

function word(key) {
  return (WORDS[state.language] || WORDS.it)[key] || key;
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
    const name = (several ? `${chunk.label}: ` : '') + pick(def.title, state.language);
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
  const cache = new Map();
  return (feature) => {
    const p = feature.getProperties();
    if (p.layer !== undefined && state.hidden.has(`${def.url}|${p.layer}`)) return null;
    const group = groups.get(p.layer);
    const colour = colourOf(p.colour, colourOf(group && group.colour, colourOf(def.colour, '#ffb000')));
    const label = p.label === undefined || p.label === null ? '' : String(p.label);
    const kind = feature.getGeometry().getType();
    const key = `${colour}|${kind}|${label}`;
    if (!cache.has(key)) {
      const text = label ? new ol.style.Text({
        text: label, font: '12px system-ui, sans-serif', overflow: kind !== 'Point',
        offsetY: kind === 'Point' ? -11 : 0, fill: new ol.style.Fill({color: '#111'}),
        stroke: new ol.style.Stroke({color: 'rgba(255,255,255,0.85)', width: 3}),
      }) : undefined;
      cache.set(key, new ol.style.Style({
        stroke: new ol.style.Stroke({color: colour, width: 2}),
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

// ---------------------------------------------------------------------------------------------- the side
function modelList(box) {
  const several = state.site.chunks.length > 1;
  state.models.forEach((model, index) => {
    if (several) box.append(el('h3', model.chunk.label));
    const row = el('label', null, 'layer');
    const check = el('input');
    check.type = 'checkbox';
    check.checked = model.def.visible !== false;
    check.addEventListener('change', () => {
      model.def.visible = check.checked;
      if (state.view3d) state.view3d.setVisible(index, check.checked);
    });
    row.append(check, el('span', pick(model.def.title, state.language)));
    if (model.def.faces) row.append(el('span', `${Math.round(model.def.faces / 1000)}k △`, 'count'));
    box.append(row);
  });
}

function layerList() {
  const box = $('layers');
  box.replaceChildren();
  if (state.tab === '3d') {
    modelList(box);
    return;
  }
  const several = state.site.chunks.length > 1;
  for (const chunk of state.site.chunks) {
    if (several) box.append(el('h3', chunk.label));
    for (const entry of state.entries.filter((e) => e.chunk === chunk)) {
      const row = el('label', null, 'layer');
      const check = el('input');
      check.type = 'checkbox';
      check.checked = entry.layer.getVisible();
      check.addEventListener('change', () => entry.layer.setVisible(check.checked));
      row.append(check);
      if (entry.def.colour) {
        const swatch = el('span', null, 'swatch');
        swatch.style.background = colourOf(entry.def.colour, '#888');
        row.append(swatch);
      }
      row.append(el('span', pick(entry.def.title, state.language)));
      if (entry.def.count && !entry.def.groups) row.append(el('span', entry.def.count, 'count'));
      box.append(row);
      for (const group of entry.def.groups || []) {
        const key = `${entry.def.url}|${group.name}`;
        const sub = el('label', null, 'layer group');
        const mark = el('input');
        mark.type = 'checkbox';
        mark.checked = !state.hidden.has(key);
        mark.addEventListener('change', () => {
          if (mark.checked) state.hidden.delete(key);
          else state.hidden.add(key);
          entry.layer.changed();
        });
        const swatch = el('span', null, 'swatch');
        swatch.style.background = colourOf(group.colour, '#888');
        sub.append(mark, swatch, el('span', pick(group.title, state.language)), el('span', group.count, 'count'));
        box.append(sub);
      }
    }
  }
}

function fieldName(key) {
  const known = (state.site.fields || {})[key];
  return known ? pick(known, state.language) : key;
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
    box.append(el('p', word(state.tab === '3d' ? 'hint3d' : 'hint'), 'note'));
    return;
  }
  const table = el('table');
  rows(table, [[word('x'), number(last.x)], [word('y'), number(last.y)],
    [word('height'), last.height === null ? word('noHeight') : number(last.height)]]);
  box.append(table);
  if (last.model && state.site.chunks.length > 1) box.append(el('p', `${word('model')}: ${last.model}`, 'note'));
  for (const properties of last.features) {
    const {label, colour, kind, ...others} = properties; // colour and kind: of the drawing, not for reading
    box.append(el('h3', label || others.layer || ''));
    const list = el('table');
    rows(list, Object.entries(others).filter(([, v]) => v !== null && v !== '')
      .map(([k, v]) => [fieldName(k), typeof v === 'object' ? JSON.stringify(v) : v]));
    box.append(list);
  }
}

function showWords() {
  const site = state.site;
  document.documentElement.lang = state.language;
  $('description').textContent = site.description || '';
  $('layers-title').textContent = word('layers');
  $('measure-title').textContent = word('measure');
  $('info-title').textContent = word('info');
  $('distance').textContent = word('distance');
  $('area').textContent = word('area');
  $('stop').textContent = word('stop');
  $('clear').textContent = word('clear');
  $('fit').textContent = word(state.tab === '3d' ? 'home' : 'fit');
  $('tab-plan').textContent = word('plan');
  $('tab-3d').textContent = word('model3d');
  $('language').textContent = word('language');
  const foot = $('foot');
  foot.replaceChildren(el('p', site.frame === 'local' ? word('local') : `${word('crs')}: ${site.crs || ''}`));
  const generator = site.generator || {};
  const libraries = state.models.length ? 'OpenLayers · three.js · 3DTilesRendererJS' : 'OpenLayers';
  foot.append(el('p', `${word('made')} ${generator.name || ''} ${generator.version || ''} · ${libraries}`));
  layerList();
  showNotice();
  showInfo();
  if (state.tab === '3d') showMeasure3d(state.result3d);
  else if (!state.draw) $('measure-out').textContent = '';
}

// ---------------------------------------------------------------------------------------------- reading
async function heightAt(x, y) {
  for (const dem of state.dems) {
    const value = await dem.valueAt(x, y);
    if (value !== null) return value;
  }
  return null;
}

async function clicked(event) {
  if (state.draw) return;
  const [x, y] = event.coordinate;
  const features = [];
  state.map.forEachFeatureAtPixel(event.pixel, (feature, layer) => {
    if (!layer || layer === state.sketch) return;
    const {geometry, ...properties} = feature.getProperties();
    features.push(properties);
  }, {hitTolerance: 5});
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
  state.sketch.getSource().clear();
  const draw = new ol.interaction.Draw({source: state.sketch.getSource(), type});
  draw.on('drawstart', (event) => {
    state.sketch.getSource().clear();
    event.feature.getGeometry().on('change', (change) => {
      $('measure-out').textContent = measured(change.target);
    });
  });
  draw.on('drawend', (event) => {
    $('measure-out').textContent = measured(event.feature.getGeometry());
  });
  state.map.addInteraction(draw);
  state.draw = draw;
  $(type === 'Polygon' ? 'area' : 'distance').classList.add('on');
  $('stop').hidden = false;
  $('measure-out').textContent = word('measureHint');
}

function endMeasure() {
  if (state.draw) state.map.removeInteraction(state.draw);
  state.draw = null;
  if (state.view3d && state.view3d.mode) state.result3d = state.view3d.finish();
  $('distance').classList.remove('on');
  $('area').classList.remove('on');
  $('stop').hidden = true;
}

// Esc: the measure in progress is dropped, on the plan as on the model
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
  state.sketch.getSource().clear();
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
  return view;
}

async function setTab(name) {
  if (name === state.tab) return;
  endMeasure();
  state.tab = name;
  $('tab-plan').classList.toggle('on', name === 'plan');
  $('tab-3d').classList.toggle('on', name === '3d');
  $('tab-plan').setAttribute('aria-selected', String(name === 'plan'));
  $('tab-3d').setAttribute('aria-selected', String(name === '3d'));
  $('map').hidden = name !== 'plan';
  $('view3d').hidden = name !== '3d';
  showWords();
  if (name === 'plan') {
    if (state.view3d) state.view3d.active = false;
    state.map.updateSize();
    return;
  }
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

async function start() {
  const answer = await fetch('site.json');
  if (!answer.ok) throw new Error(`site.json: HTTP ${answer.status}`);
  const site = await answer.json();
  if (site.format !== 'ppg-site') throw new Error('site.json: not a site of this program');
  state.site = site;
  state.language = firstLanguage(site);
  document.title = site.title || document.title;
  $('title').textContent = site.title || '';

  const extent = extentOf(site);
  const projection = new ol.proj.Projection({code: 'PPG:PLAN', units: 'm', extent});
  const order = {ortho: 0, hillshade: 1, units: 2, drawings: 3};
  const layers = [];
  for (const chunk of site.chunks) {
    for (const def of [...chunk.layers].sort((a, b) => (order[a.id] ?? 9) - (order[b.id] ?? 9))) {
      if (def.type === 'tiles') { // the model: in the 3D tab
        state.models.push({chunk, def});
        continue;
      }
      const cog = def.type === 'vector' ? null : await raster(chunk, def);
      if (def.type !== 'vector' && !cog) continue; // left out: the notice names it
      if (def.type === 'heights') {
        state.dems.push(cog);
        continue;
      }
      const layer = def.type === 'image' ? pictureLayer(def, projection, cog) : await vectorLayer(def, projection);
      state.entries.push({chunk, def, layer});
      layers.push(layer);
    }
  }
  // pictures under the drawings, whatever the chunk
  layers.sort((a, b) => (a instanceof ol.layer.Vector) - (b instanceof ol.layer.Vector));
  state.sketch = new ol.layer.Vector({source: new ol.source.Vector(), style: new ol.style.Style({
    stroke: new ol.style.Stroke({color: '#0b6bcb', width: 2}),
    fill: new ol.style.Fill({color: 'rgba(11, 107, 203, 0.15)'}),
  })});
  const pad = Math.max(extent[2] - extent[0], extent[3] - extent[1]);
  state.map = new ol.Map({
    target: 'map',
    layers: [...layers, state.sketch],
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
  $('fit').addEventListener('click', () => (state.tab === '3d' && state.view3d ? state.view3d.home() : fit()));
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
  if (state.models.length) {
    $('tabs').hidden = false;
    $('tab-plan').addEventListener('click', () => setTab('plan'));
    $('tab-3d').addEventListener('click', () => setTab('3d'));
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
    });
  }
  showWords();
  window.ppgSite = {map: state.map, site, state, heightAt, measured, setTab, openModel};
  const nothingOnPlan = !state.entries.length && !state.dems.length;
  if (state.models.length && nothingOnPlan) {
    await setTab('3d'); // a survey with a model alone opens on it
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
