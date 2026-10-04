"""No-RF command line for inspecting the deterministic scout plan."""
from __future__ import annotations

import argparse
import json
import time

from ft8_scout import build_dry_run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit a pure FT8 scout dry-run plan")
    parser.add_argument("--dry-run", action="store_true", required=True)
    parser.add_argument("--now", type=float, default=None)
    args = parser.parse_args(argv)
    now = time.time() if args.now is None else args.now
    print(json.dumps(build_dry_run(now), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
