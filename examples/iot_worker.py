#!/usr/bin/env python3
"""
Phonos IoT Device Worker
Submit sensor data to Phonos platform via v2 API

Usage:
    export PHONOS_API_KEY="dk_your_device_api_key"
    export PHONOS_API_URL="https://your-phonos-server.com"
    python iot_worker.py
"""

import os
import time
import json
import random
import logging
import requests
from datetime import datetime
from typing import Dict, Any, Optional, List

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PhonosIoTWorker:
    """IoT device worker for Phonos platform."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "http://localhost:5000",
        poll_interval: int = 30
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.poll_interval = poll_interval
        self.api_prefix = f"{self.base_url}/api/iot/v2"
        self.running = False

    def _headers(self) -> Dict[str, str]:
        """Get request headers with device authentication."""
        return {
            "Content-Type": "application/json",
            "X-Device-Key": self.api_key
        }

    def submit_data(self, data: Dict[str, Any], data_type: str = "reading") -> Optional[Dict]:
        """
        Submit sensor data to Phonos.

        Args:
            data: Dictionary of sensor readings (e.g., {"temperature": 25.5})
            data_type: Type of data ("reading", "event", "alert")

        Returns:
            Response dict with data_id and any pending commands
        """
        try:
            payload = {
                "data": data,
                "type": data_type,
                "device_timestamp": int(time.time())
            }

            response = requests.post(
                f"{self.api_prefix}/device/data",
                headers=self._headers(),
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                logger.info(f"Data submitted: {data} -> data_id={result.get('data_id')}")

                # Process any commands returned
                if result.get('commands'):
                    self._process_commands(result['commands'])

                return result
            else:
                logger.error(f"Submit failed: {response.status_code} - {response.text}")
                return None

        except requests.exceptions.RequestException as e:
            logger.error(f"Connection error: {e}")
            return None

    def get_commands(self) -> List[Dict]:
        """Poll for pending commands."""
        try:
            response = requests.get(
                f"{self.api_prefix}/device/commands",
                headers=self._headers(),
                timeout=30
            )

            if response.status_code == 200:
                result = response.json()
                commands = result.get('commands', [])
                if commands:
                    logger.info(f"Received {len(commands)} commands")
                return commands
            else:
                logger.error(f"Get commands failed: {response.status_code}")
                return []

        except requests.exceptions.RequestException as e:
            logger.error(f"Connection error: {e}")
            return []

    def acknowledge_command(self, command_id: str, success: bool = True, response_data: Any = None, error: str = None):
        """Acknowledge command execution."""
        try:
            payload = {
                "command_id": command_id,
                "success": success
            }
            if response_data:
                payload["response"] = response_data
            if error:
                payload["error"] = error

            response = requests.post(
                f"{self.api_prefix}/device/commands/ack",
                headers=self._headers(),
                json=payload,
                timeout=30
            )

            if response.status_code == 200:
                logger.info(f"Command {command_id} acknowledged")
            else:
                logger.error(f"Ack failed: {response.status_code}")

        except requests.exceptions.RequestException as e:
            logger.error(f"Connection error: {e}")

    def heartbeat(self):
        """Send heartbeat to maintain online status."""
        try:
            response = requests.post(
                f"{self.api_prefix}/device/heartbeat",
                headers=self._headers(),
                timeout=10
            )

            if response.status_code == 200:
                logger.debug("Heartbeat sent")
            else:
                logger.warning(f"Heartbeat failed: {response.status_code}")

        except requests.exceptions.RequestException as e:
            logger.warning(f"Heartbeat error: {e}")

    def _process_commands(self, commands: List[Dict]):
        """Process received commands."""
        for cmd in commands:
            command_id = cmd.get('command_id')
            cmd_type = cmd.get('type')
            target = cmd.get('target')
            payload = cmd.get('payload', {})

            logger.info(f"Processing command: {cmd_type} -> {target} = {payload}")

            try:
                # Handle command based on type
                if cmd_type == 'set_value':
                    result = self.handle_set_value(target, payload.get('value'))
                    self.acknowledge_command(command_id, success=True, response_data=result)
                else:
                    logger.warning(f"Unknown command type: {cmd_type}")
                    self.acknowledge_command(command_id, success=False, error=f"Unknown command: {cmd_type}")

            except Exception as e:
                logger.error(f"Command execution failed: {e}")
                self.acknowledge_command(command_id, success=False, error=str(e))

    def handle_set_value(self, target: str, value: Any) -> Dict:
        """
        Handle set_value command - override this in your implementation.

        Args:
            target: The input/output ID to set
            value: The value to set

        Returns:
            Result dict
        """
        logger.info(f"SET {target} = {value}")
        # Implement your device-specific logic here
        # e.g., control GPIO, relay, actuator, etc.
        return {"set": target, "value": value, "status": "ok"}

    def read_sensors(self) -> Dict[str, Any]:
        """
        Read sensor data - override this in your implementation.

        Returns:
            Dictionary of sensor readings
        """
        # Example: Replace with actual sensor readings
        return {
            "temperature": round(20 + random.uniform(-5, 10), 2),
            "humidity": round(50 + random.uniform(-20, 30), 1),
            "pressure": round(1013 + random.uniform(-10, 10), 1),
            "timestamp": datetime.now().isoformat()
        }

    def run(self):
        """Main worker loop."""
        logger.info(f"Starting IoT worker, polling every {self.poll_interval}s")
        logger.info(f"API endpoint: {self.api_prefix}")

        self.running = True
        last_heartbeat = 0
        heartbeat_interval = 60  # Send heartbeat every 60 seconds

        while self.running:
            try:
                # Read and submit sensor data
                data = self.read_sensors()
                self.submit_data(data)

                # Send heartbeat periodically
                now = time.time()
                if now - last_heartbeat > heartbeat_interval:
                    self.heartbeat()
                    last_heartbeat = now

                # Wait for next poll
                time.sleep(self.poll_interval)

            except KeyboardInterrupt:
                logger.info("Shutting down...")
                self.running = False
            except Exception as e:
                logger.error(f"Worker error: {e}")
                time.sleep(5)  # Brief pause before retry

    def stop(self):
        """Stop the worker."""
        self.running = False


# Example: Custom worker for specific sensors
class TemperatureSensorWorker(PhonosIoTWorker):
    """Example custom worker for temperature sensor."""

    def __init__(self, api_key: str, base_url: str, sensor_pin: int = 4):
        super().__init__(api_key, base_url, poll_interval=60)
        self.sensor_pin = sensor_pin
        # Initialize your sensor library here
        # e.g., import Adafruit_DHT

    def read_sensors(self) -> Dict[str, Any]:
        """Read from DHT22 sensor."""
        # Example for DHT22 on Raspberry Pi:
        # humidity, temperature = Adafruit_DHT.read_retry(
        #     Adafruit_DHT.DHT22, self.sensor_pin
        # )

        # Simulated for demo
        temperature = round(22 + random.uniform(-2, 2), 1)
        humidity = round(55 + random.uniform(-5, 5), 1)

        return {
            "temperature": temperature,
            "humidity": humidity,
            "unit": "celsius"
        }

    def handle_set_value(self, target: str, value: Any) -> Dict:
        """Handle actuator commands."""
        if target == "fan":
            # Control fan relay
            logger.info(f"Fan {'ON' if value else 'OFF'}")
            # GPIO.output(FAN_PIN, value)
            return {"fan": value}
        elif target == "heater":
            # Control heater relay
            logger.info(f"Heater {'ON' if value else 'OFF'}")
            # GPIO.output(HEATER_PIN, value)
            return {"heater": value}
        else:
            return super().handle_set_value(target, value)


if __name__ == "__main__":
    # Configuration from environment variables
    API_KEY = os.environ.get("PHONOS_API_KEY")
    API_URL = os.environ.get("PHONOS_API_URL", "http://localhost:5000")
    POLL_INTERVAL = int(os.environ.get("PHONOS_POLL_INTERVAL", "30"))

    if not API_KEY:
        print("Error: PHONOS_API_KEY environment variable required")
        print("Get your device API key from the Phonos IoT dashboard")
        exit(1)

    # Create and run worker
    worker = PhonosIoTWorker(
        api_key=API_KEY,
        base_url=API_URL,
        poll_interval=POLL_INTERVAL
    )

    # Or use custom sensor worker:
    # worker = TemperatureSensorWorker(
    #     api_key=API_KEY,
    #     base_url=API_URL,
    #     sensor_pin=4
    # )

    worker.run()
