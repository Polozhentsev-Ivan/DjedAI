"""Local operator entry point, separate from the backend's eager imports."""

import argparse
import json
from pathlib import Path

from djedai.backend.config import get_settings
from djedai.backend.diagnostics import read_events


def main() -> None:
    parser = argparse.ArgumentParser(description="Read local operator-only agent diagnostics")
    parser.add_argument("--run", help="Filter by run_id")
    args = parser.parse_args()
    for event in read_events(Path(get_settings().diagnostics_dir), args.run):
        print(json.dumps(event, ensure_ascii=False))


if __name__ == "__main__":
    main()
