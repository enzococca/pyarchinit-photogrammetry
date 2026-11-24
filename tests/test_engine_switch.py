"""The engine of the matching, the alignment and the depth maps: ``auto``, ``classic`` or ``native``.

The native engine does the matching and the alignment (``NATIVE_STEPS``) and not the depth maps yet: ``auto`` is the
classic engine until a step is put in ``AUTO_NATIVE_STEPS`` (after the gate of the step: the matching), and ``native``
on a step the native engine does not do falls back to the classic one and says so - a line in the log, what was asked and
what was used in ``chunk.meta["engine"]``, a row in the report. The word comes from the ``engine`` argument of the
three methods of the chunk, else from ``PPG_ENGINE``; the command line takes it with a hidden ``--engine`` and refuses
an unknown one before it opens the project."""
from __future__ import annotations

import inspect

import pytest
from cli_helpers import plain_words
from typer.testing import CliRunner

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.cli import app
from pyarchinit_photogrammetry.core.chunk import Chunk
from pyarchinit_photogrammetry.engine import switch


class _Ran(list):
    """The steps an idle engine ran, in order; ``reason``: what ``native_match.cannot_run`` answers (None: nothing
    keeps the native matching from running, whether the native core is installed or not)."""

    reason: str | None = None
    align_reason: str | None = None  # what ``native_map.cannot_run`` answers for the alignment


@pytest.fixture
def idle_engine(monkeypatch):
    """The classic engine with its three steps doing nothing (and no dense cloud after the depth maps), and the native
    matching doing nothing too, whatever the native core of the computer (the native alignment is the classic step
    asked with ``engine="native"``, which does nothing as well): the tests that use it look at what a chunk
    keeps of a step, not at the step. Gives the list of the steps that ran (``reason``: :class:`_Ran`)."""
    from pyarchinit_photogrammetry.engine import dense, native_map, native_match, sfm

    ran = _Ran()

    def match_photos(chunk, **kwargs):
        ran.append("match_photos")

    monkeypatch.setattr(native_match, "match_photos", match_photos)
    monkeypatch.setattr(native_match, "cannot_run", lambda chunk, matcher, reset: ran.reason)
    monkeypatch.setattr(native_match, "core_unavailable", lambda: None)  # the reasons of a request, not of the core
    monkeypatch.setattr(native_map, "cannot_run", lambda chunk, mapper, reset: ran.align_reason)
    monkeypatch.setattr(native_map, "core_unavailable", lambda: None)

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
        c = switch.resolve("build_depth_maps")
    assert (c.requested, c.used) == ("native", "classic") and "build_depth_maps" in c.fallback
    assert c.fallback == "the native engine does not do 'build_depth_maps' yet"
    assert "classic" in caplog.text
    assert len(caplog.records) == 1  # one line for one fallback
    line = caplog.records[0]
    assert (line.name, line.levelname) == (switch.__name__, "WARNING")
    assert line.getMessage() == ("engine 'native' asked for build_depth_maps: classic used "
                                 "(the native engine does not do 'build_depth_maps' yet)")


def test_the_alignment_is_the_native_engines_when_asked_for(monkeypatch, caplog):
    """``align_cameras`` is a step the native engine does (its global mapper, :mod:`engine.native_map`): ``native``
    gives it to the native engine, with no fallback and no line in the log; ``auto`` stays classic until the step has
    passed its gate (it is not in ``AUTO_NATIVE_STEPS``)."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    assert "align_cameras" in switch.NATIVE_STEPS and "align_cameras" not in switch.AUTO_NATIVE_STEPS
    with caplog.at_level("INFO"):
        assert switch.resolve("align_cameras", "native") == switch.Choice("native", "native", None)
        for word in (None, "auto"):
            assert switch.resolve("align_cameras", word) == switch.Choice("auto", "classic", None)
        assert switch.resolve("align_cameras", "classic") == switch.Choice("classic", "classic", None)
    assert not caplog.records


def test_the_matching_is_the_native_engines_when_asked_for(monkeypatch, caplog):
    """``match_photos`` is a step the native engine does: ``native`` gives it to the native engine, with no fallback
    and no line in the log, and so does ``auto`` since the matching passed its gate; ``classic`` keeps the classic
    one."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    assert "match_photos" in switch.NATIVE_STEPS
    with caplog.at_level("INFO"):
        c = switch.resolve("match_photos", "native")
        assert (c.requested, c.used, c.fallback) == ("native", "native", None)
        for word in (None, "auto"):
            c = switch.resolve("match_photos", word)
            assert (c.requested, c.used, c.fallback) == ("auto", "native", None)
        c = switch.resolve("match_photos", "classic")
        assert (c.requested, c.used, c.fallback) == ("classic", "classic", None)
    assert not caplog.records


