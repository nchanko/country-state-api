import json
import os
import redis
from functools import lru_cache
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

# (Redis code was moved above to initialize Limiter with it)

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

# Load and process data at startup
def find_state_relaxedly(country_code: str, state_name: str, country_lookup_map: dict):
    """Helper to find a state in a country using exact then relaxed matching"""
    if country_code not in country_lookup_map:
        return None
    
    country = country_lookup_map[country_code]
    target = state_name.lower().strip()
    
    # 1. Exact match
    state = next((s for s in (country.states or []) if s.name.lower() == target), None)
    if state:
        return state
        
    # 2. Relaxed match (substring)
    return next((s for s in (country.states or []) 
                if target in s.name.lower() or s.name.lower() in target), None)

def load_data():
    with open('data.json', 'r', encoding='utf-8') as file:
        countries_data = json.load(file)
    
    with open('regions.json', 'r', encoding='utf-8') as file:
        regions_data = json.load(file)
    
    all_countries = [Country(**country) for country in countries_data]
    country_lookup = {country.code: country for country in all_countries}

    # Populate City Data
    global city_search_index
    city_search_index = []

    if os.path.exists('world_cities.json'):
        try:
            with open('world_cities.json', 'r', encoding='utf-8') as f:
                world_cities = json.load(f)
            
            # Automatic Redis Auto-Loading
            if USE_REDIS:
                try:
                    if not redis_client.get("system:cities_loaded"):
                        print("Redis data marker missing. Starting automatic data synchronization...")
                        data_by_state = {}
                        for city in world_cities:
                            c_code, s_name = city["country_code"], city["state_name"]
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

            state_lookup = {c.code: {s.name.lower(): s for s in (c.states or [])} for c in all_countries}

            for city in world_cities:
                c_code, s_name = city["country_code"], city["state_name"]
                
                # Build lightweight search index for both modes
                city_name_local = city.get("name_local", city.get("name_mm", ""))
                city_search_index.append({
                    "n": city["name"], 
                    "nl": city_name_local, 
                    "s": s_name, 
                    "c": c_code
                })

                # Ensure dynamic state creation (common for both modes)
                if c_code in state_lookup and s_name.lower() not in state_lookup[c_code]:
                    new_state = State(name=s_name, cities=[])
                    country_lookup[c_code].states.append(new_state)
                    state_lookup[c_code][s_name.lower()] = new_state

                # Only load full city objects into RAM if Redis is NOT used
                if not USE_REDIS:
                    state = find_state_relaxedly(c_code, s_name, country_lookup)
                    if state:
                        if not state.cities: state.cities = []
                        state.cities.append(City(
                            name=city["name"],
                            name_local=city.get("name_local", city.get("name_mm", "")),
                            latitude=city.get("latitude"),
                            longitude=city.get("longitude")
                        ))
            
            mode_msg = "Redis (On-demand)" if USE_REDIS else "In-Memory (Heavy)"
            print(f"City data initialized in {mode_msg} mode. Indexed {len(city_search_index)} cities.")
            
        except Exception as e:
            print(f"Warning: Failed to load city data: {e}")
    
    return all_countries, country_lookup, regions_data

all_countries, country_lookup, regions_lookup = load_data()

@app.get("/")
@limiter.limit(f"{RATE_LIMIT_METADATA}/minute")
def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@v1_router.get("/countries", response_model=List[CountryBase])
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
@lru_cache(maxsize=1)
def get_countries(request: Request):
    """Get all countries - optimized for dropdown usage"""
    return [CountryBase(
        code=country.code,
        name=country.name,
        phone_code=country.phone_code,
        flag=country.flag
    ) for country in all_countries]

@v1_router.get("/countries/{country_code}/states", response_model=List[State])
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
@lru_cache(maxsize=256)
def get_states(request: Request, country_code: str):
    """Get all states for a specific country"""
    country_code = country_code.upper()
    
    if country_code not in country_lookup:
        raise HTTPException(status_code=404, detail="Country not found")
    
    country = country_lookup[country_code]
    return country.states or []

@v1_router.get("/countries/{country_code}/states/{state_name}/cities", response_model=List[City])
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
@lru_cache(maxsize=1024)
def get_cities(request: Request, country_code: str, state_name: str):
    """Get all cities for a specific state in a country"""
    country_code = country_code.upper()
    
    # Try Redis first if available
    if USE_REDIS:
        # Search for key using normalization and relaxed matching
        state = find_state_relaxedly(country_code, state_name, country_lookup)
        if state:
            key = f"cities:{country_code}:{state.name.lower().replace(' ', '_')}"
            cities_data = redis_client.get(key)
            if cities_data:
                raw_cities = json.loads(cities_data)
                # Map name_mm to name_local if missing (compatibility check)
                for c in raw_cities:
                    if "name_local" not in c and "name_mm" in c:
                        c["name_local"] = c["name_mm"]
                return raw_cities

    # Fallback to in-memory lookup
    state = find_state_relaxedly(country_code, state_name, country_lookup)
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
    
    results = [
        CountryBase(
            code=country.code,
            name=country.name,
            phone_code=country.phone_code,
            flag=country.flag
        )
        for country in all_countries
        if query in country.name.lower()
    ]
    
    return results[:20]

