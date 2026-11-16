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
NOT_A_BASELINE = f"not a baseline (n < {BASELINE_RUNS})"
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


def _version(program: str) -> tuple:
    """The version of a program as it sorts: its runs of digits as numbers (``1.9.0`` before ``1.10.2``), the rest as
    text after them (``2.0.0`` before ``2.0.0.dev1``)."""
    return tuple((0, int(piece)) if piece.isdigit() else (1, piece) for piece in re.findall(r"\d+|[^\d.]+", program))


def _engine(value) -> str | None:
    """The engine a step used, as its entry records it (``used`` of the engine's record, or the text)."""
    if isinstance(value, dict):
        used = value.get("used")
        return str(used) if used else None
    return str(value) if value else None


def summarise(records) -> tuple[list[dict], dict[str, int]]:
    """The rows of the valid records, one per ``(name, program, step)``, in the order of the names and the programs
    and, within them, of the steps as the records list them: ``runs`` (the valid records that ran the step),
    ``seconds`` (the median), ``min``, ``max``, ``peak_memory_mb`` (the highest), ``stages`` (the median of each),
    ``engine`` (the engines the step used, joined by ``/``), ``baseline`` (``runs >= BASELINE_RUNS``), and of the
    name and program as a whole ``valid_runs`` (its valid records) and ``results`` (the median of each number of
    their results; a result that is no number, the text of an error, gives no figure), the same for every step.

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
        group = groups.setdefault((str(rec.get("name")), str(rec.get("program"))), {"runs": 0, "results": {},
                                                                                     "steps": {}})
        group["runs"] += 1
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
        for step, s in group["steps"].items():
            rows.append({"name": name, "program": program, "step": step, "engine": "/".join(s["engines"]) or None,
                         "runs": len(s["seconds"]), "seconds": median(s["seconds"]), "min": min(s["seconds"]),
                         "max": max(s["seconds"]), "peak_memory_mb": max(s["peaks"]) if s["peaks"] else None,
                         "stages": {stage: median(values) for stage, values in s["stages"].items()},
                         "baseline": len(s["seconds"]) >= BASELINE_RUNS, "valid_runs": group["runs"],
                         "results": results})
    return rows, left_out


def _line(cells) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def _number(value) -> str:
    """A result as the table shows it: a whole number as it is, the others to six significant digits."""
    return f"{value:g}"


def render(rows, left_out: dict[str, int]) -> str:
    """``rows`` (of :func:`summarise`) as a Markdown table - seconds and stages to a tenth of a second, the memory in
    whole MB; a row with fewer than :data:`BASELINE_RUNS` runs says :data:`NOT_A_BASELINE` -, then the table of the
    results (one line per name and program: the median of each number over its valid runs, the keys of
    :data:`FIRST_RESULTS` first, the others by name, ``-`` where a program has no such number) and, as its last line,
    the count of the records left out, by reason."""
    lines = [_line(HEAD), _line(["---"] * len(HEAD))]
    for row in rows:
        stages = ", ".join(f"{stage} {value:.1f}" for stage, value in row["stages"].items()) or "-"
        peak = "-" if row["peak_memory_mb"] is None else f"{row['peak_memory_mb']:.0f}"
        lines.append(_line([row["name"], row["program"], row["step"], row["engine"] or "-", row["runs"],
                            f"{row['seconds']:.1f}", f"{row['min']:.1f}", f"{row['max']:.1f}", peak, stages,
                            "yes" if row["baseline"] else NOT_A_BASELINE]))
    groups: dict[tuple[str, str], tuple[int, dict]] = {}
    for row in rows:
        groups.setdefault((row["name"], row["program"]), (row["valid_runs"], row["results"]))
    keys = sorted({key for _, results in groups.values() for key in results},
                  key=lambda k: (k not in FIRST_RESULTS, FIRST_RESULTS.index(k) if k in FIRST_RESULTS else 0, k))
    if keys:
        lines += ["", "Results, the median of the valid runs:", "", _line(["name", "program", "runs", *keys]),
                  _line(["---"] * (3 + len(keys)))]
        for (name, program), (runs, results) in groups.items():
            lines.append(_line([name, program, runs,
                                *("-" if key not in results else _number(results[key]) for key in keys)]))
    counted = ", ".join(f"{kind} {count}" for kind, count in sorted(left_out.items()))
    lines += ["", f"Records left out: {counted or 'none'}."]
    return "\n".join(lines)
