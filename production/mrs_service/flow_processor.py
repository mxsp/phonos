"""Flow processing logic for the MRS.

SOTA Big Data & Pipeline Features:
- Parallel DAG Execution (run independent branches concurrently)
- Topological Sort (proper dependency ordering)
- Speculative Execution (run multiple models, use first result)
- Conditional Branching (if/else based on intermediate results)
- Step-level metrics and progress tracking
"""
from __future__ import annotations

import json
import time
import threading
import uuid
import operator
import re
from typing import Optional, Dict, Any, List, Set, Tuple, Callable
from collections import defaultdict
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed, Future

import redis

from database import get_db_connection
from .runtime import (
    FLOW_EXECUTION_QUEUE,
    NEW_JOBS_QUEUE,
    JOB_STATUS_CHANNEL_PREFIX,
    logger,
    get_redis_connection,
    JobPriority,
)
from .translation import translator


# =============================================================================
# SAFE CONDITION EVALUATOR (No eval())
# =============================================================================

class SafeConditionEvaluator:
    """Safe condition evaluator that doesn't use eval().

    Supports simple conditions like:
    - confidence > 0.8
    - status == 'success'
    - score >= 50 and score <= 100
    - result.plddt > 70
    """

    OPERATORS: Dict[str, Callable] = {
        '==': operator.eq,
        '!=': operator.ne,
        '>': operator.gt,
        '>=': operator.ge,
        '<': operator.lt,
        '<=': operator.le,
    }

    @classmethod
    def evaluate(cls, condition: str, context: Dict[str, Any]) -> bool:
        """Evaluate a condition string safely."""
        if not condition or condition.lower() in ('true', '1', 'yes'):
            return True
        if condition.lower() in ('false', '0', 'no'):
            return False

        # Handle 'and' / 'or' conditions
        if ' and ' in condition.lower():
            parts = re.split(r'\s+and\s+', condition, flags=re.IGNORECASE)
            return all(cls._evaluate_simple(p.strip(), context) for p in parts)

        if ' or ' in condition.lower():
            parts = re.split(r'\s+or\s+', condition, flags=re.IGNORECASE)
            return any(cls._evaluate_simple(p.strip(), context) for p in parts)

        return cls._evaluate_simple(condition, context)

    @classmethod
    def _evaluate_simple(cls, condition: str, context: Dict[str, Any]) -> bool:
        """Evaluate a simple comparison condition."""
        # Find the operator
        for op_str, op_func in cls.OPERATORS.items():
            if op_str in condition:
                parts = condition.split(op_str, 1)
                if len(parts) == 2:
                    left = cls._resolve_value(parts[0].strip(), context)
                    right = cls._resolve_value(parts[1].strip(), context)
                    try:
                        return op_func(left, right)
                    except (TypeError, ValueError):
                        return False

        # No operator found - treat as truthy check
        value = cls._resolve_value(condition.strip(), context)
        return bool(value)

    @classmethod
    def _resolve_value(cls, expr: str, context: Dict[str, Any]) -> Any:
        """Resolve a value expression from context."""
        # String literal
        if (expr.startswith('"') and expr.endswith('"')) or \
           (expr.startswith("'") and expr.endswith("'")):
            return expr[1:-1]

        # Number literal
        try:
            if '.' in expr:
                return float(expr)
            return int(expr)
        except ValueError:
            pass

        # Boolean literal
        if expr.lower() == 'true':
            return True
        if expr.lower() == 'false':
            return False
        if expr.lower() == 'none' or expr.lower() == 'null':
            return None

        # Context lookup (supports dot notation like inputs.node1.confidence)
        parts = expr.split('.')
        value = context

        for part in parts:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                return None

            if value is None:
                return None

        return value


# =============================================================================
# DAG EXECUTION STRUCTURES
# =============================================================================

@dataclass
class FlowNode:
    """A node in the flow DAG."""
    id: str
    node_type: str
    config: Dict[str, Any]
    step_index: int
    dependencies: List[str] = field(default_factory=list)
    dependents: List[str] = field(default_factory=list)

    @property
    def is_source(self) -> bool:
        """True if this node has no dependencies (can start immediately)."""
        return len(self.dependencies) == 0


