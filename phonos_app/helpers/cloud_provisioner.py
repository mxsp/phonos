"""
Cloud Provisioner — lifecycle management for remote kernel VMs.

Supports AWS EC2 and GCP Compute Engine as backends.
Each backend can start, stop, and check the status of a VM instance.

Placeholder: concrete SDK calls are stubbed with logs.
"""

from __future__ import annotations

import abc
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class VmSpec:
    """Desired shape of the remote VM."""
    provider: str               # "aws" | "gcp"
    region: str
    instance_type: str          # e.g. "g4dn.xlarge" (AWS) or "n1-standard-4" (GCP)
    image_id: str               # AMI or image project/family
    disk_size_gb: int = 100
    disk_type: str = "gp3"
    subnet_id: str = ""         # VPC subnet (AWS) / network (GCP)
    security_group_ids: list[str] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    ssh_key_name: str = ""
    iam_instance_profile: str = ""
    user_data_script: str = ""  # cloud-init / startup script


@dataclass
class VmInstance:
    """Represents a running (or pending) VM."""
    instance_id: str
    provider: str
    public_ip: str = ""
    private_ip: str = ""
    status: str = "pending"     # pending | running | stopping | stopped | terminated
    created_at: datetime = field(default_factory=datetime.utcnow)
    spec: VmSpec = None


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class CloudProvider(abc.ABC):
    """Interface every cloud provider must implement."""

    @abc.abstractmethod
    def start_instance(self, spec: VmSpec) -> VmInstance:
        """Provision a VM and return its descriptor.
        Blocks until the instance exists (but may return before it is 'running')."""

    @abc.abstractmethod
    def stop_instance(self, instance_id: str) -> None:
        """Stop (not terminate) the instance so it can be resumed later."""

    @abc.abstractmethod
    def terminate_instance(self, instance_id: str) -> None:
        """Permanently delete the instance and attached volumes."""

    @abc.abstractmethod
    def get_instance(self, instance_id: str) -> VmInstance:
        """Return current state, or None if gone."""

    @abc.abstractmethod
    def wait_until_running(self, instance_id: str, timeout: float = 300.0) -> VmInstance:
        """Poll until the instance reports 'running' and has an IP."""

    @abc.abstractmethod
    def get_cost_estimate(self, spec: VmSpec) -> dict:
        """Return estimated hourly / monthly cost in USD."""


# ---------------------------------------------------------------------------
# AWS EC2  (placeholder — stubs)
# ---------------------------------------------------------------------------

class AwsEc2Provider(CloudProvider):
    """Manage EC2 instances via boto3."""

    def __init__(self, config: dict = None):
        self._config = config or {}
        # self._client = boto3.client("ec2", region_name=…)

    def start_instance(self, spec: VmSpec) -> VmInstance:
        logger.info("AWS start_instance: would create EC2 %s in %s (image=%s, disk=%dG)",
                     spec.instance_type, spec.region, spec.image_id, spec.disk_size_gb)
        # TODO: actual boto3 call →
        #   ec2.run_instances(ImageId=spec.image_id, InstanceType=spec.instance_type, …)
        return VmInstance(
            instance_id=f"i-placeholder-{int(time.time())}",
            provider="aws",
            status="pending",
            spec=spec,
        )

    def stop_instance(self, instance_id: str) -> None:
        logger.info("AWS stop_instance: would stop %s", instance_id)
        # ec2.stop_instances(InstanceIds=[instance_id])

    def terminate_instance(self, instance_id: str) -> None:
        logger.info("AWS terminate_instance: would terminate %s", instance_id)
        # ec2.terminate_instances(InstanceIds=[instance_id])

    def get_instance(self, instance_id: str) -> VmInstance:
        logger.info("AWS get_instance: would describe %s", instance_id)
        # ec2.describe_instances(InstanceIds=[instance_id])
        return None

    def wait_until_running(self, instance_id: str, timeout: float = 300.0) -> VmInstance:
        logger.info("AWS wait_until_running: waiting for %s (%.0fs timeout)", instance_id, timeout)
        # waiter = ec2.get_waiter("instance_running")
        # waiter.wait(InstanceIds=[instance_id])
        # then describe to get IPs
        return VmInstance(instance_id=instance_id, provider="aws", status="running",
                          public_ip="203.0.113.1", private_ip="10.0.0.1")

    def get_cost_estimate(self, spec: VmSpec) -> dict:
        # boto3 does not have a direct pricing API without a separate subscription
        return {"hourly": 0.0, "monthly": 0.0, "note": "placeholder — configure actual pricing"}  # fmt: skip


# ---------------------------------------------------------------------------
# GCP Compute Engine  (placeholder — stubs)
# ---------------------------------------------------------------------------

