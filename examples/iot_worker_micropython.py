"""
Phonos IoT Worker for MicroPython (ESP32/ESP8266)

Upload to device and run:
    import iot_worker_micropython
    iot_worker_micropython.run()
"""

import urequests
import ujson
import time
import machine

# Configuration - UPDATE THESE
API_KEY = "dk_your_device_api_key"
API_URL = "http://your-phonos-server.com"
POLL_INTERVAL = 30  # seconds

# Optional: WiFi config (if not already connected)
WIFI_SSID = "your_wifi_ssid"
WIFI_PASS = "your_wifi_password"


def connect_wifi():
    """Connect to WiFi network."""
    import network
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)

    if not wlan.isconnected():
        print(f"Connecting to {WIFI_SSID}...")
        wlan.connect(WIFI_SSID, WIFI_PASS)

        timeout = 20
        while not wlan.isconnected() and timeout > 0:
            time.sleep(1)
            timeout -= 1

        if wlan.isconnected():
            print(f"Connected: {wlan.ifconfig()[0]}")
        else:
            print("WiFi connection failed!")
            return False

    return True


def submit_data(data):
    """Submit sensor data to Phonos."""
    try:
        url = f"{API_URL}/api/iot/v2/device/data"
        headers = {
            "Content-Type": "application/json",
            "X-Device-Key": API_KEY
        }
        payload = ujson.dumps({
            "data": data,
            "type": "reading",
            "device_timestamp": time.time()
        })

        response = urequests.post(url, headers=headers, data=payload)
        result = response.json()
        response.close()

        print(f"Submitted: {data} -> {result.get('data_id')}")

        # Process commands if any
        if result.get('commands'):
            process_commands(result['commands'])

        return result

    except Exception as e:
        print(f"Submit error: {e}")
        return None


def process_commands(commands):
    """Handle commands from server."""
    for cmd in commands:
        cmd_type = cmd.get('type')
        target = cmd.get('target')
        payload = cmd.get('payload', {})
        value = payload.get('value')

        print(f"Command: {cmd_type} {target} = {value}")

        # Handle commands - customize for your device
        if target == 'led':
            led = machine.Pin(2, machine.Pin.OUT)
            led.value(1 if value else 0)
        elif target == 'relay':
            relay = machine.Pin(5, machine.Pin.OUT)
            relay.value(1 if value else 0)


def read_sensors():
    """Read sensor data - customize for your sensors."""
    # Example: Internal temperature (ESP32)
    # import esp32
    # temp = (esp32.raw_temperature() - 32) * 5/9

    # Example: DHT sensor
    # import dht
    # d = dht.DHT22(machine.Pin(4))
    # d.measure()
    # return {"temperature": d.temperature(), "humidity": d.humidity()}

    # Example: Analog sensor
    # adc = machine.ADC(machine.Pin(34))
    # adc.atten(machine.ADC.ATTN_11DB)
    # return {"value": adc.read()}

    # Demo data
    import random
    return {
        "temperature": 20 + random.random() * 10,
        "humidity": 50 + random.random() * 20,
        "uptime": time.time()
    }


def heartbeat():
    """Send heartbeat to server."""
    try:
        url = f"{API_URL}/api/iot/v2/device/heartbeat"
        headers = {
            "Content-Type": "application/json",
            "X-Device-Key": API_KEY
        }
        response = urequests.post(url, headers=headers, data="{}")
        response.close()
    except:
        pass


def run():
    """Main worker loop."""
    print("Phonos IoT Worker starting...")

    # Connect to WiFi
    if not connect_wifi():
        return

    print(f"Polling every {POLL_INTERVAL}s")

    last_heartbeat = 0

    while True:
        try:
            # Read and submit sensor data
            data = read_sensors()
            submit_data(data)

            # Heartbeat every 60s
            now = time.time()
            if now - last_heartbeat > 60:
                heartbeat()
                last_heartbeat = now

            # Wait
            time.sleep(POLL_INTERVAL)

        except KeyboardInterrupt:
            print("Stopped")
            break
        except Exception as e:
            print(f"Error: {e}")
            time.sleep(5)


# Auto-run on boot (optional)
# run()
