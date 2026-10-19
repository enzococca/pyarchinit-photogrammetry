"""Pottery: rim diameter and preserved arc of a sherd, the profile signature, and the search and
save of pottery forms in PyArchInit (``pottery_table``)."""
from __future__ import annotations

import json

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from pyarchinit_photogrammetry.archaeo import pottery
from pyarchinit_photogrammetry.testing.synthetic import make_vessel

TOP_RIM = 0.161  # outer diameter of the synthetic bowl in the top 3 mm (rim 0.16 m, flaring just below it)


def _rotated(mesh, seed):
    t = np.eye(4)
    t[:3, :3] = Rotation.random(random_state=seed).as_matrix()
    t[:3, 3] = [1.0, 2.0, 3.0]
    mesh.apply_transform(t)
    return mesh, t[:3, 2]


def test_rim_of_a_whole_vessel_and_of_sherds():
    whole = pottery.rim(make_vessel(), pottery.Axis((0, 0, 0), (0, 0, 1)))
    assert abs(whole["rim_diameter_m"] - TOP_RIM) < 0.001 and whole["rim_preserved_pct"] > 98 and whole["reliable"]
    # the shorter the arc, the looser its circle (a 45 degree arc of 8 cm radius rises 6 mm in the middle)
    for sherd, sectors, pct, tol in (((0, 120), 360, 100 / 3, 0.0015), ((10, 70), 720, 100 / 6, 0.002),
                                     ((0, 45), 720, 12.5, 0.003)):
        mesh = make_vessel(sherd=sherd, noise=0.0001, sectors=sectors)
        # the axis is used for its direction only: a point 2 cm away gives the same circle
        r = pottery.rim(mesh, pottery.Axis((0.02, -0.01, 0.05), (0, 0, 1)))
        assert r["reliable"], (sherd, r)
        assert abs(r["rim_diameter_m"] - TOP_RIM) < tol, (sherd, r)
        assert abs(r["rim_preserved_pct"] - pct) < 1.0, (sherd, r)
        assert np.hypot(*r["centre"][:2]) < 0.002  # the centre of the circle is on the true axis


def test_small_sherds_are_flagged():
    """A 20 degree arc does not fix a circle: flagged, and measured around the axis given."""
    mesh = make_vessel(sherd=(0, 20), noise=0.0001, sectors=720)
    assert not pottery.rim(mesh, pottery.Axis((0, 0, 0), (0, 0, 1)))["reliable"]
    _, meas = pottery.measure(mesh, pottery.Axis((0, 0, 0), (0, 0, 1)))
    assert not meas["rim_reliable"] and abs(meas["rim_diameter_m"] - TOP_RIM) < 0.002
    assert abs(meas["rim_preserved_pct"] - 100 / 18) < 1.0  # from the chord of the arc


def test_measure_finds_the_axis_of_a_rotated_sherd():
    mesh, z = _rotated(make_vessel(sherd=(0, 120), noise=0.0001, sectors=360), 5)
    prof, meas = pottery.measure(mesh)
    assert abs(np.dot(meas["axis"]["direction"], z)) > np.cos(np.radians(1.0))
    assert abs(meas["rim_diameter_m"] - TOP_RIM) < 0.0015 and meas["rim_reliable"]
    assert abs(meas["rim_preserved_pct"] - 100 / 3) < 1.0
    assert abs(meas["max_diameter_m"] - 0.17) < 0.003 and abs(meas["height_m"] - 0.12) < 0.002
    assert not meas["full_profile"] and meas["base_diameter_m"] is None  # a sherd has no base centre
    assert pottery.percent_class(meas["rim_preserved_pct"]) == "25-50"


