"""
Download the freely licensed test media listed in datasets/media/manifest.json
(Wikimedia Commons only) into datasets/media/files/, which git ignores.

    python tools/fetch_media.py            # skips files already present

Polite to Commons: one request at a time, a pause between files, and backoff
on HTTP 429. Each file's licence and author are in datasets/media/ATTRIBUTION.md.
Videos are reduced to a few still frames by the tests (tests/test_media.py).
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA = os.path.join(ROOT, "datasets", "media")
USER_AGENT = "tnstc-lost-found-test-media/1.0 (college project; fetches a fixed list of Commons files)"
ALLOWED_HOST = "https://upload.wikimedia.org/"
MAX_BYTES = 20 * 1024 * 1024


def fetch(url, dest):
    for attempt in range(5):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("file larger than expected")
            with open(dest + ".part", "wb") as f:
                f.write(data)
            os.replace(dest + ".part", dest)
            return len(data)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                time.sleep(5 * (attempt + 1))
                continue
            raise
    raise RuntimeError("rate limited too many times")


def main():
    items = json.load(open(os.path.join(MEDIA, "manifest.json"), encoding="utf8"))["items"]
    os.makedirs(os.path.join(MEDIA, "files"), exist_ok=True)
    ok = failed = 0
    for item in items:
        if not item["url"].startswith(ALLOWED_HOST):
            print("skip (not Commons):", item["id"])
            continue
        dest = os.path.join(MEDIA, item["file"])
        if os.path.exists(dest):
            ok += 1
            continue
        try:
            size = fetch(item["url"], dest)
            ok += 1
            print(f"{item['id']:8} {size / 1e6:5.1f} MB  {item['license']}")
        except Exception as e:                       # keep going; report at the end
            failed += 1
            print(f"{item['id']:8} FAILED: {e}")
        time.sleep(1.5)
    print(f"{ok} present, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
