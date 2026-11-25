"""
Phonos Notebook Kernel Manager
Manages Docker containers running Jupyter kernels with ZMQ communication.
"""
import os
import json
import hmac
import shutil
import hashlib
import secrets
import asyncio
import logging
import queue
import threading
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, Callable, List
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Try to import docker, but allow graceful fallback
try:
    import docker
    from docker.errors import NotFound, APIError
    DOCKER_AVAILABLE = True
except ImportError:
    DOCKER_AVAILABLE = False
    logger.warning("Docker SDK not available - kernel management disabled")

# Try to import ZMQ
try:
    import zmq
    import zmq.asyncio
    ZMQ_AVAILABLE = True
except ImportError:
    ZMQ_AVAILABLE = False
    logger.warning("PyZMQ not available - kernel communication disabled")

# Eventlet monkey-patches queue.Queue to a green-thread-only queue.  The ZMQ
# reader threads are real OS threads, so inter-thread queues must be the
# standard library version to avoid lost messages / deadlocks.
try:
    import eventlet.patcher
    _real_queue = eventlet.patcher.original('queue')
except Exception:
    import queue as _real_queue


@dataclass
class KernelSession:
    """Represents an active kernel session"""
    session_id: str
    container_id: str
    user_id: int
    notebook_id: int
    status: str = 'starting'
    last_activity: datetime = field(default_factory=datetime.utcnow)
    created_at: datetime = field(default_factory=datetime.utcnow)
    execution_count: int = 0
    workspace_path: str = ''
    container_ip: str = ''
    connection_info: Dict[str, Any] = field(default_factory=dict)
    zmq_context: Any = None
    shell_socket: Any = None
    shell_sync_socket: Any = None
    shell_sync_context: Any = None
    stdin_sync_socket: Any = None
    stdin_sync_context: Any = None
    iopub_socket: Any = None
    error_message: str = ''
    project_files_count: int = 0
    project_path: str = ''
    runtime_level: str = 'python'
    execution_timeout: int = 300
    is_virtual: bool = False
    wolfram_cell_history: List[str] = field(default_factory=list)
    # Gateway capabilities (read once from /tmp/kernel_capabilities.json in the
    # container). Old images have no capabilities file -> empty dict.
    capabilities: Dict[str, Any] = field(default_factory=dict)
    capabilities_checked: bool = False
    # Thread-safe queues for shell/stdin messages. The reply reader thread owns
    # the ZMQ sockets, so execution/input handlers enqueue messages instead of
    # touching sockets directly (avoids ZMQ thread-safety violations).
    shell_send_queue: queue.Queue = field(default_factory=_real_queue.Queue)
    stdin_send_queue: queue.Queue = field(default_factory=_real_queue.Queue)
    # Replies/outputs produced by the real OS reader threads are pushed into
    # these queues; a SocketIO green thread drains them and emits to clients.
    reply_queue: queue.Queue = field(default_factory=_real_queue.Queue)
    output_queue: queue.Queue = field(default_factory=_real_queue.Queue)
    _reply_reader_started: bool = False


class RuntimeBackend(ABC):
    """Abstract base class for container runtime backends."""

    @abstractmethod
    def start_container(self, session_id, workspace_path, volumes, env_vars,
                        labels, mem_limit, cpu_quota, **kwargs):
        """Start a container and return dict with 'id' and 'name' keys."""

    @abstractmethod
    def stop_container(self, container_id):
        """Stop and remove a container."""

    @abstractmethod
    def get_container_status(self, container_id):
        """Return container status string (running, exited, etc.)."""

    @abstractmethod
    def exec_in_container(self, container_id, cmd):
        """Execute a command in the container. Returns (exit_code, output)."""


class LocalDockerBackend(RuntimeBackend):
    """Backend that uses the local Docker daemon."""

    def __init__(self, docker_client, network_name):
        self.client = docker_client
        self.network_name = network_name

    def start_container(self, session_id, workspace_path, volumes, env_vars,
                        labels, mem_limit, cpu_quota, image=None, **kwargs):
        image = image or 'phonos/notebook-kernel:latest'
        container_name = f"phonos-nb-{session_id[:12]}"
        cpu_period = 100000
        cpu_quota_val = int(cpu_quota * cpu_period)

        container = self.client.containers.run(
            image,
            name=container_name,
            detach=True,
            remove=False,
            network=self.network_name,
            volumes=volumes,
            mem_limit=mem_limit,
            cpu_period=cpu_period,
            cpu_quota=cpu_quota_val,
            environment=env_vars,
            labels=labels,
            security_opt=['no-new-privileges:true'],
            cap_drop=['ALL'],
            cap_add=['SETUID', 'SETGID'],
        )
        return {'id': container.id, 'name': container_name, 'container': container}

    def stop_container(self, container_id):
        try:
            container = self.client.containers.get(container_id)
            container.stop(timeout=5)
            container.remove(force=True)
        except Exception:
            pass

    def get_container_status(self, container_id):
        try:
            container = self.client.containers.get(container_id)
            return container.status
        except Exception:
            return 'not_found'

    def exec_in_container(self, container_id, cmd):
        try:
            container = self.client.containers.get(container_id)
            return container.exec_run(cmd)
        except Exception as e:
            return (1, str(e).encode())


class LocalPythonBackend(RuntimeBackend):
    """Zero-Docker stateful Python kernel running in an isolated local subprocess.

    Works out-of-the-box on Linux, Windows, macOS, and single-container Docker
    deployments when a Docker daemon or ``phonos/notebook-kernel`` image is not
    installed.
    """

    def __init__(self):
        self._procs: Dict[str, Any] = {}

    @staticmethod
    def _pick_free_port() -> int:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('127.0.0.1', 0))
            return int(s.getsockname()[1])

    def start_container(self, session_id, workspace_path, volumes, env_vars,
                        labels, mem_limit, cpu_quota, image=None, **kwargs):
        import subprocess
        import sys
        import urllib.request

        port = self._pick_free_port()
        token = secrets.token_urlsafe(32)
        cid = f"local-py-{session_id[:12]}"
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        sdk_dir = os.path.join(project_root, 'packages', 'sdk')

        proc_env = os.environ.copy()
        proc_env.update({k: str(v) for k, v in (env_vars or {}).items()})
        proc_env['NOTEBOOK_WORKSPACE'] = workspace_path
        py_paths = [project_root, os.path.join(project_root, 'production')]
        if os.path.isdir(sdk_dir):
            py_paths.append(sdk_dir)
        existing_pp = os.environ.get('PYTHONPATH', '')
        if existing_pp:
            py_paths.append(existing_pp)
        proc_env['PYTHONPATH'] = os.pathsep.join(py_paths)

        popen_kwargs: Dict[str, Any] = {
            'cwd': workspace_path,
            'env': proc_env,
            'stdout': subprocess.DEVNULL,
            'stderr': subprocess.DEVNULL,
        }
        if os.name == 'nt':
            popen_kwargs['creationflags'] = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)

        worker_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'local_kernel_worker.py')
        if getattr(sys, 'frozen', False):
            worker_cmd = [sys.executable, '--kernel-worker', str(port), token, workspace_path]
        else:
            worker_cmd = [sys.executable, '-u', worker_script, str(port), token, workspace_path]
        proc = subprocess.Popen(
            worker_cmd,
            **popen_kwargs,
        )
        service_url = f"http://127.0.0.1:{port}"
        for _ in range(40):
            if proc.poll() is not None:
                raise RuntimeError("Local Python kernel process exited prematurely.")
            try:
                urllib.request.urlopen(service_url + '/health', timeout=1).read()
                break
            except Exception:
                time.sleep(0.1)
        else:
            proc.kill()
            raise RuntimeError("Local Python kernel failed health check.")

        self._procs[cid] = proc
        return {
            'id': cid,
            'name': cid,
            'container': None,
            'virtual': True,
            'local_python': True,
            'service_url': service_url,
            'auth_token': token,
        }

    def stop_container(self, container_id):
        proc = self._procs.pop(container_id, None)
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def get_container_status(self, container_id):
        proc = self._procs.get(container_id)
        if proc is None:
            return 'not_found'
        return 'running' if proc.poll() is None else 'exited'

    def exec_in_container(self, container_id, cmd):
        return (1, b'Container exec not applicable for local kernel')


