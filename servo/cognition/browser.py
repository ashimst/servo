"""Playwright browser tier.

Web pages expose a poor UIA tree, so browser work goes through Playwright instead:
the DOM/accessibility tree is the perception source and locators are the actuators.

* Perception: role/label/placeholder/text locators. Exactly one visible match is a
  confident target; zero is a perception miss (→ Mirage on a page screenshot); several
  is ambiguity (→ System 2 with the candidates).
* Screen description for System 2: ``locator('body').aria_snapshot()``.

Playwright's sync API is bound to the thread that started it, so every call runs on a
dedicated driver thread.
"""

from __future__ import annotations

import hashlib
import io
import logging
import queue
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

from pydantic import BaseModel, Field

from servo.config import settings

logger = logging.getLogger("servo.browser")

GENERIC_INPUT_HINTS = {
    "", "editor", "input", "search", "search box", "searchbox", "search bar", "search field",
    "text box", "textbox", "box", "field", "query", "the search box", "search input",
}
CLICK_ROLES = ("button", "link", "menuitem", "tab", "checkbox", "radio", "option", "switch", "combobox", "textbox", "searchbox")
INPUT_ROLES = ("searchbox", "textbox", "combobox")


class BrowserUnavailable(RuntimeError):
    pass


class LocateResult(BaseModel):
    found: bool
    count: int = 0
    strategy: str = ""
    confidence: float = 0.0
    candidates: List[str] = Field(default_factory=list)
    tried: List[Dict[str, Any]] = Field(default_factory=list)


class PageState(BaseModel):
    url: str = ""
    title: str = ""
    snapshot: str = ""

    @property
    def digest(self) -> str:
        return hashlib.sha1(f"{self.url}|{self.title}|{self.snapshot}".encode()).hexdigest()


def playwright_installed() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