def test_auto_takes_a_step_only_once_it_is_in_auto_native_steps(monkeypatch, caplog):
    """``AUTO_NATIVE_STEPS`` names the steps ``auto`` gives to the native engine: none until a step has passed its
    gate (the session owner adds it then, one line): the matching, since its gate; ``auto`` is native for the
    matching, with no line in the log, and ``classic`` is still the classic engine."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    assert switch.AUTO_NATIVE_STEPS == frozenset({"match_photos"})
    monkeypatch.setattr(switch, "AUTO_NATIVE_STEPS", frozenset())
    for word in (None, "auto"):
        assert switch.resolve("match_photos", word) == switch.Choice("auto", "classic", None)
    monkeypatch.setattr(switch, "AUTO_NATIVE_STEPS", frozenset({"match_photos"}))
    with caplog.at_level("INFO"):
        for word in (None, "auto"):
            assert switch.resolve("match_photos", word) == switch.Choice("auto", "native", None)
        assert switch.resolve("match_photos", "classic") == switch.Choice("classic", "classic", None)
        assert switch.resolve("align_cameras", "auto") == switch.Choice("auto", "classic", None)
    assert not caplog.records
    # a step in AUTO_NATIVE_STEPS that the native engine does not do stays classic: the two sets are read together
    monkeypatch.setattr(switch, "AUTO_NATIVE_STEPS", frozenset({"build_depth_maps"}))
    assert switch.resolve("build_depth_maps", "auto") == switch.Choice("auto", "classic", None)


def test_fall_back_keeps_the_request_and_logs(caplog):
    """``fall_back`` is what a method does when the engine chosen cannot do the step after all (the native core
    missing, a learned matcher, a failure half way): the same request, the classic engine used, the reason kept, and
    one WARNING line that gives the reason."""
    choice = switch.Choice("native", "native", None)
    with caplog.at_level("INFO"):
        back = switch.fall_back(choice, "learned matcher 'disk': the native engine matches SIFT points only")
    assert back == switch.Choice("native", "classic", "learned matcher 'disk': the native engine matches SIFT points only")
    assert choice == switch.Choice("native", "native", None)  # the choice given is not changed
    assert len(caplog.records) == 1
    line = caplog.records[0]
    assert (line.name, line.levelname) == (switch.__name__, "WARNING")
    assert "native" in line.getMessage() and "classic" in line.getMessage()
    assert "learned matcher 'disk'" in line.getMessage()


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
    """The classic engine doing what was asked, or what ``auto`` gives, is not news: no line in the log; nor is the
    native engine doing a step it does."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    # the steps the native engine does: the matching and the alignment
    assert switch.NATIVE_STEPS == frozenset({"match_photos", "align_cameras"})
    classic_only_steps = [step for step in switch.STEPS if step not in switch.NATIVE_STEPS]
    assert classic_only_steps == ["build_depth_maps"]
    with caplog.at_level("INFO"):
        for step in switch.STEPS:
            for word in (None, "auto", "classic"):
                c = switch.resolve(step, word)
                used = "native" if step in switch.AUTO_NATIVE_STEPS and word != "classic" else "classic"
                assert (c.requested, c.used, c.fallback) == (word or "auto", used, None)
        assert switch.resolve("match_photos", "native") == switch.Choice("native", "native", None)
        assert switch.resolve("align_cameras", "native") == switch.Choice("native", "native", None)
    assert not caplog.records
    with caplog.at_level("INFO"):
        for step in classic_only_steps:
            assert switch.resolve(step, "native").fallback == f"the native engine does not do '{step}' yet"
    assert [record.getMessage().split(":")[0] for record in caplog.records] == [
        f"engine 'native' asked for {step}" for step in classic_only_steps]  # one line per step that fell back


