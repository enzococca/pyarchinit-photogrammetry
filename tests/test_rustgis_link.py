"""Finding RustGIS and talking to it (io/rustgis_link): where it publishes its address (the endpoint.json of a running
RustGIS) and its token, MCP over HTTP against a fake RustGIS (an http.server on this computer that checks every request
the way the real listener does), the same tools over the pipes of an engine, how each failure is named, and the line of
the diagnosis; then the edits made in RustGIS brought into the live GeoPackage of the QGIS link (a pull), against a fake
RustGIS that exports what the test prepared. The real engine runs only when PPG_RUSTGIS_CLI names a rustgis-cli; the
open app has its own opt-in test (test_rustgis_app.py)."""
from __future__ import annotations

import copy
import json
import logging
import os
import plistlib
import shutil
import socket
import socketserver
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import shapely
from shapely import affinity
from shapely.geometry import MultiLineString, MultiPolygon, Point, Polygon, box

from pyarchinit_photogrammetry import doctor
from pyarchinit_photogrammetry.io import gpkg
from pyarchinit_photogrammetry.io import rustgis_link as link
from pyarchinit_photogrammetry.io.qgis_link import ID_FIELD, PART_FIELD, LiveLink, new_id

TOKEN = "Zq7Kx9Lm2PwR4sT8vB1nC5dF3gH6jY0a"  # 32 letters and digits, as RustGIS makes its token


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):  # no reverse lookup of the address: the tests never ask a name server
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    def handle_error(self, request, client_address):  # a client that gave up (the timeout test) is no error
        pass


class FakeRustGis:
    """RustGIS's MCP over HTTP as far as the link goes. Every request is checked in the order the real listener checks
    it (the host, no Origin, the Bearer token, the path, the content type); a request that breaks a rule of the client
    is noted in ``refused`` (a wrong token is the test's doing, not noted). It answers from ``tools`` (a tool -> the
    dict its text carries) unless ``replies`` has the tool: a JSON-RPC body (``{"result": ...}`` or ``{"error": ...}``),
    a function of the recorded request that gives one, or ``(status, text)`` for an HTTP status of its own. ``delays``
    holds a tool back for some seconds."""

    def __init__(self, token: str = TOKEN, version: str = "0.4.0"):
        self.token, self.version = token, version
        self.tools: dict[str, dict] = {}
        self.replies: dict[str, object] = {}
        self.delays: dict[str, float] = {}
        self.requests: list[dict] = []  # path, headers (lower case) and message of each request that got through
        self.refused: list[str] = []
        self.folder: Path | None = None  # the settings folder the fixture makes for it, with the token in it
        self.runtime: Path | None = None  # the run folder (XDG_RUNTIME_DIR) where it publishes its endpoint.json
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                fake._serve(self)

            def log_message(self, *args):
                pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _serve(self, handler) -> None:
        headers = {k.lower(): v for k, v in handler.headers.items()}
        raw = handler.rfile.read(int(headers.get("content-length") or 0))
        status, text = self._answer(handler.path, headers, raw)
        data = text.encode("utf-8")
        handler.send_response(status)
        if data:
            handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(data)

    def _answer(self, path: str, headers: dict, raw: bytes) -> tuple[int, str]:
        if headers.get("host") != f"127.0.0.1:{self.port}" or "origin" in headers:
            self.refused.append("a host that is not this computer's, or an Origin")
            return 403, json.dumps({"error": "the Host header must name this computer's loopback address and port"})
        if headers.get("authorization") != f"Bearer {self.token}":
            return 401, json.dumps({"error": "a valid bearer token is required"})
        if path != "/mcp":
            self.refused.append(f"the path {path}")
            return 404, json.dumps({"error": "post MCP messages to /mcp"})
        if headers.get("content-type") != "application/json":
            self.refused.append("a content type other than application/json")
            return 415, json.dumps({"error": "send application/json"})
        message = json.loads(raw)
        request = {"path": path, "headers": headers, "message": message}
        self.requests.append(request)
        if "id" not in message:  # a notification: accepted, no body
            return 202, ""
        method, params = message["method"], message.get("params") or {}
        if method == "initialize":
            info = {"name": "rustgis", "title": "RustGIS", "version": self.version}
            reply = self.replies.get("initialize") or {"result": {"serverInfo": info}}
        elif method == "tools/call":
            tool = params["name"]
            time.sleep(self.delays.get(tool, 0))
            reply = self.replies.get(tool)
            if reply is None:
                text = json.dumps(self.tools.get(tool, {}))
                reply = {"result": {"content": [{"type": "text", "text": text}], "isError": False}}
        else:
            reply = {"error": {"code": -32601, "message": f"unknown method '{method}'"}}
        if callable(reply):
            reply = reply(request)
        if isinstance(reply, tuple):
            return reply
        return 200, json.dumps({"jsonrpc": "2.0", "id": message["id"], **reply})


def publish_endpoint(runtime: Path, port: int, token_file: Path, **changes) -> Path:
    """The endpoint.json a running RustGIS publishes (the fields ``relay.rs`` writes) in ``rustgis`` of the run folder
    that XDG_RUNTIME_DIR names, for this process, which runs. ``changes`` replace fields; ``...`` takes one out."""
    info = {"schema": 1, "host": "127.0.0.1", "port": 8767, "url": "tcp://127.0.0.1:8767", "pid": os.getpid(),
            "started_at": "2026-10-28T12:00:00Z", "version": "0.4.0",
            "token_file": str(runtime / "rustgis" / "8767.token"), "attach": "rustgis-cli mcp --attach 8767",
            "http_url": f"http://127.0.0.1:{port}/mcp", "http_token_file": str(token_file)}
    info.update(changes)
    folder = runtime / "rustgis"
    folder.mkdir(exist_ok=True)
    (folder / "endpoint.json").write_text(json.dumps({k: v for k, v in info.items() if v is not ...}), encoding="utf-8")
    return folder / "endpoint.json"


def dead_pid() -> int:
    """The pid of a process that has run and ended."""
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True,
                          close_fds=False, check=True)
    return int(done.stdout)


@pytest.fixture
def fake_rustgis(tmp_path, monkeypatch):
    """A fake RustGIS on a free port that publishes itself as the real one does: an endpoint.json in the run folder
    XDG_RUNTIME_DIR names, for this process (which runs), whose http_token_file is the token file in its settings
    folder (``fake.folder``). The program finds it only so."""
    fake = FakeRustGis()
    fake.folder = tmp_path / "RustGIS"
    fake.folder.mkdir()
    (fake.folder / link.TOKEN_FILE).write_text(TOKEN + "\n", encoding="utf-8")
    fake.runtime = tmp_path / "run"
    fake.runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(fake.runtime))
    publish_endpoint(fake.runtime, fake.port, fake.folder / link.TOKEN_FILE)
    yield fake
    fake.close()


def test_the_constants_of_the_link():
    assert link.MIN_VERSION == (0, 4, 0) and link.HTTP_PORT == 8768 and link.TOKEN_FILE == "mcp-http.token"
    assert link.RASTER_SAMPLES == 120_000_000 and link.TOLERANCE_M == 0.001 and link.BUSY_CODES == (-32004, -32001)
    for kind in (link.RustGisOff, link.RustGisBusy, link.RustGisRefused, link.RustGisTooOld):
        assert issubclass(kind, link.RustGisError)
    assert issubclass(link.RustGisError, RuntimeError)


