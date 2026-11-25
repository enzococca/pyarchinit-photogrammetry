"""An elevation made from rasters (:func:`pyarchinit_photogrammetry.engine.elevation.entry_from_rasters`) and the
composite of the campaigns of a wall (:mod:`pyarchinit_photogrammetry.engine.elevation_composite`): per cell the
surface of the most recent campaign, replaced by an older one deeper by more than the threshold or where the cell
holds nothing; the colours matched where the campaigns join; stale when a source changes; made again in place; no
patches on it; ``ppg elevation --like ID --composite``."""
from __future__ import annotations

import json
import threading

import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.engine import elevation as E
from pyarchinit_photogrammetry.engine import elevation_composite as C
from pyarchinit_photogrammetry.engine import elevation_groups as G
from pyarchinit_photogrammetry.engine import patches as P

from wall_helpers import wall_campaigns

COLOUR_A, COLOUR_B = (200, 100, 50), (100, 150, 200)


@pytest.fixture(scope="module")
def walls(tmp_path_factory, wall_dataset):
    """Two chunks of the wall and the elevation of its face on the first (its grid is that of every campaign)."""
    doc, chunks, _info, entry = wall_campaigns(tmp_path_factory.mktemp("composite") / "doc", wall_dataset)
    return doc, chunks, entry


def _boxes(h: int, w: int) -> dict:
    r = h // 5
    c = w // 8
    boxes = {"R1": (slice(r, 2 * r), slice(c, 2 * c)), "R2": (slice(r, 2 * r), slice(3 * c, 4 * c)),
             "R3": (slice(r, 2 * r), slice(5 * c, 6 * c)), "R4": (slice(3 * r, 4 * r), slice(c, 2 * c))}
    rest = np.ones((h, w), bool)
    for rows, cols in boxes.values():
        rest[rows, cols] = False
    boxes["rest"] = rest
    return boxes


def _rasters(shape, colour, holes=None, noise=0.0, seed=0):
    h, w = shape
    relief = np.zeros((h, w), np.float32)
    rgba = np.empty((4, h, w), np.uint8)
    rgb = np.broadcast_to(np.asarray(colour, np.float64)[:, None, None], (3, h, w))
    if noise:
        rgb = rgb + np.random.default_rng(seed).normal(0.0, noise, (3, h, w))
    rgba[:3] = np.clip(np.round(rgb), 0, 255).astype(np.uint8)
    rgba[3] = 255
    if holes is not None:
        relief[holes] = np.nan
        rgba[3][holes] = 0
    return rgba, relief


def _make(doc, ref, other, entry, noise=0.0):
    w, h = entry["size"]
    boxes = _boxes(h, w)
    rgba_a, relief_a = _rasters((h, w), COLOUR_A, noise=noise, seed=1)
    relief_a[boxes["R1"]] = -0.30
    relief_a[boxes["R3"]] = -0.10
    rgba_b, relief_b = _rasters((h, w), COLOUR_B, holes=boxes["R4"], noise=noise, seed=2)
    relief_b[boxes["R2"]] = -0.10
    a = E.entry_from_rasters(ref, entry, rgba_a, relief_a, name="Campagna 2017", kind="elevation")
    b = E.entry_from_rasters(other, entry, rgba_b, relief_b, name="Campagna 2022", kind="elevation")
    group = G.new_group(ref, a)
    G.set_date(group, ref.key, a["id"], "2017-11-05")
    G.add_member(group, other, b, "2022-11-03")
    return group, (a, b), boxes


@pytest.fixture
def campaigns(walls):
    """``(doc, (ref, other), group, (a, b), boxes)``: the campaign of 2017 (``a``, in the reference chunk) and that
    of 2022 (``b``, in the other), made from arrays on the grid of the wall's elevation and grouped; cleaned after."""
    doc, (ref, other), entry = walls
    group, made, boxes = _make(doc, ref, other, entry)
    yield doc, (ref, other), group, made, boxes
    _clean(doc, entry)


