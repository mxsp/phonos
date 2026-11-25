"""
Phonos Notebook WebSocket Handlers
Real-time kernel communication via Socket.IO
"""
import base64
import json
import time
import asyncio
import logging
import threading
import queue
from datetime import datetime
from functools import wraps
from typing import Optional

try:
    import zmq
except ImportError:
    zmq = None  # type: ignore

# Eventlet may patch queue.Queue; use the real OS-thread-safe queue for the
# drainer loops that communicate with the real ZMQ reader threads.
try:
    import eventlet.patcher
    _real_queue = eventlet.patcher.original('queue')
except Exception:
    _real_queue = queue

from flask import request, current_app
from flask_socketio import emit, join_room, leave_room, disconnect

# Use absolute imports for compatibility with importlib loading
try:
    from phonos_app.helpers.kernel_manager import kernel_manager
    from phonos_app.helpers.database import get_db
except ImportError:
    from .helpers.kernel_manager import kernel_manager
    from .helpers.database import get_db

logger = logging.getLogger(__name__)

# Track active output listeners by session_id (not socket SID)
_output_tasks = {}
_output_task_sessions = {}  # session_id -> True if listener running
# Track active reply-queue drainers by session_id
_reply_task_sessions = {}

# Track execution start times for duration_ms calculation: msg_id -> start_time_ms
_exec_start_times = {}
# Track execution msg_id -> session_db_id for DB updates
_exec_session_ids = {}
# Track execution msg_id -> database execution row id for exact completion matching
_exec_row_ids = {}
# Track cell timeout: cell_id -> (start_time, timeout_seconds)
_cell_timeouts = {}

# Per-session execution locks guarantee that code is sent to the kernel one
# cell at a time. This prevents interleaved execute_request/execute_reply
# pairs from updating the wrong execution record when a user runs multiple
# cells in quick succession.
_session_exec_locks: dict[str, threading.Lock] = {}

# Output buffers for replay on reconnect: session_id -> list of output dicts
# Each buffer stores the last N output messages so rejoining clients can
# catch up on outputs they missed during disconnect.
_output_buffers: dict[str, list[dict]] = {}
_MAX_BUFFERED_OUTPUTS = 500  # Max output messages to buffer per session

# Server-side mapping of msg_id -> cell_id so the output buffer can include
# cell_id in replayed messages, allowing the frontend to route outputs even
# when the msg_id is not in its pendingExecutions map (e.g. after page refresh).
_msg_to_cell: dict[str, str] = {}

# Chimera lang cells: compiled and run INSIDE the kernel container by the
# bind-mounted phonos.lang SDK module, reached through the ordinary Jupyter
# execute path (so they inherit timeouts, interrupt, stdin and streaming).
LANG_CELL_TYPES = ('c', 'cpp', 'rust', 'r')


def _make_lang_shim(lang: str, source: str) -> str:
    """One-line kernel shim for a C/C++/Rust/R cell. The source travels
    base64-encoded so no quoting or injection issue can arise."""
    encoded = base64.b64encode(source.encode('utf-8')).decode('ascii')
    return (f"import phonos.lang as __phonos_lang; "
            f"__phonos_lang.run('{lang}', '{encoded}')")


def _buffer_output(session_id: str, event: str, msg: dict):
    """Store an emitted event in the session's replay buffer.

    Records the original ``event`` name alongside the payload so replay can
    re-emit it as the *same* event — an ``execute_reply`` must never be
    replayed as a cell output. Enriches the payload with ``cell_id`` (resolved
    from ``_msg_to_cell`` via ``msg_id``) so replayed messages route to the
    correct cell even after a page refresh (when ``pendingExecutions`` is
    empty).
    """
    if 'cell_id' not in msg and 'msg_id' in msg:
        cell_id = _msg_to_cell.get(msg['msg_id'])
        if cell_id:
            msg = dict(msg, cell_id=cell_id)
    buf = _output_buffers.setdefault(session_id, [])
    buf.append({'event': event, 'data': msg})
    # Trim oldest entries when buffer exceeds limit (mutate in place so the
    # buffer object identity is preserved for concurrent readers).
    if len(buf) > _MAX_BUFFERED_OUTPUTS:
        del buf[:-_MAX_BUFFERED_OUTPUTS]


def _clear_cell_buffer(session_id: str, cell_id: str):
    """Drop buffered outputs and stale msg->cell mappings for a cell that is
    about to be re-executed.

    Without this, the replay that fires on every reconnect would resurrect
    outputs from the cell's *previous* run: an error that has since been fixed
    "sticks" to a cell that now runs clean, and as the whole history is
    re-appended it appears to spill onto neighbouring cells. Purging the cell's
    prior entries here makes the buffer hold only the latest run of each cell.
    """
    buf = _output_buffers.get(session_id)
    if buf:
        buf[:] = [e for e in buf if e['data'].get('cell_id') != cell_id]
    for mid in [mid for mid, cid in _msg_to_cell.items() if cid == cell_id]:
        _msg_to_cell.pop(mid, None)


def _emit_and_buffer(socketio, event: str, data: dict, room: str, session_id: Optional[str] = None):
    """Emit a SocketIO event and buffer it for replay if a session_id is provided."""
    socketio.emit(event, data, room=room, namespace='/notebook')
    if session_id:
        _buffer_output(session_id, event, data)


def _get_session_exec_lock(session_id: str) -> threading.Lock:
    """Return a per-session lock used to serialize code execution."""
    lock = _session_exec_locks.get(session_id)
    if lock is None:
        lock = threading.Lock()
        _session_exec_locks[session_id] = lock
    return lock


def _handle_execute_reply(
    socketio,
    session_id: str,
    room: str,
    parent_msg_id: Optional[str],
    status: str,
    execution_count: Optional[int] = None,
):
    """Update DB tracking and broadcast execute_reply for a completed cell."""
    exec_status = 'completed' if status == 'ok' else 'error'

    duration_ms = None
    if parent_msg_id in _exec_start_times:
        duration_ms = int(time.time() * 1000) - _exec_start_times.pop(parent_msg_id)

    exec_row_id = _exec_row_ids.pop(parent_msg_id, None)
    session_db_id = _exec_session_ids.pop(parent_msg_id, None)
    if exec_row_id:
        try:
            db = get_db()
            db.execute('''
                UPDATE notebook_executions
                SET status = %s, finished_at = CURRENT_TIMESTAMP, duration_ms = %s
                WHERE id = %s AND status = 'running'
            ''', (exec_status, duration_ms, exec_row_id))
            db.commit()
        except Exception as db_err:
            logger.warning(f"Failed to update execution record: {db_err}")
    elif session_db_id:
        # Fallback for rows created before msg_id was stored.
        try:
            db = get_db()
            db.execute('''
                UPDATE notebook_executions
                SET status = %s, finished_at = CURRENT_TIMESTAMP, duration_ms = %s
                WHERE session_id = %s AND status = 'running'
                AND id = (
                    SELECT id FROM notebook_executions
                    WHERE session_id = %s AND status = 'running'
                    ORDER BY started_at DESC LIMIT 1
                )
            ''', (exec_status, duration_ms, session_db_id, session_db_id))
            db.commit()
        except Exception as db_err:
            logger.warning(f"Failed to update execution record: {db_err}")

    # The cell finished — cancel its execution timeout so it cannot fire a
    # bogus notebook:cell_timeout + kernel interrupt minutes later.
    finished_cell_id = _msg_to_cell.get(parent_msg_id)
    if finished_cell_id:
        _cell_timeouts.pop(finished_cell_id, None)

    if parent_msg_id in _msg_to_cell:
        del _msg_to_cell[parent_msg_id]

    _emit_and_buffer(socketio, 'notebook:execute_reply', {
        'msg_id': parent_msg_id,
        'cell_id': finished_cell_id,
        'status': status,
        'execution_count': execution_count,
        'duration_ms': duration_ms
    }, room=room, session_id=session_id)


def get_user_from_session():
    """Get user info from Flask session (set during socket connection).

    Returns a dict with ``id`` and (when available) ``username`` and
    ``is_admin``. We resolve the username lazily from the database when the
    session only stores the user_id, so collaboration presence messages can
    show the actual name instead of the placeholder ``"User {id}"``.
    """
    from flask import session
    user_id = session.get('user_id')
    if not user_id:
        return None

    user = {'id': user_id}
    if session.get('username'):
        user['username'] = session.get('username')
        user['is_admin'] = bool(session.get('is_admin'))
    else:
        # Fall back to a DB lookup so legacy sessions still expose a real name.
        try:
            row = get_db().execute(
                'SELECT username, is_admin FROM users WHERE id = %s',
                (user_id,),
            ).fetchone()
            if row:
                user['username'] = row['username']
                user['is_admin'] = bool(row['is_admin'])
        except Exception:
            pass
    return user


def notebook_auth_required(f):
    """Decorator to require authentication for notebook socket events"""
    @wraps(f)
    def wrapped(*args, **kwargs):
        user = get_user_from_session()
        if not user:
            emit('notebook:error', {'message': 'Authentication required'})
            return
        return f(user, *args, **kwargs)
    return wrapped