def test_settings_folder_and_token_file(tmp_path, monkeypatch):
    """RustGIS makes its token in the settings folder once agent control is ticked and keeps it for good; where it
    listens is what a RUNNING RustGIS publishes in endpoint.json, and only that."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("RUSTGIS_CONFIG_DIR", str(tmp_path))
    assert link.settings_dir() == tmp_path
    assert link.endpoint() is None  # nothing published: RustGIS is not running, or its agent control is off
    (tmp_path / "mcp-http.token").write_text(TOKEN + "\n")
    assert link.endpoint() is None  # the token that stays proves nothing about who listens at 127.0.0.1:8768
    published = publish_endpoint(runtime, 9123, tmp_path / link.TOKEN_FILE)  # for this process, which runs
    assert published == link.token_dir() / "endpoint.json"
    assert link.endpoint() == link.Endpoint("http://127.0.0.1:9123/mcp", tmp_path / "mcp-http.token")
    publish_endpoint(runtime, 9123, tmp_path / link.TOKEN_FILE, pid=dead_pid())  # RustGIS died and left its file
    assert link.endpoint() is None
    published.unlink()  # RustGIS quit
    assert link.endpoint() is None


def test_there_is_no_default_address(fake_rustgis, monkeypatch):
    """A token and a listener (here on the port RUSTGIS_MCP_HTTP_PORT names) are not an address: without the
    endpoint.json of a running RustGIS the program does not know who listens, and the token stays where it is."""
    fake = fake_rustgis
    (fake.runtime / "rustgis" / "endpoint.json").unlink()
    monkeypatch.setenv("RUSTGIS_CONFIG_DIR", str(fake.folder))
    monkeypatch.setenv("RUSTGIS_MCP_HTTP_PORT", str(fake.port))
    assert (fake.folder / link.TOKEN_FILE).is_file()
    assert link.endpoint() is None
    assert not hasattr(link, "http_port") and fake.requests == []


def test_endpoint_json_is_found_in_the_temporary_folder_without_a_run_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("USER", "enzo")
    (tmp_path / "tmp").mkdir()
    publish_endpoint(tmp_path / "tmp", 9123, tmp_path / link.TOKEN_FILE)  # makes tmp/rustgis ...
    assert link.endpoint() is None  # ... which is not the folder RustGIS uses here
    (tmp_path / "tmp" / "rustgis").rename(tmp_path / "tmp" / "rustgis-mcp-enzo")
    assert link.endpoint().url == "http://127.0.0.1:9123/mcp"


@pytest.mark.parametrize("changes", [
    pytest.param({"http_url": None}, id="the http listener is off"),
    pytest.param({"http_url": None, "http_token_file": None}, id="the relay only"),
    pytest.param({"http_url": ...}, id="no http_url"),
    pytest.param({"http_token_file": ...}, id="no http_token_file"),
    pytest.param({"pid": ...}, id="no pid"),
    pytest.param({"http_url": ""}, id="empty http_url"),
    pytest.param({"http_token_file": ""}, id="empty http_token_file"),
    pytest.param({"http_url": 8768}, id="http_url not a string"),
    pytest.param({"http_token_file": ["x"]}, id="http_token_file not a string"),
    pytest.param({"pid": None}, id="pid null"),
    pytest.param({"pid": 0}, id="pid 0"),
    pytest.param({"pid": -1}, id="pid -1"),
    pytest.param({"pid": True}, id="pid true"),
    pytest.param({"pid": "1234"}, id="pid a string"),
    pytest.param({"pid": 12.5}, id="pid a float"),
    pytest.param({"pid": 2 ** 70}, id="pid too large"),
])
def test_an_endpoint_file_that_is_not_a_running_rustgis_with_http_gives_nothing(fake_rustgis, monkeypatch, changes):
    real_kill = os.kill

    def kill(pid, sig):  # a pid of 0 or less would be a group of processes, not a process
        assert pid > 0
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", kill)
    assert link.endpoint() is not None
    publish_endpoint(fake_rustgis.runtime, fake_rustgis.port, fake_rustgis.folder / link.TOKEN_FILE, **changes)
    assert link.endpoint() is None


@pytest.mark.parametrize("content", [b"", b"not json", b"[1, 2]", b'"text"', b"null", b"{", b'{"pid": 1',
                                     b"\xff\xfe\x00"])
def test_a_file_that_is_not_what_rustgis_writes_gives_nothing(fake_rustgis, content):
    published = fake_rustgis.runtime / "rustgis" / "endpoint.json"
    published.write_bytes(content)
    assert link.endpoint() is None
    published.unlink()
    published.mkdir()  # a folder in its place
    assert link.endpoint() is None


def test_the_run_folder_follows_rustgis(tmp_path, monkeypatch):
    """RustGIS's token_dir: ``rustgis`` in XDG_RUNTIME_DIR when that is an absolute folder, else
    ``rustgis-mcp-<user>`` in the temporary folder; the user is $USER (an empty one included) or, when there is none,
    $USERNAME, kept to its letters, digits, - and _, and ``user`` when nothing is left."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "a file").write_text("x")
    runtime = tmp_path / "run"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    assert link.token_dir() == runtime / "rustgis"  # the folder in it need not exist yet
    monkeypatch.setenv("USER", "enzo")
    for unusable in ("", "run/relative", str(tmp_path / "missing"), str(tmp_path / "a file")):
        monkeypatch.setenv("XDG_RUNTIME_DIR", unusable)
        assert link.token_dir() == tmp_path / "tmp" / "rustgis-mcp-enzo", unusable
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert link.token_dir() == tmp_path / "tmp" / "rustgis-mcp-enzo"
    for user, folder in (("Enzo.Cocca-2_x \u00e9/..", "EnzoCocca-2_x"), ("///", "user"), ("", "user")):
        monkeypatch.setenv("USER", user)
        monkeypatch.setenv("USERNAME", "ignored")  # only when there is no USER at all
        assert link.token_dir() == tmp_path / "tmp" / f"rustgis-mcp-{folder}", user
    monkeypatch.delenv("USER")
    assert link.token_dir() == tmp_path / "tmp" / "rustgis-mcp-ignored"
    monkeypatch.delenv("USERNAME")
    assert link.token_dir() == tmp_path / "tmp" / "rustgis-mcp-user"


def test_a_process_that_runs_is_asked_with_signal_zero(fake_rustgis, monkeypatch):
    """os.kill(pid, 0) only asks: no such process is a RustGIS that is gone, 'not allowed' a process of another user
    that is there (running); anything else is not a running RustGIS."""
    monkeypatch.setattr(link, "_platform", lambda: "linux")
    asked = []
    for error, runs in ((None, True), (PermissionError(1, "not permitted"), True),
                        (ProcessLookupError(3, "no such process"), False), (OverflowError("too large"), False),
                        (OSError(22, "invalid"), False)):
        def kill(pid, sig, error=error):
            asked.append((pid, sig))
            if error is not None:
                raise error

        monkeypatch.setattr(os, "kill", kill)
        assert (link.endpoint() is not None) is runs, error
    assert set(asked) == {(os.getpid(), 0)}


def test_on_windows_a_process_is_asked_through_openprocess_never_signalled(fake_rustgis, monkeypatch):
    """os.kill terminates a process on Windows (any signal but Ctrl-C is TerminateProcess): never there."""
    monkeypatch.setattr(link, "_platform", lambda: "win32")
    monkeypatch.setattr(os, "kill", lambda pid, sig: pytest.fail("os.kill on Windows terminates the process"))
    asked = []
    monkeypatch.setattr(link, "_running_on_windows", lambda pid: asked.append(pid) or True)
    assert link.endpoint() is not None and asked == [os.getpid()]
    monkeypatch.setattr(link, "_running_on_windows", lambda pid: False)
    assert link.endpoint() is None


class FakeKernel32:
    """The calls of kernel32 the check makes, answering as the test says."""

    def __init__(self):
        self.handle, self.wait, self.opened, self.closed = 1234, 0x102, [], []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return self.handle

    def WaitForSingleObject(self, handle, milliseconds):
        assert (handle, milliseconds) == (1234, 0)  # asks, never waits
        return self.wait

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


def test_the_windows_check_of_a_process(monkeypatch):
    import ctypes

    kernel = FakeKernel32()
    monkeypatch.setattr(link, "_kernel32", lambda: kernel)
    assert link._running_on_windows(4321) is True  # the wait times out: it runs
    # query limited information + synchronize, not inherited; the handle is given back
    assert kernel.opened == [(0x1000 | 0x100000, False, 4321)] and kernel.closed == [1234]
    kernel.wait = 0  # signalled: it has exited
    assert link._running_on_windows(4321) is False
    assert kernel.closed == [1234, 1234]  # the handle is always given back
    kernel.handle = None  # it cannot be opened
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5, raising=False)  # access denied: there, not ours to open
    assert link._running_on_windows(4321) is True
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 87, raising=False)  # invalid parameter: no such process
    assert link._running_on_windows(4321) is False


