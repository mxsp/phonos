from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from config import Config


python_executable = sys.executable
production_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(production_dir)

env = os.environ.copy()
existing_pythonpath = env.get("PYTHONPATH", "")
path_parts = [production_dir, project_dir]
if existing_pythonpath:
    path_parts.append(existing_pythonpath)
env["PYTHONPATH"] = os.pathsep.join(path_parts)

WORKER_ID_OVERRIDES = {
    "boltz-2": "production/boltz2_worker.py",
    "rfdiffusion": "production/rfdiffusion_worker.py",
    "diffdock": "production/diffdock_worker.py",
    "openfold3": "production/openfold3_worker.py",
    "openfold2": "production/openfold2_worker.py",
    "alphafold2": "production/alphafold2_worker.py",
    "alphafold2-multimer": "production/alphafold2_multimer_worker.py",
    "esmfold": "production/esmfold_worker.py",
    "evo2-40b": "production/evo2_worker.py",
    "proteinmpnn": "production/proteinmpnn_worker.py",
    "molmim": "production/molmim_worker.py",
    "genmol": "production/genmol_worker.py",
}


def _script_exists(script_path: str) -> bool:
    if os.path.isabs(script_path):
        return os.path.isfile(script_path)
    return os.path.isfile(os.path.join(project_dir, script_path))


def _resolve_script_path(script_path: str) -> str:
    if os.path.isabs(script_path):
        return script_path
    return os.path.join(project_dir, script_path)


def _discover_worker_scripts() -> list[str]:
    """Discover worker scripts from configured model IDs with cross-platform overrides."""
    workers: list[str] = []
    seen: set[str] = set()

    for model in getattr(Config, "SUPPORTED_MODELS", []):
        model_id = str(model.get("id") or "").strip()
        if not model_id:
            continue

        candidates = []
        if model_id in WORKER_ID_OVERRIDES:
            candidates.append(WORKER_ID_OVERRIDES[model_id])
        candidates.append(f"production/{model_id.replace('-', '_')}_worker.py")
        candidates.append(f"production/{model_id.replace('-', '')}_worker.py")

        for candidate in candidates:
            if candidate in seen:
                break
            if _script_exists(candidate):
                workers.append(candidate)
                seen.add(candidate)
                break

    return workers


def _default_scripts() -> list[str]:
    core = ["production/mrs.py", "production/main.py"]
    for script in list(core):
        if not _script_exists(script):
            core.remove(script)

    run_workers = os.environ.get("RUN_WITH_WORKERS", "true").lower() not in {"0", "false", "no"}
    worker_scripts = _discover_worker_scripts() if run_workers else []
    return core + worker_scripts


def main() -> int:
    scripts_to_run = _default_scripts()
    if len(sys.argv) > 1:
        print("Executing scripts provided via command line.")
        scripts_to_run = sys.argv[1:]
    else:
        print("No scripts provided via command line. Using dynamic defaults.")
        print(f"Discovered {max(0, len(scripts_to_run) - 2)} worker script(s).")

    print("--- Phonos multi-service runner started ---")
    start_time = time.time()

    processes: list[subprocess.Popen] = []
    for script in scripts_to_run:
        print(f"Launching {script}...")
        try:
            script_path = _resolve_script_path(script)
            script_dir = os.path.dirname(script_path) or project_dir
            proc = subprocess.Popen([python_executable, script_path], env=env, cwd=script_dir)
            processes.append(proc)
        except FileNotFoundError:
            print(f"Error: Python executable '{python_executable}' not found.")
            return 1
        except Exception as exc:
            print(f"Error launching {script}: {exc}")

    if not processes:
        print("No scripts were launched. Exiting.")
        return 0

    def _shutdown(*_args):
        for proc in processes:
            if proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    print("\nAll specified scripts have been launched concurrently.\n")
    try:
        for process in processes:
            process.wait()
    except KeyboardInterrupt:
        _shutdown()
        for process in processes:
            try:
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    end_time = time.time()
    print("\n--- All scripts have finished ---")
    print(f"Total execution time: {end_time - start_time:.2f} seconds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
