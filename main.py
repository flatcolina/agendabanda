import os
import json
import base64
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

import firebase_admin
from firebase_admin import credentials, firestore

APP_NAME = "AgendaBanda"
VERSION = "2.0.0"

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip()

# Aceita qualquer uma dessas envs (você disse que já tem o JSON no Railway):
SERVICE_ACCOUNT_ENV_CANDIDATES = [
    "FIREBASE_SERVICE_ACCOUNT_JSON",
    "FIREBASE_ADMIN_SDK_JSON",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "FIREBASE_CREDENTIALS_JSON",
]

BANDS_COLLECTION = os.getenv("FIRESTORE_BANDS_COLLECTION", "bands")
EVENTS_COLLECTION = os.getenv("FIRESTORE_EVENTS_COLLECTION", "events")


def utc_now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


def require_maps():
    if not GOOGLE_MAPS_API_KEY:
        raise HTTPException(status_code=400, detail="GOOGLE_MAPS_API_KEY não configurada no backend.")


def _get_service_account_json_raw() -> str:
    for k in SERVICE_ACCOUNT_ENV_CANDIDATES:
        v = os.getenv(k, "")
        if v and v.strip():
            return v.strip()

    # fallback: caminho em arquivo, se preferir usar
    path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def _parse_service_account(raw: str) -> Dict[str, Any]:
    s = (raw or "").strip()
    if not s:
        raise RuntimeError(
            "Credenciais Firebase não encontradas. Defina a env FIREBASE_SERVICE_ACCOUNT_JSON com o JSON da service account."
        )

    # Remove aspas externas (alguns painéis salvam como string com aspas)
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        s = s[1:-1].strip()

    # Suporte opcional: base64:....
    if s.lower().startswith("base64:"):
        b = s.split(":", 1)[1].strip()
        try:
            s = base64.b64decode(b).decode("utf-8").strip()
        except Exception as e:
            raise RuntimeError(f"Falha ao decodificar credencial base64: {e}")

    try:
        data = json.loads(s)
    except Exception:
        # Algumas plataformas escapam a string inteira; tenta decodificar escapes
        try:
            data = json.loads(s.encode("utf-8").decode("unicode_escape"))
        except Exception as e:
            raise RuntimeError(f"JSON inválido em FIREBASE_SERVICE_ACCOUNT_JSON: {e}")

    if not isinstance(data, dict):
        raise RuntimeError("FIREBASE_SERVICE_ACCOUNT_JSON precisa ser um objeto JSON (dict).")

    # Conserto comum: private_key vem com \n literal — precisamos de newlines reais
    pk = data.get("private_key")
    if isinstance(pk, str):
        pk = pk.replace("\\\\n", "\\n")  # double-escape -> single
        pk = pk.replace("\\n", "\n")     # literal -> newline real
        data["private_key"] = pk

    return data


def init_firebase() -> firestore.Client:
    if firebase_admin._apps:
        return firestore.client()

    raw = _get_service_account_json_raw()
    data = _parse_service_account(raw)

    try:
        cred = credentials.Certificate(data)
        firebase_admin.initialize_app(cred)
        return firestore.client()
    except Exception as e:
        proj = data.get("project_id") if isinstance(data, dict) else None
        hint = (
            "Falha ao inicializar Firebase. Verifique se FIREBASE_SERVICE_ACCOUNT_JSON é o JSON de service account "
            "e se o campo private_key está intacto (com \n)."
        )
        raise RuntimeError(f"{hint} project_id={proj} error={e}")

    raw = _get_service_account_json()
    if not raw:
        raise RuntimeError(
            "Credenciais Firebase não encontradas. Defina uma env com o JSON da service account "
            "(ex: FIREBASE_SERVICE_ACCOUNT_JSON) ou GOOGLE_APPLICATION_CREDENTIALS."
        )

    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError(f"JSON inválido nas credenciais Firebase: {e}")

    cred = credentials.Certificate(data)
    firebase_admin.initialize_app(cred)
    return firestore.client()


def db() -> firestore.Client:
    try:
        return init_firebase()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def row_defaults(d: Dict[str, Any]) -> Dict[str, Any]:
    if not d.get("status"):
        d["status"] = "planned"
    if not d.get("created_at"):
        d["created_at"] = utc_now()
    if not d.get("updated_at"):
        d["updated_at"] = d["created_at"]
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


@app.get("/health")
def health():
    _ = db()
    return {"ok": True, "app": APP_NAME, "version": VERSION, "bands_collection": BANDS_COLLECTION, "events_collection": EVENTS_COLLECTION}


@app.get("/debug/firebase")
def debug_firebase():
    raw = _get_service_account_json_raw()
    # Não retorna a chave, só metadados úteis
    try:
        data = _parse_service_account(raw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "ok": True,
        "project_id": data.get("project_id"),
        "client_email": data.get("client_email"),
        "bands_collection": BANDS_COLLECTION,
        "events_collection": EVENTS_COLLECTION,
        "has_private_key": bool(data.get("private_key")),
    }


# -------- Helpers (Firestore) --------
def bands_col():
    return db().collection(BANDS_COLLECTION)

def events_col():
    return db().collection(EVENTS_COLLECTION)

def band_from_doc(doc) -> Dict[str, Any]:
    d = doc.to_dict() or {}
    d["id"] = doc.id
    if not d.get("created_at"):
        d["created_at"] = utc_now()
    if not d.get("updated_at"):
        d["updated_at"] = d["created_at"]
    return d

