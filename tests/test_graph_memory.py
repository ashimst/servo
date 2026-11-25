"""Unit tests for Graphify-powered Knowledge Graph & Persistent Memory Layer."""

import json
from pathlib import Path
import pytest

from servo.cognition.graph_memory import (
    GraphMemory,
    KnowledgeGraph,
    GraphNode,
    GraphEdge,
    parse_frontmatter,
    parse_knowledge_documents,
    parse_servo_codebase,
)


def test_parse_frontmatter_standard():
    text = """---
id: test_quirk
title: Test Quirk Title
app: testapp
surface: Win32
symptom: Test symptom
root_cause: Test cause
mitigation: Test mitigation
tags: [tag1, tag2]
related_nodes: [nodeA, nodeB]
---

## Description
Some detailed description here.
"""
    fm, body = parse_frontmatter(text)
    assert fm["id"] == "test_quirk"
    assert fm["title"] == "Test Quirk Title"
    assert fm["app"] == "testapp"
    assert fm["surface"] == "Win32"
    assert fm["tags"] == ["tag1", "tag2"]
    assert fm["related_nodes"] == ["nodeA", "nodeB"]
    assert "Some detailed description here." in body


def test_parse_frontmatter_empty():
    text = "# Just markdown\nNo frontmatter here."
    fm, body = parse_frontmatter(text)
    assert fm == {}
    assert body == text


def test_knowledge_graph_node_and_edge_dedup():
    graph = KnowledgeGraph()
    n1 = GraphNode(id="n1", type="Test", name="Node 1")
    n2 = GraphNode(id="n2", type="Test", name="Node 2")
    graph.add_node(n1)
    graph.add_node(n2)
    assert len(graph.nodes) == 2

    e1 = GraphEdge(source="n1", target="n2", type="connects")
    e2 = GraphEdge(source="n1", target="n2", type="connects")
    graph.add_edge(e1)
    graph.add_edge(e2)
    assert len(graph.edges) == 1

    neighbors = graph.get_neighbors("n1")
    assert len(neighbors) == 1
    assert neighbors[0][1].id == "n2"


def test_knowledge_ontology_parsing(tmp_path):
    knowledge_dir = Path(__file__).resolve().parent.parent / "knowledge"
    assert knowledge_dir.is_dir()

    graph = KnowledgeGraph()
    parse_knowledge_documents(knowledge_dir, graph)

    # UI Surfaces
    assert "surface_win32" in graph.nodes
    assert "surface_wpf" in graph.nodes
    assert "surface_electron" in graph.nodes
    assert graph.nodes["surface_win32"].type == "UISurface"
    # Only real knowledge: no decorative primitive/bridge/playbook nodes.
    assert not [n for n in graph.nodes.values() if n.type in ("OSPrimitive", "Bridge", "Playbook")]

    # Learned Quirks
    assert "edge_app_mode_mic" in graph.nodes
    edge_quirk = graph.nodes["edge_app_mode_mic"]
    assert edge_quirk.type == "Quirk"
    assert "msedge" in edge_quirk.tags
    assert "--use-fake-ui-for-media-stream" in edge_quirk.attributes.get("mitigation", "")


def test_ast_parsing_servo_subset(tmp_path):
    servo_root = Path(__file__).resolve().parent.parent / "servo"
    graph = KnowledgeGraph()
    parse_servo_codebase(servo_root, graph)

    # Check key module nodes
    assert any("mod:servo.cognition.system2" in k for k in graph.nodes)
    assert any("mod:servo.control.jev_gate" in k for k in graph.nodes)

    # Check class nodes
    assert any("class:servo.control.jev_gate.JevGate" in k for k in graph.nodes)
    assert any("class:servo.cognition.system2.System2" in k for k in graph.nodes)

    # Check that edges exist
    assert len(graph.edges) > 50