def _clean(doc, entry):
    import shutil

    for ch in doc.chunks:
        for e in list(E.elevations(ch)):
            if e["id"] != entry["id"]:
                E.remove_elevation(ch, e["id"])
        ch.meta.pop(G.KEY, None)
        ch.meta.pop("drawings", None)
        root = ch.data_dir / "elevations"
        for folder in root.iterdir() if root.exists() else ():
            if folder.name != entry["id"]:                                 # what the command line wrote on disk
                shutil.rmtree(folder)


def _read(ch, entry, key, band=None):
    with rasterio.open(ch.data_dir / entry[key]) as src:
        return src.read(band) if band is not None else src.read()


def sources_of(ch, entry):
    return _read(ch, entry, "sources_raster", 1)


def relief_of(ch, entry):
    return _read(ch, entry, "relief", 1)


def ortho_of(ch, entry):
    return _read(ch, entry, "orthophoto")


def test_the_wall_as_it_was_when_most_exposed(campaigns):
    doc, (ref, other), group, (a, b), boxes = campaigns          # a: the 2017 entry of ref; b: the 2022 one of other
    entry = C.composite(doc, ref, group, match=False)
    assert entry["kind"] == "composite" and entry["threshold"] == 0.15 == C.DEEPER
    assert entry["sources"] == [{"chunk": ref.key, "entry": a["id"]}, {"chunk": other.key, "entry": b["id"]}]
    src, d, rgb = sources_of(ref, entry), relief_of(ref, entry), ortho_of(ref, entry)
    assert (src[boxes["R1"]] == 1).all() and np.allclose(d[boxes["R1"]], -0.30, atol=1e-6)   # older, 0.30 deeper
    assert (src[boxes["R2"]] == 2).all() and np.allclose(d[boxes["R2"]], -0.10, atol=1e-6)   # the recent one
    assert (src[boxes["R3"]] == 2).all() and np.allclose(d[boxes["R3"]], 0.0, atol=1e-6)     # 0.10 < 0.15: recent
    assert (src[boxes["R4"]] == 1).all()                                                     # the recent has nothing
    assert (src[boxes["rest"]] == 2).all() and (rgb[:3, boxes["R1"][0], boxes["R1"][1]].T == (200, 100, 50)).all()
    assert (sources_of(ref, C.composite(doc, ref, group, threshold=0.05))[boxes["R3"]] == 1).all()
    assert (rgb[3] == 255).all() and (rgb[:3][:, boxes["R2"][0], boxes["R2"][1]].T == COLOUR_B).all()
    assert entry["group"] == group["id"] and entry["source"] == "rasters" and entry["blending"] == "composite"
    record = group["composite"]
    assert record["entry"] == entry["id"] and record["threshold"] == 0.05                    # the last one made
    assert [(s["chunk"], s["entry"]) for s in record["sources"]] == [(ref.key, a["id"]), (other.key, b["id"])]
    assert [s["stamp"] for s in record["sources"]] == [E.source_stamp(a), E.source_stamp(b)]
    assert [e["kind"] for e in E.elevations(ref)].count("composite") == 1                    # made again in place


