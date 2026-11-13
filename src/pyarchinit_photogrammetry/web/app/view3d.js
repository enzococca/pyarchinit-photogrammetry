// The 3D view of a survey: the models of the chunks as 3D Tiles, without a globe, Z up, in the frame of the site.
// A click gives the coordinates of the point of the model under the pointer; distances (polyline: 3D length,
// plan length, height difference) and areas (polygon: 3D area, plan area, perimeter) are measured on the
// model. Points are picked by ray casting on the tiles shown (the finest loaded at that moment).
//
// Coordinates: the tiles of a site in the local frame are in the metres of the plan; those of a site in its
// reference system are around the centre of each model (layer.origin), placed on the globe by the transform of
// their root, which is undone here. The scene is drawn around the centre of all the models (`centre`), so that
// single precision on the graphics card keeps the millimetres; every coordinate shown adds it back.

import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {TilesRenderer} from './vendor/3d-tiles-renderer/index.three.js';

const MARK = 0x0b6bcb;

// The area vector of a closed polygon (Newell): its length is the area on the mean plane, its z the plan area.
export function areaVector(points) {
  const n = new THREE.Vector3();
  for (let i = 0; i < points.length; i++) {
    const a = points[i];
    const b = points[(i + 1) % points.length];
    n.x += (a.y - b.y) * (a.z + b.z);
    n.y += (a.z - b.z) * (a.x + b.x);
    n.z += (a.x - b.x) * (a.y + b.y);
  }
  return n.multiplyScalar(0.5);
}

// What a polyline or a polygon of points ({x, y, z}) measures, in metres.
export function measures(points, closed) {
  let length = 0;
  let plan = 0;
  const count = closed ? points.length : points.length - 1;
  for (let i = 0; i < count; i++) {
    const a = points[i];
    const b = points[(i + 1) % points.length];
    length += Math.hypot(b.x - a.x, b.y - a.y, b.z - a.z);
    plan += Math.hypot(b.x - a.x, b.y - a.y);
  }
  const out = {points: points.length, length, plan};
  if (points.length) out.rise = points[points.length - 1].z - points[0].z;
  if (closed) {
    const n = areaVector(points);
    out.area = n.length();
    out.planArea = Math.abs(n.z);
  }
  return out;
}

function boxOf(models) {
  const lo = [Infinity, Infinity, Infinity];
  const hi = [-Infinity, -Infinity, -Infinity];
  for (const m of models) {
    for (let i = 0; i < 3; i++) {
      lo[i] = Math.min(lo[i], m.def.box[i]);
      hi[i] = Math.max(hi[i], m.def.box[i + 3]);
    }
  }
  return {lo, hi};
}

