"""Self-diagnosis: explain why a run failed, down to the code that produced the failure.

Inputs: the saved run record (servo/telemetry/run_record.py: every step, verdict,
error, planning round and trace event) and the knowledge graph of Servo's own code
(servo/cognition/graph_memory.py: every function with its file, line range and the
literal text it contains).

How it works:
1. Pick the run (by id, or the latest one that failed or ended unconfirmed).
2. Find the failing step and its error text, the tier that handled it, what the
   planner tried, and the gate decisions around it.
3. Trace the error text back to the code: error messages are assembled from string
   literals, so the function whose literals appear verbatim in the error produced it.
4. Match the failure against known failure patterns for a plain-language cause and a
   concrete next step.
5. Optionally ask System 2 to explain (with function names and docstrings; code
   bodies are sent only if SERVO_DIAGNOSE_SEND_CODE is true).

Entry points: "why did that fail?" in the copilot, the Runs tab's Diagnose button,
GET /api/runs/{id}/diagnose, and ``python -m servo.cognition.diagnose [run_id]``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

DIAGNOSE_REQUEST = re.compile(
    r"^(?:why\s+(?:did|didn'?t|does|doesn'?t|is|isn'?t)\s+(?:that|it|this|the\s+last\s+(?:one|run|task))"
    r"(?:\s+(?:fail|work|not\s+work|happen|go\s+wrong|stop|get\s+stuck))?|what\s+went\s+wrong|"
    r"diagnose(?:\s+(?:that|it|the\s+last\s+(?:run|task|one)|run\s+(\w+)))?|"
    r"explain\s+(?:the\s+)?(?:failure|error|what\s+happened))\s*[?.!]*$",
    re.I,
)


def parse_diagnose_request(text: str) -> Optional[str]:
    """'' for "the last failed run", a run id for "diagnose run abc123", None if not a request."""
    m = DIAGNOSE_REQUEST.match(text.strip())
    if not m:
        return None
    return m.group(1) or ""


class CodeLocation(BaseModel):
    function: str
    module: str
    file: str
    line: int
    end_line: int
    matched: str = ""
    doc: str = ""

    def ref(self) -> str:
        return f"{self.file}:{self.line}"


class Diagnosis(BaseModel):
    run_id: str = ""
    goal: str = ""
    found: bool = True
    status: str = ""                 # failed | unconfirmed | ok
    failing_step: Dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    tier: str = ""
    cause: str = ""
    next_step: str = ""
    planner_rounds: List[Dict[str, Any]] = Field(default_factory=list)
    gate: List[str] = Field(default_factory=list)
    code: List[CodeLocation] = Field(default_factory=list)
    explanation: str = ""            # optional System 2 narrative

    def to_text(self) -> str:
        if not self.found:
            return self.cause or "I couldn't find a run to diagnose."
        lines = [f"Run {self.run_id}: “{self.goal}” — {self.status}."]
        if self.failing_step:
            s = self.failing_step
            lines.append(f"Failing step: {s.get('call') or s.get('command')} (tier {self.tier or s.get('tier')})")
        if self.error:
            lines.append(f"Error: {self.error[:400]}")
        if self.cause:
            lines.append(f"Likely cause: {self.cause}")
        if self.next_step:
            lines.append(f"Next step: {self.next_step}")
        if self.planner_rounds:
            tried = "; ".join(" → ".join(r.get("subtasks") or []) for r in self.planner_rounds[-3:] if r.get("subtasks"))
            if tried:
                lines.append(f"The planner tried: {tried[:500]}")
        if self.gate:
            lines.append("Gate: " + "; ".join(self.gate[:4]))
        if self.code:
            lines.append("Code that produced it:")
            for c in self.code[:4]:
                lines.append(f"  • {c.module}.{c.function} — {c.ref()}" + (f"  (matched “{c.matched[:70]}”)" if c.matched else ""))
        if self.explanation:
            lines.append(f"\n{self.explanation}")
        return "\n".join(lines)


# ------------------------------------------------------------ known patterns

PATTERNS: List[Tuple[str, str, str]] = [
    (r"not one of the step forms|could not interpret this step",
     "The planner wrote a step that isn't one of the step forms, so no Needle agent could run it.",
     "Compare the step with STEP_FORMS (servo/cognition/specialists.py) and the planner agent's prompt "
     "(servo/cognition/planners.py)."),
    (r"doesn't match the step|but Needle called|but Needle's",
     "Needle's tool call didn't match the step's own words, so check_call refused it.",
     "See check_call (servo/cognition/needle.py) and the step form's example in specialists.py."),
    (r"no ui element found|ambiguous target|no input field matching",
     "The label System 2 asked for wasn't in the window's accessibility tree (or matched several elements).",
     "Compare the control with the numbered 'Controls' list in the screen description. If it isn't exposed "
     "to UIA, the loop tries Windows OCR and then the vision locator; see their trace lines."),
    (r"could not bring hwnd .* foreground|refusing to send keystrokes",
     "Windows refused to bring the target window to the front (foreground lock), so Servo didn't type.",
     "Usually transient: retry. If it repeats, another window is holding focus (a dialog, a fullscreen app)."),
    (r"page didn't load|tab still shows",
     "The browser tab never navigated: the address was typed but not accepted.",
     "Check that the right browser window was focused and the address bar accepted Enter "
     "(servo/cognition/user_browser.py navigate)."),
    (r"typed text is not in the target control|typed text was not observed",
     "Text was typed, but reading the control back shows it isn't there; the keys went elsewhere or were lost.",
     "Check which control was focused; for editors with existing content, a new document/tab first helps."),
    (r"rejected by user confirmation|declined|timed out",
     "The action needed your approval and wasn't approved (declined, or nobody answered in time).",
     "Run it again and approve it, or rephrase the goal so it needs a lower-risk action."),
    (r"exited with code|is not recognized as the name of a cmdlet|missing the terminator",
     "A PowerShell command failed: usually wrong quoting or a wrapped `powershell -command` inside PowerShell.",
     "See the command's output in the step; the planner must send one plain PowerShell line."),
    (r"unparsable plan|did not return a json object",
     "The planner model's answer wasn't the JSON Servo expects.",
     "Try again; if it repeats, the model may be too small for planning (Config → reasoning model)."),
    (r"not installed|did not appear in time|could not find or launch",
     "The app couldn't be found or its window didn't appear.",
     "Check the app name; for web apps use 'go to <site>'."),
    (r"no visible change|not confirmed|effect not visible",
     "The step ran, but its effect wasn't visible in the accessibility tree, so success can't be confirmed.",
     "Look at the screen; if it did work, nothing is wrong. If not, the click likely hit a non-interactive element."),
    (r"servo copilot",
     "The step targeted Servo's own copilot window instead of an app.",
     "Name the app explicitly, or click into the app you mean before asking."),
]


def _pattern(text: str) -> Tuple[str, str]:
    for rx, cause, nxt in PATTERNS:
        if re.search(rx, text, re.I):
            return cause, nxt
    return "", ""


# --------------------------------------------------------------- code lookup

_EDGE = " .:;,'\"“”‘’()[]"


def locate_error(text: str, graph: Any = None, limit: int = 4) -> List[CodeLocation]:
    """Functions whose literal strings appear verbatim in ``text`` (longest match first)."""
    if graph is None:
        from servo.cognition.graph_memory import graph_memory
        graph_memory.refresh_if_stale()
        graph = graph_memory.graph
    low = " ".join(text.lower().split())
    hits: List[Tuple[int, CodeLocation]] = []
    for node in graph.nodes.values():
        if node.type != "Function":
            continue
        best = ""
        for s in node.attributes.get("strings") or []:
            frag = " ".join(re.sub(r"\{[^}]*\}", " ", s).lower().split()).strip(_EDGE)
            # f-string pieces: keep the longest literal run between placeholders
            pieces = [p.strip(_EDGE) for p in re.split(r"\s{2,}", frag) if len(p.strip(_EDGE)) >= 12]
            for p in pieces or ([frag] if len(frag) >= 12 else []):
                if p in low and len(p) > len(best):
                    best = p
        if best:
            parts = node.id.split(":")   # fn:mod:servo.x.y:name  or  fn:class:servo.x.y.Cls:name
            module = parts[2] if len(parts) >= 4 else node.id
            hits.append((len(best), CodeLocation(
                function=node.name, module=module,
                file=_rel(node.file_path or ""), line=int(node.attributes.get("lineno") or 0),
                end_line=int(node.attributes.get("end_lineno") or 0), matched=best, doc=node.description or "")))
    hits.sort(key=lambda h: -h[0])
    seen, out = set(), []
    for _, loc in hits:
        key = (loc.file, loc.function)
        if key not in seen:
            seen.add(key)
            out.append(loc)
        if len(out) >= limit:
            break
    return out


def _rel(path: str) -> str:
    try:
        root = Path(__file__).resolve().parent.parent.parent
        return str(Path(path).resolve().relative_to(root)).replace("\\", "/")
    except Exception:
        return path


# ------------------------------------------------------------------ diagnose

def _pick_run(run_id: str = "") -> Optional[Dict[str, Any]]:
    from servo.telemetry.run_record import list_runs, load_run
    if run_id:
        return load_run(run_id)
    for r in list_runs(limit=50):
        if r.get("route") == "diagnose":
            continue
        if not r.get("success") or (r.get("verdicts") or {}).get("unconfirmed"):
            return load_run(r["run_id"])
    return None


def diagnose(run_id: str = "", record: Optional[Dict[str, Any]] = None, graph: Any = None,
             explain: bool = False) -> Diagnosis:
    rec = record or _pick_run(run_id)
    if not rec:
        return Diagnosis(found=False, cause=(f"No saved run {run_id}." if run_id else
                                             "No recent run failed or ended unconfirmed, so there's nothing to diagnose."))
    steps = rec.get("steps") or []
    failing = next((s for s in steps if not s.get("success")), None) or \
        next((s for s in steps if s.get("verification") == "unconfirmed"), None) or {}
    status = "ok" if rec.get("success") and not failing else ("unconfirmed" if failing.get("success") else "failed")
    events = rec.get("events") or []
    fail_events = [e for e in events if e.get("status") == "fail" and e.get("kind") not in ("result",)]
    error = failing.get("error") or failing.get("note") or \
        (fail_events[-1].get("detail") if fail_events else "") or (rec.get("reply") if not rec.get("success") else "")
    evidence = " ".join(filter(None, [error, rec.get("reply") or "", *(e.get("detail") or "" for e in fail_events[-3:])]))
    cause, nxt = _pattern(evidence)
    gate = [f"{e.get('title')}: {e.get('detail')}"[:200] for e in events
            if e.get("tier") == "laya" and e.get("kind") == "decision" and e.get("status") in ("warn", "fail")]
    d = Diagnosis(
        run_id=rec.get("run_id", ""), goal=rec.get("goal", ""), status=status, failing_step=failing,
        error=error or "", tier=failing.get("tier", ""), cause=cause, next_step=nxt,
        planner_rounds=rec.get("rounds") or [], gate=gate,
        code=locate_error(evidence, graph) if evidence else [],
    )
    if explain:
        d.explanation = _explain(d)
    return d


def _explain(d: Diagnosis) -> str:
    """Optional narrative from System 2. Sends the diagnosis and function names/docstrings;
    code bodies only when SERVO_DIAGNOSE_SEND_CODE is true."""
    try:
        from servo.cognition.system2 import System2
        from servo.config import settings
        s2 = System2()
        if not s2.available():
            return ""
        code_ctx = []
        for c in d.code[:3]:
            entry = f"- {c.module}.{c.function} ({c.ref()}): {c.doc}"
            if settings.diagnose_send_code:
                entry += "\n" + _snippet(c)
            code_ctx.append(entry)
        prompt = (
            "A Servo desktop-automation run failed. Explain the most likely root cause in 2-4 sentences "
            "and suggest one concrete fix (to the request, the knowledge recipes, or the code).\n\n"
            + d.to_text() + ("\n\nRelevant code:\n" + "\n".join(code_ctx) if code_ctx else "")
        )
        res = s2._complete("You diagnose failures of a Windows automation agent. Be specific and brief.",
                           prompt, json_mode=False, max_tokens=800)
        return (res.reply or res.raw or "").strip()
    except Exception:
        return ""


def _snippet(c: CodeLocation, pad: int = 3) -> str:
    try:
        root = Path(__file__).resolve().parent.parent.parent
        lines = (root / c.file).read_text(encoding="utf-8").splitlines()
        a, b = max(0, c.line - 1 - pad), min(len(lines), (c.end_line or c.line) + pad)
        return "\n".join(f"{i + 1:5} {lines[i]}" for i in range(a, min(b, a + 60)))
    except Exception:
        return ""


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Explain why a Servo run failed.")
    ap.add_argument("run_id", nargs="?", default="", help="run id (default: latest failed/unconfirmed run)")
    ap.add_argument("--explain", action="store_true", help="also ask System 2 for a narrative")
    args = ap.parse_args(argv)
    print(diagnose(args.run_id, explain=args.explain).to_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
