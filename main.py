import json
import os
import re
import redis
from typing import List, Optional
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# Rate Limit Configuration
RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "500")
RATE_LIMIT_HEAVY = os.getenv("RATE_LIMIT_HEAVY", "200")
RATE_LIMIT_METADATA = os.getenv("RATE_LIMIT_METADATA", "100")

# Redis Connection Configuration
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD")
REDIS_SSL = os.getenv("REDIS_SSL", "False").lower() == "true"

# Construct Redis URI for SlowAPI storage
redis_uri = f"redis://{REDIS_HOST}:{REDIS_PORT}"
if REDIS_PASSWORD:
    redis_uri = f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}"
if REDIS_SSL:
    redis_uri = redis_uri.replace("redis://", "rediss://")

# Initialize Redis client for data
try:
    redis_client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        ssl=REDIS_SSL,
        decode_responses=True
    )
    redis_client.ping()
    USE_REDIS = True
    print(f"Successfully connected to Redis at {REDIS_HOST}:{REDIS_PORT}. Using Redis for city data.")
except Exception as e:
    print(f"Warning: Redis not available at {REDIS_HOST}:{REDIS_PORT}, falling back to local JSON: {e}")
    USE_REDIS = False

# Initialize FastAPI app and rate limiter
# Use Redis as storage backend for Limiter if available for shared rate limiting
limiter_storage = redis_uri if USE_REDIS else "memory://"
limiter = Limiter(key_func=get_remote_address, storage_uri=limiter_storage)

app = FastAPI(
    title="Country State API",
    description="API for countries and their states - optimized for dropdown usage",
    version="1.1.0",
    contact={"name": "Nyein Chan Ko Ko", "url": "https://github.com/nchanko"},
    license_info={"name": "MIT License", "url": "https://opensource.org/licenses/MIT"},
)

# Setup middleware and handlers
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Setup templates and router
templates = Jinja2Templates(directory="templates")
v1_router = APIRouter(prefix="/v1", tags=["v1"])

# Data Models
class City(BaseModel):
    name: str
    name_local: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None

class State(BaseModel):
    name: str
    cities: Optional[List[City]] = []

class CountryBase(BaseModel):
    code: str
    name: str
    phone_code: str = ""
    flag: str = ""

class Country(CountryBase):
    region: str = ""
    subregion: str = ""
    currency: str = ""
    currency_symbol: str = ""
    language: str = ""
    population: int = 0
    states: Optional[List[State]] = []

class CountryWithRegion(CountryBase):
    region: str = ""
    subregion: str = ""

class Region(BaseModel):
    name: str
    subregions: List[str] = []
    countries: List[str] = []

# ---------------------------------------------------------------------------
# Global lookup structures (populated once at startup)
# ---------------------------------------------------------------------------

# {country_code: Country}
country_lookup: dict = {}
# {country_code: {state_name_lower: State}}  – kept for O(1) state resolution
state_lookup: dict = {}
# Lightweight city search index – only kept when Redis is NOT available
# Each entry: {"n": name, "nl": name_local, "s": state_name, "c": country_code,
#              "lat": float|None, "lon": float|None}
city_search_index: list = []

# In-memory response caches for endpoints that have no per-request variance
# (lru_cache cannot be used on FastAPI handlers directly because Request is
#  always a new object, making every call a cache miss)
_cache: dict = {}


def find_state_relaxedly(country_code: str, state_name: str):
    """O(1) exact lookup then O(n) relaxed fallback using the global state_lookup."""
    states_by_name = state_lookup.get(country_code)
    if not states_by_name:
        return None

    target = state_name.lower().strip()

    # 1. Exact match – O(1)
    state = states_by_name.get(target)
    if state:
        return state

    # 2. Starts-with, then substring – O(n) over states in this country only
    state = next((s for s in states_by_name.values() if s.name.lower().startswith(target)), None)
    if state:
        return state

    return next(
        (s for s in states_by_name.values()
         if target in s.name.lower() or s.name.lower() in target),
        None,
    )


_WORD_RE = re.compile(r"\w+")


def _same_word(a: str, b: str) -> bool:
    """Equal, or one is the other plus a short inflection (Hesse/Hessen,
    Silesia/Silesian). A longer tail is a different place: Derby is not
    Derbyshire, Kyiv is not Kyivska."""
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and long_.startswith(short) and len(long_) - len(short) <= 2


def _word_run(outer: list, inner: list):
    """Where inner first appears as a contiguous run of words in outer, as
    (index, exact); exact is False if an inflection was needed. None if absent."""
    n = len(inner)
    for k in range(len(outer) - n + 1):
        window = outer[k:k + n]
        if all(_same_word(o, i) for o, i in zip(window, inner)):
            return k, window == inner
    return None


