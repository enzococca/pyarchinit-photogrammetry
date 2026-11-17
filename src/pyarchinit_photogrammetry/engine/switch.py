"""The engine of a step: ``auto``, ``classic`` or ``native``.

The matching, the alignment and the depth maps of a chunk (:data:`STEPS`) take an ``engine``. ``classic`` is the
chain the program has always had (:mod:`.sfm`, :mod:`.dense`), ``native`` the engine of the native core, ``auto``
the choice of the program. The native engine does none of these steps yet (:data:`NATIVE_STEPS` is empty): ``auto``
is the classic engine, and ``native`` falls back to it, says so in the log and leaves the reason in the record of
the step (:func:`record`, ``chunk.meta["engine"]``), which the report prints.

The word comes from the argument, else from the ``PPG_ENGINE`` environment variable, else it is ``auto``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)

ENGINES = ("auto", "classic", "native")
# the steps that take an engine, by the name of their method of Chunk
STEPS = ("match_photos", "align_cameras", "build_depth_maps")
# those of them the native engine does: none yet
NATIVE_STEPS: frozenset[str] = frozenset()
_ACCEPTED = f"{', '.join(ENGINES[:-1])} or {ENGINES[-1]}"


@dataclass(frozen=True)
class Choice:
    """The engine of one step: the one asked for (``requested``), the one that does the step (``used``) and, when
    the one asked for cannot do it, the reason another one does (``fallback``; None otherwise)."""

    requested: str
    used: str
    fallback: str | None


def check(engine: str | None) -> str:
    """``engine`` as one of :data:`ENGINES`, without the spaces around it and in small letters. None: the word of
    the ``PPG_ENGINE`` environment variable, ``auto`` when it is not set or empty. An unknown word raises a
    ValueError that names the accepted ones, and the variable when the word came from it."""
    if engine is None:
        word, where = os.environ.get("PPG_ENGINE", "").strip().lower() or "auto", " in PPG_ENGINE"
    else:
        word, where = str(engine).strip().lower(), ""
    if word not in ENGINES:
        raise ValueError(f"unknown engine '{word}'{where}: use {_ACCEPTED}")
    return word


def resolve(step: str, engine: str | None = None) -> Choice:
    """The engine that does ``step`` (one of :data:`STEPS`, the name of its method of
    :class:`..core.chunk.Chunk`) when ``engine`` is asked for (:func:`check`). ``classic`` is the classic engine.
    ``auto`` takes the native engine for a step only once that step has passed its gate: none has, and ``auto`` is
    the classic engine. ``native`` on a step the native engine does not do falls back to the classic one, with the
    reason in ``fallback`` and one line in the log: never an error. A name that is not a step raises a ValueError
    that names it, before anything is done under it."""
    if step not in STEPS:
        raise ValueError(f"unknown step '{step}': the steps that take an engine are "
                         f"{', '.join(STEPS[:-1])} and {STEPS[-1]}")
    requested = check(engine)
    if requested != "native":
        return Choice(requested, "classic", None)
    if step in NATIVE_STEPS:
        return Choice(requested, "native", None)
    fallback = f"the native engine does not do '{step}' yet"
    log.warning("engine 'native' asked for %s: classic used (%s)", step, fallback)
    return Choice(requested, "classic", fallback)


def classic_only(step: str, choice: Choice) -> None:
    """For a method that calls the classic engine and no other: stop it, before it starts, when ``choice`` gives
    ``step`` to another engine - a NotImplementedError that names the step and the engine. A step put in
    :data:`NATIVE_STEPS` before its method calls the native engine would otherwise run on the classic one and be
    recorded as run on the native one."""
    if choice.used != "classic":
        raise NotImplementedError(f"{step} cannot run on the '{choice.used}' engine: it calls only the classic one")


def record(chunk, step: str, choice: Choice) -> None:
    """Keep in ``chunk.meta["engine"]`` what was asked and what was used for ``step``, once the step has finished:
    ``{"requested", "used", "fallback"}`` under the name of the step. The report prints it on a fallback, or when
    the engine used is not the classic one.

    The work of the step is done when this is called, and it does not fail on what the project holds under the key
    (a name that was free for a plugin or a script before the program took it): anything there that is not a dict
    is replaced, with a warning that names its type and never its value; ``None`` is replaced without a word."""
    kept = chunk.meta.get("engine")
    if not isinstance(kept, dict):
        if kept is not None:
            log.warning('meta["engine"] keeps the engines of the steps and held a value of type %s: it was replaced',
                        type(kept).__name__)
        kept = chunk.meta["engine"] = {}
    kept[step] = {"requested": choice.requested, "used": choice.used, "fallback": choice.fallback}
