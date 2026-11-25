"""
Production Gunicorn Configuration
"""
import multiprocessing
import os

# Server socket
bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:5000")
backlog = 2048

# Worker processes (default 1 for eventlet Socket.IO room/scheduler consistency)
workers = int(os.environ.get('GUNICORN_WORKERS', 1))
worker_class = "eventlet"  # Required for Flask-SocketIO
worker_connections = 1000
max_requests = 10000
max_requests_jitter = 1000
timeout = 120
keepalive = 5

# Logging
base_dir = os.path.dirname(os.path.abspath(__file__))
accesslog = os.environ.get('GUNICORN_ACCESS_LOG', os.path.join(base_dir, 'logs/gunicorn_access.log'))
errorlog = os.environ.get('GUNICORN_ERROR_LOG', os.path.join(base_dir, 'logs/gunicorn_error.log'))
loglevel = os.environ.get('GUNICORN_LOG_LEVEL', 'info')
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = 'phonos-main'

# Server mechanics
daemon = False
pidfile = None
umask = 0o077
user = None
group = None
tmp_upload_dir = None

# SSL (if needed)
keyfile = os.environ.get('SSL_KEYFILE')
certfile = os.environ.get('SSL_CERTFILE')

# Hooks
def on_starting(server):
    """Called just before the master process is initialized."""
    print("Starting Phonos main server...")


def when_ready(server):
    """Called just after the server is started."""
    print(f"Phonos main server ready. Workers: {workers}, Bind: {bind}")


def on_exit(server):
    """Called just before exiting."""
    print("Phonos main server shutting down...")


def worker_int(worker):
    """Called when a worker receives an INT or QUIT signal."""
    worker.log.info("Worker received INT or QUIT signal")


def pre_fork(server, worker):
    """Called just before a worker is forked."""
    pass


def post_fork(server, worker):
    """Called after a worker has been forked."""
    server.log.info(f"Worker spawned (pid: {worker.pid})")


def pre_exec(server):
    """Called before a new master process is forked."""
    server.log.info("Forked child, re-executing.")


def worker_exit(server, worker):
    """Called when a worker is about to exit."""
    server.log.info(f"Worker exited (pid: {worker.pid})")
