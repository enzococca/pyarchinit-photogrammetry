"""A site folder (:mod:`.site`) published on Cloudflare through the user's own ``wrangler`` (since 1.10.0).

Two steps:

* :func:`plan` (always available, no network, no wrangler): what goes where - the page on Cloudflare Pages, the data
  files on Pages too or in an R2 bucket -, how many files and bytes, which limits of the free plans they meet or
  exceed (:data:`LIMITS`, read on Cloudflare's documentation on :data:`CHECKED`), what is made public and what it
  costs in free-tier terms. Nothing is sent.
* :func:`publish`: the plan carried out by running ``wrangler`` (on the PATH, in the usual folders of Node, or
  ``npx --no wrangler``: never installed by the program). The login is the user's (``wrangler login`` in a
  terminal): the program never asks for, reads, stores or passes a token, a key or an account id; it reads only the
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
LOGIN_HINT = ("wrangler is not logged in to Cloudflare. Open a terminal and run: wrangler login (it opens the "
              "browser to sign in; the program never sees the credentials), then try again.")


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
        out = [f"every file of the page and the manifest ({len(self.pages_files)} files) at "
               f"https://{self.project}.pages.dev (or the address Cloudflare gives the project)"]
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
            out.append(f"data address: {self.data_url or 'r2.dev public address of the bucket (rate-limited, meant '
                       'for development: a custom domain is better for a lasting site)'}")
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


def plan(folder: str | os.PathLike, project: str, bucket: str | None = None, data: str = "auto",
         data_url: str | None = None) -> Plan:
    """What publishing ``folder`` on the Pages project ``project`` would do (see the module docstring); no network,
    no wrangler. ``bucket``: the R2 bucket for the data (needed when they go to R2); ``data``: one of
    :data:`DATA_MODES`; ``data_url``: the https address under which the bucket is served (a custom domain the user
    connected to it), else the r2.dev address is turned on at publishing."""
    folder = Path(folder)
    project = str(project or "").strip()
    bucket = str(bucket or "").strip() or None
    data_url = str(data_url or "").strip().rstrip("/") or None
    manifest = _manifest(folder)
    p = Plan(folder=folder, project=project, bucket=bucket, data="pages", data_url=data_url,
             frame=site_frame(folder), title=str(manifest.get("title") or ""))
    if data not in DATA_MODES:
        p.problems.append(f"data must be one of {', '.join(DATA_MODES)}")
        data = "auto"
    if not manifest:
        p.problems.append(f"{folder} is not a site built by this program (no site.json): build it first")
        return p
    if not _PROJECT.match(project):
        p.problems.append("the name of the Pages project: lowercase letters, digits and dashes, not at the ends "
                          "(it becomes <name>.pages.dev)")
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
        if data == "pages":
            p.problems.append(f"{len(over)} files are larger than the {PAGES_FILE_BYTES // 2**20} MiB a file Pages "
                              f"takes ({names}): send the data to R2 (a bucket) or build a smaller site")
        else:
            p.problems.append(f"{len(over)} files outside data/ are larger than the {PAGES_FILE_BYTES // 2**20} MiB "
                              f"a file Pages takes ({names})")
    if len(p.pages_files) > PAGES_FREE_FILES:
        p.problems.append(f"{len(p.pages_files):,} files on Pages, more than the {PAGES_FREE_FILES:,} of the Free "
                          f"plan" + (": send the data to R2 (a bucket)" if data == "pages" else ""))
    if data == "r2":
        if bucket is None:
            p.problems.append("the data must go to R2 (files above the limits of Pages): name a bucket")
        elif not _BUCKET.match(bucket):
            p.problems.append("the name of the bucket: 3-63 lowercase letters, digits and dashes, not at the ends")
        if data_url and not re.match(r"^https://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s]*)?$", data_url):
            p.problems.append("the address of the data must start with https:// (the custom domain of the bucket)")
        biggest = max((e.size for e in p.r2_files), default=0)
        p.limits.append(Limit("r2_put_bytes", biggest, R2_PUT_BYTES, biggest <= R2_PUT_BYTES))
        if biggest > R2_PUT_BYTES:
            names = ", ".join(e.path for e in p.r2_files if e.size > R2_PUT_BYTES)
            p.problems.append(f"files larger than the {R2_PUT_BYTES // 10**6} MB wrangler sends to R2 in one go "
                              f"({names}): build a smaller site (budget) or send them with rclone")
        state = read_state(folder).get("cloudflare") or {}
        sent = state.get("objects") if state.get("bucket") == bucket and isinstance(state.get("objects"), dict) \
            else {}
        for e in p.r2_files:
            e.sha256 = _sha256(folder / e.path)
            (p.unchanged if sent.get(e.path) == e.sha256 else p.upload).append(e.path)
        keys = {e.path for e in p.r2_files}
        p.remove = sorted(k for k in sent if k not in keys)
        p.limits.append(Limit("r2_storage", p.r2_bytes, R2_FREE_STORAGE, p.r2_bytes <= R2_FREE_STORAGE))
        writes = len(p.upload) + 3
        p.limits.append(Limit("r2_class_a", writes, R2_FREE_CLASS_A, writes <= R2_FREE_CLASS_A))
        p.limits.append(Limit("r2_class_b", len(p.r2_files), R2_FREE_CLASS_B, len(p.r2_files) <= R2_FREE_CLASS_B))
        if p.r2_bytes > R2_FREE_STORAGE:
            p.warnings.append("the data are above the 10 GB-month R2 keeps for free: the rest is billed")
        if not data_url:
            p.warnings.append("the data are served from the r2.dev address of the bucket: rate-limited and meant "
                              "for development; connect a custom domain to the bucket for a lasting site and "
                              "publish with its address")
        p.warnings.append("R2 needs an R2 subscription on the account (checkout in the Cloudflare dashboard, once)")
        p.warnings.append(f"the CORS rule of the bucket {bucket} is replaced with the one of this site: use a bucket "
                          "for this site alone")
    if p.frame == "crs":
        p.warnings.append("the site holds the TRUE coordinates of the survey: publishing tells anyone where it is")
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
        return [str(Path(npx).absolute()), "--no", "wrangler"]
    return None


@dataclass
class Status:
    state: str  # "missing", "logged_out", "ready"
    version: str = ""
    command: list[str] = field(default_factory=list)
    text: str = ""


class Wrangler:
    """The user's wrangler, run as a child process: stdin closed (no prompt waits), the output read and scrubbed,
    cancelled through ``progress``. The environment is passed as it is (wrangler reads its own login); the program
    adds only ``WRANGLER_SEND_METRICS=false`` and no colours."""

    def __init__(self, command: list[str] | None = None, env: dict | None = None, timeout: float = 1800.0):
        self.command = list(command) if command else find_wrangler(env)
        self.timeout = timeout
        base = dict(os.environ if env is None else env)
        path = _search_path(base)
        if self.command:
            first = str(Path(self.command[0]).parent)
            path = os.pathsep.join([first] + [p for p in path.split(os.pathsep) if p != first])
        base.update({"PATH": path, "WRANGLER_SEND_METRICS": "false", "NO_COLOR": "1", "FORCE_COLOR": "0"})
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
            raise WranglerFailed(f"{line} failed (exit code {proc.returncode}): {_last_lines(scrub(out))}",
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
            if re.search(r"(?i)not (authenticated|logged in)", out):
                code = 1
        if code:
            return Status("logged_out", version, self.command, LOGIN_HINT)
        return Status("ready", version, self.command, f"wrangler {version}, logged in")


def _version(text: str) -> str:
    m = re.search(r"(\d+\.\d+\.\d+)", text or "")
    return m.group(1) if m else ""


def _last_lines(text: str, n: int = 6) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
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
    address: str
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

    # the Pages project
    progress(0.03, f"Cloudflare Pages: project {p.project}")
    _code, out = w.run(["pages", "project", "list", "--json"], progress, check=True)
    entry = _project_entry(_json_in(out), p.project)
    branch = BRANCH
    if entry is None:
        _code, out = w.run(["pages", "project", "create", p.project, "--production-branch", BRANCH], progress,
                           check=True)
        known = _pages_host(out, p.project)
    else:
        known = _pages_host(json.dumps(entry), p.project)
        branch = _production_branch(entry)
    host = known or f"{p.project}.pages.dev"  # Cloudflare adds a suffix when the name is taken: read when said

    data_address = None
    sent = removed = 0
    if p.data == "r2":
        bucket = p.bucket
        code, _out = w.run(["r2", "bucket", "info", bucket], progress)
        if code:
            w.run(["r2", "bucket", "create", bucket], progress, check=True)
        cf = state.get("cloudflare") if isinstance(state.get("cloudflare"), dict) else {}
        if cf.get("bucket") != bucket:
            cf = {"bucket": bucket, "objects": {}}
        objects = cf.setdefault("objects", {})
        cf["project"] = p.project
        state["cloudflare"] = cf
        sizes = {e.path: e for e in p.r2_files}
        total = max(1, p.upload_bytes)
        done = 0
        for key in p.upload:
            e = sizes[key]
            progress.sub(0.05 + 0.8 * done / total, 0.8 * e.size / total)(0.0, f"R2: {key}")
            w.run(["r2", "object", "put", f"{bucket}/{key}", "--file", str((folder / key).resolve()),
                   "--content-type", content_type(key), "--cache-control", CACHE_DATA, "--remote"], progress,
                  check=True)
            objects[key] = e.sha256
            _write_state(folder, state)  # a publication cancelled or failed starts again from here
            done += e.size
            sent += 1
        for key in p.remove:
            progress(0.86, f"R2: deleting {key}")
            code, _out = w.run(["r2", "object", "delete", f"{bucket}/{key}", "--remote"], progress)
            objects.pop(key, None)
            _write_state(folder, state)
            removed += 1
        progress(0.87, f"R2: the public address of {bucket}")
        if p.data_url:
            data_address = p.data_url
        else:
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
        host = _pages_host(out, p.project) or host
    address = f"https://{host}"
    result = Result(address=address, project=p.project, data=p.data, data_address=data_address,
                    bucket=p.bucket if p.data == "r2" else None, sent=sent, unchanged=len(p.unchanged),
                    removed=removed, when=datetime.now().isoformat(timespec="seconds"), frame=p.frame,
                    transcript=list(w.transcript))
    state["published"] = result.record()
    _write_state(folder, state)
    progress(1.0, f"published: {address}")
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
    return pub if isinstance(pub, dict) and pub.get("address") else None


def stale_publication(document) -> bool:
    """The site was built again after it was published (the published copy is older)."""
    from .site import META_KEY

    record = document.meta.get(META_KEY) or {}
    pub = published_record(document)
    return bool(pub and record.get("when") and str(record["when"]) > str(pub.get("when") or ""))


__all__ = ["plan", "publish", "Plan", "Result", "Limit", "Wrangler", "Status", "find_wrangler", "scrub",
           "check_consent", "record_published", "published_record", "stale_publication", "site_frame",
           "default_project", "site_files", "cors_rule", "stage", "ConsentError", "WranglerMissing", "NotLoggedIn",
           "WranglerFailed", "LIMITS", "SOURCES", "CHECKED", "DATA_MODES", "STATE"]
