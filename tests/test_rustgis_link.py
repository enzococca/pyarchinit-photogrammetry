"""Finding RustGIS and talking to it (io/rustgis_link): where it publishes its address (the endpoint.json of a running
RustGIS) and its token, MCP over HTTP against a fake RustGIS (an http.server on this computer that checks every request
the way the real listener does), the same tools over the pipes of an engine, how each failure is named, and the line of
the diagnosis; then the edits made in RustGIS brought into the live GeoPackage of the QGIS link (a pull), against a fake
RustGIS that exports what the test prepared. The real engine runs only when PPG_RUSTGIS_CLI names a rustgis-cli; the
open app has its own opt-in test (test_rustgis_app.py)."""
from __future__ import annotations

import copy
import http.client
import json
import logging
import os
import plistlib
import re
import shutil
import socket
import socketserver
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import shapely
from shapely import affinity
from shapely.geometry import MultiLineString, MultiPolygon, Point, Polygon, box

from pyarchinit_photogrammetry import doctor
from pyarchinit_photogrammetry.io import gpkg
from pyarchinit_photogrammetry.io import rustgis_link as link
from pyarchinit_photogrammetry.io.qgis_link import (ID_FIELD, LIVE_TABLES, PART_FIELD, TWO_WAY_TABLES, LiveLink,
                                                    new_id)

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
    # the link's own logger, explicitly: the window's logging setup (gui/logging_setup.install, run by an earlier test
    # of the suite) leaves the package's logger at INFO, under which the root logger caplog sets would get no debug
    # record of the transport
    caplog.set_level(logging.DEBUG, logger=link.log.name)
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
    """An executable at ``path``: on Windows, where a program on the PATH is looked for by the extensions of PATHEXT
    (shutil.which), with ``.exe``."""
    if sys.platform == "win32" and not path.suffix:
        path = path.with_suffix(".exe")
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
    """RustGIS as far as a pull goes: ``list_layers`` lists the layers the test prepared, read from ``source`` (the
    live GeoPackage), with the fields prepared; ``edit.status`` says nothing was edited; ``share.exportLayer`` copies
    the GeoPackage the test prepared for that layer into the path asked, refusing a destination where the file or one
    of its journals exists (as RustGIS does); ``fail`` (layer -> exception) makes the export of a layer fail,
    ``left_out`` (layer -> n) says it left features out. Every call is in ``calls``."""

    def __init__(self, folder: Path, source: Path | None = None):
        self.folder = folder
        self.folder.mkdir(parents=True, exist_ok=True)
        self.source = source
        self.prepared: dict[str, Path] = {}
        self.tables: dict[str, tuple[str, list]] = {}  # layer -> the table and the fields prepared for it
        self.fail: dict[str, Exception] = {}
        self.left_out: dict[str, int] = {}
        self.calls: list[tuple[str, dict]] = []

    def prepare(self, layer: str, table: str, types: dict, rows: list, crs: str = GB) -> None:
        self.prepared[layer] = _exported(self.folder / f"{layer}.gpkg", table, EXPORTED_AS[table], types, rows, crs)
        self.tables[layer] = (table, list(types))

    def exports(self) -> list:
        return [args for tool, args in self.calls if tool == "run_command" and args["command"] == "share.exportLayer"]

    def call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, json.loads(json.dumps(args))))
        if tool == "list_layers":
            return {"layers": [{"id": int(layer), "name": table, "type": link.KINDS[LIVE_TABLES[table][1]],
                                "source": str(self.source), "fields": [f"{name} (Text)" for name in names]}
                               for layer, (table, names) in self.tables.items()], "groups": []}
        if tool == "run_command" and args["command"] == "edit.status":
            return {"layers": [], "unsaved": False}
        assert tool == "run_command" and args["command"] == "share.exportLayer", (tool, args)
        params = args["params"]
        assert isinstance(params["layer"], int), "a layer id goes as a number: a text is taken for a name first"
        layer = str(params["layer"])
        if layer in self.fail:
            raise self.fail[layer]
        dest = Path(params["path"])
        assert dest.is_absolute() and dest.suffix == ".gpkg"
        if any(Path(f"{dest}{suffix}").exists() for suffix in ("", "-wal", "-shm", "-journal")):
            raise link.RustGisRefused(f"i/o: GeoPackage export cannot replace {dest} or its SQLite journals. Choose a "
                                      "new filename to preserve existing data.")
        shutil.copyfile(self.prepared[layer], dest)
        return {"crs": GB, "features": 0, "leftOut": self.left_out.get(layer, 0),
                "notices": [f"Exported {dest}"], "path": str(dest)}


def _linked(tmp_path, drawings, layers=None):
    """A live GeoPackage the program wrote, and a link to a fake RustGIS that was given its tables and loaded them as
    they are (what the start of the link does: ``remember``, from RustGIS's own export). ``calls`` starts empty."""
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(drawings)
    assert live.read_changes() == {}
    fake = FakeExporter(tmp_path / "prepared", live.path)
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    rl.layers = dict(layers or {"unita": "7"})
    for table, layer in rl.layers.items():
        if table in TWO_WAY_TABLES:
            fake.prepare(layer, table, *_as_rustgis(live.path, table))
    rl.remember()
    fake.calls.clear()
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
    # the status first (what the pull takes once it has read), the layers checked to be the drawings', the exports
    assert [(tool, args.get("command")) for tool, args in fake.calls[:2]] == [("run_command", "edit.status"),
                                                                              ("list_layers", None)]
    assert fake.exports() == [{"command": "share.exportLayer", "params": {
        "layer": layer, "path": str(scratch / f"{table}.gpkg")}} for table, layer in (("unita", 7), ("quote", 5))]
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
    and nothing is read back - also with a vertex on a half millimetre, which the nanometre carries to the other side
    of a rounding to the millimetre."""
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
    # the half millimetre: rounded to the millimetre, the program's vertex and RustGIS's fall on two sides; compared
    # within a millimetre, the program's row is what RustGIS has (a push sends nothing)
    import numpy as np

    _, stored = _live_table(live.path, "unita")
    key = (one[ID_FIELD], 1)
    x = stored[0][1].exterior.coords[0][0]
    assert x == GX + 0.0005 and np.rint(x / 0.001) != np.rint((x + 1e-9) / 0.001)
    assert not rl.baseline["unita"][key][0].equals_exact(stored[0][1], 0)  # RustGIS's, a nanometre away
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
    # unit 1's new part is a change of a unit known here; the unit drawn there as two polygons comes as one unit added,
    # under the identity it was given; the single polygon is given one by the reader
    assert sorted((f.values["us_s"], f.part) for f in got.changed) == [("1", 1), ("1", 2)]
    assert sorted((f.values["us_s"], f.part) for f in got.added) == [("12", 1), ("13", 1), ("13", 2)]
    assert {f.id for f in got.added if f.values["us_s"] == "13"} == {pid13}
    single = next(f for f in got.added if f.values["us_s"] == "12")
    assert single.id not in (None, pid13, one[ID_FIELD]) and single.part == 1
    assert all(f.values["tipo_us_s"] == TEXT for f in got.changed + got.added if f.values["us_s"] != "12")
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
    assert [args["params"]["layer"] for args in fake.exports()] == [7]  # RustGIS is not even asked for it
    assert _live_table(live.path, "curve") == before and not live.external_change()
    rl.layers = {"curve": "1", "linee": "2"}
    assert rl.pull() == {} and len(fake.exports()) == 1


def test_what_the_program_has_not_sent_yet_is_kept(tmp_path):
    """The program changed unit 1 (its number and its outline), deleted unit 2 and drew unit 4 after RustGIS was given
    the tables, and has not sent them yet: a pull does not undo them - also with a vertex on a half millimetre, which
    RustGIS's round trip carries to the other side of a rounding to the millimetre. What RustGIS changes meanwhile
    comes, column by column: unit 3 moved there, the drawer of unit 1 set there beside the number and outline changed
    here; unit 2, deleted here and changed there, comes back under its identity, as an edit of QGIS to a unit deleted
    here does."""
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
    assert by_id[three[ID_FIELD]].geometry.bounds[1] == pytest.approx(GY + 0.2) and len(by_id) == 2
    (back,) = got.added
    assert back.id == two[ID_FIELD] and back.values["us_s"] == "20" and not got.deleted
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
    (changed,) = got.changed
    assert changed.id == one[ID_FIELD] and changed.values["nota"] == "campione"
    assert sorted((f.values["us_s"], f.id == foreign) for f in got.added) == [("1", False), ("9", True)]
    copied = next(f for f in got.added if f.id != foreign)
    assert copied.id != one[ID_FIELD] and copied.geometry.bounds[0] == pytest.approx(GX + 2)
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


@pytest.mark.parametrize("read_first", [False, True], ids=["read after the pull", "read before the link remembers"])
def test_a_row_without_identity_given_to_rustgis_is_not_written_again(tmp_path, read_first):
    """A unit drawn in QGIS and not read yet when RustGIS is given the tables reaches RustGIS without an identity: the
    pull knows it for the row that is already there - also when the window gave the row its identity before the link
    remembered what RustGIS has -, and QGIS's drawing is read once."""
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
    fake = FakeExporter(tmp_path / "prepared", live.path)
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    rl.layers = {"unita": "7"}
    fake.prepare("7", "unita", *_as_rustgis(live.path, "unita"))  # RustGIS loads the table as it is
    if read_first:
        (drawn,) = live.read_changes()["unita"].added
    rl.remember()
    assert [d.fids for d in rl.drawn["unita"]] == [[2]]
    assert rl.pull() == {"unita": (0, 0, 0)}
    if not read_first:
        (drawn,) = live.read_changes()["unita"].added
    assert drawn.values["us_s"] == "9" and drawn.fid == 2
    assert rl.pull() == {"unita": (0, 0, 0)} and live.read_changes() == {}


def test_what_rustgis_did_not_load_is_never_taken_for_deleted(tmp_path):
    """RustGIS is given the tables and loads unit 1; before the link remembers what it has, the program writes unit 2
    and QGIS saves a unit drawn there. What RustGIS has is taken from its own export, not from the file: the first
    pull deletes nothing, and QGIS's unit is read once (the program's rows go to RustGIS with its edits)."""
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([one]))
    assert live.read_changes() == {}
    fake = FakeExporter(tmp_path / "prepared", live.path)
    fake.prepare("7", "unita", *_as_rustgis(live.path, "unita"))  # what RustGIS loaded: unit 1
    live.write(_drawings([one, two]))  # the program, meanwhile
    qgis = sqlite3.connect(live.path, isolation_level=None)  # and QGIS
    qgis.execute("BEGIN IMMEDIATE")
    qgis.execute('INSERT INTO "unita" (geom, us_s) VALUES (?, ?)',
                 (gpkg.blob(box(GX + 3, GY, GX + 4, GY + 1), 3004), "9"))
    qgis.execute("COMMIT")
    qgis.close()
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    rl.layers = {"unita": "7"}
    rl.remember()
    assert list(rl.baseline["unita"]) == [(one[ID_FIELD], 1)] and rl.drawn["unita"] == []
    assert rl.pull() == {"unita": (0, 0, 0)}
    got = live.read_changes()["unita"]
    assert not got.deleted and not got.changed and [f.values["us_s"] for f in got.added] == ["9"]
    _, stored = _live_table(live.path, "unita")
    assert sorted(v["us_s"] for _, _, v in stored) == ["1", "2", "9"]


def test_what_rustgis_has_beyond_what_it_was_given_comes_in(tmp_path):
    """Between the tables reaching RustGIS and the link remembering what it has, a unit is drawn there and a polygon
    added there to unit 1: neither is among the rows RustGIS was given, so the first pull brings both in, once."""
    one = _unit(1, GX, GY)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([one]))
    assert live.read_changes() == {}
    fake = FakeExporter(tmp_path / "prepared", live.path)
    types, rows = _as_rustgis(live.path, "unita")
    rows[0][0] = MultiPolygon([rows[0][0].geoms[0], box(GX + 3, GY, GX + 3.5, GY + 0.5)])
    rows.append([MultiPolygon([box(GX + 5, GY, GX + 5.5, GY + 0.5)]), {"us_s": "7"}])
    fake.prepare("7", "unita", types, rows)
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    rl.layers = {"unita": "7"}
    rl.remember()
    assert list(rl.baseline["unita"]) == [(one[ID_FIELD], 1)] and rl.drawn["unita"] == []
    assert rl.pull() == {"unita": (0, 2, 0)}
    got = live.read_changes()["unita"]
    assert [(f.id, f.part) for f in got.changed] == [(one[ID_FIELD], 2)]  # a new part of unit 1
    assert [f.values["us_s"] for f in got.added] == ["7"]
    assert rl.pull() == {"unita": (0, 0, 0)} and live.read_changes() == {}


def test_a_polygon_added_in_rustgis_to_a_unit_is_never_lost(tmp_path):
    """Unit 1 has a second part drawn in the program and not sent yet; in RustGIS a polygon is added to unit 1: it takes
    a part number after every part the unit has here and there, comes in beside the program's part, and keeps that
    number at the next pull."""
    one = _unit(1, GX, GY)
    live, fake, rl = _linked(tmp_path, _drawings([one]))
    types, rows = _as_rustgis(live.path, "unita")
    second = dict(one, _geom=_square(GX, GY + 1))
    live.write(_drawings([one, second]))  # (unit 1, part 2): the program's, not sent yet
    there = box(GX + 3, GY, GX + 3.5, GY + 0.5)
    rows[0][0] = MultiPolygon([rows[0][0].geoms[0], there])
    fake.prepare("7", "unita", types, rows)
    assert rl.pull() == {"unita": (0, 1, 0)}
    _, stored = _live_table(live.path, "unita")
    assert [(v[ID_FIELD], v[PART_FIELD]) for _, _, v in stored] == [(one[ID_FIELD], n) for n in (1, 2, 3)]
    assert stored[1][1].equals_exact(Polygon(second["_geom"][1]), 0) and link._same_geometry(stored[2][1], there)
    got = live.read_changes()["unita"]
    assert [(f.id, f.part) for f in got.changed] == [(one[ID_FIELD], 3)] and not got.added
    assert rl.pull() == {"unita": (0, 0, 0)} and live.read_changes() == {}  # the same number: written once


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
    fake = FakeExporter(tmp_path / "prepared", live.path)
    rl = link.RustGisLink(live, fake, w.chunk.label, w.chunk.crs)
    rl.layers = {"unita": "7"}
    types, rows = _as_rustgis(live.path, "unita")
    fake.prepare("7", "unita", types, rows)  # RustGIS loads the table as it is
    rl.remember()
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
        (changed,) = got.changed
        assert changed.id == one[ID_FIELD] and changed.values["tipo_us_s"] == TEXT
        added = sorted(got.added, key=lambda f: (f.values["us_s"], f.part))
        assert [(f.values["us_s"], f.part) for f in added] == [("12", 1), ("13", 1), ("13", 2)]
        assert link._same_geometry(added[0].geometry, single)
        assert added[1].id == added[2].id and len({one[ID_FIELD], added[0].id, added[1].id}) == 3
        assert link._same_geometry(added[1].geometry, holed) and len(added[1].geometry.interiors) == 1
        assert link._same_geometry(added[2].geometry, pair.geoms[1])
        assert rl.pull() == {"unita": (0, 0, 0), "quote": (0, 0, 0), "sezioni": (0, 0, 0)}
        assert live.read_changes() == {}


# ---- the link started, and the program's edits sent (a push)

LAYER_KINDS = {"Polygon": "Polygon", "MultiPolygon": "Polygon", "LineString": "Polyline", "MultiLineString": "Polyline",
               "Point": "Point", "MultiPoint": "Point"}  # RustGIS's kind of layer for a geometry
SQL_TYPES = {"Long": "INTEGER", "Double": "REAL", "Text": "TEXT", "Boolean": "INTEGER"}
EXPORT_TYPES = {"Polygon": ("MULTIPOLYGON", MultiPolygon), "Polyline": ("MULTILINESTRING", MultiLineString),
                "Point": ("POINT", None)}


def _reprojected(geometry, source: str, target: str):
    import numpy as np
    from pyproj import Transformer

    to = Transformer.from_crs(source, target, always_xy=True)
    return shapely.transform(geometry, lambda xy: np.column_stack(to.transform(xy[:, 0], xy[:, 1])))


