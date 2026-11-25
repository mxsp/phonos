import redis
import json
import os
from dotenv import load_dotenv

load_dotenv()

REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)

RESULTS_QUEUE = "queue:job_results"

print(f"Checking {RESULTS_QUEUE} length...")
length = r.llen(RESULTS_QUEUE)
print(f"Length: {length}")

if length > 0:
    print("Peek first item:")
    item = r.lindex(RESULTS_QUEUE, 0)
    print(item)
