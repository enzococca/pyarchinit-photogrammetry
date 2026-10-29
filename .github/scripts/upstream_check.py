"""The weekly look at the libraries the engine is built on - pycolmap (COLMAP) and Open3D - run by
.github/workflows/upstream.yml: the program takes them up to the versions it was tested with (pyproject.toml), and this
tries the newest ones before pip users get them.

    upstream_check.py plan --track pycolmap --track open3d [--asked pycolmap=4.3.0] [--no-prereleases] --out plan.json
    upstream_check.py api --plan plan.json --out api-before.json     # the public API of the installed versions
    pip install <the versions of the plan>                           # (the workflow)
    upstream_check.py api --plan plan.json --out api-after.json
    pytest ...; upstream_check.py failures pytest.log                 # the failed tests, run once more
    upstream_check.py tests --status 1 --log pytest.log [--rerun-log ... --rerun-status 0] --out tests.json
    upstream_check.py rustgis --tag v0.4.1 --stage tests --status 1 --log rustgis.log --out rustgis.json
    upstream_check.py report --plan plan.json --before ... --after ... --tests tests.json --rustgis rustgis.json --src src

The plan takes, for each library, the newest version on PyPI beyond the installed one that has a wheel for the
runner, pre-releases included (the warning comes before the release); the versions the program excludes (``!=``, a
known crash) are left out. The report says what the new versions remove that the program uses (by name, where), what
they add - a GPU back-end among it - how the tests went with them, and what to do: raise the ceiling, adapt the
program first, or exclude a version pip users already get.

RustGIS (the desktop GIS the live link talks to) is not a library of the engine: its newest release is tried apart, with
the tests of the link against its ``rustgis-cli`` (``rustgis`` command), and its outcome is one section of the report."""
from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from packaging.requirements import Requirement
from packaging.tags import Tag, sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

# the modules whose public API is compared: those the program uses
MODULES = {"pycolmap": ["pycolmap"],
           "open3d": ["open3d.core", "open3d.geometry", "open3d.io", "open3d.utility", "open3d.t.geometry",
                      "open3d.pipelines.registration", "open3d.pipelines.integration"]}
GPU_WORDS = {"cuda", "metal", "sycl", "vulkan", "gpu", "mps"}  # a GPU back-end among the names added
LISTED = 30  # names of a long list shown in the report, the rest counted


# ------------------------------------------------------------------------------------------------- the plan
def requirement(pyproject: dict, name: str) -> Requirement:
    """The requirement of the program on ``name``: among its dependencies, else in its optional ones."""
    project = pyproject["project"]
    lines = list(project.get("dependencies", []))
    lines += [line for extra in project.get("optional-dependencies", {}).values() for line in extra]
    for line in lines:
        req = Requirement(line)
        if canonicalize_name(req.name) == canonicalize_name(name):
            return req
    raise ValueError(f"the program does not name {name} in pyproject.toml")


def compatible_versions(page: dict, tags) -> list[Version]:
    """The versions of a PyPI project (its JSON page) with a wheel for ``tags`` that is not withdrawn, oldest first:
    nothing is compiled on the runner (COLMAP and Open3D take an hour and more)."""
    tags = set(tags)
    found = []
    for text, files in page.get("releases", {}).items():
        try:
            version = Version(text)
        except InvalidVersion:
            continue
        for f in files:
            if f.get("yanked") or not f.get("filename", "").endswith(".whl"):
                continue
            try:
                wheel_tags = parse_wheel_filename(f["filename"])[3]
            except Exception:
                continue
            if wheel_tags & tags:
                found.append(version)
                break
    return sorted(found)


def excluded(req: Requirement) -> list[str]:
    """The versions the program excludes one by one (``!=``): a known crash, not tried again."""
    return [s.version for s in req.specifier if s.operator == "!="]


def newest(req: Requirement, installed: str, versions: list[Version], prereleases: bool = True) -> Version | None:
    """The newest of ``versions`` beyond ``installed`` that the program does not exclude, or None."""
    current = Version(installed)
    skip = {Version(v) for v in excluded(req) if "*" not in v}
    for version in reversed(versions):
        if version <= current:
            return None
        if version in skip or (version.is_prerelease and not prereleases):
            continue
        return version
    return None


