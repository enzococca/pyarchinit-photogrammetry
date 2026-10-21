"""Plugin projects to start from: ``ppg plugins new NAME`` and *Plugin > Nuovo plugin...* write a
small working plugin with the parts chosen (a menu action, an action that works in the background
with the progress bar, a command of ``ppg plugin``, an export format), its tests, READMEs in
English and Italian, the licence notice, a ``.gitignore`` and a GitHub workflow that builds the
wheel of a release. The author replaces what it does and keeps the shape."""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from string import Template

PARTS = ("action", "background", "command", "exporter")
PART_TITLES = {"action": "a menu action", "background": "an action that works in the background",
               "command": "a command of the command line", "exporter": "an export format"}


def slug_of(name: str) -> str:
    """The name of a plugin from any text: lowercase letters, digits and dashes, starting with a
    letter (``"Conta Foto"`` -> ``"conta-foto"``; a leading ``ppg-`` is dropped)."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    slug = re.sub(r"^ppg-", "", slug)
    if not slug or not slug[0].isalpha():
        raise ValueError(f"not a plugin name: {name!r} (letters, digits and dashes, starting with a letter)")
    return slug


def names(name: str) -> dict:
    """The names derived from the plugin's: ``slug`` (the entry point), ``distribution``
    (``ppg-<slug>``), ``package`` (``ppg_<slug>``) and ``class_name`` (``<Slug>Plugin``)."""
    slug = slug_of(name)
    return {"slug": slug, "distribution": "ppg-" + slug, "package": "ppg_" + slug.replace("-", "_"),
            "class_name": "".join(part.capitalize() for part in slug.split("-")) + "Plugin"}


_PYPROJECT = Template('''[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "$distribution"
version = "0.1.0"
description = "pyarchinit-photogrammetry plugin: $description"
readme = "README.md"
license = "GPL-2.0-or-later"
authors = [$authors]
requires-python = ">=3.10"
# nothing the program does not already carry: the packaged application has no pip
dependencies = []

[project.urls]
Homepage = "https://github.com/you/$distribution"

# the name before "=" is the name of the plugin (lowercase letters, digits and dashes)
[project.entry-points."pyarchinit_photogrammetry.plugins"]
$slug = "$package:$class_name"

[tool.setuptools]
packages = ["$package"]
''')

_HEAD = Template('''"""$title: a plugin of pyarchinit-photogrammetry.

$description

