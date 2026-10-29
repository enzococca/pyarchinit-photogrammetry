"""API keys of the assistant: from the environment, else from the system keyring (never written anywhere else)."""
from __future__ import annotations

import logging
import os

from .providers import ProviderError

log = logging.getLogger(__name__)

SERVICE = "pyarchinit-photogrammetry"
ENV = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


class KeyringUnavailable(ProviderError):
    """No keyring backend on this computer: the key cannot be kept."""


def _backend():
    """The keyring of the system, or None when the library or a usable backend is missing."""
    try:
        import keyring
        from keyring.backends import fail
    except Exception:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception as exc:  # a broken backend configuration
        log.debug("keyring: %s", exc)
        return None
    if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 1) <= 0:
        return None
    return backend


def keyring_available() -> bool:
    """True when a key can be kept in the keyring of the system."""
    return _backend() is not None


def _keyring_get(service: str, user: str) -> str | None:
    backend = _backend()
    if backend is None:
        return None
    try:
        return backend.get_password(service, user)
    except Exception as exc:
        log.warning("keyring: chiave non letta: %s", exc)
        return None


def _keyring_set(service: str, user: str, key: str) -> None:
    backend = _backend()
    if backend is None:
        raise KeyringUnavailable("nessun portachiavi disponibile su questo computer: la chiave non puo' essere "
                                 "conservata (usare la variabile d'ambiente)")
    try:
        backend.set_password(service, user, key)
    except Exception as exc:
        raise KeyringUnavailable(f"il portachiavi ha rifiutato la chiave: {exc}") from exc


def _keyring_delete(service: str, user: str) -> None:
    backend = _backend()
    if backend is None:
        return
    try:
        backend.delete_password(service, user)
    except Exception as exc:  # PasswordDeleteError when there is none
        log.debug("keyring: %s", exc)


def get_key(kind: str) -> str | None:
    """The key of ``kind`` ("claude" or "openai"): the environment first, then the keyring; None without both."""
    name = ENV.get(kind)
    if name and os.environ.get(name, "").strip():
        return os.environ[name].strip()
    return _keyring_get(SERVICE, kind) or None


def set_key(kind: str, key: str) -> None:
    """Keep ``key`` in the keyring; raises KeyringUnavailable when there is no backend."""
    if not keyring_available():
        raise KeyringUnavailable("nessun portachiavi disponibile su questo computer: la chiave non puo' essere "
                                 "conservata (usare la variabile d'ambiente)")
    _keyring_set(SERVICE, kind, key.strip())


def forget_key(kind: str) -> None:
    """Remove the key of ``kind`` from the keyring (the environment is not touched)."""
    _keyring_delete(SERVICE, kind)
