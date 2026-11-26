// The 3D view of a survey: the models and the point clouds of the chunks as 3D Tiles, without a globe, Z up, in the
// frame of the site. A cloud is a tileset of points refined by adding its finer tiles as the camera comes near.
// A click gives the coordinates of the point of the model under the pointer; distances (polyline: 3D length,
// plan length, height difference) and areas (polygon: 3D area, plan area, perimeter) are measured on the
// model. Points are picked by ray casting on the tiles shown (the finest loaded at that moment).
//
// Coordinates: the tiles of a site in the local frame are in the metres of the plan; those of a site in its
// reference system are around the centre of each model (layer.origin), placed on the globe by the transform of
// their root, which is undone here. The scene is drawn around the centre of all the models (`centre`), so that
// single precision on the graphics card keeps the millimetres; every coordinate shown adds it back.
//
// The unit volumes of a chunk (layer "volumes") are one glTF file (proxies.glb: a mesh and a node a volume, the
// node named by the key of the volume, Y up as the tiles) and what the page shows of each (proxies.json). They are
// drawn translucent, each in its colour; a click on a volume gives that unit, even behind the surface of the model
// (the volumes usually lie under it).

import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';
import {GLTFLoader} from 'three/addons/loaders/GLTFLoader.js';
import {TilesRenderer} from './vendor/3d-tiles-renderer/index.three.js';

const MARK = 0x0b6bcb;
const POINT_SIZE = 2; // pixels of a point of a cloud
const PICK_PIXELS = 6; // how near to the pointer a point of a cloud is picked
const VOLUME_OPACITY = 0.45;
const SELECTED_OPACITY = 0.8;

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

// The volumes of a "volumes" layer: {key: {mesh, data}} with the mesh of each node of the glTF ``gltf`` named by a
// volume of ``data`` (proxies.json), each with its own translucent material.
function volumesOf(gltf, data) {
  const byKey = new Map((data.volumes || []).map((v) => [v.key, v]));
  const nodes = (gltf.parser.json.nodes || []);
  const found = new Map();
  gltf.scene.traverse((object) => {
    if (!object.isMesh) return;
    // the name of the node as written (the loader makes the names of the objects safe for animations)
    const link = gltf.parser.associations.get(object);
    const name = link && link.nodes !== undefined && nodes[link.nodes] ? nodes[link.nodes].name : object.name;
    const volume = byKey.get(name);
    if (!volume) {
      object.visible = false;
      return;
    }
    object.material = new THREE.MeshBasicMaterial({color: new THREE.Color(volume.colour || '#888888'),
      transparent: true, opacity: VOLUME_OPACITY, depthWrite: false, side: THREE.DoubleSide});
    object.renderOrder = 1;
    found.set(name, {mesh: object, data: volume, outline: null});
  });
  return found;
}