def register_notebook_socket_handlers(socketio):
    """
    Register all notebook-related socket handlers.
    Call this from your main app initialization.
    """
    # Start the background stale-presence sweeper so silently-disconnected
    # collaborators are pruned even when no other events fire in a room.
    _start_collab_stale_sweeper(socketio)

    @socketio.on('connect', namespace='/notebook')
    def handle_connect():
        """Handle socket connection"""
        user = get_user_from_session()
        if not user:
            logger.warning("Unauthorized notebook socket connection attempt")
            disconnect()
            return False

        logger.info(f"Notebook socket connected: user {user['id']}, sid {request.sid}")
        emit('notebook:welcome', {'message': 'Connected to Phonos Notebook', 'user_id': user['id']})
        return True

    @socketio.on('disconnect', namespace='/notebook')
    def handle_disconnect(reason=None):
        """Handle socket disconnection"""
        sid = request.sid
        logger.info(f"Notebook socket disconnected: {sid}, reason: {reason}")

        # Remove output task marker for this socket
        if sid in _output_tasks:
            del _output_tasks[sid]

        # Clean up any terminal exec process attached to this socket. Without
        # this, closing the tab / refreshing / a network drop leaves the
        # docker-exec'd bash process, its pty, and the background stream
        # listener greenlet running forever inside the kernel container.
        _close_terminal_socket(sid)

        # Clean up collaboration presence for this socket (sid-keyed).
        # Removing by sid means a user's other open tabs/sessions stay alive.
        for nid in list(_collab_presence.keys()):
            if request.sid in _collab_presence.get(nid, {}):
                _collab_presence[nid].pop(request.sid, None)
                _collab_editors.get(nid, set()).discard(request.sid)
                if not _collab_presence[nid]:
                    del _collab_presence[nid]
                    _collab_editors.pop(nid, None)
                _broadcast_presence(nid)

    @socketio.on('notebook:join', namespace='/notebook')
    @notebook_auth_required
    def handle_join_session(user, data):
        """Join a kernel session room to receive outputs"""
        session_id = data.get('session_id')
        if not session_id:
            emit('notebook:error', {'message': 'No session ID provided'})
            return

        # Verify session belongs to user
        db = get_db()
        session_record = db.execute('''
            SELECT id, kernel_status FROM notebook_sessions
            WHERE session_token = %s AND user_id = %s
            AND kernel_status IN ('idle', 'busy', 'starting')
        ''', (session_id, user['id'])).fetchone()

        if not session_record:
            emit('notebook:error', {'message': 'Session not found or not active'})
            return

        # Join the session room
        room = f'notebook:{session_id}'
        join_room(room)

        logger.info(f"User {user['id']} joined session room {session_id}")

        # Replay buffered outputs to the rejoining client so outputs emitted
        # during a disconnect (e.g. page refresh, network blip) are not lost.
        # Only this socket receives the replay — already-connected clients
        # already have these outputs.
        if session_id in _output_buffers:
            replay_count = len(_output_buffers[session_id])
            if replay_count:
                logger.info(f"Replaying {replay_count} buffered outputs to user {user['id']} for session {session_id}")
                # Signal the start of a replay batch so the client clears each
                # cell's rendered outputs the first time this batch touches it,
                # then appends — making replay idempotent instead of piling
                # duplicate/stale outputs (and old errors) onto cells.
                emit('notebook:output_replay_start', {'count': replay_count})
                for entry in _output_buffers[session_id]:
                    # Re-emit under the ORIGINAL event name (execute_reply must
                    # not become an output) and mark it as replayed.
                    emit(entry['event'], dict(entry['data'], replay=True))
                emit('notebook:output_replay_done', {
                    'count': replay_count
                })

        # Get kernel session
        session = kernel_manager.get_session(session_id)

        emit('notebook:joined', {
            'session_id': session_id,
            'status': session.status if session else session_record['kernel_status']
        })

        # Virtual sessions (Wolfram service) have no ZMQ kernel; skip the
        # reply reader and IOPub listener.  Execution still works via HTTP.
        if session and getattr(session, 'is_virtual', False):
            _output_tasks[request.sid] = session_id
            return

        # Start the ZMQ reply reader as a SocketIO background task (green
        # thread under eventlet).  Replies are pushed into session.reply_queue
        # and drained by another green thread below so SocketIO emits happen
        # from green threads, not from the reader loop.
        if not session or not session._reply_reader_started:
            kernel_manager.start_reply_reader(session_id)
            socketio.start_background_task(
                kernel_manager._read_shell_replies, session_id
            )

        # Start a green-thread drainer for replies if not already running.
        if session_id not in _reply_task_sessions:
            _reply_task_sessions[session_id] = True
            socketio.start_background_task(
                _drain_reply_queue, session_id, room, socketio
            )

        # Start output listener if not already running for this SESSION
        # Track by session_id to avoid duplicate listeners on reconnect
        if session and session_id not in _output_task_sessions:
            # Use socketio.start_background_task for eventlet compatibility
            _output_task_sessions[session_id] = True  # Mark as started
            _output_tasks[request.sid] = session_id  # Track which session this socket joined
            socketio.start_background_task(
                _listen_and_broadcast_sync, session_id, room, socketio
            )
        else:
            # Just track the socket -> session mapping
            _output_tasks[request.sid] = session_id

    @socketio.on('notebook:leave', namespace='/notebook')
    @notebook_auth_required
    def handle_leave_session(user, data):
        """Leave a kernel session room"""
        session_id = data.get('session_id')
        if session_id:
            room = f'notebook:{session_id}'
            leave_room(room)
            logger.info(f"User {user['id']} left session room {session_id}")

        # Clean up socket -> session mapping
        sid = request.sid
        if sid in _output_tasks:
            del _output_tasks[sid]
        # Note: Don't remove _output_task_sessions here - the listener should
        # continue for other clients in the room. It will clean up when session ends.

    @socketio.on('notebook:execute', namespace='/notebook')
    @notebook_auth_required
    def handle_execute(user, data):
        """Handle code execution request"""
        session_id = data.get('session_id')
        code = data.get('code', '')
        cell_id = data.get('cell_id')
        cell_type = data.get('cell_type', 'code')
        logger.info(f"handle_execute called: session={session_id[:16] if session_id else None}, cell={cell_id}, type={cell_type}, code_len={len(code)}")

        if not session_id or not cell_id:
            emit('notebook:error', {'message': 'Missing session_id or cell_id'})
            return

        if not code.strip():
            emit('notebook:error', {'message': 'No code to execute', 'cell_id': cell_id})
            return

        # Enforce global viewer read-only ceiling
        try:
            from .helpers.lab_accounts import can_run_compute
            if not can_run_compute(get_db(), user['id']):
                emit('notebook:error', {
                    'message': 'Viewer role accounts are strictly read-only and cannot execute notebook code.',
                    'cell_id': cell_id,
                })
                return
        except Exception:
            pass

        # Handle Wolfram cells via Wolfram Cloud API (runs directly, no Docker kernel needed)
        if cell_type == 'wolfram':
            return _handle_wolfram_cell(user, session_id, cell_id, code)

        # Lang cells (C/C++/Rust/R) go down the standard kernel path as a
        # one-line phonos.lang shim; keep the user's source for the
        # notebook_executions record.
        raw_code = code
        if cell_type in LANG_CELL_TYPES:
            code = _make_lang_shim(cell_type, raw_code)

        # Verify session
        db = get_db()
        session_record = db.execute('''
            SELECT id, runtime_level FROM notebook_sessions
            WHERE session_token = %s AND user_id = %s
            AND kernel_status IN ('idle', 'busy')
        ''', (session_id, user['id'])).fetchone()

        if not session_record:
            emit('notebook:error', {
                'message': 'Session not found or not ready',
                'cell_id': cell_id
            })
            return

        # Execute synchronously. The per-session lock serializes dispatch so
        # overlapping runs cannot create execution records out of order.
        def do_execute():
            lock = _get_session_exec_lock(session_id)
            acquired = False
            try:
                acquired = lock.acquire(timeout=30)
                if not acquired:
                    raise RuntimeError("Could not acquire execution lock for session")

                session = kernel_manager.get_session(session_id)
                if not session:
                    # Wolfram sessions may not be in the in-memory kernel manager
                    # (e.g. after server restart). Route to _handle_wolfram_cell
                    # which handles missing sessions gracefully.
                    if session_record['runtime_level'] == 'wolfram':
                        if cell_type in LANG_CELL_TYPES:
                            return _emit_lang_needs_container(session_id, cell_id, cell_type)
                        return _handle_wolfram_cell(user, session_id, cell_id, code)
                    socketio.emit('notebook:error', {
                        'message': 'Kernel session not found',
                        'cell_id': cell_id
                    }, room=f'notebook:{session_id}', namespace='/notebook')
                    return

                # Zero-Docker Local Python Kernel sessions execute via HTTP subprocess worker
                if (getattr(session, 'connection_info', None) or {}).get('local_python'):
                    return _handle_local_python_cell(user, session, session_record, cell_id, code, raw_code)

                # Wolfram sessions don't have a Docker kernel — route to
                # _handle_wolfram_cell even when cell_type is 'code' (legacy
                # notebooks or new notebooks with kernel_type=wolfram).
                if getattr(session, 'runtime_level', None) == 'wolfram':
                    if cell_type in LANG_CELL_TYPES:
                        return _emit_lang_needs_container(session_id, cell_id, cell_type)
                    return _handle_wolfram_cell(user, session_id, cell_id, code)

                # Purge any buffered outputs from a PREVIOUS run of this cell so
                # a later reconnect replay cannot resurrect stale outputs/errors
                # onto a cell that is now being re-run.
                _clear_cell_buffer(session_id, cell_id)

                msg_id = kernel_manager.execute_code_sync(session_id, code, cell_id)

                # Track msg_id -> cell_id mapping for output buffer replay so
                # replayed outputs can be routed to the correct cell even when
                # the client's pendingExecutions Map was lost (page refresh).
                _msg_to_cell[msg_id] = cell_id

                # Track execution start time for duration calculation
                _exec_start_times[msg_id] = int(time.time() * 1000)
                _exec_session_ids[msg_id] = session_record['id']

                # Track cell timeout (session-scoped so one session's watchdog
                # can never time out / interrupt another session's cells).
                timeout_secs = getattr(session, 'execution_timeout', 300)
                _cell_timeouts[cell_id] = (time.time(), timeout_secs, session_id)

                # Log execution and store msg_id for exact completion matching.
                db = get_db()
                db.execute('''
                    INSERT INTO notebook_executions
                    (session_id, cell_id, msg_id, execution_count, source, status)
                    VALUES (%s, %s, %s, %s, %s, 'running')
                ''', (session_record['id'], cell_id, msg_id, session.execution_count, raw_code))
                db.commit()
                row = db.execute(
                    'SELECT id FROM notebook_executions WHERE msg_id = %s',
                    (msg_id,)
                ).fetchone()
                if row:
                    _exec_row_ids[msg_id] = row['id']

                socketio.emit('notebook:execute_started', {
                    'cell_id': cell_id,
                    'msg_id': msg_id,
                    'execution_count': session.execution_count
                }, room=f'notebook:{session_id}', namespace='/notebook')

                logger.info(f"Execution started: session={session_id}, cell={cell_id}, msg={msg_id}")

            except Exception as e:
                logger.error(f"Execute error: {e}")
                socketio.emit('notebook:error', {
                    'message': str(e),
                    'cell_id': cell_id
                }, room=f'notebook:{session_id}', namespace='/notebook')
            finally:
                if acquired:
                    try:
                        lock.release()
                    except RuntimeError:
                        pass

        do_execute()

    def _emit_lang_needs_container(session_id, cell_id, cell_type):
        """Friendly error for lang cells on the (container-less) Wolfram
        runtime; C/C++/Rust/R compile and run inside the kernel container."""
        label = {'c': 'C', 'cpp': 'C++', 'rust': 'Rust', 'r': 'R'}.get(cell_type, cell_type)
        socketio.emit('notebook:output', {
            'cell_id': cell_id,
            'output_type': 'error',
            'ename': 'RuntimeError',
            'evalue': f'{label} cells need a container kernel. Switch this notebook to the Hybrid runtime.',
            'traceback': [
                f'{label} cells run inside a kernel container (Python, Quantum or Hybrid runtime).',
                'This notebook is on the Wolfram runtime, which has no container.',
                'Use the toolbar "Change Runtime" → "Quantum + Wolfram (Experimental)" and run the cell again.',
            ],
        }, room=f'notebook:{session_id}', namespace='/notebook')
        socketio.emit('notebook:execute_reply', {
            'cell_id': cell_id,
            'status': 'error',
        }, room=f'notebook:{session_id}', namespace='/notebook')

    def _handle_local_python_cell(user, session, session_record, cell_id, code, raw_code):
        """Execute a Python cell in the zero-Docker LocalPythonBackend subprocess worker."""
        import json as _json
        import secrets as _secrets
        from urllib import request as _urlrequest

        session_id = session.session_id
        room = f'notebook:{session_id}'
        msg_id = _secrets.token_hex(16)
        _clear_cell_buffer(session_id, cell_id)

        session.execution_count += 1
        session.status = 'busy'
        session.last_activity = datetime.utcnow()

        socketio.emit('notebook:execute_started', {
            'cell_id': cell_id,
            'msg_id': msg_id,
            'execution_count': session.execution_count,
        }, room=room, namespace='/notebook')

        def _bg_run():
            t0 = int(time.time() * 1000)
            svc = (session.connection_info or {}).get('service_url', '')
            tok = (session.connection_info or {}).get('auth_token', '')
            try:
                req = _urlrequest.Request(
                    f"{svc.rstrip('/')}/execute",
                    data=_json.dumps({'code': code}).encode('utf-8'),
                    headers={'Content-Type': 'application/json', 'X-Phonos-Kernel-Token': tok},
                    method='POST',
                )
                timeout_s = max(30, int(getattr(session, 'execution_timeout', 300)))
                with _urlrequest.urlopen(req, timeout=timeout_s) as resp:
                    res = _json.loads(resp.read().decode('utf-8'))

                exec_cnt = int(res.get('execution_count') or session.execution_count)
                session.execution_count = exec_cnt
                for out in (res.get('outputs') or []):
                    payload = dict(out, cell_id=cell_id, msg_id=msg_id)
                    _buffer_output(session_id, 'notebook:output', payload)
                    socketio.emit('notebook:output', payload, room=room, namespace='/notebook')

                reply_payload = {
                    'cell_id': cell_id,
                    'msg_id': msg_id,
                    'status': res.get('status', 'ok'),
                    'execution_count': exec_cnt,
                    'duration_ms': int(time.time() * 1000) - t0,
                }
                _buffer_output(session_id, 'notebook:execute_reply', reply_payload)
                socketio.emit('notebook:execute_reply', reply_payload, room=room, namespace='/notebook')
            except Exception as exc:
                err_out = {
                    'cell_id': cell_id,
                    'msg_id': msg_id,
                    'output_type': 'error',
                    'ename': type(exc).__name__,
                    'evalue': str(exc),
                    'traceback': [f"{type(exc).__name__}: {exc}"],
                }
                _buffer_output(session_id, 'notebook:output', err_out)
                socketio.emit('notebook:output', err_out, room=room, namespace='/notebook')
                socketio.emit('notebook:execute_reply', {
                    'cell_id': cell_id,
                    'msg_id': msg_id,
                    'status': 'error',
                    'execution_count': session.execution_count,
                }, room=room, namespace='/notebook')
            finally:
                session.status = 'idle'
                session.last_activity = datetime.utcnow()

        socketio.start_background_task(_bg_run)

    def _handle_wolfram_cell(user, session_id, cell_id, code):
        """Handle Wolfram Language cell execution via the Wolfram service container."""
        import time
        import secrets
        import traceback as tb
        import json as _json
        from urllib import request as _urlrequest
        from urllib.error import URLError, HTTPError

        db = get_db()

        # Resolve tracking session — accept any active session or create ephemeral wolfram tracking
        _wolfram_exec_state = globals().setdefault('_wolfram_exec_state', {})
        session_record = db.execute('''
            SELECT id FROM notebook_sessions
            WHERE session_token = %s AND user_id = %s
            AND kernel_status IN ('idle', 'busy')
        ''', (session_id, user['id'])).fetchone()

        if session_record:
            session = kernel_manager.get_session(session_id)
        else:
            session = None

        # Pre-flight: if the caller is a non-admin user, they must have saved their
        # own Wolfram Cloud credentials. Fail fast with a helpful message instead
        # of waiting for the cloud to return an auth error.
        try:
            user_record = get_db().execute(
                "SELECT is_admin FROM users WHERE id = %s", (user['id'],)
            ).fetchone()
            is_admin_user = bool(user_record and user_record['is_admin'])
            if not is_admin_user:
                from phonos_app.helpers.wolfram_user_creds import has_credentials
                if not has_credentials(user['id']):
                    preflight_msg_id = secrets.token_hex(16)
                    emit('notebook:output', {
                        'msg_id': preflight_msg_id,
                        'cell_id': cell_id,
                        'output_type': 'error',
                        'ename': 'MissingCredentials',
                        'evalue': 'Wolfram Cloud account not configured',
                        'traceback': [
                            'No Wolfram Cloud account is configured for your user.',
                            'Open Notebook IDE Settings → Wolfram Account and enter your Wolfram ID and password.',
                            'Admins use the global account configured in the container environment.',
                        ],
                    }, room=f'notebook:{session_id}', namespace='/notebook')
                    socketio.emit('notebook:execute_reply', {
                        'msg_id': preflight_msg_id,
                        'cell_id': cell_id,
                        'status': 'error',
                        'execution_count': 0,
                    }, room=f'notebook:{session_id}', namespace='/notebook')
                    return
        except Exception as e:
            logger.warning(f"Pre-flight credentials check failed: {e}")

        # Resolve Wolfram service URL + evaluation limits.
        # Precedence: runtime_levels.wolfram.config > WOLFRAM_SERVICE_URL env > http://localhost:5100
        import os as _os
        service_url = _os.environ.get('WOLFRAM_SERVICE_URL', 'http://localhost:5100')
        wolfram_timeout = 60
        wolfram_image_size = None
        try:
            from phonos_app.helpers.runtime_config import get_runtime_config
            rc = get_runtime_config()
            level = rc.get_level('wolfram')
            level_config = (level or {}).get('config') or {}
            if level_config.get('service_url'):
                service_url = level_config['service_url']
            try:
                wolfram_timeout = max(10, min(600, int(level_config.get('timeout', 60))))
            except (TypeError, ValueError):
                wolfram_timeout = 60
            try:
                if level_config.get('image_size') is not None:
                    wolfram_image_size = max(64, min(2400, int(level_config['image_size'])))
            except (TypeError, ValueError):
                wolfram_image_size = None
        except Exception as e:
            logger.warning(f"Could not load wolfram service config: {e}")
            try:
                get_db().rollback()
            except Exception:
                pass

        # Execution tracking
        if session and hasattr(session, 'execution_count'):
            session.execution_count += 1
            execution_count = session.execution_count
            track_session_id = session_record['id']
        else:
            exkey = f'wolfram:{session_id}:{user["id"]}'
            state = _wolfram_exec_state
            state[exkey] = state.get(exkey, 0) + 1
            execution_count = state[exkey]
            track_session_id = None

        msg_id = secrets.token_hex(16)
        _msg_to_cell[msg_id] = cell_id
        _exec_start_times[msg_id] = int(time.time() * 1000)
        if track_session_id:
            _exec_session_ids[msg_id] = track_session_id

        socketio.emit('notebook:execute_started', {
            'cell_id': cell_id,
            'msg_id': msg_id,
            'execution_count': execution_count
        }, room=f'notebook:{session_id}', namespace='/notebook')

        try:
            start_time = int(time.time() * 1000)

            # Resolve Wolfram Cloud credentials:
            #   Per-user creds from user_wolfram_credentials (encrypted DB) take
            #   precedence for all roles. If absent, fall back to the container's
            #   WOLFRAM_USERNAME / WOLFRAM_PASSWORD (global env, optional).
            try:
                from phonos_app.helpers.wolfram_user_creds import get_credentials, record_verification
                wolfram_user_creds = get_credentials(user['id'])
            except Exception:
                wolfram_user_creds = None

            request_headers = {'Content-Type': 'application/json'}
            if wolfram_user_creds:
                import base64 as _b64
                auth_token = _b64.b64encode(
                    f"{wolfram_user_creds[0]}:{wolfram_user_creds[1]}".encode('utf-8')
                ).decode('ascii')
                request_headers['X-Wolfram-Auth'] = f"Basic {auth_token}"

            # Keep a bounded history of successfully executed cells for export
            # and reference; actual kernel state is maintained by the service.
            MAX_HISTORY = 200
            history = getattr(session, 'wolfram_cell_history', None) if session else []
            if history is None:
                history = []
            if len(history) > MAX_HISTORY:
                history = history[-MAX_HISTORY:]

            # Use the persistent Wolfram evaluation endpoint so state (Set,
            # SetDelayed, Module definitions, etc.) survives between cells in
            # the same notebook session. This avoids replaying prior cells on
            # every call, which previously broke multi-line := definitions and
            # could corrupt assignment syntax when cells were concatenated.
            eval_payload = {
                'session_id': session_id,
                'code': code,
                'timeout': wolfram_timeout,
            }
            if wolfram_image_size is not None:
                eval_payload['image_size'] = wolfram_image_size
            req = _urlrequest.Request(
                f"{service_url.rstrip('/')}/evaluate-session",
                data=_json.dumps(eval_payload).encode('utf-8'),
                headers=request_headers,
                method='POST',
            )
            try:
                with _urlrequest.urlopen(req, timeout=wolfram_timeout + 60) as resp:
                    resp_body = resp.read().decode('utf-8')
                    data = _json.loads(resp_body)
            except HTTPError as e:
                err_body = ''
                try:
                    err_body = e.read().decode('utf-8', errors='replace')[:300]
                except Exception:
                    pass
                if wolfram_user_creds and e.code in (401, 403):
                    try:
                        record_verification(
                            user['id'], ok=False,
                            error=f"Auth failed (HTTP {e.code}) — check Wolfram username/password",
                        )
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"Wolfram Cloud rejected your credentials (HTTP {e.code}). "
                        f"Update them in Notebook Settings → Wolfram Account. Detail: {err_body}"
                    )
                else:
                    raise RuntimeError(
                        f"Wolfram execution failed (HTTP {e.code}): {err_body}"
                    )
            except URLError as e:
                raise RuntimeError(
                    "Cannot reach the Wolfram Language service. "
                    "Make sure the phonos-wolfram-service container is running on port 5100."
                )

            # Record verification status and accumulate cell history on success.
            if data.get('success') is True:
                history.append(code)
                if session:
                    try:
                        session.wolfram_cell_history = history
                    except Exception:
                        pass
                if wolfram_user_creds:
                    try:
                        record_verification(user['id'], ok=True)
                    except Exception:
                        pass

            duration_ms = int(time.time() * 1000) - start_time

            success = data.get('success', False)
            output_text = data.get('text', '') or ''
            error_text = data.get('error', '') or ''
            image_data = data.get('image', None)

            # Emit image output (display_data) if present
            if image_data:
                socketio.emit('notebook:output', {
                    'msg_id': msg_id,
                    'cell_id': cell_id,
                    'output_type': 'display_data',
                    'data': {
                        'image/png': image_data
                    },
                    'metadata': {}
                }, room=f'notebook:{session_id}', namespace='/notebook')

            # Emit text output (stdout) - filter out Wolfram image/plot artifacts
            if output_text.strip():
                clean_output = output_text

                # Filter Wolfram-specific artifact lines that represent images/graphics
                # These appear as text when the image data is already emitted separately
                WOLFRAM_ARTIFACT_PATTERNS = (
                    '-Graphics-', '-Graphics3D-', '-Plot-', '-SparseArray-',
                    '-Image-', '-Sound-', '-Manipulate-', '-ContourPlot-',
                    '-DensityPlot-', '-SurfaceGraphics-', '-StreamPlot-',
                    '-ListPlot-', '-ListLinePlot-', '-BarChart-', '-PieChart-',
                    '-Histogram-', '-RegionPlot-',
                )
                lines = clean_output.split('\n')
                filtered_lines = [l for l in lines if l.strip() not in WOLFRAM_ARTIFACT_PATTERNS]

                # Defensive filter: strip any line containing the session-boundary
                # sentinel (``<<PHONOS:CELL:...>>``). The wolfram service should
                # always strip this, but if the cloud returns Print output in a
                # format the boundary-slicer doesn't recognise, the sentinel could
                # leak into the visible output.
                filtered_lines = [l for l in filtered_lines if '<<PHONOS:CELL:' not in l]

                clean_output = '\n'.join(filtered_lines)

                # Remove trailing "Null" for image-only outputs
                if image_data:
                    if clean_output.rstrip().endswith("\nNull"):
                        clean_output = clean_output.rstrip()[:-4]
                    elif clean_output.strip() == "Null":
                        clean_output = ""
                    elif clean_output.strip() == "":
                        clean_output = ""

                if clean_output.strip():
                    socketio.emit('notebook:output', {
                        'msg_id': msg_id,
                        'cell_id': cell_id,
                        'output_type': 'stream',
                        'name': 'stdout',
                        'text': clean_output + ('\n' if not clean_output.endswith('\n') else '')
                    }, room=f'notebook:{session_id}', namespace='/notebook')

            if not success:
                # ``messages`` is the structured error list from
                # wolfram_errors.classify_warnings (server-side, in the
                # wolfram-service container). Each item has symbol/tag/
                # category/caret/snippet so the frontend can render inline
                # error markers. Older containers without the parser omit it,
                # in which case we fall back to the raw evalue split.
                wolfram_messages = data.get('messages') or []
                socketio.emit('notebook:output', {
                    'msg_id': msg_id,
                    'cell_id': cell_id,
                    'output_type': 'error',
                    'ename': 'WolframError',
                    'evalue': error_text or 'Wolfram execution failed',
                    'traceback': (error_text or 'Unknown error').split('\n'),
                    'messages': wolfram_messages,
                }, room=f'notebook:{session_id}', namespace='/notebook')

            socketio.emit('notebook:execute_reply', {
                'msg_id': msg_id,
                'cell_id': cell_id,
                'status': 'ok' if success else 'error',
                'execution_count': execution_count,
                'duration_ms': duration_ms,
                'encoding': data.get('encoding'),
                'raw_encoding': data.get('raw_encoding'),
            }, room=f'notebook:{session_id}', namespace='/notebook')

            # Clean up tracking dicts
            if msg_id in _msg_to_cell:
                del _msg_to_cell[msg_id]
            if msg_id in _exec_start_times:
                del _exec_start_times[msg_id]
            if msg_id in _exec_session_ids:
                del _exec_session_ids[msg_id]

            exec_row_id = _exec_row_ids.pop(msg_id, None)
            if exec_row_id:
                db.execute('''
                    UPDATE notebook_executions
                    SET status = %s, finished_at = CURRENT_TIMESTAMP, duration_ms = %s
                    WHERE id = %s AND status = 'running'
                ''', ('completed' if success else 'error', duration_ms, exec_row_id))
                db.commit()

        except Exception as e:
            logger.error(f"Wolfram execution error: {e}")
            tb_str = tb.format_exc()
            socketio.emit('notebook:output', {
                'msg_id': msg_id,
                'cell_id': cell_id,
                'output_type': 'error',
                'ename': type(e).__name__,
                'evalue': str(e),
                'traceback': tb_str.split('\n')
            }, room=f'notebook:{session_id}', namespace='/notebook')

            socketio.emit('notebook:execute_reply', {
                'msg_id': msg_id,
                'cell_id': cell_id,
                'status': 'error',
                'execution_count': execution_count
            }, room=f'notebook:{session_id}', namespace='/notebook')

            exec_row_id = _exec_row_ids.pop(msg_id, None)
            if exec_row_id:
                try:
                    db.execute('''
                        UPDATE notebook_executions
                        SET status = 'error', finished_at = CURRENT_TIMESTAMP
                        WHERE id = %s AND status = 'running'
                    ''', (exec_row_id,))
                    db.commit()
                except Exception as db_err:
                    logger.warning(f"Failed to update wolfram execution record: {db_err}")

            # Clean up tracking dicts
            if msg_id in _msg_to_cell:
                del _msg_to_cell[msg_id]
            if msg_id in _exec_start_times:
                del _exec_start_times[msg_id]
            if msg_id in _exec_session_ids:
                del _exec_session_ids[msg_id]

    @socketio.on('notebook:interrupt', namespace='/notebook')
    @notebook_auth_required
    def handle_interrupt(user, data):
        """Interrupt kernel execution"""
        session_id = data.get('session_id')

        if not session_id:
            emit('notebook:error', {'message': 'No session ID provided'})
            return

        # Verify session
        db = get_db()
        session_record = db.execute('''
            SELECT id FROM notebook_sessions
            WHERE session_token = %s AND user_id = %s
        ''', (session_id, user['id'])).fetchone()

        if not session_record:
            emit('notebook:error', {'message': 'Session not found'})
            return

        async def do_interrupt():
            room = f'notebook:{session_id}'
            try:
                await kernel_manager.interrupt_kernel(session_id)

                # Cancel this session's pending cell timeouts so the watchdog
                # doesn't fire for cells the user just halted.
                for cid, entry in list(_cell_timeouts.items()):
                    if len(entry) >= 3 and entry[2] == session_id:
                        _cell_timeouts.pop(cid, None)

                # The frontend listens for 'notebook:kernel_interrupt'; keep
                # the legacy 'notebook:interrupted' for older cached clients.
                payload = {'session_id': session_id}
                socketio.emit('notebook:kernel_interrupt', payload, room=room, namespace='/notebook')
                socketio.emit('notebook:interrupted', payload, room=room, namespace='/notebook')

                # Virtual (Wolfram) sessions have no IOPub status stream, so
                # push the idle transition explicitly.
                session = kernel_manager.sessions.get(session_id)
                if session and getattr(session, 'is_virtual', False):
                    socketio.emit('notebook:kernel_status', {
                        'status': 'idle'
                    }, room=room, namespace='/notebook')
            except Exception as e:
                socketio.emit('notebook:error', {
                    'message': str(e)
                }, room=room, namespace='/notebook')

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(do_interrupt())
        finally:
            loop.close()

    @socketio.on('notebook:restart', namespace='/notebook')
    @notebook_auth_required
    def handle_restart(user, data):
        """Restart kernel"""
        session_id = data.get('session_id')
        notebook_id = data.get('notebook_id')

        if not session_id or not notebook_id:
            emit('notebook:error', {'message': 'Missing session_id or notebook_id'})
            return

        async def do_restart():
            room = f'notebook:{session_id}'
            try:
                # Cancel pending cell timeouts and drop the replay buffer —
                # outputs from the previous kernel life must not be replayed
                # into the restarted session.
                for cid, entry in list(_cell_timeouts.items()):
                    if len(entry) >= 3 and entry[2] == session_id:
                        _cell_timeouts.pop(cid, None)
                _output_buffers.pop(session_id, None)

                # Fast path: restart the kernel process inside the existing
                # container (or reset the Wolfram service state). Keeps the
                # same session token, ZMQ ports, and socket room.
                old_session = kernel_manager.get_session(session_id)
                runtime_level = getattr(old_session, 'runtime_level', 'python') if old_session else 'python'

                if old_session and await kernel_manager.restart_kernel_inplace(session_id):
                    db = get_db()
                    db.execute('''
                        UPDATE notebook_sessions
                        SET kernel_status = 'idle', last_activity = CURRENT_TIMESTAMP
                        WHERE session_token = %s
                    ''', (session_id,))
                    db.commit()
                    socketio.emit('notebook:restarted', {
                        'old_session_id': session_id,
                        'new_session_id': session_id,
                        'in_place': True,
                        'status': 'idle',
                        'runtime_level': runtime_level
                    }, room=room, namespace='/notebook')
                    socketio.emit('notebook:kernel_status', {
                        'status': 'idle', 'runtime_level': runtime_level
                    }, room=room, namespace='/notebook')
                    return

                # Slow path: full container stop + fresh start.
                await kernel_manager.stop_kernel(session_id)

                # Update database
                db = get_db()
                db.execute('''
                    UPDATE notebook_sessions
                    SET kernel_status = 'disconnected', ended_at = CURRENT_TIMESTAMP
                    WHERE session_token = %s
                ''', (session_id,))

                # Get notebook info + the user's project role so the restarted
                # kernel keeps the same /project mount permissions.
                notebook = db.execute(
                    'SELECT project_id FROM notebooks WHERE id = %s',
                    (notebook_id,)
                ).fetchone()
                team_id = None
                has_write_access = False
                if notebook and notebook.get('project_id'):
                    project_info = db.execute('''
                        SELECT p.team_id, pa.role
                        FROM projects p
                        LEFT JOIN project_assignments pa ON p.id = pa.project_id AND pa.user_id = %s
                        WHERE p.id = %s
                    ''', (user['id'], notebook['project_id'])).fetchone()
                    if project_info:
                        team_id = project_info.get('team_id')
                        if project_info.get('role') in ('owner', 'editor', 'admin', 'Project Manager', 'Project Contributor'):
                            has_write_access = True

                # Start new kernel with same runtime level
                new_session = await kernel_manager.start_kernel(
                    user_id=user['id'],
                    notebook_id=notebook_id,
                    project_id=notebook['project_id'] if notebook else None,
                    team_id=team_id,
                    has_write_access=has_write_access,
                    runtime_level=runtime_level
                )

                # Save new session
                db.execute('''
                    INSERT INTO notebook_sessions
                    (notebook_id, user_id, container_id, session_token, kernel_status, workspace_path, runtime_level)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                ''', (notebook_id, user['id'], new_session.container_id,
                      new_session.session_id, new_session.status,
                      new_session.workspace_path, runtime_level))
                db.commit()

                socketio.emit('notebook:restarted', {
                    'old_session_id': session_id,
                    'new_session_id': new_session.session_id,
                    'in_place': False,
                    'status': new_session.status,
                    'runtime_level': runtime_level
                }, room=room, namespace='/notebook')

            except Exception as e:
                logger.error(f"Restart error: {e}")
                socketio.emit('notebook:error', {
                    'message': f'Restart failed: {str(e)}'
                }, room=room, namespace='/notebook')

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(do_restart())
        finally:
            loop.close()

    @socketio.on('notebook:ping', namespace='/notebook')
    @notebook_auth_required
    def handle_ping(user, data):
        """Heartbeat ping"""
        session_id = data.get('session_id')

        if session_id:
            # Update last activity
            db = get_db()
            db.execute('''
                UPDATE notebook_sessions
                SET last_activity = CURRENT_TIMESTAMP
                WHERE session_token = %s AND user_id = %s
            ''', (session_id, user['id']))
            db.commit()

            # Get session status
            session = kernel_manager.get_session(session_id)
            status = session.status if session else 'disconnected'

            emit('notebook:pong', {
                'session_id': session_id,
                'status': status
            }, room=f'notebook:{session_id}', namespace='/notebook')

    @socketio.on('notebook:input_reply', namespace='/notebook')
    @notebook_auth_required
    def handle_input_reply(user, data):
        """Handle user input response from frontend modal"""
        session_id = data.get('session_id')
        value = data.get('value', '')

        if not session_id:
            emit('notebook:error', {'message': 'No session ID provided'})
            return

        # Verify session
        db = get_db()
        session_record = db.execute('''
            SELECT id FROM notebook_sessions
            WHERE session_token = %s AND user_id = %s
        ''', (session_id, user['id'])).fetchone()

        if not session_record:
            emit('notebook:error', {'message': 'Session not found'})
            return

        # Send input to kernel by enqueueing it for the reply reader thread.
        # The reader thread owns the stdin ZMQ socket, so we never touch it
        # directly from the SocketIO handler thread.
        async def do_send_input():
            try:
                session = kernel_manager.get_session(session_id)
                if not session:
                    logger.warning(f"No session available for input reply {session_id}")
                    return

                # Build Jupyter input_reply message
                from datetime import datetime
                import secrets
                msg_id = secrets.token_hex(8)
                header = {
                    'msg_id': msg_id,
                    'msg_type': 'input_reply',
                    'username': '',
                    'session': session_id,
                    'date': datetime.utcnow().isoformat(),
                    'version': '5.3',
                }

                import hmac
                import hashlib
                content = {'value': value}

                key = session.connection_info.get('key', '').encode() if session.connection_info else b''
                h = hmac.new(key, digestmod=hashlib.sha256)
                h.update(json.dumps(header).encode())
                h.update(b'{}')  # parent_header
                h.update(b'{}')  # metadata
                h.update(json.dumps(content).encode())
                signature = h.hexdigest()

                msg_parts = [
                    b'<IDS|MSG>',
                    signature.encode(),
                    json.dumps(header).encode(),
                    b'{}',
                    b'{}',
                    json.dumps(content).encode(),
                ]

                session.stdin_send_queue.put(msg_parts, timeout=5)
                logger.info(f"Input reply queued for session {session_id}")
            except Exception as e:
                logger.error(f"Error queuing input reply: {e}")
                emit('notebook:error', {'message': str(e)}, room=f'notebook:{session_id}', namespace='/notebook')

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(do_send_input())
        finally:
            loop.close()

    @socketio.on('terminal:start', namespace='/notebook')
    @notebook_auth_required
    def handle_terminal_start(user, data):
        """Start a terminal session in the kernel container"""
        session_id = data.get('session_id')
        cols = data.get('cols', 80)
        rows = data.get('rows', 24)

        if not session_id:
            emit('terminal:error', {'message': 'No session ID provided'})
            return

        session = kernel_manager.get_session(session_id)
        if not session:
            emit('terminal:error', {'message': 'Session not active'})
            return

        if session.user_id != user['id']:
            logger.warning(
                f"User {user['id']} attempted to open a terminal on session "
                f"{session_id} owned by user {session.user_id}"
            )
            emit('terminal:error', {'message': 'Session not active'})
            return

        if getattr(session, 'is_virtual', False):
            emit('terminal:error', {'message': 'Terminal is not available for Wolfram/cloud sessions'})
            return

        # A previous terminal is still attached to this socket (duplicate
        # start, e.g. a fast double-click on the Terminal tab before
        # 'terminal:ready' came back, or a stale entry from a session we
        # never got a clean 'terminal:stop'/disconnect for). Close it first
        # instead of overwriting the dict entry and orphaning the old
        # docker exec process + stream listener greenlet.
        _close_terminal_socket(request.sid)

        try:
            # Create exec instance
            # We need to use the low-level API to get a socket
            # This requires docker-py to be configured correctly
            client = kernel_manager.docker_client.api
            exec_id = client.exec_create(
                session.container_id,
                cmd=["/bin/bash"],
                stdin=True,
                tty=True,
                user='notebook'
            )['Id']

            # Start exec and get socket
            socket = client.exec_start(
                exec_id,
                detach=False,
                tty=True,
                socket=True
            )
            
            # Store socket for this user session
            if not hasattr(kernel_manager, 'terminal_sockets'):
                kernel_manager.terminal_sockets = {}
            
            kernel_manager.terminal_sockets[request.sid] = {
                'socket': socket,
                'exec_id': exec_id
            }

            # Resize terminal
            client.exec_resize(exec_id, height=rows, width=cols)

            # Start listener thread
            socketio.start_background_task(_terminal_stream_listener, socket, request.sid, socketio)

            emit('terminal:ready')
            logger.info(f"Terminal started for session {session_id}")

        except Exception as e:
            logger.error(f"Failed to start terminal: {e}")
            emit('terminal:error', {'message': str(e)})

    @socketio.on('terminal:input', namespace='/notebook')
    def handle_terminal_input(data):
        """Handle input from client terminal"""
        input_data = data.get('data')
        if not input_data:
            return

        if hasattr(kernel_manager, 'terminal_sockets') and request.sid in kernel_manager.terminal_sockets:
            try:
                sock = kernel_manager.terminal_sockets[request.sid]['socket']
                # If using raw socket from docker-py, we might need to write bytes
                if hasattr(sock, '_sock'): # requests socket
                    sock._sock.send(input_data.encode('utf-8'))
                else:
                    sock.send(input_data.encode('utf-8'))
            except Exception as e:
                logger.error(f"Terminal input error: {e}")

    @socketio.on('terminal:resize', namespace='/notebook')
    def handle_terminal_resize(data):
        """Resize terminal"""
        cols = data.get('cols')
        rows = data.get('rows')
        
        if hasattr(kernel_manager, 'terminal_sockets') and request.sid in kernel_manager.terminal_sockets:
            try:
                info = kernel_manager.terminal_sockets[request.sid]
                kernel_manager.docker_client.api.exec_resize(info['exec_id'], height=rows, width=cols)
            except Exception as e:
                logger.error(f"Terminal resize error: {e}")

    @socketio.on('terminal:stop', namespace='/notebook')
    def handle_terminal_stop():
        """Stop terminal session"""
        _close_terminal_socket(request.sid)

    # ============================================================================
    # NOTEBOOK REAL-TIME COLLABORATION EVENTS
    # ============================================================================

    @socketio.on('notebook:join_collab', namespace='/notebook')
    @notebook_auth_required
    def handle_notebook_join_collab(user, data):
        """Join a notebook's real-time collaboration session."""
        notebook_id = _coerce_notebook_id((data or {}).get('notebook_id'))
        if not notebook_id:
            emit('notebook:error', {'message': 'No notebook_id provided'})
            return

        # Access first: only the owner or a member of the notebook's project
        # may enter the room (same rule as the notebook HTTP routes).
        can_read, can_write = _notebook_collab_access(notebook_id, user['id'])
        if not can_read:
            logger.warning(
                f"[Collab] join denied: notebook={notebook_id} user={user['id']}"
            )
            emit('notebook:error', {'message': 'No access to this notebook'})
            return

        logger.info(
            f"[Collab] join_collab: notebook={notebook_id} user={user['id']} "
            f"sid={request.sid}"
        )

        # Enforce a hard cap on the number of distinct users who can be
        # actively collaborating in a single notebook at once. Stale sids
        # are pruned first so the count is accurate, and a user rejoining
        # (e.g. reconnecting, or a second tab) never counts against the
        # limit — only truly new users do.
        allowed, reason = _can_join_collab(notebook_id, user['id'])
        if not allowed:
            emit('notebook:collab_full', {
                'limit': _COLLAB_MAX_USERS,
                'message': reason,
            })
            return

        room = f'notebook_collab:{notebook_id}'
        join_room(room)

        # Register presence keyed by socket id (sid), not user id. This lets
        # the same user have multiple tabs open without one disconnect
        # dropping their presence on the other. _broadcast_presence
        # aggregates by user_id for display.
        if notebook_id not in _collab_presence:
            _collab_presence[notebook_id] = {}
        _collab_presence[notebook_id][request.sid] = {
            'user_id': user['id'],
            'username': user.get('username') or 'User {}'.format(user['id']),
            'sid': request.sid,
            'cell_id': data.get('cell_id'),
            'cursor_pos': data.get('cursor_pos'),
            'last_seen': time.time(),
            'can_write': can_write,
        }

        if notebook_id not in _collab_editors:
            _collab_editors[notebook_id] = set()

        _broadcast_presence(notebook_id)

    @socketio.on('notebook:leave_collab', namespace='/notebook')
    @notebook_auth_required
    def handle_notebook_leave_collab(user, data):
        """Leave a notebook's real-time collaboration session."""
        notebook_id = _coerce_notebook_id((data or {}).get('notebook_id'))
        if not notebook_id:
            return

        room = f'notebook_collab:{notebook_id}'
        leave_room(room)

        if notebook_id in _collab_presence:
            _collab_presence[notebook_id].pop(request.sid, None)
            if not _collab_presence[notebook_id]:
                del _collab_presence[notebook_id]

        # Editors is sid-keyed too now.
        if notebook_id in _collab_editors:
            _collab_editors[notebook_id].discard(request.sid)

        _broadcast_presence(notebook_id)

    @socketio.on('notebook:cell_edit', namespace='/notebook')
    @notebook_auth_required
    def handle_notebook_cell_edit(user, data):
        """Real-time cell content update from a collaborator."""
        notebook_id = _coerce_notebook_id(data.get('notebook_id'))
        cell_id = data.get('cell_id')
        source = data.get('source')
        timestamp = data.get('timestamp', time.time())

        if not notebook_id or not cell_id or source is None:
            return
        if not _collab_authorize(notebook_id, user, need_write=True):
            return

        room = f'notebook_collab:{notebook_id}'

        if notebook_id in _collab_editors:
            _collab_editors[notebook_id].add(request.sid)

        if notebook_id in _collab_presence and request.sid in _collab_presence[notebook_id]:
            _collab_presence[notebook_id][request.sid]['cell_id'] = cell_id
            _collab_presence[notebook_id][request.sid]['last_seen'] = time.time()

        emit('notebook:cell_synced', {
            'user_id': user['id'],
            'username': user.get('username', f'User {user["id"]}'),
            'cell_id': cell_id,
            'source': source,
            'timestamp': timestamp,
        }, room=room, skip_sid=request.sid)

    @socketio.on('notebook:cursor_sync', namespace='/notebook')
    @notebook_auth_required
    def handle_notebook_cursor_sync(user, data):
        """Sync cursor position within a cell."""
        notebook_id = _coerce_notebook_id(data.get('notebook_id'))
        cell_id = data.get('cell_id')
        cursor_pos = data.get('cursor_pos')
        selection = data.get('selection')

        if not notebook_id or not _collab_authorize(notebook_id, user):
            return

        room = f'notebook_collab:{notebook_id}'

        # Re-register if the sweeper pruned this sid (e.g. backgrounded tab),
        # then record the latest cell/cursor.
        _ensure_collab_presence(
            notebook_id, request.sid, user, cell_id=cell_id, cursor_pos=cursor_pos
        )

        emit('notebook:cursor_updated', {
            'user_id': user['id'],
            'username': user.get('username', f'User {user["id"]}'),
            'cell_id': cell_id,
            'cursor_pos': cursor_pos,
            'selection': selection,
        }, room=room, namespace='/notebook', skip_sid=request.sid)

    @socketio.on('notebook:cell_select', namespace='/notebook')
    @notebook_auth_required
    def handle_notebook_cell_select(user, data):
        """Cell selection changed."""
        notebook_id = _coerce_notebook_id(data.get('notebook_id'))
        cell_id = data.get('cell_id')

        if not notebook_id or not _collab_authorize(notebook_id, user):
            return

        room = f'notebook_collab:{notebook_id}'

        # Re-register if the sweeper pruned this sid, then record the cell.
        _ensure_collab_presence(notebook_id, request.sid, user, cell_id=cell_id)

        emit('notebook:cell_selected', {
            'user_id': user['id'],
            'username': user.get('username', f'User {user["id"]}'),
            'cell_id': cell_id,
        }, room=room, namespace='/notebook', skip_sid=request.sid)

    @socketio.on('notebook:heartbeat', namespace='/notebook')
    @notebook_auth_required
    def handle_notebook_heartbeat(user, data):
        """Presence heartbeat to track active collaborators.

        Uses _ensure_collab_presence so a socket that was pruned by the
        stale sweeper (backgrounded tab, brief network drop) is silently
        re-registered on the next heartbeat — instead of the user staying
        invisible to everyone until a full page refresh re-joins.
        """
        notebook_id = _coerce_notebook_id(data.get('notebook_id'))
        if not notebook_id or not _collab_authorize(notebook_id, user):
            return
        _ensure_collab_presence(
            notebook_id, request.sid, user, cell_id=data.get('cell_id')
        )

    @socketio.on('notebook:typing', namespace='/notebook')
    @notebook_auth_required
    def handle_notebook_typing(user, data):
        """Notify collaborators that a user is typing in a cell."""
        notebook_id = _coerce_notebook_id(data.get('notebook_id'))
        cell_id = data.get('cell_id')
        is_typing = data.get('is_typing', False)

        if not notebook_id or not cell_id:
            return
        if not _collab_authorize(notebook_id, user, need_write=True):
            return

        room = f'notebook_collab:{notebook_id}'
        emit('notebook:typing_update', {
            'user_id': user['id'],
            'username': user.get('username', f'User {user["id"]}'),
            'cell_id': cell_id,
            'is_typing': is_typing,
        }, room=room, skip_sid=request.sid)

    logger.info("Notebook socket handlers registered")


