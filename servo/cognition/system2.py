"""System 2: the LLM tier.

System 2 decides WHAT to do, in plain language; it never emits tool calls. Each subtask
it writes is a short command in the Tier 1 grammar, which Tier 1 (grammar / Needle)
turns into a tool call against the *live* screen at execution time. That keeps the
LLM out of element IDs and coordinates, and lets every subtask be re-grounded.

Escalation context flows up (what was tried, why it failed, what the screen shows now)
and System 2 answers with a revised list of remaining subtasks.

Every call returns the model's visible reasoning when the provider exposes it, so the
copilot can show the full trace:
  * Anthropic: adaptive thinking with ``display: "summarized"``
  * OpenAI-compatible / Ollama: ``reasoning_content`` / ``reasoning`` fields, or
    ``<think>...</think>`` blocks in the content
  * Gemini: parts flagged ``thought: true`` (``thinkingConfig.includeThoughts``)
  * cactus-engine (local): ``thinking``, plus ``confidence`` / ``cloud_handoff``
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

from servo.cognition.needle import COMMAND_GRAMMAR
from servo.cognition.trace import NullTracer, Tracer
from servo.config import settings

logger = logging.getLogger("servo.system2")


def parse_api_keys(raw: Optional[str]) -> List[str]:
    """Split comma, semicolon, or newline delimited API keys for multi-key pool failover."""
    if not raw:
        return []
    parts = re.split(r"[,;\n\r]+", raw.strip())
    return [p.strip() for p in parts if p.strip()]


@dataclass
class ProviderCandidate:
    provider: str
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    name: str = ""



class System2Unavailable(RuntimeError):
    pass


class System2Result(BaseModel):
    thought: str = ""
    reply: str = ""
    subtasks: List[str] = Field(default_factory=list)
    done: bool = False
    reasoning: str = ""
    raw: str = ""
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    meta: Dict[str, Any] = Field(default_factory=dict)


PLAN_SYSTEM = f"""You are System 2 of Servo, a Windows desktop copilot. You decide WHAT to do; \
a fast on-device System 1 decides HOW, against the live screen.

Write each subtask as ONE short command in exactly this grammar (one action per command):
{COMMAND_GRAMMAR}

How the loop works:
- You are called repeatedly. After the subtasks you return have run, you are called
  again with what was done and a fresh description of the screen (for a web page: its
  accessibility snapshot, including link text and URLs).
