"""The comparison sheet of a wall (:mod:`pyarchinit_photogrammetry.engine.elevation_sheet`): the campaigns of its
group stacked on the same ``u`` axis, the oldest on top, one scale and one grid for all; height ticks every 0.5 m,
distance ticks under the last band, the corners of the trace marked through every band; PNG or JPEG and a PDF
beside it; the members without files left out and said; ``ppg elevation --like ID --compare OUT``."""
from __future__ import annotations

import numpy as np
import pytest
import rasterio
from PIL import Image

from pyarchinit_photogrammetry.archaeo.sections import grid_step
from pyarchinit_photogrammetry.engine import elevation as E
from pyarchinit_photogrammetry.engine import elevation_groups as G
from pyarchinit_photogrammetry.engine import elevation_sheet as S

from wall_helpers import wall_campaigns


def _make_group(folder, dataset):
    doc, (a, b), info, entry = wall_campaigns(folder, dataset)
    (_ch, new), = G.build_on_chunks(doc, a, entry, [b])["built"]
    (group,) = G.groups(a)
    return doc, (a, b), info, entry, new, group


@pytest.fixture(scope="module")
def built(tmp_path_factory, wall_dataset):
    """One group of two campaigns (2017 on chunk a, 2022 on chunk b) and its sheet as PNG."""
    folder = tmp_path_factory.mktemp("sheet")
    doc, chunks, info, entry, new, group = _make_group(folder / "doc", wall_dataset)
    r = S.comparison_sheet(doc, chunks[0], group, folder / "confronto.png")
    return doc, chunks, info, entry, new, group, r


def _ortho_rgb(chunk, entry) -> np.ndarray:
    """The orthophoto of an elevation as the sheet pastes it at factor 1: RGB, white where it has no alpha."""
    with rasterio.open(chunk.data_dir / entry["orthophoto"]) as src:
        arr = src.read()
    rgb = np.moveaxis(arr[:3], 0, -1)
    return np.where(arr[3][..., None] > 0, rgb, 255).astype(np.uint8)


def test_bands_share_one_scale_and_grid(built):
    _doc, (a, b), _info, entry, _new, _group, r = built
    first, second = r["bands"]
    assert [x["label"] for x in r["bands"]] == [a.label, b.label]
    assert [x["date"] for x in r["bands"]] == ["2017-11-05", "2022-11-03"]
    assert first["top"] < second["top"]                                         # the oldest on top
    assert first["left"] == second["left"] and first["width"] == second["width"]
    assert first["height"] == second["height"] and r["skipped"] == []
    assert r["factor"] == 1
    assert r["px_per_m"] == pytest.approx(1 / (0.02 * r["factor"]))
    image = np.asarray(Image.open(r["image"]).convert("RGB"))
    assert image.shape[1] >= first["left"] + first["width"]
    x = int(first["left"] + 3.0 * r["px_per_m"])                                # the mark at 3.0 m along the face
    v1 = entry["bounds"][3]
    for band in r["bands"]:
        y = int(band["top"] + (v1 - 100.6) * r["px_per_m"])                     # its height 0.6 m (100.6)
        assert image[y, x].max() < 60
        assert image[y, x - 10].max() >= 60 and image[y, x + 10].max() >= 60


def test_ticks_every_half_metre_and_corners_marked(built):
    _doc, (a, b), _info, entry, new, _group, r = built
    u0, v0, u1, v1 = entry["bounds"]
    first = r["bands"][0]
    values = [v for v, _y in r["v_ticks"]]
    assert values == pytest.approx([100.0, 100.5, 101.0, 101.5, 102.0])
    for value, y in r["v_ticks"]:
        assert abs(value / S.HEIGHT_TICK - round(value / S.HEIGHT_TICK)) < 1e-9
        assert y == pytest.approx(first["top"] + (v1 - value) * r["px_per_m"], abs=0.5)
    step = grid_step(u1 - u0, 20)
    us = [u for u, _x in r["u_ticks"]]
    assert us[0] == pytest.approx(0.0, abs=1e-9) and np.allclose(np.diff(us), step)
    assert us[-1] <= u1 + 0.01 and us[-1] + step > u1                         # half a cell of slack
    for u, x in r["u_ticks"]:
        assert x == pytest.approx(first["left"] + (u - u0) * r["px_per_m"], abs=1.0)
    (corner,) = r["corners"]
    assert corner[0] == pytest.approx(6.0, abs=0.01)
    assert corner[1] == pytest.approx(first["left"] + (6.0 - u0) * r["px_per_m"], abs=1.0)
    image = np.asarray(Image.open(r["image"]).convert("RGB")).astype(int)
    x = int(round(corner[1]))
    for band, (ch, e) in zip(r["bands"], [(a, entry), (b, new)]):
        own = _ortho_rgb(ch, e).astype(int)
        top, left = band["top"], band["left"]
        rows = slice(top, top + band["height"])
        col = x - left
        sheet_col = image[rows, x]
        assert (np.abs(sheet_col - own[:, col]).max(axis=1) > 30).sum() >= band["height"] // 5   # dashes on it
        assert (np.abs(sheet_col - own[:, col]).max(axis=1) <= 30).sum() >= band["height"] // 5  # and gaps
        assert np.array_equal(image[rows, x + 6], own[:, col + 6])              # pasted as read beside it