class FakeEngine:
    """RustGIS's project as far as the link goes, in memory, with the rules rustgis-cli 0.4.0 showed: features kept in
    longitude and latitude (pyproj from the system of the layer's file); a script run as one step that keeps nothing
    when a line fails, and read to its 10,000th line at most; a selection that mode "new" clears in every layer; a
    delete that refuses an empty selection; fields added only with names of letters, digits and _; a where clause of
    at most 10,000 characters (evaluated by SQLite: the clauses the link writes are SQL; texts compared in either case
    alike, ASCII only, as RustGIS does); an export that writes polygons and lines as multi-part geometries in the
    layer's own system. ``busy``: the next call of each tool named there (or ``run_command:<id>``) answers "not drawing"
    without running; ``lost``: runs, then its answer is lost (busy, outcome unknown); ``later``: kept, its answer lost,
    and run by :meth:`run_later` (RustGIS draws again); ``hooks``: functions of a tool and its arguments called before
    RustGIS answers (an edit the user makes there meanwhile); ``rules``: functions of a command and its parameters that
    refuse it with their text (a domain of a layer, say); ``cut``: runs only that many lines of a script and says so,
    as RustGIS does past its 10,000th. A GeoPackage table of more than ``stream_above`` features is read in place
    unless ``layer.addData`` says ``"stream": false``, and then refuses every edit, as RustGIS does past 100,000.
    ``layer.addUrl`` reads the first 64 KiB of the address by range, as RustGIS does, and adds a TIFF as a tile layer
    read in place (kind ``cog``, ``tile_layers``, top first: not a layer, in no group), or, with ``whole``, downloads it
    whole as a raster layer, as RustGIS does a GeoTIFF in strips; what it read is in ``fetched``. A tile layer is named
    by its place or by its name (the first of that name, in any case); ``map.setTiles`` (visible, opacity, rename)
    answers with no object (refused as a command, as the transport refuses it; it works in a script), and a removal of
    tile layers by an empty or missing place, which in RustGIS takes away the top tile layer or all of them, fails the
    test; what was removed is in ``removed``. ``shift``: metres east by which it misplaces what it reads (a system it
    does not know). Every call is in ``calls``, every script in ``scripts``."""

    def __init__(self, version: str = "0.4.0", shift: float = 0.0):
        self.version, self.shift = version, shift
        self.layers: list[dict] = []  # top first: id, name, source, table, kind, srs, fields, features, selection
        self.groups: list[dict] = []  # id, name, layers
        self.next_id = 1
        self.status: dict[int, list] = {}  # layer id -> [added, modified, deleted] since the project was saved
        self.calls: list = []
        self.scripts: list[str] = []
        self.busy: list[str] = []
        self.lost: list[str] = []
        self.later: list[str] = []
        self.queued: list = []  # what ``later`` kept, in order
        self.hooks: list = []
        self.rules: list = []
        self.cut: int | None = None
        self.stream_above = 100_000
        self.active: int | None = None  # the layer the last command naming one named: edits go to it
        self.tile_layers: list[dict] = []  # top first: name, url, kind (what layer.addUrl reads in place)
        self.fetched: list = []  # (url, range, status) of each first read of an address
        self.whole = False  # layer.addUrl downloads the file whole, as a raster layer
        self.removed: list[dict] = []  # each tile layer map.removeTiles took away

    def initialize(self) -> str:
        return self.version

    def call(self, tool: str, args: dict) -> dict:
        args = json.loads(json.dumps(args))  # what crosses the wire
        self.calls.append((tool, args))
        names = {tool, f"{tool}:{args.get('command')}"}
        for hook in list(self.hooks):
            hook(tool, args)
        for name in names & set(self.busy):
            self.busy.remove(name)
            raise link.RustGisBusy("RustGIS isn't drawing")
        for name in names & set(self.later):
            self.later.remove(name)
            self.queued.append((tool, args))
            raise link.RustGisBusy("RustGIS took the request and did not answer", outcome_unknown=True)
        answer = self._answer(tool, args)
        for name in names & set(self.lost):
            self.lost.remove(name)
            raise link.RustGisBusy("the answer did not come back", outcome_unknown=True)
        return answer

    def run_later(self) -> None:
        """What ``later`` kept, run in order: RustGIS draws again and answers its queue (nobody hears the answers)."""
        queued, self.queued = self.queued, []
        for tool, args in queued:
            self._answer(tool, args)

    def _answer(self, tool: str, args: dict):
        if tool == "list_layers":
            answer = self.listing()
        elif tool == "read_features":
            layer = self._layer(args["layer"])
            rows = self._where(layer, args["where"]) if args.get("where") else list(range(len(layer["features"])))
            answer = {"layer": layer["name"], "matched": len(rows), "total": len(layer["features"]),
                      "rows": [dict(layer["features"][r]["attrs"], _row=r) for r in rows[:args.get("limit", 20)]]}
        elif tool == "run_command":
            answer = self.command(args["command"], args.get("params") or {})
            if args["command"] == "map.setTiles":  # what it answers is a list: the transport refuses it
                raise link.RustGisRefused("RustGIS answered run_command with JSON that is not an object")
        elif tool == "run":
            answer = self.script(args["script"])
        else:
            raise link.RustGisRefused(f"unknown tool '{tool}'")
        return answer

    # -- the project
    def listing(self) -> dict:
        def group_of(lid):
            return next((g["name"] for g in self.groups if lid in g["layers"]), None)

        return {"layers": [{"id": l["id"], "name": l["name"], "group": group_of(l["id"]), "type": l["kind"],
                            "source": l["source"], "features": 0 if l.get("streamed") else len(l["features"]),
                            "fields": [f"{n} ({k})" for n, k in l["fields"].items()],
                            "visible": l.get("visible", True)} for l in self.layers],
                "groups": [copy.deepcopy(g) for g in self.groups], "unsaved": any(map(any, self.status.values())),
                "tileLayers": [dict(t, index=n) for n, t in enumerate(self.tile_layers)]}

    def layer(self, lid: int) -> dict:
        return next(l for l in self.layers if l["id"] == lid)

    def linked(self, rl, table: str) -> dict:
        """The layer RustGIS has of a table of the link."""
        return self.layer(int(rl.layers[table]))

    def state(self) -> list:
        """Every feature of every layer, as a sorted list: what a send leaves, whatever the order."""
        return sorted((l["id"], json.dumps(f["attrs"], sort_keys=True), f["geometry"].wkb_hex)
                      for l in self.layers for f in l["features"])

    def identities(self, layer: dict) -> list:
        return [f["attrs"].get(ID_FIELD) for f in layer["features"]]

    def native(self, layer: dict, pid) -> list:
        """The geometries of the features of ``layer`` with identity ``pid``, in the layer's own system."""
        return [_reprojected(f["geometry"], "EPSG:4326", f"EPSG:{layer['srs']}") for f in layer["features"]
                if f["attrs"].get(ID_FIELD) == pid]

    # -- what the user does in RustGIS
    def _count(self, layer: dict, kind: int, n: int = 1) -> None:
        self.status.setdefault(layer["id"], [0, 0, 0])[kind] += n

    def draw(self, layer: dict, geometry, crs: str, **attrs) -> None:
        values = {name: attrs.get(name) for name in layer["fields"]}
        layer["features"].append({"geometry": _reprojected(geometry, crs, "EPSG:4326"), "attrs": values})
        self._count(layer, 0)

    def reshape(self, layer: dict, pid, geometry, crs: str) -> None:
        feature = next(f for f in layer["features"] if f["attrs"].get(ID_FIELD) == pid)
        feature["geometry"] = _reprojected(geometry, crs, "EPSG:4326")
        self._count(layer, 1)

    def paste(self, layer: dict, pid) -> None:
        """What edit.copy and edit.paste do to a feature: a copy at the end of its layer, every attribute with it."""
        feature = next(f for f in layer["features"] if f["attrs"].get(ID_FIELD) == pid)
        layer["features"].append(copy.deepcopy(feature))
        self._count(layer, 0)

    def remove(self, lid: int) -> None:
        self.layers = [l for l in self.layers if l["id"] != lid]
        for g in self.groups:
            g["layers"] = [i for i in g["layers"] if i != lid]

    # -- the commands
    def _tile(self, p: dict) -> dict:
        """The tile layer a command names: by its place in the list, or by its name - the first of that name, in any
        case, as RustGIS finds it."""
        if "index" in p:
            if not 0 <= p["index"] < len(self.tile_layers):
                raise link.RustGisRefused("no tile layer at that index")
            return self.tile_layers[p["index"]]
        found = next((t for t in self.tile_layers if t["name"].lower() == str(p.get("name")).lower()), None)
        if found is None:
            raise link.RustGisRefused(f"no tile layer '{p.get('name')}'")
        return found

    def _layer(self, ref) -> dict:
        if isinstance(ref, int):
            found = next((l for l in self.layers if l["id"] == ref), None)
            if found is None:
                raise link.RustGisRefused(f"no layer with id {ref}")
            return found
        found = next((l for l in self.layers if l["name"].lower() == str(ref).lower()), None)
        if found is None and str(ref).isdigit():
            found = next((l for l in self.layers if l["id"] == int(ref)), None)
        if found is None:
            raise link.RustGisRefused(f"no layer named '{ref}'")
        return found

    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id - 1

    def _read(self, path: Path, table: str | None):
        con = sqlite3.connect(path)
        try:
            table = table or con.execute("SELECT table_name FROM gpkg_geometry_columns").fetchone()[0]
            found = con.execute("SELECT geometry_type_name, srs_id FROM gpkg_geometry_columns WHERE table_name = ?",
                                (table,)).fetchone()
            if found is None:
                raise link.RustGisRefused(f"no layer '{table}' in {path.name}")
            info = con.execute(f'PRAGMA table_info("{table}")').fetchall()
            columns = [(r[1], r[2]) for r in info if r[1] not in ("fid", "geom")]
            names = "".join(f', "{c}"' for c, _ in columns)
            rows = con.execute(f'SELECT geom{names} FROM "{table}" ORDER BY fid').fetchall()
        finally:
            con.close()
        gtype, srs = found
        fields = {c: {"INTEGER": "Long", "REAL": "Double"}.get(link._affinity(t), "Text") for c, t in columns}
        features = []
        for row in rows:
            g = affinity.translate(gpkg.geometry(row[0]), xoff=self.shift)
            features.append({"geometry": _reprojected(g, f"EPSG:{srs}", "EPSG:4326"),
                             "attrs": dict(zip([c for c, _ in columns], row[1:]))})
        kind = LAYER_KINDS[{"POLYGON": "Polygon", "MULTIPOLYGON": "Polygon", "LINESTRING": "LineString",
                            "MULTILINESTRING": "LineString", "POINT": "Point"}[gtype]]
        return table, kind, srs, fields, features

    def _where(self, layer: dict, clause: str) -> list:
        if len(clause) > 10_000:
            raise link.RustGisRefused("the expression is too long")
        if clause.count(" AND ") > 255 or clause.count(" OR ") > 255:
            raise link.RustGisRefused("the expression has too many terms")
        names = list(layer["fields"])
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE TABLE t (_row INTEGER" + "".join(f', "{n}" COLLATE NOCASE' for n in names) + ")")
            con.executemany(f"INSERT INTO t VALUES (?{', ?' * len(names)})",
                            [(i, *[f["attrs"].get(n) for n in names]) for i, f in enumerate(layer["features"])])
            try:
                return [r[0] for r in con.execute(f"SELECT _row FROM t WHERE {clause} ORDER BY _row")]
            except sqlite3.Error as exc:
                raise link.RustGisRefused(str(exc)) from None
        finally:
            con.close()

    @staticmethod
    def _coerce(value, kind: str):
        if value is None:
            return None
        try:
            if kind == "Long":
                return int(float(value))
            if kind == "Double":
                return float(value)
        except (TypeError, ValueError):
            return None
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    def command(self, command: str, p: dict):
        for rule in self.rules:
            said = rule(command, p)
            if said:
                raise link.RustGisRefused(said)
        if command.startswith(("edit.", "select.")) and p.get("layer") is not None:
            self.active = self._layer(p["layer"])["id"]
        label = {"edit.addFeature": "Create Feature", "edit.delete": "Delete",
                 "edit.addField": "Add Field"}.get(command)
        active = next((l for l in self.layers if l["id"] == self.active), None)
        if label and active is not None and active.get("streamed"):
            raise link.RustGisRefused(f"{label}: {active['name']} is read in place from its file; Load All Features "
                                      "(layer.loadAll) to edit it")
        if command == "layer.addData":
            path = Path(p["path"])
            if path.suffix.lower() == ".tif":
                table, kind, srs, fields, features = None, "Raster", None, {}, []
            else:
                table, kind, srs, fields, features = self._read(path, p.get("layer"))
            streamed = bool(p["stream"]) if "stream" in p else table is not None and len(features) > self.stream_above
            layer = {"id": self._new_id(), "name": p.get("name") or table, "source": str(path), "table": table,
                     "kind": kind, "srs": srs, "fields": fields, "features": features, "selection": set(),
                     "streamed": streamed}
            self.layers.insert(0, layer)
            return {"layersAdded": [layer["name"]], "result": {"features": len(features), "layer": layer["id"],
                                                               "layers": [layer["id"]], "streamed": streamed}}
        if command == "layer.addUrl":  # a tiled GeoTIFF by its address: its header read by range, then a tile layer
            url = p["url"]
            parts = urllib.parse.urlsplit(url)
            asked = None if self.whole else "bytes=0-65535"
            connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
            try:
                connection.request("GET", parts.path, headers={"Range": asked} if asked else {})
                reply = connection.getresponse()
                head = reply.read()
            finally:
                connection.close()
            self.fetched.append((url, asked, reply.status))
            if reply.status not in (200, 206):
                raise link.RustGisRefused(f"{url}: http status: {reply.status}")
            if head[:4] not in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
                raise link.RustGisRefused(f"{url}: GeoTIFF: format error: TIFF signature not found")
            name = p.get("name") or parts.path.rsplit("/", 1)[-1]
            if self.whole:
                layer = {"id": self._new_id(), "name": name, "source": parts.path.rsplit("/", 1)[-1], "table": None,
                         "kind": "Raster", "srs": None, "fields": {}, "features": [], "selection": set()}
                self.layers.insert(0, layer)
                return {"layersAdded": [name], "result": {"bands": 3, "layer": layer["id"]}}
            self.tile_layers.insert(0, {"name": name, "url": url, "kind": "cog"})
            return {"bounds": [0.0, 0.0, 0.0, 0.0], "inPlace": True, "index": 0, "kind": "cog", "name": name}
        if command == "map.removeTiles":
            if not ({"index", "name"} & set(p)) or p.get("id") == "" or ("index" in p and (
                    isinstance(p["index"], bool) or not isinstance(p["index"], int) or p["index"] < 0)):
                raise AssertionError(f"map.removeTiles {p}: RustGIS takes away the top tile layer or all of them")
            self.removed.append(self._tile(p))
            self.tile_layers.remove(self.removed[-1])
            return {"removed": [self.removed[-1]["name"]]}
        if command == "map.setTiles":
            tile = self._tile(p)
            tile.update({k: p[k] for k in ("visible", "opacity") if k in p})
            if "rename" in p:
                tile["name"] = p["rename"]
            return [dict(t, index=n) for n, t in enumerate(self.tile_layers)]
        if command == "layer.visibility":
            refs = p["layer"] if isinstance(p["layer"], list) else [p["layer"]]
            for ref in refs:
                self._layer(ref)["visible"] = bool(p.get("visible", True))
            return {"ok": True}
        if command == "layer.transparency":
            self._layer(p["layer"])["transparency"] = p["value"]
            return {"ok": True}
        if command == "layer.remove":
            refs = p["layer"] if isinstance(p["layer"], list) else [p["layer"]]
            for ref in refs:
                self.remove(self._layer(ref)["id"])
            return None
        if command == "group.add":
            group = {"id": self._new_id(), "name": p.get("name") or "New Group", "layers": []}
            for ref in p.get("layers") or []:
                lid = self._layer(ref)["id"]
                for g in self.groups:
                    g["layers"] = [i for i in g["layers"] if i != lid]
                group["layers"].append(lid)
            self.groups.append(group)
            return {"group": group["id"]}
        if command == "layer.group":  # with "before": just above that layer, the group where it is
            layer = self._layer(p["layer"])
            lid = layer["id"]
            group = next((g for g in self.groups if p["group"] in (g["id"], g["name"])), None)
            if group is None:
                raise link.RustGisRefused(f"no group '{p['group']}'")
            for g in self.groups:
                g["layers"] = [i for i in g["layers"] if i != lid]
            group["layers"].append(lid)
            if p.get("before") is not None:
                before = self._layer(p["before"])
                self.layers.remove(layer)
                self.layers.insert(self.layers.index(before), layer)
            return {"group": group["id"], "layer": lid}
        if command == "layer.move":  # one layer to a place of the whole list, top first, never out of its group
            (layer,) = [self._layer(ref) for ref in (p["layer"] if isinstance(p["layer"], list) else [p["layer"]])]
            order = [l["id"] for l in self.layers]
            to = min(max(0, int(p["to"])), len(order) - 1)
            group = next((g for g in self.groups if layer["id"] in g["layers"]), None)
            places = [order.index(i) for i in group["layers"] if i in order] if group is not None else []
            if places and not min(places) <= to <= max(places):
                raise link.RustGisRefused("the layer can't move out of its group (take it out of the group first)")
            if order.index(layer["id"]) == to:
                raise link.RustGisRefused("the layer is already there")
            self.layers.remove(layer)
            self.layers.insert(to, layer)
            return {"ok": True}
        if command == "group.remove":  # the group and its layers
            group = next((g for g in self.groups if p["group"] in (g["id"], g["name"])), None)
            if group is None:
                raise link.RustGisRefused(f"no group '{p['group']}'")
            for lid in list(group["layers"]):
                self.remove(lid)
            self.groups.remove(group)
            return {"ok": True}
        if command == "select.byAttributes":
            layer = self._layer(p.get("layer"))
            rows = set(self._where(layer, p["where"]))
            mode = p.get("mode", "new")
            if mode == "new":
                for other in self.layers:
                    other["selection"] = set()
                layer["selection"] = rows
            elif mode == "add":
                layer["selection"] |= rows
            elif mode == "remove":
                layer["selection"] -= rows
            else:
                layer["selection"] &= rows
            return {"selected": len(rows)}
        if command == "select.clear":
            if not any(l["selection"] for l in self.layers):
                raise link.RustGisRefused("Clear: nothing is selected")
            for layer in self.layers:
                layer["selection"] = set()
            return None
        if command == "edit.delete":
            chosen = [l for l in self.layers if l["selection"]]
            if not chosen:
                raise link.RustGisRefused("Delete: nothing is selected")
            n = 0
            for layer in chosen:
                for r in sorted(layer["selection"], reverse=True):
                    del layer["features"][r]
                    n += 1
                self._count(layer, 2, len(layer["selection"]))
                layer["selection"] = set()
            return {"deleted": n}
        if command == "edit.addFeature":
            layer = self._layer(p.get("layer"))
            g = shapely.geometry.shape(p["geometry"])
            if LAYER_KINDS[g.geom_type] != layer["kind"]:
                raise link.RustGisRefused(f"{g.geom_type} can't go in a {layer['kind']} layer")
            given = p.get("attributes") or {}
            layer["features"].append({"geometry": g, "attrs": {n: self._coerce(given.get(n), k)
                                                               for n, k in layer["fields"].items()}})
            layer["selection"] = {len(layer["features"]) - 1}
            self._count(layer, 0)
            return {"row": len(layer["features"]) - 1}
        if command == "edit.addField":
            layer, name = self._layer(p.get("layer")), p["name"]
            if not (0 < len(name) <= 64 and all(c.isalnum() or c == "_" for c in name)):
                raise link.RustGisRefused("field names use letters, digits and _ (at most 64)")
            if name.lower() in {n.lower() for n in layer["fields"]}:
                raise link.RustGisRefused(f"a field named '{name}' already exists")
            layer["fields"][name] = {"long": "Long", "double": "Double", "text": "Text"}[p.get("type", "double")]
            for feature in layer["features"]:
                feature["attrs"][name] = None
            return None
        if command == "edit.status":
            return {"layers": [{"layer": l["name"], "added": c[0], "modified": c[1], "deleted": c[2]}
                               for l in self.layers if any(c := self.status.get(l["id"], [0, 0, 0]))],
                    "unsaved": any(map(any, self.status.values()))}
        if command == "share.exportLayer":
            layer, path = self._layer(p.get("layer")), Path(p["path"])
            if any(Path(f"{path}{suffix}").exists() for suffix in ("", "-wal", "-shm", "-journal")):
                raise link.RustGisRefused(f"i/o: GeoPackage export cannot replace {path}")
            if path.suffix == ".geojson":
                features = [{"type": "Feature", "geometry": shapely.geometry.mapping(f["geometry"]),
                             "properties": f["attrs"]} for f in layer["features"]]
                path.write_text(json.dumps({"type": "FeatureCollection", "name": layer["name"], "features": features}))
            else:
                gtype, multi = EXPORT_TYPES[layer["kind"]]
                rows = []
                for f in layer["features"]:
                    g = _reprojected(f["geometry"], "EPSG:4326", f"EPSG:{layer['srs']}")
                    rows.append([multi([g]) if multi and not g.geom_type.startswith("Multi") else g, f["attrs"]])
                _exported(path, layer["name"], gtype, {n: SQL_TYPES[k] for n, k in layer["fields"].items()}, rows,
                          crs=f"EPSG:{layer['srs']}")
            return {"crs": f"EPSG:{layer['srs']}", "features": len(layer["features"]), "leftOut": 0, "path": str(path)}
        raise link.RustGisRefused(f"unknown command '{command}'; find ids with search_commands")

    def script(self, text: str) -> dict:
        self.scripts.append(text)
        lines = text.split("\n")
        assert len(lines) <= 10_000, "RustGIS reads no further than the 10,000th line, and says nothing"
        lines = lines if self.cut is None else lines[:self.cut]
        saved = copy.deepcopy((self.layers, self.groups, self.next_id, self.status, self.tile_layers, self.removed))
        result, ran = None, 0
        try:
            for number, line in enumerate(lines, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                command, _, rest = line.partition(" ")
                try:
                    result = self.command(command, json.loads(rest) if rest.strip() else {})
                except link.RustGisRefused as exc:
                    raise link.RustGisRefused(f"line {number}: {command}: {exc}") from None
                ran += 1
        except link.RustGisRefused as exc:
            self._restore(saved)
            raise link.RustGisRefused(json.dumps({"duration_s": 0.0, "error": str(exc), "layersAdded": [], "lines": 0,
                                                  "notices": [], "result": None, "success": False})) from None
        return {"duration_s": 0.0, "error": None, "layersAdded": [], "lines": ran, "notices": [], "result": result,
                "success": True}


    def _restore(self, saved) -> None:
        """The project as it was before a script that failed, its layers the same objects (a test holds them)."""
        layers, self.groups, self.next_id, self.status, self.tile_layers, self.removed = saved
        now = {layer["id"]: layer for layer in self.layers}
        for n, before in enumerate(layers):
            layer = now.get(before["id"])
            if layer is not None:
                layer.clear()
                layer.update(before)
                layers[n] = layer
        self.layers = layers


def _commands(script: str) -> list:
    """The lines of a script as ``(command, params)``."""
    out = []
    for line in script.split("\n"):
        command, _, rest = line.partition(" ")
        out.append((command, json.loads(rest) if rest.strip() else {}))
    return out


def _sent(fake: FakeEngine) -> list:
    """The scripts the program sent, as their commands."""
    return [_commands(args["script"]) for tool, args in fake.calls if tool == "run"]


def _spot(z, x, y, **values):
    return {"quota_q": z, "us_q": "1", ID_FIELD: new_id(), "_geom": ("Point", [x, y]), **values}


def _taken(feature, **values):
    """A unit as the window writes it once it took one from the live GeoPackage (a Feature of read_changes): its
    identity and its shape (``_shape``: holes and parts as they are; LiveLink writes a feature only with a
    ``_geom``)."""
    coords = shapely.get_coordinates(feature.geometry).tolist()
    return {**values, ID_FIELD: feature.id, "_geom": ("Polygon", coords), "_shape": feature.geometry}


def _started(tmp_path, drawings, crs=GB, **options):
    """A live GeoPackage the program wrote, and the link started on a fake RustGIS; ``calls`` starts empty."""
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(drawings)
    assert live.read_changes() == {}
    fake = FakeEngine(**options)
    rl = link.RustGisLink(live, fake, "Saggio 1", crs)
    rl.start([])
    fake.calls.clear()
    return live, fake, rl


def test_start_adds_the_tables_in_a_group(tmp_path):
    """The system is checked first (a point RustGIS reads, exports in longitude and latitude and removes, in one step);
    then every table of the live GeoPackage, empty ones included, becomes a layer named as in QGIS, in a group named
    after the chunk, and what RustGIS loaded is remembered from its own export. A local frame is refused before RustGIS
    is asked anything."""
    one, spot = _unit(1, GX, GY), _spot(99.25, GX + 0.2, GY + 0.2)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([one], [spot]))
    fake = FakeEngine()
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    assert rl.start([]) == {"layers": 7, "rasters": 0, "version": "0.4.0"}
    commands = [(args["command"], args["params"]) for tool, args in fake.calls if tool == "run_command"]
    assert [params for command, params in commands if command == "layer.addData"] == [  # loaded whole, never streamed
        {"path": str(live.path), "layer": table, "name": title, "stream": False}
        for table, (title, _) in LIVE_TABLES.items()]
    (group,) = [params for command, params in commands if command == "group.add"]
    assert group == {"name": "Saggio 1", "layers": [int(rl.layers[t]) for t in LIVE_TABLES]}
    assert fake.groups == [{"id": fake.groups[0]["id"], "name": "Saggio 1", "layers": group["layers"]}]
    # the system: checked before any table, the point gone again
    assert fake.calls[0][0] == "run" and [c for c, _ in _commands(fake.scripts[0])] == [
        "layer.addData", "share.exportLayer", "layer.remove"]
    assert [l["name"] for l in fake.layers] == [title for title, _ in reversed(LIVE_TABLES.values())]
    # empty tables too, with their columns and their system
    sections = fake.linked(rl, "sezioni")
    assert sections["features"] == [] and sections["srs"] == 3004 and "lunghezza_m" in sections["fields"]
    # what RustGIS has, remembered: from its export for the two-way tables, from the file for the others
    assert list(rl.baseline["unita"]) == [(one[ID_FIELD], 1)] and list(rl.baseline["quote"]) == [(spot[ID_FIELD], 1)]
    assert rl.baseline["sezioni"] == {} and rl.baseline["curve"] == {}
    assert not rl.pending and not rl.status_changed()
    # a local frame: refused, RustGIS never asked
    local = LiveLink(tmp_path / "locale", "LOCAL")
    local.write(_drawings([one]))
    fresh = FakeEngine()
    with pytest.raises(link.RustGisRefused, match="LOCAL"):
        link.RustGisLink(local, fresh, "Saggio 1", "LOCAL").start([])
    assert fresh.calls == []


def test_a_system_rustgis_places_elsewhere_is_refused(tmp_path):
    """RustGIS takes a system it cannot convert (a Cassini or polyconic projection) and places it elsewhere: the point
    that comes back is compared with pyproj's, and the link refuses naming the system. Nothing is left in RustGIS."""
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine(shift=0.002)  # two millimetres east: more than the tolerance
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    with pytest.raises(link.RustGisRefused, match="EPSG:3004"):
        rl.start([])
    assert fake.layers == [] and rl.layers == {} and not rl.pending
    assert not [args for tool, args in fake.calls if tool == "run_command"]
    fake.shift = 0.0005  # half a millimetre (a datum shift, measured 0.46 mm on Monte Mario): taken
    assert rl.start([])["layers"] == 7


def test_push_sends_only_what_the_program_changed(tmp_path):
    """The program moves unit 1 and adds a spot height: one script, where each layer deletes by identity what it is
    about to add (ruling 1: a send whose answer was lost may run again), so a select/delete pair per layer - after a
    placeholder feature that makes the selection never empty (RustGIS refuses to delete nothing) - and the two rows,
    their geometry in longitude and latitude as pyproj gives it. Unit 2 is never named; a second push sends nothing."""
    import numpy as np

    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    units = fake.linked(rl, "unita")
    untouched = units["features"][1]
    moved, spot = dict(one, _geom=_square(GX + 0.05, GY)), _spot(99.25, GX + 0.2, GY + 0.2, data="2026-10-28")
    live.write(_drawings([moved, two], [spot]))
    assert rl.pending
    assert rl.push() == 2
    # the layers, the status right before the script, the script: what RustGIS has is never read again (no remember)
    assert [tool for tool, _ in fake.calls] == ["list_layers", "run_command", "run"]
    (script,) = _sent(fake)
    unita, quote = int(rl.layers["unita"]), int(rl.layers["quote"])
    assert [(c, p.get("layer")) for c, p in script] == [
        ("edit.addFeature", unita), ("select.byAttributes", unita), ("edit.delete", None), ("edit.addFeature", unita),
        ("edit.addFeature", quote), ("select.byAttributes", quote), ("edit.delete", None), ("edit.addFeature", quote),
        ("select.clear", None), ("edit.status", None)]
    for block, pid in ((script[:4], one[ID_FIELD]), (script[4:8], spot[ID_FIELD])):
        placeholder, select, _, row = (p for _, p in block)
        assert placeholder["attributes"][ID_FIELD].startswith("ppg-invio-")  # taken by the LIKE, with any leftover
        assert select["mode"] == "new" and select["where"] == (
            f"\"ppg_id\" LIKE 'ppg-invio-%' OR \"ppg_id\" IN ('{pid}')")
        assert row["attributes"][ID_FIELD] == pid
    assert two[ID_FIELD] not in json.dumps(script)
    for (_, row), shape in ((script[3], Polygon(moved["_geom"][1])), (script[7], Point(spot["_geom"][1]))):
        expected = shapely.get_coordinates(shapely.geometry.shape(_lonlat(shape, GB)))
        assert np.abs(shapely.get_coordinates(shapely.geometry.shape(row["geometry"])) - expected).max() <= 1e-9
    assert script[3][1]["attributes"]["us_s"] == "1" and script[7][1]["attributes"]["data"] == "2026-10-28"
    # RustGIS has it: unit 1 moved, unit 2 the feature it was, the spot height once, no placeholder left
    assert units["features"][0] is untouched and fake.identities(units) == [two[ID_FIELD], one[ID_FIELD]]
    (there,) = fake.native(units, one[ID_FIELD])
    assert link._same_geometry(there, Polygon(moved["_geom"][1]))
    assert fake.identities(fake.linked(rl, "quote")) == [spot[ID_FIELD]]
    assert not rl.pending
    fake.calls.clear()
    assert rl.push() == 0 and _sent(fake) == [] and not rl.pending


def test_layers_are_named_by_number_and_checked(tmp_path):
    """A text is a layer's NAME first for RustGIS: a layer of the user named as a linked layer's id is never touched
    (ids go as numbers). Another project open in RustGIS has its own layers under the same ids: the push refuses,
    and sends nothing to them."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    decoy = dict(copy.deepcopy(units), id=999, name=rl.layers["unita"], source=str(tmp_path / "mine.gpkg"))
    fake.layers.insert(0, decoy)
    live.write(_drawings([dict(one, _geom=_square(GX + 0.1, GY))]))
    assert rl.push() == 1
    assert fake.state() != [] and decoy["features"] and decoy["features"][0]["attrs"]["us_s"] == "1"
    (there,) = fake.native(decoy, one[ID_FIELD])  # the user's layer: as it was
    assert link._same_geometry(there, Polygon(one["_geom"][1]))
    (there,) = fake.native(units, one[ID_FIELD])
    assert link._same_geometry(there, Polygon(_square(GX + 0.1, GY)[1]))
    for layer in fake.layers:  # another project, its layers under the same ids
        layer["source"] = str(tmp_path / "altro progetto.gpkg")
    state = fake.state()
    live.write(_drawings([one]))
    fake.calls.clear()
    with pytest.raises(link.RustGisRefused, match="another project"):
        rl.push()
    assert fake.state() == state and [tool for tool, _ in fake.calls] == ["list_layers"] and rl.pending


def test_what_came_from_rustgis_is_not_sent_back(tmp_path):
    """A unit moved in RustGIS, pulled and applied by the program, is never sent back. A unit drawn there gets its
    identity in the next send - the feature without one replaced by it, found by a where clause on its values -
    once."""
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    units = fake.linked(rl, "unita")
    fake.reshape(units, one[ID_FIELD], box(GX + 0.05, GY, GX + 0.55, GY + 0.5), GB)
    assert rl.pull()["unita"] == (1, 0, 0)
    (changed,) = live.read_changes()["unita"].changed
    one = dict(one, _shape=changed.geometry)  # the window takes it and writes its drawings again
    live.write(_drawings([one, two]))
    assert rl.push() == 0 and _sent(fake) == []
    fake.draw(units, box(GX + 3, GY, GX + 3.5, GY + 0.5), GB, us_s="12")
    assert rl.pull()["unita"] == (0, 1, 0)
    (drawn,) = live.read_changes()["unita"].added
    twelve = _taken(drawn, etichetta="US 12", us_s="12")
    live.write(_drawings([one, two, twelve]))
    assert rl.push() == 1
    (script,) = _sent(fake)
    adds = [p for c, p in script if c == "edit.addFeature" and p["attributes"].get(ID_FIELD) == drawn.id]
    assert len(adds) == 1 and adds[0]["attributes"]["etichetta"] == "US 12"
    selects = [p["where"] for c, p in script if c == "select.byAttributes"]
    assert any(where.startswith('"ppg_id" IS NULL AND ') and "'12'" in where for where in selects)
    assert sorted(fake.identities(units)) == sorted([one[ID_FIELD], two[ID_FIELD], drawn.id])  # none without identity
    fake.calls.clear()
    assert rl.push() == 0 and _sent(fake) == []
    assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}


def test_busy_keeps_the_rows_pending(tmp_path):
    """RustGIS does not draw (-32004): nothing is lost, the rows stay pending and the next push sends them, once. An
    answer lost after RustGIS ran the send (the outcome unknown): sent again, and RustGIS still has each row once."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    live.write(_drawings([dict(one, _geom=_square(GX + 0.2, GY))]))
    before, state = copy.deepcopy(rl.baseline), fake.state()
    fake.busy.append("run")
    with pytest.raises(link.RustGisBusy):
        rl.push()
    assert rl.pending and fake.state() == state
    assert {k: v[1] for k, v in rl.baseline["unita"].items()} == {k: v[1] for k, v in before["unita"].items()}
    assert rl.push() == 1 and not rl.pending
    first, second = (s for s in _sent(fake))
    assert [c for c, _ in first] == [c for c, _ in second]  # the same rows: refused once, sent once
    assert fake.identities(units) == [one[ID_FIELD]]
    live.write(_drawings([dict(one, _geom=_square(GX + 0.4, GY))]))
    fake.lost.append("run")
    with pytest.raises(link.RustGisBusy) as lost:
        rl.push()
    assert lost.value.outcome_unknown and rl.pending
    assert rl.push() == 1 and not rl.pending  # sent again: it deletes by identity what it adds
    assert fake.identities(units) == [one[ID_FIELD]]
    (there,) = fake.native(units, one[ID_FIELD])
    assert link._same_geometry(there, Polygon(_square(GX + 0.4, GY)[1]))


def test_a_script_delivered_twice_leaves_the_same_state(tmp_path):
    """Ruling 1: a send must leave the same state when RustGIS runs it twice. A send with every kind of change - a unit
    moved, one deleted, a part added to one, a spot height added, a section changed, a unit drawn in RustGIS given its
    identity, another drawn there left as it was - run a second time by RustGIS: nothing twice, nothing lost."""
    one, two, three = _unit(1, GX, GY), _unit(2, GX + 1, GY), _unit(3, GX + 2, GY)
    section = {"etichetta": "Sezione 1", ID_FIELD: new_id(), "_geom": ("LineString", [[GX, GY + 2], [GX + 2, GY + 2]])}
    live, fake, rl = _started(tmp_path, _drawings([one, two, three], sections=[section]))
    units = fake.linked(rl, "unita")
    fake.draw(units, box(GX + 5, GY, GX + 5.5, GY + 0.5), GB, us_s="12")
    assert rl.pull()["unita"] == (0, 1, 0)
    (drawn,) = live.read_changes()["unita"].added
    fake.draw(units, box(GX + 7, GY, GX + 7.5, GY + 0.5), GB, us_s="12")  # the same values, not read yet
    assert rl.pull()["unita"] == (0, 1, 0)
    twelve = _taken(drawn, us_s="12")
    part = dict(three, _geom=_square(GX + 2, GY + 3))
    live.write(_drawings([dict(one, _geom=_square(GX + 0.1, GY)), three, part, twelve],
                         [_spot(98.5, GX + 0.3, GY + 0.3)],
                         [dict(section, _geom=("LineString", [[GX, GY + 2], [GX + 3, GY + 2.5]]))]))
    assert rl.push() == 7  # 6 rows added (unit 3 has two parts now), unit 2 gone
    once = fake.state()
    assert fake.identities(units).count(None) == 1 and fake.identities(units).count(three[ID_FIELD]) == 2
    (script,) = [args["script"] for tool, args in fake.calls if tool == "run"]
    fake.call("run", {"script": script})  # RustGIS runs it again
    assert fake.state() == once


def test_a_removed_layer_is_added_again_once(tmp_path):
    """The user removes the layer of the units in RustGIS: the next push adds it again - once, from the live GeoPackage,
    so its rows are there whole and none is sent - into the chunk's group; the next push adds nothing. When RustGIS
    added it but its answer was lost, the next push takes that layer: never a second one."""
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    title = LIVE_TABLES["unita"][0]
    old = rl.layers["unita"]
    fake.remove(int(old))
    live.write(_drawings([dict(one, _geom=_square(GX + 0.2, GY)), two]))
    assert rl.push() == 0 and _sent(fake) == []
    adds = [args["params"] for tool, args in fake.calls if tool == "run_command" and args["command"] == "layer.addData"]
    assert adds == [{"path": str(live.path), "layer": "unita", "name": title, "stream": False}]
    assert rl.layers["unita"] != old and int(rl.layers["unita"]) in fake.groups[0]["layers"]
    units = fake.linked(rl, "unita")
    assert sorted(fake.identities(units)) == sorted([one[ID_FIELD], two[ID_FIELD]])
    (there,) = fake.native(units, one[ID_FIELD])
    assert link._same_geometry(there, Polygon(_square(GX + 0.2, GY)[1]))
    fake.calls.clear()
    assert rl.push() == 0 and not [a for t, a in fake.calls if t == "run_command" and a["command"] == "layer.addData"]
    # added, the answer lost: the next push finds it
    fake.remove(int(rl.layers["unita"]))
    fake.lost.append("run_command:layer.addData")
    with pytest.raises(link.RustGisBusy):
        rl.push()
    assert rl.push() == 0
    assert [l["name"] for l in fake.layers].count(title) == 1 and rl.layers["unita"] == str(fake.layers[0]["id"])
    assert len([a for t, a in fake.calls if t == "run_command" and a["command"] == "layer.addData"]) == 1
    live.write(_drawings([one, two]))
    assert rl.push() == 1  # and it is linked: what the program changes goes there
    (there,) = fake.native(fake.linked(rl, "unita"), one[ID_FIELD])
    assert link._same_geometry(there, Polygon(one["_geom"][1]))


