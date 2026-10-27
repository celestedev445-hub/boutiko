import logging

from fastapi import FastAPI

from app.whatsapp.webhook import router as whatsapp_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Boutiko", version="0.1.0")
app.include_router(whatsapp_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
