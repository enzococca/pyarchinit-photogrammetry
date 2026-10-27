"""MkDocs hook of the page "Downloads and visits" (docs/stats.md, docs/stats.it.md): the placeholder
``<!-- ppg-stats en -->`` (``it`` on the Italian page) becomes the summary, the monthly charts and the tables of
the statistics that the weekly workflow .github/workflows/stats.yml keeps on the branch "stats" (made by
.github/scripts/download_stats.py, whose docstring describes the file). The docs workflow puts that file in
build/stats.json before building; PPG_STATS_JSON names another one. Without it the page says when the numbers
will appear.

The charts are inline SVG coloured with the variables of the theme, so they read in light and dark mode. Every
text that comes from a source (pages, referrers, systems) is escaped: anyone can send a visit to GoatCounter."""
from __future__ import annotations

import html
import json
import math
import os
import re
from pathlib import Path

try:
    from mkdocs.exceptions import PluginError
except ImportError:  # the tests import the hook without MkDocs
    PluginError = ValueError

PLACEHOLDER = re.compile(r"<!--\s*ppg-stats\s+(en|it)\s*-->")
MIN_UNIQUES = 2  # the pages of the repository seen by fewer unique visitors are left out
PLATFORMS = {"macos": "macOS", "windows-x64": "Windows x64", "windows-arm64": "Windows ARM64"}
SYSTEMS = {"darwin": "macOS", "linux": "Linux", "windows": "Windows"}
COUNTRIES = {  # the common ones, named in the language of the page; the others show flag and code
    "AE": ("United Arab Emirates", "Emirati Arabi Uniti"), "AL": ("Albania", "Albania"),
    "AR": ("Argentina", "Argentina"), "AT": ("Austria", "Austria"), "AU": ("Australia", "Australia"),
    "BE": ("Belgium", "Belgio"), "BG": ("Bulgaria", "Bulgaria"), "BR": ("Brazil", "Brasile"),
    "CA": ("Canada", "Canada"), "CH": ("Switzerland", "Svizzera"), "CL": ("Chile", "Cile"), "CN": ("China", "Cina"),
    "CO": ("Colombia", "Colombia"), "CY": ("Cyprus", "Cipro"), "CZ": ("Czechia", "Repubblica Ceca"),
    "DE": ("Germany", "Germania"), "DK": ("Denmark", "Danimarca"), "EG": ("Egypt", "Egitto"),
    "ES": ("Spain", "Spagna"), "FI": ("Finland", "Finlandia"), "FR": ("France", "Francia"),
    "GB": ("United Kingdom", "Regno Unito"), "GR": ("Greece", "Grecia"), "HK": ("Hong Kong", "Hong Kong"),
    "HR": ("Croatia", "Croazia"), "HU": ("Hungary", "Ungheria"), "ID": ("Indonesia", "Indonesia"),
    "IE": ("Ireland", "Irlanda"), "IL": ("Israel", "Israele"), "IN": ("India", "India"), "IQ": ("Iraq", "Iraq"),
    "IR": ("Iran", "Iran"), "IT": ("Italy", "Italia"), "JO": ("Jordan", "Giordania"), "JP": ("Japan", "Giappone"),
    "KE": ("Kenya", "Kenya"), "KR": ("South Korea", "Corea del Sud"), "LB": ("Lebanon", "Libano"),
    "MA": ("Morocco", "Marocco"), "MT": ("Malta", "Malta"), "MX": ("Mexico", "Messico"),
    "NG": ("Nigeria", "Nigeria"), "NL": ("Netherlands", "Paesi Bassi"), "NO": ("Norway", "Norvegia"),
    "NZ": ("New Zealand", "Nuova Zelanda"), "PE": ("Peru", "Perù"), "PL": ("Poland", "Polonia"),
    "PT": ("Portugal", "Portogallo"), "RO": ("Romania", "Romania"), "RS": ("Serbia", "Serbia"),
    "RU": ("Russia", "Russia"), "SA": ("Saudi Arabia", "Arabia Saudita"), "SE": ("Sweden", "Svezia"),
    "SG": ("Singapore", "Singapore"), "SI": ("Slovenia", "Slovenia"), "SK": ("Slovakia", "Slovacchia"),
    "SY": ("Syria", "Siria"), "TH": ("Thailand", "Thailandia"), "TN": ("Tunisia", "Tunisia"),
    "TR": ("Türkiye", "Turchia"), "TW": ("Taiwan", "Taiwan"), "UA": ("Ukraine", "Ucraina"),
    "US": ("United States", "Stati Uniti"), "VN": ("Vietnam", "Vietnam"), "ZA": ("South Africa", "Sudafrica"),
}
MONTHS = {
    "en": ("January February March April May June July August September October November December").split(),
    "it": ("gennaio febbraio marzo aprile maggio giugno luglio agosto settembre ottobre novembre dicembre").split(),
}
TEXT = {
    "en": {
        "nodata": "The numbers are published by a workflow of the repository that runs every week: they appear "
                  "here after its first run.",
        "updated": "Numbers of {date}.",
        "summary": "Summary", "last30": "Last 30 days", "total": "Total", "since": "Since",
        "app": "Downloads of the application (GitHub)", "clicks": "Download clicks on the installation page",
        "pip_real": "pip installs (PyPI), real", "pip_raw": "pip installs (PyPI), raw",
        "pip_raw_pypistats": "pip installs (PyPI), raw: robots included, known mirrors left out",
        "site": "Visitors of this site", "repository": "Visitors of the repository (unique per day)",
        "not_configured": "not configured yet", "error": "not updated at the last run: the source did not answer",
        "budget": "stopped until next month: this month's queries billed {gb} GB, and one more day would pass "
                  "the {budget} GB the workflow allows itself",
        "paused": "stopped until next month: one day of downloads billed {gb} GB, more than {limit} GB, so the "
                  "table does not narrow the query to this project",
        "no_data": "no data yet",
        "application": "Application", "pip": "pip installs", "this_site": "This site", "repo": "Repository",
        "chart_app": "Downloads of the application per month",
        "readings": "counted at the weekly readings", "before": "before the first reading, in the month of the release",
        "app_partial": "The last 30 days appear once the weekly readings span a month.",
        "platforms": "Platforms", "platform": "Platform", "versions": "Versions", "version": "Version",
        "downloads": "Downloads",
        "chart_pip": "pip installs per month", "raw": "raw", "real": "real", "projects": "Projects",
        "project": "Project", "systems": "Operating systems", "system": "System", "python": "Python versions",
        "countries": "Countries", "country": "Country", "installs": "Installs",
        "pip_real_note": "Tables: real installs of pyarchinit-photogrammetry.", "bigquery": "BigQuery",
        "chart_site": "Visitors of this site per month", "visitors": "Visitors", "sources": "Sources",
        "source": "Source", "pages": "Most visited pages", "page": "Page", "browsers": "Browsers",
        "browser": "Browser", "clicks_short": "Download clicks", "clicks_systems": "Download clicks by system",
        "clicks_refs": "Sources of the download clicks",
        "chart_repo": "Visits of the repository per month", "views": "views", "uniques": "unique visitors (per day)",
        "repo_refs": "Sources, 14 days to {date}", "repo_paths": "Most visited pages, 14 days to {date}",
        "views_col": "Views", "uniques_col": "Unique visitors", "unknown": "unknown", "other": "other",
        "one_visitor": "Pages seen by only one visitor are left out.",
    },
    "it": {
        "nodata": "I numeri sono pubblicati da un workflow del repository che gira ogni settimana: compaiono qui "
                  "dopo la sua prima esecuzione.",
        "updated": "Numeri del {date}.",
        "summary": "Riepilogo", "last30": "Ultimi 30 giorni", "total": "Totale", "since": "Dal",
        "app": "Download dell'applicazione (GitHub)", "clicks": "Clic di download nella pagina di installazione",
        "pip_real": "Installazioni con pip (PyPI), reali", "pip_raw": "Installazioni con pip (PyPI), grezze",
        "pip_raw_pypistats": "Installazioni con pip (PyPI), grezze: robot compresi, mirror noti esclusi",
        "site": "Visitatori di questo sito", "repository": "Visitatori del repository (unici per giorno)",
        "not_configured": "non ancora configurato",
        "error": "non aggiornato all'ultima esecuzione: la fonte non ha risposto",
        "budget": "fermo fino al mese prossimo: le query di questo mese hanno fatturato {gb} GB, e un altro giorno "
                  "supererebbe il limite di {budget} GB che il workflow si concede",
        "paused": "fermo fino al mese prossimo: un giorno di download ha fatturato {gb} GB, oltre {limit} GB: la "
                  "tabella non restringe la query a questo progetto",
        "no_data": "ancora nessun dato",
        "application": "Applicazione", "pip": "Installazioni con pip", "this_site": "Questo sito",
        "repo": "Repository",
        "chart_app": "Download dell'applicazione per mese",
        "readings": "contati alle letture settimanali", "before": "prima della prima lettura, nel mese della versione",
        "app_partial": "Gli ultimi 30 giorni compaiono quando le letture settimanali coprono un mese.",
        "platforms": "Piattaforme", "platform": "Piattaforma", "versions": "Versioni", "version": "Versione",
        "downloads": "Download",
        "chart_pip": "Installazioni con pip per mese", "raw": "grezze", "real": "reali", "projects": "Progetti",
        "project": "Progetto", "systems": "Sistemi operativi", "system": "Sistema", "python": "Versioni di Python",
        "countries": "Paesi", "country": "Paese", "installs": "Installazioni",
        "pip_real_note": "Tabelle: installazioni reali di pyarchinit-photogrammetry.", "bigquery": "BigQuery",
        "chart_site": "Visitatori di questo sito per mese", "visitors": "Visitatori", "sources": "Provenienza",
        "source": "Provenienza", "pages": "Pagine più visitate", "page": "Pagina", "browsers": "Browser",
        "browser": "Browser", "clicks_short": "Clic di download", "clicks_systems": "Clic di download per sistema",
        "clicks_refs": "Provenienza dei clic di download",
        "chart_repo": "Visite del repository per mese", "views": "visualizzazioni",
        "uniques": "visitatori unici (per giorno)",
        "repo_refs": "Provenienza, 14 giorni fino al {date}", "repo_paths": "Pagine più visitate, 14 giorni fino al {date}",
        "views_col": "Visualizzazioni", "uniques_col": "Visitatori unici", "unknown": "sconosciuto", "other": "altri",
        "one_visitor": "Le pagine viste da un solo visitatore sono escluse.",
    },
}
STYLE = """<style>
.ppg-stats-chart svg{display:block;width:100%;max-width:36rem;height:auto}
.ppg-stats-chart text{fill:var(--md-default-fg-color--light);font-size:12px}
.ppg-stats-chart .g{stroke:var(--md-default-fg-color--lightest)}
.ppg-stats-chart .b1{fill:var(--md-typeset-a-color)}
.ppg-stats-chart .b0{fill:var(--md-default-fg-color--lighter)}
.ppg-key{display:inline-block;width:.75em;height:.75em;margin:0 .35em 0 1em;border-radius:.1em}
.ppg-key:first-child{margin-left:0}
.ppg-stats-grid>*{min-width:0}
.ppg-stats-table td:first-child{overflow-wrap:anywhere}
@media screen and (max-width:44.9375em){
.md-typeset .ppg-stats-table table:not([class]) th{min-width:0}
.md-typeset .ppg-stats-table table:not([class]) th,.md-typeset .ppg-stats-table table:not([class]) td{padding:.6em .7em}
.ppg-stats-chart text{font-size:17px}
}
.md-typeset .ppg-stats-note{margin-top:-.5em;font-size:.64rem;color:var(--md-default-fg-color--light)}
.ppg-key.b1{background:var(--md-typeset-a-color)}
.ppg-key.b0{background:var(--md-default-fg-color--lighter)}
</style>"""

