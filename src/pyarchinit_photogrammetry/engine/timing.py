"""The time of a step and of each of its stages, kept in the chunk.

Matching and alignment (:mod:`.sfm`) measure themselves and keep what they measured in ``chunk.meta["timings"]``
(:func:`store`), beside the record of the engine that did them (``chunk.meta["engine"]``): the seconds of the whole
step and those of each of its stages - the parts of the matching (``features``, ``preselection``, ``pairs``), the
stages COLMAP announces in its log during the alignment (``rotation_averaging``, ``global_positioning``, ...) - so
that one engine can be compared with another stage by stage. What no stage covers is the difference between the
seconds of the step and the sum of its stages; it is not stored. The record holds times, the names of stages and, for
an alignment that failed, the name of the type of its error (``failed``): no path, no name of a photo, no coordinate,
no text of an error.
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterator

log = logging.getLogger(__name__)

LIVE_LOG_ENV = {"GLOG_logbufsecs": "0"}
"""The environment a process needs, before pycolmap is first imported, for the stages of the alignment to be marked
when COLMAP logs them: glog keeps INFO lines in a 4 KB buffer that it writes out when it is full, every 30 s on a
later line, or when the file is closed, and it reads this setting once, when it loads. The package puts the same into
its environment when it is imported (``_env.glog_flushes_each_line``, which reads this constant), but only if the
environment holds no value of its own and pycolmap is not loaded yet: a process that measures the engines is started
with this, whatever its parent says."""


class Stages:
    """The seconds of the stages of one step, by name, in the order the names first came. A stage is timed around
    its code (:meth:`stage`) or from the moment a log announces it to the next announcement (:meth:`mark`,
    :meth:`close`); a name that comes again adds its seconds to those it has. The stages of a step do not overlap.

    A lock guards the times: the thread that tails COLMAP's log (:func:`.colmap_log.relay`) marks the stages it
    reads while the step goes on in its own thread. ``clock`` gives the time in seconds."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._seconds: dict[str, float] = {}
        self._open: tuple[str, float] | None = None  # the stage a mark opened, and when

    @contextlib.contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """The time of the ``with`` block as the stage ``name``, also when the block raises."""
        with self._lock:
            self._seconds.setdefault(name, 0.0)
        start = self._clock()
        try:
            yield
        finally:
            seconds = self._clock() - start
            with self._lock:
                self._seconds[name] += seconds

    def mark(self, name: str) -> None:
        """Close the stage a mark opened, if one is open, and open ``name``: for the stages a log announces as they
        begin. A stage read from COLMAP's log (:func:`.colmap_log.relay`) begins when the tail reads its line: its
        boundaries are known to the polling interval of the tail (0.25 s) only when glog writes out each line
        (:data:`LIVE_LOG_ENV`, which the package also asks for when it is imported before pycolmap); otherwise they
        are known to glog's buffer - a stage can be marked late, and stages whose lines come out together get
        0.0 s. The seconds of the step are exact either way."""
        with self._lock:
            now = self._clock()
            self._close(now)
            self._seconds.setdefault(name, 0.0)
            self._open = (name, now)

    def close(self) -> None:
        """Close the stage a mark opened; nothing when none is open."""
        with self._lock:
            self._close(self._clock())

    def _close(self, now: float) -> None:
        if self._open is not None:
            name, start = self._open
            self._seconds[name] += now - start
            self._open = None

    def as_dict(self) -> dict[str, float]:
        """The seconds of each stage, rounded to the millisecond, in the order the names first came; the time of a
        stage still open is added when it is closed."""
        with self._lock:
            return {name: round(seconds, 3) for name, seconds in self._seconds.items()}


def store(chunk, step: str, seconds: float, stages: dict[str, float], **extra) -> None:
    """Keep the time of ``step`` in ``chunk.meta["timings"]``, under the name of the step: ``seconds`` (the whole
    step, rounded to the millisecond), ``stages`` (:meth:`Stages.as_dict`) and the ``extra`` items. A step measured
    again replaces its record. What the project holds under the key and is not a dict (a name that was free for a
    plugin or a script before the program took it) is replaced, with a warning that names its type, never its value;
    ``None`` is replaced without a word."""
    kept = chunk.meta.get("timings")
    if not isinstance(kept, dict):
        if kept is not None:
            log.warning('meta["timings"] keeps the time of the steps and held a value of type %s: it was replaced',
                        type(kept).__name__)
        kept = chunk.meta["timings"] = {}
    kept[step] = {"seconds": round(seconds, 3), "stages": stages, **extra}
