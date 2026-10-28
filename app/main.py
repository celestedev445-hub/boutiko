import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.logging_config import setup_logging
from app.services.daily import summary_loop
from app.whatsapp.webhook import router as whatsapp_router

setup_logging()

STATIC_DIR = Path(__file__).resolve().parent / "static"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    task = asyncio.create_task(summary_loop()) if get_settings().summary_enabled else None
    yield
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="Boutiko", version="0.1.0", lifespan=lifespan)
app.include_router(whatsapp_router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def landing_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api/public-config")
def public_config() -> dict[str, str]:
    """What the landing page may know. Nothing secret goes here."""
    return {"demo_whatsapp_number": get_settings().demo_whatsapp_number}
