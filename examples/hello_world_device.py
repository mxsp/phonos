#!/usr/bin/env python3
"""
Hello World IoT Device
A simple example device that sends greetings to Phonos.

Usage:
    python hello_world_device.py

The device will:
1. Send a "Hello World" greeting every 10 seconds
2. Increment a counter with each message
3. Respond to any commands received
"""

import time
import requests

# Configuration
API_URL = "http://localhost:5000"
API_KEY = "dk_helloworld_aa6206bce810214c758c30247a2e14be"

def send_data(greeting: str, counter: int):
    """Send data to Phonos IoT platform."""
    try:
        response = requests.post(
            f"{API_URL}/api/iot/v2/device/data",
            headers={
                "Content-Type": "application/json",
                "X-Device-Key": API_KEY
            },
            json={
                "data": {
                    "greeting": greeting,
                    "counter": counter
                },
                "type": "reading"
            },
            timeout=10
        )

        result = response.json()
        print(f"[{counter}] Sent: {greeting}")

        # Check for commands
        if result.get("commands"):
            for cmd in result["commands"]:
                print(f"  -> Command received: {cmd}")
                handle_command(cmd)

        return result.get("success", False)

    except Exception as e:
        print(f"Error: {e}")
        return False

def handle_command(cmd):
    """Handle incoming commands from the platform."""
    cmd_type = cmd.get("type")
    target = cmd.get("target")
    payload = cmd.get("payload", {})

    if target == "message":
        new_message = payload.get("value", "")
        print(f"  -> Message updated to: {new_message}")

def main():
    """Main loop - sends Hello World every 10 seconds."""
    print("=" * 50)
    print("Hello World IoT Device")
    print("=" * 50)
    print(f"API URL: {API_URL}")
    print(f"Device Key: {API_KEY[:20]}...")
    print("=" * 50)
    print()

    counter = 0
    greetings = [
        "Hello, World!",
        "Greetings from IoT!",
        "Phonos says hi!",
        "Device is running!",
        "All systems go!"
    ]

    print("Starting device loop (Ctrl+C to stop)...")
    print()

    try:
        while True:
            # Pick a greeting
            greeting = greetings[counter % len(greetings)]

            # Send data
            send_data(greeting, counter)

            counter += 1

            # Wait 10 seconds
            time.sleep(10)

    except KeyboardInterrupt:
        print("\nDevice stopped.")

if __name__ == "__main__":
    main()
