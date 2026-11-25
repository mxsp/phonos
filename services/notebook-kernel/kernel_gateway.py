#!/usr/bin/env python3
"""
Phonos Notebook Kernel Gateway
Manages Jupyter kernel lifecycle and exposes ZMQ ports for external communication.

Signal protocol (v2):
  SIGTERM        clean shutdown (docker stop)
  SIGINT         interrupt the running cell (forwarded to the kernel process)
  SIGUSR1        interrupt the running cell (same as SIGINT; explicit alias)
  SIGUSR2        restart the kernel in place (same container, same ZMQ ports)

Capabilities are advertised in /tmp/kernel_capabilities.json so the Phonos
backend can feature-detect; old images without the file get exec-based
interrupt and full container-recycle restarts instead.
"""
import os
import sys
import json
import time
import signal
import asyncio
import logging
from pathlib import Path

from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpecManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger('kernel_gateway')

# Connection file path
CONNECTION_FILE = '/tmp/kernel_connection.json'
READY_FILE = '/tmp/kernel_ready'
CAPABILITIES_FILE = '/tmp/kernel_capabilities.json'
RESTART_MARKER_FILE = '/tmp/kernel_restarted'

# How many times to auto-restart a kernel that died unexpectedly before
# giving up and letting the container exit.
MAX_AUTO_RESTARTS = 3


