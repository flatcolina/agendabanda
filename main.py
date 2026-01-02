from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from settings import settings
from logging_config import configure_logging
import venues_router as venues
import events_router as events
import logistics_router as logistics

configure_logging(settings.LOG_LEVEL)

app = FastAPI(title="Bandas Agenda Pro API", version="1.0.0")

# CORS
origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(",") if o.strip()]
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

@app.get("/health")
def health():
    return {"ok": True}

app.include_router(venues.router, prefix="/api/venues", tags=["venues"])
app.include_router(events.router, prefix="/api/events", tags=["events"])
app.include_router(logistics.router, prefix="/api", tags=["logistics"])
