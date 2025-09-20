from fastapi import FastAPI
from utils.replicate_api import generate_image

app = FastAPI()

@app.get("/")
def root():
    return {"status": "ok"}

@app.post("/generate")
def generate(prompt: str):
    image_url = generate_image(prompt)
    return {"url": image_url}
