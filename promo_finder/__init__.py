"""Promo Finder. Importing the package loads ./.env so nobody has to `export` anything."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path: str | Path | None = None) -> None:
    """Minimal .env loader: KEY=VALUE lines, # comments, optional quotes.
    Real environment variables win over the file (so Railway variables override it)."""
    p = Path(path) if path else ROOT / ".env"
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if k:
            os.environ.setdefault(k, v)


load_env()
