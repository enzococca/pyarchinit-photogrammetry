"""The table of the bench: the records of a folder (:func:`load`), summarised by run, program and step over the valid
runs (:func:`summarise`) and written as Markdown (:func:`render`), so that two versions measured on the same set are
read side by side.

A figure is a baseline with :data:`BASELINE_RUNS` runs: the median of three stands against one run the machine
disturbed, and two cannot say which one it was. The records that are not valid are left out of every figure and
counted by reason; the results of a run (photos aligned, errors on the references, those against the truth of a
synthetic set) are the median of each number over the valid runs.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from statistics import median

from . import record

log = logging.getLogger(__name__)

# the runs a figure needs to be a baseline: the median of three stands against one run the machine disturbed
BASELINE_RUNS = 3
# the entries of a record's settings that record what the run did, not what it was asked for: the version of pycolmap,
# the count of pairs (above 40 photos the preselection's RANSAC has a random seed, so it moves between repeats) and
# their split between the GPU and the CPU (the GPU threads take pairs as they go), the mapper and its fallback (a
# global alignment that falls back in some runs); runs that differ in any other setting are not one measurement
RUN_FACTS = ("pycolmap", "pairs", "gpu_pairs", "cpu_pairs", "mapper", "global_fallback")
# the facts of the machine that two runs of one measurement share
MACHINE_KEYS = ("system", "arch", "cpu", "memory_gb")
# the facts of the set that two runs of one measurement share: the same photos (how many, their bytes)
SET_KEYS = ("photos", "bytes")
HEAD = ("name", "program", "step", "engine", "runs", "seconds (median)", "min", "max", "peak memory (MB)", "stages",
        "baseline")
# the results of every alignment, first in the table of the results in this order; the others follow by name
FIRST_RESULTS = ("cameras", "aligned", "reprojection_rms_px", "control_rmse", "check_rmse", "camera_rmse", "markers")


def load(folder) -> list[dict]:
    """The records of schema :data:`.record.SCHEMA` in the ``*.json`` files of ``folder``, in the order of their names
    (the run, its start, its index). A file that is not one - not JSON, not an object, another schema - is named in a
    warning, by its name and never its path, and skipped."""
    records = []
    for path in sorted(Path(folder).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("%s is not a record (%s): skipped", path.name, type(exc).__name__)
            continue
        if not isinstance(data, dict) or data.get("schema") != record.SCHEMA:
            log.warning("%s is not a record of schema %d: skipped", path.name, record.SCHEMA)
            continue
        records.append(data)
    return records


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _pieces(program: str) -> tuple:
    """The version of a program as it sorts without ``packaging``: its runs of digits as numbers (``1.9.0`` before
    ``1.10.2``), the rest as text after them (``2.0.0`` before ``2.0.0.dev1``, unlike the rules of versions)."""
    return tuple((0, int(piece)) if piece.isdigit() else (1, piece) for piece in re.findall(r"\d+|[^\d.]+", program))


def _version(program: str) -> tuple:
    """The version of a program as it sorts: by the rules of versions when ``packaging`` can be imported (it is not a
    dependency of the program: ``1.9.0`` before ``1.10.2``, ``2.0.0.dev1`` before ``2.0.0``; a text that is no version
    after every version), else :func:`_pieces`."""
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:  # pragma: no cover - the tests run with pytest, which brings packaging
        return (0, _pieces(program))
    try:
        return (0, Version(program))
    except InvalidVersion:
        return (1, _pieces(program))


def _settings_key(rec: dict) -> str:
    """The settings a run was asked for, as one text: the record's settings without :data:`RUN_FACTS`."""
    settings = {key: value for key, value in (rec.get("settings") or {}).items() if key not in RUN_FACTS}
    return json.dumps(settings, sort_keys=True, default=str)


def _machine_key(rec: dict) -> str:
    """The facts of :data:`MACHINE_KEYS` of the machine of a run, as one text."""
    machine = rec.get("machine") or {}
    return json.dumps([machine.get(key) for key in MACHINE_KEYS], default=str)