class BrowserTier:
    def __init__(self) -> None:
        self._q: "queue.Queue[Optional[Tuple[Callable[[], Any], queue.Queue]]]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._pw: Any = None
        self._context: Any = None
        self._lock = threading.Lock()
        self.channel_used: Optional[str] = None

    # ----------------------------------------------------------- driver thread

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            fn, reply = item
            try:
                reply.put((True, fn()))
            except BaseException as e:  # noqa: BLE001 - marshal everything back
                reply.put((False, e))
        try:
            if self._context is not None:
                self._context.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._context = self._pw = None

    def _run(self, fn: Callable[[], Any], timeout: float = 60.0) -> Any:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="servo-playwright", daemon=True)
                self._thread.start()
        reply: "queue.Queue" = queue.Queue()
        self._q.put((fn, reply))
        ok, val = reply.get(timeout=timeout)
        if not ok:
            raise val
        return val

    def close(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._q.put(None)
            self._thread.join(timeout=10)

    # ------------------------------------------------------- inside the thread

    def _ensure_page(self) -> Any:
        if self._context is not None:
            try:
                pages = [p for p in self._context.pages if not p.is_closed()]
                if pages:
                    return pages[-1]
                return self._context.new_page()
            except Exception:
                self._context = None  # the user closed the browser window
        if not playwright_installed():
            raise BrowserUnavailable("Playwright is not installed: pip install servo[browser]")
        from playwright.sync_api import sync_playwright

        if self._pw is None:
            self._pw = sync_playwright().start()
        settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        channel = (settings.browser_channel or "").strip().lower()
        attempts = [channel] if channel and channel != "chromium" else []
        attempts.append("")  # bundled Chromium as last resort
        last_err: Optional[Exception] = None
        for ch in attempts:
            try:
                kwargs: Dict[str, Any] = {
                    "user_data_dir": str(settings.browser_profile_dir),
                    "headless": settings.browser_headless,
                    "no_viewport": True,
                    "args": ["--start-maximized"],
                }
                if ch:
                    kwargs["channel"] = ch
                self._context = self._pw.chromium.launch_persistent_context(**kwargs)
                self.channel_used = ch or "chromium"
                break
            except Exception as e:
                last_err = e
                logger.info("Playwright launch with channel %r failed: %s", ch or "chromium", e)
        if self._context is None:
            raise BrowserUnavailable(f"Could not start a Playwright browser: {last_err}")
        pages = self._context.pages
        return pages[0] if pages else self._context.new_page()

    @staticmethod
    def _visible(loc: Any) -> Any:
        try:
            return loc.filter(visible=True)
        except TypeError:  # Playwright < 1.51
            return loc.locator("visible=true")

    def _strategies(self, page: Any, target: str, for_input: bool) -> List[Tuple[str, Any, float]]:
        t = target.strip().strip("'\"")
        out: List[Tuple[str, Any, float]] = []
        if for_input and t.lower() in GENERIC_INPUT_HINTS:
            out += [
                ("searchbox role", page.get_by_role("searchbox"), 0.9),
                ("search input", page.locator("input[type=search]"), 0.9),
                ("combobox role", page.get_by_role("combobox"), 0.8),
                ("textbox role", page.get_by_role("textbox"), 0.75),
            ]
            return out
        roles = INPUT_ROLES if for_input else CLICK_ROLES
        for r in roles:
            out.append((f"role={r} name=“{t}” (exact)", page.get_by_role(r, name=t, exact=True), 0.95))
        out += [
            (f"label “{t}” (exact)", page.get_by_label(t, exact=True), 0.93),
            (f"placeholder “{t}” (exact)", page.get_by_placeholder(t, exact=True), 0.93),
        ]
        if not for_input:
            out.append((f"text “{t}” (exact)", page.get_by_text(t, exact=True), 0.88))
        for r in roles:
            out.append((f"role={r} name~“{t}”", page.get_by_role(r, name=t), 0.78))
        out += [
            (f"label~“{t}”", page.get_by_label(t), 0.75),
            (f"placeholder~“{t}”", page.get_by_placeholder(t), 0.75),
            (f"title~“{t}”", page.get_by_title(t), 0.72),
        ]
        if not for_input:
            out.append((f"text~“{t}”", page.get_by_text(t), 0.7))
        return out

    def _locate(self, page: Any, target: str, for_input: bool) -> Tuple[LocateResult, Any]:
        tried: List[Dict[str, Any]] = []
        ambiguous: Optional[Tuple[str, Any, int, float]] = None
        generic = for_input and target.strip().lower() in GENERIC_INPUT_HINTS
        for name, loc, conf in self._strategies(page, target, for_input):
            try:
                vis = self._visible(loc)
                n = vis.count()
            except Exception as e:
                tried.append({"strategy": name, "error": str(e)[:120]})
                continue
            tried.append({"strategy": name, "count": n})
            if n == 1:
                return LocateResult(found=True, count=1, strategy=name, confidence=conf, tried=tried), vis.first
            if n > 1:
                if generic:  # "type into the search box": first visible field is the convention
                    return LocateResult(found=True, count=n, strategy=name + " (first of many)",
                                        confidence=round(conf * 0.85, 3), tried=tried), vis.first
                if ambiguous is None:
                    ambiguous = (name, vis, n, conf)
        if ambiguous is not None:
            name, vis, n, conf = ambiguous
            cands: List[str] = []
            for i in range(min(n, 6)):
                try:
                    cands.append((vis.nth(i).inner_text(timeout=500) or "").strip()[:60])
                except Exception:
                    pass
            return LocateResult(found=False, count=n, strategy=name, confidence=0.4, candidates=cands, tried=tried), None
        return LocateResult(found=False, count=0, tried=tried), None

    def _state(self, page: Any, with_snapshot: bool = True) -> PageState:
        snap = ""
        if with_snapshot:
            try:
                snap = page.locator("body").aria_snapshot(timeout=3000)
            except Exception:
                try:
                    snap = page.locator("body").inner_text(timeout=3000)
                except Exception:
                    snap = ""
        try:
            title = page.title()
        except Exception:
            title = ""
        return PageState(url=page.url, title=title, snapshot=snap[:30000])

    @staticmethod
    def _settle(page: Any) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        page.wait_for_timeout(350)

    # ------------------------------------------------------------- public API

    def is_open(self) -> bool:
        def _f() -> bool:
            try:
                return self._context is not None and any(not p.is_closed() for p in self._context.pages)
            except Exception:
                return False
        if self._thread is None or not self._thread.is_alive():
            return False
        return bool(self._run(_f, timeout=5))

    def open_url(self, url: str) -> PageState:
        def _f() -> PageState:
            page = self._ensure_page()
            page.bring_to_front()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            self._settle(page)
            return self._state(page)
        return self._run(_f)

    def web_search(self, query: str) -> PageState:
        return self.open_url(settings_search_url().format(q=quote_plus(query)))

    def state(self, with_snapshot: bool = True) -> PageState:
        return self._run(lambda: self._state(self._ensure_page(), with_snapshot))

    def locate(self, target: str, for_input: bool = False) -> LocateResult:
        return self._run(lambda: self._locate(self._ensure_page(), target, for_input)[0])

    def click(self, target: str, button: str = "left") -> Tuple[LocateResult, PageState, PageState]:
        def _f() -> Tuple[LocateResult, PageState, PageState]:
            page = self._ensure_page()
            page.bring_to_front()
            before = self._state(page)
            res, loc = self._locate(page, target, for_input=False)
            if loc is None or res.confidence < settings.needle_confidence_threshold:
                return res, before, before
            if button == "double":
                loc.dblclick(timeout=8000)
            else:
                loc.click(button="right" if button == "right" else "left", timeout=8000)
            self._settle(page)
            return res, before, self._state(page)
        return self._run(_f)

    def fill(self, target: str, text: str, press_enter: bool = False, clear_first: bool = True) -> Tuple[LocateResult, PageState, PageState, str]:
        def _f() -> Tuple[LocateResult, PageState, PageState, str]:
            page = self._ensure_page()
            page.bring_to_front()
            before = self._state(page)
            res, loc = self._locate(page, target, for_input=True)
            if loc is None or res.confidence < settings.needle_confidence_threshold:
                return res, before, before, ""
            if clear_first:
                loc.fill(text, timeout=8000)
            else:
                loc.press_sequentially(text, timeout=8000)
            value = ""
            try:
                value = loc.input_value(timeout=1000)
            except Exception:
                pass
            if press_enter:
                loc.press("Enter")
                self._settle(page)
            return res, before, self._state(page), value
        return self._run(_f)

    def press(self, keys: List[str]) -> Tuple[PageState, PageState]:
        combo = "+".join(_pw_key(k) for k in keys)

        def _f() -> Tuple[PageState, PageState]:
            page = self._ensure_page()
            before = self._state(page)
            page.keyboard.press(combo)
            self._settle(page)
            return before, self._state(page)
        return self._run(_f)

    def screenshot(self) -> Optional[bytes]:
        return self._run(lambda: self._ensure_page().screenshot(scale="css", timeout=10000))

    def click_point(self, x: int, y: int) -> Tuple[PageState, PageState]:
        def _f() -> Tuple[PageState, PageState]:
            page = self._ensure_page()
            before = self._state(page)
            # Move along a short path first so hover-revealed controls appear.
            page.mouse.move(x, y, steps=12)
            page.mouse.click(x, y)
            self._settle(page)
            return before, self._state(page)
        return self._run(_f)

    def screenshot_image(self) -> Any:
        from PIL import Image
        data = self.screenshot()
        return Image.open(io.BytesIO(data)) if data else None


_KEYMAP = {"ctrl": "Control", "control": "Control", "alt": "Alt", "shift": "Shift", "win": "Meta",
           "enter": "Enter", "return": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab",
           "backspace": "Backspace", "delete": "Delete", "del": "Delete", "space": "Space",
           "up": "ArrowUp", "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
           "pageup": "PageUp", "pagedown": "PageDown", "home": "Home", "end": "End"}


def _pw_key(k: str) -> str:
    k = k.strip()
    low = k.lower()
    if low in _KEYMAP:
        return _KEYMAP[low]
    if re.fullmatch(r"f\d{1,2}", low):
        return low.upper()
    return k if len(k) != 1 else k.lower()


def settings_search_url() -> str:
    return getattr(settings, "search_url", "") or "https://www.bing.com/search?q={q}"


def snapshot_delta(before: PageState, after: PageState) -> Dict[str, Any]:
    """SceneDelta-shaped summary of a page transition (for the Kev post-check)."""
    b = set(before.snapshot.splitlines())
    a = set(after.snapshot.splitlines())
    added = sorted(line.strip() for line in (a - b))[:40]
    removed = sorted(line.strip() for line in (b - a))[:40]
    return {
        "has_changed": before.digest != after.digest,
        "title_changed": before.title != after.title or before.url != after.url,
        "old_title": before.title,
        "new_title": f"{after.title} <{after.url}>",
        "added_labels": added,
        "removed_labels": removed,
        "value_changes": {},
    }


browser_tier = BrowserTier()
