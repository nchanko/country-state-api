"""Export every static endpoint of the FastAPI app to a directory of JSON files.

The live app is the source of truth: we boot it in-process and call the real
handlers, so the exported tree is byte-identical to what Railway serves today.
This exists because a large share of the state list is synthesized at runtime
from the city data (see the stub-creation path in main.py) and cannot be
reconstructed from data.json alone.

Files are written with a .html extension so Cloudflare's html_handling can serve
them at extensionless URLs; _headers restores the JSON content-type.
"""

import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

# Must be set before importing main: force the in-memory path (which carries
# lat/lon) and lift the rate limiter, since we make ~7k calls in a tight loop.
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"
os.environ["RATE_LIMIT_DEFAULT"] = "100000000"
os.environ["RATE_LIMIT_HEAVY"] = "100000000"
os.environ["RATE_LIMIT_METADATA"] = "100000000"

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

OUT = ROOT / "dist"


def write(path: str, payload) -> int:
    """Write payload as JSON at a normalized API path. Returns bytes written."""
    target = OUT / (path.strip("/") + ".html")
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    target.write_text(body, encoding="utf-8")
    return len(body.encode("utf-8"))


def main_export() -> None:
    if main.USE_REDIS:
        sys.exit("Redis unexpectedly reachable; export requires the in-memory path.")

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    client = TestClient(main.app)
    files = 0
    total = 0
    # Normalized path -> the raw state name that produced it. Two distinct states
    # collapsing onto one path means one of them becomes unreachable.
    claimed: dict = {}
    collisions = []

    def emit(request_url: str, out_path: str | None = None) -> None:
        nonlocal files, total
        r = client.get(request_url)
        if r.status_code != 200:
            sys.exit(f"{request_url} returned {r.status_code}: {r.text[:200]}")
        total += write(out_path or request_url, r.json())
        files += 1

    emit("/v1/countries")
    emit("/v1/regions")
    emit("/version")

    for region in main.regions_lookup:
        emit(f"/v1/regions/{quote(region)}/countries", f"/v1/regions/{region}/countries")

    # country_lookup is post-load, so it includes the runtime-synthesized states
    for code, country in main.country_lookup.items():
        emit(f"/v1/countries/{code}")
        emit(f"/v1/countries/{code}/states")
        for state in country.states or []:
            # Request with the exact stored name so the handler resolves it,
            # but publish at the cleaned name, which is what callers actually send.
            clean = " ".join(state.name.split())
            out = f"/v1/countries/{code}/states/{clean}/cities"

            # macOS is case-insensitive, so fold case when detecting collisions.
            key = out.lower()
            if key in claimed and claimed[key] != state.name:
                collisions.append((code, claimed[key], state.name))
                continue
            claimed[key] = state.name

            emit(f"/v1/countries/{code}/states/{quote(state.name)}/cities", out)

    print(f"files: {files}")
    print(f"total: {total / 1e6:.1f} MB")
    print(f"path collisions collapsed: {len(collisions)}")
    for c in collisions[:10]:
        print(f"    {c[0]}: {c[1]!r} <- {c[2]!r}")


if __name__ == "__main__":
    main_export()
