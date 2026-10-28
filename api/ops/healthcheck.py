"""Container healthcheck for operations sidecar heartbeats."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ops.common import heartbeat_is_fresh


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("heartbeat_file")
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=int(os.getenv("SENTRY_OPS_HEALTHCHECK_MAX_AGE_S", "180")),
    )
    args = parser.parse_args(argv)
    if args.max_age_seconds < 30:
        raise SystemExit("--max-age-seconds must be at least 30")
    return (
        0
        if heartbeat_is_fresh(
            Path(args.heartbeat_file),
            args.max_age_seconds,
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