def test_measure_corrects_a_shifted_axis():
    """The turntable gives the direction, not the centre of a sherd laid on it: the circle of the
    lip moves the axis there, and the profile is measured around it."""
    mesh = make_vessel(sherd=(0, 120), noise=0.0001, sectors=360)
    shifted = pottery.Axis((0.015, 0.01, 0.0), (0, 0, 1))
    bad = pottery.best_profile(mesh, shifted).measurements()
    _, meas = pottery.measure(mesh, shifted)
    assert abs(bad["rim_diameter_m"] - TOP_RIM) > 0.005  # around the wrong centre the radius is wrong
    assert abs(meas["rim_diameter_m"] - TOP_RIM) < 0.0015 and abs(meas["max_diameter_m"] - 0.17) < 0.003
    assert np.hypot(*meas["axis"]["point"][:2]) < 0.002


def test_whole_vessel_and_rim_down():
    mesh = make_vessel()
    _, meas = pottery.measure(mesh, pottery.Axis(mesh.vertices.mean(axis=0), (0, 0, 1)))
    assert meas["full_profile"] and abs(meas["base_diameter_m"] - 0.088) < 0.003
    assert pottery.percent_class(meas["rim_preserved_pct"]) == ">50"
    flipped = make_vessel(sherd=(0, 120), noise=0.0001, sectors=360)
    flipped.apply_transform(np.diag([1.0, -1.0, -1.0, 1.0]))  # upside down: the rim lies on the table
    _, down = pottery.measure(flipped, pottery.Axis(flipped.vertices.mean(axis=0), (0, 0, -1)))
    assert abs(down["rim_diameter_m"] - TOP_RIM) < 0.0015 and abs(down["rim_preserved_pct"] - 100 / 3) < 1.0


def test_percent_classes():
    assert [pottery.percent_class(p) for p in (0, 4.9, 5, 24.9, 25, 50, 99.5, 100)] == \
        ["<5", "<5", "5-25", "5-25", "25-50", ">50", ">50", ">50"]
    assert pottery.percent_class(None) is None


def test_signature_ignores_size_and_tells_shapes_apart():
    a = pottery.signature(pottery.best_profile(make_vessel(), pottery.Axis((0, 0, 0), (0, 0, 1))))
    big = make_vessel()
    big.apply_scale(1.5)
    b = pottery.signature(pottery.best_profile(big, pottery.Axis((0, 0, 0), (0, 0, 1))))
    other = make_vessel(rim_r=0.05, height=0.12)  # same family of curves, a narrow cup
    c = pottery.signature(pottery.best_profile(other, pottery.Axis((0, 0, 0), (0, 0, 1))))
    sherd = make_vessel(sherd=(0, 90), noise=0.0001, sectors=360)
    d = pottery.signature(pottery.measure(sherd, pottery.Axis((0, 0, 0), (0, 0, 1)))[0])
    assert pottery.signature_distance(a, b) < 0.01
    assert pottery.signature_distance(a, d) < 0.02  # a sherd of the same bowl
    assert pottery.signature_distance(a, c) > 0.05
    assert pottery.signature_distance(a, [None] * len(a)) is None


def test_drawing_as_image(tmp_path):
    from PIL import Image

    prof, meas = pottery.measure(make_vessel(), pottery.Axis((0, 0, 0), (0, 0, 1)))
    png = pottery.draw(prof, tmp_path / "p.png", scale=2.0, title="Ciotola & co.", measures=meas, dpi=100)
    with Image.open(png) as im:
        w, h = im.size
        dark = (np.asarray(im.convert("L")) < 128).mean()
    # 1:2 -> the bowl (17 cm wide, 12 cm high) is 85 x 60 mm on paper, plus the margins
    assert abs(w - (85 + 24) / 25.4 * 100) < 4 and abs(h - (60 + 34) / 25.4 * 100) < 4
    assert 0.01 < dark < 0.3
    svg = pottery.draw(prof, tmp_path / "p.svg", title="Ciotola & co.", measures=meas).read_text()
    assert "Ciotola &amp; co." in svg and "orlo Ø 16.1 cm" in svg


