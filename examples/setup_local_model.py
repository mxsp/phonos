
import sys
import os
import json

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'production')))

from phonos_app.app import app
from phonos_app.helpers.user_models import create_user_model, create_model_api_key, get_user_models
from phonos_app.helpers.database import get_db

def setup():
    with app.app_context():
        db = get_db()
        
        # Ensure a user exists
        user_id = 1
        user_check = db.execute("SELECT id FROM users WHERE id = %s", (user_id,)).fetchone()
        
        if not user_check:
            print("Creating test user...")
            # We need to insert a user. Schema depends on the table, usually just username/email/credits
            # Check schema quickly? Assuming standard fields.
            try:
                # Insert minimal required fields
                # user_id is text (unique), username is text (unique)
                db.execute("""
                    INSERT INTO users (id, user_id, username, credits, created_at)
                    VALUES (%s, 'test-user-id-1', 'testuser', 1000, CURRENT_TIMESTAMP)
                """, (user_id,))
                db.commit()
            except Exception as e:
                # Rollback transaction on error to avoid InFailedSqlTransaction
                db.rollback()
                print(f"Failed to create user with ID 1: {e}")
                
                # Try creating without ID and getting it
                try:
                    cursor = db.execute("""
                        INSERT INTO users (user_id, username, credits, created_at)
                        VALUES ('test-user-id-auto', 'testuser_auto', 1000, CURRENT_TIMESTAMP)
                        RETURNING id
                    """)
                    user_id = cursor.lastrowid
                    db.commit()
                    print(f"Created user with ID {user_id}")
                except Exception as e2:
                    db.rollback()
                    print(f"Retry failed: {e2}")
                    # Try to fetch existing user if insertion failed (maybe unique constraint)
                    cursor = db.execute("SELECT id FROM users LIMIT 1")
                    row = cursor.fetchone()
                    if row:
                        user_id = row['id']
                        print(f"Falling back to existing user ID {user_id}")
                    else:
                        raise e2
        
        # 1. Check for existing model
        models = get_user_models(user_id)
        model = None
        for m in models:
            if m['model_name'] == "Local PDB Worker":
                model = m
                break
        
        if not model:
            print("Creating new user model...")
            model = create_user_model(
                user_id=user_id,
                model_name="Local PDB Worker",
                model_description="Test model for random PDB worker",
                input_schema=[{"name": "sequence", "label": "Sequence", "type": "text"}],
                credit_cost=0
            )
        else:
            print(f"Using existing model: {model['model_id']}")
            
        # 2. Create API Key
        print("Creating new API key...")
        key_data = create_model_api_key(
            user_id=user_id,
            name=f"Worker Key {os.getpid()}",
            user_model_id=model['id']
        )
        
        print("\n" + "="*60)
        print("SETUP COMPLETE")
        print("="*60)
        print(f"Model ID: {model['model_id']}")
        print(f"API Key:  {key_data['full_key']}")
        print("\nRun the worker with:")
        print(f"export PHONOS_API_URL='http://localhost:5000/api/bridge/v1'")
        print(f"export PHONOS_API_KEY='{key_data['full_key']}'")
        print("python3 phonos/examples/random_pdb_worker.py")
        print("="*60 + "\n")

if __name__ == '__main__':
    setup()
