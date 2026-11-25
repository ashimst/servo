"""Needle's tools for what a user types: one-line desktop requests and commands to Servo itself.

Two Needle agents read the user's own words (servo/cognition/needle.py reads planner steps):

* REQUEST_TOOLS: one desktop action (open an app, louder, open bluetooth settings, search
  YouTube for X, read notes.txt). A confident, grounded call runs directly; anything else
  goes to the planner.
* COMMAND_TOOLS: requests to Servo itself (schedule or queue a request, list or cancel the
  schedule, set the browser, explain a failure, remember a fact). These replace the regex
  parsers that used to recognise only fixed phrasings.

Tool design, measured on Needle 3 (needle_model/README.md):
  * one tool per action, a few words each; Needle picks tools by the words in their
    descriptions, so a description lists its own trigger words and never a sibling's
    ("Never closes it" pulled close requests to that tool);
  * every value is copied word for word or bounded (minimum/maximum), and code turns the
    copied phrase into a setting (a settings topic into an ms-settings page, a time phrase
    into a time), never Needle;
  * a call acts only when it passes ``accept``: confidence at or above the threshold, no
    engine flag (ungrounded value, negated request), and every copied value present in the
    request's own words.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_NAME = {"type": "string", "minLength": 1, "maxLength": 60}
_TEXT = {"type": "string", "minLength": 1, "maxLength": 200}


def _tool(name: str, description: str, props: Optional[Dict[str, Dict[str, Any]]] = None,
          required: Optional[List[str]] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {"type": "object", "properties": props or {}}
    if required:
        params["required"] = required
    return {"name": name, "description": description, "parameters": params}


def _s(desc: str, base: Dict[str, Any] = _NAME) -> Dict[str, Any]:
    return {**base, "description": desc}


def _pct(desc: str, low: int) -> Dict[str, Any]:
    return {"type": "integer", "description": desc, "minimum": low, "maximum": 100}


REQUEST_SYSTEM = ("You map one request from a Windows user to tool calls. Copy names, paths and text word for "
                  "word; never guess a missing value. One call per action asked for. A request that needs several "
                  "steps, works inside a document, asks a question, is chat, is vague, or is negated gets no call.")

REQUEST_TOOLS: List[Dict[str, Any]] = [
    _tool("open_app", "Open, launch, start, switch to or bring up a program by name.",
          {"name": _s("The program name copied word for word, e.g. notepad, calculator.")}, ["name"]),
    _tool("close_app", "Quit, exit or shut down a program.",
          {"name": _s("The program name copied word for word.")}, ["name"]),
    _tool("minimize_window", "Minimize or hide a window to the taskbar.",
          {"name": _s("The program name copied word for word.")}, ["name"]),
    _tool("maximize_window", "Maximize a window, make it full size or full screen.",
          {"name": _s("The program name copied word for word.")}, ["name"]),
    _tool("open_settings", "Go to a Windows Settings page: bluetooth, display, wifi, network, sound settings, "
                           "time zone, date, windows update, background, default apps, night light.",
          {"topic": _s("The settings topic copied word for word.")}, ["topic"]),
    _tool("set_brightness", "Set the screen brightness or dim the display.",
          {"percent": _pct("The brightness level as stated.", 0)}, ["percent"]),
    _tool("volume_up", "Louder: turn up, raise or increase the volume.",
          {"percent": _pct("The amount as stated; include only when a number is given.", 1)}),
    _tool("volume_down", "Quieter: turn down, lower, reduce or decrease the volume.",
          {"percent": _pct("The amount as stated; include only when a number is given.", 1)}),
    _tool("mute", "Mute or silence the audio."),
    _tool("unmute", "Unmute, turn the audio back on."),
    _tool("pause_media", "Pause or stop the song, music or video."),
    _tool("resume_media", "Resume, continue or keep playing the song, music or video."),
    _tool("next_track", "Skip ahead to the next song or track."),
    _tool("previous_track", "Go back to the previous or last song or track, play it again."),
    _tool("open_website", "Go to or visit a website or web address in the browser.",
          {"address": _s("The site or web address copied word for word, e.g. github.com.")}, ["address"]),
    _tool("search_web", "Search the web, google or look up something online.",
          {"query": _s("The words to search for, copied word for word.", _TEXT)}, ["query"]),
    _tool("search_youtube", "Search YouTube for videos.",
          {"query": _s("The words to search for, copied word for word.", _TEXT)}, ["query"]),
    _tool("new_tab", "Open a new, empty browser tab."),
    _tool("close_browser_tab", "Close the current browser tab."),
    _tool("restore_closed_tab", "Reopen or bring back the browser tab that was just closed."),
    _tool("list_folder", "List or show the files in a folder.",
          {"path": _s("The folder copied word for word: downloads, documents, desktop or a full path.")}, ["path"]),
    _tool("read_file", "Read a text file and show what it says.",
          {"path": _s("The file name or path copied word for word.")}, ["path"]),
    _tool("write_file", "Write or save text into a file.",
          {"path": _s("The file name or path copied word for word."),
           "text": _s("The text to write, copied word for word.", _TEXT)}, ["path", "text"]),
    _tool("open_file", "Open a document or file, such as a .pdf or .xlsx, with its default app.",
          {"path": _s("The file name or path copied word for word.")}, ["path"]),
]

BROWSERS = ["dia", "arc", "chrome", "edge", "firefox", "brave", "opera", "vivaldi"]

COMMAND_SYSTEM = ("You recognise commands addressed to the assistant itself: scheduling or queueing a request, "
                  "managing the schedule, choosing the browser, explaining a failure, remembering a fact. Copy "
                  "phrases word for word. An ordinary task on the computer, a question or chat gets no call.")

COMMAND_TOOLS: List[Dict[str, Any]] = [
    _tool("schedule_request", "Do a request later or repeatedly at a stated time, e.g. in 10 minutes, tomorrow at "
                              "9am, every day at 8, every 30 minutes.",
          {"when": _s("The time phrase copied word for word."),
           "request": _s("What to do, copied word for word without the time phrase.", _TEXT)}, ["when", "request"]),
    _tool("queue_request", "Do a request after the current one is finished: after this, when you're done.",
          {"request": _s("What to do, copied word for word.", _TEXT)}, ["request"]),
    _tool("list_scheduled", "Show what is scheduled or queued."),
    _tool("cancel_scheduled", "Cancel, remove or drop one scheduled job by its id.",
          {"job_id": {"type": "string", "description": "The job id copied exactly, e.g. ab12cd.",
                      "minLength": 4, "maxLength": 8}}, ["job_id"]),
    _tool("clear_schedule", "Cancel everything that is scheduled or queued."),
    _tool("set_default_browser", "Remember which browser to open web pages in from now on.",
          {"browser": {"type": "string", "enum": BROWSERS, "description": "The browser the user named."}},
          ["browser"]),
    _tool("diagnose_failure", "Explain why the last request failed or what went wrong.",
          {"run_id": {"type": "string", "description": "A run id, only when the user gives one.",
                      "minLength": 4, "maxLength": 12}}),
    _tool("remember_fact", "Remember something the user tells about themselves: their name, a preference, "
                           "where something is.",
          {"fact": _s("The fact copied word for word.", _TEXT)}, ["fact"]),
]

TOOL_NAMES = {t["name"] for t in REQUEST_TOOLS} | {t["name"] for t in COMMAND_TOOLS}
_SCHEMAS = {t["name"]: t for t in REQUEST_TOOLS + COMMAND_TOOLS}


# ------------------------------------------------------------------ acceptance

def _norm(s: Any) -> str:
    return " ".join(re.sub(r"[^\w:/.\\%'-]+", " ", str(s).lower()).split())


def ungrounded_values(text: str, name: str, args: Dict[str, Any]) -> List[str]:
    """Arguments whose value is not in the request's own words: a copied string must appear in
    the text, a number must be written in it. Enum values (a browser) are checked by the engine."""
    props = (_SCHEMAS.get(name, {}).get("parameters") or {}).get("properties", {})
    t = _norm(text)
    numbers = set(re.findall(r"\d+", text or ""))
    bad = []
    for k, v in (args or {}).items():
        if v is None or isinstance(v, bool) or "enum" in props.get(k, {}):
            continue
        if isinstance(v, (int, float)):
            if str(int(v)) not in numbers:
                bad.append(k)
        elif not _norm(v) or _norm(v) not in t:
            bad.append(k)
    return bad


def accept(text: str, resp: Dict[str, Any], threshold: float) -> Tuple[List[Dict[str, Any]], str]:
    """The calls Needle may act on, or ([], why not). All or nothing: one rejected call rejects all."""
    calls = [c for c in (resp.get("function_calls") or []) if isinstance(c, dict)]
    if not calls:
        return [], "no tool fits"
    val = resp.get("validation") or {}
    if val.get("negation"):
        return [], "the request is negated"
    if val.get("ungrounded"):
        return [], f"made-up values: {', '.join(map(str, val['ungrounded']))}"
    for c in calls:
        name = str(c.get("name"))
        if name not in TOOL_NAMES:
            return [], f"unknown tool {name}"
        bad = ungrounded_values(text, name, c.get("arguments") or {})
        if bad:
            return [], f"{name}: {', '.join(bad)} not in the request"
    conf = resp.get("confidence")
    if not isinstance(conf, (int, float)):
        return [], "no confidence reported"
    if conf < threshold:
        return [], f"not confident ({conf:.2f} < {threshold:.2f})"
    return calls, ""


# ------------------------------------------------------------------ settings topics

# A settings topic as the user said it -> the ms-settings page. Code, not Needle, owns this
# mapping; a topic it does not know is not guessed (the request goes to the planner).
SETTINGS_PAGES: List[Tuple[Tuple[str, ...], str]] = [
    (("bluetooth", "devices"), "bluetooth"),
    (("display", "screen resolution", "resolution", "scale", "scaling", "monitor"), "display"),
    (("night light",), "nightlight"),
    (("wifi", "wi-fi", "wireless"), "network-wifi"),
    (("vpn",), "network-vpn"),
    (("airplane mode", "flight mode"), "network-airplanemode"),
    (("hotspot",), "network-mobilehotspot"),
    (("network", "internet", "ethernet"), "network"),
    (("sound", "audio", "speakers", "microphone settings"), "sound"),
    (("time zone", "timezone", "date", "time", "clock"), "dateandtime"),
    (("windows update", "update", "updates"), "windowsupdate"),
    (("background", "wallpaper"), "personalization-background"),
    (("lock screen",), "lockscreen"),
    (("colors", "colours", "dark mode", "light mode", "accent color"), "colors"),
    (("themes", "theme"), "themes"),
    (("taskbar",), "taskbar"),
    (("start menu",), "personalization-start"),
    (("default apps", "default browser", "default app"), "defaultapps"),
    (("startup apps", "startup"), "startupapps"),
    (("installed apps", "apps", "uninstall", "programs"), "appsfeatures"),
    (("notifications", "notification"), "notifications"),
    (("focus", "do not disturb"), "quiethours"),
    (("battery", "battery saver", "power", "sleep"), "powersleep"),
    (("storage", "disk space"), "storagesense"),
    (("about", "pc name", "system info", "specs"), "about"),
    (("mouse", "touchpad"), "mousetouchpad"),
    (("keyboard", "typing"), "typing"),
    (("language", "region"), "regionlanguage"),
    (("printers", "printer", "scanners"), "printers"),
    (("camera", "webcam"), "privacy-webcam"),
    (("microphone", "mic"), "privacy-microphone"),
    (("privacy", "security"), "privacy"),
    (("sign-in options", "sign in options", "password", "pin", "windows hello"), "signinoptions"),
    (("accounts", "account", "your info"), "yourinfo"),
    (("email accounts",), "emailandaccounts"),
    (("accessibility", "narrator", "magnifier"), "easeofaccess"),
    (("troubleshoot",), "troubleshoot"),
    (("recovery", "reset this pc"), "recovery"),
    (("activation", "product key"), "activation"),
    (("multitasking", "snap"), "multitasking"),
    (("clipboard",), "clipboard"),
    (("gaming", "game mode"), "gaming-gamemode"),
]


def settings_page(topic: str) -> Optional[str]:
    """'time zone' -> 'dateandtime'; None for a topic not in SETTINGS_PAGES. The longest
    matching key wins ('night light' over 'light', 'windows update' over 'windows')."""
    t = f" {_norm(topic).replace('settings', ' ').strip()} "
    best: Tuple[int, Optional[str]] = (0, None)
    for keys, page in SETTINGS_PAGES:
        for k in keys:
            if f" {k} " in t and len(k) > best[0]:
                best = (len(k), page)
    return best[1]


# ------------------------------------------------------------------ calls -> executor steps

class IntentError(ValueError):
    """An accepted call that Servo can't carry out as asked (unknown settings topic, no such file)."""


