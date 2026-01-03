import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from uuid import uuid4

import requests
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm
except Exception:
    canvas = None  # reportlab optional (for PDF)

APP_NAME = "Hospediou Events + Google Maps"
DB_PATH = os.getenv("DB_PATH", "/tmp/hospediou_events.db")
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()

_db_lock = threading.Lock()

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table});")
    cols = [r[1] for r in cur.fetchall()]
    return column in cols

def init_db() -> None:
    with _db_lock:
        conn = _get_conn()
        try:
            # Bands
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bands (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    city TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

            # Events
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    band_id TEXT,
                    band_name TEXT NOT NULL,
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

            # Migrations for older DBs
            if not _table_has_column(conn, "events", "band_id"):
                conn.execute("ALTER TABLE events ADD COLUMN band_id TEXT;")
                conn.commit()
            if not _table_has_column(conn, "events", "band_name"):
                conn.execute("ALTER TABLE events ADD COLUMN band_name TEXT;")
                conn.commit()

# Preenche band_name em eventos antigos que ficaram NULL após adicionar a coluna
conn.execute("UPDATE events SET band_name='Banda' WHERE band_name IS NULL OR TRIM(band_name)='';")
conn.commit()
        finally:
            conn.close()

# ----------------- Models -----------------

class BandBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    city: Optional[str] = Field(default=None, max_length=80)
    notes: Optional[str] = Field(default=None, max_length=1000)

class BandCreate(BandBase):
    pass

class Band(BandBase):
    id: str
    created_at: str
    updated_at: str

class EventBase(BaseModel):
    # Selecione uma banda cadastrada (band_id) OU preencha o nome (band_name)
    band_id: Optional[str] = Field(default=None, description="ID da banda (se cadastrado)")
    band_name: Optional[str] = Field(default=None, max_length=120, description="Nome da banda/artista (texto)")

    event_name: str = Field(..., min_length=2, max_length=120)
    contractor_name: str = Field(..., min_length=2, max_length=120)
    contact: str = Field(..., min_length=2, max_length=120, description="Phone/WhatsApp/email")
    date: str = Field(..., description="YYYY-MM-DD")
    time: str = Field(..., description="HH:MM")
    address: str = Field(..., min_length=5, max_length=240)

    city: Optional[str] = Field(default=None, max_length=80)
    state: Optional[str] = Field(default=None, max_length=80)
    postal_code: Optional[str] = Field(default=None, max_length=30)
    notes: Optional[str] = Field(default=None, max_length=2000)
    status: str = Field(default="planned", max_length=30)

    @field_validator("band_id", "band_name", "city", "state", "postal_code", "notes", "status", mode="before")
    @classmethod
    def _empty_to_none(cls, v, info):
        if v is None:
            return None
        if isinstance(v, str):
            v2 = v.strip()
            if v2 == "":
                return "planned" if getattr(info, 'field_name', '') == 'status' else None
            return v2
        return v

    @model_validator(mode="after")
    def _check_band(self):
        if not self.band_id and not self.band_name:
            raise ValueError("Informe a banda: selecione uma banda cadastrada ou preencha o nome da banda.")
        # normalize status fallback
        if not self.status:
            self.status = "planned"
        return self

class EventCreate(EventBase):
    pass

class EventUpdate(BaseModel):
    band_id: Optional[str] = None
    band_name: Optional[str] = Field(default=None, min_length=2, max_length=120)

    event_name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    contractor_name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    contact: Optional[str] = Field(default=None, min_length=2, max_length=120)
    date: Optional[str] = None
    time: Optional[str] = None
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

app = FastAPI(title=APP_NAME, version="4.1.1")

def _add_cors_headers(request: Request, response: Response):
    origin = request.headers.get("origin")
    response.headers["Access-Control-Allow-Origin"] = origin if origin else "*"
    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = request.headers.get("access-control-request-headers", "*")
    response.headers["Access-Control-Expose-Headers"] = "*"
    return response

@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(request: Request, exc: RequestValidationError):
    res = JSONResponse(status_code=422, content={"detail": exc.errors()})
    return _add_cors_headers(request, res)

@app.exception_handler(HTTPException)
async def _http_exc_handler(request: Request, exc: HTTPException):
    res = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return _add_cors_headers(request, res)

@app.exception_handler(Exception)
async def _any_exc_handler(request: Request, exc: Exception):
    res = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    return _add_cors_headers(request, res)


# --- CORS (para Netlify) ---
# Libera durante testes. Depois a gente trava só para o seu domínio.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
    max_age=86400,
)

