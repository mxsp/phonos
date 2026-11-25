#!/usr/bin/env python3
"""
Phonos IoT Client - Secure device communication
No external dependencies - uses Python standard library only

Usage:
    python3 iot_client.py --api-key YOUR_KEY --device-id sensor-001 --project-id 1

Or as a library:
    from iot_client import PhonosIoT
    client = PhonosIoT("your_api_key")
    client.send_data("device-001", 1, {"temperature": 23.5})
"""
import argparse
import hashlib
import hmac
import json
import random
import time
import urllib.request
import urllib.error


class PhonosIoT:
    """Secure client for Phonos IoT Bridge API."""

    def __init__(self, api_key, endpoint="https://phonos.fyreflysystems.de"):
        self.api_key = api_key
        self.endpoint = endpoint.rstrip("/")

    def _sign_request(self, payload, timestamp):
        """Generate HMAC-SHA256 signature for request."""
        message = f"{timestamp}.{json.dumps(payload, sort_keys=True)}"
        return hmac.new(
            self.api_key.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def _request(self, path, payload):
        """Make signed request to API."""
        timestamp = int(time.time())
        payload["timestamp"] = timestamp
        signature = self._sign_request(payload, timestamp)

        url = f"{self.endpoint}/api/iot/v1/{path}?api_key={self.api_key}"
        data = json.dumps(payload, sort_keys=True).encode("utf-8")

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Phonos-Timestamp", str(timestamp))
        req.add_header("X-Phonos-Signature", signature)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"error": True, "message": e.read().decode("utf-8"), "code": e.code}
        except Exception as e:
            return {"error": True, "message": str(e)}

    def send_data(self, device_id, project_id, data, metadata=None):
        """Send sensor data to Phonos."""
        payload = {"device_id": device_id, "project_id": project_id, "data": data}
        if metadata:
            payload["metadata"] = metadata
        return self._request("data", payload)

    def register_device(self, device_id, project_id, name, device_type="generic"):
        """Register a new device."""
        payload = {
            "device_id": device_id,
            "project_id": project_id,
            "name": name,
            "device_type": device_type
        }
        return self._request("register", payload)


def generate_temperature_data():
    """Generate random temperature sensor data."""
    return {
        "temperature": round(random.uniform(18, 28), 2),
        "humidity": round(random.uniform(30, 70), 2),
        "pressure": round(random.uniform(1000, 1025), 1)
    }


def generate_bioreactor_data():
    """Generate random bioreactor data."""
    return {
        "ph": round(random.uniform(6.5, 7.5), 2),
        "dissolved_oxygen": round(random.uniform(4, 10), 2),
        "temperature": round(random.uniform(35, 38), 2),
        "agitation_rpm": random.randint(80, 250),
        "volume_liters": round(random.uniform(3, 10), 2)
    }


def generate_generic_data():
    """Generate random generic sensor data."""
    return {
        "value1": round(random.uniform(0, 100), 2),
        "value2": round(random.uniform(0, 100), 2),
        "status": random.choice(["ok", "warning", "normal", "active"]),
        "counter": random.randint(0, 10000)
    }


def main():
    parser = argparse.ArgumentParser(description="Phonos IoT Client")
    parser.add_argument("--api-key", required=True, help="IoT API key")
    parser.add_argument("--endpoint", default="https://phonos.fyreflysystems.de",
                        help="Phonos server URL")
    parser.add_argument("--device-id", required=True, help="Device identifier")
    parser.add_argument("--project-id", type=int, required=True, help="Project ID")
    parser.add_argument("--device-type", default="generic",
                        choices=["temperature_sensor", "bioreactor", "generic"],
                        help="Device type for data generation")
    parser.add_argument("--register", action="store_true", help="Register device first")
    parser.add_argument("--name", help="Device name (for registration)")
    parser.add_argument("--loop", type=int, default=1, help="Number of data sends")
    parser.add_argument("--interval", type=float, default=5, help="Seconds between sends")

    args = parser.parse_args()

    client = PhonosIoT(args.api_key, args.endpoint)

    # Register device if requested
    if args.register:
        name = args.name or f"{args.device_type.replace('_', ' ').title()} {args.device_id}"
        print(f"Registering device: {args.device_id}")
        result = client.register_device(
            args.device_id, args.project_id, name, args.device_type
        )
        print(f"Registration result: {result}")

    # Select data generator based on device type
    generators = {
        "temperature_sensor": generate_temperature_data,
        "bioreactor": generate_bioreactor_data,
        "generic": generate_generic_data
    }
    generate_data = generators.get(args.device_type, generate_generic_data)

    # Send data
    for i in range(args.loop):
        data = generate_data()
        print(f"\n[{i+1}/{args.loop}] Sending data: {data}")
        result = client.send_data(args.device_id, args.project_id, data)
        print(f"Result: {result}")

        if i < args.loop - 1:
            time.sleep(args.interval)

    print("\nDone!")


if __name__ == "__main__":
    main()