def test_every_part_of_a_unit_goes_with_it(tmp_path):
    """Ruling 3: in RustGIS a polygon is added to unit 1's feature (one feature of two polygons, two parts here). The
    program then changes part 1 only: the send deletes the feature by identity and adds both parts - part 2 is not
    lost."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    feature = units["features"][0]
    there = box(GX + 3, GY, GX + 3.5, GY + 0.5)
    feature["geometry"] = MultiPolygon([feature["geometry"], _reprojected(there, GB, "EPSG:4326")])
    assert rl.pull()["unita"] == (0, 1, 0)
    got = live.read_changes()["unita"]
    assert [(f.id, f.part) for f in got.changed] == [(one[ID_FIELD], 2)]
    second = dict(one, _shape=got.changed[0].geometry)
    live.write(_drawings([one, second]))  # the window takes the part
    assert rl.push() == 0
    live.write(_drawings([dict(one, _geom=_square(GX, GY, 0.6)), second]))
    assert rl.push() == 2
    parts = sorted(fake.native(units, one[ID_FIELD]), key=lambda g: g.bounds[0])
    assert len(parts) == 2 and link._same_geometry(parts[1], there)
    assert link._same_geometry(parts[0], Polygon(_square(GX, GY, 0.6)[1]))
    assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}


def test_what_was_drawn_there_and_deleted_here_is_deleted_there(tmp_path):
    """Ruling 4: a unit drawn in RustGIS, brought into the program and deleted there before it went back: the next send
    deletes it in RustGIS (found by its values), and nothing comes back at the next pull."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    fake.draw(units, box(GX + 3, GY, GX + 3.5, GY + 0.5), GB, us_s="12", tipo_us_s=TEXT)
    assert rl.pull()["unita"] == (0, 1, 0)
    (drawn,) = live.read_changes()["unita"].added
    live.write(_drawings([one, _taken(drawn, us_s="12")]))  # the window takes it
    live.write(_drawings([one]))  # and the user deletes it there, before the next send
    assert rl.push() == 1
    assert fake.identities(units) == [one[ID_FIELD]]
    assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}


def test_a_feature_drawn_there_with_the_same_values_is_never_deleted(tmp_path):
    """Two units drawn in RustGIS with the same values: the first came into the program and has its identity, the
    second was drawn after that pull. The where clause that finds the first would find both: the send waits (the rows
    stay pending) and the second is not touched; after the next pull the first is replaced and the second put back as
    it was, until it has its identity too."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    first, second = box(GX + 3, GY, GX + 3.5, GY + 0.5), box(GX + 5, GY, GX + 5.5, GY + 0.5)
    fake.draw(units, first, GB, us_s="7")
    assert rl.pull()["unita"] == (0, 1, 0)
    (a,) = live.read_changes()["unita"].added
    seven = _taken(a, us_s="7")
    live.write(_drawings([one, seven]))
    fake.draw(units, second, GB, us_s="7")
    assert rl.push() == 0 and rl.pending and _sent(fake) == []
    assert fake.identities(units) == [one[ID_FIELD], None, None]
    assert rl.pull()["unita"] == (0, 1, 0)
    assert rl.push() == 1
    assert sorted(map(str, fake.identities(units))) == sorted([one[ID_FIELD], a.id, "None"])
    (kept,) = [f for f in units["features"] if f["attrs"][ID_FIELD] is None]
    assert link._same_geometry(_reprojected(kept["geometry"], "EPSG:4326", GB), second)
    (b,) = live.read_changes()["unita"].added
    live.write(_drawings([one, seven, _taken(b, us_s="7")]))
    assert rl.push() == 1 and not rl.pending
    assert sorted(fake.identities(units)) == sorted([one[ID_FIELD], a.id, b.id])


def test_large_sends_fit_what_rustgis_reads(tmp_path, monkeypatch):
    """Three hundred spot heights: their identities in several where clauses (RustGIS reads at most 10,000 characters
    of one), all of them deleted and added once. A script that would be longer than RustGIS reads is cut between two
    features: the rest stays pending and goes with the next push."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    spots = [_spot(99.0 + n / 1000, GX + n * 0.01, GY) for n in range(300)]
    live.write(_drawings([one], spots))
    assert rl.push() == 300
    (script,) = _sent(fake)
    selects = [p for c, p in script if c == "select.byAttributes"]
    assert len(selects) > 1 and all(len(p["where"]) <= link.WHERE_CHARS for p in selects)
    assert [p["mode"] for p in selects] == ["new"] + ["add"] * (len(selects) - 1)
    heights = fake.linked(rl, "quote")
    assert sorted(fake.identities(heights)) == sorted(s[ID_FIELD] for s in spots)
    monkeypatch.setattr(link, "SCRIPT_LINES", 60)
    moved = [dict(s, _geom=("Point", [s["_geom"][1][0], GY + 1])) for s in spots]
    live.write(_drawings([one], moved))
    fake.calls.clear()
    sent = 0
    while rl.pending:
        sent += rl.push()
    scripts = _sent(fake)
    assert sent == 300 and len(scripts) > 4 and all(len(s) <= 60 for s in scripts)
    assert sorted(fake.identities(heights)) == sorted(s[ID_FIELD] for s in spots)
    assert all(g.y > GY + 0.999 for pid in fake.identities(heights) for g in fake.native(heights, pid))


def test_a_column_the_program_adds_becomes_a_field_there(tmp_path):
    """A unit written with a column RustGIS's layer does not have: the send adds the field first, then the row with
    its value; the next pull and push find nothing to do."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    live.write(_drawings([dict(one, nota="campione", spessore=0.25)]))
    assert rl.push() == 1
    (script,) = _sent(fake)
    fields = [p for c, p in script if c == "edit.addField"]
    assert sorted((p["name"], p["type"]) for p in fields) == [("nota", "text"), ("spessore", "text")]
    assert script.index(("edit.addField", fields[0])) < [c for c, _ in script].index("select.byAttributes")
    units = fake.linked(rl, "unita")
    assert units["features"][0]["attrs"]["nota"] == "campione"
    assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}
    fake.calls.clear()
    assert rl.push() == 0 and _sent(fake) == []


def test_one_way_tables_follow_the_program(tmp_path):
    """Contour lines computed again by the program go to RustGIS (the old ones deleted, the new ones added); an edit
    made there to a contour line is not the program's business: no send touches it until the program changes it."""
    curves = [{"quota": 99.0 + n / 10, "_geom": ("LineString", [[GX, GY + n], [GX + 5, GY + n]])} for n in range(3)]
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one], curves=curves))
    contours = fake.linked(rl, "curve")
    assert len(contours["features"]) == 3
    again = [{"quota": 99.0 + n / 10, "_geom": ("LineString", [[GX, GY + n + 0.5], [GX + 5, GY + n + 0.5]])}
             for n in range(4)]
    live.write(_drawings([one], curves=again))
    assert rl.push() == 7  # three gone, four added
    assert len(contours["features"]) == 4
    assert sorted(round(f["attrs"]["quota"], 1) for f in contours["features"]) == [99.0, 99.1, 99.2, 99.3]
    pid = contours["features"][0]["attrs"][ID_FIELD]
    fake.reshape(contours, pid, shapely.geometry.LineString([(GX, GY), (GX + 1, GY + 1)]), GB)
    state = fake.state()
    fake.calls.clear()
    assert rl.push() == 0 and _sent(fake) == [] and fake.state() == state


def test_status_changed(tmp_path):
    """RustGIS's edit status, compared with the last look: an edit made there changes it, and it stays changed - the
    question changes nothing - until a pull has read the edit; the program's own sends do not change it (each takes the
    status it leaves, read right before it runs)."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    assert not rl.status_changed()
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY, GX + 3.5, GY + 0.5), GB, us_s="12")
    assert rl.status_changed() and rl.status_changed()
    assert rl.pull()["unita"] == (0, 1, 0)
    assert not rl.status_changed()
    live.write(_drawings([dict(one, us_s="10")]))
    assert rl.push() == 1
    assert not rl.status_changed()
    asked = [args["command"] for tool, args in fake.calls if tool == "run_command"]
    assert [c for c in asked if c != "share.exportLayer"] == ["edit.status"] * 7  # 5 questions, the pull, the send


def test_a_pull_that_fails_leaves_the_status_changed(tmp_path):
    """OOS3: a unit moved in RustGIS, and a pull that fails (RustGIS busy in the export): status_changed() is still
    true, so the window pulls again, and false once a pull has read the edit."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    fake.reshape(fake.linked(rl, "unita"), one[ID_FIELD], box(GX + 0.3, GY, GX + 0.8, GY + 0.5), GB)
    assert rl.status_changed()
    fake.busy.append("run_command:share.exportLayer")
    with pytest.raises(link.RustGisBusy):
        rl.pull()
    assert rl.status_changed() and live.read_changes() == {}
    assert rl.pull()["unita"] == (1, 0, 0)
    assert not rl.status_changed()


def test_a_start_that_fails_leaves_the_link_stopped(tmp_path):
    """Ruling 5: what RustGIS loaded is remembered right after it loaded it, or not at all - RustGIS busy then: the
    start fails as busy, the link stays stopped (the caller starts it again later) and what RustGIS added stays there,
    since it does not answer. RustGIS refusing a step: the layers the start added are taken out again."""
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine()
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    fake.busy.append("run_command:share.exportLayer")
    with pytest.raises(link.RustGisBusy):
        rl.start([])
    assert rl.layers == {} and rl.baseline == {} and not rl.pending
    assert len(fake.layers) == 7 and not [a for t, a in fake.calls if a.get("command") == "layer.remove"]
    fake.layers, fake.groups = [], []
    real = fake.command

    def command(name, params):
        if name == "group.add":
            raise link.RustGisRefused("no groups today")
        return real(name, params)

    fake.command = command
    with pytest.raises(link.RustGisRefused, match="no groups"):
        rl.start([])
    assert rl.layers == {} and fake.layers == []


def test_an_export_without_identities_is_refused(tmp_path):
    """A layer whose ppg_id field was deleted in RustGIS (or another layer under the linked id) would look drawn there
    whole, and every row of the program deleted: the pull refuses it and writes nothing - from the list of the layers,
    and from the export itself when the list said otherwise."""
    live, fake, rl = _linked(tmp_path, _drawings([_unit(1, GX, GY)]))
    types, rows = _as_rustgis(live.path, "unita")
    del types[ID_FIELD]
    fake.prepare("7", "unita", types, [[g, {k: v for k, v in values.items() if k != ID_FIELD}] for g, values in rows])
    with pytest.raises(link.RustGisRefused, match="unita"):
        rl.pull()
    fake.tables["7"] = ("unita", [*fake.tables["7"][1], ID_FIELD])  # listed with it, exported without it
    with pytest.raises(link.RustGisRefused, match="ppg_id"):
        rl.pull()
    assert not live.external_change() and live.read_changes() == {}


def test_drawings_in_another_system_are_not_sent(tmp_path):
    """The chunk's system changed and the live GeoPackage was written in the new one while the link runs in the old: a
    push would convert the rows from the wrong system, so it sends nothing and says so (the link starts again)."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    state = fake.state()
    live.crs = "EPSG:32633"
    live.write(_drawings([dict(one, _geom=_square(291000.0, 4640000.0))]), force=True)
    with pytest.raises(RuntimeError, match="EPSG:32633"):
        rl.push()
    assert fake.state() == state and _sent(fake) == [] and rl.pending
    with pytest.raises(RuntimeError, match="EPSG:32633"):
        link.RustGisLink(live, FakeEngine(), "Saggio 1", GB).start([])


def test_stop_forgets_the_layers(tmp_path):
    live, fake, rl = _started(tmp_path, _drawings([_unit(1, GX, GY)]))
    rl.stop()
    assert rl.layers == {} and rl.baseline == {} and not rl.pending and fake.calls == []
    assert len(fake.layers) == 7  # RustGIS keeps them
    with pytest.raises(RuntimeError, match="start"):
        rl.push()


def test_start_with_rasters(tmp_path):
    """The rasters given go into the chunk's group with the tables, below them; one RustGIS cannot read does not keep
    the link from starting, and says why."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    ortho = tmp_path / "ortofoto.tif"
    with rasterio.open(ortho, "w", driver="GTiff", width=4, height=4, count=3, dtype="uint8", crs="EPSG:3004",
                       transform=from_origin(GX, GY + 4, 1, 1)) as dst:
        dst.write(np.zeros((3, 4, 4), dtype="uint8"))
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine()
    real = fake.command

    def command(name, params):
        if name == "layer.addData" and params["path"].endswith("rotto.tif"):
            raise link.RustGisRefused("this is not a raster RustGIS can read")
        return real(name, params)

    fake.command = command
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    out = rl.start([{"path": ortho, "name": "Ortofoto"}, {"path": tmp_path / "rotto.tif", "name": "Rotto"}])
    assert out["rasters"] == 1 and out["layers"] == 7 and "not a raster" in out["refused"]["Rotto"]
    assert [l["name"] for l in fake.layers][-1] == "Ortofoto"
    assert fake.layers[-1]["id"] in fake.groups[0]["layers"]


# ---- rasters at full resolution: one larger than RustGIS loads whole is read in place, by range

TILED = {"tiled": True, "blockxsize": 256, "blockysize": 256}


def _header_only(path: Path, width: int, height: int, count: int, overviews: str | None = "internal",
                 palette: bool = False, **options) -> Path:
    """A GeoTIFF of that size with no pixel written: GDAL leaves its blocks out (SPARSE_OK), so the file is its header
    and directories alone, a few kilobytes whatever its size. ``options``: how it is written (else tiled, Deflate);
    ``overviews``: "internal" (built inside it), "external" (an .ovr beside it), or None; ``palette``: one band of
    indices coloured by a palette."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin

    options = options or {**TILED, "compress": "deflate"}
    if palette:
        options["photometric"] = "palette"
    with rasterio.open(path, "w", driver="GTiff", width=width, height=height, count=count, dtype="uint8", crs=GB,
                       transform=from_origin(GX, GY + height * 0.01, 0.01, 0.01), sparse_ok=True, **options) as dst:
        if palette:
            dst.write_colormap(1, {1: (200, 60, 30, 255), 9: (30, 60, 200, 255)})
        if overviews == "internal":
            dst.build_overviews([2, 4, 8, 16, 32, 64], Resampling.average)
    if overviews == "external":
        with rasterio.Env(TIFF_USE_OVR=True, COMPRESS_OVERVIEW="DEFLATE", SPARSE_OK_OVERVIEW=True):
            with rasterio.open(path, "r+") as src:
                src.build_overviews([2, 4, 8, 16, 32, 64], Resampling.average)
    assert all(p.stat().st_size < 100_000 for p in path.parent.glob(path.name + "*"))  # no pixel reaches the disk
    return path


def _two_tone(path: Path, count: int = 3, side: int = 256, crs: str = GB, x0: float = GX, y0: float = GY,
              pixel: float = 0.05, **options) -> Path:
    """A small raster with pixels: its left half (200, 60, 30), its right half (30, 60, 200); a fourth band is alpha,
    transparent on the left quarter (as engine/ortho.py writes the alpha). ``options``: how it is written (else tiled,
    Deflate); its overviews built inside."""
    import numpy as np
    import rasterio
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.transform import from_origin

    data = np.zeros((count, side, side), dtype="uint8")
    for band, (left, right) in enumerate(zip((200, 60, 30), (30, 60, 200))):
        if band < count:
            data[band, :, : side // 2], data[band, :, side // 2:] = left, right
    if count == 4:
        data[3] = 255
        data[3, :, : side // 4] = 0
    options = options or {**TILED, "compress": "deflate"}
    with rasterio.open(path, "w", driver="GTiff", width=side, height=side, count=count, dtype="uint8", crs=crs,
                       transform=from_origin(x0, y0 + side * pixel, pixel, pixel), **options) as dst:
        if count == 4:
            dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        dst.write(data)
        dst.build_overviews([2, 4], Resampling.average)
    return path


def _two_tables(path: Path) -> Path:
    """A GeoPackage of two raster tables: GDAL opens the file as their container, with no band of its own (its
    subdatasets name them)."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    for name, value, more in (("alfa", 7, {}), ("beta", 9, {"APPEND_SUBDATASET": "YES"})):
        with rasterio.open(path, "w", driver="GPKG", width=64, height=64, count=1, dtype="uint8", crs=GB,
                           transform=from_origin(GX, GY + 64, 1, 1), RASTER_TABLE=name, **more) as dst:
            dst.write(np.full((1, 64, 64), value, np.uint8))
    return path


def _small_dem(path: Path, side: int = 100, crs: str = GB, x0: float = GX, y0: float = GY, pixel: float = 0.1) -> Path:
    """A small float DEM (heights from 99 to 101) RustGIS loads whole."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    heights = np.linspace(99.0, 101.0, side * side, dtype="float32").reshape(1, side, side)
    with rasterio.open(path, "w", driver="GTiff", width=side, height=side, count=1, dtype="float32", crs=crs,
                       transform=from_origin(x0, y0 + side * pixel, pixel, pixel)) as dst:
        dst.write(heights)
    return path


def _mixed_vrt(folder: Path) -> Path:
    """A VRT of two bands of different types (8-bit and float), each read from a small GeoTIFF of its own."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    sources = []
    for dtype, value in (("uint8", 7), ("float32", 1234.5)):
        source = folder / f"banda_{dtype}.tif"
        with rasterio.open(source, "w", driver="GTiff", width=64, height=64, count=1, dtype=dtype, crs=GB,
                           transform=from_origin(GX, GY + 1, 0.01, 0.01)) as dst:
            dst.write(np.full((1, 64, 64), value, dtype=dtype))
        sources.append(source)
    vrt = folder / "misto.vrt"
    vrt.write_text(f"""<VRTDataset rasterXSize="64" rasterYSize="64">
  <SRS>EPSG:3004</SRS>
  <GeoTransform>{GX}, 0.01, 0, {GY + 1}, 0, -0.01</GeoTransform>
  <VRTRasterBand dataType="Byte" band="1"><SimpleSource><SourceFilename relativeToVRT="0">{sources[0]}</SourceFilename>
    <SourceBand>1</SourceBand></SimpleSource></VRTRasterBand>
  <VRTRasterBand dataType="Float32" band="2"><SimpleSource>
    <SourceFilename relativeToVRT="0">{sources[1]}</SourceFilename><SourceBand>1</SourceBand></SimpleSource>
  </VRTRasterBand>
</VRTDataset>""", encoding="utf-8")
    return vrt


