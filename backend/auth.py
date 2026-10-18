import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from pydantic import BaseModel
from db import get_database

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SECRET_KEY = "brand-guardian-dev-secret-change-in-prod"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 # 24 hours

# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(password), hashed)

# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def create_access_token(username: str, role: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": username, "role": role, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


async def decode_token(token: str) -> dict:
    """Return payload or raise 401."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if "sub" not in payload or "role" not in payload:
            raise HTTPException(status_code=401, detail="Invalid token")
        return payload
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AuthRequest(BaseModel):
    username: str
    password: str
    role: str = "client" # Default to client, can be "agent"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class UserResponse(BaseModel):
    username: str
    role: str

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Brand Guardian Auth")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/signup", response_model=TokenResponse)
async def signup(body: AuthRequest):
    database = await get_database()
    users_collection = database["users"]
    
    existing_user = await users_collection.find_one({"username": body.username})
    if existing_user:
        raise HTTPException(status_code=400, detail="Username already exists")
        
    if body.role not in ["client", "agent"]:
        raise HTTPException(status_code=400, detail="Role must be 'client' or 'agent'")
    
    new_user = {
        "username": body.username,
        "password": hash_password(body.password),
        "role": body.role,
        "created_at": datetime.now(timezone.utc)
    }
    await users_collection.insert_one(new_user)
    
    token = create_access_token(body.username, body.role)
    return TokenResponse(access_token=token, role=body.role)


@app.post("/login", response_model=TokenResponse)
async def login(body: AuthRequest):
    database = await get_database()
    users_collection = database["users"]
    
    user_data = await users_collection.find_one({"username": body.username})
    if not user_data or not verify_password(body.password, user_data["password"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    
    token = create_access_token(body.username, user_data["role"])
    return TokenResponse(access_token=token, role=user_data["role"])


@app.get("/me", response_model=UserResponse)
async def me(token: str = Depends(oauth2_scheme)):
    payload = await decode_token(token)
    username = payload["sub"]
    
    database = await get_database()
    users_collection = database["users"]
    
    user_data = await users_collection.find_one({"username": username})
    if not user_data:
        raise HTTPException(status_code=401, detail="User not found")
    
    return UserResponse(username=username, role=user_data["role"])
