# AgendaBandas API (Railway)

FastAPI + Firebase Admin + Google Maps (Geocoding/Routes) para:
- Geocodificar locais (endereços -> lat/lng)
- Recalcular logística do dia (distância/tempo entre eventos)

## Variáveis de ambiente (Railway)
- FIREBASE_SERVICE_ACCOUNT_JSON
- GOOGLE_MAPS_API_KEY
- ALLOWED_ORIGINS

## Run local
pip install -r requirements.txt
uvicorn main:app --reload --host 0.0.0.0 --port 8000
