#!/usr/bin/env python3
"""
Deployment script for new NVIDIA API workers
Sets up automatic startup, monitoring, and configuration management
"""
import os
import sys
import json
import time
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

# Repository root: PHONOS_DIR if set, otherwise derived from this file's
# location (scripts/utils/deploy_new_workers.py -> repo root is two levels up).
PHONOS_DIR = Path(os.environ.get('PHONOS_DIR', Path(__file__).resolve().parents[2]))

# Add production directory to path
sys.path.insert(0, str(PHONOS_DIR / 'production'))

# New workers configuration
NEW_WORKERS = {
    'alphafold2-re': {
        'script': 'alphafold2_re_worker.py',
        'config_file': 'node_config_alphafold2_re.json',
        'worker_type': 'alphafold2-re',
        'name': 'AlphaFold2-RE',
        'description': 'Enhanced protein structure prediction',
        'queue_type': 'alphafold2-re',
        'priority': 1,
        'max_instances': 2,
        'memory_gb': 8,
        'gpu_required': True
    },
    'alphafold2-re-multimer': {
        'script': 'alphafold2_re_multimer_worker.py',
        'config_file': 'node_config_alphafold2_re_multimer.json',
        'worker_type': 'alphafold2-re-multimer',
        'name': 'AlphaFold2-RE-Multimer',
        'description': 'Multi-protein complex prediction',
        'queue_type': 'alphafold2-re-multimer',
        'priority': 1,
        'max_instances': 1,
        'memory_gb': 16,
        'gpu_required': True
    },
    'boltz2-enhanced': {
        'script': 'boltz2_enhanced_worker.py',
        'config_file': 'node_config_boltz2_enhanced.json',
        'worker_type': 'boltz2-enhanced',
        'name': 'Boltz2-Enhanced',
        'description': 'Advanced biomolecular structure prediction',
        'queue_type': 'boltz2-enhanced',
        'priority': 1,
        'max_instances': 1,
        'memory_gb': 12,
        'gpu_required': True
    }
}