DEFAULT_VOLUME_CHANGE = 10   # "louder" without a number: 10 points of the 0-100 volume
YOUTUBE_SEARCH = "https://www.youtube.com/results?search_query={q}"


def request_steps(name: str, args: Dict[str, Any], start: int = 1) -> List[Any]:
    """A REQUEST_TOOLS call -> Servo executor steps (servo/cognition/router.py runs them)."""
    from urllib.parse import quote_plus

    from servo.cognition.plan import PlanStep

    a = {k: v for k, v in (args or {}).items() if v not in (None, "")}
    why = f"Needle: {name}"

    def step(tool: str, **kw: Any) -> List[Any]:
        canon = None
        if tool in ("launch_app", "close_window"):
            from servo.actuation.alias_mapping import AliasMapper
            canon = AliasMapper.canonicalize_action(raw_action=tool, **kw)
        return [PlanStep(step_index=start, tool_name=tool, arguments=kw, rationale=why, canonical_action=canon)]

    if name == "open_app":
        return step("launch_app", app_name=a["name"])
    if name == "close_app":
        return step("close_window", target=a["name"])
    if name in ("minimize_window", "maximize_window"):
        return step("window_state", name=a["name"], state=name.split("_")[0])
    if name == "open_settings":
        page = settings_page(a["topic"])
        if not page:
            raise IntentError(f"I don't know which Settings page “{a['topic']}” is")
        return step("launch_app", app_name=f"ms-settings:{page}")
    if name == "set_brightness":
        pct = int(a["percent"])
        return step("run_command", shell="powershell",
                    command=f"(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods)"
                            f".WmiSetBrightness(1, {pct})")
    if name in ("volume_up", "volume_down"):
        change = int(a.get("percent") or DEFAULT_VOLUME_CHANGE)
        return step("audio", op="volume", change=change if name == "volume_up" else -change)
    if name in ("mute", "unmute"):
        return step("audio", op=name)
    if name in ("pause_media", "resume_media"):
        # Windows has one play/pause key and no reliable way to read what is playing,
        # so both press it; the step's report says that it toggles.
        return step("media_key", key="playpause", presses=1)
    if name == "next_track":
        return step("media_key", key="nexttrack", presses=1)
    if name == "previous_track":
        return step("media_key", key="prevtrack", presses=1)
    if name == "open_website":
        return step("open_url", url=a["address"])
    if name == "search_web":
        return step("web_search", query=a["query"])
    if name == "search_youtube":
        return step("open_url", url=YOUTUBE_SEARCH.format(q=quote_plus(a["query"])))
    if name in ("new_tab", "close_browser_tab", "restore_closed_tab"):
        keys = {"new_tab": ["ctrl", "t"], "close_browser_tab": ["ctrl", "w"],
                "restore_closed_tab": ["ctrl", "shift", "t"]}[name]
        return step("browser_hotkey", keys=keys)
    if name == "list_folder":
        return step("list_folder", path=a["path"])
    if name == "read_file":
        return step("read_file", path=a["path"])
    if name == "write_file":
        return step("write_file", path=a["path"], text=a["text"], append=False)
    if name == "open_file":
        from servo.actuation.system_tools import resolve_path
        p = resolve_path(a["path"])
        if not p.exists():
            raise IntentError(f"There is no file {p}")
        return step("open_file", path=str(p))
    raise IntentError(f"{name} is not a request tool")


