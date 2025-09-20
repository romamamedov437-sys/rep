import os
import replicate

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")

def generate_image(prompt: str) -> str:
    model = "stability-ai/stable-diffusion:db21e45a"  # пример модели
    output = replicate.run(model, input={"prompt": prompt})
    return output[0] if output else "Ошибка генерации"
