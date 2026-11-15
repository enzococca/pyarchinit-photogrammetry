"""The engine of the matching, the alignment and the depth maps: ``auto``, ``classic`` or ``native``.

The native engine does none of the three steps yet: ``auto`` is the classic engine, and ``native`` falls back to it
and says so - a line in the log, what was asked and what was used in ``chunk.meta["engine"]``, a row in the report.
The word comes from the ``engine`` argument of the three methods of the chunk, else from ``PPG_ENGINE``; the command
line takes it with a hidden ``--engine`` and refuses an unknown one before it opens the project."""
from __future__ import annotations

import inspect
import re

import pytest
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.core.chunk import Chunk
from pyarchinit_photogrammetry.engine import switch

METHODS = ("match_photos", "align_cameras", "build_depth_maps")


def _plain_words(output: str) -> str:
    """What a command printed, as plain words on one line: Typer prints a usage error in a box that wraps the
    message at the width of the terminal, which is not the same on every computer."""
    return " ".join(re.sub("[\u2500-\u257f]", " ", output).split())


@pytest.fixture
def idle_engine(monkeypatch):
    """The classic engine with its three steps doing nothing (and no dense cloud after the depth maps): the tests
    that use it look at what a chunk keeps of a step, not at the step. Gives the list of the steps that ran."""
    from pyarchinit_photogrammetry.engine import dense, sfm

    ran: list[str] = []

    def match_photos(chunk, **kwargs):
        ran.append("match_photos")

    def align_cameras(chunk, **kwargs):
        ran.append("align_cameras")
        return 0

    def build_depth_maps(chunk, **kwargs):
        ran.append("build_depth_maps")

    monkeypatch.setattr(sfm, "match_photos", match_photos)
    monkeypatch.setattr(sfm, "align_cameras", align_cameras)
    monkeypatch.setattr(dense, "build_depth_maps", build_depth_maps)
    monkeypatch.setattr(dense, "build_point_cloud", lambda chunk, **kwargs: [])
    return ran


# ---------------------------------------------------------------------------------------- the word and the engine
def test_the_default_is_auto_and_auto_is_classic_today(monkeypatch):
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    c = switch.resolve("align_cameras")
    assert (c.requested, c.used, c.fallback) == ("auto", "classic", None)


def test_native_falls_back_to_classic_and_says_so(monkeypatch, caplog):
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    monkeypatch.setenv("PPG_ENGINE", " Native ")
    with caplog.at_level("INFO"):
        c = switch.resolve("match_photos")
    assert (c.requested, c.used) == ("native", "classic") and "match_photos" in c.fallback
    assert c.fallback == "the native engine does not do 'match_photos' yet"
    assert "classic" in caplog.text
    assert len(caplog.records) == 1  # one line for one fallback
    line = caplog.records[0]
    assert (line.name, line.levelname) == (switch.__name__, "WARNING")
    assert line.getMessage() == ("engine 'native' asked for match_photos: classic used "
                                 "(the native engine does not do 'match_photos' yet)")


def test_the_argument_wins_over_the_environment(monkeypatch):
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    monkeypatch.setenv("PPG_ENGINE", "native")
    assert switch.resolve("align_cameras", "classic").requested == "classic"


def test_an_unknown_engine_names_the_accepted_words(monkeypatch):
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    with pytest.raises(ValueError, match="auto, classic or native"):
        switch.resolve("align_cameras", "colmap")
    monkeypatch.setenv("PPG_ENGINE", "fast")
    with pytest.raises(ValueError, match="PPG_ENGINE"):
        switch.resolve("align_cameras")