_cache: dict = {}


def on_page_markdown(markdown, page, config, files, **kwargs):
    match = PLACEHOLDER.search(markdown)
    if not match:
        return markdown
    return markdown[:match.start()] + render(_load(config), match.group(1)) + markdown[match.end():]


def _load(config) -> dict | None:
    path = Path(os.environ.get("PPG_STATS_JSON") or Path(config["config_file_path"]).parent / "build" / "stats.json")
    if path not in _cache:
        data = None
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError as e:
                raise PluginError(f"{path}: not a statistics file ({e})") from e
            if data.get("schema") != 1:
                raise PluginError(f"{path}: unknown schema {data.get('schema')!r}")
        _cache[path] = data
    return _cache[path]


# ------------------------------------------------------------------------------------------------ formatting
def _esc(text) -> str:
    return html.escape(str(text), quote=True)


def _num(value, lang: str) -> str:
    if value is None:
        return "–"
    text = f"{value:,.1f}" if isinstance(value, float) and not value.is_integer() else f"{int(value):,}"
    return text.translate(str.maketrans(",.", ".,")) if lang == "it" else text


def _date(day: str | None, lang: str) -> str:
    if not day:
        return "–"
    year, month, d = (int(x) for x in day[:10].split("-"))
    return f"{d} {MONTHS[lang][month - 1]} {year}"