def _close_terminal_socket(sid: str) -> None:
    """Close and forget any terminal exec socket attached to ``sid``.

    Shared by 'terminal:stop', a fresh 'terminal:start' on the same sid
    (duplicate-start guard), and socket disconnect, so there is exactly one
    place that tears down the docker-exec'd process instead of three
    slightly different copies of the same cleanup.
    """
    if not hasattr(kernel_manager, 'terminal_sockets'):
        return
    info = kernel_manager.terminal_sockets.pop(sid, None)
    if not info:
        return
    try:
        info['socket'].close()
    except Exception as e:
        logger.error(f"Terminal stop error: {e}")


def _terminal_stream_listener(socket, sid, socketio):
    """Listen for terminal output from Docker socket and forward to client"""
    import codecs
    import socket as pysocket
    # A stateful incremental decoder so a multi-byte UTF-8 character split
    # across two 4096-byte reads (very easy with unicode prompts, git log,
    # colored output, man pages, etc.) is buffered and completed on the next
    # chunk instead of being mangled into a '�' replacement character.
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    try:
        # Docker socket stream reader
        while True:
            try:
                # Read from socket - handling raw socket or requests socket wrapper
                if hasattr(socket, '_sock'):
                    data = socket._sock.recv(4096)
                else:
                    data = socket.recv(4096)

                if not data:
                    break

                text = decoder.decode(data)
                if text:
                    socketio.emit('terminal:output', {'data': text}, room=sid, namespace='/notebook')
            except pysocket.timeout:
                continue
            except Exception as e:
                break
    except Exception as e:
        logger.error(f"Terminal stream error: {e}")
    finally:
        _close_terminal_socket(sid)


