#!/usr/bin/env python3
"""
Fix DiffDock jobs where ligand SDF files were incorrectly named with .pdb extension.
Also creates receptor PDB files from original protein data when missing.
"""

import os
import sys
import sqlite3
import psycopg2
import json
import glob

def fix_diffdock_jobs():
    """Fix old DiffDock jobs with incorrectly named ligand files."""

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
        SELECT job_id, output_file, artifacts, sequence
        FROM jobs
        WHERE model = 'type-3'
        AND status = 'completed'
        AND output_file IS NOT NULL
    """)
    jobs = cursor.fetchall()
    print(f"Found {len(jobs)} DiffDock jobs to check")

    fixed_count = 0
    skipped_count = 0

    for job_id, output_file, artifacts, sequence in jobs:
        if not output_file:
            print(f"Skipping {job_id}: No output_file")
            skipped_count += 1
            continue

        output_dir = os.path.dirname(output_file)
        if not os.path.exists(output_dir):
            print(f"Skipping {job_id}: Output directory doesn't exist")
            skipped_count += 1
            continue

        # Check if main output file has .pdb extension but contains SDF content
        if output_file.endswith('.pdb') and os.path.exists(output_file):
            try:
                with open(output_file, 'r') as f:
                    content = f.read(500)

                if 'V2000' in content or 'V3000' in content or 'RDKit' in content:
                    # This is an SDF file incorrectly named as .pdb
                    print(f"Found incorrectly named SDF file for {job_id}: {output_file}")

                    # Rename to .sdf
                    new_output_file = output_file[:-4] + '.sdf'
                    os.rename(output_file, new_output_file)
                    print(f"  Renamed to: {new_output_file}")

                    # Update database
                    cursor.execute("""
                        UPDATE jobs
                        SET output_file = %s
                        WHERE job_id = %s
                    """, (new_output_file, job_id))
                    conn.commit()

                    # Also update artifacts if present
                    if artifacts:
                        try:
                            artifacts_list = json.loads(artifacts) if isinstance(artifacts, str) else artifacts
                            for artifact in artifacts_list:
                                if artifact.get('filename', '').endswith('.pdb'):
                                    artifact_path = os.path.join(output_dir, artifact['filename'])
                                    if os.path.exists(artifact_path):
                                        with open(artifact_path, 'r') as f:
                                            art_content = f.read(500)
                                        if 'V2000' in art_content or 'V3000' in art_content or 'RDKit' in art_content:
                                            new_artifact_filename = artifact['filename'][:-4] + '.sdf'
                                            new_artifact_path = os.path.join(output_dir, new_artifact_filename)
                                            os.rename(artifact_path, new_artifact_path)
                                            artifact['filename'] = new_artifact_filename
                                            artifact['path'] = new_artifact_path
                                            print(f"  Also renamed artifact: {artifact['filename']}")
                            # Save updated artifacts
                            cursor.execute("""
                                UPDATE jobs
                                SET artifacts = %s
                                WHERE job_id = %s
                            """, (json.dumps(artifacts_list), job_id))
                            conn.commit()
                        except Exception as e:
                            print(f"  Warning: Could not update artifacts: {e}")

                    fixed_count += 1
                    print(f"  ✓ Fixed {job_id}")
                    continue

            except Exception as e:
                print(f"Error checking {job_id}: {e}")
                skipped_count += 1
                continue

        # Check if we need to create a receptor PDB file
        pdb_files = glob.glob(os.path.join(output_dir, "*.pdb"))
        if not pdb_files:
            # No receptor PDB found, try to create one from sequence or protein data
            print(f"No receptor PDB found for {job_id}")

            # Try to get protein from sequence field or artifacts
            protein_pdb = None

            # Check if sequence contains PDB data
            if sequence and 'ATOM' in sequence[:500]:
                protein_pdb = sequence
                print(f"  Found PDB data in sequence field")
            elif artifacts:
                try:
                    artifacts_list = json.loads(artifacts) if isinstance(artifacts, str) else artifacts
                    for artifact in artifacts_list:
                        if 'protein' in artifact.get('name', '').lower() or 'receptor' in artifact.get('name', '').lower():
                            artifact_path = artifact.get('path')
                            if artifact_path and os.path.exists(artifact_path):
                                with open(artifact_path, 'r') as f:
                                    content = f.read()
                                if 'ATOM' in content[:500]:
                                    protein_pdb = content
                                    print(f"  Found protein in artifacts: {artifact['filename']}")
                                    break
                except Exception as e:
                    print(f"  Warning: Could not parse artifacts: {e}")

            if protein_pdb:
                receptor_filename = f"{job_id}_receptor.pdb"
                receptor_path = os.path.join(output_dir, receptor_filename)
                with open(receptor_path, 'w') as f:
                    f.write(protein_pdb)
                print(f"  Created receptor PDB: {receptor_path}")
                fixed_count += 1
            else:
                print(f"  Could not find protein data for {job_id}")
                skipped_count += 1

    cursor.close()
    conn.close()

    print(f"\nFixed {fixed_count} jobs, skipped {skipped_count}")
    return fixed_count

if __name__ == '__main__':
    try:
        fixed = fix_diffdock_jobs()
        print(f"\nSuccess! Fixed {fixed} DiffDock jobs.")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
