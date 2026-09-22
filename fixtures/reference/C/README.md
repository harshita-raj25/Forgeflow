# URL shortener (reference fixture, stage A)

Run: `SHORTENER_DB_PATH=links.db BASE_URL=http://localhost:8000 uvicorn app.main:app`
Test: `python -m pytest -q tests`
