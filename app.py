import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Depends, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import (
    Column, Integer, String, DateTime, Boolean, Text, create_engine, Index
)
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# ================== CONFIG ==================
DB_URL = os.getenv("DB_URL", "sqlite:///./wipeweb.db")
ANON_ID_TTL_HOURS = int(os.getenv("ANON_ID_TTL_HOURS", "24"))
PUBLIC_ROOM_LIMIT = int(os.getenv("PUBLIC_ROOM_LIMIT", "100"))   # public groups
PRIVATE_ROOM_LIMIT = int(os.getenv("PRIVATE_ROOM_LIMIT", "2"))   # 1:1
MAX_MESSAGE_LEN = int(os.getenv("MAX_MESSAGE_LEN", "4000"))

# Frontend base to compose share URLs (fallback if not provided in request)
FRONTEND_BASE = os.getenv("FRONTEND_BASE", "https://myda.netlify.app")

# ================== DB ==================
engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

class AnonymousIdentity(Base):
    __tablename__ = "anonymous_identities"
    id = Column(Integer, primary_key=True)
    anon_id = Column(String(48), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    active = Column(Boolean, default=True, nullable=False)
Index("idx_identity_expiry", AnonymousIdentity.expires_at)

class Room(Base):
    __tablename__ = "rooms"
    id = Column(Integer, primary_key=True)  # numeric room_id
    room_type = Column(String(16), nullable=False)  # 'public' | 'private'
    owner_anon_id = Column(String(48), nullable=False, index=True)
    invite_token = Column(String(64), nullable=True, unique=True, index=True)  # private only
    name = Column(String(64), nullable=True)
    max_members = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, index=True)

class RoomMember(Base):
    __tablename__ = "room_members"
    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, nullable=False, index=True)
    anon_id = Column(String(48), nullable=False, index=True)
    joined_at = Column(DateTime(timezone=True), nullable=False)

class Message(Base):
    __tablename__ = "messages"
    id = Column(Integer, primary_key=True)
    room_id = Column(Integer, index=True, nullable=False)
    sender_anon_id = Column(String(48), index=True, nullable=False)
    msg_type = Column(String(16), nullable=False)  # text/image/voice/link/document
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try: yield db
    finally: db.close()

