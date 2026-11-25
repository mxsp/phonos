import sqlite3
import redis
import json
import os
import sys

# Add production directory to path for config
sys.path.insert(0, os.path.abspath('production'))
from config import Config

def check_failed_jobs():
    print("--- Checking Database for Failed Jobs ---")
    try:
        conn = sqlite3.connect(Config.DATABASE_FILE)
        cursor = conn.cursor()
        
        # Check for recently failed jobs (last 24 hours)
        cursor.execute("""
            SELECT job_id, status, model, created_at, error_message 
            FROM jobs 
            WHERE status = 'failed' 
            ORDER BY created_at DESC 
            LIMIT 10
        """)
        
        rows = cursor.fetchall()
        if not rows:
            print("No recently failed jobs found in database.")
        else:
            print(f"Found {len(rows)} recently failed jobs:")
            for row in rows:
                print(f"  Job ID: {row[0]}, Model: {row[2]}, Created: {row[3]}, Error: {row[4]}")
        
        # Check for stuck 'processing' jobs (older than 1 hour)
        cursor.execute("""
            SELECT job_id, status, model, created_at 
            FROM jobs 
            WHERE status = 'processing' 
            AND created_at < datetime('now', '-1 hour')
            ORDER BY created_at DESC 
            LIMIT 10
        """)
        
        rows = cursor.fetchall()
        if not rows:
            print("No stuck processing jobs found in database.")
        else:
            print(f"Found {len(rows)} stuck processing jobs:")
            for row in rows:
                print(f"  Job ID: {row[0]}, Model: {row[2]}, Created: {row[3]}")
                
        conn.close()
    except Exception as e:
        print(f"Database error: {e}")

def check_dead_letter_queue():
    print("\n--- Checking Redis Dead Letter Queue ---")
    try:
        r = redis.Redis(
            host=Config.REDIS_HOST,
            port=Config.REDIS_PORT,
            db=Config.REDIS_DB,
            password=Config.REDIS_PASSWORD,
            decode_responses=True
        )
        
        queue_name = "queue:dead_letter"
        length = r.llen(queue_name)
        print(f"Dead Letter Queue Length: {length}")
        
        if length > 0:
            print("First 5 items:")
            items = r.lrange(queue_name, 0, 4)
            for item in items:
                try:
                    data = json.loads(item)
                    print(f"  Job ID: {data.get('job_id')}, Error: {data.get('final_error')}")
                except:
                    print(f"  (Invalid JSON): {item[:50]}...")
                    
    except Exception as e:
        print(f"Redis error: {e}")

if __name__ == "__main__":
    check_failed_jobs()
    check_dead_letter_queue()