def test_a_step_the_native_engine_does_is_its_own_when_asked_for(monkeypatch, caplog):
    """A step named in ``NATIVE_STEPS`` (the next milestones add theirs) goes to the native engine when ``native`` is
    asked for, with no fallback and no line in the log; ``auto`` stays on the classic engine, and the steps not named
    fall back as before."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    monkeypatch.setattr(switch, "NATIVE_STEPS", frozenset({"build_depth_maps"}))
    with caplog.at_level("INFO"):
        c = switch.resolve("build_depth_maps", "native")
        assert (c.requested, c.used, c.fallback) == ("native", "native", None)
        for word in (None, "auto", "classic"):
            c = switch.resolve("build_depth_maps", word)
            assert (c.requested, c.used, c.fallback) == (word or "auto", "classic", None)
    assert not caplog.records
    c = switch.resolve("match_photos", "native")
    assert (c.used, c.fallback) == ("classic", "the native engine does not do 'match_photos' yet")


# ------------------------------------------------------------------------------------------------------ the steps
def test_a_name_that_is_not_a_step_is_refused(monkeypatch, caplog):
    """``resolve`` knows the steps that take an engine and refuses any other name, whatever the engine asked for,
    with the name in the error: a misspelt step stops before its work, where it would otherwise leave a record the
    report never finds."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    with caplog.at_level("INFO"):
        for name in ("align_camera", "optimize_cameras", ""):
            for word in (None, "classic", "native"):
                with pytest.raises(ValueError) as refused:
                    switch.resolve(name, word)
                assert str(refused.value) == (f"unknown step '{name}': the steps that take an engine are "
                                              "match_photos, align_cameras and build_depth_maps")
    assert not caplog.records  # refused before the fallback of ``native`` is thought of


def test_the_steps_are_written_once():
    """``STEPS`` names the steps that take an engine, by their method of the chunk. The methods the report looks for
    (``ENGINE_METHODS``) are those and no other, each of them is a method of the chunk that takes an ``engine``, and
    the steps the native engine does (``NATIVE_STEPS``) are some of them."""
    from pyarchinit_photogrammetry.io import report_content as rc

    assert switch.STEPS == ("match_photos", "align_cameras", "build_depth_maps")
    assert sorted(method for methods in rc.ENGINE_METHODS.values() for method in methods) == sorted(switch.STEPS)
    for name in switch.STEPS:
        method = getattr(Chunk, name, None)
        assert inspect.isfunction(method) and "engine" in inspect.signature(method).parameters, name
    assert switch.AUTO_NATIVE_STEPS <= switch.NATIVE_STEPS <= set(switch.STEPS)


# ------------------------------------------------------------------------------------------- what the chunk keeps
def test_a_chunk_records_the_engine_it_used(monkeypatch, idle_engine):
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    chunk = Chunk(key=0, label="x")
    idle_engine.reason = "the native core (ppg_core) is not installed"
    chunk.match_photos(engine="native")
    idle_engine.reason = None
    record = chunk.meta["engine"]["match_photos"]
    assert sorted(record) == ["fallback", "requested", "used"]
    assert (record["requested"], record["used"]) == ("native", "classic")
    assert isinstance(record["fallback"], str) and record["fallback"]
    assert chunk.align_cameras() == 0
    assert chunk.meta["engine"]["align_cameras"] == {"requested": "auto", "used": "classic", "fallback": None}
    assert chunk.meta["engine"]["match_photos"] == record  # each step has its own record
    chunk.build_depth_maps(engine=" Classic ")
    assert chunk.meta["engine"]["build_depth_maps"] == {"requested": "classic", "used": "classic", "fallback": None}
    assert idle_engine == list(switch.STEPS)
    monkeypatch.setenv("PPG_ENGINE", "native")  # without the argument, the variable
    chunk.align_cameras()
    assert chunk.meta["engine"]["align_cameras"] == {"requested": "native", "used": "native", "fallback": None}


def test_the_record_is_of_a_step_that_finished(monkeypatch, idle_engine):
    """An unknown word stops a step before it starts, and a step that fails leaves the record of the last one that
    ended: what the chunk keeps is the engine of the last run of each method that ended, not of what it holds."""
    from pyarchinit_photogrammetry.engine import dense, native_match, sfm

    monkeypatch.delenv("PPG_ENGINE", raising=False)
    chunk = Chunk(key=0, label="x")
    for name in switch.STEPS:
        with pytest.raises(ValueError, match="auto, classic or native"):
            getattr(chunk, name)(engine="colmap")
    assert idle_engine == [] and "engine" not in chunk.meta
    for name in switch.STEPS:
        getattr(chunk, name)(engine="classic")
    kept = {name: dict(chunk.meta["engine"][name]) for name in switch.STEPS}

    def fails(chunk, **kwargs):
        raise RuntimeError("the step failed")

    monkeypatch.setattr(sfm, "match_photos", fails)
    monkeypatch.setattr(native_match, "match_photos", fails)
    monkeypatch.setattr(sfm, "align_cameras", fails)
    monkeypatch.setattr(dense, "build_depth_maps", fails)
    for name in switch.STEPS:
        with pytest.raises(RuntimeError, match="the step failed"):
            getattr(chunk, name)(engine="native")
    assert chunk.meta["engine"] == kept


