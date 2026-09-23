import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from sqlalchemy import DateTime, Float, ForeignKey, String, create_engine
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
    """报警推送当时的冻结抄本：测点、浓度、状态原文、校验短串与正文单独存档。

    抄本一经生成不再随班测浓度改正而变化；只有再次达到报警线的推送才产生新抄本。
    """

    __tablename__ = "transcripts"
    id: Mapped[int] = mapped_column(primary_key=True)
    reading_id: Mapped[int] = mapped_column(ForeignKey("readings.id"))
    site: Mapped[str] = mapped_column(String(80))
    ch4_pct: Mapped[float] = mapped_column(Float)
    level_text: Mapped[str] = mapped_column(String(20))
    note_text: Mapped[str] = mapped_column(String(200))
    checksum: Mapped[str] = mapped_column(String(64), unique=True)
    body: Mapped[str] = mapped_column(String)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginIn(BaseModel):
    username: str
    password: str


class ReadingIn(BaseModel):
    site: str = Field(min_length=1, max_length=80)
    ch4_pct: float = Field(ge=0)


class ReadingFixIn(BaseModel):
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
    *,
    reading_id: int,
    site: str,
    ch4_pct: float,
    level_text: str,
    note_text: str,
    created_by: str,
    pushed_at: datetime,
) -> str:
    """冻结正文（不含校验短串本身），浓度、状态原文均取推送当时的值。"""
    return "\n".join(
        [
            "矿井瓦斯报警推送抄本",
            f"班测编号：{reading_id}",
            f"测点：{site}",
            f"甲烷浓度：{ch4_pct:.2f}%",
            f"状态：{level_text}",
            f"状态原文：{note_text}",
            f"推送时间：{pushed_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"上报人：{created_by}",
            "",
            "本抄本冻结于报警推送当时；事后班测浓度改正不改变本抄本正文与校验短串。",
        ]
    )


def short_checksum(frozen_body: str) -> str:
    return hashlib.sha256(frozen_body.encode("utf-8")).hexdigest()[:12]


def freeze_transcript(db: Session, row: Reading) -> Transcript:
    """把一次报警推送冻结为抄本，单独存档。"""
    frozen = build_transcript_body(
        reading_id=row.id,
        site=row.site,
        ch4_pct=row.ch4_pct,
        level_text=row.level,
        note_text=row.note,
        created_by=row.created_by,
        pushed_at=row.created_at,
    )
    checksum = short_checksum(frozen)
    transcript = Transcript(
        reading_id=row.id,
        site=row.site,
        ch4_pct=row.ch4_pct,
        level_text=row.level,
        note_text=row.note,
        checksum=checksum,
        body=f"{frozen}\n校验短串：{checksum}\n",
        created_by=row.created_by,
        created_at=row.created_at,
    )
    db.add(transcript)
    db.commit()
    db.refresh(transcript)
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
            # 兼容升级前的旧库：给尚无抄本的报警班测补冻结存档。
            existing = {t.reading_id for t in db.query(Transcript).all()}
            missing = (
                db.query(Reading)
                .filter(Reading.level == "报警", ~Reading.id.in_(existing or [0]))
                .order_by(Reading.id)
                .all()
            )
            for row in missing:
                freeze_transcript(db, row)
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
        scripts = (
            db.query(Transcript.reading_id, Transcript.id)
            .order_by(Transcript.id.desc())
            .all()
        )
        transcript_ids: dict[int, list[int]] = {}
        for reading_id, transcript_id in scripts:
            transcript_ids.setdefault(reading_id, []).append(transcript_id)
        return [
            {
                "id": r.id,
                "site": r.site,
                "ch4_pct": r.ch4_pct,
                "level": r.level,
                "note": r.note,
                "created_by": r.created_by,
                "transcript_ids": transcript_ids.get(r.id, []),
            }
            for r in rows
        ]
    finally:
        db.close()


@app.patch("/api/readings/{reading_id}")
def fix_reading(reading_id: int, body: ReadingFixIn, user: dict = Depends(require_writer)):
    """检查员改正班测浓度。

    改正只影响班测记录本身：不重新推送、不生成新抄本、不改动已生成抄本的正文与校验短串。
    """
    db = SessionLocal()
    try:
        row = db.get(Reading, reading_id)
        if row is None:
            raise HTTPException(status_code=404, detail="班测不存在")
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
        db.commit()
        db.refresh(row)
        # 只有达到报警线的这次推送才冻结抄本；正常班测不产生抄本。
        transcript_id = None
        if level == "报警":
            transcript = freeze_transcript(db, row)
            transcript_id = transcript.id
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


@app.get("/api/transcripts")
def list_transcripts(reading_id: int | None = None, _user: dict = Depends(current_user)):
    """抄本列表。可带 reading_id 进某条班测的抄本详情；旁观账号同样可看。"""
    db = SessionLocal()
    try:
        query = db.query(Transcript).order_by(Transcript.id.desc())
        if reading_id is not None:
            query = query.filter(Transcript.reading_id == reading_id)
        return [
            {
                "id": t.id,
                "reading_id": t.reading_id,
                "site": t.site,
                "ch4_pct": t.ch4_pct,
                "level_text": t.level_text,
                "note_text": t.note_text,
                "checksum": t.checksum,
                "created_by": t.created_by,
                "created_at": t.created_at.isoformat(),
            }
            for t in query.all()
        ]
    finally:
        db.close()


@app.get("/api/transcripts/{transcript_id}/download")
def download_transcript(transcript_id: int, _user: dict = Depends(current_user)):
    """下载冻结抄本正文（含校验短串）。旁观账号可下载，正文永远是推送当时的样子。"""
    db = SessionLocal()
    try:
        t = db.get(Transcript, transcript_id)
        if t is None:
            raise HTTPException(status_code=404, detail="抄本不存在")
        filename = f"transcript-{t.id}-{t.checksum}.txt"
        return Response(
            content=t.body,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
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
