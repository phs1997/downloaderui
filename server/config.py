import os
from dotenv import load_dotenv

load_dotenv()

JD_URL = os.getenv("JD_URL", "http://172.17.0.2:3128").rstrip("/")
if not JD_URL.startswith("http://") and not JD_URL.startswith("https://"):
    JD_URL = f"http://{JD_URL}"

STORAGE_PATH = os.getenv("STORAGE_PATH", "/output")

PORT = int(os.getenv("PORT", 8080))
HOST = os.getenv("HOST", "0.0.0.0")