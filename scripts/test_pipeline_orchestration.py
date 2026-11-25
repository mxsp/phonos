"""Direct orchestration test for pipelines (no GPU jobs, no credits).

Imports the app (which does NOT start the Redis listener — that's wsgi's job),
stubs _handle_job_submission so internal "jobs" are recorded without enqueuing
to a worker, then drives advance_pipeline_after_job by hand and asserts the
stage-advance / matrix / stall-vs-completed behaviour. Cleans up all test rows.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "production"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from phonos_app.app import app, get_redis_connection  # noqa: E402
from phonos_app.helpers.database import get_db  # noqa: E402
import phonos_app.helpers.pipelines as P  # noqa: E402
import phonos_app.routes.api_jobs as J  # noqa: E402

P.socketio.emit = lambda *a, **k: None  # silence socket emits in this process

PREFIX = "pipeline_TEST_"
_submit_should_fail = {"v": False}


class _FakeResp:
    def __init__(self, body):
        self._body = body
    def get_json(self):
        return self._body


def _stub_submit(project_id, payload, *, user_id=None, skip_credit=False,
                 cost_override=None, pipeline_id=None, pipeline_stage=None,
                 skip_role_check=False, skip_rate_limit=False):
    """Record an internal job without touching Redis/workers."""
    if _submit_should_fail["v"]:
        return _FakeResp({"status": "error"}), 500
    db = get_db()
    jid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO jobs (project_id, creator_id, job_id, name, model, status, "
        "cost, pipeline_id, pipeline_stage) VALUES (%s,%s,%s,%s,%s,'submitted',0,%s,%s)",
        (project_id, user_id, jid, payload.get("name", "test"),
         payload.get("model", "type-0"), pipeline_id, pipeline_stage),
    )
    db.commit()
    return _FakeResp({"job_id": jid}), 202


def _mk_pipeline(db, project_id, user_id, stages):
    pid = PREFIX + uuid.uuid4().hex[:8]
    import json
    db.execute(
        "INSERT INTO job_pipelines (pipeline_id, project_id, creator_id, name, status, "
        "definition, seed_input, cost, billing_user_id, total_stages, current_stage, "
        "total_jobs, safety_cap) VALUES (%s,%s,%s,%s,'running',%s,%s,100,%s,%s,0,1,200)",
        (pid, project_id, user_id, "TEST", json.dumps({"stages": stages}),
         json.dumps({"type": "sequence", "sequence": "MKTAYIAKQR"}), user_id, len(stages)),
    )
    row = db.execute("SELECT * FROM job_pipelines WHERE pipeline_id=%s", (pid,)).fetchone()
    return row


def _add_stage0_completed_job(db, pipeline, category, model):
    """Simulate a stage-0 job that has finished successfully."""
    jid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO jobs (project_id, creator_id, job_id, name, model, status, cost, "
        "pipeline_id, pipeline_stage, output_file) VALUES "
        "(%s,%s,%s,'stage0',%s,'completed',0,%s,0,%s)",
        (pipeline["project_id"], pipeline["creator_id"], jid, model,
         pipeline["pipeline_id"], "/tmp/fake_stage0.pdb"),
    )
    db.execute(
        "INSERT INTO pipeline_jobs (pipeline_pk, stage_index, category, model, job_id, "
        "status, parent_pipeline_job_ids) VALUES (%s,0,%s,%s,%s,'submitted','{}')",
        (pipeline["id"], category, model, jid),
    )
    db.execute("UPDATE job_pipelines SET total_jobs=1 WHERE id=%s", (pipeline["id"],))
    db.commit()
    return jid


def _status(db, pk):
    r = db.execute("SELECT status, current_stage, total_jobs, completed_jobs, "
                   "failed_jobs, metadata FROM job_pipelines WHERE id=%s", (pk,)).fetchone()
    return dict(r)


def run():
    with app.app_context():
        db = get_db()
        r = get_redis_connection()
        J._handle_job_submission = _stub_submit  # patch module attribute

        proj = db.execute("SELECT id FROM projects ORDER BY id LIMIT 1").fetchone()
        usr = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        assert proj and usr, "need at least one project and one user"
        pid_proj, uid = proj["id"], usr["id"]
        print(f"Using project {pid_proj} (user {uid})")

        results = []

        # ---- Scenario A: Fold -> Dock, happy path ----
        _submit_should_fail["v"] = False
        pl = _mk_pipeline(db, pid_proj, uid, [
            {"index": 0, "category": "folding", "model": "type-9", "custom_params": {}},
            {"index": 1, "category": "docking", "model": "type-3", "custom_params": {"ligand": "CCO"}},
        ])
        fold_job = _add_stage0_completed_job(db, pl, "folding", "type-9")
        P.advance_pipeline_after_job(db, r, fold_job, "completed")
        st = _status(db, pl["id"])
        dock_rows = db.execute(
            "SELECT job_id FROM pipeline_jobs WHERE pipeline_pk=%s AND stage_index=1",
            (pl["id"],)).fetchall()
        a1 = (st["status"] == "running" and st["current_stage"] == 1 and len(dock_rows) == 1)
        results.append(("A: fold->dock advanced to stage 1", a1, st))

        # complete the docking job -> pipeline should finish "completed"
        dock_job = dock_rows[0]["job_id"]
        db.execute("UPDATE jobs SET status='completed' WHERE job_id=%s", (dock_job,))
        db.commit()
        P.advance_pipeline_after_job(db, r, dock_job, "completed")
        st = _status(db, pl["id"])
        a2 = (st["status"] == "completed")
        results.append(("A: pipeline finished as completed", a2, st))

        # ---- Scenario B: stall (next stage can't be built) ----
        pl2 = _mk_pipeline(db, pid_proj, uid, [
            {"index": 0, "category": "folding", "model": "type-9", "custom_params": {}},
            {"index": 1, "category": "docking", "model": "type-3", "custom_params": {}},
        ])
        fold2 = _add_stage0_completed_job(db, pl2, "folding", "type-9")
        _submit_should_fail["v"] = True  # next-stage submit fails
        P.advance_pipeline_after_job(db, r, fold2, "completed")
        st2 = _status(db, pl2["id"])
        meta = st2.get("metadata") or {}
        if isinstance(meta, str):
            import json
            meta = json.loads(meta or "{}")
        b1 = (st2["status"] in ("partial", "failed") and bool(meta.get("stall_reason")))
        results.append(("B: stalled pipeline NOT marked completed", b1, st2))

        # ---- cleanup ----
        db.execute("DELETE FROM jobs WHERE pipeline_id LIKE %s", (PREFIX + "%",))
        db.execute("DELETE FROM job_pipelines WHERE pipeline_id LIKE %s", (PREFIX + "%",))
        db.commit()

        print("\n=== RESULTS ===")
        ok = True
        for name, passed, detail in results:
            print(f"[{'PASS' if passed else 'FAIL'}] {name}")
            if not passed:
                ok = False
                print("        detail:", detail)
        print("\nALL PASSED" if ok else "\nSOME FAILED")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
