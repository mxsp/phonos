"""Compatibility wrapper for configuration access.

Provides ``Config`` from ``production.config`` so legacy modules using
``from config import Config`` continue to function when the project is run as a
package.
"""
from production.config import Config  # re-export for legacy imports

__all__ = ["Config"]
