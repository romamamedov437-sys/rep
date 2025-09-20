import os
import replicate
import aiohttp
import tempfile

REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
client = replicate.Client(api_token=REPLICATE_API_TOKEN)

# Генерация по промпту
async def generate_image(prompt: str) -> str:
    try:
        output = client.run(
            "stability-ai/sdxl:latest",  # можно заменить на Flux/SDXL++
            input={"prompt": prompt}
        )
        return output[0] if output else None
    except Exception as e:
        print(f"Ошибка генерации: {e}")
        return None

# Обучение модели
async def start_training(photo) -> str | None:
    try:
        file = await photo.get_file()
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
        await file.download_to_drive(tmp.name)

        training = client.trainings.create(
            version="stability-ai/sdxl:latest",
            input={"instance_prompt": "photo of person", "images": [tmp.name]}
        )
        return training.id
    except Exception as e:
        print(f"Ошибка обучения: {e}")
        return None
