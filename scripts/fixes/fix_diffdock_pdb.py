#!/usr/bin/env python3
"""
Fix old DiffDock jobs by extracting receptor PDB from sequence column
and saving it as a proper PDB file.
"""

import os
import sys
import sqlite3
import psycopg2

def fix_diffdock_jobs():
    """Fix old DiffDock jobs with missing receptor PDB files."""

    # Connect to PostgreSQL
    conn = psycopg2.connect(
        host='localhost',
        database='phonos',
        user='phonos_user',
        password=os.environ.get('POSTGRES_PASSWORD', '')
    )
    cursor = conn.cursor()

    # Find all type-3 (DiffDock) completed jobs
    cursor.execute("""
        SELECT job_id, sequence, output_file
        FROM jobs
        WHERE model = 'type-3'
        AND status = 'completed'
        AND output_file IS NOT NULL
        AND sequence IS NOT NULL
    """)

    jobs = cursor.fetchall()
    print(f"Found {len(jobs)} DiffDock jobs to fix")

    fixed_count = 0
    for job_id, sequence, output_file in jobs:
        # Check if sequence contains PDB data
        if not sequence or 'ATOM' not in sequence[:500]:
            print(f"Skipping {job_id}: No PDB data in sequence")
            continue

        # Determine output directory from output_file path
        output_dir = os.path.dirname(output_file)
        if not os.path.exists(output_dir):
            print(f"Skipping {job_id}: Output directory doesn't exist: {output_dir}")
            continue

        # Create receptor PDB filename
        receptor_pdb_filename = f"{job_id}_receptor.pdb"
        receptor_pdb_path = os.path.join(output_dir, receptor_pdb_filename)

        # Check if receptor PDB already exists
        if os.path.exists(receptor_pdb_path):
            print(f"Skipping {job_id}: Receptor PDB already exists")
            # Update output_file to point to receptor
            cursor.execute("""
                UPDATE jobs
                SET output_file = %s
                WHERE job_id = %s
            """, (receptor_pdb_path, job_id))
            conn.commit()
            fixed_count += 1
            continue

        # Write receptor PDB file
        try:
            with open(receptor_pdb_path, 'w') as f:
                f.write(sequence)
            print(f"Created receptor PDB for {job_id}: {receptor_pdb_path}")

            # Update output_file in database to point to receptor PDB
            cursor.execute("""
                UPDATE jobs
                SET output_file = %s
                WHERE job_id = %s
            """, (receptor_pdb_path, job_id))
            conn.commit()

            fixed_count += 1
            print(f"✓ Fixed {job_id}")

        except Exception as e:
            print(f"✗ Error fixing {job_id}: {e}")
            continue

    cursor.close()
    conn.close()

    print(f"\nFixed {fixed_count} out of {len(jobs)} DiffDock jobs")
    return fixed_count

if __name__ == '__main__':
    try:
        fixed = fix_diffdock_jobs()
        print(f"\nSuccess! Fixed {fixed} DiffDock jobs.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
