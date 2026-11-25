"""The same elevation of a wall on several chunks - the campaigns of that wall - as a group kept in the chunk the wall
was traced on (:mod:`pyarchinit_photogrammetry.engine.elevation_groups`): the grids that lie on the same pixels, the
date of each campaign, the chunks refused and those where the wall is not, a cancel half way; and ``ppg elevation``
with ``--path`` and ``--chunks``."""
from __future__ import annotations

import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.core.progress import Cancelled
from pyarchinit_photogrammetry.engine import elevation as E
from pyarchinit_photogrammetry.engine import elevation_groups as G
from pyarchinit_photogrammetry.engine import orient

from wall_helpers import CAMPAIGN_DATES, wall_campaigns


@pytest.fixture
def two(tmp_path, wall_dataset):
    doc, chunks, info, entry = wall_campaigns(tmp_path / "doc", wall_dataset)
    return doc, chunks, info, entry


def test_the_second_campaign_lies_on_the_same_pixels(two):
    doc, (a, b), info, entry = two
    out = G.build_on_chunks(doc, a, entry, [b])
    (chunk, new), = out["built"]
    assert chunk is b and new["grid_of"] == entry["id"] and out["refused"] == [] and out["empty"] == []
    assert new["frame"] == entry["frame"] and new["bounds"] == entry["bounds"] and new["size"] == entry["size"]
    for key in ("orthophoto", "relief"):
        with rasterio.open(a.data_dir / entry[key]) as x, rasterio.open(b.data_dir / new[key]) as y:
            assert x.transform == y.transform and x.shape == y.shape           # not a pixel of shift
    (group,) = G.groups(a)
    assert G.groups(b) == [] and group["reference"] == entry["id"]              # kept where the wall was traced
    assert [(m["chunk"], m["entry"], m["date"]) for m in group["members"]] == \
        [(a.key, entry["id"], "2017-11-05"), (b.key, new["id"], "2022-11-03")]
    ref, found = G.group_of(doc, b, new["id"])
    assert ref is a and found is group and "elevation_groups" in orient.PROGRAM_KEYS
    assert group["id"].startswith("g") and len(group["id"]) == 9 and group["composite"] is None
    again = G.build_on_chunks(doc, b, new, [a, b])                              # from a member: the same group
    assert again["built"] == [] and [r["reason"] for r in again["refused"]] == ["already in the group"] * 2
    assert [r["code"] for r in again["refused"]] == ["already"] * 2
    assert G.groups(a) == [group] and G.groups(b) == []


def test_dates_by_hand_then_the_median_of_the_photos(two):
    doc, (a, b), _info, entry = two
    cams = a.cameras[:5]
    assert len(cams) == 5
    for cam, when in zip(cams, ["2019:03:02 10:00:00", "2017:11:05 09:00:00", "2017:11:05 11:00:00",
                                "2019:03:02 12:00:00", "2017:11:05 10:00:00"]):
        cam.meta["datetime"] = when
    for cam in a.cameras[5:]:
        cam.meta.pop("datetime", None)
    assert G.campaign_date(a) == "2017-11-05"                                    # the third in time order
    for cam, when in zip(cams, ["2019:03:02 10:00:00", "2017:11:05 09:00:00", "2019:03:02 11:00:00",
                                "2019:03:02 12:00:00", "2017:11:05 10:00:00"]):
        cam.meta["datetime"] = when
    assert G.campaign_date(a) == "2019-03-02"
    a.meta["date"] = "2020-02-01T10:00"
    assert G.campaign_date(a) == "2020-02-01"                                    # set by hand: it wins
    del a.meta["date"]
    for cam in a.cameras:
        cam.meta.pop("datetime", None)
    assert G.campaign_date(a) is None

    out = G.build_on_chunks(doc, a, entry, [b])
    (group,) = G.groups(a)
    assert [m["date"] for m in G.members(doc, a, group)] == ["2022-11-03", None]  # the undated last
    G.set_date(group, a.key, entry["id"], "2023-01-01")
    listed = G.members(doc, a, group)
    assert [(m["chunk"], m["date"]) for m in listed] == [(b, "2022-11-03"), (a, "2023-01-01")]
    assert listed[1]["entry"] is E.elevations(a)[0] and listed[0]["entry"] is out["built"][0][1]
    assert listed[1]["label"] == a.label and listed[1]["record"] is group["members"][0]
    assert "date" not in a.meta and "date" not in b.meta                         # the group keeps its own dates
    with pytest.raises(ValueError):
        G.set_date(group, a.key, entry["id"], "5 novembre")
    with pytest.raises(KeyError):
        G.set_date(group, a.key, "nope", "2023-01-01")