def _http(port: int, path: str, method: str = "GET", headers: dict | None = None) -> tuple[int, dict, bytes]:
    """One request to 127.0.0.1 on a connection of its own (http.client: never through a proxy): the status, the
    headers in lower case and the body."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, headers=headers or {})
        reply = connection.getresponse()
        return reply.status, {k.lower(): v for k, v in reply.getheaders()}, reply.read()
    finally:
        connection.close()


def _waited(condition, seconds: float = 5.0) -> bool:
    """Whether ``condition()`` holds within ``seconds`` (asked every 20 ms)."""
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def test_raster_route(tmp_path):
    """By address (read in place, at full resolution) only what RustGIS reads so and would otherwise open from an
    overview: a GeoTIFF above RASTER_SAMPLES (width x height x bands), tiled, with internal overviews, compressed as
    RustGIS decodes in place, without a palette. Above the limit by file what RustGIS opens right from an overview: in
    strips, without internal overviews (an .ovr beside it is never served), a YCbCr JPEG (RustGIS converts it from a
    file, in place it would show raw Y, Cb, Cr), a palette (applied to a file only). A copy for what RustGIS decodes
    neither way (WebP, LERC, LZMA) and for every other format, whatever the size: RustGIS refuses a VRT, draws a PNG
    without its world file, and reads a JPEG 2000 or an ERDAS IMG only through GDAL's tools, which an app started from
    the Finder does not find. By file what no copy is made of (complex numbers, bands of different types) and, below
    the limit, what RustGIS loads whole and what rasterio cannot read: RustGIS says whether it reads it. Every large
    case is a header-only sparse file."""
    import numpy as np
    import rasterio
    import rasterio.shutil
    from rasterio.transform import from_origin

    small = tmp_path / "piccolo.tif"
    with rasterio.open(small, "w", driver="GTiff", width=100, height=100, count=3, dtype="uint8", crs=GB,
                       transform=from_origin(GX, GY + 1, 0.01, 0.01)) as dst:
        dst.write(np.zeros((3, 100, 100), dtype="uint8"))
    assert link.raster_route(small) == "file"
    good = _header_only(tmp_path / "buona.tif", 8000, 6000, 4)
    assert link.raster_route(good) == "url"
    assert link.raster_route(_header_only(tmp_path / "al_limite.tif", 10_000, 4_000, 3)) == "file"  # 120 M: whole
    assert link.raster_route(_header_only(tmp_path / "oltre.tif", 10_001, 4_000, 3)) == "url"
    # above the limit, read right from an overview as a file
    ycbcr = _header_only(tmp_path / "ycbcr.tif", 8000, 6000, 3, compress="jpeg", photometric="YCbCr", **TILED)
    assert link.raster_route(ycbcr) == "file"
    assert link.raster_route(_header_only(tmp_path / "strisce.tif", 8000, 6000, 4, None, compress="deflate")) == "file"
    assert link.raster_route(_header_only(tmp_path / "senza.tif", 8000, 6000, 4, None)) == "file"
    assert link.raster_route(_header_only(tmp_path / "accanto.tif", 8000, 6000, 4, "external")) == "file"
    assert link.raster_route(_header_only(tmp_path / "palette.tif", 12_000, 11_000, 1, palette=True)) == "file"
    # decoded neither way: a copy, whatever the size
    for codec in ("webp", "lerc", "lzma"):
        large = _header_only(tmp_path / f"{codec}.tif", 8000, 6000, 3, None, compress=codec, **TILED)
        assert link.raster_route(large) == "copy", codec
    assert link.raster_route(_two_tone(tmp_path / "webp_piccolo.tif", compress="webp", **TILED)) == "copy"
    # other formats: a copy, but for those RustGIS loads as files (through GDAL's tools)
    rasterio.shutil.copy(good, tmp_path / "buona.vrt", driver="VRT")
    assert link.raster_route(tmp_path / "buona.vrt") == "copy"
    rasterio.shutil.copy(small, tmp_path / "piccolo.png", driver="PNG", WORLDFILE="YES")
    assert link.raster_route(tmp_path / "piccolo.png") == "copy"
    rasterio.shutil.copy(small, tmp_path / "piccolo.jp2", driver="JP2OpenJPEG")
    assert link.raster_route(tmp_path / "piccolo.jp2") == "copy"
    rasterio.shutil.copy(small, tmp_path / "piccolo.img", driver="HFA")
    assert link.raster_route(tmp_path / "piccolo.img") == "copy"
    # no copy of complex numbers or of bands of different types: by file
    with rasterio.open(tmp_path / "complessa.tif", "w", driver="GTiff", width=64, height=64, count=1, dtype="complex64",
                       crs=GB, transform=from_origin(GX, GY + 1, 0.01, 0.01)) as dst:
        dst.write(np.zeros((1, 64, 64), dtype="complex64"))
    assert link.raster_route(tmp_path / "complessa.tif") == "file"
    rasterio.shutil.copy(tmp_path / "complessa.tif", tmp_path / "complessa.vrt", driver="VRT")
    assert link.raster_route(tmp_path / "complessa.vrt") == "file"
    assert link.raster_route(_mixed_vrt(tmp_path)) == "file"
    (tmp_path / "rotto.tif").write_bytes(b"not a raster")
    assert link.raster_route(tmp_path / "rotto.tif") == "file"
    assert link.raster_route(tmp_path / "manca.tif") == "file"
    # a file that holds rasters but has no band of its own (a GeoPackage of two raster tables): nothing to give
    assert link.raster_route(_two_tables(tmp_path / "due.gpkg")) == "refused"


def test_the_server_answers_ranges(tmp_path, monkeypatch, capfd):
    """The raster server listens on 127.0.0.1 only, its files under a random prefix of 32 hexadecimal characters and a
    random name each: a file given to url() whole or by one byte range (206, Content-Range), its last bytes, 416 past
    its end, HEAD with the length and Accept-Ranges: bytes; several ranges or a malformed one get the whole file (a
    server may ignore them). Anything else is not found (404): another prefix, a name never given, the folder, a path
    climbing out of it, a file's own name, another host, a link put where the file was, the file written again or
    changed in place since its address was given. It reads a few bytes at a time, keeps a connection for the next
    request, serves at most CONNECTIONS at once (one more is closed at once), says nothing on stderr (a reader gone in
    the middle of an answer included), and close() stops it, itself ending a connection kept open for a next request
    and one whose reader stopped taking the answer, and joins its thread."""
    monkeypatch.setattr(link.RasterServer, "CHUNK", 7)  # several reads for every answer
    monkeypatch.setattr(link.RasterServer, "CONNECTIONS", 4)
    data = bytes(range(256)) * 4
    raster = tmp_path / "ortofoto.tif"
    raster.write_bytes(data)
    secret = tmp_path / "segreto.tif"
    secret.write_bytes(b"not for RustGIS")
    server = link.RasterServer()
    port = server.server_address[1]
    kept = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    idle: list = []
    stuck = None
    try:
        assert server.server_address[0] == "127.0.0.1" and server.thread.daemon
        address = server.url(raster)
        parts = urllib.parse.urlsplit(address)
        assert (parts.scheme, parts.hostname, parts.port) == ("http", "127.0.0.1", port)
        path = parts.path
        prefix, name = path.strip("/").split("/")
        assert re.fullmatch("[0-9a-f]{32}", prefix) and re.fullmatch(r"[0-9a-f]{32}\.tif", name)
        assert server.base == f"http://127.0.0.1:{port}/{prefix}/" and address.startswith(server.base)
        status, headers, body = _http(port, path)
        assert (status, body) == (200, data) and headers["content-length"] == "1024"
        assert headers["accept-ranges"] == "bytes" and headers["content-type"] == "image/tiff"
        for asked, part, said in (("bytes=0-99", data[:100], "bytes 0-99/1024"),
                                  ("bytes=1000-1099", data[1000:], "bytes 1000-1023/1024"),  # cut at the end
                                  ("bytes=1000-", data[1000:], "bytes 1000-1023/1024"),
                                  ("bytes=-10", data[-10:], "bytes 1014-1023/1024"),
                                  ("bytes=-5000", data, "bytes 0-1023/1024")):
            status, headers, body = _http(port, path, headers={"Range": asked})
            assert (status, body, headers["content-range"]) == (206, part, said), asked
            assert headers["content-length"] == str(len(part)) and headers["accept-ranges"] == "bytes"
        for asked in ("bytes=999999-", "bytes=1024-", "bytes=-0"):
            status, headers, body = _http(port, path, headers={"Range": asked})
            assert (status, body, headers["content-range"]) == (416, b"", "bytes */1024"), asked
        status, headers, body = _http(port, path, "HEAD")
        assert (status, body, headers["content-length"], headers["accept-ranges"]) == (200, b"", "1024", "bytes")
        status, headers, body = _http(port, path, "HEAD", {"Range": "bytes=10-19"})
        assert (status, body, headers["content-length"]) == (206, b"", "10")
        for asked in ("bytes=0-1,5-6", "bytes=50-10", "items=0-9", "bytes=x-y", "bytes=-"):
            status, _, body = _http(port, path, headers={"Range": asked})
            assert (status, body) == (200, data), asked
        assert _http(port, f"{path}?v=2", headers={"Range": "bytes=0-9"})[2] == data[:10]  # a query changes nothing
        # nothing but the files given
        other = "0" * 32 if prefix != "0" * 32 else "1" * 32
        for elsewhere in (f"/{other}/{name}", f"/{prefix}/{other}.tif", f"/{prefix}/", f"/{prefix}", "/",
                          f"/{prefix}/segreto.tif", f"/{prefix}/../segreto.tif", f"/{prefix}/{name}/../segreto.tif",
                          f"/{prefix}/%2e%2e/segreto.tif", f"/{name}", f"/{prefix}/ortofoto.tif", str(secret),
                          f"{path}/", f"{path}x"):
            status, _, body = _http(port, elsewhere)
            assert (status, body) == (404, b""), elsewhere
        assert _http(port, path, headers={"Host": f"rebound.example:{port}"})[0] == 404
        assert _http(port, path, "POST")[0] in (405, 501)
        # each call its own address; a file that is not there has none
        again = server.url(raster)
        assert again != address and _http(port, urllib.parse.urlsplit(again).path)[2] == data
        with pytest.raises(FileNotFoundError):
            server.url(tmp_path / "manca.tif")
        # a link given: the file it names is served; a link put later where the file was is never followed
        alias = tmp_path / "alias.tif"
        alias.symlink_to(raster)
        aliased = urllib.parse.urlsplit(server.url(alias)).path
        assert _http(port, aliased)[2] == data
        raster.rename(tmp_path / "spostato.tif")
        raster.symlink_to(secret)
        assert _http(port, path)[0] == 404 and _http(port, aliased)[0] == 404
        raster.unlink()
        raster.write_bytes(data)  # the product written again where it was: another file, its addresses not found
        assert _http(port, path)[0] == 404 and _http(port, aliased)[0] == 404
        path = urllib.parse.urlsplit(server.url(raster)).path  # the program gives RustGIS a new one
        assert _http(port, path)[2] == data
        os.utime(raster, ns=(time.time_ns(), raster.stat().st_mtime_ns + 10**9))  # changed in place: not found
        assert _http(port, path)[0] == 404
        path = urllib.parse.urlsplit(server.url(raster)).path
        # one connection, several requests: RustGIS reads a tile at a time
        for start in (0, 256):
            kept.request("GET", path, headers={"Range": f"bytes={start}-{start + 9}"})
            reply = kept.getresponse()
            assert (reply.status, reply.read()) == (206, data[start:start + 10])
        # at most CONNECTIONS at once: one more is closed at once; a place freed is taken again
        assert _waited(lambda: len(server._open) == 1)  # the one kept
        idle = [socket.create_connection(("127.0.0.1", port), timeout=10) for _ in range(3)]
        assert _waited(lambda: len(server._open) == 4)
        extra = socket.create_connection(("127.0.0.1", port), timeout=10)
        assert extra.recv(100) == b""  # closed, nothing said
        extra.close()
        idle.pop().close()
        assert _waited(lambda: len(server._open) == 3)
        assert _http(port, path, headers={"Range": "bytes=0-9"})[2] == data[:10]
        while idle:
            idle.pop().close()
        assert _waited(lambda: len(server._open) == 1)
        # a reader that goes away in the middle of an answer: nothing said, the server goes on
        monkeypatch.setattr(link.RasterServer, "CHUNK", 1 << 16)
        large = tmp_path / "grande.tif"
        large.write_bytes(bytes(4 << 20))
        far = urllib.parse.urlsplit(server.url(large)).path
        reader = socket.create_connection(("127.0.0.1", port), timeout=10)
        reader.sendall(f"GET {far} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        assert reader.recv(12).startswith(b"HTTP/1.1 200")
        reader.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # a reset, not a goodbye
        reader.close()
        assert _http(port, path, headers={"Range": "bytes=0-9"})[2] == data[:10]
        # a reader that stops taking an answer (RustGIS busy): the thread serving it is blocked in a send
        stuck = _stuck_reader(server, tmp_path / "enorme.tif")
        assert _waited(lambda: len(server._open) == 2)  # the one kept, the one stuck
    finally:
        server.close()
        left = list(server._open)  # the connections close() did not end itself
        kept.close()
        for connection in idle + [stuck] * (stuck is not None):
            connection.close()
    # close() ended the connection kept for a next request and the one stuck in an answer, before its wait on them
    # (CLOSE_WAIT) could run out: judged by that, not by the time it took, which a slow computer stretches
    assert left == []
    assert not server.thread.is_alive()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    server.close()  # twice is harmless
    with pytest.raises(RuntimeError, match="closed"):
        server.url(secret)
    assert capfd.readouterr().err == ""  # no request lines, no tracebacks


def _stuck_reader(server, path: Path) -> socket.socket:
    """A connection that asked ``server`` for a large sparse file at ``path`` (nothing on the disk) and takes no more
    of the answer: with its receive buffer small, the thread serving it fills the buffers and blocks in a send."""
    with open(path, "wb") as fh:
        fh.truncate(32 << 20)
    port = server.server_address[1]
    far = urllib.parse.urlsplit(server.url(path)).path
    stuck = socket.socket()
    stuck.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    stuck.settimeout(10)
    stuck.connect(("127.0.0.1", port))
    stuck.sendall(f"GET {far} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
    assert stuck.recv(12).startswith(b"HTTP/1.1 200")
    time.sleep(0.2)  # the thread fills the buffers and blocks (close() must end it in any state all the same)
    return stuck


def test_close_ends_the_connections_without_shutting_their_sockets(tmp_path, monkeypatch, capfd):
    """The threads serving the connections end by themselves when the server closes, within a step: one waiting for
    the next request on a connection kept open, one sending an answer its reader stopped taking. Here without the
    shutdown of their sockets close() also does - what makes a thread finish at once on POSIX, and what Windows
    leaves blocked - and with nothing closed under them (on BSD systems that drops a poll without waking it): close()
    does not wait CLOSE_WAIT out on any system."""
    monkeypatch.setattr(link.RasterServer, "CHUNK", 1 << 16)
    monkeypatch.setattr(link, "_end_connection", lambda connection: None)
    raster = tmp_path / "ortofoto.tif"
    raster.write_bytes(bytes(range(256)))
    server = link.RasterServer()
    port = server.server_address[1]
    kept = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    stuck = None
    try:
        kept.request("GET", urllib.parse.urlsplit(server.url(raster)).path, headers={"Range": "bytes=0-9"})
        assert kept.getresponse().read() == bytes(range(10))  # answered: the connection kept for the next request
        stuck = _stuck_reader(server, tmp_path / "enorme.tif")
        assert _waited(lambda: len(server._open) == 2)
        server.close()
        left = list(server._open)
    finally:
        server.close()
        kept.close()
        if stuck is not None:
            stuck.close()
    assert left == [] and not server.thread.is_alive()
    assert capfd.readouterr().err == ""  # the threads ended quietly

def test_start_sends_large_rasters_by_address(tmp_path):
    """A raster larger than RustGIS loads whole that RustGIS reads in place goes by the address the link's raster
    server gives it (layer.addUrl): a tile layer there, drawn under its layers and in no group (RustGIS groups only
    layers); a smaller one as its file (layer.addData), into the chunk's group. Every raster is kept, two of the same
    name too. stop() takes the tile layer away (by its address) and closes the server; RustGIS keeps the layers."""
    small = _small_dem(tmp_path / "dem.tif")
    other = _small_dem(tmp_path / "dem_2.tif")
    large = _header_only(tmp_path / "ortofoto.tif", 8000, 6000, 4)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine()
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    try:
        out = rl.start([{"path": large, "name": "Ortofoto"}, {"path": small, "name": "DEM"},
                        {"path": other, "name": "DEM"}])
        assert out == {"layers": 7, "rasters": 3, "version": "0.4.0"}
        commands = [(args["command"], args["params"]) for tool, args in fake.calls if tool == "run_command"]
        port = rl.server.server_address[1]
        (by_address,) = [params for command, params in commands if command == "layer.addUrl"]
        assert set(by_address) == {"url", "name"} and by_address["name"] == "Ortofoto"
        assert re.fullmatch(rf"http://127\.0\.0\.1:{port}/[0-9a-f]{{32}}/[0-9a-f]{{32}}\.tif", by_address["url"])
        assert [params for command, params in commands if command == "layer.addData" and "layer" not in params] == [
            {"path": str(small), "name": "DEM"}, {"path": str(other), "name": "DEM"}]
        assert fake.fetched == [(by_address["url"], "bytes=0-65535", 206)]  # RustGIS read its header from the server
        assert fake.tile_layers == [{"name": "Ortofoto", "url": by_address["url"], "kind": "cog"}]
        ortho, dem, dem_2 = rl.rasters
        assert (ortho.name, ortho.path, ortho.url, ortho.layer, ortho.copy) == ("Ortofoto", large, by_address["url"],
                                                                                 None, None)
        assert (dem.name, dem.path, dem.url, dem_2.name, dem_2.path) == ("DEM", small, None, "DEM", other)
        (group,) = fake.groups
        assert {int(dem.layer), int(dem_2.layer)} <= set(group["layers"]) and len(group["layers"]) == 9
    finally:
        rl.stop()
    assert rl.server is None and rl.rasters == []
    with pytest.raises(OSError):
        _http(port, urllib.parse.urlsplit(by_address["url"]).path)
    assert fake.tile_layers == [] and len(fake.layers) == 9


def _marked(script: str) -> list:
    """The commands of a script, each mark of a tile layer (TILE_MARK and 16 hexadecimal digits) written "<mark>"."""
    def plain(value):
        mark = isinstance(value, str) and re.fullmatch(rf"{link.TILE_MARK}-[0-9a-f]{{16}}", value)
        return "<mark>" if mark else value

    return [(command, {k: plain(v) for k, v in params.items()}) for command, params in _commands(script)]


def test_a_start_that_fails_takes_away_the_rasters_it_served(tmp_path):
    """A start that fails after RustGIS took rasters by address takes those tile layers away - marked by their
    places, their address checked, removed by their marks - and closes the server: a tile layer of the user's with the
    same name stays, its name as it was."""
    large = _header_only(tmp_path / "ortofoto.tif", 8000, 6000, 4)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine()
    mine = {"name": "Ortofoto", "url": "https://example.org/mia.tif", "kind": "cog"}  # the user's own
    fake.tile_layers.append(dict(mine))
    fake.rules.append(lambda command, params: "no groups today" if command == "group.add" else None)
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    try:
        with pytest.raises(link.RustGisRefused, match="no groups today"):
            rl.start([{"path": large, "name": "Ortofoto"}, {"path": large, "name": "Ortofoto"}])
    finally:
        rl.stop()
    assert fake.tile_layers == [mine] and fake.layers == [] and len(fake.removed) == 2
    renaming, removal = [script for script in fake.scripts if "Tiles " in script]
    assert _marked(renaming) == [("map.setTiles", {"index": 0, "rename": "<mark>"}),
                                 ("map.setTiles", {"index": 1, "rename": "<mark>"})]
    assert _marked(removal) == [("map.removeTiles", {"name": "<mark>"})] * 2
    assert rl.server is None and rl.rasters == [] and rl.layers == {}


def test_stop_takes_away_only_the_links_tile_layers(tmp_path, caplog):
    """stop() marks the tile layers RustGIS reads from the link's server with names of their own (by their places, in
    one script), reads the list again, and removes by their marks those found on an address of the server (one
    script): the user's, whatever their name, above and below, stay as they were. RustGIS busy at the first reading:
    nothing sent; a removal it refuses: said in the log, the marked ones given their names back; a renaming whose
    answer was lost: nothing removed (it may still run: marks are all it leaves); a removal whose answer was lost:
    never sent again. Never raised; the server closes all the same."""
    large = _header_only(tmp_path / "ortofoto.tif", 8000, 6000, 4)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    xyz, cog = "http://127.0.0.1:9/{z}/{x}/{y}.png", "https://example.org/mia.tif"
    links = []

    def started():
        fake = FakeEngine()
        rl = link.RustGisLink(live, fake, "Saggio 1", GB)
        links.append(rl)
        rl.start([{"path": large, "name": "Ortofoto"}])
        fake.tile_layers.insert(0, {"name": "Ortofoto", "url": xyz, "kind": "raster"})  # the user's, same name
        fake.tile_layers.append({"name": "Ortofoto", "url": cog, "kind": "cog"})
        fake.calls.clear()
        fake.scripts.clear()
        return fake, rl

    def names(fake):
        return [re.sub(rf"{link.TILE_MARK}-[0-9a-f]{{16}}", "<mark>", t["name"]) for t in fake.tile_layers]

    try:
        fake, rl = started()
        ours = rl.rasters[0].url
        rl.stop()
        assert [(t["name"], t["url"]) for t in fake.tile_layers] == [("Ortofoto", xyz), ("Ortofoto", cog)]
        assert [t["url"] for t in fake.removed] == [ours] and rl.server is None
        assert [tool for tool, _ in fake.calls] == ["list_layers", "run", "list_layers", "run"]
        assert _marked(fake.scripts[0]) == [("map.setTiles", {"index": 1, "rename": "<mark>"})]
        assert _marked(fake.scripts[1]) == [("map.removeTiles", {"name": "<mark>"})]
        fake, rl = started()
        fake.busy.append("list_layers")
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            rl.stop()
        assert [tool for tool, _ in fake.calls] == ["list_layers"] and names(fake) == ["Ortofoto"] * 3
        assert "stay in RustGIS" in caplog.text and rl.server is None
        fake, rl = started()
        fake.rules.append(lambda command, params: "not now" if command == "map.removeTiles" else None)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            rl.stop()
        assert names(fake) == ["Ortofoto"] * 3 and "stay in RustGIS" in caplog.text and rl.server is None
        fake, rl = started()
        fake.lost.append("run")  # the renaming runs, its answer lost
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            rl.stop()
        assert [tool for tool, _ in fake.calls] == ["list_layers", "run"] and fake.removed == []
        assert names(fake) == ["Ortofoto", "<mark>", "Ortofoto"] and "may be named ppg-tile-" in caplog.text
        assert "the tile layers at places 1 of RustGIS's list" in caplog.text
        fake, rl = started()
        fake.hooks.append(lambda tool, args: fake.busy.append("list_layers") if tool == "run" and not fake.busy
                          else None)  # renamed, then the list not read again
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            rl.stop()
        assert [tool for tool, _ in fake.calls] == ["list_layers", "run", "list_layers"] and fake.removed == []
        assert names(fake) == ["Ortofoto", "<mark>", "Ortofoto"] and "are named ppg-tile-" in caplog.text
        assert "now (RustGIS did not list them again)" in caplog.text
        fake, rl = started()
        base, listing = rl.server.base, fake.listing

        def numbered_otherwise():  # RustGIS giving the link's tile layer a place other than its own
            answer = listing()
            for tile in answer["tileLayers"]:
                tile["index"] += tile["url"].startswith(base)
            return answer

        fake.listing = numbered_otherwise
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            rl.stop()
        assert [tool for tool, _ in fake.calls] == ["list_layers"] and names(fake) == ["Ortofoto"] * 3
        assert "at a place other than their own" in caplog.text and fake.removed == []
        fake, rl = started()
        fake.hooks.append(lambda tool, args: fake.lost.append("run") if "map.removeTiles" in args.get(
            "script", "") and not fake.lost else None)  # the removal runs, its answer lost
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            rl.stop()
        assert [tool for tool, _ in fake.calls] == ["list_layers", "run", "list_layers", "run"]
        assert [t["url"] for t in fake.tile_layers] == [xyz, cog] and "may stay" in caplog.text
    finally:
        for rl in links:
            rl.stop()


def test_tile_layers_go_by_their_marks_when_the_list_changes(tmp_path, caplog):
    """RustGIS's list of tile layers changes while a link takes its own away - another link stopping between the
    first's reading and its renaming, a basemap the user adds then: a mark that lands on a tile layer of the user's
    gives it back the name it had (one added meanwhile keeps the mark, said in the log), the link's layer that moved
    stays, said in the log, and nothing but a tile layer of a link is ever removed."""
    large = _header_only(tmp_path / "ortofoto.tif", 8000, 6000, 4)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    basemap = {"name": "Basemap dell'utente", "url": "http://127.0.0.1:9/user/{z}/{x}/{y}.png", "kind": "raster"}
    late = {"name": "Altro sfondo", "url": "http://127.0.0.1:9/late/{z}/{x}/{y}.png", "kind": "raster"}
    fake = FakeEngine()
    fake.tile_layers.append(dict(basemap))
    first, second = link.RustGisLink(live, fake, "Saggio 1", GB), link.RustGisLink(live, fake, "Saggio 2", GB)
    links = [first, second]
    try:
        second.start([{"path": large, "name": "Ortofoto (B)"}])
        first.start([{"path": large, "name": "Ortofoto (A)"}])
        a, b = first.rasters[0].url, second.rasters[0].url
        assert [t["url"] for t in fake.tile_layers] == [a, b, basemap["url"]]

        def meanwhile(tool, args):  # right before the second's renaming: the first stops, whole
            if tool == "run" and fake.hooks:
                fake.hooks.clear()
                first.stop()

        fake.hooks.append(meanwhile)
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            second.stop()
        assert [(t["name"], t["url"]) for t in fake.tile_layers] == [("Ortofoto (B)", b),
                                                                     (basemap["name"], basemap["url"])]
        assert [t["url"] for t in fake.removed] == [a] and "moved" in caplog.text
        # a basemap the user adds between the reading and the renaming
        fake = FakeEngine()
        fake.tile_layers.append(dict(basemap))
        rl = link.RustGisLink(live, fake, "Saggio 1", GB)
        links.append(rl)
        rl.start([{"path": large, "name": "Ortofoto"}])
        ours = rl.rasters[0].url

        def added(tool, args):
            if tool == "run" and fake.hooks:
                fake.hooks.clear()
                fake.tile_layers.insert(0, dict(late))

        fake.hooks.append(added)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            rl.stop()
        assert fake.removed == [] and [t["url"] for t in fake.tile_layers] == [late["url"], ours, basemap["url"]]
        assert [t["name"] for t in fake.tile_layers][1:] == ["Ortofoto", basemap["name"]]
        assert "the name they had cannot be told" in caplog.text and "moved" in caplog.text
    finally:
        for rl in links:
            rl.stop()


def test_hidden_rasters_leave_the_orthophoto_on_top(tmp_path):
    """RustGIS draws its layers over every tile layer: the rasters given hidden are hidden - their layers in one
    layer.visibility - so the orthophoto read in place is not covered by a DEM; opacities go as well, the layers' by
    layer.transparency and the tile layers' by map.setTiles - marked first with names of their own, then set by those
    names and given their names back on the same line, in one script with the transparencies (map.setTiles alone
    answers with no object). A tile layer of the user's is left as it is."""
    ortho = _header_only(tmp_path / "ortofoto.tif", 8000, 6000, 4)
    reflectance = _header_only(tmp_path / "riflettanza.tif", 8000, 6000, 4)
    dem = _small_dem(tmp_path / "dem.tif")
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine()
    fake.tile_layers.append({"name": "Sfondo", "url": "http://127.0.0.1:9/{z}/{x}/{y}.png", "kind": "raster"})
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    try:
        rl.start([{"path": ortho, "name": "Ortofoto", "opacity": 0.8},
                  {"path": dem, "name": "DEM (ombreggiato)", "hidden": True, "opacity": 0.6},
                  {"path": dem, "name": "DEM", "hidden": True},
                  {"path": reflectance, "name": "Riflettanza", "hidden": True}])
        ortho_r, shaded, plain, refl = rl.rasters
        commands = [(args["command"], args["params"]) for tool, args in fake.calls if tool == "run_command"]
        assert [params for command, params in commands if command == "layer.visibility"] == [
            {"layer": [int(shaded.layer), int(plain.layer)], "visible": False}]
        assert not [command for command, _ in commands if command in ("map.setTiles", "layer.transparency")]
        renaming, setting = [script for script in fake.scripts if "map.setTiles" in script]
        assert _marked(renaming) == [("map.setTiles", {"index": 0, "rename": "<mark>"}),
                                     ("map.setTiles", {"index": 1, "rename": "<mark>"})]
        assert _marked(setting) == [("layer.transparency", {"layer": int(shaded.layer), "value": 40}),
                                    ("map.setTiles", {"name": "<mark>", "visible": False, "rename": "Riflettanza"}),
                                    ("map.setTiles", {"name": "<mark>", "opacity": 0.8, "rename": "Ortofoto"})]
        assert [(t["name"], t.get("visible"), t.get("opacity")) for t in fake.tile_layers] == [
            ("Riflettanza", False, None), ("Ortofoto", None, 0.8), ("Sfondo", None, None)]
        assert [fake.layer(int(r.layer))["visible"] for r in (shaded, plain)] == [False, False]
        assert fake.layer(int(shaded.layer))["transparency"] == 40
        assert ortho_r.url and refl.url and (refl.hidden, ortho_r.opacity, shaded.opacity) == (True, 0.8, 0.6)
    finally:
        rl.stop()


def test_a_raster_rustgis_cannot_decode_goes_as_a_copy(tmp_path, monkeypatch):
    """A raster compressed as RustGIS does not decode (WebP, LZMA here) goes as a copy in the chunk's data folder
    (rustgis/), given as any raster is - a small one as its file, a larger one by its address: JPEG of RGB pixels for
    RGB imagery, Deflate with the alpha band for one with transparency (RustGIS ignores an internal mask), a palette
    kept (its overviews of the nearest cell, never averaged indices); tiles of 512, internal overviews; written
    through a temporary file of its own. Made once, reused while the raster is the same file, made again when it
    changes."""
    import numpy as np
    import rasterio
    from rasterio.enums import ColorInterp

    photo = _two_tone(tmp_path / "foto.tif", 3, compress="webp", **TILED)
    ortho = _two_tone(tmp_path / "ortofoto.tif", 4, compress="webp", **TILED)
    classes = _header_only(tmp_path / "classi.tif", 600, 600, 1, None, palette=True, compress="lzma", **TILED)
    with rasterio.open(classes, "r+") as dst:
        dst.write(np.where(np.arange(600)[None, None, :] < 300, 1, 9).repeat(600, axis=1).astype("uint8"))
    data_dir = tmp_path / "progetto.ppg.files" / "chunk_1"
    (data_dir / "rustgis").mkdir(parents=True)
    stale = data_dir / "rustgis" / "foto.tif.tmp"  # a fixed name an older copy might have used: never touched
    stale.write_bytes(b"left there")
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine()
    rl = link.RustGisLink(live, fake, "Saggio 1", GB, data_dir=data_dir)
    rasters = [{"path": photo, "name": "Foto"}, {"path": ortho, "name": "Ortofoto"},
               {"path": classes, "name": "Classi"}]
    try:
        assert rl.start(rasters)["rasters"] == 3
        first, second, third = rl.rasters
        assert first.copy.parent == second.copy.parent == third.copy.parent == data_dir / "rustgis"
        assert all(r.layer and r.url is None for r in rl.rasters)  # small: the copies as files
        given = [params["path"] for tool, args in fake.calls if tool == "run_command"
                 for params in [args["params"]] if args["command"] == "layer.addData" and "layer" not in params]
        assert given == [str(first.copy), str(second.copy), str(third.copy)]
        with rasterio.open(first.copy) as copy:
            assert copy.compression.name == "jpeg" and copy.count == 3 and copy.overviews(1)
            assert "SOURCE_COLOR_SPACE" not in copy.tags(ns="IMAGE_STRUCTURE") and copy.block_shapes[0] == (512, 512)
        with rasterio.open(second.copy) as copy, rasterio.open(ortho) as source:
            assert copy.compression.name == "deflate" and copy.colorinterp[3] == ColorInterp.alpha
            assert (copy.read() == source.read()).all() and copy.read(4)[:, :10].max() == 0  # as decoded, alpha kept
        with rasterio.open(third.copy) as copy, rasterio.open(classes) as source:
            assert copy.colorinterp[0] == ColorInterp.palette and copy.colormap(1) == source.colormap(1)
            assert (copy.read() == source.read()).all() and copy.overviews(1)
        with rasterio.open(third.copy, overview_level=0) as overview:
            assert set(np.unique(overview.read())) <= {1, 9}  # the nearest cell, no index between
        assert stale.read_bytes() == b"left there"
        assert sorted(p.name for p in (data_dir / "rustgis").iterdir() if not p.name.endswith(".json")) == sorted(
            [first.copy.name, second.copy.name, third.copy.name, stale.name])  # no temporary file left
        made = first.copy.stat().st_mtime_ns
        rl.start(rasters)
        assert rl.rasters[0].copy.stat().st_mtime_ns == made  # the same raster: the copy reused
        note = first.copy.with_name(first.copy.name + ".json")
        noted = json.loads(note.read_text(encoding="utf-8"))
        assert noted["layout"] == link.COPY_LAYOUT == 1
        note.write_text(json.dumps(dict(noted, layout=0)), encoding="utf-8")  # a copy written another way
        rl.start(rasters)
        assert rl.rasters[0].copy.stat().st_mtime_ns != made and json.loads(note.read_text())["layout"] == 1
        made = rl.rasters[0].copy.stat().st_mtime_ns
        later = photo.stat().st_mtime_ns + 10**9
        os.utime(photo, ns=(later, later))
        rl.start(rasters)
        assert rl.rasters[0].copy.stat().st_mtime_ns != made  # the raster changed: copied again
        # a larger one (here above a lower limit) goes by the address of its copy
        large = _two_tone(tmp_path / "grande.tif", 3, side=600, compress="webp", **TILED)
        monkeypatch.setattr(link, "RASTER_SAMPLES", 1_000_000)
        rl.start([{"path": large, "name": "Grande"}])
        (big,) = rl.rasters
        assert big.copy and big.url and big.layer is None and fake.fetched[-1][0] == big.url
        # a copy that fails to land (replace_file: put in place as a product is) leaves no temporary file
        monkeypatch.setattr(link, "replace_file", lambda *args: (_ for _ in ()).throw(OSError("disk gone")))
        with pytest.raises(OSError, match="disk gone"):
            link._write_copy(photo, tmp_path / "copia.tif")
        assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".copia")] == []
    finally:
        rl.stop()


def test_a_mark_gives_back_the_one_name_missing_or_stays(caplog):
    """A mark that lands on a tile layer of the user's whose address another shares (a basemap added twice, with two
    names) gives back the one name missing at that address among the tile layers there without a mark; when that is
    not one name - two marks landed on two layers of one address with different names - the marks stay, said in the
    log, rather than a guess."""
    base, osm = "http://127.0.0.1:1/ours/", "http://127.0.0.1:9/osm/{z}/{x}/{y}.png"

    def marked(ours: int):
        fake = FakeEngine()
        fake.tile_layers = ([{"name": n, "url": f"http://127.0.0.1:2/other/{n}.tif", "kind": "cog"} for n in "XY"]
                            + [{"name": "Ortofoto", "url": f"{base}{n}.tif", "kind": "cog"} for n in range(ours)]
                            + [{"name": "OSM", "url": osm, "kind": "raster"},
                               {"name": "OSM chiaro", "url": osm, "kind": "raster"}])

        def another_link_stops(tool, args):  # its two tile layers, above, gone right before the renaming
            if tool == "run" and fake.hooks:
                fake.hooks.clear()
                del fake.tile_layers[0:2]

        fake.hooks.append(another_link_stops)
        rl = link.RustGisLink.__new__(link.RustGisLink)
        rl.transport = fake
        marks = rl._mark_tiles(lambda url: url.startswith(base))
        return [re.sub(rf"{link.TILE_MARK}-[0-9a-f]{{16}}", "<mark>", t["name"]) for t in fake.tile_layers], marks

    with caplog.at_level(logging.WARNING, logger=link.__name__):
        names, marks = marked(1)
    assert (names, marks) == (["Ortofoto", "OSM", "OSM chiaro"], {}) and "moved" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=link.__name__):
        names, marks = marked(2)
    assert (names, marks) == (["Ortofoto", "Ortofoto", "<mark>", "<mark>"], {})
    assert "the name they had cannot be told" in caplog.text