class WolframServiceBackend(RuntimeBackend):
    """Backend for Wolfram Language execution via the Wolfram service container.

    Instead of running a Docker container with a Jupyter kernel, this backend
    represents a "virtual" session that delegates code execution to the
    standalone Wolfram service container (via HTTP API).
    """

    def __init__(self, config, network_name):
        self.config = config or {}
        self.network_name = network_name
        # Default to localhost since Flask app runs on host (service exposed on host port 5100)
        self.service_url = self.config.get('service_url', 'http://localhost:5100')
        self.kernel = self.config.get('kernel', 'wolframscript')
        self.use_cloud = self.config.get('use_cloud', True)
        self.timeout = int(self.config.get('timeout', 60))

    def start_container(self, session_id, workspace_path, volumes, env_vars,
                        labels, mem_limit, cpu_quota, image=None, **kwargs):
        """Create a virtual session record. No actual container is started."""
        return {
            'id': f'wolfram-virtual-{session_id[:12]}',
            'name': f'phonos-wolfram-{session_id[:12]}',
            'container': None,
            'virtual': True,
            'service_url': self.service_url,
            'kernel': self.kernel,
            'use_cloud': self.use_cloud,
            'timeout': self.timeout,
        }

    def stop_container(self, container_id):
        """No-op for virtual sessions."""
        return True

    def get_container_status(self, container_id):
        """Report the virtual session as 'running'."""
        return 'running'

    def exec_in_container(self, container_id, cmd):
        """Not used for Wolfram - execution goes through the service HTTP API."""
        return (1, b'WolframServiceBackend does not support exec_in_container; use the service HTTP API')


class RemoteDockerBackend(RuntimeBackend):
    """Backend that connects to a remote Docker daemon via TLS."""

    def __init__(self, config, network_name):
        self.network_name = network_name
        self._client = None
        self._config = config or {}

    @property
    def client(self):
        if self._client is None:
            if not DOCKER_AVAILABLE:
                raise RuntimeError("Docker SDK not installed")
            host = self._config.get('host', 'localhost')
            port = self._config.get('port', 2376)
            tls_config = None
            if self._config.get('tls_cert'):
                tls_config = docker.tls.TLSConfig(
                    client_cert=(self._config['tls_cert'], self._config.get('tls_key', '')),
                    ca_cert=self._config.get('tls_ca'),
                    verify=True
                )
            self._client = docker.DockerClient(
                base_url=f"tcp://{host}:{port}",
                tls=tls_config
            )
        return self._client

    def start_container(self, session_id, workspace_path, volumes, env_vars,
                        labels, mem_limit, cpu_quota, image=None, **kwargs):
        image = image or 'phonos/notebook-kernel:latest'
        container_name = f"phonos-nb-{session_id[:12]}"
        cpu_period = 100000
        cpu_quota_val = int(cpu_quota * cpu_period)

        container = self.client.containers.run(
            image,
            name=container_name,
            detach=True,
            remove=False,
            network=self.network_name,
            volumes=volumes,
            mem_limit=mem_limit,
            cpu_period=cpu_period,
            cpu_quota=cpu_quota_val,
            environment=env_vars,
            labels=labels,
            security_opt=['no-new-privileges:true'],
            cap_drop=['ALL'],
            cap_add=['SETUID', 'SETGID'],
        )
        return {'id': container.id, 'name': container_name, 'container': container}

    def stop_container(self, container_id):
        try:
            container = self.client.containers.get(container_id)
            container.stop(timeout=5)
            container.remove(force=True)
        except Exception:
            pass

    def get_container_status(self, container_id):
        try:
            container = self.client.containers.get(container_id)
            return container.status
        except Exception:
            return 'not_found'

    def exec_in_container(self, container_id, cmd):
        try:
            container = self.client.containers.get(container_id)
            return container.exec_run(cmd)
        except Exception as e:
            return (1, str(e).encode())


# ---------------------------------------------------------------------------
# RemoteSSHBackend — runs kernel containers on a remote VM via SSH + WireGuard
# ---------------------------------------------------------------------------

class RemoteSSHBackend(RuntimeBackend):
    """
    Backend that manages kernel containers on a remote VM over SSH.

    Designed to work with CloudProvisioner + WireGuardTunnel:
      1. CloudProvisioner starts an AWS/GCP worker VM.
      2. WireGuard tunnel is established (e.g. 10.242.0.0/24).
      3. This backend SSHes into the worker's tunnel IP and runs Docker commands.

    File syncing (rsync over tunnel) is handled by CloudProvisioner before
    the kernel starts, so the remote ``/project`` mount is up to date.

    Placeholder — actual ``paramiko`` / ``ssh`` calls are stubbed.
    """

    def __init__(self, config: dict = None):
        self._config = config or {}
        # Connection details populated at start_container time:
        self._ssh_host: str = ""       # tunnel IP of the worker (e.g. 10.242.0.2)
        self._ssh_port: int = 22
        self._ssh_user: str = "root"
        self._ssh_key: str = ""        # path to SSH private key for the worker

    def start_container(self, session_id, workspace_path, volumes, env_vars,
                        labels, mem_limit, cpu_quota, image=None, **kwargs):
        image = image or 'phonos/notebook-kernel:latest'
        container_name = f"phonos-nb-{session_id[:12]}"

        # Worker IP comes from config or provisioner (set by caller)
        self._ssh_host = self._config.get("worker_tunnel_ip", "10.242.0.2")
        self._ssh_user = self._config.get("ssh_user", "root")
        self._ssh_key = self._config.get("ssh_key_path", "/etc/phonos/worker_ssh_key")

        volumes_cli = " ".join(f"-v {src}:{dst}" for src, dst in volumes.items())
        # Actually mounts must exist on the remote host — they were rsynced earlier

        logger.info("RemoteSSH: would run Docker on %s@%s (image=%s, name=%s)",
                     self._ssh_user, self._ssh_host, image, container_name)

        # TODO: actual SSH call →
        #   ssh = paramiko.SSHClient()
        #   ssh.connect(self._ssh_host, username=self._ssh_user, key_filename=self._ssh_key)
        #   ssh.exec_command(
        #       f"docker run -d --name {container_name} "
        #       f"--network host {volumes_cli} "
        #       f"-e JUPYTER_TOKEN=… {image}"
        #   )
        #   result = ssh.exec_command(f"docker inspect {container_name} --format '{{.Id}}'")

        return {"id": f"remote-{container_name}", "name": container_name, "container": None}

    def stop_container(self, container_id):
        logger.info("RemoteSSH stop: would stop %s on %s", container_id, self._ssh_host)
        # ssh.exec_command(f"docker stop {container_id} && docker rm {container_id}")

    def get_container_status(self, container_id):
        logger.info("RemoteSSH status: would check %s", container_id)
        return "running"  # placeholder

    def exec_in_container(self, container_id, cmd):
        logger.info("RemoteSSH exec: would run in %s: %s", container_id, cmd)
        return (0, b"")  # placeholder


# ---------------------------------------------------------------------------