def test_png_jpeg_and_pdf(built, tmp_path):
    doc, (a, _b), _info, _entry, _new, group, r = built
    assert open(r["image"], "rb").read(8) == b"\x89PNG\r\n\x1a\n"
    assert r["pdf"].endswith(".pdf") and open(r["pdf"], "rb").read(4) == b"%PDF"
    j = S.comparison_sheet(doc, a, group, tmp_path / "confronto.jpg", title="Muro nord")
    assert open(j["image"], "rb").read(3) == b"\xff\xd8\xff"
    assert j["pdf"] == str(tmp_path / "confronto.pdf") and open(j["pdf"], "rb").read(4) == b"%PDF"


def test_max_width_reduces_by_one_integer_factor(built, tmp_path):
    doc, (a, _b), _info, entry, _new, group, _r = built
    w = entry["size"][0]
    r = S.comparison_sheet(doc, a, group, tmp_path / "small.png", max_width=w // 3)
    assert r["factor"] == int(np.ceil(w / (w // 3))) and r["factor"] >= 3
    assert r["px_per_m"] == pytest.approx(1 / (0.02 * r["factor"]))
    assert all(band["width"] <= w // 3 + 1 for band in r["bands"])


def test_a_member_without_files_is_skipped_and_said(tmp_path, wall_dataset):
    doc, (a, b), _info, entry, new, group = _make_group(tmp_path / "doc", wall_dataset)
    assert E.remove_elevation(b, new["id"])
    r = S.comparison_sheet(doc, a, group, tmp_path / "one.png")
    assert [x["label"] for x in r["bands"]] == [a.label] and r["skipped"] == [b.label]
    assert r["codes"] == {b.label: "gone"}
    assert E.remove_elevation(a, entry["id"])
    with pytest.raises(ValueError):
        S.comparison_sheet(doc, a, group, tmp_path / "none.png")


def test_a_group_of_planes_has_its_sheet_too(tmp_path, wall_dataset):
    doc, (a, b), info, _entry = wall_campaigns(tmp_path / "doc", wall_dataset)
    (x0, y0), (x1, y1) = info["face"][:2]
    plane = E.plane_from_line(a, (x0, y0), (x1, y1), side=((x0 + x1) / 2, y0 - 2.0))
    entry = E.build_elevation(a, plane, 0.05, "Piano", (-0.5, 0.5), max_angle=89.0)
    out = G.build_on_chunks(doc, a, entry, [b])
    assert len(out["built"]) == 1
    group = G.group_of(doc, a, entry["id"])[1]
    r = S.comparison_sheet(doc, a, group, tmp_path / "piano.png")
    assert len(r["bands"]) == 2 and r["corners"] == []
    assert r["v_ticks"] and all(abs(v / 0.5 - round(v / 0.5)) < 1e-9 for v, _y in r["v_ticks"])
    assert open(r["pdf"], "rb").read(4) == b"%PDF"


def test_command_line_compare(tmp_path, wall_dataset):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.core.document import Document

    doc, (a, b), _info, entry, _new, _group = _make_group(tmp_path / "doc", wall_dataset)
    doc.save()
    project = str(doc.path)
    before = [len(E.elevations(c)) for c in Document.open(project).chunks]
    out = tmp_path / "out.png"
    runner = CliRunner()
    r = runner.invoke(app, ["elevation", project, "--like", entry["id"], "--compare", str(out),
                            "--title", "Muro nord"])
    assert r.exit_code == 0, r.output
    assert out.exists() and (tmp_path / "out.pdf").exists()
    assert str(out) in r.output and str(tmp_path / "out.pdf") in r.output
    assert [len(E.elevations(c)) for c in Document.open(project).chunks] == before
    r = runner.invoke(app, ["elevation", project, "--like", entry["id"], "--compare", str(out)])
    assert r.exit_code == 1 and "out.png" in r.output and "--overwrite" in r.output    # kept: both exist
    r = runner.invoke(app, ["elevation", project, "--like", entry["id"], "--compare", str(out), "--overwrite"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["elevation", project, "--like", entry["id"], "--overwrite"])
    assert r.exit_code == 2 and "--overwrite" in r.output
    r = runner.invoke(app, ["elevation", project, "--like", entry["id"], "--compare", str(out), "--chunks", "X"])
    assert r.exit_code == 2 and "--compare" in r.output
    r = runner.invoke(app, ["elevation", project, "--like", entry["id"], "--title", "T"])
    assert r.exit_code == 2 and "--title" in r.output
    r = runner.invoke(app, ["elevation", project, "--trace", "1,2,3,4", "--compare", str(out)])
    assert r.exit_code == 2 and "--compare" in r.output
    alone = E.build_elevation(a, E.frame_from_dict(entry["frame"]), 0.05, "Solo", (-0.5, 0.5), max_angle=89.0,
                              heights=(100.0, 102.0))
    doc.save()
    r = runner.invoke(app, ["elevation", project, "--like", alone["id"], "--compare", str(tmp_path / "x.png")])
    assert r.exit_code == 1 and "group" in r.output


def test_marks_stay_in_one_column_at_a_reduced_scale(built, tmp_path):
    doc, (a, _b), _info, entry, _new, group, _r = built
    w = entry["size"][0]
    r = S.comparison_sheet(doc, a, group, tmp_path / "half.png", max_width=int(np.ceil(w / 2)))
    assert r["factor"] == 2
    image = np.asarray(Image.open(r["image"]).convert("RGB")).astype(int)
    v1 = entry["bounds"][3]
    expected = r["bands"][0]["left"] + (3.0 - entry["bounds"][0]) * r["px_per_m"]
    columns = []
    for band in r["bands"]:
        y = int(band["top"] + (v1 - 100.6) * r["px_per_m"])
        x0 = int(expected) - 6
        strip = image[y - 1:y + 2, x0:x0 + 13].max(axis=2).min(axis=0)       # darkest of three rows, by column
        columns.append(x0 + int(np.argmin(strip)))
        assert strip.min() < 60
    assert columns[0] == columns[1] and abs(columns[0] - expected) <= 1.0


def test_hole_edges_keep_their_colour_at_a_reduced_scale(tmp_path, wall_dataset):
    doc, (a, b), _info, entry, new, group = _make_group(tmp_path / "doc", wall_dataset)
    path = b.data_dir / new["orthophoto"]
    with rasterio.open(path, "r+") as dst:                                       # a hole from column 101 on
        arr = dst.read()
        arr[:, :, 101:140] = 0
        dst.write(arr)
    r = S.comparison_sheet(doc, a, group, tmp_path / "hole.png", max_width=int(np.ceil(entry["size"][0] / 3)))
    assert r["factor"] == 3
    image = np.asarray(Image.open(r["image"]).convert("RGB")).astype(float)
    band = r["bands"][1]
    cell = image[band["top"] + 10, band["left"] + 33]          # reduced cell (10, 33): source rows 30-32, columns
    src = arr[:3, 30:33, 99:102].astype(float)                   # 99-101, of which 99 and 100 are kept
    alpha = arr[3, 30:33, 99:102].astype(float)
    weighted = (src * alpha).sum(axis=(1, 2)) / alpha.sum()
    assert np.allclose(cell, weighted, atol=1.5)                                 # no darkening by the empty cells
    assert image[band["top"] + 10, band["left"] + 40].min() == 255               # all hole: paper


def test_a_member_on_another_grid_is_skipped_with_its_reason(tmp_path, wall_dataset):
    doc, (a, b), _info, entry, new, group = _make_group(tmp_path / "doc", wall_dataset)
    other = E.build_elevation(b, E.frame_from_dict(entry["frame"]), 0.03, "Altra griglia", (-0.5, 0.5),
                              max_angle=89.0, heights=(100.0, 102.0))
    record = next(m for m in group["members"] if m["chunk"] == b.key)
    record["entry"] = other["id"]
    r = S.comparison_sheet(doc, a, group, tmp_path / "grid.png")
    assert [x["label"] for x in r["bands"]] == [a.label] and r["skipped"] == [b.label]
    assert "0.03" in r["reasons"][b.label] and "0.02" in r["reasons"][b.label]
    assert r["codes"] == {b.label: "grid"}
    record["entry"] = new["id"]                                                  # half a cell off: refused too
    with rasterio.open(b.data_dir / new["orthophoto"], "r+") as dst:
        t = dst.transform
        dst.transform = rasterio.Affine(t.a, t.b, t.c + 0.01, t.d, t.e, t.f)
    r = S.comparison_sheet(doc, a, group, tmp_path / "shift.png")
    assert r["skipped"] == [b.label] and "pixel" in r["reasons"][b.label] and r["codes"] == {b.label: "shift"}


def test_an_existing_sheet_is_kept_unless_overwrite(built, tmp_path):
    doc, (a, _b), _info, _entry, _new, group, _r = built
    (tmp_path / "s.pdf").write_bytes(b"mine")
    with pytest.raises(FileExistsError, match="s.pdf"):
        S.comparison_sheet(doc, a, group, tmp_path / "s.png", overwrite=False)
    assert not (tmp_path / "s.png").exists() and (tmp_path / "s.pdf").read_bytes() == b"mine"
    S.comparison_sheet(doc, a, group, tmp_path / "s.png", overwrite=True)
    assert (tmp_path / "s.pdf").read_bytes()[:4] == b"%PDF"