def test_settings_folder_by_system(tmp_path, monkeypatch):
    """The rules of RustGIS's own folders: the override as it is, else the system's place; an empty variable is no
    variable, and a system that does not say where its folders are gives none."""
    monkeypatch.delenv("RUSTGIS_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(link, "_platform", lambda: "darwin")
    assert link.settings_dir() == tmp_path / "home" / "Library" / "Application Support" / "RustGIS"
    monkeypatch.setattr(link, "_platform", lambda: "win32")
    assert link.settings_dir() == tmp_path / "roaming" / "RustGIS"
    monkeypatch.setattr(link, "_platform", lambda: "linux")
    assert link.settings_dir() == tmp_path / "home" / ".config" / "rustgis"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert link.settings_dir() == tmp_path / "xdg" / "rustgis"
    monkeypatch.setenv("RUSTGIS_CONFIG_DIR", str(tmp_path / "chosen"))
    assert link.settings_dir() == tmp_path / "chosen"  # whatever the system
    monkeypatch.setenv("RUSTGIS_CONFIG_DIR", "")
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert link.settings_dir() == tmp_path / "home" / ".config" / "rustgis"
    monkeypatch.setenv("HOME", "")
    assert link.settings_dir() is None
    monkeypatch.setattr(link, "_platform", lambda: "darwin")
    assert link.settings_dir() is None
    monkeypatch.delenv("APPDATA")
    monkeypatch.setattr(link, "_platform", lambda: "win32")
    assert link.settings_dir() is None


def test_requests_carry_the_token_and_no_origin(fake_rustgis):
    fake = fake_rustgis
    layers = {"activeMap": "Map", "layers": [{"id": 1, "name": "unita", "features": 3}]}
    fake.tools["list_layers"] = layers
    (fake.folder / link.TOKEN_FILE).write_text(TOKEN + "\r\n")  # the text, stripped
    transport = link.HttpTransport(link.endpoint())
    assert transport.initialize() == "0.4.0"
    assert transport.call("list_layers", {}) == layers
    assert fake.refused == []
    assert [r["message"]["method"] for r in fake.requests] == ["initialize", "tools/call"]
    assert fake.requests[1]["message"]["params"] == {"name": "list_layers", "arguments": {}}
    assert [r["message"]["id"] for r in fake.requests] == [1, 2]
    assert all(r["message"]["jsonrpc"] == "2.0" for r in fake.requests)
    for request in fake.requests:
        headers = request["headers"]
        assert request["path"] == "/mcp"
        assert headers["authorization"] == f"Bearer {TOKEN}"
        assert headers["content-type"] == "application/json"
        assert headers["host"] == f"127.0.0.1:{fake.port}"
        assert "origin" not in headers


def test_the_token_is_read_at_each_connection(fake_rustgis):
    """RustGIS makes a new token when its file is deleted: the next request has it, nothing was kept."""
    fake = fake_rustgis
    transport = link.HttpTransport(link.endpoint())
    assert transport.call("list_layers", {}) == {}
    fake.token = "Mn3Bv6Cx9Zl2Kj5Hg8Fd1Sa4Pq7Wo0Ie"
    (fake.folder / link.TOKEN_FILE).write_text(fake.token + "\n")
    assert transport.call("list_layers", {}) == {}
    assert fake.requests[-1]["headers"]["authorization"] == f"Bearer {fake.token}"


def test_loopback_requests_never_go_through_a_proxy(fake_rustgis, monkeypatch):
    """urllib's own opener asks the proxy of the shell (http_proxy) or of the system for 127.0.0.1 too, and a computer
    behind a proxy would never reach RustGIS: the transport's opener has none."""
    for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")  # a proxy that is not there
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    with pytest.raises(OSError):  # the default opener, made now, takes the proxy
        link.urllib.request.build_opener().open(link.endpoint().url, timeout=2)
    assert link.HttpTransport(link.endpoint()).initialize() == "0.4.0"


def test_errors_are_named(fake_rustgis):
    fake = fake_rustgis
    transport = link.HttpTransport(link.endpoint())
    fake.replies.update(
        busy={"error": {"code": -32004, "message": "RustGIS isn't drawing (a dialog is open, its window hidden)"}},
        queue={"error": {"code": -32001, "message": "the app is busy; try again"}},
        no={"result": {"isError": True, "content": [
            {"type": "text", "text": "unknown command 'no.such'; find ids with search_commands"}]}},
        words={"result": {"content": [{"type": "text", "text": "plain words"}], "isError": False}},
        nothing={"result": {"content": [], "isError": False}},
        scalar={"result": {"content": [{"type": "text", "text": "[1, 2]"}], "isError": False}},
        nope={"error": {"code": -32602, "message": "unknown tool 'nope'"}})
    with pytest.raises(link.RustGisBusy, match="isn't drawing") as busy:
        transport.call("busy", {})
    assert busy.value.outcome_unknown is False  # RustGIS said it did not run the request
    with pytest.raises(link.RustGisBusy, match="try again"):
        transport.call("queue", {})
    with pytest.raises(link.RustGisRefused) as refused:
        transport.call("no", {})
    assert str(refused.value) == "unknown command 'no.such'; find ids with search_commands"
    with pytest.raises(link.RustGisRefused, match="not JSON.*plain words"):
        transport.call("words", {})
    with pytest.raises(link.RustGisRefused, match="not JSON"):
        transport.call("nothing", {})
    with pytest.raises(link.RustGisRefused, match="not an object"):
        transport.call("scalar", {})
    with pytest.raises(link.RustGisRefused, match="unknown tool 'nope'"):
        transport.call("nope", {})
    fake.version = "0.3.0"
    with pytest.raises(link.RustGisTooOld, match=r"0\.3\.0.*0\.4\.0"):
        transport.initialize()
    fake.version = "0.10.2"  # numbers, not text: 0.10 comes after 0.4
    assert transport.initialize() == "0.10.2"
    fake.close()
    with pytest.raises(link.RustGisOff, match="127.0.0.1"):
        transport.call("list_layers", {})
    with pytest.raises(link.RustGisOff):
        transport.initialize()


def test_a_closed_port_and_a_missing_token_are_off(tmp_path):
    folder = tmp_path / "RustGIS"
    folder.mkdir()
    with socket.socket() as probe:  # a port nothing listens on
        probe.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{probe.getsockname()[1]}/mcp"
    ep = link.Endpoint(url, folder / link.TOKEN_FILE)
    with pytest.raises(link.RustGisOff, match="token"):  # no token file (deleted since endpoint() looked)
        link.HttpTransport(ep).call("list_layers", {})
    ep.token_file.write_text("\n")
    with pytest.raises(link.RustGisOff, match="no token"):
        link.HttpTransport(ep).call("list_layers", {})
    ep.token_file.write_bytes(b"caf\xc3\xa9\n")  # not the letters and digits of a token
    with pytest.raises(link.RustGisOff, match="no token"):
        link.HttpTransport(ep).call("list_layers", {})
    ep.token_file.write_text(TOKEN + "\n")
    with pytest.raises(link.RustGisOff, match=url):
        link.HttpTransport(ep).call("list_layers", {})


@pytest.mark.parametrize("status, words, kind, unknown", [
    (503, "the app is busy; try again", link.RustGisBusy, False),
    (503, "the app did not run the request in time; it was cancelled", link.RustGisBusy, False),
    (504, "the app is still running the request; its outcome is unknown", link.RustGisBusy, True),
    (404, "post MCP messages to /mcp", link.RustGisRefused, None),
    (413, "the body is too large", link.RustGisRefused, None),
])
def test_http_statuses_are_named(fake_rustgis, status, words, kind, unknown):
    """A refusal of the listener itself (an HTTP status with ``{"error": ...}``, not a JSON-RPC error): busy means try
    later (an outcome that is unknown says so), the rest is a no. RustGIS's words are the message."""
    fake_rustgis.replies["x"] = (status, json.dumps({"error": words}))
    with pytest.raises(kind) as caught:
        link.HttpTransport(link.endpoint()).call("x", {})
    assert words in str(caught.value)
    if unknown is None:
        assert f"HTTP {status}" in str(caught.value)
    else:
        assert caught.value.outcome_unknown is unknown


def test_a_status_without_words_is_named_by_its_number(fake_rustgis):
    fake_rustgis.replies["x"] = (400, "not even JSON")
    with pytest.raises(link.RustGisRefused, match="HTTP 400.*not even JSON"):
        link.HttpTransport(link.endpoint()).call("x", {})
    fake_rustgis.replies["y"] = (500, "")
    with pytest.raises(link.RustGisRefused, match="HTTP 500"):
        link.HttpTransport(link.endpoint()).call("y", {})


def test_a_token_that_is_not_the_running_ones_is_off(fake_rustgis):
    """401: the file is not the running RustGIS's (another run, another settings folder)."""
    fake_rustgis.token = "Mn3Bv6Cx9Zl2Kj5Hg8Fd1Sa4Pq7Wo0Ie"
    with pytest.raises(link.RustGisOff, match="does not accept the token"):
        link.HttpTransport(link.endpoint()).initialize()


def test_a_bad_reply_is_named(fake_rustgis):
    fake = fake_rustgis
    transport = link.HttpTransport(link.endpoint())
    fake.replies["empty"] = (200, "")
    fake.replies["page"] = (200, "<html>not RustGIS</html>")
    fake.replies["list"] = (200, "[1, 2]")
    for tool in ("empty", "page", "list"):
        with pytest.raises(link.RustGisError) as caught:
            transport.call(tool, {})
        assert type(caught.value) is link.RustGisError  # not one of the cases a caller waits for
    fake.replies["initialize"] = {"result": {"serverInfo": {"name": "someone else"}}}
    with pytest.raises(link.RustGisRefused, match="version"):
        transport.initialize()


def test_a_rustgis_that_stays_silent_is_busy_and_may_still_run_the_call(fake_rustgis):
    """Over HTTP a RustGIS that does not draw (minimized, hidden) says nothing for a minute: the transport gives up at
    its timeout. That is not a failure, but the call is still in RustGIS's queue and runs when it draws again."""
    fake = fake_rustgis
    fake.delays["slow"] = 2.0
    transport = link.HttpTransport(link.endpoint(), timeout=0.2)
    started = time.monotonic()
    with pytest.raises(link.RustGisBusy, match="did not answer") as busy:
        transport.call("slow", {})
    assert time.monotonic() - started < 1.5 and busy.value.outcome_unknown is True


def test_the_token_never_reaches_the_log(fake_rustgis, caplog):
    fake = fake_rustgis
    caplog.set_level(logging.DEBUG)
    # a RustGIS that quotes in its refusal the header of the request that offended it
    fake.replies["no"] = lambda request: {"result": {"isError": True, "content": [
        {"type": "text", "text": "refused: " + request["headers"]["authorization"]}]}}
    transport = link.HttpTransport(link.endpoint())
    caught = []
    with pytest.raises(link.RustGisRefused) as refused:
        transport.call("no", {})
    caught.append(refused.value)
    assert "refused: Bearer" in str(refused.value)
    fake.token = "Mn3Bv6Cx9Zl2Kj5Hg8Fd1Sa4Pq7Wo0Ie"  # the file's token is no longer RustGIS's: 401
    with pytest.raises(link.RustGisOff) as off:
        transport.call("list_layers", {})
    caught.append(off.value)
    fake.close()
    with pytest.raises(link.RustGisOff) as closed:
        transport.call("list_layers", {})
    caught.append(closed.value)
    for exc in caught:
        assert TOKEN not in str(exc) and TOKEN not in repr(exc)
        assert TOKEN not in "".join(traceback.format_exception(exc))
    assert caplog.records, "the transport logs what it does (at debug level)"
    for record in caplog.records:
        assert TOKEN not in record.getMessage() and TOKEN not in str(record.args) and TOKEN not in str(record.exc_info)
    assert TOKEN not in repr(vars(transport)) and TOKEN not in repr(vars(link.endpoint()))


def test_the_token_is_not_kept_in_the_transport(fake_rustgis):
    transport = link.HttpTransport(link.endpoint())
    transport.initialize()
    assert all(TOKEN not in str(value) for value in vars(transport).values())


def _bundle(path: Path, version: str) -> Path:
    (path / "Contents").mkdir(parents=True)
    (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": version,
                                                                   "CFBundleExecutable": "rustgis"}))
    return path