def test_an_entry_from_rasters_is_an_elevation_like_the_others(campaigns, walls, tmp_path):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.engine.elevation_drawings import add_units

    doc, (ref, other), _group, (a, b), boxes = campaigns
    entry = walls[2]
    surf = E.frame_grid(ref, a, None, threading.Lock())                   # its file is on the grid of its record
    assert surf.shape == (entry["size"][1], entry["size"][0])
    assert E.frame_grid(other, b, None, threading.Lock()).shape == surf.shape
    for key in ("orthophoto", "relief"):
        with rasterio.open(ref.data_dir / entry[key]) as x, rasterio.open(ref.data_dir / a[key]) as y:
            assert x.transform == y.transform and x.shape == y.shape and x.dtypes == y.dtypes
            assert x.tags()["ppg_frame"] == y.tags()["ppg_frame"] and x.tags()["ppg_depth"] == y.tags()["ppg_depth"]
            assert x.nodata == y.nodata or (np.isnan(x.nodata) and np.isnan(y.nodata))
    frame_of = lambda e: json.loads((ref.data_dir / e["orthophoto"]).parent.joinpath("frame.json").read_text())  # noqa: E731
    assert frame_of(a) == frame_of(entry)
    for key in ("frame", "crs", "depth", "resolution", "bounds", "v_is_height", "scale", "size"):
        assert a[key] == entry[key]
    assert a["source"] == "rasters" and len(a["build"]) == 12 and a["build"] != b["build"] != entry["build"]
    assert a["built_in"] == np.asarray(ref.transform).tolist()            # stamped
    rows, cols = boxes["R1"]
    t = rasterio.open(ref.data_dir / a["relief"]).transform
    u, v = t.c + t.a * (cols.start + cols.stop) / 2, t.f + t.e * (rows.start + rows.stop) / 2
    assert E.relief_at(ref, a, u, v) == pytest.approx(-0.30, abs=1e-6)
    assert np.allclose(E.world_of(a, u, v, None, ref), E.world_of(entry, u, v, -0.30), atol=1e-9)
    add_units(ref, a["id"], [UnitPolygon(box(u - 0.1, v - 0.1, u + 0.1, v + 0.1), us="3", area="1", sito="Scavo")])
    out = E.export_drawings(ref, a, tmp_path / "out", dxf_3d=False)
    assert all(p.exists() for p in out.values())
    h, w = surf.shape
    rgba, relief = _rasters((h, w), COLOUR_A)
    with pytest.raises(ValueError):
        E.entry_from_rasters(ref, entry, rgba[:, :-1], relief[:, :-1])
    with pytest.raises(ValueError):
        E.entry_from_rasters(ref, entry, rgba[:3], relief)
    with pytest.raises(ValueError):
        E.entry_from_rasters(ref, entry, rgba, relief[:-1])


def test_colours_meet_where_campaigns_join(campaigns, walls):
    doc, (ref, other), group, (a, b), boxes = campaigns
    entry = C.composite(doc, ref, group)                                  # match=True by default
    src, rgb = sources_of(ref, entry), ortho_of(ref, entry)
    taken = src == 1
    assert taken[boxes["R1"]].all() and taken[boxes["R4"]].all()
    assert (np.abs(rgb[:3][:, taken].astype(int) - np.asarray(COLOUR_B)[:, None]) < 2).all()
    # with noise in both campaigns: the means of what the older gives meet the recent one's
    _clean(doc, walls[2])
    group, (a, b), boxes = _make(doc, ref, other, walls[2], noise=10.0)
    entry = C.composite(doc, ref, group)
    src, rgb = sources_of(ref, entry), ortho_of(ref, entry)
    recent = ortho_of(other, b)
    have = recent[3] > 0
    mean_b = recent[:3][:, have].mean(axis=1)
    mean_a = rgb[:3][:, src == 1].mean(axis=1)
    assert np.abs(mean_a - mean_b).max() < 3
    assert np.abs(mean_a - np.asarray(COLOUR_A)).max() > 50                 # it was not left as it was


def test_match_colours_by_mean_and_deviation_in_lab():
    rng = np.random.default_rng(5)
    ref = np.clip(rng.normal(120, 20, (3, 40, 50)), 0, 255).astype(np.uint8)
    rgb = np.clip(rng.normal(60, 5, (3, 40, 50)), 0, 255).astype(np.uint8)
    where = np.zeros((40, 50), bool)
    where[:, :25] = True
    out = C.match_colours(rgb, ref, where)
    assert out.shape == rgb.shape and out.dtype == np.uint8
    assert np.abs(out[:, where].mean(axis=1) - ref[:, where].mean(axis=1)).max() < 3
    assert out[:, where].std(axis=1).min() > 10                             # the deviation stretched too
    assert np.array_equal(C.match_colours(rgb, ref, np.zeros_like(where)), rgb)   # nothing in common: as it was