class GcpComputeProvider(CloudProvider):
    """Manage GCE instances via google-api-python-client / google-cloud-compute."""

    def __init__(self, config: dict = None):
        self._config = config or {}
        # self._client = google.cloud.compute_v1.InstancesClient()

    def start_instance(self, spec: VmSpec) -> VmInstance:
        logger.info("GCP start_instance: would create VM %s in %s (image=%s, disk=%dG)",
                     spec.instance_type, spec.region, spec.image_id, spec.disk_size_gb)
        # TODO: actual GCP call →
        #   instances.insert(project=…, zone=…, instance_resource={…})
        return VmInstance(
            instance_id=f"gcp-placeholder-{int(time.time())}",
            provider="gcp",
            status="provisioning",
            spec=spec,
        )

    def stop_instance(self, instance_id: str) -> None:
        logger.info("GCP stop_instance: would stop %s", instance_id)
        # instances.stop(project=…, zone=…, instance=instance_id)

    def terminate_instance(self, instance_id: str) -> None:
        logger.info("GCP terminate_instance: would delete %s", instance_id)
        # instances.delete(project=…, zone=…, instance=instance_id)

    def get_instance(self, instance_id: str) -> VmInstance:
        logger.info("GCP get_instance: would describe %s", instance_id)
        # instances.get(project=…, zone=…, instance=instance_id)
        return None

    def wait_until_running(self, instance_id: str, timeout: float = 300.0) -> VmInstance:
        logger.info("GCP wait_until_running: waiting for %s (%.0fs timeout)", instance_id, timeout)
        return VmInstance(instance_id=instance_id, provider="gcp", status="running",
                          public_ip="34.1.2.3", private_ip="10.0.0.2")

    def get_cost_estimate(self, spec: VmSpec) -> dict:
        return {"hourly": 0.0, "monthly": 0.0, "note": "placeholder — configure actual pricing"}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDER_REGISTRY: dict[str, type[CloudProvider]] = {
    "aws": AwsEc2Provider,
    "gcp": GcpComputeProvider,
}


def get_provider(provider_name: str, config: dict = None) -> CloudProvider:
    cls = _PROVIDER_REGISTRY.get(provider_name)
    if not cls:
        raise ValueError(f"Unknown cloud provider '{provider_name}'. Available: {list(_PROVIDER_REGISTRY)}")
    return cls(config)


def register_provider(name: str, cls: type[CloudProvider]) -> None:
    _PROVIDER_REGISTRY[name] = cls


# ---------------------------------------------------------------------------
# Secure Tunnel (WireGuard)
# ---------------------------------------------------------------------------

@dataclass
class TunnelConfig:
    """WireGuard interface configuration for one peer."""
    interface: str = "wg0"
    private_key: str = ""           # server's private key
    listen_port: int = 51820
    peer_public_key: str = ""       # worker's public key
    peer_endpoint: str = ""         # worker's public IP:port
    allowed_ips: str = "10.242.0.0/24"
    tunnel_ip: str = "10.242.0.1/32"   # IP for this side of the tunnel


class WireGuardTunnel:
    """
    Manage a WireGuard tunnel between the Phonos server and a worker VM.

    Placeholder — actual ``wg set`` / ``wg-quick`` calls are stubbed.

    Flow:
      1. Provision worker VM (AwsEc2Provider / GcpComputeProvider).
      2. Generate server WireGuard key pair (stored in config / DB).
      3. Generate worker WireGuard key pair (injected via cloud-init / SSH).
      4. Exchange public keys → both sides have each other's public key.
      5. Bring up ``wg0`` interface on server, peer points to worker's public IP.
      6. Worker brings up ``wg0`` via cloud-init, peer points to server's public IP.
      7. Tunnel established — all ZMQ / file-sync traffic goes through ``10.242.0.x``.
    """

    def __init__(self, config: dict = None):
        self._config = config or {}

    def generate_keypair(self) -> dict[str, str]:
        """Generate a WireGuard private + public key pair."""
        logger.info("WireGuard: would run ``wg genkey | tee privatekey | wg pubkey``")
        return {"private_key": "CHANGE_ME_SERVER_PRIVATE_KEY", "public_key": "CHANGE_ME_SERVER_PUBLIC_KEY"}

    def up(self, cfg: TunnelConfig) -> None:
        """Create / configure the tunnel interface on the Phonos server."""
        logger.info("WireGuard up: would create %s (tunnel=%s, peer=%s)",
                     cfg.interface, cfg.tunnel_ip, cfg.peer_endpoint)
        # TODO: subprocess.run(["wg", "set", cfg.interface, …])
        #       ip addr add cfg.tunnel_ip dev cfg.interface
        #       ip link set cfg.interface up

    def down(self, interface: str = "wg0") -> None:
        """Tear down the tunnel interface."""
        logger.info("WireGuard down: would remove %s", interface)
        # ip link set interface down && wg showconf interface | wg-quick strip … | …

    def health_check(self, tunnel_ip: str) -> bool:
        """Quick ping test through the tunnel."""
        logger.info("WireGuard health: would ping %s", tunnel_ip)
        # subprocess.run(["ping", "-c1", "-W2", tunnel_ip], …)
        return True


