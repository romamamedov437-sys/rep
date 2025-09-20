import replicate
import os

replicate_api_token = os.getenv("REPLICATE_API_TOKEN")
client = replicate.Client(api_token=replicate_api_token)

def train_model():
    return "Training started"
