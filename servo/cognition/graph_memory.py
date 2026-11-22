"""Graphify Knowledge Graph & Persistent Memory Layer for Servo.

Provides a structural, compounding knowledge layer for System 2 (Macro-Planner):
1. AST Parser: Parses Servo's Python codebase (`servo/`) to map modules, classes,
   functions, and execution paths.
2. Markdown & Frontmatter Parser: Ingests the Windows interaction ontology
   (`knowledge/windows/`), playbooks (`knowledge/playbooks/`), and learned quirks
   (`knowledge/learned_quirks/`).
3. Graph Index: Compiles entities and typed relationships into `~/.servo/graph.json`.
4. System 2 Graph Retrieval: Injects high-signal structural context (surfaces,
   bridges, recovery playbooks, app quirks) into System 2 planning prompts.
5. Incremental Learning: Appends newly discovered application quirks and recovery
   heuristics to `knowledge/learned_quirks/` and updates `graph.json`.

ARCHITECTURAL GUARDRAIL:
Graphify is strictly decoupled from System 1 (UIA, Mirage, Jev, Needle 3).
The reflex loop remains zero-overhead and sub-second; only System 2 or offline
processes query or update the graph.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

from servo.config import settings

logger = logging.getLogger("servo.cognition.graph_memory")

DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge"
DEFAULT_GRAPH_PATH = Path.home() / ".servo" / "graph.json"


# ===================================================================== Models

class GraphNode(BaseModel):
    id: str
    type: str  # "Module" | "Class" | "Function" | "UISurface" | "OSPrimitive" | "Bridge" | "Playbook" | "Quirk"
    name: str
    description: str = ""
    tags: List[str] = Field(default_factory=list)
    attributes: Dict[str, Any] = Field(default_factory=dict)
    file_path: Optional[str] = None


class GraphEdge(BaseModel):
    source: str
    target: str
    type: str  # "imports" | "calls" | "defines" | "bridges_to" | "falls_back_to" | "applies_to" | "mitigates"
    weight: float = 1.0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class KnowledgeGraph(BaseModel):
    version: str = "1.0"
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    nodes: Dict[str, GraphNode] = Field(default_factory=dict)
    edges: List[GraphEdge] = Field(default_factory=list)

    def add_node(self, node: GraphNode) -> None:
        self.nodes[node.id] = node

    def add_edge(self, edge: GraphEdge) -> None:
        # Avoid duplicate edges
        for existing in self.edges:
            if (
                existing.source == edge.source
                and existing.target == edge.target
                and existing.type == edge.type
            ):
                return
        self.edges.append(edge)

    def get_neighbors(self, node_id: str, edge_types: Optional[Set[str]] = None) -> List[Tuple[GraphEdge, GraphNode]]:
        """Return all adjacent nodes connected from or to `node_id`."""
        neighbors: List[Tuple[GraphEdge, GraphNode]] = []
        for edge in self.edges:
            if edge_types and edge.type not in edge_types:
                continue
            if edge.source == node_id and edge.target in self.nodes:
                neighbors.append((edge, self.nodes[edge.target]))
            elif edge.target == node_id and edge.source in self.nodes:
                neighbors.append((edge, self.nodes[edge.source]))
        return neighbors


# ========================================================= Codebase AST Parser

class ServoASTVisitor(ast.NodeVisitor):
    """Parses Python source AST into modules, classes, functions, and import edges."""

    def __init__(self, module_id: str, file_path: str, graph: KnowledgeGraph) -> None:
        self.module_id = module_id
        self.file_path = file_path
        self.graph = graph
        self.current_class: Optional[str] = None

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            target_mod = alias.name
            target_id = f"mod:{target_mod}"
            self.graph.add_edge(GraphEdge(source=self.module_id, target=target_id, type="imports"))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            target_id = f"mod:{node.module}"
            self.graph.add_edge(GraphEdge(source=self.module_id, target=target_id, type="imports"))
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        class_id = f"class:{self.module_id[4:]}.{node.name}"
        doc = ast.get_docstring(node) or ""
        bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
        self.graph.add_node(GraphNode(
            id=class_id,
            type="Class",
            name=node.name,
            description=doc.split("\n")[0] if doc else "",
            file_path=self.file_path,
            attributes={"bases": bases, "full_doc": doc},
        ))
        self.graph.add_edge(GraphEdge(source=self.module_id, target=class_id, type="defines"))
        for base in bases:
            self.graph.add_edge(GraphEdge(source=class_id, target=f"class:{base}", type="inherits"))

        prev_class = self.current_class
        self.current_class = class_id
        self.generic_visit(node)
        self.current_class = prev_class

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_function(node)

    def _record_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent = self.current_class or self.module_id
        fn_id = f"fn:{parent}:{node.name}"
        doc = ast.get_docstring(node) or ""
        args = [a.arg for a in node.args.args]
        self.graph.add_node(GraphNode(
            id=fn_id,
            type="Function",
            name=node.name,
            description=doc.split("\n")[0] if doc else "",
            file_path=self.file_path,
            attributes={"args": args, "is_method": self.current_class is not None},
        ))
        self.graph.add_edge(GraphEdge(source=parent, target=fn_id, type="defines"))
        self.generic_visit(node)


def parse_servo_codebase(servo_root: Path, graph: KnowledgeGraph) -> None:
    """Parse all Python files in `servo_root` and register nodes and relationships."""
    if not servo_root.is_dir():
        return
    for py_file in servo_root.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        rel = py_file.relative_to(servo_root.parent)
        mod_name = ".".join(rel.with_suffix("").parts)
        mod_id = f"mod:{mod_name}"
        try:
            content = py_file.read_text(encoding="utf-8")
            parsed = ast.parse(content, filename=str(py_file))
            doc = ast.get_docstring(parsed) or ""
            graph.add_node(GraphNode(
                id=mod_id,
                type="Module",
                name=mod_name,
                description=doc.split("\n")[0] if doc else "",
                file_path=str(py_file),
                attributes={"full_doc": doc},
            ))
            visitor = ServoASTVisitor(mod_id, str(py_file), graph)
            visitor.visit(parsed)
        except Exception as e:
            logger.debug("Failed to parse AST for %s: %s", py_file, e)


# =============================================== Markdown & Ontology Ingestion

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Parse simple YAML/key-value frontmatter and return (metadata, body)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    raw_fm = m.group(1)
    body = text[m.end():]
    metadata: Dict[str, Any] = {}
    for line in raw_fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, val = line.split(":", 1)
        key = key.strip()
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            items = [x.strip() for x in val[1:-1].split(",") if x.strip()]
            metadata[key] = items
        elif val.lower() in ("true", "false"):
            metadata[key] = val.lower() == "true"
        else:
            metadata[key] = val.strip('"\'')
    return metadata, body


def parse_knowledge_documents(knowledge_root: Path, graph: KnowledgeGraph) -> None:
    """Ingest Windows UI surfaces, OS primitives, bridges, playbooks, and learned quirks."""
    if not knowledge_root.is_dir():
        return

    # 1. UI Surfaces
    surfaces_file = knowledge_root / "windows" / "ui_surfaces.md"
    if surfaces_file.is_file():
        surfaces = [
            ("surface_win32", "Win32 Classic Controls", "USER32/GDI deep HWND tree; native UIA proxy support."),
            ("surface_wpf", "Windows Presentation Foundation (WPF)", "DirectX/XAML tree; VirtualizingStackPanel requires scrolling."),
            ("surface_electron", "Electron & Chromium Shell", "Chrome_WidgetWin_1; lazy accessibility tree activation; canvas fallback."),
            ("surface_browser", "Browser Web DOM", "Direct Playwright DOM tree accessibility snapshot."),
            ("surface_canvas", "Custom Canvas / Direct3D", "Non-accessible pixel surface; strictly requires Mirage OCR + Needle."),
        ]
        for sid, name, desc in surfaces:
            graph.add_node(GraphNode(id=sid, type="UISurface", name=name, description=desc, file_path=str(surfaces_file)))
        # Semantic fallback edges
        graph.add_edge(GraphEdge(source="surface_electron", target="surface_canvas", type="falls_back_to"))
        graph.add_edge(GraphEdge(source="surface_wpf", target="surface_canvas", type="falls_back_to"))

    # 2. OS Primitives
    primitives_file = knowledge_root / "windows" / "os_primitives.md"
    if primitives_file.is_file():
        primitives = [
            ("primitive_hwnd_focus", "HWND Focus & Z-Order", "AttachThreadInput and SW_RESTORE rules for foreground activation."),
            ("primitive_uac", "User Account Control (UAC)", "Secure Desktop isolation; non-interactive boundary; alerts user."),
            ("primitive_file_dialogs", "Common File Dialogs", "Direct path typing into Edit1/ComboBox to avoid UI click latency."),
            ("primitive_system_tray", "System Tray & Notification Area", "Shell_TrayWnd and NotifyIconOverflowWindow interactions."),
            ("primitive_clipboard", "Windows Global Clipboard", "Exclusive lock contention with retry backoff."),
        ]
        for pid, name, desc in primitives:
            graph.add_node(GraphNode(id=pid, type="OSPrimitive", name=name, description=desc, file_path=str(primitives_file)))

    # 3. Servo-to-Windows Bridges
    bridges_file = knowledge_root / "windows" / "servo_bridges.md"
    if bridges_file.is_file():
        bridges = [
            ("bridge_uia_sensor", "UIA Sensor", "servo.perception.uia_sensor for COM-based accessible tree navigation.", "mod:servo.perception.uia_sensor"),
            ("bridge_mirage_sensor", "Mirage Sensor", "servo.perception.mirage_sensor for visual OCR and neural perception.", "mod:servo.perception.mirage_sensor"),
            ("bridge_trajectory", "Trajectory Generator", "servo.actuation.trajectory for continuous Bezier mouse/keyboard input.", "mod:servo.actuation.trajectory"),
            ("bridge_watchdog", "Settling & Watchdog", "servo.perception.watchdog and settling for window stability.", "mod:servo.perception.watchdog"),
            ("bridge_system_tools", "System Tools", "servo.actuation.system_tools for direct headless file and shell execution.", "mod:servo.actuation.system_tools"),
            ("bridge_jev_gate", "Kev Gate", "servo.control.jev_gate for pre/post-action safety checks.", "mod:servo.control.jev_gate"),
        ]
        for bid, name, desc, mod_ref in bridges:
            graph.add_node(GraphNode(id=bid, type="Bridge", name=name, description=desc, file_path=str(bridges_file)))
            graph.add_edge(GraphEdge(source=bid, target=mod_ref, type="implements"))

        # Link Primitives and Surfaces to Bridges
        graph.add_edge(GraphEdge(source="surface_win32", target="bridge_uia_sensor", type="bridges_to"))
        graph.add_edge(GraphEdge(source="surface_wpf", target="bridge_uia_sensor", type="bridges_to"))
        graph.add_edge(GraphEdge(source="surface_canvas", target="bridge_mirage_sensor", type="bridges_to"))
        graph.add_edge(GraphEdge(source="primitive_hwnd_focus", target="bridge_watchdog", type="bridges_to"))
        graph.add_edge(GraphEdge(source="primitive_uac", target="bridge_jev_gate", type="bridges_to"))

    # 4. Playbooks
    playbook_dir = knowledge_root / "playbooks"
    if playbook_dir.is_dir():
        for pb_file in playbook_dir.glob("*.md"):
            pb_id = f"playbook_{pb_file.stem}"
            content = pb_file.read_text(encoding="utf-8")
            title = pb_file.stem.replace("_", " ").title()
            first_line = [l.strip("# ").strip() for l in content.splitlines() if l.startswith("#")]
            if first_line:
                title = first_line[0]
            graph.add_node(GraphNode(
                id=pb_id,
                type="Playbook",
                name=title,
                description=f"Behavioral guide: {title}",
                file_path=str(pb_file),
            ))

    # 5. Learned Quirks
    quirks_dir = knowledge_root / "learned_quirks"
    if quirks_dir.is_dir():
        for q_file in quirks_dir.glob("*.md"):
            if q_file.name == "README.md":
                continue
            content = q_file.read_text(encoding="utf-8")
            fm, body = parse_frontmatter(content)
            qid = fm.get("id") or f"quirk_{q_file.stem}"
            title = fm.get("title") or q_file.stem.replace("_", " ").title()
            app = fm.get("app") or ""
            surface = fm.get("surface") or ""
            symptom = fm.get("symptom") or ""
            root_cause = fm.get("root_cause") or ""
            mitigation = fm.get("mitigation") or ""
            tags = fm.get("tags") or []
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.split(",")]

            graph.add_node(GraphNode(
                id=qid,
                type="Quirk",
                name=title,
                description=mitigation or symptom,
                tags=tags,
                file_path=str(q_file),
                attributes={
                    "app": app,
                    "surface": surface,
                    "symptom": symptom,
                    "root_cause": root_cause,
                    "mitigation": mitigation,
                    "body": body[:500],
                },
            ))

            if app:
                graph.add_edge(GraphEdge(source=qid, target=f"app:{app}", type="applies_to_app"))
            if surface:
                surf_node = f"surface_{surface.lower()}"
                if surf_node in graph.nodes:
                    graph.add_edge(GraphEdge(source=qid, target=surf_node, type="applies_to_surface"))

            # Explicit related nodes in frontmatter
            for rel in fm.get("related_nodes", []):
                graph.add_edge(GraphEdge(source=qid, target=rel, type="mitigates"))


# ======================================================= Graph Memory Manager

class GraphMemory:
    """System 2 Graph Retrieval & Compounding Knowledge Engine."""

    def __init__(
        self,
        graph_path: Optional[Path] = None,
        knowledge_dir: Optional[Path] = None,
        servo_root: Optional[Path] = None,
    ) -> None:
        self.graph_path = graph_path or DEFAULT_GRAPH_PATH
        self.knowledge_dir = knowledge_dir or DEFAULT_KNOWLEDGE_DIR
        self.servo_root = servo_root or (Path(__file__).resolve().parent.parent)
        self.graph: KnowledgeGraph = KnowledgeGraph()
        self._load_or_build()

    def _load_or_build(self) -> None:
        """Load compiled graph from JSON if available, otherwise compile from sources."""
        if self.graph_path.is_file():
            try:
                data = json.loads(self.graph_path.read_text(encoding="utf-8"))
                self.graph = KnowledgeGraph.model_validate(data)
                return
            except Exception as e:
                logger.warning("Failed to load cached graph.json (%s), rebuilding...", e)
        self.rebuild_graph()

    def rebuild_graph(self) -> KnowledgeGraph:
        """Parse codebase AST and knowledge markdown to produce a fresh graph.json."""
        self.graph = KnowledgeGraph()
        # Ingest knowledge documents first so nodes exist for AST edges
        parse_knowledge_documents(self.knowledge_dir, self.graph)
        parse_servo_codebase(self.servo_root, self.graph)
        self.save()
        return self.graph

    def save(self) -> None:
        """Save current graph state to ~/.servo/graph.json."""
        self.graph_path.parent.mkdir(parents=True, exist_ok=True)
        self.graph.generated_at = datetime.now(timezone.utc).isoformat()
        self.graph_path.write_text(self.graph.model_dump_json(indent=2), encoding="utf-8")

    # ------------------------------------------------------------- Queries

    def search_nodes(self, query: str, node_type: Optional[str] = None, limit: int = 5) -> List[GraphNode]:
        """Search nodes by keyword relevance across id, name, description, and tags."""
        q = query.lower().strip()
        if not q:
            return []
        matches: List[Tuple[float, GraphNode]] = []
        for node in self.graph.nodes.values():
            if node_type and node.type != node_type:
                continue
            score = 0.0
            if q in node.id.lower():
                score += 3.0
            if q in node.name.lower():
                score += 2.0
            if q in node.description.lower():
                score += 1.0
            for tag in node.tags:
                if q in tag.lower():
                    score += 1.5
            if score > 0:
                matches.append((score, node))

        matches.sort(key=lambda x: x[0], reverse=True)
        return [node for _, node in matches[:limit]]

    def get_app_quirks(self, app_name: str) -> List[GraphNode]:
        """Retrieve all learned quirks associated with a target app executable or name."""
        app = app_name.lower().replace(".exe", "").strip()
        quirks: List[GraphNode] = []
        for node in self.graph.nodes.values():
            if node.type == "Quirk":
                node_app = str(node.attributes.get("app", "")).lower()
                if node_app == app or app in node.tags or app in node.name.lower():
                    quirks.append(node)
        return quirks

    def format_context_for_prompt(
        self,
        goal: str,
        active_app: Optional[str] = None,
        surface_type: Optional[str] = None,
        limit_quirks: int = 3,
    ) -> str:
        """Render a concise [Knowledge Graph Context] block for System 2 planning."""
        sections: List[str] = ["[Knowledge Graph Context]"]

        # 1. Matched App Quirks
        quirks: List[GraphNode] = []
        if active_app:
            quirks = self.get_app_quirks(active_app)
        if not quirks and goal:
            # Query-based quirk matching
            words = [w for w in re.split(r"\W+", goal.lower()) if len(w) > 3]
            for w in words:
                quirks.extend(self.search_nodes(w, node_type="Quirk", limit=2))
                if len(quirks) >= limit_quirks:
                    break

        seen_quirks: Set[str] = set()
        deduped_quirks = [q for q in quirks if not (q.id in seen_quirks or seen_quirks.add(q.id))]

        if deduped_quirks:
            sections.append("Learned Application Quirks & Mitigations:")
            for q in deduped_quirks[:limit_quirks]:
                mitigation = q.attributes.get("mitigation") or q.description
                sections.append(f"  * [{q.name}] Root cause: {q.attributes.get('root_cause', 'N/A')}")
                sections.append(f"    -> Mitigation: {mitigation}")

        # 2. UI Surface & Fallback Paths
        if surface_type:
            surf_node = f"surface_{surface_type.lower()}"
            if surf_node in self.graph.nodes:
                node = self.graph.nodes[surf_node]
                sections.append(f"UI Surface Profile: {node.name}")
                sections.append(f"  * Details: {node.description}")
                # Check fallback bridges
                fallbacks = [n for edge, n in self.graph.get_neighbors(surf_node) if edge.type == "falls_back_to"]
                if fallbacks:
                    sections.append(f"  * Fallback Required: If UIA misses, fallback to {fallbacks[0].name}")

        # 3. Behavioral Playbook Suggestions
        if "fail" in goal.lower() or "error" in goal.lower() or "not found" in goal.lower():
            pb = self.graph.nodes.get("playbook_failure_recovery")
            if pb:
                sections.append(f"Active Playbook: {pb.name} (Check coordinates/OCR before retrying same action)")

        # Mandatory Safety Invariant Guardrail Reminder
        sections.append("Safety Guardrail: Hard risk boundaries (Kev Gate) and harness sandboxes are enforced in code.")

        if len(sections) == 2:  # Only header and safety guardrail
            return ""
        return "\n".join(sections)

    # -------------------------------------------------- Compounding Learning

    def record_learned_quirk(
        self,
        quirk_id: str,
        title: str,
        app: str,
        surface: str,
        symptom: str,
        root_cause: str,
        mitigation: str,
        tags: Optional[List[str]] = None,
        markdown_body: str = "",
    ) -> Path:
        """Write a new quirk markdown document to knowledge/learned_quirks/ and link it into graph.json."""
        tags = tags or [app.lower(), surface.lower()]
        quirks_dir = self.knowledge_dir / "learned_quirks"
        quirks_dir.mkdir(parents=True, exist_ok=True)
        file_path = quirks_dir / f"{quirk_id}.md"

        frontmatter = [
            "---",
            f"id: {quirk_id}",
            f"title: {title}",
            f"app: {app}",
            f"surface: {surface}",
            f"symptom: {symptom}",
            f"root_cause: {root_cause}",
            f"mitigation: {mitigation}",
            f"tags: [{', '.join(tags)}]",
            f"related_nodes: [surface_{surface.lower()}]",
            "---",
            "",
            f"## Description",
            markdown_body or symptom,
            "",
            f"## Mitigation Playbook",
            mitigation,
            "",
        ]
        file_path.write_text("\n".join(frontmatter), encoding="utf-8")

        # Ingest directly into active graph
        node = GraphNode(
            id=quirk_id,
            type="Quirk",
            name=title,
            description=mitigation,
            tags=tags,
            file_path=str(file_path),
            attributes={
                "app": app,
                "surface": surface,
                "symptom": symptom,
                "root_cause": root_cause,
                "mitigation": mitigation,
                "body": markdown_body,
            },
        )
        self.graph.add_node(node)
        self.graph.add_edge(GraphEdge(source=quirk_id, target=f"app:{app}", type="applies_to_app"))
        surf_node = f"surface_{surface.lower()}"
        if surf_node in self.graph.nodes:
            self.graph.add_edge(GraphEdge(source=quirk_id, target=surf_node, type="applies_to_surface"))

        self.save()
        logger.info("Recorded new learned quirk [%s] and updated graph.json", quirk_id)
        return file_path


# Global singleton instance
graph_memory = GraphMemory()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Graphify Knowledge Graph Builder for Servo")
    parser.add_argument("--build", action="store_true", help="Rebuild graph.json from AST and markdown")
    parser.add_argument("--query", type=str, help="Search the knowledge graph")
    parser.add_argument("--stats", action="store_true", help="Print graph node and edge counts")
    args = parser.parse_args()

    gm = GraphMemory()
    if args.build:
        g = gm.rebuild_graph()
        print(f"Graph rebuilt: {len(g.nodes)} nodes, {len(g.edges)} edges saved to {gm.graph_path}")
    elif args.stats or not args.query:
        print(f"Graph contains {len(gm.graph.nodes)} nodes, {len(gm.graph.edges)} edges.")
    if args.query:
        results = gm.search_nodes(args.query)
        print(f"Found {len(results)} matches for '{args.query}':")
        for r in results:
            print(f"  [{r.type}] {r.name} ({r.id}) - {r.description}")
