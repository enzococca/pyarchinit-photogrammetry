"""Language-model providers of the assistant.

Two shapes: the OpenAI chat-completions API (local servers - LM Studio, llama.cpp, Ollama, Unsloth - on this
computer only when there is no key, or OpenAI itself with a key) and Claude through the ``anthropic`` SDK. Both
stream: ``complete`` hands each text delta to ``on_text`` and stops reading as soon as ``cancel()`` is true.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
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


@dataclass
class ToolSpec:
    """A tool the model may call: ``parameters`` is a JSON schema of type object."""
    name: str
    description: str
    parameters: dict


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Message:
    """One message of the conversation: role "system", "user", "assistant" or "tool" (a tool result: ``content``
    is JSON text and ``tool_call_id`` names the call)."""
    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None


@dataclass
class Reply:
    """An answer: ``stop`` is "end", "tool_calls", "length" or "refusal"."""
    text: str
    tool_calls: list[ToolCall]
    stop: str
    usage: dict


class ProviderError(Exception):
    """A provider cannot answer (address refused, server down, key missing or rejected)."""


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
    """True when ``url`` names this computer (localhost or a loopback address)."""
    try:
        host = urllib.parse.urlparse(url).hostname
    except ValueError:
        return False
    if not host:
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


class OpenAICompatible:
    """The chat-completions API of OpenAI: a local server (no key, loopback only) or OpenAI (with a key)."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout: float = 120.0,
                 kind: str | None = None):
        self.base_url = base_url.rstrip("/")
        if api_key is None and not is_loopback(self.base_url):
            raise ProviderError("solo indirizzi di questo computer senza chiave")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.kind = kind or ("local" if api_key is None else "openai")

    @property
    def label(self) -> str:
        if self.kind == "local":
            return f"Locale · {_server_name(self.base_url)} · {self.model}"
        return f"OpenAI · {self.model}"

    # HTTP
    def _open(self, path: str, body: dict | None = None, stream: bool = False):
        url = f"{self.base_url}{path}"
        headers = {"Accept": "text/event-stream" if stream else "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
        context = _ssl_context() if url.startswith("https:") else None
        try:
            return urllib.request.urlopen(request, timeout=self.timeout, context=context)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace").strip()[:300]
            except Exception:
                detail = ""
            if exc.code in (401, 403):
                raise ProviderError(f"{_address(url)}: chiave non valida o senza permesso ({exc.code})") from exc
            raise ProviderError(f"{_address(url)} ha risposto {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ProviderError(f"{_address(url)} non risponde: {reason}") from exc

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
        body: dict = {"model": self.model, "messages": [self._message(m) for m in messages], "stream": True}
        if tools:
            body["tools"] = [self._tool(t) for t in tools]
            body["tool_choice"] = "auto"
        response = self._open("/chat/completions", body, stream=True)
        try:
            if "json" in (response.headers.get("Content-Type") or ""):  # a server that does not stream
                return self._whole(json.loads(response.read()), on_text)
            return self._stream(response, on_text, cancel)
        except (socket.timeout, TimeoutError, OSError) as exc:
            raise ProviderError(f"{_address(self.base_url)} non risponde: {exc}") from exc
        finally:
            response.close()

    def _stream(self, response, on_text, cancel) -> Reply:
        text: list[str] = []
        calls: dict[int, dict] = {}
        finish = None
        usage: dict = {}
        for raw in response:
            if cancel is not None and cancel():
                return Reply("".join(text), [], "end", usage)
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                log.debug("SSE non JSON: %s", payload[:200])
                continue
            if chunk.get("error"):
                raise ProviderError(f"{_address(self.base_url)}: {chunk['error']}")
            if chunk.get("usage"):
                usage = _openai_usage(chunk["usage"])
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    text.append(piece)
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
            if cancel is not None and cancel():
                return Reply("".join(text), [], "end", usage)
        tool_calls = [_tool_call(calls[i], i) for i in sorted(calls)]
        return Reply("".join(text), tool_calls, _openai_stop(finish, tool_calls), usage)

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
        except (ValueError, OSError) as exc:
            raise ProviderError(f"{_address(self.base_url)}: elenco dei modelli illeggibile ({exc})") from exc
        finally:
            response.close()
        return [m["id"] for m in data.get("data") or [] if isinstance(m, dict) and m.get("id")]

    def check(self) -> str:
        return f"ok: {len(self.list_models())} modelli"


def _tool_call(slot: dict, position: int) -> ToolCall:
    raw = slot["arguments"].strip() or "{}"
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"argomenti non validi per lo strumento {slot['name']}: {raw[:200]}") from exc
    if not isinstance(arguments, dict):
        raise ProviderError(f"argomenti non validi per lo strumento {slot['name']}: {raw[:200]}")
    return ToolCall(slot["id"] or f"call_{position}", slot["name"], arguments)


def _openai_stop(finish: str | None, tool_calls: list[ToolCall]) -> str:
    if tool_calls or finish in ("tool_calls", "function_call"):
        return "tool_calls"
    return {"length": "length", "content_filter": "refusal"}.get(finish or "", "end")


def _openai_usage(usage: dict) -> dict:
    return {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}


class Claude:
    """Claude through the ``anthropic`` SDK, with prompt caching on the system text and on the tools."""

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
                role, blocks = "user", [{"type": "tool_result", "tool_use_id": message.tool_call_id,
                                         "content": message.content}]
            elif message.role == "assistant":
                role = "assistant"
                blocks = [{"type": "text", "text": message.content}] if message.content else []
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                           for c in message.tool_calls]
            else:
                role, blocks = "user", [{"type": "text", "text": message.content}]
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
        request = self._request(messages, tools)
        shown: list[str] = []  # the text handed to on_text: what a cancelled answer keeps
        try:
            with self.client.messages.stream(model=self.model, max_tokens=self.max_tokens, **request) as stream:
                for event in stream:
                    if cancel is not None and cancel():
                        return Reply("".join(shown), [], "end", {})
                    if event.type == "text":
                        shown.append(event.text)
                        if on_text is not None:
                            on_text(event.text)
                message = stream.get_final_message()
        except self._anthropic.AnthropicError as exc:
            raise self._error(exc) from exc
        text = "".join(b.text for b in message.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in message.content if b.type == "tool_use"]
        stop = {"tool_use": "tool_calls", "max_tokens": "length", "refusal": "refusal"}.get(message.stop_reason or "",
                                                                                              "end")
        return Reply(text, calls, stop, _claude_usage(message.usage))

    def _error(self, exc: Exception) -> ProviderError:
        anthropic = self._anthropic
        where = _address(self.base_url) if self.base_url else "Claude"
        if isinstance(exc, anthropic.AuthenticationError):
            return ProviderError("chiave di Claude non valida")
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderError(f"{where} non risponde: {exc}")
        if isinstance(exc, anthropic.APIStatusError):
            return ProviderError(f"{where} ha risposto {exc.status_code}: {exc.message}")
        return ProviderError(f"{where}: {exc}")

    def list_models(self) -> list[str]:
        try:
            return [m.id for m in self.client.models.list()]
        except self._anthropic.AnthropicError as exc:
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
            return name, url, OpenAICompatible(url, "", timeout=timeout).list_models()
        except ProviderError:
            return None

    if not servers:
        return []
    with ThreadPoolExecutor(max_workers=len(servers)) as pool:
        return [found for found in pool.map(ask, servers) if found is not None]


def make_provider(kind: str, model: str = "", base_url: str = "", api_key: str | None = None) -> Provider:
    """A provider of ``kind`` ("local", "claude" or "openai"); keys from the environment or the keyring."""
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
