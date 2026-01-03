import os
import sqlite3
import textwrap
from datetime import datetime
from io import BytesIO
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

APP_NAME = "AgendaBanda"
VERSION = "1.0.0"

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()
DB_PATH = os.getenv("DB_PATH", "/tmp/agendabanda.db")

def utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bands (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            city TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY,
            band_id TEXT,
            band_name TEXT NOT NULL,
            event_name TEXT NOT NULL,
            contractor_name TEXT NOT NULL,
            contact TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            address TEXT NOT NULL,
            city TEXT,
            state TEXT,
            postal_code TEXT,
            notes TEXT,
            status TEXT NOT NULL,
            lat REAL,
            lng REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_band_date ON events(band_id, date)")
    conn.commit()
    conn.close()

def row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    if not d.get("status"):
        d["status"] = "planned"
    if not d.get("created_at"):
        d["created_at"] = utc_now()
    if not d.get("updated_at"):
        d["updated_at"] = d["created_at"]
    if not d.get("band_name"):
        d["band_name"] = "Banda"
    return d

# -------- Models --------
class BandCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    city: Optional[str] = Field(default=None, max_length=80)

    @field_validator("name", "city", mode="before")
    @classmethod
    def _strip(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v

class BandItem(BaseModel):
    id: str
    name: str
    city: Optional[str] = None
    created_at: str
    updated_at: str

class EventCreate(BaseModel):
    band_id: Optional[str] = None
    event_name: str = Field(..., min_length=2, max_length=120)
    contractor_name: str = Field(..., min_length=2, max_length=120)
    contact: str = Field(..., min_length=2, max_length=120)
    date: str = Field(..., description="YYYY-MM-DD")
    time: str = Field(..., description="HH:MM")
    address: str = Field(..., min_length=5, max_length=240)

    city: Optional[str] = Field(default=None, max_length=80)
    state: Optional[str] = Field(default=None, max_length=80)
    postal_code: Optional[str] = Field(default=None, max_length=30)
    notes: Optional[str] = Field(default=None, max_length=2000)
    status: str = Field(default="planned", max_length=30)

    @field_validator(
        "band_id","event_name","contractor_name","contact","date","time","address",
        "city","state","postal_code","notes","status", mode="before"
    )
    @classmethod
    def _strip_all(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            return s if s else None
        return v

    @model_validator(mode="after")
    def _validate(self):
        if not self.band_id:
            raise ValueError("Selecione a banda (band_id).")
        if not self.status:
            self.status = "planned"
        return self

class EventItem(BaseModel):
    id: str
    band_id: Optional[str] = None
    band_name: str
    event_name: str
    contractor_name: str
    contact: str
    date: str
    time: str
    address: str
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    notes: Optional[str] = None
    status: str
    lat: Optional[float] = None
    lng: Optional[float] = None
    created_at: str
    updated_at: str

class DistanceResult(BaseModel):
    origin: str
    destination: str
    distance_text: str
    duration_text: str
    distance_meters: int
    duration_seconds: int

class ItineraryStep(BaseModel):
    from_event_id: str
    to_event_id: str
    from_label: str
    to_label: str
    distance: Optional[DistanceResult] = None

# -------- Google Maps --------
def require_maps():
    if not GOOGLE_MAPS_API_KEY:
        raise HTTPException(status_code=400, detail="GOOGLE_MAPS_API_KEY não configurada no backend.")

def geocode_address(address: str) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[str], Optional[str]]:
    require_maps()
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    r = requests.get(url, params={"address": address, "key": GOOGLE_MAPS_API_KEY}, timeout=20)
    data = r.json()
    if data.get("status") != "OK" or not data.get("results"):
        return (None, None, None, None, None)

    res = data["results"][0]
    loc = res["geometry"]["location"]
    lat, lng = loc.get("lat"), loc.get("lng")

    city = state = postal = None
    for comp in res.get("address_components", []):
        types = comp.get("types", [])
        if "locality" in types and not city:
            city = comp.get("long_name")
        if "administrative_area_level_1" in types and not state:
            state = comp.get("short_name") or comp.get("long_name")
        if "postal_code" in types and not postal:
            postal = comp.get("long_name")

    return (lat, lng, city, state, postal)

def distance_matrix(origin: str, destination: str) -> DistanceResult:
    require_maps()
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    r = requests.get(url, params={
        "origins": origin,
        "destinations": destination,
        "key": GOOGLE_MAPS_API_KEY,
        "mode": "driving",
        "language": "pt-BR",
        "region": "br",
    }, timeout=20)
    data = r.json()
    if data.get("status") != "OK":
        raise HTTPException(status_code=400, detail=f"DistanceMatrix status={data.get('status')}")
    row0 = (data.get("rows") or [{}])[0]
    el0 = (row0.get("elements") or [{}])[0]
    if el0.get("status") != "OK":
        raise HTTPException(status_code=400, detail=f"DistanceMatrix element status={el0.get('status')}")
    dist = el0["distance"]
    dur = el0["duration"]
    return DistanceResult(
        origin=origin,
        destination=destination,
        distance_text=dist.get("text", ""),
        duration_text=dur.get("text", ""),
        distance_meters=int(dist.get("value", 0)),
        duration_seconds=int(dur.get("value", 0)),
    )

# -------- FastAPI app --------
app = FastAPI(title="AgendaBanda API", version=VERSION)

frontend_origins = [o.strip() for o in os.getenv("FRONTEND_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

def add_cors_headers(request: Request, response: Response):
    origin = request.headers.get("origin")
    response.headers["Access-Control-Allow-Origin"] = origin if origin else "*"
    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = request.headers.get("access-control-request-headers", "*")
    response.headers["Access-Control-Expose-Headers"] = "*"
    return response

@app.exception_handler(RequestValidationError)
async def validation_exc(request: Request, exc: RequestValidationError):
    res = JSONResponse(status_code=422, content={"detail": exc.errors()})
    return add_cors_headers(request, res)

@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    res = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return add_cors_headers(request, res)

@app.exception_handler(Exception)
async def any_exc(request: Request, exc: Exception):
    res = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    return add_cors_headers(request, res)

@app.options("/{full_path:path}")
def preflight(full_path: str, request: Request):
    return add_cors_headers(request, Response(status_code=200))

@app.on_event("startup")
def on_startup():
    init_db()

@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME, "version": VERSION, "db_path": DB_PATH}

# -------- Bands --------
@app.get("/api/bands", response_model=List[BandItem])
def list_bands():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM bands ORDER BY name ASC").fetchall()
    conn.close()
    return [BandItem(**row_to_dict(r)) for r in rows]

@app.post("/api/bands", response_model=BandItem)
def create_band(payload: BandCreate):
    conn = get_conn()
    cur = conn.cursor()
    now = utc_now()
    band_id = f"band_{int(datetime.utcnow().timestamp()*1000)}"
    cur.execute(
        "INSERT INTO bands(id,name,city,created_at,updated_at) VALUES(?,?,?,?,?)",
        (band_id, payload.name, payload.city, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM bands WHERE id=?", (band_id,)).fetchone()
    conn.close()
    return BandItem(**row_to_dict(row))

@app.delete("/api/bands/{band_id}")
def delete_band(band_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM events WHERE band_id=?", (band_id,))
    cur.execute("DELETE FROM bands WHERE id=?", (band_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# -------- Events --------
def resolve_band_name(conn: sqlite3.Connection, band_id: str) -> str:
    row = conn.execute("SELECT name FROM bands WHERE id=?", (band_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=400, detail="Banda não encontrada. Cadastre/seleciona uma banda válida.")
    name = (row["name"] or "").strip()
    return name if name else "Banda"

@app.get("/api/events", response_model=List[EventItem])
def list_events(
    date: Optional[str] = Query(default=None),
    band_id: Optional[str] = Query(default=None),
    city: Optional[str] = Query(default=None),
):
    conn = get_conn()
    q = "SELECT * FROM events WHERE 1=1"
    params: List[Any] = []
    if date:
        q += " AND date=?"
        params.append(date)
    if band_id:
        q += " AND band_id=?"
        params.append(band_id)
    if city:
        q += " AND lower(city)=lower(?)"
        params.append(city)
    q += " ORDER BY date ASC, time ASC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [EventItem(**row_to_dict(r)) for r in rows]

@app.post("/api/events", response_model=EventItem)
def create_event(payload: EventCreate):
    conn = get_conn()
    cur = conn.cursor()
    now = utc_now()
    eid = f"evt_{int(datetime.utcnow().timestamp()*1000)}"
    band_name = resolve_band_name(conn, payload.band_id)

    lat = lng = None
    g_city = g_state = g_postal = None
    try:
        lat, lng, g_city, g_state, g_postal = geocode_address(payload.address)
    except Exception:
        pass

    city = payload.city or g_city
    state = payload.state or g_state
    postal = payload.postal_code or g_postal

    cur.execute(
        """
        INSERT INTO events(
            id, band_id, band_name, event_name, contractor_name, contact,
            date, time, address, city, state, postal_code, notes, status,
            lat, lng, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            eid, payload.band_id, band_name, payload.event_name, payload.contractor_name, payload.contact,
            payload.date, payload.time, payload.address, city, state, postal, payload.notes, payload.status or "planned",
            lat, lng, now, now
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone()
    conn.close()
    return EventItem(**row_to_dict(row))

@app.delete("/api/events/{event_id}")
def delete_event(event_id: str):
    conn = get_conn()
    conn.execute("DELETE FROM events WHERE id=?", (event_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# -------- Distance / Itinerary --------
@app.get("/api/distancia", response_model=DistanceResult)
def distancia(origem: str = Query(...), destino: str = Query(...)):
    return distance_matrix(origem, destino)

@app.get("/api/itinerary", response_model=List[ItineraryStep])
def itinerary(band_id: str = Query(...), date: str = Query(...)):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM events WHERE band_id=? AND date=? ORDER BY time ASC",
        (band_id, date),
    ).fetchall()
    events = [EventItem(**row_to_dict(r)) for r in rows]
    conn.close()

    if len(events) < 2:
        return []

    steps: List[ItineraryStep] = []
    for i in range(len(events) - 1):
        a = events[i]
        b = events[i + 1]
        label_a = f"{a.time} • {a.event_name} • {a.address}"
        label_b = f"{b.time} • {b.event_name} • {b.address}"
        dist = None
        try:
            dist = distance_matrix(a.address, b.address)
        except Exception:
            dist = None
        steps.append(
            ItineraryStep(
                from_event_id=a.id,
                to_event_id=b.id,
                from_label=label_a,
                to_label=label_b,
                distance=dist,
            )
        )
    return steps

# -------- Reports (PDF) --------
def fetch_events_for_report(conn: sqlite3.Connection, band_id: Optional[str], date: Optional[str], city: Optional[str]) -> List[Dict[str, Any]]:
    q = "SELECT * FROM events WHERE 1=1"
    params: List[Any] = []
    if band_id:
        q += " AND band_id=?"
        params.append(band_id)
    if date:
        q += " AND date=?"
        params.append(date)
    if city:
        q += " AND lower(city)=lower(?)"
        params.append(city)
    q += " ORDER BY date ASC, time ASC"
    rows = conn.execute(q, params).fetchall()
    return [row_to_dict(r) for r in rows]

@app.get("/api/reports/pdf")
def report_pdf(
    band_id: Optional[str] = Query(default=None),
    date: Optional[str] = Query(default=None),
    city: Optional[str] = Query(default=None),
    mode: str = Query(default="list", description="list | itinerary"),
):
    conn = get_conn()
    items = fetch_events_for_report(conn, band_id, date, city)
    conn.close()

    itinerary_steps: List[ItineraryStep] = []
    if mode == "itinerary" and band_id and date:
        itinerary_steps = itinerary(band_id=band_id, date=date)  # type: ignore

    buff = BytesIO()
    c = canvas.Canvas(buff, pagesize=A4)
    w, h = A4

    y = h - 48
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, y, f"Relatório • {APP_NAME}")
    y -= 18

    c.setFont("Helvetica", 10)
    filters = []
    if band_id:
        filters.append(f"Banda: {band_id}")
    if date:
        filters.append(f"Data: {date}")
    if city:
        filters.append(f"Cidade: {city}")
    c.drawString(40, y, " | ".join(filters) if filters else "Sem filtros")
    y -= 22

    c.setFont("Helvetica-Bold", 11)
    c.drawString(40, y, "Eventos")
    y -= 14
    c.setFont("Helvetica", 9)

    for ev in items:
        line = f"{ev.get('date')} {ev.get('time')} • {ev.get('band_name')} • {ev.get('event_name')} • {ev.get('city') or '-'} • {ev.get('address')}"
        for chunk in textwrap.wrap(line, width=110):
            c.drawString(40, y, chunk)
            y -= 12
            if y < 60:
                c.showPage()
                y = h - 48
                c.setFont("Helvetica", 9)
        y -= 4

    if itinerary_steps:
        if y < 140:
            c.showPage()
            y = h - 48
        c.setFont("Helvetica-Bold", 11)
        c.drawString(40, y, "Itinerário (rotas)")
        y -= 14
        c.setFont("Helvetica", 9)
        for st in itinerary_steps:
            dist = st.distance.model_dump() if st.distance else {}
            line = f"{st.from_label} -> {st.to_label} | {dist.get('distance_text','-')} • {dist.get('duration_text','-')}"
            for chunk in textwrap.wrap(line, width=110):
                c.drawString(40, y, chunk)
                y -= 12
                if y < 60:
                    c.showPage()
                    y = h - 48
                    c.setFont("Helvetica", 9)
            y -= 4

    c.showPage()
    c.save()
    buff.seek(0)

    return StreamingResponse(
        buff,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="relatorio_agendabanda.pdf"'},
    )
