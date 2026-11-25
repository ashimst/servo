"""System 2: runs a planner agent's prompt (servo/cognition/planners.py) on a model.

The planner decides WHAT to do; it never emits tool calls. Each step it writes is one of
the step forms, which a Needle agent turns into a tool call against the live screen.
That keeps the model out of element IDs and coordinates and lets every step be grounded
on the screen as it is when the step runs. Tiers: the local planner (Qwen3.5-4B on
llama-server) and the cloud planner (settings.planner_provider), per settings.planner_mode.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from pydantic import BaseModel, Field

from servo.cognition.trace import NullTracer, Tracer
from servo.config import settings

logger = logging.getLogger("servo.system2")


class System2Unavailable(RuntimeError):
    pass


class System2Result(BaseModel):
    thought: str = ""
    reply: str = ""
    subtasks: List[str] = Field(default_factory=list)
    done: bool = False
    next_agent: str = ""  # a planner agent asks to hand the task to another (planners.py)
    reasoning: str = ""
    raw: str = ""
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    meta: Dict[str, Any] = Field(default_factory=dict)


# Planner prompts are per agent (servo/cognition/planners.py).
from servo.cognition.planners import DEFAULT_AGENT, plan_system  # noqa: E402

CHAT_SYSTEM = (
    "You are Servo, a friendly on-screen Windows copilot. The user's name is {name}. "
    "Answer concisely (1-3 sentences). If they ask you to do something on the computer, "
    "say what you will do."
)

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.S | re.I)
_reach_cache: Dict[str, tuple] = {}


def list_openai_models(base_url: str, api_key: str = "", timeout: float = 1.5) -> Optional[List[str]]:
    """GET {base}/models. Returns model ids, or None if nothing answers."""
    if not base_url:
        return None
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        with httpx.Client(timeout=timeout) as c:
            r = c.get(f"{base_url.rstrip('/')}/models", headers=headers)
        if r.status_code >= 400:
            return None
        return sorted(str(m.get("id")) for m in (r.json().get("data") or []) if m.get("id"))
    except Exception:
        return None


def _reachable(base_url: str, api_key: str = "") -> bool:
    hit = _reach_cache.get(base_url)
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    ok = list_openai_models(base_url, api_key) is not None
    _reach_cache[base_url] = (time.time(), ok)
    return ok


def _split_think(text: str) -> tuple[str, str]:
    thoughts = "\n".join(m.strip() for m in _THINK_RE.findall(text or ""))
    return _THINK_RE.sub("", text or "").strip(), thoughts


def _parse_plan(text: str) -> Dict[str, Any]:
    body = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", (text or "").strip())
    m = re.search(r"\{.*\}", body, re.S)
    if not m:
        raise ValueError("System 2 did not return a JSON object")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("System 2 JSON is not an object")
    subs = data.get("subtasks") or []
    if not isinstance(subs, list):
        subs = []
    data["subtasks"] = [str(s).strip() for s in subs if str(s).strip()][:12]
    return data


def knowledge_context(goal: str, failure: Optional[Dict[str, Any]] = None, active_app: str = "",
                      conversation: str = "") -> Tuple[str, Dict[str, Any]]:
    """Recipes + knowledge-graph context for the planner, and what was chosen (for the trace)."""
    blocks: List[str] = []
    meta: Dict[str, Any] = {"recipes": [], "graph": False, "missing": []}  # "missing" shows in the trace
    failure_text = json.dumps(failure, ensure_ascii=False) if failure else ""
    try:
        from servo.cognition.recipes import recipe_book
        block, ids = recipe_book.prompt_block(" ".join([goal, failure_text, conversation[-600:]]), active_app)
        if block:
            blocks.append(block)
            meta["recipes"] = ids
    except Exception as e:  # noqa: BLE001 - context, not a decision: plan without it, and say so
        meta["missing"].append(f"recipes: {e}")
        logger.warning("recipes unavailable: %s", e)
    try:
        from servo.cognition.graph_memory import graph_memory
        g = graph_memory.format_context_for_prompt(goal=goal, active_app=active_app or None, failure=failure)
        if g:
            blocks.append(g)
            meta["graph"] = True
    except Exception as e:  # noqa: BLE001
        meta["missing"].append(f"knowledge graph: {e}")
        logger.warning("graph context unavailable: %s", e)
    try:
        from servo.cognition.desktop_graph import desktop_graph
        d = desktop_graph().context_for(" ".join([goal, failure_text[:300]]))
        if d:
            blocks.append(d)
            meta["desktop_graph"] = True
    except Exception as e:  # noqa: BLE001
        meta["missing"].append(f"desktop graph: {e}")
        logger.warning("desktop graph context unavailable: %s", e)
    return "\n\n".join(blocks), meta


def build_plan_prompt(
    goal: str,
    screen: str = "",
    completed: Optional[List[str]] = None,
    failure: Optional[Dict[str, Any]] = None,
    user_name: str = "User",
    round_no: int = 1,
    memory_context: Optional[str] = None,
    meta_out: Optional[Dict[str, Any]] = None,
) -> str:
    from servo.cognition.needle import known_folder
    from servo.cognition.windows_context import windows_context

    ctx = windows_context.retrieve_context(goal)
    ctx_block = ctx.to_prompt_text()

    memory_error = ""
    if memory_context is None:
        try:
            from servo.cognition.memory import chat_memory
            memory_context = chat_memory.to_prompt_context(query=goal)
        except Exception as e:  # noqa: BLE001
            memory_context, memory_error = "", f"chat memory: {e}"
            logger.warning("chat memory unavailable: %s", e)

    focused = next((w for w in ctx.active_windows if w.get("is_focused")), None) or \
        (ctx.active_windows[0] if ctx.active_windows else {})
    active_app = str(focused.get("process") or "")
    knowledge, meta = knowledge_context(goal, failure, active_app, memory_context or "")
    meta["active_app"] = active_app
    if memory_error:
        meta["missing"].append(memory_error)
    if meta_out is not None:
        meta_out.update(meta)

    lines = [
        f"User name: {user_name}",
        f"Today: {time.strftime('%A %d %B %Y, %H:%M')}",
        f"Desktop folder: {known_folder('desktop')}  |  Documents: {known_folder('documents')}",
        f"Preferred browser: {settings.preferred_browser or '(Windows default)'}",
        f"Goal: {goal}",
        f"Round: {round_no}",
        ctx_block,
    ]
    if knowledge:
        lines.append(knowledge)
    if memory_context:
        lines.append(memory_context)
    if completed:
        lines.append("Already done (in order):\n" + "\n".join(f"- {c}" for c in completed))
        if not failure:
            lines.append("Look at the screen below. If the goal is achieved, set done=true. "
                         "Otherwise return the next subtasks.")
    if failure:
        lines.append("The last attempt FAILED:\n" + json.dumps(failure, indent=1, ensure_ascii=False)[:4000])
        lines.append("Return ONLY the remaining subtasks (do not repeat completed ones).")
    if screen:
        lines.append("What is on screen now:\n" + screen[:12000])
    return "\n\n".join(lines)


class System2:
    def __init__(self, provider: Optional[str] = None, user_name: str = "User", tracer: Optional[Tracer] = None) -> None:
        self.provider = (provider or settings.planner_provider or "none").lower()
        if self.provider in ("ollama", "openai_compat"):
            self.provider = "openai"
        if self.provider in ("claude",):
            self.provider = "anthropic"
        self.user_name = user_name
        self.tracer = tracer or NullTracer()
        self.tier = "local"
        self.step_ups: List[str] = []
        self._unparsable = 0
        self.reset_tier()

    # ---------------------------------------------------------------- status

    def describe(self) -> Dict[str, Any]:
        cloud_model = {"anthropic": settings.anthropic_model, "openai": settings.openai_model,
                       "gemini": settings.gemini_model}.get(self.provider, "")
        local = {"url": settings.local_planner_url, "model": settings.local_planner_model,
                 "available": _reachable(settings.local_planner_url)}
        cloud = {"provider": self.provider, "model": cloud_model, "available": self._cloud_configured()}
        on_tier = local if self.tier == "local" else cloud
        return {"mode": settings.planner_mode, "tier": self.tier, "local": local, "cloud": cloud,
                "provider": "local" if self.tier == "local" else self.provider,
                "model": on_tier["model"], "available": self.available()}

    def _provider_available(self, provider: str) -> bool:
        if provider == "anthropic":
            try:
                import anthropic  # noqa: F401
            except ImportError:
                return False
            return bool(settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
                        or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        if provider == "openai":
            return bool(settings.openai_base_url) and _reachable(settings.openai_base_url, settings.openai_api_key)
        if provider == "gemini":
            return bool(settings.gemini_api_key)
        return False

    def available(self) -> bool:
        """Is the planner for the current tier ready (local first in local_first mode, with the
        cloud as its step-up)? For a local server this probes it (cached for 20 s)."""
        mode = settings.planner_mode
        local_ok = _reachable(settings.local_planner_url)
        if mode == "local_only":
            return local_ok
        if mode == "cloud_only" or self.tier == "cloud":
            return self._cloud_configured()
        return local_ok or self._cloud_configured()

    def unavailable_reason(self) -> str:
        local = f"nothing is answering at {settings.local_planner_url} (start llama-server with the Qwen model)"
        p = self.provider
        if p in ("none", ""):
            cloud = "no cloud planner chosen"
        elif p == "anthropic":
            cloud = "no Anthropic API key or SDK"
        elif p == "openai":
            cloud = f"nothing is answering at {settings.openai_base_url or '(no endpoint set)'}"
        elif p == "gemini":
            cloud = "no Gemini API key"
        else:
            cloud = f"provider '{p}' is not ready"
        mode = settings.planner_mode
        if mode == "local_only":
            return f"Local planner: {local}."
        if mode == "cloud_only":
            return f"Cloud planner: {cloud}."
        return f"Local planner: {local}; cloud planner: {cloud}."

    def probe(self) -> Dict[str, Any]:
        """Real connection test (network call). Returns {ok, detail, models}."""
        p = self.provider
        try:
            if p == "anthropic":
                if not self.available():
                    return {"ok": False, "detail": self.unavailable_reason()}
                import anthropic
                client = anthropic.Anthropic(api_key=settings.anthropic_api_key or None, timeout=15.0, max_retries=0)
                try:
                    m = client.models.retrieve(settings.anthropic_model or "claude-opus-5-5")
                except anthropic.AuthenticationError:
                    return {"ok": False, "detail": "Anthropic rejected the API key."}
                except anthropic.NotFoundError:
                    return {"ok": False, "detail": f"Model '{settings.anthropic_model}' isn't available to this key."}
                except anthropic.APIConnectionError as e:
                    return {"ok": False, "detail": f"Couldn't reach Anthropic: {e}"}
                return {"ok": True, "detail": f"Connected · {getattr(m, 'display_name', None) or m.id}"}
            if p == "openai":
                models = list_openai_models(settings.openai_base_url, settings.openai_api_key)
                if models is None:
                    return {"ok": False, "detail": self.unavailable_reason()}
                if settings.openai_model and models and settings.openai_model not in models:
                    return {"ok": False, "models": models,
                            "detail": f"Connected, but '{settings.openai_model}' isn't installed there."}
                return {"ok": True, "models": models, "detail": f"Connected · {settings.openai_model}"}
            if p == "gemini":
                if not settings.gemini_api_key:
                    return {"ok": False, "detail": "No Gemini API key."}
                with httpx.Client(timeout=10.0) as c:
                    r = c.get(f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_model}",
                              headers={"x-goog-api-key": settings.gemini_api_key})
                if r.status_code in (400, 401, 403):
                    return {"ok": False, "detail": "Google rejected the API key."}
                if r.status_code == 404:
                    return {"ok": False, "detail": f"Model '{settings.gemini_model}' not found."}
                r.raise_for_status()
                return {"ok": True, "detail": f"Connected · {settings.gemini_model}"}
            return {"ok": False, "detail": self.unavailable_reason()}
        except Exception as e:
            return {"ok": False, "detail": f"{e.__class__.__name__}: {e}"}

    # -------------------------------------------------------------- public API

    def plan(
        self,
        goal: str,
        screen: str = "",
        completed: Optional[List[str]] = None,
        failure: Optional[Dict[str, Any]] = None,
        round_no: int = 1,
        agent: str = DEFAULT_AGENT,
    ) -> System2Result:
        knowledge_meta: Dict[str, Any] = {}
        prompt = build_plan_prompt(goal, screen, completed, failure, self.user_name, round_no,
                                   meta_out=knowledge_meta)
        system = plan_system(agent)
        if failure:
            title = f"System 2 ({agent}) · re-plan after failure"
        elif completed:
            title = f"System 2 ({agent}) · look at the result, decide what's next (round {round_no})"
        else:
            title = f"System 2 ({agent}) · plan"
        with self.tracer.span("model", title, tier="system2", provider=self.provider, agent=agent) as sp:
            res = self._complete(system, prompt, json_mode=True)
            try:
                data = _parse_plan(res.raw)
            except (ValueError, json.JSONDecodeError) as e:
                # One retry on the same tier, told what was wrong, before it counts as a failure.
                self.tracer.emit("model", "Planner reply was not valid JSON · asking again", str(e),
                                 tier="system2", status="warn", response=res.raw[:2000])
                res = self._complete(system, prompt + "\n\nYour previous reply was not a valid JSON object ("
                                     f"{e}). Reply with ONLY the JSON object.", json_mode=True)
                try:
                    data = _parse_plan(res.raw)
                except (ValueError, json.JSONDecodeError) as e2:
                    sp.done("fail", f"Unparsable plan: {e2}", prompt=prompt, response=res.raw, reasoning=res.reasoning)
                    self._unparsable += 1
                    if self.tier == "local" and self.step_up(f"The local planner returned unusable output twice ({e2})"):
                        self._unparsable = 0
                        return self.plan(goal, screen, completed, failure, round_no, agent)
                    raise System2Unavailable(f"System 2 returned an unparsable plan: {e2}") from e2
            self._unparsable = 0
            res.thought = str(data.get("thought") or "")
            res.reply = str(data.get("reply") or "")
            res.subtasks = data["subtasks"]
            res.done = bool(data.get("done"))
            res.next_agent = str(data.get("next_agent") or "").strip().lower()
            sp.done(
                "ok",
                res.thought or f"{len(res.subtasks)} subtask(s)",
                confidence=res.meta.get("confidence"),
                provider=res.provider, model=res.model, latency_ms=res.latency_ms,
                system=system, prompt=prompt, response=res.raw, reasoning=res.reasoning,
                subtasks=res.subtasks, meta=res.meta, knowledge=knowledge_meta, agent=agent,
            )
            if res.reasoning:
                self.tracer.emit("model", "Model reasoning", res.reasoning, tier="system2", reasoning=True)
            return res

    def chat(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> System2Result:
        try:
            from servo.cognition.memory import chat_memory
            mem_text = chat_memory.to_prompt_context(query=message)
        except Exception:
            convo = "\n".join(f"{m['role']}: {m['text']}" for m in (history or [])[-6:])
            mem_text = (convo + "\n" if convo else "")
        prompt = (mem_text + "\n" if mem_text else "") + f"user: {message}"
        with self.tracer.span("model", "System 2 · chat", tier="system2", provider=self.provider) as sp:
            res = self._complete(CHAT_SYSTEM.format(name=self.user_name), prompt, json_mode=False, max_tokens=600)
            res.reply = res.raw.strip()
            sp.done("ok", "", provider=res.provider, model=res.model, latency_ms=res.latency_ms,
                    prompt=prompt, response=res.raw, reasoning=res.reasoning)
            return res

    # -------------------------------------------------------------- providers

    # -------------------------------------------------------------- tiers

    def _cloud_configured(self) -> bool:
        return self.provider not in ("none", "") and self._provider_available(self.provider)

    def step_up(self, reason: str) -> bool:
        """Local planner failed: use the cloud planner for the rest of this task.

        Only in local_first mode and only when a cloud planner is configured. Returns
        whether the tier changed; the step-up is always traced."""
        if self.tier == "cloud" or settings.planner_mode != "local_first" or not self._cloud_configured():
            return False
        self.tier = "cloud"
        self.step_ups.append(reason)
        self.tracer.emit("escalate", "Step up · local planner → cloud planner", reason, tier="system2",
                         status="warn", provider=self.provider)
        return True

    def reset_tier(self) -> None:
        """A new task starts on the configured first tier again."""
        self.tier = "cloud" if settings.planner_mode in ("cloud_first", "cloud_only") else "local"
        self.step_ups = []
        self._unparsable = 0

    def _complete(self, system: str, user: str, json_mode: bool, max_tokens: int = 16000) -> System2Result:
        t0 = time.perf_counter()
        try:
            res = self._complete_on_tier(self.tier, system, user, json_mode, max_tokens)
        except System2Unavailable as e:
            if self.tier == "local" and self.step_up(f"Local planner failed: {e}"):
                res = self._complete_on_tier("cloud", system, user, json_mode, max_tokens)
            else:
                raise
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        res.meta["tier"] = self.tier
        return res

    def _complete_on_tier(self, tier: str, system: str, user: str, json_mode: bool, max_tokens: int) -> System2Result:
        if tier == "local":
            try:
                return self._openai(system, user, json_mode, api_key="", model=settings.local_planner_model,
                                    base_url=settings.local_planner_url, timeout=settings.local_planner_timeout_seconds,
                                    provider_name="local")
            except System2Unavailable:
                raise
            except Exception as e:  # noqa: BLE001
                raise System2Unavailable(f"Local planner at {settings.local_planner_url} failed: {e}") from e
        p = self.provider
        if p in ("none", ""):
            raise System2Unavailable("No cloud planner configured (SERVO_PLANNER_PROVIDER).")
        if p == "anthropic":
            return self._anthropic(system, user, max_tokens)
        if p == "openai":
            if not settings.openai_base_url:
                raise System2Unavailable("The cloud planner has no endpoint (SERVO_OPENAI_BASE_URL).")
            return self._openai(system, user, json_mode)
        if p == "gemini":
            return self._gemini(system, user, json_mode)
        raise System2Unavailable(f"Unknown planner provider '{p}'.")

    def _anthropic(self, system: str, user: str, max_tokens: int, api_key: str = "", model: str = "") -> System2Result:
        try:
            import anthropic
        except ImportError as e:
            raise System2Unavailable("The 'anthropic' package is not installed: pip install servo[anthropic]") from e
        key = api_key or settings.anthropic_api_key or None
        client = anthropic.Anthropic(api_key=key, timeout=120.0)
        mdl = model or settings.anthropic_model or "claude-opus-5-5"
        try:
            # Streaming keeps long thinking turns clear of HTTP timeouts.
            with client.messages.stream(
                model=mdl,
                max_tokens=max_tokens,
                system=system,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": settings.system2_effort},
                messages=[{"role": "user", "content": user}],
            ) as stream:
                msg = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise System2Unavailable("Anthropic authentication failed; set ANTHROPIC_API_KEY or run `ant auth login`.") from e
        except anthropic.RateLimitError as e:
            raise System2Unavailable("Anthropic rate limit reached; try again shortly.") from e
        except anthropic.APIStatusError as e:
            raise System2Unavailable(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise System2Unavailable(f"Could not reach the Anthropic API: {e}") from e
        if msg.stop_reason == "refusal":
            raise System2Unavailable("The model declined this request.")
        text = "".join(b.text for b in msg.content if b.type == "text")
        thinking = "\n\n".join(getattr(b, "thinking", "") for b in msg.content if b.type == "thinking").strip()
        return System2Result(
            raw=text, reasoning=thinking, provider="anthropic", model=msg.model,
            meta={"stop_reason": msg.stop_reason, "usage": msg.usage.model_dump() if msg.usage else {}},
        )

    def _openai(self, system: str, user: str, json_mode: bool, api_key: Optional[str] = None, model: str = "",
                base_url: str = "", timeout: float = 90.0, provider_name: str = "openai") -> System2Result:
        key = settings.openai_api_key if api_key is None else api_key
        mdl = model or settings.openai_model
        b_url = base_url or settings.openai_base_url
        if not b_url:
            raise System2Unavailable("No endpoint configured for this planner.")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload: Dict[str, Any] = {
            "model": mdl,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.1,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        url = f"{b_url.rstrip('/')}/chat/completions"
        try:
            with httpx.Client(timeout=timeout) as client:
                r = client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
        except httpx.HTTPError as e:
            raise System2Unavailable(f"OpenAI-compatible endpoint {b_url} failed: {e}") from e
        except (KeyError, IndexError, ValueError) as e:
            raise System2Unavailable(f"Unexpected response from {b_url}: {e}") from e
        content, think = _split_think(msg.get("content") or "")
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or think
        return System2Result(raw=content, reasoning=str(reasoning or ""), provider=provider_name, model=mdl)

    def _gemini(self, system: str, user: str, json_mode: bool, api_key: str = "", model: str = "") -> System2Result:
        key = api_key or settings.gemini_api_key
        if not key:
            raise System2Unavailable("SERVO_GEMINI_API_KEY is not set.")
        mdl = model or settings.gemini_model or "gemini-2.0-flash"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent"
        gen: Dict[str, Any] = {"temperature": 0.2, "thinkingConfig": {"includeThoughts": True}}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen,
        }
        try:
            with httpx.Client(timeout=90.0) as client:
                r = client.post(url, headers={"x-goog-api-key": key}, json=payload)
            r.raise_for_status()
            parts = r.json()["candidates"][0]["content"]["parts"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as e:
            raise System2Unavailable(f"Gemini request failed: {e}") from e
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        thoughts = "\n".join(p.get("text", "") for p in parts if p.get("thought"))
        return System2Result(raw=text, reasoning=thoughts, provider="gemini", model=mdl)