def _set_key(rec: dict) -> str:
    """The facts of :data:`SET_KEYS` of the set of a run, as one text."""
    set_ = rec.get("set") or {}
    return json.dumps([set_.get(key) for key in SET_KEYS], default=str)


def _engine(value) -> str | None:
    """The engine a step used, as its entry records it (``used`` of the engine's record, or the text)."""
    if isinstance(value, dict):
        used = value.get("used")
        return str(used) if used else None
    return str(value) if value else None


def summarise(records) -> tuple[list[dict], dict[str, int]]:
    """The rows of the valid records, one per ``(name, program, step)``, in the order of the names, of the programs
    as versions and, within them, of the steps as the records list them: ``runs`` (the valid records that ran the
    step), ``seconds`` (the median), ``min``, ``max``, ``peak_memory_mb`` (the highest), ``stages`` (the median of
    each), ``engine`` (the engines the step used, joined by ``/``), ``baseline``, and of the name and program as a
    whole ``valid_runs`` (its valid records), ``results`` (the median of each number of their results; a result that
    is no number, the text of an error, gives no figure), ``result_runs`` (how many of them hold each result),
    ``mixed_settings`` (they differ in a setting other than :data:`RUN_FACTS`), ``mixed_machines`` (they differ in a
    fact of :data:`MACHINE_KEYS`) and ``mixed_sets`` (they differ in a fact of :data:`SET_KEYS`), the same for every
    step. A row is a ``baseline`` with :data:`BASELINE_RUNS` runs or more of one setting on one machine and one set.

    With them the records left out: those not valid, counted by the text of each of their reasons before its colon
    (``slow disk link``; a record with two reasons counts under both); one not valid without a reason, or never
    judged, counts as ``not valid``."""
    left_out: dict[str, int] = {}
    groups: dict[tuple[str, str], dict] = {}
    for rec in records:
        if rec.get("valid") is not True:
            for reason in rec.get("invalid_reasons") or ["not valid"]:
                kind = str(reason).split(":", 1)[0].strip() or "not valid"
                left_out[kind] = left_out.get(kind, 0) + 1
            continue
        group = groups.setdefault((str(rec.get("name")), str(rec.get("program"))),
                                  {"runs": 0, "results": {}, "steps": {}, "settings": set(), "machines": set(),
                                   "sets": set()})
        group["runs"] += 1
        group["settings"].add(_settings_key(rec))
        group["machines"].add(_machine_key(rec))
        group["sets"].add(_set_key(rec))
        for key, value in (rec.get("results") or {}).items():
            if _is_number(value):
                group["results"].setdefault(str(key), []).append(value)
        for entry in rec.get("steps") or []:
            if not isinstance(entry, dict) or not entry.get("step") or not _is_number(entry.get("seconds")):
                continue
            step = group["steps"].setdefault(str(entry["step"]), {"seconds": [], "peaks": [], "stages": {},
                                                                 "engines": []})
            step["seconds"].append(float(entry["seconds"]))
            if _is_number(entry.get("peak_memory_mb")):
                step["peaks"].append(float(entry["peak_memory_mb"]))
            for stage, value in (entry.get("stages") or {}).items():
                if _is_number(value):
                    step["stages"].setdefault(str(stage), []).append(float(value))
            engine = _engine(entry.get("engine"))
            if engine and engine not in step["engines"]:
                step["engines"].append(engine)
    rows = []
    for (name, program), group in sorted(groups.items(), key=lambda item: (item[0][0], _version(item[0][1]))):
        results = {key: median(values) for key, values in group["results"].items()}
        result_runs = {key: len(values) for key, values in group["results"].items()}
        mixed_settings, mixed_machines = len(group["settings"]) > 1, len(group["machines"]) > 1
        mixed_sets = len(group["sets"]) > 1
        mixed = mixed_settings or mixed_machines or mixed_sets
        for step, s in group["steps"].items():
            rows.append({"name": name, "program": program, "step": step, "engine": "/".join(s["engines"]) or None,
                         "runs": len(s["seconds"]), "seconds": median(s["seconds"]), "min": min(s["seconds"]),
                         "max": max(s["seconds"]), "peak_memory_mb": max(s["peaks"]) if s["peaks"] else None,
                         "stages": {stage: median(values) for stage, values in s["stages"].items()},
                         "baseline": len(s["seconds"]) >= BASELINE_RUNS and not mixed,
                         "valid_runs": group["runs"], "results": results, "result_runs": result_runs,
                         "mixed_settings": mixed_settings, "mixed_machines": mixed_machines,
                         "mixed_sets": mixed_sets})
    return rows, left_out


