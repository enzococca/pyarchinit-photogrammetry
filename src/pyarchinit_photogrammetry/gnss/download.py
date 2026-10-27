"""What the GNSS work downloads and where it keeps it: the folder named by ``PPG_GNSS_DIR`` or, when that is not
set, ``gnss`` in the data folder of the program, and the two calls that reach the public archives (EPN, IGS, RING,
BKG): :func:`fetch`, which brings one file, and :func:`listing`, which reads the page that lists a folder. Both
introduce the program (``User-Agent``), try again when the server or the line fails, name the address when they give
up, and also take ``file://`` addresses, which is what the tests use."""
from __future__ import annotations

import hashlib
import http.client
import logging
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser
from pathlib import Path

from .. import __version__
from ..core.progress import as_progress

log = logging.getLogger(__name__)

USER_AGENT = f"pyarchinit-photogrammetry/{__version__}"
ATTEMPTS = 3  # a download is tried this many times ...
FIRST_WAIT = 2.0  # ... waiting 2 s after the first failure, 4 s after the second
BLOCK = 1 << 20
LISTING_SECONDS = 600.0  # a listing is kept in memory for ten minutes
LISTING_LIMIT = 64 << 20  # the largest page that is read as a listing
# a listing is a small page: a site that has not answered in this many seconds (to connect, or between two pieces of
# the page) is taken for one that does not answer - three attempts and their waits are half a minute, where a port
# closed by a firewall (RING's 44324) held a run for three minutes at the 60 s of a download
LISTING_TIMEOUT = 8.0


def gnss_dir() -> Path:
    """The folder of the GNSS data of the program: the one ``PPG_GNSS_DIR`` names (the tests name a temporary one) or
    ``gnss`` in the data folder of the program (``<data>/gnss``). It is not made."""
    named = os.environ.get("PPG_GNSS_DIR", "").strip()
    if named:
        return Path(named)
    from ..ai.sam_env import data_dir

    return data_dir() / "gnss"


def cache_dir(kind: str) -> Path:
    """The cache of one kind of file - ``"rinex"``: the decompressed copies of RINEX files - which is ``cache/<kind>``
    in :func:`gnss_dir`, made when it is not there; a ``ValueError`` when ``kind`` is not the name of a folder."""
    if not kind or kind in (".", "..") or "/" in kind or "\\" in kind:
        raise ValueError(f"{kind!r} is not the name of a cache folder")
    folder = gnss_dir() / "cache" / kind
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class DownloadError(Exception):
    """A file or a page that could not be fetched. ``url`` is the address that was asked for."""

    def __init__(self, message: str, url: str = ""):
        super().__init__(message)
        self.url = url


class NotPublished(DownloadError):
    """The server has no such file (HTTP 404 or 410), or the ``file://`` path does not exist: asking again does not
    help. For a file of today it may only be too early: the archives publish after the day."""


class _Transient(DownloadError):
    """A failure that may not come back (the line, a timeout, an error of the server): the call tries again."""


def _sleep(seconds: float) -> None:
    """The wait between two attempts (the tests replace it)."""
    time.sleep(seconds)


def _clock() -> float:
    """Seconds, on a clock that does not go back (the tests replace it)."""
    return time.monotonic()


class _LocalFile:
    """A file on the computer as ``urlopen`` gives a remote one: ``read``, ``headers`` with its size, and ``close``."""

    def __init__(self, path: Path):
        self._file = open(path, "rb")
        self.headers = {"Content-Length": str(os.fstat(self._file.fileno()).st_size)}

    def read(self, size: int = -1) -> bytes:
        return self._file.read(size)

    def geturl(self) -> str | None:
        return None

    def close(self) -> None:
        self._file.close()


def _local_path(url: str) -> Path:
    return Path(urllib.request.url2pathname(urllib.parse.urlsplit(url).path))


def _opened(url: str, timeout: float):
    """The answer of the server to a request for ``url`` (an object with ``read``, ``headers``, ``geturl`` and ``close``),
    or the error that says why there is none: :class:`NotPublished` when the file is not there, a plain
    :class:`DownloadError` when asking again would not help, and the private ``_Transient`` when it might."""
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    try:
        if scheme == "file":
            return _LocalFile(_local_path(url))
        if scheme not in ("http", "https"):
            raise DownloadError(f"{url}: only http, https and file addresses can be downloaded", url)
        kwargs = {}
        if scheme == "https":
            from ..updates import _ssl_context

            kwargs["context"] = _ssl_context()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        return urllib.request.urlopen(request, timeout=timeout, **kwargs)
    except DownloadError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            raise NotPublished(f"{url}: not published (HTTP {exc.code})", url) from exc
        if exc.code >= 500:
            raise _Transient(f"{url}: the server answered HTTP {exc.code}", url) from exc
        raise DownloadError(f"{url}: the server answered HTTP {exc.code}", url) from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise DownloadError(f"{url}: the certificate of the server was not accepted ({reason})", url) from exc
        if isinstance(reason, (FileNotFoundError, NotADirectoryError)):
            raise NotPublished(f"{url}: not found", url) from exc
        raise _Transient(f"{url}: could not connect ({reason})", url) from exc
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise NotPublished(f"{url}: not found", url) from exc
    except (IsADirectoryError, PermissionError) as exc:
        raise DownloadError(f"{url}: could not be read ({exc})", url) from exc
    except ValueError as exc:  # an address that cannot be one
        raise DownloadError(f"{url}: not a valid address ({exc})", url) from exc
    except (OSError, http.client.HTTPException) as exc:  # a timeout, a reset, a broken answer
        raise _Transient(f"{url}: could not connect ({exc})", url) from exc


