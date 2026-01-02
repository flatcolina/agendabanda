import logging
from fastapi import APIRouter, Header, HTTPException

from firebase_admin_client import get_db, verify_bearer_token
from schemas import GeocodeRequest
from maps_service import geocode_address

logger = logging.getLogger("venues")
router = APIRouter()

@router.post("/geocode")
def geocode(req: GeocodeRequest, authorization: str | None = Header(default=None)):
    try:
        decoded = verify_bearer_token(authorization)
        uid = decoded.get("uid")
        db = get_db()

        venue_ref = db.collection("orgs").document(req.orgId).collection("venues").document(req.venueId)
        venue = venue_ref.get()
        if not venue.exists:
            raise HTTPException(status_code=404, detail="Local (venue) não encontrado.")

        v = venue.to_dict() or {}
        address = v.get("address") or ""
        if not address.strip():
            raise HTTPException(status_code=400, detail="Venue sem endereço para geocodificar.")

        lat, lng = geocode_address(address)
        venue_ref.update({"lat": lat, "lng": lng})

        logger.info("Geocode ok org=%s venue=%s uid=%s", req.orgId, req.venueId, uid)
        return {"ok": True, "lat": lat, "lng": lng}

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.exception("Erro no geocode")
        raise HTTPException(status_code=500, detail=str(e))