def event_from_doc(doc) -> Dict[str, Any]:
    d = doc.to_dict() or {}
    d["id"] = doc.id
    d = row_defaults(d)
    if not d.get("band_name"):
        d["band_name"] = "Banda"
    return d

def get_band_name(band_id: str) -> str:
    snap = bands_col().document(band_id).get()
    if not snap.exists:
        raise HTTPException(status_code=400, detail="Banda não encontrada. Cadastre/seleciona uma banda válida.")
    d = snap.to_dict() or {}
    name = (d.get("name") or "").strip()
    return name if name else "Banda"


# -------- Bands --------
@app.get("/api/bands", response_model=List[BandItem])
def list_bands():
    docs = list(bands_col().stream())
    items = [BandItem(**band_from_doc(d)) for d in docs]
    items.sort(key=lambda x: (x.name or "").lower())
    return items

@app.post("/api/bands", response_model=BandItem)
def create_band(payload: BandCreate):
    now = utc_now()
    band_id = f"band_{int(datetime.utcnow().timestamp()*1000)}"
    data = {"name": payload.name, "city": payload.city, "created_at": now, "updated_at": now}
    bands_col().document(band_id).set(data)
    return BandItem(id=band_id, **data)

@app.delete("/api/bands/{band_id}")
def delete_band(band_id: str):
    client = db()
    batch = client.batch()
    ev_docs = list(events_col().where("band_id", "==", band_id).stream())
    for d in ev_docs:
        batch.delete(d.reference)
    batch.delete(bands_col().document(band_id))
    batch.commit()
    return {"ok": True, "deleted_events": len(ev_docs)}


# -------- Events --------
@app.get("/api/events", response_model=List[EventItem])
def list_events(
    date: Optional[str] = Query(default=None),
    band_id: Optional[str] = Query(default=None),
    city: Optional[str] = Query(default=None),
):
    # Para não depender de índices compostos, usamos no máximo 1 filtro no Firestore.
    q = events_col()
    chosen = None
    if date:
        q = q.where("date", "==", date)
        chosen = "date"
    elif band_id:
        q = q.where("band_id", "==", band_id)
        chosen = "band_id"
    elif city:
        q = q.where("city_lower", "==", city.strip().lower())
        chosen = "city"

    docs = list(q.stream())
    items = [EventItem(**event_from_doc(d)) for d in docs]

    if chosen != "date" and date:
        items = [x for x in items if x.date == date]
    if chosen != "band_id" and band_id:
        items = [x for x in items if (x.band_id or "") == band_id]
    if chosen != "city" and city:
        c = city.strip().lower()
        items = [x for x in items if (x.city or "").strip().lower() == c]

    items.sort(key=lambda x: (x.date or "", x.time or ""))
    return items

@app.post("/api/events", response_model=EventItem)
def create_event(payload: EventCreate):
    now = utc_now()
    eid = f"evt_{int(datetime.utcnow().timestamp()*1000)}"
    band_name = get_band_name(payload.band_id or "")

    lat = lng = None
    g_city = g_state = g_postal = None
    try:
        lat, lng, g_city, g_state, g_postal = geocode_address(payload.address)
    except Exception:
        pass

    city = payload.city or g_city
    state = payload.state or g_state
    postal = payload.postal_code or g_postal

    data = {
        "band_id": payload.band_id,
        "band_name": band_name,
        "event_name": payload.event_name,
        "contractor_name": payload.contractor_name,
        "contact": payload.contact,
        "date": payload.date,
        "time": payload.time,
        "address": payload.address,
        "city": city,
        "state": state,
        "postal_code": postal,
        "notes": payload.notes,
        "status": payload.status or "planned",
        "lat": lat,
        "lng": lng,
        "city_lower": (city or "").strip().lower() if city else None,
        "created_at": now,
        "updated_at": now,
    }
    events_col().document(eid).set(data)
    return EventItem(id=eid, **row_defaults(data))

@app.delete("/api/events/{event_id}")
def delete_event(event_id: str):
    events_col().document(event_id).delete()
    return {"ok": True}


# -------- Distance / Itinerary --------
@app.get("/api/distancia", response_model=DistanceResult)
def distancia(origem: str = Query(...), destino: str = Query(...)):
    return distance_matrix(origem, destino)

@app.get("/api/itinerary", response_model=List[ItineraryStep])
def itinerary(band_id: str = Query(...), date: str = Query(...)):
    # Query simples por date e filtra band em memória (sem índice composto).
    docs = list(events_col().where("date", "==", date).stream())
    items = [EventItem(**event_from_doc(d)) for d in docs]
    items = [x for x in items if (x.band_id or "") == band_id]
    items.sort(key=lambda x: (x.time or ""))

    if len(items) < 2:
        return []

    steps: List[ItineraryStep] = []
    for i in range(len(items) - 1):
        a = items[i]
        b = items[i + 1]
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
def fetch_events_for_report(band_id: Optional[str], date: Optional[str], city: Optional[str]) -> List[Dict[str, Any]]:
    items = list_events(date=date, band_id=band_id, city=city)  # type: ignore
    return [x.model_dump() for x in items]

@app.get("/api/reports/pdf")
def report_pdf(
    band_id: Optional[str] = Query(default=None),
    date: Optional[str] = Query(default=None),
    city: Optional[str] = Query(default=None),
    mode: str = Query(default="list", description="list | itinerary"),
):
    items = fetch_events_for_report(band_id, date, city)

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