class WorkerManager:
    """Manages deployment and monitoring of new workers."""
    
    def __init__(self):
        _base = PHONOS_DIR
        self.workers_dir = _base / 'production'
        self.logs_dir = _base / 'production'
        self.pids_file = _base / 'production' / 'new_workers_pids.json'
        self.status_file = _base / 'production' / 'new_workers_status.json'
        
    def create_worker_config(self, worker_id: str, worker_config: Dict) -> str:
        """Create configuration file for a worker."""
        config_path = self.workers_dir / worker_config['config_file']
        
        # Generate worker ID with timestamp for uniqueness
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        unique_worker_id = f"{worker_config['worker_type']}-{os.getpid()}-{timestamp}"
        
        config_data = {
            'worker_id': unique_worker_id,
            'worker_type': worker_config['worker_type'],
            'organization_id': None,  # Public queue by default
            'private_key': None,  # Will be loaded from file if exists
            'nvidia_api_key': os.getenv('NVIDIA_API_KEY'),
            'log_level': 'INFO',
            'heartbeat_interval': 30,
            'max_retries': 3,
            'timeout_seconds': 600,
            'auto_restart': True,
            'health_check_interval': 60,
            'performance_monitoring': True,
            'resource_limits': {
                'max_memory_mb': worker_config['memory_gb'] * 1024,
                'max_cpu_percent': 80,
                'gpu_memory_mb': worker_config['memory_gb'] * 1024 if worker_config['gpu_required'] else 0
            }
        }
        
        # Write config file
        with open(config_path, 'w') as f:
            json.dump(config_data, f, indent=2)
        
        print(f"✓ Created config: {config_path}")
        print(f"  Worker ID: {unique_worker_id}")
        return unique_worker_id
    
    def start_worker(self, worker_id: str, worker_config: Dict) -> Optional[subprocess.Popen]:
        """Start a worker process."""
        script_path = self.workers_dir / worker_config['script']
        config_path = self.workers_dir / worker_config['config_file']
        
        cmd = [
            sys.executable,
            str(script_path),
            '--worker-id', worker_id,
            '--config', str(config_path)
        ]
        
        # Set up environment variables
        env = os.environ.copy()
        env['PYTHONPATH'] = str(self.workers_dir)
        
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(self.workers_dir),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
            
            print(f"✓ Started worker: {worker_config['name']} (PID: {process.pid})")
            return process
            
        except Exception as e:
            print(f"✗ Failed to start {worker_config['name']}: {e}")
            return None
    
    def monitor_worker(self, process: subprocess.Popen, worker_config: Dict):
        """Monitor a worker process."""
        if not process:
            return
            
        worker_name = worker_config['name']
        max_restarts = 3
        restart_count = 0
        
        while restart_count < max_restarts:
            try:
                # Check if process is still running
                return_code = process.poll()
                if return_code is None:
                    # Process is running, check logs for errors
                    time.sleep(worker_config['priority'] * 30)  # Check based on priority
                    continue
                else:
                    print(f"⚠ {worker_name} worker exited with code: {return_code}")
                    
                    if return_code != 0:
                        # Check for error patterns in logs
                        log_file = self.logs_dir / f"{worker_config['worker_type']}_{process.pid}.log"
                        if log_file.exists():
                            with open(log_file, 'r') as f:
                                recent_logs = f.readlines()[-20:]  # Last 20 lines
                                error_patterns = ['ERROR', 'CRITICAL', 'FATAL', 'Exception', 'timeout']
                                error_found = any(pattern in line for line in recent_logs for pattern in error_patterns)
                                
                                if error_found:
                                    print(f"⚠ {worker_name}: Errors detected in logs, restarting...")
                                    restart_count += 1
                                    break
                        else:
                            print(f"⚠ {worker_name}: No log file found, restarting...")
                            restart_count += 1
                    else:
                        print(f"⚠ {worker_name}: Clean exit, not restarting")
                        return
                    
                    if restart_count < max_restarts:
                        print(f"🔄 Restarting {worker_name} (attempt {restart_count + 1}/{max_restarts})...")
                        new_process = self.start_worker(f"{worker_config['worker_type']}-restarted", worker_config)
                        if new_process:
                            process = new_process
                        else:
                            print(f"✗ Failed to restart {worker_name}")
                            return
                    else:
                        print(f"✓ {worker_name} restarted successfully")
                        
                time.sleep(5)  # Brief pause before next check
                
            except Exception as e:
                print(f"✗ Monitor error for {worker_name}: {e}")
                time.sleep(10)
                continue
    
    def save_worker_status(self, workers: Dict[str, Dict]):
        """Save current worker status."""
        status_data = {
            'timestamp': time.time(),
            'workers': {}
        }
        
        for worker_type, info in workers.items():
            status_data['workers'][worker_type] = {
                'name': info['config']['name'],
                'pid': info['pid'],
                'status': info['status'],
                'started_at': info['started_at'],
                'restarts': info.get('restarts', 0),
                'memory_usage_mb': 0,  # Will be updated by monitoring
                'cpu_usage_percent': 0
            }
        
        with open(self.status_file, 'w') as f:
            json.dump(status_data, f, indent=2)
    
    def start_all_workers(self, selected_workers: List[str] = None):
        """Start all new workers or specific selection."""
        if selected_workers is None:
            selected_workers = list(NEW_WORKERS.keys())
        
        print(f"🚀 Starting workers: {', '.join(selected_workers)}")
        print("=" * 60)
        
        active_workers = {}
        
        for worker_type in selected_workers:
            if worker_type not in NEW_WORKERS:
                print(f"⚠ Unknown worker type: {worker_type}")
                continue
                
            worker_config = NEW_WORKERS[worker_type]
            
            # Check if worker is already running
            if self.is_worker_running(worker_type):
                print(f"⚠ {worker_config['name']} is already running")
                continue
            
            # Create config and start worker
            worker_id = self.create_worker_config(worker_type, worker_config)
            process = self.start_worker(worker_id, worker_config)
            
            if process:
                active_workers[worker_type] = {
                    'config': worker_config,
                    'process': process,
                    'pid': process.pid,
                    'status': 'running',
                    'started_at': time.time()
                }
        
        # Save status
        self.save_worker_status(active_workers)
        
        # Start monitoring in background
        for worker_type, info in active_workers.items():
            import threading
            monitor_thread = threading.Thread(
                target=self.monitor_worker,
                args=(info['process'], info['config']),
                daemon=True
            )
            monitor_thread.start()
        
        print("=" * 60)
        print(f"✓ Started {len(active_workers)} workers")
        print("Monitoring active...")
        
        # Save PIDs for management
        self.save_pids(active_workers)
        
        return active_workers
    
    def is_worker_running(self, worker_type: str) -> bool:
        """Check if a worker type is already running."""
        if not self.pids_file.exists():
            return False
            
        try:
            with open(self.pids_file, 'r') as f:
                pids_data = json.load(f)
                worker_info = pids_data.get('active_workers', {}).get(worker_type)
                if not worker_info:
                    return False
                pid = worker_info.get('pid')
                if not pid:
                    return False
                try:
                    os.kill(int(pid), 0)
                except (ProcessLookupError, ValueError, TypeError):
                    return False
                return True
        except:
            return False
    
    def save_pids(self, workers: Dict[str, Dict]):
        """Save worker PIDs for management."""
        pids_data = {
            'timestamp': time.time(),
            'active_workers': {}
        }
        
        for worker_type, info in workers.items():
            pids_data['active_workers'][worker_type] = {
                'pid': info['pid'],
                'name': info['config']['name'],
                'started_at': info['started_at']
            }
        
        with open(self.pids_file, 'w') as f:
            json.dump(pids_data, f, indent=2)
    
    def stop_workers(self, worker_types: List[str] = None):
        """Stop specific or all workers."""
        if worker_types is None:
            worker_types = list(NEW_WORKERS.keys())
        
        if not self.pids_file.exists():
            print("⚠ No PID file found, cannot stop workers")
            return
        
        try:
            with open(self.pids_file, 'r') as f:
                pids_data = json.load(f)
                active_workers = pids_data.get('active_workers', {})
        except:
            print("⚠ Could not read PID file")
            return
        
        stopped_count = 0
        for worker_type in worker_types:
            if worker_type in active_workers:
                pid = active_workers[worker_type]['pid']
                try:
                    os.kill(pid, 15)  # SIGTERM
                    print(f"✓ Stopped {worker_type} (PID: {pid})")
                    stopped_count += 1
                except ProcessLookupError:
                    print(f"⚠ Worker {worker_type} (PID: {pid}) not found")
                except Exception as e:
                    print(f"✗ Error stopping {worker_type}: {e}")
        
        # Clear stopped workers from PID file
        if stopped_count > 0:
            remaining_workers = {k: v for k, v in active_workers.items() if k not in worker_types}
            pids_data['active_workers'] = remaining_workers
            
            with open(self.pids_file, 'w') as f:
                json.dump(pids_data, f, indent=2)
    
    def show_status(self):
        """Display current worker status."""
        print("=" * 60)
        print("📊 New Workers Status")
        print("=" * 60)
        
        if not self.status_file.exists():
            print("⚠ No status file found")
            return
        
        try:
            with open(self.status_file, 'r') as f:
                status_data = json.load(f)
                
            if not status_data.get('workers'):
                print("⚠ No active workers")
                return
            
            for worker_type, info in status_data['workers'].items():
                name = info['name']
                status = info['status']
                pid = info['pid']
                started_at = info['started_at']
                restarts = info.get('restarts', 0)
                
                # Calculate uptime
                uptime_seconds = time.time() - started_at
                uptime_hours = uptime_seconds / 3600
                
                print(f"🔧 {name}")
                print(f"   Status: {status}")
                print(f"   PID: {pid}")
                print(f"   Uptime: {uptime_hours:.1f}h")
                print(f"   Restarts: {restarts}")
                print()
                
        except Exception as e:
            print(f"✗ Error reading status: {e}")