def test_an_unknown_word_in_the_variable_stops_the_three_steps(monkeypatch, idle_engine):
    """Without the argument the word is the variable's: an unknown one there stops each of the three steps before it
    starts, with the variable and the word named, and leaves no record."""
    monkeypatch.setenv("PPG_ENGINE", "colmap")
    chunk = Chunk(key=0, label="x")
    for name in switch.STEPS:
        with pytest.raises(ValueError) as refused:
            getattr(chunk, name)()
        assert str(refused.value) == "unknown engine 'colmap' in PPG_ENGINE: use auto, classic or native"
    assert idle_engine == [] and "engine" not in chunk.meta


@pytest.mark.parametrize("held", ["openmvs", None, ["x"], 3], ids=["str", "None", "list", "int"])
@pytest.mark.parametrize("name", switch.STEPS)
def test_a_step_bears_whatever_is_under_the_key(name, held, monkeypatch, caplog, idle_engine):
    """``meta["engine"]`` was a name a plugin or a script could use before the program took it, and a project file
    can hold anything under it. A step that has done its work does not fail on it: what is there and is not the
    engines of the steps is replaced by them, with one warning that names its type and not its value (none for
    ``None``: nothing is lost). The alignment goes on to what comes after its record, the new frame of the chunk."""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    chunk = Chunk(key=0, label="x")
    chunk.meta.update(engine=held, alignment_id="before", levelled=True)
    with caplog.at_level("INFO"):
        result = getattr(chunk, name)()
    assert result == (0 if name == "align_cameras" else None) and idle_engine == [name]
    used = "native" if name in switch.AUTO_NATIVE_STEPS else "classic"  # the matching: native since its gate
    assert chunk.meta["engine"] == {name: {"requested": "auto", "used": used, "fallback": None}}
    warned = [line for line in caplog.records if line.name == switch.__name__]
    if held is None:
        assert warned == []
    else:
        assert [line.levelname for line in warned] == ["WARNING"]
        said = warned[0].getMessage()
        assert said == ('meta["engine"] keeps the engines of the steps and held a value of type '
                        f'{type(held).__name__}: it was replaced')
        assert str(held) not in said and repr(held) not in said
    if name == "align_cameras":
        assert chunk.meta["alignment_id"] != "before" and "levelled" not in chunk.meta


def test_a_method_records_no_engine_it_did_not_run(monkeypatch, idle_engine):
    """The depth maps call the classic engine and no other. With the step named in ``NATIVE_STEPS`` before its method
    calls the native engine, ``native`` stops the method - the step and the engine named, nothing run, nothing
    recorded - where the classic engine would have run under the name of the native one; ``auto`` and ``classic`` run
    the classic engine as they do. (The matching and the alignment call the native engine: the tests after this.)"""
    monkeypatch.delenv("PPG_ENGINE", raising=False)
    for name in switch.STEPS:
        if name in ("match_photos", "align_cameras"):
            continue
        monkeypatch.setattr(switch, "NATIVE_STEPS", frozenset({name}))
        chunk = Chunk(key=0, label="x")
        with pytest.raises(NotImplementedError) as stopped:
            getattr(chunk, name)(engine="native")
        assert str(stopped.value) == f"{name} cannot run on the 'native' engine: it calls only the classic one"
        assert idle_engine == [] and "engine" not in chunk.meta
        for word in (None, "auto", "classic"):
            getattr(chunk, name)(engine=word)
            assert chunk.meta["engine"] == {name: {"requested": word or "auto", "used": "classic", "fallback": None}}
        assert idle_engine == [name] * 3
        with pytest.raises(NotImplementedError):
            getattr(chunk, name)(engine="native")
        assert idle_engine == [name] * 3  # stopped again, and the record is still of the last run that ended
        assert chunk.meta["engine"] == {name: {"requested": "classic", "used": "classic", "fallback": None}}
        idle_engine.clear()


