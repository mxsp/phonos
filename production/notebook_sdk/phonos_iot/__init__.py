"""
Phonos IoT SDK for Notebooks

Usage in notebook cells:
    import phonos_iot

    # List devices in current project
    devices = phonos_iot.list_devices()

    # Get specific device
    device = phonos_iot.get_device('temperature_sensor')

    # Read current state
    state = device.read()
    print(f"Temperature: {state['temperature']}C")

    # Write to device (for input widgets)
    device.write('relay_1', True)

    # Get historical data
    history = device.history(limit=100)
"""

import os
import requests
from datetime import datetime
from typing import Optional, Dict, List, Any

_BASE_URL = os.environ.get('PHONOS_API_URL', 'http://localhost:5000')
_PROJECT_ID = os.environ.get('NOTEBOOK_PROJECT_ID')
# Kernel manager sets NOTEBOOK_SESSION_ID, but we also check TOKEN for compatibility
_SESSION_TOKEN = os.environ.get('NOTEBOOK_SESSION_ID') or os.environ.get('NOTEBOOK_SESSION_TOKEN')


class IoTDevice:
    """Represents an IoT device in the project."""

    def __init__(self, device_data: Dict):
        self.id = device_data['id']
        self.device_id = device_data['device_id']
        self.name = device_data['device_name']
        self.type = device_data['device_type']
        self.description = device_data.get('description', '')
        self.schema = device_data.get('schema', {})
        self.status = device_data.get('status', 'unknown')
        self._state = device_data.get('current_state', {})

    def read(self, output_id: Optional[str] = None) -> Any:
        """Read current device state or specific output.

        Args:
            output_id: Optional specific output to read. If None, returns all state.

        Returns:
            The current value of the output, or full state dict if output_id is None.

        Example:
            >>> device.read()
            {'temperature': 23.5, 'humidity': 65}
            >>> device.read('temperature')
            23.5
        """
        response = _api_request('GET', f'/api/iot/internal/devices/{self.id}/read')
        self._state = response.get('state', {})
        self.status = response.get('status', self.status)
        self.last_seen = response.get('last_seen')

        if output_id:
            return self._state.get(output_id)
        return self._state

    def write(self, input_id: str, value: Any) -> Dict:
        """Send command to device input.

        Args:
            input_id: The input identifier from device schema
            value: The value to set

        Returns:
            Dict with command_id and status

        Example:
            >>> device.write('relay_1', True)
            {'success': True, 'command_id': 'abc123'}
        """
        response = _api_request('POST', f'/api/iot/internal/devices/{self.id}/write', {
            'input_id': input_id,
            'value': value
        })
        return response

    def toggle(self, input_id: str) -> Dict:
        """Toggle a boolean input.

        Reads the current state (if available) or assumes False, then inverts it.

        Args:
            input_id: The input identifier

        Returns:
            Dict with command_id and new value
        """
        # Read current state first
        self.read()
        current = bool(self._state.get(input_id, False))
        new_value = not current
        return self.write(input_id, new_value)

    def monitor(self, interval: float = 1.0, duration: Optional[float] = None, output_id: Optional[str] = None, show_history: bool = True):
        """Monitor device data in real-time within the notebook.

        Updates the cell output in-place with a rich dashboard view.

        Args:
            interval: Update interval in seconds (default 1.0)
            duration: Total duration in seconds (default None = infinite loop until stopped)
            output_id: Optional specific output to monitor
            show_history: Show recent values as a sparkline (default True)

        Usage:
            Stop the monitoring by interrupting the kernel (Stop button).
        """
        try:
            from IPython.display import clear_output, display, HTML
            import time
        except ImportError:
            print("IPython not available. Monitor mode disabled.")
            return

        start_time = time.time()
        history_buffer = []  # Store recent values for sparkline
        max_history = 30

        try:
            while True:
                if duration and (time.time() - start_time > duration):
                    break

                # Fetch data
                try:
                    data = self.read(output_id)
                    timestamp = datetime.now().strftime('%H:%M:%S')
                    elapsed = time.time() - start_time

                    # Track history for numeric values
                    if show_history and isinstance(data, (int, float)):
                        history_buffer.append(data)
                        if len(history_buffer) > max_history:
                            history_buffer.pop(0)

                    # Clear and redraw
                    clear_output(wait=True)

                    if output_id:
                        # Single output monitoring view
                        sparkline_html = self._generate_sparkline(history_buffer) if show_history and history_buffer else ''
                        stats_html = self._generate_stats(history_buffer) if history_buffer else ''

                        display(HTML(self._render_single_output_monitor(
                            output_id=output_id,
                            value=data,
                            timestamp=timestamp,
                            elapsed=elapsed,
                            sparkline_html=sparkline_html,
                            stats_html=stats_html
                        )))
                    else:
                        # Full device monitoring view
                        display(HTML(self._render_full_monitor(timestamp, elapsed)))

                except Exception as e:
                    clear_output(wait=True)
                    display(HTML(f"""
                    <div style="border: 1px solid #ef4444; border-radius: 8px; padding: 16px; background: #18181b; color: #fca5a5; font-family: system-ui, -apple-system, sans-serif;">
                        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
                            <span style="font-size: 1.25em;">⚠️</span>
                            <strong>Error fetching data</strong>
                        </div>
                        <code style="color: #f87171;">{str(e)}</code>
                    </div>
                    """))

                time.sleep(interval)
        except KeyboardInterrupt:
            clear_output(wait=True)
            display(HTML(f"""
            <div style="border: 1px solid #3f3f46; border-radius: 8px; padding: 16px; background: #18181b; color: #a1a1aa; font-family: system-ui, -apple-system, sans-serif;">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span style="font-size: 1.25em;">⏹️</span>
                    <span>Monitoring stopped after {time.time() - start_time:.1f}s</span>
                </div>
            </div>
            """))

    def _generate_sparkline(self, values: List[float], width: int = 200, height: int = 40) -> str:
        """Generate an SVG sparkline from values."""
        if not values or len(values) < 2:
            return ''

        min_val = min(values)
        max_val = max(values)
        range_val = max_val - min_val if max_val != min_val else 1

        # Normalize values to height
        points = []
        for i, v in enumerate(values):
            x = (i / (len(values) - 1)) * width
            y = height - ((v - min_val) / range_val) * (height - 4) - 2
            points.append(f"{x:.1f},{y:.1f}")

        path = "M " + " L ".join(points)

        # Gradient for area fill
        area_points = points + [f"{width},{ height}", f"0,{height}"]
        area_path = "M " + " L ".join(area_points) + " Z"

        return f"""
        <svg width="{width}" height="{height}" style="display: block;">
            <defs>
                <linearGradient id="sparkGrad" x1="0%" y1="0%" x2="0%" y2="100%">
                    <stop offset="0%" style="stop-color:#22c55e;stop-opacity:0.3"/>
                    <stop offset="100%" style="stop-color:#22c55e;stop-opacity:0.05"/>
                </linearGradient>
            </defs>
            <path d="{area_path}" fill="url(#sparkGrad)"/>
            <path d="{path}" fill="none" stroke="#22c55e" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <circle cx="{(len(values)-1) / (len(values)-1) * width}" cy="{height - ((values[-1] - min_val) / range_val) * (height - 4) - 2}" r="3" fill="#22c55e"/>
        </svg>
        """

    def _generate_stats(self, values: List[float]) -> str:
        """Generate statistics HTML for numeric values."""
        if not values:
            return ''

        min_val = min(values)
        max_val = max(values)
        avg_val = sum(values) / len(values)
        current = values[-1]

        # Determine trend
        if len(values) >= 3:
            recent_avg = sum(values[-3:]) / 3
            older_avg = sum(values[:-3]) / max(1, len(values) - 3) if len(values) > 3 else recent_avg
            if recent_avg > older_avg * 1.02:
                trend = '↑'
                trend_color = '#22c55e'
            elif recent_avg < older_avg * 0.98:
                trend = '↓'
                trend_color = '#ef4444'
            else:
                trend = '→'
                trend_color = '#a1a1aa'
        else:
            trend = '→'
            trend_color = '#a1a1aa'

        return f"""
        <div style="display: flex; gap: 16px; margin-top: 12px; font-size: 0.75em; color: #71717a;">
            <div><span style="color: #a1a1aa;">Min:</span> {min_val:.2f}</div>
            <div><span style="color: #a1a1aa;">Max:</span> {max_val:.2f}</div>
            <div><span style="color: #a1a1aa;">Avg:</span> {avg_val:.2f}</div>
            <div><span style="color: {trend_color};">{trend}</span></div>
        </div>
        """

    def _render_single_output_monitor(self, output_id: str, value: Any, timestamp: str, elapsed: float, sparkline_html: str, stats_html: str) -> str:
        """Render the single output monitor view."""
        status_color = '#22c55e' if self.is_online else '#71717a'
        status_text = 'Online' if self.is_online else 'Offline'

        # Format value display
        if isinstance(value, float):
            value_display = f"{value:.2f}"
        elif isinstance(value, bool):
            value_display = '🟢 ON' if value else '🔴 OFF'
        else:
            value_display = str(value)

        # Get unit from schema if available
        unit = ''
        for output in self.outputs:
            if output.get('id') == output_id:
                unit = output.get('unit', '')
                break

        return f"""
        <div style="border: 1px solid #27272a; border-radius: 12px; padding: 20px; background: linear-gradient(180deg, #18181b 0%, #0f0f10 100%); color: #f4f4f5; font-family: system-ui, -apple-system, sans-serif; max-width: 400px;">
            <!-- Header -->
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px;">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span style="width: 8px; height: 8px; border-radius: 50%; background: {status_color}; box-shadow: 0 0 8px {status_color};"></span>
                    <span style="font-weight: 600; font-size: 1em;">{self.name}</span>
                </div>
                <span style="color: #52525b; font-size: 0.75em;">{status_text}</span>
            </div>

            <!-- Output Label -->
            <div style="color: #71717a; font-size: 0.875em; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.05em;">
                {output_id}
            </div>

            <!-- Main Value -->
            <div style="font-size: 3em; font-weight: 700; color: #fafafa; line-height: 1; margin-bottom: 8px;">
                {value_display}<span style="font-size: 0.4em; color: #71717a; margin-left: 4px;">{unit}</span>
            </div>

            <!-- Sparkline -->
            {f'<div style="margin: 16px 0;">{sparkline_html}</div>' if sparkline_html else ''}

            <!-- Stats -->
            {stats_html}

            <!-- Footer -->
            <div style="display: flex; justify-content: space-between; margin-top: 16px; padding-top: 12px; border-top: 1px solid #27272a; font-size: 0.75em; color: #52525b;">
                <span>Updated: {timestamp}</span>
                <span>Running: {elapsed:.0f}s</span>
            </div>
        </div>
        """

    def _render_full_monitor(self, timestamp: str, elapsed: float) -> str:
        """Render the full device monitor view with all outputs."""
        status_color = '#22c55e' if self.is_online else '#71717a'
        status_text = 'Online' if self.is_online else 'Offline'

        # Build output cards
        output_cards = []
        for output in self.outputs:
            output_id = output.get('id', '')
            label = output.get('label', output_id)
            unit = output.get('unit', '')
            value = self._state.get(output_id, 'N/A')

            # Format value
            if isinstance(value, float):
                value_display = f"{value:.2f}"
            elif isinstance(value, bool):
                value_display = '🟢' if value else '🔴'
            else:
                value_display = str(value)

            output_cards.append(f"""
            <div style="background: #27272a; border-radius: 8px; padding: 12px; min-width: 100px;">
                <div style="color: #71717a; font-size: 0.75em; margin-bottom: 4px;">{label}</div>
                <div style="font-size: 1.5em; font-weight: 600; color: #fafafa;">
                    {value_display}<span style="font-size: 0.5em; color: #71717a;">{unit}</span>
                </div>
            </div>
            """)

        outputs_html = ''.join(output_cards) if output_cards else '<div style="color: #71717a;">No outputs defined</div>'

        # Build input status
        input_items = []
        for inp in self.inputs:
            input_id = inp.get('id', '')
            label = inp.get('label', input_id)
            input_type = inp.get('type', 'unknown')
            value = self._state.get(input_id, 'N/A')

            if isinstance(value, bool):
                value_display = '🟢 ON' if value else '🔴 OFF'
            else:
                value_display = str(value)

            input_items.append(f"""
            <div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #27272a;">
                <span style="color: #a1a1aa;">{label}</span>
                <span style="color: #fafafa; font-family: monospace;">{value_display}</span>
            </div>
            """)

        inputs_html = ''.join(input_items) if input_items else '<div style="color: #71717a; padding: 8px 0;">No inputs defined</div>'

        return f"""
        <div style="border: 1px solid #27272a; border-radius: 12px; padding: 20px; background: linear-gradient(180deg, #18181b 0%, #0f0f10 100%); color: #f4f4f5; font-family: system-ui, -apple-system, sans-serif; max-width: 500px;">
            <!-- Header -->
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                <div>
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span style="width: 10px; height: 10px; border-radius: 50%; background: {status_color}; box-shadow: 0 0 8px {status_color};"></span>
                        <span style="font-weight: 600; font-size: 1.25em;">{self.name}</span>
                    </div>
                    <div style="color: #71717a; font-size: 0.875em; margin-top: 4px;">{self.device_id} • {self.type}</div>
                </div>
                <div style="text-align: right;">
                    <div style="color: {status_color}; font-weight: 500;">{status_text}</div>
                    <div style="color: #52525b; font-size: 0.75em;">Live</div>
                </div>
            </div>

            <!-- Outputs Section -->
            <div style="margin-bottom: 20px;">
                <div style="color: #a1a1aa; font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 8px;">Outputs</div>
                <div style="display: flex; flex-wrap: wrap; gap: 8px;">
                    {outputs_html}
                </div>
            </div>

            <!-- Inputs Section -->
            <div style="margin-bottom: 16px;">
                <div style="color: #a1a1aa; font-size: 0.75em; text-transform: uppercase; letter-spacing: 0.1em; margin-bottom: 8px;">Inputs</div>
                <div style="background: #0f0f10; border-radius: 8px; padding: 4px 12px;">
                    {inputs_html}
                </div>
            </div>

            <!-- Footer -->
            <div style="display: flex; justify-content: space-between; padding-top: 12px; border-top: 1px solid #27272a; font-size: 0.75em; color: #52525b;">
                <span>Updated: {timestamp}</span>
                <span>Running: {elapsed:.0f}s</span>
            </div>
        </div>
        """

    def history(self, limit: int = 100, output_id: Optional[str] = None) -> List[Dict]:
        """Get historical data readings.

        Args:
            limit: Maximum number of records to return (default 100, max 10000)
            output_id: Optional filter for specific output

        Returns:
            List of historical data records with timestamps

        Example:
            >>> history = device.history(limit=50, output_id='temperature')
            >>> import pandas as pd
            >>> df = pd.DataFrame(history)
            >>> df.plot(x='timestamp', y='temperature')
        """
        params = {'limit': min(limit, 10000)}
        if output_id:
            params['output_id'] = output_id
        response = _api_request('GET', f'/api/iot/internal/devices/{self.id}/history', params=params)
        return response.get('data', [])

    @property
    def state(self) -> Dict:
        """Get cached state (use read() for fresh data)."""
        return self._state

    @property
    def inputs(self) -> List[Dict]:
        """List of device inputs (controllable)."""
        return self.schema.get('inputs', [])

    @property
    def outputs(self) -> List[Dict]:
        """List of device outputs (readable)."""
        return self.schema.get('outputs', [])

    @property
    def is_online(self) -> bool:
        """Check if device is currently online."""
        return self.status == 'online'

    def __repr__(self):
        return f"IoTDevice(id='{self.device_id}', name='{self.name}', type='{self.type}', status='{self.status}')"

    def _repr_html_(self):
        """Rich display for Jupyter notebooks."""
        inputs_html = ''.join(
            f"<li><code>{i.get('id')}</code>: {i.get('label', i.get('id'))} ({i.get('type', 'unknown')})</li>"
            for i in self.inputs
        ) or '<li>None</li>'

        outputs_html = ''.join(
            f"<li><code>{o.get('id')}</code>: {o.get('label', o.get('id'))} ({o.get('type', 'unknown')})</li>"
            for o in self.outputs
        ) or '<li>None</li>'

        status_color = '#22c55e' if self.is_online else '#71717a'

        return f"""
        <div style="border: 1px solid #3f3f46; border-radius: 8px; padding: 12px; background: #18181b; color: #f4f4f5; font-family: monospace;">
            <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 8px;">
                <span style="width: 8px; height: 8px; border-radius: 50%; background: {status_color};"></span>
                <strong>{self.name}</strong>
                <span style="color: #71717a;">({self.device_id})</span>
            </div>
            <div style="color: #a1a1aa; font-size: 0.875em;">
                Type: {self.type} | Status: {self.status}
            </div>
            <div style="margin-top: 8px;">
                <strong>Inputs:</strong>
                <ul style="margin: 4px 0; padding-left: 20px;">{inputs_html}</ul>
            </div>
            <div style="margin-top: 8px;">
                <strong>Outputs:</strong>
                <ul style="margin: 4px 0; padding-left: 20px;">{outputs_html}</ul>
            </div>
        </div>
        """


