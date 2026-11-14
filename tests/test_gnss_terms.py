"""The Italian terms of GNSS (docs/gnss-glossary.it.md) in the interface: every text of the GNSS windows has its English,
and the variants the glossary leaves out appear nowhere - in the windows, in the messages of the GNSS engine, in the
report and in the Italian guide."""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from pyarchinit_photogrammetry.gui import i18n  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "src" / "pyarchinit_photogrammetry" / "gui"
GNSS_WINDOWS = ("gnss_pane.py", "ppk_dialog.py", "shots_widgets.py", "stations_dialog.py")
# the variants of docs/gnss-glossary.it.md, column "Non usare"
FORBIDDEN = {
    "stazione base": r"\bstazione base\b",
    "stazione di riferimento": r"\bstazione di riferimento\b",
    "stazione CORS": r"\bstazion[ei] CORS\b",
    "epoch": r"\bepochs?\b",
    "effemeridi broadcast": r"\beffemeridi broadcast\b",
    "log raw": r"\blog raw\b",
    "RTK fissato": r"\bRTK fissat[oaie]\b",
    "posizione fissata": r"\bposizion[ei] fissat[aei]\b",
    "soluzione fissata": r"\bsoluzion[ei] (?:\w+ )?(?:non )?fissat[aei]\b",
    "flottante": r"\bflottant[ei]\b",
    "single": r"(?<![=\-])\bsingle\b",
    "baseline": r"\bbaseline\b",
    "quota dell'antenna": r"\bquota dell'antenna\b",
    "scostamento dell'antenna": r"\bscostament[oi] dell'antenna\b",
    "scostamento del centro di fase": r"\bscostament[oi] (?:del suo |del )?centro di fase\b",
    "scostamenti di L1": r"\bscostamenti di L1\b",
    "lever arm": r"\blever arm\b",
    "frame": r"(?-i:\bframe\b)",
    "altezza ellissoidica": r"\baltezz[ae] ellissoidic",
    "altezza ortometrica": r"\baltezz[ae] ortometric",
    "separazione del geoide": r"\bseparazione del geoide\b",
}
PATTERN = re.compile("|".join(f"(?:{p})" for p in FORBIDDEN.values()), re.I)
# texts of the windows that are not words: file filters, formats, names
NOT_WORDS = re.compile(r"^[\w .*;()|\-/#]*\(\*.*\)$|^\{\d+\}|^[^a-zà-ù]*$")
GNSS_WORDS = re.compile(r"RINEX|RTKLIB|rover|\bbase\b|stazion|antenn|epoc|effemerid|satellit|UBX|RTCM|PPK|PPP|GNSS|"
                        r"ricevitor|geoide|ellissoid|centro di fase|ITRF|ETRF|marc[ah]e? di tempo|scatt", re.I)


def _forbidden(text: str) -> list[str]:
    text = re.sub(r"`[^`]*`|--[\w-]+(?:[ =]\S+)?|\([A-Z ]+:\)", "", text)  # code, options, header labels of RINEX
    return [name for name, p in FORBIDDEN.items() if re.search(p, text, re.I)]


def _window_texts() -> list[tuple[str, int, str]]:
    """The Italian texts of the GNSS windows: what tr() is given, the labels and tips of their fields and widgets."""
    out = []
    for name in GNSS_WINDOWS:
        tree = ast.parse((GUI / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            args = []
            if func == "tr" or func in ("QLabel", "QCheckBox", "QPushButton", "QGroupBox", "QRadioButton",
                                        "setToolTip", "setPlaceholderText", "setText", "setTitle"):
                args = node.args[:1]
            elif func == "Field":
                args = node.args[1:2] + [k.value for k in node.keywords if k.arg == "tip"]
            for a in args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.strip():
                    out.append((name, a.lineno, a.value))
    return out


def test_every_text_of_the_gnss_windows_has_its_english():
    cat = i18n.catalog()
    texts = _window_texts()
    assert len(texts) > 150, len(texts)
    missing = [f"{n}:{line} {t!r}" for n, line, t in texts
               if t not in cat and not NOT_WORDS.match(t) and re.search(r"[a-zà-ù]{3}", t)]
    assert not missing, "\n".join(missing)


def test_the_variants_left_out_by_the_glossary_appear_nowhere():
    import json

    left = [f"{n}:{line} {_forbidden(t)} {t[:90]!r}" for n, line, t in _window_texts() if _forbidden(t)]
    data = json.loads((GUI / "translations" / "en.json").read_text(encoding="utf-8"))
    for it in [*data["texts"], *data["formats"]]:  # every text of the interface: none of these is right anywhere
        bad = [b for b in _forbidden(it) if b not in ("frame", "single", "epoch")]
        if bad:
            left.append(f"en.json texts {bad} {it[:90]!r}")
    for it in data["engine"]:  # the messages of the engine about GNSS, as they come out in Italian
        if GNSS_WORDS.search(it) and _forbidden(it):
            left.append(f"en.json engine {_forbidden(it)} {it[:90]!r}")
    from pyarchinit_photogrammetry.io.report_content import TEXT

    for key, (it, _en) in TEXT.items():
        if GNSS_WORDS.search(it) and _forbidden(it):
            left.append(f"report {key} {_forbidden(it)} {it[:90]!r}")
    assert not left, "\n".join(left)


def _gnss_section(text: str) -> str:
    start = text.index("## Posizioni GNSS delle foto")
    end = text.find("\n## ", start + 5)
    return text[start:end if end > 0 else None]


def test_the_italian_guide_follows_the_glossary():
    docs = ROOT / "docs"
    it, en = (docs / "gnss-glossary.it.md").read_text(encoding="utf-8"), (docs / "gnss-glossary.md").read_text(
        encoding="utf-8")
    rows_it = [ln for ln in it.splitlines() if ln.startswith("| **")]
    rows_en = [ln for ln in en.splitlines() if ln.startswith("| **")]
    assert len(rows_it) == len(rows_en) >= 30  # the same terms in both languages
    for term in ("base", "rover", "fix", "float", "linea di base", "epoca", "effemeridi trasmesse", "ricevitore",
                 "offset dell'antenna", "sistema di riferimento", "geoide", "quota ellissoidica (h)",
                 "quota ortometrica (H)"):
        assert f"| **{term}**" in it, term
    section = _gnss_section((docs / "gui" / "photos-markers.it.md").read_text(encoding="utf-8"))
    assert "gnss-glossary.md" in section
    left = [f"{ln[:100]!r} {_forbidden(ln)}" for ln in section.splitlines()
            if not ln.lstrip().startswith(("#   ", "ppg ")) and _forbidden(ln)]
    assert not left, "\n".join(left)
