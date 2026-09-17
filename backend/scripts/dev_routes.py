from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import articles, search

print("articles.router routes:", len(articles.router.routes))
for r in articles.router.routes:
    print("   ", type(r).__name__, getattr(r, "path", ""), getattr(r, "methods", None))
print("search.router routes:", len(search.router.routes))

from app.main import app

print("app.routes:", len(app.routes))
for r in app.routes:
    print("   ", type(r).__name__, getattr(r, "path", ""), getattr(r, "methods", None))