async def _listen_and_broadcast(session_id: str, room: str, socketio):
    """
    Listen for kernel output and broadcast to room.
    This runs as a background task.
    """
    session = kernel_manager.get_session(session_id)
    if not session:
        logger.warning(f"No session found for output listening: {session_id}")
        return

    logger.info(f"Starting output listener for session {session_id}")

    try:
        async for msg in kernel_manager.listen_for_output(session_id):
            msg_type = msg.get('msg_type')
            content = msg.get('content', {})
            parent_msg_id = msg.get('parent_msg_id')

            if msg_type == 'status':
                status = content.get('execution_state', 'unknown')
                session.status = status
                socketio.emit('notebook:kernel_status', {
                    'status': status
                }, room=room, namespace='/notebook')

            elif msg_type == 'stream':
                _emit_and_buffer(socketio, 'notebook:output', {
                    'msg_id': parent_msg_id,
                    'output_type': 'stream',
                    'name': content.get('name', 'stdout'),
                    'text': content.get('text', '')
                }, room=room, session_id=session_id)

            elif msg_type == 'execute_result':
                _emit_and_buffer(socketio, 'notebook:output', {
                    'msg_id': parent_msg_id,
                    'output_type': 'execute_result',
                    'data': content.get('data', {}),
                    'execution_count': content.get('execution_count')
                }, room=room, session_id=session_id)

            elif msg_type == 'display_data':
                _emit_and_buffer(socketio, 'notebook:output', {
                    'msg_id': parent_msg_id,
                    'output_type': 'display_data',
                    'data': content.get('data', {}),
                    'metadata': content.get('metadata', {})
                }, room=room, session_id=session_id)

            elif msg_type == 'error':
                _emit_and_buffer(socketio, 'notebook:output', {
                    'msg_id': parent_msg_id,
                    'output_type': 'error',
                    'ename': content.get('ename', 'Error'),
                    'evalue': content.get('evalue', ''),
                    'traceback': content.get('traceback', [])
                }, room=room, session_id=session_id)

            elif msg_type == 'execute_reply':
                _handle_execute_reply(
                    socketio, session_id, room, parent_msg_id,
                    content.get('status', 'ok'),
                    content.get('execution_count')
                )

    except asyncio.CancelledError:
        logger.info(f"Output listener cancelled for session {session_id}")
    except Exception as e:
        logger.error(f"Output listener error for session {session_id}: {e}")


