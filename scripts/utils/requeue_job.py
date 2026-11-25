import redis
import json
import os
from dotenv import load_dotenv

load_dotenv()

REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)

JOB_ID = 'd1d686ba-f3cb-480e-b724-1b2e330c1633'
ACTIVE_JOBS_HASH = 'active_jobs'
QUEUE_NAME = 'queue:job:type-12'

# Check active jobs hash
job_info_json = r.hget(ACTIVE_JOBS_HASH, JOB_ID)
if job_info_json:
    print(f"Job found in ACTIVE_JOBS_HASH")
    job_info = json.loads(job_info_json)
    print(f"Status in Redis: {job_info.get('status')}")
    
    # Check if we have job_data
    if 'job_data' in job_info:
        print("Job data found, requeueing...")
        job_data = job_info['job_data']
        # It might be a string or dict
        if isinstance(job_data, str):
            # Verify it's valid JSON
            try:
                json.loads(job_data)
                # It's a string, push as is
                r.lpush(QUEUE_NAME, job_data)
            except:
                # Not valid JSON string?
                print("Job data string is not valid JSON?")
                r.lpush(QUEUE_NAME, json.dumps(job_data))
        else:
            r.lpush(QUEUE_NAME, json.dumps(job_data))
            
        print(f"Job pushed to {QUEUE_NAME}")
        
        # Remove from active hash so it doesn't look like it's running twice?
        # Actually, if we push it, a worker will pop it and overwrite/update the active hash entry.
        # But if it's currently marked 'processing', the worker might expect it to NOT be there?
        # Standard flow: Worker pops -> Set active -> Process -> Remove active -> Push result
        # So we should probably remove it from active hash to be clean.
        r.hdel(ACTIVE_JOBS_HASH, JOB_ID)
        print("Removed from ACTIVE_JOBS_HASH")
    else:
        print("No job_data in active entry, cannot requeue.")
else:
    print("Job NOT found in ACTIVE_JOBS_HASH")
    
    # Check if it's already in the queue?
    # Scanning queue is expensive/slow if large, but let's just peek?
    # Nah, if it's processing in DB and not in active hash, it's lost.
    # Without payload, we can't requeue easily.