def match_existing_state(name: str, states) -> Optional["State"]:
    """Find the existing state a city file's state name refers to.

    Best first: a state that starts with the name ('Berat' -> 'Berat County'),
    then one containing it ('Barnet' -> 'London Borough of Barnet'), then one
    contained in it. Exact words beat inflections, and fewer extra words win
    ('Panamá' -> 'Panamá Province', not 'Panamá Oeste Province').
    """
    target = _WORD_RE.findall(name.lower())
    if not target:
        return None
    best, best_score, tied = None, None, False
    seen = set()
    for state in states:
        if id(state) in seen:  # s_lookup holds aliases of the same state
            continue
        seen.add(id(state))
        words = _WORD_RE.findall(state.name.lower())
        if not words:
            continue
        hit = _word_run(words, target)
        if hit:
            rank = 0 if hit[0] == 0 else 1
        else:
            hit = _word_run(target, words)
            if not hit:
                continue
            rank = 2
        score = (rank, not hit[1], len(words))
        if best_score is None or score < best_score:
            best, best_score, tied = state, score, False
        elif score == best_score:
            tied = True
    # A tie among states that start with the name ('Berat County' vs 'Berat
    # District') is one place at different admin levels, so the first wins.
    # Any other tie is different places sharing a word ('Tobago' in Eastern and
    # Western Tobago): return None so the caller makes a stub rather than guess.
    if tied and best_score[0] != 0:
        return None
    return best


def load_data():
    global city_search_index, state_lookup

    with open('data.json', 'r', encoding='utf-8') as f:
        countries_data = json.load(f)

    with open('regions.json', 'r', encoding='utf-8') as f:
        regions_data = json.load(f)

    all_countries = [Country(**c) for c in countries_data]
    c_lookup = {c.code: c for c in all_countries}

    # Build the global state_lookup dict once – reused by every request
    s_lookup = {
        c.code: {s.name.lower(): s for s in (c.states or [])}
        for c in all_countries
    }

    if os.path.exists('world_cities.json'):
        try:
            with open('world_cities.json', 'r', encoding='utf-8') as f:
                world_cities = json.load(f)

            # Automatic Redis population (runs once; guarded by a Redis marker)
            if USE_REDIS:
                try:
                    if not redis_client.get("system:cities_loaded"):
                        print("Redis data marker missing. Starting automatic data synchronization...")
                        data_by_state = {}
                        for city in world_cities:
                            c_code = city["country_code"]
                            s_name = city["state_name"]
                            key = f"cities:{c_code.upper()}:{s_name.lower().replace(' ', '_')}"
                            if key not in data_by_state:
                                data_by_state[key] = []
                            data_by_state[key].append(city)

                        pipe = redis_client.pipeline()
                        for key, cities_list in data_by_state.items():
                            pipe.set(key, json.dumps(cities_list, ensure_ascii=False))
                        pipe.set("system:cities_loaded", "true")
                        pipe.execute()
                        print(f"Successfully auto-populated Redis with {len(world_cities)} cities.")
                except Exception as e:
                    print(f"Warning: Failed to auto-populate Redis: {e}")

            # Build search index and optionally load full city objects into RAM.
            # When Redis is available we only need the lightweight search index
            # (name + state + country) so we can answer /search/cities quickly
            # without holding lat/lon data in RAM.
            _city_index = []
            for city in world_cities:
                c_code = city["country_code"]
                s_name = city["state_name"]
                city_name_local = city.get("name_local", city.get("name_mm", ""))

                # Ensure state exists. The city file often names a state
                # differently from data.json, so try a word-level match before
                # creating a stub.
                if c_code in s_lookup and s_name.lower() not in s_lookup[c_code]:
                    matched_state = match_existing_state(s_name, s_lookup[c_code].values())
                    if matched_state:
                        s_lookup[c_code][s_name.lower()] = matched_state
                    else:
                        new_state = State(name=s_name, cities=[])
                        c_lookup[c_code].states.append(new_state)
                        s_lookup[c_code][s_name.lower()] = new_state

                if USE_REDIS:
                    # Lightweight index only – no lat/lon to save RAM
                    _city_index.append({
                        "n": city["name"],
                        "nl": city_name_local,
                        "s": s_name,
                        "c": c_code,
                    })
                else:
                    # Full in-memory mode: attach City objects to State nodes AND
                    # keep lat/lon in the search index so /search/cities can return them
                    # Use s_lookup directly for O(1) exact match
                    state = s_lookup.get(c_code, {}).get(s_name.lower())
                    if state is None:
                        # Relaxed fallback for mismatched state names
                        states_by_name = s_lookup.get(c_code, {})
                        target = s_name.lower().strip()
                        state = next(
                            (s for s in states_by_name.values() if s.name.lower().startswith(target)),
                            None,
                        ) or next(
                            (s for s in states_by_name.values()
                             if target in s.name.lower() or s.name.lower() in target),
                            None,
                        )
                    if state is not None:
                        if not state.cities:
                            state.cities = []
                        lat = city.get("latitude")
                        lon = city.get("longitude")
                        state.cities.append(City(
                            name=city["name"],
                            name_local=city_name_local,
                            latitude=float(lat) if lat is not None else None,
                            longitude=float(lon) if lon is not None else None,
                        ))

                    _city_index.append({
                        "n": city["name"],
                        "nl": city_name_local,
                        "s": s_name,
                        "c": c_code,
                        "lat": float(city["latitude"]) if city.get("latitude") is not None else None,
                        "lon": float(city["longitude"]) if city.get("longitude") is not None else None,
                    })

            # Discard the raw list to free ~23 MB of parsed JSON
            del world_cities

            city_search_index = _city_index
            mode_msg = "Redis (On-demand)" if USE_REDIS else "In-Memory (Heavy)"
            print(f"City data initialized in {mode_msg} mode. Indexed {len(city_search_index)} cities.")

        except Exception as e:
            print(f"Warning: Failed to load city data: {e}")

    state_lookup = s_lookup
    return all_countries, c_lookup, regions_data