def test_dates_given_replace_those_of_the_photos(two):
    doc, (a, b), _info, entry = two
    G.build_on_chunks(doc, a, entry, [b], dates={b.key: "2021-06-30"})
    (group,) = G.groups(a)
    assert [m["date"] for m in group["members"]] == ["2017-11-05", "2021-06-30"]


def test_local_chunks_need_the_same_frame_said(tmp_path, wall_dataset):
    doc, (a, b), _info, entry = wall_campaigns(tmp_path / "doc", wall_dataset, crs="LOCAL")
    out = G.build_on_chunks(doc, a, entry, [b])
    assert out["built"] == [] and out["empty"] == []
    (refused,) = out["refused"]
    assert refused["chunk"] == b.label and "same_frame" in refused["reason"] and refused["code"] == "local"
    assert E.elevations(b) == [] and G.groups(a) == []
    out = G.build_on_chunks(doc, a, entry, [b], same_frame=True)
    assert [c for c, _e in out["built"]] == [b] and len(G.groups(a)[0]["members"]) == 2


def test_another_system_is_refused_and_the_others_are_built(tmp_path, wall_dataset):
    doc, (a, b, c), _info, entry = wall_campaigns(tmp_path / "doc", wall_dataset, chunks=3, dates=CAMPAIGN_DATES)
    b.crs = "EPSG:32634"
    out = G.build_on_chunks(doc, a, entry, [b, c])
    (refused,) = out["refused"]
    assert refused["chunk"] == b.label and "EPSG:32633" in refused["reason"] and "EPSG:32634" in refused["reason"]
    assert refused["code"] == "crs"
    assert [ch for ch, _e in out["built"]] == [c] and E.elevations(b) == []
    assert [m["chunk"] for m in G.groups(a)[0]["members"]] == [a.key, c.key]


def test_a_campaign_without_the_wall_is_said_and_left_out(two):
    doc, (a, b), _info, entry = two
    t = np.array(b.transform, np.float64)
    t[0, 3] += 100.0                                                             # its model 100 m away
    b.transform = t
    out = G.build_on_chunks(doc, a, entry, [b])
    assert out["empty"] == [b.label] and out["built"] == [] and out["refused"] == []
    assert E.elevations(b) == []
    folder = b.data_dir / "elevations"
    assert not folder.exists() or not any(folder.iterdir())
    assert all(m["chunk"] != b.key for g in G.groups(a) for m in g["members"])


def test_a_cancel_keeps_what_was_built(tmp_path, wall_dataset):
    doc, (a, b, c), _info, entry = wall_campaigns(tmp_path / "doc", wall_dataset, chunks=3, dates=CAMPAIGN_DATES)

    def progress(fraction, message=""):
        if fraction > 0.75:                                                      # half way through the third
            raise Cancelled("cancelled")

    with pytest.raises(Cancelled):
        G.build_on_chunks(doc, a, entry, [b, c], progress=progress)
    (group,) = G.groups(a)
    assert [m["chunk"] for m in group["members"]] == [a.key, b.key]
    assert E.elevations(c) == []
    folder = c.data_dir / "elevations"
    assert not folder.exists() or not any(folder.iterdir())


def test_members_resolve_and_survive_a_removal(two):
    doc, (a, b), _info, entry = two
    (_ch, new), = G.build_on_chunks(doc, a, entry, [b])["built"]
    assert E.remove_elevation(b, new["id"])
    (group,) = G.groups(a)
    listed = G.members(doc, a, group)
    assert [(m["chunk"], m["entry"] is None) for m in listed] == [(a, False), (b, True)]
    assert listed[1]["record"]["entry"] == new["id"] and len(group["members"]) == 2  # the record stays
    out = G.build_on_chunks(doc, a, entry, [b])                                  # built again: the record replaced
    (_ch, newer), = out["built"]
    assert [(m["chunk"], m["entry"]) for m in G.groups(a)[0]["members"]] == [(a.key, entry["id"]),
                                                                            (b.key, newer["id"])]