def _drain_reply_queue(session_id: str, room: str, socketio):
    """
    SocketIO green thread that drains replies placed in session.reply_queue by
    the real-OS-thread ZMQ reply reader and emits them to the notebook room.
    """
    import eventlet

    logger.info(f"Starting reply queue drainer for session {session_id}")
    try:
        while True:
            session = kernel_manager.get_session(session_id)
            if not session or session.status == 'dead':
                logger.info(f"Reply drainer exiting: session {session_id} ended")
                break

            try:
                msg = session.reply_queue.get_nowait()
            except _real_queue.Empty:
                eventlet.sleep(0.05)
                continue

            content = msg.get('content', {})
            parent_msg_id = msg.get('parent_msg_id')
            msg_type = msg.get('msg_type')
            if msg_type == 'input_request':
                _emit_and_buffer(socketio, 'notebook:input_request', {
                    'msg_id': parent_msg_id,
                    'prompt': content.get('prompt', ''),
                    'password': content.get('password', False)
                }, room=room, session_id=session_id)
            else:
                _handle_execute_reply(
                    socketio, session_id, room, parent_msg_id,
                    content.get('status', 'ok'),
                    content.get('execution_count')
                )
    except Exception as e:
        logger.error(f"Reply queue drainer error for session {session_id}: {e}")
    finally:
        _reply_task_sessions.pop(session_id, None)


