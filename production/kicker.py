import logging
import os
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from tkinter import ttk
from typing import Any, Optional

import json
import redis
import requests
import sqlite3
import time
import webbrowser

from config import Config
from database import get_db_connection
from host_resolution import resolve_host
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from mrs_service.runtime import (
    ACTIVE_JOBS_HASH,
    WORKER_HEALTH_HASH,
    infer_model_id_from_active_job_state,
)
from model_overrides import (
    apply_overrides_to_models,
    fetch_model_overrides,
    upsert_model_override,
)
from secure_tunnel import decrypt_payload, derive_tunnel_key, encrypt_payload


logger = logging.getLogger(__name__)


class ScrollableFrame(ttk.Frame):
    """A scrollable frame with mouse wheel support.

    This frame wraps content in a canvas with scrollbars, providing
    both vertical and optional horizontal scrolling with mouse wheel support.
    """

    def __init__(self, parent, horizontal_scroll=False, **kwargs):
        super().__init__(parent, **kwargs)

        # Create canvas and scrollbars
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)

        # Configure canvas scrolling
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)

        # Optional horizontal scrollbar
        if horizontal_scroll:
            self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
            self.canvas.configure(xscrollcommand=self.hsb.set)
            self.hsb.pack(side="bottom", fill="x")

        # Pack canvas and scrollbar
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")

        # Bind mouse wheel events
        self._bind_mouse_wheel()

        # Bind canvas resize to update scrollable frame width
        self.canvas.bind('<Configure>', self._on_canvas_configure)

    def _on_canvas_configure(self, event):
        """Update scrollable frame width to match canvas width."""
        canvas_width = event.width
        self.canvas.itemconfig(self.canvas_window, width=canvas_width)

    def _bind_mouse_wheel(self):
        """Bind mouse wheel events for scrolling."""
        # Bind to canvas and scrollable frame
        self.canvas.bind_all("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind_all("<Button-4>", self._on_mouse_wheel)  # Linux scroll up
        self.canvas.bind_all("<Button-5>", self._on_mouse_wheel)  # Linux scroll down

    def _on_mouse_wheel(self, event):
        """Handle mouse wheel scrolling."""
        # Check if mouse is over this widget
        if not self._is_mouse_over():
            return

        # Scroll based on platform
        if event.num == 4 or event.delta > 0:
            self.canvas.yview_scroll(-1, "units")
        elif event.num == 5 or event.delta < 0:
            self.canvas.yview_scroll(1, "units")

    def _is_mouse_over(self):
        """Check if mouse is currently over this widget."""
        x, y = self.winfo_pointerxy()
        widget = self.winfo_containing(x, y)

        # Check if widget is this canvas or a child of scrollable_frame
        if widget is None:
            return False

        current = widget
        while current:
            if current == self.canvas or current == self.scrollable_frame:
                return True
            try:
                current = current.master
            except:
                break
        return False


@dataclass(slots=True)
class TunnelSnapshot:
    """Container for remote snapshot responses."""

    payload: dict


class SecureTunnel:
    """Create an encrypted tunnel between the local kicker and the remote server.

    The tunnel prefers the public Phonos endpoint and gracefully falls back to
    localhost for development use. Payloads are wrapped using AES-GCM with a
    128-bit key derived from :class:`Config` so that sensitive monitoring data
    is protected end-to-end.
    """

    _PREFERRED_HOST = "phonos.fyreflysystems.de"
    _FALLBACK_HOSTS = ("127.0.0.1", "localhost")
    _TIMEOUT = 5
    _SNAPSHOT_PATH = "/api/internal/kicker-snapshot"
    _HELPDESK_PATH = "/admin/helpdesk"
    _ENV_HOST_OVERRIDE = "PHONOS_KICKER_HOST"
    _ENV_DISABLE_REMOTE = "PHONOS_KICKER_DISABLE_TUNNEL"
    _ENV_TLS_VERIFY = "PHONOS_KICKER_VERIFY_TLS"

    def __init__(self) -> None:
        self._logger = logger.getChild("SecureTunnel")
        host_override = os.environ.get(self._ENV_HOST_OVERRIDE)
        disable_remote = self._resolve_env_flag(
            self._ENV_DISABLE_REMOTE,
            default=False,
            log_message="Secure tunnel forced into local-only mode via %s.",
        )

        if host_override:
            self._logger.info(
                "Using host override from %s: %s",
                self._ENV_HOST_OVERRIDE,
                host_override,
            )

        preferred_host = host_override or self._PREFERRED_HOST
        fallback_hosts: tuple[str, ...]

        if disable_remote:
            preferred_host = self._FALLBACK_HOSTS[0]
            fallback_hosts = self._FALLBACK_HOSTS[1:]
        else:
            fallback_hosts = self._FALLBACK_HOSTS

        self._host = resolve_host(
            preferred_host,
            fallback_hosts,
            logger=self._logger,
            context="kicker tunnel",
        )

        self._is_remote = not disable_remote and self._host not in self._FALLBACK_HOSTS
        self._base_url = self._determine_base_url()
        self._session = requests.Session()
        self._configure_tls_verification(self._session)
        self._aes_key = derive_tunnel_key()

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def helpdesk_url(self) -> str:
        return f"{self._base_url}{self._HELPDESK_PATH}"

    @property
    def is_remote(self) -> bool:
        return self._is_remote

    def fetch_snapshot(self) -> Optional[TunnelSnapshot]:
        """Retrieve an encrypted snapshot from the remote server.

        The method transparently decrypts responses and returns a
        :class:`TunnelSnapshot` when remote communication succeeds. ``None`` is
        returned when the tunnel cannot be established so the caller can fall
        back to local data sources.
        """

        if not self._is_remote:
            return None

        endpoint = f"{self._base_url}{self._SNAPSHOT_PATH}"
        payload = {"request": "kicker.snapshot", "issued_at": time.time()}
        envelope = {"payload": encrypt_payload(payload, key=self._aes_key, logger=self._logger)}

        try:
            response = self._session.post(
                endpoint,
                json=envelope,
                timeout=self._TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            self._logger.warning(
                "Secure tunnel request to %s failed: %s. Falling back to local data.",
                endpoint,
                exc,
            )
            return None

        try:
            body = response.json()
        except json.JSONDecodeError:
            self._logger.warning("Secure tunnel returned non-JSON response.")
            return None

        encrypted_payload = body.get("payload")
        if encrypted_payload is None:
            # Allow plain JSON for backwards compatibility with installations
            # that do not yet support the encrypted tunnel endpoint.
            if isinstance(body, dict):
                payload = body.get("data") if isinstance(body.get("data"), dict) else body
                return TunnelSnapshot(payload=payload)
            self._logger.warning("Secure tunnel response was not a dictionary.")
            return None

        decrypted = decrypt_payload(encrypted_payload, key=self._aes_key, logger=self._logger)
        if decrypted is None:
            self._logger.warning("Failed to decrypt secure tunnel response.")
            return None
        if "data" in decrypted and isinstance(decrypted["data"], dict):
            return TunnelSnapshot(payload=decrypted["data"])
        return TunnelSnapshot(payload=decrypted)

    def _determine_base_url(self) -> str:
        if self._is_remote:
            return f"https://{self._host}"
        port = int(os.environ.get("PHONOS_LOCAL_PORT", 5000))
        scheme = "http"
        return f"{scheme}://{self._host}:{port}"

    def _resolve_env_flag(
        self,
        name: str,
        *,
        default: bool,
        log_message: Optional[str] = None,
    ) -> bool:
        """Return a boolean flag from environment variable ``name``.

        ``default`` indicates the value to use when the variable is unset or
        invalid. When ``log_message`` is provided and the resolved flag is
        ``True``, it is logged using :mod:`logging` formatting semantics.
        """

        raw_value = os.environ.get(name)
        if raw_value is None:
            return default

        value = raw_value.strip().lower()
        if value in {"", "1", "true", "yes", "on"}:
            resolved = True
        elif value in {"0", "false", "no", "off"}:
            resolved = False
        else:
            self._logger.warning(
                "Environment variable %s has unrecognised value '%s'; defaulting to %s.",
                name,
                raw_value,
                default,
            )
            resolved = default

        if resolved and log_message:
            self._logger.info(log_message, name)
        return resolved

    def _configure_tls_verification(self, session: requests.Session) -> None:
        """Configure TLS verification behaviour for ``session``.

        The behaviour can be customised with ``PHONOS_KICKER_VERIFY_TLS``. The
        variable accepts case-insensitive boolean strings or a filesystem path
        to a custom CA bundle understood by :mod:`requests`.
        """

        raw_value = os.environ.get(self._ENV_TLS_VERIFY)
        if raw_value is None:
            session.verify = True
            return

        value = raw_value.strip()
        lowered = value.lower()

        if lowered in {"", "1", "true", "yes", "on"}:
            session.verify = True
            self._logger.info(
                "TLS verification explicitly enabled via %s.",
                self._ENV_TLS_VERIFY,
            )
            return

        if lowered in {"0", "false", "no", "off"}:
            session.verify = False
            self._logger.warning(
                "TLS verification disabled via %s. Encrypted tunnel authenticity cannot be guaranteed.",
                self._ENV_TLS_VERIFY,
            )
            return

        if os.path.exists(value):
            session.verify = value
            self._logger.info(
                "Using custom CA bundle for secure tunnel TLS verification: %s",
                value,
            )
            return

        self._logger.warning(
            "Value '%s' supplied for %s does not point to an accessible file. Falling back to default verification.",
            raw_value,
            self._ENV_TLS_VERIFY,
        )
        session.verify = True

# --- Application ---
class KickerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Phonos System Monitor")
        self.geometry("1200x800")

        self.tunnel = SecureTunnel()
        if self.tunnel.is_remote:
            logger.info("Using secure tunnel to %s", self.tunnel.base_url)
        else:
            logger.info("Secure tunnel resolved to local host %s", self.tunnel.base_url)

        # --- Redis Connection ---
        try:
            self.redis_conn = redis.Redis(
                host=Config.REDIS_HOST,
                port=Config.REDIS_PORT,
                db=Config.REDIS_DB,
                decode_responses=True
            )
            self.redis_conn.ping()
            print("Successfully connected to Redis.")
        except redis.exceptions.ConnectionError as e:
            print(f"Error connecting to Redis: {e}")
            self.redis_conn = None

        # --- Database Connection ---
        try:
            self.db_conn = get_db_connection()
            print("Successfully connected to the database.")
        except sqlite3.Error as e:
            print(f"Error connecting to database: {e}")
            self.db_conn = None

        self.feature_flags: dict[str, Any] = {}
        self.premium_tab: Optional[ttk.Frame] = None
        self.premium_tree: Optional[ttk.Treeview] = None
        self.premium_summary_label: Optional[ttk.Label] = None

        self.model_states = self._load_model_states()
        self.create_widgets()
        self.update_data()

    def create_widgets(self):
        """Create the main widgets for the application."""
        # --- Main Tab Control ---
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(expand=True, fill='both', padx=10, pady=10)

        # --- Tab 1: System Monitor ---
        self.monitor_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.monitor_tab, text='System Monitor')
        self.create_monitor_tab(self.monitor_tab)

        # --- Tab 2: Database Editor ---
        self.db_editor_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.db_editor_tab, text='Database Editor')
        self.create_db_editor_tab(self.db_editor_tab)

        # --- Tab 3: Advanced Statistics ---
        self.advanced_stats_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.advanced_stats_tab, text='Advanced Statistics')
        self.create_advanced_stats_tab(self.advanced_stats_tab)

        # --- Tab 4: Help Desk ---
        self.help_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.help_tab, text='Help Desk')
        self.create_help_desk_tab(self.help_tab)

    def create_help_desk_tab(self, parent_tab):
        """Create the widgets for the Help Desk tab."""
        # Create scrollable container for future expansion
        help_scrollable = ScrollableFrame(parent_tab)
        help_scrollable.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        help_frame = help_scrollable.scrollable_frame

        # --- Center the content ---
        center_frame = ttk.Frame(help_frame)
        center_frame.pack(expand=True, fill=tk.BOTH)

        # Title
        title_label = ttk.Label(center_frame, text="Help Desk", font=("Helvetica", 16, "bold"))
        title_label.pack(pady=(20, 10))

        ttk.Label(center_frame, text="The help desk has been migrated to the web interface.", font=("Helvetica", 12)).pack(pady=10)

        # --- Button to open web interface ---
        ttk.Button(center_frame, text="Open Web Help Desk", command=self.open_web_help_desk).pack(pady=20)

        # Additional help information (expandable in future)
        info_frame = ttk.LabelFrame(center_frame, text="Quick Help", padding="10")
        info_frame.pack(pady=20, padx=20, fill=tk.BOTH)

        help_text = """
        System Monitor:
        - View compute node status and system statistics
        - Monitor active jobs and worker health
        - Visualize system performance metrics

        Database Editor:
        - Browse and edit database tables
        - Add, edit, and delete records
        - Manage system data directly

        Advanced Statistics:
        - View detailed system metrics
        - Track user activity and job statistics
        - Monitor system performance over time
        """

        info_label = ttk.Label(info_frame, text=help_text, justify=tk.LEFT, wraplength=500)
        info_label.pack(pady=10)

    def open_web_help_desk(self):
        """Opens the admin help desk URL in the default web browser."""
        url = self.tunnel.helpdesk_url
        webbrowser.open_new_tab(url)

    def create_monitor_tab(self, parent_tab):
        """Create the widgets for the System Monitor tab."""
        # Create main scrollable container
        main_scrollable = ScrollableFrame(parent_tab)
        main_scrollable.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Use the scrollable_frame as the container
        main_frame = main_scrollable.scrollable_frame

        # --- Left Frame for Matrix and Stats (also scrollable) ---
        left_scrollable = ScrollableFrame(main_frame)
        left_scrollable.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        left_frame = left_scrollable.scrollable_frame

        # --- Compute Node Matrix ---
        matrix_frame = ttk.LabelFrame(left_frame, text="Compute Node Status", padding="10")
        matrix_frame.pack(fill=tk.X, pady=(0, 10))
        self.create_matrix(matrix_frame)

        # --- General Statistics ---
        stats_frame = ttk.LabelFrame(left_frame, text="System Statistics", padding="10")
        stats_frame.pack(fill=tk.X)
        self.create_stats(stats_frame)

        # --- Right Frame for Graphs (also scrollable) ---
        right_scrollable = ScrollableFrame(main_frame)
        right_scrollable.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        graph_frame = ttk.LabelFrame(right_scrollable.scrollable_frame, text="Visualizations", padding="10")
        graph_frame.pack(fill=tk.BOTH, expand=True)
        self.create_graphs(graph_frame)

    def create_db_editor_tab(self, parent_tab):
        """Create the widgets for the Database Editor tab."""
        # Create scrollable container for the entire tab
        db_scrollable = ScrollableFrame(parent_tab)
        db_scrollable.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        db_frame = db_scrollable.scrollable_frame

        # --- Top frame for table selection ---
        top_frame = ttk.Frame(db_frame)
        top_frame.pack(fill=tk.X, pady=(0, 5))

        ttk.Label(top_frame, text="Select Table:").pack(side=tk.LEFT, padx=(0, 5))
        self.table_selector = ttk.Combobox(top_frame, state="readonly")
        self.table_selector.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.table_selector.bind("<<ComboboxSelected>>", self.load_table_data)

        # --- Treeview for displaying table data ---
        tree_frame = ttk.Frame(db_frame)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        self.db_tree = ttk.Treeview(tree_frame, show="headings")

        # Scrollbars
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.db_tree.yview)
        vsb.pack(side='right', fill='y')
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.db_tree.xview)
        hsb.pack(side='bottom', fill='x')
        self.db_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.db_tree.pack(fill=tk.BOTH, expand=True)

        # --- Bottom frame for buttons ---
        button_frame = ttk.Frame(db_frame)
        button_frame.pack(fill=tk.X, pady=(5, 0))

        ttk.Button(button_frame, text="Add Record", command=self.add_record_window).pack(side=tk.LEFT, padx=2)
        ttk.Button(button_frame, text="Edit Record", command=self.edit_record_window).pack(side=tk.LEFT, padx=2)
        ttk.Button(button_frame, text="Delete Record", command=self.delete_record).pack(side=tk.LEFT, padx=2)

        # --- Populate the table selector ---
        self.load_table_names()

    def create_advanced_stats_tab(self, parent_tab):
        """Create the widgets for the Advanced Statistics tab."""
        # Create scrollable container using ScrollableFrame class
        stats_scrollable = ScrollableFrame(parent_tab)
        stats_scrollable.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        stats_frame = ttk.LabelFrame(stats_scrollable.scrollable_frame, text="Detailed System Metrics", padding="10")
        stats_frame.pack(fill=tk.BOTH, expand=True)

        self.advanced_stats_labels = {}
        stat_definitions = {
            "total_users": "Total Registered Users:",
            "jobs_completed": "Jobs Completed:",
            "jobs_failed": "Jobs Failed:",
            "jobs_queued": "Jobs Queued:",
            "job_success_rate": "Job Success Rate:",
            "avg_processing_time": "Avg. Processing Time (s):",
            "total_projects": "Total Projects:",
            "total_files": "Total Files:",
        }

        row = 0
        for key, text in stat_definitions.items():
            ttk.Label(stats_frame, text=text, font=("Helvetica", 10, "bold")).grid(row=row, column=0, sticky="w", pady=2)
            label = ttk.Label(stats_frame, text="N/A")
            label.grid(row=row, column=1, sticky="w", padx=5)
            self.advanced_stats_labels[key] = label
            row += 1

    def create_premium_uptime_tab(self, parent_tab: ttk.Frame) -> None:
        """Initialise widgets for the premium uptime dashboard."""
        # Create scrollable container for the premium tab
        premium_scrollable = ScrollableFrame(parent_tab)
        premium_scrollable.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        container = premium_scrollable.scrollable_frame

        ttk.Label(
            container,
            text="Premium Node Uptime",
            font=("Helvetica", 12, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        description = (
            "Track the live uptime of each premium-enabled compute node."
            " Values update automatically while the monitor is running."
        )
        ttk.Label(container, text=description, wraplength=700, justify="left").pack(anchor="w", pady=(0, 12))

        tree_container = ttk.Frame(container)
        tree_container.pack(fill=tk.BOTH, expand=True)

        columns = ("worker_id", "model", "uptime", "last_seen")
        self.premium_tree = ttk.Treeview(tree_container, columns=columns, show="headings")
        self.premium_tree.heading("worker_id", text="Worker ID")
        self.premium_tree.heading("model", text="Model")
        self.premium_tree.heading("uptime", text="Uptime")
        self.premium_tree.heading("last_seen", text="Last Seen")

        self.premium_tree.column("worker_id", width=180, anchor="w")
        self.premium_tree.column("model", width=200, anchor="w")
        self.premium_tree.column("uptime", width=140, anchor="center")
        self.premium_tree.column("last_seen", width=200, anchor="center")

        vsb = ttk.Scrollbar(tree_container, orient="vertical", command=self.premium_tree.yview)
        self.premium_tree.configure(yscrollcommand=vsb.set)
        self.premium_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        summary_frame = ttk.Frame(container)
        summary_frame.pack(fill=tk.X, pady=(12, 0))
        self.premium_summary_label = ttk.Label(summary_frame, text="")
        self.premium_summary_label.pack(anchor="w")

    def create_matrix(self, parent_frame):
        """Create the grid for the compute node matrix."""
        self.matrix_labels = {}
        headers = ["Model", "Needed", "Online", "Active Jobs", "Active", "Visible"]
        for col, header in enumerate(headers):
            label = ttk.Label(parent_frame, text=header, font=("Helvetica", 10, "bold"))
            label.grid(row=0, column=col, padx=5, pady=5, sticky="w")

        for i, model in enumerate(Config.SUPPORTED_MODELS):
            row = i + 1
            model_name = model['name']
            model_id = model['id']

            name_label = ttk.Label(parent_frame, text=model_name)
            name_label.grid(row=row, column=0, padx=5, pady=2, sticky="w")

            needed_label = ttk.Label(parent_frame, text="0")
            needed_label.grid(row=row, column=1, padx=5, pady=2)

            online_label = ttk.Label(parent_frame, text="0")
            online_label.grid(row=row, column=2, padx=5, pady=2)

            active_jobs_label = ttk.Label(parent_frame, text="0")
            active_jobs_label.grid(row=row, column=3, padx=5, pady=2)

            state = self.model_states.get(model_id, {"is_active": True, "is_visible": True})
            active_var = tk.BooleanVar(value=state.get("is_active", True))
            visible_var = tk.BooleanVar(value=state.get("is_visible", True))

            handler = self._create_model_toggle_handler(model_id, active_var, visible_var)
            active_check = ttk.Checkbutton(parent_frame, variable=active_var, command=handler)
            active_check.grid(row=row, column=4, padx=5, pady=2)
            visible_check = ttk.Checkbutton(parent_frame, variable=visible_var, command=handler)
            visible_check.grid(row=row, column=5, padx=5, pady=2)

            if not self.db_conn:
                active_check.state(['disabled'])
                visible_check.state(['disabled'])

            self.matrix_labels[model_id] = {
                "needed": needed_label,
                "online": online_label,
                "active_jobs": active_jobs_label,
                "active_var": active_var,
                "visible_var": visible_var,
                "active_check": active_check,
                "visible_check": visible_check,
            }

    def create_stats(self, parent_frame):
        """Create labels for general statistics."""
        self.stats_labels = {}

        # Online Users
        ttk.Label(parent_frame, text="Online Users:", font=("Helvetica", 10, "bold")).grid(row=0, column=0, sticky="w")
        self.stats_labels["online_users"] = ttk.Label(parent_frame, text="0")
        self.stats_labels["online_users"].grid(row=0, column=1, sticky="w")

        # Active Jobs
        ttk.Label(parent_frame, text="Active Jobs:", font=("Helvetica", 10, "bold")).grid(row=1, column=0, sticky="w")
        self.stats_labels["active_jobs"] = ttk.Label(parent_frame, text="0")
        self.stats_labels["active_jobs"].grid(row=1, column=1, sticky="w")

    def create_graphs(self, parent_frame):
        """Create matplotlib figures and canvases."""
        self.fig = Figure(figsize=(6, 6), dpi=100)
        self.ax1 = self.fig.add_subplot(211)
        self.ax2 = self.fig.add_subplot(212)

        self.canvas = FigureCanvasTkAgg(self.fig, master=parent_frame)
        self.canvas.draw()
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=1)

    def fetch_data(self):
        """Fetch data from Redis and SQLite."""
        if self.tunnel.is_remote:
            snapshot = self.tunnel.fetch_snapshot()
            if snapshot is not None:
                return snapshot.payload

        if not self.redis_conn or not self.db_conn:
            return None

        self.model_states = self._sync_model_state_vars()

        # --- Fetch Worker Health ---
        worker_health = self.redis_conn.hgetall(WORKER_HEALTH_HASH)
        online_workers = {model['id']: 0 for model in Config.SUPPORTED_MODELS}
        worker_uptime_details: list[dict[str, Any]] = []
        cutoff = time.time() - Config.WORKER_HEARTBEAT_TIMEOUT
        for worker_info_json in worker_health.values():
            try:
                worker_info = json.loads(worker_info_json)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

            worker_type = worker_info.get("type")
            last_seen = worker_info.get("last_seen") or worker_info.get("timestamp")
            try:
                last_seen_ts = float(last_seen) if last_seen is not None else None
            except (TypeError, ValueError):
                last_seen_ts = None

            if last_seen_ts is not None and last_seen_ts < cutoff:
                continue

            if worker_type in online_workers:
                online_workers[worker_type] += 1

            first_seen = worker_info.get("first_seen")
            try:
                first_seen_ts = float(first_seen) if first_seen is not None else None
            except (TypeError, ValueError):
                first_seen_ts = None

            if first_seen_ts is None:
                first_seen_ts = last_seen_ts

            if last_seen_ts is not None and first_seen_ts is not None:
                worker_uptime_details.append(
                    {
                        "worker_id": worker_info.get("worker_id"),
                        "worker_type": worker_type,
                        "first_seen": first_seen_ts,
                        "last_seen": last_seen_ts,
                        "uptime_seconds": max(0.0, last_seen_ts - first_seen_ts),
                    }
                )

        # --- Fetch Needed Nodes (Queued Jobs) ---
        needed_workers: dict[str, int] = {}
        for model in Config.SUPPORTED_MODELS:
            needed_workers[model['id']] = self._count_model_queue_depth(model['id'])

        # --- Fetch Online Users ---
        five_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=5)
        cursor = self.db_conn.cursor()
        online_users = self._resolve_online_user_count(cursor, five_minutes_ago)

        # --- Fetch Active Jobs ---
        active_jobs, active_jobs_by_model = self._gather_active_job_metrics()

        # --- Fetch Advanced Statistics ---
        adv_stats = self.fetch_advanced_stats(cursor)
        flags = self._resolve_feature_flags(cursor)

        return {
            "online_workers": online_workers,
            "needed_workers": needed_workers,
            "online_users": online_users,
            "active_jobs": active_jobs,
            "active_jobs_by_model": active_jobs_by_model,
            "worker_uptime": worker_uptime_details,
            "flags": flags,
            **adv_stats
        }

    def fetch_advanced_stats(self, cursor):
        """Fetch detailed statistics from the database."""
        stats = {}
        try:
            cursor.execute("SELECT COUNT(id) FROM users")
            stats["total_users"] = cursor.fetchone()[0]

            cursor.execute("SELECT status, COUNT(id) FROM jobs GROUP BY status")
            job_counts = {row[0]: row[1] for row in cursor.fetchall()}
            stats["jobs_completed"] = job_counts.get('completed', 0)
            stats["jobs_failed"] = job_counts.get('failed', 0)
            stats["jobs_queued"] = job_counts.get('queued', 0)

            total_finished = stats["jobs_completed"] + stats["jobs_failed"]
            if total_finished > 0:
                stats["job_success_rate"] = f"{(stats['jobs_completed'] / total_finished) * 100:.2f}%"
            else:
                stats["job_success_rate"] = "N/A"

            cursor.execute("SELECT AVG(execution_time) FROM jobs WHERE status = 'completed' AND execution_time IS NOT NULL")
            avg_time_row = cursor.fetchone()
            avg_time = avg_time_row[0] if avg_time_row else None
            stats["avg_processing_time"] = f"{avg_time:.2f}" if avg_time else "N/A"

            cursor.execute("SELECT COUNT(id) FROM projects")
            stats["total_projects"] = cursor.fetchone()[0]

            cursor.execute("SELECT COUNT(id) FROM files")
            stats["total_files"] = cursor.fetchone()[0]

        except sqlite3.Error as e:
            print(f"Error fetching advanced stats: {e}")
        return stats

    def _load_model_states(self) -> dict[str, dict[str, bool]]:
        """Load merged model state from configuration and overrides."""

        if not self.db_conn:
            merged = apply_overrides_to_models(Config.SUPPORTED_MODELS, {})
        else:
            overrides = fetch_model_overrides(self.db_conn)
            merged = apply_overrides_to_models(Config.SUPPORTED_MODELS, overrides)

        states: dict[str, dict[str, bool]] = {}
        for model in merged:
            model_id = model.get("id")
            if not model_id:
                continue
            states[model_id] = {
                "is_active": bool(model.get("is_active", True)),
                "is_visible": bool(model.get("is_visible", True)),
            }
        return states

    def _sync_model_state_vars(self) -> dict[str, dict[str, bool]]:
        """Refresh cached model states and update checkbox widgets."""

        latest_states = self._load_model_states()
        for model_id, state in latest_states.items():
            widgets = self.matrix_labels.get(model_id)
            if not widgets:
                continue
            active_var = widgets.get("active_var")
            visible_var = widgets.get("visible_var")
            if isinstance(active_var, tk.BooleanVar):
                active_var.set(bool(state.get("is_active", True)))
            if isinstance(visible_var, tk.BooleanVar):
                visible_var.set(bool(state.get("is_visible", True)))
        return latest_states

    def _create_model_toggle_handler(self, model_id: str, active_var: tk.BooleanVar, visible_var: tk.BooleanVar):
        def handler():
            self._on_model_toggle(model_id, bool(active_var.get()), bool(visible_var.get()))

        return handler

    def _on_model_toggle(self, model_id: str, is_active: bool, is_visible: bool):
        if not self.db_conn:
            print("Database connection unavailable; cannot update model state.")
            return

        try:
            upsert_model_override(
                self.db_conn, model_id, is_active=is_active, is_visible=is_visible
            )
        except sqlite3.Error as exc:
            print(f"Failed to update model state for {model_id}: {exc}")
            return
        self.model_states[model_id] = {"is_active": is_active, "is_visible": is_visible}

        # Re-render statistics on next update cycle; Redis/DB data drives the UI.

    def _count_model_queue_depth(self, model_id: str) -> int:
        """Return total queued jobs across all shards for ``model_id``."""

        if not self.redis_conn:
            return 0

        total = 0
        pattern = f"queue:job:{model_id}*"
        try:
            for key in self.redis_conn.scan_iter(match=pattern):
                if not isinstance(key, str):
                    try:
                        key = key.decode("utf-8")
                    except Exception:  # pragma: no cover - defensive decoding
                        continue
                if not key.startswith(f"queue:job:{model_id}"):
                    continue
                try:
                    total += self.redis_conn.llen(key)
                except redis.RedisError:
                    continue
        except redis.RedisError:
            return 0
        return total

    def _gather_active_job_metrics(self) -> tuple[int, dict[str, int]]:
        """Return total active jobs and per-model counts."""

        counts = {model["id"]: 0 for model in Config.SUPPORTED_MODELS}
        if not self.redis_conn:
            return 0, counts

        try:
            active_job_entries = self.redis_conn.hgetall(ACTIVE_JOBS_HASH)
        except redis.RedisError as exc:
            logger.warning("Failed to read active jobs for kicker UI: %s", exc)
            return 0, counts

        total = len(active_job_entries)
        for active_job_json in active_job_entries.values():
            if not active_job_json:
                continue
            try:
                active_job_state = json.loads(active_job_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            model_id = infer_model_id_from_active_job_state(active_job_state)
            if model_id and model_id in counts:
                counts[model_id] += 1

        return total, counts

    def _resolve_online_user_count(self, cursor: sqlite3.Cursor, since: datetime) -> int:
        """Derive the count of recently active users across schema variants."""

        timestamp_str = since.strftime('%Y-%m-%d %H:%M:%S')
        try:
            if self._table_exists(cursor, "login_history"):
                cursor.execute(
                    "SELECT COUNT(DISTINCT user_id) FROM login_history WHERE timestamp > ?",
                    (timestamp_str,),
                )
                row = cursor.fetchone()
                return row[0] if row else 0

            if self._table_exists(cursor, "user_sessions"):
                columns = self._get_columns(cursor, "user_sessions")
                if "last_seen_at" in columns:
                    cursor.execute(
                        "SELECT COUNT(DISTINCT user_id) FROM user_sessions WHERE last_seen_at > ?",
                        (timestamp_str,),
                    )
                elif "updated_at" in columns:
                    cursor.execute(
                        "SELECT COUNT(DISTINCT user_id) FROM user_sessions WHERE updated_at > ?",
                        (timestamp_str,),
                    )
                else:
                    cursor.execute("SELECT COUNT(DISTINCT user_id) FROM user_sessions")
                row = cursor.fetchone()
                return row[0] if row else 0
        except sqlite3.Error as exc:
            print(f"Error resolving online user count: {exc}")
        return 0

    def _table_exists(self, cursor: sqlite3.Cursor, table_name: str) -> bool:
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cursor.fetchone() is not None

    def _get_columns(self, cursor: sqlite3.Cursor, table_name: str) -> set[str]:
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}

    def _resolve_feature_flags(self, cursor: sqlite3.Cursor) -> dict[str, bool]:
        """Determine feature toggles available to the connected account."""

        flags: dict[str, bool] = {}
        try:
            if self._table_exists(cursor, "user_credentials"):
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM user_credentials
                    WHERE LOWER(COALESCE(plan, '')) IN ('premium', 'managed premium', 'enterprise')
                    """
                )
                row = cursor.fetchone()
                flags["premium_monitoring"] = bool(row and row[0])
        except sqlite3.Error as exc:
            logger.warning("Failed to resolve kicker feature flags: %s", exc)
        return flags

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """Render ``seconds`` as a compact human-readable duration."""

        if seconds < 0:
            seconds = 0
        delta = timedelta(seconds=int(seconds))
        days = delta.days
        remainder = delta.seconds
        hours, remainder = divmod(remainder, 3600)
        minutes, secs = divmod(remainder, 60)
        if days > 0:
            return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _ensure_premium_tab(self) -> None:
        """Add the premium tab to the notebook if it is not present."""

        if self.premium_tab is not None:
            return

        self.premium_tab = ttk.Frame(self.notebook)
        self.create_premium_uptime_tab(self.premium_tab)

        try:
            help_index = self.notebook.index(self.help_tab)
        except tk.TclError:
            help_index = None

        if isinstance(help_index, int):
            self.notebook.insert(help_index, self.premium_tab, text="Premium Uptime")
        else:
            self.notebook.add(self.premium_tab, text="Premium Uptime")

    def _remove_premium_tab(self) -> None:
        """Remove the premium tab when premium features are disabled."""

        if self.premium_tab is None:
            return

        try:
            self.notebook.forget(self.premium_tab)
        except tk.TclError:
            pass
        finally:
            try:
                self.premium_tab.destroy()
            except Exception:
                pass
            self.premium_tab = None
            self.premium_tree = None
            self.premium_summary_label = None

    def _refresh_premium_uptime(self, worker_uptime: list[dict[str, Any]]) -> None:
        """Populate the premium uptime tree with ``worker_uptime`` details."""

        if self.premium_tab is None or self.premium_tree is None:
            return

        for item in self.premium_tree.get_children():
            self.premium_tree.delete(item)

        formatted_rows: list[tuple[float, str, str, str, str]] = []
        model_lookup = {model.get("id"): model.get("name", model.get("short_name")) for model in Config.SUPPORTED_MODELS}
        total_uptime = 0.0
        for entry in worker_uptime:
            worker_id = entry.get("worker_id") or "—"
            worker_type = entry.get("worker_type") or "Unknown"
            uptime_seconds = entry.get("uptime_seconds")
            last_seen = entry.get("last_seen")

            try:
                uptime_seconds = float(uptime_seconds) if uptime_seconds is not None else 0.0
            except (TypeError, ValueError):
                uptime_seconds = 0.0

            try:
                last_seen_ts = float(last_seen) if last_seen is not None else None
            except (TypeError, ValueError):
                last_seen_ts = None

            uptime_label = self._format_duration(uptime_seconds)
            if last_seen_ts is not None:
                last_seen_dt = datetime.fromtimestamp(last_seen_ts, tz=timezone.utc)
                last_seen_label = last_seen_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            else:
                last_seen_label = "—"

            model_label = model_lookup.get(worker_type, worker_type)

            formatted_rows.append((uptime_seconds, worker_id, model_label, uptime_label, last_seen_label))
            total_uptime += uptime_seconds

        formatted_rows.sort(key=lambda row: row[0], reverse=True)

        for _, worker_id, model_label, uptime_label, last_seen_label in formatted_rows:
            self.premium_tree.insert("", "end", values=(worker_id, model_label, uptime_label, last_seen_label))

        if self.premium_summary_label is not None:
            if formatted_rows:
                average_uptime = total_uptime / len(formatted_rows)
                self.premium_summary_label.config(
                    text=(
                        f"Monitoring {len(formatted_rows)} nodes · "
                        f"Average uptime {self._format_duration(average_uptime)}"
                    )
                )
            else:
                self.premium_summary_label.config(text="No active premium nodes detected.")

    def update_data(self):
        """Update the GUI with new data."""
        data = self.fetch_data()
        if data:
            flags = data.get("flags") or {}
            if isinstance(flags, dict):
                self.feature_flags = flags
            else:
                self.feature_flags = {}

            premium_enabled = bool(self.feature_flags.get("premium_monitoring"))
            worker_uptime = data.get("worker_uptime") or []

            if premium_enabled:
                self._ensure_premium_tab()
                if isinstance(worker_uptime, list):
                    self._refresh_premium_uptime(worker_uptime)
            else:
                self._remove_premium_tab()

            needed_workers = data.get("needed_workers", {}) or {}
            online_workers = data.get("online_workers", {}) or {}
            active_jobs_by_model = data.get("active_jobs_by_model", {}) or {}
            online_users = data.get("online_users", 0)
            active_jobs = data.get("active_jobs", 0)

            # Update matrix
            for model_id, labels in self.matrix_labels.items():
                labels["needed"].config(text=str(needed_workers.get(model_id, 0)))
                labels["online"].config(text=str(online_workers.get(model_id, 0)))
                labels["active_jobs"].config(text=str(active_jobs_by_model.get(model_id, 0)))

            # Update stats
            self.stats_labels["online_users"].config(text=str(online_users))
            self.stats_labels["active_jobs"].config(text=str(active_jobs))

            # Update advanced stats
            for key, label in self.advanced_stats_labels.items():
                label.config(text=str(data.get(key, "N/A")))

            # Update graph
            self.update_graph(
                {
                    "needed_workers": needed_workers,
                    "online_workers": online_workers,
                    "active_jobs_by_model": active_jobs_by_model,
                    "online_users": online_users,
                    "active_jobs": active_jobs,
                }
            )

        # Schedule the next update
        self.after(10000, self.update_data)

    def update_graph(self, data):
        """Updates the matplotlib graph with new data."""
        # --- Graph 1: Compute Nodes ---
        self.ax1.clear()
        model_names = [model['name'] for model in Config.SUPPORTED_MODELS]
        model_ids = [model['id'] for model in Config.SUPPORTED_MODELS]

        needed_data = data.get('needed_workers', {}) or {}
        online_data = data.get('online_workers', {}) or {}
        active_data = data.get('active_jobs_by_model', {}) or {}

        needed = [needed_data.get(mid, 0) for mid in model_ids]
        online = [online_data.get(mid, 0) for mid in model_ids]
        active = [active_data.get(mid, 0) for mid in model_ids]

        x_positions = list(range(len(model_names)))
        width = 0.25

        self.ax1.bar([i - width for i in x_positions], needed, width, label='Queued Jobs')
        self.ax1.bar(x_positions, online, width, label='Online Workers')
        self.ax1.bar([i + width for i in x_positions], active, width, label='Active Jobs')

        self.ax1.set_ylabel('Count')
        self.ax1.set_title('Compute Node Status')
        self.ax1.set_xticks(x_positions)
        self.ax1.set_xticklabels(model_names, rotation=45, ha="right")
        self.ax1.legend()

        # --- Graph 2: System Stats ---
        self.ax2.clear()
        stats_labels = ['Online Users', 'Active Jobs']
        stats_values = [data.get('online_users', 0), data.get('active_jobs', 0)]
        self.ax2.bar(stats_labels, stats_values)
        self.ax2.set_ylabel('Count')
        self.ax2.set_title('Live System Statistics')

        self.fig.tight_layout()
        self.canvas.draw()

    def on_closing(self):
        """Handle window closing."""
        if self.db_conn:
            self.db_conn.close()
        self.destroy()

    # --- Database Editor Methods ---
    def load_table_names(self):
        """Load table names into the combobox."""
        if not self.db_conn:
            return
        cursor = self.db_conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [table[0] for table in cursor.fetchall()]
        self.table_selector['values'] = tables

    def load_table_data(self, event=None):
        """Load data from the selected table into the Treeview."""
        table_name = self.table_selector.get()
        if not table_name or not self.db_conn:
            return

        # Clear existing treeview
        for i in self.db_tree.get_children():
            self.db_tree.delete(i)
        self.db_tree["columns"] = []

        cursor = self.db_conn.cursor()
        try:
            cursor.execute(f"PRAGMA table_info({table_name})")
            columns = [info[1] for info in cursor.fetchall()]
            self.db_tree["columns"] = columns
            for col in columns:
                self.db_tree.heading(col, text=col)
                self.db_tree.column(col, width=100)

            cursor.execute(f"SELECT * FROM {table_name}")
            for row in cursor.fetchall():
                self.db_tree.insert("", "end", values=row)
        except sqlite3.Error as e:
            print(f"Error loading table {table_name}: {e}")

    def add_record_window(self):
        """Show a window to add a new record."""
        table_name = self.table_selector.get()
        if not table_name:
            return

        window = tk.Toplevel(self)
        window.title(f"Add Record to {table_name}")
        window.geometry("500x500")

        cursor = self.db_conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [info[1] for info in cursor.fetchall()]

        # Create scrollable frame using ScrollableFrame class
        scroll_container = ScrollableFrame(window)
        scroll_container.pack(side="top", fill="both", expand=True, padx=5, pady=5)

        entries = {}
        for i, col in enumerate(columns):
            ttk.Label(scroll_container.scrollable_frame, text=col).grid(row=i, column=0, padx=5, pady=2, sticky='w')
            entry = ttk.Entry(scroll_container.scrollable_frame, width=40)
            entry.grid(row=i, column=1, padx=5, pady=2, sticky='ew')
            entries[col] = entry

        scroll_container.scrollable_frame.grid_columnconfigure(1, weight=1)

        def save():
            values = {col: entry.get() for col, entry in entries.items()}
            try:
                cols = ', '.join(values.keys())
                placeholders = ', '.join(['?'] * len(values))
                sql = f"INSERT INTO {table_name} ({cols}) VALUES ({placeholders})"
                cursor.execute(sql, tuple(values.values()))
                self.db_conn.commit()
                self.load_table_data()
                window.destroy()
            except sqlite3.Error as e:
                print(f"Error adding record: {e}")

        button_frame = ttk.Frame(window)
        ttk.Button(button_frame, text="Save", command=save).pack(pady=10)
        button_frame.pack(side="bottom", fill="x")

    def edit_record_window(self):
        """Show a window to edit the selected record."""
        selected_item = self.db_tree.focus()
        if not selected_item:
            return

        table_name = self.table_selector.get()
        values = self.db_tree.item(selected_item, 'values')

        window = tk.Toplevel(self)
        window.title(f"Edit Record in {table_name}")
        window.geometry("500x500")

        cursor = self.db_conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [info[1] for info in cursor.fetchall()]

        # Create scrollable frame using ScrollableFrame class
        scroll_container = ScrollableFrame(window)
        scroll_container.pack(side="top", fill="both", expand=True, padx=5, pady=5)

        entries = {}
        for i, col in enumerate(columns):
            ttk.Label(scroll_container.scrollable_frame, text=col).grid(row=i, column=0, padx=5, pady=2, sticky='w')
            entry = ttk.Entry(scroll_container.scrollable_frame, width=40)
            entry.grid(row=i, column=1, padx=5, pady=2, sticky='ew')
            entry.insert(0, values[i])
            entries[col] = entry

        scroll_container.scrollable_frame.grid_columnconfigure(1, weight=1)

        def save():
            new_values = {col: entry.get() for col, entry in entries.items()}
            try:
                pk_column = columns[0] # Assuming first column is the primary key
                pk_value = values[0]

                set_clause = ', '.join([f"{col} = ?" for col in new_values.keys()])
                sql = f"UPDATE {table_name} SET {set_clause} WHERE {pk_column} = ?"

                cursor.execute(sql, tuple(new_values.values()) + (pk_value,))
                self.db_conn.commit()
                self.load_table_data()
                window.destroy()
            except sqlite3.Error as e:
                print(f"Error updating record: {e}")

        button_frame = ttk.Frame(window)
        ttk.Button(button_frame, text="Save", command=save).pack(pady=10)
        button_frame.pack(side="bottom", fill="x")

    def delete_record(self):
        """Delete the selected record from the table."""
        selected_item = self.db_tree.focus()
        if not selected_item:
            return

        table_name = self.table_selector.get()
        values = self.db_tree.item(selected_item, 'values')

        cursor = self.db_conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = [info[1] for info in cursor.fetchall()]
        pk_column = columns[0]  # Assuming first column is the primary key
        pk_value = values[0]

        try:
            sql = f"DELETE FROM {table_name} WHERE {pk_column} = ?"
            cursor.execute(sql, (pk_value,))
            self.db_conn.commit()
            self.load_table_data()
        except sqlite3.Error as e:
            print(f"Error deleting record: {e}")

if __name__ == "__main__":
    app = KickerApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
