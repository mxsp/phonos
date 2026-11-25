#!/usr/bin/env python3
"""
Batch processing example for Phonos SDK.

This example demonstrates how to:
- Process multiple sequences in batch
- Upload a FASTA file
- Submit multiple jobs
- Track all jobs
- Download all results
"""

from phonos_sdk import PhonosClient
import os
import time


API_KEY = os.getenv('PHONOS_API_KEY', 'your_api_key_here')
PROJECT_ID = 1  # Replace with your project ID


def submit_batch_jobs(client, project_id, sequences):
    """Submit multiple jobs and return their IDs."""
    job_ids = []

    print(f"Submitting {len(sequences)} jobs...")
    for i, (name, seq) in enumerate(sequences.items(), 1):
        try:
            job = client.jobs.submit(
                project_id=project_id,
                model_id="type-0",  # ESMFold
                sequence=seq,
                job_name=f"Batch Job {i}: {name}"
            )
            job_id = job['job_id']
            job_ids.append(job_id)
            print(f"  [{i}/{len(sequences)}] Submitted: {job_id} ({name})")
        except Exception as e:
            print(f"  [{i}/{len(sequences)}] Failed to submit {name}: {e}")

    return job_ids


def monitor_jobs(client, job_ids):
    """Monitor all jobs until completion."""
    pending_jobs = set(job_ids)

    print(f"\nMonitoring {len(pending_jobs)} jobs...")
    while pending_jobs:
        for job_id in list(pending_jobs):
            try:
                logs = client.jobs.get_logs(job_id)
                status = logs.get('status', 'unknown')

                if status in ['completed', 'failed']:
                    print(f"  {job_id}: {status.upper()}")
                    pending_jobs.remove(job_id)
            except Exception as e:
                print(f"  Error checking {job_id}: {e}")

        if pending_jobs:
            print(f"  {len(pending_jobs)} jobs still running...")
            time.sleep(10)  # Check every 10 seconds

    print("\nAll jobs finished!")


def download_results(client, job_ids, output_dir="results"):
    """Download results for all completed jobs."""
    os.makedirs(output_dir, exist_ok=True)

    print(f"\nDownloading results to {output_dir}/...")
    for i, job_id in enumerate(job_ids, 1):
        try:
            output_path = os.path.join(output_dir, f"{job_id}.pdb")
            client.jobs.download_output(job_id, output_path)
            print(f"  [{i}/{len(job_ids)}] Downloaded: {output_path}")
        except Exception as e:
            print(f"  [{i}/{len(job_ids)}] Failed to download {job_id}: {e}")


def main():
    # Initialize client
    client = PhonosClient(api_key=API_KEY)

    # Example sequences (replace with your own)
    sequences = {
        "seq1": "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEK",
        "seq2": "MKWVTFISLLFLFSSAYSRGVFRRDAHKSEVAHRFKDLGEENFKALVLIAFAQYLQQCPFEDHVKLVNEVTEFAKTCVADESAENCDKSLHTLFGDKLCTVATLRETYGEMADCCAKQEPERNECFLQHKDDNPNLPRLVRPEVDVMCTAFHDNEETFLKKYLYEIARRHPYFYAPELLFFAKRYKAAFTECCQAADKAACLLPKLDELRDEGKASSAKQRLKCASLQKFGERAFKAWAVARLSQRFPKAEFAEVSKLVTDLTKVHTECC",
        "seq3": "GSHMKKILLLGLDGAGKTTILYKLKLGEIVTTIPTIGFNVETVEYKNISFTVWDVGGQDKIRPLWRHYFQNTQGLIFVVDSNDRERVNEAREELMRMLAEDELRDAVLLVFANKQDLPNAMNAAEITDKLGLHVLRQIFVERDPANLKGDPLMLTIRPCGNFALNIGVIHGKTTKLTLVDTPGFGDAVKSGSFLAELDTKIKFRIRWRDTFKNRSKLKVEQEQLENAGQEVFADLIRYQINNVEVPARPLFMKAVSREYPAEILEQSKDIRKTIRQRFGKIQALASKPGLGTETIAEKAKEIAETFKARGYSVTVQGKVADLIERARDTGLLKANGLKFPVNIFFDGDIDEAQMERVLGLVKEGDLEQEIQKIISSLGASGRVPKRQLYNVGTLTMDAVTAIEVLRKRGFSVEEVERAQINVDQRMQYIDASLLKNEHLRNEGVQYKDRLTLALRDSLNQLNEEKRHKQDMFAVQARKILEEGESVDIDFSLISAGPDEQAEKAEEIGKEIGKDTETIGGIIGGLGKELVASRSVSAKNEALSAQEIAAKLQAYGVSVEESKAAKFGVSAEVHKEVKEIAEGFGADWKSLEESIEDLKKLIEEILASGKDIEKATLEKVKILEKEHDQFLEHVRGRSALEAIGEGVEDLNEVLDTLKELNQLAASSGNEPVFAGFAAKELSSMKQTVARLVEELQKHGIDLLSDLLKKGESLKEELDKLIESISGKKSEQVLKKEKDLVKELGIPVEKAKAEANPILEKVKNELNKLVDSYGQKLVETIKELTDKFDALEELRQNSQNAADKVAQLKKKIQEDLENMKASLQSAGGQKGEGLSAELTDLIETIASILSKNSGEPKLEKVKEELSKLKEKVDELLDEYKEQFQGKSEKTTVEVLKELINRLAAQKGNEEHKVEKVKTELQKLLEEFGSVEQDTEGQILKKLDEELNKLEKEGGGHKDKAEQQLAILTELIEKGIEQLEEKLKALEKELGQE"
    }

    # Submit all jobs
    job_ids = submit_batch_jobs(client, PROJECT_ID, sequences)

    if not job_ids:
        print("No jobs were submitted successfully")
        return

    # Monitor until completion
    monitor_jobs(client, job_ids)

    # Download all results
    download_results(client, job_ids)

    print("\nBatch processing complete!")


if __name__ == '__main__':
    main()