class KernelGateway:
    """
    Kernel Gateway that manages a Jupyter kernel and exposes connection info.
    The Phonos backend connects to this kernel via ZMQ sockets.
    """

    def __init__(self):
        self.km = None
        self.kc = None
        self.running = True
        self.session_id = os.environ.get('NOTEBOOK_SESSION_ID', 'default')
        self.user_id = os.environ.get('NOTEBOOK_USER_ID', '0')
        self._restart_requested = False
        self._auto_restarts = 0

    async def start(self):
        """Start the kernel and write connection info"""
        logger.info(f"Starting kernel for session {self.session_id}")

        try:
            # Initialize kernel manager - bind to all interfaces for Docker networking
            # Map NOTEBOOK_LANGUAGE to the actual kernel spec name
            lang = os.environ.get('NOTEBOOK_LANGUAGE', 'python3')
            kernel_map = {'python': 'python3', 'basic': 'python3', 'quantum': 'quantum', 'hybrid': 'quantum'}
            kernel_name = kernel_map.get(lang, lang)
            self.km = KernelManager(kernel_name=kernel_name, ip='0.0.0.0')

            # Start the kernel
            self.km.start_kernel()
            logger.info("Kernel process started")

            # Get client and start channels
            self._connect_client()

            # Write connection info for external access
            conn_info = self.km.get_connection_info()
            conn_info['session_id'] = self.session_id

            # Convert bytes to strings for JSON serialization
            for key, value in conn_info.items():
                if isinstance(value, bytes):
                    conn_info[key] = value.decode('utf-8')

            with open(CONNECTION_FILE, 'w') as f:
                json.dump(conn_info, f, indent=2)

            logger.info(f"Connection info written to {CONNECTION_FILE}")
            logger.info(f"Shell port: {conn_info.get('shell_port')}")
            logger.info(f"IOPub port: {conn_info.get('iopub_port')}")
            logger.info(f"Stdin port: {conn_info.get('stdin_port')}")
            logger.info(f"Control port: {conn_info.get('control_port')}")
            logger.info(f"HB port: {conn_info.get('hb_port')}")

            # Advertise gateway capabilities for host-side feature detection.
            with open(CAPABILITIES_FILE, 'w') as f:
                json.dump({
                    'version': 2,
                    'interrupt_signal': 'SIGUSR1',
                    'restart_signal': 'SIGUSR2',
                    'auto_restart': True,
                    'languages': ['python', 'quantum', 'c', 'cpp', 'rust', 'r'],
                }, f)

            # Write ready marker
            Path(READY_FILE).touch()
            logger.info("Kernel gateway ready")

            # Execute startup script if exists
            await self._run_startup_script()

            # Keep running until signaled to stop
            while self.running:
                if self._restart_requested:
                    self._restart_requested = False
                    await self._restart_kernel_inplace(reason='restart requested')
                    continue

                # Heartbeat check
                if not self.km.is_alive():
                    if self._auto_restarts >= MAX_AUTO_RESTARTS:
                        logger.error(
                            f"Kernel died unexpectedly (auto-restart limit "
                            f"{MAX_AUTO_RESTARTS} reached); shutting down"
                        )
                        break
                    self._auto_restarts += 1
                    logger.warning(
                        f"Kernel died unexpectedly; auto-restarting "
                        f"({self._auto_restarts}/{MAX_AUTO_RESTARTS})"
                    )
                    await self._restart_kernel_inplace(reason='kernel died')
                    continue

                await asyncio.sleep(1)

        except Exception as e:
            logger.error(f"Fatal error: {e}")
            raise
        finally:
            self.shutdown()

    def _connect_client(self):
        """(Re)create the kernel client and wait for the kernel to answer."""
        if self.kc:
            try:
                self.kc.stop_channels()
            except Exception:
                pass
        self.kc = self.km.client()
        self.kc.start_channels()
        logger.info("Waiting for kernel ready...")
        try:
            self.kc.wait_for_ready(timeout=60)
            logger.info("Kernel is ready")
        except RuntimeError as e:
            logger.error(f"Kernel failed to start: {e}")
            raise

    async def _restart_kernel_inplace(self, reason=''):
        """Restart the kernel process, keeping the same connection ports.

        The host's ZMQ sockets stay connected to the same endpoints, so no
        reconnection is needed on the Phonos backend side. The startup script
        re-runs so the phonos SDK and quantum credentials are available in the
        fresh kernel, and a restart marker file is (re)written so the host can
        confirm completion.
        """
        logger.info(f"Restarting kernel in place ({reason})...")
        try:
            # newports=False (default) keeps the connection file and ports.
            self.km.restart_kernel(now=True)
            self._connect_client()
            await self._run_startup_script()
            with open(RESTART_MARKER_FILE, 'w') as f:
                f.write(str(time.time()))
            Path(READY_FILE).touch()
            logger.info("In-place kernel restart complete")
        except Exception as e:
            logger.error(f"In-place restart failed: {e}")
            # Leave the loop to decide (heartbeat will catch a dead kernel).

    async def _run_startup_script(self):
        """Execute startup script to configure the environment"""
        startup_path = Path('/home/notebook/startup.py')
        if startup_path.exists():
            logger.info("Running startup script...")
            try:
                with open(startup_path) as f:
                    startup_code = f.read()

                # Execute via kernel
                self.kc.execute(startup_code)

                # Wait for execution to complete
                while True:
                    try:
                        msg = self.kc.get_iopub_msg(timeout=1)
                        if msg['msg_type'] == 'status' and msg['content']['execution_state'] == 'idle':
                            break
                    except:
                        break

                logger.info("Startup script completed")
            except Exception as e:
                logger.warning(f"Startup script error: {e}")

    def shutdown(self, signum=None, frame=None):
        """Clean shutdown of kernel"""
        logger.info("Shutting down kernel gateway...")
        self.running = False

        try:
            if self.kc:
                self.kc.stop_channels()
                logger.info("Channels stopped")

            if self.km:
                if self.km.is_alive():
                    self.km.shutdown_kernel(now=True)
                    logger.info("Kernel shutdown complete")

            # Clean up files
            for path in (CONNECTION_FILE, READY_FILE, CAPABILITIES_FILE, RESTART_MARKER_FILE):
                if os.path.exists(path):
                    os.remove(path)

        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

        logger.info("Kernel gateway stopped")

    def handle_terminate(self, signum, frame):
        """SIGTERM: clean shutdown (docker stop)."""
        logger.info(f"Received signal {signum} (terminate)")
        self.shutdown()
        sys.exit(0)

    def handle_interrupt_signal(self, signum, frame):
        """SIGINT / SIGUSR1: interrupt the running cell, keep the kernel alive.

        Historically SIGINT shut the whole gateway down, which made the
        frontend's Stop button kill the session. Interrupting the kernel is
        the correct semantic for both signals; container stop uses SIGTERM.
        """
        logger.info(f"Received signal {signum} (interrupt request)")
        try:
            if self.km and self.km.is_alive():
                self.km.interrupt_kernel()
                logger.info("Kernel interrupted")
        except Exception as e:
            logger.error(f"Failed to interrupt kernel: {e}")

    def handle_restart_signal(self, signum, frame):
        """SIGUSR2: request an in-place kernel restart (handled in main loop)."""
        logger.info(f"Received signal {signum} (restart request)")
        self._restart_requested = True


def main():
    """Main entry point"""
    gateway = KernelGateway()

    # Register signal handlers
    signal.signal(signal.SIGTERM, gateway.handle_terminate)
    signal.signal(signal.SIGINT, gateway.handle_interrupt_signal)
    signal.signal(signal.SIGUSR1, gateway.handle_interrupt_signal)
    signal.signal(signal.SIGUSR2, gateway.handle_restart_signal)

    # Run the gateway
    try:
        asyncio.run(gateway.start())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