// models: [{chunk, def}] with def the "tiles" layer of site.json. Returns the view, or throws when the browser
// has no WebGL.
export async function createView(container, models) {
  const renderer = new THREE.WebGLRenderer({antialias: true});
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setClearColor(0xdfe3e6);
  container.append(renderer.domElement);

  const {lo, hi} = boxOf(models);
  const centre = new THREE.Vector3((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2);
  const span = Math.max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], 0.1);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, span / 2000, span * 200);
  camera.up.set(0, 0, 1);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = false;
  controls.zoomToCursor = true;
  controls.screenSpacePanning = true;
  controls.minDistance = span / 1000;
  controls.maxDistance = span * 20;

  const loaded = [];
  for (const model of models) {
    const answer = await fetch(model.def.url);
    if (!answer.ok) throw new Error(`${model.def.url}: HTTP ${answer.status}`);
    const tileset = await answer.json();
    const holder = new THREE.Group(); // the model in the scene: its coordinates less the centre of the view
    const origin = model.def.origin || [0, 0, 0];
    holder.position.set(origin[0] - centre.x, origin[1] - centre.y, origin[2] - centre.z);
    const tiles = new TilesRenderer(model.def.url);
    if (tileset.root && Array.isArray(tileset.root.transform)) { // a tileset on the globe: back to its own frame
      const placed = new THREE.Matrix4().fromArray(tileset.root.transform);
      tiles.group.matrixAutoUpdate = false;
      tiles.group.matrix.copy(placed.invert());
    }
    holder.add(tiles.group);
    holder.visible = model.def.visible !== false;
    scene.add(holder);
    tiles.setCamera(camera);
    tiles.setResolutionFromRenderer(camera, renderer);
    // the pages of the texture: loaded once, shared by the tiles (the tiles carry the page in their material)
    const base = new URL(model.def.url, window.location.href);
    const pages = ((tileset.extras || {}).textures || []).map((name) => {
      const texture = new THREE.TextureLoader().load(new URL(name, base).href);
      texture.flipY = false;
      texture.colorSpace = THREE.SRGBColorSpace;
      // no mipmaps: the faces of a model have their own small cells in the texture, and the coarser images of a
      // mipmap would blend each cell with its neighbours (light seams along the edges)
      texture.generateMipmaps = false;
      texture.minFilter = THREE.LinearFilter;
      return texture;
    });
    tiles.addEventListener('load-model', ({scene: content}) => {
      content.traverse((object) => {
        const material = object.material;
        if (!material || material.userData.page === undefined) return;
        const page = pages[material.userData.page];
        if (page) {
          material.map = page; // after the tile listed its own textures: the page is never disposed with it
          material.needsUpdate = true;
        }
      });
    });
    tiles.addEventListener('load-error', ({error, url}) => console.error(`${url}: ${error && error.message}`));
    loaded.push({...model, tiles, holder, tileset});
  }

  // the marks of a measure: points over the model, the line between them
  const marks = new THREE.Group();
  marks.renderOrder = 10;
  scene.add(marks);
  const pointMaterial = new THREE.PointsMaterial({color: MARK, size: 9, sizeAttenuation: false, depthTest: false,
    transparent: true});
  const lineMaterial = new THREE.LineBasicMaterial({color: MARK, depthTest: false, transparent: true});

  const view = {
    renderer, camera, controls, scene, centre, models: loaded, active: false, mode: null, points: [],
    closed: false, onPick: null, onMeasure: null,
  };

  function draw() {
    marks.clear();
    if (!view.points.length) return;
    const local = view.points.map((p) => new THREE.Vector3(p.x - centre.x, p.y - centre.y, p.z - centre.z));
    const dots = new THREE.Points(new THREE.BufferGeometry().setFromPoints(local), pointMaterial);
    dots.renderOrder = 11;
    marks.add(dots);
    if (local.length > 1) {
      const kind = view.closed || (view.mode === 'area' && local.length > 2) ? THREE.LineLoop : THREE.Line;
      const line = new kind(new THREE.BufferGeometry().setFromPoints(local), lineMaterial);
      line.renderOrder = 10;
      marks.add(line);
    }
  }

  // the point of the model under a pixel of the page (clientX, clientY), in the frame of the site; null if none
  function pick(clientX, clientY) {
    const rect = renderer.domElement.getBoundingClientRect();
    const ndc = new THREE.Vector2(((clientX - rect.left) / rect.width) * 2 - 1,
      -((clientY - rect.top) / rect.height) * 2 + 1);
    const ray = new THREE.Raycaster();
    ray.setFromCamera(ndc, camera);
    let best = null;
    for (const m of loaded) {
      if (!m.holder.visible) continue;
      const hits = ray.intersectObject(m.tiles.group, true);
      if (hits.length && (!best || hits[0].distance < best.hit.distance)) best = {hit: hits[0], model: m};
    }
    if (!best) return null;
    const p = best.hit.point;
    return {x: p.x + centre.x, y: p.y + centre.y, z: p.z + centre.z, chunk: best.model.chunk};
  }

  // a point of the frame of the site on the page: [clientX, clientY]
  view.project = (x, y, z) => {
    const v = new THREE.Vector3(x - centre.x, y - centre.y, z - centre.z).project(camera);
    const rect = renderer.domElement.getBoundingClientRect();
    return [rect.left + ((v.x + 1) / 2) * rect.width, rect.top + ((1 - v.y) / 2) * rect.height];
  };
  view.pick = pick;

  view.home = () => {
    const radius = Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2 || 1;
    const distance = radius / Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) * 1.05;
    const direction = new THREE.Vector3(0, -1, 1.1).normalize(); // from the south, looking down at 48 degrees
    camera.position.copy(direction.multiplyScalar(distance));
    controls.target.set(0, 0, 0);
    camera.lookAt(controls.target);
    controls.update();
  };

  view.measure = (mode) => {
    view.mode = mode;
    view.points = [];
    view.closed = false;
    draw();
  };
  view.finish = () => { // the measure ends: the drawing and its value stay
    if (view.mode === 'area' && view.points.length > 2) view.closed = true;
    view.mode = null;
    draw();
    return view.result();
  };
  view.clear = () => {
    view.mode = null;
    view.points = [];
    view.closed = false;
    draw();
  };
  view.result = () => {
    if (!view.points.length) return null;
    const area = view.closed || view.mode === 'area';
    return {kind: area ? 'area' : 'distance', ...measures(view.points, area && view.points.length > 2)};
  };
  view.setVisible = (index, visible) => {
    loaded[index].holder.visible = visible;
  };
  view.ready = () => loaded.every((m) => m.tiles.root && m.tiles.loadProgress === 1
    && m.tiles.stats.downloading + m.tiles.stats.parsing + m.tiles.stats.queued === 0)
    && loaded.some((m) => m.tiles.visibleTiles.size > 0);

  // a click (not a drag) picks a point; the second click of a double click ends the measure
  let down = null;
  renderer.domElement.addEventListener('pointerdown', (event) => {
    down = [event.clientX, event.clientY];
  });
  renderer.domElement.addEventListener('click', (event) => {
    if (!down || Math.hypot(event.clientX - down[0], event.clientY - down[1]) > 4) return;
    if (event.detail === 2 && view.mode) {
      if (view.onMeasure) view.onMeasure(view.finish(), true);
      return;
    }
    const point = pick(event.clientX, event.clientY);
    if (!point) return;
    if (view.mode) {
      view.points.push(point);
      draw();
      if (view.onMeasure) view.onMeasure(view.result(), false);
    } else {
      view.points = [point];
      view.closed = false;
      draw();
      if (view.onPick) view.onPick(point);
    }
  });

  function resize() {
    const w = Math.max(1, container.clientWidth);
    const h = Math.max(1, container.clientHeight);
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    for (const m of loaded) m.tiles.setResolutionFromRenderer(camera, renderer);
  }
  new ResizeObserver(resize).observe(container);
  resize();
  view.home();

  function frame() {
    if (view.active) {
      controls.update();
      scene.updateMatrixWorld();
      camera.updateMatrixWorld();
      for (const m of loaded) m.tiles.update();
      renderer.render(scene, camera);
    }
    window.requestAnimationFrame(frame);
  }
  window.requestAnimationFrame(frame);
  return view;
}