# ================== APP / CORS ==================
app = FastAPI(title="WipeWeb API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# ================== HELPERS ==================
ALPHANUM = string.ascii_letters + string.digits
def gen_anon_id(prefix: str = "Anon", length: int = 10) -> str:
    token = ''.join(secrets.choice(ALPHANUM) for _ in range(length))
    return f"{prefix}-{token.upper()}"
def gen_invite_token(length: int = 24) -> str:
    return ''.join(secrets.choice(ALPHANUM) for _ in range(length))
def ensure_active_identity(db: Session, anon_id: str) -> AnonymousIdentity:
    ident = db.query(AnonymousIdentity).filter(
        AnonymousIdentity.anon_id == anon_id,
        AnonymousIdentity.active == True,
        AnonymousIdentity.expires_at > now_utc()
    ).first()
    if not ident:
        raise HTTPException(status_code=401, detail="Anonymous ID invalid or expired")
    return ident
def count_members(db: Session, room_id: int) -> int:
    return db.query(RoomMember).filter(RoomMember.room_id == room_id).count()
def frontend_base_from_request(req: Request) -> str:
    # Prefer configured FRONTEND_BASE; fallback to request base URL host
    return FRONTEND_BASE or str(req.base_url).rstrip('/')

# ================== SCHEMAS ==================
class HealthOut(BaseModel): ok: bool; ts: str
class NewAnonOut(BaseModel): id: str; expires_at: datetime
class CreatePublicIn(BaseModel): name: Optional[str] = Field(None, max_length=64)
class CreatePublicOut(BaseModel):
    room_id: int; share_url: str
class CreatePrivateOut(BaseModel):
    room_id: int; invite_token: str; invite_url: str
class JoinPublicIn(BaseModel): anon_id: str
class JoinPrivateIn(BaseModel): anon_id: str; invite_token: str
class MessageIn(BaseModel):
    anon_id: str
    msg_type: str = Field(..., pattern="^(text|image|voice|link|document)$")
    content: str = Field(..., max_length=MAX_MESSAGE_LEN)
class MessageOut(BaseModel):
    id: int; room_id: int; sender_anon_id: str; msg_type: str; content: str; created_at: datetime
class OkOut(BaseModel): ok: bool

# ================== ROUTES ==================
@app.get("/v1/health", response_model=HealthOut)
def health(): return HealthOut(ok=True, ts=now_utc().isoformat())

@app.get("/v1/anon/new", response_model=NewAnonOut)
def new_anon(db: Session = Depends(get_db)):
    aid = gen_anon_id(); now = now_utc(); exp = now + timedelta(hours=ANON_ID_TTL_HOURS)
    rec = AnonymousIdentity(anon_id=aid, created_at=now, expires_at=exp, active=True)
    db.add(rec); db.commit()
    return NewAnonOut(id=aid, expires_at=exp)

# Public: create group (auto-join owner), return share URL
@app.post("/v1/public/create", response_model=CreatePublicOut)
def public_create(
    payload: CreatePublicIn,
    anon_id: str = Query(...),
    req: Request = None,
    db: Session = Depends(get_db)
):
    ensure_active_identity(db, anon_id)
    room = Room(
        room_type="public",
        owner_anon_id=anon_id,
        invite_token=None,
        name=(payload.name or None),
        max_members=PUBLIC_ROOM_LIMIT,
        created_at=now_utc(),
    )
    db.add(room); db.commit(); db.refresh(room)
    # auto-join owner
    db.add(RoomMember(room_id=room.id, anon_id=anon_id, joined_at=now_utc())); db.commit()
    base = frontend_base_from_request(req)
    share_url = f"{base}/public.html?room_id={room.id}"
    return CreatePublicOut(room_id=room.id, share_url=share_url)

# Public: join by room_id
@app.post("/v1/public/join", response_model=OkOut)
def public_join(
    payload: JoinPublicIn,
    room_id: int = Query(...),
    db: Session = Depends(get_db)
):
    ensure_active_identity(db, payload.anon_id)
    room = db.query(Room).filter(Room.id == room_id, Room.room_type == "public").first()
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if count_members(db, room_id) >= room.max_members:
        raise HTTPException(status_code=409, detail="Room is full")
    exists = db.query(RoomMember).filter(RoomMember.room_id == room_id, RoomMember.anon_id == payload.anon_id).first()
    if not exists:
        db.add(RoomMember(room_id=room_id, anon_id=payload.anon_id, joined_at=now_utc())); db.commit()
    return OkOut(ok=True)

# Private: create 1:1 chat (auto-join owner), return invite URL
@app.post("/v1/private/create", response_model=CreatePrivateOut)
def private_create(
    anon_id: str = Query(...),
    req: Request = None,
    db: Session = Depends(get_db)
):
    ensure_active_identity(db, anon_id)
    invite = gen_invite_token()
    room = Room(
        room_type="private",
        owner_anon_id=anon_id,
        invite_token=invite,
        name=None,
        max_members=PRIVATE_ROOM_LIMIT,  # 2
        created_at=now_utc(),
    )
    db.add(room); db.commit(); db.refresh(room)
    # auto-join owner
    db.add(RoomMember(room_id=room.id, anon_id=anon_id, joined_at=now_utc())); db.commit()
    base = frontend_base_from_request(req)
    invite_url = f"{base}/private.html?room_id={room.id}&invite={invite}"
    return CreatePrivateOut(room_id=room.id, invite_token=invite, invite_url=invite_url)

# Private: join via invite
@app.post("/v1/private/join", response_model=OkOut)
def private_join(
    payload: JoinPrivateIn,
    room_id: int = Query(...),
    db: Session = Depends(get_db)
):
    ensure_active_identity(db, payload.anon_id)
    room = db.query(Room).filter(Room.id == room_id, Room.room_type == "private").first()
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if payload.invite_token != room.invite_token:
        raise HTTPException(status_code=403, detail="Invalid invite token")
    if count_members(db, room_id) >= room.max_members:
        raise HTTPException(status_code=409, detail="Room is full")
    exists = db.query(RoomMember).filter(RoomMember.room_id == room_id, RoomMember.anon_id == payload.anon_id).first()
    if not exists:
        db.add(RoomMember(room_id=room_id, anon_id=payload.anon_id, joined_at=now_utc())); db.commit()
    return OkOut(ok=True)

# Messages
@app.post("/v1/messages/post", response_model=OkOut)
def post_message(
    payload: MessageIn,
    room_id: int = Query(...),
    db: Session = Depends(get_db)
):
    ensure_active_identity(db, payload.anon_id)
    # must be member
    member = db.query(RoomMember).filter(RoomMember.room_id == room_id, RoomMember.anon_id == payload.anon_id).first()
    if not member: raise HTTPException(status_code=403, detail="Not a room member")
    msg = Message(room_id=room_id, sender_anon_id=payload.anon_id, msg_type=payload.msg_type, content=payload.content, created_at=now_utc())
    db.add(msg); db.commit()
    return OkOut(ok=True)

@app.get("/v1/messages/list", response_model=List[MessageOut])
def list_messages(
    room_id: int = Query(...),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db)
):
    msgs = db.query(Message).filter(Message.room_id == room_id).order_by(Message.created_at.desc()).limit(limit).all()
    return [
        MessageOut(id=m.id, room_id=m.room_id, sender_anon_id=m.sender_anon_id, msg_type=m.msg_type, content=m.content, created_at=m.created_at)
        for m in reversed(msgs)
    ]

# TTL cleanup (same as earlier)
TTL_RULES_HOURS = {"text":29, "voice":12, "image":13, "link":13, "document":13}
@app.post("/v1/admin/cleanup", response_model=OkOut)
def cleanup_expired(db: Session = Depends(get_db), secret: Optional[str] = Query(None)):
    now = now_utc()
    # expire IDs
    ex = db.query(AnonymousIdentity).filter(AnonymousIdentity.active == True, AnonymousIdentity.expires_at <= now).all()
    for i in ex: i.active = False
    db.commit()
    # delete messages by TTL
    for mtype, hrs in TTL_RULES_HOURS.items():
        cutoff = now - timedelta(hours=hrs)
        db.query(Message).filter(Message.msg_type == mtype, Message.created_at <= cutoff).delete(synchronize_session=False)
    db.commit()
    return OkOut(ok=True)