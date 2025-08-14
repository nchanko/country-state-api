import json
from functools import lru_cache
from typing import List, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# Initialize FastAPI app and rate limiter
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(
    title="Country State API",
    description="API for countries and their states - optimized for dropdown usage",
    version="1.0.0",
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
class State(BaseModel):
    name: str

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
def load_data():
    with open('data.json', 'r', encoding='utf-8') as file:
        countries_data = json.load(file)
    
    with open('regions.json', 'r', encoding='utf-8') as file:
        regions_data = json.load(file)
    
    all_countries = [Country(**country) for country in countries_data]
    country_lookup = {country.code: country for country in all_countries}
    
    return all_countries, country_lookup, regions_data

all_countries, country_lookup, regions_lookup = load_data()

@app.get("/")
@limiter.limit("100/minute")
def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@v1_router.get("/countries", response_model=List[CountryBase])
@limiter.limit("500/minute")
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
@limiter.limit("500/minute")
@lru_cache(maxsize=256)
def get_states(request: Request, country_code: str):
    """Get all states for a specific country"""
    country_code = country_code.upper()
    
    if country_code not in country_lookup:
        raise HTTPException(status_code=404, detail="Country not found")
    
    country = country_lookup[country_code]
    return country.states or []

@v1_router.get("/search/countries", response_model=List[CountryBase])
@limiter.limit("200/minute")
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
@limiter.limit("200/minute")
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

@v1_router.get("/regions", response_model=List[Region])
@limiter.limit("300/minute")
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
@limiter.limit("300/minute")
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
@limiter.limit("200/minute")
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
@limiter.limit("300/minute")
def get_country_details(request: Request, country_code: str):
    """Get detailed information about a specific country"""
    country_code = country_code.upper()
    
    if country_code not in country_lookup:
        raise HTTPException(status_code=404, detail="Country not found")
    
    return country_lookup[country_code]

# Add version info endpoint
@app.get("/version")
@limiter.limit("100/minute")  
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