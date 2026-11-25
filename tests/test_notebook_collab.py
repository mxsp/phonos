"""Tests for notebook collaboration + git-history diff helpers.

Covers:
- The line-level unified diff (LCS) added to notebook_history._line_diff /
  diff_snapshots (used by the commit-detail / compare modal).
- The sid-keyed collaboration presence aggregation in
  socket_handlers_notebook._broadcast_presence (multi-tab support + stale
  pruning), which previously dropped presence when a single tab disconnected.

Run with:
    ENCRYPTION_KEY="a-secure-test-key-for-dev-use-32" pytest tests/test_notebook_collab.py -v
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
# production/ submodules are imported as bare top-level modules (e.g.
# `from plan_service import ...` in phonos_app/helpers/credits.py), so the
# production directory must also be on sys.path — mirroring what
# production/main.py does at import time.
PRODUCTION_DIR = os.path.join(PROJECT_ROOT, 'production')
if PRODUCTION_DIR not in sys.path:
    sys.path.insert(0, PRODUCTION_DIR)

os.environ.setdefault('ENCRYPTION_KEY', 'a-secure-test-key-for-dev-use-32')


class LineDiffTestCase(unittest.TestCase):
    def setUp(self):
        from phonos_app.helpers import notebook_history
        self.nb_history = notebook_history

    def test_line_diff_detects_addition(self):
        diff = self.nb_history._line_diff("a\nb", "a\nb\nc")
        types = [d['type'] for d in diff]
        self.assertIn('add', types)
        # The added line text is preserved
        added = [d for d in diff if d['type'] == 'add']
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['text'], 'c')
        # New line numbers only on added lines
        self.assertIsNone(added[0]['old_no'])
        self.assertIsNotNone(added[0]['new_no'])

    def test_line_diff_detects_deletion(self):
        diff = self.nb_history._line_diff("a\nb\nc", "a\nc")
        types = [d['type'] for d in diff]
        self.assertIn('del', types)
        deleted = [d for d in diff if d['type'] == 'del']
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]['text'], 'b')

    def test_line_diff_unchanged_context(self):
        diff = self.nb_history._line_diff("a\nb", "a\nb")
        # No add/del when identical
        self.assertTrue(all(d['type'] == 'ctx' for d in diff))
        self.assertEqual(len(diff), 2)

    def test_line_diff_empty_inputs(self):
        diff = self.nb_history._line_diff("", "x\ny")
        self.assertEqual([d['type'] for d in diff], ['add', 'add'])
        diff_back = self.nb_history._line_diff("x\ny", "")
        self.assertEqual([d['type'] for d in diff_back], ['del', 'del'])

    def test_line_diff_truncates_large_diff(self):
        a = "\n".join(str(i) for i in range(1000))
        b = "\n".join(str(i) for i in range(1000, 2000))
        diff = self.nb_history._line_diff(a, b, max_lines=50)
        self.assertLessEqual(len(diff), 51)  # 50 + truncation marker
        self.assertEqual(diff[-1]['text'], '… (diff truncated)')


class DiffSnapshotsTestCase(unittest.TestCase):
    def setUp(self):
        from phonos_app.helpers import notebook_history
        self.nb_history = notebook_history

    def test_added_removed_changed(self):
        cells_a = [
            {'id': 'c1', 'cell_type': 'code', 'source': 'print(1)'},
            {'id': 'c2', 'cell_type': 'code', 'source': 'print(2)'},
        ]
        cells_b = [
            {'id': 'c1', 'cell_type': 'code', 'source': 'print(1)\nprint(99)'},
            {'id': 'c3', 'cell_type': 'code', 'source': 'print(3)'},
        ]
        result = self.nb_history.diff_snapshots(cells_a, cells_b)
        self.assertEqual(set(result['changed']), {'c1'})
        self.assertEqual(set(result['added']), {'c3'})
        self.assertEqual(set(result['removed']), {'c2'})
        self.assertIn('c1', [d['cell_id'] for d in result['changed_details']])

    def test_changed_details_include_line_diff(self):
        cells_a = [{'id': 'c1', 'cell_type': 'code', 'source': 'a\nb'}]
        cells_b = [{'id': 'c1', 'cell_type': 'code', 'source': 'a\nb\nc'}]
        result = self.nb_history.diff_snapshots(cells_a, cells_b)
        detail = result['changed_details'][0]
        self.assertIn('diff_lines', detail)
        types = [d['type'] for d in detail['diff_lines']]
        self.assertIn('add', types)

    def test_added_details_have_full_add_diff(self):
        cells_b = [{'id': 'c1', 'cell_type': 'code', 'source': 'x\ny'}]
        result = self.nb_history.diff_snapshots([], cells_b)
        self.assertEqual(len(result['added_details']), 1)
        detail = result['added_details'][0]
        self.assertEqual(detail['cell_id'], 'c1')
        self.assertEqual([d['type'] for d in detail['diff_lines']], ['add', 'add'])

    def test_type_change_detected_via_both_keys(self):
        # Backend stores cell_type; frontend stores type. diff_snapshots
        # should read both so a type change (code -> markdown) is detected.
        cells_a = [{'id': 'c1', 'type': 'code', 'source': 'x'}]
        cells_b = [{'id': 'c1', 'type': 'markdown', 'source': 'x'}]
        result = self.nb_history.diff_snapshots(cells_a, cells_b)
        self.assertIn('c1', result['changed'])


class CollabPresenceTestCase(unittest.TestCase):
    """Tests sid-keyed presence aggregation (multi-tab) and stale pruning.

    These exercise the module-level _broadcast_presence / _prune_stale_presence
    directly with a mocked emit, so no live socket / auth context is needed.
    """

    def setUp(self):
        try:
            import phonos_app.socket_handlers_notebook as nb_sock
        except Exception as e:  # pragma: no cover - env-dependent
            self.nb_sock = None
            self.skipTest(f"socket_handlers_notebook not importable in this env: {e}")
            return
        self.nb_sock = nb_sock
        # Always start from a clean presence state.
        self.nb_sock._collab_presence.clear()
        self.nb_sock._collab_editors.clear()
        # Patch emit so _broadcast_presence does not require a request context.
        patcher = patch.object(self.nb_sock, 'emit')
        self.mock_emit = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self.nb_sock is not None:
            self.nb_sock._collab_presence.clear()
            self.nb_sock._collab_editors.clear()

    def _last_presence_users(self):
        """Return the users list from the most recent emit call.

        emit is invoked as emit('notebook:presence', {'users': [...]}, room=...)
        so the users list lives in the second positional argument.
        """
        self.assertTrue(self.mock_emit.called, "emit was not called")
        args, kwargs = self.mock_emit.call_args
        if len(args) >= 2 and isinstance(args[1], dict):
            return args[1].get('users', [])
        return kwargs.get('users', [])

    def test_multi_tab_same_user_aggregated_to_one_entry(self):
        nb = self.nb_sock
        nid = 1001
        nb._collab_presence[nid] = {
            'sid-A': {'user_id': 5, 'username': 'alice', 'cell_id': 'c1',
                      'cursor_pos': None, 'last_seen': time.time()},
            'sid-B': {'user_id': 5, 'username': 'alice', 'cell_id': 'c2',
                      'cursor_pos': None, 'last_seen': time.time()},
            'sid-C': {'user_id': 7, 'username': 'bob', 'cell_id': None,
                      'cursor_pos': None, 'last_seen': time.time()},
        }
        nb._broadcast_presence(nid)
        users = self._last_presence_users()
        user_ids = sorted(u['user_id'] for u in users)
        # Two distinct users, even though there are three sids.
        self.assertEqual(user_ids, [5, 7])

    def test_disconnecting_one_tab_keeps_user_present(self):
        nb = self.nb_sock
        nid = 1002
        nb._collab_presence[nid] = {
            'sid-A': {'user_id': 5, 'username': 'alice', 'last_seen': time.time(),
                      'cell_id': None, 'cursor_pos': None},
            'sid-B': {'user_id': 5, 'username': 'alice', 'last_seen': time.time(),
                      'cell_id': None, 'cursor_pos': None},
        }
        # Simulate one tab disconnecting (sid removed by the disconnect handler).
        nb._collab_presence[nid].pop('sid-A', None)
        nb._broadcast_presence(nid)
        users = self._last_presence_users()
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]['user_id'], 5)

    def test_stale_sid_is_pruned(self):
        nb = self.nb_sock
        nid = 1003
        old = time.time() - 120  # well past the 30s threshold
        fresh = time.time()
        nb._collab_presence[nid] = {
            'sid-old': {'user_id': 5, 'username': 'alice', 'last_seen': old,
                        'cell_id': None, 'cursor_pos': None},
            'sid-fresh': {'user_id': 7, 'username': 'bob', 'last_seen': fresh,
                          'cell_id': None, 'cursor_pos': None},
        }
        nb._broadcast_presence(nid)
        users = self._last_presence_users()
        # Only the fresh user remains.
        self.assertEqual([u['user_id'] for u in users], [7])
        # And the stale sid was removed from the dict.
        self.assertNotIn('sid-old', nb._collab_presence[nid])

    def test_empty_room_cleans_up(self):
        nb = self.nb_sock
        nid = 1004
        old = time.time() - 120
        nb._collab_presence[nid] = {
            'sid-old': {'user_id': 5, 'username': 'alice', 'last_seen': old,
                        'cell_id': None, 'cursor_pos': None},
        }
        nb._broadcast_presence(nid)
        # No active users -> the notebook entry is removed entirely.
        self.assertNotIn(nid, nb._collab_presence)


class CollabUserCapTestCase(unittest.TestCase):
    """The notebook collaboration cap: at most _COLLAB_MAX_USERS (5) distinct
    users may be active in one notebook at once. A user who is already
    present (reconnect / second tab) is always allowed and does not count
    against the limit.
    """

    def setUp(self):
        try:
            import phonos_app.socket_handlers_notebook as nb_sock
        except Exception as e:  # pragma: no cover - env-dependent
            self.nb_sock = None
            self.skipTest(f"socket_handlers_notebook not importable in this env: {e}")
            return
        self.nb_sock = nb_sock
        self.nb_sock._collab_presence.clear()
        self.nb_sock._collab_editors.clear()
        patcher = patch.object(self.nb_sock, 'emit')
        self.mock_emit = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        if self.nb_sock is not None:
            self.nb_sock._collab_presence.clear()
            self.nb_sock._collab_editors.clear()

    def _add_user(self, nid, uid, username):
        sid = f'sid-{uid}'
        self.nb_sock._collab_presence.setdefault(nid, {})[sid] = {
            'user_id': uid, 'username': username, 'sid': sid,
            'cell_id': None, 'cursor_pos': None, 'last_seen': time.time(),
        }

    def test_cap_is_five(self):
        self.assertEqual(self.nb_sock._COLLAB_MAX_USERS, 5)

    def test_sixth_new_user_is_rejected(self):
        nid = 2001
        for uid in range(1, 6):  # five distinct users
            self._add_user(nid, uid, f'user{uid}')
        allowed, reason = self.nb_sock._can_join_collab(nid, 999)
        self.assertFalse(allowed)
        self.assertIsNotNone(reason)

    def test_fifth_new_user_is_allowed(self):
        nid = 2002
        for uid in range(1, 5):  # four users
            self._add_user(nid, uid, f'user{uid}')
        allowed, reason = self.nb_sock._can_join_collab(nid, 5)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_existing_user_can_rejoin_even_when_full(self):
        nid = 2003
        for uid in range(1, 6):  # five users, room is full
            self._add_user(nid, uid, f'user{uid}')
        # User 3 is already present -> a second tab / reconnect must be
        # allowed and not blocked by the cap.
        allowed, reason = self.nb_sock._can_join_collab(nid, 3)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_stale_users_do_not_consume_slots(self):
        nid = 2004
        # Five users, but three of them are stale (disconnected silently).
        self.nb_sock._collab_presence[nid] = {
            'sid-1': {'user_id': 1, 'last_seen': time.time() - 999,
                      'cell_id': None, 'cursor_pos': None, 'username': 'u1', 'sid': 'sid-1'},
            'sid-2': {'user_id': 2, 'last_seen': time.time() - 999,
                      'cell_id': None, 'cursor_pos': None, 'username': 'u2', 'sid': 'sid-2'},
            'sid-3': {'user_id': 3, 'last_seen': time.time() - 999,
                      'cell_id': None, 'cursor_pos': None, 'username': 'u3', 'sid': 'sid-3'},
            'sid-4': {'user_id': 4, 'last_seen': time.time(),
                      'cell_id': None, 'cursor_pos': None, 'username': 'u4', 'sid': 'sid-4'},
            'sid-5': {'user_id': 5, 'last_seen': time.time(),
                      'cell_id': None, 'cursor_pos': None, 'username': 'u5', 'sid': 'sid-5'},
        }
        # After pruning, only 2 active users -> a new user may join.
        allowed, reason = self.nb_sock._can_join_collab(nid, 6)
        self.assertTrue(allowed)
        self.assertIsNone(reason)


if __name__ == '__main__':
    unittest.main()