def _program(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_find_rustgis_on_each_system(tmp_path, monkeypatch):
    home, shared, empty = tmp_path / "home", tmp_path / "Applications", tmp_path / "empty"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(empty))  # nothing on the PATH
    monkeypatch.delenv(link.CLI_ENV, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    monkeypatch.setattr(link, "APPLICATIONS", shared)
    # macOS: /Applications, then ~/Applications; the version is the bundle's
    monkeypatch.setattr(link, "_platform", lambda: "darwin")
    assert link.find_rustgis() is None
    mine = _bundle(home / "Applications" / "RustGIS.app", "0.3.9")
    found = link.find_rustgis()
    assert (found.app, found.version, found.cli) == (mine, "0.3.9", None)
    system = _bundle(shared / "RustGIS.app", "0.4.0")
    found = link.find_rustgis()
    assert (found.app, found.version) == (system, "0.4.0")
    # Windows: the folder of the installer of one user, then the one of all users
    monkeypatch.setattr(link, "_platform", lambda: "win32")
    assert link.find_rustgis() is None
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "program files"))
    everyone = _program(tmp_path / "program files" / "RustGIS" / "RustGIS.exe")
    assert link.find_rustgis() == link.RustGisInstall(everyone, None, None)
    one_user = _program(tmp_path / "local" / "Programs" / "RustGIS" / "RustGIS.exe")
    assert link.find_rustgis().app == one_user
    # Linux: rustgis on the PATH
    monkeypatch.setattr(link, "_platform", lambda: "linux")
    assert link.find_rustgis() is None
    app = _program(tmp_path / "bin" / "rustgis")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert link.find_rustgis().app == app
    # the engine without the window: PPG_RUSTGIS_CLI first, else rustgis-cli on the PATH
    cli = _program(tmp_path / "bin" / "rustgis-cli")
    assert link.find_rustgis() == link.RustGisInstall(app, cli, None)
    named = _program(tmp_path / "chosen" / "rustgis-cli")
    monkeypatch.setenv(link.CLI_ENV, str(named))
    assert link.find_rustgis().cli == named
    monkeypatch.setenv(link.CLI_ENV, str(tmp_path / "not there"))  # a path that is no file: the PATH is looked in
    assert link.find_rustgis().cli == cli
    # the engine alone (a computer that runs the tests, no app)
    monkeypatch.setattr(link, "_platform", lambda: "darwin")
    monkeypatch.setattr(link, "APPLICATIONS", empty)
    monkeypatch.setenv("HOME", str(empty))
    found = link.find_rustgis()
    assert (found.app, found.cli, found.version) == (None, cli, None)