def test_stale_when_a_campaign_is_rebuilt_patched_or_gone_and_made_again_in_place(campaigns, walls):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.engine.elevation_drawings import add_units

    doc, (ref, other), group, (a, b), _boxes = campaigns
    entry = C.composite(doc, ref, group)
    assert not C.is_stale(doc, ref, entry)
    build = a["build"]
    a["build"] = "0" * 12                                                  # campaign A built again
    assert C.is_stale(doc, ref, entry)
    a["build"] = build
    assert not C.is_stale(doc, ref, entry)
    a["applied_patches"] = [{"id": "p1"}]                                  # patches applied to A
    assert C.is_stale(doc, ref, entry)
    del a["applied_patches"]
    assert not C.is_stale(doc, ref, entry)
    G.add_member(group, ref, walls[2], "2020-01-01")                       # a third campaign with files
    assert C.is_stale(doc, ref, entry)
    group["members"].pop()
    assert not C.is_stale(doc, ref, entry)
    add_units(ref, entry["id"], [UnitPolygon(box(1.0, 100.5, 2.0, 101.0), us="5", area="1", sito="Scavo")])
    folder = (ref.data_dir / entry["orthophoto"]).parent
    assert E.remove_elevation(other, b["id"])                              # campaign B gone
    assert C.is_stale(doc, ref, entry)
    b2 = E.entry_from_rasters(other, walls[2], *_rasters((entry["size"][1], entry["size"][0]), COLOUR_B),
                              kind="elevation")
    G.add_member(group, other, b2, "2023-06-01")
    again = C.composite(doc, ref, group)
    assert again["id"] == entry["id"] and again["name"] == entry["name"]
    assert len(ref.meta["drawings"]["frames"][entry["id"]]["units"]) == 1
    assert not C.is_stale(doc, ref, again)
    assert [e["kind"] for e in E.elevations(ref)].count("composite") == 1
    assert (ref.data_dir / again["orthophoto"]).parent == folder and folder.exists()
    assert sorted(p.name for p in folder.parent.iterdir()) == sorted(e["id"] for e in E.elevations(ref))
    assert again["build"] != entry["build"]
    assert [s["entry"] for s in again["sources"]] == [a["id"], b2["id"]]


def test_a_composite_refuses_patches_and_is_built_like_an_elevation(campaigns):
    doc, (ref, other), group, (a, b), _boxes = campaigns
    entry = C.composite(doc, ref, group)
    square = [(1.0, 100.5), (2.0, 100.5), (2.0, 101.0), (1.0, 101.0)]
    with pytest.raises(P.CompositePatched) as exc:
        P.add_patch(ref, square, [ref.cameras[0].label], frame=entry["id"])
    assert exc.value.italian and exc.value.english == str(exc.value)
    with pytest.raises(P.CompositePatched):
        P.update_elevation(ref, entry["id"])
    with pytest.raises(P.CompositePatched):
        P.photos_for_area(ref, square, frame=entry["id"])
    with pytest.raises(P.CompositePatched):
        P.previews(ref, square, [ref.cameras[0].label], frame=entry["id"])
    with pytest.raises(P.CompositePatched):
        P.add_patch(ref, square, [ref.cameras[0].label], frame=a["id"])   # made from rasters: no photos to choose
    new = E.build_like(ref, entry)
    assert new["kind"] == "elevation" and new["blending"] == "mosaic" and new.get("source") != "rasters"
    assert new["size"] == entry["size"] and "sources" not in new


def test_too_few_campaigns_are_refused(campaigns):
    doc, (ref, other), group, (a, b), _boxes = campaigns
    E.remove_elevation(other, b["id"])
    with pytest.raises(ValueError, match="two campaigns"):
        C.composite(doc, ref, group)


def test_command_line_composite(campaigns):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.core.document import Document

    doc, (ref, other), group, (a, b), _boxes = campaigns
    doc.save()
    project = str(doc.path)
    runner = CliRunner()
    r = runner.invoke(app, ["elevation", project, "--like", a["id"], "--composite", "--deeper", "0.2",
                            "--name", "Composito"])
    assert r.exit_code == 0, r.output
    saved = Document.open(project)
    (comp,) = [e for e in E.elevations(saved.chunks[0]) if e["kind"] == "composite"]
    assert comp["threshold"] == 0.2 and comp["name"] == "Composito"
    assert comp["name"] in r.output
    r = runner.invoke(app, ["elevation", project, "--like", a["id"], "--deeper", "0.2"])
    assert r.exit_code == 2 and "--deeper" in r.output
    r = runner.invoke(app, ["elevation", project, "--like", a["id"], "--composite", "--chunks", "X"])
    assert r.exit_code == 2 and "--composite" in r.output
    r = runner.invoke(app, ["elevation", project, "--trace", "1,2,3,4", "--composite"])
    assert r.exit_code == 2 and "--composite" in r.output
    G.groups(ref).clear()
    doc.save()
    r = runner.invoke(app, ["elevation", project, "--like", a["id"], "--composite"])
    assert r.exit_code == 1 and "group" in r.output