@v1_router.get("/search/states", response_model=List[dict])
@limiter.limit(f"{RATE_LIMIT_HEAVY}/minute")
def search_states(request: Request, q: str = Query(..., description="Search query for state name")):
    """Search states by name across all countries"""
    query = q.lower().strip()
    
    if not query:
        return []
    
    results = [
        {
            "name": state.name,
            "country_code": country.code,
            "country_name": country.name
        }
        for country in all_countries
        if country.states
        for state in country.states
        if query in state.name.lower()
    ]
    
    return results[:20]

@v1_router.get("/search/cities", response_model=List[dict])
@limiter.limit(f"{RATE_LIMIT_HEAVY}/minute")
def search_cities(request: Request, q: str = Query(..., description="Search query for city name")):
    """Search cities by name across all countries"""
    query = q.lower().strip()
    
    if not query:
        return []
    
    results = []
    
    # If using Redis, use our lightweight search index
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
                    "latitude": None, # Latitude/Longitude not in lightweight index
                    "longitude": None
                })
                if len(results) >= 50:
                    return results
        return results

    # Global search using in-memory data (Fallback Mode)
    for country in all_countries:
        if not country.states:
            continue
        for state in country.states:
            if not state.cities:
                continue
            for city in state.cities:
                if query in city.name.lower() or (city.name_local and query in city.name_local):
                    results.append({
                        "name": city.name,
                        "name_local": city.name_local,
                        "state_name": state.name,
                        "country_code": country.code,
                        "country_name": country.name,
                        "latitude": city.latitude,
                        "longitude": city.longitude
                    })
                    if len(results) >= 50:
                        return results
    
    return results

@v1_router.get("/regions", response_model=List[Region])
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
@lru_cache(maxsize=1)
def get_regions(request: Request):
    """Get all regions with their subregions and countries"""
    return [
        Region(
            name=region_name,
            subregions=region_data["subregions"],
            countries=region_data["countries"]
        )
        for region_name, region_data in regions_lookup.items()
    ]

@v1_router.get("/regions/{region}/countries", response_model=List[CountryWithRegion])
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
@lru_cache(maxsize=10)
def get_countries_by_region(request: Request, region: str):
    """Get all countries in a specific region"""
    region_key = next(
        (key for key in regions_lookup.keys() if key.lower() == region.lower()),
        None
    )
    
    if not region_key:
        raise HTTPException(status_code=404, detail="Region not found")
    
    country_codes = set(regions_lookup[region_key]["countries"])
    
    results = [
        CountryWithRegion(
            code=country.code,
            name=country.name,
            region=country.region,
            subregion=country.subregion,
            phone_code=country.phone_code,
            flag=country.flag
        )
        for country in all_countries
        if country.code in country_codes
    ]
    
    return sorted(results, key=lambda x: x.name)

@v1_router.get("/search/phone-code/{code}", response_model=List[CountryBase])
@limiter.limit(f"{RATE_LIMIT_HEAVY}/minute")
def search_by_phone_code(request: Request, code: str):
    """Find countries by phone code (e.g., '+1', '1', '+44')"""
    search_code = code.strip()
    if not search_code.startswith('+'):
        search_code = '+' + search_code
    
    results = [
        CountryBase(
            code=country.code,
            name=country.name,
            phone_code=country.phone_code,
            flag=country.flag
        )
        for country in all_countries
        if country.phone_code == search_code or country.phone_code.startswith(search_code)
    ]
    
    return results[:10]

@v1_router.get("/countries/{country_code}", response_model=Country)
@limiter.limit(f"{RATE_LIMIT_DEFAULT}/minute")
def get_country_details(request: Request, country_code: str):
    """Get detailed information about a specific country"""
    country_code = country_code.upper()
    
    if country_code not in country_lookup:
        raise HTTPException(status_code=404, detail="Country not found")
    
    return country_lookup[country_code]

# Add version info endpoint
@app.get("/version")
@limiter.limit(f"{RATE_LIMIT_METADATA}/minute")  
def get_version_info(request: Request):
    """Get API version information"""
    return {
        "api_name": "Country State API",
        "current_version": "v1",
        "version": "1.0.0",
        "available_versions": ["v1"],
        "endpoints": {
            "v1": "/v1/"
        },
        "documentation": {
            "interactive": "/docs",
            "redoc": "/redoc"
        }
    }

# Include the versioned router
app.include_router(v1_router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)