def _line(cells) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _number(value) -> str:
    """A result as the table shows it: a whole number as it is, the others to six significant digits."""
    return f"{value:g}"


def _baseline(row: dict) -> str:
    """The baseline cell of a row: ``yes``, or ``not a baseline`` with why (``n < 3``, ``mixed settings``, ``mixed
    machines``, ``mixed sets``)."""
    reasons = []
    if row["runs"] < BASELINE_RUNS:
        reasons.append(f"n < {BASELINE_RUNS}")
    if row["mixed_settings"]:
        reasons.append("mixed settings")
    if row["mixed_machines"]:
        reasons.append("mixed machines")
    if row["mixed_sets"]:
        reasons.append("mixed sets")
    return "yes" if not reasons else f"not a baseline ({', '.join(reasons)})"


def _result(row: dict, key: str) -> str:
    """The cell of the result ``key`` of a name and program: its median, with its count when fewer than the valid
    runs hold it (``0.002 (2 of 3)``); ``-`` when none does."""
    if key not in row["results"]:
        return "-"
    text = _number(row["results"][key])
    runs = row["result_runs"].get(key, row["valid_runs"])
    return text if runs >= row["valid_runs"] else f"{text} ({runs} of {row['valid_runs']})"


def render(rows, left_out: dict[str, int]) -> str:
    """``rows`` (of :func:`summarise`) as a Markdown table - seconds and stages to a tenth of a second, the memory in
    whole MB; a row that is no baseline says why (:func:`_baseline`) -, then the table of the results (one line per
    name and program: the median of each number over its valid runs, with its count when only some of them hold it,
    the keys of :data:`FIRST_RESULTS` first, the others by name, ``-`` where a program has no such number) and, as
    its last line, the count of the records left out, by reason."""
    lines = [_line(HEAD), _line(["---"] * len(HEAD))]
    for row in rows:
        stages = ", ".join(f"{stage} {value:.1f}" for stage, value in row["stages"].items()) or "-"
        peak = "-" if row["peak_memory_mb"] is None else f"{row['peak_memory_mb']:.0f}"
        lines.append(_line([row["name"], row["program"], row["step"], row["engine"] or "-", row["runs"],
                            f"{row['seconds']:.1f}", f"{row['min']:.1f}", f"{row['max']:.1f}", peak, stages,
                            _baseline(row)]))
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        groups.setdefault((row["name"], row["program"]), row)
    keys = sorted({key for row in groups.values() for key in row["results"]},
                  key=lambda k: (k not in FIRST_RESULTS, FIRST_RESULTS.index(k) if k in FIRST_RESULTS else 0, k))
    if keys:
        lines += ["", "Results, the median of the valid runs:", "", _line(["name", "program", "runs", *keys]),
                  _line(["---"] * (3 + len(keys)))]
        for (name, program), row in groups.items():
            lines.append(_line([name, program, row["valid_runs"], *(_result(row, key) for key in keys)]))
    counted = ", ".join(f"{kind} {count}" for kind, count in sorted(left_out.items()))
    lines += ["", f"Records left out: {counted or 'none'}."]
    return "\n".join(lines)