- So return only the steps you can write NOW. If a later step depends on something you
  have not seen yet (search results, a page's contents, a dialog), stop the list there;
  you will see it next round.
- When text must be composed (a summary, the links you found, a reply), write the exact
  words yourself inside type "...", using what the screen description shows. Never type
  a description of the text instead of the text.
- Set "done": true only when the whole goal is achieved, judged from the screen.

Rules:
- Use labels as they appear on screen when the screen description lists them.
- Never invent coordinates, element IDs or tool names.
- Prefer the browser commands ("go to", "search the web for") for anything on the web.
- To save a file, use "save as <name>" (add "on the desktop" etc. for a folder).
- For work on files or system information (reading or writing a text file, listing a
  folder, disk space, processes, network, versions), prefer "read file", "list folder",
  "write ... to file" and "run command" over driving the GUI: they are faster and
  exact. Their output appears under "Already done" next round; read it there before
  deciding what's next. Use one short, read-only command where possible; every command
  is shown to the user for approval, and destructive ones are flagged.
- Never touch the ~/.servo folder.
- If the request is only conversation, return no subtasks and answer in "reply".
- Consult the [Windows Desktop Context] block in the prompt: it lists running windows, installed apps, and system settings. If an app is not installed locally, use its web version or explain to the user in "reply".
- If something failed, do not repeat the same command unchanged; use the screen
  description to pick a different label or approach, or stop and explain in "reply".

Respond with ONLY a JSON object:
{{"thought": "<one or two sentences>", "reply": "<short message to the user>", \
"subtasks": ["<command>", ...], "done": false}}"""

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


def build_plan_prompt(
    goal: str,
    screen: str = "",
    completed: Optional[List[str]] = None,
    failure: Optional[Dict[str, Any]] = None,
    user_name: str = "User",
    round_no: int = 1,
    memory_context: Optional[str] = None,
) -> str:
    from servo.cognition.needle import known_folder
    from servo.cognition.windows_context import windows_context

    ctx = windows_context.retrieve_context(goal)
    ctx_block = ctx.to_prompt_text()

    if memory_context is None:
        try:
            from servo.cognition.memory import chat_memory
            memory_context = chat_memory.to_prompt_context(query=goal)
        except Exception:
            memory_context = ""

    graph_block = ""
    try:
        from servo.cognition.graph_memory import graph_memory
        active_app = None
        if ctx.active_windows:
            active_app = ctx.active_windows[0].get("process")
        graph_block = graph_memory.format_context_for_prompt(
            goal=goal,
            active_app=active_app,
        )
    except Exception:
        graph_block = ""

    lines = [
        f"User name: {user_name}",
        f"Today: {time.strftime('%A %d %B %Y, %H:%M')}",
        f"Desktop folder: {known_folder('desktop')}  |  Documents: {known_folder('documents')}",
        f"Goal: {goal}",
        f"Round: {round_no}",
        ctx_block,
    ]
    if graph_block:
        lines.append(graph_block)
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
        self._engine_handle: Any = None

    # ---------------------------------------------------------------- status

    def describe(self) -> Dict[str, Any]:
        model = {
            "anthropic": settings.anthropic_model,
            "openai": settings.openai_model,
            "gemini": settings.gemini_model,
            "cactus": settings.cactus_engine_model,
        }.get(self.provider, "")
        return {"provider": self.provider, "model": model, "available": self.available()}

    def available(self) -> bool:
        """Cheap readiness check. For a local server this probes it (cached for 20 s)."""
        if self.provider == "anthropic":
            try:
                import anthropic  # noqa: F401
            except ImportError:
                return False
            return bool(settings.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
                        or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
        if self.provider == "openai":
            return _reachable(settings.openai_base_url, settings.openai_api_key)
        if self.provider == "gemini":
            return bool(settings.gemini_api_key)
        if self.provider == "cactus":
            try:
                from cactus import cactus_complete  # type: ignore  # noqa: F401
                return True
            except Exception:
                return False
        return False

    def unavailable_reason(self) -> str:
        p = self.provider
        if p in ("none", "passthrough", ""):
            return "No reasoning model chosen."
        if p == "anthropic":
            try:
                import anthropic  # noqa: F401
            except ImportError:
                return "The Anthropic SDK isn't installed (pip install anthropic)."
            return "No Anthropic API key."
        if p == "openai":
            return f"Nothing is answering at {settings.openai_base_url}. Is Ollama running?"
        if p == "gemini":
            return "No Gemini API key."
        if p == "cactus":
            return "cactus-engine isn't installed."
        return f"Unknown provider '{p}'."

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
            if p == "cactus":
                return {"ok": self.available(), "detail": "cactus-engine ready" if self.available() else self.unavailable_reason()}
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
    ) -> System2Result:
        prompt = build_plan_prompt(goal, screen, completed, failure, self.user_name, round_no)
        if failure:
            title = "System 2 · re-plan after failure"
        elif completed:
            title = f"System 2 · look at the result, decide what's next (round {round_no})"
        else:
            title = "System 2 · plan"
        with self.tracer.span("model", title, tier="system2", provider=self.provider) as sp:
            res = self._complete(PLAN_SYSTEM, prompt, json_mode=True)
            try:
                data = _parse_plan(res.raw)
            except (ValueError, json.JSONDecodeError) as e:
                sp.done("fail", f"Unparsable plan: {e}", prompt=prompt, response=res.raw, reasoning=res.reasoning)
                raise System2Unavailable(f"System 2 returned an unparsable plan: {e}") from e
            res.thought = str(data.get("thought") or "")
            res.reply = str(data.get("reply") or "")
            res.subtasks = data["subtasks"]
            res.done = bool(data.get("done"))
            sp.done(
                "ok",
                res.thought or f"{len(res.subtasks)} subtask(s)",
                confidence=res.meta.get("confidence"),
                provider=res.provider, model=res.model, latency_ms=res.latency_ms,
                system=PLAN_SYSTEM, prompt=prompt, response=res.raw, reasoning=res.reasoning,
                subtasks=res.subtasks, meta=res.meta,
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

    def _complete(self, system: str, user: str, json_mode: bool, max_tokens: int = 16000) -> System2Result:
        t0 = time.perf_counter()
        if self.provider in ("none", "passthrough"):
            raise System2Unavailable(
                f"No System 2 provider configured (SERVO_PLANNER_PROVIDER={self.provider!r}). "
                "Set it to anthropic, openai (Ollama/any OpenAI-compatible server), gemini or cactus."
            )

        candidates: List[ProviderCandidate] = []

        # 1. Primary provider candidates (all keys configured for primary provider)
        if self.provider == "anthropic":
            keys = parse_api_keys(settings.anthropic_api_key) or [""]
            for i, k in enumerate(keys):
                name = f"anthropic[key_{i+1}]" if len(keys) > 1 else "anthropic"
                candidates.append(ProviderCandidate(
                    provider="anthropic", api_key=k, model=settings.anthropic_model, name=name
                ))
        elif self.provider == "gemini":
            keys = parse_api_keys(settings.gemini_api_key) or [""]
            for i, k in enumerate(keys):
                name = f"gemini[key_{i+1}]" if len(keys) > 1 else "gemini"
                candidates.append(ProviderCandidate(
                    provider="gemini", api_key=k, model=settings.gemini_model, name=name
                ))
        elif self.provider == "openai":
            keys = parse_api_keys(settings.openai_api_key) or [""]
            for i, k in enumerate(keys):
                name = f"openai[key_{i+1}:{settings.openai_model}]" if len(keys) > 1 else f"openai[{settings.openai_model}]"
                candidates.append(ProviderCandidate(
                    provider="openai", api_key=k, model=settings.openai_model, base_url=settings.openai_base_url, name=name
                ))
        elif self.provider == "cactus":
            candidates.append(ProviderCandidate(
                provider="cactus", model=settings.cactus_engine_model, name="cactus"
            ))

        # 2. Backup cloud providers if alternative keys are configured
        if self.provider != "anthropic" and settings.anthropic_api_key:
            for i, k in enumerate(parse_api_keys(settings.anthropic_api_key)):
                candidates.append(ProviderCandidate(
                    provider="anthropic", api_key=k, model=settings.anthropic_model, name=f"anthropic_backup[key_{i+1}]"
                ))
        if self.provider != "gemini" and settings.gemini_api_key:
            for i, k in enumerate(parse_api_keys(settings.gemini_api_key)):
                candidates.append(ProviderCandidate(
                    provider="gemini", api_key=k, model=settings.gemini_model, name=f"gemini_backup[key_{i+1}]"
                ))
        if self.provider != "openai" and settings.openai_api_key:
            for i, k in enumerate(parse_api_keys(settings.openai_api_key)):
                candidates.append(ProviderCandidate(
                    provider="openai", api_key=k, model=settings.openai_model, base_url=settings.openai_base_url, name=f"openai_backup[key_{i+1}]"
                ))

        # 3. Default local small model on Ollama (tailored for 4GB VRAM devices)
        local_model = getattr(settings, "default_local_model", "llama3.2:1b")
        already_has_local = any(
            c.provider == "openai" and c.model == local_model and "11434" in c.base_url
            for c in candidates
        )
        if not already_has_local:
            candidates.append(ProviderCandidate(
                provider="openai",
                api_key="",
                model=local_model,
                base_url="http://localhost:11434/v1",
                name=f"local_default[{local_model}]",
            ))

        if not candidates:
            raise System2Unavailable(
                f"No System 2 provider configured (SERVO_PLANNER_PROVIDER={self.provider!r}). "
                "Set it to anthropic, openai (Ollama/any OpenAI-compatible server), gemini or cactus."
            )

        errors: List[str] = []
        for idx, cand in enumerate(candidates):
            try:
                if cand.provider == "anthropic":
                    res = self._anthropic(system, user, max_tokens, api_key=cand.api_key, model=cand.model)
                elif cand.provider == "openai":
                    res = self._openai(system, user, json_mode, api_key=cand.api_key, model=cand.model, base_url=cand.base_url)
                elif cand.provider == "gemini":
                    res = self._gemini(system, user, json_mode, api_key=cand.api_key, model=cand.model)
                elif cand.provider == "cactus":
                    res = self._cactus(system, user)
                else:
                    continue
                res.latency_ms = int((time.perf_counter() - t0) * 1000)
                return res
            except Exception as e:
                err_str = f"{cand.name}: {e}"
                errors.append(err_str)
                logger.warning("System 2 candidate %s failed: %s", cand.name, e)
                if idx < len(candidates) - 1:
                    next_cand = candidates[idx + 1]
                    self.tracer.emit(
                        "model",
                        f"Failover: {cand.name} failed ({e}); switching to {next_cand.name}...",
                        tier="system2",
                        candidate_failed=cand.name,
                        candidate_next=next_cand.name,
                    )

        raise System2Unavailable(
            "All System 2 providers/keys in failover pool failed:\n" + "\n".join(f"- {err}" for err in errors)
        )

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
            with client.beta.messages.stream(
                model=mdl,
                max_tokens=max_tokens,
                system=system,
                thinking={"type": "adaptive", "display": "summarized"},
                output_config={"effort": settings.system2_effort},
                # Server-side refusal fallback re-runs a declined request on a fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
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

    def _openai(self, system: str, user: str, json_mode: bool, api_key: str = "", model: str = "", base_url: str = "") -> System2Result:
        key = api_key if api_key != "" else settings.openai_api_key
        mdl = model or settings.openai_model or getattr(settings, "default_local_model", "llama3.2:1b")
        b_url = base_url or settings.openai_base_url or "http://localhost:11434/v1"
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
            with httpx.Client(timeout=90.0) as client:
                r = client.post(url, headers=headers, json=payload)
                if r.status_code == 400 and json_mode:  # server without JSON mode
                    payload.pop("response_format", None)
                    r = client.post(url, headers=headers, json=payload)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
        except httpx.HTTPError as e:
            raise System2Unavailable(f"OpenAI-compatible endpoint {b_url} failed: {e}") from e
        except (KeyError, IndexError, ValueError) as e:
            raise System2Unavailable(f"Unexpected response from {b_url}: {e}") from e
        content, think = _split_think(msg.get("content") or "")
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or think
        return System2Result(raw=content, reasoning=str(reasoning or ""), provider="openai", model=mdl)

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

    def _cactus(self, system: str, user: str) -> System2Result:
        try:
            from cactus import cactus_complete, cactus_init, get_bundle_dir  # type: ignore
        except Exception as e:
            raise System2Unavailable(
                "cactus-engine is not installed (pip install cactus-engine). Note: it cannot be "
                "installed alongside cactus-compute (Needle/Mirage); both import as 'cactus'."
            ) from e
        if self._engine_handle is None:
            self._engine_handle = cactus_init(str(get_bundle_dir(settings.cactus_engine_model)), None, False)
        result = cactus_complete(
            self._engine_handle,
            json.dumps([{"role": "system", "content": system}, {"role": "user", "content": user}]),
            json.dumps({"max_tokens": 1024, "temperature": 0.2, "enable_thinking_if_supported": True}),
            None,
            None,
        )
        if not result.get("success", True):
            raise System2Unavailable(f"cactus-engine error: {result.get('error')}")
        content, think = _split_think(result.get("response") or "")
        return System2Result(
            raw=content,
            reasoning=str(result.get("thinking") or think or ""),
            provider="cactus",
            model=settings.cactus_engine_model,
            meta={
                "confidence": result.get("confidence"),
                "cloud_handoff": result.get("cloud_handoff"),
                "decode_tps": result.get("decode_tps"),
            },
        )
