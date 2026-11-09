"""Pictures of the guide of the *Flight plans* plugin (``docs/plugins/flightplan*.md``), in English and in Italian.

    QT_QPA_PLATFORM=offscreen PPG_RENDERER=software python tools/docs_flightplan.py [FOLDER]

The survey is made up: a small tell of 20 m on a gentle plain, a DEM and a shaded orthophoto written with numpy
in a project of its own (``FOLDER/rilievo.ppg``, made when missing; default ``/Users/Shared/ppg_demo/flightplan_docs``)
around the neutral place of the plugin's tests (500000, 4600000 in EPSG:32632). No network: the terrain comes from
the project DEM only. The plugin must be installed in the Python that runs the script. Every picture is written
twice: ``<name>.png`` in English and ``<name>.it.png`` in Italian (mkdocs-static-i18n)."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "img" / "plugins" / "flightplan"
SIZE = (1400, 860)
X0, Y0 = 500000.0, 4600000.0          # the neutral place of the plugin's tests
HALF, CELL = 450.0, 1.0               # the DEM: 900 m around (X0, Y0)
ORTHO_CELL = 0.5
TELL = (X0 + 20.0, Y0 + 15.0)
AREA = [(-175, -130), (165, -150), (195, 95), (40, 165), (-160, 120)]   # around the tell, metres from (X0, Y0)
TAKEOFF = (-215.0, -165.0)
TARGETS = [("T1", -140, -100), ("T2", 140, -115), ("T3", 160, 70), ("T4", 30, 130), ("T5", -130, 90),
           ("T6", 20, 15)]


# ---------------------------------------------------------------- the made-up survey
def ground(x, y):
    """A tell of 20 m with a flat top and a gully on its east side, on a plain rising gently to the north-east."""
    dx, dy = (x - TELL[0]) / 120.0, (y - TELL[1]) / 90.0
    r = np.hypot(dx, dy)
    tell = 20.0 / (1.0 + np.exp((r - 0.75) * 7.0))
    gully = 3.0 * np.exp(-((y - TELL[1] + 0.25 * (x - TELL[0])) / 9.0) ** 2) * (x > TELL[0]) * np.clip(r - 0.3, 0, 1)
    plain = 150.0 + 0.012 * (x - X0) + 0.02 * (y - Y0)
    rng = np.random.default_rng(7)
    waves = sum(a * np.sin((x - X0) / s + p) * np.cos((y - Y0) / (s * 1.3) + p)
                for a, s, p in zip(rng.uniform(0.2, 0.6, 5), rng.uniform(15, 70, 5), rng.uniform(0, 6, 5)))
    return plain + tell - gully + waves


def make_project(folder: Path) -> Path:
    import rasterio
    from rasterio.transform import from_origin

    import pyarchinit_photogrammetry as ppg

    path = folder / "rilievo.ppg"
    if path.exists():
        return path
    folder.mkdir(parents=True, exist_ok=True)
    doc = ppg.Document()
    ch = doc.add_chunk("Collina")
    doc.chunk = ch
    doc.save(path)
    ch.crs = "EPSG:32632"
    n = int(2 * HALF / CELL)
    xs = X0 - HALF + (np.arange(n) + 0.5) * CELL
    ys = Y0 + HALF - (np.arange(n) + 0.5) * CELL
    gx, gy = np.meshgrid(xs, ys)
    z = ground(gx, gy).astype("float32")
    dem = ch.data_dir / "dem.tif"
    with rasterio.open(dem, "w", driver="GTiff", width=n, height=n, count=1, dtype="float32", crs=ch.crs,
                       transform=from_origin(X0 - HALF, Y0 + HALF, CELL, CELL), nodata=float("nan"),
                       compress="deflate") as ds:
        ds.write(z, 1)
    ch._set_product("dem", dem)
    # the orthophoto: soil, fields and a track, shaded by the DEM
    m = int(2 * HALF / ORTHO_CELL)
    xs = X0 - HALF + (np.arange(m) + 0.5) * ORTHO_CELL
    ys = Y0 + HALF - (np.arange(m) + 0.5) * ORTHO_CELL
    gx, gy = np.meshgrid(xs, ys)
    z = ground(gx, gy)
    zy, zx = np.gradient(z, ORTHO_CELL)
    az, alt = np.radians(315), np.radians(45)
    slope = np.arctan(np.hypot(zx, zy))
    aspect = np.arctan2(-zx, zy)
    shade = np.clip(np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect), 0, 1)
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 1, (m // 8, m // 8))
    import cv2

    noise = cv2.resize(cv2.GaussianBlur(noise, (0, 0), 1.5), (m, m), interpolation=cv2.INTER_CUBIC)
    fine = cv2.GaussianBlur(rng.normal(0, 1, (m, m)), (0, 0), 1.0)
    field = ((gx - X0) * 0.8 + (gy - Y0) * 0.6) // 70 % 3          # strips of fields of three tints
    tints = np.array([[176, 150, 104], [150, 152, 96], [190, 168, 120]], float)
    rgb = tints[field.astype(int)]
    on_tell = 1.0 / (1.0 + np.exp(-(z - (150.0 + 0.012 * (gx - X0) + 0.02 * (gy - Y0)) - 4.0)))
    rgb = rgb * (1 - on_tell[..., None]) + np.array([205, 182, 140]) * on_tell[..., None]
    track = np.abs((gy - Y0) - 0.35 * (gx - X0) + 185.0) < 2.5
    rgb[track] = [215, 200, 170]
    rgb = rgb * (0.55 + 0.6 * shade[..., None]) + (8 * noise + 6 * fine)[..., None]
    rgb = np.clip(rgb, 0, 255).astype("uint8")
    ortho = ch.data_dir / "orthomosaic.tif"
    with rasterio.open(ortho, "w", driver="GTiff", width=m, height=m, count=4, dtype="uint8", crs=ch.crs,
                       transform=from_origin(X0 - HALF, Y0 + HALF, ORTHO_CELL, ORTHO_CELL), compress="deflate",
                       photometric="RGB", alpha="YES") as ds:
        for b in range(3):
            ds.write(rgb[..., b], b + 1)
        ds.write(np.full((m, m), 255, "uint8"), 4)
        ds.build_overviews([2, 4, 8, 16])
    ch._set_product("orthomosaic", ortho)
    rows = ["nome,est,nord,quota"]
    for name, dx, dy in TARGETS:
        rows.append(f"{name},{X0 + dx:.2f},{Y0 + dy:.2f},{float(ground(X0 + dx, Y0 + dy)):.2f}")
    (folder / "target.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    doc.save()
    return path


# ---------------------------------------------------------------- driver
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", nargs="?", type=Path, default=Path("/Users/Shared/ppg_demo/flightplan_docs"))
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--lang", nargs="+", default=["en", "it"])
    ap.add_argument("--worker", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.worker:
        return _worker(args)
    make_project(args.folder)
    args.out.mkdir(parents=True, exist_ok=True)
    for lang in args.lang:
        home = Path(tempfile.mkdtemp(prefix="ppg-docs-home-"))
        screen = home / "screen.json"
        screen.write_text(json.dumps({"screens": [{"name": "docs", "x": 0, "y": 0, "width": 2560, "height": 2048,
                                                   "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}))
        env = dict(os.environ, PPG_LANG=lang, HOME=str(home), USERPROFILE=str(home), PPG_NO_UPDATE_CHECK="1",
                   PPG_PLUGINS_DIR=str(home / "plugins"), PPG_PLUGINS_ENVIRONMENT="0", PPG_NO_PLUGINS="1",
                   PPG_GPU="off", QT_QPA_PLATFORM=f"offscreen:configfile={screen}",
                   PPG_RENDERER=os.environ.get("PPG_RENDERER", "software"))
        try:
            subprocess.run([sys.executable, __file__, str(args.folder), "--out", str(args.out), "--worker", lang],
                           check=True, env=env)
        finally:
            shutil.rmtree(home, ignore_errors=True)
    return 0


def _worker(args) -> int:  # noqa: C901 - one picture after the other
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtGui import QFont
    from PySide6.QtWidgets import QApplication

    lang, out = args.worker, args.out
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication.instance() or QApplication([sys.argv[0]])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, os.path.join(os.environ["HOME"], "s"))
    app.setApplicationName("pyarchinit-photogrammetry")
    app.setOrganizationName("pyarchinit")
    app.setFont(QFont("Inter", 10))
    from pyarchinit_photogrammetry.app import app as ppg_app
    from pyarchinit_photogrammetry.core.document import Document
    from pyarchinit_photogrammetry.gui import dialogs, i18n, install_translations, theme
    from pyarchinit_photogrammetry.plugins import Context

    if not i18n.install(app):
        install_translations(app)
    theme.apply(app, "light")
    from pyarchinit_photogrammetry.gui import main_window as mwmod
    from ppg_flightplan import FlightPlanPlugin, project

    def pump(ms: int = 200) -> None:
        end = time.monotonic() + ms / 1000
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)

    def wait(w) -> None:
        t0 = time.monotonic()
        while w.runner.busy and time.monotonic() - t0 < 600:
            app.processEvents()
            time.sleep(0.02)
        pump(300)

    def save(img, name: str, jpeg: bool = False) -> None:
        from PIL import Image

        suffix = ".jpg" if jpeg else ".png"
        path = out / (f"{name}{suffix}" if lang == "en" else f"{name}.{lang}{suffix}")
        tmp = path.with_suffix(".tmp.png")
        img.save(str(tmp), "PNG")
        im = Image.open(tmp).convert("RGB")
        if jpeg:
            im.save(path, "JPEG", quality=85, optimize=True)
        else:
            im.quantize(colors=256, method=Image.Quantize.MEDIANCUT,
                        dither=Image.Dither.FLOYDSTEINBERG).save(path, "PNG", optimize=True)
        tmp.unlink()
        print(f"  {path.name}  {im.size[0]}x{im.size[1]}")

    said = []
    dialogs.message = lambda *a, **k: said.append(a[3] if len(a) > 3 else a)
    dialogs.choice = lambda *a, **k: 0
    mwmod.message = dialogs.message
    project_path = args.folder / "rilievo.ppg"
    doc = Document.open(project_path)
    ch = doc.chunk
    shutil.rmtree(ch.data_dir / "flightplan", ignore_errors=True)
    ch.meta.pop("flightplan", None)
    ppg_app.document = doc
    w = mwmod.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    errors = []
    w._error = lambda *a, **k: errors.append(a)
    w.resize(*SIZE)
    w.show()
    pump(500)

    feed = {"xy": None}

    def request(kind, on_finished, on_cancelled=None, hint=""):
        on_finished(np.asarray(feed["xy"], float))
        return True

    w.request_geometry = request
    plugin_home = Path(os.environ["HOME"]) / "plugin"
    plugin = FlightPlanPlugin()
    plugin.activate(Context("flightplan", plugin_home, window=w))
    plugin._open_path = lambda *a: str(args.folder / "target.csv")
    plugin._ask = lambda *a, **k: {"epsg": "32632"}
    w._show_raster("ortho")
    plugin.show_pane()
    pump(500)
    p = plugin.pane
    dock = w.panes.groups["right"].dock(p)
    w.resizeDocks([dock], [420], Qt.Orientation.Horizontal)
    from PySide6.QtWidgets import QDockWidget

    for d in w.findChildren(QDockWidget):  # the panes under the view (photos, console...): room for the map
        if w.dockWidgetArea(d) == Qt.DockWidgetArea.BottomDockWidgetArea:
            d.close()
    pump(300)

    # 1-3: the area, the camera, the parameters, and the plan
    feed["xy"] = [(X0 + dx, Y0 + dy) for dx, dy in AREA]
    plugin.draw_area()
    p.s_margin.setValue(15)
    p.c_goal.setCurrentIndex(0)
    p.k_high.setChecked(False)
    p.s_value.setValue(3.0)
    p.s_front.setValue(75)
    p.c_mode.setCurrentIndex(p.c_mode.findData("per_punto"))
    p.b_design.click()
    wait(w)
    feed["xy"] = [(X0 + TAKEOFF[0], Y0 + TAKEOFF[1])]
    p.b_takeoff.click()
    pump(300)
    # 5: follow the terrain on the project DEM
    p.c_source.setCurrentIndex(p.c_source.findData("dem"))
    p.b_terrain.click()
    wait(w)
    # 7: the targets
    p.b_csv.click()
    wait(w)
    assert not errors, errors
    e = project.active_plan(ch)
    assert e is not None and e["piano"]["blocchi"][0].get("quote"), "the plan does not follow the terrain"

    view = w.ortho_view

    def frame() -> None:
        from ppg_flightplan.texts import LAYER_AREA

        view.zoom_to(view.layer_rect(LAYER_AREA), margin=0.12)
        pump(600)

    def scroll_pane_to(key: str) -> None:
        from PySide6.QtWidgets import QScrollArea

        sa = p.findChild(QScrollArea)
        sa.verticalScrollBar().setValue(max(0, p.titles[key].y() - 4))
        pump(200)

    # the plan on the orthophoto, the pane at the plan's table
    frame()
    scroll_pane_to("plan")
    save(w.grab(), "plan-view")

    # the pane: steps 1-4 and 5-9, taken whole from its scroll area
    from PySide6.QtWidgets import QScrollArea

    sa = p.findChild(QScrollArea)
    body = sa.widget()
    body.resize(410, body.sizeHint().height())
    body.adjustSize()
    pump(200)
    whole = body.grab().toImage()
    cut = p.titles["terrain"].y() - 4
    save(whole.copy(0, 0, whole.width(), cut), "pane-design")

    # 6: the coverage
    p.s_low.setValue(9)
    p.b_coverage.click()
    wait(w)
    p.b_show_coverage.click()
    pump(500)
    from ppg_flightplan.texts import LAYER_SHOTS

    view.set_layer_visible(LAYER_SHOTS, False)  # the cells under the dots of the shots
    frame()
    scroll_pane_to("terrain")
    save(w.grab(), "coverage")
    body.resize(410, body.sizeHint().height())
    pump(200)
    whole = body.grab().toImage()
    cut = p.titles["terrain"].y() - 4
    end = p.titles["export"].y() - 4
    save(whole.copy(0, cut, whole.width(), end - cut), "pane-terrain")

    # 5: the heights along a strip that crosses the tell
    _profile(ch, e["piano"], lang, out / ("terrain-profile.jpg" if lang == "en" else f"terrain-profile.{lang}.jpg"))
    assert not errors, errors
    w.setWindowModified(False)
    w.close()
    return 0


def _profile(ch, piano, lang, path):
    """The ground along the strip that crosses the top of the tell, the flight at one height, the flight that
    follows the terrain."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from ppg_flightplan import plan as pl
    from ppg_flightplan import project

    b = pl.active_blocks(piano)[0]
    route, _ = pl.route_of_block(b, piano)
    xy = project.to_chunk(ch, list(route.points))
    heights = np.array([route.height_at(i) for i in range(len(route.points))])
    takeoff = pl.plan_takeoff(piano)
    (tx, ty), = project.to_chunk(ch, [takeoff])
    z0 = float(ground(tx, ty))
    # the strips: runs of points along the same direction; the one nearest the top of the tell
    d = np.diff(xy, axis=0)
    seg = np.hypot(*d.T)
    breaks = np.where(seg > 1.5 * np.median(seg))[0]
    runs = np.split(np.arange(len(xy)), breaks + 1)
    runs = [r for r in runs if len(r) > 5]
    best = min(runs, key=lambda r: np.hypot(*(xy[r] - TELL).T).min())
    pts, h = xy[best], heights[best]
    s = np.r_[0, np.cumsum(np.hypot(*np.diff(pts, axis=0).T))]
    dense = np.linspace(0, s[-1], 400)
    gx, gy = np.interp(dense, s, pts[:, 0]), np.interp(dense, s, pts[:, 1])
    it = lang == "it"
    fig, ax = plt.subplots(figsize=(10.4, 4.6), dpi=100)
    ax.fill_between(dense, ground(gx, gy), ground(gx, gy).min() - 5, color="#c8b48c", alpha=0.6)
    ax.plot(dense, ground(gx, gy), color="#7a5a2a", lw=1.5, label="Terreno (DEM del progetto)" if it else
            "Ground (project DEM)")
    ax.plot(s, z0 + np.full_like(s, float(b.get("quota", route.height))), color="#999999", ls="--", lw=1.2,
            label="Una quota per il blocco" if it else "One height for the block")
    ax.plot(s, z0 + h, color="#1f6fb4", lw=1.6, marker="o", ms=3,
            label="Segue il terreno: un punto per scatto" if it else "Following the terrain: a point per shot")
    ax.axhline(z0, color="#444444", lw=0.8, ls=":")
    ax.text(s[-1], z0 + 0.8, "quota del decollo" if it else "takeoff ground", ha="right", va="bottom", fontsize=9,
            color="#444444")
    ax.set_xlabel("Distanza lungo la strisciata (m)" if it else "Distance along the strip (m)")
    ax.set_ylabel("Quota (m)" if it else "Height (m)")
    ax.set_title("Quote lungo la strisciata che attraversa la collina" if it else
                 "Heights along the strip that crosses the hill")
    ax.legend(loc="center right", fontsize=9, framealpha=0.9)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, format="jpg", pil_kwargs={"quality": 88})
    plt.close(fig)
    print(f"  {path.name}")


if __name__ == "__main__":
    sys.exit(main())
