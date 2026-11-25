"""Phonos Copilot — an in-app assistant with a typed, context-aware tool catalog.

The package is deliberately self-contained: it uses the existing Flask app,
session authentication and database helpers, and it adds only its own tables
(``phonos_agent_*``).  Nothing in the core application imports from here, so
the assistant can be disabled by not registering :func:`register_agent_routes`.
"""

from __future__ import annotations

__all__ = ["register_agent_routes"]


def register_agent_routes(app):
    """Register the assistant blueprint and its page on ``app``."""
    from .router import agent_bp

    if "agent" not in app.blueprints:
        app.register_blueprint(agent_bp)
    return agent_bp
