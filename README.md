# Country State API

Fast, reliable API for countries and their states/provinces with phone codes, regions, and flags. Now handles over 150,000 cities with specialized Myanmar PCode data.

## Features

- 249 countries with complete data
- 150,000+ cities with coordinates
- **Zero-Config Redis Support**: Automatically syncs data to Redis on startup.
- **Shared Rate Limiting**: Production-grade traffic control via Redis.
- **Intelligent Fallback**: Works perfectly without Redis for local testing.
- Support for localized names (`name_local`)
- Global search functionality for countries, states, and cities

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Start the API
python main.py
```
*On first run, the API will automatically detect your Redis settings and sync the city data for you.*

## Usage

### Get Countries
`GET /v1/countries`

### Get Cities for State
`GET /v1/countries/US/states/California/cities`

### Search Cities (Global)
`GET /v1/search/cities?q=Tokyo`

## Documentation

- Interactive: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Data Sources

This project utilizes data from:
- **Global Data**: [dr5hn/countries-states-cities-database](https://github.com/dr5hn/countries-states-cities-database)
- **Myanmar Specialized Data**: [Myanmar Information Management Unit (MIMU)](https://themimu.info/)

## Author

**Nyein Chan Ko Ko**  
GitHub: [@nchanko](https://github.com/nchanko)

## License

MIT License. Data remains property of its respective owners.