r"""Update proxies.txt from the iplocate free-proxy-list repo.

The upstream list is refreshed every 30 minutes, so run this before a
scraping session:

    .venv\Scripts\python.exe update_proxies.py

Output: proxies.txt in this folder, one `ip:port` per line (HTTPS-capable).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

SOURCE_URL = "https://raw.githubusercontent.com/iplocate/free-proxy-list/main/protocols/https.txt"
OUT_FILE = Path(__file__).parent / "proxies.txt"


def main() -> int:
    resp = requests.get(SOURCE_URL, timeout=30)
    resp.raise_for_status()

    proxies = []
    seen = set()
    for line in resp.text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line not in seen:
            seen.add(line)
            proxies.append(line)

    if not proxies:
        print("No proxies in the upstream list; not touching the old file.")
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    OUT_FILE.write_text(f"# {len(proxies)} proxies, updated {stamp}\n" + "\n".join(proxies) + "\n",
                        encoding="utf-8")
    print(f"Wrote {len(proxies)} proxies to {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
