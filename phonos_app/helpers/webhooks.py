"""
Webhook management and delivery system.

This module handles webhook creation, validation, delivery, and logging.
"""

import hmac
import hashlib
import json
import logging
import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from phonos_app.helpers.field_crypto import encrypt_field, decrypt_field

logger = logging.getLogger(__name__)

# Valid webhook events
VALID_EVENTS = ['job.completed', 'job.failed', 'job.started', 'job.progress']

# Webhook limits
MAX_WEBHOOKS_PER_USER = 10
MAX_RETRIES = 3
DEFAULT_TIMEOUT = 10


def validate_webhook_url(url: str) -> Tuple[bool, Optional[str]]:
    """
    Validate webhook URL.

    Args:
        url: The webhook URL to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not url:
        return False, "URL is required"

    if not isinstance(url, str):
        return False, "URL must be a string"

    # Parse URL
    try:
        parsed = urlparse(url)

        # Must have valid hostname
        if not parsed.netloc:
            return False, "Invalid URL: missing hostname"

        hostname = parsed.netloc.split(':')[0].lower()
        is_local = (
            hostname in ['localhost', '127.0.0.1', '::1'] or
            hostname.startswith('192.168.') or
            hostname.startswith('10.') or
            hostname.startswith('172.')
        )

        # Enforce HTTPS for public URLs, but allow HTTP for local/internal
        if not is_local and not url.startswith('https://'):
            return False, "Webhook URL must use HTTPS for security"
            
        # We now allow localhost and internal IPs for development flexibility
        # The previous block restricting them has been removed.

    except Exception as e:
        return False, f"Invalid URL format: {str(e)}"

    return True, None


def validate_events(events: List[str]) -> Tuple[bool, Optional[str]]:
    """
    Validate webhook events.

    Args:
        events: List of event types to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not events:
        return False, "At least one event is required"

    if not isinstance(events, list):
        return False, "Events must be a list"

    if len(events) == 0:
        return False, "At least one event is required"

    # Check each event is valid
    invalid_events = [e for e in events if e not in VALID_EVENTS]
    if invalid_events:
        return False, f"Invalid events: {', '.join(invalid_events)}. Valid events: {', '.join(VALID_EVENTS)}"

    return True, None


def generate_webhook_secret() -> str:
    """Generate a secure random webhook secret."""
    import secrets
    return secrets.token_urlsafe(32)


