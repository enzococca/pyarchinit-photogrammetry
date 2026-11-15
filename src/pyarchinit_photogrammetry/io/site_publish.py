"""A site folder (:mod:`.site`) published on Cloudflare through the user's own ``wrangler`` (since 1.10.0).

Two steps:

* :func:`plan` (always available, no network, no wrangler): what goes where - the page on Cloudflare Pages, the data
  files on Pages too or in an R2 bucket -, how many files and bytes, which limits of the free plans they meet or
  exceed (:data:`LIMITS`, read on Cloudflare's documentation on :data:`CHECKED`), what is made public and what it
  costs in free-tier terms. Nothing is sent.
* :func:`publish`: the plan carried out by running ``wrangler`` (on the PATH, in the usual folders of Node, or
  ``npx --no wrangler``: never installed by the program). The login is the user's (``wrangler login`` in a
  terminal, or :meth:`Wrangler.login`, which runs it and keeps nothing of its output): the program never asks for,
  reads, stores or passes a token, a key or an account id; it reads only the
  exit codes and the output of wrangler, and what it keeps or shows of that output goes through :func:`scrub`.

Where the data go (``data``): ``"auto"`` puts everything on Pages when every file fits its limits (25 MiB a file,
20,000 files on the free plan) and the folder ``data/`` in R2 otherwise; ``"pages"`` and ``"r2"`` force one. Pages
answers range requests with the whole file (``200``), which the viewer accepts up to 50 MB a file
(``app/cog.js``), so a site whose files all fit Pages works there whole; R2 serves ranges. With R2 the published copy
of ``site.json`` carries the address of the data (``data_base``, each ``data/...`` address made absolute) and the
page's Content-Security-Policy admits that origin; the folder on the disk stays as it was built. Files sent to R2 are
remembered by their SHA-256 in ``.ppg-publish.json`` inside the site folder (not part of the site, never published),
so publishing again sends only what changed and deletes what is gone; Pages (``wrangler pages deploy``) sends only
the files it does not have already.

Privacy: :func:`publish` refuses without ``public=True`` (the site becomes readable by anyone) and, for a site built
with ``frame="crs"`` (true coordinates), without ``coordinates=True`` as well (:class:`ConsentError`).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..core.progress import Cancelled, as_progress

# ---------------------------------------------------------------------------------------- limits (verified on CHECKED)
CHECKED = "2026-11-14"
SOURCES = {
    "pages_limits": "https://developers.cloudflare.com/pages/platform/limits/",
    "pages_serving": "https://developers.cloudflare.com/pages/configuration/serving-pages/",
    "pages_pricing": "https://developers.cloudflare.com/pages/functions/pricing/",
    "r2_pricing": "https://developers.cloudflare.com/r2/pricing/",
    "r2_limits": "https://developers.cloudflare.com/r2/platform/limits/",
    "r2_upload": "https://developers.cloudflare.com/r2/objects/upload-objects/",
    "r2_public": "https://developers.cloudflare.com/r2/buckets/public-buckets/",
    "r2_cors": "https://developers.cloudflare.com/r2/buckets/cors/",
    "wrangler_pages": "https://developers.cloudflare.com/workers/wrangler/commands/pages/",
    "wrangler_r2": "https://developers.cloudflare.com/workers/wrangler/commands/r2/",
    "wrangler_general": "https://developers.cloudflare.com/workers/wrangler/commands/general/",
}
PAGES_FREE_FILES = 20_000  # files of a Pages site on the Free plan
PAGES_PAID_FILES = 100_000  # on paid plans
PAGES_FILE_BYTES = 25 * 1024 * 1024  # "25 MiB" a file
PAGES_PROJECTS = 100  # projects of an account
R2_PUT_BYTES = 315_000_000  # "Wrangler supports uploading files up to 315 MB"
R2_FREE_STORAGE = 10 * 1000 ** 3  # 10 GB-month a month (Standard storage)
R2_FREE_CLASS_A = 1_000_000  # writes (PutObject, ListObjects, PutBucketCors...) a month
R2_FREE_CLASS_B = 10_000_000  # reads (GetObject, HeadObject) a month
WHOLE_LIMIT = 50 * 1024 * 1024  # the largest file the viewer takes whole from a host without ranges (app/cog.js)
LIMITS = {"pages_files": PAGES_FREE_FILES, "pages_file_bytes": PAGES_FILE_BYTES, "r2_put_bytes": R2_PUT_BYTES,
          "r2_storage": R2_FREE_STORAGE, "r2_class_a": R2_FREE_CLASS_A, "r2_class_b": R2_FREE_CLASS_B}

DATA = "data"  # the folder of a site that may go to R2
STATE = ".ppg-publish.json"  # what was sent where, inside the site folder (never published)
RECORDS = 2  # the state records what the program created (bucket_created, dev_url_enabled): since 1.10.2
DATA_MODES = ("auto", "pages", "r2")
CACHE_DATA = "public, max-age=3600"
BRANCH = "main"  # the production branch of the Pages projects the program creates
_PROJECT = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,56}[a-z0-9])?$")
_BUCKET = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".json": "application/json; charset=utf-8", ".geojson": "application/geo+json; charset=utf-8",
          ".tif": "image/tiff", ".tiff": "image/tiff", ".svg": "image/svg+xml", ".png": "image/png",
          ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".txt": "text/plain; charset=utf-8",
          ".md": "text/plain; charset=utf-8", ".glb": "model/gltf-binary", ".laz": "application/octet-stream",
          ".bin": "application/octet-stream", ".wasm": "application/wasm"}


class ConsentError(PermissionError):
    """Publishing without the explicit consent the site needs (public; true coordinates)."""


class WranglerMissing(RuntimeError):
    """No wrangler on this computer."""


class NotLoggedIn(RuntimeError):
    """wrangler is there but nobody is logged in to Cloudflare with it."""


class WranglerFailed(RuntimeError):
    """A wrangler command ended with an error: ``output`` is its output, scrubbed."""

    def __init__(self, text: str, output: str = ""):
        super().__init__(text)
        self.output = output


INSTALL_HINT = ("wrangler, the command line of Cloudflare, is not installed on this computer. Install Node.js "
                "(https://nodejs.org), then in a terminal: npm install -g wrangler ; then wrangler login. "
                "The program never installs it by itself.")
LOGIN_TIMEOUT = 600.0  # seconds given to the user to finish ``wrangler login`` in the browser


def typed_command(command: list[str] | None) -> str:
    """wrangler as the user types it in a terminal, from the command actually found by :func:`find_wrangler`:
    ``npx wrangler`` when only npx runs it (wrangler alone is then "command not found" in a terminal), else
    ``wrangler`` (since 1.10.1)."""
    if command and len(command) >= 2 and Path(command[0]).stem.lower() == "npx" and command[-1] == "wrangler":
        return "npx wrangler"
    return "wrangler"


def login_hint(command: list[str] | None = None) -> str:
    """What to do when wrangler is not logged in, with the login command as the user can type it."""
    return (f"wrangler is not logged in to Cloudflare. Open a terminal and run: {typed_command(command)} login (it "
            "opens the browser to sign in; the program never sees the credentials), then try again.")


LOGIN_HINT = login_hint()
_NOT_LOGGED = re.compile(r"(?i)not (logged in|authenticated)|\"loggedIn\"\s*:\s*false")


def legacy_folder(home: str | os.PathLike) -> Path | None:
    """``~/.wrangler`` holding no ``config`` folder: made by running wrangler with the home as working folder (its
    ``cache``, ``logs``), it makes wrangler take it as its configuration folder, find no login there and answer "Not
    logged in" although the login exists elsewhere. Only ``is_dir()`` checks: nothing inside is read (since
    1.10.1)."""
    folder = Path(home) / ".wrangler"
    try:
        return folder if folder.is_dir() and not (folder / "config").is_dir() else None
    except OSError:
        return None


def legacy_folder_hint(home: str | os.PathLike) -> str:
    """What to do about :func:`legacy_folder`, or "" when there is none."""
    folder = legacy_folder(home)
    if folder is None:
        return ""
    return (f" If you logged in already: the folder {folder} (only a cache and logs, made by running wrangler in the "
            "home folder) hides the login from wrangler; delete it and check again (the login is kept elsewhere).")


def _home() -> Path:
    return Path.home()


def _failure_text(out: str) -> str:
    """The head of the error of a wrangler output, scrubbed, with :func:`legacy_folder_hint` when it says "Not
    logged in"."""
    text = _last_lines(scrub(out))
    if _NOT_LOGGED.search(out or ""):
        text += legacy_folder_hint(_home())
    return text


