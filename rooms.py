"""Export by room: the photos of a shoot sorted into rooms (Bedroom, Pool...)
so the export names them after the room - Bedroom1.jpg, Pool1..Pool10 - the
way property portals want them.

Only rooms you add exist, and an empty one is never exported, so a house with
no pool never gets a Pool section. The plan is kept per folder, so you can
come back to it and re-export.
"""

import json
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy.orm import Session

from auth import get_current_user
from database import Base, User, engine, get_db

router = APIRouter(prefix="/api/rooms", tags=["rooms"])


class RoomPlan(Base):
    __tablename__ = "room_plans"
    key = Column(String, primary_key=True)      # "folder:<id>"
    data = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String, nullable=True)


class Room(BaseModel):
    id: str = Field(..., max_length=40)
    name: str = Field(..., max_length=60)
    ids: List[int] = []


class Plan(BaseModel):
    key: str = Field(..., max_length=80)
    rooms: List[Room] = []
    style: str = Field("Bedroom1", max_length=20)


@router.get("")
def get_plan(key: str, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(RoomPlan).filter(RoomPlan.key == key).first()
    if not row:
        return {"key": key, "rooms": [], "style": "Bedroom1"}
    try:
        d = json.loads(row.data)
    except ValueError:
        d = {}
    return {"key": key, "rooms": d.get("rooms", []), "style": d.get("style", "Bedroom1"),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}


@router.put("")
def put_plan(body: Plan, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    row = db.query(RoomPlan).filter(RoomPlan.key == body.key).first()
    data = json.dumps({"rooms": [r.model_dump() for r in body.rooms][:200], "style": body.style})
    if row:
        row.data = data
        row.updated_at = datetime.utcnow()
        row.updated_by = current_user.username
    else:
        db.add(RoomPlan(key=body.key, data=data, updated_by=current_user.username))
    db.commit()
    return {"status": "ok"}


def install(app):
    Base.metadata.create_all(bind=engine, tables=[RoomPlan.__table__])
    app.include_router(router)
