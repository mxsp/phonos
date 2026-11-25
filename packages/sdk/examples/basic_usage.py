#!/usr/bin/env python3
"""
Basic usage example for Phonos SDK.

This example demonstrates:
- Connecting to Phonos
- Listing available models
- Creating a project
- Submitting a job
- Monitoring job progress
- Downloading results
"""

from phonos_sdk import PhonosClient
import os
import time

# Set your API key (or use environment variable)
API_KEY = os.getenv('PHONOS_API_KEY', 'your_api_key_here')


def main():
    # Initialize the client
    print("Connecting to Phonos...")
    client = PhonosClient(api_key=API_KEY)

    # Get user info
    try:
        user = client.users.get_info()
        print(f"\nLogged in as: {user['username']}")
        print(f"Current credits: {user['credits_balance']}")
        print(f"Storage used: {user['storage_used']} bytes")
    except Exception as e:
        print(f"Error getting user info: {e}")
        return

    # List available models
    print("\n=== Available Models ===")
    models = client.get_available_models()
    for model in models[:5]:  # Show first 5
        print(f"  {model['id']}: {model['name']}")
        print(f"    Task: {model['task']}")
        print(f"    Cost: {model['credit_cost']} credits")
        print()

    # List existing projects
    print("=== Your Projects ===")
    projects = client.projects.list()
    if projects:
        for project in projects[:3]:  # Show first 3
            print(f"  {project['id']}: {project['name']}")
    else:
        print("  No projects found")

    # Example: Create a new project (uncomment to use)
    # print("\nCreating a new project...")
    # project = client.projects.create(
    #     name="Example Project",
    #     description="Testing Phonos SDK"
    # )
    # print(f"Created project: {project['name']} (ID: {project['id']})")

    # Example: Submit a protein folding job (uncomment to use)
    # Replace with your actual project_id
    # PROJECT_ID = 1
    # SEQUENCE = "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEK"
    #
    # print(f"\nSubmitting job to project {PROJECT_ID}...")
    # job = client.jobs.submit(
    #     project_id=PROJECT_ID,
    #     model_id="type-0",  # ESMFold
    #     sequence=SEQUENCE,
    #     job_name="Example Folding Job"
    # )
    # job_id = job['job_id']
    # print(f"Job submitted: {job_id}")
    #
    # # Monitor job progress
    # print("\nMonitoring job progress...")
    # while True:
    #     logs = client.jobs.get_logs(job_id)
    #     status = logs.get('status', 'unknown')
    #     print(f"  Status: {status}")
    #
    #     if status in ['completed', 'failed']:
    #         break
    #
    #     time.sleep(5)  # Check every 5 seconds
    #
    # if status == 'completed':
    #     print("\nJob completed! Downloading results...")
    #     client.jobs.download_output(job_id, "output.pdb")
    #     print("Results saved to output.pdb")
    # else:
    #     print(f"\nJob failed with status: {status}")

    print("\nDone!")


if __name__ == '__main__':
    main()
