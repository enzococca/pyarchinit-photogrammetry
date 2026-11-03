"""Providers of the assistant: a local OpenAI-compatible server and Claude, both faked on loopback."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pyarchinit_photogrammetry.assistant import keys, providers
from pyarchinit_photogrammetry.assistant.providers import (Claude, Message, OpenAICompatible, ProviderError, ToolCall,
                                                           ToolSpec, is_loopback, probe_local)

TOOL = ToolSpec("dem_statistics", "Statistics of the DEM of a chunk",
                {"type": "object", "properties": {"chunk": {"type": "string"}}, "required": ["chunk"]})


class _Fake:
    """An HTTP server on 127.0.0.1 in a thread: records the requests, answers from ``routes``."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict | None]] = []
        self.script: list = []
        self.pause = 0.0  # between two chunks
        self.delay = 0.0  # before the first byte (a model still thinking)
        self.stall = 0.0  # after the last chunk, before closing (a server that hangs mid-stream)
        self.raw: list[bytes] | None = None  # a whole answer written as is, piece by piece
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else None

            def do_GET(self):
                fake.requests.append(("GET", self.path, None))
                fake.get(self)

            def do_POST(self):
                fake.requests.append(("POST", self.path, self._body()))
                try:
                    if fake.delay:
                        time.sleep(fake.delay)
                    if fake.raw is not None:
                        for piece in fake.raw:
                            self.wfile.write(piece)
                            self.wfile.flush()
                            time.sleep(fake.pause)
                        self.close_connection = True
                        return
                    fake.post(self)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    @staticmethod
    def json_answer(handler, data, status=200):
        raw = json.dumps(data).encode()
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(raw)))
        handler.end_headers()
        handler.wfile.write(raw)

    def sse(self, handler, chunks):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Connection", "close")
        handler.end_headers()
        try:
            for chunk in chunks:
                handler.wfile.write(chunk.encode())
                handler.wfile.flush()
                if self.pause:
                    time.sleep(self.pause)
            time.sleep(self.stall)
        except (BrokenPipeError, ConnectionResetError):
            pass
        handler.close_connection = True

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def body(self) -> dict:
        return [r for r in self.requests if r[0] == "POST"][-1][2]


class _OpenAIFake(_Fake):
    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/v1"

    def get(self, h):
        if h.path == "/v1/models":
            self.json_answer(h, {"object": "list", "data": [{"id": "m1"}, {"id": "m2"}]})
        else:
            self.json_answer(h, {"error": "not found"}, 404)

    def post(self, h):
        if h.path != "/v1/chat/completions":
            return self.json_answer(h, {"error": "not found"}, 404)
        chunks = [f"data: {json.dumps({'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]})}\n\n"
                  for delta, finish in self.script]
        self.sse(h, chunks + ["data: [DONE]\n\n"])


class _ClaudeFake(_Fake):
    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def get(self, h):
        if h.path.startswith("/v1/models"):
            self.json_answer(h, {"data": [{"type": "model", "id": "claude-opus-5-5", "display_name": "Opus 5.5",
                                           "created_at": "2026-01-01T00:00:00Z"}],
                                 "has_more": False, "first_id": "claude-opus-5-5", "last_id": "claude-opus-5-5"})
        else:
            self.json_answer(h, {"error": "not found"}, 404)

    def post(self, h):
        if h.path != "/v1/messages":
            return self.json_answer(h, {"type": "error", "error": {"type": "not_found_error", "message": "x"}}, 404)
        self.sse(h, [f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in self.script])


@pytest.fixture
def openai_fake():
    fake = _OpenAIFake()
    yield fake
    fake.close()


@pytest.fixture
def claude_fake():
    fake = _ClaudeFake()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def _memory_keyring(monkeypatch):
    """Never the user's keychain: an in-memory keyring for every test of this file."""
    store: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keys, "_keyring_get", lambda service, user: store.get((service, user)))

    def put(service, user, key):
        store[(service, user)] = key

    def drop(service, user):
        store.pop((service, user), None)

    monkeypatch.setattr(keys, "_keyring_set", put)
    monkeypatch.setattr(keys, "_keyring_delete", drop)
    monkeypatch.setattr(keys, "keyring_available", lambda: True)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return store


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _tool_call_script():
    return [({"role": "assistant", "content": "Ci"}, None),
            ({"content": "ao"}, None),
            ({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                              "function": {"name": "dem_statistics", "arguments": "{\"chunk\": "}}]}, None),
            ({"tool_calls": [{"index": 0, "function": {"arguments": "\"Saggio 1\"}"}}]}, None),
            ({}, "tool_calls")]


