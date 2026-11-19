"""``ppg bench adjust``: the native adjustment, COLMAP's and scipy's on the reconstruction of a kept project, on the
same plain problem (:func:`..engine.bundle.problem_from_reconstruction`: the tie points, every calibration free but
the principal point, the first photo and the farthest held), each timed in a child process and recorded side by side
in one record per run (:mod:`.record`, as ``ppg bench run`` records the steps of a set of photos).

    ppg bench adjust PROJECT --name NAME [--solvers native,colmap,scipy] [--repeat 3] [--records DIR]
    python -m pyarchinit_photogrammetry.bench.adjust <job.json>      # the child

:func:`run` is the parent: per repeat it starts the child in a work folder under the records folder and, with the
runner's own functions, samples its memory, collects and judges the record. :func:`main` is the child: it opens the
project and never saves it, reads the reconstruction from disk once per solver, builds the plain problem and solves
it. The costs of the three are compared by one evaluator, the native :func:`..engine.bundle.evaluate` of the problem
at each solver's end point (for COLMAP, the problem read again from the reconstruction it adjusted in memory), so
that the figures are comparable whatever a solver reports of itself. scipy, the reference of the tests, is left out
above :data:`SCIPY_MAX_PHOTOS` photos (hours on a large block), with a warning in the record.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..core.document import Document
from ..engine import bundle
from . import machine, record, runner, steps

SOLVERS = ("native", "colmap", "scipy")
# scipy adjusts a block of this many photos in minutes; above it, hours: it is left out, with a warning in the record
SCIPY_MAX_PHOTOS = 100
# the budget of every solver (scipy: its evaluations), that of the equivalence tests of the synthetic problems
MAX_ITERATIONS = 200
# the terminations that are a convergence: the words of the native solver and of scipy, and COLMAP's
CONVERGED = ("function", "gradient", "parameters", "function_and_parameters", "convergence")


@dataclass(frozen=True)
class ColmapResult:
    """What :func:`colmap_adjust` reports of COLMAP's solve: the ``seconds`` of the solve (with Ceres' set-up of its
    problem), the ``residuals``
    Ceres had (two per observation), its ``iterations`` (what Ceres' report calls the minimizer iterations: the
    successful and the unsuccessful steps), its ``termination`` as a word (``convergence``, ``no_convergence``,
    ``failure``, ``user_success``, ``user_failure``) and the ``linear_solver`` it used (``dense_schur``,
    ``sparse_schur``, ``iterative_schur``...)."""

    seconds: float
    residuals: int
    iterations: int
    termination: str
    linear_solver: str


def colmap_adjust(rec, held: tuple[int, int], max_iterations: int) -> ColmapResult:
    """COLMAP's bundle adjustment (pycolmap, the Ceres backend) of ``rec`` in place, set up on the problem of
    :func:`..engine.bundle.problem_from_reconstruction`: every registered image, the poses of the frames of the two
    ``held`` image ids constant (one photo per frame, as that problem requires), the focal lengths and the
    distortion refined and the principal points held, the squared loss, every point of ``rec`` in
    (``min_track_length`` 0), at most ``max_iterations`` iterations of Ceres. The reconstruction is changed in
    memory only. The seconds are those of the solve with Ceres' set-up of its problem (the residual blocks and
    the parameterisation), as the native solver's seconds hold the core's set-up from the problem given to it."""
    import pycolmap

    config = pycolmap.BundleAdjustmentConfig()
    for iid, image in rec.images.items():
        if image.has_pose:
            config.add_image(iid)
    for iid in held:
        config.set_constant_rig_from_world_pose(rec.images[iid].frame_id)
    options = pycolmap.BundleAdjustmentOptions()
    options.backend = pycolmap.BundleAdjustmentBackend.CERES
    options.refine_focal_length = True
    options.refine_extra_params = True
    options.refine_principal_point = False
    options.min_track_length = 0
    options.print_summary = False
    options.ceres.loss_function_type = pycolmap.LossFunctionType.TRIVIAL
    options.ceres.solver_options.max_num_iterations = int(max_iterations)
    started = time.perf_counter()  # with Ceres' set-up of its problem, as the native seconds hold the core's
    adjuster = pycolmap.create_default_bundle_adjuster(options, config, rec)
    summary = adjuster.solve()
    seconds = time.perf_counter() - started
    ceres = summary.ceres_summary
    return ColmapResult(seconds, int(summary.num_residuals),
                        int(ceres.num_successful_steps) + int(ceres.num_unsuccessful_steps),
                        summary.termination_type.name.lower(), ceres.linear_solver_type_used.name.lower())


def camera_sigmas(pb: bundle.Problem, at: bundle.Result) -> np.ndarray:
    """The standard deviation of every photo and intrinsic parameter of ``pb`` at the result ``at``: the square root
    of the diagonal of the inverse of the reduced camera system, the Schur complement of the normal matrix ``J^T J``
    with the tie points eliminated (``J`` the Jacobian of the native evaluation of the problem started at ``at``),
    in the columns of :class:`..engine.bundle._Layout` before the points. It is
    :func:`..testing.bundle_problems.sigmas` without the points: that one inverts the whole normal matrix, dense,
    which a block of thousands of points cannot afford; here each point goes through its 3 x 3 block (a tie residual
    sees one point, so the point part of the normal matrix is block diagonal) and only the camera system is
    inverted dense. The columns of the problem must be photos, intrinsics and points alone - no markers, rolling
    shutter or biases (ValueError otherwise) - and every point seen by two photos or more
    (:func:`..engine.bundle._adjustable`: a point seen once has a singular block)."""
    from scipy.sparse import csc_matrix

    lay = bundle._Layout(pb)
    n_cam, n_pts = lay.point_col, 3 * len(pb.points)
    if lay.size != n_cam + n_pts:
        raise ValueError("camera_sigmas takes a problem of tie points alone")
    jac = bundle.evaluate(bundle.problem_at(pb, at)).jacobian.tocsc()
    cameras, points = jac[:, :n_cam], jac[:, n_cam:]
    a = (cameras.T @ cameras).toarray()
    b = cameras.T @ points
    c = (points.T @ points).tocoo()
    c.sum_duplicates()  # one entry per cell for the assignment below (the product is canonical already)
    if not np.array_equal(c.row // 3, c.col // 3):  # a residual that saw two points: not a problem of tie points
        raise ValueError("camera_sigmas takes a problem of tie points alone")
    blocks = np.zeros((len(pb.points), 3, 3))
    blocks[c.row // 3, c.row % 3, c.col % 3] = c.data
    inverse = np.linalg.inv(blocks)
    cell = np.arange(3)
    rows = (3 * np.arange(len(pb.points))[:, None, None] + cell[None, :, None] + 0 * cell[None, None, :]).ravel()
    cols = (3 * np.arange(len(pb.points))[:, None, None] + 0 * cell[None, :, None] + cell[None, None, :]).ravel()
    c_inverse = csc_matrix((inverse.ravel(), (rows, cols)), shape=(n_pts, n_pts))
    reduced = a - (b @ c_inverse @ b.T).toarray()
    return np.sqrt(np.diag(np.linalg.inv(reduced)))


# ---------------------------------------------------------------- the parent
@dataclass(frozen=True)
class _Plan:
    """What every repeat of a run shares: the job's own facts and what the runner's :func:`.runner._one_run`,
    :func:`.runner._collect` and :func:`.runner._finish` read of a plan (``name``, ``out``, ``records``, ``keep``,
    ``private``, ``names``); the work folders go under the records folder and are never kept by request."""

    project: Path
    name: str
    solvers: tuple[str, ...]
    out: Path
    records: Path
    env: dict
    disk: dict
    set: dict
    settings: dict
    private: tuple[Path, ...]
    names: tuple[str, ...]
    keep: bool = False


def check(project: Path, name: str, solvers: tuple[str, ...] = SOLVERS, repeat: int = 1,
          records: Path | None = None) -> tuple[Path, Path]:
    """What :func:`run` refuses before anything runs, in its order: a name that is not one a file can take
    (ValueError), a solver that is not one of :data:`SOLVERS`, named twice, or none (ValueError), a repeat below 1
    (ValueError), ``project`` that is not a file (ValueError), a work folder of one of the repeats that exists
    already under the records folder (FileExistsError; nothing is deleted in silence), psutil missing (RuntimeError
    naming the extra). Returned: ``project`` and ``records`` resolved (``records`` is the current folder when not
    given)."""
    record.check_name(name)
    unknown = [s for s in solvers if s not in SOLVERS]
    if unknown:
        raise ValueError(f"the solvers are {', '.join(SOLVERS[:-1])} or {SOLVERS[-1]}, not {unknown[0]!r}")
    if not solvers:
        raise ValueError("no solver to run")
    if len(set(solvers)) != len(solvers):
        raise ValueError("a solver is named twice")
    if int(repeat) < 1:
        raise ValueError("repeat must be 1 or more")
    project = Path(project).resolve()
    if not project.is_file():
        raise ValueError(f"{project} is not a project file")
    records = Path(records).resolve() if records else Path.cwd()
    for index in range(1, int(repeat) + 1):
        work = records / f"{name}-{index}"
        if work.exists():
            raise FileExistsError(f"the work folder {work} exists: delete it or choose another name")
    runner._psutil()
    return project, records


def run(project: Path, name: str, solvers: tuple[str, ...] = SOLVERS, repeat: int = 1,
        records: Path | None = None) -> list[Path]:
    """``repeat`` runs of ``solvers`` (in their order) on the reconstruction of the chunk of the project file
    ``project``, each in a child process working in ``records/<name>-<i>`` (removed after, unless the run failed:
    then it stays with the child's log, said in the log by its name), one record per run written to ``records``
    (the current folder when not given). Returned: the path of each record. Refused before anything runs:
    :func:`check`.

    The child opens the project and never saves it. It reads the reconstruction from disk once per solver and
    builds the plain problem (:func:`..engine.bundle.problem_from_reconstruction`); tie points seen by fewer than
    two photos, which no solver can adjust, are left out of the reconstruction every solver loads, so that the
    three get the same problem, and the record's warnings say so. The steps ``bundle_native``, ``bundle_colmap``,
    ``bundle_scipy`` have the ``seconds`` of the solve alone, the peak memory of the child sampled by the parent, and
    an ``engine`` that says which solver ran and its ``termination``; ``bundle_scipy`` is skipped, with a warning in
    the record, above :data:`SCIPY_MAX_PHOTOS` registered photos. The results: ``photos``, ``points``,
    ``observations``, ``bundle_cost_initial``, per solver ``bundle_cost_<s>`` (by the native evaluation of the
    problem at its end point, the same evaluator for the three), ``bundle_iterations_<s>``,
    ``bundle_gradient_max_<s>`` (by the same evaluation) and ``bundle_converged_<s>`` (1 or 0); ``bundle_gap_colmap``
    and ``bundle_gap_scipy``, the cost of the native solver against the other's, relative (``(native - other) /
    other``); ``bundle_sigma_ratio_scipy``, the largest gap between the native and the scipy parameters over the
    photo and intrinsic columns in sigmas of :func:`camera_sigmas` at scipy's end point - a figure, to be read
    with ``bundle_converged_scipy``. The settings: the solvers asked, the native ``linear_solver`` and COLMAP's
    (``linear_solver_colmap``), ``max_iterations``, ``scipy_max_photos``, the version of pycolmap. The set is the
    project's photos (how many, their bytes, how many online-only or with a position), the disk the one of the
    project; the record holds no path, no photo name, no coordinate. A KeyboardInterrupt while the child runs
    terminates it, writes the record with the error ``cancelled``, removes the work folder and is raised again."""
    project, records = check(project, name, solvers, repeat, records)
    chunk = Document.open(project).chunk
    cameras = chunk.cameras if chunk is not None else []
    files = [Path(c.path) for c in cameras]
    disk = machine.disk_facts(project.parent)
    disk["read_mb_s"] = None  # the adjustment reads the model once and no photo: the disk is no part of the measure
    settings = {"solvers": list(solvers), "linear_solver": None, "linear_solver_colmap": None,
                "max_iterations": MAX_ITERATIONS, "scipy_max_photos": SCIPY_MAX_PHOTOS,
                "pycolmap": steps._pycolmap_version()}
    plan = _Plan(project=project, name=name, solvers=tuple(solvers), out=records, records=records, env=dict(os.environ),
                 disk=disk, set=_set_facts(files, cameras), settings=settings,
                 private=(project.parent, records, Path.home()), names=tuple(p.name for p in files))
    return [runner._one_run(plan, index, _run_in) for index in range(1, int(repeat) + 1)]


def _set_facts(files: list[Path], cameras) -> dict:
    """The set of a kept project as the record keeps it (the keys of :func:`.machine.photo_set_facts`): how many
    photos the chunk has, the bytes of those on disk, how many of them are online-only and how many cameras carry
    a reference position. A photo the project no longer finds counts as a photo and nothing else: the adjustment
    reads none of them."""
    present = [p for p in files if p.is_file()]
    local = [p for p in present if not machine.is_online_only(p)]
    return {"photos": len(files), "bytes": sum(p.stat().st_size for p in local),
            "online_only": len(present) - len(local),
            "gps": sum(1 for c in cameras if c.reference.location is not None)}


def _run_in(plan: _Plan, index: int, work: Path) -> Path:
    """One repeat in ``work`` (made by :func:`.runner._one_run`): the job, the child - this interpreter on this
    module -, its memory sampled every :data:`.runner.SAMPLE_SECONDS`, the record collected, judged and written by
    the runner's own functions (a step the child did not end, a child that died, the scrub of the texts, the work
    folder kept with the child's log when the run failed), the child's warnings after the judge's. A
    KeyboardInterrupt terminates the child and leaves a record saying ``cancelled``."""
    job = {"project": str(plan.project), "work": str(work), "solvers": list(plan.solvers),
           "max_iterations": MAX_ITERATIONS, "scipy_max_photos": SCIPY_MAX_PHOTOS}
    (work / runner.JOB).write_text(json.dumps(job, indent=1), encoding="utf-8")
    rec = record.new(plan.name, machine.machine_facts(), dict(plan.disk), dict(plan.set), dict(plan.settings))
    peaks: dict[str, int] = {}
    with open(work / runner.LOG, "wb") as log_file:
        proc = subprocess.Popen(_command(work / runner.JOB), stdin=subprocess.DEVNULL, stdout=log_file,
                                stderr=subprocess.STDOUT, env=plan.env, close_fds=False)
    try:
        while proc.poll() is None:
            time.sleep(runner.SAMPLE_SECONDS)
            runner._sample(proc.pid, work / steps.RESULT, peaks)
    except KeyboardInterrupt:
        runner._terminate(proc)
        warnings = runner._collect(rec, work, peaks, plan, ended=time.time(), cancelled=True)
        runner._finish(rec, work, plan, index, cancelled=True, warnings=warnings)
        raise
    warnings = runner._collect(rec, work, peaks, plan, ended=time.time(), code=proc.returncode)
    return runner._finish(rec, work, plan, index, warnings=warnings)


def _command(job: Path) -> list[str]:
    """The child: this interpreter, by its absolute path, running this module."""
    return [os.path.abspath(sys.executable), "-m", "pyarchinit_photogrammetry.bench.adjust", str(job)]


# ---------------------------------------------------------------- the child
@dataclass
class _End:
    """Where a solver ended: the figures of the record and, for the native solver and scipy, the problem and the
    result (for the gap between their parameters)."""

    requested: str
    used: str
    fallback: str | None
    seconds: float
    cost: float
    gradient_max: float
    iterations: int
    termination: str
    linear_solver: str | None
    pb: bundle.Problem | None = None
    result: bundle.Result | None = None


class _Block:
    """The plain problem of the chunk's reconstruction as the child solves it: the reconstruction is read from disk
    for the first figures and once more for every solver, each time the same block (its ids are compared); the
    tie points seen by fewer than two photos, which no solver can adjust, are deleted from every reconstruction
    read (in memory) before the problem is built."""

    def __init__(self, chunk) -> None:
        from ..engine.sfm import load_reconstruction

        self.chunk, self.load_reconstruction = chunk, load_reconstruction
        rec = load_reconstruction(chunk)
        pb, ids = bundle.problem_from_reconstruction(rec)
        keep = bundle._adjustable(pb)
        self.left_out = [ids["points"][i] for i in np.flatnonzero(~keep)]
        if self.left_out:
            pb, ids = bundle.problem_from_reconstruction(self._without_left_out(rec))
        self.ids = {"images": list(ids["images"]), "points": list(ids["points"])}
        self.photos, self.points, self.observations = len(pb.centres), len(pb.points), len(pb.obs_image)
        self.held = tuple(ids["images"][i] for i in sorted(pb.fixed_images))
        self.cost_initial = bundle.evaluate(pb).cost

    def _without_left_out(self, rec):
        for pid in self.left_out:
            rec.delete_point3D(pid)
        return rec

    def load(self):
        """The reconstruction read again and its plain problem, the same block as the first time."""
        rec = self._without_left_out(self.load_reconstruction(self.chunk))
        pb, ids = bundle.problem_from_reconstruction(rec)
        if list(ids["images"]) != self.ids["images"] or list(ids["points"]) != self.ids["points"]:
            raise RuntimeError("the reconstruction read again is not the block of the first read")
        return rec, pb

    def solve(self, solver: str, max_iterations: int) -> _End:
        """``solver`` on a reconstruction of its own; the cost and the gradient at its end point by the native
        evaluation of the problem there."""
        rec, pb = self.load()
        if solver == "colmap":
            out = colmap_adjust(rec, self.held, max_iterations)
            if out.residuals != 2 * len(pb.obs_image):
                raise RuntimeError(f"COLMAP adjusted {out.residuals} residuals where the problem has "
                                   f"{2 * len(pb.obs_image)}")
            after, _ids = bundle.problem_from_reconstruction(rec)
            # the photos held are those of the problem solved (judged again on the adjusted centres, the farthest
            # could be another photo): the gradient is read over the same columns as the other solvers'
            after.fixed_images = pb.fixed_images
            ev = bundle.evaluate(after)
            return _End("colmap", "colmap", None, out.seconds, ev.cost, float(np.abs(ev.gradient).max()),
                        out.iterations, out.termination, out.linear_solver)
        res = bundle.adjust(pb, max_iterations, solver=solver)
        ev = bundle.evaluate(bundle.problem_at(pb, res))
        return _End(solver, res.solver, res.fallback, res.seconds, ev.cost, float(np.abs(ev.gradient).max()),
                    res.iterations, str(res.termination), res.linear_solver, pb, res)


def _sigma_ratio(native: _End, scipy: _End) -> float:
    """The largest gap between the native and the scipy parameters over the photo and intrinsic columns, in sigmas
    of :func:`camera_sigmas` at scipy's end point."""
    from ..testing.bundle_problems import parameter_gap

    n_cam = bundle._Layout(native.pb).point_col
    gap = parameter_gap(native.pb, native.result, scipy.result)[:n_cam]
    return float(np.max(np.abs(gap) / camera_sigmas(native.pb, scipy.result)))