def test_flat_shared_cells_do_not_stretch_the_colours():
    """Shared cells almost flat (deviation 0.5) against a reference of deviation 20: the gain is clamped, the stones
    elsewhere are not stretched to the limits; under :data:`MATCH_MIN_CELLS` only the mean is moved."""
    rng = np.random.default_rng(9)
    h, w = 80, 120
    rgb = 128 + rng.normal(0, 20, (1, h, w)) + rng.normal(0, 4, (3, h, w))   # stones: light and shade, some hue
    where = np.zeros((h, w), bool)
    where[:, :20] = True
    rgb[:, where] = 128 + rng.normal(0, 0.5, (3, int(where.sum())))
    rgb = np.clip(np.round(rgb), 0, 255).astype(np.uint8)
    ref = np.clip(np.round(rng.normal(128, 20, (3, h, w))), 0, 255).astype(np.uint8)
    info = {}
    out = C.match_colours(rgb, ref, where, info=info)
    stones = out[:, ~where]
    clipped = ((stones == 0) | (stones == 255)).any(axis=0).mean()
    assert clipped < 0.03, clipped
    assert info["cells"] == int(where.sum()) and info["mode"] == "clamped"
    few = np.zeros((h, w), bool)
    few[:5, :5] = True                                                     # 25 cells
    info = {}
    out = C.match_colours(rgb, ref, few, info=info)
    assert info["mode"] == "mean" and info["cells"] == 25 < C.MATCH_MIN_CELLS
    assert abs(out[:, ~where].astype(float).std() - rgb[:, ~where].astype(float).std()) < 1.5   # not stretched


def test_the_record_says_how_the_colours_were_matched(campaigns):
    doc, (ref, other), group, (a, b), _boxes = campaigns
    C.composite(doc, ref, group)
    (older, recent) = group["composite"]["sources"]
    assert older["colours"]["mode"] in ("full", "clamped", "mean") and older["colours"]["cells"] > 0
    assert "colours" not in recent                                         # the reference of the colours
    assert [s["date"] for s in group["composite"]["sources"]] == ["2017-11-05", "2022-11-03"]


def test_stale_when_a_date_swaps_the_recent_and_the_older(campaigns):
    doc, (ref, other), group, (a, b), _boxes = campaigns
    entry = C.composite(doc, ref, group)
    assert not C.is_stale(doc, ref, entry)
    G.set_date(group, ref.key, a["id"], "2024-05-01")                     # A is now the most recent
    assert C.is_stale(doc, ref, entry)
    G.set_date(group, ref.key, a["id"], "2017-11-05")
    assert not C.is_stale(doc, ref, entry)


def test_a_campaign_without_a_date_is_refused(campaigns):
    doc, (ref, other), group, (a, b), _boxes = campaigns
    G.set_date(group, other.key, b["id"], None)
    assert C.undated(doc, ref, group) == [other.label]
    with pytest.raises(C.Undated, match=f"give a date to {other.label}") as exc:
        C.composite(doc, ref, group)
    assert exc.value.italian.startswith(f"Dai una data a «{other.label}»") and exc.value.english == str(exc.value)
    assert isinstance(exc.value, ValueError) and group.get("composite") is None
    assert not C.composites(ref)


def test_entry_from_rasters_without_a_depth_is_a_value_error(walls):
    _doc, (ref, _other), entry = walls
    w, h = entry["size"]
    like = {k: v for k, v in entry.items() if k != "depth"}
    with pytest.raises(ValueError, match="depth"):
        E.entry_from_rasters(ref, like, *_rasters((h, w), COLOUR_A), kind="elevation")
    assert [e["id"] for e in E.elevations(ref)] == [entry["id"]]


