"""The weekly look at pycolmap and Open3D (.github/scripts/upstream_check.py, run by .github/workflows/upstream.yml):
the newest versions to try, their public API against the installed ones, the failures of the tests and the summary of
the run. Imported by path; nothing here goes on the network: PyPI answers with fake responses."""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from packaging.tags import Tag

ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = _module("upstream_check", ".github/scripts/upstream_check.py")

TAGS = {Tag("cp312", "cp312", "manylinux_2_28_x86_64"), Tag("py3", "none", "any")}
PYPROJECT = {"project": {"dependencies": ["numpy>=1.24", "pycolmap>=4.2.1,<4.3"],
                         "optional-dependencies": {"mesh": ["open3d>=0.18,!=0.20.0,<0.21"],
                                                   "all": ["open3d>=0.18,!=0.20.0,<0.21", "pye57>=0.4"]}}}


def _wheel(name, version, platform="manylinux_2_28_x86_64", yanked=False):
    return {"filename": f"{name}-{version}-cp312-cp312-{platform}.whl", "yanked": yanked, "packagetype": "bdist_wheel"}


def _pypi(name, releases):
    return {"info": {"name": name}, "releases": releases}


COLMAP = _pypi("pycolmap", {
    "4.2.0": [_wheel("pycolmap", "4.2.0")],
    "4.2.1": [_wheel("pycolmap", "4.2.1")],
    "4.2.2": [_wheel("pycolmap", "4.2.2", yanked=True)],  # withdrawn
    "4.2.3": [{"filename": "pycolmap-4.2.3.tar.gz", "yanked": False, "packagetype": "sdist"}],  # to compile: no
    "4.3.0.dev0": [_wheel("pycolmap", "4.3.0.dev0"), _wheel("pycolmap", "4.3.0.dev0", "macosx_14_0_arm64")],
    "4.3.0rc1": [_wheel("pycolmap", "4.3.0rc1", "macosx_14_0_arm64")],  # no wheel for this runner
})
OPEN3D = _pypi("open3d", {"0.18.0": [_wheel("open3d", "0.18.0")], "0.19.0": [_wheel("open3d", "0.19.0")],
                          "0.20.0": [_wheel("open3d", "0.20.0")]})


def test_the_requirement_is_read_wherever_the_program_names_the_library():
    assert str(check.requirement(PYPROJECT, "pycolmap").specifier) == "<4.3,>=4.2.1"
    assert str(check.requirement(PYPROJECT, "Open3D").specifier) == "!=0.20.0,<0.21,>=0.18"


def test_the_newest_version_beyond_the_installed_one():
    """A pre-release is tried (the warning comes before the release), a version withdrawn, without a wheel for the
    runner or older than the installed one is not."""
    req = check.requirement(PYPROJECT, "pycolmap")
    versions = check.compatible_versions(COLMAP, TAGS)
    assert [str(v) for v in versions] == ["4.2.0", "4.2.1", "4.3.0.dev0"]
    assert str(check.newest(req, "4.2.1", versions, prereleases=True)) == "4.3.0.dev0"
    assert check.newest(req, "4.2.1", versions, prereleases=False) is None
    assert check.newest(req, "4.3.0.dev0", versions, prereleases=True) is None


def test_the_versions_the_program_excludes_are_not_tried():
    """Open3D 0.20.0 crashes in the Poisson reconstruction and the program excludes it: a fix (0.20.1) is tried, and
    being within the range pip users get it already."""
    req = check.requirement(PYPROJECT, "open3d")
    assert check.newest(req, "0.19.0", check.compatible_versions(OPEN3D, TAGS), prereleases=True) is None
    fixed = _pypi("open3d", {**OPEN3D["releases"], "0.20.1": [_wheel("open3d", "0.20.1")]})
    assert str(check.newest(req, "0.19.0", check.compatible_versions(fixed, TAGS), prereleases=True)) == "0.20.1"