def test_open_app_starts_the_app(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(link.subprocess, "Popen", lambda command, **options: started.append((command, options)))
    app = tmp_path / "RustGIS.app"
    monkeypatch.setattr(link, "find_rustgis", lambda: link.RustGisInstall(app, None, "0.4.0"))
    monkeypatch.setattr(link, "_platform", lambda: "darwin")
    link.open_app([tmp_path / "unita.gpkg", "other.tif"])
    link.open_app()
    # posix_spawn, never fork (SpatiaLite may be loaded): an absolute program and no cwd
    assert started == [
        (["/usr/bin/open", "-a", str(app), str(tmp_path / "unita.gpkg"), "other.tif"], {"close_fds": False}),
        (["/usr/bin/open", "-a", str(app)], {"close_fds": False})]
    started.clear()
    exe = tmp_path / "RustGIS.exe"
    monkeypatch.setattr(link, "find_rustgis", lambda: link.RustGisInstall(exe, None, None))
    monkeypatch.setattr(link, "_platform", lambda: "win32")
    link.open_app([tmp_path / "unita.gpkg"])
    assert started == [([str(exe), str(tmp_path / "unita.gpkg")], {"close_fds": False})]
    for found in (None, link.RustGisInstall(None, tmp_path / "rustgis-cli", None)):
        monkeypatch.setattr(link, "find_rustgis", lambda found=found: found)
        with pytest.raises(link.RustGisError, match="not installed"):
            link.open_app()


# ---- the engine over pipes

ENGINE = r'''
import json, sys, time

print("engine ready", file=sys.stderr, flush=True)
version = sys.argv[1]
for line in sys.stdin:
    message = json.loads(line)
    method, rid = message.get("method"), message.get("id")
    if rid is None:
        continue  # a notification: no reply
    if method == "initialize":
        result = {"serverInfo": {"name": "rustgis", "version": version}}
    else:
        name, args = message["params"]["name"], message["params"]["arguments"]
        if name == "die":
            print("engine failed: boom", file=sys.stderr, flush=True)
            sys.exit(3)
        if name == "hang":
            time.sleep(2)
        if name == "late":  # the answer to a call given up on comes before this one's
            stale = {"content": [{"type": "text", "text": "{\"stale\": true}"}]}
            print(json.dumps({"jsonrpc": "2.0", "id": rid - 1, "result": stale}), flush=True)
            print("not a message", flush=True)
        if name == "busy":
            busy = {"code": -32004, "message": "not drawing"}
            print(json.dumps({"jsonrpc": "2.0", "id": rid, "error": busy}), flush=True)
            continue
        if name == "refuse":
            result = {"content": [{"type": "text", "text": "unknown command"}], "isError": True}
        else:
            result = {"content": [{"type": "text", "text": json.dumps({"name": name, "arguments": args})}]}
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
'''


@pytest.fixture
def engine(tmp_path, monkeypatch):
    script = tmp_path / "engine.py"
    script.write_text(ENGINE, encoding="utf-8")
    monkeypatch.setattr(link.StdioTransport, "CLOSE_WAIT", 0.5)

    def start(version: str = "0.4.0", **options) -> link.StdioTransport:
        return link.StdioTransport(Path(sys.executable), [str(script), version], **options)

    return start


def test_the_engine_over_pipes(engine):
    with engine() as transport:
        assert transport.version == "0.4.0" and transport.initialize() == "0.4.0"
        assert transport.call("run_command", {"command": "edit.status", "text": "perché"}) == {
            "name": "run_command", "arguments": {"command": "edit.status", "text": "perché"}}
        with pytest.raises(link.RustGisRefused, match="unknown command"):
            transport.call("refuse", {})
        with pytest.raises(link.RustGisBusy, match="not drawing") as busy:
            transport.call("busy", {})
        assert busy.value.outcome_unknown is False
        # the late answer of an earlier call and a line that is no message are passed over
        assert transport.call("late", {}) == {"name": "late", "arguments": {}}
        assert transport.call("run_command", {}) == {"name": "run_command", "arguments": {}}
        process = transport._process
    assert process.poll() is not None  # closed with the block
    transport.close()  # twice is harmless


def test_the_engine_too_old_is_refused_at_start(engine):
    with pytest.raises(link.RustGisTooOld, match=r"0\.3\.0"):
        engine("0.3.0")


def test_the_engine_that_dies_says_why(engine):
    transport = engine()
    with pytest.raises(link.RustGisError, match=r"(?s)exit code 3.*engine failed: boom"):
        transport.call("die", {})
    with pytest.raises(link.RustGisError, match="stopped"):  # and a later call fails at once
        transport.call("list_layers", {})
    transport.close()


def test_the_engine_that_stays_silent_is_busy(engine):
    with engine(timeout=0.3) as transport:
        with pytest.raises(link.RustGisBusy, match="did not answer") as busy:
            transport.call("hang", {})
        assert busy.value.outcome_unknown is True
        transport.timeout = 10  # the engine answers the call given up on after two seconds, then the next one
        # and the late answer of the call given up on is passed over
        assert transport.call("run_command", {}) == {"name": "run_command", "arguments": {}}


def test_a_program_that_cannot_start_is_named(tmp_path):
    with pytest.raises(link.RustGisError, match="could not be started"):
        link.StdioTransport(tmp_path / "no-such-rustgis-cli", ["mcp"])


CLI = os.environ.get("PPG_RUSTGIS_CLI")


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_the_engine_over_pipes_with_the_real_cli():
    with link.StdioTransport(Path(CLI), ["mcp", "--sample", "--yes"]) as transport:
        assert not link.too_old(transport.version)
        layers = transport.call("list_layers", {})
        assert layers["layers"] and "activeMap" in layers
        with pytest.raises(link.RustGisRefused, match="no.such"):
            transport.call("run_command", {"command": "no.such", "params": {}})


# ---- the line of the diagnosis

def _installed(version):
    return lambda: link.RustGisInstall(Path("/Applications/RustGIS.app"), None, version)


def test_doctor_line(monkeypatch, tmp_path, fake_rustgis):
    fake = fake_rustgis
    token = fake.folder / link.TOKEN_FILE
    published = fake.runtime / "rustgis" / "endpoint.json"
    published.unlink()  # RustGIS is not running: nothing published
    # not installed: not a problem, and how to install it
    monkeypatch.setattr(link, "find_rustgis", lambda: None)
    check = doctor.rustgis_check()
    assert (check.name, check.level) == ("RustGIS", doctor.OK) and "not installed" in check.detail
    assert "github.com/opengeos/RustGIS" in check.hint and "0.4.0" in check.hint
    monkeypatch.setattr(link, "find_rustgis", lambda: link.RustGisInstall(None, Path("/x/rustgis-cli"), None))
    assert "not installed" in doctor.rustgis_check().detail  # the engine alone is not the app
    # installed, older than the link needs
    monkeypatch.setattr(link, "find_rustgis", _installed("0.3.0"))
    check = doctor.rustgis_check()
    assert check.level == doctor.WARN and "0.3.0" in check.detail and "0.4.0" in check.detail
    assert "github.com/opengeos/RustGIS" in check.hint
    # installed, agent control off (nothing published): fine, and how to turn it on
    monkeypatch.setattr(link, "find_rustgis", _installed("0.4.0"))
    check = doctor.rustgis_check()
    assert check.level == doctor.OK and "0.4.0" in check.detail and "agent control is off" in check.detail
    assert "Let your Agents Control RustGIS" in check.hint and "AI Assistant" in check.hint
    # RustGIS runs with the box unticked (the relay is up, the HTTP listener is not), or it crashed and left its file
    # with a pid that does not run, or it runs and nothing answers at its address (the token of an earlier run stays
    # in its settings folder all the while): the same
    for changes in ({"http_url": None, "http_token_file": None}, {"pid": dead_pid()}):
        publish_endpoint(fake.runtime, fake.port, token, **changes)
        check = doctor.rustgis_check()
        assert check.level == doctor.OK and "agent control is off" in check.detail and "not running" in check.detail
        assert "Let your Agents Control RustGIS" in check.hint
    publish_endpoint(fake.runtime, fake.port, token)
    fake.close()
    check = doctor.rustgis_check()
    assert check.level == doctor.OK and "agent control is off" in check.detail and "not running" in check.detail
    assert "Let your Agents Control RustGIS" in check.hint


def test_doctor_line_when_rustgis_answers(monkeypatch, fake_rustgis):
    fake = fake_rustgis
    monkeypatch.setattr(link, "find_rustgis", _installed("0.4.0"))
    check = doctor.rustgis_check()
    assert (check.name, check.level) == ("RustGIS", doctor.OK) and check.hint == ""
    assert "0.4.0" in check.detail and f"http://127.0.0.1:{fake.port}/mcp" in check.detail
    assert TOKEN not in check.detail + check.hint
    # the running RustGIS is the one that counts, not the bundle that was updated meanwhile
    monkeypatch.setattr(link, "find_rustgis", _installed("0.3.0"))
    assert doctor.rustgis_check().level == doctor.OK
    fake.version = "0.3.0"
    check = doctor.rustgis_check()
    assert check.level == doctor.WARN and "0.3.0" in check.detail
    # listening but not answering now (minimized, hidden, a dialog open): agent control is on, nothing is wrong
    fake.version = "0.4.0"
    fake.replies["initialize"] = {"error": {"code": -32004, "message": "RustGIS isn't drawing"}}
    check = doctor.rustgis_check()
    assert check.level == doctor.OK and "agent control is on" in check.detail and "not answering" in check.detail
    fake.replies["initialize"] = {"error": {"code": -32602, "message": "something else"}}
    check = doctor.rustgis_check()
    assert check.level == doctor.WARN and "something else" in check.detail
    assert TOKEN not in check.detail + check.hint


def test_run_checks_has_the_line_after_open3d_gpu(monkeypatch):
    from pyarchinit_photogrammetry.engine import isolate

    monkeypatch.setattr(isolate, "run_isolated", lambda fn, timeout=None: None)  # no SpatiaLite probe here
    monkeypatch.setattr(link, "find_rustgis", lambda: None)
    names = [c.name for c in doctor.run_checks(gui=False).checks]
    assert names.index("RustGIS") == names.index("Open3D GPU") + 1
    assert "rustgis_check" in doctor.__all__


# ---- the edits made in RustGIS, back into the live GeoPackage (a pull)

GB = "EPSG:3004"  # Gauss-Boaga, as the trench of the window's tests: metre coordinates that float32 would round
GX, GY = 2480000.0, 4500000.0
TEXT = "Strato di crollò, più fine"
EXPORTED_AS = {"unita": "MULTIPOLYGON", "sezioni": "MULTILINESTRING", "curve": "MULTILINESTRING", "quote": "POINT"}


def _square(x, y, side=0.5):
    return ("Polygon", [[x, y], [x + side, y], [x + side, y + side], [x, y + side]])


def _unit(us, x, y, side=0.5, **values):
    return {"etichetta": f"US {us}", "us_s": str(us), "unita_tipo_s": "US", ID_FIELD: new_id(),
            "_geom": _square(x, y, side), **values}


def _drawings(units=(), quote=(), sections=(), curves=()):
    """The layers of the window, as LiveLink.write takes them."""
    return {"Unita' stratigrafiche": {"kind": "polygon", "features": list(units)},
            "Quote": {"kind": "point", "features": list(quote)},
            "Sezioni": {"kind": "line", "features": list(sections)},
            "Curve di livello (0.1 m)": {"kind": "line", "features": list(curves)}}


def _live_table(path, table):
    """The columns (name -> declared type) of a table of the live GeoPackage, and its rows by fid as
    ``[fid, geometry, values]``."""
    con = sqlite3.connect(path)
    try:
        types = {r[1]: r[2] for r in con.execute(f'PRAGMA table_info("{table}")') if r[1] not in ("fid", "geom")}
        names = "".join(f', "{c}"' for c in types)
        rows = [[r[0], gpkg.geometry(r[1]), dict(zip(types, r[2:]))]
                for r in con.execute(f'SELECT fid, geom{names} FROM "{table}" ORDER BY fid')]
    finally:
        con.close()
    return types, rows


def _as_rustgis(path, table, noise=1e-9):
    """What RustGIS holds of a table it was given, as its export writes it: every vertex moved by ``noise`` (its round
    trip through longitude and latitude), polygons and lines as multi-part geometries of one part, every column (the
    technical ones too) with its type. Returns ``(types, [[geometry, values], ...])``, to change and prepare."""
    types, rows = _live_table(path, table)
    out = []
    for _, g, values in rows:
        g = shapely.transform(g, lambda xy: xy + noise)
        if g.geom_type == "Polygon":
            g = MultiPolygon([g])
        elif g.geom_type == "LineString":
            g = MultiLineString([g])
        out.append([g, dict(values)])
    return types, out


def _exported(path, name, gtype, types, rows, crs=GB):
    """A GeoPackage as RustGIS's share.exportLayer writes one: a single table named after the layer, its own fid, a
    geometry column of the multi-part type (POINT for points), every column with the type given."""
    srs = gpkg.srs_of(crs)
    path.unlink(missing_ok=True)
    con = gpkg.connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        gpkg.create(con, srs)
        cols = gpkg.ensure_table(con, name, gtype, types, srs[0])
        names = "".join(f", {gpkg.quote(c)}" for c in cols)
        marks = ", ?" * len(cols)
        for g, values in rows:
            con.execute(f"INSERT INTO {gpkg.quote(name)} (geom{names}) VALUES (?{marks})",
                        [None if g is None else gpkg.blob(g, srs[0]), *[values.get(c) for c in cols]])
        con.execute("COMMIT")
    finally:
        con.close()
    return path


class FakeExporter:
    """RustGIS as far as a pull goes: ``share.exportLayer`` copies the GeoPackage the test prepared for that layer into
    the path asked, refusing a destination where the file or one of its journals exists (as RustGIS does); ``fail``
    (layer -> exception) makes the export of a layer fail, ``left_out`` (layer -> n) says it left features out. Every
    call is in ``calls``."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.prepared: dict[str, Path] = {}
        self.fail: dict[str, Exception] = {}
        self.left_out: dict[str, int] = {}
        self.calls: list[tuple[str, dict]] = []

    def prepare(self, layer: str, table: str, types: dict, rows: list, crs: str = GB) -> None:
        self.prepared[layer] = _exported(self.folder / f"{layer}.gpkg", table, EXPORTED_AS[table], types, rows, crs)

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, json.loads(json.dumps(args))))
        assert tool == "run_command" and args["command"] == "share.exportLayer", (tool, args)
        params = args["params"]
        if params["layer"] in self.fail:
            raise self.fail[params["layer"]]
        dest = Path(params["path"])
        assert dest.is_absolute() and dest.suffix == ".gpkg"
        if any(Path(f"{dest}{suffix}").exists() for suffix in ("", "-wal", "-shm", "-journal")):
            raise link.RustGisRefused(f"i/o: GeoPackage export cannot replace {dest} or its SQLite journals. Choose a "
                                      "new filename to preserve existing data.")
        shutil.copyfile(self.prepared[params["layer"]], dest)
        return {"crs": GB, "features": 0, "leftOut": self.left_out.get(params["layer"], 0),
                "notices": [f"Exported {dest}"], "path": str(dest)}


def _linked(tmp_path, drawings, layers=None):
    """A live GeoPackage the program wrote, and a link to a fake RustGIS that was given its tables (what the start of
    the link does: ``remember``)."""
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(drawings)
    assert live.read_changes() == {}
    fake = FakeExporter(tmp_path / "prepared")
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    rl.layers = dict(layers or {"unita": "7"})
    rl.remember()
    return live, fake, rl


def test_normalize():
    """A row as the live GeoPackage keeps it: RustGIS's multi-part geometry of one part is that part, the values
    typed as the QGIS link types its columns, NULL kept, an empty text NULL, the export's feature id left out."""
    square = box(0, 0, 1, 1)
    g, values = link.normalize(MultiPolygon([square]), {
        "fid": 3, "us_s": 6, "area_s": 2.0, "stratigraph_index_us": 2.0, PART_FIELD: 1.0, "quota_q": "99.5",
        "area_m2": 1, "x": float("nan"), "data": "", "disegnatore": None, "tipo_us_s": TEXT, ID_FIELD: "abc"})
    assert g.geom_type == "Polygon" and g.equals_exact(square, 0)
    assert values == {"us_s": "6", "area_s": "2", "stratigraph_index_us": 2, PART_FIELD: 1, "quota_q": 99.5,
                      "area_m2": 1.0, "x": None, "data": None, "disegnatore": None, "tipo_us_s": TEXT, ID_FIELD: "abc"}
    assert type(values["stratigraph_index_us"]) is int and type(values["area_m2"]) is float
    two = MultiPolygon([square, box(2, 0, 3, 1)])
    assert link.normalize(two, {})[0].equals_exact(two, 0)  # several parts stay one: the pull makes a row of each
    assert link.normalize(MultiLineString([[(0, 0), (1, 1)]]), {})[0].geom_type == "LineString"
    assert link.normalize(None, {}) == (None, {}) and link.normalize(Polygon(), {})[0] is None
    assert not link.normalize(Point(1, 2, 3), {})[0].has_z


def test_a_pull_is_read_back_as_an_edit_of_another_program(tmp_path, monkeypatch):
    """Unit 1 moved 5 cm in RustGIS, unit 2 deleted there, a unit drawn there, a spot height moved there with a new
    height: the pull writes them into the live GeoPackage through a connection of its own, in one transaction for
    both tables, and LiveLink reads them as it reads QGIS's."""
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    spot = {"quota_q": 99.25, "us_q": "1", ID_FIELD: new_id(), "_geom": ("Point", [GX + 0.2, GY + 0.2])}
    live, fake, rl = _linked(tmp_path, _drawings([one, two], [spot]), {"unita": "7", "quote": "5"})
    types, rows = _as_rustgis(live.path, "unita")
    moved = affinity.translate(rows[0][0], xoff=0.05)
    rows[0][0] = moved
    del rows[1]
    rows.append([MultiPolygon([box(GX + 3, GY, GX + 3.5, GY + 0.5)]), {"us_s": "7", "unita_tipo_s": "USM"}])
    fake.prepare("7", "unita", types, rows)
    qtypes, qrows = _as_rustgis(live.path, "quote")
    qrows[0] = [Point(GX + 0.3, GY + 0.4), dict(qrows[0][1], quota_q=99.4)]
    fake.prepare("5", "quote", qtypes, qrows)
    _, stored = _live_table(live.path, "unita")
    technical = {k: stored[0][2][k] for k in (ID_FIELD, PART_FIELD, "ppg_sig")}
    connections, real = [], gpkg.connect

    def traced(path):
        con = real(path)
        said: list[str] = []
        con.set_trace_callback(said.append)
        connections.append((con, said))
        return con

    monkeypatch.setattr(gpkg, "connect", traced)
    assert rl.pull() == {"unita": (1, 1, 1), "quote": (1, 0, 0)}
    monkeypatch.setattr(gpkg, "connect", real)
    scratch = tmp_path / "qgis" / "rustgis"  # beside the live GeoPackage
    assert fake.calls == [("run_command", {"command": "share.exportLayer", "params": {"layer": layer, "path": str(
        scratch / f"{table}.gpkg")}}) for table, layer in (("unita", "7"), ("quote", "5"))]
    assert (scratch / "unita.gpkg").is_file() and (scratch / "quote.gpkg").is_file()
    # written as another program writes: a connection of its own, one transaction, closed after it
    said = [s for _, statements in connections for s in statements]
    assert [s for s in said if s.split()[0].upper() in ("BEGIN", "COMMIT", "ROLLBACK", "END")] == [
        "BEGIN IMMEDIATE", "COMMIT"]
    for con, _ in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            con.execute("SELECT 1")
    _, stored = _live_table(live.path, "unita")  # the moved row: its identity, its part and its signature untouched
    assert stored[0][0] == 1 and {k: stored[0][2][k] for k in technical} == technical
    assert live.external_change()
    changes = live.read_changes()
    got = changes["unita"]
    (changed,) = got.changed
    assert changed.id == one[ID_FIELD] and changed.geometry.equals_exact(moved.geoms[0], 1e-9)
    assert changed.values["us_s"] == "1" and changed.values["etichetta"] == "US 1"
    assert got.deleted == [(two[ID_FIELD], 1)]
    (added,) = got.added
    assert added.id and added.part == 1 and added.values["us_s"] == "7" and added.values["unita_tipo_s"] == "USM"
    (height,) = changes["quote"].changed
    assert height.id == spot[ID_FIELD] and height.geometry.equals(Point(GX + 0.3, GY + 0.4))
    assert height.values["quota_q"] == 99.4 and height.values["us_q"] == "1"
    assert not live.external_change() and live.read_changes() == {}  # given once
    assert rl.pull() == {"unita": (0, 0, 0), "quote": (0, 0, 0)}  # the same exports again: nothing more
    assert not live.external_change()


def test_noise_under_a_millimetre_is_no_change(tmp_path):
    """What RustGIS gives back of rows nobody touched: every vertex moved a nanometre (its round trip through longitude
    and latitude), integers as REAL, a text column as INTEGER, NULLs and accented text as they were. Nothing is written
    and nothing is read back - also with a vertex on a half millimetre, where the rounded signature flips with the
    noise."""
    one = _unit(1, GX, GY, stratigraph_index_us=1, tipo_us_s=TEXT, area_m2=0.25, data=None)
    one["_geom"] = ("Polygon", [[GX + 0.0005, GY], [GX + 0.5, GY], [GX + 0.5, GY + 0.5], [GX, GY + 0.5]])
    spot = {"quota_q": 99.25, "us_q": "1", ID_FIELD: new_id(), "_geom": ("Point", [GX + 0.2, GY + 0.2])}
    section = {"etichetta": "Sezione 1", ID_FIELD: new_id(), "_geom": ("LineString", [[GX, GY + 2], [GX + 2, GY + 2]])}
    live, fake, rl = _linked(tmp_path, _drawings([one, _unit(6, GX + 1, GY)], [spot], [section]),
                             {"unita": "7", "quote": "5", "sezioni": "6"})
    for table, layer in (("unita", "7"), ("quote", "5"), ("sezioni", "6")):
        types, rows = _as_rustgis(live.path, table)
        if table == "unita":
            types.update(stratigraph_index_us="REAL", ppg_parte="REAL", us_s="INTEGER")
            for _, values in rows:
                values[PART_FIELD] = float(values[PART_FIELD])
                values["us_s"] = int(values["us_s"])
                if values["stratigraph_index_us"] is not None:
                    values["stratigraph_index_us"] = float(values["stratigraph_index_us"])
        fake.prepare(layer, table, types, rows)
    assert rl.pull() == {"unita": (0, 0, 0), "quote": (0, 0, 0), "sezioni": (0, 0, 0)}
    assert not live.external_change() and live.read_changes() == {}
    # the half millimetre: the program's row and what RustGIS has now have different rounded signatures, and are
    # the same within a millimetre
    _, rows = _as_rustgis(live.path, "unita")
    _, stored = _live_table(live.path, "unita")
    key = (one[ID_FIELD], 1)
    assert rl.baseline["unita"][key] == link.signature(*rows[0]) != link.signature(*stored[0][1:])
    assert rl.matches_baseline("unita", key, *stored[0][1:])
    assert not rl.matches_baseline("unita", key, affinity.translate(stored[0][1], xoff=0.002), stored[0][2])
    assert not rl.matches_baseline("unita", key, stored[0][1], dict(stored[0][2], us_s="2"))


def test_parts_holes_and_accents(tmp_path):
    """Unit 1 redrawn in RustGIS as two polygons, one with a hole, its text changed to accented Italian; a unit drawn
    there as two polygons; one drawn as a single polygon. A feature of several polygons comes back as one unit, a row
    per part (ppg_parte 1, 2) under one identity, the hole kept, the text as written."""
    one = _unit(1, GX, GY)
    live, fake, rl = _linked(tmp_path, _drawings([one]))
    types, rows = _as_rustgis(live.path, "unita")
    holed = Polygon(box(GX, GY, GX + 2, GY + 2).exterior.coords,
                    [box(GX + 0.5, GY + 0.5, GX + 1, GY + 1).exterior.coords])
    beside = box(GX + 3, GY, GX + 3.5, GY + 0.5)
    rows[0] = [MultiPolygon([holed, beside]), dict(rows[0][1], tipo_us_s=TEXT)]
    pair = [affinity.translate(holed, yoff=5), affinity.translate(beside, yoff=5)]
    rows.append([MultiPolygon(pair), {"us_s": "13", "tipo_us_s": TEXT}])
    rows.append([MultiPolygon([box(GX + 6, GY, GX + 6.5, GY + 0.5)]), {"us_s": "12"}])
    fake.prepare("7", "unita", types, rows)
    assert rl.pull() == {"unita": (1, 4, 0)}  # unit 1 changed, its second part added; 2 + 1 rows drawn there
    _, stored = _live_table(live.path, "unita")
    parts = {}
    for _, g, values in stored:
        parts.setdefault(values["us_s"], []).append((values[ID_FIELD], values[PART_FIELD], g, values["tipo_us_s"]))
    assert [(pid, n) for pid, n, *_ in parts["1"]] == [(one[ID_FIELD], 1), (one[ID_FIELD], 2)]
    (pid13, _, _, _), _ = parts["13"]
    assert pid13 and pid13 != one[ID_FIELD] and [(pid, n) for pid, n, *_ in parts["13"]] == [(pid13, 1), (pid13, 2)]
    for us, polygons in (("1", [holed, beside]), ("13", pair)):
        for (_, _, g, text), polygon in zip(parts[us], polygons):
            assert g.equals_exact(polygon, 1e-6) and len(g.interiors) == len(polygon.interiors) and text == TEXT
    assert [(pid, n) for pid, n, *_ in parts["12"]] == [(None, None)]  # LiveLink gives it its identity
    got = live.read_changes()["unita"]
    (single,) = got.added
    assert single.values["us_s"] == "12" and single.id and single.part == 1
    # the rows of an identity the reader has never seen (unit 1's new part, the unit of two polygons drawn there) come
    # as changed: the window takes an identity it does not know for a unit added there
    assert sorted((f.values["us_s"], f.part) for f in got.changed) == [("1", 1), ("1", 2), ("13", 1), ("13", 2)]
    assert {f.id for f in got.changed if f.values["us_s"] == "13"} == {pid13}
    assert all(f.values["tipo_us_s"] == TEXT for f in got.changed)
    assert rl.pull() == {"unita": (0, 0, 0)}  # nothing written twice


def test_one_way_tables_are_never_pulled(tmp_path):
    """The contour lines (and the other tables computed by the program) go one way: what is changed there in RustGIS
    is never asked for, nor written."""
    curve = {"quota": 99.5, "_geom": ("LineString", [[GX, GY], [GX + 2, GY + 2]])}
    live, fake, rl = _linked(tmp_path, _drawings([_unit(1, GX, GY)], curves=[curve]), {"unita": "7", "curve": "1"})
    for table, layer in (("unita", "7"), ("curve", "1")):
        types, rows = _as_rustgis(live.path, table)
        if table == "curve":
            rows[0] = [affinity.translate(rows[0][0], xoff=1.0), dict(rows[0][1], quota=101.0)]
            rows.append([MultiLineString([[(GX, GY + 3), (GX + 1, GY + 3)]]), {"quota": 98.0}])
        fake.prepare(layer, table, types, rows)
    before = _live_table(live.path, "curve")
    assert rl.pull() == {"unita": (0, 0, 0)}
    assert [call[1]["params"]["layer"] for call in fake.calls] == ["7"]  # RustGIS is not even asked for it
    assert _live_table(live.path, "curve") == before and not live.external_change()
    rl.layers = {"curve": "1", "linee": "2"}
    assert rl.pull() == {} and len(fake.calls) == 1


def test_what_the_program_has_not_sent_yet_is_kept(tmp_path):
    """The program changed unit 1 (its number and its outline), deleted unit 2 and drew unit 4 after RustGIS was given
    the tables, and has not sent them yet: a pull does not undo them - also with a vertex on a half millimetre, where
    RustGIS's round trip flips the rounded signature. What RustGIS changes meanwhile comes, column by column: unit 3
    moved there, the drawer of unit 1 set there beside the number and outline changed here; unit 2, deleted here and
    changed there, comes back, as an edit of QGIS to a unit deleted here does."""
    one, two, three = _unit(1, GX, GY), _unit(2, GX + 1, GY), _unit(3, GX + 2, GY)
    one["_geom"] = ("Polygon", [[GX + 0.0005, GY], [GX + 0.5, GY], [GX + 0.5, GY + 0.5], [GX, GY + 0.5]])
    live, fake, rl = _linked(tmp_path, _drawings([one, two, three]))
    types, rows = _as_rustgis(live.path, "unita")  # RustGIS has them as they were given
    fake.prepare("7", "unita", types, rows)
    one["us_s"] = "10"
    one["_geom"] = ("Polygon", [[GX + 0.0005, GY], [GX + 0.6, GY], [GX + 0.5, GY + 0.5], [GX, GY + 0.5]])
    outline = Polygon(one["_geom"][1])
    four = _unit(4, GX + 3, GY)
    live.write(_drawings([one, three, four]))  # the program's edits, not sent yet
    assert rl.pull() == {"unita": (0, 0, 0)}
    _, stored = _live_table(live.path, "unita")
    assert sorted(v["us_s"] for _, _, v in stored) == ["10", "3", "4"] and stored[0][1].equals_exact(outline, 0)
    assert not live.external_change() and live.read_changes() == {}
    rows[2][0] = affinity.translate(rows[2][0], yoff=0.2)
    rows[0][1]["disegnatore"] = "E. C."
    rows[1][1]["us_s"] = "20"
    fake.prepare("7", "unita", types, rows)
    assert rl.pull() == {"unita": (2, 1, 0)}
    got = live.read_changes()["unita"]
    by_id = {f.id: f for f in got.changed}
    assert by_id[one[ID_FIELD]].values["us_s"] == "10" and by_id[one[ID_FIELD]].values["disegnatore"] == "E. C."
    assert by_id[one[ID_FIELD]].geometry.equals_exact(outline, 0)
    assert by_id[three[ID_FIELD]].geometry.bounds[1] == pytest.approx(GY + 0.2)
    assert by_id[two[ID_FIELD]].values["us_s"] == "20" and not got.added and not got.deleted
    _, stored = _live_table(live.path, "unita")
    assert four[ID_FIELD] in {v[ID_FIELD] for _, _, v in stored}


def test_a_unit_drawn_in_rustgis_is_written_once(tmp_path):
    """A unit drawn in RustGIS has no identity there until the program's edits go back to it, so every pull meanwhile
    finds it again: it is written once; an attribute set there comes into its row, beside the program's own (here the
    number it was given in the window); deleted there, its row goes."""
    one = _unit(1, GX, GY)
    live, fake, rl = _linked(tmp_path, _drawings([one]))
    types, rows = _as_rustgis(live.path, "unita")
    rows.append([MultiPolygon([box(GX + 3, GY, GX + 3.5, GY + 0.5)]), {"us_s": "7"}])
    fake.prepare("7", "unita", types, rows)
    assert rl.pull() == {"unita": (0, 1, 0)}
    assert rl.pull() == {"unita": (0, 0, 0)}  # not read yet: not written again
    (new,) = live.read_changes()["unita"].added
    live.write(_drawings([one, dict(_unit(8, GX + 3, GY), **{ID_FIELD: new.id})]))  # the window's version: US 8
    assert rl.pull() == {"unita": (0, 0, 0)} and live.read_changes() == {}
    rows[1][1]["disegnatore"] = "E. C."  # set there, the unit still without identity in RustGIS
    fake.prepare("7", "unita", types, rows)
    assert rl.pull() == {"unita": (1, 0, 0)}
    (f,) = live.read_changes()["unita"].changed
    assert f.id == new.id and f.values["disegnatore"] == "E. C."
    assert f.values["us_s"] == "8" and f.values["etichetta"] == "US 8"  # the program's own, not RustGIS's 7
    del rows[1]
    fake.prepare("7", "unita", types, rows)
    assert rl.pull() == {"unita": (0, 0, 1)}
    assert live.read_changes()["unita"].deleted == [(new.id, 1)]


def test_nothing_is_written_when_an_export_cannot_be_taken(tmp_path):
    """RustGIS busy for one of the tables, or a layer exported in another system than the drawings': nothing is
    written, and what RustGIS had is not forgotten; a pull before the link knows what RustGIS was given is refused."""
    live, fake, rl = _linked(tmp_path, _drawings([_unit(1, GX, GY)]), {"unita": "7", "quote": "5"})
    types, rows = _as_rustgis(live.path, "unita")
    rows[0][0] = affinity.translate(rows[0][0], xoff=0.3)
    fake.prepare("7", "unita", types, rows)
    fake.prepare("5", "quote", *_as_rustgis(live.path, "quote"))
    baseline = copy.deepcopy(rl.baseline)
    fake.fail["5"] = link.RustGisBusy("RustGIS isn't drawing")
    with pytest.raises(link.RustGisBusy):
        rl.pull()
    assert not live.external_change() and rl.baseline == baseline
    del fake.fail["5"]
    fake.prepare("7", "unita", types, rows, crs="EPSG:32633")
    with pytest.raises(link.RustGisRefused, match="32633"):
        rl.pull()
    assert not live.external_change() and rl.baseline == baseline
    fake.prepare("7", "unita", types, rows)
    assert rl.pull() == {"unita": (1, 0, 0), "quote": (0, 0, 0)}
    fresh = link.RustGisLink(live, fake, "Saggio 1", GB)
    fresh.layers = {"unita": "7"}
    with pytest.raises(RuntimeError, match="remember"):
        fresh.pull()


def test_copies_fields_and_identities_rustgis_was_not_given(tmp_path):
    """A unit copied in RustGIS (every attribute copied, its identity too) is a unit of its own; a field added there
    becomes a column of the live table; a feature with an identity the program does not have here comes in with it;
    a unit the program drew after RustGIS was given the tables, which reached RustGIS meanwhile with another number (a
    send whose answer was lost), keeps the program's version, and the baseline learns what RustGIS has of it."""
    one = _unit(1, GX, GY)
    live, fake, rl = _linked(tmp_path, _drawings([one]))
    later = _unit(5, GX + 4, GY)
    live.write(_drawings([one, later]))
    types, rows = _as_rustgis(live.path, "unita")
    rows[1][1]["us_s"] = "50"
    rows.append([affinity.translate(rows[0][0], xoff=2), dict(rows[0][1])])  # unit 1 copied there
    foreign = new_id()
    rows.append([MultiPolygon([box(GX + 6, GY, GX + 6.5, GY + 0.5)]), {ID_FIELD: foreign, PART_FIELD: 1, "us_s": "9"}])
    types["nota"] = "TEXT"
    rows[0][1]["nota"] = "campione"
    fake.prepare("7", "unita", types, rows)
    assert rl.pull() == {"unita": (1, 2, 0)}
    stored_types, stored = _live_table(live.path, "unita")
    assert stored_types["nota"] == "TEXT"
    by_id = {v[ID_FIELD]: v for _, _, v in stored}
    assert by_id[one[ID_FIELD]]["nota"] == "campione" and by_id[later[ID_FIELD]]["us_s"] == "5"
    assert sorted(v["us_s"] for k, v in by_id.items() if k) == ["1", "5", "9"] and len(stored) == 4
    assert by_id[None]["us_s"] == "1" and by_id[None]["ppg_sig"] is None  # the copy: LiveLink gives it an identity
    got = live.read_changes()["unita"]
    (copied,) = got.added
    assert copied.values["us_s"] == "1" and copied.id != one[ID_FIELD]
    assert copied.geometry.bounds[0] == pytest.approx(GX + 2)
    assert sorted((f.values["us_s"], f.id == foreign) for f in got.changed) == [("1", False), ("9", True)]
    assert rl.matches_baseline("unita", (later[ID_FIELD], 1), *rows[1])  # RustGIS's version: the program sends its own
    later_row = next(r for r in stored if r[2]["us_s"] == "5")
    assert not rl.matches_baseline("unita", (later[ID_FIELD], 1), *later_row[1:])


def test_what_rustgis_left_out_of_an_export_is_not_taken_for_deleted(tmp_path):
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _linked(tmp_path, _drawings([one, two]))
    types, rows = _as_rustgis(live.path, "unita")
    del rows[1]
    fake.prepare("7", "unita", types, rows)
    fake.left_out["7"] = 1  # RustGIS says it could not write a feature
    assert rl.pull() == {"unita": (0, 0, 0)}
    assert (two[ID_FIELD], 1) in rl.baseline["unita"] and not live.external_change()
    del fake.left_out["7"]
    assert rl.pull() == {"unita": (0, 0, 1)}
    assert live.read_changes()["unita"].deleted == [(two[ID_FIELD], 1)]


def test_a_row_without_identity_given_to_rustgis_is_not_written_again(tmp_path):
    """A unit drawn in QGIS and not read yet when RustGIS is given the tables reaches RustGIS without an identity: the
    pull knows it for the row that is already there, and QGIS's drawing is read once."""
    one = _unit(1, GX, GY)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([one]))
    assert live.read_changes() == {}
    qgis = sqlite3.connect(live.path, isolation_level=None)
    qgis.execute("BEGIN IMMEDIATE")
    qgis.execute('INSERT INTO "unita" (geom, us_s) VALUES (?, ?)',
                 (gpkg.blob(box(GX + 3, GY, GX + 4, GY + 1), 3004), "9"))
    qgis.execute("COMMIT")
    qgis.close()
    fake = FakeExporter(tmp_path / "prepared")
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    rl.layers = {"unita": "7"}
    rl.remember()
    assert [d.fids for d in rl.drawn["unita"]] == [[2]]
    fake.prepare("7", "unita", *_as_rustgis(live.path, "unita"))
    assert rl.pull() == {"unita": (0, 0, 0)}
    (drawn,) = live.read_changes()["unita"].added
    assert drawn.values["us_s"] == "9" and drawn.fid == 2
    assert rl.pull() == {"unita": (0, 0, 0)} and live.read_changes() == {}