def make_plan(names, pyproject: dict, fetch, installed, tags, asked: dict | None = None,
              prereleases: bool = True) -> list[dict]:
    """What the run tries, one entry per library: ``fetch(name)`` gives its PyPI page, ``installed(name)`` the version
    installed with the program; ``asked`` the versions asked for (a run started by hand), tried as they are."""
    plan = []
    for name in names:
        req = requirement(pyproject, name)
        versions = compatible_versions(fetch(name), tags)
        current = installed(name)
        if asked and asked.get(name):
            candidate = Version(asked[name])
            if candidate not in versions:
                raise ValueError(f"{name} {asked[name]}: no wheel of it for this runner on PyPI")
        else:
            candidate = newest(req, current, versions, prereleases)
        plan.append({"name": name, "requirement": str(req.specifier), "installed": current,
                     "candidate": None if candidate is None else str(candidate),
                     "prerelease": bool(candidate is not None and candidate.is_prerelease),
                     "within": bool(candidate is not None and req.specifier.contains(candidate, prereleases=True)),
                     "asked": bool(asked and asked.get(name)), "excluded": excluded(req)})
    return plan


def pip_specs(plan: list[dict]) -> list[str]:
    return [f"{e['name']}=={e['candidate']}" for e in plan if e["candidate"]]


def _pypi(name: str) -> dict:
    request = urllib.request.Request(f"https://pypi.org/pypi/{name}/json",
                                     headers={"User-Agent": "pyarchinit-photogrammetry/upstream-check"})
    with urllib.request.urlopen(request, timeout=60) as answer:
        return json.load(answer)


def _installed(name: str) -> str:
    from importlib import metadata

    return metadata.version(name)


# --------------------------------------------------------------------------------------------------- the API
def api_names(modules: list[str]) -> list[str]:
    """The public names of ``modules`` and of the members of their classes (``pycolmap.SiftExtractionOptions.
    max_num_features``): what the program can touch."""
    names = set()
    for module_name in modules:
        module = importlib.import_module(module_name)
        for name in dir(module):
            if name.startswith("_"):
                continue
            names.add(f"{module_name}.{name}")
            value = getattr(module, name, None)
            if inspect.isclass(value):
                names.update(f"{module_name}.{name}.{member}" for member in dir(value) if not member.startswith("_"))
    return sorted(names)


def api(plan: list[dict]) -> dict[str, list[str]]:
    """The public API of every library of the plan, each read in a process of its own (two native libraries in one
    process can disagree on their OpenMP runtime)."""
    found = {}
    for entry in plan:
        modules = MODULES.get(canonicalize_name(entry["name"]), [entry["name"]])
        done = subprocess.run([sys.executable, __file__, "names", *modules], capture_output=True, text=True)
        if done.returncode:
            raise RuntimeError(f"the API of {entry['name']} could not be read: {done.stderr.strip()[-2000:]}")
        found[entry["name"]] = json.loads(done.stdout)
    return found


def _gpu(name: str, modules: list[str]) -> bool:
    """``name`` speaks of a GPU back-end or device: a flag ``has_<x>`` of the module itself (``pycolmap.has_cuda``),
    or a word of its last part (``use_gpu``, ``open3d.core.sycl``, ``MetalOptions``)."""
    parent, last = name.rsplit(".", 1)
    if parent in modules and last.startswith("has_"):
        return True
    words = re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", last)
    return any(w.lower() in GPU_WORDS or w.lower().rstrip("s") in GPU_WORDS for w in words)


def _uses(names: list[str], src: Path) -> dict[str, list[str]]:
    """Where the program uses each of ``names`` (as ``.<last part>``: an attribute, a function, an option): up to
    three ``path:line``, paths from the parent of ``src``."""
    lines = []
    for path in sorted(src.rglob("*.py")):
        rel = path.relative_to(src.parent).as_posix()
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            lines.append((rel, number, line))
    found = {}
    for name in names:
        pattern = re.compile(r"\." + re.escape(name.rsplit(".", 1)[-1]) + r"\b")
        found[name] = [f"{rel}:{number}" for rel, number, line in lines if pattern.search(line)][:3]
    return found


# ------------------------------------------------------------------------------------------------- the tests
def failures(log: str) -> list[str]:
    """The tests that failed, from the short summary of pytest (``-rfE``): their node ids."""
    found = []
    for line in log.splitlines():
        for word in ("FAILED ", "ERROR "):
            if line.startswith(word):
                node = line[len(word):].split(" - ", 1)[0].strip()
                if node and node not in found:
                    found.append(node)
    return found


# ------------------------------------------------------------------------------------------------ the report
def _listed(names: list[str]) -> list[str]:
    shown = [f"- `{n}`" for n in names[:LISTED]]
    if len(names) > LISTED:
        shown.append(f"- ... and {len(names) - LISTED} more")
    return shown


