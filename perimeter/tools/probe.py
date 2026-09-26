"""Container health probe without curl: ``python -m perimeter.tools.probe URL``."""

from __future__ import annotations

import sys
import urllib.request


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/readyz"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - fixed local URL
            return 0 if response.status == 200 else 1
    except OSError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
