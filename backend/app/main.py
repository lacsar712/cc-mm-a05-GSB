import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from app.rules import classify


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://app:app@localhost:54391/methane"
    jwt_secret: str = "mine-methane-dev-secret"


settings = Settings()
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer(auto_error=False)
USERS = {
    "gasman": {"role": "writer", "password_hash": pwd.hash("gas123456")},
    "viewer": {"role": "reader", "password_hash": pwd.hash("view123456")},
}

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Reading(Base):
    __tablename__ = "readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Transcript(Base):
    """推送抄本：推送当时的测点、浓度、状态原文与校验短串，冻结存档。"""

    __tablename__ = "transcripts"
    id: Mapped[int] = mapped_column(primary_key=True)
    reading_id: Mapped[int] = mapped_column()
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(32), index=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float


class CorrectionIn(BaseModel):
    ch4_pct: float = Field(ge=0)


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(security)) -> dict:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效令牌") from exc
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=401, detail="无效令牌")
    return {"username": username, "role": payload.get("role")}


def require_writer(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "writer":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="仅瓦斯检查员可上报")
    return user


sockets: set[WebSocket] = set()
app = FastAPI(title="矿井瓦斯班测台")


def build_transcript_body(
    transcript_id: int,
    site: str,
    ch4_pct: float,
    level: str,
    note: str,
    created_by: str,
    created_at: datetime,
) -> str:
    """抄本正文：冻结推送当时的测点、浓度、状态原文。"""
    stamp = created_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        "矿井瓦斯推送抄本\n"
        f"抄本编号：{transcript_id}\n"
        f"测点：{site}\n"
        f"甲烷浓度：{ch4_pct}%\n"
        f"状态原文：{level}｜{note}\n"
        f"推送时间：{stamp}\n"
        f"检查员：{created_by}\n"
        "（正文冻结于推送当时，事后改正不影响本抄本）"
    )


def make_checksum(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def freeze_transcript(db: Session, reading: Reading) -> Transcript:
    """按推送当时的读数生成一份冻结抄本；编号先占位，落库后重算正文与校验短串。"""
    transcript = Transcript(
        reading_id=reading.id,
        site=reading.site,
        ch4_pct=reading.ch4_pct,
        level=reading.level,
        note=reading.note,
        body="",
        checksum="",
        created_by=reading.created_by,
        created_at=reading.created_at,
    )
    db.add(transcript)
    db.flush()  # 取得抄本编号
    body = build_transcript_body(
        transcript.id,
        transcript.site,
        transcript.ch4_pct,
        transcript.level,
        transcript.note,
        transcript.created_by,
        transcript.created_at,
    )
    transcript.body = body
    transcript.checksum = make_checksum(body)
    return transcript


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Reading).count() == 0:
            now = datetime.now(timezone.utc)
            for site, ch4 in (("东翼-12", 0.35), ("回风巷", 1.4)):
                level, note = classify(ch4)
                row = Reading(
                    site=site,
                    ch4_pct=ch4,
                    level=level,
                    note=note,
                    created_by="gasman",
                    created_at=now,
                )
                db.add(row)
                db.flush()
                if level == "报警":
                    freeze_transcript(db, row)
            db.commit()
        else:
            # 兼容旧库：给尚未存档的报警补建抄本
            archived = {rid for (rid,) in db.query(Transcript.reading_id).all()}
            for row in db.query(Reading).filter(Reading.level == "报警").order_by(Reading.id).all():
                if row.id not in archived:
                    freeze_transcript(db, row)
            db.commit()
    finally:
        db.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "mine-methane-shift"}


@app.post("/api/auth/login")
def login(body: LoginIn):
    user = USERS.get(body.username.strip())
    if not user or not pwd.verify(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    exp = datetime.now(timezone.utc) + timedelta(hours=8)
    token = jwt.encode(
        {"sub": body.username.strip(), "role": user["role"], "exp": exp},
        settings.jwt_secret,
        algorithm="HS256",
    )
    return {"access_token": token, "username": body.username.strip(), "role": user["role"]}


@app.get("/api/readings")
def list_readings(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Reading).order_by(Reading.id.desc()).all()
        return [
            {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "created_by": r.created_by,
            }
            for r in rows
        ]
    finally:
        db.close()


@app.post("/api/readings", status_code=201)
async def create_reading(body: ReadingIn, user: dict = Depends(require_writer)):
    level, note = classify(body.ch4_pct)
    db = SessionLocal()
    try:
        row = Reading(
            site=body.site.strip(),
            ch4_pct=body.ch4_pct,
            level=level,
            note=note,
            created_by=user["username"],
            created_at=datetime.now(timezone.utc),
        )
        db.add(row)
        db.flush()
        transcript_id = None
        if level == "报警":
            # 达到报警线的推送才生成抄本，冻结存档
            transcript_id = freeze_transcript(db, row).id
        db.commit()
        db.refresh(row)
        payload = {
            "id": row.id,
            "site": row.site,
            "ch4_pct": row.ch4_pct,
            "level": row.level,
            "note": row.note,
            "transcript_id": transcript_id,
        }
    finally:
        db.close()
    dead = []
    for ws in list(sockets):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)
    return payload


@app.patch("/api/readings/{reading_id}")
def correct_reading(reading_id: int, body: CorrectionIn, user: dict = Depends(require_writer)):
    """检查员改正班测浓度；已生成的抄本保持推送当时的样子，不受影响。"""
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="班测记录不存在")
        row.ch4_pct = body.ch4_pct
        row.level, row.note = classify(body.ch4_pct)
        db.commit()
        db.refresh(row)
        return {
            "id": row.id,
            "site": row.site,
            "ch4_pct": row.ch4_pct,
            "level": row.level,
            "note": row.note,
            "created_by": row.created_by,
        }
    finally:
        db.close()


def transcript_summary(t: Transcript) -> dict:
    return {
        "id": t.id,
        "reading_id": t.reading_id,
        "site": t.site,
        "ch4_pct": t.ch4_pct,
        "level": t.level,
        "note": t.note,
        "checksum": t.checksum,
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat(),
    }


@app.get("/api/transcripts")
def list_transcripts(_user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        rows = db.query(Transcript).order_by(Transcript.id.desc()).all()
        return [transcript_summary(t) for t in rows]
    finally:
        db.close()


@app.get("/api/transcripts/{transcript_id}")
def transcript_detail(transcript_id: int, _user: dict = Depends(current_user)):
    db = SessionLocal()
    try:
        t = db.get(Transcript, transcript_id)
        if t is None:
            raise HTTPException(status_code=404, detail="抄本不存在")
        return {**transcript_summary(t), "body": t.body}
    finally:
        db.close()


@app.get("/api/transcripts/{transcript_id}/download")
def download_transcript(transcript_id: int, _user: dict = Depends(current_user)):
    """下载抄本：正文与校验短串均为推送当时的存档内容。旁观账号亦可下载。"""
    db = SessionLocal()
    try:
        t = db.get(Transcript, transcript_id)
        if t is None:
            raise HTTPException(status_code=404, detail="抄本不存在")
        content = f"{t.body}\n校验短串：{t.checksum}\n"
        return PlainTextResponse(
            content,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="transcript-{t.id}.txt"',
                "X-Transcript-Checksum": t.checksum,
            },
        )
    finally:
        db.close()


@app.websocket("/ws/alerts")
async def alerts(ws: WebSocket):
    await ws.accept()
    sockets.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        sockets.discard(ws)
