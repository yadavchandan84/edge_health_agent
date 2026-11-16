"""Entry point: python -m healthagent -c config.toml"""

from __future__ import annotations

import argparse
import errno
import logging
import sys

from . import __version__
from .config import ConfigError, load_config

# Linux/macOS report EADDRINUSE; Windows reports WSAEADDRINUSE (10048).
PORT_IN_USE = {errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="healthagent", description="Zero Forg edge health agent")
    ap.add_argument("-c", "--config", help="TOML config file (defaults are used when omitted)")
    ap.add_argument("--db", help="override agent.db_path")
    ap.add_argument("--host", help="override api.host")
    ap.add_argument("--port", type=int, help="override api.port")
    ap.add_argument("--check-config", action="store_true", help="validate the config and exit")
    ap.add_argument("--version", action="version", version=f"healthagent {__version__}")
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.db:
        cfg.agent.db_path = args.db
    if args.host:
        cfg.api.host = args.host
    if args.port:
        cfg.api.port = args.port

    logging.basicConfig(
        level=getattr(logging, cfg.agent.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if args.check_config:
        print("config ok")
        return 0

    from .agent import Agent  # imported late so --check-config stays fast

    agent = Agent(cfg)
    try:
        agent.run_forever()
    except OSError as exc:
        if exc.errno not in PORT_IN_USE:
            raise
        agent.db.close()
        print(f"error: port {cfg.api.port} on {cfg.api.host} is already in use, so the agent cannot start.\n"
              "Another healthagent is probably running already. Stop it (Ctrl+C in its window),\n"
              "or start this one on another port with --port.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
