"""Backend entrypoint (package form): python -m main"""
import os

import uvicorn

from server.app import app

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=port)