def test_a_campaign_left_out_for_another_grid_is_stamped(campaigns, walls):
    doc, (ref, other), group, (a, b), _boxes = campaigns
    entry = walls[2]
    res = float(entry["resolution"])
    u0, v0, u1, v1 = entry["bounds"]
    shifted = dict(entry, bounds=[u0 + 7 * res, v0, u1 + 7 * res, v1])    # another grid, the same shape
    w, h = entry["size"]
    c = E.entry_from_rasters(other, shifted, *_rasters((h, w), COLOUR_A), kind="elevation")
    G.add_member(group, other, c, "2019-06-01")
    made = C.composite(doc, ref, group)
    (left,) = group["composite"]["left_out"]
    assert left["entry"] == c["id"] and left["stamp"] == E.source_stamp(c)
    assert not C.is_stale(doc, ref, made)
    c["build"] = "1" * 12                                                  # built again (on the right grid)
    assert C.is_stale(doc, ref, made)


def test_more_sources_than_the_raster_holds_are_refused(campaigns, monkeypatch):
    doc, (ref, other), group, (a, b), _boxes = campaigns
    many = [{"chunk": ref, "entry": a, "date": "2017-11-05", "label": f"c{i}", "record": {}} for i in range(255)]
    monkeypatch.setattr(C, "_sources", lambda *_a: many)
    with pytest.raises(ValueError, match="254"):
        C.composite(doc, ref, group)


def test_command_line_refuses_a_campaign_without_a_date(campaigns):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, (ref, other), group, (a, b), _boxes = campaigns
    G.set_date(group, other.key, b["id"], None)
    doc.save()
    r = CliRunner().invoke(app, ["elevation", str(doc.path), "--like", a["id"], "--composite"])
    assert r.exit_code == 1 and f"give a date to {other.label}" in r.output


def test_a_project_appended_keeps_its_groups_and_composite_on_its_own_chunks(campaigns, tmp_path):
    """Source chunks with keys 0 and 1, a group and its composite; a target with chunks X (0) and Y (1): after the
    append the group and the composite name the appended chunks (2 and 3), never X or Y, and the composite is not
    stale; the reference chunk appended alone drops the group (said) and names no chunk of the target."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.append import append_projects

    doc, (ref, other), group, (a, b), _boxes = campaigns
    comp = C.composite(doc, ref, group, match=False)
    doc.save()
    assert (ref.key, other.key) == (0, 1)
    target = ppg.Document()
    target.add_chunk("X")
    target.add_chunk("Y")
    target.save(tmp_path / "dst" / "target.ppg")
    (res,) = append_projects(target, [doc.path])
    assert res.error is None, res.error
    new_ref, new_other = res.chunks
    assert (new_ref.key, new_other.key) == (2, 3)
    found = G.group_of(target, new_other, b["id"])
    assert found is not None and found[0] is new_ref
    g = found[1]
    assert sorted(m["chunk"] for m in g["members"]) == [2, 3]
    record = g["composite"]
    assert [s["chunk"] for s in record["sources"]] == [2, 3]
    assert all(s["chunk"] in (2, 3) for s in record["left_out"])
    assert [o[0] for o in record["order"]] == [2, 3]
    (entry,) = C.composites(new_ref)
    assert entry["id"] == comp["id"] and [s["chunk"] for s in entry["sources"]] == [2, 3]
    assert not C.is_stale(target, new_ref, entry)
    assert G.group_of(target, target.chunks[0], a["id"]) is None
    assert G.group_of(target, target.chunks[1], b["id"]) is None
    # the reference chunk alone: its group names a chunk not appended - dropped and said, no key of X or Y left
    alone = ppg.Document()
    alone.add_chunk("X")
    alone.add_chunk("Y")
    alone.save(tmp_path / "alone" / "target.ppg")
    (res2,) = append_projects(alone, [doc.path], chunks=[0])
    (only,) = res2.chunks
    assert G.groups(only) == []
    (entry2,) = C.composites(only)
    assert all(s["chunk"] in (None, only.key) for s in entry2["sources"])
    assert C.is_stale(alone, only, entry2)
    assert any("gruppo" in w for w in res2.warnings)
