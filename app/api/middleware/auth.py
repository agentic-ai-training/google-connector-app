from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from jose import JWTError, jwt
from app.config.settings import get_settings

router = APIRouter()
class TokenRequest(BaseModel):
    email: str
    admin: bool = False

def create_token(email, admin=False):
    settings = get_settings()
    return jwt.encode({"sub":email,"admin":admin,"exp":datetime.now(timezone.utc)+timedelta(days=1)}, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

@router.post("/auth/token")
async def token(req: TokenRequest):
    return {"access_token":create_token(req.email, req.admin),"token_type":"bearer"}

async def auth_middleware(request: Request, call_next):
    protected = request.url.path.startswith(("/chat","/feedback","/history","/admin"))
    if not protected:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    try:
        raw = header.split(" ",1)[1] if header.lower().startswith("bearer ") else ""
        payload = jwt.decode(raw,get_settings().jwt_secret_key,algorithms=[get_settings().jwt_algorithm])
        if request.url.path.startswith("/admin") and not payload.get("admin"):
            raise HTTPException(403,"Admin access required")
        request.state.user_id = payload["sub"]
    except HTTPException as exc:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    except (JWTError, KeyError, IndexError):
        return JSONResponse(
            {"detail": "Invalid or missing bearer token"}, status_code=401
        )
    return await call_next(request)
