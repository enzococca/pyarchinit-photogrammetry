"""The models of several chunks merged into one textured model (engine/model_merge, ppg merge-models): synthetic
patches in a local frame - two overlapping ones of different brightness, a third one apart with an open gap."""
from __future__ import annotations

import cv2
import numpy as np
import pytest
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.engine import model_merge
from pyarchinit_photogrammetry.engine.texturing import texture_pages
from pyarchinit_photogrammetry.io.exports import components
from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

from engine_gaps_helpers import checker, grid_mesh, new_doc, textured_chunk


def _three(tmp_path, dark=0.6):
    """A: x 0-2 m, B: x 1-3 m (overlapping A from 1 to 2, the same checkerboard ``dark`` times as bright), C: x 3.5-4.5
    (a gap of 0.5 m from B); each in its own internal frame (another shift) but the same world."""
    doc = new_doc(tmp_path)
    colour = np.array([200, 150, 100])
    xyz, f, uv = grid_mesh(0.0, 0.0, 2.0, 1.0, 40, 20)
    page_a = checker(512, 256, colour, cells=16)
    a = textured_chunk(doc, "A", xyz, f, uv, [page_a], shift=(100.0, 200.0, 10.0))
    xyz, f, uv = grid_mesh(1.0, 0.0, 3.0, 1.0, 40, 20)
    page_b = checker(512, 256, (colour * dark).astype(int), cells=16)  # same pattern as A from x = 1 (shifted)
    # B's texture must agree with A where they overlap: the checker of A over [0, 2] at 16 cells is 8 cells per metre;
    # B over [1, 3] at 16 cells is also 8 cells per metre and starts on a cell edge
    b = textured_chunk(doc, "B", xyz, f, uv, [page_b], shift=(-50.0, 0.0, 0.0))
    xyz, f, uv = grid_mesh(3.5, 0.0, 4.5, 1.0, 20, 20)
    c = textured_chunk(doc, "C", xyz, f, uv, [checker(256, 256, (90, 160, 90)), checker(128, 128, (20, 20, 200))],
                       texnumber=np.r_[np.zeros(400, np.int32), np.ones(400, np.int32)], shift=(0.0, 0.0, 0.0))
    doc.save()
    return doc, a, b, c


def _world_mesh(ch):
    m = read_mesh(ch.product_path("model"))
    return m, ch.to_world(m.xyz)


def test_merge_keeps_every_face_the_frame_the_pages_and_the_groups(tmp_path):
    doc, a, b, c = _three(tmp_path)
    merged, r = model_merge.merge_models(doc, [a, b, c], label="Unito")
    assert merged.label == "Unito" and not merged.cameras
    assert np.allclose(merged.transform, a.transform) and merged.crs == a.crs
    m, world = _world_mesh(merged)
    assert len(m.faces) == sum(len(read_mesh(ch.product_path("model")).faces) for ch in (a, b, c))
    assert r["pages"] == 4 and len(texture_pages(merged)) == 4  # A 1 page, B 1, C 2: kept as they are
    assert m.group_names == {0: "A", 1: "B", 2: "C"}
    for g, ch in enumerate((a, b, c)):  # every group lies where its chunk's model is
        _, w = _world_mesh(ch)
        mine = world[np.unique(m.faces[m.groups == g])]
        assert mine[:, 0].min() == pytest.approx(w[:, 0].min(), abs=1e-4)
        assert mine[:, 0].max() == pytest.approx(w[:, 0].max(), abs=1e-4)
    # the gap between B and C stays open: no face spans it
    span = world[m.faces][:, :, 0]
    assert not ((span.min(axis=1) < 3.0 + 1e-6) & (span.max(axis=1) > 3.5 - 1e-6)).any()
    assert len(np.unique(components(m.faces))) == 3  # A and B touch nothing: three pieces (not joined)
    # the texture of C's second page is the blue one, on the faces that had it
    pages = texture_pages(merged)
    blue = cv2.imread(str(pages[m.texnumber[m.groups == 2][-1]]))
    assert blue[..., 0].mean() > blue[..., 2].mean()
    assert merged.meta["merge_models"]["faces"] == len(m.faces)
    doc.save()
    again = ppg.Document.open(doc.path)
    assert again.chunks[-1].meta["model"]["source"] == "merge"


