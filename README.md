# Country State API

Fast, reliable API for countries and their states/provinces with phone codes, regions, and flags.

## Features

- 249 countries with complete data
- Phone codes for all countries  
- 6 world regions with subregions
- Thousands of states/provinces
- Currency codes and symbols
- Unicode flag emojis
- Search functionality
- Rate limited and cached

## Quick Start

```bash
# Install
pip install -r requirements.txt

# Run
uvicorn main:app --reload
```

## Usage

### Get Countries
```bash
curl http://localhost:8000/v1/countries
```

### Get States
```bash
curl http://localhost:8000/v1/countries/US/states
```

### Search Countries
```bash
curl "http://localhost:8000/v1/search/countries?q=united"
```

### Get Regions
```bash
curl http://localhost:8000/v1/regions
```

### Search by Phone Code
```bash
curl http://localhost:8000/v1/search/phone-code/1
```

## API Endpoints

- `GET /v1/countries` - All countries
- `GET /v1/countries/{code}` - Country details  
- `GET /v1/countries/{code}/states` - States for country
- `GET /v1/regions` - All regions
- `GET /v1/regions/{region}/countries` - Countries in region
- `GET /v1/search/countries?q={query}` - Search countries
- `GET /v1/search/states?q={query}` - Search states  
- `GET /v1/search/phone-code/{code}` - Search by phone code
- `GET /version` - API version info

## Documentation

- Interactive: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Author

**Nyein Chan Ko Ko**  
GitHub: [@nchanko](https://github.com/nchanko)

## License

MIT License