def test_openai_compatible_streams_text_and_tool_calls(openai_fake):
    openai_fake.script = _tool_call_script()
    received = []
    reply = OpenAICompatible(openai_fake.url, "m1").complete([Message("user", "Statistiche?")], [TOOL],
                                                            on_text=received.append)
    assert reply.text == "Ciao"
    assert received == ["Ci", "ao"]
    assert reply.tool_calls == [ToolCall("call_1", "dem_statistics", {"chunk": "Saggio 1"})]
    assert reply.stop == "tool_calls"


def test_openai_compatible_sends_tools_and_messages_in_the_openai_shape(openai_fake):
    openai_fake.script = [({"content": "Fatto"}, "stop")]
    history = [Message("user", "Statistiche?"),
               Message("assistant", "", [ToolCall("call_1", "dem_statistics", {"chunk": "Saggio 1"})]),
               Message("tool", json.dumps({"mean": 12.5}), tool_call_id="call_1")]
    reply = OpenAICompatible(openai_fake.url, "m1").complete([Message("system", "Sei l'assistente.")] + history,
                                                            [TOOL])
    assert reply.text == "Fatto" and reply.stop == "end"
    body = openai_fake.body
    assert body["model"] == "m1" and body["stream"] is True and body["tool_choice"] == "auto"
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "dem_statistics"
    assert body["tools"][0]["function"]["parameters"] == TOOL.parameters
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "tool"]
    call = body["messages"][2]["tool_calls"][0]
    assert call["id"] == "call_1" and call["type"] == "function"
    assert json.loads(call["function"]["arguments"]) == {"chunk": "Saggio 1"}
    assert body["messages"][3]["tool_call_id"] == "call_1"
    assert json.loads(body["messages"][3]["content"]) == {"mean": 12.5}


def test_list_models_and_check(openai_fake):
    provider = OpenAICompatible(openai_fake.url, "m1")
    assert provider.list_models() == ["m1", "m2"]
    assert "2" in provider.check()
    assert provider.kind == "local" and "m1" in provider.label


def test_local_kind_refuses_a_foreign_address():
    with pytest.raises(ProviderError):
        OpenAICompatible("http://example.com/v1", "m")
    for url in ("http://127.0.0.1:1234/v1", "http://localhost:8080/v1", "http://[::1]:11434/v1"):
        assert OpenAICompatible(url, "m").kind == "local"
    assert is_loopback("http://127.0.0.1:1234/v1") and not is_loopback("https://api.openai.com/v1")
    assert not is_loopback("http://127.0.0.1.example.com/v1")
    remote = OpenAICompatible("https://api.openai.com/v1", "gpt", api_key="k")
    assert remote.kind == "openai"


def test_unreachable_server_fails_within_the_timeout():
    url = f"http://127.0.0.1:{_closed_port()}/v1"
    start = time.monotonic()
    with pytest.raises(ProviderError) as error:
        OpenAICompatible(url, "m", timeout=0.5).complete([Message("user", "ciao")])
    assert time.monotonic() - start < 3
    assert f"127.0.0.1:{url.split(':')[2].split('/')[0]}" in str(error.value)


def test_cancel_stops_the_stream(openai_fake):
    openai_fake.script = [({"content": f"parola{i} "}, None) for i in range(20)] + [({}, "stop")]
    openai_fake.pause = 0.05
    received = []
    start = time.monotonic()
    reply = OpenAICompatible(openai_fake.url, "m1").complete([Message("user", "parla")], on_text=received.append,
                                                            cancel=lambda: len(received) >= 3)
    assert 3 <= len(reply.text.split()) <= 5
    assert reply.stop == "end"
    assert time.monotonic() - start < 0.05 * 15


