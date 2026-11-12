"""Height-field surface: the edge limit is local, so that a cloud of uneven density has no
holes where it is sparser; empty areas and the outside margin stay open, and holes are closed
only on request (and reported as interpolated)."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.core.document import Document
from pyarchinit_photogrammetry.core.pointcloud import PointCloud
from pyarchinit_photogrammetry.engine import meshing

UP = np.array([0.0, 0.0, 1.0])
HOLE = np.array([6.5, 5.0])  # centre of the area without points
HOLE_RADIUS = 1.5


def uneven_cloud(notch: bool = False) -> np.ndarray:
    """10 x 10 m of ground: 2500 points per square metre in a strip of 3 m, 25 in the rest
    (a spacing of 2 cm against 20 cm), nothing in a disc of 3 m; ``notch``: nothing in the
    corner of 2 x 2 m either, a re-entrant outline."""
    rng = np.random.default_rng(3)
    dense = rng.uniform([0, 0], [3, 10], (75_000, 2))
    sparse = rng.uniform([3, 0], [10, 10], (1750, 2))
    xy = np.vstack([dense, sparse])
    xy = xy[np.linalg.norm(xy - HOLE, axis=1) > HOLE_RADIUS]
    if notch:
        xy = xy[~((xy[:, 0] > 8) & (xy[:, 1] > 8))]
    z = 0.2 * np.sin(xy[:, 0]) * np.cos(0.7 * xy[:, 1])
    return np.column_stack([xy, z])


def plan_area(mesh, lo=(0.0, 0.0), hi=(10.0, 10.0)) -> float:
    """Area in plan of the faces whose centre is in the rectangle."""
    v = mesh.vertices[:, :2][mesh.faces]
    c = v.mean(axis=1)
    keep = (c >= lo).all(axis=1) & (c <= hi).all(axis=1)
    a, b = v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]
    return float(0.5 * np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])[keep].sum())


def covered(mesh, p) -> bool:
    """Whether a face lies over the point of the plan."""
    v = mesh.vertices[:, :2][mesh.faces] - np.asarray(p, float)
    cross = [v[:, i, 0] * v[:, (i + 1) % 3, 1] - v[:, i, 1] * v[:, (i + 1) % 3, 0] for i in range(3)]
    return bool(((np.sign(cross[0]) == np.sign(cross[1])) & (np.sign(cross[1]) == np.sign(cross[2]))).any())


def test_sparser_zone_has_no_holes():
    """With one limit for the whole cloud (set by the dense strip) the sparse zone was riddled
    with holes: 7 x 10 m less the disc must be covered."""
    mesh = meshing._height_field(uneven_cloud(), UP)
    expected = 70.0 - np.pi * HOLE_RADIUS ** 2
    assert plan_area(mesh, lo=(3.0, 0.0)) > 0.93 * expected
    assert plan_area(mesh, hi=(3.0, 10.0)) > 0.97 * 30.0
    for p in ((5.0, 1.0), (9.0, 9.0), (4.0, 8.0), (8.5, 2.0), (3.05, 5.0)):
        assert covered(mesh, p)
    assert (mesh.face_normals @ UP > 0).all()


def test_empty_area_stays_open():
    """Nothing was measured in the disc: no face over its middle, and little over the rest."""
    mesh = meshing._height_field(uneven_cloud(), UP)
    assert not covered(mesh, HOLE)
    assert not covered(mesh, HOLE + [0.5, 0.0]) and not covered(mesh, HOLE - [0.0, 0.5])
    assert plan_area(mesh) < 100.0 - 0.5 * np.pi * HOLE_RADIUS ** 2


def test_holes_closed_up_to_a_size():
    report: dict = {}
    small = meshing._height_field(uneven_cloud(), UP, fill_holes=1.0, report=report)
    assert not covered(small, HOLE)  # a gap of 3 m is larger than 1 m
    assert report["holes_left"] >= 1
    report = {}
    mesh = meshing._height_field(uneven_cloud(), UP, fill_holes=3.5, report=report)
    assert covered(mesh, HOLE)
    assert plan_area(mesh) > 99.0
    # the faces that close the holes are counted apart: the disc, less what the local limit spans
    assert report["holes_filled"] >= 1
    assert 0.4 * np.pi * HOLE_RADIUS ** 2 < report["interpolated_area"] < 1.1 * np.pi * HOLE_RADIUS ** 2
    # the heights over the hole are those of its rim (interpolated on flat faces)
    assert np.abs(mesh.vertices[:, 2]).max() <= 0.2 + 1e-9


def test_outside_margin_is_never_closed():
    """A re-entrant corner is outside the survey, however large the holes to close."""
    report: dict = {}
    mesh = meshing._height_field(uneven_cloud(notch=True), UP, fill_holes=100.0, report=report)
    assert covered(mesh, HOLE)
    for p in ((9.4, 9.4), (9.8, 9.0), (9.0, 9.8)):
        assert not covered(mesh, p)
    assert plan_area(mesh, lo=(8.9, 8.9)) < 0.3
    assert report["holes_left"] == 0


@pytest.fixture
def chunk(tmp_path):
    doc = Document()
    ch = doc.add_chunk("terreno")
    doc.save(tmp_path / "t.ppg")
    xyz = uneven_cloud()
    ch.point_cloud = PointCloud(xyz, np.full((len(xyz), 3), 120, np.uint8))
    return ch


def test_build_model_reports_what_is_interpolated(chunk):
    mesh = chunk.build_model(surface="height_field", face_count=10 ** 9)
    report = chunk.meta["model"]
    assert report["surface_used"] == "height_field" and report["fill_holes"] is None
    assert report["interpolated_area"] == 0.0 and not covered(mesh, HOLE)
    mesh = chunk.build_model(surface="height_field", face_count=10 ** 9, fill_holes=3.5)
    report = chunk.meta["model"]
    assert covered(mesh, HOLE)
    assert report["fill_holes"] == 3.5 and report["holes_filled"] >= 1
    assert 2.5 < report["interpolated_area"] < 8.0  # square metres, in plan


def test_fill_holes_is_in_metres(chunk):
    """A chunk scaled by 2: a hole of 3 internal units is 6 m wide."""
    chunk.transform = np.diag([2.0, 2.0, 2.0, 1.0])
    mesh = chunk.build_model(surface="height_field", face_count=10 ** 9, fill_holes=3.5)
    assert not covered(mesh, HOLE)
    mesh = chunk.build_model(surface="height_field", face_count=10 ** 9, fill_holes=7.0)
    assert covered(mesh, HOLE)
    assert 10.0 < chunk.meta["model"]["interpolated_area"] < 32.0


def test_height_field_is_mapped_from_above(chunk):
    """A height field shows every face from above: the orthophoto mapping, whatever its size."""
    from pyarchinit_photogrammetry.engine.texturing import choose_mapping

    mesh = chunk.build_model(surface="height_field", face_count=5000)
    assert len(mesh.faces) < 200_000
    assert choose_mapping(chunk, mesh.vertices, mesh.faces) == "orthophoto"
    chunk.meta["model"]["surface"] = "arbitrary"  # also when Poisson fell back to the height field
    assert choose_mapping(chunk, mesh.vertices, mesh.faces) == "generic"


def test_model_dialog_closes_the_holes(trench, monkeypatch):
    """*Flusso di lavoro > Costruisci modello*: the gap to close is a field of the dialog, and the log says how
    much of the surface is interpolated."""
    w, app, mw = trench
    xyz = uneven_cloud()
    w.chunk.point_cloud = PointCloud(xyz, np.full((len(xyz), 3), 120, np.uint8))
    asked, said = [], []

    def ask(_parent, title, fields, intro=""):
        asked.append({f.name: f.default for f in fields})
        return {**asked[-1], "source": "point_cloud", "surface": "height_field", "face_count": "high",
                "fill_holes": 3.5}

    monkeypatch.setattr(mw.ParamDialog, "ask", ask)
    monkeypatch.setattr(w, "_need_chunk", lambda *a, **k: True)
    monkeypatch.setattr(w.log_pane, "warning", lambda text, *a: said.append(text))
    w.build_model()
    w.runner.wait()
    app.processEvents()
    assert asked[0]["fill_holes"] == 0.0  # nothing is closed unless asked
    report = w.chunk.meta["model"]
    assert report["fill_holes"] == 3.5 and report["holes_filled"] >= 1 and report["interpolated_area"] > 2.5
    assert said and "interpolati (non misurati)" in said[-1]


def test_batch_model_step_closes_the_holes(chunk, tmp_path, caplog):
    """A *Modello 3D* step of a batch has the gap to close (0: none), a batch file written before it existed is
    still read, and the log says how much of the surface is interpolated."""
    import json
    import logging

    from pyarchinit_photogrammetry import batch

    doc = chunk.document
    assert batch.step("build_model")["params"]["fill_holes"] == 0.0  # nothing is closed unless asked
    old = tmp_path / "old.json"  # a batch saved by 1.7.8
    old.write_text(json.dumps({"steps": [{"op": "build_model", "chunks": "all", "params": {
        "source": "point_cloud", "surface": "height_field", "face_count": "high"}}]}))
    steps = batch.load_steps(old)
    assert steps[0]["params"]["fill_holes"] == 0.0
    report = batch.run_batch(doc, steps)
    assert report["done"] == ["0:0"] and not report["failed"]
    model = chunk.meta["model"]
    assert model["fill_holes"] is None and not model["holes_filled"]
    open_area = plan_area(chunk.model)

    steps = [batch.step("build_model", surface="height_field", face_count="high", fill_holes=3.5)]
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        report = batch.run_batch(doc, steps)
    assert report["done"] == ["0:0"] and not report["failed"]
    model = chunk.meta["model"]
    assert model["fill_holes"] == 3.5 and model["holes_filled"] >= 1 and model["interpolated_area"] > 2.5
    assert plan_area(chunk.model) > open_area + 2.5
    assert "interpolated, not measured" in caplog.text
    # the arbitrary surface has no holes to close: said, and the step still runs
    caplog.clear()
    seen = {}
    chunk.build_model = lambda **kw: seen.update(kw)
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        batch.run_step(doc, chunk, "build_model", batch.step("build_model", fill_holes=2.0)["params"])
    assert seen["fill_holes"] == 2.0 and seen["surface"] == "arbitrary" and "only in the height field" in caplog.text
    batch.run_step(doc, chunk, "build_model", batch.step("build_model")["params"])
    assert seen["fill_holes"] is None  # 0 in the batch: the method is told nothing


def test_batch_stopped_before_the_new_parameter_is_resumed(chunk):
    """A batch stopped under 1.7.8 (its model step without ``fill_holes``) is the same batch after the update: it
    goes on from where it stopped instead of starting again."""
    import json

    from pyarchinit_photogrammetry import batch

    doc = chunk.document
    old = [{"op": "build_point_cloud", "chunks": "all", "params": {}},
           {"op": "build_model", "chunks": "all",
            "params": {"source": "point_cloud", "surface": "height_field", "face_count": "high"}}]
    batch.state_path(doc).parent.mkdir(parents=True, exist_ok=True)
    batch.state_path(doc).write_text(json.dumps({"steps": old, "done": ["0:0"], "failed": {}, "skipped": [],
                                                 "started": "2026-11-11T08:00:00", "finished": None}))
    state = batch.pending(doc)
    assert state is not None and batch.pending(doc, [batch.step(s["op"], s["chunks"], **s["params"]) for s in old])
    ran = []
    chunk.build_point_cloud = lambda **kw: ran.append("cloud")
    report = batch.run_batch(doc, state["steps"], resume=True)
    assert not ran and report["done"] == ["0:0", "0:1"]  # the cloud was done: only the model is built
    assert chunk.meta["model"]["surface_used"] == "height_field"
    # a state that is not a list of steps is no batch to resume
    batch.state_path(doc).write_text(json.dumps({"steps": [{"op": "nothing"}], "done": [], "finished": None}))
    assert batch.pending(doc, [batch.step("build_model")]) is None


def test_batch_dialog_has_the_gap_of_the_model_dialog(chunk, monkeypatch):
    """*Elaborazione batch > Modello 3D*: the same field as *Costruisci modello* (words, default, range), kept in
    the step and shown in its line."""
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtWidgets import QApplication

    from pyarchinit_photogrammetry import batch
    from pyarchinit_photogrammetry.gui import batch_dialog, i18n
    from pyarchinit_photogrammetry.gui import main_window as mw

    app = QApplication.instance() or QApplication([])
    form = {f.name: f for f in batch_dialog.fields("build_model", {})}
    field = form["fill_holes"]
    assert (field.default, field.minimum, field.maximum, field.decimals) == (0.0, 0.0, 1000.0, 3)
    asked = []
    monkeypatch.setattr(mw.ParamDialog, "ask", lambda _p, _t, fields, intro="": asked.append(fields))
    window = type("W", (), {"_need_chunk": lambda self, **k: True, "chunk": chunk, "_class_fields": lambda self, t: [],
                            "_version_fields": lambda self, k: [], "_class_hint": lambda self, t: ""})()
    mw.MainWindow.build_model(window)
    same = next(f for f in asked[0] if f.name == "fill_holes")
    assert (field.label, field.tip, field.default, field.minimum, field.maximum, field.decimals) == \
        (same.label, same.tip, same.default, same.minimum, same.maximum, same.decimals)
    assert field.label in i18n.catalog() and field.tip in i18n.catalog()
    assert batch_dialog.fields("build_model", {"fill_holes": 2.5})[-1].default == 2.5

    monkeypatch.setattr(batch_dialog.ParamDialog, "ask", lambda _p, _t, fields: {
        **{f.name: f.default for f in fields}, "surface": "height_field", "fill_holes": 2.5})
    dlg = batch_dialog.BatchDialog(chunk.document)
    dlg.add_step("build_model")
    step = dlg.steps()[0]
    assert step["params"]["fill_holes"] == 2.5 and step["params"]["surface"] == "height_field"
    assert "Chiudi i buchi fino a: 2.5" in dlg.list.item(0).text()
    assert "Chiudi i buchi" not in batch_dialog.describe(batch.step("build_model"))  # the default is not listed
    assert i18n.catalog()["Chiudi i buchi fino a"] == "Close holes up to"
    dlg.deleteLater()
    app.processEvents()


def test_process_all_closes_the_holes(trench, monkeypatch):
    """*Elabora tutto*: the gap to close goes to the model of the height field, and the log says how much of the
    surface is interpolated; on the arbitrary surface it says that nothing is closed."""
    w, app, mw = trench
    xyz = uneven_cloud()
    cloud = PointCloud(xyz, np.full((len(xyz), 3), 120, np.uint8))
    ch, asked, said, answer = w.chunk, [], [], {"surface": "height_field", "fill_holes": 3.5}

    def ask(_parent, title, fields, intro=""):
        asked.append({f.name: f for f in fields})
        return {**{f.name: f.default for f in fields}, "markers": False, "texture": False, "dem": False,
                "ortho": False, "report": False, **answer}

    kinds = type(ch)
    built = []
    real = kinds.build_model
    monkeypatch.setattr(mw.ParamDialog, "ask", ask)
    monkeypatch.setattr(w, "_need_chunk", lambda *a, **k: True)
    monkeypatch.setattr(w, "_photos_to_skip", lambda *a, **k: False)
    monkeypatch.setattr(w.log_pane, "warning", lambda text, *a: said.append(text))
    monkeypatch.setattr(mw.MainWindow, "_show_model", lambda self, kind: None)
    monkeypatch.setattr("pyarchinit_photogrammetry.markers.gcp.preflight", lambda chunk: None)
    for name in ("align_cameras", "build_depth_maps"):
        monkeypatch.setattr(kinds, name, lambda self, *a, **k: None)
    monkeypatch.setattr(kinds, "match_photos", lambda self, *a, **k: self.cameras.clear())
    monkeypatch.setattr(kinds, "build_point_cloud", lambda self, **k: setattr(self, "point_cloud", cloud))
    monkeypatch.setattr(kinds, "build_model", lambda self, *a, **k: (
        built.append(k), real(self, *a, **{**k, "face_count": 10 ** 9}) if k["surface"] == "height_field" else None)[1])

    def run():
        ch.cameras.append(object())  # a photo, so that the window opens: the made-up matching takes it away
        w.process_all()
        w.runner.wait()
        app.processEvents()

    run()
    field = asked[0]["fill_holes"]
    assert (field.default, field.minimum, field.maximum, field.decimals) == (0.0, 0.0, 1000.0, 3)
    assert list(asked[0]).index("fill_holes") == list(asked[0]).index("surface") + 1
    assert built[0]["fill_holes"] == 3.5
    report = ch.meta["model"]
    assert report["fill_holes"] == 3.5 and report["holes_filled"] >= 1
    assert said and "interpolati (non misurati)" in said[-1]

    said.clear()
    answer.update(surface="arbitrary")
    run()
    assert built[1]["fill_holes"] == 3.5 and any("solo nel campo di altezze" in t for t in said)
    answer.update(fill_holes=0.0)
    run()
    assert built[2]["fill_holes"] is None


def test_process_command_closes_the_holes(tmp_path, monkeypatch):
    """``ppg process --surface height_field --fill-holes``: the whole pipeline builds the height field and closes
    its holes; the gap alone, on the arbitrary surface, is said to do nothing."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry import cli
    from pyarchinit_photogrammetry.core.chunk import Chunk
    from pyarchinit_photogrammetry.markers import gcp

    asked = []
    for name in ("add_photos", "match_photos", "align_cameras", "build_depth_maps", "build_point_cloud",
                 "build_texture", "build_dem", "build_orthomosaic"):
        monkeypatch.setattr(Chunk, name, lambda self, *a, **k: None)
    monkeypatch.setattr(Chunk, "generate_report", lambda self, path: path)
    monkeypatch.setattr(gcp, "preflight", lambda chunk: None)

    def build_model(self, **params):
        asked.append(params)
        if params.get("fill_holes") and params.get("surface") == "height_field":
            self.meta["model"] = {"holes_filled": 3, "interpolated_area": 1.25}

    monkeypatch.setattr(Chunk, "build_model", build_model)
    run = lambda *more: CliRunner().invoke(  # noqa: E731
        cli.app, ["process", str(tmp_path / f"p{len(asked)}.ppg"), str(tmp_path), "--targets", "", *more])
    out = run("--surface", "height_field", "--fill-holes", "0.5")
    assert out.exit_code == 0, out.output
    assert asked[-1]["surface"] == "height_field" and asked[-1]["fill_holes"] == 0.5
    assert "3 holes closed: 1.25 m2" in out.output
    out = run("--fill-holes", "0.5")
    assert out.exit_code == 0, out.output
    assert asked[-1]["surface"] == "arbitrary" and "--surface height_field" in out.output
    out = run()
    assert asked[-1]["surface"] == "arbitrary" and asked[-1]["fill_holes"] is None
    assert "holes" not in out.output


