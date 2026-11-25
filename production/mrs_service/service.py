"""Entry point for the Management & Routing Service.

SOTA Big Data & Pipeline Features:
- Priority Queue System with SLA monitoring
- Sequence Deduplication & Caching
- Parallel DAG Flow Execution
- Real-time Pipeline Analytics
"""
from __future__ import annotations

import threading
import time

from .monitors import monitor_jobs, monitor_worker_health
from .processors import process_job_results, process_new_jobs, monitor_sla_compliance
from .flow_processor import process_flow_executions, process_flow_continuations
from .recovery import recover_interrupted_jobs
from .registration import listen_for_node_registrations
from .runtime import logger, get_pipeline_stats


def log_pipeline_stats() -> None:
    """Periodically log pipeline statistics."""
    logger.info("Pipeline stats logger started.")

    while True:
        try:
            stats = get_pipeline_stats()

            # Log summary every 5 minutes
            total_queued = sum(stats.get("queue_depths", {}).values())
            active = stats.get("active_jobs", 0)
            workers = stats.get("worker_count", 0)
            cache_rate = stats.get("cache_hit_rate", 0) * 100

            logger.info(
                "Pipeline Stats: queued=%d, active=%d, workers=%d, cache_hit_rate=%.1f%%",
                total_queued, active, workers, cache_rate
            )

            time.sleep(300)  # Log every 5 minutes

        except Exception as exc:
            logger.error("Error logging pipeline stats: %s", exc)
            time.sleep(300)


def main() -> None:
    """Start all manager background threads.

    Thread Architecture:
    1. NewJobProcessor - Process incoming jobs with priority routing & dedup
    2. ResultProcessor - Handle job results with cache resolution
    3. FlowProcessor - Execute flows with parallel DAG support
    4. FlowContinuationProcessor - Continue sequential flows on job completion
    5. JobMonitor - Check for job timeouts and trigger retries
    6. WorkerHealthMonitor - Track worker heartbeats and prune dead workers
    7. NodeRegistrationListener - Handle worker node registration
    8. SLAMonitor - Track priority queue SLA compliance
    9. StatsLogger - Periodic pipeline statistics logging
    """
    logger.info("=" * 60)
    logger.info("Management & Routing Service (MRS) Starting Up")
    logger.info("=" * 60)
    logger.info("SOTA Features Enabled:")
    logger.info("  - Priority Queue System (4-tier with SLA guarantees)")
    logger.info("  - Sequence Deduplication & Caching")
    logger.info("  - Parallel DAG Flow Execution")
    logger.info("  - Real-time Pipeline Analytics")
    logger.info("=" * 60)

    recover_interrupted_jobs()

    threads = [
        # Core job processing
        threading.Thread(target=process_new_jobs, daemon=True, name="NewJobProcessor"),
        threading.Thread(target=process_job_results, daemon=True, name="ResultProcessor"),

        # Flow execution
        threading.Thread(target=process_flow_executions, daemon=True, name="FlowProcessor"),
        # threading.Thread(target=process_flow_continuations, daemon=True, name="FlowContinuationProcessor"),

        # Monitoring
        threading.Thread(target=monitor_jobs, daemon=True, name="JobMonitor"),
        threading.Thread(target=monitor_worker_health, daemon=True, name="WorkerHealthMonitor"),

        # Registration
        threading.Thread(target=listen_for_node_registrations, daemon=True, name="NodeRegistrationListener"),

        # SOTA: SLA monitoring
        threading.Thread(target=monitor_sla_compliance, daemon=True, name="SLAMonitor"),

        # SOTA: Pipeline stats logging
        threading.Thread(target=log_pipeline_stats, daemon=True, name="StatsLogger"),
    ]

    for thread in threads:
        thread.start()
        logger.info("Started thread: %s", thread.name)

    logger.info("All %d manager threads are running.", len(threads))

    while True:
        try:
            time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down Management Engine.")
            break


__all__ = ["main"]
