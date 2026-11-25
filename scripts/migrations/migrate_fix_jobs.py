#!/usr/bin/env python3
"""
Database migration script to fix missing batches table and batch_id column
"""

import os
import psycopg2
import psycopg2.extras
from urllib.parse import urlparse

def get_db_url():
    """Get database URL from environment or default"""
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        database_url = "postgresql://phonos_user:phonos_user@localhost:5432/phonos"
    return database_url

def migrate():
    """Run database migrations"""
    db_url = get_db_url()
    
    try:
        # Parse database URL
        parsed = urlparse(db_url)
        conn_params = {
            'host': parsed.hostname or 'localhost',
            'port': parsed.port or 5432,
            'database': parsed.path[1:] if parsed.path else 'phonos',
            'user': parsed.username or 'phonos_user',
            'password': parsed.password or 'phonos_user'
        }
        
        conn = psycopg2.connect(**conn_params)
        cursor = conn.cursor()
        
        print("Starting database migration...")
        
        # 1. Create batches table if not exists
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS batches (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255),
                project_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        print("✓ Created batches table")
        
        # 2. Add batch_id column to jobs table if not exists
        cursor.execute('''
            ALTER TABLE jobs 
            ADD COLUMN IF NOT EXISTS batch_id INTEGER
        ''')
        print("✓ Added batch_id column to jobs table")
        
        # 3. Add updated_at column if not exists
        cursor.execute('''
            ALTER TABLE jobs 
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ''')
        print("✓ Added updated_at column to jobs table")
        
        # Commit changes
        conn.commit()
        
        # Verify tables
        cursor.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'batches'")
        batches_exists = cursor.fetchone()[0] > 0
        print(f"✓ Batches table exists: {batches_exists}")
        
        cursor.execute("""
            SELECT COUNT(*) FROM information_schema.columns 
            WHERE table_name = 'jobs' AND column_name = 'batch_id'
        """)
        batch_id_exists = cursor.fetchone()[0] > 0
        print(f"✓ batch_id column exists: {batch_id_exists}")
        
        print("\n🎉 Migration completed successfully!")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        if 'conn' in locals():
            conn.close()
        return False

if __name__ == "__main__":
    success = migrate()
    exit(0 if success else 1)