def test_the_word_is_checked_as_it_is_resolved(monkeypatch):
    """``check`` gives the word the program goes by - of the argument, else of the variable, else ``auto`` - without
    the spaces around it and in small letters, and refuses any other with the sentence the command line prints."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    assert switch.ENGINES == ("auto", "classic", "native")
    assert [switch.check(word) for word in (None, "auto", " Classic ", "NATIVE")] == ["auto", "auto", "classic",
                                                                                      "native"]
    monkeypatch.setenv("PPG_ENGINE", "Classic")
    assert switch.check(None) == "classic" and switch.check("native") == "native"
    monkeypatch.setenv("PPG_ENGINE", "  ")  # a variable set to nothing asks for nothing
    assert switch.check(None) == "auto"
    with pytest.raises(ValueError) as refused:
        switch.check(" Colmap ")
    assert str(refused.value) == "unknown engine 'colmap': use auto, classic or native"
    assert "PPG_ENGINE" not in str(refused.value)  # the variable is named only when the word came from it
    with pytest.raises(ValueError, match="auto, classic or native"):
        switch.check("")  # nothing, given as the argument, is not a word
    monkeypatch.setenv("PPG_ENGINE", "fast")
    with pytest.raises(ValueError, match="unknown engine 'fast'.*PPG_ENGINE.*: use auto, classic or native"):
        switch.check(None)


def test_only_a_fallback_is_logged(monkeypatch, caplog):
    """The classic engine doing what was asked, or what ``auto`` gives, is not news: no line in the log."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    assert switch.NATIVE_STEPS == frozenset()  # the steps the native engine does: none in this milestone
    with caplog.at_level("INFO"):
        for step in METHODS:
            for word in (None, "auto", "classic"):
                c = switch.resolve(step, word)
                assert (c.requested, c.used, c.fallback) == (word or "auto", "classic", None)
    assert not caplog.records
    with caplog.at_level("INFO"):
        for step in METHODS:
            assert switch.resolve(step, "native").fallback == f"the native engine does not do '{step}' yet"
    assert [record.getMessage().split(":")[0] for record in caplog.records] == [
        f"engine 'native' asked for {step}" for step in METHODS]  # one line per step that fell back


def test_a_step_the_native_engine_does_is_its_own_when_asked_for(monkeypatch, caplog):
    """A step named in ``NATIVE_STEPS`` (none today: the next milestones add theirs) goes to the native engine when
    ``native`` is asked for, with no fallback and no line in the log; ``auto`` stays on the classic engine, and the
    steps not named fall back as before."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    monkeypatch.setattr(switch, "NATIVE_STEPS", frozenset({"match_photos"}))
    with caplog.at_level("INFO"):
        c = switch.resolve("match_photos", "native")
        assert (c.requested, c.used, c.fallback) == ("native", "native", None)
        for word in (None, "auto", "classic"):
            c = switch.resolve("match_photos", word)
            assert (c.requested, c.used, c.fallback) == (word or "auto", "classic", None)
    assert not caplog.records
    c = switch.resolve("align_cameras", "native")
    assert (c.used, c.fallback) == ("classic", "the native engine does not do 'align_cameras' yet")


# ------------------------------------------------------------------------------------------- what the chunk keeps
def test_a_chunk_records_the_engine_it_used(monkeypatch, idle_engine):
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    chunk = Chunk(key=0, label="x")
    chunk.match_photos(engine="native")
    record = chunk.meta["engine"]["match_photos"]
    assert sorted(record) == ["fallback", "requested", "used"]
    assert (record["requested"], record["used"]) == ("native", "classic")
    assert isinstance(record["fallback"], str) and record["fallback"]
    assert chunk.align_cameras() == 0
    assert chunk.meta["engine"]["align_cameras"] == {"requested": "auto", "used": "classic", "fallback": None}
    assert chunk.meta["engine"]["match_photos"] == record  # each step has its own record
    chunk.build_depth_maps(engine=" Classic ")
    assert chunk.meta["engine"]["build_depth_maps"] == {"requested": "classic", "used": "classic", "fallback": None}
    assert idle_engine == list(METHODS)
    monkeypatch.setenv("PPG_ENGINE", "native")  # without the argument, the variable
    chunk.align_cameras()
    assert chunk.meta["engine"]["align_cameras"]["requested"] == "native"


def test_the_record_is_of_a_step_that_finished(monkeypatch, idle_engine):
    """An unknown word stops a step before it starts, and a step that fails leaves the record of the last one that
    ended: what the chunk keeps is the engine of what it holds."""
    from pyarchinit_photogrammetry.engine import dense, sfm

    monkeypatch.delenv("PPG_ENGINE", raising=False)
    chunk = Chunk(key=0, label="x")
    for name in METHODS:
        with pytest.raises(ValueError, match="auto, classic or native"):
            getattr(chunk, name)(engine="colmap")
    assert idle_engine == [] and "engine" not in chunk.meta
    for name in METHODS:
        getattr(chunk, name)(engine="classic")
    kept = {name: dict(chunk.meta["engine"][name]) for name in METHODS}

    def fails(chunk, **kwargs):
        raise RuntimeError("the step failed")

    monkeypatch.setattr(sfm, "match_photos", fails)
    monkeypatch.setattr(sfm, "align_cameras", fails)
    monkeypatch.setattr(dense, "build_depth_maps", fails)
    for name in METHODS:
        with pytest.raises(RuntimeError, match="the step failed"):
            getattr(chunk, name)(engine="native")
    assert chunk.meta["engine"] == kept


def test_the_engine_is_the_last_argument_and_by_keyword_only():
    """The other arguments of the three methods keep their places (the program and its plugins also give them by
    position): the engine comes after them, by its name."""
    for name in METHODS:
        parameters = inspect.signature(getattr(Chunk, name)).parameters
        engine = parameters["engine"]
        assert list(parameters)[-1] == "engine", name
        assert engine.kind is inspect.Parameter.KEYWORD_ONLY and engine.default is None, name
        assert [p.kind for p in parameters.values()].count(inspect.Parameter.KEYWORD_ONLY) == 1, name


# ------------------------------------------------------------------------------------------------ the command line
def test_the_cli_refuses_an_unknown_engine(tmp_path, monkeypatch):
    """An unknown word is a usage error that names the accepted ones, and it comes before the project is opened: the
    project named here is not there, which ``align`` and ``dense`` would say otherwise and ``process`` would mend by
    making it."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    missing = tmp_path / "missing.ppg"
    run = CliRunner().invoke
    commands = (["align", str(missing)], ["dense", str(missing)], ["process", str(missing), str(tmp_path)])
    for command in commands:
        result = run(app, [*command, "--engine", "colmap"])
        assert result.exit_code == 2, result.output
        said = _plain_words(result.output)
        assert "unknown engine 'colmap': use auto, classic or native" in said, result.output
        assert "--engine" in said, result.output
    monkeypatch.setenv("PPG_ENGINE", "fast")  # the variable is checked like the option, and named
    for command in commands:
        result = run(app, command)
        assert result.exit_code == 2, result.output
        said = _plain_words(result.output)
        assert "unknown engine 'fast'" in said and "PPG_ENGINE" in said, result.output
        assert "use auto, classic or native" in said, result.output
    assert not missing.exists() and not missing.with_name("missing.ppg.files").exists()


