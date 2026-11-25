"""Flow execution and processing helpers."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..app import get_redis_connection, logger, socketio
from . import get_db, create_user_notification
from config import Config


class FlowExecutor:
    """Handles the execution of flows."""

    def __init__(self, execution_id: str):
        self.execution_id = execution_id
        self.db = get_db()
        # self.cursor = self.db.cursor() # Not needed with PostgreSQL wrapper

    def execute(self):
        """Execute the flow."""
        try:
            # Get execution details
            result = self.db.execute("""
                SELECT
                    fe.flow_id, fe.project_id, fe.creator_id,
                    fe.current_step, fe.total_steps,
                    f.flow_data, f.name
                FROM flow_executions fe
                JOIN flows f ON f.flow_id = fe.flow_id
                WHERE fe.execution_id = %s
            """, (self.execution_id,))

            row = result.fetchone()
            if not row:
                logger.error(f"Execution {self.execution_id} not found")
                return

            flow_id = row['flow_id']
            project_id = row['project_id']
            creator_id = row['creator_id']
            current_step = row['current_step']
            total_steps = row['total_steps']
            flow_data = json.loads(row['flow_data']) if row['flow_data'] else {}
            flow_name = row['name']

            nodes = flow_data.get('nodes', [])
            connections = flow_data.get('connections', [])

            logger.info(f"Starting flow execution {self.execution_id} for flow {flow_id}")

            # Build execution order based on connections
            execution_order = self._build_execution_order(nodes, connections)

            # Execute each step in order
            for step_index in execution_order:
                node = nodes[step_index]

                # Update current step
                self.db.execute("""
                    UPDATE flow_executions
                    SET current_step = %s
                    WHERE execution_id = %s
                """, (step_index + 1, self.execution_id))
                self.db.commit()

                # Execute the step
                success = self._execute_step(step_index, node, project_id, creator_id)

                if not success:
                    # Mark execution as failed
                    self.db.execute("""
                        UPDATE flow_executions
                        SET status = 'failed', completed_at = CURRENT_TIMESTAMP
                        WHERE execution_id = %s
                    """, (self.execution_id,))
                    self.db.commit()

                    create_user_notification(
                        self.db,
                        creator_id,
                        'Flow Execution Failed',
                        f'Your flow "{flow_name}" failed at step {step_index + 1}.',
                        category='flow',
                        metadata={'flow_id': flow_id, 'execution_id': self.execution_id}
                    )

                    socketio.emit('flow_execution_failed', {
                        'execution_id': self.execution_id,
                        'flow_id': flow_id,
                        'project_id': project_id,
                        'step_index': step_index
                    }, room=f'project_{project_id}')

                    return

            # Mark execution as completed
            self.db.execute("""
                UPDATE flow_executions
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                WHERE execution_id = %s
            """, (self.execution_id,))
            self.db.commit()

            create_user_notification(
                self.db,
                creator_id,
                'Flow Execution Completed',
                f'Your flow "{flow_name}" has completed successfully.',
                category='flow',
                metadata={'flow_id': flow_id, 'execution_id': self.execution_id}
            )

            socketio.emit('flow_execution_completed', {
                'execution_id': self.execution_id,
                'flow_id': flow_id,
                'project_id': project_id
            }, room=f'project_{project_id}')

            logger.info(f"Flow execution {self.execution_id} completed successfully")

        except Exception as e:
            logger.exception(f"Error executing flow {self.execution_id}")
            if self.db:
                try:
                    self.db.execute("""
                        UPDATE flow_executions
                        SET status = 'failed', error_message = %s, completed_at = CURRENT_TIMESTAMP
                        WHERE execution_id = %s
                    """, (str(e), self.execution_id))
                    self.db.commit()
                except Exception:
                    pass

    def _build_execution_order(self, nodes: List[Dict], connections: List[Dict]) -> List[int]:
        """Build the order in which nodes should be executed based on connections."""
        # Simple topological sort
        # For now, just execute in the order they appear
        # In a production system, this would respect dependencies
        return list(range(len(nodes)))

    def _execute_step(self, step_index: int, node: Dict, project_id: int, creator_id: int) -> bool:
        """Execute a single step in the flow."""
        try:
            step_type = node.get('type', 'unknown')

            # Update step status to running
            self.db.execute("""
                UPDATE flow_execution_steps
                SET status = 'running', started_at = CURRENT_TIMESTAMP
                WHERE execution_id = %s AND step_index = %s
            """, (self.execution_id, step_index))
            self.db.commit()

            logger.info(f"Executing step {step_index} of type {step_type}")

            # Map flow node types to job submissions
            job_id = None

            if step_type in ['folding', 'docking']:
                # Submit a job for this step
                job_id = self._submit_job_for_step(node, project_id, creator_id)

                if job_id:
                    # Wait for job completion (in production, this would be async)
                    # For now, just mark the step as completed
                    self.db.execute("""
                        UPDATE flow_execution_steps
                        SET job_id = %s, status = 'completed', completed_at = CURRENT_TIMESTAMP
                        WHERE execution_id = %s AND step_index = %s
                    """, (job_id, self.execution_id, step_index))
                    self.db.commit()
                    return True
                else:
                    raise Exception("Failed to submit job for step")

            elif step_type in ['databank', 'upload', 'ligand']:
                # Data source nodes - these would fetch data
                # For now, mark as completed
                self.db.execute("""
                    UPDATE flow_execution_steps
                    SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                    WHERE execution_id = %s AND step_index = %s
                """, (self.execution_id, step_index))
                self.db.commit()
                return True

            elif step_type in ['notify', 'viewer']:
                # Utility nodes - these would perform utility functions
                # For now, mark as completed
                self.db.execute("""
                    UPDATE flow_execution_steps
                    SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                    WHERE execution_id = %s AND step_index = %s
                """, (self.execution_id, step_index))
                self.db.commit()
                return True

            else:
                logger.warning(f"Unknown step type: {step_type}")
                self.db.execute("""
                    UPDATE flow_execution_steps
                    SET status = 'skipped', completed_at = CURRENT_TIMESTAMP
                    WHERE execution_id = %s AND step_index = %s
                """, (self.execution_id, step_index))
                self.db.commit()
                return True

        except Exception as e:
            logger.exception(f"Error executing step {step_index}")
            self.db.execute("""
                UPDATE flow_execution_steps
                SET status = 'failed', error_message = %s, completed_at = CURRENT_TIMESTAMP
                WHERE execution_id = %s AND step_index = %s
            """, (str(e), self.execution_id, step_index))
            self.db.commit()
            return False

    def _submit_job_for_step(self, node: Dict, project_id: int, creator_id: int) -> Optional[str]:
        """Submit a job based on the flow step configuration."""
        try:
            step_type = node.get('type')
            node_data = node.get('data', {})

            # Map step types to model IDs
            model_map = {
                'folding': 'esmfold',
                'docking': 'diffdock'
            }

            model = model_map.get(step_type)
            if not model:
                logger.error(f"No model mapping for step type: {step_type}")
                return None

            # Generate job ID
            job_id = str(uuid.uuid4())

            # Insert job into database
            # This would need to be adapted to your actual job submission logic
            self.db.execute("""
                INSERT INTO jobs (
                    job_id, project_id, creator_id, model, status,
                    name, policy, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, 'queued', %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (
                job_id,
                project_id,
                creator_id,
                model,
                f"Flow step: {node.get('title', step_type)}",
                json.dumps(node_data)
            ))
            self.db.commit()

            # Submit to job queue via Redis
            redis_conn = get_redis_connection()
            if redis_conn:
                redis_conn.publish('job_queue', json.dumps({
                    'job_id': job_id,
                    'model': model,
                    'project_id': project_id,
                    'user_id': creator_id,
                    'params': node_data
                }))

            logger.info(f"Submitted job {job_id} for flow step")
            return job_id

        except Exception as e:
            logger.exception("Error submitting job for flow step")
            return None