@app.middleware("http")
async def _force_cors_headers(request: Request, call_next):
    # Garante headers mesmo em respostas de erro
    response = await call_next(request)
    origin = request.headers.get("origin")
    response.headers["Access-Control-Allow-Origin"] = origin if origin else "*"
    response.headers["Vary"] = "Origin"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = request.headers.get("access-control-request-headers", "*")
    return response

@app.options("/{full_path:path}")
def _preflight(full_path: str, request: Request):
    return Response(status_code=200)

@app.get("/debug/cors")
def debug_cors(request: Request):
    return {
        "ok": True,
        "origin": request.headers.get("origin"),
        "note": "CORS habilitado (teste): Access-Control-Allow-Origin será ecoado quando houver Origin.",
    }


# CORS (modo teste / Netlify)
# Forçamos CORS permissivo para evitar bloqueio no browser durante ajustes.
# Depois que estiver 100%, você pode restringir (eu faço pra você).
# Fallback extra: garante header mesmo em respostas de erro / 404.

# Preflight handler (OPTIONS) para qualquer rota


@app.on_event("startup")
def _startup():
    init_db()

@app.get("/health")
def health():
    return {"ok": True, "app": APP_NAME, "version": "4.1.1"}

@app.get("/debug/cors")
def debug_cors(origin: str | None = None):
    # origin param é opcional; o navegador envia o header Origin automaticamente
    return {
        "allow_origins_env": os.getenv("FRONTEND_ORIGINS", ""),
        "allow_origin_regex_env": os.getenv("FRONTEND_ORIGIN_REGEX", ""),
        "note": "Use o DevTools/Network para ver Access-Control-Allow-Origin no response.",
        "origin_param": origin,
    }

# ----------------- Helpers -----------------

def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)

    # Normalizações para compatibilidade com DB antigo (evita 500 na serialização)
    if d.get("status") in (None, ""):
        d["status"] = "planned"

    # band_name pode vir NULL em DB antigo (coluna adicionada por migration)
    if d.get("band_name") in (None, ""):
        d["band_name"] = "Banda"

    # timestamps obrigatórios nos modelos de resposta
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    if d.get("created_at") in (None, ""):
        d["created_at"] = now
    if d.get("updated_at") in (None, ""):
        d["updated_at"] = d.get("created_at") or now

    # strings vazias -> None para campos opcionais
    for k in ["band_id", "band_name", "city", "state", "postal_code", "notes"]:
        if k in d and isinstance(d[k], str) and d[k].strip() == "":
            d[k] = None

    return d

def _require_key():
    if not GOOGLE_MAPS_API_KEY:
        raise HTTPException(status_code=500, detail="GOOGLE_MAPS_API_KEY não configurada no Railway (Variables).")

def _distance_matrix_single(origem: str, destino: str, mode: str = "driving") -> Dict[str, Any]:
    _require_key()
    url = "https://maps.googleapis.com/maps/api/distancematrix/json"
    r = requests.get(
        url,
        params={
            "origins": origem,
            "destinations": destino,
            "mode": mode,
            "language": "pt-BR",
            "region": "br",
            "key": GOOGLE_MAPS_API_KEY,
        },
        timeout=20,
    )
    data = r.json()
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

def _resolve_band_name(conn: sqlite3.Connection, band_id: Optional[str], band_name_fallback: str) -> str:
    if not band_id:
        return band_name_fallback
    row = conn.execute("SELECT name FROM bands WHERE id = ?", (band_id,)).fetchone()
    return (row["name"] if row and row.get("name") else band_name_fallback)

# ----------------- Bands CRUD -----------------

