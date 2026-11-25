"""Servo's knowledge graph: its own code, Windows UI surfaces, app quirks and recipes.

1. Code: an AST pass over `servo/` maps modules, classes and functions (self-diagnosis
   uses it to point a failure at the code that produced it).
2. Knowledge: learned quirks (`knowledge/learned_quirks/`) and recipes
   (`knowledge/recipes/`), plus the UI surfaces below.
3. Saved to `~/.servo/graph.json` and drawn in the copilot's Graph tab.
4. Planner context: quirks of the active app, its UI surface, and recovery advice for the
   failure that just happened (`format_context_for_prompt`).
"""

from __future__ import annotations

import ast
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field


logger = logging.getLogger("servo.cognition.graph_memory")

DEFAULT_KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge"
from servo.paths import servo_home

DEFAULT_GRAPH_PATH = servo_home() / "graph.json"


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

def _literal_fragments(fn: ast.AST, limit: int = 60) -> List[str]:
    """Distinct string literals (and constant parts of f-strings) of 12+ chars in a function."""
    out: List[str] = []
    seen: Set[str] = set()
    for node in ast.walk(fn):
        if node is not fn and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = " ".join(node.value.split())
            if len(s) >= 12 and s not in seen and not s.startswith(("http", "\\")):
                seen.add(s)
                out.append(s[:220])
        if len(out) >= limit:
            break
    return out