def _end(result: steps.Result, entry: dict, end: _End) -> None:
    """``entry`` completed: the seconds of the solve alone (not of the step, whose loads and evaluations are not
    timed), its peak, the engine; the figures of the solver into the results and the settings."""
    solver = end.requested
    entry["end"] = time.time()
    entry["seconds"] = round(end.seconds, 3)
    entry["peak_memory_mb"] = round(max(entry["peak_memory_mb"], steps._rss_mb()), 1)
    entry["engine"] = {"requested": solver, "used": end.used, "fallback": end.fallback, "termination": end.termination}
    results = result.data["results"]
    results[f"bundle_cost_{solver}"] = float(end.cost)
    results[f"bundle_iterations_{solver}"] = int(end.iterations)
    results[f"bundle_gradient_max_{solver}"] = float(end.gradient_max)
    results[f"bundle_converged_{solver}"] = int(end.termination in CONVERGED)
    if solver == "native":
        result.data["settings"]["linear_solver"] = end.linear_solver
    elif solver == "colmap":
        result.data["settings"]["linear_solver_colmap"] = end.linear_solver
    result.save()


def _failed(result: steps.Result, entry: dict, clock: float, error: BaseException) -> None:
    """``entry`` ended by ``error``: its seconds up to the failure, its peak, the error in it and in the result."""
    entry["end"] = time.time()
    entry["seconds"] = round(time.perf_counter() - clock, 3)
    entry["peak_memory_mb"] = round(max(entry["peak_memory_mb"], steps._rss_mb()), 1)
    result.failed(entry, error)