# ------------------------------------------------------------------ PyArchInit
def _forms(pa):
    """Three forms of other sites: a bowl like the synthetic one, a plate, a jar."""
    rows = [("Altro sito", "Ciotola", 16.2, 17.1, None, None), ("Altro sito", "Piatto", 30.0, 30.0, 12.0, 4.0),
            ("Terzo sito", "Giara", 11.0, 34.0, 14.0, 40.0)]
    with pa.db.begin() as conn:
        for i, (sito, shape, rim, dmax, base, height) in enumerate(rows, 1):
            pa.db.insert(conn, "pottery_table", {"sito": sito, "id_number": i, "specific_shape": shape,
                                                 "diametro_rim": rim, "diametro_max": dmax, "diametro_bottom": base,
                                                 "diametro_height": height}, id_column="id_rep")


def test_save_a_sherd_in_a_new_form(pa_db, tmp_path):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    pa = PyArchInit.connect(media_root=tmp_path / "media")
    prof, meas = pottery.measure(make_vessel(sherd=(0, 120), noise=0.0001, sectors=360), pottery.Axis((0, 0, 0), (0, 0, 1)))
    out = pa.save_pottery("Saggio", meas, area="1", us="2", profile=prof, form="Aperta", specific_form="Orlo")
    rec = pa.db.query("pottery_table", id_rep=out["id_rep"])[0]
    assert float(rec["diametro_rim"]) == pytest.approx(TOP_RIM * 100, abs=0.15)
    assert float(rec["diametro_preserved"]) == pytest.approx(12.0, abs=0.2) and rec["diametro_height"] is None
    assert rec["percent"] == "25-50" and rec["form"] == "Aperta" and rec["specific_form"] == "Orlo"
    assert rec["us"] == "2" and rec["id_number"] == 1 and rec["entity_uuid"]
    assert rec["drawing"] == f"ceramica_{out['id_rep']}.svg"
    media = pa._with_previews(pa.media_for("CERAMICA", out["id_rep"]))
    assert len(media) == 1 and media[0]["path"].suffix == ".png" and media[0]["thumb"].exists()
    for key in ("image", "drawing", "dxf", "profile"):
        assert (tmp_path / "media" / "ceramica" / "Saggio") in __import__("pathlib").Path(out[key]).parents
    side = json.loads(open(out["profile"], encoding="utf-8").read())
    assert side["signature"] == meas["signature"] and side["measures"]["rim_reliable"]
    assert pa.pottery_profiles()[out["id_rep"]]["id_rep"] == out["id_rep"]
    # the same form again: the drawing takes the next name, the form is not duplicated
    again = pa.save_pottery("Saggio", meas, id_rep=out["id_rep"], profile=prof)
    assert again["id_rep"] == out["id_rep"] and again["image"].endswith(f"ceramica_{out['id_rep']}_2.png")
    assert len(pa.db.query("pottery_table", sito="Saggio")) == 1


def test_existing_values_are_kept_unless_asked(pa_db, tmp_path):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    pa = PyArchInit.connect(media_root=tmp_path / "media")
    with pa.db.begin() as conn:
        rid = pa.db.insert(conn, "pottery_table", {"sito": "Saggio", "id_number": 7, "diametro_rim": 15.0,
                                                   "form": "Chiusa"}, id_column="id_rep")
    meas = {"rim_diameter_m": 0.161, "max_diameter_m": 0.17, "height_m": 0.05, "rim_preserved_pct": 12.0,
            "full_profile": False}
    out = pa.save_pottery("Saggio", meas, id_rep=rid, ware="Fine")
    rec = pa.db.query("pottery_table", id_rep=rid)[0]
    assert float(rec["diametro_rim"]) == 15.0 and set(out["kept"]) == {"diametro_rim"}
    assert float(rec["diametro_max"]) == 17.0 and rec["percent"] == "5-25" and rec["ware"] == "Fine"
    assert rec["form"] == "Chiusa"  # a field not given stays as it was
    pa.save_pottery("Saggio", meas, id_rep=rid, overwrite=True)
    assert float(pa.db.query("pottery_table", id_rep=rid)[0]["diametro_rim"]) == 16.1
    with pytest.raises(KeyError):
        pa.save_pottery("Saggio", meas, id_rep=999)