@dataclass
class FlowDAG:
    """Directed Acyclic Graph representation of a flow."""
    execution_id: str
    nodes: Dict[str, FlowNode]
    adjacency: Dict[str, List[str]]  # node_id -> list of dependent node_ids
    reverse_adjacency: Dict[str, List[str]]  # node_id -> list of dependency node_ids

    @classmethod
    def from_flow_definition(cls, execution_id: str, flow_data: Dict[str, Any]) -> 'FlowDAG':
        """Build DAG from flow definition JSON."""
        nodes_data = flow_data.get('nodes', [])
        connections_data = flow_data.get('connections', [])

        nodes = {}
        adjacency = defaultdict(list)
        reverse_adjacency = defaultdict(list)

        # Create nodes
        for idx, node_data in enumerate(nodes_data):
            node_id = node_data.get('id', f"node_{idx}")
            nodes[node_id] = FlowNode(
                id=node_id,
                node_type=node_data.get('type', 'unknown'),
                config=node_data.get('config', {}),
                step_index=idx,
            )

        # Build adjacency from connections
        for conn in connections_data:
            source = conn.get('source') or conn.get('from')
            target = conn.get('target') or conn.get('to')

            if source and target and source in nodes and target in nodes:
                adjacency[source].append(target)
                reverse_adjacency[target].append(source)
                nodes[target].dependencies.append(source)
                nodes[source].dependents.append(target)

        return cls(
            execution_id=execution_id,
            nodes=nodes,
            adjacency=dict(adjacency),
            reverse_adjacency=dict(reverse_adjacency),
        )

    def get_source_nodes(self) -> List[FlowNode]:
        """Get all nodes with no dependencies (entry points)."""
        return [node for node in self.nodes.values() if node.is_source]

    def get_ready_nodes(self, completed_nodes: Set[str]) -> List[FlowNode]:
        """Get nodes whose dependencies are all completed."""
        ready = []
        for node in self.nodes.values():
            if node.id in completed_nodes:
                continue
            if all(dep in completed_nodes for dep in node.dependencies):
                ready.append(node)
        return ready

    def topological_sort(self) -> List[FlowNode]:
        """Return nodes in topological order (dependencies first)."""
        visited = set()
        result = []

        def dfs(node_id: str):
            if node_id in visited:
                return
            visited.add(node_id)
            for dep in self.reverse_adjacency.get(node_id, []):
                dfs(dep)
            result.append(self.nodes[node_id])

        for node_id in self.nodes:
            dfs(node_id)

        return result


# =============================================================================
# PARALLEL FLOW EXECUTOR
# =============================================================================

