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

What goes wrong has a name a caller can wait for: :class:`RustGisOff` (nobody listens: agent control is off, or RustGIS
is closed), :class:`RustGisBusy` (try later: RustGIS does not draw, minimized or hidden, a dialog is open, too many
requests), :class:`RustGisRefused` (RustGIS said no), :class:`RustGisTooOld`. Over HTTP an app that does not draw says
nothing for a minute and then cancels the request (503); a transport gives up at its ``timeout`` first, and the request
is still in RustGIS's queue, so it may run when the window is back: ``RustGisBusy.outcome_unknown`` says so, and only
what is harmless to repeat (a read) should be given a short timeout.
"""
from __future__ import annotations

import http.client
import itertools
import json
import logging
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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .. import __version__
from ..updates import bundle_version, version_key
from . import gpkg
from .qgis_link import (ID_FIELD, PART_FIELD, TECHNICAL_FIELDS, TWO_WAY_TABLES, LiveLink, _affinity, _bind, _bounds,
                        _sql_type, new_id, row_signature)

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


def signature(geometry, values: dict) -> str:
    """The signature of a row as RustGIS has it (:attr:`RustGisLink.baseline`): the geometry and values of
    :func:`normalize`, the coordinates rounded to :data:`TOLERANCE_M`, the technical columns and the empty values left
    out (:func:`.qgis_link.row_signature`). A coordinate on a rounding boundary may round the other way after RustGIS's
    round trip: :meth:`RustGisLink.matches_baseline` compares within the tolerance, never by the signature alone."""
    import numpy as np
    import shapely

    geometry, values = normalize(geometry, values)
    raw = b""
    if geometry is not None:
        rounded = shapely.transform(geometry, lambda xy: np.rint(xy / TOLERANCE_M) * TOLERANCE_M + 0.0)  # no -0.0
        raw = shapely.to_wkb(rounded, byte_order=1, output_dimension=2)
    return row_signature(raw, values)


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
    :data:`TOLERANCE_M` (an untouched feature comes back from RustGIS moved by about a nanometre)."""
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


def _rustgis_rows(features: list, parted: bool) -> tuple[dict, list]:
    """RustGIS's features as rows of the live table: those with an identity by ``(identity, part)`` - each polygon of a
    feature of several its own row, the parts after the first numbered after the largest part its identity has -, and
    the features without one (drawn there, or copies of a feature: every attribute is copied) as ``(parts, values)``.
    The values are of :func:`normalize`."""
    rows = [normalize(g, v) for g, v in features]
    last: dict[str, int] = {}
    for _, values in rows:
        pid = _identity(values)
        if pid is not None:
            last[pid] = max(last.get(pid, 0), _part_of(values, parted))
    keyed, drawn = {}, []
    for geometry, values in rows:
        pid, parts = _identity(values), _parts(geometry, parted)
        first = (pid, _part_of(values, parted))
        if pid is None or first in keyed:
            drawn.append((parts, values))
            continue
        keyed[first] = (parts[0], values)
        for extra in parts[1:]:
            last[pid] += 1
            keyed[(pid, last[pid])] = (extra, values)
    return keyed, drawn