def test_graph_memory_query_and_prompt_formatting(tmp_path):
    test_graph_file = tmp_path / "test_graph.json"
    gm = GraphMemory(graph_path=test_graph_file)

    # Verify search
    matches = gm.search_nodes("edge", limit=5)
    assert len(matches) > 0

    # Verify app quirks lookup
    edge_quirks = gm.get_app_quirks("msedge")
    assert len(edge_quirks) >= 1
    assert any(q.id == "edge_app_mode_mic" for q in edge_quirks)

    # Prompt formatting for System 2: the Edge mic quirk is about Servo's own copilot
    # window (scope: servo-internal), so it must NOT leak into a user's Edge task.
    prompt_ctx = gm.format_context_for_prompt(
        goal="open edge and record voice command",
        active_app="msedge.exe",
        surface_type="electron",
    )
    assert "[Knowledge Graph Context]" in prompt_ctx
    assert "Electron" in prompt_ctx
    assert "Edge App Mode Microphone" not in prompt_ctx

    # Task-scoped quirks for the focused app do appear.
    gm.graph.add_node(GraphNode(id="q_test", type="Quirk", name="Test App Lag",
                                attributes={"app": "testapp", "mitigation": "wait 1 after opening"}))
    assert "wait 1 after opening" in gm.format_context_for_prompt(goal="select a row", active_app="testapp.exe")

    # Recovery advice follows the actual failure, not words in the goal.
    assert "Recovery" not in gm.format_context_for_prompt(goal="fix the error dialog", active_app="notepad.exe")
    failed = gm.format_context_for_prompt(goal="play it", active_app="notepad.exe",
                                          failure={"kind": "perception", "error": "No UI element found"})
    assert "Recovery for this failure (perception)" in failed

    # Nothing applicable -> no block at all (no boilerplate noise).
    assert gm.format_context_for_prompt(goal="hello", active_app="") == ""


def test_graph_rebuilds_when_knowledge_changes(tmp_path):
    import os, shutil, time
    from servo.cognition.graph_memory import DEFAULT_KNOWLEDGE_DIR
    kdir = tmp_path / "knowledge"
    shutil.copytree(DEFAULT_KNOWLEDGE_DIR, kdir)
    gm = GraphMemory(graph_path=tmp_path / "g.json", knowledge_dir=kdir)
    assert not gm.is_stale()
    assert "recipe_youtube" in gm.graph.nodes
    new = kdir / "recipes" / "zz_test.md"
    new.write_text("---\nid: zz_test\ntitle: Test recipe\ntriggers: [zzz]\n---\nDo it.\n", encoding="utf-8")
    future = time.time() + 5
    os.utime(new, (future, future))
    assert gm.is_stale()
    gm2 = GraphMemory(graph_path=tmp_path / "g.json", knowledge_dir=kdir)
    assert "recipe_zz_test" in gm2.graph.nodes


def test_system1_reflex_loop_decoupling():
    """Verify that System 1 perception and control modules do not import graph memory."""
    import inspect
    import servo.perception.uia_sensor as uia
    import servo.perception.mirage_sensor as mirage
    import servo.control.loop as reflex_loop
    import servo.cognition.needle as needle

    for mod in (uia, mirage, reflex_loop, needle):
        source = inspect.getsource(mod)
        assert "graph_memory" not in source, f"{mod.__name__} violates System 1 decoupling guardrail!"
        assert "GraphMemory" not in source, f"{mod.__name__} violates System 1 decoupling guardrail!"


def test_graph_and_consistency_api_endpoints():
    """Verify that FastAPI app provides /api/graph, /api/graph/consistency, and /api/graph/rebuild."""
    from servo.daemon.copilot_app import CopilotSession, create_app
    from fastapi.testclient import TestClient

    session = CopilotSession()
    app = create_app(session)
    client = TestClient(app, base_url="http://127.0.0.1")

    # 1. GET /api/graph
    r = client.get("/api/graph", headers={"X-Servo-Token": session.token})
    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data
    assert "edges" in data
    assert "stats" in data
    assert data["stats"]["nodes_count"] > 10

    # 2. GET /api/graph/consistency
    r = client.get("/api/graph/consistency", headers={"X-Servo-Token": session.token})
    assert r.status_code == 200
    audit = r.json()
    assert "score" in audit
    assert "status" in audit
    assert "checks" in audit
    assert len(audit["checks"]) >= 3
    assert audit["score"] >= 90

    # 3. POST /api/graph/rebuild
    r = client.post("/api/graph/rebuild", headers={"X-Servo-Token": session.token}, json={})
    assert r.status_code == 200
    rebuild = r.json()
    assert rebuild["ok"] is True
    assert rebuild["nodes_count"] > 10