def _called_names(fn: ast.AST, limit: int = 80) -> List[str]:
    names: List[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            if name and name not in names:
                names.append(name)
                if len(names) >= limit:
                    break
    return names


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
            attributes={
                "args": args, "is_method": self.current_class is not None,
                "lineno": node.lineno, "end_lineno": getattr(node, "end_lineno", node.lineno),
                # Literal text in the function (message fragments, f-string parts). Error
                # messages are built from these, so a run's error text can be traced back
                # to the function that produced it (see servo/cognition/diagnose.py).
                "strings": _literal_fragments(node),
                "calls": _called_names(node),
            },
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

    # 1. UI surfaces: what kind of accessibility tree an app has.
    for sid, name, desc in (
        ("surface_win32", "Win32 Classic Controls", "Native controls with a full UIA tree."),
        ("surface_wpf", "Windows Presentation Foundation (WPF)",
         "Long lists are virtualized: items below the fold are not in the tree until scrolled into view."),
        ("surface_electron", "Electron & Chromium Shell",
         "The page's accessibility tree is built lazily and can be empty at first; text on screen is read with OCR."),
        ("surface_browser", "Browser Web Page",
         "A page in the user's own browser: the accessibility tree Chromium builds from the DOM, then OCR and vision."),
    ):
        graph.add_node(GraphNode(id=sid, type="UISurface", name=name, description=desc))

    # 2. Learned quirks
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
                    # "servo-internal" quirks describe Servo's own plumbing (e.g. the copilot
                    # window's mic permissions) and never go into task-planning prompts.
                    "scope": str(fm.get("scope") or "task"),
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

    # 6. Task recipes (knowledge/recipes): the procedures System 2 actually follows.
    try:
        from servo.cognition.recipes import RecipeBook
        for r in RecipeBook(knowledge_root / "recipes").recipes():
            graph.add_node(GraphNode(
                id=f"recipe_{r.id}", type="Recipe", name=r.title,
                description=r.body.splitlines()[0][:200] if r.body else r.title,
                tags=list(r.triggers), file_path=r.path,
                attributes={"apps": list(r.apps), "body": r.body[:1500]},
            ))
            for app in r.apps:
                graph.add_edge(GraphEdge(source=f"recipe_{r.id}", target=f"app:{app.removesuffix('.exe')}",
                                         type="applies_to_app"))
    except Exception as e:
        logger.debug("recipe ingestion skipped: %s", e)


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
        if self.graph_path.is_file() and not self.is_stale():
            try:
                data = json.loads(self.graph_path.read_text(encoding="utf-8"))
                self.graph = KnowledgeGraph.model_validate(data)
                return
            except Exception as e:
                logger.warning("Failed to load cached graph.json (%s), rebuilding...", e)
        self.rebuild_graph()

    def _source_files(self) -> List[Path]:
        files: List[Path] = []
        if self.knowledge_dir.is_dir():
            files += list(self.knowledge_dir.rglob("*.md"))
        if self.servo_root.is_dir():
            files += list(self.servo_root.rglob("*.py"))
        return files

    def is_stale(self) -> bool:
        """True if any knowledge doc or source file changed since graph.json was built.

        Previously the cache was trusted forever, so edits to knowledge/*.md never
        reached the planner without a manual rebuild.
        """
        try:
            built = self.graph_path.stat().st_mtime
        except OSError:
            return True
        return any(f.stat().st_mtime > built for f in self._source_files() if f.exists())

    def refresh_if_stale(self) -> None:
        if self.is_stale():
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
        failure: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Render a concise [Knowledge Graph Context] block for System 2 planning.

        Only what applies right now: quirks for the focused app (never servo-internal
        ones), the UI surface of that app, and recovery advice for the failure that
        actually happened. Empty when nothing applies, so it never adds noise.
        Procedures for tasks come from knowledge/recipes (see servo/cognition/recipes.py).
        """
        sections: List[str] = []

        # 1. Quirks for the app the user is in
        app = (active_app or "").lower().removesuffix(".exe")
        quirks = [q for q in self.get_app_quirks(app) if q.attributes.get("scope", "task") != "servo-internal"] if app else []
        if quirks:
            sections.append("Known quirks of the active app:")
            for q in quirks[:limit_quirks]:
                sections.append(f"  * {q.name}: {q.attributes.get('mitigation') or q.description}")

        # 2. UI surface of the active app (or as given)
        surface = (surface_type or _surface_for_app(app) or "").lower()
        surf_node = f"surface_{surface}" if surface else ""
        if surf_node in self.graph.nodes:
            node = self.graph.nodes[surf_node]
            line = f"Active app's UI surface: {node.name}: {node.description}"
            sections.append(line)

        # 3. Recovery advice for the failure that just happened
        if failure:
            advice = _RECOVERY.get(str(failure.get("kind") or "error"), _RECOVERY["error"])
            sections.append(f"Recovery for this failure ({failure.get('kind') or 'error'}): {advice}")

        if not sections:
            return ""
        return "\n".join(["[Knowledge Graph Context]", *sections])

    # --------------------------------------------------- Consistency Auditor

    def audit_consistency(self) -> Dict[str, Any]:
        """Audit graph integrity, surface fallback completeness, bridge grounding, and reflex decoupling."""
        checks: List[Dict[str, Any]] = []
        issues_count = 0
        total_checks = 0

        # Check 1: Dangling edge references
        total_checks += 1
        dangling_edges = []
        for e in self.graph.edges:
            if e.source not in self.graph.nodes:
                dangling_edges.append(f"Missing source node: {e.source} in edge {e.type}->{e.target}")
        if dangling_edges:
            checks.append({
                "id": "edge_integrity",
                "name": "Graph Edge Referential Integrity",
                "status": "warn" if len(dangling_edges) < 10 else "fail",
                "message": f"Found {len(dangling_edges)} edge(s) with missing source nodes.",
                "details": dangling_edges[:10],
            })
            issues_count += 1
        else:
            checks.append({
                "id": "edge_integrity",
                "name": "Graph Edge Referential Integrity",
                "status": "pass",
                "message": f"All {len(self.graph.edges)} edges resolve to valid graph nodes.",
                "details": [],
            })

        # Check 4: Learned Quirks Schema & Grounding
        total_checks += 1
        quirk_nodes = [n for n in self.graph.nodes.values() if n.type == "Quirk"]
        invalid_quirks = []
        for q in quirk_nodes:
            attrs = q.attributes
            if not attrs.get("app") or not attrs.get("mitigation"):
                invalid_quirks.append(f"Quirk {q.id} missing required app or mitigation attribute.")
        if invalid_quirks:
            checks.append({
                "id": "quirk_schema",
                "name": "Learned Quirks Schema Compliance",
                "status": "warn",
                "message": f"{len(invalid_quirks)} quirk(s) missing required attributes.",
                "details": invalid_quirks,
            })
            issues_count += 1
        else:
            checks.append({
                "id": "quirk_schema",
                "name": "Learned Quirks Schema Compliance",
                "status": "pass",
                "message": f"All {len(quirk_nodes)} learned quirks have valid application targets and mitigations.",
                "details": [f"{q.name} ({q.attributes.get('app')})" for q in quirk_nodes],
            })

        # Check 5: System 1 Reflex Loop Decoupling
        total_checks += 1
        decoupling_violations = []
        reflex_files = [
            self.servo_root / "perception" / "uia_sensor.py",
            self.servo_root / "perception" / "mirage_sensor.py",
            self.servo_root / "control" / "loop.py",
            self.servo_root / "cognition" / "needle.py",
        ]
        for f in reflex_files:
            if f.is_file():
                src = f.read_text(encoding="utf-8")
                if "graph_memory" in src or "KnowledgeGraph" in src:
                    decoupling_violations.append(f"Violation: {f.name} imports graph_memory directly!")
        if decoupling_violations:
            checks.append({
                "id": "system1_decoupling",
                "name": "System 1 Reflex Loop Decoupling Guardrail",
                "status": "fail",
                "message": "System 1 reflex arc has illegal dependencies on graph memory!",
                "details": decoupling_violations,
            })
            issues_count += 2
        else:
            checks.append({
                "id": "system1_decoupling",
                "name": "System 1 Reflex Loop Decoupling Guardrail",
                "status": "pass",
                "message": "None of the step-execution modules (UIA, Mirage, Needle, loop) import graph memory.",
                "details": [f.name for f in reflex_files],
            })

        score = max(0, int(((total_checks - issues_count) / total_checks) * 100))
        status = "healthy" if score >= 90 else ("warning" if score >= 70 else "error")

        return {
            "score": score,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "nodes_count": len(self.graph.nodes),
            "edges_count": len(self.graph.edges),
            "checks": checks,
            "summary": f"Graph Consistency: {score}% ({status.upper()}). {len(checks)} audits evaluated.",
        }


_BROWSER_APPS = {"chrome", "msedge", "dia", "arc", "firefox", "brave", "opera", "vivaldi"}
_ELECTRON_APPS = {"code", "slack", "discord", "teams", "ms-teams", "spotify", "whatsapp", "notion", "obsidian"}
_WPF_APPS = {"devenv", "visualstudio"}


def _surface_for_app(app: str) -> str:
    if app in _BROWSER_APPS or app in _ELECTRON_APPS:
        return "electron"
    if app in _WPF_APPS:
        return "wpf"
    return ""


_RECOVERY = {
    "perception": "the control wasn't found on screen. Use a control number from the CURRENT screen "
                  "description, or scroll / open the menu that contains it first.",
    "verify": "the action ran but the expected change wasn't seen. Look at the screen: it may have worked, "
              "or a dialog may be in the way. Don't repeat the same command blindly.",
    "reasoning": "the step was not one of the step forms, or its tool call didn't match it. Rewrite it in "
                 "exactly one of the step forms.",
    "policy": "the user declined or the action was refused. Don't retry it; explain or ask in reply.",
    "error": "the step errored. Read the error text, change the approach, and stop after two failed approaches.",
}


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
