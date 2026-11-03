"""The assistant: a chat about the project with a local model (loopback only) or with Claude / OpenAI."""
from __future__ import annotations

from .providers import (CLAUDE_CHOICES, DEFAULT_CLAUDE, LOCAL_SERVERS, Claude, Message, OpenAICompatible, Provider,
                        ProviderError, Reply, ToolCall, ToolSpec, is_loopback, make_provider, probe_local)

__all__ = ["CLAUDE_CHOICES", "DEFAULT_CLAUDE", "LOCAL_SERVERS", "Claude", "Message", "OpenAICompatible", "Provider",
           "ProviderError", "Reply", "ToolCall", "ToolSpec", "is_loopback", "make_provider", "probe_local"]