// models: [{chunk, def}] with def a "tiles", "points" or "volumes" layer of site.json. Returns the view, or throws
// when the browser has no WebGL.
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
    if (model.def.type === 'volumes') {
      const answer = await fetch(model.def.data);
      if (!answer.ok) throw new Error(`${model.def.data}: HTTP ${answer.status}`);
      const data = await answer.json();
      const gltf = await new GLTFLoader().loadAsync(model.def.url);
      const holder = new THREE.Group();
      const origin = model.def.origin || [0, 0, 0];
      holder.position.set(origin[0] - centre.x, origin[1] - centre.y, origin[2] - centre.z);
      gltf.scene.rotation.x = Math.PI / 2; // Y up of glTF to the Z up of the site
      holder.add(gltf.scene);
      holder.visible = model.def.visible !== false;
      scene.add(holder);
      loaded.push({...model, holder, data, volumes: volumesOf(gltf, data), hiddenGroups: new Set(),
        shown: model.def.visible !== false});
      continue;
    }
    const answer = await fetch(model.def.url);
    if (!answer.ok) throw new Error(`${model.def.url}: HTTP ${answer.status}`);
    const tileset = await answer.json();
    const holder = new THREE.Group(); // the model in the scene: its coordinates less the centre of the view
    const origin = model.def.origin || [0, 0, 0];
    holder.position.set(origin[0] - centre.x, origin[1] - centre.y, origin[2] - centre.z);
    const tiles = new TilesRenderer(model.def.url);
    // the points of a node of a cloud leave gaps of their spacing between them, seen at once at two pixels a point:
    // its finer nodes are loaded sooner than the finer tiles of a model
    if (model.def.type === 'points') tiles.errorTarget = 4;
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
    const points = model.def.type === 'points';
    tiles.addEventListener('load-model', ({scene: content}) => {
      content.traverse((object) => {
        if (points && object.isPoints && object.material) {
          object.material.size = POINT_SIZE;
          object.material.sizeAttenuation = false;
          return;
        }
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
    loaded.push({...model, tiles, holder, tileset, points, shown: model.def.visible !== false});
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
    // a point of a cloud: within a few pixels of the ray at the distance of the centre of the view
    const pixel = 2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2))
      * camera.position.distanceTo(controls.target) / Math.max(1, rect.height);
    ray.params.Points.threshold = PICK_PIXELS * pixel;
    // a volume first, whatever lies in front of it: the volumes are usually under the surface of the model
    let volume = null;
    for (const m of loaded) {
      if (!m.volumes || !m.holder.visible) continue;
      const meshes = [...m.volumes.values()].filter((v) => v.mesh.visible).map((v) => v.mesh);
      const hits = ray.intersectObjects(meshes, false);
      if (hits.length && (!volume || hits[0].distance < volume.hit.distance)) {
        const item = [...m.volumes.values()].find((v) => v.mesh === hits[0].object);
        volume = {hit: hits[0], model: m, item};
      }
    }
    if (volume) {
      const p = volume.hit.point;
      const d = volume.item.data;
      const properties = {label: d.label, us: d.us, area: d.area, tipo: d.tipo, volume_m3: d.volume_m3};
      if (d.record) properties.record = d.record;
      return {x: p.x + centre.x, y: p.y + centre.y, z: p.z + centre.z, chunk: volume.model.chunk,
        feature: {properties, chunk: volume.model.chunk}, volume: volume.item};
    }
    let best = null;
    for (const m of loaded) {
      if (!m.holder.visible || m.volumes) continue;
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
  // the volumes of the group ``name`` ('' or null: those in no group) of the volumes layer ``index`` shown or not
  view.setGroupVisible = (index, name, visible) => {
    const m = loaded[index];
    if (!m || !m.volumes) return;
    const group = name || '';
    if (visible) m.hiddenGroups.delete(group);
    else m.hiddenGroups.add(group);
    for (const v of m.volumes.values()) v.mesh.visible = !m.hiddenGroups.has(v.data.group || '');
  };

  // the volumes marked: more opaque, with their edges drawn
  const edgeMaterial = new THREE.LineBasicMaterial({color: 0x000000});
  function mark(items) {
    for (const m of loaded) {
      if (!m.volumes) continue;
      for (const v of m.volumes.values()) {
        const on = items.includes(v);
        v.mesh.material.opacity = on ? SELECTED_OPACITY : VOLUME_OPACITY;
        if (on && !v.outline) {
          v.outline = new THREE.LineSegments(new THREE.EdgesGeometry(v.mesh.geometry, 30), edgeMaterial);
          v.outline.renderOrder = 2;
          v.mesh.add(v.outline);
        } else if (!on && v.outline) {
          v.mesh.remove(v.outline);
          v.outline.geometry.dispose();
          v.outline = null;
        }
      }
    }
  }
  // the volumes of the record ``key`` of the chunk ``chunkId`` marked (null: none)
  view.highlight = (chunkId, key) => {
    const items = [];
    for (const m of loaded) {
      if (!m.volumes || !key || m.chunk.id !== chunkId) continue;
      for (const v of m.volumes.values()) if (v.data.record === key) items.push(v);
    }
    mark(items);
  };
  view.selected = () => loaded.flatMap((m) => (m.volumes ? [...m.volumes.values()] : []))
    .filter((v) => v.outline).map((v) => v.data.key);

  // the visible models and clouds, every tile they need loaded (the volumes are loaded with the view)
  view.ready = () => {
    const shown = loaded.filter((m) => m.holder.visible);
    const tiled = shown.filter((m) => m.tiles);
    return shown.length > 0 && tiled.every((m) => m.tiles.root && m.tiles.loadProgress === 1
      && m.tiles.stats.downloading + m.tiles.stats.parsing + m.tiles.stats.queued === 0)
      && shown.some((m) => m.volumes || m.tiles.visibleTiles.size > 0);
  };

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
      mark(point.volume ? [point.volume] : []);
      if (view.onPick) view.onPick(point);
    }
  });

  function resize() {
    const w = Math.max(1, container.clientWidth);
    const h = Math.max(1, container.clientHeight);
    renderer.setSize(w, h);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    for (const m of loaded) if (m.tiles) m.tiles.setResolutionFromRenderer(camera, renderer);
  }
  new ResizeObserver(resize).observe(container);
  resize();
  view.home();

  function frame() {
    if (view.active) {
      controls.update();
      scene.updateMatrixWorld();
      camera.updateMatrixWorld();
      for (const m of loaded) {
        if (m.tiles && m.holder.visible) m.tiles.update(); // a hidden model or cloud loads nothing
      }
      renderer.render(scene, camera);
    }
    window.requestAnimationFrame(frame);
  }
  window.requestAnimationFrame(frame);
  return view;
}
