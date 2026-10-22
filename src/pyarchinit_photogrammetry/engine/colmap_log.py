"""COLMAP's own log relayed as progress: the photos it extracts points from, the blocks of pairs
it matches, the photos it places in the block, the adjustments it runs, the depth maps it fuses.

pycolmap writes its log through glog. :func:`relay` sends it to a file for the duration of a step
and tails the file in a thread, turning the lines that measure progress into calls of the step's
:class:`~pyarchinit_photogrammetry.core.progress.Progress` - the fraction when a line counts
(``Processed file [12/57]``, ``Registering image #3 (num_reg_frames=120)``), a message otherwise -
so the window shows how the step goes on inside COLMAP, as Metashape's progress window does,
instead of one message for an hour. The lines are COLMAP 3.12's (pycolmap 4.2); a line nobody
recognises is simply not shown.
"""
from __future__ import annotations

import contextlib
import re
import threading
import time
from pathlib import Path
from typing import NamedTuple

from ..core.progress import Progress, as_progress

_LINE = re.compile(r"^[IWEF]\d{8} \d\d:\d\d:\d\d\.\d+\s+\d+\s+\S+\]\s?(.*)$")  # glog's prefix
STAGES = {  # the stages of the global mapper, with the fraction of the alignment they start at
    "rotation averaging": 0.1, "track establishment": 0.25, "global positioning": 0.4,
    "iterative bundle adjustment": 0.6, "iterative retriangulation and refinement": 0.8,
}
_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    # (regex, kind, message template with {a}, {b}, {c}, {name}); the fraction is a / b when the kind counts
    (re.compile(r"Processed file \[(\d+)/(\d+)\]"), "features", "Key points of photo {a} of {b}"),
    (re.compile(r"Processing block \[(\d+)/(\d+), (\d+)/(\d+)\]"), "blocks2", "Matching block {a} of {b}"),
    (re.compile(r"Processing block \[(\d+)/(\d+)\]"), "blocks", "Matching block {a} of {b}"),
    (re.compile(r"Registering initial image pair #(\d+) and #(\d+)"), "note", "First pair of photos: #{a} and #{b}"),
    (re.compile(r"Registering image #(\d+) \(num_reg_frames=(\d+)\)"), "register", "Placing photo #{a}: {b} in the block"),
    (re.compile(r"=> Image sees (\d+) / (\d+) points"), "sees", "   it sees {a} of {b} points"),
    (re.compile(r"Undistorting image \[(\d+)/(\d+)\]"), "undistort", "Undistorting photo {a} of {b}"),
    (re.compile(r"Fusing image \[(\d+)/(\d+)\]"), "fusing", "Fusing depth map {a} of {b}"),
    (re.compile(r"=== Reconstructing component (\d+) / (\d+) with (\d+) images ==="), "note",
     "Component {a} of {b}: {c} photos"),
    (re.compile(r"=== Running (.+?) ==="), "stage", "Global alignment: {name}"),
    (re.compile(r"Global bundle adjustment iteration (\d+) / (\d+) finished"), "note", "Adjustment iteration {a} of {b}"),
    (re.compile(r"^Retriangulation and Global bundle adjustment"), "note", "Retriangulation and adjustment of the whole block"),
    (re.compile(r"^Global bundle adjustment$"), "note", "Adjustment of the whole block"),
    (re.compile(r"^Finding good initial image pair"), "note", "Looking for the first pair of photos"),
    (re.compile(r"^Merging reconstructions|^Merging clusters"), "note", "Merging the clusters"),
    (re.compile(r"^Bundle adjustment report"), "note", "Adjustment done"),
    (re.compile(r"^Reconstruction done in"), "note", "Reconstruction done"),
]
COUNTING = ("features", "blocks", "blocks2", "undistort", "fusing")
_LOCK = threading.Lock()
_ACTIVE: list[str] = []  # the folder of the relay in progress, if any (nested steps keep the outer one)


class Line(NamedTuple):
    kind: str
    message: str
    a: int | None
    b: int | None
    fraction: float | None  # None for a line that counts nothing by itself


def parse(line: str) -> Line | None:
    """The :class:`Line` of a line of COLMAP's log, or None for a line that says nothing of the
    progress. ``register`` lines carry the photos placed so far in ``b`` (the fraction needs the
    total); ``stage`` lines the fraction of :data:`STAGES`."""
    m = _LINE.match(line.rstrip())
    text = m.group(1) if m else line.strip()
    for rx, kind, template in _PATTERNS:
        g = rx.search(text)
        if not g:
            continue
        groups = g.groups()
        nums = [int(v) if v is not None and v.isdigit() else None for v in groups]
        a = nums[0] if nums else None
        b = nums[1] if len(nums) > 1 else None
        c = nums[2] if len(nums) > 2 else None
        name = groups[0] if groups and nums[0] is None else ""
        fraction = None
        if kind == "blocks2":  # [i/n, j/n]: block (i, j) of n x n, in COLMAP's order
            i, n, j, _ = nums
            a, b = (i - 1) * n + j, n * n
        if kind in COUNTING and a is not None and b:
            fraction = min(1.0, a / b)
        elif kind == "stage":
            fraction = STAGES.get(name)
        return Line(kind, template.format(a=a, b=b, c=c, name=name), a, b, fraction)
    return None


