"""Legacy entrypoint: `python app.py` keeps working (CWD = repo root or package dir)."""
import os
import sys

# Ensure the repo root is on sys.path when run as `python app.py` from a different CWD.
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)

import uvicorn

from server.app import app

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=port)