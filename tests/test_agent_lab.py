"""Tests for Phonos Copilot (agent lab): schema, tools, context, SSE and chat storage."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from production.database import init_db
from production.main import app
from phonos_app.helpers.database import get_db


@pytest.fixture()
def agent_client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app.config.update(TESTING=True, DATABASE_FILE=path, DATABASE_URL="sqlite:///" + path)
    with app.app_context():
        init_db()
        db = get_db()
        db.execute(
            "INSERT INTO users (user_id, username, plan, credits, is_admin, storage_quota, storage_used) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("agent-test", "agent", "free", 500, 1, 10_000_000, 1000),
        )
        db.commit()
        user_id = db.execute(
            "SELECT id FROM users WHERE username = %s", ("agent",)
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO projects (name, description, creator_id, project_type) VALUES (%s, %s, %s, %s)",
            ("Alpha", "Test project", user_id, "private"),
        )
        db.commit()
        project_id = db.execute(
            "SELECT id FROM projects WHERE name = %s", ("Alpha",)
        ).fetchone()["id"]
        from phonos_app.agent import viewer_context
        viewer_context.clear(user_id)
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = user_id
        session["username"] = "agent"
        session["is_admin"] = True
    yield client, user_id, project_id
    try:
        os.unlink(path)
    except OSError:
        pass


def _tool(client, name, arguments=None, context=None):
    response = client.post(
        f"/api/agent/tools/{name}/run",
        json={"arguments": arguments or {}, "context": context or {}},
    )
    return response.status_code, response.get_json()


def test_catalog_and_context(agent_client):
    client, _, project_id = agent_client
    response = client.get("/api/agent/tools")
    assert response.status_code == 200
    data = response.get_json()
    names = {tool["name"] for tool in data["tools"]}
    assert {"propose_job", "sequence_analysis", "structure_summary", "model_input_schema"} <= names
    assert data["summary"]["count"] >= 50

    response = client.get("/api/agent/model-catalog")
    assert response.status_code == 200
    models = response.get_json()["models"]
    assert any(model["id"] == "openfold3" for model in models)

    response = client.post(
        "/api/agent/context",
        json={"context": {
            "route": "project",
            "title": "Alpha",
            "entity": {"type": "project", "id": project_id, "label": "Alpha"},
            "facts": ["2 jobs"],
        }},
    )
    assert response.status_code == 200
    context = response.get_json()["context"]
    assert context["entity"]["id"] == project_id
    assert "Current view" in response.get_json()["rendered"]


def test_bio_tools(agent_client):
    client, _, project_id = agent_client
    status, data = _tool(client, "sequence_analysis", {
        "sequence": "ATGGCCATTGTAATGGGCCGCTGAAAGGGTGCCCGATAG",
    })
    assert status == 200
    assert data["result"]["valid"] is True
    assert data["result"]["kind"] == "dna"

    status, data = _tool(client, "validate_ligand", {"smiles": "CC(=O)Oc1ccccc1C(=O)O"})
    assert status == 200
    assert data["result"]["valid"] is True
    assert data["result"]["formula"] == "C9H8O4"

    status, data = _tool(client, "model_input_schema", {"model_id": "openfold3"})
    assert status == 200
    assert data["result"]["model"]["id"] == "openfold3"
    assert data["result"]["fields"]

    status, data = _tool(client, "propose_job", {
        "model_id": "openfold3",
        "project_id": project_id,
        "sequence": "MKTAYIAKQRQISFVKSHFSRQDILDLWIYHTQGYFP",
    })
    assert status == 200
    action = data["result"]["client_action"]
    assert action["type"] == "confirm_job"
    assert action["draft"]["model"] == "openfold3"
    assert action["cost"] >= 1


def test_chat_and_sse(agent_client, monkeypatch):
    client, _, _ = agent_client
    import phonos_app.agent.runner as runner

    def fake_stream(**_kwargs):
        yield {"type": "text", "text": "Hello "}
        yield {"type": "text", "text": "from the test model."}
        yield {
            "type": "done",
            "content": "Hello from the test model.",
            "tool_calls": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "finish_reason": "stop",
        }

    monkeypatch.setattr(runner, "stream_completion", fake_stream)
    response = client.post("/api/agent/chats", json={})
    assert response.status_code == 201
    chat_id = response.get_json()["chat"]["id"]
    response = client.post(
        f"/api/agent/chats/{chat_id}/messages",
        json={"message": "Hello", "context": {"route": "jobs"}},
    )
    assert response.status_code == 200
    body = response.data.decode()
    assert "event: start" in body
    assert "event: delta" in body
    assert "event: done" in body

    response = client.get(f"/api/agent/chats/{chat_id}")
    messages = response.get_json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]

    response = client.get("/api/agent/stats?days=30")
    assert response.get_json()["totals"]["total_tokens"] == 15


def test_tool_loop(agent_client, monkeypatch):
    client, _, _ = agent_client
    import phonos_app.agent.runner as runner

    calls = {"n": 0}

    def fake_stream(**_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"type": "text", "text": "Checking. "}
            yield {
                "type": "done",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "list_models", "arguments": "{}"},
                }],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                "finish_reason": "tool_calls",
            }
        else:
            yield {"type": "text", "text": "There are models."}
            yield {"type": "done", "tool_calls": [], "usage": {}, "finish_reason": "stop"}

    monkeypatch.setattr(runner, "stream_completion", fake_stream)
    chat_id = client.post("/api/agent/chats", json={}).get_json()["chat"]["id"]
    body = client.post(
        f"/api/agent/chats/{chat_id}/messages",
        json={"message": "Which models?", "context": {}},
    ).data.decode()
    assert "event: tool_call" in body
    assert "event: tool_result" in body
    messages = client.get(f"/api/agent/chats/{chat_id}").get_json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant", "tool", "assistant"]
    assert json.loads(messages[2]["content"])["ok"] is True

def test_viewer_context_and_tools(agent_client):
    client, _, _ = agent_client
    pdb = (
        "ATOM      1  CA  ALA A   1      11.104   6.134  -6.504  1.00  0.00           C\n"
        "ATOM      2  CA  GLY A   2      12.104   6.134  -6.504  1.00  0.00           C\n"
        "HETATM    3  C1  ATP B 101      10.000   5.000  -5.000  1.00  0.00           C\n"
        "END\n"
    )
    response = client.put("/api/agent/viewer-context", json={
        "structure_text": pdb,
        "name": "demo.pdb",
        "chains": [{"chain": "A", "sequence": "AG"}],
        "selection": [{"chain": "A", "resi": 1, "resname": "ALA"}],
        "ligand": "B:101:ATP",
        "job_id": "job-demo",
    })
    assert response.status_code == 200
    assert response.get_json()["viewer"]["attached"] is True

    status, data = _tool(client, "get_viewer_context")
    assert status == 200
    assert data["result"]["viewer"]["name"] == "demo.pdb"
    status, data = _tool(client, "read_viewer_structure", {"max_chars": 100})
    assert status == 200
    assert "ATOM" in data["result"]["content"]
    status, data = _tool(client, "find_in_viewer_structure", {"motif": "AG"})
    assert status == 200
    assert data["result"]["hits"][0]["start"] == 1

    response = client.post("/api/agent/context", json={"context": {
        "route": "visualizer", "title": "3D",
        "viewer": {"attached": True, "name": "demo.pdb", "format": "pdb",
                   "structure_chars": len(pdb), "chains": [{"chain": "A", "length": 2}],
                   "ligand": "ATP"},
    }})
    assert "3D viewer" in response.get_json()["rendered"]

    response = client.delete("/api/agent/viewer-context")
    assert response.get_json()["viewer"]["attached"] is False



def test_fyrefly_provider_hides_key(agent_client):
    client, _, _ = agent_client
    response = client.get("/api/agent/config")
    presets = {preset["id"]: preset for preset in response.get_json()["presets"]}
    assert presets["fyrefly"]["label"] == "Fyreflysystems Models"
    assert presets["fyrefly"]["hidden_key"] is True

    response = client.put("/api/agent/settings", json={
        "provider_label": "Fyreflysystems Models",
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "models_url": "https://opencode.ai/inference/go/v1/models",
        "model": "deepseek-v4.1-flash",
        "api_key": "must-not-be-stored",
    })
    assert response.status_code == 200
    settings = response.get_json()["settings"]
    assert settings["key_managed"] is True
    assert settings["provider_label"] == "Fyreflysystems Models"
    assert settings["api_key_source"] in ("none", "environment")


def test_auto_run_token(agent_client):
    client, user_id, project_id = agent_client
    from phonos_app.agent import jobtokens
    from phonos_app.agent.tools import ToolContext, execute_tool

    chat = client.post("/api/agent/chats", json={}).get_json()["chat"]
    response = client.patch(f"/api/agent/chats/{chat['id']}", json={"write_mode": True})
    assert response.status_code == 200
    assert response.get_json()["chat"]["write_mode"] is True

    with app.app_context():
        from phonos_app.helpers.database import get_db
        db = get_db()
        ctx = ToolContext(db=db, user_id=user_id, chat_id=chat["id"], auto_run=True)
        result = execute_tool(ctx, "propose_job", {
            "model_id": "openfold3",
            "project_id": project_id,
            "sequence": "MKTAYIAKQRQISFVKSHFSRQDILDLWIYHTQGYFP",
        })
    action = result["client_action"]
    assert action["auto"] is True
    assert action["auto_token"]
    assert jobtokens.verify(action["auto_token"], user_id, chat["id"], action["draft"])
    assert jobtokens.verify(action["auto_token"], user_id, chat["id"] + 1, action["draft"]) is False