all_countries, country_lookup, regions_lookup = load_data()

# ---------------------------------------------------------------------------
# Pre-build the static responses that never change so handlers just return them
# ---------------------------------------------------------------------------
_countries_response: List[CountryBase] = [
    CountryBase(code=c.code, name=c.name, phone_code=c.phone_code, flag=c.flag)
    for c in all_countries
]

_regions_response: List[Region] = [
    Region(name=rname, subregions=rdata["subregions"], countries=rdata["countries"])
    for rname, rdata in regions_lookup.items()
]

# {region_key_lower: List[CountryWithRegion]} – built lazily, cached forever
_region_countries_cache: dict = {}

# {country_code: List[State]} – states list is already in country_lookup but we
# cache the serialised list so the same list object is returned every time
_states_cache: dict = {}


@app.get("/")
@limiter.limit(f"{RATE_LIMIT_METADATA}/minute")
def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@v1_router.get("/countries", response_model=List[CountryBase])
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
def get_countries(request: Request):
    """Get all countries - optimized for dropdown usage"""
    return _countries_response


@v1_router.get("/countries/{country_code}/states", response_model=List[State])
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
def get_states(request: Request, country_code: str):
    """Get all states for a specific country"""
    country_code = country_code.upper()

    if country_code not in country_lookup:
        raise HTTPException(status_code=404, detail="Country not found")

    if country_code not in _states_cache:
        states_raw = country_lookup[country_code].states or []
        _states_cache[country_code] = [State(name=s.name, cities=[]) for s in states_raw]

    return _states_cache[country_code]


@v1_router.get("/countries/{country_code}/states/{state_name}/cities", response_model=List[City])
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
def get_cities(request: Request, country_code: str, state_name: str):
    """Get all cities for a specific state in a country"""
    country_code = country_code.upper()

    # Try Redis first if available
    if USE_REDIS:
        state = find_state_relaxedly(country_code, state_name)
        if state:
            key = f"cities:{country_code}:{state.name.lower().replace(' ', '_')}"
            cities_data = redis_client.get(key)
            if cities_data:
                raw_cities = json.loads(cities_data)
                # Normalise name_mm -> name_local for older data
                for c in raw_cities:
                    if "name_local" not in c and "name_mm" in c:
                        c["name_local"] = c["name_mm"]
                return raw_cities

    # Fallback to in-memory lookup
    state = find_state_relaxedly(country_code, state_name)
    if not state:
        raise HTTPException(status_code=404, detail="State not found")

    return state.cities or []


@v1_router.get("/search/countries", response_model=List[CountryBase])
@limiter.limit(f"{RATE_LIMIT_HEAVY}/minute")
def search_countries(request: Request, q: str = Query(..., description="Search query for country name")):
    """Search countries by name"""
    query = q.lower().strip()

    if not query:
        return []

    results = [c for c in _countries_response if query in c.name.lower()]
    return results[:20]


