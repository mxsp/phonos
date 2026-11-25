"""
Honeypot System for Bot Detection
Provides invisible fields, timing analysis, and behavioral detection.
"""
from __future__ import annotations

import time
from typing import Optional
from functools import wraps
from flask import request, jsonify

from .audit_log import log_audit_event, AuditEventType
from .ip_security import IPSecurity, ThreatEvent


class HoneypotDetector:
    """Bot detection using honeypot techniques."""

    # Honeypot field names (these should be invisible in forms)
    HONEYPOT_FIELDS = {
        'website',  # Bots often fill this
        'url',
        'homepage',
        'company',
        'phone_number',
        'fax',
        'email_confirm',  # Hidden confirmation field
    }

    # Timing thresholds
    MIN_FORM_TIME = 2  # Minimum seconds to fill form (humans take time)
    MAX_FORM_TIME = 3600  # Maximum seconds (1 hour) before session expires

    @classmethod
    def check_honeypot_fields(cls, data: dict) -> bool:
        """
        Check if honeypot fields were filled.
        Returns True if bot detected.
        """
        for field in cls.HONEYPOT_FIELDS:
            if field in data and data[field]:
                return True  # Bot filled honeypot field
        return False

    @classmethod
    def check_form_timing(cls, start_time: Optional[float]) -> bool:
        """
        Check if form was filled too quickly or too slowly.
        Returns True if timing is suspicious.
        """
        if start_time is None:
            return False  # No timing data

        elapsed = time.time() - start_time

        # Too fast (bot)
        if elapsed < cls.MIN_FORM_TIME:
            return True

        # Too slow (suspicious)
        if elapsed > cls.MAX_FORM_TIME:
            return True

        return False

    @classmethod
    def check_user_agent(cls) -> bool:
        """
        Check for missing or suspicious user agent.
        Returns True if suspicious.
        """
        user_agent = request.user_agent.string

        if not user_agent:
            return True  # No user agent

        # Check for bot patterns
        bot_indicators = [
            'bot', 'crawler', 'spider', 'scraper', 'curl', 'wget',
            'python-requests', 'java', 'php', 'perl'
        ]

        user_agent_lower = user_agent.lower()
        return any(indicator in user_agent_lower for indicator in bot_indicators)

    @classmethod
    def check_javascript_enabled(cls, data: dict) -> bool:
        """
        Check if JavaScript was enabled (via hidden field set by JS).
        Returns True if JS appears disabled (potential bot).
        """
        # This requires frontend to set a field like:
        # <input type="hidden" name="_js_check" value="">
        # And JavaScript to change it: document.querySelector('[name="_js_check"]').value = '1'

        js_check = data.get('_js_check')
        return js_check != '1'  # JS not enabled or bot

    @classmethod
    def detect_bot(cls, data: dict, form_start_time: Optional[float] = None) -> tuple[bool, list[str]]:
        """
        Comprehensive bot detection.

        Args:
            data: Form data
            form_start_time: When form was loaded (timestamp)

        Returns:
            Tuple of (is_bot, reasons)
        """
        reasons = []

        # Check honeypot fields
        if cls.check_honeypot_fields(data):
            reasons.append("Honeypot field filled")

        # Check timing
        if form_start_time and cls.check_form_timing(form_start_time):
            elapsed = time.time() - form_start_time
            reasons.append(f"Suspicious timing: {elapsed:.2f}s")

        # Check user agent
        if cls.check_user_agent():
            reasons.append(f"Suspicious user agent: {request.user_agent.string}")

        # Check JavaScript
        if cls.check_javascript_enabled(data):
            reasons.append("JavaScript not enabled")

        # Check for automated patterns
        if cls._detect_automated_patterns(data):
            reasons.append("Automated submission patterns detected")

        is_bot = len(reasons) > 0
        return is_bot, reasons

    @staticmethod
    def _detect_automated_patterns(data: dict) -> bool:
        """Detect patterns typical of automated submissions."""
        # Check if all fields filled perfectly (bots often do this)
        if len(data) > 10:  # Large form
            empty_fields = sum(1 for v in data.values() if not v)
            if empty_fields == 0:
                return True  # Too perfect

        # Check for repeated values
        values = [str(v) for v in data.values() if v]
        if len(values) != len(set(values)) and len(values) > 3:
            return True  # Repeated values

        return False