# --------------------------------------------------------------------------------------------- scrubbing the output
_PUBLIC_HOST = re.compile(r"https?://[A-Za-z0-9.-]+\.(?:pages\.dev|r2\.dev)\b[^\s'\"<>]*")
_SECRETS = (
    (re.compile(r"(?i)\b(bearer)(\s+)([^\s'\",]+)"), r"\1\2[hidden]"),
    (re.compile(r"(?i)\b(token|api[_-]?token|api[_-]?key|authorization|secret|password|x-auth-key|"
                r"cloudflare_api_token|cloudflare_api_key|cf_api_token|account[_ -]?id)(\"?\s*[:=]\s*\"?)"
                r"([^\s'\",]+)"), r"\1\2[hidden]"),
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "[hidden]"),  # API tokens, OAuth tokens, long keys
    (re.compile(r"\b[0-9a-fA-F]{32}\b"), "[hidden]"),  # account ids, zone ids, access keys
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[e-mail]"),
)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def scrub(text: str) -> str:
    """``text`` (the output of wrangler) with whatever looks like a token, a key, an account id or an e-mail address
    replaced; the public addresses of the site (``*.pages.dev``, ``*.r2.dev``) are kept."""
    text = _ANSI.sub("", str(text or ""))
    kept: list[str] = []

    def keep(m):
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"

    text = _PUBLIC_HOST.sub(keep, text)
    for rx, repl in _SECRETS:
        text = rx.sub(repl, text)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], text)


# ------------------------------------------------------------------------------------------------- the site folder
@dataclass
class Entry:
    path: str  # relative, posix
    size: int
    sha256: str = ""


def _hidden(rel: Path) -> bool:
    return any(part.startswith(".") for part in rel.parts)