def test_repacked_pages_within_the_cap_sample_the_same_colours(tmp_path):
    doc, a, b, c = _three(tmp_path)
    kept, _ = model_merge.merge_models(doc, [a, b, c], label="keep")
    packed, r = model_merge.merge_models(doc, [a, b, c], label="repack", pages="repack", page_size=600)
    assert r["pages"] >= 2
    for page in texture_pages(packed):
        h, w = cv2.imread(str(page)).shape[:2]
        assert max(h, w) <= 600

    def colours(ch):
        m = read_mesh(ch.product_path("model"))
        pages = [cv2.imread(str(p)) for p in texture_pages(ch)]
        uv = m.texcoords.reshape(-1, 3, 2).mean(axis=1)  # the centre of every face
        num = m.texnumber if m.texnumber is not None else np.zeros(len(m.faces), int)
        out = np.zeros((len(uv), 3))
        for k, img in enumerate(pages):
            sel = num == k
            h, w = img.shape[:2]
            x = np.clip((uv[sel, 0] * w).astype(int), 0, w - 1)
            y = np.clip(((1 - uv[sel, 1]) * h).astype(int), 0, h - 1)
            out[sel] = img[y, x]
        return out

    ck, cp = colours(kept), colours(packed)
    assert np.median(np.abs(ck - cp)) < 6  # the same texels (JPEG and the reduction of a page aside)


def test_colours_matched_on_the_overlap(tmp_path):
    doc, a, b, c = _three(tmp_path, dark=0.6)
    for method in ("gain", "lab"):
        merged, r = model_merge.merge_models(doc, [a, b], label=method, colour=method)
        fit = r["colours"][0]
        assert fit["chunk"] == "B" and fit["pairs"] > 200
        assert fit["fit"]["after"] < 0.4 * fit["fit"]["before"]
        m = read_mesh(merged.product_path("model"))
        pages = texture_pages(merged)
        mean = [cv2.imread(str(pages[k])).reshape(-1, 3).mean(0) for k in range(2)]
        assert np.abs(mean[1] - mean[0]).max() < 12  # B's page now as bright as A's
    _merged, r = model_merge.merge_models(doc, [a, c], label="apart", colour="gain")
    assert r["colours"][0]["fit"] is None and any("no overlap" in w for w in r["warnings"])


def test_overlapped_faces_removed_by_priority(tmp_path):
    doc, a, b, c = _three(tmp_path)
    merged, r = model_merge.merge_models(doc, [a, b], label="prior", remove_overlaps=True, priority=["B", "A"],
                                         overlap_distance=0.01)
    m, world = _world_mesh(merged)
    a_faces = world[m.faces[m.groups == 0]].mean(axis=1)
    assert r["removed"]["A"] == 800 and (a_faces[:, 0] < 1.0 + 1e-6).all()  # A keeps only x 0-1
    assert (m.groups == 1).sum() == 1600  # B, first, keeps everything


def test_merged_model_gives_a_dem_and_exports_groups(tmp_path):
    doc, a, b, c = _three(tmp_path)
    merged, _ = model_merge.merge_models(doc, [a, b, c])
    dem = merged.build_dem(resolution=0.05, source="model")
    assert dem.exists()
    out = tmp_path / "out" / "unito.obj"
    merged.export_model(out, groups="source")
    text = out.read_text()
    assert [ln for ln in text.splitlines() if ln.startswith("g ")] == ["g A", "g B", "g C"]


def test_cli_merge_models(tmp_path):
    doc, a, b, c = _three(tmp_path)
    res = CliRunner().invoke(app, ["merge-models", str(doc.path), "A", "B", "C", "--label", "Tutti", "--colour",
                                   "gain", "--pages", "repack", "--page-size", "1024"])
    assert res.exit_code == 0, res.output
    assert "faces" in res.output
    doc = ppg.Document.open(doc.path)
    assert doc.chunks[-1].label == "Tutti" and doc.chunks[-1].meta["merge_models"]["page_layout"] == "repack"


def test_errors(tmp_path):
    doc, a, b, c = _three(tmp_path)
    with pytest.raises(ValueError, match="at least two"):
        model_merge.merge_models(doc, [a])
    empty = doc.add_chunk("vuoto")
    with pytest.raises(ValueError, match="has no model"):
        model_merge.merge_models(doc, [a, empty])
    assert [ch.label for ch in doc.chunks] == ["A", "B", "C", "vuoto"]  # nothing left behind