def sign_payload(payload: dict, secret: str) -> str:
    """
    Generate HMAC signature for webhook payload.

    Args:
        payload: The webhook payload dict
        secret: The webhook secret key

    Returns:
        HMAC signature as hex string
    """
    payload_bytes = json.dumps(payload, sort_keys=True).encode('utf-8')
    signature = hmac.new(
        secret.encode('utf-8'),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()
    return signature


def create_webhook(db, user_id: int, url: str, events: List[str],
                  description: str = None, retry_enabled: bool = True,
                  max_retries: int = MAX_RETRIES, timeout: int = DEFAULT_TIMEOUT) -> Tuple[bool, Optional[Dict], Optional[str]]:
    """
    Create a new webhook.

    Args:
        db: Database connection
        user_id: User ID
        url: Webhook URL
        events: List of events to subscribe to
        description: Optional description
        retry_enabled: Whether to enable retries
        max_retries: Maximum retry attempts
        timeout: Request timeout in seconds

    Returns:
        Tuple of (success, webhook_data, error_message)
    """
    # Validate URL
    url_valid, url_error = validate_webhook_url(url)
    if not url_valid:
        return False, None, url_error

    # Validate events
    events_valid, events_error = validate_events(events)
    if not events_valid:
        return False, None, events_error

    # Check webhook limit
    count_result = db.execute(
        "SELECT COUNT(*) as count FROM api_webhooks WHERE user_id = %s",
        (user_id,)
    )
    webhook_count = count_result.fetchone()['count']

    if webhook_count >= MAX_WEBHOOKS_PER_USER:
        return False, None, f"Maximum {MAX_WEBHOOKS_PER_USER} webhooks per user"

    # Generate secret
    secret = generate_webhook_secret()

    # Create webhook
    try:
        result = db.execute("""
            INSERT INTO api_webhooks
            (user_id, url, events, secret_key, description, is_active,
             retry_enabled, max_retries, timeout_seconds, created_at)
            VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, CURRENT_TIMESTAMP)
            RETURNING id, created_at
        """, (
            user_id,
            url,
            json.dumps(events),
            encrypt_field(secret),  # Encrypted at rest; plaintext returned to caller below
            description,
            1 if retry_enabled else 0,
            max_retries,
            timeout
        ))

        row = result.fetchone()
        db.commit()

        logger.info(f"Created webhook {row['id']} for user {user_id}: {url}")

        webhook_data = {
            'id': row['id'],
            'url': url,
            'events': events,
            'description': description,
            'secret_key': secret,  # Return secret only on creation
            'is_active': True,
            'retry_enabled': retry_enabled,
            'max_retries': max_retries,
            'timeout_seconds': timeout,
            'created_at': row['created_at'].isoformat() if row['created_at'] else None
        }

        return True, webhook_data, None

    except Exception as e:
        logger.error(f"Error creating webhook: {e}", exc_info=True)
        db.rollback()
        return False, None, f"Failed to create webhook: {str(e)}"


def get_user_webhooks(db, user_id: int) -> List[Dict]:
    """
    Get all webhooks for a user.

    Args:
        db: Database connection
        user_id: User ID

    Returns:
        List of webhook dicts
    """
    try:
        result = db.execute("""
            SELECT id, url, events, description, is_active,
                   created_at, last_triggered_at, success_count, failure_count,
                   retry_enabled, max_retries, timeout_seconds
            FROM api_webhooks
            WHERE user_id = %s
            ORDER BY created_at DESC
        """, (user_id,))

        webhooks = []
        for row in result.fetchall():
            webhooks.append({
                'id': row['id'],
                'url': row['url'],
                'events': json.loads(row['events']) if row['events'] else [],
                'description': row['description'],
                'is_active': bool(row['is_active']),
                'created_at': row['created_at'].isoformat() if row['created_at'] else None,
                'last_triggered_at': row['last_triggered_at'].isoformat() if row['last_triggered_at'] else None,
                'retry_enabled': bool(row['retry_enabled']),
                'max_retries': row['max_retries'],
                'timeout_seconds': row['timeout_seconds'],
                'stats': {
                    'success_count': row['success_count'] or 0,
                    'failure_count': row['failure_count'] or 0
                }
            })

        return webhooks

    except Exception as e:
        logger.error(f"Error getting webhooks: {e}", exc_info=True)
        return []


def deliver_webhook(db, webhook_id: int, event_type: str, payload: dict, retry_count: int = 0) -> Tuple[bool, Optional[str]]:
    """
    Deliver a webhook payload.

    Args:
        db: Database connection
        webhook_id: Webhook ID
        event_type: Event type being delivered
        payload: Payload data
        retry_count: Current retry attempt

    Returns:
        Tuple of (success, error_message)
    """
    try:
        # Get webhook details
        result = db.execute("""
            SELECT url, secret_key, timeout_seconds, is_active
            FROM api_webhooks
            WHERE id = %s
        """, (webhook_id,))

        webhook = result.fetchone()
        if not webhook:
            return False, "Webhook not found"

        if not webhook['is_active']:
            return False, "Webhook is inactive"

        # Prepare payload
        webhook_payload = {
            'event': event_type,
            'timestamp': datetime.now().isoformat(),
            'data': payload,
            'webhook_id': webhook_id
        }

        # Sign payload (decrypt_field passes legacy plaintext rows through unchanged)
        signature = sign_payload(webhook_payload, decrypt_field(webhook['secret_key']))

        # Prepare headers
        headers = {
            'Content-Type': 'application/json',
            'User-Agent': 'PHONOS-Webhook/1.0',
            'X-Webhook-Event': event_type,
            'X-Webhook-Signature': f'sha256={signature}',
            'X-Webhook-ID': str(webhook_id),
            'X-Webhook-Retry': str(retry_count)
        }

        # Deliver webhook
        start_time = datetime.now()
        try:
            response = requests.post(
                webhook['url'],
                json=webhook_payload,
                headers=headers,
                timeout=webhook['timeout_seconds'] or DEFAULT_TIMEOUT
            )

            elapsed_ms = int((datetime.now() - start_time).total_seconds() * 1000)

            # Log delivery
            log_delivery(
                db, webhook_id, event_type, json.dumps(webhook_payload),
                response.status_code, response.text[:1000], elapsed_ms,
                None, retry_count
            )

            # Update webhook stats
            if response.status_code < 400:
                db.execute("""
                    UPDATE api_webhooks
                    SET success_count = success_count + 1,
                        last_triggered_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (webhook_id,))
                db.commit()

                logger.info(f"Webhook {webhook_id} delivered successfully: {response.status_code}")
                return True, None
            else:
                db.execute("""
                    UPDATE api_webhooks
                    SET failure_count = failure_count + 1,
                        last_triggered_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                """, (webhook_id,))
                db.commit()

                error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.warning(f"Webhook {webhook_id} failed: {error_msg}")
                return False, error_msg

        except requests.exceptions.Timeout:
            elapsed_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            error_msg = f"Request timeout after {webhook['timeout_seconds']}s"

            log_delivery(
                db, webhook_id, event_type, json.dumps(webhook_payload),
                None, None, elapsed_ms, error_msg, retry_count
            )

            db.execute("""
                UPDATE api_webhooks
                SET failure_count = failure_count + 1,
                    last_triggered_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (webhook_id,))
            db.commit()

            logger.warning(f"Webhook {webhook_id} timeout: {error_msg}")
            return False, error_msg

        except requests.exceptions.RequestException as e:
            elapsed_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            error_msg = f"Request error: {str(e)}"

            log_delivery(
                db, webhook_id, event_type, json.dumps(webhook_payload),
                None, None, elapsed_ms, error_msg, retry_count
            )

            db.execute("""
                UPDATE api_webhooks
                SET failure_count = failure_count + 1,
                    last_triggered_at = CURRENT_TIMESTAMP
                WHERE id = %s
            """, (webhook_id,))
            db.commit()

            logger.error(f"Webhook {webhook_id} request failed: {error_msg}")
            return False, error_msg

    except Exception as e:
        logger.error(f"Webhook delivery error: {e}", exc_info=True)
        return False, str(e)


def log_delivery(db, webhook_id: int, event_type: str, payload: str,
                response_status: Optional[int], response_body: Optional[str],
                response_time_ms: int, error_message: Optional[str], retry_count: int):
    """Log a webhook delivery attempt."""
    try:
        db.execute("""
            INSERT INTO webhook_deliveries
            (webhook_id, event_type, payload, response_status, response_body,
             response_time_ms, error_message, retry_count, delivered_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
        """, (
            webhook_id, event_type, payload, response_status,
            response_body[:1000] if response_body else None,
            response_time_ms, error_message, retry_count
        ))
        db.commit()
    except Exception as e:
        logger.error(f"Error logging webhook delivery: {e}", exc_info=True)


def get_webhook_deliveries(db, webhook_id: int, limit: int = 50) -> List[Dict]:
    """Get delivery history for a webhook."""
    try:
        result = db.execute("""
            SELECT id, event_type, response_status, response_time_ms,
                   error_message, retry_count, delivered_at
            FROM webhook_deliveries
            WHERE webhook_id = %s
            ORDER BY delivered_at DESC
            LIMIT %s
        """, (webhook_id, limit))

        deliveries = []
        for row in result.fetchall():
            deliveries.append({
                'id': row['id'],
                'event_type': row['event_type'],
                'status_code': row['response_status'],
                'response_time_ms': row['response_time_ms'],
                'error': row['error_message'],
                'retry_count': row['retry_count'],
                'delivered_at': row['delivered_at'].isoformat() if row['delivered_at'] else None,
                'success': row['response_status'] and row['response_status'] < 400
            })

        return deliveries

    except Exception as e:
        logger.error(f"Error getting webhook deliveries: {e}", exc_info=True)
        return []


def delete_webhook(db, user_id: int, webhook_id: int) -> Tuple[bool, Optional[str]]:
    """Delete a webhook."""
    try:
        result = db.execute("""
            DELETE FROM api_webhooks
            WHERE id = %s AND user_id = %s
            RETURNING id
        """, (webhook_id, user_id))

        if not result.fetchone():
            return False, "Webhook not found or unauthorized"

        db.commit()
        logger.info(f"Deleted webhook {webhook_id} for user {user_id}")
        return True, None

    except Exception as e:
        logger.error(f"Error deleting webhook: {e}", exc_info=True)
        db.rollback()
        return False, str(e)


def update_webhook(db, user_id: int, webhook_id: int, **kwargs) -> Tuple[bool, Optional[str]]:
    """Update webhook settings."""
    try:
        # Verify ownership
        result = db.execute("""
            SELECT id FROM api_webhooks
            WHERE id = %s AND user_id = %s
        """, (webhook_id, user_id))

        if not result.fetchone():
            return False, "Webhook not found or unauthorized"

        # Build update query
        updates = []
        params = []

        if 'url' in kwargs:
            url_valid, url_error = validate_webhook_url(kwargs['url'])
            if not url_valid:
                return False, url_error
            updates.append("url = %s")
            params.append(kwargs['url'])

        if 'events' in kwargs:
            events_valid, events_error = validate_events(kwargs['events'])
            if not events_valid:
                return False, events_error
            updates.append("events = %s")
            params.append(json.dumps(kwargs['events']))

        if 'description' in kwargs:
            updates.append("description = %s")
            params.append(kwargs['description'])

        if 'is_active' in kwargs:
            updates.append("is_active = %s")
            params.append(1 if kwargs['is_active'] else 0)

        if 'retry_enabled' in kwargs:
            updates.append("retry_enabled = %s")
            params.append(1 if kwargs['retry_enabled'] else 0)

        if 'max_retries' in kwargs:
            updates.append("max_retries = %s")
            params.append(kwargs['max_retries'])

        if 'timeout_seconds' in kwargs:
            updates.append("timeout_seconds = %s")
            params.append(kwargs['timeout_seconds'])

        if not updates:
            return False, "No fields to update"

        # Execute update
        query = f"UPDATE api_webhooks SET {', '.join(updates)} WHERE id = %s AND user_id = %s"
        params.extend([webhook_id, user_id])

        db.execute(query, tuple(params))
        db.commit()

        logger.info(f"Updated webhook {webhook_id} for user {user_id}")
        return True, None

    except Exception as e:
        logger.error(f"Error updating webhook: {e}", exc_info=True)
        db.rollback()
        return False, str(e)
