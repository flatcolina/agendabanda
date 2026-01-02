# Railway Backend (FastAPI) — Eventos + Google Maps

## Rodar local
```bash
cd railway-backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edite .env e coloque GOOGLE_MAPS_API_KEY
uvicorn main:app --reload --port 8000
```

## Endpoints
- `GET /health`
- `POST /api/events`
- `GET /api/events`
- `GET /api/events/{id}`
- `PUT /api/events/{id}`
- `DELETE /api/events/{id}`
- `GET /api/geocode?address=...`
- `GET /api/distancia?origem=...&destino=...`
- `GET /api/rota?origem=...&destino=...`

## Railway
- Suba a pasta `railway-backend` como serviço.
- Variables:
  - `GOOGLE_MAPS_API_KEY`
  - (opcional) `FRONTEND_ORIGINS`
