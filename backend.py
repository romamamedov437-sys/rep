from fastapi import APIRouter

router = APIRouter()

@router.get("/train")
def train_model():
    return {"message": "Training started"}

@router.get("/status")
def check_status():
    return {"message": "Training status: running"}