def test_one_raster_that_fails_never_fails_the_start(tmp_path, monkeypatch, caplog):
    """A raster whose routing or copy fails in a way of its own (an exotic raster) is left out and said in
    "refused", logged with its name; the other rasters and the tables go to RustGIS all the same."""
    photo = _two_tone(tmp_path / "foto.tif", 3, compress="webp", **TILED)
    dem = _small_dem(tmp_path / "dem.tif")
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    rl = link.RustGisLink(live, FakeEngine(), "Saggio 1", GB, data_dir=tmp_path / "chunk_1")

    def exotic(source):
        raise TypeError("a raster of an exotic type")

    monkeypatch.setattr(link, "_copy_bytes", exotic)
    try:
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            out = rl.start([{"path": photo, "name": "Foto"}, {"path": dem, "name": "DEM"}])
        assert out["refused"] == {"Foto": "a raster of an exotic type"} and out["layers"] == 7 and out["rasters"] == 1
        assert [r.name for r in rl.rasters] == ["DEM"] and "The raster Foto cannot be given" in caplog.text
    finally:
        rl.stop()


def test_a_raster_without_a_band_is_refused_with_its_reason(tmp_path, caplog):
    """A dataset with no band of its own - a GeoPackage holding two raster tables, which GDAL opens as their container
    - is not given to RustGIS: in "refused" with what the file holds (its subdatasets), said in the log without a
    traceback; the other rasters and the tables go all the same."""
    container = _two_tables(tmp_path / "due.gpkg")
    dem = _small_dem(tmp_path / "dem.tif")
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    rl = link.RustGisLink(live, FakeEngine(), "Saggio 1", GB)
    try:
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            out = rl.start([{"path": container, "name": "Due"}, {"path": dem, "name": "DEM"}])
        reason = out["refused"]["Due"]
        assert "no raster band of its own" in reason and "2 rasters" in reason and ":alfa" in reason
        assert ":beta" in reason and out["rasters"] == 1 and [r.name for r in rl.rasters] == ["DEM"]
        said = [r for r in caplog.records if "The raster Due cannot be given" in r.getMessage()]
        assert len(said) == 1 and not said[0].exc_info and reason in said[0].getMessage()
    finally:
        rl.stop()


def test_a_copy_needs_room_on_the_disk(tmp_path, monkeypatch):
    """A copy is made only with room for it on the disk: 1.5 times its size free - the size its pixels give, which
    can be far larger than the raster's (a VRT is a few kilobytes; a WebP copied as Deflate grows) - and 2 GB left
    after it. Else nothing is written and the raster, which RustGIS cannot read, is refused, saying why."""
    import rasterio.shutil

    good = _header_only(tmp_path / "buona.tif", 8000, 6000, 4)
    mosaic = tmp_path / "mosaico.vrt"
    rasterio.shutil.copy(good, mosaic, driver="VRT")
    webp = _header_only(tmp_path / "foto.tif", 8000, 6000, 3, None, compress="webp", **TILED)
    assert link._copy_bytes(mosaic) == 8000 * 6000 * 4 * 4 // 3  # Deflate, 4 bands, a third more for the overviews
    assert link._copy_bytes(webp) == 8000 * 6000 * 3 * 4 // 3 // 6  # JPEG: about a sixth
    data_dir = tmp_path / "chunk_1"
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine()
    rl = link.RustGisLink(live, fake, "Saggio 1", GB, data_dir=data_dir)
    try:
        for raster, name, free in ((mosaic, "Mosaico", 300_000_000),  # less than 1.5 times its 256 MB
                                   (mosaic, "Mosaico", 1_000_000_000),  # 1.5 times, but less than 2 GB left after
                                   (webp, "Foto", 2_020_000_000)):  # 32 MB: 1.988 GB left after it
            monkeypatch.setattr(link.shutil, "disk_usage",
                                lambda folder, free=free: shutil._ntuple_diskusage(10**12, 10**12 - free, free))
            out = rl.start([{"path": raster, "name": name}])
            assert rl.rasters == [] and "needs 1.5 times that free and 2 GB left after it" in out["refused"][name]
        assert [p for p in (data_dir / "rustgis").iterdir()] == []  # nothing written
    finally:
        rl.stop()


def test_stop_closes_the_server_whatever_happens(tmp_path, monkeypatch):
    """stop() closes the raster server and forgets the link's state even when taking the tile layers away fails in
    a way it did not foresee."""
    large = _header_only(tmp_path / "ortofoto.tif", 8000, 6000, 4)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    rl = link.RustGisLink(live, FakeEngine(), "Saggio 1", GB)
    rl.start([{"path": large, "name": "Ortofoto"}])
    server = rl.server

    def broken(base):
        raise RuntimeError("something unforeseen")

    monkeypatch.setattr(rl, "_remove_tiles", broken)
    with pytest.raises(RuntimeError, match="unforeseen"):
        rl.stop()
    assert rl.server is None and rl.rasters == [] and rl.layers == {} and not server.thread.is_alive()


def test_a_raster_downloaded_whole_or_not_served(tmp_path, monkeypatch, caplog):
    """When RustGIS downloads a raster given by its address whole (as it does a GeoTIFF it cannot read in place), it
    is a layer of it: in the group, and said in the log once its answer is read. One the server cannot give (a file
    gone, a server that cannot start) is left out and said in "refused" - two of the same name each under its own
    key; the link starts all the same."""
    large = _header_only(tmp_path / "ortofoto.tif", 8000, 6000, 4)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine()
    fake.whole = True
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    try:
        with caplog.at_level(logging.INFO, logger=link.__name__):
            assert rl.start([{"path": large, "name": "Ortofoto"}])["rasters"] == 1
        (ortho,) = rl.rasters
        assert ortho.layer and ortho.url is None and int(ortho.layer) in fake.groups[-1]["layers"]
        assert fake.fetched[-1][1] is None and "downloaded the raster Ortofoto whole" in caplog.text

        def gone(self, path):
            raise FileNotFoundError(f"gone: {path}")

        monkeypatch.setattr(link.RasterServer, "url", gone)
        out = rl.start([{"path": large, "name": "Ortofoto"}, {"path": large, "name": "Ortofoto"}])
        assert out["rasters"] == 0 and out["layers"] == 7 and sorted(out["refused"]) == ["Ortofoto", "Ortofoto (2)"]
        assert all("gone" in reason for reason in out["refused"].values()) and rl.rasters == [] and rl.layers
    finally:
        rl.stop()


class FakeFiles:
    """The calls of kernel32 the raster server makes to open a file on Windows, answering as the test says: each
    CreateFileW gives the next handle; ``tag`` is (attributes, reparse tag) of what is opened - the attributes given
    with BY_HANDLE_FILE_INFORMATION (``informed`` says whether that is read), the tag with FileAttributeTagInfo
    (``tagged``; the handles it was asked of in ``asked``); ``files`` the (volume serial number, file index high, low)
    of each handle."""

    def __init__(self):
        self.next, self.handles = 4321, []
        self.tag, self.tagged, self.informed = (0x20, 0), 1, 1  # FILE_ATTRIBUTE_ARCHIVE: a plain file
        self.files: dict[int, tuple] = {}
        self.opened, self.closed, self.asked = [], [], []

    def CreateFileW(self, name, access, share, security, disposition, flags, template):
        self.opened.append((name, access, share, security, disposition, flags, template))
        handle, self.next = self.next, self.next + 1
        self.handles.append(handle)
        self.files.setdefault(handle, (7, 0, 99))
        return handle

    def GetFileInformationByHandleEx(self, handle, kind, information, size):
        assert (kind, size) == (9, 8)  # FileAttributeTagInfo: attributes and reparse tag
        self.asked.append(handle)
        information._obj.attributes, information._obj.reparse_tag = self.tag
        return self.tagged

    def GetFileInformationByHandle(self, handle, information):
        information._obj.attributes = self.tag[0]
        information._obj.volume, information._obj.index_high, information._obj.index_low = self.files[handle]
        return self.informed

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


def test_on_windows_a_served_file_is_shared_and_no_link(tmp_path, monkeypatch):
    """On Windows the raster server opens a file with CreateFileW shared for reading, writing and deleting - the
    program replaces or deletes a product RustGIS is reading a tile of - and with FILE_FLAG_OPEN_REPARSE_POINT. Its
    attributes and identity are read from the handle (BY_HANDLE_FILE_INFORMATION; not readable: refused); only a
    reparse point has its tag asked for, as CPython's lstat and Rust's std do: a name surrogate (a symbolic link, a
    junction) put where the file was is refused, as O_NOFOLLOW refuses a link elsewhere, and so is a reparse point
    whose tag cannot be read; another reparse point (deduplicated, a OneDrive or Dropbox placeholder) is a file
    itself: opened again without the flag and kept when it is the same file (volume serial number, file index).
    Every handle not handed over is closed exactly once. The address and the answers go through the same opening."""
    import ctypes

    raster = tmp_path / "ortofoto.tif"
    raster.write_bytes(b"pixels")
    files = FakeFiles()
    taken = []
    monkeypatch.setattr(link, "_platform", lambda: "win32")
    monkeypatch.setattr(link, "_kernel32", lambda: files)
    monkeypatch.setattr(link, "_osfhandle", lambda handle: taken.append(handle) or os.open(raster, os.O_RDONLY))

    def opened():
        before = (len(files.opened), len(files.closed), len(taken), len(files.asked))
        handle = link._open_regular(raster)
        data = None
        if handle is not None:
            with handle:
                data = handle.read()
        return data, files.opened[before[0]:], files.closed[before[1]:], taken[before[2]:], files.asked[before[3]:]

    # a plain file: one opening, no tag asked for, handed to the descriptor
    data, made, closed, handed, asked = opened()
    # GENERIC_READ; FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE; OPEN_EXISTING; FILE_FLAG_OPEN_REPARSE_POINT
    assert made == [(str(raster), 0x80000000, 0x7, None, 3, 0x00200000, None)]
    assert (data, closed, handed, asked) == (b"pixels", [], [files.handles[-1]], [])
    # a name surrogate (IO_REPARSE_TAG_SYMLINK): its tag asked for, refused, its handle closed
    files.tag = (0x400 | 0x20, 0xA000000C)
    data, made, closed, handed, asked = opened()
    assert (data, len(made), closed, handed, asked) == (None, 1, [files.handles[-1]], [], [files.handles[-1]])
    # a cloud placeholder (IO_REPARSE_TAG_CLOUD_6) or a deduplicated file (IO_REPARSE_TAG_DEDUP): a file itself
    for tag in (0x9000601A, 0x80000013):
        files.tag = (0x400 | 0x20, tag)
        data, made, closed, handed, asked = opened()
        first, second = files.handles[-2:]
        assert [m[5] for m in made] == [0x00200000, 0] and [m[2] for m in made] == [0x7, 0x7]  # the same sharing
        assert (data, closed, handed, asked) == (b"pixels", [first], [second], [first])
    # opened again, another file (something put there in between): refused, both closed once
    files.next = 5000
    files.files[5001] = (7, 0, 100)
    data, made, closed, handed, asked = opened()
    assert (data, sorted(closed), handed) == (None, [5000, 5001], [])
    # the tag of a reparse point not read: refused; a plain file is never asked for its tag
    files.tagged = 0
    data, made, closed, handed, asked = opened()
    assert (data, closed, asked) == (None, [files.handles[-1]], [files.handles[-1]])
    files.tag = (0x20, 0)
    data, made, closed, handed, asked = opened()
    assert (data, closed, asked) == (b"pixels", [], [])
    files.tagged = 1
    # its attributes and identity not read, or nothing opened: refused
    files.informed = 0
    data, made, closed, handed, asked = opened()
    assert (data, closed, handed, asked) == (None, [files.handles[-1]], [], [])
    files.informed, files.next = 1, ctypes.c_void_p(-1).value  # INVALID_HANDLE_VALUE: nothing to close
    data, made, closed, handed, asked = opened()
    assert (data, len(made), closed) == (None, 1, [])
    assert sorted(files.closed) == sorted(set(files.closed))  # never a handle closed twice
    files.next = 6000
    server = link.RasterServer()
    try:
        path = urllib.parse.urlsplit(server.url(raster)).path
        assert _http(server.server_address[1], path)[2] == b"pixels"
    finally:
        server.close()
    assert [m[0] for m in files.opened[-2:]] == [str(raster)] * 2  # the address, the answer


@pytest.mark.skipif(sys.platform != "win32", reason="Windows only: the sharing of a file opened by the raster server")
def test_on_windows_a_product_read_by_rustgis_can_be_replaced(tmp_path):
    """Windows, a real file: while the raster server holds a product open for RustGIS, the program puts the product
    rebuilt in its place with replace_file - a rename with POSIX semantics, as os.replace (MoveFileEx) refuses to
    replace a file with a handle open on it, FILE_SHARE_DELETE or not; the handle goes on reading the file it opened,
    which lives on nameless until it is closed."""
    from pyarchinit_photogrammetry.io.files import replace_file

    product = tmp_path / "orthomosaic.tif"
    product.write_bytes(b"the old pixels")
    rebuilt = tmp_path / "orthomosaic.tmp.tif"
    rebuilt.write_bytes(b"the new pixels")
    handle = link._open_regular(product)
    assert handle is not None
    try:
        replace_file(rebuilt, product)
        assert product.read_bytes() == b"the new pixels" and not rebuilt.exists()
        assert handle.read() == b"the old pixels"
    finally:
        handle.close()
    assert [p.name for p in tmp_path.iterdir()] == ["orthomosaic.tif"]


class FakeRenames:
    """The calls of kernel32 replace_file makes on Windows, answering as the test says: CreateFileW gives a handle
    (INVALID_HANDLE_VALUE when ``invalid``); SetFileInformationByHandle records what it is given - the name as the
    bytes of the FileName field - and, when ``renames`` and not among the first ``refuse`` calls, renames as Windows
    would: to the FileNameLength bytes of FileName read as UTF-16."""

    def __init__(self, renames: bool = True):
        self.renames, self.invalid, self.refuse = renames, False, 0
        self.opened, self.renamed, self.closed = [], [], []

    def CreateFileW(self, name, access, share, security, disposition, flags, template):
        import ctypes

        self.opened.append((name, access, share, security, disposition, flags, template))
        return ctypes.c_void_p(-1).value if self.invalid else 777

    def SetFileInformationByHandle(self, handle, kind, information, size):
        given = information._obj
        raw = bytes(given.name)
        self.renamed.append((handle, kind, given.flags, given.root, given.length, raw, size))
        if self.refuse:
            self.refuse -= 1
            return 0
        if self.renames:
            os.replace(self.opened[-1][0], raw[:given.length].decode("utf-16-le", "surrogatepass"))
        return int(self.renames)

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


def test_replace_file_renames_with_posix_semantics_on_windows(tmp_path, monkeypatch):
    """replace_file puts a file in the place of another in one move: os.replace, but on Windows first a rename with
    POSIX semantics - the file opened for DELETE, shared with every other opening, SetFileInformationByHandle with
    FileRenameInfoEx, FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS and the new name whole,
    sized as Windows counts it (UTF-16 units: a character outside the BMP is two) - which a handle open on the file
    replaced (the raster server's, while RustGIS reads a product) does not stop, as it stops os.replace (MoveFileEx).
    Where Windows does not do that rename, os.replace; both tried again after each of RETRY_DELAYS while a handle
    refuses them (an antivirus holding the file just written refuses the rename for a moment), then the refusal of
    os.replace raised."""
    import ctypes

    from pyarchinit_photogrammetry.io import files

    def written(final: str = "orthomosaic.tif") -> tuple[Path, Path]:
        src, dst = tmp_path / "orthomosaic.tmp.tif", tmp_path / final
        src.write_bytes(b"new")
        dst.write_bytes(b"old")
        return src, dst

    slept: list = []
    monkeypatch.setattr(files.time, "sleep", slept.append)
    # not Windows: os.replace itself, kernel32 never asked for, no second try
    monkeypatch.setattr(files, "_windows", lambda: False)
    monkeypatch.setattr(files, "_kernel32", lambda: pytest.fail("kernel32 asked for outside Windows"))
    src, dst = written()
    files.replace_file(src, dst)
    assert (dst.read_bytes(), src.exists(), slept) == (b"new", False, [])
    monkeypatch.setattr(files, "_replace", lambda s, d: (_ for _ in ()).throw(PermissionError(13, "held")))
    src, dst = written()
    with pytest.raises(PermissionError):
        files.replace_file(src, dst)
    assert (dst.read_bytes(), src.exists(), slept) == (b"old", True, [])
    # Windows: the rename with POSIX semantics, the handle closed, os.replace not needed
    monkeypatch.setattr(files, "_windows", lambda: True)
    fake = FakeRenames()
    monkeypatch.setattr(files, "_kernel32", lambda: fake)
    for final in ("orthomosaic.tif", "orto \U0001d538.tif"):  # a name with a character outside the BMP
        src, dst = written(final)
        files.replace_file(src, dst)
        assert (dst.read_bytes(), src.exists(), slept) == (b"new", False, []), final
        # DELETE; FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE; OPEN_EXISTING; FILE_FLAG_BACKUP_SEMANTICS |
        # FILE_FLAG_OPEN_REPARSE_POINT
        assert fake.opened[-1] == (str(src), 0x00010000, 0x7, None, 3, 0x02000000 | 0x00200000, None)
        encoded = os.path.abspath(dst).encode("utf-16-le", "surrogatepass")
        handle, kind, flags, root, length, raw, size = fake.renamed[-1]
        assert (handle, kind, flags, root, length) == (777, 22, 0x3, None, len(encoded))  # the length in bytes
        assert raw[:length] == encoded and raw[length:length + 2] == b"\0\0"  # the name whole, then its end
        assert size >= 20 + len(encoded) + 2 and fake.closed[-1] == 777  # FILE_RENAME_INFO with the name and its end
    assert len(fake.closed) == 2 and ctypes.sizeof(ctypes.c_uint32) == 4  # one opening per rename; DWORDs of 4
    # the rename refused once (an antivirus holds the file just written) while os.replace cannot replace the file
    # RustGIS holds: the rename is tried again after the first delay, and does it
    refusals = [PermissionError(13, "held by RustGIS")] * 9
    replaced: list = []

    def replace(s, d):
        if refusals:
            raise refusals.pop()
        replaced.append((s, d))
        os.replace(s, d)

    monkeypatch.setattr(files, "_replace", replace)
    fake.refuse = 1
    src, dst = written()
    files.replace_file(src, dst)
    assert (dst.read_bytes(), replaced, slept, len(fake.renamed)) == (b"new", [], [files.RETRY_DELAYS[0]], 4)
    assert len(fake.closed) == 4  # the handle of the refused rename closed too
    # the rename never done (another file system, an older Windows): os.replace, tried again while a handle refuses
    # it, the rename tried again before each try
    slept.clear()
    refusals[:] = [PermissionError(13, "held by an antivirus")] * 2
    fake.renames = False
    src, dst = written()
    files.replace_file(src, dst)
    assert (dst.read_bytes(), replaced, slept) == (b"new", [(src, dst)], list(files.RETRY_DELAYS[:2]))
    assert len(fake.renamed) == 7 and len(fake.closed) == 7
    # refused every time: the last refusal of os.replace raised, after every delay, the rename tried each time
    slept.clear()
    refusals[:] = [PermissionError(13, "held")] * 9
    src, dst = written()
    with pytest.raises(PermissionError, match="held"):
        files.replace_file(src, dst)
    assert (dst.read_bytes(), slept, len(fake.renamed)) == (b"old", list(files.RETRY_DELAYS), 12)
    # the file not opened (INVALID_HANDLE_VALUE): os.replace, nothing to close
    fake.invalid, fake.renames = True, True
    refusals.clear()
    slept.clear()
    src, dst = written()
    files.replace_file(src, dst)
    assert (dst.read_bytes(), slept, len(fake.closed), len(fake.renamed)) == (b"new", [], 12, 12)

def _orthophoto(path: Path, side: int, pixel: float, crs: str, x0: float, y0: float, colour=None) -> Path:
    """An orthophoto as engine/ortho.py writes one: RGBA with its alpha, Deflate, tiles of 256 pixels, BigTIFF when
    needed, the overviews built inside after the tiles (not a COG, whose directories come first); its pixels a pattern,
    or one ``colour``."""
    import numpy as np
    import rasterio
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.transform import from_origin
    from rasterio.windows import Window

    profile = {"driver": "GTiff", "height": side, "width": side, "count": 4, "dtype": "uint8",
               "transform": from_origin(x0, y0, pixel, pixel), "compress": "deflate", "photometric": "RGB",
               "BIGTIFF": "IF_SAFER", "tiled": True, "blockxsize": 256, "blockysize": 256, "crs": crs}
    rows, cols = np.mgrid[0:256, 0:side]
    with rasterio.open(path, "w", **profile) as dst:
        dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        for r0 in range(0, side, 256):  # a row of tiles at a time, as they are made
            if colour is None:
                band = np.stack([cols % 251, (rows + r0) % 241, (rows + r0 + cols) % 239, np.full(cols.shape, 255)])
            else:
                band = np.stack([np.full(cols.shape, c) for c in (*colour, 255)])
            dst.write(band.astype("uint8"), window=Window(0, r0, side, 256))
        dst.build_overviews([f for f in (2, 4, 8, 16, 32, 64, 128) if side // f >= 64], Resampling.average)
    return path


def _drawn_colour(engine, png: Path, fx: float = 0.5) -> tuple:
    """The colour RustGIS's engine draws at the middle row, ``fx`` across, of a map it exports to ``png``."""
    import numpy as np
    from PIL import Image

    engine.call("run_command", {"command": "share.exportMap", "params": {"path": str(png), "width": 160,
                                                                       "height": 160}})
    pixels = np.asarray(Image.open(png).convert("RGB")).astype(int)
    h, w = pixels.shape[:2]
    x = int(w * fx)
    return tuple(pixels[h // 2 - 2:h // 2 + 3, x - 2:x + 3].reshape(-1, 3).mean(0).round().astype(int).tolist())


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_rustgis_reads_our_orthophoto_by_range(tmp_path, monkeypatch):
    """RustGIS's own engine and an orthophoto written as engine/ortho.py writes one: the link serves it, layer.addUrl
    takes it in place as a tile layer (kind cog) after reading its header by range, and a map drawn where the full
    resolution is needed reads full-resolution tiles only, each request one range of whole tiles - never the whole
    file. stop() takes the tile layer away. The pixels are 50 cm: the engine's map goes no closer than 1:399."""
    import rasterio

    x0, y0, side, crs = 291000.0, 4640000.0, 2048, "EPSG:32633"
    ortho = _orthophoto(tmp_path / "orthomosaic.tif", side, 0.5, crs, x0, y0)
    with rasterio.open(ortho) as src:  # the bytes of each full-resolution tile
        tiles = [(int(src.get_tag_item(f"BLOCK_OFFSET_{c}_{r}", "TIFF", bidx=1)),
                  int(src.get_tag_item(f"BLOCK_SIZE_{c}_{r}", "TIFF", bidx=1))) for r in range(8) for c in range(8)]
    starts, ends = {s for s, _ in tiles}, {s + n - 1 for s, n in tiles}
    asked = []
    serve = link._RasterRequest._serve

    def recording(self, body):
        asked.append((self.command, self.headers.get("Range")))
        return serve(self, body)

    monkeypatch.setattr(link._RasterRequest, "_serve", recording)
    monkeypatch.setattr(link, "RASTER_SAMPLES", 1_000_000)  # 2048 x 2048 x 4 goes by address
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([_unit(1, x0 + 500, y0 - 500)]))
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        rl = link.RustGisLink(live, engine, "Saggio 1", crs)
        try:
            assert rl.start([{"path": ortho, "name": "Ortofoto"}])["rasters"] == 1
            (given,) = rl.rasters
            assert given.url and given.layer is None
            (tile,) = engine.call("list_layers", {})["tileLayers"]
            assert (tile["name"], tile["kind"], tile["url"]) == ("Ortofoto", "cog", given.url)
            assert asked and all(method == "GET" and spec for method, spec in asked)  # its header, by range
            for command, params in (("map.zoomToTiles", {"name": "Ortofoto"}), ("map.setScale", {"scale": 800})):
                engine.call("run_command", {"command": command, "params": params})
            asked.clear()
            engine.call("run_command", {"command": "share.exportMap", "params": {
                "path": str(tmp_path / "mappa.png"), "width": 1024, "height": 1024}})
            spans = [tuple(map(int, spec.removeprefix("bytes=").split("-"))) for _, spec in asked]
            assert spans and all(first in starts and last in ends for first, last in spans), asked
            assert sum(last - first + 1 for first, last in spans) < ortho.stat().st_size / 2
            address = given.url
        finally:
            rl.stop()
        assert engine.call("list_layers", {})["tileLayers"] == []  # taken away by stop()
    with pytest.raises(OSError):
        _http(urllib.parse.urlsplit(address).port, urllib.parse.urlsplit(address).path)


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_hidden_dems_leave_the_orthophoto_on_top_with_the_real_engine(tmp_path, monkeypatch):
    """RustGIS's own engine: an orthophoto read in place and two DEMs given as files, hidden, over the same ground
    (as open_in_qgis gives them): the map shows the orthophoto; one DEM shown again covers it - which is why they go
    hidden."""
    x0, y0, crs = 291000.0, 4640000.0, "EPSG:32633"
    ortho = _orthophoto(tmp_path / "orthomosaic.tif", 512, 0.5, crs, x0, y0, colour=(220, 20, 20))
    dem = _small_dem(tmp_path / "dem.tif", side=256, crs=crs, x0=x0, y0=y0 - 256, pixel=1.0)
    monkeypatch.setattr(link, "RASTER_SAMPLES", 500_000)  # 512 x 512 x 4 by address, the DEM (256 x 256) as a file
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([_unit(1, x0 + 50, y0 - 50)]))
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        rl = link.RustGisLink(live, engine, "Saggio 1", crs)
        try:
            rl.start([{"path": ortho, "name": "Ortofoto"},
                      {"path": dem, "name": "DEM (ombreggiato)", "hidden": True, "opacity": 0.6},
                      {"path": dem, "name": "DEM", "hidden": True}])
            ortho_r, shaded, plain = rl.rasters
            assert ortho_r.url and shaded.layer and plain.layer
            visible = {str(l["id"]): l.get("visible") for l in engine.call("list_layers", {})["layers"]}
            assert (visible[shaded.layer], visible[plain.layer]) == (False, False)
            engine.call("run_command", {"command": "map.zoomToTiles", "params": {"name": "Ortofoto"}})
            seen = _drawn_colour(engine, tmp_path / "mappa.png")
            assert max(abs(a - b) for a, b in zip(seen, (220, 20, 20))) <= 3, seen
            engine.call("run_command", {"command": "layer.visibility", "params": {"layer": [int(plain.layer)],
                                                                                   "visible": True}})
            covered = _drawn_colour(engine, tmp_path / "coperta.png")
            assert max(abs(a - b) for a, b in zip(covered, (220, 20, 20))) > 30, covered
        finally:
            rl.stop()


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_stop_takes_away_only_our_tile_layers_with_the_real_engine(tmp_path, monkeypatch):
    """RustGIS's own engine: the link's orthophoto read in place, given hidden and half transparent (set through its
    mark, its name given back), then a tile layer of the user's (XYZ) and a COG of the user's read from another server,
    both named as the link's: stop() takes away only the link's, the user's keep their names."""
    x0, y0, crs = 291000.0, 4640000.0, "EPSG:32633"
    ortho = _orthophoto(tmp_path / "orthomosaic.tif", 512, 0.5, crs, x0, y0)
    monkeypatch.setattr(link, "RASTER_SAMPLES", 500_000)
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([_unit(1, x0 + 50, y0 - 50)]))
    theirs = link.RasterServer()
    try:
        with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
            rl = link.RustGisLink(live, engine, "Saggio 1", crs)
            try:
                rl.start([{"path": ortho, "name": "Ortofoto", "hidden": True, "opacity": 0.4}])
                (tile,) = engine.call("list_layers", {})["tileLayers"]
                assert (tile["name"], tile["visible"], round(tile["opacity"], 3)) == ("Ortofoto", False, 0.4)
                xyz = "http://127.0.0.1:9/{z}/{x}/{y}.png"
                engine.call("run_command", {"command": "map.addTiles", "params": {"url": xyz, "name": "Ortofoto"}})
                cog = theirs.url(ortho)
                engine.call("run_command", {"command": "layer.addUrl", "params": {"url": cog, "name": "Ortofoto"}})
                assert len(engine.call("list_layers", {})["tileLayers"]) == 3
            finally:
                rl.stop()
            left = engine.call("list_layers", {})["tileLayers"]
            assert sorted((t["url"], t["name"]) for t in left) == sorted([(cog, "Ortofoto"), (xyz, "Ortofoto")])
    finally:
        theirs.close()


class Hooked:
    """A transport that calls ``before`` once, right before the next script it is asked to run (armed by setting it)."""

    def __init__(self, transport):
        self.transport, self.before = transport, None

    @property
    def timeout(self):
        return self.transport.timeout

    @timeout.setter
    def timeout(self, seconds):
        self.transport.timeout = seconds

    def initialize(self) -> str:
        return self.transport.initialize()

    def call(self, tool: str, args: dict) -> dict:
        if tool == "run" and self.before is not None:
            before, self.before = self.before, None
            before()
        return self.transport.call(tool, args)


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_tile_layers_go_by_their_marks_with_the_real_engine(tmp_path, monkeypatch):
    """RustGIS's own engine and the reviewer's sequences: two links whose stops interleave (the second reads the
    list, the first stops whole, the second renames), and a basemap the user adds between a link's reading and its
    renaming. The user's tile layers always stay, with their names (but one added meanwhile, which keeps the mark);
    nothing but a link's tile layer is removed; a link's that moved meanwhile stays."""
    x0, y0, crs = 291000.0, 4640000.0, "EPSG:32633"
    ortho = _orthophoto(tmp_path / "orthomosaic.tif", 512, 0.5, crs, x0, y0)
    monkeypatch.setattr(link, "RASTER_SAMPLES", 500_000)
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([_unit(1, x0 + 50, y0 - 50)]))
    user = "http://127.0.0.1:9/user/{z}/{x}/{y}.png"

    def tiles(engine):
        return [(t["name"], t["url"]) for t in engine.call("list_layers", {})["tileLayers"]]

    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        hooked = Hooked(engine)
        first, second = link.RustGisLink(live, engine, "Saggio 1", crs), link.RustGisLink(live, hooked, "Saggio 2", crs)
        try:
            engine.call("run_command", {"command": "map.addTiles", "params": {"url": user,
                                                                               "name": "Basemap dell'utente"}})
            second.start([{"path": ortho, "name": "Ortofoto (B)"}])
            first.start([{"path": ortho, "name": "Ortofoto (A)"}])
            a, b = first.rasters[0].url, second.rasters[0].url
            assert [url for _, url in tiles(engine)] == [a, b, user]
            hooked.before = first.stop
            second.stop()
            left = tiles(engine)
            assert ("Basemap dell'utente", user) in left and a not in [url for _, url in left]
            assert all(url in (b, user) for _, url in left)  # the second's stays (it moved) or went: nothing else
        finally:
            first.stop()
            second.stop()
        for _ in tiles(engine):  # an empty map for the next sequence
            engine.call("run", {"script": 'map.removeTiles {"index":0}'})
        # a basemap the user adds between the link's reading and its renaming
        rl = link.RustGisLink(live, hooked, "Saggio 1", crs)
        try:
            engine.call("run_command", {"command": "map.addTiles", "params": {"url": user,
                                                                               "name": "Basemap dell'utente"}})
            rl.start([{"path": ortho, "name": "Ortofoto"}])
            ours = rl.rasters[0].url
            late = "http://127.0.0.1:9/late/{z}/{x}/{y}.png"
            hooked.before = lambda: engine.call("run_command", {"command": "map.addTiles", "params": {
                "url": late, "name": "Altro sfondo"}})
            rl.stop()
            left = tiles(engine)
            assert ("Basemap dell'utente", user) in left and late in [url for _, url in left]
            assert all(url in (late, ours, user) for _, url in left)
        finally:
            rl.stop()


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_a_copy_for_a_codec_rustgis_cannot_read_with_the_real_engine(tmp_path, monkeypatch):
    """RustGIS's own engine and rasters it decodes neither as files nor in place (WebP): each goes as a copy in the
    chunk's data folder (JPEG of RGB pixels) - a small one as its file, a larger one by its address - which RustGIS
    draws in its colours."""
    x0, y0, crs = 291000.0, 4640000.0, "EPSG:32633"
    small = _two_tone(tmp_path / "foto.tif", 3, side=256, crs=crs, x0=x0, y0=y0, pixel=1.0, compress="webp", **TILED)
    large = _two_tone(tmp_path / "grande.tif", 3, side=1024, crs=crs, x0=x0, y0=y0, pixel=0.25, compress="webp",
                      **TILED)
    monkeypatch.setattr(link, "RASTER_SAMPLES", 1_000_000)  # 1024 x 1024 x 3 above it, 256 x 256 x 3 below
    data_dir = tmp_path / "chunk_1"
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([_unit(1, x0 + 50, y0 + 50)]))
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        rl = link.RustGisLink(live, engine, "Saggio 1", crs, data_dir=data_dir)
        try:
            for raster, name, how in ((small, "Foto", "layer"), (large, "Grande", "url")):
                rl.start([{"path": raster, "name": name}])
                (given,) = rl.rasters
                assert given.copy.parent == data_dir / "rustgis" and getattr(given, how)
                if how == "url":
                    engine.call("run_command", {"command": "map.zoomToTiles", "params": {"name": name}})
                else:
                    engine.call("run_command", {"command": "layer.zoomTo", "params": {"layer": int(given.layer)}})
                left = _drawn_colour(engine, tmp_path / "mappa.png", 0.4)
                right = _drawn_colour(engine, tmp_path / "mappa.png", 0.6)
                assert max(abs(a - b) for a, b in zip(left, (200, 60, 30))) <= 4, (name, left)
                assert max(abs(a - b) for a, b in zip(right, (30, 60, 200))) <= 4, (name, right)
                engine.call("run_command", {"command": "layer.remove", "params": {"layer": [
                    int(lid) for lid in rl.layers.values()] + ([int(given.layer)] if given.layer else [])}})
        finally:
            rl.stop()


