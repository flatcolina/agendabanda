import logging
from datetime import datetime
from fastapi import APIRouter, Header, HTTPException

from firebase_admin_client import get_db, verify_bearer_token
from schemas import RecalcRequest
from maps_service import compute_route_minutes_km

logger = logging.getLogger("events")
router = APIRouter()

def _parse_hhmm(s: str) -> int:
    # retorna minutos desde 00:00
    if not s:
        return 0
    parts = s.split(":")
    if len(parts) != 2:
        return 0
    h = int(parts[0]); m = int(parts[1])
    return h*60 + m

@router.post("/recalc-logistics")
def recalc_logistics(req: RecalcRequest, authorization: str | None = Header(default=None)):
    try:
        decoded = verify_bearer_token(authorization)
        uid = decoded.get("uid")
        db = get_db()

        events_col = db.collection("orgs").document(req.orgId).collection("events")
        venues_col = db.collection("orgs").document(req.orgId).collection("venues")

        # busca eventos do dia
        qs = events_col.where("date", "==", req.date).stream()
        events = []
        for doc in qs:
            d = doc.to_dict() or {}
            d["_id"] = doc.id
            events.append(d)

        # ordena por startTime, e fallback por 'order'
        events.sort(key=lambda e: (_parse_hhmm(e.get("startTime","")), int(e.get("order", 999999))))

        # pré-carrega venues
        venue_ids = list({e.get("venueId") for e in events if e.get("venueId")})
        venues = {}
        for vid in venue_ids:
            vdoc = venues_col.document(vid).get()
            if vdoc.exists:
                venues[vid] = vdoc.to_dict() or {}

        updates = 0
        for i, ev in enumerate(events):
            ev_id = ev["_id"]
            if i == len(events) - 1:
                # último: sem próximo
                events_col.document(ev_id).update({
                    "logistics": {
                        "toNextKm": None,
                        "toNextMinutes": None,
                        "toNextVenueId": None,
                        "toNextUpdatedAt": datetime.utcnow().isoformat() + "Z"
                    }
                })
                updates += 1
                continue

            cur_venue = venues.get(ev.get("venueId"))
            next_ev = events[i+1]
            next_venue = venues.get(next_ev.get("venueId"))

            if not cur_venue or not next_venue:
                events_col.document(ev_id).update({
                    "logistics": {
                        "toNextKm": None,
                        "toNextMinutes": None,
                        "toNextVenueId": next_ev.get("venueId"),
                        "toNextUpdatedAt": datetime.utcnow().isoformat() + "Z",
                        "error": "Venue não encontrado"
                    }
                })
                updates += 1
                continue

            if cur_venue.get("lat") is None or cur_venue.get("lng") is None or next_venue.get("lat") is None or next_venue.get("lng") is None:
                events_col.document(ev_id).update({
                    "logistics": {
                        "toNextKm": None,
                        "toNextMinutes": None,
                        "toNextVenueId": next_ev.get("venueId"),
                        "toNextUpdatedAt": datetime.utcnow().isoformat() + "Z",
                        "error": "Venue sem lat/lng (geocodifique os locais)"
                    }
                })
                updates += 1
                continue

            origin = (float(cur_venue["lat"]), float(cur_venue["lng"]))
            dest = (float(next_venue["lat"]), float(next_venue["lng"]))
            minutes, km = compute_route_minutes_km(origin, dest)

            events_col.document(ev_id).update({
                "logistics": {
                    "toNextKm": km,
                    "toNextMinutes": minutes,
                    "toNextVenueId": next_ev.get("venueId"),
                    "toNextUpdatedAt": datetime.utcnow().isoformat() + "Z"
                }
            })
            updates += 1

        logger.info("Recalc ok org=%s date=%s updates=%s uid=%s", req.orgId, req.date, updates, uid)
        return {"ok": True, "date": req.date, "updated": updates, "eventsCount": len(events)}

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.exception("Erro no recalc")
        raise HTTPException(status_code=500, detail=str(e))
