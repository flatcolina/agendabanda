import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from uuid import uuid4

import requests
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

APP_NAME = "Hospediou Events + Google Maps"
DB_PATH = os.getenv("DB_PATH", "app.db")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

_db_lock = threading.Lock()

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    event_name TEXT NOT NULL,
                    contractor_name TEXT NOT NULL,
                    contact TEXT NOT NULL,
                    date TEXT NOT NULL,         -- YYYY-MM-DD
                    time TEXT NOT NULL,         -- HH:MM
                    address TEXT NOT NULL,      -- free text
                    city TEXT,
                    state TEXT,
                    postal_code TEXT,
                    notes TEXT,
                    status TEXT NOT NULL DEFAULT 'planned',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

class EventBase(BaseModel):
    event_name: str = Field(..., min_length=2, max_length=120)
    contractor_name: str = Field(..., min_length=2, max_length=120)
    contact: str = Field(..., min_length=2, max_length=120, description="Phone/WhatsApp/email")
    date: str = Field(..., description="YYYY-MM-DD")
    time: str = Field(..., description="HH:MM")
    address: str = Field(..., min_length=5, max_length=240)

    # extras úteis (opcionais)
    city: Optional[str] = Field(default=None, max_length=80)
    state: Optional[str] = Field(default=None, max_length=80)
    postal_code: Optional[str] = Field(default=None, max_length=30)
    notes: Optional[str] = Field(default=None, max_length=2000)
    status: str = Field(default="planned", max_length=30)

class EventCreate(EventBase):
    pass

class EventUpdate(BaseModel):
    event_name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    contractor_name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    contact: Optional[str] = Field(default=None, min_length=2, max_length=120)
    date: Optional[str] = Field(default=None)
    time: Optional[str] = Field(default=None)
    address: Optional[str] = Field(default=None, min_length=5, max_length=240)

    city: Optional[str] = Field(default=None, max_length=80)
    state: Optional[str] = Field(default=None, max_length=80)
    postal_code: Optional[str] = Field(default=None, max_length=30)
    notes: Optional[str] = Field(default=None, max_length=2000)
    status: Optional[str] = Field(default=None, max_length=30)

class Event(EventBase):
    id: str
    created_at: str
    updated_at: str

app = FastAPI(title=APP_NAME, version="1.0.0")

# CORS (Netlify -> Railway)
origins_env = os.getenv("FRONTEND_ORIGINS", "").strip()
if origins_env:
    allow_origins = [o.strip() for o in origins_env.split(",") if o.strip()]
else:
    # dev friendly; troque por seus domínios quando quiser "travar"
    allow_origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def _startup():
    init_db()

@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME}

# ---------- EVENTS CRUD ----------

def _row_to_event(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)