def site(url: str) -> str:
    """The site an address is on, as a message names it: its host and port (``webring.gm.ingv.it:44324``), or its
    scheme for an address without one (``file``)."""
    parts = urllib.parse.urlsplit(url)
    return parts.netloc or parts.scheme or url


def _retrying(url: str, attempts: int, action, progress=None):
    """``action()``, again after a wait (2 s and then 4 s) while it fails with something that may pass; the last
    failure is raised as a :class:`DownloadError` that names the address. With ``progress`` every attempt on the
    network says which site it contacts, and a cancel stops before the next attempt."""
    attempts = max(1, int(attempts))
    remote = urllib.parse.urlsplit(url).scheme.lower() in ("http", "https")
    for attempt in range(1, attempts + 1):
        if progress is not None:
            progress.check()
            if remote:
                again = f" (attempt {attempt} of {attempts})" if attempt > 1 else ""
                progress.message(f"Contacting {site(url)}{again}")
        try:
            return action()
        except _Transient as exc:
            if attempt >= attempts:
                after = f" (after {attempts} attempts)" if attempts > 1 else ""
                raise DownloadError(f"{exc}{after}", url) from exc
            wait = FIRST_WAIT * 2 ** (attempt - 1)
            log.warning("%s; trying again in %g s (attempt %d of %d)", exc, wait, attempt + 1, attempts)
            _sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover


def _announced(response) -> int | None:
    """The size the server says the answer has, when it does."""
    try:
        size = int(response.headers.get("Content-Length"))
    except (AttributeError, TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _download_once(url: str, work: Path, timeout: float, progress) -> tuple[int, str]:
    """One attempt: the file of ``url`` written to ``work``; returns its size and its SHA-256. A connection that
    closes before the size the server announced is a failure that may pass (``_Transient``)."""
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]) or url
    response = _opened(url, timeout)
    try:
        total = _announced(response)
        digest = hashlib.sha256()
        done = 0
        with open(work, "wb") as out:
            while True:
                progress.check()
                try:
                    block = response.read(BLOCK)
                except (OSError, http.client.HTTPException) as exc:
                    raise _Transient(f"{url}: the connection failed after {done} bytes ({exc})", url) from exc
                if not block:
                    break
                out.write(block)
                digest.update(block)
                done += len(block)
                if total:
                    progress(min(done / total, 1.0), f"Downloading {name}: {done / 1e6:.1f} of {total / 1e6:.1f} MB")
                else:
                    progress.message(f"Downloading {name}: {done / 1e6:.1f} MB")
    finally:
        response.close()
    if total is not None and done != total:
        raise _Transient(f"{url}: the connection closed after only {done} of {total} bytes", url)
    return done, digest.hexdigest()


def fetch(url: str, target: Path, progress=None, *, sha256: str | None = None, timeout: float = 60.0,
          retries: int = 3) -> Path:
    """The file at ``url`` (http, https or file) saved as ``target``, whose folder is made; returns ``target``.

    It is written beside the target and moved into place when it is whole (and, with ``sha256``, when it is the file
    that was asked for), so a failure leaves nothing behind and a file that was there stays. ``retries`` is the number
    of attempts: a timeout, a failed connection, an error 5xx of the server and a download that closes before the
    size announced are tried again (after 2 s, then 4 s); a 404 raises :class:`NotPublished` at once, and everything
    else - another HTTP error, a certificate that is not accepted, a wrong SHA-256 - a :class:`DownloadError`. Both
    carry the address in ``.url`` and in their message. ``progress`` is the usual callback; the download can be
    cancelled through it."""
    progress = as_progress(progress)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = sha256.strip().lower() if sha256 else None
    work = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.part")
    try:
        _done, digest = _retrying(url, retries, lambda: _download_once(url, work, timeout, progress), progress)
        if expected and digest != expected:
            raise DownloadError(f"{url}: the file is not the one that was published (SHA-256 {digest[:12]}..., "
                                f"expected {expected[:12]}...)", url)
        os.replace(work, target)
    finally:
        work.unlink(missing_ok=True)
    progress(1.0, f"{target.name} downloaded")
    return target