def test_process_command_reads_the_file_of_the_points(tmp_path, monkeypatch):
    """``ppg process --gcp``: the file of the control points reaches ``import_reference``, and without it nothing is
    imported (the module of the checks took the name of the option: every run stopped after the alignment)."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry import cli
    from pyarchinit_photogrammetry.core.chunk import Chunk
    from pyarchinit_photogrammetry.markers import gcp

    read = []
    for name in ("add_photos", "match_photos", "align_cameras", "build_depth_maps", "build_point_cloud",
                 "build_model", "build_texture", "build_dem", "build_orthomosaic"):
        monkeypatch.setattr(Chunk, name, lambda self, *a, **k: None)
    monkeypatch.setattr(Chunk, "generate_report", lambda self, path: path)
    monkeypatch.setattr(Chunk, "import_reference", lambda self, path, crs=None: read.append((path, crs)))
    monkeypatch.setattr(Chunk, "update_transform", lambda self: {"method": "similarity", "control_rmse": 0.001})
    monkeypatch.setattr(gcp, "preflight", lambda chunk: None)
    points = tmp_path / "gcp.csv"
    points.write_text("label,x,y,z\n", encoding="utf-8")
    base = ["process", str(tmp_path / "p.ppg"), str(tmp_path), "--targets", ""]
    out = CliRunner().invoke(cli.app, base)
    assert out.exit_code == 0, out.output
    assert read == [] and "georeferencing" not in out.output
    out = CliRunner().invoke(cli.app, [*base, "--gcp", str(points), "--crs", "EPSG:32633"])
    assert out.exit_code == 0, out.output
    assert read == [(points, "EPSG:32633")] and "georeferencing: similarity" in out.output