def test_the_matching_runs_the_native_step_or_falls_back(monkeypatch, idle_engine, caplog):
    """``Chunk.match_photos`` with ``native``: the native step runs when nothing keeps it from running
    (:func:`native_match.cannot_run`), and the record says native; a reason not to run it, or a failure of it other
    than a cancel or a full disk, gives the classic step the same arguments and the record keeps the reason; a cancel
    and a full disk come out as they are, nothing recorded. ``auto`` runs the native step as ``native`` does (the
    matching passed its gate), except that a request the native engine does not take goes to the classic step as the
    program's choice, without a reason - and without a line in the log, but for one INFO line when the native core is
    missing, switched off or too old; ``classic`` never asks the native step."""
    import errno

    from pyarchinit_photogrammetry.core.progress import Cancelled
    from pyarchinit_photogrammetry.engine import native_match

    monkeypatch.delenv("PPG_ENGINE", raising=False)
    native_calls: list[dict] = []
    reason = {"value": None}
    outcome = {"raise": None}

    def cannot_run(chunk, matcher, reset):
        return reason["value"]

    def native_step(chunk, **kwargs):
        native_calls.append(kwargs)
        if outcome["raise"] is not None:
            raise outcome["raise"]

    monkeypatch.setattr(native_match, "cannot_run", cannot_run)
    monkeypatch.setattr(native_match, "match_photos", native_step)
    chunk = Chunk(key=0, label="x")
    chunk.match_photos(engine="native", quality="medium", reset=False)
    assert chunk.meta["engine"]["match_photos"] == {"requested": "native", "used": "native", "fallback": None}
    assert idle_engine == [] and len(native_calls) == 1
    assert native_calls[0]["quality"] == "medium" and native_calls[0]["reset"] is False
    assert "matcher" not in native_calls[0]  # the native step matches SIFT points: no matcher to choose
    # a reason not to run it: the classic step, the reason kept
    reason["value"] = "learned matcher 'disk': the native engine matches SIFT points only"
    with caplog.at_level("INFO"):
        chunk.match_photos(engine="native", matcher="disk")
    assert chunk.meta["engine"]["match_photos"] == {"requested": "native", "used": "classic",
                                                    "fallback": reason["value"]}
    assert idle_engine == ["match_photos"] and len(native_calls) == 1
    assert any("learned matcher 'disk'" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)
    # a failure of the native step: the classic one, with the failure as the reason
    reason["value"] = None
    outcome["raise"] = RuntimeError("boom")
    chunk.match_photos(engine="native")
    assert chunk.meta["engine"]["match_photos"] == {"requested": "native", "used": "classic",
                                                    "fallback": "native matching failed: boom"}
    assert idle_engine == ["match_photos"] * 2 and len(native_calls) == 2
    # a cancel and a full disk are not failures of the engine: they come out, nothing runs, nothing is recorded
    kept = dict(chunk.meta["engine"]["match_photos"])
    outcome["raise"] = Cancelled("stop")
    with pytest.raises(Cancelled, match="stop"):
        chunk.match_photos(engine="native")
    outcome["raise"] = OSError(errno.ENOSPC, "No space left on device")
    with pytest.raises(OSError) as full:
        chunk.match_photos(engine="native")
    assert full.value.errno == errno.ENOSPC
    assert idle_engine == ["match_photos"] * 2 and len(native_calls) == 4
    assert chunk.meta["engine"]["match_photos"] == kept
    # any other OSError is a failure of the engine, like the rest
    outcome["raise"] = OSError(errno.EACCES, "Permission denied")
    chunk.match_photos(engine="native")
    assert chunk.meta["engine"]["match_photos"]["fallback"].startswith("native matching failed: ")
    assert idle_engine == ["match_photos"] * 3
    # auto asks the native step, as native does, and falls back the same way on a failure of it
    outcome["raise"] = None
    for word in (None, "auto"):
        chunk.match_photos(engine=word)
        assert chunk.meta["engine"]["match_photos"] == {"requested": "auto", "used": "native", "fallback": None}
    assert len(native_calls) == 7 and idle_engine == ["match_photos"] * 3
    outcome["raise"] = RuntimeError("boom")
    with caplog.at_level("INFO"):
        chunk.match_photos()
    assert chunk.meta["engine"]["match_photos"] == {"requested": "auto", "used": "classic",
                                                    "fallback": "native matching failed: boom"}
    assert any("native matching failed: boom" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)
    assert len(native_calls) == 8 and idle_engine == ["match_photos"] * 4
    # a request the native engine does not take: under auto the classic step, as the program's choice - no reason
    # kept, no line in the log (as auto on a step the native engine does not do); under native the fallback above
    outcome["raise"] = None
    reason["value"] = "learned matcher 'disk': the native engine matches SIFT points only"
    caplog.clear()
    with caplog.at_level("INFO"):
        chunk.match_photos(matcher="disk")
    assert chunk.meta["engine"]["match_photos"] == {"requested": "auto", "used": "classic", "fallback": None}
    assert not caplog.records
    assert len(native_calls) == 8 and idle_engine == ["match_photos"] * 5
    # the native core missing, switched off or too old: the same choice, said in one INFO line (the user can mend it)
    core = "the native core (ppg_core) is not installed"
    reason["value"] = core
    monkeypatch.setattr(native_match, "core_unavailable", lambda: core)
    caplog.clear()
    with caplog.at_level("INFO"):
        chunk.match_photos()
    assert chunk.meta["engine"]["match_photos"] == {"requested": "auto", "used": "classic", "fallback": None}
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [("INFO", f"engine 'auto': classic used ({core})")]
    assert len(native_calls) == 8 and idle_engine == ["match_photos"] * 6
    # classic never asks the native step
    reason["value"] = None
    chunk.match_photos(engine="classic")
    assert chunk.meta["engine"]["match_photos"] == {"requested": "classic", "used": "classic", "fallback": None}
    assert len(native_calls) == 8 and idle_engine == ["match_photos"] * 7


