"""Task recipes for System 2: short, tested procedures picked by trigger words.

Recipes live in ``knowledge/recipes/*.md``. Each has frontmatter with ``triggers``
(words or phrases matched on word boundaries against the request, the failure text and
recent conversation) and optional ``apps`` (process names of the active window). The
best few are injected into the planning prompt, so the planner gets concrete steps
("search youtube for X, then click the result link") instead of generic advice.

Files are re-read when they change, so edits take effect on the next request.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

RECIPES_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge" / "recipes"


class Recipe(BaseModel):
    id: str
    title: str
    triggers: List[str] = Field(default_factory=list)
    apps: List[str] = Field(default_factory=list)
    body: str = ""
    path: str = ""


def _parse_list(value: str) -> List[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    return [v.strip().strip("'\"").lower() for v in value.split(",") if v.strip()]


def parse_recipe(text: str, path: str = "") -> Optional[Recipe]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        return None
    meta: Dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip().lower()] = v.strip()
    if "id" not in meta:
        return None
    return Recipe(id=meta["id"], title=meta.get("title", meta["id"]), triggers=_parse_list(meta.get("triggers", "")),
                  apps=_parse_list(meta.get("apps", "")), body=m.group(2).strip(), path=path)


class RecipeBook:
    def __init__(self, directory: Optional[Path] = None) -> None:
        self.directory = Path(directory or RECIPES_DIR)
        self._lock = threading.Lock()
        self._stamp: Tuple = ()
        self._recipes: List[Recipe] = []

    def _files(self) -> List[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(p for p in self.directory.glob("*.md") if p.name.lower() != "readme.md")

    def recipes(self) -> List[Recipe]:
        files = self._files()
        stamp = tuple((str(p), p.stat().st_mtime_ns) for p in files)
        with self._lock:
            if stamp != self._stamp:
                loaded = []
                for p in files:
                    try:
                        r = parse_recipe(p.read_text(encoding="utf-8"), str(p))
                    except OSError:
                        r = None
                    if r:
                        loaded.append(r)
                    else:
                        logger.warning("Skipping recipe without frontmatter id: %s", p)
                self._recipes, self._stamp = loaded, stamp
            return list(self._recipes)

    def select(self, text: str, active_app: str = "", limit: int = 3) -> List[Tuple[Recipe, float, List[str]]]:
        """Best recipes for ``text``: (recipe, score, matched triggers), highest first."""
        low = (text or "").lower()
        app = (active_app or "").lower()
        scored = []
        for r in self.recipes():
            hits = [t for t in r.triggers if re.search(r"(?<![\w])" + re.escape(t) + r"(?![\w])", low)]
            score = sum(1.0 + 0.25 * len(t.split()) for t in hits)  # phrases count a bit more
            if app and app in r.apps:
                score += 1.5
                hits.append(f"app:{app}")
            if score > 0:
                scored.append((r, score, hits))
        scored.sort(key=lambda x: -x[1])
        return scored[:limit]

    def prompt_block(self, text: str, active_app: str = "", limit: int = 3) -> Tuple[str, List[str]]:
        """('[Recipes] ...' block, ids used). Empty block when nothing matches."""
        chosen = self.select(text, active_app, limit)
        if not chosen:
            return "", []
        parts = ["[Recipes: tested procedures for this kind of task. Follow them unless the screen shows otherwise]"]
        for r, _, _ in chosen:
            parts.append(f"## {r.title}\n{r.body}")
        return "\n\n".join(parts), [r.id for r, _, _ in chosen]


recipe_book = RecipeBook()
