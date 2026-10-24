"""Work shared with other computers through the project folder (network and remote processing).

The program started as a **node** on another computer (``ppg node FOLDER``, where FOLDER is the
``<project>.ppg.files`` folder as that computer sees it on the shared disk) takes from
``nodes/todo`` the units of work the main program puts there and writes their results into
``nodes/done``. A unit is a JSON file; a node takes it by renaming it into ``nodes/taken`` (an
atomic rename: the first wins), touches it while it works (a unit whose node died comes back to
``todo`` after :data:`STALE`), and leaves a ``done`` or ``failed`` file. The main program is a
node too: it works on the units nobody has taken, so with no node present nothing changes, and
with nodes present every computer takes what it can at its own speed. Nodes say they are there
with a ``nodes/hello/<host>.json`` file renewed every :data:`HELLO_EVERY` seconds.

What splits by photo goes to the nodes: the detection of the key points (units of a few photos
of one sensor: the node runs COLMAP's extractor into a database of its own and sends keypoints
and descriptors back, the main program writes them into its database) and the PatchMatch depth
maps (units of a few photos: the node opens the project read-only on the shared folder, computes
the maps in the workspace there with its GPUs and CPU). The alignment stays on one machine.

A unit that fails on a node (a photo it cannot read) is done again by the main program; the units
of a step cancelled or failed are taken back.

Paths inside a unit are the main program's: a node maps the main program's project folder to
its own (``nodes/master.json`` says where the main program sees it) and any other prefix the
user gives (``--map /Users/x/foto=/mnt/foto``). Over SSH (``ppg remote``) the project folder and
the photos go to the other computer with rsync, the node runs there and the results come back.
"""
from __future__ import annotations

import json
import os
import platform
import queue
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from ..core.progress import Progress, as_progress

NODES_DIR = "nodes"
STATES = ("hello", "todo", "taken", "done", "failed")
HELLO_EVERY = 30.0  # seconds between the hello files of a node
HELLO_TIMEOUT = 90.0  # a node whose hello is older is gone
STALE = 180.0  # a taken unit untouched for this long goes back to todo (its node died)
FEATURE_UNIT_PHOTOS = 8
DEPTH_UNIT_PHOTOS = 4


def enabled() -> bool:
    """``PPG_NODES`` (default on): whether the main program hands work to the nodes present."""
    return os.environ.get("PPG_NODES", "1").strip().lower() not in ("0", "off", "false", "no")


def folder(files_dir: str | os.PathLike, create: bool = True) -> Path:
    """``<files_dir>/nodes`` with its state folders."""
    root = Path(files_dir) / NODES_DIR
    if create:
        for state in STATES:
            (root / state).mkdir(parents=True, exist_ok=True)
    return root


def host_name() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def present(files_dir: str | os.PathLike) -> list[dict]:
    """The nodes whose hello is fresh (``host``, ``cores``, ``gpus``, ``age`` seconds)."""
    hello = folder(files_dir, create=False) / "hello"
    out = []
    if not hello.is_dir():
        return out
    now = time.time()
    for p in sorted(hello.glob("*.json")):
        try:
            age = now - p.stat().st_mtime
            if age > HELLO_TIMEOUT:
                continue
            info = json.loads(p.read_text(encoding="utf-8"))
            info["age"] = round(age, 1)
            out.append(info)
        except (OSError, ValueError):
            continue
    return out


def wanted(files_dir: str | os.PathLike) -> list[dict]:
    """The nodes to share the work with: those present, when sharing is enabled."""
    return present(files_dir) if enabled() else []



def write_master(files_dir: str | os.PathLike, project_path: str | os.PathLike | None) -> None:
    """Where the main program sees the project: nodes map it to their own path."""
    info = {"files_dir": str(Path(files_dir).resolve()), "project_dir": str(Path(files_dir).resolve().parent),
            "project": str(Path(project_path).resolve()) if project_path else None, "host": host_name(),
            "time": time.time()}
    (folder(files_dir) / "master.json").write_text(json.dumps(info, indent=1), encoding="utf-8")


def mapping_for(node_folder: Path, extra=()) -> list[tuple[str, str]]:
    """Path prefixes of the main program and what they are on this computer: the project folder
    (from ``master.json``) and the pairs ``SRC=DST`` given by the user (longest prefixes first)."""
    pairs: list[tuple[str, str]] = []
    master = node_folder / "master.json"
    if master.exists():
        try:
            info = json.loads(master.read_text(encoding="utf-8"))
            if info.get("files_dir"):
                pairs.append((info["files_dir"], str(node_folder.parent.resolve())))
            if info.get("project_dir"):
                pairs.append((info["project_dir"], str(node_folder.parent.parent.resolve())))
        except (OSError, ValueError):
            pass
    for item in extra:
        src, _, dst = str(item).partition("=")
        if src and dst:
            pairs.append((src, dst))
    return sorted(pairs, key=lambda p: -len(p[0]))