@app.get("/api/bands", response_model=List[Band])
def list_bands(limit: int = Query(default=500, ge=1, le=5000)):
    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute("SELECT * FROM bands ORDER BY LOWER(name) ASC LIMIT ?", (limit,)).fetchall()
        finally:
            conn.close()
    return [_row_to_dict(r) for r in rows]

@app.post("/api/bands", response_model=Band)
def create_band(payload: BandCreate):
    band_id = str(uuid4())
    now = _now_iso()
    name = payload.name.strip()

    with _db_lock:
        conn = _get_conn()
        try:
            # enforce unique (case-insensitive) in app layer too
            exists = conn.execute("SELECT id FROM bands WHERE LOWER(name)=LOWER(?)", (name,)).fetchone()
            if exists:
                raise HTTPException(status_code=400, detail="Banda já cadastrada.")
            conn.execute(
                """
                INSERT INTO bands (id, name, city, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (band_id, name, payload.city, payload.notes, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    return {"id": band_id, "name": name, "city": payload.city, "notes": payload.notes, "created_at": now, "updated_at": now}

@app.delete("/api/bands/{band_id}")
def delete_band(band_id: str):
    with _db_lock:
        conn = _get_conn()
        try:
            # keep events intact; only detach band_id
            conn.execute("UPDATE events SET band_id = NULL WHERE band_id = ?", (band_id,))
            cur = conn.execute("DELETE FROM bands WHERE id = ?", (band_id,))
            conn.commit()
        finally:
            conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Banda não encontrada.")
    return {"ok": True}

# ----------------- Events CRUD -----------------

@app.post("/api/events", response_model=Event)
def create_event(payload: EventCreate):
    event_id = str(uuid4())
    now = _now_iso()

    with _db_lock:
        conn = _get_conn()
        try:
            band_name = _resolve_band_name(conn, payload.band_id, (payload.band_name or '')).strip()
            if not band_name:
                raise HTTPException(status_code=400, detail='Banda é obrigatória.')
            conn.execute(
                """
                INSERT INTO events (
                    id, band_id, band_name, event_name, contractor_name, contact, date, time, address,
                    city, state, postal_code, notes, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    payload.band_id,
                    band_name,
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

    data = payload.model_dump()
    data["band_name"] = band_name
    return {"id": event_id, "created_at": now, "updated_at": now, **data}

@app.get("/api/events", response_model=List[Event])
def list_events(limit: int = Query(default=2000, ge=1, le=5000)):
    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY date ASC, time ASC, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
    return [_row_to_dict(r) for r in rows]

@app.get("/api/events/{event_id}", response_model=Event)
def get_event(event_id: str):
    with _db_lock:
        conn = _get_conn()
        try:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        finally:
            conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Event not found")
    return _row_to_dict(row)

@app.put("/api/events/{event_id}", response_model=Event)
def update_event(event_id: str, payload: EventUpdate):
    now = _now_iso()
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")

    with _db_lock:
        conn = _get_conn()
        try:
            if "band_id" in data or "band_name" in data:
                bid = data.get("band_id")
                bname = data.get("band_name")
                # if band_id changed, resolve official name; else keep provided
                if "band_id" in data:
                    resolved = _resolve_band_name(conn, bid, bname or "")
                    data["band_name"] = resolved.strip() if resolved else (bname or "")
                elif "band_name" in data:
                    data["band_name"] = data["band_name"].strip()

            set_parts = [f"{k} = ?" for k in data.keys()]
            params = list(data.values())
            set_parts.append("updated_at = ?")
            params.append(now)
            params.append(event_id)

            cur = conn.execute(f"UPDATE events SET {', '.join(set_parts)} WHERE id = ?", tuple(params))
            conn.commit()
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="Event not found")
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        finally:
            conn.close()

    return _row_to_dict(row)

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

# ----------------- Google Maps -----------------

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
    origem: str = Query(..., min_length=3),
    destino: str = Query(..., min_length=3),
    mode: str = Query(default="driving"),
):
    try:
        return _distance_matrix_single(origem, destino, mode=mode)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Erro ao chamar Distance Matrix: {e}")

# ----------------- Reports -----------------