def test_the_alignment_runs_the_native_mapper_or_falls_back(monkeypatch, caplog):
    """``Chunk.align_cameras`` with ``native``: the classic step is asked with ``engine="native"`` (its global branch
    runs the native mapper) when nothing keeps the native mapper from running (:func:`native_map.cannot_run`), and the
    record says native; a reason not to run it gives ``engine="classic"`` and the record keeps the reason with one
    WARNING line; a block the native mapper gave up on (``meta["sfm"]["native_fallback"]``, the classic chain having
    done the alignment) is a fallback too. ``auto`` gives the alignment to the native mapper only once the step is in
    ``AUTO_NATIVE_STEPS`` and the mapper ``auto`` picks is the global one (:func:`native_map.auto_takes`): otherwise the
    classic engine is its choice, without a reason and without a line."""
    from pyarchinit_photogrammetry.engine import native_map, sfm

    monkeypatch.delenv("PPG_ENGINE", raising=False)
    asked: list[str] = []
    state = {"reason": None, "gave_up": None, "takes": False}

    def align(chunk, **kwargs):
        asked.append(kwargs["engine"])
        info = chunk.meta.setdefault("sfm", {})
        info.pop("native_fallback", None)
        if kwargs["engine"] == "native" and state["gave_up"]:
            info["native_fallback"] = state["gave_up"]
        return 0

    monkeypatch.setattr(sfm, "align_cameras", align)
    monkeypatch.setattr(native_map, "cannot_run", lambda chunk, mapper, reset: state["reason"])
    monkeypatch.setattr(native_map, "auto_takes", lambda chunk, mapper: state["takes"])
    chunk = Chunk(key=0, label="x")
    with caplog.at_level("INFO"):
        chunk.align_cameras(engine="native")
    assert asked == ["native"] and not caplog.records
    assert chunk.meta["engine"]["align_cameras"] == {"requested": "native", "used": "native", "fallback": None}
    state["reason"] = "mapper 'incremental' asked"
    with caplog.at_level("INFO"):
        chunk.align_cameras(engine="native", mapper="incremental")
    assert asked[-1] == "classic"
    assert chunk.meta["engine"]["align_cameras"] == {"requested": "native", "used": "classic",
                                                     "fallback": "mapper 'incremental' asked"}
    assert [(r.levelname, r.getMessage()) for r in caplog.records] == [
        ("WARNING", "engine 'native' asked, classic used (mapper 'incremental' asked)")]
    state["reason"], state["gave_up"] = None, "placed 3 of 48 photos"
    chunk.align_cameras(engine="native")
    assert asked[-1] == "native"
    assert chunk.meta["engine"]["align_cameras"] == {"requested": "native", "used": "classic",
                                                     "fallback": "placed 3 of 48 photos"}
    state["gave_up"] = None
    # auto: classic while the step is not in AUTO_NATIVE_STEPS, and while auto_takes says no once it is
    caplog.clear()
    with caplog.at_level("INFO"):
        chunk.align_cameras()
        assert asked[-1] == "classic"
        monkeypatch.setattr(switch, "AUTO_NATIVE_STEPS", frozenset({"match_photos", "align_cameras"}))
        chunk.align_cameras()
        assert asked[-1] == "classic"
        assert chunk.meta["engine"]["align_cameras"] == {"requested": "auto", "used": "classic", "fallback": None}
    assert not caplog.records
    state["takes"] = True
    chunk.align_cameras()
    assert asked[-1] == "native"
    assert chunk.meta["engine"]["align_cameras"] == {"requested": "auto", "used": "native", "fallback": None}
    chunk.align_cameras(engine="classic")
    assert asked[-1] == "classic" and len(asked) == 7


