"""Dynamic Persistent Chat Memory Engine.

Provides longer-term persistent conversational memory for Servo Copilot:
1. Disk Persistence: Saves conversations and distilled episodic facts to
   ~/.servo/chat_memory.json across app restarts and crashes.
2. Dynamic Sliding Window: Keeps the most recent K turns verbatim for low-latency
   dialogue continuity.
3. Rolling Episodic Compaction: Dynamically distills older turns into persistent
   bullet points (User Facts, Preferences, Completed Tasks, and Active Entities)
   without token window explosion.
4. Semantic / Keyword Recall: Retrieves relevant past turns when a user refers
   to entities, files, or tasks from earlier in the session.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger("servo.cognition.memory")

DEFAULT_MEMORY_PATH = Path.home() / ".servo" / "chat_memory.json"


# ===================================================================== Models

class MemoryEntry(BaseModel):
    role: str  # "user" | "assistant" | "system"
    text: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    run_id: Optional[str] = None
    route: Optional[str] = None
    entities: List[str] = Field(default_factory=list)


class MemorySummary(BaseModel):
    user_facts: List[str] = Field(default_factory=list)
    completed_tasks: List[str] = Field(default_factory=list)
    active_entities: Dict[str, str] = Field(default_factory=dict)
    overview: str = ""


# ======================================================= Entity & Fact Extractor

_PATH_RE = re.compile(r'(?:[A-Za-z]:\\[^"\'\s<>\?]+|\b[\w\-_\.]+\.(?:txt|py|json|md|html|csv|pdf|docx|png|jpg)\b)', re.I)
_URL_RE = re.compile(r'https?://[^\s"\'<>]+', re.I)
_APP_VERB_RE = re.compile(r'^(?:open|launch|start|switch to)\s+([a-zA-Z0-9_\s]+)$', re.I)

_FACT_PATTERNS = [
    re.compile(r'\bmy\s+name\s+is\s+([A-Z][a-z0-9_-]+)', re.I),
    re.compile(r'\bcall\s+me\s+([A-Z][a-z0-9_-]+)', re.I),
    re.compile(r'\bmy\s+project\s+(?:is|in)\s+([A-Za-z]:\\[^"\'\n]+)', re.I),
    re.compile(r'\bI\s+(?:prefer|use)\s+([a-zA-Z0-9_\s]+)\s+(?:as|for)\s+(?:my\s+)?(browser|editor|terminal)', re.I),
    re.compile(r'\bremember\s+(?:that\s+)?(.+)', re.I),
]


def _extract_entities(text: str) -> List[str]:
    """Extract file paths, URLs, and key entities mentioned in a turn."""
    found: Set[str] = set()
    for m in _PATH_RE.findall(text):
        if len(m) > 3 and not m.endswith("."):
            found.add(m)
    for m in _URL_RE.findall(text):
        found.add(m)
    return sorted(found)


def _extract_facts(user_text: str) -> List[str]:
    """Extract stated user facts and preferences deterministically."""
    facts: List[str] = []
    t = user_text.strip()
    for pat in _FACT_PATTERNS:
        m = pat.search(t)
        if m:
            if len(m.groups()) == 1:
                val = m.group(1).strip().rstrip(".")
                facts.append(f"User stated: {val}")
            elif len(m.groups()) == 2:
                tool, cat = m.group(1).strip(), m.group(2).strip()
                facts.append(f"User prefers {tool} for {cat}")
    return facts


# ==================================================== DynamicChatMemory Engine

class DynamicChatMemory:
    """Persistent, dynamically compacted within-chat memory engine."""

    def __init__(
        self,
        storage_path: Optional[Path] = None,
        max_buffer_size: int = 6,
        compaction_threshold: int = 12,
    ) -> None:
        env_path = os.environ.get("SERVO_CHAT_MEMORY_PATH")
        self.storage_path = Path(env_path) if env_path else (storage_path or DEFAULT_MEMORY_PATH)
        self.max_buffer_size = max_buffer_size
        self.compaction_threshold = compaction_threshold
        self.entries: List[MemoryEntry] = []
        self.summary = MemorySummary()
        self._lock = threading.RLock()
        self.load()

    # ----------------------------------------------------------- Persistence

    def load(self) -> None:
        """Load persisted chat memory from disk."""
        with self._lock:
            if not self.storage_path.exists():
                return
            try:
                raw = json.loads(self.storage_path.read_text(encoding="utf-8"))
                self.entries = [MemoryEntry(**e) for e in raw.get("entries", [])]
                if "summary" in raw:
                    self.summary = MemorySummary(**raw["summary"])
                logger.debug("Loaded %d chat memory entries from %s", len(self.entries), self.storage_path)
            except Exception as e:
                logger.warning("Failed loading chat memory from %s (%s); starting fresh.", self.storage_path, e)

    def save(self) -> None:
        """Atomically persist memory to disk."""
        with self._lock:
            try:
                self.storage_path.parent.mkdir(parents=True, exist_ok=True)
                data = {
                    "version": 1,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "summary": self.summary.model_dump(),
                    "entries": [e.model_dump() for e in self.entries],
                }
                serialized = json.dumps(data, indent=2, ensure_ascii=False)
                # Atomic write via temp file in same directory
                with tempfile.NamedTemporaryFile("w", dir=self.storage_path.parent, delete=False, encoding="utf-8") as tf:
                    tf.write(serialized)
                    temp_name = tf.name
                os.replace(temp_name, self.storage_path)
            except Exception as e:
                logger.warning("Failed saving chat memory to %s: %s", self.storage_path, e)

    # ------------------------------------------------------- Turn Management

    def add_turn(
        self,
        role: str,
        text: str,
        run_id: Optional[str] = None,
        route: Optional[str] = None,
    ) -> MemoryEntry:
        """Add a conversation turn, extract entities/facts, and trigger compaction if needed."""
        text = text.strip()
        entities = _extract_entities(text)
        entry = MemoryEntry(role=role, text=text, run_id=run_id, route=route, entities=entities)

        with self._lock:
            self.entries.append(entry)

            # Fact extraction on user turns
            if role == "user":
                new_facts = _extract_facts(text)
                for f in new_facts:
                    if f not in self.summary.user_facts:
                        self.summary.user_facts.append(f)

            # Update active entities
            for ent in entities:
                if ent.startswith("http"):
                    self.summary.active_entities["last_url"] = ent
                elif re.search(r'\.[a-zA-Z0-9]+$', ent):
                    self.summary.active_entities["last_file"] = ent

            # Record task completion on successful assistant turns
            if role == "assistant" and text and not text.startswith("Failed:"):
                # Pair with the preceding user goal if available
                user_prev = [e for e in self.entries[:-1] if e.role == "user"]
                if user_prev:
                    last_goal = user_prev[-1].text
                    clean_reply = text.split("\n")[0]
                    task_summary = f"User: \"{last_goal}\" ──► Result: {clean_reply}"
                    if task_summary not in self.summary.completed_tasks:
                        self.summary.completed_tasks.append(task_summary)
                        if len(self.summary.completed_tasks) > 15:
                            self.summary.completed_tasks.pop(0)

            # Dynamic compaction when total turns exceed threshold
            if len(self.entries) >= self.compaction_threshold:
                self._compact_locked()

        self.save()
        return entry

    def _compact_locked(self) -> None:
        """Compact older entries beyond the recent sliding window buffer."""
        if len(self.entries) <= self.max_buffer_size:
            return

        # Older entries to be compacted
        older = self.entries[:-self.max_buffer_size]

        # Distill older entries into summary overview
        retained_facts: Set[str] = set(self.summary.user_facts)
        for e in older:
            if e.role == "user":
                for f in _extract_facts(e.text):
                    retained_facts.add(f)
            for ent in e.entities:
                if ent.startswith("http"):
                    self.summary.active_entities["last_url"] = ent
                elif re.search(r'\.[a-zA-Z0-9]+$', ent):
                    self.summary.active_entities["last_file"] = ent

        self.summary.user_facts = list(retained_facts)[:12]

        # Build concise historical digest
        archived_goals = [f"- {e.text}" for e in older if e.role == "user"][:10]
        if archived_goals:
            self.summary.overview = "Prior session goals:\n" + "\n".join(archived_goals)

        # Retain recent buffer + keep compact history size (bounded to at most 40 entries)
        if len(self.entries) > 40:
            self.entries = self.entries[-40:]

    # ------------------------------------------------------- Dynamic Recall

    def recall(self, query: str, limit: int = 3) -> List[MemoryEntry]:
        """Retrieve historical turns relevant to a query from outside the recent buffer."""
        q_words = set(re.findall(r"\w+", query.lower()))
        if not q_words:
            return []

        with self._lock:
            # Look in entries outside the immediate sliding buffer
            older_entries = self.entries[:-self.max_buffer_size] if len(self.entries) > self.max_buffer_size else []
            scored: List[Tuple[float, MemoryEntry]] = []

            for entry in older_entries:
                e_words = set(re.findall(r"\w+", entry.text.lower()))
                overlap = len(q_words.intersection(e_words))
                if overlap > 0:
                    score = overlap / (len(q_words) + 1.0)
                    scored.append((score, entry))

            scored.sort(key=lambda x: -x[0])
            return [e for _, e in scored[:limit]]

    # ------------------------------------------------------- Context Generation

    def to_prompt_context(self, query: Optional[str] = None) -> str:
        """Render a compact, structured memory context block for System 2."""
        with self._lock:
            lines = ["[Persistent Conversation Memory]"]

            # 1. Distilled User Facts & Preferences
            if self.summary.user_facts:
                lines.append("Known User Facts:")
                for fact in self.summary.user_facts[:6]:
                    lines.append(f"  * {fact}")

            # 2. Active Referenced Entities
            if self.summary.active_entities:
                ent_parts = [f"{k}: {v}" for k, v in self.summary.active_entities.items()]
                lines.append(f"Active References: {', '.join(ent_parts)}")

            # 3. Relevant Recalled Turns (if query matches older turns)
            if query:
                recalled = self.recall(query, limit=2)
                if recalled:
                    lines.append("Relevant Context From Earlier in Session:")
                    for r in recalled:
                        lines.append(f"  * {r.role}: \"{r.text}\"")

            # 4. Recent Conversation Buffer (Sliding Window)
            recent = self.entries[-self.max_buffer_size:]
            if recent:
                lines.append("Recent Conversation:")
                for r in recent:
                    clean_t = r.text.replace("\n", " ")
                    if len(clean_t) > 160:
                        clean_t = clean_t[:157] + "..."
                    lines.append(f"  {r.role.capitalize()}: {clean_t}")
            else:
                lines.append("Recent Conversation: (Beginning of session)")

            return "\n".join(lines)

    def get_recent_history(self, limit: int = 20) -> List[Dict[str, str]]:
        """Return raw history list for API compatibility."""
        with self._lock:
            return [{"role": e.role, "text": e.text} for e in self.entries[-limit:]]

    def clear(self) -> None:
        """Clear memory and remove storage file."""
        with self._lock:
            self.entries = []
            self.summary = MemorySummary()
            try:
                if self.storage_path.exists():
                    self.storage_path.unlink()
            except Exception as e:
                logger.warning("Could not delete memory file %s: %s", self.storage_path, e)


# Global singleton instance
chat_memory = DynamicChatMemory()
