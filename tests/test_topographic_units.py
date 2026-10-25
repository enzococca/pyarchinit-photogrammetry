"""UT forms use the actual ut_table schema, never excavation US records."""
import sqlite3

import pytest

from pyarchinit_photogrammetry.pyarchinit.bridge import PyArchInit
from pyarchinit_photogrammetry.pyarchinit.database import PyArchInitDB
from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
from shapely.geometry import box


@pytest.fixture
def pa(tmp_path):
    file = tmp_path / "survey.sqlite"
    with sqlite3.connect(file) as conn:
        conn.executescript('''
        CREATE TABLE ut_table (id_ut INTEGER PRIMARY KEY, progetto TEXT, nr_ut INTEGER, ut_letterale TEXT,
                               def_ut TEXT, descrizione_ut TEXT, UNIQUE(progetto,nr_ut,ut_letterale));
        CREATE TABLE us_table (id_us INTEGER PRIMARY KEY, sito TEXT, area TEXT, us TEXT, unita_tipo TEXT);
        ''')
    db = PyArchInitDB(f"sqlite:///{file}")
    yield PyArchInit(db)
    db.engine.dispose()


def test_ut_records_are_separate_and_names_may_be_shared(pa):
    first = pa.create_ut("Survey", "001", "Tombe A", def_ut="Tomba")
    second = pa.create_ut("Survey", 2, "Tombe A")
    assert first != second
    assert pa.next_ut("Survey") == 3
    assert pa.topographic_units("Survey")[0]["nr_ut"] == 1
    assert pa.db.query("us_table") == []
    pa.update_ut(first, descrizione_ut="Ricognizione")
    assert pa.ut_record("Survey", 1, "Tombe A")["descrizione_ut"] == "Ricognizione"
    with pytest.raises(FileExistsError):
        pa.create_ut("Survey", 1, "Tombe A")


@pytest.mark.parametrize("number", ["UT1", "1.5", -1, True])
def test_invalid_number_does_not_write(pa, number):
    with pytest.raises(ValueError):
        pa.create_ut("Survey", number)
    assert pa.topographic_units() == []


def test_ut_cannot_be_written_to_us_records_or_geometry(pa):
    with pytest.raises(ValueError, match="ut_table"):
        pa.create_us("Temple", "1", "1", "UT")
    with pytest.raises(ValueError, match="ut_table"):
        pa.publish_units([UnitPolygon(box(0, 0, 1, 1), us="1", tipo="UT")], "Temple")
    assert pa.db.query("us_table") == []