class KernelManager:
    """
    Manages Docker containers running Jupyter kernels.

    This class handles:
    - Starting/stopping kernel containers
    - ZMQ socket connections for code execution
    - Session lifecycle management
    - Cleanup of idle/expired sessions
    - Runtime level routing to different backends
    """

    KERNEL_IMAGE = 'phonos/notebook-kernel:latest'
    KERNEL_IMAGE_FALLBACKS = (
        'phonos/notebook-kernel:latest',
        'phonos-notebook-kernel:latest',
    )
    CONTAINER_PREFIX = 'phonos-nb-'
    NETWORK_NAME = 'phonos-notebooks'
    MAX_IDLE_TIME = timedelta(minutes=30)
    MAX_SESSION_TIME = timedelta(hours=8)
    CLEANUP_RETENTION = timedelta(hours=24)

    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        self.sessions: Dict[str, KernelSession] = {}
        self._docker_client = None
        self._cleanup_task = None
        self._output_listeners: Dict[str, Callable] = {}
        self._reply_callbacks: Dict[str, Callable] = {}
        self._lock = threading.Lock()
        self._backends: Dict[str, RuntimeBackend] = {}

    @staticmethod
    def _is_image_not_found_error(error: Exception) -> bool:
        """Return True when container start failure indicates a missing image tag."""
        error_text = str(error).lower()
        indicators = (
            'pull access denied',
            'manifest unknown',
            'no such image',
            'image does not exist',
            '404 client error',
            'not found',
        )
        return any(indicator in error_text for indicator in indicators)

    def _resolve_kernel_image_candidates(self) -> list[str]:
        """Build an ordered list of kernel image tags to try."""
        configured = (self.config.get('NOTEBOOK_KERNEL_IMAGE') or '').strip()
        candidates = []
        for image in (configured, self.KERNEL_IMAGE, *self.KERNEL_IMAGE_FALLBACKS):
            if image and image not in candidates:
                candidates.append(image)
        return candidates

    @property
    def docker_client(self):
        """Lazy initialization of Docker client"""
        if not DOCKER_AVAILABLE:
            raise RuntimeError("Docker SDK is not installed")

        if self._docker_client is None:
            try:
                self._docker_client = docker.from_env()
                # Test connection
                self._docker_client.ping()
                logger.info("Docker client connected")
            except Exception as e:
                logger.error(f"Failed to connect to Docker: {e}")
                raise RuntimeError(f"Docker connection failed: {e}")

        return self._docker_client

    def _resolve_wolfram_config(self) -> Dict[str, Any]:
        """Resolve the effective Wolfram service configuration.

        Precedence:
          1. runtime_levels table entry named 'wolfram' (encrypted config)
          2. WOLFRAM_SERVICE_URL / WOLFRAM_KERNEL environment variables
          3. Built-in defaults pointing at localhost:5100
        """
        import os as _os
        defaults = {
            'service_url': _os.environ.get('WOLFRAM_SERVICE_URL', 'http://localhost:5100'),
            'kernel': _os.environ.get('WOLFRAM_KERNEL', 'wolframscript'),
            'use_cloud': True,
            'timeout': 60,
        }
        try:
            from .runtime_config import get_runtime_config
            rc = get_runtime_config()
            level = rc.get_level('wolfram')
            if level and level.get('config'):
                cfg = level['config']
                for key in ('service_url', 'kernel', 'use_cloud', 'timeout'):
                    if key in cfg:
                        defaults[key] = cfg[key]
        except Exception as e:
            logger.warning(f"Could not load wolfram runtime config: {e}")
            try:
                from .database import get_db
                get_db().rollback()
            except Exception:
                pass
        return defaults

    def _docker_kernel_available(self) -> bool:
        """Return True only when Docker daemon AND a notebook kernel image exist locally."""
        mode = (os.environ.get('NOTEBOOK_BACKEND') or self.config.get('NOTEBOOK_BACKEND') or 'auto').strip().lower()
        if mode in ('local', 'subprocess', 'native'):
            return False
        if not DOCKER_AVAILABLE:
            return False
        try:
            client = self.docker_client
            for img in self._resolve_kernel_image_candidates():
                try:
                    client.images.get(img)
                    return True
                except Exception:
                    continue
            return mode == 'docker'
        except Exception:
            return False

    def _get_backend(self, runtime_level: str = 'python') -> RuntimeBackend:
        """Resolve a runtime level name to a backend instance."""
        # Language runtimes (python, quantum, hybrid) use Docker if available & built,
        # otherwise seamlessly use the zero-Docker LocalPythonBackend!
        if runtime_level in ('basic', 'python', 'quantum', 'hybrid'):
            if self._docker_kernel_available():
                if runtime_level not in self._backends:
                    self._backends[runtime_level] = LocalDockerBackend(self.docker_client, self.NETWORK_NAME)
                return self._backends[runtime_level]
            if 'local_python' not in self._backends:
                self._backends['local_python'] = LocalPythonBackend()
            return self._backends['local_python']

        if runtime_level in self._backends:
            return self._backends[runtime_level]

        # Wolfram uses the Wolfram service container via HTTP API.
        # Prefer the database runtime config so admins can point to a
        # different Wolfram service URL / kernel without code changes.
        if runtime_level == 'wolfram':
            config = self._resolve_wolfram_config()
            backend = WolframServiceBackend(config, self.NETWORK_NAME)
            self._backends[runtime_level] = backend
            return backend

        # Look up runtime level from database
        try:
            from .runtime_config import get_runtime_config
            rc = get_runtime_config()
            level = rc.get_level(runtime_level)
            if not level:
                logger.warning(f"Runtime level '{runtime_level}' not found, falling back to basic")
                return self._get_backend('basic')

            bt = level['backend_type']
            config = level.get('config') or {}

            if bt == 'local_docker':
                backend = LocalDockerBackend(self.docker_client, self.NETWORK_NAME)
            elif bt == 'remote_docker':
                backend = RemoteDockerBackend(config, self.NETWORK_NAME)
            elif bt == 'remote_ssh':
                backend = RemoteSSHBackend(config)
            elif bt == 'wolfram_service':
                backend = WolframServiceBackend(config, self.NETWORK_NAME)
            else:
                logger.warning(f"Unsupported backend_type '{bt}', falling back to local_docker")
                backend = LocalDockerBackend(self.docker_client, self.NETWORK_NAME)

            self._backends[runtime_level] = backend
            return backend
        except Exception as e:
            logger.error(f"Error resolving runtime level '{runtime_level}': {e}")
            try:
                from .database import get_db
                get_db().rollback()
            except Exception:
                pass
            return self._get_backend('basic')

    def _get_workspace_path(self, user_id: int, notebook_id: int) -> str:
        """Get the persistent workspace path for a notebook"""
        base_path = self.config.get('NOTEBOOK_WORKSPACE_BASE', '/data/notebooks')
        # Use persistent path based on notebook_id
        return os.path.join(base_path, str(notebook_id))

    def _get_project_path(self, project_id: int, team_id: Optional[int] = None, creator_id: Optional[int] = None) -> Optional[str]:
        """Get the project files path using the proper path resolution system"""
        try:
            from .paths import get_project_storage_roots
            # Get all candidate paths including user-personal paths
            candidates = get_project_storage_roots(project_id, team_id, creator_id)
            for path in candidates:
                if os.path.exists(path):
                    logger.info(f"Found project path: {path}")
                    return path
        except ImportError:
            logger.warning("Could not import paths module for project path resolution")

        # Configured job-output roots (primary + fallback), used for direct lookups.
        base_paths = [
            p for p in (
                self.config.get('JOB_OUTPUT_DIR'),
                self.config.get('JOB_OUTPUT_DIR_FALLBACK'),
            ) if p
        ]

        # Fallback: check user-personal paths directly if we have creator_id
        if creator_id:
            for base in base_paths:
                user_path = os.path.join(base, f'user-{creator_id}-personal', 'projects', str(project_id))
                if os.path.exists(user_path):
                    logger.info(f"Found project path via fallback: {user_path}")
                    return user_path
                # Also check for job outputs directly (files from completed jobs)
                user_jobs_path = os.path.join(base, f'user-{creator_id}-personal', 'jobs')
                if os.path.exists(user_jobs_path):
                    logger.info(f"Found user jobs path: {user_jobs_path}")

        # Check team paths if team_id provided
        if team_id:
            for base in base_paths:
                team_path = os.path.join(base, f'team-{team_id}', 'projects', str(project_id))
                if os.path.exists(team_path):
                    logger.info(f"Found project path via team fallback: {team_path}")
                    return team_path

        # Legacy path structure
        base_path = self.config.get('UPLOAD_FOLDER', '/data/uploads')
        legacy_path = os.path.join(base_path, 'projects', str(project_id))
        if os.path.exists(legacy_path):
            return legacy_path

        return None

    async def start_kernel(
        self,
        user_id: int,
        notebook_id: int,
        project_id: Optional[int] = None,
        team_id: Optional[int] = None,
        has_write_access: bool = False,
        runtime_level: str = 'python'
    ) -> KernelSession:
        """
        Start a new kernel container for a user.

        Args:
            user_id: The user ID
            notebook_id: The notebook ID
            project_id: Optional project ID for file access
            team_id: Optional team/organization ID for path resolution
            has_write_access: Whether user has write access to project

        Returns:
            KernelSession object with connection info
        """
        session_id = secrets.token_urlsafe(32)
        container_name = f"{self.CONTAINER_PREFIX}{session_id[:12]}"

        # Create persistent workspace directory
        workspace_path = self._get_workspace_path(user_id, notebook_id)
        os.makedirs(workspace_path, exist_ok=True)
        os.chmod(workspace_path, 0o755)

        logger.info(f"Starting kernel session {session_id} for user {user_id}, workspace: {workspace_path}")

        # Prepare volume mounts
        volumes = {
            workspace_path: {'bind': '/workspace', 'mode': 'rw'}
        }

        # If project_id provided, get project info from database
        project_name = None
        project_creator_id = None
        if project_id:
            try:
                from .database import get_db
                db = get_db()
                project_row = db.execute(
                    'SELECT team_id, name, creator_id FROM projects WHERE id = %s',
                    (project_id,)
                ).fetchone()
                if project_row:
                    if not team_id:
                        team_id = project_row.get('team_id')
                    project_name = project_row.get('name')
                    project_creator_id = project_row.get('creator_id')
            except Exception as e:
                logger.warning(f"Could not fetch project info: {e}")

        # Mount project files if available (Direct Mount, No Copy)
        project_path = None
        if project_id:
            project_path = self._get_project_path(project_id, team_id, project_creator_id)
            if project_path and os.path.exists(project_path):
                # Mount read-write if user has write access, otherwise read-only
                mode = 'rw' if has_write_access else 'ro'
                volumes[project_path] = {'bind': '/project', 'mode': mode}
                logger.info(f"Mounting project {project_id} at /project (mode={mode})")

        # Mount Phonos SDK (Fix for ModuleNotFoundError)
        # Mount the source directly so changes are reflected and it's always available
        try:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(os.path.dirname(current_dir))
            sdk_source = os.path.join(project_root, 'production', 'docker', 'notebook-kernel', 'phonos')
            
            if os.path.exists(sdk_source):
                volumes[sdk_source] = {
                    'bind': '/opt/phonos_sdk/phonos',
                    'mode': 'ro'
                }
                logger.info(f"Mounting Phonos SDK from {sdk_source}")
        except Exception as e:
            logger.warning(f"Could not determine SDK path: {e}")

        # Resolve runtime level for resource limits
        # 'python' is a language runtime that uses default basic resources
        # 'wolfram' uses WolframServiceBackend (virtual sessions, no container)
        runtime_mem = self.config.get('NOTEBOOK_MEM_LIMIT', '2g')
        runtime_cpu = self.config.get('NOTEBOOK_CPU_QUOTA', 1.0)
        if runtime_level not in ('basic', 'python'):
            try:
                from .runtime_config import get_runtime_config
                rc = get_runtime_config()
                level_info = rc.get_level(runtime_level)
                if level_info:
                    runtime_mem = level_info.get('mem_limit', runtime_mem)
                    runtime_cpu = level_info.get('cpu_quota', runtime_cpu)
            except Exception as e:
                logger.warning(f"Could not resolve runtime limits for '{runtime_level}', using defaults: {e}")
                try:
                    from .database import get_db
                    get_db().rollback()
                except Exception:
                    pass

        try:
            # Get the runtime backend first (may be LocalPythonBackend or WolframServiceBackend which need no Docker)
            backend = self._get_backend(runtime_level)
            if isinstance(backend, LocalDockerBackend):
                await self._ensure_network()

            # Build environment variables.
            # The hybrid (Python quantum + Wolfram) runtime reuses the
            # container's 'quantum' kernelspec — Wolfram cells never touch the
            # container (they are routed to the Wolfram service by cell type).
            kernel_language = 'quantum' if runtime_level == 'hybrid' else runtime_level
            env_vars = {
                'NOTEBOOK_SESSION_ID': session_id,
                'NOTEBOOK_USER_ID': str(user_id),
                'NOTEBOOK_ID': str(notebook_id),
                'NOTEBOOK_LANGUAGE': kernel_language,
                'PHONOS_API_URL': self.config.get('PHONOS_API_URL', 'http://172.18.0.1:5000'),
                'PYTHONPATH': '/opt/phonos_sdk',  # Add SDK to path
            }

            # Add project info if available
            if project_id:
                env_vars['NOTEBOOK_PROJECT_ID'] = str(project_id)
                if project_name:
                    env_vars['NOTEBOOK_PROJECT_NAME'] = project_name
                if has_write_access:
                    env_vars['NOTEBOOK_PROJECT_WRITABLE'] = '1'

            # Pass quantum credentials + execution settings for quantum-capable
            # runtimes. resolve_credentials honours the user's Quantum Settings
            # (credential_source personal/platform) and returns a
            # (api_key, source, instance_crn, preferred_backend) tuple.
            if runtime_level in ('quantum', 'hybrid'):
                api_key = instance = default_backend = None
                execution_mode = 'simulator'
                try:
                    from .quantum_user_creds import resolve_credentials, get_settings
                    api_key, _cred_source, instance, default_backend = resolve_credentials(user_id)
                    settings = get_settings(user_id)
                    execution_mode = settings.get('execution_mode', 'simulator')
                    if not default_backend or default_backend == 'automatic':
                        default_backend = settings.get('preferred_backend') or default_backend
                except Exception as e:
                    logger.warning(f"Could not resolve quantum credentials for user {user_id}: {e}")
                if not api_key:
                    api_key = os.environ.get('IBM_QUANTUM_API_KEY')
                if not instance:
                    instance = os.environ.get('IBM_QUANTUM_INSTANCE')
                if not default_backend:
                    default_backend = os.environ.get('IBM_QUANTUM_DEFAULT_BACKEND')
                if api_key:
                    env_vars['IBM_QUANTUM_API_KEY'] = api_key
                if instance:
                    env_vars['IBM_QUANTUM_INSTANCE'] = instance
                if default_backend and default_backend != 'automatic':
                    env_vars['IBM_QUANTUM_DEFAULT_BACKEND'] = default_backend
                # Non-secret execution preferences for the in-kernel
                # ``phonos.quantum`` helper (simulator vs hardware).
                env_vars['PHONOS_QUANTUM_MODE'] = execution_mode
                env_vars['PHONOS_QUANTUM_BACKEND'] = default_backend or 'automatic'

            # Build labels
            labels = {
                'phonos.notebook.session': session_id,
                'phonos.notebook.user': str(user_id),
                'phonos.notebook.id': str(notebook_id),
            }
            if project_id:
                labels['phonos.notebook.project'] = str(project_id)

            # Start container via runtime backend
            image_candidates = self._resolve_kernel_image_candidates()
            result = None
            last_start_error = None
            for idx, image_name in enumerate(image_candidates):
                try:
                    result = backend.start_container(
                        session_id=session_id,
                        workspace_path=workspace_path,
                        volumes=volumes,
                        env_vars=env_vars,
                        labels=labels,
                        mem_limit=runtime_mem,
                        cpu_quota=runtime_cpu,
                        image=image_name,
                    )
                    if idx > 0:
                        logger.info(f"Kernel startup succeeded with fallback image tag '{image_name}'")
                    break
                except Exception as start_error:
                    last_start_error = start_error
                    has_fallback = idx < len(image_candidates) - 1
                    if has_fallback and self._is_image_not_found_error(start_error):
                        logger.warning(
                            f"Kernel image '{image_name}' unavailable; trying '{image_candidates[idx + 1]}'"
                        )
                        continue
                    raise

            if result is None:
                raise last_start_error or RuntimeError("Failed to start kernel container")
            container = result['container']

            logger.info(f"Container {result['id'][:12]} started via {runtime_level} backend")

            # Create session object. Quantum-capable runtimes get a long cell
            # timeout: an IBM hardware job routinely waits in queue for well
            # over the default 300s, and the watchdog must not interrupt it.
            execution_timeout = 3600 if runtime_level in ('quantum', 'hybrid') else 300
            session = KernelSession(
                session_id=session_id,
                container_id=result['id'],
                user_id=user_id,
                notebook_id=notebook_id,
                workspace_path=workspace_path,
                status='starting',
                project_files_count=0,
                project_path=project_path or '',
                runtime_level=runtime_level,
                execution_timeout=execution_timeout
            )

            # Virtual sessions (LocalPythonBackend or Wolfram service) don't need Docker ZMQ
            if result.get('virtual'):
                service_url = result.get('service_url', 'http://localhost:5100')
                logger.info(f"Virtual session for {runtime_level} using service {service_url}")
                try:
                    import urllib.request
                    urllib.request.urlopen(service_url + '/health', timeout=5).read()
                except Exception as health_err:
                    raise RuntimeError(
                        f"Kernel service not reachable at {service_url}: {health_err}"
                    )
                if result.get('local_python'):
                    session.connection_info = {
                        'local_python': True,
                        'service_url': service_url,
                        'auth_token': result.get('auth_token', ''),
                    }
                session.status = 'idle'
                session.is_virtual = True
                with self._lock:
                    self.sessions[session_id] = session
                return session

            # Wait for kernel to be ready and connect
            await self._connect_to_kernel(session, container)

            with self._lock:
                self.sessions[session_id] = session

            logger.info(f"Kernel session {session_id} ready, stored in manager {id(self)}, sessions count: {len(self.sessions)}")
            return session

        except Exception as e:
            logger.error(f"Failed to start kernel: {e}")
            # Cleanup on failure
            try:
                backend.stop_container(container_name)
            except:
                pass
            raise RuntimeError(f"Failed to start kernel: {e}")

    async def _ensure_network(self):
        """Ensure the Docker network exists"""
        try:
            self.docker_client.networks.get(self.NETWORK_NAME)
        except NotFound:
            logger.info(f"Creating network {self.NETWORK_NAME}")
            self.docker_client.networks.create(
                self.NETWORK_NAME,
                driver='bridge',
                ipam=docker.types.IPAMConfig(
                    pool_configs=[
                        docker.types.IPAMPool(subnet='172.18.0.0/16')
                    ]
                )
            )

    async def _connect_to_kernel(self, session: KernelSession, container):
        """Connect to the kernel's ZMQ sockets"""
        max_wait = 180  # seconds - cold image pulls can be slow
        poll_interval = 0.5

        logger.info(f"Waiting for kernel {session.session_id} to be ready...")

        for i in range(int(max_wait / poll_interval)):
            try:
                # Check if ready file exists
                exit_code, _ = container.exec_run('test -f /tmp/kernel_ready')
                if exit_code == 0:
                    # Read connection info
                    exit_code, output = container.exec_run('cat /tmp/kernel_connection.json')
                    if exit_code == 0:
                        conn_info = json.loads(output.decode())
                        session.connection_info = conn_info
                        break
            except Exception as e:
                logger.debug(f"Waiting for kernel... ({i})")

            await asyncio.sleep(poll_interval)
        else:
            # Timeout - get container logs for debugging
            logs = container.logs(tail=50).decode()
            logger.error(f"Kernel startup logs:\n{logs}")
            raise RuntimeError("Kernel failed to start within timeout")

        # Get container IP
        container.reload()
        networks = container.attrs.get('NetworkSettings', {}).get('Networks', {})
        if self.NETWORK_NAME in networks:
            session.container_ip = networks[self.NETWORK_NAME].get('IPAddress', '')

        if not session.container_ip:
            raise RuntimeError("Could not get container IP address")

        logger.info(f"Container IP: {session.container_ip}")

        # Setup ZMQ connections if available
        if ZMQ_AVAILABLE:
            try:
                session.zmq_context = zmq.asyncio.Context()

                # The reply reader thread owns the synchronous shell/stdin
                # DEALER sockets used for execute_request / execute_reply /
                # input_request. Do NOT create an additional async shell
                # socket here: two DEALER sockets with the same identity
                # connected to the kernel's ROUTER can cause execute_reply
                # messages to be routed to the unread socket, leaving cells
                # stuck in the "running" state.

                # IOPub socket for output (SUB)
                session.iopub_socket = session.zmq_context.socket(zmq.SUB)
                session.iopub_socket.connect(
                    f"tcp://{session.container_ip}:{conn_info['iopub_port']}"
                )
                session.iopub_socket.setsockopt(zmq.SUBSCRIBE, b'')

                logger.info("ZMQ sockets connected")
            except Exception as e:
                logger.error(f"Failed to connect ZMQ sockets: {e}")
                # Continue without ZMQ - can still use HTTP fallback

        session.status = 'idle'

    async def execute_code(
        self,
        session_id: str,
        code: str,
        cell_id: str
    ) -> str:
        """
        Execute code in a kernel session.

        Args:
            session_id: The session ID
            code: The code to execute
            cell_id: The cell ID for tracking

        Returns:
            Message ID for tracking outputs
        """
        # Use sync version internally
        return self.execute_code_sync(session_id, code, cell_id)

    def execute_code_sync(
        self,
        session_id: str,
        code: str,
        cell_id: str
    ) -> str:
        """
        Synchronous version of execute_code for eventlet compatibility.

        Uses a persistent DEALER shell socket bound to the session so the
        kernel can route execute_reply messages back to the same peer. This
        avoids the race where the temporary sender socket was closed before
        the reply arrived, which caused cells (especially ones that produce
        no output) to appear stuck in the "running" state.
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError("Session not found")

        if session.status == 'dead':
            raise RuntimeError("Kernel is dead")

        if not ZMQ_AVAILABLE:
            raise RuntimeError("ZMQ not available for code execution")

        if not session.container_ip or not session.connection_info:
            raise RuntimeError("No connection info for kernel")

        session.execution_count += 1
        session.status = 'busy'
        session.last_activity = datetime.utcnow()

        # Create Jupyter protocol execute_request message
        msg_id = secrets.token_hex(16)

        header = {
            'msg_id': msg_id,
            'msg_type': 'execute_request',
            'username': '',
            'session': session.session_id,
            'date': datetime.utcnow().isoformat(),
            'version': '5.3',
        }

        parent_header = {}
        metadata = {'cellId': cell_id}
        content = {
            'code': code,
            'silent': False,
            'store_history': True,
            'user_expressions': {},
            'allow_stdin': True,
            'stop_on_error': True,
        }

        # Serialize message parts for signing
        header_bytes = json.dumps(header).encode()
        parent_bytes = json.dumps(parent_header).encode()
        metadata_bytes = json.dumps(metadata).encode()
        content_bytes = json.dumps(content).encode()

        # Compute HMAC signature (required by Jupyter protocol)
        key = session.connection_info.get('key', '')
        if isinstance(key, str):
            key = key.encode()

        h = hmac.new(key, digestmod=hashlib.sha256)
        h.update(header_bytes)
        h.update(parent_bytes)
        h.update(metadata_bytes)
        h.update(content_bytes)
        signature = h.hexdigest()

        # Build message parts with proper signature
        msg_parts = [
            b'<IDS|MSG>',
            signature.encode(),
            header_bytes,
            parent_bytes,
            metadata_bytes,
            content_bytes,
        ]

        # Ensure a reply reader is running so the queued message is actually
        # sent and replies are read on the reader thread's sync socket.
        if not session._reply_reader_started:
            self.start_reply_reader(session_id)

        # Enqueue the request for the reply reader thread. The reader thread is
        # the only code that touches the sync shell ZMQ socket, which avoids
        # cross-thread ZMQ socket use and ensures replies are read on the same
        # socket they were sent from.
        try:
            session.shell_send_queue.put(msg_parts, timeout=5)
            logger.info(f"Execute request queued: {msg_id} for {session.container_ip}:{session.connection_info['shell_port']}")
        except _real_queue.Full as e:
            session.status = 'idle'
            logger.error(f"Failed to queue execute request: {e}")
            raise RuntimeError(f"Failed to queue execute request: {e}")

        return msg_id

    # -- Gateway capability detection -------------------------------------

    def _get_capabilities(self, session: KernelSession) -> Dict[str, Any]:
        """Read the kernel gateway's capabilities file (once per session).

        New gateway images write /tmp/kernel_capabilities.json declaring the
        interrupt/restart signals they support. Old images have no file, so
        callers fall back to exec-based interrupt and full stop/start restart.
        """
        if session.capabilities_checked:
            return session.capabilities
        session.capabilities_checked = True
        try:
            container = self.docker_client.containers.get(session.container_id)
            exit_code, output = container.exec_run('cat /tmp/kernel_capabilities.json')
            if exit_code == 0:
                session.capabilities = json.loads(output.decode())
                logger.info(f"Session {session.session_id[:8]} gateway capabilities: {session.capabilities}")
        except Exception as e:
            logger.debug(f"No gateway capabilities for {session.session_id[:8]}: {e}")
        return session.capabilities

    def _abort_wolfram_evaluations(self, session_id: str) -> bool:
        """Ask the Wolfram service to kill any in-flight evaluations for a session.

        Used for 'wolfram' virtual sessions and the Wolfram half of 'hybrid'
        sessions. Returns True when the service acknowledged the abort.
        """
        import urllib.request as _urlrequest
        import urllib.error as _urlerror
        config = self._resolve_wolfram_config()
        url = f"{config['service_url'].rstrip('/')}/session/abort"
        req = _urlrequest.Request(
            url,
            data=json.dumps({'session_id': session_id}).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        try:
            with _urlrequest.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                logger.info(f"Wolfram abort for {session_id[:8]}: {data}")
                return True
        except _urlerror.HTTPError as e:
            if e.code == 404:
                # Older service container without /session/abort
                logger.warning("Wolfram service does not support /session/abort (old image)")
                return False
            logger.warning(f"Wolfram abort failed for {session_id[:8]}: HTTP {e.code}")
            return False
        except Exception as e:
            logger.warning(f"Wolfram abort failed for {session_id[:8]}: {e}")
            return False

    # Shell one-liner that SIGINTs the ipykernel child process(es) inside the
    # container without touching the gateway (PID 1). Works on every image —
    # needs only sh + procfs + grep (-a treats the NUL-separated cmdline as
    # text), no procps/pkill. The [l] character class stops the scan from
    # matching its own sh process (whose cmdline contains this script text),
    # and $$ is skipped as a second guard.
    _INTERRUPT_CMD = (
        "sh -c '"
        "ok=1; for d in /proc/[0-9]*; do "
        "p=${d#/proc/}; [ \"$p\" = 1 ] && continue; [ \"$p\" = \"$$\" ] && continue; "
        "if grep -aq \"ipykernel_[l]auncher\" \"$d/cmdline\" 2>/dev/null; then "
        "kill -INT \"$p\" 2>/dev/null && ok=0; fi; done; exit $ok'"
    )

    async def interrupt_kernel(self, session_id: str):
        """Interrupt the currently executing cell without killing the kernel.

        - Container sessions: SIGINT the ipykernel process directly (via the
          gateway's interrupt signal when supported, else an exec-based /proc
          scan). Never signals container PID 1 with SIGINT — on old gateway
          images that shuts the whole session down.
        - Wolfram virtual sessions: abort in-flight evaluations on the service.
        - Hybrid sessions: both of the above.
        """
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError("Session not found")

        if getattr(session, 'is_virtual', False) and (session.connection_info or {}).get('local_python'):
            import urllib.request as _urlrequest
            svc = session.connection_info['service_url']
            tok = session.connection_info.get('auth_token', '')
            req = _urlrequest.Request(
                f"{svc.rstrip('/')}/interrupt",
                data=b'{}',
                headers={'Content-Type': 'application/json', 'X-Phonos-Kernel-Token': tok},
                method='POST',
            )
            _urlrequest.urlopen(req, timeout=5).read()
            session.status = 'idle'
            session.last_activity = datetime.utcnow()
            return

        # Virtual sessions (Wolfram service): abort the running wolframscript
        # subprocess(es) on the service side.
        if getattr(session, 'is_virtual', False) or (
                (session.container_id or '').startswith('wolfram-virtual')):
            if not self._abort_wolfram_evaluations(session_id):
                raise RuntimeError(
                    "Wolfram evaluation could not be aborted (service too old?); "
                    "it will stop at its own timeout"
                )
            session.status = 'idle'
            session.last_activity = datetime.utcnow()
            return

        # Hybrid runtimes may have a Wolfram evaluation in flight in addition
        # to the Python kernel; abort it best-effort before signalling Python.
        if getattr(session, 'runtime_level', '') == 'hybrid':
            try:
                self._abort_wolfram_evaluations(session_id)
            except Exception:
                pass

        try:
            container = self.docker_client.containers.get(session.container_id)
            caps = self._get_capabilities(session)
            sig = caps.get('interrupt_signal')
            if sig:
                # New gateway: dedicated signal handler interrupts the kernel.
                container.kill(signal=sig)
                logger.info(f"Interrupt signal {sig} sent to gateway for session {session_id[:8]}")
                return
            # Fallback for old images: SIGINT the ipykernel process directly.
            exit_code, output = container.exec_run(self._INTERRUPT_CMD)
            if exit_code == 0:
                logger.info(f"Interrupt (exec SIGINT) delivered to kernel for session {session_id[:8]}")
            else:
                logger.warning(
                    f"Interrupt exec found no ipykernel process for session {session_id[:8]}: "
                    f"{(output or b'').decode(errors='replace')[:200]}"
                )
        except Exception as e:
            logger.error(f"Failed to interrupt kernel: {e}")
            raise

    async def restart_kernel_inplace(self, session_id: str) -> bool:
        """Restart the kernel process without recreating the container.

        Returns True when the fast path succeeded (same session_id remains
        valid, same ZMQ ports). Returns False when unsupported — the caller
        should fall back to a full stop_kernel/start_kernel cycle.
        """
        session = self.sessions.get(session_id)
        if not session:
            return False

        if getattr(session, 'is_virtual', False) and (session.connection_info or {}).get('local_python'):
            import urllib.request as _urlrequest
            try:
                svc = session.connection_info['service_url']
                tok = session.connection_info.get('auth_token', '')
                req = _urlrequest.Request(
                    f"{svc.rstrip('/')}/reset",
                    data=b'{}',
                    headers={'Content-Type': 'application/json', 'X-Phonos-Kernel-Token': tok},
                    method='POST',
                )
                _urlrequest.urlopen(req, timeout=5).read()
                session.execution_count = 0
                session.status = 'idle'
                session.last_activity = datetime.utcnow()
                return True
            except Exception as exc:
                logger.warning(f"Local Python reset failed for {session_id[:8]}: {exc}")
                return False

        # Wolfram virtual sessions: dropping the accumulated service state IS
        # the restart (each evaluation replays history on a fresh kernel).
        if getattr(session, 'is_virtual', False) or (
                (session.container_id or '').startswith('wolfram-virtual')):
            import urllib.request as _urlrequest
            try:
                config = self._resolve_wolfram_config()
                req = _urlrequest.Request(
                    f"{config['service_url'].rstrip('/')}/session/reset",
                    data=json.dumps({'session_id': session_id}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                _urlrequest.urlopen(req, timeout=5).read()
            except Exception as e:
                logger.warning(f"Wolfram in-place restart failed for {session_id[:8]}: {e}")
                return False
            session.wolfram_cell_history = []
            session.execution_count = 0
            session.status = 'idle'
            session.last_activity = datetime.utcnow()
            logger.info(f"Wolfram session {session_id[:8]} reset in place")
            return True

        try:
            container = self.docker_client.containers.get(session.container_id)
        except Exception as e:
            logger.warning(f"In-place restart: container lookup failed for {session_id[:8]}: {e}")
            return False

        caps = self._get_capabilities(session)
        sig = caps.get('restart_signal')
        if not sig:
            return False

        sent_at = time.time()
        try:
            container.kill(signal=sig)
        except Exception as e:
            logger.warning(f"In-place restart signal failed for {session_id[:8]}: {e}")
            return False

        # Wait for the gateway to confirm the restart by rewriting the marker
        # file with a timestamp newer than our signal.
        max_wait = 60
        poll = 0.5
        for _ in range(int(max_wait / poll)):
            await asyncio.sleep(poll)
            try:
                exit_code, output = container.exec_run('cat /tmp/kernel_restarted')
                if exit_code == 0:
                    marker = float(output.decode().strip() or 0)
                    if marker >= sent_at - 2:  # small tolerance for clock granularity
                        session.execution_count = 0
                        session.status = 'idle'
                        session.last_activity = datetime.utcnow()
                        # Hybrid: also reset the Wolfram half so both languages
                        # start from a clean slate.
                        if getattr(session, 'runtime_level', '') == 'hybrid':
                            try:
                                self._abort_wolfram_evaluations(session_id)
                            except Exception:
                                pass
                            import urllib.request as _urlrequest
                            try:
                                config = self._resolve_wolfram_config()
                                req = _urlrequest.Request(
                                    f"{config['service_url'].rstrip('/')}/session/reset",
                                    data=json.dumps({'session_id': session_id}).encode('utf-8'),
                                    headers={'Content-Type': 'application/json'},
                                    method='POST',
                                )
                                _urlrequest.urlopen(req, timeout=5).read()
                            except Exception:
                                pass
                            session.wolfram_cell_history = []
                        logger.info(f"Kernel restarted in place for session {session_id[:8]}")
                        return True
            except Exception:
                pass

        logger.warning(f"In-place restart timed out for session {session_id[:8]}")
        return False

    def _close_session_resources(self, session_id: str, force: bool = False):
        """
        Synchronously close ZMQ sockets and remove session from memory.
        
        Args:
            session_id: The session ID to clean up
            force: If True, force cleanup even if session appears partially initialized
        """
        session = self.sessions.get(session_id)
        if not session:
            logger.debug(f"Session {session_id[:8]} not found for cleanup")
            return

        logger.info(f"Closing resources for session {session_id[:8]}")

        # Clean up ZMQ sockets with proper error handling
        sockets_to_close = []
        if session.shell_socket and hasattr(session.shell_socket, 'close'):
            sockets_to_close.append(('shell_socket', session.shell_socket))
        if session.shell_sync_socket and hasattr(session.shell_sync_socket, 'close'):
            sockets_to_close.append(('shell_sync_socket', session.shell_sync_socket))
        if session.stdin_sync_socket and hasattr(session.stdin_sync_socket, 'close'):
            sockets_to_close.append(('stdin_sync_socket', session.stdin_sync_socket))
        if session.iopub_socket and hasattr(session.iopub_socket, 'close'):
            sockets_to_close.append(('iopub_socket', session.iopub_socket))

        for name, sock in sockets_to_close:
            try:
                sock.close(linger=0)
                logger.debug(f"Closed {name}")
            except Exception as e:
                logger.warning(f"Error closing {name}: {e}")

        # Clean up ZMQ contexts
        for name, ctx in (
            ('zmq_context', session.zmq_context),
            ('shell_sync_context', session.shell_sync_context),
            ('stdin_sync_context', session.stdin_sync_context),
        ):
            if ctx:
                try:
                    ctx.term()
                    logger.debug(f"{name} terminated")
                except Exception as e:
                    logger.warning(f"Error terminating {name}: {e}")

        # Remove from active sessions
        with self._lock:
            removed = self.sessions.pop(session_id, None)
            if removed:
                logger.info(f"Session {session_id[:8]} removed from manager")

    async def stop_kernel(self, session_id: str, cleanup_files: bool = False):
        """
        Stop a kernel container and clean up.

        Args:
            session_id: The session ID
            cleanup_files: If True, remove workspace files immediately
        """
        session = self.sessions.get(session_id)
        if not session:
            logger.warning(f"Session {session_id} not found")
            return

        logger.info(f"Stopping kernel session {session_id[:8]}")

        # Virtual Wolfram sessions keep replay state in the Wolfram service —
        # drop it now instead of waiting for the service's idle TTL so a
        # restarted kernel starts from a clean slate.
        if getattr(session, 'is_virtual', False) or (
                session.container_id and session.container_id.startswith('wolfram-virtual')):
            try:
                import json as _json
                import urllib.request as _urlrequest
                config = self._resolve_wolfram_config()
                reset_req = _urlrequest.Request(
                    f"{config['service_url'].rstrip('/')}/session/reset",
                    data=_json.dumps({'session_id': session_id}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'},
                    method='POST',
                )
                _urlrequest.urlopen(reset_req, timeout=5).read()
                logger.info(f"Wolfram service session state reset for {session_id[:8]}")
            except Exception as e:
                logger.warning(f"Could not reset Wolfram service session {session_id[:8]}: {e}")

        # Try to stop container first (before cleanup to avoid race)
        container_stopped = False
        if session.container_id:
            try:
                container = self.docker_client.containers.get(session.container_id)
                container.stop(timeout=5)
                container.remove(force=True)
                container_stopped = True
                logger.info(f"Container {session.container_id[:12]} removed")
            except NotFound:
                logger.debug("Container already removed")
            except Exception as e:
                logger.error(f"Error stopping container: {e}")

        # Clean up resources and memory (always try, even if container stop failed)
        try:
            self._close_session_resources(session_id)
        except Exception as e:
            logger.error(f"Error closing session resources: {e}")

        # Optionally clean up workspace files
        if cleanup_files and session.workspace_path:
            try:
                import shutil
                if os.path.exists(session.workspace_path):
                    shutil.rmtree(session.workspace_path)
                    logger.info(f"Workspace cleaned up: {session.workspace_path}")
            except Exception as e:
                logger.error(f"Failed to clean workspace: {e}")

        logger.info(f"Session {session_id[:8]} stopped")

    def get_session(self, session_id: str) -> Optional[KernelSession]:
        """Get a session by ID, verifying container health"""
        logger.debug(f"get_session called for {session_id}")
        session = self.sessions.get(session_id)

        # If session in memory, verify it's still alive in Docker
        if session and DOCKER_AVAILABLE:
            # Skip container check for virtual sessions (e.g., Wolfram)
            if getattr(session, 'is_virtual', False) or session.container_id.startswith('wolfram-virtual'):
                pass  # Virtual sessions don't have containers
            else:
                try:
                    container = self.docker_client.containers.get(session.container_id)
                    if container.status != 'running':
                        logger.warning(f"Session {session_id} in memory but container {session.container_id[:12]} is {container.status}. Pruning.")
                        self._close_session_resources(session_id)
                        session = None
                except NotFound:
                    logger.warning(f"Session {session_id} in memory but container not found. Pruning.")
                    self._close_session_resources(session_id)
                    session = None
                except Exception as e:
                    logger.error(f"Error verifying session {session_id}: {e}")
                    # Assume dead on error to be safe? Or keep?
                    # If docker is down, we probably can't do anything anyway.
                    pass

        # If session not in memory (or pruned), try to reconnect to running container
        if session is None and DOCKER_AVAILABLE:
            session = self._try_reconnect_session(session_id)

        return session

    def _try_reconnect_session(self, session_id: str) -> Optional[KernelSession]:
        """Try to reconnect to an existing kernel container"""
        try:
            container_name = f"{self.CONTAINER_PREFIX}{session_id[:12]}"
            container = self.docker_client.containers.get(container_name)

            if container.status != 'running':
                logger.info(f"Container {container_name} exists but not running (status: {container.status})")
                return None

            logger.info(f"Found running container {container_name}, attempting reconnection...")

            # Read connection info from container
            exit_code, output = container.exec_run('cat /tmp/kernel_connection.json')
            if exit_code != 0:
                logger.warning(f"Could not read connection info from container {container_name}")
                return None

            conn_info = json.loads(output.decode())

            # Get container IP
            container.reload()
            networks = container.attrs.get('NetworkSettings', {}).get('Networks', {})
            container_ip = ''
            if self.NETWORK_NAME in networks:
                container_ip = networks[self.NETWORK_NAME].get('IPAddress', '')

            if not container_ip:
                logger.warning(f"Could not get IP for container {container_name}")
                return None

            # Extract user_id, notebook_id, and runtime_level from container labels.
            # The runtime level is stored as 'phonos.notebook.language' during
            # container start, with 'phonos.notebook.runtime_level' as a fallback.
            labels = container.labels or {}
            user_id = int(labels.get('phonos.notebook.user', 0))
            notebook_id = int(labels.get('phonos.notebook.id', 0))
            runtime_level = (
                labels.get('phonos.notebook.language') or
                labels.get('phonos.notebook.runtime_level') or
                'python'
            )

            # Recover the workspace path so a later stop_kernel(cleanup_files=True)
            # on this reconnected session still knows what to remove.
            workspace_path = ''
            if user_id and notebook_id:
                try:
                    workspace_path = self._get_workspace_path(user_id, notebook_id)
                except Exception:
                    pass

            # Create session object (same quantum-aware cell timeout as
            # start_kernel so reconnected sessions keep hardware-job headroom).
            session = KernelSession(
                session_id=session_id,
                container_id=container.id,
                user_id=user_id,
                notebook_id=notebook_id,
                runtime_level=runtime_level,
                status='idle',
                container_ip=container_ip,
                connection_info=conn_info,
                workspace_path=workspace_path,
                execution_timeout=3600 if runtime_level in ('quantum', 'hybrid') else 300
            )

            # Setup ZMQ connections if available
            if ZMQ_AVAILABLE:
                try:
                    session.zmq_context = zmq.asyncio.Context()

                    # The reply reader thread creates and owns the sync shell/stdin
                    # DEALER sockets when it starts. Avoid creating a competing async
                    # shell socket with the same identity, which can steal replies
                    # from the kernel's ROUTER and leave cells stuck running.

                    # IOPub socket for output (SUB)
                    session.iopub_socket = session.zmq_context.socket(zmq.SUB)
                    session.iopub_socket.connect(
                        f"tcp://{container_ip}:{conn_info['iopub_port']}"
                    )
                    session.iopub_socket.setsockopt(zmq.SUBSCRIBE, b'')

                    logger.info("ZMQ sockets reconnected")
                except Exception as e:
                    logger.error(f"Failed to reconnect ZMQ sockets: {e}")

            # Store session
            with self._lock:
                self.sessions[session_id] = session

            logger.info(f"Successfully reconnected to session {session_id}")
            return session

        except NotFound:
            logger.debug(f"No container found for session {session_id}")
            return None
        except Exception as e:
            logger.error(f"Error reconnecting to session {session_id}: {e}")
            return None

    def get_user_sessions(self, user_id: int) -> list:
        """Get all sessions for a user"""
        return [s for s in self.sessions.values() if s.user_id == user_id]

    def resume_kernel(self, session_id: str) -> Optional[KernelSession]:
        """Try to resume an existing kernel session from the database.

        Called when the frontend reconnects to a notebook that has an active
        DB session but no in-memory entry (e.g. after a server restart). If
        the underlying container is still running, this rebuilds the ZMQ
        connections and returns the session; otherwise returns None.
        """
        session = self.get_session(session_id)
        if session:
            logger.debug(f"Session {session_id[:8]} already in memory")
            return session

        if not DOCKER_AVAILABLE:
            return None

        try:
            container_name = f"{self.CONTAINER_PREFIX}{session_id[:12]}"
            container = self.docker_client.containers.get(container_name)
            if container.status != 'running':
                logger.info(f"Container {container_name} exists but is {container.status}")
                return None
            return self._try_reconnect_session(session_id)
        except NotFound:
            logger.debug(f"No container found for session {session_id}")
            return None
        except Exception as e:
            logger.error(f"Error resuming session {session_id}: {e}")
            return None

    async def _cleanup_idle_sessions_once(self) -> dict:
        """Perform a single cleanup pass over idle/expired sessions.

        Returns a stats dict ({'checked', 'cleaned', 'errors'}) so callers
        (e.g. the periodic scheduler) can log how much work was done.
        """
        now = datetime.utcnow()
        to_cleanup = []

        with self._lock:
            checked = len(self.sessions)
            for session_id, session in self.sessions.items():
                idle_time = now - session.last_activity
                if idle_time > self.MAX_IDLE_TIME:
                    # last_activity is only touched at dispatch and on kernel
                    # status transitions — a 'busy' kernel is still executing
                    # (e.g. an IBM Quantum hardware job waiting in queue), so
                    # only MAX_SESSION_TIME applies to it.
                    if session.status == 'busy':
                        logger.debug(
                            f"Session {session_id} idle for {idle_time} but kernel is busy; skipping idle reap"
                        )
                    else:
                        logger.info(f"Session {session_id} idle for {idle_time}, marking for cleanup")
                        to_cleanup.append(session_id)
                        continue
                # Enforce maximum session lifetime regardless of activity
                session_age = now - session.created_at
                if session_age > self.MAX_SESSION_TIME:
                    logger.info(f"Session {session_id} exceeded max lifetime ({session_age}), marking for cleanup")
                    to_cleanup.append(session_id)

        cleaned = 0
        errors = 0
        for session_id in to_cleanup:
            try:
                await self.stop_kernel(session_id)
                cleaned += 1
            except Exception as e:
                errors += 1
                logger.error(f"Error cleaning up session {session_id}: {e}")

        return {'checked': checked, 'cleaned': cleaned, 'errors': errors}

    async def cleanup_idle_sessions(self):
        """Long-running background task that cleans up idle sessions every minute."""
        logger.info("Starting session cleanup task")

        while True:
            await asyncio.sleep(60)  # Check every minute
            try:
                await self._cleanup_idle_sessions_once()
            except Exception as e:
                logger.error(f"Error in periodic session cleanup pass: {e}", exc_info=True)

    def cleanup_idle_sessions_sync(self) -> dict:
        """Synchronous one-shot cleanup for callers without a running event loop.

        Used by the APScheduler thread, which is not async. Mirrors the
        per-operation event-loop pattern used throughout the notebook handlers.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self._cleanup_idle_sessions_once())
        finally:
            loop.close()
            asyncio.set_event_loop(None)

    def start_cleanup_task(self, loop=None):
        """Start the background cleanup task"""
        if loop is None:
            loop = asyncio.get_event_loop()

        self._cleanup_task = loop.create_task(self.cleanup_idle_sessions())
        logger.info("Cleanup task started")

    def register_output_listener(self, session_id: str, callback: Callable):
        """Register a callback for kernel output"""
        self._output_listeners[session_id] = callback

    def unregister_output_listener(self, session_id: str):
        """Unregister output callback"""
        self._output_listeners.pop(session_id, None)

    def register_reply_callback(self, session_id: str, callback: Callable):
        """Register a callback for kernel execute_reply messages."""
        self._reply_callbacks[session_id] = callback

    def unregister_reply_callback(self, session_id: str):
        """Unregister execute_reply callback."""
        self._reply_callbacks.pop(session_id, None)

    def start_reply_reader(self, session_id: str):
        """Mark the reply reader as started for this session.

        The actual reader loop is started by the SocketIO layer via
        ``socketio.start_background_task`` so it runs as an eventlet green
        thread in production.  The reader is the only code that touches the
        sync shell and stdin ZMQ sockets, avoiding ZMQ thread-safety
        violations.
        """
        session = self.sessions.get(session_id)
        if not session:
            logger.warning(f"Cannot start reply reader for {session_id}: no session")
            return
        if session._reply_reader_started:
            return
        if not session.container_ip or not session.connection_info:
            logger.warning(f"Cannot start reply reader for {session_id}: no connection info")
            return
        session._reply_reader_started = True
        logger.info(f"Reply reader marked started for session {session_id[:8]}")

    def _create_shell_sync_socket(self, session, zmq):
        """Create the sync shell socket in the reader thread."""
        ctx = zmq.Context()
        sock = ctx.socket(zmq.DEALER)
        sock.setsockopt(zmq.IDENTITY, session.session_id.encode())
        sock.setsockopt(zmq.SNDTIMEO, 5000)
        sock.setsockopt(zmq.RCVTIMEO, 100)
        sock.setsockopt(zmq.LINGER, 500)
        sock.connect(
            f"tcp://{session.container_ip}:{session.connection_info['shell_port']}"
        )
        session.shell_sync_context = ctx
        session.shell_sync_socket = sock
        return sock

    def _create_stdin_sync_socket(self, session, zmq):
        """Create the sync stdin socket in the reader thread."""
        ctx = zmq.Context()
        sock = ctx.socket(zmq.DEALER)
        sock.setsockopt(zmq.IDENTITY, session.session_id.encode())
        sock.setsockopt(zmq.RCVTIMEO, 100)
        sock.setsockopt(zmq.LINGER, 500)
        sock.connect(
            f"tcp://{session.container_ip}:{session.connection_info['stdin_port']}"
        )
        session.stdin_sync_context = ctx
        session.stdin_sync_socket = sock
        return sock

    def _read_shell_replies(self, session_id: str):
        """Background thread that owns the shell/stdin sockets.

        Sends queued execute_request / input_reply messages and reads incoming
        execute_reply / input_request messages on the same sockets. Creating
        and using the sockets in this single thread satisfies ZMQ's thread
        safety requirement.
        """
        import zmq
        session = self.sessions.get(session_id)
        if not session:
            return

        try:
            shell_sock = self._create_shell_sync_socket(session, zmq)
            stdin_sock = self._create_stdin_sync_socket(session, zmq)
        except Exception as e:
            logger.error(f"Failed to create sync sockets for {session_id}: {e}")
            session._reply_reader_started = False
            return

        poller = zmq.Poller()
        poller.register(shell_sock, zmq.POLLIN)
        poller.register(stdin_sock, zmq.POLLIN)

        logger.info(f"Sync shell/stdin sockets connected for session {session_id[:8]}")

        while session_id in self.sessions:
            try:
                # Drain outgoing queues first so sends happen promptly.
                while True:
                    try:
                        msg_parts = session.shell_send_queue.get_nowait()
                    except _real_queue.Empty:
                        break
                    try:
                        shell_sock.send_multipart(msg_parts)
                        logger.info(f"Execute request sent on sync socket for session {session_id[:8]}")
                    except Exception as send_err:
                        logger.warning(f"Failed to send execute request for {session_id}: {send_err}")

                while True:
                    try:
                        msg_parts = session.stdin_send_queue.get_nowait()
                    except _real_queue.Empty:
                        break
                    try:
                        stdin_sock.send_multipart(msg_parts)
                        logger.info(f"Input reply sent on sync socket for session {session_id[:8]}")
                    except Exception as send_err:
                        logger.warning(f"Failed to send input reply for {session_id}: {send_err}")

                # Non-blocking poll so this green thread never stalls the
                # eventlet hub.  A short sleep yields control between polls.
                ready = dict(poller.poll(timeout=0))
                for sock, flag in ready.items():
                    if not (flag & zmq.POLLIN):
                        continue
                    try:
                        msg_parts = sock.recv_multipart(zmq.NOBLOCK)
                    except zmq.Again:
                        continue
                    if len(msg_parts) < 6:
                        continue
                    try:
                        delim_idx = msg_parts.index(b'<IDS|MSG>')
                        header = json.loads(msg_parts[delim_idx + 2])
                        parent_header = json.loads(msg_parts[delim_idx + 3])
                        content = json.loads(msg_parts[delim_idx + 5])
                    except (ValueError, json.JSONDecodeError):
                        continue

                    msg_type = header.get('msg_type')
                    logger.info(f"Shell reply received for {session_id[:8]}: msg_type={msg_type}, parent_msg_id={parent_header.get('msg_id')}")
                    if msg_type == 'execute_reply':
                        session.reply_queue.put({
                            'channel': 'shell',
                            'msg_type': 'execute_reply',
                            'msg_id': header.get('msg_id'),
                            'parent_msg_id': parent_header.get('msg_id'),
                            'content': content,
                        })
                    elif msg_type == 'input_request':
                        session.reply_queue.put({
                            'channel': 'stdin',
                            'msg_type': 'input_request',
                            'msg_id': header.get('msg_id'),
                            'parent_msg_id': parent_header.get('msg_id'),
                            'content': content,
                        })

                # Yield to the eventlet hub so other green threads can run.
                time.sleep(0.02)
            except zmq.ZMQError as e:
                if e.errno in (zmq.ETERM, zmq.EINTR):
                    break
                logger.debug(f"Shell reply read error for {session_id}: {e}")
            except Exception as e:
                logger.error(f"Shell reply reader error for {session_id}: {e}")

        session._reply_reader_started = False

    async def listen_for_output(self, session_id: str):
        """
        Listen for kernel output on IOPub socket.
        Yields parsed messages.
        """
        session = self.sessions.get(session_id)
        if not session or not session.iopub_socket:
            return

        while session_id in self.sessions:
            try:
                # Non-blocking receive with timeout
                if await asyncio.wait_for(
                    session.iopub_socket.poll(timeout=100),
                    timeout=1.0
                ):
                    msg_parts = await session.iopub_socket.recv_multipart()

                    # Parse message
                    if len(msg_parts) >= 7:
                        # Find delimiter
                        try:
                            delim_idx = msg_parts.index(b'<IDS|MSG>')
                            header = json.loads(msg_parts[delim_idx + 2])
                            parent_header = json.loads(msg_parts[delim_idx + 3])
                            content = json.loads(msg_parts[delim_idx + 5])

                            yield {
                                'msg_type': header.get('msg_type'),
                                'msg_id': header.get('msg_id'),
                                'parent_msg_id': parent_header.get('msg_id'),
                                'content': content
                            }
                        except (ValueError, json.JSONDecodeError):
                            continue

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Error reading output: {e}")
                break


# Global instance
kernel_manager = KernelManager()
