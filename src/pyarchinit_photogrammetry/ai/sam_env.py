"""Segment Anything 2.1 for installations without PyTorch (the packaged application).

PyTorch is too large to ship inside the application, so SAM 2.1 lives in a separate
Python environment that the interface installs on request with ``uv`` (downloaded from
its official GitHub release when it is not on the system):

    <app data>/pyarchinit-photogrammetry/sam-env/   (PPG_SAM_ENV overrides it)

The model runs in a worker process of that environment (``sam_worker.py``) and
:class:`ExternalSam2Segmenter` talks to it; it has the same methods as
:class:`~pyarchinit_photogrammetry.ai.sam.Sam2Segmenter`, so everything else is unchanged.
"""
from __future__ import annotations

import io
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tarfile
import threading
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

PACKAGES = ["torch", "sam2", "huggingface_hub", "numpy", "pillow"]
UV_URL = "https://github.com/astral-sh/uv/releases/latest/download/uv-{target}.{ext}"


def data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "pyarchinit-photogrammetry"


def env_dir() -> Path:
    return Path(os.environ["PPG_SAM_ENV"]) if os.environ.get("PPG_SAM_ENV") else data_dir() / "sam-env"


def env_python(env: Path | None = None) -> Path:
    env = env or env_dir()
    return env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def is_installed(env: Path | None = None) -> bool:
    env = env or env_dir()
    return env_python(env).exists() and (env / "ppg-sam.json").exists()


def in_process_available() -> bool:
    """PyTorch and sam2 importable in this interpreter (pip installs with the [sam] extra)."""
    try:
        import sam2  # noqa: F401
        import torch  # noqa: F401

        return True
    except Exception:
        return False


def available() -> str | None:
    """``"inprocess"``, ``"external"`` or None."""
    if in_process_available():
        return "inprocess"
    if is_installed():
        return "external"
    return None


def _uv_target() -> tuple[str, str]:
    mach = platform.machine().lower()
    arm = mach in ("arm64", "aarch64")
    if sys.platform == "darwin":
        return ("aarch64" if arm else "x86_64") + "-apple-darwin", "tar.gz"
    if os.name == "nt":
        return ("aarch64" if arm else "x86_64") + "-pc-windows-msvc", "zip"
    return ("aarch64" if arm else "x86_64") + "-unknown-linux-gnu", "tar.gz"


def find_uv(progress=None) -> Path:
    """``uv`` on PATH, in the data folder, or downloaded there from the official release."""
    found = shutil.which("uv")
    if found:
        return Path(found)
    exe = data_dir() / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    if exe.exists():
        return exe
    target, ext = _uv_target()
    url = UV_URL.format(target=target, ext=ext)
    if progress:
        progress(0.02, f"Download di uv ({target})")
    with urllib.request.urlopen(url, timeout=120) as r:  # noqa: S310 - fixed https URL
        data = r.read()
    exe.parent.mkdir(parents=True, exist_ok=True)
    name = exe.name
    if ext == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(m for m in z.namelist() if m.endswith("/" + name) or m == name)
            exe.write_bytes(z.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = next(m for m in t.getmembers() if m.name.endswith("/" + name) or m.name == name)
            exe.write_bytes(t.extractfile(member).read())
    exe.chmod(0o755)
    return exe


def install(progress=None, env: Path | None = None, packages: list[str] | None = None) -> Path:
    """Create the SAM environment (Python 3.12 + PyTorch + sam2). Several minutes and
    about 1-2 GB; model weights are downloaded on first use."""
    from ..core.progress import as_progress

    progress = as_progress(progress)
    env = env or env_dir()
    uv = find_uv(progress)
    env.parent.mkdir(parents=True, exist_ok=True)
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV", "CONDA"))}
    progress(0.05, "Creazione dell'ambiente Python per SAM 2.1")
    _run([str(uv), "venv", "--python", "3.12", "--allow-existing", str(env)], clean, progress, 0.05, 0.15)
    progress(0.15, "Installazione di PyTorch e SAM 2.1 (alcuni minuti)")
    _run([str(uv), "pip", "install", "--python", str(env_python(env))] + (packages or PACKAGES), clean,
         progress, 0.15, 0.95)
    out = subprocess.run([str(env_python(env)), "-c",
                          "import torch, sam2, json; print(json.dumps({'torch': torch.__version__}))"],
                         capture_output=True, text=True, env=clean, close_fds=False, timeout=600)
    if out.returncode != 0:
        raise RuntimeError("SAM 2.1 installato ma non si importa:\n" + out.stderr[-1500:])
    info = json.loads(out.stdout.strip().splitlines()[-1])
    (env / "ppg-sam.json").write_text(json.dumps({"packages": packages or PACKAGES, **info}), encoding="utf-8")
    progress(1.0, f"SAM 2.1 pronto (PyTorch {info['torch']})")
    return env


def _run(cmd, env, progress, start: float, end: float) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                            close_fds=False)
    lines = []
    k = 0
    for line in proc.stdout:  # uv prints one line per resolved/installed package
        line = line.rstrip()
        lines.append(line)
        k += 1
        progress(min(end, start + (end - start) * (1 - 1 / (1 + k / 20))), line[-120:] or "...")
    if proc.wait() != 0:
        raise RuntimeError("installazione non riuscita:\n" + "\n".join(lines[-25:]))