def site_files(folder: str | os.PathLike) -> list[Entry]:
    """The files of a site folder that are published: all but the hidden ones (``.ppg-publish.json``, ``.DS_Store``,
    the files of git), sorted."""
    folder = Path(folder)
    out = []
    for f in sorted(folder.rglob("*")):
        rel = f.relative_to(folder)
        if f.is_file() and not f.is_symlink() and not _hidden(rel):
            out.append(Entry(rel.as_posix(), f.stat().st_size))
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_state(folder: str | os.PathLike) -> dict:
    try:
        data = json.loads((Path(folder) / STATE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(folder: Path, state: dict) -> None:
    tmp = folder / (STATE + ".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, folder / STATE)


def _manifest(folder: Path) -> dict:
    from .site import FORMAT, MANIFEST

    try:
        data = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("format") == FORMAT else {}


def site_frame(folder: str | os.PathLike) -> str:
    """``"local"`` or ``"crs"`` as ``site.json`` says; anything else counts as ``"crs"`` (the safe side)."""
    frame = _manifest(Path(folder)).get("frame")
    return "local" if frame == "local" else "crs"


def default_project(name: str) -> str:
    """A name for the Pages project from ``name`` (a folder, a project): lowercase letters, digits, dashes."""
    import unicodedata

    text = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:58].strip("-")
    return text or "rilievo"


# ----------------------------------------------------------------------------------------------------- the plan
@dataclass
class Limit:
    key: str
    value: float
    limit: float
    ok: bool


@dataclass
class Plan:
    """What :func:`publish` would do with a site folder: nothing is sent to make it."""

    folder: Path
    project: str
    bucket: str | None
    data: str  # where the data go: "pages" or "r2" (``auto`` decided)
    data_url: str | None  # the address of the data given by the user (a custom domain of the bucket)
    frame: str
    title: str = ""
    pages_files: list[Entry] = field(default_factory=list)
    r2_files: list[Entry] = field(default_factory=list)
    upload: list[str] = field(default_factory=list)  # R2 keys to send (new or changed)
    unchanged: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)  # R2 keys sent before and no longer in the site
    limits: list[Limit] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # what stops the publication
    warnings: list[str] = field(default_factory=list)
    published: dict | None = None  # the last publication of this folder (from the state file)
    codes: list[tuple[str, str, tuple]] = field(default_factory=list)  # ("problem"|"warning", key of ISSUES, values)

    def add(self, kind: str, code: str, *values) -> None:
        text = ISSUES[code].format(*(f"{v:,}" if isinstance(v, int) else v for v in values))
        (self.problems if kind == "problem" else self.warnings).append(text)
        self.codes.append((kind, code, tuple(str(v) for v in values)))

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def pages_bytes(self) -> int:
        return sum(e.size for e in self.pages_files)

    @property
    def r2_bytes(self) -> int:
        return sum(e.size for e in self.r2_files)

    @property
    def upload_bytes(self) -> int:
        sizes = {e.path: e.size for e in self.r2_files}
        return sum(sizes.get(k, 0) for k in self.upload)

    @property
    def class_a(self) -> int:
        """R2 writes of this publication: the objects sent, the CORS rule, the public address, the bucket."""
        return len(self.upload) + 3 if self.data == "r2" else 0

    @property
    def public(self) -> list[str]:
        """What becomes readable by anyone, in plain English."""
        out = [f"every file of the page and the manifest ({len(self.pages_files)} files) at the pages.dev address "
               f"Cloudflare gives the project ({self.project}.pages.dev when the name is free, else with a suffix)"]
        if self.data == "r2":
            where = self.data_url or "the public r2.dev address of the bucket, turned on for it"
            out.append(f"every object of the bucket {self.bucket} (the {len(self.r2_files)} data files of the site), "
                       f"at {where}")
        if self.frame == "crs":
            out.append("the true coordinates and the reference system of the survey (the site was built with the "
                       "frame crs)")
        else:
            out.append("coordinates in the local frame of the site (shifted, no reference system): the place of the "
                       "survey is not written")
        return out

    def lines(self) -> list[str]:
        """The plan in English, for the command line and the log."""
        mb = lambda b: f"{b / 1e6:.1f} MB"  # noqa: E731
        out = [f"site {self.title or self.folder.name}: frame {self.frame}"]
        out.append(f"Cloudflare Pages, project {self.project}: {len(self.pages_files)} files, {mb(self.pages_bytes)}")
        if self.data == "r2":
            out.append(f"R2, bucket {self.bucket}: {len(self.r2_files)} data files, {mb(self.r2_bytes)}; to send "
                       f"{len(self.upload)} ({mb(self.upload_bytes)}), unchanged {len(self.unchanged)}, to delete "
                       f"{len(self.remove)}")
            where = self.data_url or ("r2.dev public address of the bucket (rate-limited, meant for development: a "
                                      "custom domain is better for a lasting site)")
            out.append(f"data address: {where}")
        for lim in self.limits:
            out.append(f"  {'ok  ' if lim.ok else 'OVER'} {limit_text(lim)}")
        out.append("public after publishing:")
        out += [f"  - {p}" for p in self.public]
        out += [f"warning: {w}" for w in self.warnings]
        out += [f"cannot publish: {p}" for p in self.problems]
        return out

    def as_dict(self) -> dict:
        return {"project": self.project, "bucket": self.bucket, "data": self.data, "data_url": self.data_url,
                "frame": self.frame, "pages_files": len(self.pages_files), "pages_bytes": self.pages_bytes,
                "r2_files": len(self.r2_files), "r2_bytes": self.r2_bytes, "upload": len(self.upload),
                "upload_bytes": self.upload_bytes, "remove": len(self.remove),
                "limits": [vars(x) for x in self.limits], "problems": list(self.problems),
                "warnings": list(self.warnings), "public": self.public}


def limit_text(lim: Limit) -> str:
    v, n = lim.value, lim.limit
    return {"pages_files": f"files on Pages {int(v):,} of {int(n):,} (Free plan; {PAGES_PAID_FILES:,} on paid plans)",
            "pages_file_bytes": f"largest file on Pages {v / 2**20:.1f} MiB of {n / 2**20:.0f} MiB",
            "r2_put_bytes": f"largest file to R2 {v / 1e6:.1f} MB of {n / 1e6:.0f} MB (one wrangler upload)",
            "r2_storage": f"R2 storage {v / 1e9:.2f} GB of {n / 1e9:.0f} GB-month free",
            "r2_class_a": f"R2 writes of this publication {int(v):,} of {int(n):,} free a month",
            "r2_class_b": f"R2 reads for one visit that opens every layer ~{int(v):,} of {int(n):,} free a month",
            }.get(lim.key, f"{lim.key} {v} of {n}")


ISSUES = {  # what a plan says, in English with its values; the window writes them in its language by their key
    "data_mode": "data must be one of {0}",
    "not_site": "{0} is not a site built by this program (no site.json): build it first",
    "project_name": "the name of the Pages project: lowercase letters, digits and dashes, not at the ends (it "
                    "becomes <name>.pages.dev)",
    "pages_file_bytes": "{0} files are larger than the {1} MiB a file Pages takes ({2}): send the data to R2 (a "
                        "bucket) or build a smaller site",
    "page_file_bytes": "{0} files outside data/ are larger than the {1} MiB a file Pages takes ({2})",
    "pages_files": "{0} files on Pages, more than the {1} of the Free plan: send the data to R2 (a bucket)",
    "page_files": "{0} files on Pages, more than the {1} of the Free plan",
    "no_bucket": "the data must go to R2 (files above the limits of Pages): name a bucket",
    "bucket_name": "the name of the bucket: 3-63 lowercase letters, digits and dashes, not at the ends",
    "data_url": "the address of the data must start with https:// (the custom domain of the bucket)",
    "r2_put_bytes": "files larger than the {0} MB wrangler sends to R2 in one go ({1}): build a smaller site "
                    "(budget) or send them with rclone",
    "r2_billed": "the data are above the 10 GB-month R2 keeps for free: the rest is billed",
    "r2_dev": "the data are served from the r2.dev address of the bucket: rate-limited and meant for development; "
              "connect a custom domain to the bucket for a lasting site and publish with its address",
    "r2_subscription": "R2 needs an R2 subscription on the account (checkout in the Cloudflare dashboard, once)",
    "cors_replaced": "the CORS rule of the bucket {0} is replaced with the one of this site: use a bucket for this "
                     "site alone",
    "true_coordinates": "the site holds the TRUE coordinates of the survey: publishing tells anyone where it is",
}


def plan(folder: str | os.PathLike, project: str, bucket: str | None = None, data: str = "auto",
         data_url: str | None = None) -> Plan:
    """What publishing ``folder`` on the Pages project ``project`` would do (see the module docstring); no network,
    no wrangler. ``bucket``: the R2 bucket for the data (needed when they go to R2); ``data``: one of
    :data:`DATA_MODES`; ``data_url``: the https address under which the bucket is served (a custom domain the user
    connected to it), else the r2.dev address is turned on at publishing. The problems (which stop the publication)
    and the warnings are in :attr:`Plan.problems` and :attr:`Plan.warnings`, in English, and by key of
    :data:`ISSUES` with their values in :attr:`Plan.codes`."""
    folder = Path(folder)
    project = str(project or "").strip()
    bucket = str(bucket or "").strip() or None
    data_url = str(data_url or "").strip().rstrip("/") or None
    manifest = _manifest(folder)
    p = Plan(folder=folder, project=project, bucket=bucket, data="pages", data_url=data_url,
             frame=site_frame(folder), title=str(manifest.get("title") or ""))
    if data not in DATA_MODES:
        p.add("problem", "data_mode", ", ".join(DATA_MODES))
        data = "auto"
    if not manifest:
        p.add("problem", "not_site", folder)
        return p
    if not _PROJECT.match(project):
        p.add("problem", "project_name")
    files = site_files(folder)
    in_data = [e for e in files if e.path.startswith(DATA + "/")]
    rest = [e for e in files if not e.path.startswith(DATA + "/")]
    too_big = [e for e in files if e.size > PAGES_FILE_BYTES]
    if data == "auto":
        data = "r2" if (too_big or len(files) > PAGES_FREE_FILES) else "pages"
    p.data = data
    if data == "r2":
        p.pages_files, p.r2_files = rest, in_data
    else:
        p.pages_files = files
    largest = max((e.size for e in p.pages_files), default=0)
    p.limits.append(Limit("pages_files", len(p.pages_files), PAGES_FREE_FILES, len(p.pages_files) <= PAGES_FREE_FILES))
    p.limits.append(Limit("pages_file_bytes", largest, PAGES_FILE_BYTES, largest <= PAGES_FILE_BYTES))
    over = [e for e in p.pages_files if e.size > PAGES_FILE_BYTES]
    if over:
        names = ", ".join(e.path for e in over[:5]) + (" ..." if len(over) > 5 else "")
        p.add("problem", "pages_file_bytes" if data == "pages" else "page_file_bytes", len(over),
              PAGES_FILE_BYTES // 2**20, names)
    if len(p.pages_files) > PAGES_FREE_FILES:
        p.add("problem", "pages_files" if data == "pages" else "page_files", len(p.pages_files), PAGES_FREE_FILES)
    if data == "r2":
        if bucket is None:
            p.add("problem", "no_bucket")
        elif not _BUCKET.match(bucket):
            p.add("problem", "bucket_name")
        if data_url and not re.match(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s]*)?$", data_url):
            p.add("problem", "data_url")
        biggest = max((e.size for e in p.r2_files), default=0)
        p.limits.append(Limit("r2_put_bytes", biggest, R2_PUT_BYTES, biggest <= R2_PUT_BYTES))
        if biggest > R2_PUT_BYTES:
            p.add("problem", "r2_put_bytes", R2_PUT_BYTES // 10**6,
                  ", ".join(e.path for e in p.r2_files if e.size > R2_PUT_BYTES))
        state = read_state(folder).get("cloudflare") or {}
        sent = state.get("objects") if state.get("bucket") == bucket and isinstance(state.get("objects"), dict) \
            else {}
        for e in p.r2_files:
            e.sha256 = _sha256(folder / e.path)
            (p.unchanged if sent.get(e.path) == e.sha256 else p.upload).append(e.path)
        keys = {e.path for e in p.r2_files}
        p.remove = sorted(k for k in sent if k not in keys)
        p.limits.append(Limit("r2_storage", p.r2_bytes, R2_FREE_STORAGE, p.r2_bytes <= R2_FREE_STORAGE))
        p.limits.append(Limit("r2_class_a", p.class_a, R2_FREE_CLASS_A, p.class_a <= R2_FREE_CLASS_A))
        p.limits.append(Limit("r2_class_b", len(p.r2_files), R2_FREE_CLASS_B, len(p.r2_files) <= R2_FREE_CLASS_B))
        if p.r2_bytes > R2_FREE_STORAGE:
            p.add("warning", "r2_billed")
        if not data_url:
            p.add("warning", "r2_dev")
        p.add("warning", "r2_subscription")
        p.add("warning", "cors_replaced", bucket)
    if p.frame == "crs":
        p.add("warning", "true_coordinates")
    p.published = read_state(folder).get("published")
    return p


# ------------------------------------------------------------------------------------------------- wrangler
def extra_dirs() -> list[str]:
    """Folders where Node and wrangler usually are, beside the PATH (the app started from the Finder has a short
    PATH)."""
    home = Path.home()
    out = ["/opt/homebrew/bin", "/usr/local/bin", str(home / ".npm-global/bin"), str(home / ".volta/bin"),
           str(home / ".bun/bin"), str(home / ".local/bin"), str(home / "Library/pnpm"), str(home / ".yarn/bin")]
    out += sorted(glob.glob(str(home / ".nvm/versions/node/*/bin")), reverse=True)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            out.append(str(Path(appdata) / "npm"))
        out.append(r"C:\Program Files\nodejs")
    return [d for d in out if Path(d).is_dir()]


def _search_path(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    parts = [p for p in str(env.get("PATH", "")).split(os.pathsep) if p]
    return os.pathsep.join(parts + [d for d in extra_dirs() if d not in parts])


def find_wrangler(env: dict | None = None) -> list[str] | None:
    """The command that runs wrangler: ``PPG_WRANGLER``, ``wrangler`` on the PATH or in :func:`extra_dirs`, else
    ``npx --no wrangler`` (``--no``: npx never installs it); None without either. Absolute paths."""
    env = os.environ if env is None else env
    given = env.get("PPG_WRANGLER")
    if given and Path(given).is_file():
        return [str(Path(given).resolve())]
    path = _search_path(env)
    found = shutil.which("wrangler", path=path)
    if found:
        return [str(Path(found).absolute())]
    npx = shutil.which("npx", path=path)
    if npx:
        # "--": npx would take --version for itself and answer the version of npm
        return [str(Path(npx).absolute()), "--no", "--", "wrangler"]
    return None


@dataclass
class Status:
    state: str  # "missing", "logged_out", "ready"
    version: str = ""
    command: list[str] = field(default_factory=list)
    text: str = ""
    legacy_folder: str = ""  # ~/.wrangler hiding the login (:func:`legacy_folder`), when "Not logged in"


def wrangler_cache_dir() -> Path:
    """Where wrangler keeps its cache when the program runs it: a folder of this user in the temporary directory.
    wrangler 4 writes it in ``.wrangler/cache`` under the working folder, which is ``/`` (not writable) for the
    application started from the Finder (since 1.10.1)."""
    who = str(os.getuid()) if hasattr(os, "getuid") else (os.environ.get("USERNAME") or "user")
    return Path(tempfile.gettempdir()) / f"ppg-wrangler-cache-{who}"


def _cache_env(env: dict) -> None:
    """``WRANGLER_CACHE_DIR`` set to :func:`wrangler_cache_dir`, made on demand, unless the user set one; left
    unset when the folder cannot be made."""
    if env.get("WRANGLER_CACHE_DIR"):
        return
    try:
        folder = wrangler_cache_dir()
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not os.access(folder, os.W_OK):
            return
    except OSError:
        return
    env["WRANGLER_CACHE_DIR"] = str(folder)


class Wrangler:
    """The user's wrangler, run as a child process: stdin closed (no prompt waits), the output read and scrubbed,
    cancelled through ``progress``. The environment is passed as it is (wrangler reads its own login); the program
    adds only ``WRANGLER_SEND_METRICS=false``, no colours and a writable ``WRANGLER_CACHE_DIR`` when none is set
    (:func:`wrangler_cache_dir`: the child runs without a working folder of its own)."""

    def __init__(self, command: list[str] | None = None, env: dict | None = None, timeout: float = 1800.0):
        self.command = list(command) if command else find_wrangler(env)
        self.timeout = timeout
        base = dict(os.environ if env is None else env)
        path = _search_path(base)
        if self.command:
            first = str(Path(self.command[0]).parent)
            path = os.pathsep.join([first] + [p for p in path.split(os.pathsep) if p != first])
        base.update({"PATH": path, "WRANGLER_SEND_METRICS": "false", "NO_COLOR": "1", "FORCE_COLOR": "0"})
        _cache_env(base)
        self.env = base
        self.transcript: list[str] = []  # the commands run and their output, scrubbed

    def run(self, args: list[str], progress=None, check: bool = False, keep: bool = True,
            timeout: float | None = None) -> tuple[int, str]:
        """``wrangler <args>``: (exit code, output). The output is returned raw (to find addresses in it) and kept
        in :attr:`transcript` scrubbed (unless ``keep`` is False: then not at all). ``check``: an exit code other
        than 0 raises :class:`WranglerFailed`."""
        if not self.command:
            raise WranglerMissing(INSTALL_HINT)
        progress = as_progress(progress)
        line = "wrangler " + " ".join(args)
        if keep:
            self.transcript.append("$ " + scrub(line))
        try:  # posix_spawn: absolute executable, close_fds=False, no cwd (see the notes of the project on fork)
            proc = subprocess.Popen(self.command + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=self.env, close_fds=False)
        except OSError as exc:
            raise WranglerMissing(f"{INSTALL_HINT} ({exc.strerror or exc})") from None
        chunks: list[bytes] = []
        reader = threading.Thread(target=lambda: chunks.extend(iter(lambda: proc.stdout.read(4096), b"")),
                                  daemon=True)
        reader.start()
        limit = time.monotonic() + (timeout or self.timeout)
        try:
            while proc.poll() is None:
                if progress.cancel_requested:
                    raise Cancelled("cancelled")
                if time.monotonic() > limit:
                    raise WranglerFailed(f"{line}: no answer after {int(timeout or self.timeout)} s")
                time.sleep(0.05)
        except BaseException:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise
        finally:
            reader.join(5)
        out = b"".join(chunks).decode("utf-8", "replace")
        if keep:
            text = scrub(out).strip()
            if text:
                self.transcript.append(text)
            if proc.returncode:
                self.transcript.append(f"(exit code {proc.returncode})")
        if check and proc.returncode:
            raise WranglerFailed(f"{line} failed (exit code {proc.returncode}): {_failure_text(out)}",
                                 scrub(out))
        return proc.returncode, out

    def status(self, progress=None) -> Status:
        """Whether wrangler is there (``wrangler --version``) and logged in (``wrangler whoami --json``: its output,
        which names the account, is neither kept nor shown; only the exit code counts)."""
        if not self.command:
            return Status("missing", text=INSTALL_HINT)
        try:
            code, out = self.run(["--version"], progress, keep=False, timeout=120)
        except WranglerMissing:
            return Status("missing", command=self.command, text=INSTALL_HINT)
        version = _version(out)
        if code or not version:
            return Status("missing", command=self.command, text=INSTALL_HINT)
        code, out = self.run(["whoami", "--json"], progress, keep=False, timeout=120)
        if code and re.search(r"(?i)unknown (argument|option)", out):  # an old wrangler without --json
            code, out = self.run(["whoami"], progress, keep=False, timeout=120)
            if _NOT_LOGGED.search(out):
                code = 1
        if code:
            legacy = legacy_folder(_home()) if _NOT_LOGGED.search(out) else None
            return Status("logged_out", version, self.command,
                          login_hint(self.command) + (legacy_folder_hint(_home()) if legacy else ""),
                          legacy_folder=str(legacy) if legacy else "")
        return Status("ready", version, self.command, f"wrangler {version}, logged in")

    def login(self, progress=None, timeout: float | None = None) -> Status:
        """``wrangler login`` (since 1.10.1): wrangler opens the browser, the user signs in there, wrangler keeps
        its own login. Its output (the sign-in address, the account) is neither kept, shown, logged nor returned:
        only the exit code counts. Cancelled through ``progress``; after ``timeout`` seconds (:data:`LOGIN_TIMEOUT`)
        wrangler is stopped. Then :meth:`status` again."""
        if not self.command:
            raise WranglerMissing(INSTALL_HINT)
        progress = as_progress(progress)
        limit = timeout or LOGIN_TIMEOUT
        progress(0.0, "Cloudflare login: complete it in the browser")
        try:
            code, out = self.run(["login"], progress, keep=False, timeout=limit)
        except WranglerFailed:  # its message names only the command; said again without it
            raise WranglerFailed(f"Cloudflare login not completed within {int(limit)} s") from None
        del out
        if code:
            raise WranglerFailed(f"Cloudflare login not completed (wrangler exit code {code})")
        progress(0.9, "Cloudflare login: checking it")
        return self.status(progress)


def _version(text: str) -> str:
    m = re.search(r"(\d+\.\d+\.\d+)", text or "")
    return m.group(1) if m else ""


def _last_lines(text: str, n: int = 6) -> str:
    """The error of a wrangler output in one short line: from the first line naming ``ERROR`` (``✘ [ERROR] ...``)
    up to 8 non-empty lines, so the head of the error is never cut; without one, the last ``n`` lines."""
    lines = [ln.strip() for ln in _ANSI.sub("", text or "").splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        if "ERROR" in ln:
            return " | ".join(lines[i:i + 8])
    return " | ".join(lines[-n:])


def _json_in(text: str):
    """The JSON value printed by wrangler, past any banner line before it."""
    for start in (i for i, c in enumerate(text) if c in "[{"):
        try:
            return json.loads(text[start:])
        except ValueError:
            try:
                return json.JSONDecoder().raw_decode(text[start:])[0]
            except ValueError:
                continue
    return None


def _pages_host(text: str, project: str) -> str | None:
    """The ``<sub>.pages.dev`` host of ``project`` in ``text`` (a deployment address ``<hash>.<sub>.pages.dev`` gives
    ``<sub>.pages.dev``)."""
    hosts = re.findall(r"(?:https?://)?((?:[a-z0-9-]+\.)+pages\.dev)\b", text or "")
    best = None
    for host in hosts:
        labels = host.split(".")[:-2]
        sub = labels[-1]
        if sub == project or sub.startswith(project + "-") or best is None:
            best = f"{sub}.pages.dev"
            if sub == project or sub.startswith(project + "-"):
                return best
    return best


def _dev_url_on(text: str) -> bool:
    """Whether the answer of ``r2 bucket dev-url get`` says the r2.dev address is on: an r2.dev address in it and no
    "disabled" (the wording of wrangler is not verified on a real account: anything else counts as off, so the
    program records the address as turned on by itself and turns it off when the site is removed)."""
    return bool(_r2_dev(text)) and not re.search(r"(?i)\bdisabled\b|not enabled", text or "")


def _r2_dev(text: str) -> str | None:
    m = re.search(r"https://[A-Za-z0-9.-]+\.r2\.dev\b", text or "")
    return m.group(0) if m else None


# ---------------------------------------------------------------------------------------------- the published copy
def _with_data_base(manifest: dict, base: str) -> dict:
    """``manifest`` with every ``data/...`` address under ``base`` and ``data_base`` set."""

    def walk(value):
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, str) and value.startswith(DATA + "/"):
            return f"{base}/{value}"
        return value

    out = walk(manifest)
    out["data_base"] = base
    return out


def _origin(url: str) -> str:
    m = re.match(r"^(https?://[^/]+)", url)
    return m.group(1) if m else url


def _page_with_origin(page: str, origin: str) -> str:
    """The Content-Security-Policy of the page opened to ``origin`` for data and images (scripts stay 'self')."""
    page = page.replace("default-src 'self'", f"default-src 'self' {origin}", 1)
    return page.replace("img-src 'self'", f"img-src 'self' {origin}", 1)


HEADERS = (f"/data/*\n  Cache-Control: {CACHE_DATA}\n"
           "/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: no-referrer\n")


def stage(p: Plan, data_base: str | None, where: str | os.PathLike | None = None) -> Path:
    """The folder ``wrangler pages deploy`` sends: links to the files of :attr:`Plan.pages_files` (copies across
    disks), ``site.json`` and ``index.html`` rewritten for the data in R2, ``_headers`` with the cache of the data
    (unless the site has its own). Beside the site folder (same disk) unless ``where`` is given."""
    from .site import MANIFEST

    try:
        root = Path(tempfile.mkdtemp(prefix=f".{p.folder.name}.publish-", dir=str(where or p.folder.parent)))
    except OSError:  # a folder that cannot be written beside the site: copies in the temporary folder
        root = Path(tempfile.mkdtemp(prefix="ppg-publish-"))
    for e in p.pages_files:
        src, dst = p.folder / e.path, root / e.path
        dst.parent.mkdir(parents=True, exist_ok=True)
        if data_base and e.path in (MANIFEST, "index.html"):
            text = src.read_text(encoding="utf-8")
            if e.path == MANIFEST:
                text = json.dumps(_with_data_base(json.loads(text), data_base), ensure_ascii=False)
            else:
                text = _page_with_origin(text, _origin(data_base))
            dst.write_text(text, encoding="utf-8")
            continue
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    if not (root / "_headers").exists():
        (root / "_headers").write_text(HEADERS, encoding="utf-8")
    return root


def cors_rule(origins: list[str]) -> dict:
    """The CORS rule of the bucket (the JSON ``wrangler r2 bucket cors set --file`` takes, as the Cloudflare API
    names its keys): GET and HEAD from the page's origins, with Range, and the headers the viewer reads."""
    return {"rules": [{"allowed": {"origins": sorted(set(origins)), "methods": ["GET", "HEAD"],
                                   "headers": ["Range", "If-None-Match", "If-Modified-Since"]},
                       "exposeHeaders": ["Content-Range", "Content-Length", "Accept-Ranges", "ETag",
                                         "Content-Type"],
                       "maxAgeSeconds": 3600}]}


def content_type(path: str) -> str:
    return _TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


# ------------------------------------------------------------------------------------------------- publishing
@dataclass
class Result:
    address: str | None  # None: not found in what wrangler answered (shown in the dashboard), never guessed
    project: str
    data: str
    data_address: str | None = None
    bucket: str | None = None
    sent: int = 0
    unchanged: int = 0
    removed: int = 0
    when: str = ""
    frame: str = "local"
    transcript: list[str] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)  # what could not be read in an answer of wrangler (English)
    unread_output: list[str] = field(default_factory=list)  # those answers whole, scrubbed: for the log

    def record(self) -> dict:
        """What is kept with the project (``meta["web_site"]["published"]``): addresses, names, date; no secret."""
        return {"host": "cloudflare", "address": self.address, "project": self.project, "data": self.data,
                "data_address": self.data_address, "bucket": self.bucket, "when": self.when, "frame": self.frame}


def check_consent(frame: str, public: bool, coordinates: bool) -> None:
    """:class:`ConsentError` unless ``public``, and ``coordinates`` too for a site with true coordinates."""
    if not public:
        raise ConsentError("publishing makes the site public: anyone with the address can open it. Confirm it "
                           "explicitly (--yes-public on the command line)")
    if frame != "local" and not coordinates:
        raise ConsentError("this site holds the TRUE coordinates of the survey (built with --frame crs): anyone "
                           "will know where it is. Confirm it explicitly (--yes-true-coordinates), or build the "
                           "site again in the local frame")


def _project_entry(listing, project: str) -> dict | None:
    if isinstance(listing, dict):
        listing = listing.get("result") or listing.get("projects") or [listing]
    for entry in listing or []:
        if isinstance(entry, dict) and any(str(v).strip() == project for v in entry.values()):
            return entry
    return None


def _production_branch(entry: dict) -> str:
    for key, value in entry.items():
        if re.sub(r"[^a-z]", "", str(key).lower()) == "productionbranch" and str(value).strip():
            return str(value).strip()
    return BRANCH


def publish(p: Plan, public: bool = False, coordinates: bool = False, wrangler: Wrangler | None = None,
            origins: list[str] | None = None, progress=None) -> Result:
    """Carry out the plan ``p`` (:func:`plan`) with the user's wrangler. ``public`` and ``coordinates``: the
    consent (:func:`check_consent`); ``origins``: more origins of the page for the CORS rule of the bucket (a custom
    domain of the Pages project). Raises :class:`ConsentError`, :class:`WranglerMissing`, :class:`NotLoggedIn`,
    :class:`WranglerFailed`, :class:`ValueError` (a plan with problems) or :class:`Cancelled`.

    The wrangler commands, in order: ``whoami --json``; ``pages project list --json`` and, for a new project,
    ``pages project create <project> --production-branch main``; with R2: ``r2 bucket info <bucket>`` and, for a
    new bucket, ``r2 bucket create <bucket>``, ``r2 object put <bucket>/<key> --file <file> --content-type <type>
    --cache-control <...> --remote`` for each new or changed file, ``r2 object delete <bucket>/<key> --remote`` for
    each file gone, ``r2 bucket dev-url enable <bucket> -y`` and ``r2 bucket dev-url get <bucket>`` without a
    ``data_url``, ``r2 bucket cors set <bucket> --file <rule.json> -y``; last ``pages deploy <staged folder>
    --project-name <project> --branch <production branch> --commit-dirty=true``."""
    progress = as_progress(progress)
    check_consent(p.frame, public, coordinates)
    if p.problems:
        raise ValueError("; ".join(p.problems))
    w = wrangler or Wrangler()
    if not w.command:
        raise WranglerMissing(INSTALL_HINT)
    progress(0.01, "Cloudflare: checking wrangler and the login")
    status = w.status(progress)
    if status.state == "missing":
        raise WranglerMissing(status.text)
    if status.state == "logged_out":
        raise NotLoggedIn(status.text)
    w.transcript.append(f"wrangler {status.version}: logged in")
    folder = p.folder
    state = read_state(folder)

    unread: list[str] = []
    unread_output: list[str] = []

    def not_read(what: str, out: str) -> None:  # an answer of wrangler not as expected: said, and kept whole
        unread.append(what)
        unread_output.append(f"[{what}]\n{scrub(out).strip()}")

    def listed(out: str):
        """(found, entry of the project): found is False when the answer is not a list of projects."""
        listing = _json_in(out)
        if not isinstance(listing, (list, dict)):
            return False, None
        return True, _project_entry(listing, p.project)

    # the Pages project
    progress(0.03, f"Cloudflare Pages: project {p.project}")
    _code, out = w.run(["pages", "project", "list", "--json"], progress, check=True)
    if state.pop("removing", None) is not None:  # a removal left halfway (unpublish): from here on this publication
        _write_state(folder, state)  # makes again what that removed, so all its steps are to do again
    ok, entry = listed(out)
    if not ok:
        not_read("the list of the Pages projects (pages project list --json)", out)
    branch = BRANCH
    if entry is None:
        code, out = w.run(["pages", "project", "create", p.project, "--production-branch", BRANCH], progress)
        if code and not (not ok and re.search(r"(?i)already (exists|taken|in use)", out)):
            raise WranglerFailed(f"wrangler pages project create {p.project} failed (exit code {code}): "
                                 f"{_failure_text(out)}", scrub(out))
        known = _pages_host(out, p.project)
        if known is None:  # the project exists now: its address from the list
            _code, again = w.run(["pages", "project", "list", "--json"], progress)
            ok2, entry2 = listed(again)
            known = _pages_host(json.dumps(entry2), p.project) if entry2 else None
            if entry2:
                branch = _production_branch(entry2)
            if known is None:
                not_read("the address of the new project (pages project create, pages project list --json)",
                         out + "\n" + again)
    else:
        known = _pages_host(json.dumps(entry), p.project)
        branch = _production_branch(entry)
        if known is None:
            not_read("the pages.dev address in the list of the projects (pages project list --json)", out)
    # Cloudflare adds a suffix to the subdomain when the name is taken: an address is read, never guessed. The CORS
    # rule of a bucket needs one before the deploy: the plain name then, said among what was not read
    host = known or f"{p.project}.pages.dev"

    data_address = None
    sent = removed = 0
    upload, remove, unchanged = list(p.upload), list(p.remove), len(p.unchanged)
    if p.data == "r2":
        bucket = p.bucket
        code, _out = w.run(["r2", "bucket", "info", bucket], progress)
        created = False
        if code:
            w.run(["r2", "bucket", "create", bucket], progress, check=True)
            created = True
        cf = state.get("cloudflare") if isinstance(state.get("cloudflare"), dict) else {}
        if cf.get("bucket") != bucket:
            # a new record: bucket_created and dev_url_enabled are written from here on, their absence means "no".
            # A record of the same bucket written before 1.10.2 gets no "records": what it does not say stays
            # unknown, and unpublish then leaves the bucket and its r2.dev address as they are
            cf = {"bucket": bucket, "objects": {}, "records": RECORDS}
        objects = cf.setdefault("objects", {})
        cf["project"] = p.project
        if created:  # what the program made, so that removing the site (unpublish) may undo it (since 1.10.2)
            cf["bucket_created"] = True
        state["cloudflare"] = cf
        if created and objects:  # a new bucket holds nothing of what an earlier publication recorded as sent (the
            objects.clear()  # bucket was deleted meanwhile, in the dashboard): every file is sent again
            upload, remove, unchanged = [e.path for e in p.r2_files], [], 0
            progress(0.05, f"R2: the bucket {bucket} is new: every file is sent again")
        _write_state(folder, state)
        sizes = {e.path: e for e in p.r2_files}
        total = max(1, sum(sizes[k].size for k in upload))
        done = 0
        for key in upload:
            e = sizes[key]
            progress.sub(0.05 + 0.8 * done / total, 0.8 * e.size / total)(0.0, f"R2: sending {key}")
            w.run(["r2", "object", "put", f"{bucket}/{key}", "--file", str((folder / key).resolve()),
                   "--content-type", content_type(key), "--cache-control", CACHE_DATA, "--remote"], progress,
                  check=True)
            objects[key] = e.sha256
            _write_state(folder, state)  # a publication cancelled or failed starts again from here
            done += e.size
            sent += 1
        for key in remove:
            progress(0.86, f"R2: deleting {key}")
            code, _out = w.run(["r2", "object", "delete", f"{bucket}/{key}", "--remote"], progress)
            objects.pop(key, None)
            _write_state(folder, state)
            removed += 1
        progress(0.87, f"R2: the public address of {bucket}")
        if p.data_url:
            data_address = p.data_url
        else:
            if not cf.get("dev_url_enabled"):  # on already before the program (an existing bucket): not ours
                was_on = False
                if not created:
                    code, before = w.run(["r2", "bucket", "dev-url", "get", bucket], progress)
                    was_on = not code and _dev_url_on(before)
                if not was_on:
                    cf["dev_url_enabled"] = True
            w.run(["r2", "bucket", "dev-url", "enable", bucket, "-y"], progress, check=True)
            _code, out = w.run(["r2", "bucket", "dev-url", "get", bucket], progress, check=True)
            data_address = _r2_dev(out)
            if not data_address:
                raise WranglerFailed(f"the r2.dev address of the bucket {bucket} was not found in the answer of "
                                     "wrangler: turn it on in the dashboard (R2 > bucket > Settings) or publish with "
                                     "the address of a custom domain", scrub(out))
        cf["data_address"] = data_address
        _write_state(folder, state)
        rule = cors_rule([f"https://{host}"] + [_origin(o.rstrip("/")) for o in origins or []])
        fd, rule_path = tempfile.mkstemp(prefix="ppg-cors-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(rule, fh)
            w.run(["r2", "bucket", "cors", "set", bucket, "--file", rule_path, "-y"], progress, check=True)
        finally:
            Path(rule_path).unlink(missing_ok=True)

    progress(0.9, f"Cloudflare Pages: sending {len(p.pages_files)} files")
    staged = stage(p, data_address)
    try:
        _code, out = w.run(["pages", "deploy", str(staged), "--project-name", p.project, "--branch", branch,
                            "--commit-dirty=true"], progress, check=True)
    finally:
        shutil.rmtree(staged, ignore_errors=True)
    if known is None:
        known = _pages_host(out, p.project)
        if known is None:  # the deploy said no address either: the list of the projects, read once more
            _code, again = w.run(["pages", "project", "list", "--json"], progress)
            _ok, entry = listed(again)
            known = _pages_host(json.dumps(entry), p.project) if entry else None
            if known is None:
                not_read("the address of the site (pages deploy, pages project list --json)", out + "\n" + again)
        if known is not None and p.data == "r2" and known != host:
            unread.append(f"the CORS rule of the bucket was written for https://{host} before the address was "
                          f"known (https://{known}): publish again to correct it")
    address = f"https://{known}" if known else None
    result = Result(address=address, project=p.project, data=p.data, data_address=data_address,
                    bucket=p.bucket if p.data == "r2" else None, sent=sent, unchanged=unchanged,
                    removed=removed, when=datetime.now().isoformat(timespec="seconds"), frame=p.frame,
                    transcript=list(w.transcript), unread=unread, unread_output=unread_output)
    state["published"] = result.record()
    _write_state(folder, state)
    progress(1.0, f"Cloudflare: published at {address}" if address else
             "Cloudflare: published; the address is shown in the Cloudflare dashboard or by wrangler")
    return result


def record_published(document, result: Result) -> dict:
    """The publication kept with the project in ``document.meta["web_site"]["published"]`` (addresses, names,
    date: no secret). Returned."""
    from .site import META_KEY

    record = document.meta.get(META_KEY)
    if not isinstance(record, dict):
        record = {}
        document.meta[META_KEY] = record
    record["published"] = result.record()
    return record["published"]


def published_record(document) -> dict | None:
    from .site import META_KEY

    record = document.meta.get(META_KEY)
    pub = record.get("published") if isinstance(record, dict) else None
    return pub if isinstance(pub, dict) and pub.get("host") else None


def set_address(document, address: str) -> dict:
    """The address of the published site typed by the user (when wrangler did not say it): ``https://...``."""
    address = str(address or "").strip().rstrip("/")
    if not re.match(r"^https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?(?:/[^\s]*)?$", address):
        raise ValueError("the address of the site must be https://... (for example https://name.pages.dev)")
    pub = published_record(document)
    if pub is None:
        raise ValueError("the site was not published from this project")
    pub["address"] = address
    pub["address_typed"] = True
    return pub


def stale_publication(document) -> bool:
    """The site was built again after it was published (the published copy is older)."""
    from .site import META_KEY

    record = document.meta.get(META_KEY) or {}
    pub = published_record(document)
    return bool(pub and record.get("when") and str(record["when"]) > str(pub.get("when") or ""))


# ------------------------------------------------------------------------------------------- removing the site
# The flags that skip wrangler's own confirmation, as Cloudflare's documentation of wrangler names them
# (SOURCES["wrangler_pages"], ["wrangler_r2"]): "pages project delete <name> --yes", "r2 bucket dev-url disable
# <bucket> -y". Not checked against a real account: one constant each, to change in one place.
PAGES_DELETE_YES = "--yes"
DEV_URL_DISABLE_YES = "-y"
# What wrangler answers for a thing that is not there, which then counts as removed: Cloudflare's "Project not found.
# [...] [code: 8000007]", "The specified bucket does not exist. [code: 10006]", "The specified key does not exist.
# [code: 10007]". Only a project, a bucket, an object or a key said missing: another failure that happens to say
# "not found" must not pass for a removal. A wording not foreseen is then said as a failure (the dashboard is left)
_GONE = re.compile(r"(?i)\b(?:project|bucket|object|key)\b[^\n]{0,60}?\b(?:not found|does not exist)\b"
                   r"|\bno such (?:bucket|object|key|project)\b|\[code: (?:8000007|10006|10007)\]")
_NO_BUCKET = re.compile(r"(?i)\bbucket\b[^\n]{0,60}?\b(?:not found|does not exist)\b|\bno such bucket\b"
                        r"|\[code: 10006\]")


class NotPublished(ValueError):
    """No publication recorded in the site folder: the program removes only what it published itself."""


@dataclass
class Removal:
    """What :func:`unpublish` would remove from Cloudflare (:func:`removal_plan`): nothing is run to make it.

    ``dev_url``: ``"disable"`` (the program turned the r2.dev address on: it is turned off), ``"unknown"`` (a
    record written before 1.10.2 does not say: left as it is), ``"not_ours"`` (on before the program, or never
    turned on by it: left), ``"custom"`` (the data are served from the user's own domain: never touched), ``"done"``
    (turned off by an earlier run), ``"none"`` (no data in R2). ``bucket_rule``: ``"allowed"`` (the program created
    the bucket: it may be deleted, when asked, once every object the program sent is gone), ``"not_created"``,
    ``"unknown"`` (a record before 1.10.2), ``"done"``, ``"none"``. ``bucket_missing``: an earlier run did not find
    the bucket in the Cloudflare account, so nothing of it is left to remove."""

    folder: Path
    project: str | None
    bucket: str | None = None
    objects: list[str] = field(default_factory=list)  # the R2 keys the program recorded as sent, still there
    data_address: str | None = None
    dev_url: str = "none"
    bucket_rule: str = "none"
    delete_bucket: bool = False  # asked and allowed
    bucket_asked: bool = False
    project_done: bool = False
    bucket_missing: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def bucket_allowed(self) -> bool:
        return self.bucket_rule == "allowed"

    @property
    def confirm_name(self) -> str:
        """What the user types to confirm: the name of the Pages project (the bucket when no project is known)."""
        return self.project or self.bucket or ""

    def lines(self) -> list[str]:
        """The plan in English, for the command line and the log."""
        if self.problems:
            return [f"cannot remove: {p}" for p in self.problems]
        out = [f"site folder {self.folder} (kept on this computer)"]
        if self.project and not self.project_done:
            out.append(f"Cloudflare Pages: the project {self.project} is deleted, with all its deployments")
        elif self.project:
            out.append(f"Cloudflare Pages: the project {self.project} was deleted already")
        if self.bucket and self.bucket_missing:
            out.append(f"R2: the bucket {self.bucket} was not found in the Cloudflare account by an earlier run: "
                       "nothing of it is left to remove")
        elif self.bucket:
            out.append(f"R2, bucket {self.bucket}: {len(self.objects)} objects the program sent are deleted (other "
                       "objects of the bucket are never touched)")
            out.append({"disable": f"the public r2.dev address of the bucket is turned off "
                                   f"({self.data_address or 'r2.dev'})",
                        "unknown": "the r2.dev address of the bucket is left as it is: the record of this "
                                   "publication (before 1.10.2) does not say whether the program turned it on; turn "
                                   "it off in the dashboard (R2 > bucket > Settings) if you wish",
                        "not_ours": "the r2.dev address of the bucket is left as it is (the program did not turn "
                                    "it on)",
                        "custom": f"the data address {self.data_address} (your own domain) is left as it is",
                        "done": "the r2.dev address of the bucket was turned off already",
                        "none": "no public address of the bucket to turn off"}[self.dev_url])
            if self.delete_bucket:
                out.append(f"the bucket {self.bucket} is deleted once its objects are gone (it fails if it holds "
                           "objects the program did not send)")
            elif self.bucket_rule == "done":
                out.append(f"the bucket {self.bucket} was deleted already")
            else:
                why = {"allowed": "not asked (--delete-bucket)",
                       "not_created": "the program did not create it",
                       "unknown": "the record of this publication (before 1.10.2) does not say whether the program "
                                  "created it"}.get(self.bucket_rule, "")
                out.append(f"the bucket {self.bucket} is kept" + (f": {why}" if why else ""))
        out.append("this cannot be undone")
        return out

    def as_dict(self) -> dict:
        return {"project": self.project, "bucket": self.bucket, "objects": len(self.objects),
                "data_address": self.data_address, "dev_url": self.dev_url, "bucket_rule": self.bucket_rule,
                "delete_bucket": self.delete_bucket, "project_done": self.project_done,
                "bucket_missing": self.bucket_missing, "problems": list(self.problems)}


def _cf_records(state: dict) -> tuple[dict, dict]:
    cf = state.get("cloudflare") if isinstance(state.get("cloudflare"), dict) else {}
    pub = state.get("published") if isinstance(state.get("published"), dict) else {}
    return cf, pub


def publication_recorded(folder: str | os.PathLike | None) -> bool:
    """A publication of this site folder is recorded (``.ppg-publish.json`` names a project or a bucket)."""
    if folder is None:
        return False
    cf, pub = _cf_records(read_state(folder))
    return bool(pub.get("project") or cf.get("project") or cf.get("bucket"))


def removal_plan(folder: str | os.PathLike, delete_bucket: bool = False) -> Removal:
    """What :func:`unpublish` would remove (see :class:`Removal`), from the state file of the site folder alone: no
    network, no wrangler. Only what the program recorded as published is named; without a record,
    :attr:`Removal.problems` says why nothing can be removed."""
    folder = Path(folder)
    state = read_state(folder)
    cf, pub = _cf_records(state)
    done = state.get("removing") if isinstance(state.get("removing"), dict) else {}
    project = str(pub.get("project") or cf.get("project") or "").strip() or None
    bucket = str(cf.get("bucket") or "").strip() or None
    r = Removal(folder=folder, project=project, bucket=bucket, bucket_asked=bool(delete_bucket))
    if not (folder / STATE).is_file() or not (project or bucket):
        r.problems.append(f"no publication is recorded in {folder} ({STATE}): the program removes only what it "
                          "published itself; delete the site in the Cloudflare dashboard (Workers & Pages, R2)")
        return r
    if (project and not _PROJECT.match(project)) or (bucket and not _BUCKET.match(bucket)):
        r.problems.append(f"the record of the publication in {folder / STATE} names an invalid project or bucket")
        return r
    r.project_done = bool(done.get("project_deleted"))
    if bucket:
        objects = cf.get("objects") if isinstance(cf.get("objects"), dict) else {}
        r.objects = sorted(str(k) for k in objects)
        address = str(cf.get("data_address") or pub.get("data_address") or "").strip() or None
        r.data_address = address
        if done.get("bucket_not_found"):  # an earlier run did not find it: nothing of it is left to remove
            r.bucket_missing, r.objects, r.bucket_rule = True, [], "done"
            return r
        records = cf.get("records") == RECORDS
        if done.get("dev_url_disabled"):
            r.dev_url = "done"
        elif cf.get("dev_url_enabled"):
            r.dev_url = "disable"
        elif address and not _r2_dev(address):
            r.dev_url = "custom"
        elif records:
            r.dev_url = "not_ours"
        elif address:
            r.dev_url = "unknown"
        if done.get("bucket_deleted"):
            r.bucket_rule = "done"
        elif cf.get("bucket_created"):
            r.bucket_rule = "allowed"
        else:
            r.bucket_rule = "not_created" if records else "unknown"
        r.delete_bucket = bool(delete_bucket) and r.bucket_rule == "allowed"
    return r


@dataclass
class Removed:
    """What :func:`unpublish` did. ``not_found``: ``"project"``, ``"bucket"`` when wrangler said that it is not
    in the Cloudflare account it is logged in to; it then counts as removed (``project_deleted`` is True), and a
    note says what to look at."""

    project: str | None
    bucket: str | None
    address: str | None = None  # of the page and of the data, as recorded by the publication: where to look when
    data_address: str | None = None  # a project or a bucket was not found
    project_deleted: bool = False
    objects_deleted: int = 0
    objects_left: list[str] = field(default_factory=list)
    dev_url_disabled: bool = False
    bucket_deleted: bool = False
    not_found: list[str] = field(default_factory=list)
    complete: bool = False  # everything planned went: the state file is gone
    failures: list[str] = field(default_factory=list)  # English, scrubbed
    notes: list[str] = field(default_factory=list)  # what was left as it is or not found, and why (English)
    codes: list[tuple[str, str]] = field(default_factory=list)  # the notes by key ("dev_url_unknown", ...), value
    transcript: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = []
        missing = "not found in this Cloudflare account (it counts as removed)"
        if self.project:
            out.append(f"Pages project {self.project}: " + (missing if "project" in self.not_found else
                                                             "deleted" if self.project_deleted else "NOT deleted"))
        if self.bucket and "bucket" in self.not_found:
            out.append(f"R2 bucket {self.bucket}: " + missing +
                       (f"; {self.objects_deleted} objects deleted before" if self.objects_deleted else ""))
        elif self.bucket:
            out.append(f"R2 bucket {self.bucket}: {self.objects_deleted} objects deleted, {len(self.objects_left)} "
                       "left" + ("; r2.dev address turned off" if self.dev_url_disabled else "") +
                       ("; bucket deleted" if self.bucket_deleted else ""))
        out += [f"note: {n}" for n in self.notes]
        out += [f"failed: {f}" for f in self.failures]
        out.append("removed from Cloudflare (the site folder on this computer is kept)" if self.complete else
                   "not everything was removed: run it again to go on with what is left")
        return out


def unpublish(folder: str | os.PathLike, delete_bucket: bool = False, wrangler: Wrangler | None = None,
              progress=None) -> Removed:
    """Remove from Cloudflare what the program published from the site folder ``folder`` (:func:`removal_plan`),
    with the user's wrangler (since 1.10.2). In order, so that the site stops answering first: ``pages project
    delete <project> --yes``; ``r2 bucket dev-url disable <bucket> -y`` when the program turned the address on;
    ``r2 object delete <bucket>/<key> --remote`` for each object recorded as sent (never another); last ``r2 bucket
    delete <bucket>`` when ``delete_bucket``, the program created it and every recorded object went. A custom data
    domain is never touched; the folder on this computer is never deleted. The state file is written after each
    step (a run cancelled or failed goes on from there next time) and removed once everything went. A step that
    fails is said in :attr:`Removed.failures` and the others are tried. An object, a bucket or a project that
    wrangler says is not there counts as removed; wrangler looks only in the account it is logged in to, so a
    project or a bucket not found is said (:attr:`Removed.not_found`, :attr:`Removed.notes`).

    Raises :class:`NotPublished` (nothing recorded), :class:`WranglerMissing`, :class:`NotLoggedIn` or
    :class:`Cancelled`."""
    progress = as_progress(progress)
    folder = Path(folder)
    r = removal_plan(folder, delete_bucket)
    if not r.ok:
        raise NotPublished("; ".join(r.problems))
    w = wrangler or Wrangler()
    if not w.command:
        raise WranglerMissing(INSTALL_HINT)
    progress(0.01, "Cloudflare: checking wrangler and the login")
    status = w.status(progress)
    if status.state == "missing":
        raise WranglerMissing(status.text)
    if status.state == "logged_out":
        raise NotLoggedIn(status.text)
    w.transcript.append(f"wrangler {status.version}: logged in")
    state = read_state(folder)
    cf, pub = _cf_records(state)
    objects = cf.get("objects") if isinstance(cf.get("objects"), dict) else {}
    done = state.get("removing") if isinstance(state.get("removing"), dict) else {}
    state["removing"] = done
    done.setdefault("started", datetime.now().isoformat(timespec="seconds"))
    out = Removed(project=r.project, bucket=r.bucket, address=str(pub.get("address") or "").strip() or None,
                  data_address=r.data_address)

    def note(code: str, value: str, text: str) -> None:
        out.notes.append(text)
        out.codes.append((code, value))

    def step(args: list[str], what: str) -> str:
        """``wrangler <args>``: ``"done"`` when it worked; when wrangler says the thing is not there, which counts
        as removed, ``"no_bucket"`` (a command on a bucket that is not there) or ``"gone"``; ``""`` after a failure,
        which is said in the result."""
        code, text = w.run(args, progress)
        if not code:
            return "done"
        if args[0] == "r2" and _NO_BUCKET.search(text or ""):
            return "no_bucket"
        if _GONE.search(text or ""):
            return "gone"
        out.failures.append(f"{what}: {_failure_text(text)}")
        return ""

    # the page first: its address stops answering before the slower removal of the data
    if r.project and not r.project_done:
        progress(0.05, f"Cloudflare Pages: deleting the project {r.project}")
        got = step(["pages", "project", "delete", r.project, PAGES_DELETE_YES], f"the Pages project {r.project}")
        if got:
            done["project_deleted"] = True
            if got == "gone":
                done["project_not_found"] = True
            _write_state(folder, state)

    there = bool(r.bucket) and not done.get("bucket_not_found")  # the bucket, as far as wrangler said
    if there and r.dev_url == "disable":
        progress(0.1, f"R2: turning off the public address of {r.bucket}")
        got = step(["r2", "bucket", "dev-url", "disable", r.bucket, DEV_URL_DISABLE_YES],
                   f"the r2.dev address of {r.bucket}")
        if got == "no_bucket":
            there = False
        elif got:
            done["dev_url_disabled"] = True
            _write_state(folder, state)
    elif r.bucket and r.dev_url == "unknown":
        note("dev_url_unknown", r.bucket,
             f"the r2.dev address of the bucket {r.bucket} was left as it is: the record of this publication "
             "(made before 1.10.2) does not say whether the program turned it on; turn it off in the dashboard "
             "(R2 > bucket > Settings) if you wish")
    elif r.bucket and r.dev_url == "custom":
        note("custom_domain", str(r.data_address), f"the data address {r.data_address} (your own domain) was left "
                                                   "as it is")
    for i, key in enumerate(r.objects):
        if not there:
            break
        progress(0.15 + 0.75 * i / len(r.objects), f"R2: deleting {key}")
        got = step(["r2", "object", "delete", f"{r.bucket}/{key}", "--remote"], f"the object {key}")
        if got == "no_bucket":
            there = False
        elif got:
            objects.pop(key, None)
            out.objects_deleted += 1
            _write_state(folder, state)  # a run cancelled or failed goes on from here
    if there and r.delete_bucket and not objects:
        progress(0.92, f"R2: deleting the bucket {r.bucket}")
        got = step(["r2", "bucket", "delete", r.bucket], f"the bucket {r.bucket}")
        if got == "no_bucket":
            there = False
        elif got:
            done["bucket_deleted"] = True
            _write_state(folder, state)
    elif there and r.delete_bucket:
        note("bucket_objects_left", r.bucket, f"the bucket {r.bucket} was kept: {len(objects)} objects the program "
                                              "sent are still there")
    elif there and r.bucket_asked and r.bucket_rule in ("not_created", "unknown"):
        note("bucket_" + r.bucket_rule, r.bucket,
             f"the bucket {r.bucket} was kept: " +
             ("the program did not create it" if r.bucket_rule == "not_created" else
              "the record of this publication (made before 1.10.2) does not say whether the program created it"))
    if r.bucket and not there:  # not there: nothing of it is left to remove
        objects.clear()
        done["bucket_not_found"] = True
        _write_state(folder, state)

    # wrangler looks only in the account it is logged in to: what it did not find there may be in another one
    if done.get("project_not_found"):
        out.not_found.append("project")
        note("project_not_found", str(r.project),
             f"the Pages project {r.project} was not found in the Cloudflare account wrangler is logged in to: it "
             f"counts as removed. If the site still answers at {out.address or 'its pages.dev address'}, it is in "
             "another account: delete it in the dashboard of that account (Workers & Pages)")
    if done.get("bucket_not_found"):
        out.not_found.append("bucket")
        note("bucket_not_found", str(r.bucket),
             f"the bucket {r.bucket} was not found in the Cloudflare account wrangler is logged in to: it counts as "
             "removed, with its objects" +
             (f". If the data still answer at {r.data_address}, the bucket is in another account: empty or delete "
              "it in the dashboard of that account (R2)" if r.data_address else
              ". If it is in another account, empty or delete it in the dashboard of that account (R2)"))
    out.objects_left = sorted(objects)
    out.project_deleted = bool(done.get("project_deleted"))
    out.bucket_deleted = bool(done.get("bucket_deleted"))
    out.dev_url_disabled = bool(done.get("dev_url_disabled"))
    out.complete = not out.failures and not out.objects_left and (out.project_deleted or not r.project)
    if out.complete:
        (folder / STATE).unlink(missing_ok=True)
    else:
        _write_state(folder, state)
    out.transcript = list(w.transcript)
    progress(1.0, "Cloudflare: the site was removed" if out.complete else
             "Cloudflare: not everything was removed (run it again to go on)")
    return out


def forget_published(document) -> bool:
    """The publication kept with the project (``meta["web_site"]["published"]``) forgotten, once the site was
    removed from Cloudflare. True when there was one."""
    from .site import META_KEY

    record = document.meta.get(META_KEY)
    if isinstance(record, dict) and "published" in record:
        del record["published"]
        return True
    return False


__all__ = ["plan", "publish", "Plan", "Result", "Limit", "Wrangler", "Status", "find_wrangler", "scrub",
           "check_consent", "record_published", "published_record", "set_address", "stale_publication", "site_frame",
           "default_project", "site_files", "cors_rule", "stage", "ConsentError", "WranglerMissing", "NotLoggedIn",
           "WranglerFailed", "typed_command", "login_hint", "LOGIN_TIMEOUT", "wrangler_cache_dir", "legacy_folder",
           "legacy_folder_hint", "ISSUES", "LIMITS", "SOURCES", "CHECKED", "DATA_MODES", "STATE", "removal_plan",
           "unpublish", "Removal", "Removed", "NotPublished", "publication_recorded", "forget_published",
           "PAGES_DELETE_YES", "DEV_URL_DISABLE_YES"]