def _country(code: str, lang: str, t: dict) -> str:
    code = (code or "").upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        return _esc(t["unknown"])
    flag = "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)
    name = COUNTRIES.get(code)
    return f"{flag} {code}" + (f" {_esc(name[lang == 'it'])}" if name else "")


def _label(text, t: dict, names: dict | None = None, limit: int = 60) -> str:
    text = str(text or "")
    if names:
        text = names.get(text.lower(), text)
    if not text or text.lower() in ("null", "none", "(unknown)"):
        return _esc(t["unknown"])
    if text.lower() == "other":  # pypistats.org: every system but Linux, macOS and Windows
        return _esc(t["other"])
    return _esc(text if len(text) <= limit else text[:limit - 1] + "…")


def _status(source: dict | None, t: dict, lang: str) -> str:
    status = (source or {}).get("status")
    if status == "not configured":
        return t["not_configured"]
    if status in ("budget", "paused"):  # BigQuery
        return t[status].format(gb=_num(source.get("gb"), lang), budget=_num(source.get("budget"), lang),
                                limit=_num(source.get("limit"), lang))
    if status == "error":
        return t["error"]
    return t["no_data"]


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + "."


def _note(name: str, source: dict | None, t: dict, lang: str) -> list[str]:
    """A line under a section whose numbers were not updated at the last run."""
    if (source or {}).get("status") in ("budget", "paused", "error"):
        return [f'!!! warning "{name}"', f"    {_sentence(_status(source, t, lang))}", ""]
    return []