def _listen_and_broadcast_sync(session_id: str, room: str, socketio):
    """
    Synchronous version of output listener for eventlet compatibility.
    Uses blocking ZMQ recv with timeout.
    Handles IO errors gracefully with automatic reconnection logic.
    Listens to both IOPub (output) and Stdin (input_request) channels.
    """
    import zmq
    import eventlet

    # Track connection state for reconnection
    connection_error_count = 0
    max_connection_errors = 10
    reconnected = False

    session = kernel_manager.get_session(session_id)
    if not session:
        logger.warning(f"No session found for output listening: {session_id}")
        if session_id in _output_task_sessions:
            del _output_task_sessions[session_id]
        return

    if not session.container_ip or not session.connection_info:
        logger.warning(f"No connection info for session {session_id}")
        if session_id in _output_task_sessions:
            del _output_task_sessions[session_id]
        return

    logger.info(f"Starting sync output listener for session {session_id}")

    ctx = None
    iopub = None

    try:
        ctx = zmq.Context()

        # IOPub socket for output (stream, display_data, execute_result, etc.)
        iopub = ctx.socket(zmq.SUB)
        iopub.setsockopt(zmq.SUBSCRIBE, b'')
        iopub.setsockopt(zmq.RCVTIMEO, 1000)  # 1 second timeout
        iopub.setsockopt(zmq.LINGER, 500)  # Don't block on close
        iopub.connect(f"tcp://{session.container_ip}:{session.connection_info['iopub_port']}")

        logger.info(f"Sync IOPub socket connected for session {session_id}")

        # Main loop with session validation. Cache the active session object and
        # refresh it only periodically instead of hitting the manager on every
        # 50ms tick, which avoids CPU churn and log spam.
        current_session = session
        last_session_refresh = time.time()
        session_refresh_interval = 5.0
        active_session_ids = set(kernel_manager.sessions.keys())
        last_session_ids_refresh = time.time()
        session_ids_refresh_interval = 2.0

        def _process_iopub_msg(msg_parts):
            """Parse and emit a single IOPub message."""
            nonlocal connection_error_count
            if len(msg_parts) < 7:
                return
            try:
                delim_idx = msg_parts.index(b'<IDS|MSG>')
                header = json.loads(msg_parts[delim_idx + 2])
                parent_header = json.loads(msg_parts[delim_idx + 3])
                content = json.loads(msg_parts[delim_idx + 5])
            except (ValueError, json.JSONDecodeError, KeyError):
                return

            msg_type = header.get('msg_type')
            parent_msg_id = parent_header.get('msg_id')
            logger.debug(f"IOPub message received for {session_id[:8]}: {msg_type}")

            if msg_type == 'status':
                status = content.get('execution_state', 'unknown')
                current_session.status = status
                # Restart the idle clock on every kernel state change so the
                # idle reaper measures from when an execution *finished*, not
                # from when it was dispatched.
                current_session.last_activity = datetime.utcnow()
                socketio.emit('notebook:kernel_status', {
                    'status': status
                }, room=room, namespace='/notebook')

            elif msg_type == 'stream':
                _emit_and_buffer(socketio, 'notebook:output', {
                    'msg_id': parent_msg_id,
                    'output_type': 'stream',
                    'name': content.get('name', 'stdout'),
                    'text': content.get('text', '')
                }, room=room, session_id=session_id)

            elif msg_type == 'execute_result':
                _emit_and_buffer(socketio, 'notebook:output', {
                    'msg_id': parent_msg_id,
                    'output_type': 'execute_result',
                    'data': content.get('data', {}),
                    'execution_count': content.get('execution_count')
                }, room=room, session_id=session_id)

            elif msg_type == 'display_data':
                _emit_and_buffer(socketio, 'notebook:output', {
                    'msg_id': parent_msg_id,
                    'output_type': 'display_data',
                    'data': content.get('data', {}),
                    'metadata': content.get('metadata', {}),
                    'transient': content.get('transient', {})
                }, room=room, session_id=session_id)

            elif msg_type == 'update_display_data':
                _emit_and_buffer(socketio, 'notebook:output', {
                    'msg_id': parent_msg_id,
                    'output_type': 'update_display_data',
                    'data': content.get('data', {}),
                    'metadata': content.get('metadata', {}),
                    'transient': content.get('transient', {})
                }, room=room, session_id=session_id)

            elif msg_type == 'error':
                _emit_and_buffer(socketio, 'notebook:output', {
                    'msg_id': parent_msg_id,
                    'output_type': 'error',
                    'ename': content.get('ename', 'Error'),
                    'evalue': content.get('evalue', ''),
                    'traceback': content.get('traceback', [])
                }, room=room, session_id=session_id)

        while True:
            try:
                now = time.time()
                if now - last_session_ids_refresh > session_ids_refresh_interval:
                    active_session_ids = set(kernel_manager.sessions.keys())
                    last_session_ids_refresh = now
                if session_id not in active_session_ids:
                    logger.info(f"Session {session_id} is no longer active")
                    break

                # Refresh the live session reference periodically.
                if now - last_session_refresh > session_refresh_interval:
                    current_session = kernel_manager.get_session(session_id)
                    last_session_refresh = now
                if not current_session or current_session.status == 'dead':
                    logger.info(f"Session {session_id} is no longer active")
                    break

                # Check for cell execution timeouts — only for THIS session's
                # cells. Entries are (start, timeout, session_id); legacy
                # 2-tuples (pre-restart handlers) are skipped rather than
                # letting one session's watchdog interrupt another kernel.
                now = time.time()
                timed_out = []
                for cell_id, entry in list(_cell_timeouts.items()):
                    if len(entry) < 3 or entry[2] != session_id:
                        continue
                    start, timeout = entry[0], entry[1]
                    elapsed = now - start
                    if elapsed > timeout:
                        timed_out.append(cell_id)
                    elif elapsed > timeout - 10 and elapsed < timeout - 9:
                        # Warning 10 seconds before timeout
                        socketio.emit('notebook:timeout_warning', {
                            'cell_id': cell_id,
                            'remaining_seconds': int(timeout - elapsed)
                        }, room=room, namespace='/notebook')

                for cell_id in timed_out:
                    _cell_timeouts.pop(cell_id, None)
                    socketio.emit('notebook:cell_timeout', {
                        'cell_id': cell_id,
                        'message': 'Cell execution timed out'
                    }, room=room, namespace='/notebook')
                    # Send interrupt to kernel
                    try:
                        import asyncio as _asyncio
                        loop = _asyncio.new_event_loop()
                        loop.run_until_complete(kernel_manager.interrupt_kernel(session_id))
                        loop.close()
                    except Exception as int_err:
                        logger.warning(f"Failed to interrupt timed out cell: {int_err}")

                # Drain all available IOPub messages without sleeping between
                # them, so bursts of output are not dropped by the SUB HWM.
                drained = 0
                while True:
                    try:
                        msg_parts = iopub.recv_multipart(flags=zmq.NOBLOCK)
                        connection_error_count = 0
                    except zmq.Again:
                        break
                    except zmq.ZMQError as e:
                        if e.errno == zmq.ETERM or e.errno == zmq.EINTR:
                            logger.warning("ZMQ socket closed, exiting listener")
                            return
                        connection_error_count += 1
                        if connection_error_count > max_connection_errors:
                            logger.error(f"Too many ZMQ errors, stopping listener for {session_id}")
                            socketio.emit('notebook:error', {
                                'message': 'Kernel connection lost. Please restart the kernel.',
                                'fatal': True
                            }, room=room, namespace='/notebook')
                            return
                        break
                    _process_iopub_msg(msg_parts)
                    drained += 1
                    # Yield every few messages so other green threads can run.
                    if drained % 10 == 0:
                        eventlet.sleep(0)

                # Yield to the eventlet hub between polls.
                eventlet.sleep(0.05)

            except eventlet.timeout.Timeout:
                continue
            except Exception as e:
                logger.error(f"Error in output listener loop for {session_id}: {e}")
                connection_error_count += 1
                if connection_error_count > max_connection_errors:
                    logger.error(f"Too many errors, stopping listener for {session_id}")
                    break
                eventlet.sleep(1)

    except zmq.error.ZMQError as e:
        logger.error(f"ZMQ error in output listener for session {session_id}: {e}")
    except Exception as e:
        logger.error(f"Output listener error for session {session_id}: {e}")
    finally:
        # Safe cleanup of ZMQ resources
        if iopub:
            try:
                iopub.close(linger=500)
            except Exception as e:
                logger.debug(f"Error closing iopub socket: {e}")
        if ctx:
            try:
                ctx.term()
            except Exception as e:
                logger.debug(f"Error terminating ZMQ context: {e}")

        # Clean up session tracking
        if session_id in _output_task_sessions:
            del _output_task_sessions[session_id]

        logger.info(f"Output listener stopped for session {session_id}")


