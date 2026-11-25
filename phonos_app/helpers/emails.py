"""Email helpers."""
from __future__ import annotations

import threading

from flask_mail import Message

from ..app import app, logger, mail


def _send_async_email(flask_app, msg):
    with flask_app.app_context():
        try:
            mail.send(msg)
        except Exception as exc:  # pragma: no cover - logging side effect
            logger.error("Error sending async email to %s: %s", msg.recipients, exc, exc_info=True)


def send_email(to, subject, body, async_=True):
    sender = app.config.get("MAIL_DEFAULT_SENDER") or app.config.get("MAIL_USERNAME")
    if not sender:
        logger.error("MAIL_DEFAULT_SENDER not configured. Cannot send email to %s.", to)
        return False
    # Wrap plain text in minimal HTML if not already HTML
    html_body = body if body.strip().startswith('<') else f'<p>{body}</p>'
    msg = Message(
        subject,
        recipients=[to],
        html=html_body,
        sender=sender,
    )
    if async_:
        thread = threading.Thread(target=_send_async_email, args=[app, msg])
        thread.start()
        return True
    try:
        mail.send(msg)
        return True
    except Exception as exc:  # pragma: no cover - logging side effect
        logger.error("Error sending sync email to %s: %s", to, exc, exc_info=True)
        return False