def _from_qgis(w):
    t = w.log_pane.table
    return [t.item(r, 2).text() for r in range(t.rowCount()) if t.item(r, 2).text().startswith("Da QGIS")]


def test_units_drawn_in_rustgis_reach_the_window_as_one_unit_each(trench, tmp_path):
    """Through the window's reading of the live GeoPackage, as for QGIS: a unit drawn in RustGIS as one polygon and one
    drawn as two polygons (one with a hole) come in as one unit each; a pull before the program's edits have gone back
    to RustGIS brings no copy; a unit deleted there goes."""
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    w, app, mw = trench
    w._add_unit(UnitPolygon(box(GX + 1, GY + 1, GX + 2, GY + 2), us="6", area="1", sito="Saggio"))
    w.a_qgis_live.setChecked(True)
    w._export_qgis_layers()
    w._poll_qgis()
    live = w._qgis_live_link()
    fake = FakeExporter(tmp_path / "prepared")
    rl = link.RustGisLink(live, fake, w.chunk.label, w.chunk.crs)
    rl.layers = {"unita": "7"}
    rl.remember()
    types, rows = _as_rustgis(live.path, "unita")
    holed = Polygon(box(GX + 3, GY + 3, GX + 5, GY + 5).exterior.coords,
                    [box(GX + 3.5, GY + 3.5, GX + 4, GY + 4).exterior.coords])
    rows.append([MultiPolygon([box(GX + 3, GY + 1, GX + 3.5, GY + 1.5)]), {"us_s": "12", "area_s": "1"}])
    rows.append([MultiPolygon([holed, box(GX + 0.5, GY + 4, GX + 1, GY + 4.5)]),
                 {"us_s": "13", "area_s": "1", "tipo_us_s": TEXT}])
    fake.prepare("7", "unita", types, rows)
    assert rl.pull() == {"unita": (0, 3, 0)}
    w._poll_qgis()
    units = {u.us: u for u in w.units}
    assert set(units) == {"6", "12", "13"}
    assert units["12"].geometry.geom_type == "Polygon"
    two = units["13"].geometry
    assert two.geom_type == "MultiPolygon" and sorted(len(p.interiors) for p in two.geoms) == [0, 1]
    assert units["13"].attributes["tipo_us_s"] == TEXT
    assert sorted(_from_qgis(w)) == ["Da QGIS: US 12 aggiunta", "Da QGIS: US 13 aggiunta"]
    w._export_qgis_layers()  # the window writes its version of them (the timer of the live link does it)
    assert rl.pull() == {"unita": (0, 0, 0)}
    w._poll_qgis()
    assert len(w.units) == 3 and len(_from_qgis(w)) == 2
    del rows[1]  # unit 12 deleted in RustGIS
    fake.prepare("7", "unita", types, rows)
    assert rl.pull() == {"unita": (0, 0, 1)}
    w._poll_qgis()
    assert {u.us for u in w.units} == {"6", "13"} and "Da QGIS: US 12 eliminata" in _from_qgis(w)