def test_command_line_path_and_campaigns(two):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.core.document import Document

    doc, (a, b), info, entry = two
    b.label = "Campagna 2"
    doc.save()
    project = str(doc.path)
    runner = CliRunner()
    face = " ".join(f"{x!r},{y!r}" for x, y in info["face"])
    r = runner.invoke(app, ["elevation", project, "--path", face, "--side", "right", "--extend", "0.5,0.5",
                            "--heights", "100,102", "--resolution", "0.02", "--no-snap", "--name", "Da riga"])
    assert r.exit_code == 0, r.output
    assert "along the wall" in r.output and "corners: 1" in r.output and "v = height" in r.output
    made = next(e for e in E.elevations(Document.open(project).chunks[0]) if e["name"] == "Da riga")
    assert made["frame"]["kind"] == "path" and made.get("scale") == "wall"
    assert made["bounds"][2] - made["bounds"][0] == pytest.approx(11.0, abs=0.03)

    r = runner.invoke(app, ["elevation", project, "--path", "1,2"])
    assert r.exit_code == 2 and "two points" in r.output
    r = runner.invoke(app, ["elevation", project, "--path", face, "--section"])
    assert r.exit_code == 2 and "--section" in r.output
    r = runner.invoke(app, ["elevation", project, "--trace", "1,2,3,4", "--scale", "wall"])
    assert r.exit_code == 2 and "--circle" in r.output
    (x0, y0), (x1, y1) = info["face"][:2]
    for side, name in (("left", "Lato sinistro"), ("right", "Lato destro")):
        r = runner.invoke(app, ["elevation", project, "--trace", f"{x0},{y0},{x1},{y1}", "--side", side,
                                "--resolution", "0.05", "--front", "0.3", "--back", "0.3", "--name", name])
        assert r.exit_code == 0, r.output
    planes = {e["name"]: e["frame"] for e in E.elevations(Document.open(project).chunks[0])}
    assert x1 > x0 and y1 == y0                                                 # the first leg runs east
    assert planes["Lato sinistro"]["u_axis"][0] < -0.99 and planes["Lato destro"]["u_axis"][0] > 0.99

    r = runner.invoke(app, ["elevation", project, "--like", entry["id"], "--chunks", "Campagna 2",
                            "--dates", "Campagna 2=2022-11-04"])
    assert r.exit_code == 0, r.output
    assert "Campagna 2: built" in r.output
    saved = Document.open(project)
    (group,) = G.groups(saved.chunks[0])
    assert [m["date"] for m in group["members"]] == ["2017-11-05", "2022-11-04"]
    r = runner.invoke(app, ["elevation", project, "--like", entry["id"], "--chunks", "Campagna 2"])
    assert r.exit_code == 1 and "Campagna 2: refused: already in the group" in r.output
    r = runner.invoke(app, ["elevation", project, "--like", entry["id"], "--chunks", "X", "--chunk", "Y"])
    assert r.exit_code == 2 and "--chunk" in r.output
    r = runner.invoke(app, ["elevation", project, "--like", entry["id"], "--chunks", "Campagna 2", "--snap-band", "1"])
    assert r.exit_code == 2 and "--snap-band" in r.output


def test_command_line_path_builds_on_the_line_given_unless_snap(two):
    """The line given is the frame (the snap is a proposal of the window); ``--snap`` builds on the face."""
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.core.document import Document

    from wall_helpers import distance_to

    doc, (a, b), info, entry = two
    project = str(doc.path)
    face = np.asarray(info["face"], float)
    clicks = face + np.array([[0.0, -0.3], [0.3, -0.3], [0.3, 0.0]])  # the face clicked 0.3 m in front of it
    line = " ".join(f"{float(x)!r},{float(y)!r}" for x, y in clicks)
    runner = CliRunner()
    common = ["--side", "right", "--heights", "100,102", "--resolution", "0.05"]
    r = runner.invoke(app, ["elevation", project, "--path", line, *common, "--name", "Cliccata"])
    assert r.exit_code == 0, r.output
    assert "snapped" not in r.output
    r = runner.invoke(app, ["elevation", project, "--path", line, *common, "--snap", "--name", "Agganciata"])
    assert r.exit_code == 0, r.output
    assert "snapped, vertices moved" in r.output
    frames = {e["name"]: e["frame"] for e in E.elevations(Document.open(project).chunks[0])}
    assert np.allclose(frames["Cliccata"]["vertices"], clicks, rtol=0, atol=1e-6)  # the vertices are the clicks
    snapped = np.asarray(frames["Agganciata"]["vertices"], float)
    assert distance_to(face, snapped).max() < 0.05 and np.allclose(frames["Agganciata"]["clicked"], clicks, rtol=0, atol=1e-6)
    assert np.abs(snapped - clicks).max() > 0.2
    r = runner.invoke(app, ["elevation", project, "--path", line, *common, "--snap-band", "1"])
    assert r.exit_code == 2 and "--snap-band" in r.output