# ============================================================================
# NOTEBOOK REAL-TIME COLLABORATION EVENTS
# ============================================================================

_collab_presence: dict[int, dict[str, dict]] = {}
_collab_editors: dict[int, set] = {}

# Stale-presence threshold (seconds). A socket with no heartbeat/ activity
# within this window is considered disconnected and pruned. Kept well above
# the 10s client heartbeat interval so a backgrounded/throttled browser tab
# (where setInterval is clamped) is not pruned while still genuinely
# connected — pruned-but-live sockets are re-registered on the next
# heartbeat/cursor activity via _ensure_collab_presence.
_COLLAB_STALE_SECONDS = 60

# Hard cap on the number of distinct users who can be actively collaborating
# in a single notebook at once.
_COLLAB_MAX_USERS = 5


def _prune_stale_presence(notebook_id: int) -> bool:
    """Remove stale sids from a notebook's presence. Returns True if anything
    was pruned (so the caller can re-broadcast)."""
    if notebook_id not in _collab_presence:
        return False
    now = time.time()
    before = len(_collab_presence[notebook_id])
    _collab_presence[notebook_id] = {
        sid: info for sid, info in _collab_presence[notebook_id].items()
        if now - info.get('last_seen', 0) < _COLLAB_STALE_SECONDS
    }
    pruned = len(_collab_presence[notebook_id]) != before
    if not _collab_presence[notebook_id]:
        del _collab_presence[notebook_id]
        _collab_editors.pop(notebook_id, None)
    return pruned


