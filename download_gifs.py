#!/usr/bin/env python3
"""Download GIFs from Wall-E-Desk repo, validate, and pick working ones."""
import json
import urllib.request
import urllib.parse
import os
import sys
from pathlib import Path

OUT = Path("/home/Aarz/homescreen-preflight/gifs")
OUT.mkdir(parents=True, exist_ok=True)

# Fetch file list
with urllib.request.urlopen("https://api.github.com/repos/JoshuaThadi/Wall-E-Desk/contents/Pixel-Art") as r:
    files = json.loads(r.read())

# Filter: .gif under 500KB
candidates = []
for f in files:
    name = f["name"]
    size = f["size"]
    if not name.lower().endswith(".gif"):
        continue
    if size >= 500_000:
        print(f"SKIP (too big {size}b): {name}")
        continue
    candidates.append((name, f["download_url"], size))

print(f"\n=== {len(candidates)} candidate GIFs under 500KB ===\n")

# Download each, validate magic bytes
GIF_MAGIC = b"GIF87a" + b"GIF89a"
working = []
for name, url, size in candidates:
    local = OUT / name
    try:
        with urllib.request.urlopen(url) as r:
            data = r.read()
        # Check GIF magic
        if not (data.startswith(b"GIF87a") or data.startswith(b"GIF89a")):
            print(f"NOT-GIF: {name} ({len(data)}b, header={data[:6]!r})")
            continue
        local.write_bytes(data)
        working.append((name, size, len(data)))
        print(f"OK: {name} ({len(data)}b)")
    except Exception as e:
        print(f"FAIL: {name} — {e}")

print(f"\n=== {len(working)} verified GIFs ===")
for n, s, d in working:
    print(f"  {n}: {d}b")