class Recording:
    """A transport that keeps the scripts sent through it."""

    def __init__(self, transport):
        self.transport, self.scripts = transport, []

    def initialize(self) -> str:
        return self.transport.initialize()

    def call(self, tool: str, args: dict) -> dict:
        if tool == "run":
            self.scripts.append(args["script"])
        return self.transport.call(tool, args)


def _export_of(engine, layer, path: Path) -> list:
    """What RustGIS has of a layer, from its own export: ``[(geometry, values)]``, normalized."""
    path.unlink(missing_ok=True)
    engine.call("run_command", {"command": "share.exportLayer", "params": {"layer": int(layer), "path": str(path)}})
    return [link.normalize(g, v) for g, v in link._read_export(path)[2]]


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
@pytest.mark.parametrize("crs, x0, y0", [("EPSG:32633", 291000.0, 4640000.0), ("EPSG:7792", 291000.0, 4640000.0),
                                         ("EPSG:3004", 2480000.0, 4500000.0)])
def test_push_and_pull_with_the_real_engine(tmp_path, crs, x0, y0):
    """RustGIS's own engine on an empty project: the link starts (the system accepted), the program's edits are pushed
    - a unit moved with accented text, one deleted, a section into the empty table, 300 spot heights (several where
    clauses) - and RustGIS's export has them within a millimetre; nothing is sent again; a unit drawn there gets its
    identity back once; the last send run a second time leaves the same layers."""
    one, two = _unit(1, x0, y0), _unit(2, x0 + 1, y0)
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([one, two]))
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        transport = Recording(engine)
        rl = link.RustGisLink(live, transport, "Saggio 1", crs)
        out = rl.start([])
        assert out["layers"] == 7 and out["rasters"] == 0 and not link.too_old(out["version"])
        listing = engine.call("list_layers", {})
        assert [g["name"] for g in listing["groups"]] == ["Saggio 1"]
        assert sorted(l["name"] for l in listing["layers"]) == sorted(t for t, _ in LIVE_TABLES.values())
        moved = dict(one, _geom=_square(x0 + 0.05, y0), tipo_us_s=TEXT)
        section = {"etichetta": "Sezione 1", ID_FIELD: new_id(), "lunghezza_m": 2.0,
                   "_geom": ("LineString", [[x0, y0 + 2], [x0 + 2, y0 + 2]])}
        spots = [_spot(99.0 + n / 1000, x0 + n * 0.01, y0 + 1) for n in range(300)]
        live.write(_drawings([moved], spots, [section]))
        assert rl.push() == 1 + 1 + 1 + 300  # unit 1, unit 2 gone, the section, the spot heights
        units = _export_of(engine, rl.layers["unita"], tmp_path / "u.gpkg")
        assert [v[ID_FIELD] for _, v in units] == [one[ID_FIELD]]
        assert link._same_geometry(units[0][0], Polygon(moved["_geom"][1])) and units[0][1]["tipo_us_s"] == TEXT
        lines = _export_of(engine, rl.layers["sezioni"], tmp_path / "s.gpkg")
        assert [v[ID_FIELD] for _, v in lines] == [section[ID_FIELD]] and lines[0][1]["lunghezza_m"] == 2.0
        assert link._same_geometry(lines[0][0], shapely.geometry.LineString(section["_geom"][1]))
        heights = _export_of(engine, rl.layers["quote"], tmp_path / "q.gpkg")
        assert sorted(v[ID_FIELD] for _, v in heights) == sorted(s[ID_FIELD] for s in spots)
        assert rl.push() == 0 and len(transport.scripts) == 2  # the system's, the send's
        assert rl.pull() == {"unita": (0, 0, 0), "quote": (0, 0, 0), "sezioni": (0, 0, 0)}
        assert live.read_changes() == {} and not rl.status_changed()
        # a unit drawn there: its identity goes back, once
        engine.call("run_command", {"command": "edit.addFeature", "params": {
            "layer": int(rl.layers["unita"]), "geometry": _lonlat(box(x0 + 3, y0, x0 + 3.5, y0 + 0.5), crs),
            "attributes": {"us_s": "12", "tipo_us_s": TEXT}}})
        assert rl.status_changed()
        assert rl.pull()["unita"] == (0, 1, 0)
        (drawn,) = live.read_changes()["unita"].added
        live.write(_drawings([moved, _taken(drawn, us_s="12", tipo_us_s=TEXT)],
                             spots, [section]))
        assert rl.push() == 1
        units = _export_of(engine, rl.layers["unita"], tmp_path / "u.gpkg")
        assert sorted(v[ID_FIELD] for _, v in units) == sorted([one[ID_FIELD], drawn.id])
        before = {t: _export_of(engine, rl.layers[t], tmp_path / f"{t}-1.gpkg") for t in TWO_WAY_TABLES}
        engine.call("run", {"script": transport.scripts[-1]})  # the send, delivered a second time
        after = {t: _export_of(engine, rl.layers[t], tmp_path / f"{t}-2.gpkg") for t in TWO_WAY_TABLES}
        for table in TWO_WAY_TABLES:
            key = lambda row: (str(row[1].get(ID_FIELD)), row[0].wkb_hex)  # noqa: E731
            assert sorted(map(key, after[table])) == sorted(map(key, before[table]))
        assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_a_system_the_real_engine_misplaces_is_refused(tmp_path):
    """RustGIS 0.4.0 takes the Soldner (Cassini) system of Berlin without converting it: the link refuses it, naming
    it, and leaves nothing in RustGIS."""
    live = LiveLink(tmp_path / "qgis", "EPSG:3068")
    live.write(_drawings([_unit(1, 25000.0, 21000.0)]))
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        with pytest.raises(link.RustGisRefused, match="EPSG:3068"):
            link.RustGisLink(live, engine, "Saggio 1", "EPSG:3068").start([])
        assert engine.call("list_layers", {})["layers"] == []


# ---- fix round 1 of the push: copies, the status, foreign layers, adoption, leftovers, set aside

def _moved_last(layer: dict, crs: str, dx: float) -> None:
    """The last feature of a layer moved ``dx`` metres east (a copy, moved off the feature it copies)."""
    feature = layer["features"][-1]
    native = _reprojected(feature["geometry"], "EPSG:4326", crs)
    feature["geometry"] = _reprojected(affinity.translate(native, xoff=dx), crs, "EPSG:4326")


def test_a_unit_pasted_in_rustgis_keeps_both(tmp_path):
    """Finding 1: unit 1 copied and pasted in RustGIS - the copy carries unit 1's identity. The pull brings the copy in
    as a unit of its own; the next send takes unit 1 with its copy there (``IN`` takes both) and adds them back, the
    copy under its new identity; when the program later edits unit 1 nothing else goes, and no pull deletes anything."""
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    units = fake.linked(rl, "unita")
    fake.paste(units, one[ID_FIELD])
    _moved_last(units, GB, 3.0)
    assert rl.pull()["unita"] == (0, 1, 0)
    (copied,) = live.read_changes()["unita"].added
    assert copied.id not in (one[ID_FIELD], two[ID_FIELD]) and copied.values["us_s"] == "1"
    taken = _taken(copied, us_s="1", etichetta="US 1 (copia)")
    live.write(_drawings([one, two, taken]))
    assert rl.push() == 2  # unit 1 again (its copy there went with it) and the copy under its own identity
    assert rl.push() == 0 and not rl.pending
    assert sorted(fake.identities(units)) == sorted([one[ID_FIELD], two[ID_FIELD], copied.id])
    live.write(_drawings([dict(one, _geom=_square(GX + 0.1, GY)), two, taken]))
    assert rl.push() == 1
    assert sorted(fake.identities(units)) == sorted([one[ID_FIELD], two[ID_FIELD], copied.id])
    assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}
    _, stored = _live_table(live.path, "unita")
    assert sorted(v[ID_FIELD] for _, _, v in stored) == sorted([one[ID_FIELD], two[ID_FIELD], copied.id])


def test_a_copy_not_read_yet_survives_a_send_of_what_it_copies(tmp_path):
    """Finding 1: the program edits unit 1 while its copy pasted in RustGIS is pulled but not read by the window yet:
    the send takes unit 1 and the copy and puts the copy back as it was, under unit 1's identity; once read, the copy
    goes under its own, and nothing is lost on either side."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    fake.paste(units, one[ID_FIELD])
    _moved_last(units, GB, 3.0)
    copy_there = _reprojected(units["features"][-1]["geometry"], "EPSG:4326", GB)
    assert rl.pull()["unita"] == (0, 1, 0)
    edited = dict(one, _geom=_square(GX, GY, 0.6))
    live.write(_drawings([edited]))  # the copy's row is in the file, without identity yet
    assert rl.push() == 1
    assert fake.identities(units).count(one[ID_FIELD]) == 2  # unit 1 as edited, and the copy put back
    shapes = sorted(fake.native(units, one[ID_FIELD]), key=lambda g: g.bounds[0])
    assert link._same_geometry(shapes[0], Polygon(edited["_geom"][1])) and link._same_geometry(shapes[1], copy_there)
    assert rl.pull()["unita"] == (0, 0, 0)
    (copied,) = live.read_changes()["unita"].added
    live.write(_drawings([edited, _taken(copied, us_s="1")]))
    assert rl.push() == 2 and not rl.pending
    assert sorted(fake.identities(units)) == sorted([one[ID_FIELD], copied.id])
    assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}


def test_a_unit_deleted_while_its_copy_is_unread_waits_for_the_window(tmp_path, caplog):
    """The program deletes unit 1 while its copy pasted in RustGIS is pulled but not read yet: put back under unit 1's
    identity, the copy would come back at the next pull as unit 1 itself. The send waits (said in the log) until the
    window reads the copy; then unit 1 goes, the copy stays under its own identity, and nothing comes back."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    units = fake.linked(rl, "unita")
    fake.paste(units, one[ID_FIELD])
    _moved_last(units, GB, 3.0)
    assert rl.pull()["unita"] == (0, 1, 0)
    live.write(_drawings([two]))  # unit 1 deleted in the window; the copy's row is not read yet
    state = fake.state()
    assert rl.push() == 0 and rl.pending and fake.state() == state
    assert "the window has not read yet" in caplog.text and one[ID_FIELD] in caplog.text
    (copied,) = live.read_changes()["unita"].added
    live.write(_drawings([two, _taken(copied, us_s="1")]))
    assert rl.push() == 2 and not rl.pending  # unit 1 gone there, the copy under its own identity
    assert sorted(fake.identities(units)) == sorted([two[ID_FIELD], copied.id])
    assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}
    _, stored = _live_table(live.path, "unita")
    assert sorted(v[ID_FIELD] for _, _, v in stored) == sorted([two[ID_FIELD], copied.id])


def test_a_copy_pasted_since_the_last_pull_makes_the_send_wait(tmp_path, caplog):
    """Finding 1: a second copy of unit 1 pasted after the last pull: the send that would take unit 1 with its copies
    finds three where the link knows two, and waits for the next pull - saying so in the log - rather than delete a
    copy it does not know. After the pull it goes, both copies put back."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    fake.paste(units, one[ID_FIELD])
    _moved_last(units, GB, 3.0)
    assert rl.pull()["unita"] == (0, 1, 0)
    fake.paste(units, one[ID_FIELD])
    _moved_last(units, GB, 6.0)
    live.write(_drawings([dict(one, _geom=_square(GX, GY, 0.6))]))
    assert rl.push() == 0 and rl.pending and fake.identities(units).count(one[ID_FIELD]) == 3
    assert "RustGIS has 3 of these features, the link knows 2" in caplog.text
    assert "unita" in caplog.text and one[ID_FIELD] in caplog.text
    assert rl.pull()["unita"] == (0, 1, 0)
    assert rl.push() == 1 and not rl.pending
    assert fake.identities(units).count(one[ID_FIELD]) == 3  # unit 1 as edited, both copies put back


def test_a_unit_of_two_polygons_with_a_copy_counts_as_one_feature(tmp_path, caplog):
    """Finding 1 with ruling 3: unit 1 is copied and pasted in RustGIS, then given a second polygon there (one feature
    of two polygons). The program edits part 1 before the window reads either: the send that takes unit 1 with its copy
    counts unit 1 as the one feature RustGIS has, not one per polygon, and goes at once - both parts sent, the copy put
    back. Once read, the copy goes under its own identity."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    fake.paste(units, one[ID_FIELD])
    _moved_last(units, GB, 6.0)
    feature = units["features"][0]
    there = box(GX + 3, GY, GX + 3.5, GY + 0.5)
    feature["geometry"] = MultiPolygon([feature["geometry"], _reprojected(there, GB, "EPSG:4326")])
    assert rl.pull()["unita"] == (0, 2, 0)  # the second polygon, and the copy
    edited = dict(one, _geom=_square(GX, GY, 0.6))
    live.write(_drawings([edited]))  # part 2 and the copy are in the file, not read yet
    assert rl.push() == 2 and not rl.pending and "wait" not in caplog.text
    assert fake.identities(units).count(one[ID_FIELD]) == 3  # both parts, and the copy put back
    parts = sorted(fake.native(units, one[ID_FIELD]), key=lambda g: g.bounds[0])
    assert link._same_geometry(parts[0], Polygon(edited["_geom"][1])) and link._same_geometry(parts[1], there)
    assert rl.pull()["unita"] == (0, 0, 0)
    got = live.read_changes()["unita"]
    (copied,) = got.added
    second = dict(one, _shape=next(f for f in got.changed if f.part == 2).geometry)
    live.write(_drawings([edited, second, _taken(copied, us_s="1")]))
    assert rl.push() == 3 and not rl.pending and "wait" not in caplog.text
    assert sorted(fake.identities(units)) == sorted([one[ID_FIELD], one[ID_FIELD], copied.id])
    assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}


def test_an_edit_made_there_before_a_send_is_still_seen(tmp_path):
    """Finding 2: unit 2 moved in RustGIS, then the program sends unit 1 before the next look: the send reads the
    status first and does not take as its own a status that already held RustGIS's edit - status_changed() stays
    true, and the pull that follows brings unit 2 in."""
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    fake.reshape(fake.linked(rl, "unita"), two[ID_FIELD], box(GX + 1.3, GY, GX + 1.8, GY + 0.5), GB)
    live.write(_drawings([dict(one, _geom=_square(GX + 0.1, GY)), two]))
    assert rl.push() == 1
    assert rl.status_changed()
    assert rl.pull()["unita"] == (1, 0, 0)
    assert not rl.status_changed()
    live.write(_drawings([dict(one, _geom=_square(GX + 0.2, GY)), two]))
    assert rl.push() == 1 and not rl.status_changed()  # nothing edited there meanwhile: the send's status is taken


def test_a_layer_of_another_project_is_never_read(tmp_path):
    """Finding 3: another project open in RustGIS has a layer of its own under the id of the units' layer, with a US 7
    of its own and a ppg_id field: no pull and no remember reads it - nothing comes in, nothing is deleted here."""
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    units = fake.linked(rl, "unita")
    units["source"] = str(tmp_path / "altro progetto" / "scavo.gpkg")
    attrs = dict.fromkeys(units["fields"]) | {ID_FIELD: new_id(), "us_s": "7"}
    units["features"] = [{"geometry": _reprojected(box(GX + 5, GY, GX + 6, GY + 1), GB, "EPSG:4326"), "attrs": attrs}]
    before, baseline = _live_table(live.path, "unita"), set(rl.baseline["unita"])
    with pytest.raises(link.RustGisRefused, match="unita"):
        rl.pull()
    with pytest.raises(link.RustGisRefused, match="unita"):
        rl.remember(["unita"])
    assert _live_table(live.path, "unita") == before and not live.external_change() and live.read_changes() == {}
    assert set(rl.baseline["unita"]) == baseline == {(one[ID_FIELD], 1), (two[ID_FIELD], 1)}


def test_an_adopted_layer_is_compared_with_what_it_loaded(tmp_path):
    """Finding 4a: the contour lines' layer removed in RustGIS; added again, the answer lost; meanwhile the program
    computes the contour lines again. The layer found at the next send is compared with what RustGIS loaded - its own
    export - not with the file as it is then: the new lines go, and RustGIS ends with the program's rows."""
    curves = [{"quota": 99.0 + n / 10, "_geom": ("LineString", [[GX, GY + n], [GX + 5, GY + n]])} for n in range(3)]
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one], curves=curves))
    title = LIVE_TABLES["curve"][0]
    fake.remove(int(rl.layers["curve"]))
    fake.lost.append("run_command:layer.addData")
    with pytest.raises(link.RustGisBusy):
        rl.push()
    again = [{"quota": 99.0 + n / 10, "_geom": ("LineString", [[GX, GY + n + 0.5], [GX + 5, GY + n + 0.5]])}
             for n in range(4)]
    live.write(_drawings([one], curves=again))
    assert rl.push() == 7  # three lines gone, four added
    assert rl.push() == 0 and not rl.pending
    assert [l["name"] for l in fake.layers].count(title) == 1
    contours = fake.linked(rl, "curve")
    assert sorted(round(f["attrs"]["quota"], 1) for f in contours["features"]) == [99.0, 99.1, 99.2, 99.3]
    assert all(g.bounds[1] > GY + 0.49 for f in contours["features"]
               for g in [_reprojected(f["geometry"], "EPSG:4326", GB)])


def test_a_layer_whose_content_cannot_be_read_is_added_once_more(tmp_path):
    """Finding 4b: the units' layer added again, but RustGIS does not answer the export that would say what it loaded:
    that layer is removed at the next send and added again, remembered at once. While RustGIS refuses to remove it the
    table waits and nothing goes to that layer."""
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    title = LIVE_TABLES["unita"][0]
    fake.remove(int(rl.layers["unita"]))
    fake.busy.append("run_command:share.exportLayer")
    with pytest.raises(link.RustGisBusy):
        rl.push()
    (first,) = [l for l in fake.layers if l["name"] == title]
    fake.rules.append(lambda c, p: "not now" if c == "layer.remove" and p.get("layer") == first["id"] else None)
    moved = dict(one, _geom=_square(GX + 0.1, GY))
    live.write(_drawings([moved, two]))
    state = fake.state()
    assert rl.push() == 0 and rl.pending and fake.state() == state  # the table waits: nothing sent to that layer
    fake.rules.clear()
    assert rl.push() == 0 and not rl.pending  # removed, added again from the file - its rows whole - remembered
    (again,) = [l for l in fake.layers if l["name"] == title]
    assert again["id"] != first["id"] and int(rl.layers["unita"]) == again["id"]
    (there,) = fake.native(again, one[ID_FIELD])
    assert link._same_geometry(there, Polygon(moved["_geom"][1]))
    live.write(_drawings([one, two]))
    assert rl.push() == 1  # linked again: what the program changes goes there


def test_a_script_cut_short_is_not_taken_as_sent(tmp_path):
    """Finding 5: RustGIS runs only part of a script and says so (past its 10,000th line it drops the others without an
    error): nothing of it is taken as sent - the rows stay pending - and the next send puts things right, the
    placeholder left there taken away; the pull never reads that placeholder."""
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    units = fake.linked(rl, "unita")
    live.write(_drawings([dict(one, _geom=_square(GX + 0.2, GY)), dict(two, _geom=_square(GX + 1.2, GY))]))
    fake.cut = 1  # the placeholder is added, nothing else runs
    with pytest.raises(link.RustGisRefused, match="ran 1 of the"):
        rl.push()
    assert rl.pending and any(str(pid).startswith("ppg-invio-") for pid in fake.identities(units))
    assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}  # the leftover is nobody's
    fake.cut = None
    assert rl.push() == 2 and not rl.pending
    assert sorted(fake.identities(units)) == sorted([one[ID_FIELD], two[ID_FIELD]])


def test_rows_no_script_holds_are_set_aside(tmp_path, monkeypatch, caplog):
    """Minor 6: units too large for any script are set aside - said in the log, listed in set_aside, the rows pending -
    and the other changes go; the size counts the bytes the request carries (a letter beyond ASCII is six). Changed by
    the program to something smaller, they go."""
    import numpy as np

    caplog.set_level(logging.WARNING, logger=link.__name__)
    one, two, three = _unit(1, GX, GY), _unit(2, GX + 1, GY), _unit(3, GX + 2, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two, three]))
    monkeypatch.setattr(link, "SCRIPT_BYTES", 6000)
    angles = np.linspace(0, 2 * np.pi, 400, endpoint=False)
    ring = [[GX + 0.25 + 0.25 * np.cos(a), GY + 0.25 + 0.25 * np.sin(a)] for a in angles]
    accents = "è" * 1000  # 1000 characters, 6000 bytes in the request
    live.write(_drawings([dict(one, _geom=("Polygon", ring)), dict(two, _geom=_square(GX + 1.2, GY)),
                          dict(three, tipo_us_s=accents)]))
    assert rl.push() == 1  # unit 2
    assert rl.set_aside == {"unita": sorted([one[ID_FIELD], three[ID_FIELD]])} and rl.pending
    assert caplog.text.count("no script holds it") == 2
    units = fake.linked(rl, "unita")
    (there,) = fake.native(units, two[ID_FIELD])
    assert link._same_geometry(there, Polygon(_square(GX + 1.2, GY)[1]))
    assert rl.push() == 0 and rl.pending  # unchanged: still aside
    live.write(_drawings([dict(one, _geom=_square(GX, GY, 0.7)), dict(two, _geom=_square(GX + 1.2, GY)),
                          dict(three, tipo_us_s=TEXT)]))
    assert rl.push() == 2 and rl.set_aside == {} and not rl.pending