# Pre-build a flat state list for fast O(n) search – built once at startup
_all_states_flat: List[dict] = [
    {"name": state.name, "country_code": country.code, "country_name": country.name}
    for country in all_countries
    if country.states
    for state in country.states
]


@v1_router.get("/search/states", response_model=List[dict])
@limiter.limit(f"{RATE_LIMIT_HEAVY}/minute")
def search_states(request: Request, q: str = Query(..., description="Search query for state name")):
    """Search states by name across all countries"""
    query = q.lower().strip()

    if not query:
        return []

    results = [s for s in _all_states_flat if query in s["name"].lower()]
    return results[:20]


@v1_router.get("/search/cities", response_model=List[dict])
@limiter.limit(f"{RATE_LIMIT_HEAVY}/minute")
def search_cities(request: Request, q: str = Query(..., description="Search query for city name")):
    """Search cities by name across all countries"""
    query = q.lower().strip()

    if not query:
        return []

    results = []

    if USE_REDIS:
        for city in city_search_index:
            if query in city["n"].lower() or (city["nl"] and query in city["nl"].lower()):
                country = country_lookup.get(city["c"])
                results.append({
                    "name": city["n"],
                    "name_local": city["nl"],
                    "state_name": city["s"],
                    "country_code": city["c"],
                    "country_name": country.name if country else city["c"],
                    "latitude": None,
                    "longitude": None,
                })
                if len(results) >= 50:
                    break
        return results

    # In-memory fallback: index includes lat/lon
    for city in city_search_index:
        if query in city["n"].lower() or (city["nl"] and query in city["nl"].lower()):
            country = country_lookup.get(city["c"])
            results.append({
                "name": city["n"],
                "name_local": city["nl"],
                "state_name": city["s"],
                "country_code": city["c"],
                "country_name": country.name if country else city["c"],
                "latitude": city.get("lat"),
                "longitude": city.get("lon"),
            })
            if len(results) >= 50:
                break

    return results


@v1_router.get("/regions", response_model=List[Region])
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
def get_regions(request: Request):
    """Get all regions with their subregions and countries"""
    return _regions_response


@v1_router.get("/regions/{region}/countries", response_model=List[CountryWithRegion])
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
def get_countries_by_region(request: Request, region: str):
    """Get all countries in a specific region"""
    region_lower = region.lower()

    if region_lower in _region_countries_cache:
        return _region_countries_cache[region_lower]

    region_key = next(
        (key for key in regions_lookup.keys() if key.lower() == region_lower),
        None,
    )

    if not region_key:
        raise HTTPException(status_code=404, detail="Region not found")

    country_codes = set(regions_lookup[region_key]["countries"])

    results = sorted(
        [
            CountryWithRegion(
                code=c.code,
                name=c.name,
                region=c.region,
                subregion=c.subregion,
                phone_code=c.phone_code,
                flag=c.flag,
            )
            for c in all_countries
            if c.code in country_codes
        ],
        key=lambda x: x.name,
    )

    _region_countries_cache[region_lower] = results
    return results


@v1_router.get("/search/phone-code/{code}", response_model=List[CountryBase])
@limiter.limit(f"{RATE_LIMIT_HEAVY}/minute")
def search_by_phone_code(request: Request, code: str):
    """Find countries by phone code (e.g., '+1', '1', '+44')"""
    search_code = code.strip()
    if not search_code.startswith('+'):
        search_code = '+' + search_code

    results = [
        c for c in _countries_response
        if c.phone_code == search_code or c.phone_code.startswith(search_code)
    ]
    return results[:10]


@v1_router.get("/countries/{country_code}", response_model=Country)
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
def get_country_details(request: Request, country_code: str):
    """Get detailed information about a specific country"""
    country_code = country_code.upper()

    if country_code not in country_lookup:
        raise HTTPException(status_code=404, detail="Country not found")

    country = country_lookup[country_code]
    return Country(
        code=country.code,
        name=country.name,
        phone_code=country.phone_code,
        flag=country.flag,
        region=country.region,
        subregion=country.subregion,
        currency=country.currency,
        currency_symbol=country.currency_symbol,
        language=country.language,
        population=country.population,
        states=[State(name=s.name, cities=[]) for s in (country.states or [])],
    )


@app.get("/version")
@limiter.limit(f"{RATE_LIMIT_METADATA}/minute")
def get_version_info(request: Request):
    """Get API version information"""
    return {
        "api_name": "Country State API",
        "current_version": "v1",
        "version": "1.0.0",
        "available_versions": ["v1"],
        "endpoints": {"v1": "/v1/"},
        "documentation": {"interactive": "/docs", "redoc": "/redoc"},
    }


# Include the versioned router
app.include_router(v1_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
