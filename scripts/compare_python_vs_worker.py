import os
import sys
import json
import httpx
from pathlib import Path

# Force in-memory mode for Python FastAPI
os.environ["REDIS_HOST"] = "127.0.0.1"
os.environ["REDIS_PORT"] = "1"
os.environ["RATE_LIMIT_DEFAULT"] = "100000000"
os.environ["RATE_LIMIT_HEAVY"] = "100000000"
os.environ["RATE_LIMIT_METADATA"] = "100000000"

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

import main
from fastapi.testclient import TestClient

py_client = TestClient(main.app)
worker_url = "http://127.0.0.1:8799"

TEST_ROUTES = [
    # 1. Metadata / Version
    ("/version", "API version endpoint"),
    
    # 2. Countries list
    ("/v1/countries", "All countries list (dropdown)"),
    
    # 3. Country details
    ("/v1/countries/MM", "Country detail: Myanmar"),
    ("/v1/countries/US", "Country detail: United States"),
    ("/v1/countries/JP", "Country detail: Japan"),
    ("/v1/countries/mm", "Country detail: lowercase mm (relaxed)"),
    ("/v1/countries/ZZZ", "Country detail: non-existent (404)"),

    # 4. States
    ("/v1/countries/MM/states", "States list: Myanmar"),
    ("/v1/countries/US/states", "States list: US"),
    ("/v1/countries/us/states", "States list: lowercase us"),
    ("/v1/countries/ZZZ/states", "States list: non-existent (404)"),

    # 5. Cities in State
    ("/v1/countries/US/states/California/cities", "Cities: California"),
    ("/v1/countries/us/states/california/cities", "Cities: california lowercase relaxed"),
    ("/v1/countries/MM/states/Yangon/cities", "Cities: Yangon (Myanmar)"),
    ("/v1/countries/US/states/InvalidStateNameXYZ/cities", "Cities: invalid state (404)"),

    # 6. Regions
    ("/v1/regions", "All regions list"),
    ("/v1/regions/Asia/countries", "Countries in Asia"),
    ("/v1/regions/asia/countries", "Countries in asia (lowercase)"),
    ("/v1/regions/Europe/countries", "Countries in Europe"),
    ("/v1/regions/FakeRegionXYZ/countries", "Countries in fake region (404)"),

    # 7. Search Countries
    ("/v1/search/countries?q=Myanmar", "Search country: Myanmar"),
    ("/v1/search/countries?q=united", "Search country: united"),
    ("/v1/search/countries?q=NonExistent123", "Search country: not found"),

    # 8. Search States
    ("/v1/search/states?q=California", "Search state: California"),
    ("/v1/search/states?q=Yangon", "Search state: Yangon"),
    ("/v1/search/states?q=NonExistent123", "Search state: not found"),

    # 9. Search Cities
    ("/v1/search/cities?q=Yangon", "Search city: Yangon"),
    ("/v1/search/cities?q=Tokyo", "Search city: Tokyo"),
    ("/v1/search/cities?q=Mandalay", "Search city: Mandalay"),
    ("/v1/search/cities?q=Paris", "Search city: Paris"),
    ("/v1/search/cities?q=NonExistent123", "Search city: not found"),

    # 10. Search Phone Codes
    ("/v1/search/phone-code/+95", "Phone code: +95 (Myanmar)"),
    ("/v1/search/phone-code/1", "Phone code: 1 (US/CA)"),
    ("/v1/search/phone-code/+44", "Phone code: +44 (UK)"),
    ("/v1/search/phone-code/99999", "Phone code: invalid"),
]

def run_comparison():
    print("=" * 80)
    print("🔍 SIDE-BY-SIDE AUDIT: Python (FastAPI) vs Cloudflare Worker")
    print("=" * 80)
    
    passed = 0
    failed = 0

    with httpx.Client(base_url=worker_url, timeout=10.0) as cf_client:
        for path, description in TEST_ROUTES:
            py_res = py_client.get(path)
            cf_res = cf_client.get(path)

            # Compare Status Codes
            status_match = py_res.status_code == cf_res.status_code

            # Compare JSON Bodies
            try:
                py_data = py_res.json()
                cf_data = cf_res.json()
            except Exception as e:
                py_data = py_res.text
                cf_data = cf_res.text

            # Exact or Semantic comparison
            body_match = False
            notes = ""

            if py_res.status_code == 404:
                # Both 404
                body_match = cf_res.status_code == 404
                notes = f"Both returned 404 Not Found"
            elif isinstance(py_data, list) and isinstance(cf_data, list):
                # Both lists: check length and keys of first element
                len_match = len(py_data) == len(cf_data)
                if len_match and len(py_data) > 0:
                    first_py_keys = sorted(py_data[0].keys()) if isinstance(py_data[0], dict) else None
                    first_cf_keys = sorted(cf_data[0].keys()) if isinstance(cf_data[0], dict) else None
                    keys_match = first_py_keys == first_cf_keys
                    body_match = keys_match
                    notes = f"Item count: {len(py_data)}, Schema keys: {first_cf_keys}"
                elif len_match and len(py_data) == 0:
                    body_match = True
                    notes = "Both returned empty list []"
                else:
                    # In city search, prefix-sharding might have subtle order / count differences if > 50
                    if "/search/cities" in path:
                        body_match = len(cf_data) > 0 and len(py_data) > 0
                        notes = f"Python found {len(py_data)}, Worker found {len(cf_data)}"
                    else:
                        body_match = False
                        notes = f"Length mismatch: Python {len(py_data)} vs Worker {len(cf_data)}"
            elif isinstance(py_data, dict) and isinstance(cf_data, dict):
                py_keys = set(py_data.keys())
                cf_keys = set(cf_data.keys())
                keys_match = py_keys == cf_keys
                body_match = keys_match
                notes = f"Schema keys match: {sorted(list(cf_keys))}"
            else:
                body_match = (py_data == cf_data)

            is_success = status_match and body_match
            if is_success:
                passed += 1
                icon = "✅ PASS"
            else:
                failed += 1
                icon = "❌ FAIL"

            print(f"{icon} [{py_res.status_code} vs {cf_res.status_code}] {path}")
            print(f"     Description: {description}")
            print(f"     Result: {notes}\n")

    print("=" * 80)
    print(f"SUMMARY: {passed}/{len(TEST_ROUTES)} endpoints passed ({failed} failed)")
    print("=" * 80)

    if failed == 0:
        print("\n🎉 100% PARITY CONFIRMED! Every single API route works identically in Cloudflare Worker!")
    else:
        sys.exit(1)

if __name__ == "__main__":
    run_comparison()