def test_the_plan_of_the_run():
    pages = {"pycolmap": COLMAP, "open3d": OPEN3D}
    installed = {"pycolmap": "4.2.1", "open3d": "0.19.0"}
    plan = check.make_plan(["pycolmap", "open3d"], PYPROJECT, pages.__getitem__, installed.__getitem__, TAGS)
    colmap, o3d = plan
    assert (colmap["name"], colmap["installed"], colmap["candidate"]) == ("pycolmap", "4.2.1", "4.3.0.dev0")
    assert colmap["prerelease"] and not colmap["within"] and colmap["requirement"] == "<4.3,>=4.2.1"
    assert o3d["candidate"] is None and o3d["excluded"] == ["0.20.0"]
    assert check.pip_specs(plan) == ["pycolmap==4.3.0.dev0"]
    # a version asked for in the run (workflow_dispatch) is tried as it is, if the runner has a wheel of it
    plan = check.make_plan(["pycolmap"], PYPROJECT, pages.__getitem__, installed.__getitem__, TAGS,
                           asked={"pycolmap": "4.2.0"})
    assert plan[0]["candidate"] == "4.2.0" and plan[0]["asked"] and plan[0]["within"] is False
    try:
        check.make_plan(["pycolmap"], PYPROJECT, pages.__getitem__, installed.__getitem__, TAGS,
                        asked={"pycolmap": "4.3.0rc1"})
    except ValueError as exc:
        assert "4.3.0rc1" in str(exc) and "wheel" in str(exc)
    else:
        raise AssertionError("a version without a wheel for the runner was accepted")


def test_the_public_api_of_a_library(monkeypatch):
    """The public names of the modules and the members of their classes; the private ones are left out."""
    lib = types.ModuleType("fakelib")
    lib.has_cuda = False
    lib.match_exhaustive = lambda: None

    class Options:
        max_image_size = 3200
        _private = 1

        def check(self):
            return True

    lib.Options = Options
    lib._hidden = 1
    monkeypatch.setitem(sys.modules, "fakelib", lib)
    names = check.api_names(["fakelib"])
    assert names == sorted(names)
    assert {"fakelib.has_cuda", "fakelib.match_exhaustive", "fakelib.Options", "fakelib.Options.max_image_size",
            "fakelib.Options.check"} <= set(names)
    assert not [n for n in names if "_private" in n or "_hidden" in n or "__" in n]


def test_the_report_says_what_changes_and_what_to_do(tmp_path):
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "sfm.py").write_text("import pycolmap\n\next = pycolmap.FeatureExtractionOptions()\n"
                                "ext.max_image_size = 3200\npycolmap.match_exhaustive('db')\n", encoding="utf-8")
    plan = [{"name": "pycolmap", "requirement": "<4.3,>=4.2.1", "installed": "4.2.1", "candidate": "4.3.0.dev0",
             "prerelease": True, "within": False, "asked": False, "excluded": []},
            {"name": "open3d", "requirement": "!=0.20.0,<0.21,>=0.18", "installed": "0.19.0", "candidate": None,
             "prerelease": False, "within": False, "asked": False, "excluded": ["0.20.0"]}]
    before = {"pycolmap": ["pycolmap.FeatureExtractionOptions", "pycolmap.FeatureExtractionOptions.max_image_size",
                           "pycolmap.has_cuda", "pycolmap.match_exhaustive", "pycolmap.old_unused"]}
    after = {"pycolmap": ["pycolmap.FeatureExtractionOptions", "pycolmap.FeatureExtractionOptions.max_side",
                          "pycolmap.has_cuda", "pycolmap.has_metal", "pycolmap.match_exhaustive"]}
    text = check.report(plan, before, after, {"status": 1, "failed": ["tests/test_sfm.py::test_align"],
                                              "flaky": ["tests/test_ortho.py::test_tiles"]}, tmp_path / "src")
    assert "4.3.0.dev0" in text and "pre-release" in text and "0.20.0" in text
    removed = text.split("removes", 1)[1]
    assert "pycolmap.FeatureExtractionOptions.max_image_size" in removed and "pkg/sfm.py:4" in removed
    assert "pycolmap.old_unused" in removed and "not used by the program" in removed
    assert "pycolmap.has_metal" in text and "GPU" in text  # a new back-end flag is pointed out
    assert "tests/test_sfm.py::test_align" in text and "tests/test_ortho.py::test_tiles" in text
    assert "the ceiling keeps" in text  # beyond the ceiling and failing: pip users are safe, adapt before raising
    passed = check.report(plan, before, before, {"status": 0, "failed": [], "flaky": []}, tmp_path / "src")
    assert "raise the ceiling" in passed and "pyproject.toml" in passed
    within = [dict(plan[0], candidate="4.2.2", prerelease=False, within=True)]
    failing = check.report(within, before, before, {"status": 1, "failed": ["tests/test_sfm.py::test_align"],
                                                    "flaky": []}, tmp_path / "src")
    assert "pip users get it already" in failing and "!=4.2.2" in failing
    untested = check.report(plan, before, before, None, tmp_path / "src")
    assert "not tested in this run" in untested and "the ceiling keeps" not in untested
    nothing = check.report([plan[1]], {}, {}, None, tmp_path / "src")
    assert "nothing newer" in nothing.lower()