def test_an_uncertain_rim_is_not_written(pa_db, tmp_path):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    pa = PyArchInit.connect(media_root=tmp_path / "media")
    meas = {"rim_diameter_m": 0.14, "rim_preserved_pct": 4.0, "rim_reliable": False, "max_diameter_m": 0.15,
            "height_m": 0.03, "full_profile": False}
    out = pa.save_pottery("Saggio", meas)
    rec = pa.db.query("pottery_table", id_rep=out["id_rep"])[0]
    assert out["uncertain"] == ["diametro_rim", "percent"] and rec["diametro_rim"] is None and not rec["percent"]
    assert float(rec["diametro_max"]) == 15.0


def test_a_failed_drawing_leaves_no_form(pa_db, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    pa = PyArchInit.connect(media_root=tmp_path / "media")
    prof, meas = pottery.measure(make_vessel(), pottery.Axis((0, 0, 0), (0, 0, 1)))

    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pottery, "draw", broken)
    with pytest.raises(OSError):
        pa.save_pottery("Saggio", meas, profile=prof)
    assert pa.db.query("pottery_table", sito="Saggio") == []


def test_search_ranks_by_size_and_shape(pa_db, tmp_path):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    pa = PyArchInit.connect(media_root=tmp_path / "media")
    _forms(pa)
    prof, meas = pottery.measure(make_vessel(sherd=(0, 120), noise=0.0001, sectors=360), pottery.Axis((0, 0, 0), (0, 0, 1)))
    rows = pa.search_pottery(rim_cm=meas["rim_diameter_m"] * 100, max_cm=meas["max_diameter_m"] * 100)
    assert [r["specific_shape"] for r in rows] == ["Ciotola", "Giara", "Piatto"]
    assert rows[0]["similarity_pct"] > 95 and rows[0]["compared"] == ["diametro_rim", "diametro_max"]
    assert [r["sito"] for r in pa.search_pottery(rim_cm=16.0, sito="Terzo sito")] == ["Terzo sito"]
    # a bowl saved with its profile is found first by its shape, even when a form of the same
    # size without a profile exists; a narrow cup saved with its profile ranks after it
    whole = make_vessel()
    p_whole, m_whole = pottery.measure(whole, pottery.Axis((0, 0, 0), (0, 0, 1)))
    saved = pa.save_pottery("Saggio", m_whole, profile=p_whole, specific_shape="Ciotola")["id_rep"]
    cup = make_vessel(rim_r=0.081, height=0.05)
    p_cup, m_cup = pottery.measure(cup, pottery.Axis((0, 0, 0), (0, 0, 1)))
    pa.save_pottery("Saggio", m_cup, profile=p_cup, specific_shape="Coppa")
    rows = pa.search_pottery(rim_cm=meas["rim_diameter_m"] * 100, max_cm=meas["max_diameter_m"] * 100,
                             signature=meas["signature"])
    assert rows[0]["id_rep"] == saved and "profilo" in rows[0]["compared"] and rows[0]["profile_distance"] < 0.02
    shapes = [r["specific_shape"] for r in rows]
    assert shapes.index("Coppa") > shapes.index("Ciotola", 1)
    assert pa.search_pottery(rim_cm=16.0, exclude=saved, signature=meas["signature"])[0]["id_rep"] != saved
    assert pa.search_pottery() == []  # nothing to compare


def test_thesaurus_of_the_pottery_form(pa_db):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    pa = PyArchInit.connect()
    with pa.db.begin() as conn:
        for table, sigla, value, lang in (("pottery_table", "OPN", "Aperta", "IT"), ("pottery_table", "OPN", "Open", "EN"),
                                          ("Pottery", "CLS", "Chiusa", "IT"), ("us_table", "X", "Strato", "IT")):
            pa.db.insert(conn, "pyarchinit_thesaurus_sigle", {"nome_tabella": table, "sigla": sigla,
                                                              "sigla_estesa": value, "tipologia_sigla": "11.4",
                                                              "lingua": lang}, id_column="id_thesaurus_sigle")
    assert pa.thesaurus("11.4") == [("OPN", "Aperta"), ("CLS", "Chiusa")]
    assert pa.thesaurus("11.4", lingua="EN") == [("OPN", "Open")]
    assert pa.thesaurus("11.99") == []


