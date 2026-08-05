"""
Vercel Python entry point. Vercel's Python runtime looks for an ASGI/WSGI
`app` object in files under api/ — this just re-exports the real FastAPI app
so the actual code stays organized under app/.
"""
from app.main import app  # noqa: F401
