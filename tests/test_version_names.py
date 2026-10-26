"""The names the program gives to versions follow the language of the interface."""
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core import versions
from pyarchinit_photogrammetry.gui import i18n


@pytest.fixture
def chunk(tmp_path):
    doc = ppg.Document()
    ch = doc.add_chunk("Prova")
    doc.save(tmp_path / "p.ppg")
    dem = ch.data_dir / "dem.tif"
    dem.write_bytes(b"not read")
    ch._set_product("dem", dem)
    ch.meta["dem"] = {"classes": [2]}
    return ch


def test_without_a_window_the_names_stay_italian(chunk, monkeypatch):
    monkeypatch.setattr(versions, "_translator", None)
    info = versions.set_active_name(chunk, "dem")
    assert info["name"].startswith("DEM del ") and info["name"].endswith("(classi 2)")
    assert versions.label("Importato da {0}", "a.ply") == "Importato da a.ply"


def test_in_english_the_names_are_english(chunk, monkeypatch):
    monkeypatch.setattr(versions, "_translator", None)
    i18n.set_current("en")
    try:
        versions.set_translator(lambda text, *args: i18n.tr(text, *args) if args else i18n.translate(text))
        info = versions.set_active_name(chunk, "dem")
        assert info["name"].startswith("DEM of ") and info["name"].endswith("(classes 2)"), info["name"]
        chunk.meta["point_cloud_source"] = "mappe di profondita' (qualita' medium)"
        chunk.meta["dense"] = {}
        assert versions.label(str(chunk.meta["point_cloud_source"])) == "depth maps (quality medium)"
        assert versions.label("Importato da {0}", "a.ply") == "Imported from a.ply"
        assert versions.label("Tagliato: fetta") == "Cut: slab"
    finally:
        i18n.set_current("it")
        versions.set_translator(None)
