"""Build the search indexes.

Two different strategies, chosen by size:

  countries (249) and states (~5,300) are small enough to embed in the Worker
  bundle and scan per request, so they keep the current substring semantics
  byte-for-byte.

  cities (154,190) cannot be scanned inside the free tier's 10ms CPU budget, so
  they are sharded into static assets the Worker fetches on demand.

City search must match search_cities() in main.py exactly: a city matches when
the lowercased query is a substring of its lowercased name or name_local, and
the first 50 matches in world_cities.json order are returned.

Shard contract, which the Worker depends on:

  * Queries of 3+ characters read a trigram bucket. A city is stored in the
    bucket of every trigram of its lowercased names, and any match contains
    every trigram of the query, so each of the query's buckets holds every
    match. Buckets are COMPLETE and keep original order; the Worker reads the
    smallest candidate (sizes in src/data/tri_sizes.json) and filters it.
  * Queries of 1-2 characters have no trigram, so every 1- and 2-character
    substring gets its own list of the first LIMIT matching cities, grouped
    into hashed files as {key: rows}.
  * A key's bucket is FNV-1a of its UTF-8 bytes mod the bucket count; fnv1a()
    in src/index.ts must produce the same numbers.
  * N-grams are taken over code points (Python string slicing); the Worker
    splits the query with Array.from() to match.
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

LIMIT = 50           # /v1/search/cities caps results at 50
TRI_BUCKETS = 2048   # must match TRI_BUCKETS in src/index.ts
SHORT_BUCKETS = 512  # must match SHORT_BUCKETS in src/index.ts

IDX = ROOT / "dist" / "_idx" / "cities"
SRC_DATA = ROOT / "src" / "data"


def fnv1a(key: str) -> int:
    h = 0x811C9DC5
    for b in key.encode("utf-8"):
        h = ((h ^ b) * 0x01000193) & 0xFFFFFFFF
    return h


def ngrams(texts: list, n: int) -> set:
    out = set()
    for t in texts:
        out.update(t[i:i + n] for i in range(len(t) - n + 1))
    return out


def write_json(path: Path, payload) -> int:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text(body, encoding="utf-8")
    return len(body.encode("utf-8"))


def main_build() -> None:
    if main.USE_REDIS:
        sys.exit("Redis unexpectedly reachable; build requires the in-memory index.")

    if IDX.exists():
        shutil.rmtree(IDX)
    (IDX / "t").mkdir(parents=True)
    (IDX / "s").mkdir(parents=True)
    SRC_DATA.mkdir(parents=True, exist_ok=True)

    tri = [[] for _ in range(TRI_BUCKETS)]
    short: dict = {}
    # city_search_index is built in world_cities.json order, which is the order
    # search_cities() returns, so appending in this loop preserves it.
    for city in main.city_search_index:
        row = [city["n"], city["nl"], city["s"], city["c"], city.get("lat"), city.get("lon")]
        texts = [t.lower() for t in (city["n"], city["nl"]) if t]
        for b in {fnv1a(g) % TRI_BUCKETS for g in ngrams(texts, 3)}:
            tri[b].append(row)
        for g in ngrams(texts, 1) | ngrams(texts, 2):
            hits = short.setdefault(g, [])
            if len(hits) < LIMIT:
                hits.append(row)

    grouped = [{} for _ in range(SHORT_BUCKETS)]
    for key, rows in short.items():
        grouped[fnv1a(key) % SHORT_BUCKETS][key] = rows

    total = 0
    for i, rows in enumerate(tri):
        total += write_json(IDX / "t" / f"{i}.html", rows)
    for i, keys in enumerate(grouped):
        total += write_json(IDX / "s" / f"{i}.html", keys)
    write_json(SRC_DATA / "tri_sizes.json", [len(rows) for rows in tri])

    stale = SRC_DATA / "split_keys.json"  # manifest of the old prefix shards
    if stale.exists():
        stale.unlink()

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
        write_json(SRC_DATA / fname, payload)

    biggest = max(range(TRI_BUCKETS), key=lambda i: len(tri[i]))
    print(f"city index: {TRI_BUCKETS + SHORT_BUCKETS} files, {total / 1e6:.1f} MB")
    print(f"largest trigram bucket: #{biggest} with {len(tri[biggest])} rows")
    print(f"short keys: {len(short)}")
    print(f"bundled: {len(states)} states, {len(countries)} countries")


if __name__ == "__main__":
    main_build()