def test_features_drawn_there_alike_but_for_letter_case_go_together(tmp_path, caplog):
    """Minor 7: two units drawn in RustGIS whose values differ only in the case of their letters, which RustGIS's where
    clause does not tell apart: they go together - the one the window read is replaced, the other put back as it was -
    and nothing waits."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    fake.draw(units, box(GX + 3, GY, GX + 3.5, GY + 0.5), GB, us_s="US 7")
    assert rl.pull()["unita"] == (0, 1, 0)
    (a,) = live.read_changes()["unita"].added
    live.write(_drawings([one, _taken(a, us_s="US 7")]))
    second = box(GX + 5, GY, GX + 5.5, GY + 0.5)
    fake.draw(units, second, GB, us_s="us 7")
    assert rl.pull()["unita"] == (0, 1, 0)
    assert rl.push() == 1 and not rl.pending and "wait" not in caplog.text
    assert sorted(map(str, fake.identities(units))) == sorted([one[ID_FIELD], a.id, "None"])
    (kept,) = [f for f in units["features"] if f["attrs"][ID_FIELD] is None]
    assert kept["attrs"]["us_s"] == "us 7"
    assert link._same_geometry(_reprojected(kept["geometry"], "EPSG:4326", GB), second)


def test_a_row_rustgis_refuses_is_set_aside_and_the_others_go(tmp_path, caplog):
    """Minor 9: RustGIS refuses one row (a domain of its layer): that unit is set aside - said in the log, not sent
    again until the program changes it - and the send goes again at once with the others."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    fake.rules.append(lambda c, p: "666 isn't allowed in us_s (its domain)"
                      if c == "edit.addFeature" and (p.get("attributes") or {}).get("us_s") == "666" else None)
    live.write(_drawings([dict(one, us_s="666"), dict(two, _geom=_square(GX + 1.2, GY))]))
    assert rl.push() == 1  # unit 2
    assert rl.set_aside == {"unita": [one[ID_FIELD]]} and rl.pending
    assert "666 isn't allowed" in caplog.text and one[ID_FIELD] in caplog.text
    units = fake.linked(rl, "unita")
    assert [f["attrs"]["us_s"] for f in units["features"] if f["attrs"][ID_FIELD] == one[ID_FIELD]] == ["1"]
    assert rl.push() == 0 and rl.pending  # unchanged: not sent again
    live.write(_drawings([dict(one, us_s="6"), dict(two, _geom=_square(GX + 1.2, GY))]))
    assert rl.push() == 1 and rl.set_aside == {} and not rl.pending
    assert [f["attrs"]["us_s"] for f in units["features"] if f["attrs"][ID_FIELD] == one[ID_FIELD]] == ["6"]


def test_the_system_of_an_empty_chunk_is_checked_where_it_is_used(tmp_path):
    """Minor 12: a chunk without any drawing yet: the point that checks the system is put in the middle of the
    system's area of use, and a system RustGIS places elsewhere is refused there too."""
    from pyproj import CRS, Transformer

    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings())
    fake = FakeEngine()
    assert link.RustGisLink(live, fake, "Saggio 1", GB).start([])["layers"] == 7
    source = Path(_commands(fake.scripts[0])[0][1]["path"])
    (point,) = [g for g, _ in gpkg.read_table(source, "sistema")]
    lon, lat = Transformer.from_crs(GB, "EPSG:4326", always_xy=True).transform(point.x, point.y)
    area = CRS.from_user_input(GB).area_of_use
    assert area.west < lon < area.east and area.south < lat < area.north
    with pytest.raises(link.RustGisRefused, match="EPSG:3004"):
        link.RustGisLink(live, FakeEngine(shift=0.002), "Saggio 1", GB).start([])


def test_a_send_names_its_lines(tmp_path):
    """What a refused line is about: the rows of one identity (set aside), a group of features RustGIS has beyond the
    rows it was given (held), or nothing (the selection, the delete)."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    live.write(_drawings([dict(one, _geom=_square(GX + 0.1, GY))]))
    send = rl._send(rl._linked()[0])
    commands = [line.split(" ", 1)[0] for line in send.lines]
    owned = {c: o for c, o in zip(commands, send.owners) if o}
    assert commands == ["edit.addFeature", "select.byAttributes", "edit.delete", "edit.addFeature", "select.clear",
                        "edit.status"]
    assert [o is None for o in send.owners] == [False, True, True, False, True, True]
    assert owned["edit.addFeature"][0] == "unita" and list(owned["edit.addFeature"][1]) == [one[ID_FIELD]]
    assert link._line_number(link.RustGisRefused(json.dumps({"error": "line 4: edit.addFeature: no"}))) == 4


def test_a_copy_rustgis_refuses_holds_its_group_and_sets_nothing_else_aside(tmp_path, caplog):
    """NB1: unit 1 pasted in RustGIS, the copy taken by the window as unit Y with a value RustGIS's layer refuses (a
    domain): Y is set aside, and unit 1 - which goes only with its copy - waits, said once, never set aside; an edit of
    unit 1 while Y is aside waits too. Once the program fixes Y the group goes: unit 1 and Y both."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    fake.paste(units, one[ID_FIELD])
    _moved_last(units, GB, 3.0)
    assert rl.pull()["unita"] == (0, 1, 0)
    (copied,) = live.read_changes()["unita"].added
    fake.rules.append(lambda c, p: "666 isn't allowed in us_s (its domain)"
                      if c == "edit.addFeature" and (p.get("attributes") or {}).get("us_s") == "666" else None)
    live.write(_drawings([one, _taken(copied, us_s="666")]))
    assert rl.push() == 0 and rl.pending and rl.set_aside == {"unita": [copied.id]}
    waiting = f"waiting, {one[ID_FIELD]} and its copies there: they go with {copied.id}, set aside"
    assert caplog.text.count(waiting) == 1
    assert rl.push() == 0 and rl.set_aside == {"unita": [copied.id]} and caplog.text.count(waiting) == 1
    edited = dict(one, _geom=_square(GX, GY, 0.6))
    live.write(_drawings([edited, _taken(copied, us_s="666")]))
    assert rl.push() == 0 and rl.pending and rl.set_aside == {"unita": [copied.id]}
    assert fake.identities(units).count(one[ID_FIELD]) == 2  # unit 1 and its copy, as they were there
    live.write(_drawings([edited, _taken(copied, us_s="7")]))
    assert rl.push() == 2 and rl.set_aside == {} and not rl.pending
    assert sorted(fake.identities(units)) == sorted([one[ID_FIELD], copied.id])
    (there,) = fake.native(units, one[ID_FIELD])
    assert link._same_geometry(there, Polygon(edited["_geom"][1]))


def test_groups_go_one_by_one_and_only_one_no_script_holds_is_set_aside(tmp_path, monkeypatch, caplog):
    """NB2: units 1, 2 and 3 pasted in RustGIS (unit 3 three times), the copies not read yet, and the program edits the
    three: each unit goes whole, with its copies put back, in a script it fits - unit 1 now, unit 2 with the next send
    (said in the log) - and unit 3, which no script holds with its copies, is set aside alone (said in the log) while
    the others go. Nothing is lost there."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    monkeypatch.setattr(link, "SCRIPT_LINES", 8)  # a head of 3 lines and 3 more, beside the closing two
    one, two, three = (_unit(n, GX + 3 * n, GY) for n in (1, 2, 3))
    live, fake, rl = _started(tmp_path, _drawings([one, two, three]))
    units = fake.linked(rl, "unita")
    for unit, copies in ((one, 1), (two, 1), (three, 3)):
        for k in range(1, copies + 1):
            fake.paste(units, unit[ID_FIELD])
            _moved_last(units, GB, 0.7 * k)
    assert rl.pull()["unita"] == (0, 5, 0)
    edited = [dict(u, _geom=_square(u["_geom"][1][0][0], GY, 0.6)) for u in (one, two, three)]
    live.write(_drawings(edited))  # the copies are in the file, not read yet
    assert rl.push() == 1 and rl.pending  # unit 1 with its copy: 2 lines; unit 2: 2 more; unit 3: 4, no script
    assert rl.set_aside == {"unita": [three[ID_FIELD]]}
    assert caplog.text.count("this script is full") == 1 and caplog.text.count("no script holds them") == 1
    assert rl.push() == 1 and rl.pending and rl.set_aside == {"unita": [three[ID_FIELD]]}
    assert rl.push() == 0
    ids = fake.identities(units)
    assert (ids.count(one[ID_FIELD]), ids.count(two[ID_FIELD]), ids.count(three[ID_FIELD])) == (2, 2, 4)
    for unit, new in zip((one, two), edited):
        shapes = sorted(fake.native(units, unit[ID_FIELD]), key=lambda g: g.bounds[0])
        assert link._same_geometry(shapes[0], Polygon(new["_geom"][1]))


def test_features_drawn_there_go_in_the_scripts_they_fit(tmp_path, monkeypatch, caplog):
    """NB2: four units drawn in RustGIS, each taken by the window under an identity of its own: each goes with the line
    that finds it there by its values, as many as one script holds - two now, two with the next send (said in the log)
    - never all set aside for a head too large."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    monkeypatch.setattr(link, "SCRIPT_LINES", 9)  # a head of 3 lines and 4 more, beside the closing two
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    for n in range(4):
        fake.draw(units, box(GX + 2 + n, GY, GX + 2.5 + n, GY + 0.5), GB, us_s=str(10 + n))
    assert rl.pull()["unita"] == (0, 4, 0)
    drawn = live.read_changes()["unita"].added
    live.write(_drawings([one, *(_taken(f, us_s=f.values["us_s"]) for f in drawn)]))
    assert rl.push() == 2 and rl.pending and rl.set_aside == {}
    assert "this script is full" in caplog.text
    assert rl.push() == 2 and not rl.pending
    assert sorted(fake.identities(units)) == sorted([one[ID_FIELD], *(f.id for f in drawn)])


def test_an_edit_made_there_while_a_send_is_made_is_still_seen(tmp_path):
    """NB3: unit 2 moved in RustGIS after a send began (while the link looks at the layers) and before its script ran:
    the status is read right before the script, so the send does not take that edit as its own - status_changed()
    stays true, and the pull brings unit 2 in."""
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    units = fake.linked(rl, "unita")

    def meanwhile(tool, args):
        if tool == "list_layers":
            fake.hooks.remove(meanwhile)
            fake.reshape(units, two[ID_FIELD], box(GX + 1.3, GY, GX + 1.8, GY + 0.5), GB)

    fake.hooks.append(meanwhile)
    live.write(_drawings([dict(one, _geom=_square(GX + 0.1, GY)), two]))
    assert rl.push() == 1
    assert rl.status_changed()
    assert rl.pull()["unita"] == (1, 0, 0)
    assert not rl.status_changed()


def test_a_send_that_only_deletes_copies_no_values_into_its_placeholder(tmp_path, caplog):
    """NB4: the program deletes unit 1 and changes nothing else: the placeholder of that send takes unit 1's geometry
    and the placeholder's identity, never unit 1's values (the program no longer has them). When RustGIS refuses that
    placeholder (a rule of its layer) the unit deleted is set aside, and since the program can never change it, it goes
    again when its layer is added again (NB5), or when the link starts again."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    live.write(_drawings([two]))
    assert rl.push() == 1
    ((command, opening), *_) = _sent(fake)[0]
    assert command == "edit.addFeature" and list(opening["attributes"]) == [ID_FIELD]
    assert opening["attributes"][ID_FIELD].startswith(f"{link.PLACEHOLDER}-")
    placed = _reprojected(shapely.geometry.shape(opening["geometry"]), "EPSG:4326", GB)
    assert link._same_geometry(placed, Polygon(one["_geom"][1]))
    assert fake.identities(fake.linked(rl, "unita")) == [two[ID_FIELD]]
    fake.rules.append(lambda c, p: "us_s is required"
                      if c == "edit.addFeature" and not (p.get("attributes") or {}).get("us_s") else None)
    three, four = _unit(3, GX + 2, GY), _unit(4, GX + 3, GY)
    live.write(_drawings([two, three]))
    assert rl.push() == 1
    live.write(_drawings([two]))
    assert rl.push() == 0 and rl.set_aside == {"unita": [three[ID_FIELD]]} and rl.pending
    assert "us_s is required" in caplog.text
    assert rl.push() == 0 and rl.set_aside == {"unita": [three[ID_FIELD]]}  # nothing the program can change
    fake.remove(int(rl.layers["unita"]))  # the layer gone: added again, whole, from the file
    assert rl.push() == 0 and rl.set_aside == {} and not rl.pending
    assert fake.identities(fake.linked(rl, "unita")) == [two[ID_FIELD]]
    live.write(_drawings([two, four]))
    assert rl.push() == 1
    live.write(_drawings([two]))
    assert rl.push() == 0 and rl.set_aside == {"unita": [four[ID_FIELD]]}
    rl.start([])
    assert rl.set_aside == {} and not rl.pending


def test_what_was_set_aside_goes_again_when_its_layer_is_adopted(tmp_path, caplog):
    """NB5: unit 1 set aside (RustGIS's layer refuses a value of it); the user removes the layer there, and the answer
    to adding it again is lost: the layer found at the next send - loaded whole from the file - is adopted, and what was
    set aside for the layer that is gone goes again: nothing is left aside, nothing pending."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    fake.rules.append(lambda c, p: "666 isn't allowed in us_s (its domain)"
                      if c == "edit.addFeature" and (p.get("attributes") or {}).get("us_s") == "666" else None)
    live.write(_drawings([dict(one, us_s="666"), two]))
    assert rl.push() == 0 and rl.set_aside == {"unita": [one[ID_FIELD]]}
    fake.remove(int(rl.layers["unita"]))
    fake.lost.append("run_command:layer.addData")
    with pytest.raises(link.RustGisBusy):
        rl.push()
    assert rl.set_aside == {"unita": [one[ID_FIELD]]} and rl.pending
    assert rl.push() == 0 and rl.set_aside == {} and not rl.pending
    assert sorted(fake.identities(fake.linked(rl, "unita"))) == sorted([one[ID_FIELD], two[ID_FIELD]])


def test_a_group_rustgis_refuses_waits_for_the_next_pull_and_the_rest_goes(tmp_path, caplog):
    """NB6: two units drawn in RustGIS; the window deletes one and takes the other, and the program edits unit 1.
    RustGIS refuses the line that finds the deleted one by its values: that group waits until the next pull, said in
    the log, and the send goes again at once without it - never the same script again. After a pull, with RustGIS
    taking the line, the deleted one goes too."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    fake.draw(units, box(GX + 3, GY, GX + 3.5, GY + 0.5), GB, us_s="999")
    fake.draw(units, box(GX + 5, GY, GX + 5.5, GY + 0.5), GB, us_s="12")
    assert rl.pull()["unita"] == (0, 2, 0)
    drawn = {f.values["us_s"]: f for f in live.read_changes()["unita"].added}

    def unreadable(command, params):
        if command == "select.byAttributes" and "'999'" in params.get("where", ""):
            return "the clause cannot be read"
        return None

    fake.rules.append(unreadable)
    live.write(_drawings([dict(one, _geom=_square(GX, GY, 0.6)), _taken(drawn["12"], us_s="12")]))
    assert rl.push() == 2 and rl.pending  # unit 1 and the one taken; the one deleted waits
    assert len(_sent(fake)) == 2 and "RustGIS refused them" in caplog.text
    assert sorted(map(str, fake.identities(units))) == sorted([one[ID_FIELD], drawn["12"].id, "None"])
    assert rl.push() == 0 and len(_sent(fake)) == 2  # until the next pull: nothing goes again
    fake.rules.remove(unreadable)
    assert rl.pull()["unita"] == (0, 0, 0)
    assert rl.push() == 1 and not rl.pending  # the one deleted, now
    assert sorted(fake.identities(units)) == sorted([one[ID_FIELD], drawn["12"].id])


def test_a_wait_that_ends_and_comes_back_is_said_again(tmp_path, monkeypatch, caplog):
    """NB7: units 1 and 2 pasted in RustGIS, the copies not read yet, and the program edits both while one script holds
    only one of them: unit 2 waits for the next send, said in the log; it goes, and the wait ends. When the same
    happens again, it is said again."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    monkeypatch.setattr(link, "SCRIPT_LINES", 7)  # a head of 3 lines and 2 more, beside the closing two
    one, two = _unit(1, GX, GY), _unit(2, GX + 3, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    units = fake.linked(rl, "unita")
    for unit in (one, two):
        fake.paste(units, unit[ID_FIELD])
        _moved_last(units, GB, 1.0)
    assert rl.pull()["unita"] == (0, 2, 0)
    full = f"waiting, {two[ID_FIELD]} and its copies there: this script is full"
    for side in (0.6, 0.7):
        live.write(_drawings([dict(one, _geom=_square(GX, GY, side)), dict(two, _geom=_square(GX + 3, GY, side))]))
        assert rl.push() == 1 and rl.pending
        assert rl.push() == 1 and not rl.pending
    assert caplog.text.count(full) == 2


def test_a_send_rustgis_runs_after_its_answer_was_lost_is_no_edit_there(tmp_path):
    """OOS1: a send whose answer was lost (unit 1 moved, unit 2 relabelled) runs in RustGIS later; meanwhile the program
    moves unit 1 again and deletes unit 2. The pull takes what RustGIS has from that send for the program's, not for
    edits made there: the newer move is not undone, unit 2 does not come back, and the next send gives RustGIS both."""
    one, two = _unit(1, GX, GY), _unit(2, GX + 1, GY)
    live, fake, rl = _started(tmp_path, _drawings([one, two]))
    units = fake.linked(rl, "unita")
    live.write(_drawings([dict(one, _geom=_square(GX + 0.1, GY)), dict(two, us_s="2b")]))
    fake.later.append("run")
    with pytest.raises(link.RustGisBusy):
        rl.push()
    assert rl.pending and sorted(rl.in_flight["unita"]) == sorted([(one[ID_FIELD], 1), (two[ID_FIELD], 1)])
    fake.run_later()  # RustGIS draws again and runs the send
    newer = dict(one, _geom=_square(GX + 0.2, GY), tipo_us_s="newer")
    live.write(_drawings([newer]))
    assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {} and not rl.in_flight
    _, stored = _live_table(live.path, "unita")
    assert [values[ID_FIELD] for _, _, values in stored] == [one[ID_FIELD]]
    assert link._same_geometry(stored[0][1], Polygon(newer["_geom"][1])) and stored[0][2]["tipo_us_s"] == "newer"
    assert rl.push() == 2 and not rl.pending  # unit 1 as it is now, unit 2 deleted there
    (there,) = fake.native(units, one[ID_FIELD])
    assert link._same_geometry(there, Polygon(newer["_geom"][1])) and fake.identities(units) == [one[ID_FIELD]]


def test_an_edit_made_there_after_a_lost_send_ran_comes_column_by_column(tmp_path):
    """OOS1: a send whose answer was lost runs in RustGIS later, the user then edits the label of that unit there, and
    the program edits the unit again: the pull brings the label - the one column edited there - and keeps the program's
    newer geometry and values."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    live.write(_drawings([dict(one, _geom=_square(GX + 0.1, GY), tipo_us_s="first")]))
    fake.later.append("run")
    with pytest.raises(link.RustGisBusy):
        rl.push()
    fake.run_later()
    (feature,) = [f for f in units["features"] if f["attrs"][ID_FIELD] == one[ID_FIELD]]
    feature["attrs"]["etichetta"] = "US 1 bis"
    newer = dict(one, _geom=_square(GX + 0.2, GY), tipo_us_s="newer")
    live.write(_drawings([newer]))
    assert rl.pull()["unita"] == (1, 0, 0)
    (changed,) = live.read_changes()["unita"].changed
    assert changed.values["etichetta"] == "US 1 bis" and changed.values["tipo_us_s"] == "newer"
    assert link._same_geometry(changed.geometry, Polygon(newer["_geom"][1]))


def test_a_group_no_script_holds_goes_again_when_the_program_changes_one_of_it(tmp_path, monkeypatch, caplog):
    """NB2: unit 1 pasted in RustGIS; the window takes the copy (Y) and two more copies pasted later are not read yet;
    the program edits unit 1: unit 1, Y and the two copies to put back are one group no script holds, set aside
    together (said in the log). When the program changes one of them - the window deletes Y - all of it goes again."""
    caplog.set_level(logging.WARNING, logger=link.__name__)
    monkeypatch.setattr(link, "SCRIPT_LINES", 8)  # a head of 3 lines and 3 more, beside the closing two
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    fake.paste(units, one[ID_FIELD])
    _moved_last(units, GB, 0.7)
    assert rl.pull()["unita"] == (0, 1, 0)
    (copied,) = live.read_changes()["unita"].added
    for k in (2, 3):
        fake.paste(units, one[ID_FIELD])
        _moved_last(units, GB, 0.7 * k)
    assert rl.pull()["unita"] == (0, 2, 0)
    edited = dict(one, _geom=_square(GX, GY, 0.6))
    live.write(_drawings([edited, _taken(copied, us_s="1")]))  # 4 lines: unit 1, Y, two copies put back
    assert rl.push() == 0 and rl.set_aside == {"unita": sorted([one[ID_FIELD], copied.id])}
    assert "no script holds them" in caplog.text and "until the program changes one of them" in caplog.text
    live.write(_drawings([edited]))  # 3 lines: unit 1 and the two copies put back
    assert rl.push() == 2 and rl.set_aside == {} and not rl.pending  # unit 1, and Y deleted there
    assert fake.identities(units) == [one[ID_FIELD]] * 3
    shapes = sorted(fake.native(units, one[ID_FIELD]), key=lambda g: g.bounds[0])
    assert link._same_geometry(shapes[0], Polygon(edited["_geom"][1]))


def test_a_send_that_went_forgets_what_a_lost_one_carried(tmp_path):
    """OOS1: what a lost send carried counts until a send of that identity goes: unit 1 moved (the answer lost, the send
    run later), then moved again and sent; the user then undoes that last send in RustGIS, unit 1 back where the lost
    send put it - an edit made there, which the pull brings."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    units = fake.linked(rl, "unita")
    first = dict(one, _geom=_square(GX + 0.1, GY))
    live.write(_drawings([first]))
    fake.later.append("run")
    with pytest.raises(link.RustGisBusy):
        rl.push()
    fake.run_later()
    live.write(_drawings([dict(one, _geom=_square(GX + 0.2, GY))]))
    assert rl.push() == 1 and not rl.in_flight.get("unita")
    fake.reshape(units, one[ID_FIELD], Polygon(first["_geom"][1]), GB)  # the user undoes the last send there
    assert rl.pull()["unita"] == (1, 0, 0)
    (changed,) = live.read_changes()["unita"].changed
    assert link._same_geometry(changed.geometry, Polygon(first["_geom"][1]))


def test_tables_are_loaded_whole_so_large_ones_take_edits(tmp_path):
    """OOS2: RustGIS reads a GeoPackage table of more features than it streams above (100,000) in place and refuses
    every edit to it: the link adds the tables with "stream": false - at the start and when a layer is added again -
    and the program's edits to a large table go."""
    one = _unit(1, GX, GY)
    curves = [{"quota": 99.0 + n / 10, "_geom": ("LineString", [[GX, GY + n], [GX + 5, GY + n]])} for n in range(5)]
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([one], curves=curves))
    fake = FakeEngine()
    fake.stream_above = 3
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    rl.start([])
    contours = fake.linked(rl, "curve")
    assert not contours["streamed"] and len(contours["features"]) == 5
    in_place = fake.command("layer.addData", {"path": str(live.path), "layer": "curve", "name": "in place"})
    assert in_place["result"]["streamed"]  # what RustGIS does without "stream": false
    fake.remove(in_place["result"]["layer"])
    live.write(_drawings([one], curves=[dict(c, quota=c["quota"] + 1) for c in curves]))
    assert rl.push() == 10  # five gone, five added
    fake.remove(int(rl.layers["curve"]))
    live.write(_drawings([one], curves=[dict(c, quota=c["quota"] + 2) for c in curves]))
    assert rl.push() == 0  # added again, whole: it has the lines as they are now
    contours = fake.linked(rl, "curve")
    assert not contours["streamed"] and len(contours["features"]) == 5
    live.write(_drawings([one], curves=curves))
    assert rl.push() == 10 and not rl.pending