# ------------------------------------------------------------------ running the agents

class Reading:
    """What Needle made of the user's words: the calls to act on, or none and why."""

    def __init__(self, calls: List[Dict[str, Any]], confidence: Optional[float], why: str,
                 raw: Optional[Dict[str, Any]] = None, ms: int = 0) -> None:
        self.calls, self.confidence, self.why, self.raw, self.ms = calls, confidence, why, raw or {}, ms

    def __bool__(self) -> bool:
        return bool(self.calls)

    @property
    def call(self) -> Dict[str, Any]:
        return self.calls[0]


def read(text: str, agent: str, tracer: Any = None) -> Reading:
    """Run the ``agent`` ("request" or "command") on the user's text. Fails openly: when
    Needle is unavailable the reading is empty with the reason, and the trace says so."""
    import time

    from servo.cognition import cactus_runtime
    from servo.config import settings

    tools, system = (REQUEST_TOOLS, REQUEST_SYSTEM) if agent == "request" else (COMMAND_TOOLS, COMMAND_SYSTEM)
    needle = cactus_runtime.get_needle(tools, key=f"intent:{agent}", system=system,
                                       weights=settings.needle_intent_weights or None)
    if needle is None:
        why = f"Cactus Needle is unavailable: {cactus_runtime.needle_error() or 'not installed'}"
        if tracer is not None:
            tracer.emit("error", f"Needle {agent} agent unavailable", why, tier="needle", status="fail")
        return Reading([], None, why)
    t0 = time.perf_counter()
    resp = cactus_runtime.needle_complete(needle, text)
    ms = int((time.perf_counter() - t0) * 1000)
    if not resp:
        return Reading([], None, "Needle returned nothing usable", ms=ms)
    calls, why = accept(text, resp, settings.needle_intent_threshold)
    conf = resp.get("confidence")
    conf = float(conf) if isinstance(conf, (int, float)) else None
    if tracer is not None:
        shown = "; ".join(f"{c.get('name')}({c.get('arguments') or {}})" for c in resp.get("function_calls") or [])
        tracer.emit("model", f"Needle {agent} · {shown or 'no call'}",
                    why or f"accepted (confidence {conf:.2f})", tier="needle",
                    status="ok" if calls else "warn", confidence=conf, duration_ms=ms, response=resp)
    return Reading(calls, conf, why, resp, ms)
