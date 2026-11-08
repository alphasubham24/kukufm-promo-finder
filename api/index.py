import os
import sys
from pathlib import Path

# Add project root to sys.path so app and promo_finder can be imported
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# On Vercel / serverless platforms, root directory is read-only.
# Redirect all writable paths to /tmp
os.environ.setdefault("RUNS_DIR", "/tmp/runs")
os.environ.setdefault("UPLOAD_DIR", "/tmp/uploads")
os.environ.setdefault("LLM_CACHE_DIR", "/tmp/cache/llm")

for p in ("/tmp/runs", "/tmp/uploads", "/tmp/cache/llm"):
    try:
        Path(p).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

from app.main import app
