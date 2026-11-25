"""Desktop knowledge graph (GraphRAG): what Servo knows about this Windows PC and itself.

Nodes
  app         an installed app or Store/web app              ("Settings", "Calculator")
  page        a window or page of an app                     ("Settings › Date & time")
  control     a control seen on a page                       ("Time zone" ComboBox)
  capability  one of Servo's own tools, what it does, limits ("set", "files", "run_command")
  procedure   a task Servo completed with every step verified
  failure     a step Laya judged failed, with what went wrong
Edges
  has_page (app→page)  has_control (page→control)  opens (control→page)
  used (procedure→control / capability)  step_of (procedure→page)  failed_on (failure→control)

Built from: Servo's tool list, the installed-app catalog, every UIA snapshot of a page,
and every run's verified steps and Laya-failed steps. Retrieved by full-text search
(SQLite FTS5) seeding a 1-2 hop expansion ranked by verification count and recency.

The graph only suggests: the planner is told it is memory, and the live screen decides.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from servo.config import servo_home

DEFAULT_DB = servo_home() / "desktop_graph.sqlite"
_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, name TEXT NOT NULL, props TEXT NOT NULL DEFAULT '{}',
  verified INTEGER NOT NULL DEFAULT 0, failed INTEGER NOT NULL DEFAULT 0,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS edges (
  src TEXT NOT NULL, dst TEXT NOT NULL, type TEXT NOT NULL, props TEXT NOT NULL DEFAULT '{}',
  weight REAL NOT NULL DEFAULT 1, last_seen REAL NOT NULL, PRIMARY KEY (src, dst, type));
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(id UNINDEXED, type UNINDEXED, text);
"""


def node_id(kind: str, *parts: str) -> str:
    key = "|".join(re.sub(r"\s+", " ", (p or "").strip().lower()) for p in parts)
    return f"{kind}:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def _fts_query(text: str) -> str:
    words = [w for w in re.findall(r"[a-z0-9]{2,}", (text or "").lower()) if w not in _STOP]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


_STOP = frozenset("""a an the and or to of in on for with my me i you it is are be can could would please
this that these those from into at by as do does did so if then than up down out set get go use using""".split())


