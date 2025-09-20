import asyncio
import os
from fastapi import FastAPI
import uvicorn

from bot import run_bot

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Backend is running"}

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(run_bot())
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
