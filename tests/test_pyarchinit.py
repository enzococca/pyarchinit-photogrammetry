"""Bridge tests on a minimal PyArchInit-compatible SpatiaLite database."""
import sqlite3

import pytest
from shapely.geometry import box

pytestmark = pytest.mark.slow


def test_config_and_read(pa_db):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit, PyArchInitConfig

    cfg = PyArchInitConfig.load()
    assert cfg.sqlite_path == pa_db
    pa = PyArchInit.connect()
    assert [s["sito"] for s in pa.sites()] == ["Saggio"]
    assert pa.us_id("Saggio", "1", "2") == 2
    assert pa.srid() == 3004


def test_publish_everything(pa_db, processed, tmp_path):
    from pyarchinit_photogrammetry.archaeo import sections, terrain
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    _, ch, _ = processed
    pa = PyArchInit.connect()
    r = pa.publish_orthomosaic(ch, "Saggio", "Ortofoto saggio", link_us=[("1", "1"), ("1", "3")])
    assert r["footprint_gid"] == 1
    pa.publish_dem(ch, "Saggio", "DEM saggio")
    pa.publish_model(ch, "Saggio", "Modello saggio", link_us=[("1", "2")])
    x0, y0 = 2480000.0, 4500000.0
    pa.publish_units([UnitPolygon(box(x0 + 1.7, y0 + 1.3, x0 + 2.3, y0 + 1.5), us="2", area="1", tipo="USM")],
                     "Saggio", crs=ch.crs)
    pa.publish_elevations(ch, "Saggio", "1", "2", [(x0 + 2.0, y0 + 1.4)])
    s = sections.section(ch, (x0 + 0.5, y0 + 1.4), (x0 + 3.5, y0 + 1.4), label="A-A'")
    pa.publish_section_trace(s, "Saggio", "A-A'", crs=ch.crs)
    pa.publish_volume("Saggio", "Trincea", terrain.volume(ch.dem_path, reference=99.7))

    con = sqlite3.connect(pa_db)
    count = lambda t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: E731
    assert count("media_table") == count("media_thumb_table") == 3
    assert count("media_to_entity_table") == 3
    assert count("documentazione_table") == 3
    assert {r[0] for r in con.execute("SELECT tipo_documentazione FROM documentazione_table")} == {
        "Fotopiano", "DEM", "Modello tridimensionale"}
    thumbs = [r[0] for r in con.execute("SELECT media_thumb_filename FROM media_thumb_table ORDER BY id_media")]
    assert thumbs == ["1_Ortofoto_saggio_thumb.png", "2_DEM_saggio_thumb.png", "3_Modello_saggio_3d_thumb.png"]
    for t in thumbs:
        assert (tmp_path / "media" / "thumb" / t).exists()
    assert (tmp_path / "media" / "resize" / "3_Modello_saggio.obj").exists()
    assert count("pyunitastratigrafiche") == count("pyarchinit_quote") == count("pyarchinit_sezioni") == 1
    assert count("computo_metrico_table") == 1
    uuids = [r[0] for r in con.execute("SELECT entity_uuid FROM media_table")]
    assert all(len(u) == 36 for u in uuids)
    units = pa.us_polygons("Saggio")
    assert units[0].us == "2" and units[0].geometry.area == pytest.approx(0.12, rel=1e-6)
    assert [m["filename"] for m in pa.media_for("US", 2)] == ["Modello_saggio"]
    q = con.execute("SELECT quota_q FROM pyarchinit_quote").fetchone()[0]
    assert q == pytest.approx(99.95, abs=0.02)
