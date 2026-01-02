import logging
import requests
from settings import settings

logger = logging.getLogger("maps")

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# Google Routes API v2 computeRoutes endpoint
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

def geocode_address(address: str) -> tuple[float, float]:
    if not settings.GOOGLE_MAPS_API_KEY:
        raise RuntimeError("GOOGLE_MAPS_API_KEY não configurado.")

    params = {"address": address, "key": settings.GOOGLE_MAPS_API_KEY}
    r = requests.get(GEOCODE_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    if data.get("status") != "OK" or not data.get("results"):
        raise RuntimeError(f"Falha no geocode: {data.get('status')} - {data.get('error_message')}")

    loc = data["results"][0]["geometry"]["location"]
    return float(loc["lat"]), float(loc["lng"])

def compute_route_minutes_km(origin: tuple[float, float], dest: tuple[float, float]) -> tuple[int, float]:
    """Retorna (minutes, km) usando Routes API."""
    if not settings.GOOGLE_MAPS_API_KEY:
        raise RuntimeError("GOOGLE_MAPS_API_KEY não configurado.")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.GOOGLE_MAPS_API_KEY,
        # FieldMask reduz custo e payload:
        "X-Goog-FieldMask": "routes.distanceMeters,routes.duration",
    }

    body = {
        "origin": {"location": {"latLng": {"latitude": origin[0], "longitude": origin[1]}}},
        "destination": {"location": {"latLng": {"latitude": dest[0], "longitude": dest[1]}}},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
    }

    r = requests.post(ROUTES_URL, json=body, headers=headers, timeout=25)
    # Se Routes API não estiver habilitada, Google retorna 403/404 — melhor mensagem:
    if r.status_code >= 400:
        try:
            j = r.json()
        except Exception:
            j = {"raw": r.text}
        raise RuntimeError(f"Falha Routes API ({r.status_code}): {j}")

    data = r.json()
    routes = data.get("routes") or []
    if not routes:
        raise RuntimeError("Routes API não retornou rotas.")

    route = routes[0]
    distance_m = float(route.get("distanceMeters", 0))
    duration_str = route.get("duration", "0s")  # ex: "123s"
    seconds = int(duration_str.replace("s", "")) if isinstance(duration_str, str) else int(duration_str)
    minutes = max(1, int(round(seconds / 60)))
    km = round(distance_m / 1000.0, 2)
    return minutes, km