def main(argv: list[str] | None = None) -> int:
    """The child: the job of ``argv[0]`` run, ``result.json`` in its work folder (the shape :class:`.steps.Result`
    writes, with the warnings of the run beside it); 0 when every solver asked for ran, 1 when the job cannot be
    read, the project or its reconstruction cannot be opened or a solver failed, 2 without a job."""
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        print("usage: python -m pyarchinit_photogrammetry.bench.adjust <job.json>", file=sys.stderr)
        return 2
    job_file = Path(args[0])
    try:
        job = json.loads(job_file.read_text(encoding="utf-8"))
        work = Path(job["work"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = steps.Result(job_file.parent / steps.RESULT)
        result.data["error"] = f"job: {record.describe(exc)}"
        result.save()
        return 1
    result = steps.Result(work / steps.RESULT)
    result.data["warnings"] = []
    try:
        chunk = Document.open(job["project"]).chunk
        if chunk is None:
            raise RuntimeError("the project has no chunk")
        block = _Block(chunk)
    except Exception as exc:
        result.data["error"] = f"setup: {record.describe(exc)}"
        result.save()
        return 1
    result.data["results"].update({"photos": block.photos, "points": block.points, "observations": block.observations,
                                   "bundle_cost_initial": float(block.cost_initial)})
    if block.left_out:
        n = len(block.left_out)
        result.data["warnings"].append(f"{n} tie point{'s' if n > 1 else ''} seen by fewer than two photos left out "
                                       "of every solver")
    result.save()
    ends: dict[str, _End] = {}
    for solver in job["solvers"]:
        if solver == "scipy" and block.photos > int(job["scipy_max_photos"]):
            result.data["warnings"].append(f"scipy left out: {block.photos} photos, above {job['scipy_max_photos']}")
            result.save()
            continue
        entry, clock = result.start(f"bundle_{solver}")
        try:
            end = block.solve(solver, int(job["max_iterations"]))
        except BaseException as exc:
            _failed(result, entry, clock, exc)
            return 1
        _end(result, entry, end)
        ends[solver] = end
    try:
        _gaps(result.data["results"], ends)
    except Exception as exc:  # the solves are done and recorded; the figures between them are not
        result.data["error"] = f"figures: {record.describe(exc)}"
        result.save()
        return 1
    result.data["done"] = True
    result.save()
    return 0


def _gaps(results: dict, ends: dict[str, _End]) -> None:
    """The figures between the solvers into ``results``: the cost of the native solver against COLMAP's and
    against scipy's, relative, and the parameters of the native solver against scipy's in sigmas (each when
    both solvers ran)."""
    native = ends.get("native")
    if native is None:
        return
    for other in ("colmap", "scipy"):
        if other in ends:
            results[f"bundle_gap_{other}"] = float((native.cost - ends[other].cost) / ends[other].cost)
    if "scipy" in ends:
        results["bundle_sigma_ratio_scipy"] = _sigma_ratio(native, ends["scipy"])


if __name__ == "__main__":
    sys.exit(main())
