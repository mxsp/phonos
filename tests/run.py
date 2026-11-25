import subprocess
import sys
import time

# Get the python interpreter executable
python_executable = sys.executable

# --- Default list of scripts to execute ---
# This list will be used if no scripts are provided via command line.
scripts_to_run = ['node1-esm.py',   'mrs.py', 'main.py',"node2-alphafold.py","node3-rosettafold.py"]

# --- Check for command-line arguments ---
# sys.argv[0] is always the script's name itself.
# sys.argv[1:] will contain any arguments passed after the script name.
if len(sys.argv) > 1:
    print("Executing scripts provided via command line.")
    scripts_to_run = sys.argv[1:]
else:
    print("No scripts provided via command line. Using default list.")


print("--- Main script started ---")
start_time = time.time()

# --- Start all scripts as separate processes ---
processes = []
for script in scripts_to_run:
    print(f"Launching {script}...")
    # Popen starts the process and continues, not waiting for it to finish
    # We pass the python interpreter and the script name as arguments
    try:
        proc = subprocess.Popen([python_executable, script])
        processes.append(proc)
    except FileNotFoundError:
        print(f"Error: Python executable '{python_executable}' not found. Make sure Python is in your PATH.")
        sys.exit(1)
    except Exception as e:
        print(f"Error launching {script}: {e}")

if not processes:
    print("No scripts were launched. Exiting.")
    sys.exit(0)

print("\nAll specified scripts have been launched concurrently.\n")

# --- Wait for all processes to complete ---
for process in processes:
    process.wait() # This will block until the specific process is finished

end_time = time.time()
print(f"\n--- All scripts have finished ---")
print(f"Total execution time: {end_time - start_time:.2f} seconds")