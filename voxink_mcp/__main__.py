from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .server import serve
from voxink_cli.envelope import Failure


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve VoxInk to an MCP client over stdio.")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="grant token file to act under (default: $VOXINK_GRANT_TOKEN_FILE)",
    )
    parser.add_argument(
        "--owner",
        action="store_true",
        help="act with the owner credential instead of a grant (not for agents)",
    )
    parser.add_argument("--client-name", default="mcp-agent", help="name shown to the owner in requests")
    args = parser.parse_args()
    try:
        return serve(
            sys.stdin, sys.stdout, token_file=args.token_file, owner=args.owner, client_name=args.client_name
        )
    except Failure as error:
        print(f"{error.code}: {error.message}", file=sys.stderr)
        return error.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