def map_path(path: str | os.PathLike | None, mapping) -> str | None:
    """``path`` as this computer sees it."""
    if path is None:
        return None
    s = str(path)
    for src, dst in mapping:
        if s == src or s.startswith(src.rstrip("/\\") + "/") or s.startswith(src.rstrip("/\\") + "\\"):
            return dst + s[len(src.rstrip("/\\")):]
    return s


# ---------------------------------------------------------------------------- the units
def put(files_dir: str | os.PathLike, units: list[dict]) -> list[str]:
    """Units into ``todo``; returns their names."""
    root = folder(files_dir)
    stamp = f"{int(time.time() * 1000):x}"
    names = []
    for i, unit in enumerate(units):
        name = f"{stamp}-{i:04d}-{unit.get('kind', 'unit')}"  # sorted = the order they were put in
        tmp = root / "todo" / f".{name}.json.part"
        tmp.write_text(json.dumps({"name": name, **unit}), encoding="utf-8")
        tmp.replace(root / "todo" / f"{name}.json")
        names.append(name)
    return names


def take(root: Path, host: str) -> tuple[str, dict] | None:
    """One unit of ``todo`` for ``host``: renamed into ``taken`` (atomic: a rename that fails
    means another node has it). None when nothing is left."""
    todo = root / "todo"
    for p in sorted(todo.glob("*.json")):
        name = p.stem
        target = root / "taken" / f"{name}@{host}.json"
        try:
            os.rename(p, target)
        except OSError:
            continue
        heartbeat(root, name, host)  # a rename keeps the time it was put: a unit that waited is not stale
        try:
            return name, json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def heartbeat(root: Path, name: str, host: str) -> None:
    p = root / "taken" / f"{name}@{host}.json"
    try:
        os.utime(p, None)
    except OSError:
        pass


@contextmanager
def _beating(root: Path, name: str, host: str):
    """The taken file of a unit touched every quarter of :data:`STALE` while the unit is worked on."""
    beat = threading.Event()

    def beating():
        while not beat.wait(STALE / 4):
            heartbeat(root, name, host)

    ticker = threading.Thread(target=beating, daemon=True)
    ticker.start()
    try:
        yield
    finally:
        beat.set()
        ticker.join()


def finish(root: Path, name: str, host: str, result: dict | None = None, error: str | None = None) -> None:
    """The unit's outcome into ``done`` (or ``failed``), its taken file gone."""
    state = "failed" if error else "done"
    payload = {"name": name, "by": host, "time": time.time(), "result": result or {}, "error": error}
    tmp = root / state / f".{name}.json.part"
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(root / state / f"{name}.json")
    try:
        (root / "taken" / f"{name}@{host}.json").unlink()
    except OSError:
        pass


def requeue_stale(root: Path, stale: float = STALE) -> int:
    """Taken units nobody touched for ``stale`` seconds back into ``todo``."""
    n = 0
    now = time.time()
    for p in (root / "taken").glob("*.json"):
        try:
            if now - p.stat().st_mtime < stale:
                continue
            name = p.stem.split("@")[0]
            os.rename(p, root / "todo" / f"{name}.json")
            n += 1
        except OSError:
            continue
    return n


def outcome(root: Path, name: str) -> dict | None:
    for state in ("done", "failed"):
        p = root / state / f"{name}.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
    return None


def forget(files_dir: str | os.PathLike, names) -> None:
    """The files of finished units removed (results included)."""
    root = folder(files_dir, create=False)
    for name in names:
        for state in ("done", "failed", "todo"):
            for p in (root / state).glob(f"{name}*"):
                try:
                    p.unlink()
                except OSError:
                    pass


def _sweep(root: Path) -> None:
    """What earlier steps left (cancelled, or the main program closed while they ran): units nobody
    takes any more and results nobody reads. A step starts with no other step running."""
    for state in ("todo", "taken", "done", "failed"):
        for p in (root / state).iterdir():
            try:
                p.unlink()
            except OSError:
                pass


def _finished(root: Path, state: str) -> set[str]:
    """The names of the units with a ``done`` (or ``failed``) file: one listing for every unit."""
    try:
        return {e.name[:-5] for e in os.scandir(root / state) if e.name.endswith(".json") and not e.name.startswith(".")}
    except OSError:
        return set()