# ---------------------------------------------------------------------------
# File Sync (rsync over tunnel)
# ---------------------------------------------------------------------------

class FileSync:
    """
    Sync project files from the Phonos server to the remote worker over the
    WireGuard tunnel so the kernel container sees the same ``/project`` tree.

    Placeholder — actual ``rsync`` / ``unison`` calls are stubbed.
    """

    def __init__(self, config: dict = None):
        self._config = config or {}

    def sync_to_worker(self, local_project_root: str, worker_tunnel_ip: str,
                       remote_path: str = "/project") -> bool:
        """
        One-shot rsync of the project directory to the worker.

        Uses ``rsync -a --delete`` over SSH through the tunnel.
        """
        logger.info("FileSync: rsync %s → %s:%s", local_project_root, worker_tunnel_ip, remote_path)
        # TODO:
        #   subprocess.run([
        #       "rsync", "-a", "--delete", "-e", "ssh -o StrictHostKeyChecking=no",
        #       local_project_root + "/",
        #       f"root@{worker_tunnel_ip}:{remote_path}/",
        #   ], check=True)
        return True

    def start_watch_sync(self, local_project_root: str, worker_tunnel_ip: str,
                         remote_path: str = "/project") -> None:
        """
        Optional: run ``lsyncd`` or ``inotifywait`` loop to keep files in sync
        in near-real-time during the kernel session.
        """
        logger.info("FileSync watch: would start lsyncd for %s → %s:%s",
                     local_project_root, worker_tunnel_ip, remote_path)
        # TODO: subprocess.Popen(["lsyncd", …])


# ---------------------------------------------------------------------------
# Remote worker environment (cloud-init template)
# ---------------------------------------------------------------------------

def build_worker_cloud_init(server_public_ip: str, server_wg_public_key: str,
                            worker_wg_private_key: str, tunnel_network: str = "10.242.0") -> str:
    """
    Generate a cloud-init YAML snippet that:
      1. Installs wireguard-tools + docker
      2. Writes /etc/wireguard/wg0.conf pointing back to the Phonos server
      3. Brings up wg0
      4. Starts the kernel gateway image

    Returns the YAML string to embed as VmSpec.user_data_script.
    """
    return f"""#cloud-config
package_update: true
packages:
  - wireguard-tools
  - docker.io
  - rsync

write_files:
  - path: /etc/wireguard/wg0.conf
    content: |
      [Interface]
      PrivateKey = {worker_wg_private_key}
      Address = {tunnel_network}.2/32

      [Peer]
      PublicKey = {server_wg_public_key}
      Endpoint = {server_public_ip}:51820
      AllowedIPs = {tunnel_network}.0/24
      PersistentKeepalive = 25

runcmd:
  - systemctl enable --now wg-quick@wg0
  - systemctl enable --now docker
  - sysctl -w net.ipv4.ip_forward=1
  - echo "Worker ready at $(hostname)" > /etc/motd
"""


# ---------------------------------------------------------------------------
# Convenience: provision + tunnel + sync in one shot
# ---------------------------------------------------------------------------

def provision_worker(provider: CloudProvider, spec: VmSpec,
                     tunnel: WireGuardTunnel, file_sync: FileSync,
                     server_public_ip: str) -> VmInstance:
    """
    High-level workflow:
      1. Generate server WireGuard keys.
      2. Generate worker WireGuard keys.
      3. Build cloud-init with worker's wg config → embed in VmSpec.
      4. Start the VM.
      5. Wait for it to be running.
      6. Bring up the server side of the tunnel.
      7. Wait for tunnel health check.
      8. Sync project files to the worker.
    Returns the VmInstance with tunnel IP populated.
    """
    logger.info("=== provision_worker: starting ===")

    # 1-2. Key generation
    server_keys = tunnel.generate_keypair()
    worker_keys = tunnel.generate_keypair()

    # 3. Cloud-init
    spec.user_data_script = build_worker_cloud_init(
        server_public_ip=server_public_ip,
        server_wg_public_key=server_keys["public_key"],
        worker_wg_private_key=worker_keys["private_key"],
    )

    # 4. Start VM
    vm = provider.start_instance(spec)

    # 5. Wait for running
    vm = provider.wait_until_running(vm.instance_id)

    # 6. Server tunnel up
    t_cfg = TunnelConfig(
        peer_public_key=worker_keys["public_key"],
        peer_endpoint=f"{vm.public_ip}:51820",
        tunnel_ip="10.242.0.1/32",
    )
    tunnel.up(t_cfg)

    # 7. Health check
    tunnel.health_check("10.242.0.2")

    # 8. File sync
    file_sync.sync_to_worker("/path/to/project", "10.242.0.2")

    logger.info("=== provision_worker: done (tunnel_ip=10.242.0.2) ===")
    return vm