def test_a_chunk_without_a_model_is_refused_with_its_code(two):
    doc, (a, b), _info, entry = two
    (b.data_dir / "model.ply").unlink()
    (refused,) = G.build_on_chunks(doc, a, entry, [b])["refused"]
    assert refused == {"chunk": b.label, "reason": G.NO_MODEL, "code": "no_model"}


def test_a_replaced_member_keeps_its_date_and_the_reference_follows(two):
    doc, (a, b), _info, entry = two
    (_ch, new), = G.build_on_chunks(doc, a, entry, [b])["built"]
    (group,) = G.groups(a)
    G.set_date(group, b.key, new["id"], "2022-12-25")                          # set by hand
    E.remove_elevation(b, new["id"])
    (_ch, newer), = G.build_on_chunks(doc, a, entry, [b])["built"]
    assert [(m["entry"], m["date"]) for m in group["members"] if m["chunk"] == b.key] == [(newer["id"], "2022-12-25")]
    E.remove_elevation(b, newer["id"])
    G.build_on_chunks(doc, a, entry, [b], dates={b.key: "2023-03-03"})        # a date given wins
    assert [m["date"] for m in group["members"] if m["chunk"] == b.key] == ["2023-03-03"]

    (member,) = [e for e in E.elevations(b)]
    E.remove_elevation(a, entry["id"])                                         # the reference's own elevation
    (_ch, again), = G.build_on_chunks(doc, b, member, [a])["built"]
    assert G.groups(a) == [group] and group["reference"] == again["id"]
    assert [m["entry"] for m in group["members"] if m["chunk"] == a.key] == [again["id"]]


def test_the_reference_date_given_when_the_group_is_made(two):
    doc, (a, b), _info, entry = two
    G.build_on_chunks(doc, a, entry, [b], dates={a.key: "2016-01-02"})
    assert [m["date"] for m in G.groups(a)[0]["members"]] == ["2016-01-02", "2022-11-03"]


def test_a_duplicate_of_the_reference_takes_no_group(two):
    from pyarchinit_photogrammetry.engine.chunks import duplicate_chunk

    doc, (a, b), _info, entry = two
    G.build_on_chunks(doc, a, entry, [b])
    dup = duplicate_chunk(doc, a)
    assert G.groups(dup) == [] and len(G.groups(a)) == 1


def test_command_line_campaigns_stop_on_an_error_and_keep_what_was_built(tmp_path, wall_dataset, monkeypatch):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.core.document import Document

    doc, (a, b, c), _info, entry = wall_campaigns(tmp_path / "doc", wall_dataset, chunks=3, dates=CAMPAIGN_DATES)
    project = str(doc.path)
    real = E.build_like

    def build_like(chunk, *args, **kw):
        if chunk.label == c.label:
            raise ValueError("the third campaign broke")
        return real(chunk, *args, **kw)

    monkeypatch.setattr(E, "build_like", build_like)
    r = CliRunner().invoke(app, ["elevation", project, "--like", entry["id"], "--chunks", f"{b.label},{c.label}"])
    assert r.exit_code == 1 and isinstance(r.exception, SystemExit), (r.output, r.exception)
    assert f"{b.label}: built" in r.output and "Error: the third campaign broke" in r.output
    saved = Document.open(project)
    (group,) = G.groups(saved.chunks[0])
    assert [m["chunk"] for m in group["members"]] == [a.key, b.key]
    (kept,) = E.elevations(saved.chunks[1])
    assert (saved.chunks[1].data_dir / "elevations" / kept["id"]).is_dir()
    assert E.elevations(saved.chunks[2]) == []