def rustgis_result(tag: str, stage: str, status: int, log: str) -> dict:
    """What the run found with the newest RustGIS: ``stage`` is where it ended, "fetch" (its ``rustgis-cli`` could not be
    taken from the release) or "tests" (the tests of the live link ran, ``status`` their exit code)."""
    return {"tag": tag, "stage": stage, "status": status, "failed": failures(log) if stage == "tests" else []}


def rustgis_section(result: dict | None) -> list[str]:
    """The lines of the report about RustGIS: one line of the outcome, and the tests that failed."""
    if result is None:
        return []
    tag = result["tag"]
    out = ["", "## RustGIS", ""]
    if result["stage"] == "fetch":
        out.append(f"- RustGIS {tag}: its `rustgis-cli` could not be taken from the release (see the step), so the "
                   "live link was not tried with it.")
    elif result["status"] == 0:
        out.append(f"- RustGIS {tag}: the tests of the live link pass with its `rustgis-cli`.")
    elif result["failed"]:
        out.append(f"- RustGIS {tag}: {len(result['failed'])} tests of the live link fail with its `rustgis-cli`; "
                   "adapt the link (io/rustgis_link.py) to what changed before the program asks for it:")
        out += [f"  - `{t}`" for t in result["failed"]]
    else:
        out.append(f"- RustGIS {tag}: the tests of the live link stopped (exit {result['status']}) before the summary "
                   "of pytest: see the log.")
    return out


def report(plan: list[dict], before: dict, after: dict, tests: dict | None, src: Path,
           rustgis: dict | None = None) -> str:
    """The summary of the run (Markdown): the versions tried, what they remove and add, the tests, what to do, and how
    the newest RustGIS did."""
    out = ["## The libraries of the engine: pycolmap and Open3D", "",
           "| library | the program asks for | installed | tried | |", "|---|---|---|---|---|"]
    for e in plan:
        if e["candidate"]:
            tried = e["candidate"] + (" (pre-release)" if e["prerelease"] else "")
            where = ("asked for" if e["asked"] else "") or ("within the range" if e["within"] else "beyond the ceiling")
        else:
            tried, where = f"nothing newer than {e['installed']}", ""
        if e["excluded"]:
            where = "; ".join(filter(None, [where, "excluded: " + ", ".join(e["excluded"])]))
        out.append(f"| {e['name']} | `{e['requirement']}` | {e['installed']} | {tried} | {where} |")
    tried = [e for e in plan if e["candidate"]]
    if not tried:
        out += ["", "Nothing newer to try this week: the program has the newest versions it can take."]
        return "\n".join(out + rustgis_section(rustgis)) + "\n"
    for e in tried:
        old, new = set(before.get(e["name"], [])), set(after.get(e["name"], []))
        removed, added = sorted(old - new), sorted(new - old)
        out += ["", f"### What {e['name']} {e['candidate']} removes"]
        if removed:
            uses = _uses(removed, src)
            used = [n for n in removed if uses[n]]
            out += [f"- `{n}`: used in {', '.join(uses[n])}" for n in used]
            unused = [n for n in removed if not uses[n]]
            if unused:
                out.append(f"- not used by the program ({len(unused)}):")
                out += ["  " + line for line in _listed(unused)]
        else:
            out.append("- nothing")
        out += ["", f"### What {e['name']} {e['candidate']} adds"]
        modules = MODULES.get(canonicalize_name(e["name"]), [e["name"]])
        gpu = [n for n in added if _gpu(n, modules)]
        if gpu:
            out.append("- GPU back-ends or devices, to measure against the program's own kernels "
                       "(engine/upstream.py, the diagnosis):")
            out += ["  " + line for line in _listed(gpu)]
        rest = [n for n in added if n not in gpu]
        out += _listed(rest) if rest else ([] if gpu else ["- nothing"])
    out += ["", "### The tests with these versions"]
    if tests is None:
        out.append("- not run")
    elif tests["status"] == 0:
        out.append("- passed" + (f", {len(tests['flaky'])} on the second try:" if tests["flaky"] else ""))
        out += [f"  - `{t}`" for t in tests["flaky"]]
    elif tests["failed"]:
        out.append(f"- {len(tests['failed'])} failed twice:")
        out += [f"  - `{t}`" for t in tests["failed"]]
        if tests["flaky"]:
            out.append(f"- {len(tests['flaky'])} failed once and passed on the second try:")
            out += [f"  - `{t}`" for t in tests["flaky"]]
    else:
        out.append(f"- the run stopped (exit {tests['status']}) before the summary of pytest: see the log")
    out += ["", "### What to do"]
    ok = tests is not None and tests["status"] == 0
    for e in tried:
        name, version = e["name"], e["candidate"]
        if tests is None:
            out.append(f"- {name} {version}: not tested in this run.")
        elif e["within"] and ok:
            out.append(f"- {name} {version}: within the range, pip users get it already, and it works.")
        elif e["within"]:
            out.append(f"- {name} {version}: within the range, so pip users get it already: exclude it "
                       f"(`!={version}` in pyproject.toml) and release, then adapt the program.")
        elif ok:
            final = "" if not e["prerelease"] else " when the final release is out and passes too"
            out.append(f"- {name} {version} works: raise the ceiling in pyproject.toml{final}, then release.")
        else:
            out.append(f"- {name} {version}: the ceiling keeps it away from pip users; adapt the program (the names "
                       "removed above, the failed tests) before raising it.")
    return "\n".join(out + rustgis_section(rustgis)) + "\n"