def test_the_commands_ask_the_steps_for_the_engine(tmp_path, monkeypatch, idle_engine):
    """``align`` and ``dense`` hand the word of ``--engine`` (else of ``PPG_ENGINE``) to the steps they run, and the
    project they save keeps it; the option is in no help."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    doc = ppg.Document()
    doc.add_chunk("x")
    project = doc.save(tmp_path / "p.ppg")
    run = CliRunner().invoke

    def kept() -> dict:
        records = ppg.Document.open(project).chunk.meta.get("engine") or {}
        return {step: (record["requested"], record["used"], bool(record["fallback"]))
                for step, record in records.items()}

    for command in ("align", "dense"):
        result = run(app, [command, str(project)])
        assert result.exit_code == 0, result.output
    assert kept() == dict.fromkeys(METHODS, ("auto", "classic", False))
    for command in ("align", "dense"):
        result = run(app, [command, str(project), "--engine", "Native"])
        assert result.exit_code == 0, result.output
    assert kept() == dict.fromkeys(METHODS, ("native", "classic", True))
    monkeypatch.setenv("PPG_ENGINE", "classic")
    result = run(app, ["align", str(project)])
    assert result.exit_code == 0, result.output
    assert kept() == {"match_photos": ("classic", "classic", False), "align_cameras": ("classic", "classic", False),
                      "build_depth_maps": ("native", "classic", True)}
    assert idle_engine == ["match_photos", "align_cameras", "build_depth_maps"] * 2 + ["match_photos", "align_cameras"]
    for command in ("align", "dense", "process"):
        result = run(app, [command, "--help"])
        assert result.exit_code == 0 and "--engine" not in result.output, result.output


@pytest.mark.slow
def test_process_with_the_native_engine_asked_for(scene_dir, tmp_path, monkeypatch):
    """``ppg process --engine native`` on the trench of the session: the whole chain ends well, on the classic
    engine; the project keeps what was asked and what was used for each of the three steps, and the report says it
    once for each."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    project = tmp_path / "trench.ppg"
    result = CliRunner().invoke(app, ["process", str(project), str(scene_dir / "photos"),
                                      "--gcp", str(scene_dir / "gcp.csv"), "--crs", "EPSG:3004", "--engine", "native"])
    assert result.exit_code == 0, result.output[-3000:]
    chunk = ppg.Document.open(project).chunk
    assert len(chunk.aligned_cameras) == len(chunk.cameras) > 0
    assert sorted(chunk.meta["engine"]) == sorted(METHODS)
    for step, record in chunk.meta["engine"].items():
        assert (record["requested"], record["used"]) == ("native", "classic"), step
        assert isinstance(record["fallback"], str) and step in record["fallback"], step
    report = project.with_suffix(".report.html").read_text(encoding="utf-8")
    assert report.count("Engine used") == 3
    assert report.count("classic (the native engine does not do") == 3
