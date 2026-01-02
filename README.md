# Railway Backend (FastAPI) — Eventos + Bandas + Google Maps + Relatórios

## Variáveis (Railway)
- `GOOGLE_MAPS_API_KEY` (obrigatório para rotas/distância)
- (opcional) `FRONTEND_ORIGINS` (para travar CORS)

## Endpoints
### Bandas
- `GET /api/bands`
- `POST /api/bands`
- `DELETE /api/bands/{band_id}`

### Eventos
- `POST /api/events`
- `GET /api/events`
- `PUT /api/events/{id}`
- `DELETE /api/events/{id}`

### Relatórios
- `GET /api/reports/itinerary?band=...&date=YYYY-MM-DD`
- `GET /api/reports/pdf?band=&date=&city=`

## Observação
Se você já tinha banco antigo, o backend faz migração leve e mantém os dados.