def _engine_ids(engine, layer) -> list:
    rows = engine.call("read_features", {"layer": int(layer), "where": "", "limit": 200, "fields": [ID_FIELD]})["rows"]
    return sorted(str(row.get(ID_FIELD)) for row in rows)


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_copies_with_the_real_engine(tmp_path):
    """Finding 1 with RustGIS's own engine (edit.copy, edit.paste): the review's sequence - paste, pull, two sends, an
    edit of unit 1 in the program, a send, a pull - deletes nothing, and the copy keeps its own identity there."""
    crs, x0, y0 = "EPSG:32633", 291000.0, 4640000.0
    one, two = _unit(1, x0, y0), _unit(2, x0 + 1, y0)
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([one, two]))
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        rl = link.RustGisLink(live, engine, "Saggio 1", crs)
        rl.start([])
        units = int(rl.layers["unita"])

        def command(name, **params):
            return engine.call("run_command", {"command": name, "params": params})

        command("select.byAttributes", layer=units, where=f"\"ppg_id\" = '{one[ID_FIELD]}'", mode="new")
        command("edit.copy")
        command("edit.paste", layer=units)
        assert rl.pull()["unita"] == (0, 1, 0)
        (copied,) = live.read_changes()["unita"].added
        taken = _taken(copied, us_s="1")
        live.write(_drawings([one, two, taken]))
        assert rl.push() == 2 and rl.push() == 0
        live.write(_drawings([dict(one, _geom=_square(x0 + 0.1, y0)), two, taken]))
        assert rl.push() == 1
        assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}
        assert _engine_ids(engine, units) == sorted([one[ID_FIELD], two[ID_FIELD], copied.id])
        _, stored = _live_table(live.path, "unita")
        assert sorted(v[ID_FIELD] for _, _, v in stored) == sorted([one[ID_FIELD], two[ID_FIELD], copied.id])


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_explode_with_the_real_engine(tmp_path):
    """edit.explode on unit 1 made of two polygons there: the second polygon becomes a feature of its own with unit 1's
    values - a copy, as Task 2 reads it: a unit of its own here. The program's next edit of unit 1 loses nothing: both
    stay, there and here."""
    crs, x0, y0 = "EPSG:32633", 291000.0, 4640000.0
    one = _unit(1, x0, y0)
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([one]))
    second = box(x0 + 3, y0, x0 + 3.5, y0 + 0.5)
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        rl = link.RustGisLink(live, engine, "Saggio 1", crs)
        rl.start([])
        units = int(rl.layers["unita"])

        def command(name, **params):
            return engine.call("run_command", {"command": name, "params": params})

        two = MultiPolygon([Polygon(one["_geom"][1]), second])
        command("edit.setGeometry", layer=units, row=0, geometry=_lonlat(two, crs))
        command("select.byAttributes", layer=units, where=f"\"ppg_id\" = '{one[ID_FIELD]}'", mode="new")
        assert command("edit.explode")["features"] == 2
        assert _engine_ids(engine, units) == [one[ID_FIELD]] * 2
        pulled = rl.pull()["unita"]
        assert pulled[1:] == (1, 0)  # the piece is added (unit 1 counts as changed when its ring starts elsewhere)
        got = live.read_changes()["unita"]
        (piece,) = got.added
        assert piece.id != one[ID_FIELD] and link._same_geometry(piece.geometry, second)
        mine = [dict(one, _shape=f.geometry) for f in got.changed] or [one]
        live.write(_drawings([*mine, _taken(piece, us_s="1")]))
        assert rl.push() == 2
        live.write(_drawings([dict(one, _geom=_square(x0, y0, 0.6)), _taken(piece, us_s="1")]))
        assert rl.push() == 1
        assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}
        assert _engine_ids(engine, units) == sorted([one[ID_FIELD], piece.id])
        _, stored = _live_table(live.path, "unita")
        assert sorted(v[ID_FIELD] for _, _, v in stored) == sorted([one[ID_FIELD], piece.id])


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_split_with_the_real_engine(tmp_path):
    """edit.split on a section: the second piece becomes a feature of its own with the section's values - a section of
    its own here, as Task 2 reads it. The program's next edit of the section loses nothing."""
    crs, x0, y0 = "EPSG:32633", 291000.0, 4640000.0
    section = {"etichetta": "Sezione 1", ID_FIELD: new_id(),
               "_geom": ("LineString", [[x0, y0 + 2], [x0 + 4, y0 + 2]])}
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([_unit(1, x0, y0)], sections=[section]))
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        rl = link.RustGisLink(live, engine, "Saggio 1", crs)
        rl.start([])
        lines = int(rl.layers["sezioni"])

        def command(name, **params):
            return engine.call("run_command", {"command": name, "params": params})

        command("select.byAttributes", layer=lines, where=f"\"ppg_id\" = '{section[ID_FIELD]}'", mode="new")
        lon, lat = _lonlat(Point(x0 + 1.5, y0 + 2), crs)["coordinates"]
        command("edit.split", lon=lon, lat=lat)
        assert _engine_ids(engine, lines) == [section[ID_FIELD]] * 2
        assert rl.pull()["sezioni"] == (1, 1, 0)
        got = live.read_changes()["sezioni"]
        (shortened,), (piece,) = got.changed, got.added
        assert shortened.geometry.length == pytest.approx(1.5, abs=1e-3)
        assert piece.geometry.length == pytest.approx(2.5, abs=1e-3)
        mine = dict(section, _shape=shortened.geometry)
        unit = _live_table(live.path, "unita")[1][0][2]
        drawings = _drawings([dict(_unit(1, x0, y0), **{ID_FIELD: unit[ID_FIELD]})],
                             sections=[mine, _taken(piece, etichetta="Sezione 1 (seconda parte)")])
        live.write(drawings)
        assert rl.push() == 2
        live.write(_drawings([dict(_unit(1, x0, y0), **{ID_FIELD: unit[ID_FIELD]})],
                             sections=[dict(mine, etichetta="Sezione 1 rivista"),
                                       _taken(piece, etichetta="Sezione 1 (seconda parte)")]))
        assert rl.push() == 1
        assert rl.pull()["sezioni"] == (0, 0, 0) and live.read_changes() == {}
        assert _engine_ids(engine, lines) == sorted([section[ID_FIELD], piece.id])


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_a_leftover_placeholder_is_ignored_and_removed_with_the_real_engine(tmp_path):
    """Finding 5 with RustGIS's own engine: a placeholder a send left there (``ppg-invio-...``) is not read by a pull,
    and the next send of that layer takes it away (its first selection takes ``LIKE 'ppg-invio-%'``)."""
    crs, x0, y0 = "EPSG:32633", 291000.0, 4640000.0
    one = _unit(1, x0, y0)
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([one]))
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        rl = link.RustGisLink(live, engine, "Saggio 1", crs)
        rl.start([])
        units = int(rl.layers["unita"])
        engine.call("run_command", {"command": "edit.addFeature", "params": {
            "layer": units, "geometry": _lonlat(box(x0 + 3, y0, x0 + 3.5, y0 + 0.5), crs),
            "attributes": {ID_FIELD: "ppg-invio-0123456789abcdef", "us_s": "9"}}})
        assert rl.pull()["unita"] == (0, 0, 0) and live.read_changes() == {}
        live.write(_drawings([dict(one, _geom=_square(x0 + 0.1, y0))]))
        assert rl.push() == 1
        assert _engine_ids(engine, units) == [one[ID_FIELD]]


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_an_empty_chunk_starts_with_the_real_engine(tmp_path):
    """Minor 12 with RustGIS's own engine: a chunk without drawings, its system checked in the middle of its area of
    use; a unit drawn in the program afterwards goes there."""
    crs = "EPSG:32633"
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings())
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        rl = link.RustGisLink(live, engine, "Saggio 1", crs)
        assert rl.start([])["layers"] == 7
        one = _unit(1, 291000.0, 4640000.0)
        live.write(_drawings([one]))
        assert rl.push() == 1
        assert _engine_ids(engine, rl.layers["unita"]) == [one[ID_FIELD]]


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_a_table_over_100000_features_takes_edits_with_the_real_engine(tmp_path):
    """OOS2 with RustGIS's own engine: 100,001 contour lines, more than it reads whole by default (it would read them in
    place and refuse every edit): the link loads them whole - listed with all their features - and a line the program
    changes goes."""
    crs, x0, y0, n = "EPSG:32633", 291000.0, 4640000.0, 100_001
    curves = [{"quota": 99.0 + k / 1000, "_geom": ("LineString", [[x0 + k % 1000 * 0.1, y0 + k // 1000 * 0.1],
                                                                   [x0 + k % 1000 * 0.1 + 0.05, y0 + k // 1000 * 0.1]])}
              for k in range(n)]
    one = _unit(1, x0, y0 - 5)
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([one], curves=curves))
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        rl = link.RustGisLink(live, engine, "Saggio 1", crs)
        rl.start([])
        (layer,) = [l for l in engine.call("list_layers", {})["layers"] if str(l["id"]) == rl.layers["curve"]]
        assert layer["features"] == n  # a layer read in place is listed with none
        curves[7] = dict(curves[7], quota=500.0)  # the others run from 99 to 199
        live.write(_drawings([one], curves=curves))
        assert rl.push() == 2 and not rl.pending  # the line as it was deleted there, as it is now added
        found = engine.call("read_features", {"layer": int(rl.layers["curve"]), "where": '"quota" = 500', "limit": 5,
                                              "fields": ["quota"]})
        assert found["matched"] == 1


# ---- what the window needs: edits listed in the linked layers, the layers taken away when RustGIS gets another chunk,
# rasters that follow their files, and what earlier links left behind

def _free_port() -> int:
    """A port of 127.0.0.1 nothing listens at (the system gave it, and it was let go at once)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_edits_listed_in_the_linked_layers(tmp_path):
    """status_changed() keeps what RustGIS's edit status said: edited_there says whether it lists features added,
    modified or deleted in the layer of a linked two-way table - the window pulls now and then while it does, since a
    feature edited twice there may leave the status as it was - never for a one-way table (never pulled), and nothing
    once stopped. RustGIS lists them until its project is saved there."""
    one = _unit(1, GX, GY)
    curve = {"quota": 99.5, "_geom": ("LineString", [[GX, GY], [GX + 2, GY + 2]])}
    live, fake, rl = _started(tmp_path, _drawings([one], curves=[curve]))
    assert not rl.edited_there  # nothing read yet
    assert not rl.status_changed() and not rl.edited_there
    fake.draw(fake.linked(rl, "curve"), shapely.geometry.LineString([(GX, GY + 3), (GX + 1, GY + 3)]), GB, quota=98.0)
    assert rl.status_changed() and not rl.edited_there
    fake.reshape(fake.linked(rl, "unita"), one[ID_FIELD], box(GX, GY, GX + 0.8, GY + 0.5), GB)
    assert rl.status_changed() and rl.edited_there
    assert rl.pull()["unita"] == (1, 0, 0)
    assert not rl.status_changed() and rl.edited_there
    fake.status.clear()  # the project saved in RustGIS
    assert rl.status_changed() and not rl.edited_there
    fake.status[int(rl.layers["unita"])] = [0, 2, 0]
    assert rl.status_changed() and rl.edited_there
    rl.stop()
    assert not rl.edited_there


def test_stop_can_take_the_layers_away(tmp_path, caplog):
    """stop(withdraw=True), for when RustGIS is to show another chunk: the tables' layers that are still the link's, the
    rasters' layers and tile layers, and the chunk's group once nothing else is in it, go from RustGIS - the layers in
    one script; a layer of the user's stays, and so does the group when the user put one in it. Never raised: RustGIS
    busy, the layers stay there, said in the log; the server closes all the same."""
    dem = _small_dem(tmp_path / "dem.tif")
    large = _header_only(tmp_path / "ortofoto.tif", 8000, 6000, 4)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    osm = {"name": "OSM", "url": "https://tile.example.org/{z}/{x}/{y}.png", "kind": "raster"}
    links = []

    def started():
        fake = FakeEngine()
        rl = link.RustGisLink(live, fake, "Saggio 1", GB)
        links.append(rl)
        rl.start([{"path": large, "name": "Ortofoto"}, {"path": dem, "name": "DEM", "hidden": True}])
        fake.layers.append({"id": 999, "name": "Mio", "source": str(tmp_path / "mio.gpkg"), "table": "mio",
                            "kind": "Polygon", "srs": 3004, "fields": {}, "features": [], "selection": set()})
        fake.tile_layers.append(dict(osm))
        fake.calls.clear()
        return fake, rl

    try:
        fake, rl = started()
        assert len(fake.layers) == 9 and len(fake.tile_layers) == 2  # 7 tables, the DEM, the user's; the orthophoto
        server = rl.server
        rl.stop(withdraw=True)
        assert [l["name"] for l in fake.layers] == ["Mio"] and fake.groups == [] and fake.tile_layers == [osm]
        assert rl.server is None and not server.thread.is_alive() and rl.layers == {} and rl.rasters == []
        (script,) = [args["script"] for tool, args in fake.calls if tool == "run" and "layer.remove" in args["script"]]
        assert [command for command, _ in _commands(script)] == ["layer.remove", "group.remove"]
        fake, rl = started()  # the user put a layer of theirs in the chunk's group: the group stays, with it
        fake.groups[0]["layers"].append(999)
        rl.stop(withdraw=True)
        assert [l["name"] for l in fake.layers] == ["Mio"] and [g["layers"] for g in fake.groups] == [[999]]
        fake, rl = started()  # another project open in RustGIS under the same ids: none of its layers is touched
        for layer in fake.layers:
            if layer.get("table"):
                layer["source"] = str(tmp_path / "altro.gpkg")
        rl.stop(withdraw=True)
        assert len(fake.layers) == 8 and "DEM" not in [l["name"] for l in fake.layers]  # read from the file given
        assert len(fake.groups) == 1  # with the other project's layers in it
        fake, rl = started()
        fake.busy.append("list_layers")  # RustGIS busy: the layers stay, the tile layer goes at the next reading
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            rl.stop(withdraw=True)
        assert len(fake.layers) == 9 and "stay in RustGIS" in caplog.text and rl.server is None
    finally:
        for rl in links:
            rl.stop()


def test_rasters_follow_their_files(tmp_path, caplog):
    """update_rasters: a raster whose file is another since it was given - rebuilt, patched, another version made active
    - is taken out of RustGIS and given again: its layer into the chunk's group under the tables, hidden as asked, or
    its tile layer read from a new address (the old one no longer answers). One no longer given is taken out, one new is
    given; nothing is asked of RustGIS while every file is the one given, nor of one RustGIS refused until its file
    changes."""
    dem = _small_dem(tmp_path / "dem.tif")
    other = _small_dem(tmp_path / "dem_2.tif")
    broken = _small_dem(tmp_path / "rotto.tif")
    large = _header_only(tmp_path / "ortofoto.tif", 8000, 6000, 4)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine()
    fake.rules.append(lambda command, p: "this is not a raster RustGIS can read"
                      if command == "layer.addData" and str(p.get("path", "")).endswith("rotto.tif") else None)
    rl = link.RustGisLink(live, fake, "Saggio 1", GB)
    rasters = [{"path": large, "name": "Ortofoto"}, {"path": dem, "name": "DEM", "hidden": True},
               {"path": broken, "name": "Rotto"}]
    titles = [title for title, _ in LIVE_TABLES.values()]
    try:
        assert "Rotto" in rl.start(rasters)["refused"]
        ortho, old = rl.rasters
        fake.calls.clear()
        assert not rl.rasters_changed()
        assert rl.update_rasters(rasters) == {} and fake.calls == []  # the same files: nothing asked
        later = dem.stat().st_mtime_ns + 10**9
        _small_dem(dem)
        os.utime(dem, ns=(later, later))  # the DEM rebuilt
        assert rl.rasters_changed() and fake.calls == []  # its dates only
        assert rl.update_rasters(rasters) == {"given": ["DEM"], "taken": ["DEM"], "refused": {}, "copies": []}
        assert not rl.rasters_changed()
        assert rl.rasters[0] is ortho and fake.tile_layers[0]["url"] == ortho.url  # untouched
        new = rl.rasters[1]
        assert new.name == "DEM" and new.layer != old.layer and int(old.layer) not in [l["id"] for l in fake.layers]
        assert fake.layer(int(new.layer))["visible"] is False and int(new.layer) in fake.groups[0]["layers"]
        names = [l["name"] for l in fake.layers]
        assert names.index("DEM") > max(names.index(t) for t in titles)  # under the tables
        port = rl.server.server_address[1]
        later += 10**9
        os.utime(large, ns=(later, later))  # the orthophoto rebuilt: a new address, the old one gone
        assert rl.update_rasters(rasters)["given"] == ["Ortofoto"]
        assert [t["url"] for t in fake.tile_layers] == [rl.rasters[-1].url] and rl.rasters[-1].url != ortho.url
        assert _http(port, urllib.parse.urlsplit(ortho.url).path)[0] == 404
        out = rl.update_rasters([{"path": large, "name": "Ortofoto"}, {"path": other, "name": "DEM 2"},
                                 {"path": broken, "name": "Rotto"}])
        assert out == {"given": ["DEM 2"], "taken": ["DEM"], "refused": {}, "copies": []}
        assert sorted(r.name for r in rl.rasters) == ["DEM 2", "Ortofoto"] and "DEM" not in [l["name"] for l in
                                                                                           fake.layers]
        later += 10**9
        os.utime(broken, ns=(later, later))  # the raster RustGIS refused, another file now: tried again
        assert rl.rasters_changed()
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            out = rl.update_rasters([{"path": large, "name": "Ortofoto"}, {"path": other, "name": "DEM 2"},
                                     {"path": broken, "name": "Rotto"}])
        assert list(out["refused"]) == ["Rotto"] and out["given"] == []
        fake.busy.append("list_layers")  # RustGIS busy: raised, nothing taken; the next call does it
        os.utime(other, ns=(later + 10**9, later + 10**9))
        with pytest.raises(link.RustGisBusy):
            rl.update_rasters([{"path": large, "name": "Ortofoto"}, {"path": other, "name": "DEM 2"}])
        assert rl.update_rasters([{"path": large, "name": "Ortofoto"}, {"path": other, "name": "DEM 2"}])["given"] == [
            "DEM 2"]
        assert [l["name"] for l in fake.layers].count("DEM 2") == 1
    finally:
        rl.stop()


def test_start_clears_what_earlier_links_left(tmp_path):
    """What an earlier link left goes at the start. In the copies' folder: the temporary file of a copy that never ended
    (a crash), once it is old - one being written is young -, and the copies no raster of the link reads (their raster
    gone, or not given now), with their notes; nothing else there. In RustGIS: the tile layers of a raster server no
    longer there (their removal failed: RustGIS busy then, or the program closed) - never one that still answers
    (another window's link), nor one of the user's."""
    photo = _two_tone(tmp_path / "foto.tif", 3, compress="webp", **TILED)  # RustGIS reads it as a copy
    data_dir = tmp_path / "chunk_1"
    copies = data_dir / "rustgis"
    copies.mkdir(parents=True)
    old = time.time() - 3600

    def placed(name: str, text: str = "x", note: dict | None = None, age: float | None = None) -> Path:
        path = copies / name
        path.write_text(text, encoding="utf-8")
        if note is not None:
            path.with_name(name + ".json").write_text(json.dumps(note), encoding="utf-8")
        if age is not None:
            os.utime(path, (age, age))
        return path

    placed(".foto-k2j3h4l5.tif", age=old)  # a copy that never ended: gone
    young = placed(".ortofoto-a1b2c3d4.tif")  # being written by another link: stays
    placed("vecchia-0123456789ab.tif", note={"layout": 1, "source": str(tmp_path / "sparita.tif"), "identity": []})
    placed("foto-abcdefabcdef.tif", note={"layout": 1, "source": str(photo), "identity": []})  # not given now
    placed("foto.tif.tmp")
    placed("leggimi.txt")
    placed("orfana-0123456789ab.tif.json", note=None)  # a note without its copy
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    other_window = link.RasterServer()
    dead = f"http://127.0.0.1:{_free_port()}/{'a' * 32}/{'b' * 32}.tif"
    alive = other_window.url(photo)
    osm = "https://tile.example.org/{z}/{x}/{y}.png"
    fake = FakeEngine()
    fake.tile_layers = [{"name": "Ortofoto", "url": dead, "kind": "cog"}, {"name": "Ortofoto", "url": alive,
                                                                          "kind": "cog"},
                        {"name": "OSM", "url": osm, "kind": "raster"}]
    rl = link.RustGisLink(live, fake, "Saggio 1", GB, data_dir=data_dir)
    try:
        rl.start([{"path": photo, "name": "Foto"}])
        (given,) = rl.rasters
        assert sorted(p.name for p in copies.iterdir()) == sorted(
            [young.name, given.copy.name, given.copy.name + ".json", "foto.tif.tmp", "leggimi.txt"])
        assert [t["url"] for t in fake.tile_layers] == [alive, osm] and [t["url"] for t in fake.removed] == [dead]
        assert [t["name"] for t in fake.tile_layers] == ["Ortofoto", "OSM"]
    finally:
        rl.stop()
        other_window.close()


@pytest.mark.skipif(not CLI, reason="PPG_RUSTGIS_CLI does not name a rustgis-cli")
def test_rasters_following_their_files_and_layers_taken_away_with_the_real_engine(tmp_path):
    """update_rasters and stop(withdraw=True) with RustGIS's own engine: the DEM rebuilt is given again into the chunk's
    group, under the tables, hidden as given; the link's layers and its group then go, a layer of the user's stays."""
    crs, x0, y0 = "EPSG:32633", 291000.0, 4640000.0
    dem = _small_dem(tmp_path / "dem.tif", crs=crs, x0=x0, y0=y0)
    mine = _small_dem(tmp_path / "mio.tif", crs=crs, x0=x0, y0=y0)
    live = LiveLink(tmp_path / "qgis", crs)
    live.write(_drawings([_unit(1, x0, y0)]))
    rasters = [{"path": dem, "name": "DEM", "hidden": True}]
    with link.StdioTransport(Path(CLI), ["mcp", "--yes"]) as engine:
        rl = link.RustGisLink(live, engine, "Saggio 1", crs, data_dir=tmp_path / "chunk_1")
        rl.start(rasters)
        engine.call("run_command", {"command": "layer.addData", "params": {"path": str(mine), "name": "Mio"}})
        (old,) = rl.rasters
        later = dem.stat().st_mtime_ns + 10**9
        os.utime(dem, ns=(later, later))
        assert rl.update_rasters(rasters) == {"given": ["DEM"], "taken": ["DEM"], "refused": {}, "copies": []}
        (new,) = rl.rasters
        listing = engine.call("list_layers", {})
        layers = {str(l["id"]): l for l in listing["layers"]}
        assert old.layer not in layers and layers[new.layer]["visible"] is False
        (group,) = [g for g in listing["groups"] if g["name"] == "Saggio 1"]
        assert group["layers"][-1] == int(new.layer)  # in the group, under the tables
        assert set(group["layers"][:-1]) == {int(i) for i in rl.layers.values()}
        rl.stop(withdraw=True)
        listing = engine.call("list_layers", {})
        assert [l["name"] for l in listing["layers"]] == ["Mio"] and listing["groups"] == []


def test_copies_left_for_a_step_of_their_own(tmp_path, monkeypatch):
    """update_rasters(copies=False), as the window's looks call it: a raster RustGIS reads only as a copy, rebuilt,
    keeps its old layer and is listed in "copies" - nothing written, nothing asked of RustGIS -; write_copies writes
    the copy, its progress following it, and the next update gives it. A copy that cannot be written (no room on the
    disk) is refused with its reason at the next update, and not tried again while the raster is that file; one whose
    copy was cancelled the same, without a half copy left. rasters_changed says when there is something to do."""
    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    photo = _two_tone(tmp_path / "foto.tif", 3, compress="webp", **TILED)  # RustGIS reads it as a copy
    data_dir = tmp_path / "chunk_1"
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine()
    rl = link.RustGisLink(live, fake, "Saggio 1", GB, data_dir=data_dir)
    rasters = [{"path": photo, "name": "Foto"}]

    def rebuilt():
        later = photo.stat().st_mtime_ns + 10**9
        os.utime(photo, ns=(later, later))

    try:
        rl.start(rasters)
        (old,) = rl.rasters
        made = old.copy.stat().st_mtime_ns
        assert not rl.rasters_changed() and rl.update_rasters(rasters, copies=False) == {}
        rebuilt()
        assert rl.rasters_changed()
        fake.calls.clear()
        out = rl.update_rasters(rasters, copies=False)
        assert out == {"given": [], "taken": [], "refused": {}, "copies": [photo]} and fake.calls == []
        assert rl.rasters == [old] and old.copy.stat().st_mtime_ns == made  # the old layer kept, no copy written
        seen = []
        assert rl.write_copies(out["copies"], Progress(lambda f, m: seen.append(m))) == {}
        assert old.copy.stat().st_mtime_ns != made and "foto.tif: copy for RustGIS, block 1 of 1" in seen
        assert rl.update_rasters(rasters, copies=False) == {"given": ["Foto"], "taken": ["Foto"], "refused": {},
                                                            "copies": []}
        assert rl.rasters[0] is not old and not rl.rasters_changed()
        # no room on the disk for the next copy: refused with its reason, not tried again while it is that file
        rebuilt()
        monkeypatch.setattr(link, "COPY_RESERVE", 10**18)
        failed = rl.write_copies(rl.update_rasters(rasters, copies=False)["copies"])
        assert list(failed) == [photo] and "free" in failed[photo]
        out = rl.update_rasters(rasters, copies=False)
        assert out["taken"] == ["Foto"] and out["refused"] == {"Foto": failed[photo]} and out["copies"] == []
        assert rl.rasters == [] and not rl.rasters_changed() and rl.update_rasters(rasters, copies=False) == {}
        # its copy cancelled: the same, and no half copy left
        monkeypatch.setattr(link, "COPY_RESERVE", 0)
        rebuilt()
        assert rl.rasters_changed()
        stopped = Progress()
        stopped.cancel()
        with pytest.raises(Cancelled):
            rl.write_copies(rl.update_rasters(rasters, copies=False)["copies"], stopped)
        assert not [p.name for p in (data_dir / "rustgis").iterdir() if p.name.startswith(".")]
        out = rl.update_rasters(rasters, copies=False)
        assert "cancelled" in out["refused"]["Foto"] and out["copies"] == [] and rl.rasters == []
        rl.start(rasters)  # a new start makes it
        assert [r.name for r in rl.rasters] == ["Foto"] and rl.rasters[0].copy.is_file()
    finally:
        rl.stop()


def test_a_copy_follows_its_progress_and_stops_on_cancel(tmp_path):
    """The copy of a raster RustGIS cannot read follows the progress it is given, block by block, then its overviews;
    the cancel of that progress stops it - no copy and no temporary file left -, and a start so cancelled ends as one
    that fails: the link stopped, what it added to RustGIS taken out again."""
    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    photo = _two_tone(tmp_path / "foto.tif", 3, side=2100, compress="webp", **TILED)  # 25 blocks of 512
    seen = []
    link._write_copy(photo, tmp_path / "copia.tif", Progress(lambda f, m: seen.append((round(f, 3), m))))
    assert seen == [(0.0, "foto.tif: copy for RustGIS, block 1 of 25"),
                    (round(0.9 * 16 / 25, 3), "foto.tif: copy for RustGIS, block 17 of 25"),
                    (0.9, "foto.tif: overviews of the copy for RustGIS")]
    stopped = Progress(lambda f, m: stopped.cancel())  # Annulla pressed at the first report
    with pytest.raises(Cancelled):
        link._write_copy(photo, tmp_path / "annullata.tif", stopped)
    assert not (tmp_path / "annullata.tif").exists() and not list(tmp_path.glob(".annullata-*"))
    data_dir = tmp_path / "chunk_1"
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    fake = FakeEngine()
    rl = link.RustGisLink(live, fake, "Saggio 1", GB, data_dir=data_dir)
    reports = []
    started = Progress(lambda f, m: reports.append(m) or (started.cancel() if "block" in m else None))
    with pytest.raises(Cancelled):
        rl.start([{"path": _small_dem(tmp_path / "dem.tif"), "name": "DEM"}, {"path": photo, "name": "Foto"}],
                 started)
    assert reports[:2] == ["The raster DEM to RustGIS", "The raster Foto to RustGIS"]
    assert reports[2] == "foto.tif: copy for RustGIS, block 1 of 25"
    assert rl.layers == {} and rl.rasters == [] and rl.server is None
    assert fake.layers == [] and fake.groups == []  # the DEM, given first, taken out again
    assert not [p.name for p in (data_dir / "rustgis").iterdir() if p.suffix == ".tif"]


def test_a_folder_of_copies_that_cannot_be_read_never_fails_the_start(tmp_path, caplog):
    """The copies' folder unreadable (its permissions): nothing in it is cleaned, said in the log, and the link starts
    all the same."""
    copies = tmp_path / "chunk_1" / "rustgis"
    copies.mkdir(parents=True)
    live = LiveLink(tmp_path / "qgis", GB)
    live.write(_drawings([_unit(1, GX, GY)]))
    rl = link.RustGisLink(live, FakeEngine(), "Saggio 1", GB, data_dir=tmp_path / "chunk_1")
    copies.chmod(0)
    try:
        if os.access(copies, os.R_OK):
            pytest.skip("the folder stays readable (run as root)")
        with caplog.at_level(logging.WARNING, logger=link.__name__):
            assert rl.start([{"path": _small_dem(tmp_path / "dem.tif"), "name": "DEM"}])["rasters"] == 1
        assert "not cleaned" in caplog.text and rl.layers
    finally:
        copies.chmod(0o755)
        rl.stop()


def test_an_abandoned_link_writes_and_sends_nothing(tmp_path):
    """abandon(): a pull waiting for RustGIS writes nothing when its answer comes - the window shows another chunk by
    then, whose drawings the live GeoPackage is to hold -, and no pull, send or update of the rasters starts after;
    stop() still stops the link."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    fake.reshape(fake.linked(rl, "unita"), one[ID_FIELD], box(GX + 0.3, GY, GX + 0.8, GY + 0.5), GB)
    asked, release, caught = threading.Event(), threading.Event(), []

    def slow(tool, args):  # RustGIS slow to export: the window gives up meanwhile
        if args.get("command") == "share.exportLayer" and not release.is_set():
            asked.set()
            release.wait(10)

    def pull():
        try:
            rl.pull()
        except Exception as exc:  # noqa: BLE001 - what the pull raised, for the test
            caught.append(exc)

    fake.hooks.append(slow)
    pulling = threading.Thread(target=pull)
    pulling.start()
    try:
        assert asked.wait(10)
        rl.abandon()
    finally:
        release.set()
        pulling.join(10)
    assert len(caught) == 1 and isinstance(caught[0], link.RustGisAbandoned)
    assert live.read_changes() == {}  # nothing written
    for call in (rl.pull, rl.push, lambda: rl.update_rasters([])):
        with pytest.raises(link.RustGisAbandoned):
            call()
    rl.stop()
    assert rl.layers == {}


def test_a_layer_gone_is_told_from_the_other_refusals(tmp_path):
    """A pull refused because a linked layer is gone (removed in RustGIS) is a RustGisLayerGone - a send adds it
    again -; an export RustGIS refuses is a refusal of another kind, which no send makes go away."""
    one = _unit(1, GX, GY)
    live, fake, rl = _started(tmp_path, _drawings([one]))
    fake.rules.append(lambda command, p: "exports are switched off" if command == "share.exportLayer" else None)
    with pytest.raises(link.RustGisRefused) as refused:
        rl.pull()
    assert not isinstance(refused.value, link.RustGisLayerGone) and "switched off" in str(refused.value)
    fake.rules.clear()
    fake.remove(int(rl.layers["unita"]))
    with pytest.raises(link.RustGisLayerGone):
        rl.pull()
    rl.push()  # the layer added again, its rows whole
    assert rl.pull()["unita"] == (0, 0, 0)
