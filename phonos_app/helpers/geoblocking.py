"""
Geoblocking middleware for Phonos.

Restricts access to users from North America and Europe only.
Uses MaxMind GeoLite2 database for IP geolocation.

To set up:
1. Register at https://www.maxmind.com/en/geolite2/signup
2. Download GeoLite2-Country.mmdb
3. Place it at $GEOLITE2_DB_PATH, or under <PROJECT_ROOT>/data/GeoLite2-Country.mmdb
4. Set GEOBLOCK_ENABLED=true in environment or config
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Optional, Set

from flask import Flask, request, abort, jsonify, render_template

logger = logging.getLogger(__name__)

# Allowed continent codes
# NA = North America, EU = Europe
ALLOWED_CONTINENTS: Set[str] = {'NA', 'EU'}

# Allowed country codes (ISO 3166-1 alpha-2)
# This includes all North American and European countries
ALLOWED_COUNTRIES: Set[str] = {
    # North America
    'US', 'CA', 'MX', 'GT', 'BZ', 'HN', 'SV', 'NI', 'CR', 'PA',
    'CU', 'JM', 'HT', 'DO', 'PR', 'BS', 'TT', 'BB', 'LC', 'VC',
    'GD', 'AG', 'DM', 'KN', 'AW', 'CW', 'SX', 'BQ', 'VI', 'VG',
    'AI', 'MS', 'TC', 'KY', 'BM', 'GL', 'PM', 'MQ', 'GP',

    # Europe (EU + non-EU European countries)
    'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR',
    'DE', 'GR', 'HU', 'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL',
    'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE',  # EU countries
    'GB', 'CH', 'NO', 'IS', 'LI', 'AD', 'MC', 'SM', 'VA',  # Non-EU Western Europe
    'AL', 'BA', 'XK', 'ME', 'MK', 'RS',  # Balkans
    'BY', 'MD', 'UA',  # Eastern Europe
}

# Repository root, used to derive a portable default DB location.
_PROJECT_ROOT = os.environ.get(
    'PHONOS_DIR',
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

# Paths to check for GeoLite2 database. An explicit GEOLITE2_DB_PATH (or the
# legacy GEOIP_DB_PATH) takes precedence, followed by portable, system-standard
# locations.
GEOLITE2_PATHS = [
    p for p in (
        os.environ.get('GEOLITE2_DB_PATH'),
        os.environ.get('GEOIP_DB_PATH'),
        os.path.join(_PROJECT_ROOT, 'data', 'GeoLite2-Country.mmdb'),
        '/var/lib/GeoIP/GeoLite2-Country.mmdb',
        '/usr/share/GeoIP/GeoLite2-Country.mmdb',
        os.path.expanduser('~/GeoLite2-Country.mmdb'),
    ) if p
]

# Routes that should bypass geoblocking (public/static assets)
BYPASS_PATHS = {
    '/static/',
    '/favicon',
    '/robots.txt',
    '/health',
    '/api/health',
    '/.well-known/',
}

# Cache for geoip2 reader
_geoip_reader = None
_geoip_available = None


def _find_geolite2_db() -> Optional[str]:
    """Find the GeoLite2 database file."""
    for path in GEOLITE2_PATHS:
        if os.path.exists(path):
            return path
    return None


def _get_geoip_reader():
    """Get or create the GeoIP2 reader."""
    global _geoip_reader, _geoip_available

    if _geoip_available is False:
        return None

    if _geoip_reader is not None:
        return _geoip_reader

    try:
        import geoip2.database
        db_path = _find_geolite2_db()
        if db_path:
            _geoip_reader = geoip2.database.Reader(db_path)
            _geoip_available = True
            logger.info(f"GeoIP2 reader initialized with database: {db_path}")
            return _geoip_reader
        else:
            logger.warning("GeoLite2-Country.mmdb not found. Geoblocking will use fallback.")
            _geoip_available = False
            return None
    except ImportError:
        logger.warning("geoip2 library not installed. Geoblocking will use fallback.")
        _geoip_available = False
        return None
    except Exception as e:
        logger.error(f"Failed to initialize GeoIP2 reader: {e}")
        _geoip_available = False
        return None


def get_client_ip() -> str:
    """Get the real client IP address, handling proxies."""
    # Check X-Forwarded-For header (set by nginx/proxy)
    if request.headers.get('X-Forwarded-For'):
        # First IP in the list is the original client
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()

    # Check X-Real-IP header
    if request.headers.get('X-Real-IP'):
        return request.headers.get('X-Real-IP').strip()

    # Fall back to remote_addr
    return request.remote_addr or '127.0.0.1'


@lru_cache(maxsize=10000)
def get_country_code(ip: str) -> Optional[str]:
    """
    Get the country code for an IP address.
    Returns None if the country cannot be determined.
    Results are cached for performance.
    """
    # Skip private/local IPs
    if ip.startswith(('127.', '10.', '192.168.', '172.16.', '172.17.',
                      '172.18.', '172.19.', '172.20.', '172.21.', '172.22.',
                      '172.23.', '172.24.', '172.25.', '172.26.', '172.27.',
                      '172.28.', '172.29.', '172.30.', '172.31.', 'localhost', '::1')):
        return 'LOCAL'

    reader = _get_geoip_reader()
    if reader is None:
        # Fallback: allow all if no database
        return None

    try:
        response = reader.country(ip)
        return response.country.iso_code
    except Exception as e:
        logger.debug(f"Could not determine country for IP {ip}: {e}")
        return None


def is_allowed_country(country_code: Optional[str]) -> bool:
    """Check if a country code is in the allowed list."""
    if country_code is None:
        # If we can't determine the country, allow by default (fail-open)
        return True
    if country_code == 'LOCAL':
        # Always allow local/private IPs
        return True
    return country_code in ALLOWED_COUNTRIES


def should_bypass_geoblocking(path: str) -> bool:
    """Check if a path should bypass geoblocking."""
    for bypass_path in BYPASS_PATHS:
        if path.startswith(bypass_path):
            return True
    return False


def check_geoblocking():
    """
    Flask before_request handler for geoblocking.
    Call this in your app's before_request to enable geoblocking.
    """
    # Skip for bypassed paths
    if should_bypass_geoblocking(request.path):
        return None

    client_ip = get_client_ip()
    country_code = get_country_code(client_ip)

    if not is_allowed_country(country_code):
        logger.warning(f"Geoblocked request from {client_ip} (country: {country_code})")

        # Return a 403 Forbidden response
        if request.path.startswith('/api/'):
            return jsonify({
                'error': 'Access denied',
                'message': 'This service is not available in your region.',
                'code': 'REGION_BLOCKED'
            }), 403
        else:
            # For HTML requests, return the EU flag animation page
            try:
                return render_template('geoblocked.html'), 403
            except Exception:
                # Fallback if template not found
                return '<h1>Access Restricted</h1><p>This service is only available in North America and Europe.</p>', 403

    return None


def register_geoblocking(app: Flask, enabled: bool = True) -> None:
    """
    Register geoblocking middleware with a Flask app.

    Args:
        app: Flask application instance
        enabled: Whether geoblocking is enabled (can be controlled via config)
    """
    if not enabled:
        logger.info("Geoblocking is disabled")
        return

    # Check if GeoIP database is available
    reader = _get_geoip_reader()
    if reader is None:
        logger.warning(
            "Geoblocking enabled but GeoLite2 database not found. "
            "Please download from https://dev.maxmind.com/geoip/geolite2-free-geolocation-data "
            "and place at one of: " + ", ".join(GEOLITE2_PATHS)
        )

    @app.before_request
    def geoblocking_middleware():
        return check_geoblocking()

    logger.info("Geoblocking middleware registered (NA + EU only)")


# For manual testing
if __name__ == '__main__':
    test_ips = [
        '8.8.8.8',        # Google DNS (US)
        '1.1.1.1',        # Cloudflare (US)
        '185.199.108.133', # GitHub (US)
        '139.130.4.5',    # Australia
        '203.119.101.1',  # Japan
        '193.0.14.129',   # Netherlands
        '127.0.0.1',      # Local
    ]

    for ip in test_ips:
        country = get_country_code(ip)
        allowed = is_allowed_country(country)
        print(f"{ip}: {country} - {'ALLOWED' if allowed else 'BLOCKED'}")
