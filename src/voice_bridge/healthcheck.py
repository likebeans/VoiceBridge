"""Container liveness probe; uses only the standard library and bypasses proxies."""

import os
from urllib.request import ProxyHandler, build_opener


def main() -> None:
    host = os.getenv("HOST", "127.0.0.1")
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    url = f"http://{host}:{os.getenv('PORT', '8000')}/healthz"
    with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
        if response.status != 200:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
