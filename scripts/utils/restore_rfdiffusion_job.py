import os
import json
import redis
import psycopg2
from dotenv import load_dotenv

# Load environment
load_dotenv()

# Redis config
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', None)

# Postgres config
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///phonos.db')

JOB_ID = 'd1d686ba-f3cb-480e-b724-1b2e330c1633'
QUEUE_NAME = 'queue:job:type-12'

def restore_job():
    print(f"Connecting to database: {DATABASE_URL}")
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    
    # Fetch job details
    cur.execute(
        "SELECT sequence, policy FROM jobs WHERE job_id = %s",
        (JOB_ID,)
    )
    row = cur.fetchone()
    
    if not row:
        print(f"Job {JOB_ID} not found in database!")
        return
        
    sequence_content, policy_json = row
    
    if not sequence_content:
        print("Error: No sequence content (PDB) found in job record.")
        return
        
    if not policy_json:
        print("Error: No policy metadata found in job record.")
        return
        
    try:
        policy = json.loads(policy_json)
    except json.JSONDecodeError:
        print("Error: Invalid JSON in policy column.")
        return
        
    custom_params = policy.get('custom_params', {})
    
    # Construct worker payload
    # Note: 'input_pdb' in custom_params seems to be an ID/Filename in the policy, 
    # but the worker expects the CONTENT. We use the 'sequence' column for content.
    
    params = {
        'input_pdb': sequence_content,
        'contigs': custom_params.get('contigs', ''),
        'hotspot_res': custom_params.get('hotspot_res', []),
        'diffusion_steps': custom_params.get('diffusion_steps', 50),
        'num_designs': custom_params.get('num_designs', 1),
        'noise_scale': custom_params.get('noise_scale', 1.0),
        'partial_T': custom_params.get('partial_T')
    }
    
    job_payload = {
        'job_id': JOB_ID,
        'params': params
    }
    
    print("Constructed job payload.")
    # print(json.dumps(job_payload, indent=2)) # Too large to print PDB
    
    # Push to Redis
    print(f"Connecting to Redis {REDIS_HOST}:{REDIS_PORT}")
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True)
    
    r.lpush(QUEUE_NAME, json.dumps(job_payload))
    print(f"Successfully pushed job to {QUEUE_NAME}")
    
    # Update DB status
    cur.execute(
        "UPDATE jobs SET status = 'queued', updated_at = NOW() WHERE job_id = %s",
        (JOB_ID,)
    )
    conn.commit()
    print("Updated job status to 'queued' in database.")
    
    cur.close()
    conn.close()

if __name__ == "__main__":
    restore_job()
