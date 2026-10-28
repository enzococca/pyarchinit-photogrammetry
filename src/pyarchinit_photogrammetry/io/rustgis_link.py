"""Link with RustGIS (https://github.com/opengeos/RustGIS), the desktop GIS: finding it, reading where it listens and
the token it made, and talking to it.

While the user ticks *Let your Agents Control RustGIS* in its AI Assistant settings, RustGIS serves the tools of its
MCP server (``list_layers``, ``run_command``, ``run``, ``read_features``...) to the programs of this computer over HTTP:
``POST http://127.0.0.1:<port>/mcp`` (8768 unless it was started with another), one JSON-RPC message per request, a
JSON reply, and ``Authorization: Bearer <token>``, the token being the one RustGIS keeps in ``mcp-http.token`` of its
settings folder. A request that carries an ``Origin`` header or names another host is refused before the token is read.
That box is RustGIS's own consent: the program never writes RustGIS's settings, and the token is read from its file at
each request, never kept, logged or shown (an exception does not carry it either).

The token stays in the settings folder for good, so its file says nothing about who listens at a port: loopback is
shared by every program of every account of the computer. The address is therefore taken only from what a RUNNING
RustGIS publishes, its ``endpoint.json`` (``pid``, ``http_url``, ``http_token_file``) in :func:`token_dir`, and only
while its ``pid`` runs. There is no default address to try when that is missing.

* :func:`find_rustgis` finds the app and ``rustgis-cli``; :func:`open_app` starts the app.
* :func:`endpoint` says where a running RustGIS listens (None: it is not running, or its agent control is off).
* :class:`HttpTransport` is the channel to the app, :class:`StdioTransport` the same tools over the pipes of
  ``rustgis-cli mcp``, the engine without the window that the tests run against.
* :class:`RustGisLink` keeps RustGIS's layers and the live GeoPackage of the QGIS link in step: RustGIS never writes
  its edits back to a file, so :meth:`RustGisLink.pull` asks it to export the layers and writes what changed there
  into that GeoPackage as another program would, where :meth:`.qgis_link.LiveLink.read_changes` finds it.
  :meth:`RustGisLink.start` gives RustGIS the chunk (its system checked first, every table of the live GeoPackage a
  layer, in a group named after the chunk), and :meth:`RustGisLink.push` sends it the program's edits row by row, as
  one script RustGIS may run twice without harm.

What goes wrong has a name a caller can wait for: :class:`RustGisOff` (nobody listens: agent control is off, or RustGIS
is closed), :class:`RustGisBusy` (try later: RustGIS does not draw, minimized or hidden, a dialog is open, too many
requests), :class:`RustGisRefused` (RustGIS said no), :class:`RustGisTooOld`. Over HTTP an app that does not draw says
nothing for a minute and then cancels the request (503); a transport gives up at its ``timeout`` first, and the request
is still in RustGIS's queue, so it may run when the window is back: ``RustGisBusy.outcome_unknown`` says so, and only
what is harmless to repeat (a read) should be given a short timeout.
"""
from __future__ import annotations

import hashlib
import http.client
import itertools
import json
import logging
import math
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from ..updates import bundle_version, version_key
from . import gpkg
from .qgis_link import (ID_FIELD, LIVE_TABLES, PART_FIELD, TECHNICAL_FIELDS, TWO_WAY_TABLES, LiveLink, _affinity,
                        _bind, _bounds, _sql_type, new_id)

log = logging.getLogger(__name__)

MIN_VERSION = (0, 4, 0)  # the command ids and parameters the link uses are those of RustGIS 0.4.0
MIN_TEXT = ".".join(str(n) for n in MIN_VERSION)
HTTP_PORT = 8768  # where RustGIS's MCP over HTTP listens unless started with another: endpoint.json tells, never tried
CONFIG_ENV = "RUSTGIS_CONFIG_DIR"  # RustGIS's own variable for its settings folder
TOKEN_FILE = "mcp-http.token"  # in the settings folder: made by RustGIS once agent control is ticked, kept for good
ENDPOINT_FILE = "endpoint.json"  # what a running RustGIS publishes in token_dir(): its pid and listeners' addresses
CLI_ENV = "PPG_RUSTGIS_CLI"  # the rustgis-cli to use (the tests of the real engine), else the one on the PATH
RELEASES_URL = "https://github.com/opengeos/RustGIS/releases"
RASTER_SAMPLES = 120_000_000  # width x height x bands RustGIS loads whole; a larger raster is read in place, by range
TOLERANCE_M = 0.001  # a geometry that comes back from RustGIS within 1 mm of the program's is unchanged
BUSY_CODES = (-32004, -32001)  # JSON-RPC errors of RustGIS: it does not draw (minimized, hidden) or is busy: try later
PROTOCOL = "2025-03-26"  # of MCP, as the messages of the reference clients name it
APPLICATIONS = Path("/Applications")  # macOS: where RustGIS.app goes for all users (~/Applications is the other place)
SCRATCH_DIR = "rustgis"  # beside the live GeoPackage: the layers RustGIS exports for a pull, one file a table
EXPORT_SUFFIXES = ("", "-wal", "-shm", "-journal")  # RustGIS refuses to export where the file or a journal of it is


class RustGisError(RuntimeError):
    """A call to RustGIS that did not give its answer; the subclass says why."""


class RustGisOff(RustGisError):
    """RustGIS cannot be reached: it publishes no address (not running, or the box unticked), no token can be read,
    nothing listens at the address, or the token is not the running RustGIS's."""


class RustGisBusy(RustGisError):
    """RustGIS does not answer now (it does not draw - minimized, hidden, a dialog open - or has too many requests):
    try again later, this is not a failure. ``outcome_unknown``: the request may still run, or did (the transport gave
    up waiting, or RustGIS said it was running it): look before sending it again."""

    def __init__(self, message: str, outcome_unknown: bool = False):
        super().__init__(message)
        self.outcome_unknown = outcome_unknown


class RustGisRefused(RustGisError):
    """RustGIS answered and said no: the tool or the command refused (the message is RustGIS's), or what it answered
    is not what a tool gives."""


class RustGisTooOld(RustGisError):
    """RustGIS is older than :data:`MIN_VERSION`: the command ids and parameters of the link are not all there."""


@dataclass
class RustGisInstall:
    app: Path | None  # what starts RustGIS: RustGIS.app (macOS), RustGIS.exe (Windows), rustgis (Linux)
    cli: Path | None  # rustgis-cli: the same tools without the window (a separate download on macOS)
    version: str | None  # of the app, read from its bundle (macOS); the running RustGIS tells its own (initialize)


@dataclass
class Endpoint:
    url: str  # http://127.0.0.1:<port>/mcp, as the running RustGIS published it
    token_file: Path  # where it keeps its token (published too): read from it at each request


def _platform() -> str:
    """``darwin``, ``win32`` or ``linux``: RustGIS gives every other system the folders of Linux."""
    return sys.platform if sys.platform in ("darwin", "win32") else "linux"


def _env(name: str) -> str | None:
    """An environment variable, unless it is unset or empty (RustGIS reads them so)."""
    return os.environ.get(name) or None


def too_old(version: str | None) -> bool:
    """Whether ``version`` is a RustGIS older than :data:`MIN_VERSION` (a text that is not a version is not judged)."""
    try:
        return version_key(version or "")[:3] < MIN_VERSION
    except ValueError:
        return False


def _find_app() -> tuple[Path | None, str | None]:
    """The app and, where the system keeps it, its version: macOS ``/Applications/RustGIS.app``, then the one in
    ``~/Applications`` (the version is the bundle's); Windows the folders of its installers; Linux ``rustgis`` on the
    PATH."""
    system = _platform()
    if system == "darwin":
        for folder in (APPLICATIONS, Path(os.path.expanduser("~/Applications"))):
            if (folder / "RustGIS.app").is_dir():
                return folder / "RustGIS.app", bundle_version(folder / "RustGIS.app")
        return None, None
    if system == "win32":
        folders = [Path(root) / sub for root, sub in ((_env("LOCALAPPDATA"), "Programs/RustGIS"),
                                                      (_env("ProgramFiles"), "RustGIS")) if root]
        return next((f / "RustGIS.exe" for f in folders if (f / "RustGIS.exe").is_file()), None), None
    found = shutil.which("rustgis")
    return (Path(found).absolute() if found else None), None


def _find_cli() -> Path | None:
    """``rustgis-cli``: the file ``PPG_RUSTGIS_CLI`` names, else the one on the PATH."""
    named = _env(CLI_ENV)
    if named and Path(named).expanduser().is_file():
        return Path(named).expanduser().absolute()
    found = shutil.which("rustgis-cli")
    return Path(found).absolute() if found else None


def find_rustgis() -> RustGisInstall | None:
    """RustGIS on this computer: the app in the usual places and ``rustgis-cli`` (``PPG_RUSTGIS_CLI``, else the PATH).
    None when neither is there."""
    (app, version), cli = _find_app(), _find_cli()
    return RustGisInstall(app, cli, version) if app or cli else None


def open_app(paths: Iterable[str | Path] = ()) -> None:
    """Starts RustGIS, on the files named when there are any (macOS ``open -a``, elsewhere its program), as
    :func:`.qgis_link.open_in_qgis` starts QGIS: an absolute program and no ``cwd``, so that it is spawned, never
    forked."""
    found = find_rustgis()
    if found is None or found.app is None:
        raise RustGisError(f"RustGIS is not installed: download it from {RELEASES_URL}")
    files = [str(p) for p in paths]
    if _platform() == "darwin":
        subprocess.Popen(["/usr/bin/open", "-a", str(found.app), *files], close_fds=False)  # posix_spawn
    else:
        subprocess.Popen([str(found.app), *files], close_fds=False)


def settings_dir() -> Path | None:
    """RustGIS's settings folder, where it keeps its token (its own rules): ``RUSTGIS_CONFIG_DIR`` as it is, else
    ``~/Library/Application Support/RustGIS`` (macOS), ``%APPDATA%\\RustGIS`` (Windows), ``$XDG_CONFIG_HOME/rustgis`` or
    ``~/.config/rustgis`` (Linux). None when the system does not say where."""
    named = _env(CONFIG_ENV)
    if named:
        return Path(named)
    system, home = _platform(), _env("HOME")
    if system == "win32":
        appdata = _env("APPDATA")
        return Path(appdata) / "RustGIS" if appdata else None
    if system == "darwin":
        return Path(home) / "Library" / "Application Support" / "RustGIS" if home else None
    base = _env("XDG_CONFIG_HOME") or (str(Path(home) / ".config") if home else None)
    return Path(base) / "rustgis" if base else None