def _claude_events(blocks, stop_reason):
    events = [{"type": "message_start",
               "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
                           "content": [], "stop_reason": None, "stop_sequence": None,
                           "usage": {"input_tokens": 10, "output_tokens": 1}}}]
    for i, block in enumerate(blocks):
        if block[0] == "text":
            events.append({"type": "content_block_start", "index": i,
                           "content_block": {"type": "text", "text": ""}})
            for piece in block[1]:
                events.append({"type": "content_block_delta", "index": i,
                               "delta": {"type": "text_delta", "text": piece}})
        else:
            _, tool_id, name, parts = block
            events.append({"type": "content_block_start", "index": i,
                           "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}}})
            for part in parts:
                events.append({"type": "content_block_delta", "index": i,
                               "delta": {"type": "input_json_delta", "partial_json": part}})
        events.append({"type": "content_block_stop", "index": i})
    events.append({"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                   "usage": {"output_tokens": 7}})
    events.append({"type": "message_stop"})
    return events


def test_claude_streams_text_tool_use_and_refusal(claude_fake):
    pytest.importorskip("anthropic")
    claude_fake.script = _claude_events([("text", ["Ci", "ao"]),
                                         ("tool", "toolu_1", "dem_statistics", ["{\"chunk\": ", "\"Saggio 1\"}"])],
                                        "tool_use")
    provider = Claude(api_key="test", base_url=claude_fake.url)
    assert provider.kind == "claude" and provider.model == "claude-opus-5-5"
    received = []
    reply = provider.complete([Message("user", "Statistiche?")], [TOOL], on_text=received.append)
    assert reply.text == "Ciao" and received == ["Ci", "ao"]
    assert reply.tool_calls == [ToolCall("toolu_1", "dem_statistics", {"chunk": "Saggio 1"})]
    assert reply.stop == "tool_calls"
    assert reply.usage["output_tokens"] == 7

    claude_fake.script = _claude_events([("text", ["No."])], "refusal")
    reply = provider.complete([Message("user", "Qualcosa di vietato")])
    assert reply.stop == "refusal" and reply.text == "No."
    assert provider.list_models() == ["claude-opus-5-5"]


def test_claude_cancel_keeps_the_text_shown(claude_fake):
    pytest.importorskip("anthropic")
    claude_fake.script = _claude_events([("text", ["Ci", "ao", " a", " tutti"]),
                                         ("tool", "toolu_1", "dem_statistics", ["{}"])], "tool_use")
    received = []
    reply = Claude(api_key="test", base_url=claude_fake.url).complete(
        [Message("user", "Ciao")], [TOOL], on_text=received.append, cancel=lambda: len(received) >= 2)
    assert reply.text == "Ciao" and received == ["Ci", "ao"]
    assert reply.stop == "end" and reply.tool_calls == []


def test_claude_request_has_prompt_caching_and_tool_results(claude_fake):
    pytest.importorskip("anthropic")
    claude_fake.script = _claude_events([("text", ["Fatto"])], "end_turn")
    other = ToolSpec("list_chunks", "Chunks of the project", {"type": "object", "properties": {}})
    history = [Message("system", "Sei l'assistente."),
               Message("user", "Statistiche?"),
               Message("assistant", "Guardo.", [ToolCall("toolu_1", "dem_statistics", {"chunk": "Saggio 1"})]),
               Message("tool", json.dumps({"mean": 12.5}), tool_call_id="toolu_1")]
    reply = Claude(api_key="test", base_url=claude_fake.url).complete(history, [other, TOOL])
    assert reply.stop == "end" and reply.text == "Fatto"
    body = claude_fake.body
    assert body["model"] == "claude-opus-5-5" and body["max_tokens"] > 0
    assert body["system"][0]["text"] == "Sei l'assistente."
    assert body["system"][0]["cache_control"]["type"] == "ephemeral"
    assert "cache_control" not in body["tools"][0]
    assert body["tools"][-1]["cache_control"]["type"] == "ephemeral"
    assert body["tools"][-1]["input_schema"] == TOOL.parameters
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    use = [b for b in body["messages"][1]["content"] if b["type"] == "tool_use"][0]
    assert use == {"type": "tool_use", "id": "toolu_1", "name": "dem_statistics", "input": {"chunk": "Saggio 1"}}
    result = body["messages"][2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1"
    assert json.loads(result["content"]) == {"mean": 12.5}


def test_claude_without_the_library_says_how_to_install_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)
    with pytest.raises(ProviderError, match="assistant"):
        Claude(api_key="test")


def test_probe_local_finds_only_answering_servers(openai_fake, monkeypatch):
    monkeypatch.setattr(providers, "LOCAL_SERVERS", {"lmstudio": openai_fake.url,
                                                     "ollama": f"http://127.0.0.1:{_closed_port()}/v1"})
    assert probe_local() == [("lmstudio", openai_fake.url, ["m1", "m2"])]


def test_make_provider(openai_fake, monkeypatch):
    local = providers.make_provider("local", "m2", openai_fake.url)
    assert local.kind == "local" and local.model == "m2"
    with pytest.raises(ProviderError):
        providers.make_provider("openai", "gpt")  # no key anywhere
    monkeypatch.setenv("OPENAI_API_KEY", "k-env")
    remote = providers.make_provider("openai", "gpt")
    assert remote.kind == "openai" and remote.base_url == providers.OPENAI_URL
    with pytest.raises(ProviderError):
        providers.make_provider("nessuno")


def test_keys_env_then_keyring(monkeypatch, _memory_keyring):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-env")
    assert keys.get_key("claude") == "k-env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert keys.get_key("claude") is None
    keys.set_key("claude", "k-ring")
    assert _memory_keyring == {(keys.SERVICE, "claude"): "k-ring"}
    assert keys.get_key("claude") == "k-ring"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-env")
    assert keys.get_key("claude") == "k-env"  # the environment wins
    keys.forget_key("claude")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert keys.get_key("claude") is None


def test_keys_without_a_keyring(monkeypatch):
    monkeypatch.undo()  # the real helpers of keys.py, with the library missing
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert keys.keyring_available() is False
    assert keys.get_key("claude") is None
    with pytest.raises(keys.KeyringUnavailable):
        keys.set_key("claude", "k")
    assert issubclass(keys.KeyringUnavailable, ProviderError)
    keys.forget_key("claude")  # nothing to forget: no error


def test_keys_with_a_failing_backend(monkeypatch):
    keyring = pytest.importorskip("keyring")
    from keyring.backends import fail

    monkeypatch.undo()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    try:
        assert keys.keyring_available() is False
        assert keys.get_key("openai") is None
        with pytest.raises(keys.KeyringUnavailable):
            keys.set_key("openai", "k")
    finally:
        keyring.set_keyring(previous)


# -- fix round 1: privacy, broken servers, cancel while thinking --------------------------------------------------

def _sse_line(delta, finish=None) -> bytes:
    chunk = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()


@pytest.fixture
def proxy(monkeypatch):
    """A proxy on loopback named by every proxy variable: it records the connections it receives."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(0.1)
    hits: list = []
    done = threading.Event()

    def accept():
        while not done.is_set():
            try:
                conn, _ = server.accept()
            except (socket.timeout, OSError):
                continue
            hits.append(conn.recv(4096))
            conn.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.getsockname()[1]}"
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, url)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    yield hits
    done.set()
    thread.join(2)
    server.close()


def test_local_requests_never_go_through_a_proxy(openai_fake, proxy, monkeypatch):
    import urllib.request

    assert urllib.request.getproxies().get("http")  # the environment does name the proxy
    monkeypatch.setattr(urllib.request, "_opener", None)  # urlopen's default opener reads it again
    openai_fake.script = [({"content": "Ciao"}, "stop")]
    provider = OpenAICompatible(openai_fake.url, "m1", timeout=2)
    assert provider.complete([Message("user", "Dati riservati dello scavo")]).text == "Ciao"
    assert provider.list_models() == ["m1", "m2"]
    monkeypatch.setattr(providers, "LOCAL_SERVERS", {"lmstudio": openai_fake.url})
    assert probe_local(timeout=2) == [("lmstudio", openai_fake.url, ["m1", "m2"])]
    time.sleep(0.2)
    assert proxy == []


def test_no_request_follows_a_redirect(openai_fake, monkeypatch):
    def redirect(h):
        h.send_response(302)
        h.send_header("Location", "http://127.0.0.1:9/v1/models")
        h.send_header("Content-Length", "0")
        h.end_headers()

    monkeypatch.setattr(openai_fake, "get", redirect)
    with pytest.raises(ProviderError, match="302"):
        OpenAICompatible(openai_fake.url, "m1").list_models()
    assert len(openai_fake.requests) == 1


def test_invalid_tool_arguments_reach_the_session(openai_fake):
    openai_fake.script = [({"content": "Ciao"}, None),
                          ({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                            "function": {"name": "dem_statistics", "arguments": "{\"a\": "}}]},
                           "tool_calls")]
    received = []
    reply = OpenAICompatible(openai_fake.url, "m1").complete([Message("user", "x")], [TOOL], on_text=received.append)
    assert reply.text == "Ciao" and received == ["Ciao"]
    assert reply.tool_calls == [ToolCall("call_1", "dem_statistics", {"_invalid_json": "{\"a\": "})]
    assert reply.stop == "tool_calls"


def test_interleaved_tool_calls_are_kept_apart(openai_fake):
    def call(index, name=None, arguments="", id=None):
        function = {"arguments": arguments}
        if name:
            function["name"] = name
        out = {"index": index, "function": function}
        if id:
            out["id"] = id
        return out

    openai_fake.script = [({"tool_calls": [call(1, "list_chunks", "{", "b"), call(0, "dem_statistics", "{\"chu", "a")]},
                           None),
                          ({"tool_calls": [call(0, arguments="nk\": \"S1\"}"), call(1, arguments="}")]}, "tool_calls")]
    reply = OpenAICompatible(openai_fake.url, "m1").complete([Message("user", "x")], [TOOL])
    assert reply.tool_calls == [ToolCall("a", "dem_statistics", {"chunk": "S1"}), ToolCall("b", "list_chunks", {})]


def test_a_character_split_across_writes_arrives_whole(openai_fake):
    line = _sse_line({"content": "perché è così"}, "stop")
    cut = line.index("é".encode()) + 1  # inside the two bytes of é
    openai_fake.raw = [b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n",
                       line[:cut], line[cut:], b"data: [DONE]\n\n"]
    openai_fake.pause = 0.05
    assert OpenAICompatible(openai_fake.url, "m1").complete([Message("user", "x")]).text == "perché è così"


def test_local_kind_is_loopback_only_even_with_a_key():
    with pytest.raises(ProviderError):
        OpenAICompatible("http://example.com/v1", "m", api_key="k", kind="local")
    with pytest.raises(ProviderError):
        providers.make_provider("local", "m", "https://remote.example/v1", api_key="k")
    assert OpenAICompatible("http://127.0.0.1:8080/v1", "m", api_key="k", kind="local").kind == "local"


def test_only_http_addresses_of_this_computer_are_loopback():
    for url in ("http://127.0.0.1@evil.com/v1", "http://127.0.0.1:80@evil.com/v1", "http://localhost.evil/v1",
                "file://localhost/etc/passwd", "ftp://127.0.0.1/", "http://evil.com\\@127.0.0.1/",
                "http://user@127.0.0.1/v1", "http://0.0.0.0:1234/v1"):
        assert not is_loopback(url), url
    for url in ("https://localhost/v1", "http://LOCALHOST:1234/v1", "http://127.0.0.2:8000/v1"):
        assert is_loopback(url), url


def test_a_server_cut_off_mid_stream_is_a_provider_error(openai_fake):
    first = _sse_line({"content": "Ci"})
    openai_fake.raw = [b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n",
                       f"{len(first):x}\r\n".encode() + first + b"\r\n",
                       b"64\r\ndata: {\"cho"]  # a chunk of 100 bytes that never ends
    with pytest.raises(ProviderError, match="127.0.0.1"):
        OpenAICompatible(openai_fake.url, "m1").complete([Message("user", "x")])

    openai_fake.raw = [b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n", first]
    with pytest.raises(ProviderError, match="interrotta"):  # closed without finish_reason nor [DONE]
        OpenAICompatible(openai_fake.url, "m1").complete([Message("user", "x")])

    body = b"{not json"
    openai_fake.raw = [b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                       + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body]
    with pytest.raises(ProviderError, match="127.0.0.1"):
        OpenAICompatible(openai_fake.url, "m1").complete([Message("user", "x")])


def test_a_silent_server_fails_within_the_timeout(openai_fake):
    openai_fake.delay = 3
    start = time.monotonic()
    with pytest.raises(ProviderError, match=f"127.0.0.1:{openai_fake.port}"):
        OpenAICompatible(openai_fake.url, "m1", timeout=0.5).complete([Message("user", "x")])
    assert time.monotonic() - start < 2


def test_cancel_while_the_model_is_still_thinking(openai_fake):
    openai_fake.delay = 3
    start = time.monotonic()
    reply = OpenAICompatible(openai_fake.url, "m1", timeout=10).complete(
        [Message("user", "x")], cancel=lambda: time.monotonic() - start > 0.2)
    # by design within CANCEL_POLL of the cancel (~0.3 s here): 2 s leaves room for a loaded CI runner, and is
    # still well under the 3 s the model keeps thinking
    assert time.monotonic() - start < 2
    assert reply.stop == "end" and reply.text == "" and reply.tool_calls == []


def test_cancel_while_the_model_pauses_mid_answer(openai_fake):
    openai_fake.script = [({"content": "Ci"}, None)]
    openai_fake.stall = 3
    received = []
    start = time.monotonic()
    reply = OpenAICompatible(openai_fake.url, "m1", timeout=10).complete(
        [Message("user", "x")], on_text=received.append, cancel=lambda: time.monotonic() - start > 0.3)
    assert time.monotonic() - start < 1.2
    assert reply.stop == "end" and reply.text == "Ci"


def test_claude_cancel_while_thinking(claude_fake):
    pytest.importorskip("anthropic")
    claude_fake.delay = 3
    claude_fake.script = _claude_events([("text", ["tardi"])], "end_turn")
    start = time.monotonic()
    reply = Claude(api_key="test", base_url=claude_fake.url, timeout=10).complete(
        [Message("user", "x")], cancel=lambda: time.monotonic() - start > 0.2)
    assert time.monotonic() - start < 1
    assert reply.stop == "end" and reply.text == ""


def test_claude_stalled_stream_is_a_provider_error(claude_fake):
    pytest.importorskip("anthropic")
    claude_fake.script = _claude_events([], "end_turn")[:1]  # message_start, then nothing
    claude_fake.stall = 3
    start = time.monotonic()
    with pytest.raises(ProviderError, match=f"127.0.0.1:{claude_fake.port}"):
        Claude(api_key="test", base_url=claude_fake.url, timeout=0.5).complete([Message("user", "x")])
    assert time.monotonic() - start < 2.5


def test_claude_stream_dropped_before_the_end_is_a_provider_error(claude_fake):
    pytest.importorskip("anthropic")
    claude_fake.script = _claude_events([("text", ["Ci"])], "end_turn")[:-2]  # no message_delta, no message_stop
    with pytest.raises(ProviderError, match="interrotta"):
        Claude(api_key="test", base_url=claude_fake.url).complete([Message("user", "x")])


def test_claude_details_of_the_request_and_the_stop(claude_fake):
    pytest.importorskip("anthropic")
    claude_fake.script = _claude_events([("text", ["Troppo"]),
                                         ("tool", "toolu_2", "dem_statistics", ["{\"a\": tru"])],
                                        "model_context_window_exceeded")
    history = [Message("user", "Statistiche?"),
               Message("assistant", "", [ToolCall("toolu_1", "dem_statistics", {"chunk": "X"})]),
               Message("tool", json.dumps({"errore": "chunk sconosciuto"}), tool_call_id="toolu_1", error=True),
               Message("user", "   ")]
    reply = Claude(api_key="test", base_url=claude_fake.url).complete(history, [TOOL])
    assert reply.stop == "length"
    assert reply.tool_calls == [ToolCall("toolu_2", "dem_statistics", {"_invalid_json": "{\"a\": tru"})]
    body = claude_fake.body
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]  # the empty question is not sent
    assert body["messages"][2]["content"] == [{"type": "tool_result", "tool_use_id": "toolu_1", "is_error": True,
                                               "content": json.dumps({"errore": "chunk sconosciuto"})}]
