"""Vercel entry point: serves the RaktSetu FastAPI app (console + API) as one Python function."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from raktsetu.api import app  # noqa: E402

__all__ = ["app"]
