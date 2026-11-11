"""The record of the rows of the live GeoPackage (``ppg_rows``) is written entry by entry: a write or a read that
changed no row leaves the file as it is, and one edit made in QGIS costs a few rows, not the whole record. The
warnings and the errors of the Log pane, and what came from QGIS, are in the log file too."""
from __future__ import annotations

import logging
import sqlite3

import pytest

from shapely.geometry import LineString

from pyarchinit_photogrammetry.io.qgis_link import ROWS_TABLE, LiveLink
from test_plan_two_way import LAYER, _by_n, _line
from test_qgis_two_way import Qgis, X0, Y0, _rows


@pytest.fixture
def qt_app(tmp_path):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    return app


def _layers(n):
    return {LAYER: {"kind": "line", "features": [{"_geom": ("LineString", _line(i)), "n": i} for i in range(n)]}}


def _version(con):
    return con.execute("PRAGMA data_version").fetchone()[0]


def test_a_write_or_a_read_that_changes_no_row_leaves_the_file_as_it_is(tmp_path):
    layers = _layers(300)
    link = LiveLink(tmp_path, "EPSG:3004")
    link.write(layers)
    assert link.read_changes() == {}
    other = sqlite3.connect(link.path)
    before, record = _version(other), other.execute(f"SELECT * FROM {ROWS_TABLE} ORDER BY 1, 2, 3").fetchall()
    assert len([r for r in record if r[0] == "linee"]) == 300
    assert link.read_changes() == {}
    link.write(layers, force=True)  # every table compared row by row
    assert link.read_changes() == {}
    assert _version(other) == before  # nothing committed: nothing for QGIS to read again
    assert other.execute(f"SELECT * FROM {ROWS_TABLE} ORDER BY 1, 2, 3").fetchall() == record
    other.close()
    link.close()


def test_one_edit_in_qgis_costs_a_few_rows_of_the_record(tmp_path):
    layers = _layers(300)
    link = LiveLink(tmp_path, "EPSG:3004")
    link.write(layers)
    rows = _by_n(_rows(link.path, "linee"))
    qgis = Qgis(link.path)
    qgis.save(qgis.delete("linee", rows[7]["fid"]),
              qgis.insert("linee", LineString([(X0, Y0), (X0 + 1, Y0)]), layer=LAYER))
    con = link._connection()
    start = con.total_changes
    got = link.read_changes()["linee"]
    assert len(got.deleted) == 1 and len(got.added) == 1
    assert con.total_changes - start < 10  # the row drawn there signed, two entries of the record: not 300
    record = {(r[1], r[2]): r[3] for r in con.execute(f"SELECT * FROM {ROWS_TABLE} WHERE name = 'linee'")}
    assert len(record) == 300 and (rows[7]["ppg_id"], 1) not in record and (got.added[0].id, 1) in record
    assert link.read_changes() == {}
    qgis.close()
    link.close()


def test_the_warnings_of_the_pane_are_in_the_log_file_once(qt_app):
    from pyarchinit_photogrammetry.gui import panels

    seen = []

    class Keep(logging.Handler):
        def emit(self, record):
            seen.append((record.levelno, record.getMessage()))

    keep = Keep()
    root = logging.getLogger("pyarchinit_photogrammetry")
    root.addHandler(keep)
    pane = panels.LogPane()
    try:
        pane.warning("layer not read")
        pane.error("broken", "trace")
        pane.info("only here")
        logging.getLogger("pyarchinit_photogrammetry.gui").info("from the logger")
        assert seen == [(logging.WARNING, "layer not read"), (logging.ERROR, "broken\ntrace"),
                        (logging.INFO, "from the logger")]
        assert [e[2] for e in pane.entries] == ["layer not read", "broken", "only here", "from the logger"]
    finally:
        root.removeHandler(keep)
        root.removeHandler(pane.handler)
