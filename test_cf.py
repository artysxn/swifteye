r"""Check whether Camoufox passes the Cloudflare challenge at nopecha.com/demo.

    .venv\Scripts\python.exe test_cf.py
"""

import re
import time

from camoufox.sync_api import Camoufox

URL = "https://nopecha.com/demo"


def main() -> None:
    with Camoufox(headless=True) as browser:
        page = browser.new_page()
        page.goto(URL, timeout=90_000, wait_until="domcontentloaded")

        # Cloudflare interstitials take a few seconds; give the challenge time
        # to run and resolve.
        for attempt in range(12):
            title = page.title()
            body = page.evaluate("document.body ? document.body.innerText : ''")
            print(f"[{attempt * 5:3d}s] title={title!r}")
            if "just a moment" not in title.lower() and "attention required" not in title.lower():
                break
            time.sleep(5)

        text = re.sub(r"\s+", " ", body)
        print("\n--- page text (first 1500 chars) ---")
        print(text[:1500])
        page.screenshot(path="cf_demo.png", full_page=True)
        print("\nScreenshot saved to cf_demo.png")


if __name__ == "__main__":
    main()