def test_a_gpu_back_end_among_the_names():
    """A flag of the module itself or a GPU word: not the many ``has_<x>`` methods of the classes."""
    modules = ["pycolmap"]
    assert check._gpu("pycolmap.has_metal", modules) and check._gpu("pycolmap.SiftOptions.use_gpu", modules)
    assert check._gpu("pycolmap.MetalOptions", modules) and check._gpu("pycolmap.Device.CUDA", modules)
    assert check._gpu("open3d.core.sycl", ["open3d.core"]) and check._gpu("pycolmap.num_gpus", modules)
    assert not check._gpu("pycolmap.Image.has_pose", modules) and not check._gpu("pycolmap.dumps", modules)
    assert not check._gpu("pycolmap.SiftExtractionOptions.max_num_features", modules)


def test_the_failures_are_read_from_the_summary_of_pytest():
    log = ("....F..\n=========================== short test summary info ============================\n"
           "FAILED tests/test_sfm.py::test_align - AssertionError: 4 mm\n"
           "ERROR tests/test_gui.py::test_window[dark] - RuntimeError: no display\n"
           "FAILED tests/test_ortho.py::test_tiles\n1 failed, 2 errors in 3.2s\n")
    assert check.failures(log) == ["tests/test_sfm.py::test_align", "tests/test_gui.py::test_window[dark]",
                                   "tests/test_ortho.py::test_tiles"]
    assert check.failures("12 passed in 1.0s\n") == []


def test_the_newest_rustgis_is_one_section_of_the_report(tmp_path, capsys):
    plan = [{"name": "pycolmap", "requirement": "<4.3,>=4.2.1", "installed": "4.2.1", "candidate": None,
             "prerelease": False, "within": False, "asked": False, "excluded": []}]
    log = "..F\nFAILED tests/test_rustgis_link.py::test_real_engine_round_trip - AssertionError\n"
    failed = check.rustgis_result("v0.4.1", "tests", 1, log)
    assert failed["failed"] == ["tests/test_rustgis_link.py::test_real_engine_round_trip"]
    text = check.report(plan, {}, {}, None, tmp_path, failed)
    assert "nothing newer" in text.lower()  # the section is there when the libraries have nothing to try too
    assert "## RustGIS" in text and "RustGIS v0.4.1: 1 test of the live link fails" in text
    assert "test_real_engine_round_trip" in text
    two = check.rustgis_result("v0.4.1", "tests", 1, log + "ERROR tests/test_rustgis_link.py::test_other - boom\n")
    assert "2 tests of the live link fail with" in check.report(plan, {}, {}, None, tmp_path, two)
    ok = check.report(plan, {}, {}, None, tmp_path, check.rustgis_result("v0.4.1", "tests", 0, ""))
    assert "pass with its `rustgis-cli`" in ok
    fetch = check.report(plan, {}, {}, None, tmp_path, check.rustgis_result("v0.5.0", "fetch", 1, "ignored"))
    assert "could not be taken" in fetch and "v0.5.0" in fetch
    assert "RustGIS" not in check.report(plan, {}, {}, None, tmp_path)  # without a result, nothing about it
    # the command line: the result goes through a file, and a failure makes the run red like pycolmap and Open3D
    (tmp_path / "plan.json").write_text('[]', encoding="utf-8")
    (tmp_path / "rg.log").write_text(log, encoding="utf-8")
    for stage, status, red in (("tests", 1, 1), ("tests", 0, 0), ("fetch", 1, 1)):
        out = tmp_path / f"rg-{stage}-{status}.json"
        assert check.main(["rustgis", "--tag", "v0.4.1", "--stage", stage, "--status", str(status),
                           "--log", str(tmp_path / "rg.log"), "--out", str(out)]) == 0
        assert check.main(["report", "--plan", str(tmp_path / "plan.json"), "--rustgis", str(out)]) == red
        capsys.readouterr()
        assert check.main(["rustgis-report", str(out)]) == red  # when the plan was not made: that section alone
        assert capsys.readouterr().out.startswith("## RustGIS")
