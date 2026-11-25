"""Write the planner settings Servo needs inside the WAA VM to a file the VM can read.

Servo's saved keys are encrypted with Windows DPAPI for this user, so the benchmark VM
cannot read ~/.servo/settings.json. This exports ONLY the active planner's provider, model,
endpoint and key, in plain JSON, to WAA's shared setup folder (visible in the VM as
\\\\host.lan\\Data). Delete the file when the benchmark is done:

    python -m servo.integrations.winarena.export_planner <path>
    python -m servo.integrations.winarena.export_planner <path> --delete
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict


def planner_env() -> Dict[str, str]:
    from servo import user_settings
    from servo.config import settings
    user_settings.apply(settings)
    p = settings.planner_provider
    env = {"SERVO_PLANNER_PROVIDER": p}
    if p == "openai":
        env.update(SERVO_OPENAI_API_KEY=settings.openai_api_key, SERVO_OPENAI_MODEL=settings.openai_model,
                   SERVO_OPENAI_BASE_URL=getattr(settings, "openai_base_url", "") or "")
    elif p == "anthropic":
        env.update(SERVO_ANTHROPIC_API_KEY=settings.anthropic_api_key, SERVO_ANTHROPIC_MODEL=settings.anthropic_model)
    elif p == "gemini":
        env.update(SERVO_GEMINI_API_KEY=settings.gemini_api_key, SERVO_GEMINI_MODEL=settings.gemini_model)
    return {k: v for k, v in env.items() if v}


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    path = Path(args[0])
    if "--delete" in args:
        path.unlink(missing_ok=True)
        print(f"Deleted {path}")
        return 0
    env = planner_env()
    if not any(k.endswith("_API_KEY") for k in env):
        print("No planner API key is configured; nothing written.")
        return 1
    path.write_text(json.dumps(env), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    shown = {k: (v[:3] + "…" + v[-4:] if k.endswith("_API_KEY") else v) for k, v in env.items()}
    print(f"Wrote planner settings for the VM to {path}: {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