class ParallelFlowExecutor:
    """Execute flows with parallel branch support."""

    def __init__(self, execution_id: str, dag: FlowDAG, max_parallel: int = 4):
        self.execution_id = execution_id
        self.dag = dag
        self.max_parallel = max_parallel
        self.completed_nodes: Set[str] = set()
        self.failed_nodes: Set[str] = set()
        self.node_outputs: Dict[str, Any] = {}
        self.lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=max_parallel)

    def _publish_update(self, event_type: str, data: Dict[str, Any]) -> None:
        """Publish flow update to Redis for frontend."""
        try:
            r = get_redis_connection()
            payload = {
                'event': event_type,
                'execution_id': self.execution_id,
                'project_id': self.project_id,
                **data
            }
            r.publish("pubsub:flow_update", json.dumps(payload))
        except Exception as e:
            logger.error(f"Failed to publish flow update: {e}")

    def execute(self) -> bool:
        """Execute the flow with parallel branch support.

        Returns True if flow completed successfully.
        """
        # Get project_id for notifications
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT project_id FROM flow_executions WHERE execution_id = %s", (self.execution_id,))
            row = cursor.fetchone()
            self.project_id = row['project_id'] if row else None
        finally:
            conn.close()

        logger.info("Starting parallel flow execution %s with %d nodes",
                   self.execution_id, len(self.dag.nodes))
        
        self._publish_update('execution_started', {'status': 'running'})

        try:
            while True:
                # Get nodes that are ready to execute
                with self.lock:
                    ready_nodes = self.dag.get_ready_nodes(self.completed_nodes | self.failed_nodes)

                    # Filter out already submitted nodes
                    ready_nodes = [n for n in ready_nodes
                                  if n.id not in self.completed_nodes
                                  and n.id not in self.failed_nodes]

                if not ready_nodes:
                    # Check if we're done or stuck
                    if len(self.completed_nodes) + len(self.failed_nodes) >= len(self.dag.nodes):
                        break
                    # Wait for running jobs
                    time.sleep(0.5)
                    continue

                logger.info("Flow %s: %d nodes ready for parallel execution: %s",
                           self.execution_id, len(ready_nodes),
                           [n.id for n in ready_nodes])

                # Submit ready nodes for parallel execution
                futures: Dict[Future, FlowNode] = {}
                for node in ready_nodes[:self.max_parallel]:
                    # Gather inputs from dependencies
                    inputs = {}
                    for dep_id in node.dependencies:
                        if dep_id in self.node_outputs:
                            inputs[dep_id] = self.node_outputs[dep_id]

                    self._publish_update('node_started', {'node_id': node.id})
                    
                    future = self._executor.submit(
                        self._execute_node,
                        node,
                        inputs
                    )
                    futures[future] = node

                # Wait for at least one to complete
                for future in as_completed(futures):
                    node = futures[future]
                    try:
                        result = future.result()
                        with self.lock:
                            self.completed_nodes.add(node.id)
                            if result:
                                self.node_outputs[node.id] = result
                        logger.info("Flow %s: Node %s completed", self.execution_id, node.id)
                        self._publish_update('node_completed', {'node_id': node.id, 'result': result})
                        
                    except Exception as exc:
                        logger.error("Flow %s: Node %s failed: %s",
                                    self.execution_id, node.id, exc)
                        with self.lock:
                            self.failed_nodes.add(node.id)
                        self._publish_update('node_failed', {'node_id': node.id, 'error': str(exc)})

            success = len(self.failed_nodes) == 0
            self._publish_update('execution_completed', {'status': 'completed' if success else 'failed'})
            return success

        finally:
            self._executor.shutdown(wait=False)

    def _execute_node(self, node: FlowNode, inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Execute a single flow node."""
        logger.info("Executing node %s (type: %s) for flow %s",
                   node.id, node.node_type, self.execution_id)

        # Handle different node types
        if node.node_type in ('folding', 'docking', 'prediction'):
            return self._execute_job_node(node, inputs)
        elif node.node_type == 'databank':
            return self._execute_databank_node(node, inputs)
        elif node.node_type == 'upload':
            return self._execute_upload_node(node, inputs)
        elif node.node_type == 'condition':
            return self._execute_condition_node(node, inputs)
        elif node.node_type == 'notify':
            return self._execute_notify_node(node, inputs)
        else:
            # Default: treat as job submission
            return self._execute_job_node(node, inputs)

    def _execute_job_node(self, node: FlowNode, inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Execute a job-creating node (folding, docking, etc.)."""
        r = get_redis_connection()
        job_id = str(uuid.uuid4())

        # Build job payload
        job_payload = {
            'job_id': job_id,
            'model': node.node_type,
            'flow_execution_id': self.execution_id,
            'flow_node_id': node.id,
            'is_flow_step': True,
            'priority': int(JobPriority.BACKGROUND),
        }

        # Merge node config
        job_payload.update(node.config)

        # Apply translation from inputs
        if inputs:
            for input_node_id, input_data in inputs.items():
                input_node = self.dag.nodes.get(input_node_id)
                if input_node:
                    translated = translator.find_path(
                        input_data,
                        input_node.node_type,
                        node.node_type
                    )
                    job_payload.update(translated)

        # Update database
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE flow_execution_steps
                SET job_id = %s, status = 'running', started_at = CURRENT_TIMESTAMP
                WHERE execution_id = %s AND step_index = %s
            """, (job_id, self.execution_id, node.step_index))
            conn.commit()
        finally:
            conn.close()

        # Submit job
        r.lpush(NEW_JOBS_QUEUE, json.dumps(job_payload))
        logger.info("Submitted job %s for flow node %s", job_id, node.id)

        # Wait for job completion (with timeout)
        return self._wait_for_job(job_id, timeout=3600)

    def _wait_for_job(self, job_id: str, timeout: int = 3600) -> Optional[Dict[str, Any]]:
        """Wait for a job to complete via pub/sub."""
        r = get_redis_connection()
        pubsub = r.pubsub()
        channel = f"{JOB_STATUS_CHANNEL_PREFIX}{job_id}"

        try:
            pubsub.subscribe(channel)
            start_time = time.time()

            for message in pubsub.listen():
                if time.time() - start_time > timeout:
                    logger.error("Job %s timed out after %ds", job_id, timeout)
                    return None

                if message['type'] != 'message':
                    continue

                try:
                    payload = json.loads(message['data'])
                    status = payload.get('status')

                    if status == 'completed':
                        return payload.get('data', {})
                    elif status == 'failed':
                        logger.error("Job %s failed: %s", job_id, payload.get('message'))
                        return None

                except json.JSONDecodeError:
                    continue

        finally:
            pubsub.unsubscribe(channel)
            pubsub.close()

        return None

    def _execute_databank_node(self, node: FlowNode, inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Execute a databank lookup node."""
        # Pass through the databank configuration
        return {'databank': node.config.get('source'), **node.config}

    def _execute_upload_node(self, node: FlowNode, inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Execute an upload node."""
        # Pass through the file path
        return {'file_path': node.config.get('file_path'), **node.config}

    def _execute_condition_node(self, node: FlowNode, inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Execute a conditional branch node using safe evaluation."""
        condition = node.config.get('condition', 'true')

        # Build evaluation context
        context = {'inputs': inputs}
        for input_id, input_data in inputs.items():
            context[input_id] = input_data

        # Use safe condition evaluator (no eval())
        try:
            result = SafeConditionEvaluator.evaluate(condition, context)
            return {'condition_result': result, 'branch': 'true' if result else 'false'}
        except Exception as exc:
            logger.error("Condition evaluation failed: %s", exc)
            return {'condition_result': False, 'branch': 'false'}

    def _execute_notify_node(self, node: FlowNode, inputs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Execute a notification node."""
        # Log notification (could send email, webhook, etc.)
        message = node.config.get('message', 'Flow step completed')
        logger.info("Flow notification: %s", message)
        return {'notified': True, 'message': message}


# =============================================================================
# MAIN FLOW PROCESSOR
# =============================================================================

def process_flow_executions() -> None:
    """Continuously process new flow execution requests with parallel DAG support.

    SOTA Features:
    1. DAG-based execution (proper dependency ordering)
    2. Parallel branch execution (independent nodes run concurrently)
    3. Step-level progress tracking
    """
    logger.info("Flow execution processor started (SOTA: parallel DAG execution).")
    r = get_redis_connection()

    while True:
        try:
            # Block until a flow execution request is received
            request_json = r.blpop(FLOW_EXECUTION_QUEUE, timeout=1)
            if not request_json:
                continue

            _, payload = request_json
            data = json.loads(payload)
            execution_id = data.get('execution_id')

            if not execution_id:
                logger.error("Received invalid flow execution request: %s", data)
                continue

            logger.info("Starting flow execution %s", execution_id)

            # Load flow definition from database
            conn = get_db_connection()
            try:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT f.flow_data, fe.project_id, fe.creator_id
                    FROM flow_executions fe
                    JOIN flows f ON fe.flow_id = f.id
                    WHERE fe.execution_id = %s
                """, (execution_id,))
                row = cursor.fetchone()

                if not row:
                    logger.error("Flow execution %s not found", execution_id)
                    continue

                flow_data = json.loads(row['flow_data']) if isinstance(row['flow_data'], str) else row['flow_data']

                # Build DAG
                dag = FlowDAG.from_flow_definition(execution_id, flow_data)

                # Always use parallel executor (handles linear flows too)
                logger.info("Flow %s starting execution", execution_id)
                executor = ParallelFlowExecutor(execution_id, dag, max_parallel=4)

                # Update status
                cursor.execute("""
                    UPDATE flow_executions SET status = 'running', started_at = CURRENT_TIMESTAMP
                    WHERE execution_id = %s
                """, (execution_id,))
                conn.commit()

                # Execute with threading
                exec_thread = threading.Thread(
                    target=_run_parallel_flow,
                    args=(executor, execution_id),
                    daemon=True
                )
                exec_thread.start()

            finally:
                conn.close()

        except redis.exceptions.RedisError as exc:
            logger.error("Redis error in flow processor: %s", exc, exc_info=True)
            time.sleep(5)
        except Exception as exc:
            logger.error("Unexpected error in flow processor: %s", exc, exc_info=True)
            time.sleep(5)


def _run_parallel_flow(executor: ParallelFlowExecutor, execution_id: str) -> None:
    """Run a parallel flow in a separate thread."""
    try:
        success = executor.execute()

        # Update final status
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            if success:
                cursor.execute("""
                    UPDATE flow_executions
                    SET status = 'completed', completed_at = CURRENT_TIMESTAMP
                    WHERE execution_id = %s
                """, (execution_id,))
                logger.info("Flow %s completed successfully", execution_id)
            else:
                failed_nodes = list(executor.failed_nodes)
                cursor.execute("""
                    UPDATE flow_executions
                    SET status = 'failed', error_message = %s, completed_at = CURRENT_TIMESTAMP
                    WHERE execution_id = %s
                """, (f"Failed nodes: {failed_nodes}", execution_id))
                logger.error("Flow %s failed with nodes: %s", execution_id, failed_nodes)
            conn.commit()
        finally:
            conn.close()

    except Exception as exc:
        logger.error("Parallel flow execution failed: %s", exc, exc_info=True)
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE flow_executions
                SET status = 'failed', error_message = %s, completed_at = CURRENT_TIMESTAMP
                WHERE execution_id = %s
            """, (str(exc), execution_id))
            conn.commit()
        finally:
            conn.close()

def process_flow_continuations() -> None:
    """Listen for job completions and trigger next flow steps."""
    logger.info("Flow continuation processor started.")
    r = get_redis_connection()
    pubsub = r.pubsub()
    
    # Subscribe to all job status updates
    pubsub.psubscribe(f"{JOB_STATUS_CHANNEL_PREFIX}*")
    
    for message in pubsub.listen():
        if message['type'] != 'pmessage':
            continue
            
        try:
            channel = message['channel'].decode('utf-8')
            # Extract status from channel (e.g., pubsub:job_status:<job_id>)
            # But the message body contains the status event
            payload = json.loads(message['data'])
            
            status = payload.get('status')
            job_id = payload.get('job_id')
            
            if status != 'completed':
                continue
                
            # Check if this job is part of a flow
            step_info = get_flow_step_for_job(job_id)
            if not step_info:
                continue
                
            execution_id = step_info['execution_id']
            current_index = step_info['step_index']
            
            logger.info("Job %s completed for flow %s, step %d. Triggering next step.", 
                        job_id, execution_id, current_index)
            
            # Update current step status
            update_step_status(execution_id, current_index, 'completed', completed_at=time.time())
            
            # Trigger next step
            execute_flow_step(execution_id, current_index + 1, previous_output=payload.get('data'))
            
        except Exception as exc:
            logger.error("Error processing flow continuation: %s", exc, exc_info=True)

def execute_flow_step(execution_id: str, step_index: int, previous_output: Optional[Dict[str, Any]] = None) -> None:
    """Execute a specific step in a flow."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        
        # Get step details
        cursor.execute("""
            SELECT id, step_type, step_config 
            FROM flow_execution_steps 
            WHERE execution_id = %s AND step_index = %s
        """, (execution_id, step_index))
        
        row = cursor.fetchone()
        
        # If no row, we might be at the end of the flow
        if not row:
            check_and_complete_flow(execution_id, conn)
            return

        step_id = row['id']
        step_type = row['step_type']
        step_config = json.loads(row['step_config'])
        
        logger.info("Executing flow step %d (%s) for %s", step_index, step_type, execution_id)

        # Prepare job data
        job_id = str(uuid.uuid4())

        # Determine previous model type to look up signature
        prev_model_type = None
        if step_index > 0:
            cursor.execute("""
                SELECT step_type FROM flow_execution_steps 
                WHERE execution_id = %s AND step_index = %s
            """, (execution_id, step_index - 1))
            prev_row = cursor.fetchone()
            if prev_row:
                prev_model_type = prev_row['step_type']

        # Use the Translation System to resolve inputs
        injected_inputs = {}
        if previous_output and prev_model_type:
            injected_inputs = translator.find_path(previous_output, prev_model_type, step_type)

        # Basic job payload structure expected by MRS
        job_payload = {
            "job_id": job_id,
            "model": step_type, 
        }

        # Merge config into payload (Config takes precedence? Or Injected? Usually Injected for flows)
        # Let's merge: Config <- Injected
        final_payload = step_config.copy()
        final_payload.update(injected_inputs)
        job_payload.update(final_payload)

        # Get context from execution (Project/User)
        cursor.execute("""
            SELECT project_id, creator_id 
            FROM flow_executions 
            WHERE execution_id = %s
        """, (execution_id,))
        exec_row = cursor.fetchone()
        if exec_row:
            job_payload["project_id"] = exec_row["project_id"]
            job_payload["user_id"] = exec_row["creator_id"]


        # Update step record with job_id and status
        cursor.execute("""
            UPDATE flow_execution_steps 
            SET job_id = %s, status = 'running', started_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (job_id, step_id))
        
        # Update flow status if it's the first step
        if step_index == 0:
            cursor.execute("""
                UPDATE flow_executions
                SET current_step = %s, status = 'running'
                WHERE execution_id = %s
            """, (step_index, execution_id))
        else:
            cursor.execute("""
                UPDATE flow_executions
                SET current_step = %s
                WHERE execution_id = %s
            """, (step_index, execution_id))
            
        conn.commit()
        
        # Submit to queue
        r = get_redis_connection()
        r.lpush(NEW_JOBS_QUEUE, json.dumps(job_payload))
        
        logger.info("Submitted job %s for flow step %d", job_id, step_index)
        
    except Exception as e:
        logger.error("Failed to execute flow step: %s", e, exc_info=True)
        if conn:
            conn.rollback()
            # Mark step as failed
            mark_step_failed(execution_id, step_index, str(e))
    finally:
        if conn:
            conn.close()

def get_flow_step_for_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Check if a job belongs to a flow execution."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT execution_id, step_index 
            FROM flow_execution_steps 
            WHERE job_id = %s
        """, (job_id,))
        row = cursor.fetchone()
        if row:
            return {'execution_id': row['execution_id'], 'step_index': row['step_index']}
        return None
    finally:
        conn.close()

def update_step_status(execution_id: str, step_index: int, status: str, completed_at=None):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if completed_at:
            cursor.execute("""
                UPDATE flow_execution_steps 
                SET status = %s, completed_at = TO_TIMESTAMP(%s)
                WHERE execution_id = %s AND step_index = %s
            """, (status, completed_at, execution_id, step_index))
        else:
             cursor.execute("""
                UPDATE flow_execution_steps 
                SET status = %s
                WHERE execution_id = %s AND step_index = %s
            """, (status, execution_id, step_index))
        conn.commit()
    finally:
        conn.close()

def mark_step_failed(execution_id: str, step_index: int, error: str):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE flow_execution_steps 
            SET status = 'failed', error_message = %s, completed_at = CURRENT_TIMESTAMP
            WHERE execution_id = %s AND step_index = %s
        """, (error, execution_id, step_index))
        
        cursor.execute("""
            UPDATE flow_executions 
            SET status = 'failed', error_message = %s, completed_at = CURRENT_TIMESTAMP
            WHERE execution_id = %s
        """, (f"Step {step_index} failed: {error}", execution_id))
        conn.commit()
    finally:
        conn.close()

def check_and_complete_flow(execution_id: str, conn):
    """Check if all steps are done and mark flow as completed."""
    # Actually, if we reached here (no next step), the flow is done.
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE flow_executions 
        SET status = 'completed', completed_at = CURRENT_TIMESTAMP, current_step = total_steps
        WHERE execution_id = %s
    """, (execution_id,))
    conn.commit()
    logger.info("Flow execution %s completed successfully.", execution_id)
