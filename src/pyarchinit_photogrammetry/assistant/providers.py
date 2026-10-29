"""Language-model providers of the assistant.

Two shapes: the OpenAI chat-completions API (local servers - LM Studio, llama.cpp, Ollama, Unsloth - on this
computer only, or OpenAI itself with a key) and Claude through the ``anthropic`` SDK. Both stream: ``complete``
hands each text delta to ``on_text`` and, as soon as ``cancel()`` is true (checked every 0.1 s, also while the
model is still thinking), stops and returns what was received with ``stop="end"``.

Requests to this computer never go through a proxy and no request follows a redirect: the text of a local model
never leaves the computer, and a key never reaches another host.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import queue
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

log = logging.getLogger(__name__)

DEFAULT_CLAUDE = "claude-opus-5-5"
CLAUDE_CHOICES = ("claude-opus-5-5", "claude-opus-5")
OPENAI_URL = "https://api.openai.com/v1"
LOCAL_SERVERS = {"lmstudio": "http://127.0.0.1:1234/v1", "llama.cpp": "http://127.0.0.1:8080/v1",
                 "ollama": "http://127.0.0.1:11434/v1", "unsloth": "http://127.0.0.1:8000/v1"}
MAX_TOKENS = 8192  # of one Claude answer
STOPS = ("end", "tool_calls", "length", "refusal")
CANCEL_POLL = 0.1  # seconds between two looks at cancel()


@dataclass
class ToolSpec:
    """A tool the model may call: ``parameters`` is a JSON schema of type object."""
    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    """A call asked by the model. Arguments that are not a JSON object arrive as ``{"_invalid_json": raw}``."""
    id: str
    name: str
    arguments: dict


@dataclass
class Message:
    """One message of the conversation: role "system", "user", "assistant" or "tool" (a tool result: ``content``
    is JSON text, ``tool_call_id`` names the call and ``error`` marks a failed call)."""
    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    error: bool = False


@dataclass
class Reply:
    """An answer: ``stop`` is "end", "tool_calls", "length" or "refusal"."""
    text: str
    tool_calls: list[ToolCall]
    stop: str
    usage: dict


class ProviderError(Exception):
    """A provider cannot answer (address refused, server down or cut off, key missing or rejected)."""


@runtime_checkable
class Provider(Protocol):
    kind: str   # "local" | "claude" | "openai"
    model: str
    label: str  # shown in the pane

    def complete(self, messages: list[Message], tools: list[ToolSpec] | None = None,
                 on_text: Callable[[str], None] | None = None,
                 cancel: Callable[[], bool] | None = None) -> Reply: ...

    def list_models(self) -> list[str]: ...

    def check(self) -> str: ...


def is_loopback(url: str) -> bool:
    """True when ``url`` is an http(s) address of this computer (localhost or a loopback address)."""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not host:
        return False
    if "@" in parsed.netloc or "\\" in parsed.netloc:  # userinfo: parsers disagree on the host
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def _ssl_context():
    from ..updates import _ssl_context as context

    return context()


def _address(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return parsed.netloc or url


def _server_name(base_url: str) -> str:
    for name, url in LOCAL_SERVERS.items():
        if url.rstrip("/") == base_url:
            return name
    return _address(base_url)


def _cancelled(cancel: Callable[[], bool] | None) -> bool:
    if cancel is None:
        return False
    try:
        return bool(cancel())
    except Exception:  # a broken callback never stops the answer
        log.debug("cancel() ha sollevato un errore", exc_info=True)
        return False


class _Watch:
    """A thread that looks at ``cancel()`` every CANCEL_POLL seconds and calls ``stop()`` once when it is true."""

    def __init__(self, cancel: Callable[[], bool] | None, stop: Callable[[], None]):
        self.cancel, self.stop = cancel, stop
        self.fired = False
        self._done = threading.Event()

    def __enter__(self) -> "_Watch":
        if self.cancel is not None:
            threading.Thread(target=self._run, name="assistant-cancel", daemon=True).start()
        return self

    def _run(self):
        while not self._done.wait(CANCEL_POLL):
            if _cancelled(self.cancel):
                self.fired = True
                try:
                    self.stop()
                except Exception:
                    log.debug("annullamento", exc_info=True)
                return

    def __exit__(self, *exc):
        self._done.set()
        return False


class _Sockets:
    """The sockets of one request, so that a cancel can shut them down (the server sees the client gone)."""

    def __init__(self):
        self._socks: list = []
        self._lock = threading.Lock()
        self._shut = False

    def add(self, sock):
        with self._lock:
            self._socks.append(sock)
            if self._shut:
                self._close(sock)

    def shutdown(self):
        with self._lock:
            self._shut = True
            for sock in self._socks:
                self._close(sock)

    @staticmethod
    def _close(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def _tracked(base, sockets: _Sockets):
    class Tracked(base):
        def connect(self):
            super().connect()
            sockets.add(self.sock)

    return Tracked


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects refused: the answer that redirects reaches the caller as the HTTP error it is."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _HTTP(urllib.request.HTTPHandler):
    def __init__(self, sockets: _Sockets):
        super().__init__()
        self._sockets = sockets

    def http_open(self, req):
        return self.do_open(_tracked(http.client.HTTPConnection, self._sockets), req)


class _HTTPS(urllib.request.HTTPSHandler):
    def __init__(self, sockets: _Sockets):
        super().__init__(context=_ssl_context())
        self._sockets = sockets

    def https_open(self, req):
        return self.do_open(_tracked(http.client.HTTPSConnection, self._sockets), req, context=self._context)


class _Partial:
    """What an answer has received so far: kept when it is cancelled."""

    def __init__(self):
        self.text: list[str] = []
        self.usage: dict = {}

    def reply(self) -> Reply:
        return Reply("".join(self.text), [], "end", self.usage)


class OpenAICompatible:
    """The chat-completions API of OpenAI: a local server (loopback only, key optional) or OpenAI (with a key)."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout: float = 120.0,
                 kind: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.kind = kind or ("local" if api_key is None else "openai")
        if not is_loopback(self.base_url):
            if api_key is None:
                raise ProviderError("solo indirizzi di questo computer senza chiave")
            if self.kind == "local":
                raise ProviderError("un server locale deve essere su questo computer")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    @property
    def label(self) -> str:
        if self.kind == "local":
            return f"Locale · {_server_name(self.base_url)} · {self.model}"
        return f"OpenAI · {self.model}"

    # HTTP
    def _opener(self, sockets: _Sockets):
        handlers = [_NoRedirect(), _HTTP(sockets), _HTTPS(sockets)]
        if is_loopback(self.base_url):  # never through a proxy: the text stays on this computer
            handlers.insert(0, urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)

    def _open(self, path: str, body: dict | None = None, stream: bool = False, sockets: _Sockets | None = None):
        url = f"{self.base_url}{path}"
        headers = {"Accept": "text/event-stream" if stream else "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        try:
            return self._opener(sockets or _Sockets()).open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace").strip()[:300]
            except Exception:
                detail = ""
            if exc.code in (401, 403):
                raise ProviderError(f"{_address(url)}: chiave non valida o senza permesso ({exc.code})") from exc
            raise ProviderError(f"{_address(url)} ha risposto {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ProviderError(f"{_address(url)} non risponde: {reason}") from exc

    def _broken(self, exc: Exception) -> ProviderError:
        """A transport error after the request was sent, in the words of the engine."""
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return ProviderError(f"{_address(self.base_url)} non risponde: {exc}")
        return ProviderError(f"{_address(self.base_url)}: risposta interrotta ({exc})")

    # messages
    @staticmethod
    def _message(message: Message) -> dict:
        if message.role == "tool":
            return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
        out: dict = {"role": message.role, "content": message.content}
        if message.role == "assistant" and message.tool_calls:
            out["tool_calls"] = [{"id": call.id, "type": "function",
                                  "function": {"name": call.name,
                                               "arguments": json.dumps(call.arguments, ensure_ascii=False)}}
                                 for call in message.tool_calls]
        return out

    @staticmethod
    def _tool(tool: ToolSpec) -> dict:
        return {"type": "function",
                "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters}}

    def complete(self, messages: list[Message], tools: list[ToolSpec] | None = None,
                 on_text: Callable[[str], None] | None = None,
                 cancel: Callable[[], bool] | None = None) -> Reply:
        sent = [m for m in messages if not (m.role == "user" and not m.content.strip())]
        body: dict = {"model": self.model, "messages": [self._message(m) for m in sent], "stream": True}
        if tools:
            body["tools"] = [self._tool(t) for t in tools]
            body["tool_choice"] = "auto"
        sockets, partial = _Sockets(), _Partial()
        with _Watch(cancel, sockets.shutdown) as watch:
            try:
                response = self._open("/chat/completions", body, stream=True, sockets=sockets)
                try:
                    if "json" in (response.headers.get("Content-Type") or ""):  # a server that does not stream
                        return self._whole(json.loads(response.read()), on_text)
                    return self._stream(response, on_text, cancel, partial)
                finally:
                    response.close()
            except ProviderError:
                if watch.fired:
                    return partial.reply()
                raise
            except (OSError, http.client.HTTPException, ValueError) as exc:
                if watch.fired:
                    return partial.reply()
                raise self._broken(exc) from exc

    def _stream(self, response, on_text, cancel, partial: _Partial) -> Reply:
        calls: dict[int, dict] = {}
        finish = None
        done = False
        for raw in response:
            if _cancelled(cancel):
                return partial.reply()
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                log.debug("SSE non JSON: %s", payload[:200])
                continue
            if chunk.get("error"):
                raise ProviderError(f"{_address(self.base_url)}: {chunk['error']}")
            if chunk.get("usage"):
                partial.usage = _openai_usage(chunk["usage"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    partial.text.append(piece)
                    if on_text is not None:
                        on_text(piece)
                for position, call in enumerate(delta.get("tool_calls") or []):
                    slot = calls.setdefault(call.get("index", position), {"id": "", "name": "", "arguments": ""})
                    slot["id"] = call.get("id") or slot["id"]
                    function = call.get("function") or {}
                    slot["name"] = function.get("name") or slot["name"]
                    arguments = function.get("arguments")
                    if isinstance(arguments, dict):  # some servers send the object at once
                        arguments = json.dumps(arguments)
                    slot["arguments"] += arguments or ""
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
            if _cancelled(cancel):
                return partial.reply()
        if not done and finish is None:
            raise ProviderError(f"{_address(self.base_url)}: risposta interrotta prima della fine")
        tool_calls = [_tool_call(calls[i], i) for i in sorted(calls)]
        return Reply("".join(partial.text), tool_calls, _openai_stop(finish, tool_calls), partial.usage)

    def _whole(self, data: dict, on_text) -> Reply:
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if text and on_text is not None:
            on_text(text)
        calls = [{"id": c.get("id") or "", "name": (c.get("function") or {}).get("name") or "",
                  "arguments": (c.get("function") or {}).get("arguments") or ""}
                 for c in message.get("tool_calls") or []]
        for slot in calls:
            if isinstance(slot["arguments"], dict):
                slot["arguments"] = json.dumps(slot["arguments"])
        tool_calls = [_tool_call(slot, i) for i, slot in enumerate(calls)]
        return Reply(text, tool_calls, _openai_stop(choice.get("finish_reason"), tool_calls),
                     _openai_usage(data.get("usage") or {}))

    def list_models(self) -> list[str]:
        response = self._open("/models")
        try:
            data = json.loads(response.read())
        except (ValueError, OSError, http.client.HTTPException) as exc:
            raise ProviderError(f"{_address(self.base_url)}: elenco dei modelli illeggibile ({exc})") from exc
        finally:
            response.close()
        if not isinstance(data, dict):
            raise ProviderError(f"{_address(self.base_url)}: elenco dei modelli illeggibile")
        return [m["id"] for m in data.get("data") or [] if isinstance(m, dict) and m.get("id")]

    def check(self) -> str:
        return f"ok: {len(self.list_models())} modelli"


def _arguments(raw: str) -> dict:
    """The arguments of a tool call: a JSON object, else ``{"_invalid_json": raw}`` for the session to answer."""
    if not raw.strip():
        return {}
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError:
        return {"_invalid_json": raw}
    return arguments if isinstance(arguments, dict) else {"_invalid_json": raw}


def _tool_call(slot: dict, position: int) -> ToolCall:
    return ToolCall(slot["id"] or f"call_{position}", slot["name"], _arguments(slot["arguments"]))


def _openai_stop(finish: str | None, tool_calls: list[ToolCall]) -> str:
    if tool_calls or finish in ("tool_calls", "function_call"):
        return "tool_calls"
    return {"length": "length", "content_filter": "refusal"}.get(finish or "", "end")


def _openai_usage(usage: dict) -> dict:
    return {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}


CLAUDE_STOPS = {"tool_use": "tool_calls", "max_tokens": "length", "model_context_window_exceeded": "length",
                "refusal": "refusal"}


class Claude:
    """Claude through the ``anthropic`` SDK, with prompt caching on the system text and on the tools.

    The SDK tries a failed request once more: an endpoint that never answers fails after about twice ``timeout``
    plus its back-off."""

    kind = "claude"

    def __init__(self, model: str = DEFAULT_CLAUDE, api_key: str | None = None, base_url: str | None = None,
                 timeout: float = 120.0):
        try:
            import anthropic
        except ImportError as exc:
            raise ProviderError("manca la libreria anthropic: installa l'extra assistant "
                                "(pip install \"pyarchinit-photogrammetry[assistant]\")") from exc
        if api_key is None:
            from .keys import get_key

            api_key = get_key("claude")
        if not api_key:
            raise ProviderError("manca la chiave di Claude (ANTHROPIC_API_KEY o il portachiavi)")
        self._anthropic = anthropic
        self.model = model or DEFAULT_CLAUDE
        self.base_url = base_url
        self.max_tokens = MAX_TOKENS
        self.client = anthropic.Anthropic(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=1)

    @property
    def label(self) -> str:
        return f"Claude · {self.model}"

    @staticmethod
    def _request(messages: list[Message], tools: list[ToolSpec] | None) -> dict:
        system = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
        turns: list[dict] = []
        for message in messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                result = {"type": "tool_result", "tool_use_id": message.tool_call_id, "content": message.content}
                if message.error:
                    result["is_error"] = True
                role, blocks = "user", [result]
            elif message.role == "assistant":
                role = "assistant"
                blocks = [{"type": "text", "text": message.content}] if message.content else []
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                           for c in message.tool_calls]
            else:  # an empty user text is refused by the API: skipped
                role, blocks = "user", [{"type": "text", "text": message.content}] if message.content.strip() else []
            if not blocks:
                continue
            if turns and turns[-1]["role"] == role:  # the API wants the roles to alternate
                turns[-1]["content"].extend(blocks)
            else:
                turns.append({"role": role, "content": blocks})
        for turn in turns:  # a single text block travels as plain text
            if len(turn["content"]) == 1 and turn["content"][0]["type"] == "text":
                turn["content"] = turn["content"][0]["text"]
        request: dict = {"messages": turns}
        if system:
            request["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if tools:
            specs = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
            specs[-1]["cache_control"] = {"type": "ephemeral"}
            request["tools"] = specs
        return request

    def complete(self, messages: list[Message], tools: list[ToolSpec] | None = None,
                 on_text: Callable[[str], None] | None = None,
                 cancel: Callable[[], bool] | None = None) -> Reply:
        """The SDK reads in a thread of its own; this one hands the text on and looks at ``cancel()`` meanwhile,
        so that a cancel is honoured while Claude is still thinking."""
        request = self._request(messages, tools)
        items: queue.Queue = queue.Queue()
        stop = threading.Event()
        current: dict = {}

        def read():
            try:
                with self.client.messages.stream(model=self.model, max_tokens=self.max_tokens, **request) as stream:
                    current["stream"] = stream
                    if stop.is_set():
                        return
                    for event in stream:
                        if stop.is_set():
                            return
                        items.put(("event", event))
                    items.put(("final", stream.get_final_message()))
            except BaseException as exc:  # handed to the caller's thread
                if not stop.is_set():
                    items.put(("error", exc))

        threading.Thread(target=read, name="assistant-claude", daemon=True).start()
        shown: list[str] = []  # the text handed to on_text: what a cancelled answer keeps
        raw_inputs: dict[int, str] = {}
        finished = False
        while True:
            try:
                kind, item = items.get(timeout=CANCEL_POLL)
            except queue.Empty:
                kind, item = "wait", None
            if kind in ("wait", "event") and _cancelled(cancel):
                stop.set()
                stream = current.get("stream")
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        log.debug("chiusura dello stream", exc_info=True)
                return Reply("".join(shown), [], "end", {})
            if kind == "wait":
                continue
            if kind == "error":
                raise self._error(item) from item
            if kind == "final":
                if not finished:
                    raise ProviderError(f"{self._where()}: risposta interrotta prima della fine")
                return self._reply(item, raw_inputs)
            event = item
            if event.type == "text":
                shown.append(event.text)
                if on_text is not None:
                    on_text(event.text)
            elif event.type == "content_block_delta" and getattr(event.delta, "type", "") == "input_json_delta":
                raw_inputs[event.index] = raw_inputs.get(event.index, "") + event.delta.partial_json
            elif event.type == "message_stop":
                finished = True

    @staticmethod
    def _reply(message, raw_inputs: dict[int, str]) -> Reply:
        text = "".join(b.text for b in message.content if b.type == "text")
        calls = []
        for index, block in enumerate(message.content):
            if block.type != "tool_use":
                continue
            raw = raw_inputs.get(index)
            arguments = _arguments(raw) if raw is not None else dict(block.input or {})
            calls.append(ToolCall(block.id, block.name, arguments))
        stop = CLAUDE_STOPS.get(message.stop_reason or "", "end")
        return Reply(text, calls, stop, _claude_usage(message.usage))

    def _where(self) -> str:
        return _address(self.base_url) if self.base_url else "Claude"

    def _error(self, exc: BaseException) -> ProviderError:
        anthropic = self._anthropic
        where = self._where()
        if isinstance(exc, ProviderError):
            return exc
        if isinstance(exc, anthropic.AuthenticationError):
            return ProviderError("chiave di Claude non valida")
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderError(f"{where} non risponde: {exc}")
        if isinstance(exc, anthropic.APIStatusError):
            return ProviderError(f"{where} ha risposto {exc.status_code}: {exc.message}")
        if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:  # httpx timeouts while streaming
            return ProviderError(f"{where} non risponde: {exc or type(exc).__name__}")
        return ProviderError(f"{where}: risposta interrotta ({exc or type(exc).__name__})")

    def list_models(self) -> list[str]:
        try:
            return [m.id for m in self.client.models.list()]
        except Exception as exc:
            raise self._error(exc) from exc

    def check(self) -> str:
        return f"ok: {len(self.list_models())} modelli"


def _claude_usage(usage) -> dict:
    if usage is None:
        return {}
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    return {k: getattr(usage, k, None) or 0 for k in keys}


def probe_local(timeout: float = 0.5) -> list[tuple[str, str, list[str]]]:
    """The local servers that answer ``GET /models``: (name, base_url, models), in the order of LOCAL_SERVERS."""
    servers = list(LOCAL_SERVERS.items())

    def ask(item):
        name, url = item
        try:
            return name, url, OpenAICompatible(url, "", timeout=timeout, kind="local").list_models()
        except ProviderError:
            return None

    if not servers:
        return []
    with ThreadPoolExecutor(max_workers=len(servers)) as pool:
        return [found for found in pool.map(ask, servers) if found is not None]


def make_provider(kind: str, model: str = "", base_url: str = "", api_key: str | None = None) -> Provider:
    """A provider of ``kind`` ("local", "claude" or "openai"); keys from the environment or the keyring.
    A "local" provider is on this computer, with or without a key."""
    if kind == "local":
        if not base_url:
            found = probe_local()
            if not found:
                raise ProviderError("nessun server locale risponde (LM Studio, llama.cpp, Ollama, Unsloth)")
            _, base_url, models = found[0]
            model = model or (models[0] if models else "")
        return OpenAICompatible(base_url, model, api_key=api_key, kind="local")
    if kind == "openai":
        if api_key is None:
            from .keys import get_key

            api_key = get_key("openai")
        if not api_key:
            raise ProviderError("manca la chiave di OpenAI (OPENAI_API_KEY o il portachiavi)")
        return OpenAICompatible(base_url or OPENAI_URL, model, api_key=api_key, kind="openai")
    if kind == "claude":
        return Claude(model or DEFAULT_CLAUDE, api_key=api_key, base_url=base_url or None)
    raise ProviderError(f"fornitore sconosciuto: {kind}")