def _broadcast_presence(notebook_id: int):
    """Broadcast current presence to all collaborators in a notebook room.

    Presence is stored per-sid (so one user with multiple tabs is tracked
    correctly), but aggregated by user_id for display — each connected user
    appears once, marked editing if any of their sids is editing.
    """
    # Lazy-prune stale entries on every broadcast.
    _prune_stale_presence(notebook_id)
    if notebook_id not in _collab_presence:
        return

    collab_sids = _collab_presence.get(notebook_id, {})
    editors = _collab_editors.get(notebook_id, set())

    # Aggregate by user_id for display.
    by_user: dict[int, dict] = {}
    for sid, info in collab_sids.items():
        uid = info.get('user_id')
        if uid is None:
            continue
        existing = by_user.get(uid)
        is_editing = sid in editors
        if existing is None:
            by_user[uid] = {
                'user_id': uid,
                'username': info.get('username', f'User {uid}'),
                'cell_id': info.get('cell_id'),
                'cursor_pos': info.get('cursor_pos'),
                'is_editing': is_editing,
                'last_seen': info.get('last_seen', 0),
            }
        else:
            # Keep the most recently active cell/cursor for this user.
            if info.get('last_seen', 0) > existing.get('last_seen', 0):
                existing['cell_id'] = info.get('cell_id')
                existing['cursor_pos'] = info.get('cursor_pos')
                existing['last_seen'] = info.get('last_seen', 0)
            if is_editing:
                existing['is_editing'] = True

    presence_data = list(by_user.values())

    room = f'notebook_collab:{notebook_id}'
    emit('notebook:presence', {
        'users': presence_data,
    }, room=room, namespace='/notebook')


def _coerce_notebook_id(raw):
    """Return ``raw`` as a positive int notebook id, or None if it is not one."""
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _notebook_collab_access(notebook_id, user_id):
    """Return ``(can_read, can_write)`` for ``user_id`` on ``notebook_id``.

    Uses the rule the notebook HTTP routes use to open a notebook
    (``check_notebook_access``): the notebook's creator, or a user assigned to
    its project. Write needs the project's manager/contributor role.
    """
    try:
        try:
            from phonos_app.routes.api_notebooks import check_notebook_access
        except ImportError:
            from .routes.api_notebooks import check_notebook_access
        read, _ = check_notebook_access(notebook_id, user_id)
        if read is None:
            return False, False
        write, _ = check_notebook_access(notebook_id, user_id, require_write=True)
        return True, write is not None
    except Exception as e:  # noqa: BLE001 - fail closed
        logger.error(f"[Collab] access check failed for notebook={notebook_id}: {e}")
        return False, False


def _collab_authorize(notebook_id, user, need_write=False):
    """Authorise a collab event from the current socket.

    A sid that joined (and passed the access check) is trusted from the
    presence map. A sid the stale sweeper pruned is re-checked against the DB
    and re-registered, so a legitimate collaborator recovers while an
    outsider who never joined stays out.
    """
    info = _collab_presence.get(notebook_id, {}).get(request.sid)
    if info is not None and 'can_write' in info:
        return bool(info['can_write']) if need_write else True
    can_read, can_write = _notebook_collab_access(notebook_id, user['id'])
    if not can_read:
        return False
    _ensure_collab_presence(notebook_id, request.sid, user, can_write=can_write)
    if info is not None:
        info['can_write'] = can_write
    return can_write if need_write else True


def _ensure_collab_presence(notebook_id, sid, user, cell_id=None, cursor_pos=None, can_write=None):
    """Keep ``sid`` registered in the notebook's presence map.

    A socket that the stale sweeper pruned (e.g. its browser tab was
    backgrounded long enough to miss the heartbeat window, or a transient
    network blip) is silently RE-registered here on the next
    heartbeat / cursor / cell-select activity, so the collaborator reappears
    for everyone without needing a page refresh. Returns True when the sid
    was missing and a presence broadcast was sent (re-join), False for a
    routine activity refresh.
    """
    if not notebook_id:
        return False
    if notebook_id not in _collab_presence:
        _collab_presence[notebook_id] = {}

    info = _collab_presence[notebook_id].get(sid)
    if info is None:
        room = f'notebook_collab:{notebook_id}'
        # Re-join the room (idempotent) so broadcasts reach this socket and
        # it is counted among room members.
        try:
            join_room(room)
        except Exception:
            pass
        _collab_presence[notebook_id][sid] = {
            'user_id': user['id'],
            'username': user.get('username') or 'User {}'.format(user['id']),
            'sid': sid,
            'cell_id': cell_id,
            'cursor_pos': cursor_pos,
            'last_seen': time.time(),
        }
        if can_write is not None:
            _collab_presence[notebook_id][sid]['can_write'] = can_write
        if notebook_id not in _collab_editors:
            _collab_editors[notebook_id] = set()
        _broadcast_presence(notebook_id)
        return True

    info['last_seen'] = time.time()
    if cell_id is not None:
        info['cell_id'] = cell_id
    if cursor_pos is not None:
        info['cursor_pos'] = cursor_pos
    info['username'] = user.get('username', info.get('username'))
    return False


def _start_collab_stale_sweeper(socketio):
    """Background task that periodically prunes silently-disconnected
    collaborators. The lazy prune in _broadcast_presence only fires when
    another event occurs; without a sweeper, a user who disconnects while
    nobody else is active can linger in presence until someone else joins.
    """
    try:
        socketio.start_background_task(_collab_sweep_loop, socketio)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not start collab stale sweeper: {e}")


def _collab_sweep_loop(socketio):
    while True:
        try:
            socketio.sleep(_COLLAB_STALE_SECONDS)
            for nid in list(_collab_presence.keys()):
                if _prune_stale_presence(nid):
                    _broadcast_presence(nid)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Collab sweep error: {e}")
            try:
                socketio.sleep(_COLLAB_STALE_SECONDS)
            except Exception:
                break


def _can_join_collab(notebook_id, user_id):
    """Return (allowed, reason) for whether ``user_id`` may join the
    collaboration room for ``notebook_id``. A user already present (e.g.
    reconnecting or opening a second tab) is always allowed and does not
    count against the cap. A new user is rejected once ``_COLLAB_MAX_USERS``
    distinct users are already active.
    """
    _prune_stale_presence(notebook_id)
    existing = {
        info.get('user_id') for info in _collab_presence.get(notebook_id, {}).values()
        if info.get('user_id') is not None
    }
    if user_id in existing:
        return True, None
    if len(existing) >= _COLLAB_MAX_USERS:
        return False, (f"This notebook is at the {_COLLAB_MAX_USERS}-person "
                       f"collaboration limit. Someone needs to leave "
                       f"before you can join.")
    return True, None