# ------------------------------------------------------------------------------------------- charts, tables
def _nice(x: float) -> int:
    if x <= 1:
        return 1
    power = 10 ** math.floor(math.log10(x))
    return next(m * power for m in (1, 2, 5, 10) if m * power >= x)


def _month_keys(months: dict) -> list[str]:
    if not months:
        return []
    year, month = (int(x) for x in min(months).split("-"))
    keys, last = [], max(months)
    while f"{year:04d}-{month:02d}" <= last:
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return keys


def chart(months: dict, series: list, title: str, lang: str, stacked: bool = False) -> str:
    """Bars per month, every month from the first to the last. months: {"2026-10": [value of each series]};
    series: [(css class, label)]. Overlaid, every bar starts from zero and the first is drawn behind (the raw
    count and the real one); stacked, the second sits on the first."""
    keys = _month_keys(months)
    if not keys:
        return ""
    width, height, left, right, top, bottom = 560, 210, 48, 6, 10, 34
    values = [list(months.get(k) or [0] * len(series)) + [0] * len(series) for k in keys]
    peak = max((sum(v[:len(series)]) if stacked else max(v[:len(series)])) for v in values)
    step = _nice(peak / 4)  # at most four steps of 1, 2 or 5 times a power of ten, the top just above the peak
    steps = max(1, math.ceil(peak / step))
    plot_w, plot_h, base = width - left - right, height - top - bottom, height - bottom
    slot = plot_w / len(keys)
    bar = min(slot * 0.66, 48)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(title)}"><title>{_esc(title)}</title>']
    for i in range(steps + 1):
        y = base - plot_h * i / steps
        out.append(f'<line class="g" x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}"/>'
                   f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">{_num(step * i, lang)}</text>')
    every = max(1, math.ceil(len(keys) / 12))
    for j, (key, v) in enumerate(zip(keys, values)):
        x, below = left + slot * j + (slot - bar) / 2, 0.0
        year, month = (int(p) for p in key.split("-"))
        name = f"{MONTHS[lang][month - 1]} {year}"
        for (cls, label), n in zip(series, v):
            h = plot_h * n / (step * steps)
            y = base - h - below
            below += h if stacked else 0
            out.append(f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{bar:.1f}" height="{h:.1f}">'
                       f'<title>{_esc(name)}, {_esc(label)}: {_num(n, lang)}</title></rect>')
        if j % every == 0:
            cx = x + bar / 2
            out.append(f'<text x="{cx:.1f}" y="{base + 15}" text-anchor="middle">{MONTHS[lang][month - 1][:3]}</text>')
            if j == 0 or month == 1:
                out.append(f'<text x="{cx:.1f}" y="{base + 29}" text-anchor="middle">{year}</text>')
    out.append("</svg>")
    legend = "".join(f'<span class="ppg-key {cls}"></span>{_esc(label)}' for cls, label in series)
    return f'<div class="ppg-stats-chart">{"".join(out)}<p>{legend}</p></div>'


def table(caption: str, headers: list, rows: list, lang: str, note: str = "") -> tuple[str, bool]:
    """A table and whether it is wide: the first column is text (already escaped), the others are numbers; a
    note in small print below it. Tables of three columns or more take the whole width of the page, the
    narrower ones go side by side (_grid)."""
    right = ' align="right"'
    head = "".join(f"<th{right if i else ''}>{_esc(h)}</th>" for i, h in enumerate(headers))
    body = "".join("<tr>" + "".join(f'<td align="right">{_num(c, lang)}</td>' if i else f"<td>{c}</td>"
                                    for i, c in enumerate(row)) + "</tr>" for row in rows)
    html_ = f"<p><strong>{_esc(caption)}</strong></p>"
    if rows:
        html_ += f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    if note:
        html_ += f'<p class="ppg-stats-note">{_esc(note)}</p>'
    return f'<div class="ppg-stats-table">{html_}</div>', len(headers) >= 3


def _grid(cells: list[tuple[str, bool]]) -> list[str]:
    """Wide tables one under the other, the narrow ones in a grid whose cells never grow past their column (a
    table too wide for it scrolls, in the wrapper the theme puts around every table)."""
    out, narrow = [], []
    for html_, wide in cells + [("", True)]:
        if not wide:
            narrow.append(html_)
            continue
        if narrow:
            out += [f'<div class="grid ppg-stats-grid">{"".join(narrow)}</div>', ""]
            narrow = []
        if html_:
            out += [html_, ""]
    return out


# ---------------------------------------------------------------------------------------------- the sections
def _summary(page: dict, t: dict, lang: str) -> str:
    sources, pip, site = page.get("sources") or {}, page.get("pip") or {}, page.get("site") or {}
    rows = [("app", (page.get("releases") or {}).get("summary"), sources.get("releases"))]
    rows.append(("clicks", site.get("clicks"), sources.get("goatcounter")))
    bigquery = sources.get("bigquery")
    if pip.get("source") == "pypistats":
        rows += [("pip_real", None, bigquery), ("pip_raw_pypistats", pip["raw"], sources.get("pypistats"))]
    else:  # from BigQuery, or nothing yet: then the raw line says why pypistats.org gave nothing
        rows += [("pip_real", pip.get("real"), bigquery), ("pip_raw", pip.get("raw"), sources.get("pypistats") or bigquery)]
    rows += [("site", site.get("site"), sources.get("goatcounter")),
             ("repository", (page.get("repository") or {}).get("summary"), sources.get("traffic"))]
    body = []
    for key, entry, source in rows:
        if entry is None:
            cells = f'<td colspan="3">{_esc(_status(source, t, lang))}</td>'
        else:
            cells = (f'<td align="right">{_num(entry.get("last30"), lang)}</td><td align="right">'
                     f'{_num(entry.get("total"), lang)}</td><td>{_date(entry.get("since"), lang)}</td>')
        body.append(f"<tr><td>{_esc(t[key])}</td>{cells}</tr>")
    head = (f'<tr><th></th><th align="right">{_esc(t["last30"])}</th><th align="right">{_esc(t["total"])}</th>'
            f'<th>{_esc(t["since"])}</th></tr>')
    return f'<div class="ppg-stats-table"><table><thead>{head}</thead><tbody>{"".join(body)}</tbody></table></div>'


def _application(page: dict, t: dict, lang: str) -> list[str]:
    app, source = page.get("releases"), (page.get("sources") or {}).get("releases")
    out = [f"## {t['application']}", ""]
    if not app:
        return out + [_sentence(_status(source, t, lang)), ""]
    out += _note("GitHub", source, t, lang)
    out += [chart(app["months"], [("b1", t["readings"]), ("b0", t["before"])], t["chart_app"], lang, stacked=True), ""]
    if app["summary"]["last30"] is None:
        out += [t["app_partial"], ""]
    header = [t["last30"], t["total"]]
    return out + _grid([
        table(t["platforms"], [t["platform"]] + header, [[_label(p, t, PLATFORMS)] + v for p, *v in app["platforms"]], lang),
        table(t["versions"], [t["version"]] + header, [[_label(n, t)] + v for n, *v in app["versions"]], lang)])


def _pip(page: dict, t: dict, lang: str) -> list[str]:
    pip, sources = page.get("pip"), page.get("sources") or {}
    out = [f"## {t['pip']}", ""]
    if not pip:
        return out + [_sentence(_status(sources.get("pypistats") or sources.get("bigquery"), t, lang)), ""]
    out += _note(t["bigquery"], sources.get("bigquery"), t, lang)
    if pip["source"] == "pypistats":
        out += _note("pypistats.org", sources.get("pypistats"), t, lang)
        out += [chart(pip["months"], [("b1", t["pip_raw_pypistats"])], t["chart_pip"], lang), ""]
        return out + _grid([table(t["systems"], [t["system"], t["installs"]],
                                  [[_label(s, t, SYSTEMS), n] for s, n in pip["systems"]], lang)])
    out += [chart(pip["months"], [("b0", t["raw"]), ("b1", t["real"])], t["chart_pip"], lang), "",
            t["pip_real_note"], ""]
    return out + _grid([
        table(t["projects"], [t["project"], t["real"].capitalize(), t["raw"].capitalize()],
              [[_label(p, t), real, raw] for p, real, raw in pip["projects"]], lang),
        table(t["versions"], [t["version"], t["installs"]], [[_label(v, t), n] for v, n in pip["versions"]], lang),
        table(t["systems"], [t["system"], t["installs"]], [[_label(s, t, SYSTEMS), n] for s, n in pip["systems"]], lang),
        table(t["python"], ["Python", t["installs"]], [[_label(v, t), n] for v, n in pip["python"]], lang),
        table(t["countries"], [t["country"], t["installs"]], [[_country(c, lang, t), n] for c, n in pip["countries"]], lang)])


def _site(page: dict, t: dict, lang: str) -> list[str]:
    site, source = page.get("site"), (page.get("sources") or {}).get("goatcounter")
    out = [f"## {t['this_site']}", ""]
    if not site:
        return out + [_sentence(_status(source, t, lang)), ""]
    out += _note("GoatCounter", source, t, lang)
    out += [chart(site["months"], [("b1", t["visitors"].lower())], t["chart_site"], lang), ""]
    return out + _grid([
        table(t["countries"], [t["country"], t["visitors"], t["clicks_short"]],
              [[_country(c, lang, t), v, k] for c, _name, v, k in site["countries"]], lang),
        table(t["sources"], [t["source"], t["visitors"]], [[_label(r, t), n] for r, n in site["refs"]], lang),
        table(t["pages"], [t["page"], t["visitors"]], [[_label(p, t), n] for p, n in site["pages"]], lang),
        table(t["systems"], [t["system"], t["visitors"]], [[_label(s, t), n] for s, n in site["systems"]], lang),
        table(t["browsers"], [t["browser"], t["visitors"]], [[_label(b, t), n] for b, n in site["browsers"]], lang),
        table(t["clicks_systems"], [t["platform"], t["clicks_short"]],
              [[_label(s, t, PLATFORMS), n] for s, n in site["clicks_systems"]], lang),
        table(t["clicks_refs"], [t["source"], t["clicks_short"]], [[_label(r, t), n] for r, n in site["clicks_refs"]], lang)])


def _repository(page: dict, t: dict, lang: str) -> list[str]:
    repo, source = page.get("repository"), (page.get("sources") or {}).get("traffic")
    out = [f"## {t['repo']}", ""]
    if not repo:
        return out + [_sentence(_status(source, t, lang)), ""]
    out += _note("GitHub", source, t, lang)
    out += [chart(repo["months"], [("b0", t["views"]), ("b1", t["uniques"])], t["chart_repo"], lang), ""]
    header = [t["views_col"], t["uniques_col"]]
    refs, paths = repo["referrers"], repo["paths"]
    seen = [row for row in paths.get("rows", []) if row[2] >= MIN_UNIQUES]  # one visitor: mostly the author
    return out + _grid([
        table(t["repo_refs"].format(date=_date(refs.get("until"), lang)), [t["source"]] + header,
              [[_label(r, t)] + v for r, *v in refs.get("rows", [])], lang),
        table(t["repo_paths"].format(date=_date(paths.get("until"), lang)), [t["page"]] + header,
              [[_label(p, t)] + v for p, *v in seen], lang, note=t["one_visitor"])])


def render(data: dict | None, lang: str) -> str:
    """The Markdown that takes the place of the placeholder: headings for the table of contents, the rest raw
    HTML (charts and tables), nothing of the sources read as Markdown."""
    t = TEXT[lang]
    page = (data or {}).get("page")
    if not page:
        return t["nodata"]
    out = [STYLE, "", t["updated"].format(date=_date(page.get("updated"), lang)), "",
           f"## {t['summary']}", "", _summary(page, t, lang), ""]
    for section in (_application, _pip, _site, _repository):
        out += section(page, t, lang)
    return "\n".join(out)
