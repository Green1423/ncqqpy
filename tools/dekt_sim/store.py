"""内存数据模型（模拟器用；进程退出即清空，不落盘、不读真实数据）。"""
from __future__ import annotations

import hashlib
import itertools
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

__all__ = ["World", "build_world", "iso", "parse_iso", "digest_of"]


def iso(dt: datetime) -> str:
    """与真实接口一致的 ISO8601 UTC 形式（`2026-11-10T10:00:00Z`）。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text: str) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def digest_of(*parts) -> str:
    """资源摘要：写操作要求客户端带回，用于防重放/乐观锁（报告 §5.1）。"""
    raw = "|".join(str(p) for p in parts).encode()
    return hashlib.sha1(raw).hexdigest()[:16]


@dataclass
class User:
    id: int
    name: str
    username: str
    password: str = "dekt"
    college_id: int = 11
    class_id: int = 2026000099
    grade: int = 2026
    role_id: int = 23
    email: str = ""
    phone: str = ""
    banned_type: int = 0
    banned_before: str = ""
    college_score: int = 0
    school_score: int = 100
    version: int = 1

    def digest(self) -> str:
        return digest_of("user", self.id, self.version)

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "college_id": self.college_id,
            "class_id": self.class_id, "grade": self.grade,
            "role_id": self.role_id, "email": self.email, "phone": self.phone,
            "college_score": self.college_score, "school_score": self.school_score,
        }


@dataclass
class Activity:
    id: int
    name: str
    location: str = "沙河校区"
    area: str = "报告厅"
    activity_class: int = 1
    demands: int = 1 | 2 | 4          # 报名 + 签到 + 签退
    attend_limit: int = 100
    customer: int = 0
    signup_start: str = ""
    signup_end: str = ""
    start: str = ""
    end: str = ""
    belong_to: int = 11
    target_id: int = 0
    target_role: int = 0
    grade: int = 0
    priority: int = 0
    picture_url: str = ""
    is_group: int = 0
    state: int = 1                     # 0 已结束 / 1 进行中
    version: int = 1

    def digest(self) -> str:
        return digest_of("activity", self.id, self.version)

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "location": self.location,
            "area": self.area, "activity_class": self.activity_class,
            "demands": self.demands, "attend_limit": self.attend_limit,
            "customer": self.customer,
            "activity_start_time": self.start, "activity_end_time": self.end,
            "signup_start_time": self.signup_start, "signup_end_time": self.signup_end,
            "belong_to": self.belong_to, "target_id": self.target_id,
            "target_role": self.target_role, "grade": self.grade,
            "priority": self.priority, "picture_url": self.picture_url,
            "digest": self.digest(),
        }


@dataclass
class Product:
    id: int
    name: str
    price: int
    score_type: int = 0                # 0 校级 / 1 学院
    stock: int = 10
    status: int = 0                    # 0 在售 / 1 售罄
    limit: int = 1
    target_id: int = -1
    image: str = ""
    version: int = 1

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "price": self.price,
                "score_type": self.score_type, "stock": self.stock,
                "status": self.status, "limit": self.limit,
                "target_id": self.target_id, "image": self.image}


@dataclass
class World:
    """一个隔离的"世界"：用户、活动、报名、签到、积分、商城、成绩单。"""

    users: dict[int, User] = field(default_factory=dict)
    activities: dict[int, Activity] = field(default_factory=dict)
    products: dict[int, Product] = field(default_factory=dict)
    news: list[dict] = field(default_factory=list)
    news_tree: list[dict] = field(default_factory=list)
    classes: list[dict] = field(default_factory=list)
    roles: list[dict] = field(default_factory=list)
    colleges: list[dict] = field(default_factory=list)
    societies: list[dict] = field(default_factory=list)

    participations: dict[tuple[int, int], dict] = field(default_factory=dict)
    subscriptions: dict[tuple[int, int], dict] = field(default_factory=dict)
    preferences: dict[int, dict] = field(default_factory=dict)
    orders: list[dict] = field(default_factory=list)
    score_records: dict[int, list[dict]] = field(default_factory=dict)
    checkin_sessions: dict[str, dict] = field(default_factory=dict)
    checkin_events: list[dict] = field(default_factory=list)
    transcript_jobs: list[dict] = field(default_factory=list)
    umami_events: list[dict] = field(default_factory=list)

    _seq: itertools.count = field(default_factory=lambda: itertools.count(1000))

    # ---- 常用操作 ----

    def next_id(self) -> int:
        return next(self._seq)

    def user_by_username(self, username: str) -> Optional[User]:
        for u in self.users.values():
            if u.username == str(username):
                return u
        return None

    def user_digest(self, uid: int) -> str:
        u = self.users.get(int(uid))
        return u.digest() if u else ""

    def activity_digest(self, act_id: int) -> str:
        a = self.activities.get(int(act_id))
        return a.digest() if a else ""

    def is_participating(self, uid: int, act_id: int) -> bool:
        return (int(uid), int(act_id)) in self.participations

    def add_score(self, uid: int, delta: int, change_type: int, reason: str) -> None:
        u = self.users.get(int(uid))
        if u is None:
            return
        if delta >= 0:
            u.school_score += delta
        else:
            need = -delta
            from_college = min(u.college_score, need) if u.college_score > 0 else 0
            u.college_score -= from_college
            u.school_score -= (need - from_college)
        self.score_records.setdefault(int(uid), []).insert(0, {
            "id": self.next_id(), "change": delta, "type": change_type,
            "reason": reason, "created_at": iso(datetime.now(timezone.utc)),
            "school_score": u.school_score, "college_score": u.college_score,
        })


def build_world(*, now: Optional[datetime] = None, activities: int = 20,
                products: int = 8) -> World:
    """造一个可用的世界：1 个用户 + N 个活动（含边界态）+ 商城 + 资讯。"""
    now = now or datetime.now(timezone.utc)
    w = World()
    u = User(id=2026000001, name="模拟同学", username="2026000001",
             college_id=11, class_id=2026000001, grade=2026, role_id=23,
             email="sim@example.com", school_score=1000, college_score=50)
    w.users[u.id] = u

    w.roles = [{"id": 0, "name": "普通学生"}, {"id": 23, "name": "干事"}]
    w.colleges = [{"id": 11, "name": "计算机学院"}, {"id": 12, "name": "信通学院"}]
    w.societies = [{"id": 1, "name": "开源社"}, {"id": 2, "name": "摄影社"}]
    w.classes = [{"id": 1, "name": "志愿公益"}, {"id": 2, "name": "文体活动"},
                 {"id": 3, "name": "学术科技"}]

    # 活动：第 2 个"已结束"，第 3 个"名额已满"，其余"正在报名"
    for i in range(1, activities + 1):
        if i == 2:
            start = now - timedelta(days=3)
            signup_start, signup_end = start - timedelta(days=7), start - timedelta(days=1)
            state = 0
        elif i == 3:
            start = now + timedelta(days=2)
            signup_start, signup_end = now - timedelta(days=1), now + timedelta(days=1)
            state = 1
        else:
            start = now + timedelta(days=i)
            # 报名截止 = 活动开始（报告 §4.2：报名窗口看活动的 signup_* 两个时间）
            signup_start, signup_end = now - timedelta(days=1), start
            state = 1
        w.activities[i] = Activity(
            id=i, name=f"模拟活动 {i:02d}", start=iso(start),
            end=iso(start + timedelta(hours=2)),
            signup_start=iso(signup_start), signup_end=iso(signup_end),
            attend_limit=3 if i == 3 else 50, state=state,
            priority=10 - (i % 5), activity_class=(i % 3) + 1,
            is_group=1 if i % 5 == 0 else 0,
        )

    # "名额已满"的那一个：预置满员报名（否则单用户世界里凑不出满员）
    full = w.activities.get(3)
    if full is not None:
        for ghost in (9001, 9002, 9003):
            w.participations[(ghost, 3)] = {
                "user_id": ghost, "act_id": 3,
                "start_t_ms": 0, "end_t_ms": 0,
                "created_at": iso(now - timedelta(hours=1))}

    for i in range(1, products + 1):
        w.products[i] = Product(
            id=i, name=f"模拟商品 {i:02d}", price=50 * i,
            score_type=1 if i % 3 == 0 else 0,
            stock=0 if i == 2 else 10 + i, status=1 if i == 2 else 0,
            limit=1 if i % 2 else 2, target_id=11 if i % 3 == 0 else -1,
            image=f"https://img.example/{i}.png")

    w.news_tree = [
        {"id": 1, "name": "校园通知", "children": [
            {"id": 11, "name": "教务"}, {"id": 12, "name": "生活"}]},
        {"id": 2, "name": "活动预告", "children": []},
    ]
    for i in range(1, 16):
        w.news.append({
            "id": i, "title": f"模拟资讯 {i:02d}", "type": i % 3,
            "tree_id": 11 if i % 2 else 12,
            "content": f"这是第 {i} 条模拟资讯的正文。",
            "publish_at": iso(now - timedelta(days=i)),
            "url": f"https://dekt.bupt.edu.cn/news/{i}",
        })
    return w