def test_the_engine_is_the_last_argument_and_by_keyword_only():
    """The other arguments of the three methods keep their places (the program and its plugins also give them by
    position): the engine comes after them, by its name."""
    for name in switch.STEPS:
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
        said = plain_words(result.output)
        assert "unknown engine 'colmap': use auto, classic or native" in said, result.output
        assert "--engine" in said, result.output
    monkeypatch.setenv("PPG_ENGINE", "fast")  # the variable is checked like the option, and named
    for command in commands:
        result = run(app, command)
        assert result.exit_code == 2, result.output
        said = plain_words(result.output)
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
    assert kept() == {**dict.fromkeys(switch.STEPS, ("auto", "classic", False)),
                      "match_photos": ("auto", "native", False)}  # the matching: native since its gate
    for command in ("align", "dense"):
        result = run(app, [command, str(project), "--engine", "Native"])
        assert result.exit_code == 0, result.output
    assert kept() == {**dict.fromkeys(switch.STEPS, ("native", "classic", True)),
                      "match_photos": ("native", "native", False), "align_cameras": ("native", "native", False)}
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
    """``ppg process --engine native`` on the trench of the session: the whole chain ends well - the matching on the
    native engine where the native core carries it, on the classic one with the core named as the reason where it
    does not; the alignment likewise (the native mapper, API 16); the depth maps on the classic engine; the project
    keeps what was asked and what was
    used for each of the three steps, and the report says it once for each."""
    from pyarchinit_photogrammetry import _native

    monkeypatch.delenv("PPG_ENGINE", raising=False)
    project = tmp_path / "trench.ppg"
    result = CliRunner().invoke(app, ["process", str(project), str(scene_dir / "photos"),
                                      "--gcp", str(scene_dir / "gcp.csv"), "--crs", "EPSG:3004", "--engine", "native"])
    assert result.exit_code == 0, result.output[-3000:]
    chunk = ppg.Document.open(project).chunk
    assert len(chunk.aligned_cameras) == len(chunk.cameras) > 0
    assert sorted(chunk.meta["engine"]) == sorted(switch.STEPS)
    matching = chunk.meta["engine"]["match_photos"]
    if _native.native_match_kernels() is not None:
        assert matching == {"requested": "native", "used": "native", "fallback": None}
        assert chunk.meta["sfm"]["engine"] == "native"
    else:
        assert (matching["requested"], matching["used"]) == ("native", "classic")
        assert "native core" in matching["fallback"]
    alignment = chunk.meta["engine"]["align_cameras"]
    if _native.native_mapper() is not None:
        assert alignment == {"requested": "native", "used": "native", "fallback": None}
    else:
        assert (alignment["requested"], alignment["used"]) == ("native", "classic")
        assert alignment["fallback"] == ("PPG_NATIVE=0" if not _native.enabled() else  # switched off, or missing
                                         f"the native core with the mapper (API {_native.MAPPER_API}) is not installed")
    record = chunk.meta["engine"]["build_depth_maps"]
    assert (record["requested"], record["used"]) == ("native", "classic")
    assert record["fallback"] == "the native engine does not do 'build_depth_maps' yet"
    report = project.with_suffix(".report.html").read_text(encoding="utf-8")
    assert report.count("Engine used") == 3
    assert report.count("classic (the native engine does not do") == 1
