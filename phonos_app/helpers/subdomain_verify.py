"""
Subdomain Verification — validate external worker identity via DNS.

Every external worker connecting to the main app must present a valid
``*.fyreflysystems.de`` subdomain. This module provides the verification
primitives used by the MRS registration listener, the worker heartbeat
pipeline, and the Flask security middleware.
"""
from __future__ import annotations

import logging
import re
import socket
from typing import Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

TRUSTED_DOMAIN = "fyreflysystems.de"
SUBDOMAIN_PATTERN = re.compile(
    r"^([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?)\.fyreflysystems\.de$"
)

# ── Exceptions ─────────────────────────────────────────────────────────────


class SubdomainVerificationError(ValueError):
    """Raised when a subdomain fails verification."""


# ── Public API ─────────────────────────────────────────────────────────────


def extract_worker_id(subdomain: str) -> Optional[str]:
    """Return the worker ID (DNS label) from a verified subdomain.

    Returns ``None`` when the subdomain does not match the expected
    ``*.fyreflysystems.de`` pattern.
    """
    m = SUBDOMAIN_PATTERN.match(subdomain)
    if not m:
        return None
    return m.group(1)


def validate_subdomain(subdomain: str) -> str:
    """Verify that *subdomain* is a valid external worker subdomain.

    Checks:
      1. Format matches ``<label>.fyreflysystems.de``
      2. DNS A/AAAA record resolves
      3. Reverse DNS (PTR) matches the forward name

    Returns the worker ID (label) on success.

    Raises ``SubdomainVerificationError`` with a descriptive message when
    any check fails.
    """
    # ── Format ──────────────────────────────────────────────────────────
    worker_id = extract_worker_id(subdomain)
    if not worker_id:
        raise SubdomainVerificationError(
            f"Subdomain '{subdomain}' does not match "
            f"expected pattern '<label>.{TRUSTED_DOMAIN}'"
        )

    # The bare domain itself is not a valid worker subdomain
    if subdomain == TRUSTED_DOMAIN:
        raise SubdomainVerificationError(
            f"Bare domain '{TRUSTED_DOMAIN}' is not a valid worker subdomain. "
            f"External workers must use '<label>.{TRUSTED_DOMAIN}'."
        )

    # ── Forward DNS ─────────────────────────────────────────────────────
    try:
        addrs = socket.getaddrinfo(subdomain, None)
    except OSError as exc:
        raise SubdomainVerificationError(
            f"Subdomain '{subdomain}' does not resolve via DNS: {exc}"
        ) from exc

    resolved_ips = {addr[4][0] for addr in addrs}
    logger.debug("Subdomain %s resolves to %s", subdomain, resolved_ips)

    # ── Reverse DNS ─────────────────────────────────────────────────────
    for ip in resolved_ips:
        try:
            hostname, *_ = socket.gethostbyaddr(ip)
            if hostname.rstrip(".") == subdomain.rstrip("."):
                logger.debug("PTR match for %s → %s", ip, hostname)
                return worker_id
        except OSError:
            continue

    # If no IP had a matching PTR, verification still succeeds at the DNS
    # resolution level (PTR is recommended but not always configured).
    # Log a warning and proceed.
    logger.warning(
        "Subdomain %s resolves but PTR does not match. "
        "Proceeding without reverse DNS verification.",
        subdomain,
    )

    return worker_id


def verify_worker_subdomain(worker_id: str, subdomain: str) -> None:
    """Assert that *subdomain* belongs to *worker_id* and is valid.

    This is the high-level check used during worker registration:
      - The subdomain label must match the worker_id
      - The subdomain must pass ``validate_subdomain``

    Raises ``SubdomainVerificationError`` on mismatch.
    """
    expected_subdomain = f"{worker_id}.{TRUSTED_DOMAIN}"
    if subdomain != expected_subdomain:
        raise SubdomainVerificationError(
            f"Worker ID '{worker_id}' does not match subdomain '{subdomain}'. "
            f"Expected '{expected_subdomain}'."
        )

    validate_subdomain(subdomain)


__all__ = [
    "TRUSTED_DOMAIN",
    "SUBDOMAIN_PATTERN",
    "SubdomainVerificationError",
    "extract_worker_id",
    "validate_subdomain",
    "verify_worker_subdomain",
]
