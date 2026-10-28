"""Link with RustGIS (https://github.com/opengeos/RustGIS), the desktop GIS: finding it, reading where it listens and
the token it made, and talking to it.

While the user ticks *Let your Agents Control RustGIS* in its AI Assistant settings, RustGIS serves the tools of its
MCP server (``list_layers``, ``run_command``, ``run``, ``read_features``...) to the programs of this computer over HTTP:
``POST http://127.0.0.1:8768/mcp``, one JSON-RPC message per request, a JSON reply, and ``Authorization: Bearer
<token>``, the token being the one RustGIS keeps in ``mcp-http.token`` of its settings folder. A request that carries
an ``Origin`` header or names another host is refused before the token is read. That box is RustGIS's own consent: the
program never writes RustGIS's settings, and the token is read from its file at each request, never kept, logged or
shown (an exception does not carry it either).

* :func:`find_rustgis` finds the app and ``rustgis-cli``; :func:`open_app` starts the app.
* :func:`endpoint` says where RustGIS listens (None while no token was made: agent control never ticked).
* :class:`HttpTransport` is the channel to the app, :class:`StdioTransport` the same tools over the pipes of
  ``rustgis-cli mcp``, the engine without the window that the tests run against.

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

log = logging.getLogger(__name__)

MIN_VERSION = (0, 4, 0)  # the command ids and parameters the link uses are those of RustGIS 0.4.0
MIN_TEXT = ".".join(str(n) for n in MIN_VERSION)
HTTP_PORT = 8768  # of RustGIS's MCP over HTTP, unless RUSTGIS_MCP_HTTP_PORT names another
HTTP_PORT_ENV = "RUSTGIS_MCP_HTTP_PORT"
CONFIG_ENV = "RUSTGIS_CONFIG_DIR"  # RustGIS's own variable for its settings folder
TOKEN_FILE = "mcp-http.token"  # in the settings folder: made by RustGIS once agent control is ticked
CLI_ENV = "PPG_RUSTGIS_CLI"  # the rustgis-cli to use (the tests of the real engine), else the one on the PATH
RELEASES_URL = "https://github.com/opengeos/RustGIS/releases"
RASTER_SAMPLES = 120_000_000  # width x height x bands RustGIS loads whole; a larger raster is read in place, by range
TOLERANCE_M = 0.001  # a geometry that comes back from RustGIS within 1 mm of the program's is unchanged
BUSY_CODES = (-32004, -32001)  # JSON-RPC errors of RustGIS: it does not draw (minimized, hidden) or is busy: try later
PROTOCOL = "2025-03-26"  # of MCP, as the messages of the reference clients name it
APPLICATIONS = Path("/Applications")  # macOS: where RustGIS.app goes for all users (~/Applications is the other place)


class RustGisError(RuntimeError):
    """A call to RustGIS that did not give its answer; the subclass says why."""


class RustGisOff(RustGisError):
    """RustGIS cannot be reached: no token (agent control never ticked), nothing listens (RustGIS closed, or the box
    unticked), or the token is not the running RustGIS's."""


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
    url: str  # http://127.0.0.1:<port>/mcp
    token_file: Path  # the token is read from it at each request


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


def http_port() -> int:
    """The port of RustGIS's MCP over HTTP: ``RUSTGIS_MCP_HTTP_PORT`` when it holds a port number (1 to 65535), else
    :data:`HTTP_PORT`."""
    text = (os.environ.get(HTTP_PORT_ENV) or "").strip()
    if re.fullmatch(r"\+?[0-9]+", text) and 0 < int(text) < 65536:
        return int(text)
    return HTTP_PORT


def endpoint() -> Endpoint | None:
    """Where RustGIS's MCP over HTTP listens, once RustGIS made its token (agent control ticked): None without the
    token file. Nothing is asked of the network; whether RustGIS is running is for the first request to find out."""
    folder = settings_dir()
    if folder is None or not (folder / TOKEN_FILE).is_file():
        return None
    return Endpoint(f"http://127.0.0.1:{http_port()}/mcp", folder / TOKEN_FILE)


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
                           "dialog open) or is slow, and the request may still run when it is back", outcome_unknown=True)

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
            raise RustGisError(f"the answer of {self.endpoint.url} is not JSON: not RustGIS that answers there") from None
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


__all__ = ["find_rustgis", "open_app", "settings_dir", "http_port", "endpoint", "too_old", "HttpTransport",
           "StdioTransport", "RustGisInstall", "Endpoint", "RustGisError", "RustGisOff", "RustGisBusy",
           "RustGisRefused", "RustGisTooOld", "MIN_VERSION", "HTTP_PORT", "TOKEN_FILE", "RASTER_SAMPLES", "TOLERANCE_M",
           "BUSY_CODES"]