def test_cli_measures_and_searches(pa_db, tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    mesh = make_vessel(sherd=(0, 120), noise=0.0001, sectors=360)
    mesh.apply_scale(1000.0)  # a scan in millimetres
    mesh.export(tmp_path / "frammento.ply")
    runner = CliRunner()
    res = runner.invoke(app, ["pottery", str(tmp_path / "frammento.ply"), "--out", str(tmp_path / "f.png"),
                              "--units", "mm", "--axis", "up"])
    assert res.exit_code == 0, res.output
    meas = json.loads(res.output)
    assert abs(meas["rim_diameter_m"] - TOP_RIM) < 0.0015 and (tmp_path / "f.png").exists()
    _forms(PyArchInit.connect())
    res = runner.invoke(app, ["pyarchinit", "pottery-search", str(tmp_path / "frammento.ply"), "--units", "mm",
                              "--axis", "up", "--limit", "2"])
    assert res.exit_code == 0, res.output
    lines = res.output.strip().splitlines()
    assert len(lines) == 2 and "Ciotola" in lines[0]
    res = runner.invoke(app, ["pyarchinit", "pottery-save", str(tmp_path / "frammento.ply"), "Saggio", "--us", "3",
                              "--units", "mm", "--axis", "up", "--part", "Orlo"])
    assert res.exit_code == 0, res.output
    rid = json.loads(res.output)["id_rep"]
    rec = PyArchInit.connect().db.query("pottery_table", id_rep=rid)[0]
    assert rec["specific_form"] == "Orlo" and rec["us"] == "3" and rec["percent"] == "25-50"


def test_pottery_window(pa_db, tmp_path, monkeypatch):
    """Strumenti > Profilo ceramico: the drawing and the measurements, the similar forms of
    PyArchInit and the save in a new form (no dialog: the answers are given)."""
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.core.document import Document
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    make_vessel(sherd=(0, 120), noise=0.0001, sectors=360).export(tmp_path / "frammento.ply")
    doc = Document()
    doc.meta["sito"] = "Saggio"
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    answers = iter([{"mesh": str(tmp_path / "frammento.ply"), "out": str(tmp_path / "profilo.svg"), "scale": 2.0,
                     "units": "m", "axis": "auto", "rim": True, "title": "Frammento"},
                    {"id_rep": None, "area": "1", "us": "2", "form": "Aperta", "specific_form": "Orlo",
                     "specific_shape": "Ciotola", "ware": "", "overwrite": False}])
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: next(answers)))
    told = []
    monkeypatch.setattr(w, "_info", lambda *a, **k: told.append(a))
    w.pottery_profile()
    w.runner.wait()
    app.processEvents()
    d = w._pottery_win
    assert d.isVisible() and not d.b_search.isEnabled()  # not connected yet
    assert (tmp_path / "profilo.svg").exists() and (tmp_path / "profilo.png").exists()
    assert "34%" in d.info.text() and "(25-50)" in d.info.text()
    pa = PyArchInit.connect(media_root=tmp_path / "media")
    _forms(pa)
    w.pa = pa
    d.set_connected(True)
    w.search_similar_pottery()
    w.runner.wait()
    app.processEvents()
    assert d.table.rowCount() == 3 and d.table.item(0, 7).text() == "Ciotola"
    w.save_pottery(None)
    w.runner.wait()
    app.processEvents()
    rid = w._pottery["id_rep"]
    rec = pa.db.query("pottery_table", id_rep=rid)[0]
    assert rec["sito"] == "Saggio" and rec["us"] == "2" and rec["specific_shape"] == "Ciotola"
    assert rec["percent"] == "25-50" and told and str(rid) in told[-1][1]
    w.search_similar_pottery()  # the form just saved is not compared with itself
    w.runner.wait()
    app.processEvents()
    assert rid not in [m["id_rep"] for m in d.matches]
    w.setWindowModified(False)
    d.close()
    w.close()
