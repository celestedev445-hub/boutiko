from fastapi import FastAPI

from app.logging_config import setup_logging
from app.whatsapp.webhook import router as whatsapp_router

setup_logging()

app = FastAPI(title="Boutiko", version="0.1.0")
app.include_router(whatsapp_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