class _Tail(threading.Thread):
    """Reads the log file as COLMAP writes it and reports its lines to ``progress``."""

    def __init__(self, prefix: Path, progress: Progress, total: int | None,
                 phases: dict[str, tuple[float, float]] | None, interval: float = 0.25):
        super().__init__(name="ppg-colmap-log", daemon=True)
        self.prefix = prefix
        self.progress = progress
        self.total = total
        self.phases = phases or {}
        self.interval = interval
        self.stop = threading.Event()
        self.last_sees = 0.0
        self.lines = 0

    def _file(self) -> Path | None:
        files = sorted(self.prefix.parent.glob(self.prefix.name + "*"), key=lambda p: p.stat().st_mtime)
        return files[-1] if files else None

    def run(self) -> None:
        fh = None
        buffer = ""
        while True:
            if fh is None:
                path = self._file()
                if path is not None:
                    try:
                        fh = open(path, "r", errors="replace")
                    except OSError:
                        fh = None
            if fh is not None:
                chunk = fh.read()
                if chunk:
                    buffer += chunk
                    *lines, buffer = buffer.split("\n")
                    for line in lines:
                        self.line(line)
            if self.stop.is_set():
                if fh is not None:
                    for line in (buffer + fh.read()).split("\n"):
                        self.line(line)
                    fh.close()
                break
            time.sleep(self.interval)

    def line(self, text: str) -> None:
        parsed = parse(text)
        if parsed is None:
            return
        self.lines += 1
        fraction = parsed.fraction
        if parsed.kind == "register" and self.total and parsed.b is not None:
            fraction = min(1.0, parsed.b / self.total)
        if parsed.kind == "sees":  # one of these every photo placed: at most one a second
            now = time.monotonic()
            if now - self.last_sees < 1.0:
                return
            self.last_sees = now
        try:
            phase = self.phases.get(parsed.kind)
            if fraction is not None and phase is not None:
                start, span = phase
                self.progress(start + span * min(1.0, fraction), parsed.message)
            else:
                self.progress.message(parsed.message)
        except Exception:  # the step is being cancelled: the thread just stops reporting
            self.stop.set()


@contextlib.contextmanager
def relay(progress: Progress | None, folder: Path | str, total: int | None = None,
          phases: dict[str, tuple[float, float]] | None = None):
    """COLMAP's log of the ``with`` block relayed to ``progress``: ``phases`` maps a kind of line
    (``features``, ``blocks``, ``blocks2``, ``register``, ``stage``, ``undistort``, ``fusing``) to the
    ``(start, span)`` of the step's fraction it fills; ``total`` is the number of photos a mapper can
    place. Lines of other kinds reach the progress as messages only. The log file lives in ``folder``
    for the duration of the block. Nothing happens without pycolmap or inside another relay."""
    progress = as_progress(progress)
    try:
        import pycolmap
    except ImportError:  # pragma: no cover
        yield
        return
    folder = Path(folder)
    with _LOCK:
        nested = bool(_ACTIVE)
        if not nested:
            _ACTIVE.append(str(folder))
    if nested:
        yield
        return
    folder.mkdir(parents=True, exist_ok=True)
    prefix = folder / "colmap_log."
    for old in folder.glob(prefix.name + "*"):
        with contextlib.suppress(OSError):
            old.unlink()
    lg = pycolmap.logging
    saved = (lg.minloglevel, lg.stderrthreshold, lg.alsologtostderr)
    tail = _Tail(prefix, progress, total, phases)
    try:
        lg.set_log_destination(lg.Level.INFO, str(prefix))
        lg.minloglevel = 0
        lg.alsologtostderr = False
        lg.stderrthreshold = 1  # warnings and errors still reach the terminal
        tail.start()
        yield
    finally:
        lg.set_log_destination(lg.Level.INFO, "")  # closes (and flushes) the file
        lg.minloglevel, lg.stderrthreshold, lg.alsologtostderr = saved
        tail.stop.set()
        tail.join(5.0)
        with _LOCK:
            if _ACTIVE:
                _ACTIVE.pop()
        for old in folder.glob(prefix.name + "*"):
            with contextlib.suppress(OSError):
                old.unlink()