@app.post("/api/events", response_model=Event)
def create_event(payload: EventCreate):
    event_id = str(uuid4())
    now = _now_iso()

    with _db_lock:
        conn = _get_conn()
        try:
            conn.execute(
                """
                INSERT INTO events (
                    id, event_name, contractor_name, contact, date, time, address,
                    city, state, postal_code, notes, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    payload.event_name,
                    payload.contractor_name,
                    payload.contact,
                    payload.date,
                    payload.time,
                    payload.address,
                    payload.city,
                    payload.state,
                    payload.postal_code,
                    payload.notes,
                    payload.status or "planned",
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    return {
        "id": event_id,
        "created_at": now,
        "updated_at": now,
        **payload.model_dump(),
    }

@app.get("/api/events", response_model=List[Event])
def list_events(limit: int = Query(default=100, ge=1, le=500)):
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                "SELECT * FROM events ORDER BY date ASC, time ASC, created_at DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    return [_row_to_event(r) for r in rows]

@app.get("/api/events/{event_id}", response_model=Event)
def get_event(event_id: str):
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = cur.fetchone()
        finally:
            conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    return _row_to_event(row)

@app.put("/api/events/{event_id}", response_model=Event)
def update_event(event_id: str, payload: EventUpdate):
    now = _now_iso()
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")

    # build dynamic update query
    set_parts = [f"{k} = ?" for k in data.keys()]
    params = list(data.values())
    set_parts.append("updated_at = ?")
    params.append(now)
    params.append(event_id)

    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.execute(
                f"UPDATE events SET {', '.join(set_parts)} WHERE id = ?",
                tuple(params),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Event not found")
            cur2 = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = cur2.fetchone()
        finally:
            conn.close()

    return _row_to_event(row)

@app.delete("/api/events/{event_id}")
def delete_event(event_id: str):
    with _db_lock:
        conn = _get_conn()
        try:
            cur = conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            conn.commit()
        finally:
            conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Event not found")
    return {"ok": True}

# ---------- GOOGLE MAPS HELPERS ----------

def _require_key():
    if not GOOGLE_MAPS_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="GOOGLE_MAPS_API_KEY não configurada no Railway (Variables).",
        )

@app.get("/api/geocode")
def geocode(address: str = Query(..., min_length=5)):
    _require_key()
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    try:
        r = requests.get(url, params={"address": address, "key": GOOGLE_MAPS_API_KEY}, timeout=20)
        data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erro ao chamar Geocoding: {e}")

    if data.get("status") != "OK" or not data.get("results"):
        return {"ok": False, "status": data.get("status"), "results": []}

    top = data["results"][0]
    loc = top["geometry"]["location"]
    return {
        "ok": True,
        "formatted_address": top.get("formatted_address"),
        "place_id": top.get("place_id"),
        "lat": loc.get("lat"),
        "lng": loc.get("lng"),
        "raw_status": data.get("status"),
    }

@app.get("/api/distancia")
def distance_matrix(
    origem: str = Query(..., min_length=3, description="Endereço texto (origem)"),
    destino: str = Query(..., min_length=3, description="Endereço texto (destino)"),
    mode: str = Query(default="driving", description="driving|walking|bicycling|transit"),
    language: str = Query(default="pt-BR"),
    region: str = Query(default="br"),
):
    _require_key()
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    try:
        r = requests.get(
            url,
            params={
                "origins": origem,
                "destinations": destino,
                "mode": mode,
                "language": language,
                "region": region,
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=20,
        )
        data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erro ao chamar Distance Matrix: {e}")

    if data.get("status") != "OK":
        return {"ok": False, "status": data.get("status"), "raw": data}

    rows = data.get("rows") or []
    if not rows or not rows[0].get("elements"):
        return {"ok": False, "status": "NO_ELEMENTS", "raw": data}

    el = rows[0]["elements"][0]
    if el.get("status") != "OK":
        return {"ok": False, "status": el.get("status"), "raw": data}

    return {
        "ok": True,
        "origem": data.get("origin_addresses", [origem])[0],
        "destino": data.get("destination_addresses", [destino])[0],
        "distance_text": el["distance"]["text"],
        "distance_meters": el["distance"]["value"],
        "duration_text": el["duration"]["text"],
        "duration_seconds": el["duration"]["value"],
        "mode": mode,
    }

@app.get("/api/rota")
def directions(
    origem: str = Query(..., min_length=3),
    destino: str = Query(..., min_length=3),
    mode: str = Query(default="driving"),
    language: str = Query(default="pt-BR"),
    region: str = Query(default="br"),
):
    _require_key()
    url = "https://maps.googleapis.com/maps/api/directions/json"
    try:
        r = requests.get(
            url,
            params={
                "origin": origem,
                "destination": destino,
                "mode": mode,
                "language": language,
                "region": region,
                "key": GOOGLE_MAPS_API_KEY,
            },
            timeout=20,
        )
        data = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erro ao chamar Directions: {e}")

    if data.get("status") != "OK" or not data.get("routes"):
        return {"ok": False, "status": data.get("status"), "raw": data}

    route = data["routes"][0]
    leg = route["legs"][0] if route.get("legs") else {}
    return {
        "ok": True,
        "origem": leg.get("start_address"),
        "destino": leg.get("end_address"),
        "distance_text": (leg.get("distance") or {}).get("text"),
        "duration_text": (leg.get("duration") or {}).get("text"),
        "polyline": (route.get("overview_polyline") or {}).get("points"),
        "raw_status": data.get("status"),
    }
