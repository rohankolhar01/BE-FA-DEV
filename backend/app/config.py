import os
import secrets

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/finance_agent"
)
JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_hex(32)
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_MINUTES = 60 * 24 * 7  # 1 week
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
COOKIE_NAME = "access_token"
# Frontend and backend live on different Vercel domains, so the auth cookie is
# cross-site: it needs SameSite=None + Secure to be sent at all. Locally, over
# plain http, it stays "lax" (SameSite=None requires Secure, which requires https).
COOKIE_SAMESITE = "none" if COOKIE_SECURE else "lax"

# Comma-separated list of allowed frontend origins, e.g.
# "https://your-frontend.vercel.app,https://your-custom-domain.com"
CORS_ORIGINS = [
    o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()
] or ["http://localhost:5173", "http://127.0.0.1:5173"]
