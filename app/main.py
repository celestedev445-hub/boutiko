import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.config import get_settings
from app.logging_config import setup_logging
from app.services.daily import summary_loop
from app.whatsapp.webhook import router as whatsapp_router

setup_logging()


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