# ---------------------------------------------------------------------------------------------- command line
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    p.add_argument("--track", action="append", required=True)
    p.add_argument("--asked", action="append", default=[], help="name=version, tried as it is")
    p.add_argument("--no-prereleases", action="store_true")
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("api")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("names")
    p.add_argument("modules", nargs="+")
    p = sub.add_parser("failures")
    p.add_argument("log", type=Path)
    p = sub.add_parser("tests")
    p.add_argument("--status", type=int, required=True)
    p.add_argument("--log", type=Path, required=True)
    p.add_argument("--rerun-log", type=Path)
    p.add_argument("--rerun-status", type=int)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("rustgis")
    p.add_argument("--tag", required=True)
    p.add_argument("--stage", choices=["fetch", "tests"], required=True)
    p.add_argument("--status", type=int, required=True)
    p.add_argument("--log", type=Path)
    p.add_argument("--out", type=Path, required=True)
    p = sub.add_parser("report")
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--before", type=Path)
    p.add_argument("--after", type=Path)
    p.add_argument("--tests", type=Path)
    p.add_argument("--rustgis", type=Path)
    p.add_argument("--src", type=Path, default=Path("src"))
    args = ap.parse_args(argv)

    def read(path):
        return json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else None

    if args.command == "plan":
        import tomllib

        pyproject = tomllib.loads(args.pyproject.read_text(encoding="utf-8"))
        asked = dict(item.split("=", 1) for item in args.asked if item.split("=", 1)[-1].strip())
        plan = make_plan(args.track, pyproject, _pypi, _installed, set(sys_tags()), asked,
                         prereleases=not args.no_prereleases)
        args.out.write_text(json.dumps(plan, indent=1), encoding="utf-8")
        specs = pip_specs(plan)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write(f"install={' '.join(specs)}\n")
        print(" ".join(specs) or "nothing newer")
    elif args.command == "api":
        args.out.write_text(json.dumps(api(read(args.plan))), encoding="utf-8")
    elif args.command == "names":
        print(json.dumps(api_names(args.modules)))
    elif args.command == "failures":
        print("\n".join(failures(args.log.read_text(encoding="utf-8", errors="replace"))))
    elif args.command == "tests":
        first = failures(args.log.read_text(encoding="utf-8", errors="replace"))
        if args.rerun_log and args.rerun_log.exists():
            failed = failures(args.rerun_log.read_text(encoding="utf-8", errors="replace"))
            status = args.rerun_status if args.rerun_status is not None else args.status
            result = {"status": status, "failed": failed, "flaky": [t for t in first if t not in failed]}
        else:
            result = {"status": args.status, "failed": first, "flaky": []}
        args.out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    elif args.command == "rustgis":
        log = args.log.read_text(encoding="utf-8", errors="replace") if args.log and args.log.exists() else ""
        args.out.write_text(json.dumps(rustgis_result(args.tag, args.stage, args.status, log), indent=1),
                            encoding="utf-8")
    elif args.command == "report":
        tests, rustgis = read(args.tests), read(args.rustgis)
        print(report(read(args.plan), read(args.before) or {}, read(args.after) or {}, tests, args.src, rustgis),
              end="")
        red = (tests is not None and tests["status"] != 0) or (rustgis is not None and (
            rustgis["stage"] == "fetch" or rustgis["status"] != 0))
        return 1 if red else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
