"""Utilities for resolving preferred hostnames with fallbacks."""
from __future__ import annotations

from collections.abc import Sequence
import logging
import socket
from typing import Optional


def resolve_host(
    preferred_host: str,
    fallback_hosts: Sequence[str],
    *,
    logger: Optional["logging.Logger"] = None,
    context: str = "host",
) -> str:
    """Return ``preferred_host`` when resolvable, otherwise use the first working fallback.

    Parameters
    ----------
    preferred_host:
        The hostname that should be used when it resolves successfully.
    fallback_hosts:
        A sequence of hostnames or IP addresses to try when the preferred host
        cannot be resolved.
    logger:
        Optional logger used to emit diagnostic messages describing the host
        selection. The argument is kept optional to allow usage in modules that
        configure logging lazily.
    context:
        A human-readable description for log entries to clarify how the host is
        used (e.g. "Socket.IO server").
    """

    candidates = (preferred_host, *fallback_hosts)
    for candidate in candidates:
        try:
            socket.getaddrinfo(candidate, None)
        except OSError:
            if logger is not None:
                logger.warning(
                    "Host '%s' is not reachable for %s. Trying next candidate.",
                    candidate,
                    context,
                )
            continue

        if logger is not None:
            if candidate == preferred_host:
                logger.info(
                    "Using preferred host '%s' for %s.",
                    candidate,
                    context,
                )
            else:
                logger.info(
                    "Using fallback host '%s' for %s.",
                    candidate,
                    context,
                )
        return candidate

    # ``fallback_hosts`` is expected to contain at least one entry, but guard in
    # case it does not to avoid ``IndexError``.
    default_host = fallback_hosts[-1] if fallback_hosts else preferred_host
    if logger is not None:
        logger.error(
            "No valid host candidates resolved for %s. Defaulting to '%s'.",
            context,
            default_host,
        )
    return default_host


__all__ = ["resolve_host"]
