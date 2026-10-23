"""Learned local features matched by LightGlue: for the photos where SIFT finds little - thermal
frames, smooth plaster, sand, snow.

ALIKED or DISK find and describe the key points, LightGlue matches them (:mod:`..ai.learned_net`,
kornia on PyTorch). The key points go into COLMAP's database as its own extractor writes them (a
camera per sensor, a rig and a frame per photo), the matches as its matcher writes them, and
COLMAP's geometric verification then decides the inliers: the alignment that follows does not
know the difference. The descriptors stay beside the database in ``learned/<kind>/`` (COLMAP's
table holds SIFT's). The photos are reduced to :data:`MAX_SIDE` pixels at most for the networks.

Where it runs: always in a process of its own (``learned_worker.py``), never in the program's.
PyTorch brings its own OpenMP runtime and pycolmap another one: in the same process the second
to start aborts it (``OMP: Error #15``, or a segmentation fault after ``pthread_mutex_init
failed``). The worker runs with this Python when PyTorch and kornia are installed beside the
program (``pip install "pyarchinit-photogrammetry[learned]"``), otherwise with the separate
environment of SAM 2.1 that the packaged application installs on request (:mod:`..ai.sam_env`).
The weights are downloaded the first time (torch keeps them in its cache: DISK 4 MB, ALIKED
1 MB, LightGlue 45 MB). Apple's GPU (Metal), NVIDIA's (CUDA) or the CPU.

The networks are not invariant to rotation (SIFT is): on a drone flight the strips flown one way
and the other are half a turn apart, and the frames of a Mavic 2 Enterprise Advanced matched across
them failed nearly always (37 % of the pairs verified, SIFT 98 %), while those of the same strip
did better than SIFT (98 %, more inliers). So every photo is first turned by quarter turns to a
common heading (:func:`quarter_turns`, from the yaw the drone writes into it) and its points are
brought back to the photo as it is.

COLMAP 4 has ALIKED and LightGlue of its own through ONNX Runtime, but the pycolmap wheels are
built without it (``ALIKED feature extraction requires ONNX support``, and the process aborts):
they are never asked for.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import struct
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from ..ai.learned_net import KINDS, MAX_KEYPOINTS
from ..core.progress import Progress, as_progress

log = logging.getLogger(__name__)

MATCHERS = ("sift",) + tuple(KINDS)
MAX_SIDE = 1600  # the longer side the photos are reduced to for the networks
# What a pair needs in COLMAP's verification (COLMAP: 15 inliers, a quarter of the matches). A pair
# the networks really match has hundreds of inliers and almost all its matches agree (synthetic
# trench: median 700, 10th percentile 78 and 83 %); on self-similar texture and between views turned
# too far for them LightGlue also returns wrong matches that fit a geometry by chance (median 40
# inliers, 60-75 %): with COLMAP's thresholds they bent the block by decimetres. These keep 92 % of
# the right pairs and drop 80-90 % of the wrong ones.
MIN_INLIERS = 50
MIN_INLIER_RATIO = 0.8
NAMES = {"sift": "SIFT (COLMAP)", **KINDS}


def local() -> bool:
    """PyTorch and kornia installed for this Python (looked up, not imported: see the module)."""
    if getattr(sys, "frozen", False):  # the packaged application: its executable is not a Python
        return False
    try:
        return importlib.util.find_spec("torch") is not None and importlib.util.find_spec("kornia") is not None
    except (ImportError, ValueError):
        return False


def _env_has_kornia(env: Path) -> bool:
    return any(env.glob("lib/python3*/site-packages/kornia")) or (env / "Lib" / "site-packages" / "kornia").exists()


def available() -> str | None:
    """``"local"`` (this Python), ``"external"`` (the environment of SAM 2.1 with kornia) or None."""
    if local():
        return "local"
    from ..ai import sam_env

    if sam_env.is_installed() and _env_has_kornia(sam_env.env_dir()):
        return "external"
    return None


def install(progress: Progress | None = None) -> Path:
    """kornia into the environment of SAM 2.1, which is created first when it is not there
    (PyTorch: a few minutes and 1-2 GB)."""
    from ..ai import sam_env

    progress = as_progress(progress)
    env = sam_env.env_dir()
    if not sam_env.is_installed(env):
        return sam_env.install(progress, env)  # its packages include kornia
    uv = sam_env.find_uv(progress)
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV", "CONDA"))}
    progress(0.1, "Installing kornia into the environment of SAM 2.1")
    sam_env._run([str(uv), "pip", "install", "--python", str(sam_env.env_python(env)), "kornia"], clean,
                 progress, 0.1, 0.95)
    info_path = env / "ppg-sam.json"
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    info["packages"] = sorted(set(info.get("packages", [])) | {"kornia"})
    info_path.write_text(json.dumps(info), encoding="utf-8")
    progress(1.0, "Learned features ready")
    return env


class ExternalFeatures:
    """:class:`..ai.learned_net.Features` in a worker process: with ``python`` (this Python for
    ``local``), else with the environment of SAM 2.1."""

    def __init__(self, kind: str, max_keypoints: int, python: str | Path | None = None):
        from ..ai import sam_env

        python = str(python or sam_env.env_python())
        worker = Path(__file__).parent.parent / "ai" / "learned_worker.py"  # beside sam_worker.py in the app
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))}
        env["PYTHONNOUSERSITE"] = "1"
        env.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        self._lock = threading.Lock()
        self._proc = subprocess.Popen([python, "-u", str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env, close_fds=False)
        self._stderr: list[str] = []
        threading.Thread(target=self._drain, daemon=True).start()
        self.kind = kind
        self.device = self._call({"op": "init", "kind": kind, "max_keypoints": int(max_keypoints)})["device"]

    def _drain(self) -> None:
        for line in self._proc.stderr:
            self._stderr.append(line.decode("utf-8", "replace").rstrip())
            del self._stderr[:-200]

    def _call(self, req: dict) -> dict:
        import pickle

        with self._lock:
            if self._proc.poll() is not None:
                raise RuntimeError("the learned features process closed:\n" + "\n".join(self._stderr[-20:]))
            data = pickle.dumps(req, protocol=4)
            self._proc.stdin.write(struct.pack(">Q", len(data)) + data)
            self._proc.stdin.flush()
            head = self._proc.stdout.read(8)
            if len(head) < 8:
                raise RuntimeError("the learned features process did not answer:\n" + "\n".join(self._stderr[-20:]))
            (n,) = struct.unpack(">Q", head)
            reply = pickle.loads(self._proc.stdout.read(n))
        if not reply.get("ok"):
            raise RuntimeError("learned features: " + reply.get("error", "error"))
        return reply

    def extract(self, grey: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        r = self._call({"op": "extract", "image": np.ascontiguousarray(grey)})
        return r["kp"], r["desc"]

    def match_files(self, a, b) -> np.ndarray:
        return self._call({"op": "match", "a": str(a), "b": str(b)})["matches"]

    def close(self) -> None:
        try:
            self._call({"op": "quit"})
        except Exception:  # noqa: BLE001
            pass
        try:
            self._proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):  # pragma: no cover - a step that failed before closing it
        try:
            self._proc.kill()
        except Exception:  # noqa: BLE001
            pass


def open_features(kind: str = "disk", max_keypoints: int = MAX_KEYPOINTS):
    """The extractor and LightGlue where they can run (see the module), or ImportError."""
    if kind not in KINDS:
        raise ValueError(f"unknown learned features {kind!r}: {', '.join(KINDS)}")
    mode = available()
    if mode == "local":
        return ExternalFeatures(kind, max_keypoints, python=sys.executable)
    if mode == "external":
        return ExternalFeatures(kind, max_keypoints)
    raise ImportError("learned features need PyTorch and kornia: pip install \"pyarchinit-photogrammetry[learned]\" "
                      "or install them from the program (Tools > SAM 2.1 / learned features)")


YAW_TAGS = ("GimbalYawDegree", "FlightYawDegree", "Yaw", "CameraYaw")  # DJI's gimbal, DJI's aircraft, others


def yaw_of(path) -> float | None:
    """The heading of a photo in degrees clockwise from north: the yaw of the gimbal or of the
    aircraft the drone writes into the XMP, else the EXIF ``GPSImgDirection``; None without."""
    from .multispectral import xmp_of

    try:
        tags = xmp_of(path)
    except Exception:  # noqa: BLE001
        tags = {}
    for k in YAW_TAGS:
        v = tags.get(k)
        try:
            if v is not None:
                return float(v)
        except (TypeError, ValueError):
            pass
    try:
        from PIL import Image

        with Image.open(path) as im:
            v = im.getexif().get_ifd(0x8825).get(17)  # GPSImgDirection
        return float(v) if v is not None else None
    except Exception:  # noqa: BLE001
        return None


def _camera_yaw(cam) -> float | None:
    """The heading of a camera's photo, read from the file the drone wrote (the thermal R-JPEG or
    the first band of a multispectral capture when the photo is a copy made from them)."""
    files = []
    for band in (cam.meta.get("bands") or {}).values():
        f = band.get("path") if isinstance(band, dict) else band
        if f:
            files.append(f)
            break
    files.append(cam.path)
    for f in files:
        try:
            if f and Path(f).exists():
                y = yaw_of(f)
                if y is not None:
                    return y
        except OSError:
            pass
    return None


def quarter_turns(chunk, names: dict[int, str]) -> dict[str, int]:
    """For every photo with a heading, the quarter turns (clockwise) that bring it to the heading
    the photos share: the course of the strips modulo 90 degrees (the circular mean of four times
    the yaws), so that strips flown one way and back, and cross strips, end up the same way up and
    far from the 45-degree edges where noise would flip a photo by a quarter turn. Photos without a
    heading are not turned."""
    yaws = {}
    for cam in chunk.cameras:
        if cam.enabled and cam.key in names:
            y = _camera_yaw(cam)
            if y is not None and np.isfinite(y):
                yaws[names[cam.key]] = float(y)
    if len(yaws) < 2:
        return {}
    a = np.radians(4.0 * np.array(list(yaws.values())))
    course = np.degrees(np.arctan2(np.sin(a).mean(), np.cos(a).mean())) / 4.0
    return {name: int(np.round((y - course) / 90.0)) % 4 for name, y in yaws.items()}


def turn(img: np.ndarray, k: int) -> np.ndarray:
    """``img`` turned clockwise by ``k`` quarter turns."""
    return np.ascontiguousarray(np.rot90(img, -k)) if k % 4 else img


def unturn_points(xy: np.ndarray, k: int, width: int, height: int) -> np.ndarray:
    """Points (x, y; the centre of the first pixel at 0) found on an image of ``width`` x
    ``height`` turned clockwise by ``k`` quarter turns (:func:`turn`), on the image as it was."""
    xy = np.asarray(xy, np.float64).reshape(-1, 2)
    x, y = xy[:, 0], xy[:, 1]
    m = (-k) % 4  # counter-clockwise turns, as numpy's rot90
    if m == 1:
        x, y = width - 1 - y, x
    elif m == 2:
        x, y = width - 1 - x, height - 1 - y
    elif m == 3:
        x, y = y, height - 1 - x
    return np.column_stack([x, y])


def feature_dir(work: Path, kind: str) -> Path:
    return Path(work) / "learned" / kind


def _feature_file(work: Path, kind: str, image_id: int) -> Path:
    return feature_dir(work, kind) / f"{image_id}.npz"


def extract_to_database(db_path: Path, root: Path, chunk, groups: list[tuple[int, list[str]]], max_image_size,
                        feats, masks: Path | None = None, progress: Progress | None = None, readers: int = 4,
                        turns: dict[str, int] | None = None) -> int:
    """The learned key points of the photos into COLMAP's database (``groups``: sensor key and
    image names; ``max_image_size``: a number or a function of the sensor, the longer side the
    photo is reduced to, at most :data:`MAX_SIDE`), the descriptors beside it. The photos are read
    and reduced in threads while the network works; each is turned by its ``turns``
    (:func:`quarter_turns`) for the network, its points brought back. Returns how many photos were
    done."""
    import cv2
    import pycolmap

    from .sfm import mapping_camera

    progress = as_progress(progress)
    work = Path(db_path).parent
    kind = feats.kind
    total = sum(len(n) for _k, n in groups)
    done = 0

    def load(name: str, limit: int):
        img = cv2.imread(str(root / name), cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION)
        if img is None:
            return name, None, None, None
        h, w = img.shape
        small = img
        if max(w, h) > limit:
            f = limit / max(w, h)
            small = cv2.resize(img, (max(int(round(w * f)), 16), max(int(round(h * f)), 16)), interpolation=cv2.INTER_AREA)
        mask = None
        if masks is not None:
            m = cv2.imread(str(masks / f"{name}.png"), cv2.IMREAD_GRAYSCALE)
            if m is not None and m.shape == img.shape:
                mask = m
        k = (turns or {}).get(name, 0)
        return name, small, (w, h), mask, k

    db = pycolmap.Database.open(str(db_path))
    try:
        for skey, image_names in groups:
            sensor = chunk.sensors[skey]
            model, params = mapping_camera(sensor)
            camera = pycolmap.Camera(model=model, width=int(sensor.width), height=int(sensor.height), params=list(params))
            camera.has_prior_focal_length = True
            camera_id = db.write_camera(camera)
            sensor_id = pycolmap.sensor_t(pycolmap.SensorType.CAMERA, camera_id)
            rig = pycolmap.Rig()
            rig.add_ref_sensor(sensor_id)
            rig_id = db.write_rig(rig)
            limit = int(max_image_size(sensor) if callable(max_image_size) else max_image_size)
            limit = max(min(limit, MAX_SIDE), 64)
            with ThreadPoolExecutor(max_workers=readers) as pool:
                for name, small, size, mask, k in pool.map(lambda n: load(n, limit), image_names):
                    progress.check()
                    if small is None:
                        log.warning("%s: unreadable, no key points", name)
                        continue
                    seen = turn(small, k)
                    kp, desc = feats.extract(seen)
                    kp = np.asarray(kp, np.float32).reshape(-1, 2)
                    desc = np.asarray(desc)
                    back = unturn_points(kp, k, small.shape[1], small.shape[0])
                    sx, sy = size[0] / small.shape[1], size[1] / small.shape[0]
                    kp_colmap = np.column_stack([(back[:, 0] + 0.5) * sx, (back[:, 1] + 0.5) * sy]).astype(np.float32)
                    if mask is not None and len(kp):
                        cx = np.clip(kp_colmap[:, 0].astype(int), 0, mask.shape[1] - 1)
                        cy = np.clip(kp_colmap[:, 1].astype(int), 0, mask.shape[0] - 1)
                        keep = mask[cy, cx] > 0
                        kp_colmap, kp, desc = kp_colmap[keep], kp[keep], desc[keep]
                    image = pycolmap.Image(name=name, camera_id=camera_id)
                    iid = db.write_image(image)
                    frame = pycolmap.Frame()
                    frame.rig_id = rig_id
                    frame.add_data_id(pycolmap.data_t(sensor_id, iid))
                    db.write_frame(frame)
                    db.write_keypoints(iid, np.ascontiguousarray(kp_colmap))
                    target = _feature_file(work, kind, iid)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # LightGlue matches the points in the frame the network saw: the reduced photo, turned
                    np.savez(target, kp=kp, desc=desc, size=np.array(seen.shape[:2]), turns=np.array(k))
                    done += 1
                    if done % 4 == 0 or done == total:
                        progress(done / max(total, 1), f"Learned key points ({NAMES[kind]}): {done} of {total} photos")
    finally:
        db.close()
    return done


def match_pairs(db_path: Path, pairs, feats, progress: Progress | None = None) -> int:
    """LightGlue on every pair of image names (``pairs``), the matches written into COLMAP's
    database, then COLMAP's geometric verification of them all. Returns the pairs matched."""
    import pycolmap

    progress = as_progress(progress)
    work = Path(db_path).parent
    db = pycolmap.Database.open(str(db_path))
    try:
        ids = {im.name: int(im.image_id) for im in db.read_all_images()}
    finally:
        db.close()
    pairs = sorted({tuple(sorted(p)) for p in pairs if p[0] in ids and p[1] in ids and p[0] != p[1]})
    # the pairs in the order of their photos: the descriptor files stay in the cache of the matcher
    pairs.sort(key=lambda p: (ids[p[0]], ids[p[1]]))
    batch: dict[tuple[int, int], np.ndarray] = {}

    def flush():
        if not batch:
            return
        d = pycolmap.Database.open(str(db_path))
        try:
            for (a, b), m in batch.items():
                d.write_matches(a, b, np.ascontiguousarray(np.asarray(m, np.uint32).reshape(-1, 2)))
        finally:
            d.close()
        batch.clear()

    written = []
    for i, (na, nb) in enumerate(pairs):
        progress.check()
        a, b = ids[na], ids[nb]
        fa, fb = _feature_file(work, feats.kind, a), _feature_file(work, feats.kind, b)
        if not (fa.exists() and fb.exists()):
            continue
        batch[(a, b)] = feats.match_files(fa, fb)
        written.append((na, nb))
        if len(batch) >= 256:
            flush()
        if i % 16 == 0 or i + 1 == len(pairs):
            progress(0.9 * (i + 1) / max(len(pairs), 1), f"LightGlue: {i + 1:,} of {len(pairs):,} pairs")
    flush()
    if written:
        progress(0.92, f"Verifying the {len(written):,} pairs matched by LightGlue")
        lst = work / "pairs_learned.txt"
        lst.write_text("\n".join(f"{x} {y}" for x, y in written))
        opts = pycolmap.TwoViewGeometryOptions()
        opts.min_num_inliers = MIN_INLIERS
        opts.min_inlier_ratio = MIN_INLIER_RATIO
        pycolmap.verify_matches(str(db_path), str(lst), opts)
    progress(1.0, f"LightGlue: {len(written):,} pairs")
    return len(written)
