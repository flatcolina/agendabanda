import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="AgendaBandas API", version="1.0.0")

# CORS (opcional). Se ALLOWED_ORIGINS não estiver definido, o backend roda sem CORS.
origins_raw = os.getenv("ALLOWED_ORIGINS", "").strip()
origins = [o.strip() for o in origins_raw.split(",") if o.strip()]
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