@app.get("/api/reports/events", response_model=List[Event])
def report_events(
    band: Optional[str] = Query(default=None),
    date: Optional[str] = Query(default=None),
    city: Optional[str] = Query(default=None),
    limit: int = Query(default=5000, ge=1, le=10000),
):
    where = []
    params: List[Any] = []
    if band:
        where.append("LOWER(band_name) = LOWER(?)")
        params.append(band)
    if date:
        where.append("date = ?")
        params.append(date)
    if city:
        where.append("LOWER(COALESCE(city,'')) = LOWER(?)")
        params.append(city)

    sql = "SELECT * FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date ASC, time ASC, created_at DESC LIMIT ?"
    params.append(limit)

    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()

    return [_row_to_dict(r) for r in rows]

@app.get("/api/reports/itinerary")
def report_itinerary(
    band: str = Query(..., min_length=2),
    date: str = Query(..., min_length=10),
    mode: str = Query(default="driving"),
):
    with _db_lock:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM events WHERE LOWER(band_name)=LOWER(?) AND date=? ORDER BY time ASC",
                (band, date),
            ).fetchall()
        finally:
            conn.close()

    events = [_row_to_dict(r) for r in rows]
    legs: List[Dict[str, Any]] = []

    if len(events) >= 2:
        for i in range(len(events) - 1):
            a = events[i]
            b = events[i + 1]
            try:
                dist = _distance_matrix_single(a["address"], b["address"], mode=mode)
            except HTTPException as he:
                dist = {"ok": False, "status": "ERROR", "detail": he.detail}
            legs.append(
                {
                    "from_event_id": a["id"],
                    "to_event_id": b["id"],
                    "from_time": a["time"],
                    "to_time": b["time"],
                    "from_address": a["address"],
                    "to_address": b["address"],
                    "distance": dist,
                }
            )

    return {"ok": True, "band": band, "date": date, "events": events, "legs": legs}

def _pdf_bytes(title: str, lines: List[str]) -> bytes:
    if canvas is None:
        raise HTTPException(status_code=500, detail="PDF não disponível (dependência reportlab ausente).")

    import io
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4

    x = 18 * mm
    y = height - 18 * mm

    c.setTitle(title)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x, y, title)
    y -= 10 * mm

    c.setFont("Helvetica", 10)
    for line in lines:
        if y < 18 * mm:
            c.showPage()
            y = height - 18 * mm
            c.setFont("Helvetica", 10)
        c.drawString(x, y, line[:140])
        y -= 6 * mm

    c.showPage()
    c.save()
    return buf.getvalue()

@app.get("/api/reports/pdf")
def report_pdf(
    band: Optional[str] = Query(default=None),
    date: Optional[str] = Query(default=None),
    city: Optional[str] = Query(default=None),
):
    title = "Relatório de Eventos"
    filters = []
    if band:
        filters.append(f"Banda: {band}")
    if date:
        filters.append(f"Data: {date}")
    if city:
        filters.append(f"Cidade: {city}")
    if filters:
        title += " (" + " | ".join(filters) + ")"

    events = report_events(band=band, date=date, city=city, limit=10000)

    lines: List[str] = []
    lines.append("Filtros: " + (", ".join(filters) if filters else "nenhum"))
    lines.append(" ")
    lines.append("Eventos:")
    if not events:
        lines.append(" - (nenhum)")
    else:
        for ev in events:
            lines.append(f" - {ev['date']} {ev['time']} | {ev['band_name']} | {ev['event_name']} | {ev.get('city') or '-'} | {ev['address']}")

    if band and date:
        it = report_itinerary(band=band, date=date)
        lines.append(" ")
        lines.append("Roteiro (do 1º show ao próximo):")
        if not it["legs"]:
            lines.append(" - (apenas 1 evento no dia)")
        else:
            for leg in it["legs"]:
                d = leg["distance"]
                if d.get("ok"):
                    lines.append(f" - {leg['from_time']} -> {leg['to_time']} | {d['distance_text']} | {d['duration_text']}")
                else:
                    lines.append(f" - {leg['from_time']} -> {leg['to_time']} | (falha ao calcular)")

    pdf = _pdf_bytes(title, lines)
    filename = "relatorio_eventos.pdf"
    return StreamingResponse(
        iter([pdf]),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