class RustGisLink:
    """The drawings of the active chunk in RustGIS and back: which RustGIS layer holds which table of the live
    GeoPackage of the QGIS link (:attr:`layers`), what RustGIS last had of each row (:attr:`baseline`, :attr:`drawn`),
    and the edits made there brought into that GeoPackage as another program's (:meth:`pull`), where
    :meth:`.qgis_link.LiveLink.read_changes` finds them as it finds QGIS's and the window applies them the same way.

    What RustGIS had is what tells an edit made there from one the program has not sent yet: a row RustGIS still has
    as it had it is never written over (the program's version goes to RustGIS), one it changed comes column by column,
    one it no longer has is deleted. It is taken from the live GeoPackage when RustGIS is given the tables
    (:meth:`remember`) and from each export after (:meth:`pull`); a pull before :meth:`remember` is refused."""

    def __init__(self, live: LiveLink, transport, chunk_label: str, crs: str):
        self.live = live
        self.transport = transport  # HttpTransport (the app) or StdioTransport (the engine): call(tool, args)
        self.chunk_label = chunk_label
        self.crs = crs
        self.layers: dict[str, str] = {}  # table of the live GeoPackage -> the id of RustGIS's layer that holds it
        # per table, (ppg_id, ppg_parte) -> the signature of the row as RustGIS last had it (geometry rounded to
        # TOLERANCE_M + normalized values); _had holds the rows themselves, compared within the tolerance
        self.baseline: dict[str, dict[tuple, str]] = {}
        self._had: dict[str, dict[tuple, tuple]] = {}
        self.drawn: dict[str, list[Drawn]] = {}  # per table, what RustGIS has without an identity (see Drawn)

    @property
    def scratch(self) -> Path:
        """The folder beside the live GeoPackage where RustGIS exports the layers for a pull (a file a table,
        replaced at each pull)."""
        return self.live.path.parent / SCRATCH_DIR

    def _keep(self, table: str, had: dict) -> None:
        self._had[table] = had
        self.baseline[table] = {key: signature(g, v) for key, (g, v) in had.items()}

    def remember(self, tables=None) -> None:
        """RustGIS holds the rows of the live GeoPackage as they are now: the link has just given it ``tables`` (every
        linked two-way table when None) - when it starts, or when a layer is added again. Their baseline is taken from
        there; the rows without an identity (drawn in QGIS and not read yet) are features RustGIS has without one.
        Rows without a geometry are left out: what RustGIS may not show is never taken for deleted there."""
        tables = [t for t in (self.layers if tables is None else tables) if t in TWO_WAY_TABLES]
        found = {}
        if gpkg.is_geopackage(self.live.path):
            con = _read_only(self.live.path)
            try:
                found = {t: _live_rows(con, t) for t in tables if _has_identities(con, t)}
            finally:
                con.close()
        for table in tables:
            types, rows = found.get(table, ({}, []))
            parted = PART_FIELD in types
            primary, others = _by_key(rows)
            self._keep(table, {k: (r.geometry, r.values) for k, r in primary.items() if r.geometry is not None})
            self.drawn[table] = [Drawn(_parts(r.geometry, parted), r.values, [r.fid])
                                 for r in others if r.geometry is not None]

    def matches_baseline(self, table: str, key: tuple, geometry, values: dict) -> bool:
        """Whether RustGIS last had row ``key`` (``(ppg_id, ppg_parte)``) of ``table`` as it is given: the geometry
        within :data:`TOLERANCE_M` vertex by vertex and the same values (technical and empty ones left out)."""
        had = self._had.get(table, {}).get(tuple(key))
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
        unknown = [t for t in tables if t not in self._had]
        if unknown:
            raise RuntimeError(f"what RustGIS was given of {', '.join(unknown)} is not known: remember() the tables "
                               "when RustGIS gets them")
        counts = {t: (0, 0, 0) for t in tables}
        if not tables or not gpkg.is_geopackage(self.live.path):
            return counts
        con = _read_only(self.live.path)
        try:
            ready = [t for t in tables if _has_identities(con, t)]  # written by this version
        finally:
            con.close()
        exports = {t: self._export(t) for t in ready}  # RustGIS first: never asked while the file is held
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
            self._keep(table, had)
            self.drawn[table] = drawn
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
                                                     "params": {"layer": self.layers[table], "path": str(path)}})
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
        keyed, drawn = _rustgis_rows(export.features, parted)
        had, now = self._had.get(table, {}), {}
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


__all__ = ["find_rustgis", "open_app", "settings_dir", "token_dir", "endpoint", "too_old", "HttpTransport",
           "StdioTransport", "RustGisInstall", "Endpoint", "RustGisError", "RustGisOff", "RustGisBusy",
           "RustGisRefused", "RustGisTooOld", "RustGisLink", "Drawn", "normalize", "signature", "MIN_VERSION",
           "HTTP_PORT", "TOKEN_FILE", "ENDPOINT_FILE", "RASTER_SAMPLES", "TOLERANCE_M", "BUSY_CODES", "SCRATCH_DIR"]
