import requests
import json
import sys
import time
import os

# --- Configuration ---
# 1. The URL of your Phonos platform
BASE_URL = os.environ.get("PHONOS_API_URL", "http://localhost:5000")

# 2. The "Specialized Session Cookie" (API Key) for your worker
# You get this when you create a Key for your User Model in the dashboard.
WORKER_TOKEN = os.environ.get("PHONOS_API_KEY", "pk_...")

def plug_into_platform():
    """
    Simulates the worker 'plugging in' to the platform.
    Verifies the session token and retrieves configuration.
    """
    print(f"🔌 Connecting to Phonos Platform at {BASE_URL}...")
    
    headers = {
        "Authorization": f"Bearer {WORKER_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        # 1. Handshake / Session Verification
        # This confirms the key is valid and retrieves the worker's scope
        print("   Verifying session credentials...")
        response = requests.get(
            f"{BASE_URL}/api/bridge/v1/worker/info",
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        
        info = response.json()
        
        if info['status'] == 'success':
            print("\n✅ CONNECTION SUCCESSFUL")
            print(f"   Worker ID: {info.get('user_id')}")
            print(f"   Rate Limit: {info.get('rate_limit')} requests/min")
            
            print("\n   Authorized Models:")
            for model in info.get('authorized_models', []):
                print(f"   - {model['name']} (ID: {model['id']})")
                
            print("\n   Authorized Projects:")
            for project in info.get('authorized_projects', []):
                print(f"   - {project['name']}")
                
            return True
        else:
            print(f"\n❌ Connection Refused: {info.get('message')}")
            return False

    except requests.exceptions.ConnectionError:
        print(f"\n❌ Could not reach server at {BASE_URL}")
        print("   Is the Phonos platform running?")
        return False
    except requests.exceptions.HTTPError as e:
        print(f"\n❌ Authentication Failed: {e}")
        if e.response.status_code == 401:
            print("   The Worker Token (API Key) is invalid.")
        return False
    except Exception as e:
        print(f"\n❌ Unexpected Error: {e}")
        return False

def listen_for_work():
    """
    Enters a loop listening for assigned jobs.
    """
    print("\n📡 Listening for jobs (Ctrl+C to stop)...")
    
    headers = {
        "Authorization": f"Bearer {WORKER_TOKEN}",
        "Content-Type": "application/json"
    }
    
    try:
        while True:
            try:
                # Poll the bridge
                response = requests.post(
                    f"{BASE_URL}/api/bridge/v1/jobs/poll",
                    headers=headers,
                    json={"max_jobs": 1},
                    timeout=30 # Long polling timeout
                )
                
                if response.status_code == 200:
                    data = response.json()
                    jobs = data.get('jobs', [])
                    
                    if jobs:
                        print(f"\n🔔 JOB RECEIVED: {len(jobs)} new task(s)")
                        for job in jobs:
                            print(f"   - Job ID: {job['job_id']}")
                            print(f"   - Model ID: {job['model_id']}")
                            # Here is where you would process the job...
                    else:
                        sys.stdout.write(".")
                        sys.stdout.flush()
                else:
                    print(f"\n⚠️  Poll Error: {response.status_code}")
                    
            except requests.exceptions.Timeout:
                continue # Just poll again
            except Exception as e:
                print(f"\n⚠️  Connection Error: {e}")
                time.sleep(5) # Backoff
            
            time.sleep(2) # Polling interval

    except KeyboardInterrupt:
        print("\n\n🔌 Unplugged. Worker shutting down.")

if __name__ == "__main__":
    if "pk_" not in WORKER_TOKEN:
        print("⚠️  WARNING: Please set the PHONOS_API_KEY environment variable.")
        print("   export PHONOS_API_KEY='pk_...'")
        sys.exit(1)

    if plug_into_platform():
        listen_for_work()