def process_flow_execution(execution_id: str):
    """Process a flow execution (to be called from background worker)."""
    executor = FlowExecutor(execution_id)
    executor.execute()


def get_flow_statistics(flow_id: str) -> Dict[str, Any]:
    """Get statistics for a flow."""
    db = get_db()
    # cursor = db.cursor() # Not needed with PostgreSQL wrapper

    result = db.execute("""
        SELECT
            COUNT(*) as total_executions,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running
        FROM flow_executions
        WHERE flow_id = %s
    """, (flow_id,))

    row = result.fetchone()

    return {
        'total_executions': row['total_executions'] or 0,
        'completed': row['completed'] or 0,
        'failed': row['failed'] or 0,
        'running': row['running'] or 0
    }


def validate_flow_data(flow_data: Dict) -> tuple[bool, Optional[str]]:
    """Validate flow data structure."""
    if not isinstance(flow_data, dict):
        return False, "Flow data must be a dictionary"

    if 'nodes' not in flow_data:
        return False, "Flow data must contain 'nodes'"

    if 'connections' not in flow_data:
        return False, "Flow data must contain 'connections'"

    if not isinstance(flow_data['nodes'], list):
        return False, "'nodes' must be a list"

    if not isinstance(flow_data['connections'], list):
        return False, "'connections' must be a list"

    # Validate each node has required fields
    for i, node in enumerate(flow_data['nodes']):
        if not isinstance(node, dict):
            return False, f"Node {i} must be a dictionary"

        if 'type' not in node:
            return False, f"Node {i} missing 'type' field"

        if 'id' not in node:
            return False, f"Node {i} missing 'id' field"

    # Validate connections reference valid nodes
    node_ids = {node['id'] for node in flow_data['nodes']}
    for i, conn in enumerate(flow_data['connections']):
        if not isinstance(conn, dict):
            return False, f"Connection {i} must be a dictionary"

        if 'from' not in conn or 'to' not in conn:
            return False, f"Connection {i} missing 'from' or 'to' field"

        if conn['from'] not in node_ids:
            return False, f"Connection {i} references invalid 'from' node"

        if conn['to'] not in node_ids:
            return False, f"Connection {i} references invalid 'to' node"

    return True, None