def main():
    """Main deployment interface."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Deploy and manage new NVIDIA API workers')
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Start command
    start_parser = subparsers.add_parser('start', help='Start workers')
    start_parser.add_argument('--workers', nargs='+', help='Specific workers to start (default: all)')
    
    # Stop command
    stop_parser = subparsers.add_parser('stop', help='Stop workers')
    stop_parser.add_argument('--workers', nargs='+', help='Specific workers to stop (default: all)')
    
    # Status command
    subparsers.add_parser('status', help='Show worker status')
    
    # Deploy command (start with monitoring)
    deploy_parser = subparsers.add_parser('deploy', help='Deploy workers with monitoring')
    deploy_parser.add_argument('--workers', nargs='+', help='Specific workers to deploy (default: all)')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    manager = WorkerManager()
    
    try:
        if args.command == 'start':
            manager.start_all_workers(args.workers)
        elif args.command == 'stop':
            manager.stop_workers(args.workers)
        elif args.command == 'status':
            manager.show_status()
        elif args.command == 'deploy':
            print("🚀 Deploying workers with automatic monitoring...")
            active_workers = manager.start_all_workers(args.workers)
            
            # Keep script running for monitoring
            try:
                while True:
                    time.sleep(60)  # Check status every minute
                    # Update status file with current system metrics
                    manager.save_worker_status(active_workers)
            except KeyboardInterrupt:
                print("\n🛑 Shutdown requested...")
                manager.stop_workers()
        else:
            parser.print_help()
            
    except KeyboardInterrupt:
        print("\n🛑 Operation cancelled")
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