# ---------------------------------------------------------------------------- the work
def process(payload: dict, mapping, gpu: str | None = None, out_dir: Path | None = None) -> dict:
    """One unit on this computer; the result the main program reads (files in ``out_dir``)."""
    kind = payload.get("kind")
    if kind == "features":
        return features_unit(payload, mapping, out_dir)
    if kind == "depth_maps":
        return depth_unit(payload, mapping, gpu)
    raise ValueError(f"unknown unit {kind!r}")


def features_unit(payload: dict, mapping, out_dir: Path | None) -> dict:
    """COLMAP's key points and descriptors of the photos of the unit, extracted into a database
    of this computer and saved as ``<unit>.npz`` beside the done file."""
    import tempfile

    import pycolmap

    root = map_path(payload["root"], mapping)
    names = list(payload["names"])
    cam = payload["camera"]
    ext = pycolmap.FeatureExtractionOptions()
    ext.max_image_size = int(payload["max_image_size"])
    ext.sift.max_num_features = int(payload["max_num_features"])
    reader = pycolmap.ImageReaderOptions()
    reader.camera_model = cam["model"]
    reader.camera_params = cam["params"]
    if payload.get("mask_path"):
        reader.mask_path = map_path(payload["mask_path"], mapping)
    arrays = {}
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "features.db"
        pycolmap.extract_features(str(db), root, image_names=names, camera_mode=pycolmap.CameraMode.SINGLE,
                                  reader_options=reader, extraction_options=ext)
        d = pycolmap.Database.open(str(db))
        try:
            for im in d.read_all_images():
                arrays[f"kp:{im.name}"] = np.asarray(d.read_keypoints(im.image_id), np.float32)
                arrays[f"desc:{im.name}"] = np.asarray(d.read_descriptors(im.image_id).data, np.uint8)
        finally:
            d.close()
    out = (out_dir or Path(tempfile.gettempdir())) / f"{payload['name']}.npz"
    np.savez(out, **arrays)
    return {"file": out.name, "names": [n for n in names if f"kp:{n}" in arrays]}


def depth_unit(payload: dict, mapping, gpu: str | None) -> dict:
    """The PatchMatch depth maps of the photos of the unit, written into the workspace on the
    shared folder by this computer's GPUs and CPU."""
    from ..core.document import Document
    from .dense import patchmatch_job

    doc = Document.open(map_path(payload["project"], mapping))
    chunk = next((c for c in doc.chunks if c.data_dir.name == payload["chunk_dir"]), None)
    if chunk is None:
        raise RuntimeError(f"no chunk {payload['chunk_dir']} in the project")
    job = patchmatch_job(chunk, Path(map_path(payload["workspace"], mapping)), payload["filter_mode"],
                         int(payload["iterations"]), bool(payload["init"]))
    names = job.run([int(i) for i in payload["image_ids"]], gpu=gpu)
    return {"names": names}


def import_features(db_path: Path, done_dir: Path, results: list[dict], camera_ids: dict[str, int]) -> int:
    """The key points and descriptors the units produced into the database; ``camera_ids``: the
    camera of every image name. Returns the images written."""
    import pycolmap

    n = 0
    db = pycolmap.Database.open(str(db_path))
    try:
        for res in results:
            data = np.load(done_dir / res["file"])
            for name in res["names"]:
                kp = np.ascontiguousarray(data[f"kp:{name}"], np.float32)
                desc = np.ascontiguousarray(data[f"desc:{name}"], np.uint8)
                image = pycolmap.Image(name=name, camera_id=int(camera_ids[name]))
                iid = db.write_image(image)
                db.write_keypoints(iid, kp)
                db.write_descriptors(iid, pycolmap.FeatureDescriptors(pycolmap.FeatureExtractorType.SIFT, desc))
                n += 1
    finally:
        db.close()
    return n


# ---------------------------------------------------------------------------- the main program's side
class _Quiet(Progress):
    """The progress of a unit done by the main program: it says nothing (the step reports its
    units) and stops the unit when the step ends."""

    def __call__(self, fraction: float, message: str = "") -> None:
        self.check()

    def message(self, text: str) -> None:
        self.check()

    def sub(self, start: float, span: float) -> "Progress":
        child = _Quiet()
        child._root = self._root
        return child


