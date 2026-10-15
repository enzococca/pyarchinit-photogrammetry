"""Demo data to try the library, the command line and the desktop interface.

``create_demo(folder)`` writes::

    folder/
      LEGGIMI.txt                  what is inside and what to try
      scavo/photos/IMG_*.jpg       28 rendered photos of a 4 x 3 m trench (a wall stub,
                                   8 ArUco targets) with lens distortion
      scavo/gcp.csv                target coordinates in EPSG:3004 (Monte Mario / Italy 2)
      scavo.ppg (+ scavo.ppg.files/)  project with photos, detected targets and references
      ceramica/vaso.ply            a complete bowl (rim 16 cm, height 12 cm, wall 6 mm)
      ceramica/frammento.ply       a sherd of the same bowl (60 degrees, 0.2 mm noise)
      targets.pdf                  printable targets 0-11
      pyarchinit/pyarchinit_DB_folder/{demo.sqlite, config.cfg}
                                   PyArchInit database with site "Saggio dimostrativo"
                                   and US 1-3 (use it with PYARCHINIT_HOME=folder/pyarchinit)

With ``process=True`` the trench is also processed (alignment, georeferencing,
dense cloud, mesh, texture, DEM, orthomosaic): about two minutes on a laptop.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .core.progress import ProgressCallback, as_progress

DEMO_SITE = "Saggio dimostrativo"
DEMO_CRS = "EPSG:3004"


@dataclass
class DemoData:
    folder: Path
    project: Path
    photos: Path
    gcp: Path
    vessel: Path
    sherd: Path
    targets: Path | None
    pyarchinit_home: Path | None
    pyarchinit_config: Path | None

    def use_pyarchinit(self) -> None:
        """Point :class:`~pyarchinit_photogrammetry.pyarchinit.PyArchInit` to the demo database."""
        if self.pyarchinit_home is not None:
            os.environ["PYARCHINIT_HOME"] = str(self.pyarchinit_home)


_README = """pyarchinit-photogrammetry - dati dimostrativi
================================================

scavo/photos/        28 foto sintetiche di un saggio 4 x 3 m con un muro e 8 target ArUco
scavo/gcp.csv        coordinate dei target in EPSG:3004 (Monte Mario / Italy zone 2)
scavo.ppg            progetto pronto: foto caricate, target riconosciuti, riferimenti importati
ceramica/vaso.ply    ciotola completa (orlo 16 cm, altezza 12 cm, parete 6 mm)
ceramica/frammento.ply  frammento della stessa ciotola (60 gradi, rumore 0,2 mm)
targets.pdf          target stampabili 0-11
pyarchinit/          database PyArchInit di prova (sito "Saggio dimostrativo", US 1-3)

Da provare
----------
Interfaccia:    ppg-gui {project}
                poi Flusso di lavoro > Elabora tutto, oppure i singoli passi.
Riga di comando:
    ppg align {project}
    ppg reference {project} {gcp} --crs EPSG:3004
    ppg dense {project}
    ppg mesh {project}
    ppg dem {project}
    ppg ortho {project}
    ppg info {project}
    ppg pottery {sherd} --out profilo.svg --title "Frammento demo"
PyArchInit:
    export PYARCHINIT_HOME={pa_home}
    ppg pyarchinit sites
    ppg pyarchinit publish {project} "Saggio dimostrativo" "Ortofoto demo" --dem --model

Valori attesi dopo l'elaborazione: errore sui punti di controllo di pochi millimetri,
DEM con il muro alto circa 30 cm sul fondo del saggio, ortofoto a circa 2 mm/pixel.
"""


def create_demo(folder: str | os.PathLike, process: bool = False, with_pyarchinit: bool = True,
                with_targets: bool = True, n_x: int = 5, n_y: int = 4,
                progress: ProgressCallback | None = None) -> DemoData:
    """Write the demo data set (see the module docstring) and return its paths."""
    from .core.document import Document
    from .testing.synthetic import make_vessel, write_dataset

    progress = as_progress(progress)
    folder = Path(folder).expanduser().resolve()
    folder.mkdir(parents=True, exist_ok=True)

    progress(0.0, "Rendering the trench photos")
    scavo = folder / "scavo"
    if not (scavo / "photos").exists() or not any((scavo / "photos").glob("*.jpg")):
        write_dataset(scavo, n_x=n_x, n_y=n_y)
    photos, gcp = scavo / "photos", scavo / "gcp.csv"

    progress(0.45, "Pottery meshes")
    cer = folder / "ceramica"
    cer.mkdir(exist_ok=True)
    vessel, sherd = cer / "vaso.ply", cer / "frammento.ply"
    make_vessel().export(vessel)
    make_vessel(sherd=(10, 70), noise=0.0002).export(sherd)

    targets = None
    if with_targets:
        progress(0.5, "Printable targets")
        from .markers.targets import generate_sheet

        targets = generate_sheet(folder / "targets.pdf", codes=range(12))[0]

    pa_home = pa_cfg = None
    if with_pyarchinit:
        progress(0.55, "PyArchInit demo database")
        from .pyarchinit.demo_db import create_demo_database

        pa_home = folder / "pyarchinit"
        pa_cfg = create_demo_database(pa_home, srid=3004, site=DEMO_SITE)

    progress(0.6, "Demo project")
    project = folder / "scavo.ppg"
    doc = Document()
    chunk = doc.add_chunk("Saggio 1")
    doc.save(project)
    chunk.add_photos([photos])
    chunk.detect_markers("aruco_4x4_50")
    chunk.import_reference(gcp, crs=DEMO_CRS)
    doc.save()

    if process:
        sub = progress.sub(0.62, 0.37)
        chunk.match_photos(quality="high", progress=sub.sub(0.0, 0.1))
        chunk.align_cameras(progress=sub.sub(0.1, 0.15))
        chunk.update_transform()
        chunk.build_depth_maps(quality="medium", progress=sub.sub(0.25, 0.35))
        chunk.build_point_cloud(progress=sub.sub(0.6, 0.1))
        chunk.build_model(progress=sub.sub(0.7, 0.1))
        chunk.build_texture(texture_size=2048, progress=sub.sub(0.8, 0.06))
        chunk.build_dem(progress=sub.sub(0.86, 0.06))
        chunk.build_orthomosaic(progress=sub.sub(0.92, 0.08))
        doc.save()

    (folder / "LEGGIMI.txt").write_text(_README.format(project=project, gcp=gcp, sherd=sherd,
                                                       pa_home=pa_home or "<cartella>/pyarchinit"),
                                        encoding="utf-8")
    progress(1.0, f"Demo ready in {folder}")
    return DemoData(folder, project, photos, gcp, vessel, sherd, targets, pa_home, pa_cfg)


__all__ = ["create_demo", "DemoData", "DEMO_SITE", "DEMO_CRS"]
