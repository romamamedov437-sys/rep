import os
import replicate

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
replicate.Client(api_token=REPLICATE_API_TOKEN)

def generate_image(prompt: str):
    model = "stability-ai/sdxl"
    version = "latest"
    output = replicate.run(
        f"{model}:{version}",
        input={"prompt": prompt}
    )
    return output[0] if output else None
