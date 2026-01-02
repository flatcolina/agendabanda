import logging
from fastapi import APIRouter, Header, HTTPException, Query
from firebase_admin_client import get_db, verify_bearer_token

logger = logging.getLogger("logistics")
router = APIRouter()

@router.get("/day-logistics")
def day_logistics(orgId: str = Query(...), date: str = Query(...), authorization: str | None = Header(default=None)):
    try:
        decoded = verify_bearer_token(authorization)
        uid = decoded.get("uid")
        db = get_db()

        events_col = db.collection("orgs").document(orgId).collection("events")
        venues_col = db.collection("orgs").document(orgId).collection("venues")
        bands_col = db.collection("orgs").document(orgId).collection("bands")

        # eventos do dia
        docs = list(events_col.where("date", "==", date).stream())
        events = []
        venue_ids = set()
        band_ids = set()

        def parse_hhmm(s: str) -> int:
            if not s: return 0
            parts = s.split(":")
            if len(parts) != 2: return 0
            return int(parts[0])*60 + int(parts[1])

        for d in docs:
            ev = d.to_dict() or {}
            ev["id"] = d.id
            events.append(ev)
            if ev.get("venueId"): venue_ids.add(ev["venueId"])
            if ev.get("bandId"): band_ids.add(ev["bandId"])

        events.sort(key=lambda e: (parse_hhmm(e.get("startTime","")), int(e.get("order", 999999))))

        # carregar venues/bands para enriquecer resposta
        venues = {}
        for vid in venue_ids:
            vdoc = venues_col.document(vid).get()
            if vdoc.exists: venues[vid] = vdoc.to_dict() or {}

        bands = {}
        for bid in band_ids:
            bdoc = bands_col.document(bid).get()
            if bdoc.exists: bands[bid] = bdoc.to_dict() or {}

        out = []
        for i, ev in enumerate(events):
            v = venues.get(ev.get("venueId"), {})
            b = bands.get(ev.get("bandId"), {})
            logi = (ev.get("logistics") or {})
            to_next = {
                "km": logi.get("toNextKm"),
                "minutes": logi.get("toNextMinutes"),
                "toNextVenueId": logi.get("toNextVenueId"),
                "updatedAt": logi.get("toNextUpdatedAt"),
                "error": logi.get("error"),
            }

            # alerta simples: se existir próximo evento e tempo entre fim -> próximo início for < minutes deslocamento
            alert = None
            if i < len(events) - 1:
                next_ev = events[i+1]
                end_m = parse_hhmm(ev.get("endTime","")) or (parse_hhmm(ev.get("startTime","")) + 60)
                next_start = parse_hhmm(next_ev.get("startTime",""))
                window = max(0, next_start - end_m)
                if to_next.get("minutes") is not None:
                    if to_next["minutes"] > window:
                        alert = f"⚠️ Janela {window} min < deslocamento {to_next['minutes']} min (risco de atraso)"
            out.append({
                "id": ev.get("id"),
                "title": ev.get("title") or "Show",
                "date": ev.get("date"),
                "startTime": ev.get("startTime"),
                "endTime": ev.get("endTime"),
                "status": ev.get("status"),
                "band": {"id": ev.get("bandId"), "name": b.get("name")},
                "venue": {"id": ev.get("venueId"), "name": v.get("name"), "address": v.get("address")},
                "toNext": to_next,
                "alert": alert
            })

        logger.info("Day logistics org=%s date=%s count=%s uid=%s", orgId, date, len(out), uid)
        return {"ok": True, "date": date, "events": out}

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.exception("Erro day-logistics")
        raise HTTPException(status_code=500, detail=str(e))