class DesktopGraph:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with _lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    # ----------------------------------------------------------------- write

    def upsert_node(self, kind: str, nid: str, name: str, props: Optional[Dict[str, Any]] = None,
                    search_text: str = "", verified: int = 0, failed: int = 0) -> str:
        now = time.time()
        with _lock:
            row = self._db.execute("SELECT props FROM nodes WHERE id=?", (nid,)).fetchone()
            merged = {**(json.loads(row["props"]) if row else {}), **(props or {})}
            if row:
                self._db.execute("UPDATE nodes SET name=?, props=?, verified=verified+?, failed=failed+?, last_seen=? "
                                 "WHERE id=?", (name, json.dumps(merged, ensure_ascii=False), verified, failed, now, nid))
                self._db.execute("DELETE FROM nodes_fts WHERE id=?", (nid,))
            else:
                self._db.execute("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)",
                                 (nid, kind, name, json.dumps(merged, ensure_ascii=False), verified, failed, now, now))
            self._db.execute("INSERT INTO nodes_fts VALUES (?,?,?)", (nid, kind, f"{name} {search_text}".strip()))
        return nid

    def link(self, src: str, dst: str, kind: str, props: Optional[Dict[str, Any]] = None, weight: float = 1.0) -> None:
        now = time.time()
        with _lock:
            self._db.execute(
                "INSERT INTO edges VALUES (?,?,?,?,?,?) ON CONFLICT(src,dst,type) DO UPDATE SET "
                "weight=edges.weight+excluded.weight, last_seen=excluded.last_seen, props=excluded.props",
                (src, dst, kind, json.dumps(props or {}, ensure_ascii=False), weight, now))

    def commit(self) -> None:
        with _lock:
            self._db.commit()

    # ------------------------------------------------------------- builders

    def add_capabilities(self, tools: Iterable[Dict[str, Any]]) -> None:
        """Servo's own tools (Needle's general tool set): name, what it does, its arguments."""
        for t in tools:
            args = ", ".join(t.get("parameters", {}).get("properties", {}).keys())
            nid = node_id("capability", t["name"])
            self.upsert_node("capability", nid, t["name"], {"description": t.get("description", ""), "args": args},
                             search_text=f"{t.get('description', '')} {args}")
        self.commit()

    def add_app(self, name: str, app_id: str = "", kind: str = "", launch: str = "") -> str:
        nid = node_id("app", name)
        return self.upsert_node("app", nid, name, {"app_id": app_id, "kind": kind, "launch": launch},
                                search_text=f"{kind} {launch}")

    def add_page(self, app: str, title: str, uri: str = "") -> str:
        aid = self.add_app(app)
        pid = node_id("page", app, title)
        self.upsert_node("page", pid, title, {"app": app, "uri": uri}, search_text=f"{app} {uri}")
        self.link(aid, pid, "has_page")
        return pid

    def observe_page(self, app: str, title: str, controls: Iterable[Dict[str, Any]], uri: str = "",
                     window_box: Optional[Tuple[int, int, int, int]] = None) -> str:
        """A UIA snapshot: the page and its controls (role, label, automation id, relative box, state)."""
        pid = self.add_page(app, title, uri)
        wx, wy, ww, wh = 0, 0, 1, 1
        if window_box:
            wx, wy, ww, wh = window_box[0], window_box[1], max(1, window_box[2] - window_box[0]), \
                max(1, window_box[3] - window_box[1])
        for c in controls:
            label = (c.get("label") or "").strip()
            role = c.get("role") or ""
            if not label or role in ("Text", "VisualText", "VisualControl"):
                continue
            ctx = c.get("context") or ""
            cid = node_id("control", app, title, role, label, ctx)
            props: Dict[str, Any] = {"role": role, "app": app, "page": title, "context": ctx,
                                     "automation_id": c.get("automation_id") or ""}
            box = c.get("bbox")
            if box and window_box:
                props["rel_center"] = [round(((box[0] + box[2]) / 2 - wx) / ww, 3),
                                       round(((box[1] + box[3]) / 2 - wy) / wh, 3)]
            for k in ("toggle", "selected", "expanded", "value"):
                if c.get(k) is not None:
                    props[k] = c[k]
            self.upsert_node("control", cid, label, props, search_text=f"{role} {ctx} {title} {app}")
            self.link(pid, cid, "has_control")
        self.commit()
        return pid

    def record_run(self, goal: str, steps: List[Dict[str, Any]], success: bool) -> Optional[str]:
        """Experience: a fully verified run becomes a procedure; Laya-failed steps become failures.

        Each step dict: {call, tool, verification, error, app, page, target_label, target_role}."""
        now_steps = [s for s in steps if s.get("call")]
        for s in now_steps:
            if s.get("verification") == "failed" and s.get("target_label"):
                cid = node_id("control", s.get("app", ""), s.get("page", ""), s.get("target_role", ""),
                              s["target_label"], s.get("target_context", ""))
                fid = node_id("failure", goal, s["call"])
                self.upsert_node("failure", fid, s["call"], {"goal": goal, "error": (s.get("error") or "")[:300]},
                                 search_text=f"{goal} {s.get('error') or ''}", failed=1)
                self.link(fid, cid, "failed_on")
        pid = None
        if success and now_steps and all(s.get("verification") in ("confirmed", "readback") for s in now_steps):
            pid = node_id("procedure", goal)
            self.upsert_node("procedure", pid, goal, {"steps": [s["call"] for s in now_steps]},
                             search_text=" ".join(s["call"] for s in now_steps), verified=1)
            for i, s in enumerate(now_steps):
                if s.get("tool"):
                    self.link(pid, node_id("capability", s["tool"]), "used", {"order": i})
                if s.get("target_label"):
                    cid = node_id("control", s.get("app", ""), s.get("page", ""), s.get("target_role", ""),
                                  s["target_label"], s.get("target_context", ""))
                    self.link(pid, cid, "used", {"order": i})
                    with _lock:
                        self._db.execute("UPDATE nodes SET verified=verified+1 WHERE id=?", (cid,))
                if s.get("page"):
                    self.link(pid, node_id("page", s.get("app", ""), s["page"]), "step_of", {"order": i})
        self.commit()
        return pid

    # ------------------------------------------------------------- retrieve

    def search(self, query: str, limit: int = 12, kinds: Optional[Tuple[str, ...]] = None) -> List[sqlite3.Row]:
        q = _fts_query(query)
        if not q:
            return []
        with _lock:
            rows = self._db.execute(
                "SELECT n.*, bm25(nodes_fts) AS rank FROM nodes_fts f JOIN nodes n ON n.id=f.id "
                "WHERE nodes_fts MATCH ? ORDER BY rank LIMIT ?", (q, limit * 4)).fetchall()
        if kinds:
            rows = [r for r in rows if r["type"] in kinds]
        # BM25 relevance first, then evidence: verified beats unverified, failures count against.
        rows = sorted(rows, key=lambda r: (r["rank"] - 0.5 * min(r["verified"], 6) + 0.5 * min(r["failed"], 6)))
        return rows[:limit]

    def neighbors(self, nid: str, kinds: Optional[Tuple[str, ...]] = None) -> List[Tuple[sqlite3.Row, sqlite3.Row]]:
        with _lock:
            rows = self._db.execute(
                "SELECT e.*, n.id AS nid, n.type AS ntype, n.name AS nname, n.props AS nprops, n.verified AS nverified "
                "FROM edges e JOIN nodes n ON n.id = CASE WHEN e.src=? THEN e.dst ELSE e.src END "
                "WHERE e.src=? OR e.dst=? ORDER BY e.weight DESC LIMIT 40", (nid, nid, nid)).fetchall()
        return [(r, r) for r in rows if not kinds or r["type"] in kinds]

    def context_for(self, goal: str, limit: int = 10) -> str:
        """GraphRAG block for the planner: seed nodes by full-text match, expand one hop to
        the page/app a control lives on and the steps of matching procedures."""
        seeds = self.search(goal, limit=limit)
        if not seeds:
            return ""
        lines: List[str] = []
        for r in seeds:
            p = json.loads(r["props"])
            ev = f"verified {r['verified']}×" if r["verified"] else "seen"
            if r["failed"]:
                ev += f", failed {r['failed']}×"
            if r["type"] == "control":
                where = " › ".join(x for x in (p.get("app"), p.get("page")) if x)
                uri = ""
                for e, _ in self.neighbors(r["id"]):
                    if e["ntype"] == "page" and json.loads(e["nprops"]).get("uri"):
                        uri = f" ({json.loads(e['nprops'])['uri']})"
                        break
                lines.append(f"- {p.get('role', 'Control')} “{r['name']}”" + (f" ({p['context']})" if p.get("context") else "")
                             + f" is on {where}{uri} [{ev}]")
            elif r["type"] == "procedure":
                steps = "; ".join(p.get("steps", [])[:8])
                lines.append(f"- Done before: “{r['name']}” → {steps} [{ev}]")
            elif r["type"] == "failure":
                lines.append(f"- Failed before: {r['name']} — {p.get('error', '')[:160]}")
            elif r["type"] == "page":
                lines.append(f"- Page “{r['name']}” in {p.get('app', '')}" + (f" ({p['uri']})" if p.get("uri") else "") + f" [{ev}]")
            elif r["type"] == "app":
                lines.append(f"- App “{r['name']}”" + (f" ({p['kind']})" if p.get("kind") else "") +
                             (f", open with: {p['launch']}" if p.get("launch") else ""))
            elif r["type"] == "capability":
                lines.append(f"- Servo can: {r['name']} — {p.get('description', '')}")
        return ("[Known about this computer — memory from earlier runs; the live screen decides]\n"
                + "\n".join(lines))

    def stats(self) -> Dict[str, int]:
        with _lock:
            rows = self._db.execute("SELECT type, COUNT(*) AS n FROM nodes GROUP BY type").fetchall()
            edges = self._db.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        return {**{r["type"]: r["n"] for r in rows}, "edges": edges}


_graph: Optional[DesktopGraph] = None


def desktop_graph() -> DesktopGraph:
    global _graph
    with _lock:
        if _graph is None:
            _graph = DesktopGraph()
            from servo.cognition.needle import NEEDLE_TOOLS
            _graph.add_capabilities(NEEDLE_TOOLS)
        return _graph