def token_dir() -> Path:
    """The folder where a running RustGIS keeps its run files, ``endpoint.json`` among them (its own rule): ``rustgis``
    in ``$XDG_RUNTIME_DIR`` when that is an absolute folder that exists, else ``rustgis-mcp-<user>`` in the temporary
    folder, <user> being ``$USER`` (``$USERNAME`` when there is no ``USER`` at all) kept to its letters, digits, ``-``
    and ``_``, and ``user`` when nothing is left. The folder itself may not exist (RustGIS not run yet)."""
    runtime = _env("XDG_RUNTIME_DIR")
    if runtime and Path(runtime).is_absolute() and Path(runtime).is_dir():
        return Path(runtime) / "rustgis"
    user = os.environ["USER"] if "USER" in os.environ else os.environ.get("USERNAME", "")
    name = re.sub(r"[^A-Za-z0-9_-]", "", user) or "user"
    return Path(tempfile.gettempdir()) / f"rustgis-mcp-{name}"


# kernel32: OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE), the wait that times out, access denied
_WINDOWS_ACCESS, _WINDOWS_WAIT_TIMEOUT, _WINDOWS_ACCESS_DENIED = 0x1000 | 0x100000, 0x102, 5


def _kernel32():
    """kernel32 through ctypes, with the signatures of the calls :func:`_running_on_windows` makes (Windows only)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _running_on_windows(pid: int) -> bool:
    """Whether process ``pid`` is running, asked through OpenProcess: ``os.kill`` there terminates the process."""
    import ctypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_WINDOWS_ACCESS, False, pid)
    if not handle:  # access denied: it is there, it is not ours to open; anything else: there is no such process
        return ctypes.get_last_error() == _WINDOWS_ACCESS_DENIED
    try:
        return kernel32.WaitForSingleObject(handle, 0) == _WINDOWS_WAIT_TIMEOUT  # asks, never waits: unsignalled = runs
    finally:
        kernel32.CloseHandle(handle)


def _process_runs(pid: int) -> bool:
    """Whether process ``pid`` (more than 0: 0 and less are groups of processes) is running, whoever owns it: signal 0
    only asks (POSIX); Windows opens the process (:func:`_running_on_windows`)."""
    if _platform() == "win32":
        return _running_on_windows(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:  # a process of another user is there
        return True
    except (OSError, OverflowError):  # ProcessLookupError: gone; the rest: not a process we can tell is RustGIS
        return False
    return True


def endpoint() -> Endpoint | None:
    """Where a running RustGIS's MCP over HTTP listens, as RustGIS publishes it: the ``http_url`` and
    ``http_token_file`` of its ``endpoint.json`` in :func:`token_dir`, taken only while its ``pid`` runs. None when
    there is no such file, it cannot be read or is not what RustGIS writes, its ``pid`` does not run (a RustGIS that
    crashed leaves its file behind), or it names no HTTP address (the relay runs, agent control is off).

    There is no default address to fall back on: RustGIS keeps its token for good, and any program of any account may be
    listening at 127.0.0.1:8768 when RustGIS is not, so the token goes only to the address of a RustGIS that was seen
    running. Nothing is asked of the network: whether it still answers is for the first request to find out."""
    try:
        info = json.loads((token_dir() / ENDPOINT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    pid, url, token_file = info.get("pid"), info.get("http_url"), info.get("http_token_file")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    if not all(isinstance(value, str) and value for value in (url, token_file)):
        return None
    return Endpoint(url, Path(token_file)) if _process_runs(pid) else None


def _result_of(reply: object) -> dict:
    """The ``result`` of a JSON-RPC reply; its ``error`` raised as what it means."""
    if not isinstance(reply, dict):
        raise RustGisError("RustGIS answered with something that is not a JSON-RPC reply")
    error = reply.get("error")
    if error:
        info = error if isinstance(error, dict) else {}
        message = str(info.get("message") or error)
        if info.get("code") in BUSY_CODES:
            raise RustGisBusy(message)
        raise RustGisRefused(message)
    result = reply.get("result")
    if not isinstance(result, dict):
        raise RustGisError("RustGIS's reply has no result")
    return result


def _answer(tool: str, result: dict) -> dict:
    """The JSON a tool answered with: the text of ``result.content``, parsed."""
    parts = result.get("content")
    parts = parts if isinstance(parts, list) else []
    text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict) and p.get("type", "text") == "text")
    if result.get("isError"):
        raise RustGisRefused(text.strip() or f"RustGIS refused {tool}")
    try:
        answer = json.loads(text)
    except ValueError:
        raise RustGisRefused(f"RustGIS answered {tool} with text that is not JSON: {text.strip()[:200]!r}") from None
    if not isinstance(answer, dict):
        raise RustGisRefused(f"RustGIS answered {tool} with JSON that is not an object")
    return answer


class _Transport:
    """What the two transports share: the JSON-RPC requests and what RustGIS's answers mean."""

    def __init__(self) -> None:
        self._ids = itertools.count(1)

    def _rpc(self, method: str, params: dict) -> dict:
        """One JSON-RPC request; the ``result`` of its reply."""
        raise NotImplementedError

    def initialize(self) -> str:
        """The handshake: the version of the RustGIS that answers (``serverInfo.version``). :class:`RustGisTooOld`
        below :data:`MIN_VERSION`."""
        result = self._rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                          "clientInfo": {"name": "pyarchinit-photogrammetry", "version": __version__}})
        info = result.get("serverInfo")
        version = str(info.get("version") or "").strip() if isinstance(info, dict) else ""
        if not version:
            raise RustGisRefused("what answers did not say which version of RustGIS it is")
        if too_old(version):
            raise RustGisTooOld(f"RustGIS {version} is older than {MIN_TEXT}, which the link needs")
        return version

    def call(self, tool: str, args: dict) -> dict:
        """A tool of RustGIS's MCP server and the JSON it answers (the text of ``result.content``, parsed).
        :class:`RustGisRefused` when the tool said no (``result.isError``) or answered anything but a JSON object."""
        try:
            answer = _answer(tool, self._rpc("tools/call", {"name": tool, "arguments": args}))
        except RustGisError as exc:
            log.debug("RustGIS %s: %s: %s", tool, type(exc).__name__, exc)
            raise
        log.debug("RustGIS %s: answered", tool)
        return answer