def _api_request(method: str, endpoint: str, data: Dict = None, params: Dict = None) -> Dict:
    """Make authenticated API request to Phonos backend."""
    if not _SESSION_TOKEN or not _PROJECT_ID:
        raise RuntimeError(
            "Not running in a notebook context. "
            "NOTEBOOK_SESSION_TOKEN and NOTEBOOK_PROJECT_ID environment variables are required."
        )

    headers = {
        'X-Notebook-Session': _SESSION_TOKEN,
        'X-Project-ID': str(_PROJECT_ID),
        'Content-Type': 'application/json'
    }

    url = f"{_BASE_URL}{endpoint}"

    try:
        response = requests.request(
            method=method,
            url=url,
            headers=headers,
            json=data,
            params=params,
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.ConnectionError:
        raise ConnectionError(
            f"Could not connect to Phonos API at {_BASE_URL}. "
            "Make sure the Phonos server is running."
        )
    except requests.exceptions.HTTPError as e:
        error_detail = ''
        try:
            error_detail = e.response.json().get('error', '')
        except Exception:
            pass
        raise RuntimeError(f"API request failed: {e}. {error_detail}")


def list_devices() -> List[IoTDevice]:
    """List all IoT devices in the current project.

    Returns:
        List of IoTDevice objects

    Example:
        >>> devices = phonos_iot.list_devices()
        >>> for device in devices:
        ...     print(f"{device.name}: {device.status}")
    """
    if not _PROJECT_ID:
        raise RuntimeError(
            "Not running in a notebook context (NOTEBOOK_PROJECT_ID not set). "
            "This SDK can only be used within Phonos notebooks."
        )

    response = _api_request('GET', '/api/iot/internal/devices')
    return [IoTDevice(d) for d in response.get('devices', [])]


def get_device(device_id: str) -> Optional[IoTDevice]:
    """Get a specific device by its device_id.

    Args:
        device_id: The device's unique identifier

    Returns:
        IoTDevice object or None if not found

    Example:
        >>> device = phonos_iot.get_device('temp_sensor_1')
        >>> if device:
        ...     print(device.read('temperature'))
    """
    devices = list_devices()
    for device in devices:
        if device.device_id == device_id:
            return device
    return None


def get_device_by_name(name: str) -> Optional[IoTDevice]:
    """Get a specific device by its display name.

    Args:
        name: The device's display name

    Returns:
        IoTDevice object or None if not found

    Example:
        >>> device = phonos_iot.get_device_by_name('Temperature Sensor')
    """
    devices = list_devices()
    for device in devices:
        if device.name == name:
            return device
    return None


def get_project_info() -> Dict:
    """Get information about the current project context.

    Returns:
        Dict with project_id and session info
    """
    return {
        'project_id': _PROJECT_ID,
        'api_url': _BASE_URL,
        'has_session': bool(_SESSION_TOKEN)
    }


# Version info
__version__ = '1.0.0'
__all__ = ['IoTDevice', 'list_devices', 'get_device', 'get_device_by_name', 'get_project_info']
