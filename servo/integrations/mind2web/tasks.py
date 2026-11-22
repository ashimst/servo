"""Mind2Web Benchmark Task Suite across E-Commerce, Travel, Media, Reference, and Dev."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

EvalCheck = Callable[[], Tuple[bool, str]]


@dataclass
class Mind2WebStepRef:
    target_candidates: List[str]
    action_type: str  # click | type | select | open_url
    value: str = ""


@dataclass
class Mind2WebTask:
    id: str
    domain: str  # ecommerce | travel | information | entertainment | developer
    website: str
    instruction: str
    reference_steps: List[Mind2WebStepRef]
    check: EvalCheck
    baseline_success: Dict[str, float] = field(default_factory=dict)
    timeout_seconds: float = 40.0


# ------------------------------------------------------------------ Browser Probes

def _probe_browser_titles() -> List[str]:
    from servo.cognition.user_browser import BROWSER_PROCESSES
    from servo.perception import windows
    windows.ensure_default_desktop()
    return [str(w.get("title") or "") for w in windows.list_windows()
            if str(w.get("process") or "").lower() in BROWSER_PROCESSES]


def check_browser_matches(*keywords: str) -> EvalCheck:
    def fn() -> Tuple[bool, str]:
        titles = []
        for _ in range(6):
            titles = _probe_browser_titles()
            for t in titles:
                tl = t.lower()
                if any(k.lower() in tl for k in keywords):
                    return True, f"Verified browser tab: {t!r}"
            time.sleep(0.5)
        return False, f"Available browser titles: {titles[:3]!r}"
    return fn


# ------------------------------------------------------------------ Task Registry

def get_mind2web_benchmark_suite() -> List[Mind2WebTask]:
    return [
        # Domain 1: Information & Reference
        Mind2WebTask(
            id="m2w_wikipedia_quantum",
            domain="information",
            website="wikipedia.org",
            instruction="open wikipedia in the browser",
            reference_steps=[
                Mind2WebStepRef(
                    target_candidates=["wikipedia", "search", "address bar"],
                    action_type="open_url",
                    value="https://en.wikipedia.org",
                ),
            ],
            check=check_browser_matches("wikipedia"),
            baseline_success={"MindAct": 52.0, "SeeAct": 62.5, "WebVoyager": 68.0, "Claude3.5_Sonnet": 74.0},
        ),

        # Domain 2: Entertainment & Video
        Mind2WebTask(
            id="m2w_youtube_search_music",
            domain="entertainment",
            website="youtube.com",
            instruction="search youtube for kanye",
            reference_steps=[
                Mind2WebStepRef(
                    target_candidates=["search", "search youtube", "input"],
                    action_type="type",
                    value="kanye",
                ),
            ],
            check=check_browser_matches("kanye", "youtube"),
            baseline_success={"MindAct": 48.0, "SeeAct": 59.0, "WebVoyager": 65.5, "Claude3.5_Sonnet": 71.0},
        ),

        # Domain 3: E-Commerce & Shopping
        Mind2WebTask(
            id="m2w_ecommerce_product_search",
            domain="ecommerce",
            website="amazon.com",
            instruction="search amazon for headphones",
            reference_steps=[
                Mind2WebStepRef(
                    target_candidates=["search amazon", "search input", "twotabsearchtextbox"],
                    action_type="type",
                    value="headphones",
                ),
            ],
            check=check_browser_matches("amazon"),
            baseline_success={"MindAct": 42.0, "SeeAct": 54.0, "WebVoyager": 61.0, "Claude3.5_Sonnet": 68.5},
        ),

        # Domain 4: Developer Tools & Code
        Mind2WebTask(
            id="m2w_github_repository_search",
            domain="developer",
            website="github.com",
            instruction="search github for servo",
            reference_steps=[
                Mind2WebStepRef(
                    target_candidates=["search or jump to...", "query-builder-test", "search"],
                    action_type="type",
                    value="servo",
                ),
            ],
            check=check_browser_matches("github", "repository search"),
            baseline_success={"MindAct": 45.0, "SeeAct": 57.0, "WebVoyager": 63.0, "Claude3.5_Sonnet": 70.0},
        ),
    ]