def _lonlat(geometry, crs):
    """GeoJSON of ``geometry`` in longitude and latitude, as RustGIS's edit commands take it."""
    import numpy as np
    from pyproj import Transformer
    from shapely.geometry import mapping

    to = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    lonlat = shapely.transform(geometry, lambda xy: np.column_stack(to.transform(xy[:, 0], xy[:, 1])))
    return json.loads(json.dumps(mapping(lonlat)))


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_pull_with_the_real_engine(tmp_path):
    """RustGIS's own engine on the live GeoPackage: a value set on row 0, a unit drawn as one polygon and one drawn as
    two (one with a hole); the pull writes them, LiveLink reads them back, and a second pull writes nothing."""
    crs, x0, y0 = "EPSG:32633", 291000.0, 4640000.0
    one, two = _unit(1, x0, y0), _unit(2, x0 + 1, y0)
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([one, two]))
    assert live.read_changes() == {}
    single = box(x0 + 4, y0, x0 + 4.5, y0 + 0.5)
    holed = Polygon(box(x0 + 6, y0, x0 + 8, y0 + 2).exterior.coords,
                    [box(x0 + 6.5, y0 + 0.5, x0 + 7, y0 + 1).exterior.coords])
    pair = MultiPolygon([holed, box(x0 + 9, y0, x0 + 9.5, y0 + 0.5)])
    with link.StdioTransport(Path(CLI), ["mcp", "--data", str(live.path), "--yes"]) as engine:
        ids = {layer["name"]: str(layer["id"]) for layer in engine.call("list_layers", {})["layers"]}
        rl = link.RustGisLink(live, engine, "Saggio 1", crs)
        rl.layers = {table: ids[table] for table in ("unita", "quote", "sezioni", "curve")}
        rl.remember()
        unita = ids["unita"]
        row0 = engine.call("read_features", {"layer": unita, "fields": [ID_FIELD]})["rows"][0]
        assert row0[ID_FIELD] == one[ID_FIELD]

        def command(name, **params):
            return engine.call("run_command", {"command": name, "params": params})

        command("edit.setValue", layer=unita, row=0, field="tipo_us_s", value=TEXT)
        command("edit.addFeature", layer=unita, geometry=_lonlat(single, crs), attributes={"us_s": "12"})
        command("edit.addFeature", layer=unita, geometry=_lonlat(pair, crs), attributes={"us_s": "13"})
        assert rl.pull() == {"unita": (1, 3, 0), "quote": (0, 0, 0), "sezioni": (0, 0, 0)}
        assert (tmp_path / "qgis" / "rustgis" / "unita.gpkg").is_file()
        got = live.read_changes()["unita"]
        (added,) = got.added
        assert added.values["us_s"] == "12" and link._same_geometry(added.geometry, single)
        changed = sorted(got.changed, key=lambda f: (f.values["us_s"], f.part))
        assert [(f.values["us_s"], f.part) for f in changed] == [("1", 1), ("13", 1), ("13", 2)]
        assert changed[0].id == one[ID_FIELD] and changed[0].values["tipo_us_s"] == TEXT
        assert changed[1].id == changed[2].id != one[ID_FIELD]
        assert link._same_geometry(changed[1].geometry, holed) and len(changed[1].geometry.interiors) == 1
        assert link._same_geometry(changed[2].geometry, pair.geoms[1])
        assert rl.pull() == {"unita": (0, 0, 0), "quote": (0, 0, 0), "sezioni": (0, 0, 0)}
        assert live.read_changes() == {}
