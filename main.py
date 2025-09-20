from fastapi import FastAPI
import asyncio
from bot import run_bot

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok"}

# запуск Telegram-бота в фоне
@app.on_event("startup")
async def startup_event():
    loop = asyncio.get_event_loop()
    loop.create_task(run_bot())
