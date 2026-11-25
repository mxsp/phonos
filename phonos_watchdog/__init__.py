"""phonos_watchdog — parallel self-healing supervisor for the Phonos stack.

Watches redis, postgres, the Flask app, MRS and the workers; restarts whatever
is down (via sudo systemctl) with flap protection and dependency ordering; runs
periodic maintenance tasks (e.g. code-review-graph); and publishes status to Redis.
"""

__version__ = "1.0.0"