def run(files_dir: str | os.PathLike, units: list[dict], progress: Progress | None = None,
        label: str = "units", gpu: str | None = None, project_path=None, local=None) -> list[dict]:
    """The ``units`` done by the nodes present and by this program (a worker thread taking the
    units nobody took; ``local(payload, progress)`` does one here - ``progress`` stops it when the
    step ends - :func:`process` by default); returns their outcomes in order (``by``,
    ``result``). A unit that fails on another computer is done again here; one that fails here
    stops everything, and the units nobody took are taken back."""
    progress = as_progress(progress)
    root = folder(files_dir)
    _sweep(root)
    write_master(files_dir, project_path)
    names = put(files_dir, units)
    payloads = {n: {"name": n, **u} for n, u in zip(names, units)}
    host = host_name()
    mapping: list[tuple[str, str]] = []  # the main program sees its own paths
    stop = threading.Event()
    quiet = _Quiet()  # the unit done here: cancelled when the step ends (a copy of a unit a node did)
    again: queue.Queue = queue.Queue()  # units that failed on another computer
    errors: list[str] = []
    retried: set[str] = set()

    def local_worker():
        while not stop.is_set():
            try:
                name = again.get_nowait()
                payload = payloads[name]
            except queue.Empty:
                got = take(root, host)
                if got is None:
                    stop.wait(0.5)
                    continue
                name, payload = got
            with _beating(root, name, host):
                try:
                    result = local(payload, quiet) if local is not None else process(payload, mapping, gpu,
                                                                                       root / "done")
                    finish(root, name, host, result)
                except Exception as exc:  # noqa: BLE001 - the unit fails, the main loop reports it
                    finish(root, name, host, error=f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=local_worker, name="ppg-node-local", daemon=True)
    worker.start()
    results: dict[str, dict] = {}
    try:
        while len(results) < len(names) and not errors:
            ready = _finished(root, "done") | _finished(root, "failed")
            for name in names:
                if name in results or name not in ready:
                    continue
                got = outcome(root, name)
                if got is None:
                    continue
                if got.get("error"):
                    if got.get("by") != host and name not in retried:  # that computer could not: this one does it
                        retried.add(name)
                        try:
                            (root / "failed" / f"{name}.json").unlink()
                        except OSError:
                            pass
                        again.put(name)
                        progress.message(f"A unit failed on {got.get('by')} ({got['error']}): done here")
                        continue
                    errors.append(f"{name} on {got.get('by')}: {got['error']}")
                    break
                results[name] = got
            requeue_stale(root)
            by_others = sum(1 for r in results.values() if r.get("by") != host)
            progress(len(results) / max(len(names), 1),
                     f"{len(results)}/{len(names)} {label}" + (f" ({by_others} by other computers)" if by_others else ""))
            progress.check()
            if len(results) < len(names) and not errors:
                time.sleep(0.5)
    finally:
        quiet.cancel()
        stop.set()
        worker.join()
        if len(results) < len(names):  # cancelled or failed: nobody takes the rest any more
            forget(files_dir, names)
    if errors:
        raise RuntimeError("a unit of work failed: " + "; ".join(errors[:3]))
    return [results[n] for n in names]


# ---------------------------------------------------------------------------- the node
def serve(node_folder: str | os.PathLike, once: bool = False, gpu: str | None = None, maps=(), log=print,
          idle: float = 2.0, stop_event: threading.Event | None = None, host: str | None = None) -> int:
    """The node loop on ``node_folder`` (the ``<project>.ppg.files`` folder as this computer sees
    it): hello files, units taken and done one after the other. ``once``: leave when ``todo`` is
    empty; ``host``: the name of the node (default: the computer and the process). Returns the
    units done."""
    root = folder(node_folder)
    host = host or host_name()
    from .. import _native

    hello = root / "hello" / f"{host}.json"
    last_hello = 0.0
    done = 0
    stop_event = stop_event or threading.Event()
    try:
        while not stop_event.is_set():
            if time.time() - last_hello >= HELLO_EVERY:
                hello.write_text(json.dumps({"host": host, "machine": platform.node(), "cores": os.cpu_count(),
                                             "gpus": [a["name"] for a in _native.gpu_adapters()],
                                             "time": time.time()}), encoding="utf-8")
                last_hello = time.time()
            got = take(root, host)
            if got is None:
                if once:
                    break
                stop_event.wait(idle)
                continue
            name, payload = got
            log(f"unit {name}: {payload.get('kind')}")
            mapping = mapping_for(root, maps)  # master.json may come after the node started
            with _beating(root, name, host):
                try:
                    result = process(payload, mapping, gpu, root / "done")
                    finish(root, name, host, result)
                    done += 1
                    log(f"unit {name}: done")
                except Exception as exc:  # noqa: BLE001 - reported to the main program
                    finish(root, name, host, error=f"{type(exc).__name__}: {exc}")
                    log(f"unit {name}: failed ({exc})")
    finally:
        try:
            hello.unlink()
        except OSError:
            pass
    return done