def remove(env: Path | None = None) -> None:
    shutil.rmtree(env or env_dir(), ignore_errors=True)


class ExternalSam2Segmenter:
    """SAM 2.1 running in the SAM environment (see module docstring)."""

    def __init__(self, model: str = "small", device: str | None = None, python: str | Path | None = None):
        python = str(python or env_python())
        worker = Path(__file__).with_name("sam_worker.py")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "VIRTUAL_ENV"))}
        env["PYTHONNOUSERSITE"] = "1"
        self._lock = threading.Lock()
        self._proc = subprocess.Popen([python, "-u", str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, env=env, close_fds=False)
        self._stderr: list[str] = []
        threading.Thread(target=self._drain, daemon=True).start()
        self.device = self._call({"op": "init", "model": model, "device": device})["device"]

    def _drain(self) -> None:
        for line in self._proc.stderr:
            self._stderr.append(line.decode("utf-8", "replace").rstrip())
            del self._stderr[:-200]

    def _call(self, req: dict) -> dict:
        import pickle

        with self._lock:
            if self._proc.poll() is not None:
                raise RuntimeError("il processo di SAM 2.1 si e' chiuso:\n" + "\n".join(self._stderr[-20:]))
            data = pickle.dumps(req, protocol=4)
            self._proc.stdin.write(struct.pack(">Q", len(data)) + data)
            self._proc.stdin.flush()
            head = self._proc.stdout.read(8)
            if len(head) < 8:
                raise RuntimeError("SAM 2.1 non ha risposto:\n" + "\n".join(self._stderr[-20:]))
            (n,) = struct.unpack(">Q", head)
            reply = pickle.loads(self._proc.stdout.read(n))
        if not reply.get("ok"):
            raise RuntimeError("SAM 2.1: " + reply.get("error", "errore"))
        return reply

    def segment_points(self, image, points, labels):
        return self._call({"op": "points", "image": np.ascontiguousarray(image), "points": np.asarray(points, float),
                           "labels": np.asarray(labels, int)})["mask"]

    def segment_box(self, image, box):
        return self._call({"op": "box", "image": np.ascontiguousarray(image), "box": np.asarray(box, float)})["mask"]

    def segment_auto(self, image, points_per_side: int = 32, min_area: int = 500):
        r = self._call({"op": "auto", "image": np.ascontiguousarray(image), "points_per_side": points_per_side,
                        "min_area": min_area})
        h, w = r["shape"]
        return [np.unpackbits(m)[: h * w].reshape(h, w).astype(bool) for m in r["masks"]]

    def close(self) -> None:
        try:
            self._call({"op": "quit"})
        except Exception:
            pass
        try:
            self._proc.kill()
        except Exception:
            pass

    def __del__(self):  # pragma: no cover
        try:
            self._proc.kill()
        except Exception:
            pass


def make_segmenter(model: str = "small"):
    """The SAM 2.1 segmenter available here: in process, external environment, or ImportError."""
    mode = available()
    if mode == "inprocess":
        from .sam import Sam2Segmenter

        return Sam2Segmenter(model)
    if mode == "external":
        return ExternalSam2Segmenter(model)
    raise ImportError("SAM 2.1 non e' installato")


__all__ = ["available", "install", "is_installed", "env_dir", "env_python", "make_segmenter",
           "ExternalSam2Segmenter", "remove", "find_uv"]