Made with ``ppg plugins new``: it adds $parts_text. Replace what it does and keep the shape; the
guide is https://enzococca.github.io/pyarchinit-photogrammetry/plugins/ (what ``activate``
receives, the rules of the background work, how to publish).
"""
from __future__ import annotations

from pyarchinit_photogrammetry.plugins import Plugin

__version__ = "0.1.0"


def t(it: str, en: str) -> str:
    """The Italian text, or the English one when the interface speaks English."""
    try:
        from pyarchinit_photogrammetry.gui import i18n

        return en if i18n.current() == "en" else it
    except Exception:  # the program without its interface
        return it


def summary(chunk) -> str:
    """Photos, aligned photos, markers and products of ``chunk``, in Markdown (replace it)."""
    products = [name for name in sorted(chunk.products) if chunk.product_path(name).exists()]
    return "\\n".join([
        f"# {chunk.label}", "",
        f"- {t('foto', 'photos')}: {len(chunk.cameras)}",
        f"- {t('allineate', 'aligned')}: {len(chunk.aligned_cameras)}",
        f"- {t('marker', 'markers')}: {len(chunk.markers)}",
        f"- {t('sistema di riferimento', 'reference system')}: {chunk.crs or t('locale', 'local')}",
        f"- {t('prodotti', 'products')}: {', '.join(products) or '-'}", ""])
''')

_DEM_STATISTICS = '''

def dem_statistics(path, progress=None) -> dict:
    """Minimum, maximum and mean height of a DEM, read in blocks (a DEM can be larger than the
    memory). ``progress(fraction, message)`` raises ``Cancelled`` when the user cancels."""
    import numpy as np
    import rasterio

    progress = progress or (lambda *a: None)
    low, high, total, count = np.inf, -np.inf, 0.0, 0
    with rasterio.open(path) as src:
        windows = [w for _ij, w in src.block_windows(1)]
        for i, window in enumerate(windows):
            z = src.read(1, window=window, masked=True).compressed()
            if z.size:
                low, high = min(low, float(z.min())), max(high, float(z.max()))
                total, count = total + float(z.sum(dtype=np.float64)), count + int(z.size)
            if i % 16 == 0:
                progress(i / len(windows), t("lettura del DEM", "reading the DEM"))
    progress(1.0, t("statistiche del DEM", "DEM statistics"))
    if not count:
        return {"cells": 0}
    return {"cells": count, "min": low, "max": high, "mean": total / count}
'''

_CLASS = Template('''

class $class_name(Plugin):
    version = __version__

    def __init__(self):
        self.title = "$title"
        self.description = "$description"
        self.context = None

    def activate(self, context) -> None:
        self.context = context
$activate

    def _chunk(self):
        doc = self.context.app.document
        return None if doc is None else doc.chunk
''')

_ACTIVATE = {
    "action": '        context.add_action(t("Riepilogo del chunk", "Chunk summary"), self.show_summary)',
    "background": '        context.add_action(t("Statistiche del DEM", "DEM statistics"), self.show_dem_statistics,\n'
                  '                           processing=True)',
    "command": '        context.add_command("$slug", self.command, help="summary of a chunk of a project")',
    "exporter": '        context.add_exporter(t("Riepilogo del chunk (Markdown)", "Chunk summary (Markdown)"), ".md",\n'
                '                             self.export)',
}

_METHODS = {
    "action": '''
    # ------------------------------------------------------------------ window
    def show_summary(self) -> None:
        """A menu action: runs in the interface thread, so it can show dialogs."""
        from pyarchinit_photogrammetry.gui.dialogs import message

        chunk = self._chunk()
        if chunk is None:
            return
        settings = self.context.settings  # kept between sessions
        settings["shown"] = settings.get("shown", 0) + 1
        self.context.save_settings()
        message(self.context.window, "info", self.title, summary(chunk))
''',
    "background": '''
    def show_dem_statistics(self) -> None:
        """Long work with the progress bar of the window: the function given to ``context.run``
        runs in another thread and must not touch the window; ``done`` runs in the interface
        thread afterwards."""
        from pyarchinit_photogrammetry.gui.dialogs import message

        chunk = self._chunk()
        if chunk is None or chunk.dem_path is None or not chunk.dem_path.exists():
            message(self.context.window, "warning", self.title, t("Il chunk non ha un DEM.", "The chunk has no DEM."))
            return
        path = chunk.dem_path

        def done(stats):
            if not stats.get("cells"):
                text = t("Il DEM e' vuoto.", "The DEM is empty.")
            else:
                text = (f"{t('celle', 'cells')}: {stats['cells']}\\nmin {stats['min']:.3f} m\\n"
                        f"max {stats['max']:.3f} m\\n{t('media', 'mean')} {stats['mean']:.3f} m")
            message(self.context.window, "info", self.title, text)

        self.context.run(t("Statistiche del DEM", "DEM statistics"), lambda progress: dem_statistics(path, progress),
                         done)
''',
    "exporter": '''
    # ------------------------------------------------------------------ File > Esporta
    def export(self, chunk, path, progress) -> None:
        """Runs in the background: it only reads the chunk and writes ``path``."""
        progress(0.5, t("riepilogo", "summary"))
        path.write_text(summary(chunk), encoding="utf-8")
''',
    "command": Template('''
    # ------------------------------------------------------------------ command line
    def command(self, *args) -> int:
        """``ppg plugin $slug PROJECT [--chunk LABEL]``: the arguments as strings, an int returned
        is the exit code."""
        import argparse

        import pyarchinit_photogrammetry as ppg

        parser = argparse.ArgumentParser(prog="ppg plugin $slug", description="Summary of a chunk.")
        parser.add_argument("project", help="the .ppg project")
        parser.add_argument("--chunk", help="label of the chunk (the active one by default)")
        a = parser.parse_args(list(args))
        doc = ppg.Document.open(a.project)
        chunk = doc.chunk if not a.chunk else next((c for c in doc.chunks if c.label == a.chunk), None)
        if chunk is None:
            print(f"no chunk {a.chunk!r} in {a.project}")
            return 2
        print(summary(chunk))
        return 0
'''),
}

_CONFTEST = '''"""The tests read the Italian texts of the plugin, whatever the language of the system."""
import os

os.environ.setdefault("PPG_LANG", "it")
os.environ.setdefault("PPG_NO_UPDATE_CHECK", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
'''

_TEST_HEAD = Template('''"""The plugin activated with a context of its own (no installation needed) and what it adds; its
functions on a small project. Installed (``pip install -e .``) the program finds it by its entry
point: the last test checks that, and is skipped until then."""
from __future__ import annotations

import pytest

from $package import $class_name, summary


def _project(tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    chunk = doc.add_chunk()
    doc.chunk = chunk
    doc.save(tmp_path / "scavo.ppg")
    chunk.crs = "EPSG:32637"
    return doc


def test_activation(tmp_path):
    from pyarchinit_photogrammetry.plugins import Context

    context = Context("$slug", tmp_path / "data")
    $class_name().activate(context)
    c = context.contributions
    assert [a.title for a in c.actions] == [$action_titles]
    assert list(c.commands) == [$command_names]
    assert [e.suffix for e in c.exporters] == [$exporter_suffixes]


def test_summary(tmp_path):
    doc = _project(tmp_path)
    text = summary(doc.chunk)
    assert text.startswith(f"# {doc.chunk.label}") and "foto: 0" in text
''')

_TESTS = {
    "exporter": Template('''

def test_export(tmp_path):
    doc = _project(tmp_path)
    out = tmp_path / "riepilogo.md"
    $class_name().export(doc.chunk, out, lambda *a: None)
    assert out.read_text(encoding="utf-8") == summary(doc.chunk)
'''),
    "command": Template('''

def test_command(tmp_path, capsys):
    doc = _project(tmp_path)
    assert $class_name().command(str(doc.path)) == 0
    assert "allineate: 0" in capsys.readouterr().out
    assert $class_name().command(str(doc.path), "--chunk", "nessuno") == 2
'''),
    "background": Template('''

def test_dem_statistics(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    from $package import dem_statistics

    doc = _project(tmp_path)
    z = np.linspace(100.0, 101.0, 64 * 64, dtype=np.float32).reshape(64, 64)
    z[0, 0] = -9999  # no data
    path = doc.chunk.data_dir / "dem.tif"
    with rasterio.open(path, "w", driver="GTiff", width=64, height=64, count=1, dtype="float32", crs="EPSG:32637",
                       transform=from_origin(300000, 3960000, 0.05, 0.05), nodata=-9999) as dst:
        dst.write(z, 1)
    seen = []
    stats = dem_statistics(path, lambda f, m: seen.append(f))
    assert stats["cells"] == 64 * 64 - 1 and seen[-1] == 1.0
    assert stats["min"] == pytest.approx(100.0, abs=1e-3) and stats["max"] == pytest.approx(101.0)
'''),
}

_TEST_TAIL = Template('''

def test_found_by_the_program(tmp_path):
    from pyarchinit_photogrammetry.plugins import Manager

    manager = Manager(tmp_path / "plugins")
    manager.load()
    info = manager.get("$slug")
    if info is None:
        pytest.skip("not installed in this environment: pip install -e .")
    assert info.active, info.error
''')

_README = Template('''# $title

$description

A plugin of [pyarchinit-photogrammetry](https://github.com/enzococca/pyarchinit-photogrammetry)
(1.1 or later), made with `ppg plugins new`. It adds $parts_text. The guide for writing plugins is
[here](https://enzococca.github.io/pyarchinit-photogrammetry/plugins/).

## Developing

Install it in the Python environment of the program, editable, and run the tests:

```bash
pip install -e .
pytest -q tests
ppg plugin                  # the command is listed
ppg-gui                     # the menu Plugin has the actions
```

Replace what the plugin does (`$package/__init__.py`) and keep the rules of the guide: only modules
the program carries (`dependencies = []`: the packaged application has no pip), widgets only in
the actions and in `done`, never in the function given to `context.run` or in an exporter,
`progress(...)` called often in long loops.

## Publishing

1. Raise `version` in `pyproject.toml` and `__version__`, push, and publish a release tagged
   `v<version>` on GitHub: the workflow `release` builds the wheel and attaches it (the summary of
   the run has the address of the wheel and its sha256). Without GitHub: `python -m build --wheel`.
2. Open a pull request on the
   [plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins) adding the
   entry of the plugin to `index.json` (`python scripts/make_entry.py WHEEL_URL --write` there
   makes it from the wheel). The checks of the pull request install the plugin and start it with
   the program.

Licence: GPL-2.0-or-later, like the program the plugin runs in.
''')

_README_IT = Template('''# $title

$description

Un plugin di [pyarchinit-photogrammetry](https://github.com/enzococca/pyarchinit-photogrammetry)
(1.1 o successiva), creato con `ppg plugins new`. Aggiunge $parts_text_it. La guida per scrivere i
plugin è [qui](https://enzococca.github.io/pyarchinit-photogrammetry/it/plugins/).

## Sviluppo

Installalo nell'ambiente Python del programma, in modalità modificabile, ed esegui i test:

```bash
pip install -e .
pytest -q tests
ppg plugin                  # il comando compare nell'elenco
ppg-gui                     # il menu Plugin ha le azioni
```

Sostituisci quello che fa il plugin (`$package/__init__.py`) rispettando le regole della guida:
solo moduli che il programma porta con sé (`dependencies = []`: l'applicazione impacchettata non
ha pip), widget solo nelle azioni e in `done`, mai nella funzione data a `context.run` o in un
esportatore, `progress(...)` chiamato spesso nei cicli lunghi.

## Pubblicazione

1. Alza `version` in `pyproject.toml` e `__version__`, fai push e pubblica su GitHub una release con
   il tag `v<versione>`: il workflow `release` costruisce la wheel e la allega (il riepilogo
   dell'esecuzione ha l'indirizzo della wheel e il suo sha256). Senza GitHub: `python -m build --wheel`.
2. Apri una pull request sul
   [repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins) che
   aggiunge la voce del plugin a `index.json` (lì `python scripts/make_entry.py URL_DELLA_WHEEL
   --write` la ricava dalla wheel). I controlli della pull request installano il plugin e lo
   avviano con il programma.

Licenza: GPL-2.0-or-later, come il programma in cui il plugin gira.
''')

_LICENSE = Template('''$title
Copyright (C) $year $author

This program is free software; you can redistribute it and/or modify it under the terms of the GNU
General Public License as published by the Free Software Foundation; either version 2 of the
License, or (at your option) any later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
General Public License for more details.

The full text of the licence: https://www.gnu.org/licenses/old-licenses/gpl-2.0.txt
''')

_GITIGNORE = '''__pycache__/
*.egg-info/
build/
dist/
.venv/
.pytest_cache/
'''

_RELEASE_WORKFLOW = '''name: release

# A release tagged v<version>: the wheel of the plugin is built and attached to it; the address of
# the wheel and its sha256, for the index of the plugin repository, are in the summary of the run.
on:
  release:
    types: [published]

permissions:
  contents: write

jobs:
  wheel:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Wheel
        run: |
          python -m pip install build
          python -m build --wheel --outdir dist .
      - name: Attach it to the release
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh release upload "${{ github.event.release.tag_name }}" dist/*-py3-none-any.whl --clobber --repo "${{ github.repository }}"
      - name: For the index of the plugin repository
        run: |
          wheel=$(ls dist/*-py3-none-any.whl)
          {
            echo '### Entry of index.json'
            echo "wheel: https://github.com/${{ github.repository }}/releases/download/${{ github.event.release.tag_name }}/$(basename "$wheel")"
            echo "sha256: $(sha256sum "$wheel" | cut -d' ' -f1)"
            echo 'python scripts/make_entry.py WHEEL_URL --write (in the plugin repository) makes the entry.'
          } >> "$GITHUB_STEP_SUMMARY"
'''

_PARTS_IT = {"action": "un'azione nel menu", "background": "un'azione che lavora in background",
             "command": "un comando della riga di comando", "exporter": "un formato di esportazione"}


def _join(items: list[str], word: str) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" {word} " + items[-1]


def _toml(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _py(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def create(folder, name: str, title: str = "", description: str = "", author: str = "", email: str = "",
           parts=PARTS) -> Path:
    """Write the project of a new plugin into ``folder/ppg-<slug>`` and return that path.
    ``parts`` are those of :data:`PARTS` the plugin starts with (at least one). Raises
    ``ValueError`` for a bad name or part, ``FileExistsError`` if the folder is already there."""
    n = names(name)
    parts = [p for p in PARTS if p in set(parts)]
    unknown = set(parts or []) - set(PARTS)
    if unknown or not parts:
        raise ValueError(f"parts to choose among {', '.join(PARTS)}: {sorted(unknown) or 'none given'}")
    title = (title or name).strip()
    description = (description or f"{title}: what it does, in one line").strip()
    target = Path(folder) / n["distribution"]
    if target.exists() and any(target.iterdir()):
        raise FileExistsError(f"{target} exists already")
    package = target / n["package"]
    tests = target / "tests"
    workflows = target / ".github" / "workflows"
    for d in (package, tests, workflows):
        d.mkdir(parents=True, exist_ok=True)
    authors = f'{{ name = "{_toml(author)}", email = "{_toml(email)}" }}' if email else f'{{ name = "{_toml(author)}" }}'
    parts_text = _join([PART_TITLES[p] for p in parts], "and")
    parts_text_it = _join([_PARTS_IT[p] for p in parts], "e")
    sub = {**n, "title": _py(title), "description": _py(description), "authors": authors if author else "",
           "parts_text": parts_text, "parts_text_it": parts_text_it, "year": _dt.date.today().year,
           "author": author or "the author"}
    (target / "pyproject.toml").write_text(_PYPROJECT.substitute(sub), encoding="utf-8")
    code = _HEAD.substitute(sub)
    if "background" in parts:
        code += _DEM_STATISTICS
    activate = "\n".join(Template(_ACTIVATE[p]).substitute(sub) for p in parts)
    code += _CLASS.substitute({**sub, "activate": activate})
    for p in parts:
        block = _METHODS[p]
        code += block.substitute(sub) if isinstance(block, Template) else block
    (package / "__init__.py").write_text(code, encoding="utf-8")
    (tests / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    titles = [t for p, t in (("action", '"Riepilogo del chunk"'), ("background", '"Statistiche del DEM"')) if p in parts]
    test = _TEST_HEAD.substitute({**sub, "action_titles": ", ".join(titles),
                                  "command_names": f'"{n["slug"]}"' if "command" in parts else "",
                                  "exporter_suffixes": '".md"' if "exporter" in parts else ""})
    for p in ("exporter", "command", "background"):
        if p in parts:
            test += _TESTS[p].substitute(sub)
    test += _TEST_TAIL.substitute(sub)
    (tests / f"test_{n['package']}.py").write_text(test, encoding="utf-8")
    (target / "README.md").write_text(_README.substitute(sub), encoding="utf-8")
    (target / "README.it.md").write_text(_README_IT.substitute(sub), encoding="utf-8")
    (target / "LICENSE").write_text(_LICENSE.substitute(sub), encoding="utf-8")
    (target / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
    (workflows / "release.yml").write_text(_RELEASE_WORKFLOW, encoding="utf-8")
    return target


def next_steps(target: Path) -> str:
    """What to do with the project just written: in the words of the command line."""
    n = names(target.name)
    return "\n".join([
        f"cd {target}",
        "pip install -e .               # in the Python environment of the program",
        "pytest -q tests",
        f"ppg plugin {n['slug']} PROJECT.ppg  # if the plugin has a command; ppg-gui for the menu actions",
        "python -m build --wheel        # or a GitHub release tagged v0.1.0: the workflow builds it",
        "then a pull request on https://github.com/enzococca/pyarchinit-photogrammetry-plugins (README.md)",
    ])