def honeypot_protect(f):
    """
    Decorator to protect routes with honeypot detection.

    Example:
        @app.route('/register', methods=['POST'])
        @honeypot_protect
        def register():
            # If we get here, not a bot
            pass
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Get form data
        if request.is_json:
            data = request.get_json() or {}
        else:
            data = request.form.to_dict()

        # Get form start time from data
        form_start_time = data.get('_form_start_time')
        if form_start_time:
            try:
                form_start_time = float(form_start_time)
            except (ValueError, TypeError):
                form_start_time = None

        # Detect bot
        is_bot, reasons = HoneypotDetector.detect_bot(data, form_start_time)

        if is_bot:
            ip_address = request.remote_addr

            # Log detection
            log_audit_event(
                AuditEventType.INVALID_INPUT,
                f"Bot detected on {request.path}",
                result='blocked',
                severity='warning',
                details={
                    'reasons': reasons,
                    'user_agent': request.user_agent.string,
                    'path': request.path
                }
            )

            # Record threat event
            IPSecurity.record_threat_event(
                ip_address,
                'bot_detected',
                ThreatEvent.SUSPICIOUS_REQUEST,
                details=f"Reasons: {', '.join(reasons)}"
            )

            # Return error (but don't reveal it's honeypot)
            if request.path.startswith('/api/'):
                return jsonify({
                    'status': 'error',
                    'message': 'Please try again later.'
                }), 400
            else:
                return 'Please try again later.', 400

        return f(*args, **kwargs)

    return decorated_function


class BehaviorAnalyzer:
    """Analyze user behavior patterns to detect bots."""

    @staticmethod
    def analyze_click_pattern(clicks: list[dict]) -> dict:
        """
        Analyze mouse click patterns.

        Args:
            clicks: List of {'x': int, 'y': int, 'timestamp': float}

        Returns:
            Analysis results
        """
        if not clicks or len(clicks) < 2:
            return {'suspicious': False, 'reason': 'Insufficient data'}

        # Calculate time between clicks
        intervals = []
        for i in range(1, len(clicks)):
            interval = clicks[i]['timestamp'] - clicks[i-1]['timestamp']
            intervals.append(interval)

        # Bots often have very consistent intervals
        avg_interval = sum(intervals) / len(intervals)
        variance = sum((i - avg_interval) ** 2 for i in intervals) / len(intervals)

        if variance < 0.001 and len(intervals) > 3:
            return {
                'suspicious': True,
                'reason': 'Click intervals too consistent',
                'variance': variance
            }

        # Check for impossible speeds (< 50ms between clicks)
        fast_clicks = sum(1 for i in intervals if i < 0.05)
        if fast_clicks > len(intervals) * 0.5:
            return {
                'suspicious': True,
                'reason': 'Too many fast clicks',
                'fast_clicks': fast_clicks
            }

        return {'suspicious': False}

    @staticmethod
    def analyze_keyboard_pattern(keystrokes: list[dict]) -> dict:
        """
        Analyze keyboard typing patterns.

        Args:
            keystrokes: List of {'key': str, 'timestamp': float}

        Returns:
            Analysis results
        """
        if not keystrokes or len(keystrokes) < 5:
            return {'suspicious': False, 'reason': 'Insufficient data'}

        # Calculate intervals
        intervals = []
        for i in range(1, len(keystrokes)):
            interval = keystrokes[i]['timestamp'] - keystrokes[i-1]['timestamp']
            intervals.append(interval)

        avg_interval = sum(intervals) / len(intervals)

        # Impossibly fast typing (< 20ms average)
        if avg_interval < 0.02:
            return {
                'suspicious': True,
                'reason': 'Typing too fast for human',
                'avg_interval': avg_interval
            }

        # Too consistent (bot)
        variance = sum((i - avg_interval) ** 2 for i in intervals) / len(intervals)
        if variance < 0.0001:
            return {
                'suspicious': True,
                'reason': 'Typing rhythm too consistent',
                'variance': variance
            }

        return {'suspicious': False}


def generate_honeypot_html() -> str:
    """
    Generate HTML for honeypot fields.
    Include this in forms.

    Returns:
        HTML string with honeypot fields
    """
    import time

    html = f'''
    <!-- Honeypot fields - DO NOT FILL -->
    <div style="position: absolute; left: -9999px;">
        <input type="text" name="website" placeholder="Website" tabindex="-1" autocomplete="off">
        <input type="text" name="url" placeholder="URL" tabindex="-1" autocomplete="off">
        <input type="email" name="email_confirm" placeholder="Confirm Email" tabindex="-1" autocomplete="off">
    </div>

    <!-- Timing field -->
    <input type="hidden" name="_form_start_time" value="{time.time()}">

    <!-- JavaScript check (set by JS) -->
    <input type="hidden" name="_js_check" value="">

    <!-- JavaScript to set JS check -->
    <script>
        document.addEventListener('DOMContentLoaded', function() {{
            document.querySelector('[name="_js_check"]').value = '1';
        }});
    </script>
    '''

    return html
