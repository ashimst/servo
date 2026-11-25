"""`servo`: the copilot. `servo --mcp`: the MCP server (stdio), forwarding to the copilot when it runs."""

from __future__ import annotations

import argparse
import logging
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="servo", description="Servo: operate your Windows desktop.")
    parser.add_argument("--mcp", action="store_true", help="Run as an MCP server over stdio")
    parser.add_argument("--no-window", action="store_true", help="Serve the copilot UI without opening a window")
    parser.add_argument("--port", type=int, default=None, help="Copilot port (default: SERVO_COPILOT_PORT, 8765)")
    args = parser.parse_args()

    # stdout belongs to the MCP protocol; logs go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    if args.mcp:
        from servo.daemon.mcp_server import main as mcp_main
        mcp_main()
        return
    from servo.daemon.copilot_app import run_copilot
    run_copilot(open_window=not args.no_window, port=args.port)


if __name__ == "__main__":
    main()