class _Links(HTMLParser):
    """The ``href`` of every ``<a>`` of a page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.hrefs.extend(value for key, value in attrs if key == "href" and value)


def _is_entry(name: str) -> bool:
    """Whether a link name, decoded, is one entry of a folder and nothing that leads out of it: not ``.`` or ``..``, no
    ``/`` but the one that ends the name of a folder, and no backslash or NUL (a separator, or the end of a name, to some
    systems). Names end up as the names of files: ``..%2F..%2Fetc%2Fpasswd`` must not become a path."""
    bare = name[:-1] if name.endswith("/") else name
    return bool(bare) and bare not in (".", "..") and not any(c in bare for c in "/\\\0")


def _children(page: str, hrefs: list[str]) -> list[str]:
    """The names of the entries of the folder that ``page`` (the address of the page) lists, among its links: the ones
    that lead to something directly inside that folder. Apache writes relative links, IIS paths from the root without
    a slash after a folder; both are met, and so are skipped the sort links (``?C=N;O=D``), the parent folder, the folders
    above it, the assets and the links to other sites. A name is decoded once, then judged by :func:`_is_entry`."""
    base = urllib.parse.urlsplit(page)
    folder = base.path if base.path.endswith("/") else base.path.rsplit("/", 1)[0] + "/"
    names: list[str] = []
    for href in hrefs:
        if href.startswith(("#", "?")):
            continue
        link = urllib.parse.urlsplit(urllib.parse.urljoin(page, href))
        if link.query or (link.scheme.lower(), link.netloc.lower()) != (base.scheme.lower(), base.netloc.lower()):
            continue
        if not link.path.startswith(folder):
            continue
        rest = link.path[len(folder):]
        if not rest.strip("/") or "/" in rest.rstrip("/"):
            continue
        name = urllib.parse.unquote(rest)
        if _is_entry(name) and name not in names:
            names.append(name)
    return names


def _page(url: str, timeout: float = LISTING_TIMEOUT) -> tuple[str, bytes]:
    """One attempt: the page at ``url`` and the address that answered (after the redirections)."""
    response = _opened(url, timeout)
    try:
        total = _announced(response)
        data = bytearray()
        while len(data) <= LISTING_LIMIT:
            try:
                block = response.read(BLOCK)
            except (OSError, http.client.HTTPException) as exc:
                raise _Transient(f"{url}: the connection failed after {len(data)} bytes ({exc})", url) from exc
            if not block:
                break
            data.extend(block)
        final = response.geturl() if hasattr(response, "geturl") else None
    finally:
        response.close()
    if len(data) > LISTING_LIMIT:
        raise DownloadError(f"{url}: the page is larger than {LISTING_LIMIT >> 20} MB, it is not an index", url)
    if total is not None and len(data) != total:
        raise _Transient(f"{url}: the connection closed after only {len(data)} of {total} bytes", url)
    return final or url, bytes(data)


_LISTINGS: dict[str, tuple[float, list[str]]] = {}


def listing(url: str, progress=None) -> list[str]:
    """The names in the folder that the page at ``url`` lists, as its links give them (a folder keeps the slash its
    link has: ``C2400/``), kept in memory for ten minutes. ``url`` can also be a folder on the computer
    (``file:///.../``), whose entries are listed, or a page there. Every listing - of BKG, of the EPN, of RING alike -
    gives its site :data:`LISTING_TIMEOUT` seconds to answer (to connect, and between two pieces of the page), three
    times, where a download waits 60 s: a page is small, and a site that stays silent that long is down or closed by a
    firewall. ``progress`` (the usual callback) hears which site is contacted, and a cancel stops between two attempts.
    Raises :class:`NotPublished` when there is no page and :class:`DownloadError` when it cannot be read."""
    now = _clock()
    kept = _LISTINGS.get(url)
    if kept is not None and now - kept[0] < LISTING_SECONDS:
        return list(kept[1])
    names = _read_listing(url, None if progress is None else as_progress(progress))
    for stale in [key for key, (when, _names) in _LISTINGS.items() if now - when >= LISTING_SECONDS]:
        del _LISTINGS[stale]
    _LISTINGS[url] = (now, names)
    return list(names)


def _read_listing(url: str, progress=None) -> list[str]:
    if urllib.parse.urlsplit(url).scheme.lower() == "file":
        path = _local_path(url)
        if path.is_dir():
            return sorted(entry.name + ("/" if entry.is_dir() else "") for entry in path.iterdir())
    final, data = _retrying(url, ATTEMPTS, lambda: _page(url), progress)
    parser = _Links()
    parser.feed(data.decode("utf-8", errors="replace"))
    parser.close()
    return _children(final, parser.hrefs)