class HttpTransport(_Transport):
    """RustGIS's MCP over HTTP: the app of this computer, while its agent control is ticked. Stateless (no session to
    open), one connection per request. ``timeout`` (seconds) bounds the wait for each answer; a short one suits what
    is harmless to repeat (a read), not a script that edits (see the notes at the top: the request may still run)."""

    def __init__(self, endpoint: Endpoint, timeout: float = 30.0):
        super().__init__()
        self.endpoint = endpoint
        self.timeout = timeout
        # this computer is reached directly, never through a proxy of the shell or of the system (urllib's default
        # opener would ask one for 127.0.0.1 too)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _token(self) -> str:
        path = self.endpoint.token_file
        try:
            token = path.read_text(encoding="ascii", errors="replace").strip()
        except OSError as exc:
            raise RustGisOff(f"the token of RustGIS cannot be read ({path}): {exc.strerror or 'unreadable'}") from None
        if not token or not token.isascii() or not token.isprintable() or " " in token:
            raise RustGisOff(f"{path} holds no token")
        return token

    def _gave_up(self) -> RustGisBusy:
        return RustGisBusy(f"RustGIS did not answer within {self.timeout:g} s: it does not draw (minimized, hidden, a "
                           "dialog open) or is slow, and the request may still run when it is back",
                           outcome_unknown=True)

    def _refusal(self, status: int, body: str) -> RustGisError:
        """What an HTTP status of the listener itself means (its body is ``{"error": "..."}``)."""
        try:
            words = str(json.loads(body).get("error") or "")
        except (ValueError, AttributeError):
            words = body.strip()
        words = words[:300]
        if status == 401:
            return RustGisOff(f"RustGIS does not accept the token in {self.endpoint.token_file}: it is not the one of "
                              "the RustGIS that is running (another run, another settings folder)")
        if status == 503:  # busy, too many requests, did not run in time (and cancelled), stopping
            return RustGisBusy(words or "RustGIS is busy (HTTP 503)")
        if status == 504:  # the app is running the request and has not finished
            return RustGisBusy(words or "RustGIS is still running the request (HTTP 504)", outcome_unknown=True)
        return RustGisRefused(f"RustGIS answered HTTP {status}" + (f": {words}" if words else ""))

    def _rpc(self, method: str, params: dict) -> dict:
        token = self._token()  # read at each request, held in this call only
        message = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        request = urllib.request.Request(
            self.endpoint.url, data=json.dumps(message).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

        def scrubbed(raw: bytes) -> str:  # a RustGIS that quotes the request it refuses does not carry the token on
            return raw.decode("utf-8", errors="replace").replace(token, "<token>")

        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = scrubbed(response.read())
        except urllib.error.HTTPError as exc:
            try:
                words = scrubbed(exc.read(4096))
            finally:
                exc.close()
            raise self._refusal(exc.code, words) from None
        except TimeoutError:
            raise self._gave_up() from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise self._gave_up() from None
            raise RustGisOff(f"nothing answers at {self.endpoint.url}: RustGIS is closed, or its agent control "
                             "is off") from None
        except OSError:  # a connection reset or refused, RustGIS closing while it answers
            raise RustGisOff(f"nothing answers at {self.endpoint.url}: RustGIS is closed, or its agent control "
                             "is off") from None
        except http.client.HTTPException as exc:
            raise RustGisError(f"the answer of {self.endpoint.url} could not be read ({type(exc).__name__})") from None
        try:
            reply = json.loads(body)
        except ValueError:
            raise RustGisError(f"the answer of {self.endpoint.url} is not JSON: not RustGIS that answers") from None
        return _result_of(reply)


class StdioTransport(_Transport):
    """The tools of RustGIS over the pipes of ``rustgis-cli mcp ...`` (``args`` follow the program: ``["mcp", "--data",
    "x.gpkg", "--yes"]``): the engine of the tests, a RustGIS without the window. It is started and greeted
    (``initialize``, then ``notifications/initialized``) at once; ``version`` is the one it gave. One call at a time.
    The engine is stopped by :meth:`close` (or by leaving the ``with`` block)."""

    CLOSE_WAIT = 5.0  # seconds the engine has to end after its input is closed, before it is killed

    def __init__(self, cli: Path, args: list[str], timeout: float = 120.0):
        super().__init__()
        self.cli, self.args, self.timeout = Path(cli).absolute(), list(args), timeout  # absolute: spawned, not forked
        self.version: str | None = None
        self._lock = threading.Lock()
        self._lines: queue.Queue = queue.Queue()
        self._ended = False
        self._errors = tempfile.TemporaryFile()  # the engine's stderr: read when it dies, and never a full pipe
        try:
            self._process = subprocess.Popen([str(self.cli), *self.args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=self._errors, encoding="utf-8", errors="replace",
                                             close_fds=False)  # posix_spawn: an absolute program and no cwd
        except OSError as exc:
            self._errors.close()
            raise RustGisError(f"{self.cli} could not be started: {exc.strerror or exc}") from None
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            self.initialize()
        except BaseException:
            self.close()
            raise

    def _read(self) -> None:
        for line in self._process.stdout:
            self._lines.put(line)
        self._lines.put(None)  # the end of its output: it is gone

    def _gone(self) -> RustGisError:
        try:
            code = self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            code = None
        self._errors.seek(0)
        said = self._errors.read().decode("utf-8", errors="replace").strip()
        return RustGisError(f"{self.cli.name} stopped" + (f" (exit code {code})" if code is not None else "")
                            + (f": {said[-300:]}" if said else ""))

    def _write(self, message: dict) -> None:
        try:
            self._process.stdin.write(json.dumps(message) + "\n")
            self._process.stdin.flush()
        except (OSError, ValueError):  # a closed pipe: the engine is gone
            raise self._gone() from None

    def _rpc(self, method: str, params: dict) -> dict:
        with self._lock:
            if self._ended:
                raise self._gone()
            rid = next(self._ids)
            self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    line = self._lines.get(timeout=max(0.0, deadline - time.monotonic()))
                except queue.Empty:
                    raise RustGisBusy(f"{self.cli.name} did not answer within {self.timeout:g} s",
                                      outcome_unknown=True) from None
                if line is None:
                    self._ended = True
                    raise self._gone()
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue  # not a message
                if isinstance(reply, dict) and reply.get("id") == rid:  # a call given up on has its own id
                    return _result_of(reply)

    def initialize(self) -> str:
        if self.version is None:
            self.version = super().initialize()
            with self._lock:
                self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.version

    def close(self) -> None:
        """Ends the engine: its input closed, a few seconds to finish, then killed. Harmless a second time."""
        process = self._process
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=self.CLOSE_WAIT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self._reader.join(timeout=2)
        for stream in (process.stdout, self._errors):
            try:
                stream.close()
            except OSError:
                pass

    def __enter__(self) -> "StdioTransport":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# ---------------------------------------------------------------- RustGIS's edits, back into the live GeoPackage
def _typed(key: str, value):
    """``value`` of column ``key`` as the GeoPackage of the QGIS link keeps it: REAL for
    :data:`.qgis_link.NUMERIC_FIELDS`, INTEGER for its integer and flag columns, TEXT for the rest (a number RustGIS
    gives in a column it typed otherwise is read back as the program wrote it: 6.0 of a text column is "6"); NULL
    kept, and an empty text is NULL, as the program writes it."""
    if value is None or (isinstance(value, float) and value != value) or (isinstance(value, str) and value == ""):
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    kind = _sql_type(key)
    try:
        if kind == "REAL":
            return float(value)
        if kind == "INTEGER":
            return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def normalize(geometry, values: dict) -> tuple[object, dict]:
    """A row as the live GeoPackage of the QGIS link keeps it, so that what RustGIS exports compares with what the
    program wrote: a multi-part geometry of one part becomes that part (RustGIS exports every polygon as a MultiPolygon
    and every line as a MultiLineString), 2D, an empty one None; the values typed as that GeoPackage types its columns
    (:data:`.qgis_link.NUMERIC_FIELDS` REAL, ``INTEGER_FIELDS`` INTEGER, the rest TEXT), NULL kept, the feature id of a
    GeoPackage (``fid``) left out. A geometry of several parts stays one: :meth:`RustGisLink.pull` writes a unit drawn
    as several polygons as a row per part (``ppg_parte`` 1, 2...)."""
    import shapely

    if geometry is not None and geometry.is_empty:
        geometry = None
    if geometry is not None:
        if geometry.has_z:
            geometry = shapely.force_2d(geometry)
        if geometry.geom_type.startswith("Multi") and len(geometry.geoms) == 1:
            geometry = geometry.geoms[0]
    return geometry, {str(k): _typed(str(k), v) for k, v in values.items() if str(k) != "fid"}


def _layout(geometry) -> tuple:
    """How a geometry is made - its type, parts, rings and number of vertices - without its coordinates."""
    kind = geometry.geom_type
    if kind == "Polygon":
        return kind, len(geometry.exterior.coords), tuple(len(ring.coords) for ring in geometry.interiors)
    if hasattr(geometry, "geoms"):
        return kind, tuple(_layout(part) for part in geometry.geoms)
    return kind, len(geometry.coords)


def _same_geometry(a, b) -> bool:
    """Whether two geometries (of :func:`normalize`) are the same: made the same way and every vertex within
    :data:`TOLERANCE_M` (an untouched feature comes back from RustGIS moved by about a nanometre). Never by rounding the
    coordinates: a nanometre carries a vertex that lies on a half millimetre (SAM's outlines follow a raster grid) to
    the other side of the rounding."""
    import numpy as np
    import shapely

    if a is None or b is None:
        return a is None and b is None
    if _layout(a) != _layout(b):
        return False
    xa, xb = shapely.get_coordinates(a), shapely.get_coordinates(b)
    return xa.shape == xb.shape and (xa.size == 0 or float(np.abs(xa - xb).max()) <= TOLERANCE_M)


def _same_parts(a: list, b: list) -> bool:
    return len(a) == len(b) and all(_same_geometry(x, y) for x, y in zip(a, b))


def _plain(values: dict) -> dict:
    """The values that make a row: the technical columns and the empty ones left out."""
    return {k: v for k, v in values.items() if k not in TECHNICAL_FIELDS and v is not None}


def _changes(now: dict, before: dict) -> dict:
    """The columns of ``now`` (not technical) whose value is not the one ``before`` has."""
    return {k: v for k, v in now.items() if k not in TECHNICAL_FIELDS and v != before.get(k)}


def _identity(values: dict) -> str | None:
    return str(values.get(ID_FIELD) or "").strip() or None


def _part_of(values: dict, parted: bool) -> int:
    """The part (1, 2...) a row is of its unit, read as :class:`.qgis_link.LiveLink` reads it."""
    try:
        return int(values.get(PART_FIELD) or 1) if parted else 1
    except (TypeError, ValueError):
        return 1


def _parts(geometry, parted: bool) -> list:
    """The polygons of a unit drawn as several (a table of units: a row each), else the geometry as it is."""
    if parted and geometry is not None and geometry.geom_type == "MultiPolygon":
        return list(geometry.geoms)
    return [geometry]


@dataclass
class Drawn:
    """A feature RustGIS has without an identity (drawn there) that is already in the live GeoPackage: its parts and
    values as RustGIS had them at the last look, and the rows (fid) it became there. RustGIS learns their identity -
    given by LiveLink, or here to a feature of several polygons - only with the program's edits: until then every pull
    finds the feature again, and knows it by its geometry."""
    parts: list
    values: dict
    fids: list


@dataclass
class _Row:  # a row of the live GeoPackage, normalized
    fid: int
    key: tuple  # (identity or None, part), as LiveLink keys it
    geometry: object
    values: dict


@dataclass
class _Export:  # a layer as RustGIS exported it
    srs: int
    columns: list  # its attribute columns, in order
    features: list  # (geometry, values) in RustGIS's order
    left_out: int  # features RustGIS said it could not write: what is missing may still be there


def _has_identities(con, table: str) -> bool:
    """Whether ``table`` of the live GeoPackage is a table this version writes (with :data:`.qgis_link.ID_FIELD`)."""
    return ID_FIELD in [r[1] for r in con.execute(f"PRAGMA table_info({gpkg.quote(table)})")]


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def _live_rows(con, table: str) -> tuple[dict, list[_Row]]:
    """The columns of a table of the live GeoPackage (name -> declared type) and its rows by fid, normalized and keyed
    as :class:`.qgis_link.LiveLink` keys them."""
    q = gpkg.quote
    types = {r[1]: r[2] for r in con.execute(f"PRAGMA table_info({q(table)})") if r[1] not in ("fid", "geom")}
    parted = PART_FIELD in types
    names = "".join(f", {q(c)}" for c in types)
    rows = []
    for r in con.execute(f"SELECT fid, geom{names} FROM {q(table)} ORDER BY fid"):
        try:
            geometry = gpkg.geometry(r[1])
        except Exception:  # noqa: BLE001 - a geometry another program wrote that shapely cannot read
            geometry = None
        geometry, values = normalize(geometry, dict(zip(types, r[2:])))
        pid = _identity(values)
        rows.append(_Row(int(r[0]), (pid, _part_of(values, parted) if pid else 1), geometry, values))
    return types, rows


def _by_key(rows: list[_Row]) -> tuple[dict, list]:
    """``{key: row}`` of the rows with an identity (the first of a key: the others are copies), and the others."""
    primary, others = {}, []
    for row in rows:
        if row.key[0] is None or row.key in primary:
            others.append(row)
        else:
            primary[row.key] = row
    return primary, others


def _read_export(path: Path) -> tuple[int, list, list]:
    """The system, the columns and the features (in RustGIS's order) of the GeoPackage ``share.exportLayer`` wrote: one
    table, named after the layer, with a feature id of its own (left out)."""
    q = gpkg.quote
    con = _read_only(path)
    try:
        found = con.execute("SELECT table_name, column_name, srs_id FROM gpkg_geometry_columns").fetchall()
        if len(found) != 1:
            raise RustGisRefused(f"RustGIS's export {path.name} is not one table of features")
        table, geom, srs = found[0]
        info = con.execute(f"PRAGMA table_info({q(table)})").fetchall()
        fid = next((r[1] for r in info if r[5]), None)
        columns = [r[1] for r in info if not r[5] and r[1] != geom]
        names = "".join(f", {q(c)}" for c in columns)
        order = f" ORDER BY {q(fid)}" if fid else ""
        features = [(gpkg.geometry(r[0]), dict(zip(columns, r[1:])))
                    for r in con.execute(f"SELECT {q(geom)}{names} FROM {q(table)}{order}")]
        return int(srs), columns, features
    finally:
        con.close()


def _rustgis_rows(features: list, parted: bool, here: Iterable = (), had: Iterable = ()) -> tuple[dict, list, set]:
    """RustGIS's features as rows of the live table: those with an identity by ``(identity, part)``, and those without
    one (drawn there, or copies of a feature: every attribute is copied) as ``(parts, values)``; the values are of
    :func:`normalize`. A feature with an identity drawn as several polygons is a row per polygon: the polygons after the
    first take the part numbers they took at the last look - the keys RustGIS ``had`` that are no feature's own, in
    order -, else numbers after every part of that identity in the export, in the live table (``here``) and in ``had``,
    never the number of a part the program drew and has not sent. Returns ``(keyed, drawn, extras)``, ``extras`` the
    keys of those polygons."""
    rows = [normalize(g, v) for g, v in features]
    own = {(pid, _part_of(values, parted)) for _, values in rows if (pid := _identity(values)) is not None}
    largest: dict[str, int] = {}
    for pid, part in itertools.chain(own, here, had):
        if pid is not None:
            largest[pid] = max(largest.get(pid, 0), part)
    spare: dict[str, list[int]] = {}
    for pid, part in sorted(key for key in had if key[0] is not None and key not in own):
        spare.setdefault(pid, []).append(part)
    keyed, drawn, extras = {}, [], set()
    for geometry, values in rows:
        pid, parts = _identity(values), _parts(geometry, parted)
        first = (pid, _part_of(values, parted))
        if pid is None or first in keyed:
            drawn.append((parts, values))
            continue
        keyed[first] = (parts[0], values)
        for extra in parts[1:]:
            if spare.get(pid):
                number = spare[pid].pop(0)
            else:
                largest[pid] += 1
                number = largest[pid]
            keyed[(pid, number)] = (extra, values)
            extras.add((pid, number))
    return keyed, drawn, extras


# ---------------------------------------------------------------- the link started, and the program's edits sent
SCRIPT_LINES = 9_000  # lines of one script: RustGIS runs its first 10,000 and drops the others without a word
SCRIPT_CHARS = 8_000_000  # characters of one script: RustGIS refuses a request over 16 MiB, JSON escapes included
WHERE_CHARS = 9_000  # characters of one where clause: RustGIS refuses a clause of more than 10,000
WHERE_TERMS = 250  # terms of one AND chain: RustGIS refuses more than 256
KINDS = {"POLYGON": "Polygon", "LINESTRING": "Polyline", "POINT": "Point"}  # list_layers' type of each kind of table
FIELD_TYPES = {"INTEGER": "long", "REAL": "double"}  # edit.addField's type of a column of that affinity; else "text"
CHECK_LAYER = "ppg-sistema"  # the layer of the point that checks the system (a unique suffix added), removed at once
PLACEHOLDER = "ppg-invio"  # the identity of the feature each layer's part of a send adds first (a unique suffix added)
_NOWHERE = {  # that feature's geometry when no feature of the part can lend it its own: it is deleted at once
    "Polygon": {"type": "Polygon", "coordinates": [[[0.0, 0.0], [1e-6, 0.0], [0.0, 1e-6], [0.0, 0.0]]]},
    "Polyline": {"type": "LineString", "coordinates": [[0.0, 0.0], [1e-6, 0.0]]},
    "Point": {"type": "Point", "coordinates": [0.0, 0.0]}}


def _ref(layer_id):
    """A layer id as RustGIS must be given it: a number. RustGIS reads a text as a layer's NAME first and as an id only
    when no layer has that name, so ``"7"`` would be a layer the user named 7."""
    text = str(layer_id)
    return int(text) if text.isascii() and text.isdigit() else text


def _quoted(name: str) -> str:
    """A field name as RustGIS's where clauses read it whatever it is (a keyword too): in double quotes."""
    return '"' + str(name).replace('"', '""') + '"'


def _literal(value) -> str | None:
    """``value`` as a literal of RustGIS's where clauses: a text in single quotes (a quote in it doubled), a number
    written out without an exponent (RustGIS reads ``1e-05`` as the bad number ``1e``); None for what no literal says
    (a blob, an infinite number)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        import numpy as np

        return np.format_float_positional(value, trim="-")
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _identity_clauses(identities: Iterable) -> list[str]:
    """``"ppg_id" IN (...)`` clauses that together name every identity of ``identities``, each short enough for RustGIS
    (:data:`WHERE_CHARS`): a send names thousands when the program computes a table again."""
    head = f"{_quoted(ID_FIELD)} IN ("
    out, items, size = [], [], len(head) + 1
    for identity in identities:
        literal = _literal(str(identity))
        if items and size + len(literal) + 2 > WHERE_CHARS:
            out.append(head + ", ".join(items) + ")")
            items, size = [], len(head) + 1
        items.append(literal)
        size += len(literal) + 2
    if items:
        out.append(head + ", ".join(items) + ")")
    return out


def _fields(layer) -> dict[str, tuple[str, str]]:
    """The fields of a layer as ``list_layers`` gives them (``"name (Type)"``): lower-case name -> (name, type)."""
    out = {}
    for text in (layer.get("fields") if isinstance(layer, dict) else None) or []:
        if isinstance(text, str):
            name, sep, kind = text.rpartition(" (")
            out[(name if sep else text).lower()] = (name, kind.rstrip(")")) if sep else (text, "")
    return out


def _field_name_ok(name: str) -> bool:
    """Whether RustGIS takes ``name`` for a field it is asked to add (``edit.addField``): letters, digits and _, at most
    64 of them."""
    return 0 < len(name) <= 64 and all(c.isalnum() or c == "_" for c in name)


def _same_file(source, path: Path) -> bool:
    """Whether the source ``list_layers`` gives for a layer is the file ``path``."""
    if not isinstance(source, str) or not source:
        return False
    try:
        return os.path.samefile(source, path)
    except (OSError, ValueError):
        return False


def _lonlat(geometry, transformer) -> dict:
    """GeoJSON of ``geometry`` in longitude and latitude, as RustGIS's edit commands take it, through ``transformer``
    (pyproj from the chunk's system: where RustGIS's own conversion puts it, measured to the nanometre).
    :class:`ValueError` when a vertex has no longitude and latitude."""
    import numpy as np
    import shapely
    from shapely.geometry import mapping

    def move(xy):
        lon, lat = transformer.transform(xy[:, 0], xy[:, 1])
        return np.column_stack([lon, lat])

    moved = shapely.transform(geometry, move)
    if not np.isfinite(shapely.get_coordinates(moved)).all():
        raise ValueError("a vertex without longitude and latitude")
    return mapping(moved)


def _line(command: str, params: dict | None = None) -> str:
    """One line of a script: the command and its parameters as JSON (accents as they are, never NaN)."""
    return f"{command} {json.dumps(params or {}, ensure_ascii=False, allow_nan=False, separators=(',', ':'))}"


def _said(exc: Exception) -> str:
    """What RustGIS said when a script failed: the error its answer names (the line and why), else the message."""
    text = str(exc)
    try:
        info = json.loads(text)
    except ValueError:
        return text
    return str(info.get("error") or text) if isinstance(info, dict) else text


def _status_key(answer) -> str | None:
    """``edit.status``'s answer as something to compare: per layer the features added, modified and deleted, and
    whether the project is unsaved."""
    if not isinstance(answer, dict):
        return None
    return json.dumps({"layers": answer.get("layers"), "unsaved": answer.get("unsaved")}, sort_keys=True, default=str)


def _added_layer(answer) -> int:
    """The id of the layer ``layer.addData`` added."""
    result = answer.get("result") if isinstance(answer, dict) else None
    lid = result.get("layer") if isinstance(result, dict) else None
    if isinstance(lid, bool) or not isinstance(lid, int) or lid <= 0:
        raise RustGisRefused("RustGIS added the data without saying which layer it is")
    return lid


def _digest(con, table: str) -> str:
    """A fingerprint of everything a table of the live GeoPackage holds (its rows as stored, in order)."""
    h = hashlib.sha1()
    for row in con.execute(f"SELECT * FROM {gpkg.quote(table)} ORDER BY fid"):
        h.update(repr(row).encode("utf-8", "backslashreplace"))
    return h.hexdigest()


def _distance(lon: float, lat: float, lon2, lat2) -> float:
    """Metres between two places on the WGS 84 ellipsoid; infinite when the second is no place."""
    try:
        lon2, lat2 = float(lon2), float(lat2)
    except (TypeError, ValueError):
        return math.inf
    if not (math.isfinite(lon2) and math.isfinite(lat2)) or abs(lat2) > 90 or abs(lon2) > 360:
        return math.inf
    from pyproj import Geod

    try:
        return float(Geod(ellps="WGS84").inv(lon, lat, lon2, lat2)[2])
    except Exception:  # noqa: BLE001 - whatever pyproj makes of it, it is not where the point is
        return math.inf


def _one_point(path: Path, crs: str, x: float, y: float) -> None:
    """A GeoPackage of one point in the system of the chunk's drawings, written as the live GeoPackage is."""
    from shapely.geometry import Point

    srs = gpkg.srs_of(crs)
    con = gpkg.connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        gpkg.create(con, srs)
        gpkg.ensure_table(con, "sistema", "POINT", {}, srs[0])
        con.execute('INSERT INTO "sistema" (geom) VALUES (?)', (gpkg.blob(Point(x, y), srs[0]),))
        con.execute("COMMIT")
    finally:
        con.close()


def _same_system(con, tables: Iterable, code: int) -> None:
    """:class:`RuntimeError` when a table of the live GeoPackage is in another system than the link's: its rows would
    be converted from the wrong one and go elsewhere (the chunk's system changed: the link starts again with it)."""
    for table in tables:
        srs = gpkg.table_srs(con, table)
        if srs not in (None, code):
            raise RuntimeError(f"the drawings of {table} are in EPSG:{srs}, the link in EPSG:{code}: write them in the "
                               "chunk's system and start the link again")


def _drawn_clause(drawn: Drawn, fields: dict) -> str | None:
    """A where clause that finds a feature RustGIS has without an identity as it had it at the last look: no identity,
    and every value it had - an empty one null, or an empty text - in the fields its layer has now. Others with the
    same values are found too: the caller counts them. None when no clause RustGIS reads can say it."""
    had = {str(k).lower(): v for k, v in drawn.values.items()}
    terms = [f"{_quoted(fields.get(ID_FIELD, (ID_FIELD, ''))[0])} IS NULL"]
    for low, (name, kind) in sorted(fields.items()):
        if low == ID_FIELD or low not in had:
            continue
        value, field = had[low], _quoted(name)
        if value is None:
            terms.append(f"({field} IS NULL OR {field} = '')" if kind.lower() == "text" else f"{field} IS NULL")
        elif (literal := _literal(value)) is not None:
            terms.append(f"{field} = {literal}")
    clause = " AND ".join(terms)
    return clause if len(terms) <= WHERE_TERMS and len(clause) <= WHERE_CHARS else None


class _Budget:
    """What one script may still take: lines and characters."""

    def __init__(self, lines: int, chars: int):
        self.lines, self.chars = lines, chars

    def take(self, lines: int, chars: int) -> bool:
        if lines > self.lines or chars > self.chars:
            return False
        self.lines -= lines
        self.chars -= chars
        return True

    def give(self, lines: int, chars: int) -> None:
        self.lines += lines
        self.chars += chars


@dataclass
class _Part:  # what a send does to the layer of one table
    table: str
    lines: list  # its lines of the script
    identities: list  # deleted there by identity, and added again with every row of them the program has
    rows: list  # (key, geometry, values) of the rows added: what RustGIS has of them once the send ran
    drawn: list  # the features drawn there without identity that it replaces or deletes
    sent: int  # rows added, and rows deleted for good
    adds: bool  # whether it ends by adding (a feature added is left selected)


@dataclass
class _Send:  # one script of RustGisLink.push
    parts: list
    lines: list
    held: bool  # rows wait for the next send: more than one script holds, or features drawn there not told apart yet
    digests: dict  # one-way table -> its content, all of it now in RustGIS

    @property
    def sent(self) -> int:
        return sum(part.sent for part in self.parts)


class RustGisLink:
    """The drawings of the active chunk in RustGIS and back: which RustGIS layer holds which table of the live
    GeoPackage of the QGIS link (:attr:`layers`), what RustGIS last had of each row (:attr:`baseline`, :attr:`drawn`),
    and the edits made there brought into that GeoPackage as another program's (:meth:`pull`), where
    :meth:`.qgis_link.LiveLink.read_changes` finds them as it finds QGIS's and the window applies them the same way.

    What RustGIS had is what tells an edit made there from one the program has not sent yet: a row RustGIS still has
    as it had it is never written over (the program's version goes to RustGIS), one it changed comes column by column,
    one it no longer has is deleted. It is taken from RustGIS's own export of each layer, when RustGIS is given the
    tables (:meth:`remember`) and at each pull after (:meth:`pull`) - never from the live GeoPackage, which may hold
    rows RustGIS has not got; a pull before :meth:`remember` is refused.

    :meth:`start` gives RustGIS the chunk's tables and remembers what it loaded; :meth:`push` sends it the rows the
    program changed since (what came from RustGIS is never sent back: it matches what RustGIS had), :attr:`pending` says
    whether there are any, :meth:`status_changed` whether something was edited there since the last look, and
    :meth:`stop` forgets the layers (RustGIS keeps them)."""

    def __init__(self, live: LiveLink, transport, chunk_label: str, crs: str):
        self.live = live
        self.transport = transport  # HttpTransport (the app) or StdioTransport (the engine): call(tool, args)
        self.chunk_label = chunk_label
        self.crs = crs
        self.layers: dict[str, str] = {}  # table of the live GeoPackage -> the id of RustGIS's layer that holds it
        # per table, (ppg_id, ppg_parte) -> (geometry, values) of the row as RustGIS last had it, after normalize():
        # compared only within TOLERANCE_M (matches_baseline)
        self.baseline: dict[str, dict[tuple, tuple]] = {}
        self.drawn: dict[str, list[Drawn]] = {}  # per table, what RustGIS has without an identity (see Drawn)
        self.rasters: dict[str, str] = {}  # name -> the id of RustGIS's layer of each raster given at the start
        self._group = None  # the id of the chunk's group in RustGIS
        self._status: str | None = None  # RustGIS's edit status at the last look (status_changed)
        self._owed = False  # rows a send did not deliver: RustGIS busy, an answer lost, more than one script holds
        self._pushed_at = None  # the live GeoPackage as the file system saw it when RustGIS last got all of it
        self._unsure: dict[str, int] = {}  # table -> the highest id RustGIS had used when it was asked to add it again
        self._digests: dict[str, str] = {}  # one-way table -> the content of the live table RustGIS has all of

    @property
    def scratch(self) -> Path:
        """The folder beside the live GeoPackage where RustGIS exports the layers for a pull (a file a table,
        replaced at each export)."""
        return self.live.path.parent / SCRATCH_DIR

    def _ready(self, tables: list) -> list:
        """The tables of ``tables`` the live GeoPackage holds as this version writes them (with their identities)."""
        if not gpkg.is_geopackage(self.live.path):
            return []
        con = _read_only(self.live.path)
        try:
            return [t for t in tables if _has_identities(con, t)]
        finally:
            con.close()

    def remember(self, tables=None) -> None:
        """What RustGIS has of ``tables`` (every linked two-way table when None) when the link has just given them to
        it - when it starts, or when a layer is added again -, taken from RustGIS's own export of each layer, never
        from the live GeoPackage: a row the program or QGIS wrote meanwhile, or one RustGIS did not load, is not among
        what it had, so no pull takes it for deleted there (the program's rows go to RustGIS with its edits). The
        features it has without an identity are found among the live rows by their geometry (:attr:`drawn`, within
        :data:`TOLERANCE_M`); one with no such row was drawn there meanwhile and comes in at the next pull, as do the
        polygons it has beyond the rows it was given. The errors of RustGIS (:class:`RustGisBusy`...) leave what was
        known as it was."""
        tables = [t for t in (self.layers if tables is None else tables) if t in TWO_WAY_TABLES]
        exports = {t: self._export(t) for t in self._ready(tables)}  # RustGIS first, then the file as it is now
        found = {}
        if exports:
            con = _read_only(self.live.path)
            try:
                found = {t: _live_rows(con, t) for t in exports}
            finally:
                con.close()
        for table in tables:
            if table not in exports:  # not written by this version yet: nothing RustGIS has of it is known
                self.baseline[table], self.drawn[table] = {}, []
                continue
            types, rows = found[table]
            parted = PART_FIELD in types
            keyed, drawn, extras = _rustgis_rows(exports[table].features, parted, [r.key for r in rows if r.key[0]])
            self.baseline[table] = {key: row for key, row in keyed.items() if key not in extras}
            primary, others = _by_key(rows)
            # without an identity there: the rows given without one (drawn in QGIS, copies), then those given an
            # identity here since RustGIS loaded them
            candidates = others + [r for key, r in primary.items() if key not in keyed]
            self.drawn[table] = []
            for parts, values in drawn:
                row = next((r for r in candidates if _same_parts(parts, _parts(r.geometry, parted))), None)
                if row is not None:
                    candidates.remove(row)
                    self.drawn[table].append(Drawn(parts, values, [row.fid]))

    def matches_baseline(self, table: str, key: tuple, geometry, values: dict) -> bool:
        """Whether RustGIS last had row ``key`` (``(ppg_id, ppg_parte)``) of ``table`` with this geometry and these
        values: both taken through :func:`normalize`, the geometry made the same way (type, parts, rings, number of
        vertices) with every vertex within :data:`TOLERANCE_M` of RustGIS's, the values equal once the technical
        columns (``ppg_id``, ``ppg_parte``, ``ppg_sig``) and the empty ones are left out. False when RustGIS did not
        have that row."""
        had = self.baseline.get(table, {}).get(tuple(key))
        if had is None:
            return False
        geometry, values = normalize(geometry, values)
        return _same_geometry(geometry, had[0]) and _plain(values) == _plain(had[1])

    def pull(self) -> dict[str, tuple[int, int, int]]:
        """What was edited in RustGIS since the last look, written into the live GeoPackage as another program's: each
        linked two-way table exported by RustGIS (``share.exportLayer``, in the system its layer was read in, the
        chunk's) into :attr:`scratch`, then compared with what RustGIS had and with the live rows. A row RustGIS
        changed is updated in place - its geometry when it moved more than :data:`TOLERANCE_M`, the columns changed
        there; ``ppg_id``, ``ppg_parte`` and ``ppg_sig`` as they are, so the reader takes it for an edit of another
        program -; a feature drawn there is inserted without an identity (LiveLink gives it one), a unit drawn there
        as several polygons as a row per part under one new identity; a row RustGIS no longer has is deleted. The
        program's own edits not sent yet are left as they are. Every write in one transaction of a connection of its
        own, so the reader's connection sees a new ``PRAGMA data_version``. Returns ``(changed, added, deleted)`` rows
        per linked two-way table.

        :class:`RustGisBusy` and the other errors of RustGIS leave the GeoPackage as it was (nothing is lost: try
        again); :class:`RustGisRefused` too when an export is not in the system of the drawings, and
        :class:`sqlite3.OperationalError` (database locked) when another program holds the file."""
        tables = [t for t in TWO_WAY_TABLES if t in self.layers]
        unknown = [t for t in tables if t not in self.baseline]
        if unknown:
            raise RuntimeError(f"what RustGIS was given of {', '.join(unknown)} is not known: remember() the tables "
                               "when RustGIS gets them")
        counts = {t: (0, 0, 0) for t in tables}
        exports = {t: self._export(t) for t in self._ready(tables)}  # RustGIS first: never asked while the file is held
        if not exports:
            return counts
        states = {}
        con = gpkg.connect(self.live.path)
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                for table, export in exports.items():
                    counts[table], states[table] = self._pull_table(con, table, export)
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.close()
        for table, (had, drawn) in states.items():
            self.baseline[table], self.drawn[table] = had, drawn
        return counts

    def _export(self, table: str) -> _Export:
        """``table``'s layer exported by RustGIS into :attr:`scratch` (the file of the last pull removed first: RustGIS
        never replaces a file) and read."""
        import shapely.errors

        self.scratch.mkdir(parents=True, exist_ok=True)
        path = (self.scratch / f"{table}.gpkg").absolute()
        for suffix in EXPORT_SUFFIXES:
            Path(f"{path}{suffix}").unlink(missing_ok=True)
        answer = self.transport.call("run_command", {"command": "share.exportLayer",
                                                     "params": {"layer": _ref(self.layers[table]), "path": str(path)}})
        if not path.is_file():
            raise RustGisRefused(f"RustGIS said it exported {table} but wrote no {path}")
        try:
            left_out = max(0, int(answer.get("leftOut") or 0))
        except (TypeError, ValueError):
            left_out = 0
        try:
            srs, columns, features = _read_export(path)
        except (sqlite3.Error, ValueError, shapely.errors.ShapelyError) as exc:
            raise RustGisError(f"RustGIS's export of {table} cannot be read: {exc}") from None
        if ID_FIELD not in {c.lower() for c in columns}:  # every feature would look drawn there, every row deleted
            raise RustGisRefused(f"RustGIS's layer of {table} has no field {ID_FIELD} (removed there, or another layer "
                                 "under that id): its edits were not taken")
        if left_out:
            log.warning("RustGIS left %d features of %s out of its export: none of its rows is taken for deleted",
                        left_out, table)
        return _Export(srs, columns, features, left_out)

    def _pull_table(self, con, table: str, export: _Export) -> tuple[tuple[int, int, int], tuple[dict, list]]:
        """One table of :meth:`pull`, inside its transaction; returns the rows changed, added and deleted, and what
        RustGIS has now (the baseline and the features without identity), kept once the transaction is committed."""
        q = gpkg.quote
        srs = gpkg.table_srs(con, table)
        if srs is not None and export.srs != srs:
            raise RustGisRefused(f"RustGIS exported {table} in EPSG:{export.srs}, the drawings are in EPSG:{srs}: "
                                 "its edits were not taken")
        srs = export.srs if srs is None else srs
        types, rows = _live_rows(con, table)
        have = {c.lower() for c in types} | {"fid", "geom"}
        for column in export.columns:  # a field added in RustGIS: a column of the table, as QGIS adds one
            if column.lower() not in have and column not in TECHNICAL_FIELDS:
                con.execute(f"ALTER TABLE {q(table)} ADD COLUMN {q(column)} {_sql_type(column)}")
                types[column] = _sql_type(column)
                have.add(column.lower())
        parted = PART_FIELD in types
        primary, _ = _by_key(rows)
        by_fid = {r.fid: r for r in rows}
        had, now = self.baseline.get(table, {}), {}
        keyed, drawn, _ = _rustgis_rows(export.features, parted, [r.key for r in rows if r.key[0]], had)
        changed = added = deleted = 0
        gone: set[int] = set()

        def insert(geometry, values: dict, key: tuple | None) -> int:
            cells = {c: _bind(_affinity(types[c]), v) for c, v in values.items()
                     if c in types and c not in TECHNICAL_FIELDS}
            if key is not None:  # an identity of its own (else the reader gives it one)
                cells[ID_FIELD] = key[0]
                if parted:
                    cells[PART_FIELD] = key[1]
            names = "".join(f", {q(c)}" for c in cells)
            blob = None if geometry is None else gpkg.blob(geometry, srs)
            return con.execute(f"INSERT INTO {q(table)} (geom{names}) VALUES (?{', ?' * len(cells)})",
                               (blob, *cells.values())).lastrowid

        def update(row: _Row, geometry, moved: bool, edited: dict) -> bool:
            sets, args = [], []
            if moved and not _same_geometry(geometry, row.geometry):
                sets.append("geom = ?")
                args.append(None if geometry is None else gpkg.blob(geometry, srs))
            for column, value in edited.items():
                if column in types and value != row.values.get(column):
                    sets.append(f"{q(column)} = ?")
                    args.append(_bind(_affinity(types[column]), value))
            if sets:  # never ppg_id, ppg_parte and ppg_sig: the reader takes the row for another program's edit
                con.execute(f"UPDATE {q(table)} SET {', '.join(sets)} WHERE fid = ?", (*args, row.fid))
            return bool(sets)

        def delete(fid: int) -> None:
            con.execute(f"DELETE FROM {q(table)} WHERE fid = ?", (fid,))
            gone.add(fid)

        for key, (geometry, values) in keyed.items():
            now[key] = (geometry, values)
            row, before = primary.get(key), had.get(key)
            if before is None:  # RustGIS was not given it here (a send not answered, a feature pasted from elsewhere)
                if row is None:  # an identity the program does not have: it comes in with it
                    insert(geometry, values, key)
                    added += 1
                continue  # else the program's version is the one RustGIS gets
            moved, edited = not _same_geometry(geometry, before[0]), _changes(values, before[1])
            if not moved and not edited:
                continue  # unchanged there: what the program changed waits to be sent
            if row is None:  # changed there, deleted here: it comes back, as an edit of QGIS to it does
                insert(geometry, values, key)
                added += 1
            elif update(row, geometry, moved, edited):
                changed += 1
        for key, before in had.items():
            if key in keyed:
                continue
            if export.left_out:  # perhaps left out of the export, not deleted: RustGIS may still have it
                now[key] = before
            elif key in primary:  # deleted there
                delete(primary[key].fid)
                deleted += 1

        memory, kept, new = list(self.drawn.get(table, [])), [], []
        for parts, values in drawn:
            seen = next((m for m in memory if _same_parts(m.parts, parts)), None)
            if seen is None:
                new.append((parts, values))
                continue
            memory.remove(seen)
            edited = _changes(values, seen.values)  # set there since the last look; the program's columns stay
            for fid in seen.fids:
                if edited and fid in by_fid and fid not in gone and update(by_fid[fid], None, False, edited):
                    changed += 1
            kept.append(Drawn(parts, values, seen.fids))
        for parts, values in new:  # drawn there since the last look
            if len(parts) > 1:  # one unit of several polygons: a row each, one identity
                pid = new_id()
                fids = [insert(part, values, (pid, n)) for n, part in enumerate(parts, 1)]
            else:
                fids = [insert(parts[0], values, None)]
            added += len(fids)
            kept.append(Drawn(parts, values, fids))
        for seen in memory:  # RustGIS no longer has it as it was: deleted there, or it has its identity now
            if export.left_out:
                kept.append(seen)
                continue
            for fid in seen.fids:
                row = by_fid.get(fid)
                if row is not None and fid not in gone and not (row.key[0] is not None and row.key in keyed):
                    delete(fid)
                    deleted += 1
        if changed or added or deleted:
            gpkg.set_extent(con, table, _bounds(con, table))
        return (changed, added, deleted), (now, kept)

    # ------------------------------------------------------------ the link started, and the program's edits sent
    def start(self, rasters: Iterable[dict] = ()) -> dict:
        """Gives RustGIS the chunk. A local frame, or a system without an EPSG code, is refused before RustGIS is asked
        anything; then RustGIS's version, and the system: a point of the chunk's system, read by RustGIS, exported in
        longitude and latitude and removed again in one step, must come back where pyproj puts it (within
        :data:`TOLERANCE_M`: RustGIS takes some systems it cannot convert, a Cassini or polyconic projection, and places
        them elsewhere) - else :class:`RustGisRefused` naming the system. Then the ``rasters`` (``{"path", "name"}``,
        read in place: one RustGIS refuses is left out and said in ``"refused"``), every table of
        :data:`.qgis_link.LIVE_TABLES` from the live GeoPackage (empty ones too: the units are drawn in RustGIS on a
        chunk that has none) named as in QGIS, above the rasters, all in a group named after the chunk. What RustGIS
        loaded is remembered right after (:meth:`remember`: its own export of the two-way tables; the file as it was
        read for the others).

        Returns ``{"layers": n, "rasters": n, "version": "..."}``. A start that fails (RustGIS busy, a refusal) leaves
        the link stopped, and takes out of RustGIS the layers it added when RustGIS still answers."""
        code = self._epsg()
        if not gpkg.is_geopackage(self.live.path):
            raise RuntimeError(f"{self.live.path} is not written yet: the drawings go to RustGIS from it")
        self.stop()
        version = self.transport.initialize()
        self._check_system(code)
        added: list[int] = []
        refused: dict[str, str] = {}
        rasters_added: dict[str, str] = {}
        try:
            for raster in rasters:
                path = Path(raster["path"]).absolute()
                name = str(raster.get("name") or path.stem)
                try:
                    lid = _added_layer(self._command("layer.addData", {"path": str(path), "name": name}))
                except RustGisRefused as exc:
                    log.warning("RustGIS did not take the raster %s: %s", name, exc)
                    refused[name] = str(exc)
                    continue
                added.append(lid)
                rasters_added[name] = str(lid)
            stamp = self._fingerprint()  # what RustGIS reads now: written later, it is sent
            one_way = {t: self._file_rows(t) for t in LIVE_TABLES if t not in TWO_WAY_TABLES}
            layers: dict[str, str] = {}
            for table, (title, _) in LIVE_TABLES.items():
                lid = _added_layer(self._command("layer.addData",
                                                 {"path": str(self.live.path), "layer": table, "name": title}))
                added.append(lid)
                layers[table] = str(lid)
            self.layers, self.rasters = layers, rasters_added
            self.remember()  # right after RustGIS loaded them: an edit made there later is an edit
            self.baseline.update(one_way)
            members = [_ref(i) for i in (*layers.values(), *rasters_added.values())]
            group = self._command("group.add", {"name": self.chunk_label, "layers": members})
            self._group = group.get("group") if isinstance(group, dict) else None
            self._status = _status_key(self._command("edit.status", {}))
        except BaseException as exc:
            self.stop()
            if added and not isinstance(exc, (RustGisBusy, RustGisOff)):
                self._remove(added)
            raise
        self._pushed_at = stamp
        out = {"layers": len(layers), "rasters": len(rasters_added), "version": version}
        if refused:
            out["refused"] = refused
        return out

    def push(self) -> int:
        """Sends RustGIS the rows of the live GeoPackage it does not have as they are: those that do not match
        :attr:`baseline` (:meth:`matches_baseline`, within :data:`TOLERANCE_M`: what came from RustGIS is never sent
        back), and those it has that the program no longer has. One ``run`` script, one step of RustGIS's undo:
        for each layer, ``select.byAttributes`` by ``ppg_id`` of every identity touched - every part of it: RustGIS may
        hold several under one feature - and of the features drawn there without identity that the program has given
        one, or deleted (found by their values, and only when nothing else there has the same), then ``edit.delete``,
        then ``edit.addFeature`` of every row of those identities, in longitude and latitude. Deleting before adding
        makes the script harmless to run twice: a send whose answer was lost may run when RustGIS draws again; a
        feature added first (and selected with the others) keeps the selection from being empty, which RustGIS refuses
        to delete. A column RustGIS's layer does not have becomes a field first. Returns the rows sent (added, or
        deleted for good).

        A layer the user removed in RustGIS is added again first, once, its rows whole (:meth:`remember` right after).
        A send larger than one script holds goes in pieces: :attr:`pending` stays true until the last. The errors of
        RustGIS (:class:`RustGisBusy`...) leave what RustGIS had as it was and the rows pending: the next push sends
        them. :class:`RuntimeError` before :meth:`start`."""
        if not self.layers:
            raise RuntimeError("the link has not started: start() it first")
        stamp = self._fingerprint()  # the file as it is before it is read: written later, it is sent later
        try:
            send = self._send(self._linked())
            answer = None
            if send.lines:
                try:
                    answer = self.transport.call("run", {"script": "\n".join(send.lines)})
                except RustGisRefused as exc:
                    raise RustGisRefused(f"RustGIS refused the program's edits: {_said(exc)}") from None
        except BaseException:
            self._owed = True
            raise
        if send.lines:
            self._delivered(send, answer)
        self._digests.update(send.digests)
        self._owed, self._pushed_at = send.held, stamp
        return send.sent

    @property
    def pending(self) -> bool:
        """Whether the program may have rows RustGIS has not received: a send that did not go through (RustGIS busy,
        an answer lost) or left rows for the next one, or the live GeoPackage written since the last send. Cheap (the
        file's dates); :meth:`push` finds which rows, and sends nothing when none differs."""
        return bool(self.layers) and (self._owed or self._fingerprint() != self._pushed_at)

    def status_changed(self) -> bool:
        """Whether RustGIS's edit status (``edit.status``: per layer, the features added, modified and deleted since its
        project was saved) is not what it was at the last look: one cheap call, to ask often. Each send ends by reading
        it, so the program's own edits are not taken for RustGIS's. A feature edited twice there may leave it as it
        was: a pull now and then sees those."""
        key = _status_key(self._command("edit.status", {}))
        changed = key != self._status
        self._status = key
        return changed

    def stop(self) -> None:
        """Forgets RustGIS's layers: nothing more is sent or asked. RustGIS keeps them, with what they have."""
        self.layers, self.baseline, self.drawn, self.rasters = {}, {}, {}, {}
        self._group = self._status = self._pushed_at = None
        self._owed, self._unsure, self._digests = False, {}, {}

    def _command(self, command: str, params: dict):
        return self.transport.call("run_command", {"command": command, "params": params})

    def _epsg(self) -> int:
        """The EPSG code of the plan of the chunk's system, which RustGIS places the layers by. :class:`RustGisRefused`
        for a local frame and for a system without one."""
        from ..core import crs as crs_mod

        if crs_mod.is_local(self.crs):
            raise RustGisRefused("the chunk is in a local frame (LOCAL): RustGIS places layers only in a system with "
                                 "an EPSG code")
        code = crs_mod.epsg_code(self.crs)
        if code is None:
            raise RustGisRefused(f"the chunk's system ({self.crs}) has no EPSG code: RustGIS places layers only in a "
                                 "system with one")
        return code

    def _fingerprint(self) -> tuple:
        """The live GeoPackage as the file system sees it (identity, size, time of change of it and of a write-ahead
        log beside it): a change says something was written there."""
        out = []
        for suffix in ("", "-wal"):
            try:
                st = os.stat(f"{self.live.path}{suffix}")
            except OSError:
                out.append(None)
                continue
            out.append((st.st_ino, st.st_size, st.st_mtime_ns))
        return tuple(out)

    def _check_system(self, code: int) -> None:
        """Whether RustGIS places the chunk's system where pyproj does: a point of it written in :attr:`scratch`,
        read by RustGIS, exported in longitude and latitude and removed again - one script, nothing left behind - and
        the two places compared. :class:`RustGisRefused` naming the system when RustGIS refuses it or puts the point
        more than :data:`TOLERANCE_M` away."""
        from pyproj import Transformer

        system = f"EPSG:{code}"
        x, y = self._check_point(code)
        lon, lat = Transformer.from_crs(system, "EPSG:4326", always_xy=True).transform(x, y)
        self.scratch.mkdir(parents=True, exist_ok=True)
        source, out = (self.scratch / "sistema.gpkg").absolute(), (self.scratch / "sistema.geojson").absolute()
        for path in (source, out):
            for suffix in EXPORT_SUFFIXES:
                Path(f"{path}{suffix}").unlink(missing_ok=True)
        _one_point(source, self.crs, x, y)
        name = f"{CHECK_LAYER}-{uuid.uuid4().hex}"
        script = "\n".join([_line("layer.addData", {"path": str(source), "name": name}),
                            _line("share.exportLayer", {"layer": name, "path": str(out)}),
                            _line("layer.remove", {"layer": name})])
        try:
            self.transport.call("run", {"script": script})
        except RustGisRefused as exc:
            raise RustGisRefused(f"RustGIS does not take the chunk's system {system}: {_said(exc)}") from None
        try:
            point = json.loads(out.read_text(encoding="utf-8"))["features"][0]["geometry"]["coordinates"]
            distance = _distance(lon, lat, point[0], point[1])
        except (OSError, ValueError, KeyError, IndexError, TypeError):
            raise RustGisRefused(f"RustGIS did not give back the point that checks the system {system}") from None
        if not distance <= TOLERANCE_M:
            where = "elsewhere" if math.isinf(distance) else f"{distance * 1000:.1f} mm away"
            raise RustGisRefused(f"RustGIS places the chunk's system {system} {where} from where it is: the drawings "
                                 "would move there (a projection or a datum shift it does not convert)")
        log.debug("RustGIS places %s within %.3f mm", system, distance * 1000)

    def _check_point(self, code: int) -> tuple[float, float]:
        """Where to check the system: the middle of the drawings when there are any, else of the system's area of use
        (in the system's coordinates)."""
        from pyproj import CRS, Transformer

        con = _read_only(self.live.path)
        try:
            _same_system(con, LIVE_TABLES, code)
            boxes = con.execute(f"SELECT min_x, min_y, max_x, max_y FROM gpkg_contents WHERE table_name IN "
                                f"({', '.join('?' * len(LIVE_TABLES))})", list(LIVE_TABLES)).fetchall()
        finally:
            con.close()
        boxes = [b for b in boxes if all(isinstance(v, (int, float)) and math.isfinite(v) for v in b)]
        if boxes:
            return ((min(b[0] for b in boxes) + max(b[2] for b in boxes)) / 2,
                    (min(b[1] for b in boxes) + max(b[3] for b in boxes)) / 2)
        area = CRS.from_epsg(code).area_of_use
        if area is None:
            raise RustGisRefused(f"the system EPSG:{code} does not say where it is used: it cannot be checked")
        east = area.east if area.east >= area.west else area.east + 360  # across the antimeridian
        lon = ((area.west + east) / 2 + 180) % 360 - 180
        return Transformer.from_crs("EPSG:4326", f"EPSG:{code}", always_xy=True).transform(
            lon, (area.south + area.north) / 2)

    def _remove(self, ids: list) -> None:
        """Takes out of RustGIS the layers of a start that failed (they never were the link's), as far as it answers."""
        try:
            self._command("layer.remove", {"layer": [_ref(i) for i in ids]})
        except RustGisError as exc:
            log.warning("RustGIS keeps %d layers of a link that did not start: %s", len(ids), exc)

    def _file_rows(self, table: str) -> dict:
        """A one-way table of the live GeoPackage as what RustGIS has of it (``{key: (geometry, values)}``), read just
        before RustGIS reads it: what the program writes meanwhile looks unsent, and goes with the next push."""
        if not gpkg.is_geopackage(self.live.path):
            return {}
        con = _read_only(self.live.path)
        try:
            con.execute("BEGIN")  # the content and its fingerprint of one moment
            if not _has_identities(con, table):
                self._digests.pop(table, None)
                return {}
            self._digests[table] = _digest(con, table)
            _, rows = _live_rows(con, table)
        finally:
            con.close()
        primary, _ = _by_key(rows)
        return {key: (row.geometry, row.values) for key, row in primary.items() if row.geometry is not None}

    def _ours(self, layer, table: str) -> bool:
        """Whether a layer ``list_layers`` lists is RustGIS's layer of ``table``: read from the live GeoPackage, of the
        table's kind of geometry, with the identities (a field ``ppg_id``). Another project opened in RustGIS gives
        its own layers the same ids."""
        return (isinstance(layer, dict) and layer.get("type") == KINDS.get(LIVE_TABLES[table][1])
                and ID_FIELD in _fields(layer) and _same_file(layer.get("source"), self.live.path))

    def _linked(self) -> dict:
        """The fields of RustGIS's layer of each linked table (``{table: lower-case name -> (name, type)}``), from
        ``list_layers``. A layer no longer there (removed in RustGIS) is added again, once: one RustGIS added without
        its answer reaching the program is taken, never added twice. None of them there: another project is open in
        RustGIS, or all were removed - :class:`RustGisRefused`."""
        listing = self.transport.call("list_layers", {})
        layers = {str(l.get("id")): l for l in listing.get("layers") or [] if isinstance(l, dict)}
        missing = [t for t, lid in self.layers.items() if not self._ours(layers.get(lid), t)]
        if missing:
            if len(missing) == len(self.layers):
                raise RustGisRefused(f"RustGIS no longer has the layers of {self.chunk_label}: another project is open "
                                     "there, or they were removed")
            groups = [g for g in listing.get("groups") or [] if isinstance(g, dict)]
            used = [int(k) for k in layers if k.isascii() and k.isdigit()]
            used += [g["id"] for g in groups if isinstance(g.get("id"), int)]
            grouped = self._group is not None and any(g.get("id") == self._group for g in groups)
            for table in missing:
                self._again(table, layers, max(used, default=0), grouped)
            listing = self.transport.call("list_layers", {})
            layers = {str(l.get("id")): l for l in listing.get("layers") or [] if isinstance(l, dict)}
        return {t: _fields(layers.get(lid)) for t, lid in self.layers.items()}

    def _again(self, table: str, layers: dict, used: int, grouped: bool) -> None:
        """``table``'s layer added to RustGIS again (or the one it added when its answer was lost, found among
        ``layers``), what it loaded remembered right after, and put in the chunk's group."""
        title, two_way = LIVE_TABLES[table][0], table in TWO_WAY_TABLES
        lid = self._adopted(table, layers)
        rows = None if two_way else self._file_rows(table)
        if lid is None:
            try:
                lid = _added_layer(self._command("layer.addData",
                                                 {"path": str(self.live.path), "layer": table, "name": title}))
            except RustGisBusy as exc:
                if exc.outcome_unknown:  # it may be there: the next look takes it rather than adding another
                    self._unsure[table] = used
                raise
        before = self.layers[table]
        self.layers[table] = str(lid)
        try:
            if two_way:
                self.remember([table])
            else:
                self.baseline[table] = rows
        except BaseException:
            self.layers[table], self._unsure[table] = before, lid - 1  # it is there: the next look takes it
            raise
        log.info("RustGIS's layer of %s was gone: added again", table)
        if grouped:
            try:
                self._command("layer.group", {"layer": lid, "group": self._group})
            except RustGisRefused as exc:
                log.warning("the layer of %s added again stays out of the chunk's group: %s", table, exc)

    def _adopted(self, table: str, layers: dict) -> int | None:
        """The layer RustGIS added for ``table`` when its answer did not reach the program: the first above the ids
        RustGIS had used then, named as the table and read from the live GeoPackage. None when there is none (RustGIS
        cancelled the request)."""
        used = self._unsure.pop(table, None)
        if used is None:
            return None
        title, linked = LIVE_TABLES[table][0], set(self.layers.values())
        found = [int(k) for k, layer in layers.items() if k.isascii() and k.isdigit() and int(k) > used
                 and k not in linked and layer.get("name") == title and self._ours(layer, table)]
        return min(found) if found else None

    def _send(self, fields: dict) -> _Send:
        """The next script of :meth:`push`, within what one script may hold (:data:`SCRIPT_LINES`,
        :data:`SCRIPT_CHARS`): the two-way tables first, then the others in the order of the live GeoPackage."""
        import pyproj

        to = pyproj.Transformer.from_crs(f"EPSG:{self._epsg()}", "EPSG:4326", always_xy=True)
        order = list(LIVE_TABLES)
        tables = sorted(self.layers, key=lambda t: (t not in TWO_WAY_TABLES, order.index(t)))
        found = self._read_live(tables, self._epsg())
        budget = _Budget(SCRIPT_LINES - 2, SCRIPT_CHARS - 64)  # what closes the script: select.clear, edit.status
        placeholder = f"{PLACEHOLDER}-{uuid.uuid4().hex}"
        parts, digests, held = [], {}, False
        for table in tables:
            if table not in found:
                continue
            digest, types, rows = found[table]
            if rows is None:  # a one-way table RustGIS has all of
                continue
            part, waits = self._part(table, types, rows, fields.get(table, {}), to, budget, placeholder)
            held = held or waits
            if part is not None:
                parts.append(part)
            if digest is not None and not waits:
                digests[table] = digest
        lines = [line for part in parts for line in part.lines]
        if lines:
            if parts[-1].adds:
                lines.append(_line("select.clear"))  # the last feature added would stay selected
            lines.append(_line("edit.status"))  # what the next status_changed compares with
        return _Send(parts, lines, held, digests)

    def _read_live(self, tables: list, code: int) -> dict:
        """The linked tables of the live GeoPackage, read at one moment and let go before RustGIS is asked anything:
        ``{table: (digest, types, rows)}`` - digest None for a two-way table, rows None for a one-way table RustGIS has
        all of. :class:`RuntimeError` for a table in another system than ``code``."""
        if not gpkg.is_geopackage(self.live.path):
            return {}
        con = _read_only(self.live.path)
        try:
            con.execute("BEGIN")
            _same_system(con, tables, code)
            out = {}
            for table in tables:
                if not _has_identities(con, table):
                    continue
                digest = None if table in TWO_WAY_TABLES else _digest(con, table)
                if digest is not None and digest == self._digests.get(table):
                    out[table] = (digest, None, None)
                else:
                    out[table] = (digest, *_live_rows(con, table))
            return out
        finally:
            con.close()

    def _part(self, table: str, types: dict, rows: list, fields: dict, to, budget: _Budget,
              placeholder: str) -> tuple[_Part | None, bool]:
        """What a send does to the layer of ``table`` (None: nothing), and whether rows of it wait for a later send."""
        lid, kind = _ref(self.layers[table]), KINDS[LIVE_TABLES[table][1]]
        primary, _ = _by_key(rows)
        base = self.baseline.get(table, {})
        columns, new = {}, {}  # live column -> its field in RustGIS; the fields RustGIS's layer gets, and their types
        for column, declared in types.items():
            have = fields.get(column.lower())
            if have is not None:
                columns[column] = have[0]
            elif _field_name_ok(column):
                columns[column], new[column] = column, FIELD_TYPES.get(_affinity(declared), "text")

        def values_of(values: dict) -> dict:  # what RustGIS's layer can hold of a row, under its names
            return {columns[c]: v for c, v in values.items() if c in columns}

        touched = {key[0] for key, row in primary.items() if row.geometry is not None
                   and not self.matches_baseline(table, key, row.geometry, values_of(row.values))}
        touched |= {key[0] for key in base if key not in primary}
        rows_of: dict[str, list] = {}
        for key in sorted(primary, key=lambda k: k[1]):
            rows_of.setdefault(key[0], []).append(primary[key])
        adds = {pid: self._adds(rows_of.get(pid, []), values_of, lid, to) for pid in touched}
        unplaced = {pid for pid, lines in adds.items() if lines is None}
        if unplaced:
            log.warning("%s: %d features have vertices RustGIS cannot be given: not sent", table, len(unplaced))
        # the features drawn in RustGIS without identity that the pull brought in: replaced by their rows (which have
        # an identity now), deleted (the program deleted them), or left as they are (not read by the window yet)
        by_fid = {row.fid: row for row in rows}
        groups: dict = {}
        for drawn in self.drawn.get(table, []):
            here = [by_fid[f] for f in drawn.fids if f in by_fid]
            ids = {r.key[0] for r in here if r.key[0] is not None}
            action = "delete" if not here else "replace" if ids else "keep"
            groups.setdefault(_drawn_clause(drawn, fields), []).append((drawn, action, ids))
        waiting, held, chosen = set(unplaced), False, {}
        for clause, members in groups.items():
            if all(action == "keep" for _, action, _ in members):
                continue
            ids = set().union(*(ids for _, _, ids in members))
            back = self._put_back([d for d, action, _ in members if action == "keep"], fields, lid, to)
            if clause is None or back is None or ids & unplaced:
                log.warning("%s: %d features drawn in RustGIS cannot be found there by their values: they keep no "
                            "identity there", table, len(members))
                waiting |= ids
            elif self._found(lid, clause) != len(members):  # others there have those values (drawn since the last
                waiting |= ids  # look), or some are no longer as they were: after the next pull
                held = True
            else:
                chosen[clause] = (members, ids, back)
        while late := [c for c, (_, ids, _) in chosen.items() if ids & waiting]:  # sharing an identity that waits
            for clause in late:
                waiting |= chosen.pop(clause)[1]
            held = True
        touched -= waiting
        if not touched and not chosen:
            return None, held
        first: dict = {}
        for row in rows:
            if row.key[0] is not None:
                first.setdefault(row.key[0], row.fid)
        group_ids = set().union(*(ids for _, ids, _ in chosen.values())) if chosen else set()

        def rank(pid):
            return first.get(pid, math.inf), pid

        order = sorted(group_ids & touched, key=rank) + sorted(touched - group_ids, key=rank)
        id_name = fields.get(ID_FIELD, (ID_FIELD, ""))[0]
        geometry, lent = self._template(order, adds, base, chosen, fields, to, kind)
        opening = _line("edit.addFeature", {"layer": lid, "geometry": geometry,
                                            "attributes": {**lent, id_name: placeholder}})
        selecting = _line("select.byAttributes", {"layer": lid, "where": "", "mode": "add"})
        heads = [opening, selecting, _line("edit.delete")]
        heads += [_line("select.byAttributes", {"layer": lid, "where": c, "mode": "add"}) for c in chosen]
        heads += [_line("edit.addField", {"layer": lid, "name": c, "type": t}) for c, t in new.items()]
        prefix = len(f"{_quoted(ID_FIELD)} IN (") + 1
        size = prefix + len(_literal(placeholder)) + 2
        head = (len(heads), sum(len(line) + 1 for line in heads) + size)
        if not budget.take(*head):
            return None, True
        taken: list[str] = []

        def take(pids: list, more: list = ()) -> bool:  # the identities' rows, and their names in the clauses
            nonlocal size
            lines, chars, now = len(more), sum(len(line) + 1 for line in more), size
            for pid in pids:
                named = len(_literal(pid)) + 2
                if now + named > WHERE_CHARS:  # one more select line
                    lines, chars, now = lines + 1, chars + len(selecting) + 1, prefix
                now += named
                lines += len(adds[pid])
                chars += named + sum(len(line) + 1 for *_, line in adds[pid])
            if not budget.take(lines, chars):
                return False
            size = now
            taken.extend(pids)
            return True

        back = [line for _, _, lines in chosen.values() for line in lines]
        if chosen and not take([pid for pid in order if pid in group_ids], back):
            chosen, back, held = {}, [], True
        for pid in order:
            if pid not in group_ids and not take([pid]):
                held = True
                break
        if not taken and not chosen:
            budget.give(*head)
            return None, held
        sent = [(row, values) for pid in taken for row, values, *_ in adds[pid]]
        used = {name for _, values in sent for name in values} | set(lent)
        clauses = _identity_clauses([placeholder, *taken]) + list(chosen)
        lines = [_line("edit.addField", {"layer": lid, "name": c, "type": t}) for c, t in new.items() if c in used]
        lines.append(opening)
        lines += [_line("select.byAttributes", {"layer": lid, "where": c, "mode": "add" if n else "new"})
                  for n, c in enumerate(clauses)]
        lines.append(_line("edit.delete"))
        lines += [line for pid in taken for *_, line in adds[pid]] + back
        keys, gone = {row.key for row, _ in sent}, set(taken)
        done = [d for members, _, _ in chosen.values() for d, action, _ in members if action != "keep"]
        deleted = sum(1 for members, _, _ in chosen.values() for _, action, _ in members if action == "delete")
        count = len(sent) + sum(1 for key in base if key[0] in gone and key not in keys) + deleted
        return _Part(table, lines, taken, [(row.key, row.geometry, values) for row, values in sent], done, count,
                     bool(sent or back)), held

    @staticmethod
    def _adds(rows: list, values_of, lid, to) -> list | None:
        """The rows of an identity as features to add: ``[(row, values, geojson, line)]`` (a row without geometry left
        out: there is nothing RustGIS could draw). None when a vertex has no longitude and latitude."""
        out = []
        for row in rows:
            if row.geometry is None:
                continue
            try:
                geojson = _lonlat(row.geometry, to)
            except ValueError:
                return None
            values = {k: v for k, v in values_of(row.values).items() if v is not None and not isinstance(v, bytes)}
            out.append((row, values, geojson,
                        _line("edit.addFeature", {"layer": lid, "geometry": geojson, "attributes": values})))
        return out

    @staticmethod
    def _put_back(drawn: list, fields: dict, lid, to) -> list | None:
        """Lines that add again, as RustGIS had them at the last look, features drawn there that a where clause takes
        with the ones a send replaces. None when one cannot be."""
        from shapely.geometry import MultiPolygon

        out = []
        for feature in drawn:
            if not feature.parts or any(p is None for p in feature.parts):
                return None
            geometry = feature.parts[0] if len(feature.parts) == 1 else MultiPolygon(feature.parts)
            try:
                geojson = _lonlat(geometry, to)
            except ValueError:
                return None
            values = {fields[str(k).lower()][0]: v for k, v in feature.values.items()
                      if v is not None and str(k).lower() in fields and not isinstance(v, bytes)}
            out.append(_line("edit.addFeature", {"layer": lid, "geometry": geojson, "attributes": values}))
        return out

    @staticmethod
    def _template(order: list, adds: dict, base: dict, chosen: dict, fields: dict, to, kind: str) -> tuple:
        """The geometry and values of the feature a part adds first, so that its selection is never empty: those of a
        feature it adds - RustGIS's rules of the layer (constraints, domains) take it as they take that one -, else of
        one it deletes as RustGIS had it, else a speck far from any drawing."""
        for pid in order:
            if adds.get(pid):
                _, values, geojson, _ = adds[pid][0]
                return geojson, values
        touched = set(order)
        candidates = (had for key, had in base.items() if key[0] in touched)
        drawn = ((d.parts[0], d.values) for members, _, _ in chosen.values() for d, _, _ in members if d.parts)
        for geometry, values in itertools.chain(candidates, drawn):
            try:
                geojson = _lonlat(geometry, to)
            except (ValueError, TypeError, AttributeError):
                continue
            return geojson, {fields[str(k).lower()][0]: v for k, v in values.items()
                             if v is not None and str(k).lower() in fields and not isinstance(v, bytes)}
        return _NOWHERE[kind], {}

    def _found(self, layer, clause: str) -> int:
        """How many features of ``layer`` RustGIS finds by ``clause``; -1 when it does not say."""
        try:
            answer = self.transport.call("read_features", {"layer": layer, "where": clause, "limit": 1,
                                                           "fields": [ID_FIELD]})
        except RustGisRefused:
            return -1
        matched = answer.get("matched")
        return matched if isinstance(matched, int) and not isinstance(matched, bool) else -1

    def _delivered(self, send: _Send, answer) -> None:
        """What RustGIS has once a send ran: the rows it was given (the baseline of every identity sent is those),
        the features drawn there that the send replaced or deleted forgotten, and its edit status as it ended."""
        for part in send.parts:
            base = self.baseline.setdefault(part.table, {})
            sent = set(part.identities)
            for key in [k for k in base if k[0] in sent]:
                del base[key]
            for key, geometry, values in part.rows:
                base[key] = normalize(geometry, values)
            if part.drawn:
                self.drawn[part.table] = [d for d in self.drawn.get(part.table, [])
                                          if not any(d is x for x in part.drawn)]
        result = answer.get("result") if isinstance(answer, dict) else None
        if isinstance(result, dict):
            self._status = _status_key(result)


__all__ = ["find_rustgis", "open_app", "settings_dir", "token_dir", "endpoint", "too_old", "HttpTransport",
           "StdioTransport", "RustGisInstall", "Endpoint", "RustGisError", "RustGisOff", "RustGisBusy",
           "RustGisRefused", "RustGisTooOld", "RustGisLink", "Drawn", "normalize", "MIN_VERSION", "HTTP_PORT",
           "TOKEN_FILE", "ENDPOINT_FILE", "RASTER_SAMPLES", "TOLERANCE_M", "BUSY_CODES", "SCRATCH_DIR", "SCRIPT_LINES",
           "SCRIPT_CHARS", "WHERE_CHARS"]
