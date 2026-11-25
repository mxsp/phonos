from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, TextIO

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk


@dataclass(frozen=True)
class ModelOption:
    """Definition of a locally runnable worker model."""

    name: str
    script: Path
    description: str = ""

    def command(self, python_executable: str) -> List[str]:
        return [python_executable, "-u", str(self.script)]


class ModelRegistry:
    """Keeps track of the models that can be started from the public node UI."""

    _script_directory = Path(__file__).resolve().parent

    _LOCAL_MODEL_SCRIPTS: Dict[str, str] = {
        "ESM Fold": "node_esm.py",
        "AlphaFold": "node2-alphafold.py",
        "RoseTTAFold": "node3-rosettafold.py",
    }

    def __init__(self, python_executable: Optional[str] = None) -> None:
        self.python_executable = python_executable or sys.executable
        self._models: Dict[str, ModelOption] = {}
        self._discover_models()

    def names(self) -> Iterable[str]:
        return self._models.keys()

    def get(self, name: str) -> Optional[ModelOption]:
        return self._models.get(name)

    def _discover_models(self) -> None:
        for display_name, script_name in self._LOCAL_MODEL_SCRIPTS.items():
            script_path = self._script_directory / script_name
            if not script_path.exists():
                continue
            self._models[display_name] = ModelOption(
                name=display_name,
                script=script_path,
                description=f"Launches the {display_name} local worker",
            )

        if not self._models:
            raise RuntimeError(
                "No local worker scripts were discovered. Ensure the node scripts "
                "are present in the application directory."
            )


class NodeProcessController:
    """Starts and stops the model worker subprocess."""

    def __init__(self, model_registry: ModelRegistry) -> None:
        self._registry = model_registry
        self._process: Optional[subprocess.Popen[str]] = None
        self._log_queue: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.Lock()

    @property
    def log_queue(self) -> "queue.Queue[str]":
        return self._log_queue

    def start(self, model_name: str) -> ModelOption:
        with self._lock:
            if self._process and self._process.poll() is None:
                raise RuntimeError("A node is already running. Stop it before launching a new one.")

            model = self._registry.get(model_name)
            if not model:
                raise RuntimeError(f"Unknown model: {model_name}")
            if not model.script.exists():
                raise RuntimeError(f"Worker script not found: {model.script}")

            python_executable = self._registry.python_executable
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

            self._process = subprocess.Popen(
                model.command(python_executable),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                creationflags=creation_flags,
            )

            assert self._process.stdout is not None
            assert self._process.stderr is not None
            threading.Thread(
                target=self._enqueue_output,
                args=(self._process.stdout,),
                daemon=True,
            ).start()
            threading.Thread(
                target=self._enqueue_output,
                args=(self._process.stderr,),
                daemon=True,
            ).start()

            return model

    def stop(self) -> None:
        with self._lock:
            if not self._process:
                return

            if self._process.poll() is None:
                self._log_queue.put("--- Stopping node... ---\n")
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._log_queue.put("--- Node did not terminate gracefully, killing. ---\n")
                    self._process.kill()

            self._process = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _enqueue_output(self, stream: TextIO) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._log_queue.put(line)
        finally:
            stream.close()


class PublicNodeApp:
    """Tkinter UI that manages a local compute node."""

    def __init__(self, root: tk.Tk, registry: Optional[ModelRegistry] = None) -> None:
        self.root = root
        self.root.title("Phonos Local Compute Node")
        self.registry = registry or ModelRegistry()
        self.process_controller = NodeProcessController(self.registry)

        self.main_frame = ttk.Frame(root, padding="12")
        self.main_frame.grid(row=0, column=0, sticky="nsew")

        ttk.Label(self.main_frame, text="Select model:").grid(row=0, column=0, sticky="w")
        self.model_var = tk.StringVar(value=self._default_model())
        self.model_selector = ttk.Combobox(
            self.main_frame,
            textvariable=self.model_var,
            values=list(self.registry.names()),
            state="readonly",
        )
        self.model_selector.grid(row=0, column=1, columnspan=2, sticky="ew", pady=6)

        self.start_button = ttk.Button(self.main_frame, text="Start Node", command=self.start_node)
        self.start_button.grid(row=1, column=0, sticky="ew", pady=6)

        self.stop_button = ttk.Button(
            self.main_frame,
            text="Stop Node",
            command=self.stop_node,
            state=tk.DISABLED,
        )
        self.stop_button.grid(row=1, column=1, sticky="ew", pady=6)

        help_text = (
            "Run any supported Phonos model locally. Choose a model and press \"Start Node\" "
            "to register your machine as a compute worker."
        )
        ttk.Label(self.main_frame, text=help_text, wraplength=360, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(0, 6)
        )

        log_frame = ttk.LabelFrame(self.main_frame, text="Logs", padding="8")
        log_frame.grid(row=3, column=0, columnspan=3, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_area = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            state=tk.DISABLED,
            height=18,
        )
        self.log_area.grid(row=0, column=0, sticky="nsew")

        self.status_var = tk.StringVar(value="Status: Idle")
        ttk.Label(self.main_frame, textvariable=self.status_var).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )

        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        self.main_frame.columnconfigure(1, weight=1)
        self.main_frame.rowconfigure(3, weight=1)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self._refresh_logs)

    def _default_model(self) -> str:
        try:
            return next(iter(self.registry.names()))
        except StopIteration:
            return ""

    def start_node(self) -> None:
        model_name = self.model_var.get()
        if not model_name:
            messagebox.showerror("No model selected", "Please choose a model to start.")
            return

        try:
            model = self.process_controller.start(model_name)
        except Exception as exc:  # pylint: disable=broad-except
            messagebox.showerror("Failed to start node", str(exc))
            return

        self._append_log(f"--- Starting {model.name} node ---\n")
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.model_selector.config(state=tk.DISABLED)
        self.status_var.set(f"Status: Running {model.name}")

    def stop_node(self) -> None:
        self.process_controller.stop()
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.model_selector.config(state="readonly")
        self.status_var.set("Status: Idle")

    def on_close(self) -> None:
        if self.process_controller.is_running():
            if not messagebox.askyesno(
                "Node is running",
                "A worker is still running. Do you want to stop it and exit?",
            ):
                return
            self.stop_node()
        self.root.destroy()

    def _append_log(self, text: str) -> None:
        self.log_area.config(state=tk.NORMAL)
        self.log_area.insert(tk.END, text)
        self.log_area.see(tk.END)
        self.log_area.config(state=tk.DISABLED)

    def _refresh_logs(self) -> None:
        q = self.process_controller.log_queue
        while not q.empty():
            try:
                self._append_log(q.get_nowait())
            except queue.Empty:
                break
        self.root.after(100, self._refresh_logs)


def main() -> None:
    root = tk.Tk()
    PublicNodeApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
