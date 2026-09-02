"""Entry point: ``openrouter-image-mcp`` / ``python -m openrouter_image_mcp``."""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openrouter-image-mcp",
        description="MCP server for image generation via OpenRouter.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http"),
        default="stdio",
        help="MCP transport to serve (default: stdio, which is what Claude Code uses).",
    )
    parser.add_argument("--log-level", default="WARNING", help="Python log level.")
    args = parser.parse_args(argv)

    # stdout belongs to the MCP protocol on stdio; logs must go to stderr.
    logging.basicConfig(level=args.log_level.upper(), stream=sys.stderr)

    from .server import server

    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
