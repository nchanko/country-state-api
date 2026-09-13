"""Build the search indexes.

Two different strategies, chosen by size:

  countries (249) and states (6,966) are small enough to embed in the Worker
  bundle and scan per request, so they keep the current substring semantics
  byte-for-byte.

  cities (154,190) cannot be scanned inside the free tier's 10ms CPU budget, so
  they are sharded by word-prefix into static assets the Worker fetches on
  demand.

Sharding is adaptive: 2 chars by default, splitting to 3 only for keys that are
actually crowded. A uniform 3-char split produced 11,499 files against
Cloudflare's 20,000-file limit while the median shard held 3 cities.

Shard contract, which the Worker depends on:

  * A shard holds compact rows in ORIGINAL world_cities.json order, because the
    live API returns the first 50 matches in file order and we must match that.
  * A shard is COMPLETE (holds every city matching its key) unless its key is in
    the split manifest. A query longer than its key filters inside the shard, so
    nothing may be missing from it.
  * A shard IS capped at LIMIT rows when its key was split, or is 1 char long.
    That is safe only because a capped shard is read exclusively by a query equal
    to its own key, which never needs more than LIMIT results. Longer queries on
    a split key are routed to a complete 3-char child instead.

Filenames are hex-encoded UTF-8 of the key. Raw keys would be unsafe: macOS
normalizes and case-folds unicode filenames, so 'ą' and 'a' + combining ogonek
would silently collide at build time while staying distinct on Cloudflare.
"""

import json
import os
import shutil
import sys
from pathlib import Path

os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import main  # noqa: E402

DEPTH = 3   # deepest key we shard at; 3-char shards are always complete
LIMIT = 50  # /v1/search/cities caps results at 50
SPLIT_OVER = 1200  # a 2-char shard bigger than this is split into 3-char children

IDX = ROOT / "dist" / "_idx" / "cities"
SRC_DATA = ROOT / "src" / "data"


def keys_for(name: str, name_local: str) -> set:
    """Every shard key under which this city must be findable.

    Mirrors the Worker's match rule: a city matches query q if its full name
    starts with q, or any word of it starts with q. Both imply the city's row
    lives in the shard keyed q[:DEPTH], for every prefix length we shard at.
    """
    out = set()
    for text in (name, name_local):
        if not text:
            continue
        t = " ".join(text.split()).lower()
        if not t:
            continue
        for token in [t] + t.split(" "):
            if not token:
                continue
            for d in range(1, DEPTH + 1):
                if len(token) >= d:
                    out.add(token[:d])
    return out


def main_build() -> None:
    if main.USE_REDIS:
        sys.exit("Redis unexpectedly reachable; build requires the in-memory index.")

    if IDX.exists():
        shutil.rmtree(IDX)
    IDX.mkdir(parents=True)
    SRC_DATA.mkdir(parents=True, exist_ok=True)

    # Bucket rows by key, preserving original order (city_search_index is built
    # in world_cities.json order, and enumerate gives us that ordinal).
    buckets: dict = {}
    for i, city in enumerate(main.city_search_index):
        row = [city["n"], city["nl"], city["s"], city["c"], city.get("lat"), city.get("lon")]
        for k in keys_for(city["n"], city["nl"]):
            buckets.setdefault(k, []).append(row)

    # Only crowded 2-char keys earn 3-char children; the rest stay complete at 2.
    split = {k for k, rows in buckets.items() if len(k) == 2 and len(rows) > SPLIT_OVER}

    files = 0
    total = 0
    biggest = ("", 0)
    for key, rows in buckets.items():
        if len(key) == DEPTH and key[:2] not in split:
            continue  # parent is complete; this child would never be read
        if len(key) == 1 or key in split:
            rows = rows[:LIMIT]  # capped: only ever read by the query == key
        body = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        (IDX / (key.encode("utf-8").hex() + ".html")).write_text(body, encoding="utf-8")
        files += 1
        total += len(body.encode("utf-8"))
        if len(body) > biggest[1]:
            biggest = (key, len(body))

    (SRC_DATA / "split_keys.json").write_text(
        json.dumps(sorted(split), ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )

    # Small enough to live in the bundle and keep exact substring matching.
    states = [
        {"name": s.name, "country_code": c.code, "country_name": c.name}
        for c in main.all_countries
        for s in (c.states or [])
    ]
    countries = [
        {"code": c.code, "name": c.name, "phone_code": c.phone_code, "flag": c.flag}
        for c in main.all_countries
    ]
    names = {c.code: c.name for c in main.all_countries}

    for fname, payload in (
        ("states.json", states),
        ("countries.json", countries),
        ("country_names.json", names),
    ):
        (SRC_DATA / fname).write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )

    print(f"city shards: {files} files, {total / 1e6:.1f} MB")
    print(f"largest shard: {biggest[0]!r} at {biggest[1] / 1024:.0f} KB")
    print(f"split keys: {len(split)}")
    print(f"bundled: {len(states)} states, {len(countries)} countries")


if __name__ == "__main__":
    main_build()
