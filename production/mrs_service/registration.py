"""Node registration listener for the Management & Routing Service."""
from __future__ import annotations

import json
import sqlite3

import redis

from .key_cache import clear_public_key_cache
from .runtime import Config, NODE_REGISTRATION_CHANNEL, get_redis_connection, logger

# Import subdomain verification (with fallback for environments without it)
try:
    from phonos_app.helpers.subdomain_verify import (
        SubdomainVerificationError,
        verify_worker_subdomain,
        extract_worker_id,
    )
    SUBDOMAIN_VERIFY_AVAILABLE = True
except ImportError:
    SUBDOMAIN_VERIFY_AVAILABLE = False
    logger.warning(
        "subdomain_verify module not available — "
        "external worker subdomain checks are DISABLED"
    )


def _validate_external_worker(worker_id: str, subdomain: str) -> bool:
    """Validate that an external worker's subdomain is legitimate.

    For workers using a ``*.fyreflysystems.de`` subdomain, this verifies:
      1. Subdomain format matches ``<label>.fyreflysystems.de``
      2. The label matches the claimed worker_id
      3. DNS resolution confirms the subdomain exists

    Returns ``True`` when valid, ``False`` when the worker should be rejected.
    """
    if not SUBDOMAIN_VERIFY_AVAILABLE:
        logger.warning(
            "Subdomain verification unavailable — "
            "allowing registration for %s without DNS check",
            worker_id,
        )
        return True

    try:
        verify_worker_subdomain(worker_id, subdomain)
        logger.info(
            "External worker %s verified via subdomain %s",
            worker_id,
            subdomain,
        )
        return True
    except SubdomainVerificationError as exc:
        logger.error(
            "Subdomain verification failed for worker %s (subdomain=%s): %s",
            worker_id,
            subdomain,
            exc,
        )
        return False


def listen_for_node_registrations() -> None:
    """Continuously listen for and process new node registration requests."""
    logger.info("Node registration listener started.")
    r = get_redis_connection()
    pubsub = r.pubsub(ignore_subscribe_messages=True)

    try:
        pubsub.subscribe(NODE_REGISTRATION_CHANNEL)
        logger.info("Subscribed to node registration channel: %s", NODE_REGISTRATION_CHANNEL)
    except redis.exceptions.RedisError as exc:
        logger.critical("Could not subscribe to node registrations: %s", exc)
        return

    for message in pubsub.listen():
        try:
            payload = json.loads(message["data"])
            worker_id = payload.get("worker_id")
            public_key = payload.get("public_key")
            worker_type = payload.get("worker_type")

            # External workers include their subdomain in the registration payload
            subdomain = payload.get("subdomain", "")

            if not all([worker_id, public_key, worker_type]):
                logger.warning("Invalid registration payload received: %s", payload)
                continue

            logger.info(
                "Received registration request from worker ID: %s (type: %s, subdomain: %s)",
                worker_id,
                worker_type,
                subdomain or "(none)",
            )

            # ── External worker subdomain validation ────────────────────
            # If a subdomain is provided, it must be a valid *.fyreflysystems.de
            # subdomain that matches the worker_id. If no subdomain is provided,
            # the worker is an internal node and registration proceeds as before.
            if subdomain:
                if not _validate_external_worker(worker_id, subdomain):
                    logger.error(
                        "Rejecting registration for worker %s — "
                        "subdomain validation failed",
                        worker_id,
                    )
                    continue
            elif SUBDOMAIN_VERIFY_AVAILABLE:
                # Worker did not provide a subdomain. This is OK for internal
                # workers but log it for awareness.
                logger.info(
                    "Worker %s registered without a subdomain (internal node)",
                    worker_id,
                )

            try:
                conn = sqlite3.connect(Config.DATABASE_FILE)
                cursor = conn.cursor()
                cursor.execute(
                    "INSERT INTO customer_nodes (node_id, public_key, approved) VALUES (?, ?, 0)",
                    (worker_id, public_key),
                )
                conn.commit()
                conn.close()
                logger.info(
                    "Successfully registered node %s in the database. It is pending administrator approval.",
                    worker_id,
                )

                clear_public_key_cache(worker_id)

            except sqlite3.IntegrityError:
                logger.warning("Node %s is already registered. Ignoring duplicate registration request.", worker_id)
            except sqlite3.Error as exc:
                logger.error("Database error while registering node %s: %s", worker_id, exc)

        except json.JSONDecodeError:
            logger.error("Could not decode registration message: %s", message["data"])
        except Exception as exc:
            logger.error("Error processing registration message: %s", exc, exc_info=True)


__all__ = ["listen_for_node_registrations"]
