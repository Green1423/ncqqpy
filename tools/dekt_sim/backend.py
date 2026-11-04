"""43 条 REST 路由的内存实现（无网络、无真实账号）。

路由清单与参数来自 `plugins/bupt_news/afterclass/api-endpoints.json`，
行为顺序参照 `final_draft.md` §4~§8。

约定
----
* 响应统一 `{"data": ..., "digest": ...}`；错误用 4xx + `{"data": None, "msg": ...}`；
* 除 `auth.*`、`/config/index.json`、`/api/v1/event/send`、`/api/v1/image/*`
  之外都要 `Authorization: Bearer <JWT>`；
* 写操作要求客户端带回**当前** digest，过期/不符 → 409（乐观锁，报告 §5.1）。
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from . import codec
from .store import World, build_world, digest_of, iso, parse_iso

__all__ = ["Backend", "Response"]


@dataclass
class Response:
    status: int = 200
    payload: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)


def _ok(data: Any = None, digest: Any = None, **extra) -> Response:
    body: dict = {"data": data}
    if digest is not None:
        body["digest"] = digest
    body.update(extra)
    return Response(200, body)


def _err(status: int, msg: str, **extra) -> Response:
    body = {"data": None, "msg": msg}
    body.update(extra)
    return Response(status, body)


@dataclass
class Request:
    method: str
    path: str
    query: dict
    form: dict
    json_body: Any
    headers: dict

    def get(self, key: str, default: Any = "") -> Any:
        """取值顺序：form → query → JSON body（与客户端实际发法一致）。"""
        if key in self.form:
            return self.form[key]
        if key in self.query:
            return self.query[key]
        if isinstance(self.json_body, dict) and key in self.json_body:
            return self.json_body[key]
        return default


class Backend:
    """把请求路由到内存世界。`handle()` 永不抛异常，异常都变成 5xx 响应。"""

    def __init__(self, world: Optional[World] = None, *, now: Optional[datetime] = None):
        self.world = world or build_world(now=now)
        self.now = now
        self.calls: list[tuple[str, str]] = []      # 请求轨迹（场景脚本会断言它）
        self._routes: list[tuple[str, re.Pattern, Callable]] = []
        self._register()

    # ---------------------------------------------------------------- 入口

    def handle(self, method: str, path: str, *, query: Optional[dict] = None,
               body: bytes = b"", headers: Optional[dict] = None) -> Response:
        headers = {str(k).lower(): v for k, v in (headers or {}).items()}
        query = dict(query or {})
        form, json_body = self._parse_body(body, headers)
        req = Request(method.upper(), path.rstrip("/") or "/", query, form,
                      json_body, headers)
        self.calls.append((req.method, req.path))
        try:
            handler = self._match(req)
            if handler is None:
                return _err(404, f"没有这个接口：{req.method} {req.path}")
            # ⚠️ 鉴权对**所有**非公开路由生效，不能只卡写操作：
            # GET 同样要认人（用户路径、活动列表都依赖 sub）
            if not self._is_public(req.path):
                claims = self._claims(req)
                if claims is None:
                    return _err(401, "未登录或 token 失效")
                req.headers["_uid"] = claims["sub"]
            return handler(req)
        except Exception as e:                      # noqa: BLE001 - 模拟器不该崩
            return _err(500, f"模拟器内部错误：{type(e).__name__}: {e}")

    # ---------------------------------------------------------------- 基础设施

    @staticmethod
    def _parse_body(body: bytes, headers: dict) -> tuple[dict, Any]:
        text = (body or b"").decode("utf-8", "replace")
        ctype = str(headers.get("content-type") or "")
        if "json" in ctype:
            try:
                return {}, json.loads(text or "{}")
            except json.JSONDecodeError:
                return {}, None
        form = {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}
        if not form and "json" in str(headers.get("accept") or ""):
            try:
                return {}, json.loads(text or "{}")
            except json.JSONDecodeError:
                pass
        return form, None

    def _claims(self, req: Request) -> Optional[dict]:
        auth = str(req.headers.get("authorization") or "")
        if not auth.lower().startswith("bearer "):
            return None
        token = auth.split(" ", 1)[1].strip()
        try:
            claims = codec.verify_jwt(token)
        except codec.CodeError:
            return None
        uid = int(claims.get("sub") or 0)
        if uid not in self.world.users:
            return None
        return claims

    @staticmethod
    def _is_public(path: str) -> bool:
        return (path.startswith("/api/v1/auth/")
                or path == "/config/index.json"
                or path.startswith("/api/v1/event/")
                or path.startswith("/api/v1/image/"))

    def _match(self, req: Request) -> Optional[Callable]:
        for method, pattern, handler in self._routes:
            if method != req.method:
                continue
            m = pattern.fullmatch(req.path)
            if m:
                req.headers["_params"] = m.groupdict()
                return handler
        return None

    # ---------------------------------------------------------------- 路由注册

    def _route(self, method: str, path: str):
        """把 `/api/v1/activity/{id}` 编成正则：`{name}` → 命名组，其余字面。"""
        pieces = []
        for part in re.split(r"(\{[A-Za-z_]+\})", path):
            if part.startswith("{") and part.endswith("}"):
                pieces.append(f"(?P<{part[1:-1]}>[^/]+)")
            else:
                pieces.append(re.escape(part))
        pattern = re.compile("^" + "".join(pieces) + "/?$")

        def deco(fn: Callable) -> Callable:
            self._routes.append((method, pattern, fn))
            return fn
        return deco

    def _path_args(self, req: Request) -> dict:
        return dict(req.headers.get("_params") or {})

    def _uid(self, req: Request) -> int:
        return int(req.headers.get("_uid") or 0)

    def _user(self, req: Request):
        return self.world.users.get(self._uid(req))

    # ---------------------------------------------------------------- 注册

    def _register(self) -> None:
        r = self._route

        # ---- 认证 ----
        @r("POST", "/api/v1/auth/sessions")
        def auth_login(req: Request) -> Response:
            username = str(req.get("username") or "")
            password = str(req.get("password") or "")
            user = self.world.user_by_username(username)
            if user is None or user.password != password:
                return _err(401, "用户名或密码错误")
            if user.banned_type & codec.BANNED_SIGNUP and user.banned_before:
                return _err(403, "账号已被限制")
            token = codec.sign_jwt({
                "sub": user.id, "exp": int(time.time()) + 86400 * 7,
                "banned_type": user.banned_type,
                "banned_before": user.banned_before,
            })
            return _ok(token)

        @r("POST", "/api/v1/auth/wechat/sessions")
        def auth_wechat(req: Request) -> Response:
            # 报告：404 = openid 未绑定 → 客户端跳账号密码登录
            if str(req.get("code") or "") == "bound":
                user = next(iter(self.world.users.values()))
                return _ok(codec.sign_jwt({"sub": user.id,
                                           "exp": int(time.time()) + 86400 * 7}))
            return _err(404, "微信未绑定")

        @r("DELETE", "/api/v1/auth/wechat")
        def auth_unbind(req: Request) -> Response:
            return _ok(True)

        # ---- 用户 / 角色 ----
        @r("GET", "/api/v1/user/{currentUserId}")
        def user_get(req: Request) -> Response:
            user = self.world.users.get(int(self._path_args(req)["currentUserId"]))
            if user is None:
                return _err(404, "用户不存在")
            return _ok(user.public(), user.digest())

        @r("PUT", "/api/v1/user/{currentUserId}")
        def user_update(req: Request) -> Response:
            user = self._user(req)
            email = str(req.get("email") or "")
            phone = str(req.get("phone") or "")
            if email and ("@" not in email or "." not in email.split("@")[-1]):
                return _err(422, "邮箱格式不正确")
            if phone and not re.fullmatch(r"1\d{10}", phone):
                return _err(422, "手机号格式不正确")
            user.email = email or user.email
            user.phone = phone or user.phone
            user.version += 1
            return _ok(True, user.digest())

        @r("GET", "/api/v1/role/normal")
        def role_normal(req: Request) -> Response:
            return _ok(self.world.roles)

        @r("GET", "/api/v1/group/org-college")
        def group_college(req: Request) -> Response:
            return _ok(self.world.colleges)

        @r("GET", "/api/v1/group/societies")
        def group_societies(req: Request) -> Response:
            return _ok(self.world.societies)

        # ---- 活动 ----
        @r("GET", "/api/v1/activity")
        def activity_list(req: Request) -> Response:
            user = self._user(req)
            if user is None:
                return _err(401, "未登录")
            if str(req.query.get("digest") or "") and \
                    str(req.query["digest"]) != user.digest():
                return _err(409, "digest 已过期")
            is_group = int(req.query.get("is_group") or 0)
            items, digests = [], []
            for act in sorted(self.world.activities.values(), key=lambda a: -a.priority):
                if is_group and not act.is_group:
                    continue
                if not is_group and act.is_group:
                    continue
                if act.grade and act.grade != user.grade:
                    continue
                items.append(act.public())
                digests.append(act.digest())
            return _ok(items, digests)

        @r("GET", "/api/v1/activity/class")
        def activity_class(req: Request) -> Response:
            return _ok(self.world.classes)

        @r("GET", "/api/v1/activity/{id}")
        def activity_get(req: Request) -> Response:
            act = self.world.activities.get(int(self._path_args(req)["id"]))
            if act is None:
                return _err(404, "活动不存在")
            return _ok(act.public(), act.digest())

        # ---- 报名 ----
        @r("POST", "/api/v1/participation/{act_id}")
        def activity_join(req: Request) -> Response:
            user = self._user(req)
            act = self.world.activities.get(int(self._path_args(req)["act_id"]))
            if act is None:
                return _err(404, "活动不存在")
            # 顺序与报告 §4.2 一致：封禁 → 报名窗口 → 已结束 → 名额 → digest
            if user.banned_type & codec.BANNED_SIGNUP:
                return _err(403, "你被限制报名")
            if str(req.get("digest") or "") != act.digest():
                return _err(409, "digest 已过期")
            now = self._now()
            start, end = parse_iso(act.signup_start), parse_iso(act.signup_end)
            if start and now < start:
                return _err(409, "报名尚未开始")
            if end and now > end:
                return _err(409, "报名已截止")
            if act.state == 0:
                return _err(409, "活动已结束")
            taken = sum(1 for (uid, aid) in self.world.participations if aid == act.id)
            if taken >= act.attend_limit:
                return _err(409, "名额已满")
            if self.world.is_participating(user.id, act.id):
                return _err(409, "已经报名")
            self.world.participations[(user.id, act.id)] = {
                "user_id": user.id, "act_id": act.id,
                "start_t_ms": int(req.get("start_t_ms") or 0),
                "end_t_ms": int(req.get("end_t_ms") or 0),
                "created_at": iso(now),
            }
            act.version += 1                       # 名额变了 → digest 变
            self.world.add_score(user.id, 1, 1, f"参与活动：{act.name}")
            return _ok(True, act.digest())

        @r("DELETE", "/api/v1/participation/{act_id}")
        def activity_exit(req: Request) -> Response:
            user = self._user(req)
            act = self.world.activities.get(int(self._path_args(req)["act_id"]))
            if act is None:
                return _err(404, "活动不存在")
            if str(req.get("digest") or "") != act.digest():
                return _err(409, "digest 已过期")
            if not self.world.is_participating(user.id, act.id):
                return _err(409, "没有报名这个活动")
            self.world.participations.pop((user.id, act.id), None)
            act.version += 1
            self.world.add_score(user.id, -1, 0, f"取消报名：{act.name}")
            return _ok(True, act.digest())

        @r("GET", "/api/v1/participation/acts")
        def participation_mine(req: Request) -> Response:
            user = self._user(req)
            page = max(1, int(req.query.get("page") or 1))
            size = max(1, int(req.query.get("page_size") or 40))
            rows = []
            for (uid, aid), part in self.world.participations.items():
                if uid != user.id:
                    continue
                act = self.world.activities.get(aid)
                if act:
                    rows.append({**act.public(), "participation": part})
            rows.sort(key=lambda x: x.get("activity_start_time") or "", reverse=True)
            chunk = rows[(page - 1) * size: page * size]
            return _ok(chunk, [r["digest"] for r in chunk])

        # ---- 管理 ----
        @r("GET", "/api/v1/participation/admin/act")
        def admin_acts(req: Request) -> Response:
            user = self._user(req)
            rows = []
            for act in self.world.activities.values():
                if act.belong_to != user.college_id:
                    continue
                rows.append({**act.public(), "identity": 2,
                             "managers": [{"user_id": user.id, "identity": 2}]})
            return _ok(rows, [a["digest"] for a in rows])

        @r("GET", "/api/v1/participation/admin/act/{act_id}")
        def admin_managers_get(req: Request) -> Response:
            act = self.world.activities.get(int(self._path_args(req)["act_id"]))
            if act is None:
                return _err(404, "活动不存在")
            if str(req.get("digest") or "") != act.digest():
                return _err(409, "digest 已过期")
            return _ok([{"user_id": self._uid(req), "identity": 2}], act.digest())

        @r("POST", "/api/v1/participation/admin/act/{act_id}")
        def admin_managers_add(req: Request) -> Response:
            act = self.world.activities.get(int(self._path_args(req)["act_id"]))
            if act is None:
                return _err(404, "活动不存在")
            if str(req.get("digest") or "") != act.digest():
                return _err(409, "digest 已过期")
            if not str(req.get("new_user_id") or "").isdigit():
                return _err(422, "new_user_id 不合法")
            act.version += 1
            return _ok(True, act.digest())

        @r("DELETE", "/api/v1/participation/admin/act/{act_id}")
        def admin_managers_del(req: Request) -> Response:
            act = self.world.activities.get(int(self._path_args(req)["act_id"]))
            if act is None:
                return _err(404, "活动不存在")
            if str(req.get("digest") or "") != act.digest():
                return _err(409, "digest 已过期")
            act.version += 1
            return _ok(True, act.digest())

        @r("PUT", "/api/v1/participation/admin/act/{act_id}")
        def admin_act_end(req: Request) -> Response:
            act = self.world.activities.get(int(self._path_args(req)["act_id"]))
            if act is None:
                return _err(404, "活动不存在")
            if str(req.get("digest") or "") != act.digest():
                return _err(409, "digest 已过期")
            act.state = 0
            act.version += 1
            return _ok(True, act.digest())

        # ---- 订阅 / 偏好 ----
        @r("GET", "/api/v1/preference")
        def preference_get(req: Request) -> Response:
            return _ok(self.world.preferences.get(self._uid(req), {}))

        @r("PUT", "/api/v1/preference")
        def preference_put(req: Request) -> Response:
            body = req.json_body if isinstance(req.json_body, dict) else {
                k: v for k, v in req.form.items()}
            self.world.preferences[self._uid(req)] = body
            return _ok(True)

        @r("POST", "/api/v1/subscription/{activity_id}")
        def subscription_add(req: Request) -> Response:
            act_id = int(self._path_args(req)["activity_id"])
            if act_id not in self.world.activities:
                return _err(404, "活动不存在")
            self.world.subscriptions[(self._uid(req), act_id)] = {
                "college_id": req.get("college_id"), "grade": req.get("grade"),
                "role_id": req.get("role_id")}
            return _ok(True)

        @r("DELETE", "/api/v1/subscription/{activity_id}")
        def subscription_del(req: Request) -> Response:
            act_id = int(self._path_args(req)["activity_id"])
            self.world.subscriptions.pop((self._uid(req), act_id), None)
            return _ok(True)

        # ---- 签到 ----
        @r("GET", "/api/v1/checkin/qrcode/user")
        def checkin_user_code(req: Request) -> Response:
            user = self._user(req)
            trace = str(req.headers.get("x-trace") or "")
            if not trace:
                trace = codec.new_trace()
            codec.parse_trace(trace)
            now = int(time.time())
            payload = codec.CheckinCode(
                iat=now, exp=now + codec.QR_TTL, sub=user.id, act_id=-1,
                opr_type=0, act_demand=codec.DEMAND_CHECKIN,
                digest=user.digest(), trace=trace)
            self.world.checkin_sessions[trace] = {
                "user_id": user.id, "trace": trace, "status": "Waiting",
                "created_at": now, "act_id": None}
            return _ok(payload.to_dict())

        @r("GET", "/api/v1/checkin/status")
        def checkin_status(req: Request) -> Response:
            trace = str(req.query.get("trace") or "")
            session = self.world.checkin_sessions.get(trace)
            if session is None:
                return _err(404, "trace 不存在")
            return _ok({"msg": session["status"], "act_id": session.get("act_id")})

        @r("POST", "/api/v1/checkin/passive")
        def checkin_passive(req: Request) -> Response:
            raw = str(req.headers.get("x-token") or "")
            try:
                decoded = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
            except Exception:                       # noqa: BLE001
                return _err(422, "X-Token 不是合法的 Base64 JSON")
            act_id = int(decoded.get("act_id") or 0)
            sub = int(decoded.get("sub") or 0)
            trace = str(decoded.get("trace") or "")
            act = self.world.activities.get(act_id)
            if act is None:
                return _err(404, "活动不存在")
            if not self.world.is_participating(sub, act_id):
                return _err(409, "该用户没有报名这个活动")
            if not act.demands & codec.DEMAND_CHECKIN:
                return _err(409, "该活动不支持签到")
            session = self.world.checkin_sessions.get(trace)
            if session is not None:
                session["status"] = "Success"
                session["act_id"] = act_id
            self.world.checkin_events.insert(0, {
                "id": self.world.next_id(), "act_id": act_id, "user_id": sub,
                "opr_type": int(decoded.get("opr_type") or 0),
                "created_at": iso(datetime.now(timezone.utc)),
                "by": self._uid(req)})
            return _ok(True)

        @r("POST", "/api/v1/checkin/ble/{packet}")
        def checkin_ble(req: Request) -> Response:
            packet = self._path_args(req)["packet"]
            try:
                ble = codec.parse_ble_packet(packet)
            except codec.BLEError as e:
                return _err(422, str(e))
            user = self._user(req)
            act = self.world.activities.get(ble["act_id"])
            if act is None:
                return _err(404, "活动不存在")
            if not self.world.is_participating(user.id, act.id):
                return _err(409, "没有报名这个活动")
            self.world.checkin_events.insert(0, {
                "id": self.world.next_id(), "act_id": act.id, "user_id": user.id,
                "opr_type": ble["opr_type"],
                "created_at": iso(datetime.now(timezone.utc)), "by": "ble"})
            return _ok(True, extra={"act_id": act.id})

        @r("GET", "/api/v1/checkin/qrcode/admin/{act_id}/{opr_type}")
        def checkin_admin_code(req: Request) -> Response:
            args = self._path_args(req)
            act = self.world.activities.get(int(args["act_id"]))
            if act is None:
                return _err(404, "活动不存在")
            now = int(time.time())
            payload = codec.CheckinCode(
                iat=now, exp=now + codec.QR_TTL, sub=self._uid(req), act_id=act.id,
                opr_type=int(args["opr_type"]), act_demand=codec.DEMAND_MANAGE,
                digest=act.digest(), trace=codec.new_trace())
            return _ok(payload.to_dict(), act.digest())

        @r("GET", "/api/v1/checkin/dynamic_list")
        def checkin_dynamic(req: Request) -> Response:
            act_id = int(req.query.get("act_id") or 0)
            opr = int(req.query.get("opr_type") or 0)
            limit = max(1, min(int(req.query.get("limit") or 20), 100))
            after = int(req.query.get("last_id") or 0)
            rows = [e for e in self.world.checkin_events
                    if e["act_id"] == act_id and e["opr_type"] == opr
                    and (not after or e["id"] < after)]
            return _ok(rows[:limit])

        # ---- 资讯 ----
        @r("GET", "/api/v1/news/preview")
        def news_preview(req: Request) -> Response:
            return _ok(self.world.news[:5])

        @r("GET", "/api/v1/news/tree")
        def news_tree(req: Request) -> Response:
            return _ok(self.world.news_tree)

        @r("POST", "/api/v1/news/search")
        def news_search(req: Request) -> Response:
            body = req.json_body if isinstance(req.json_body, dict) else {}
            query = str(body.get("query") or "")
            size = int(body.get("size") or 10)
            offset = int(body.get("offset") or 0)
            hits = [n for n in self.world.news if not query or query in n["title"]]
            page = hits[offset:offset + size]
            # 真实接口这里 data 是**字符串**，客户端要二次 JSON.parse（报告 §7）
            return _ok(json.dumps({"hits": page, "total": len(hits)},
                                  ensure_ascii=False))

        # ---- 商城 ----
        @r("GET", "/api/v1/store/product")
        def store_products(req: Request) -> Response:
            status = int(req.query.get("status") or 0)
            rows = [p.public() for p in self.world.products.values()
                    if status == -1 or p.status == status]
            return _ok(rows)

        @r("GET", "/api/v1/store/product/{id}")
        def store_product(req: Request) -> Response:
            p = self.world.products.get(int(self._path_args(req)["id"]))
            return _ok(p.public()) if p else _err(404, "商品不存在")

        @r("POST", "/api/v1/store/order")
        def store_order(req: Request) -> Response:
            body = req.json_body if isinstance(req.json_body, dict) else {}
            product = self.world.products.get(int(body.get("product_id") or 0))
            if product is None:
                return _err(404, "商品不存在")
            count = max(1, int(body.get("count") or 1))
            user = self._user(req)
            if user.banned_type & codec.BANNED_REDEEM:
                return _err(403, "你被限制兑换")
            if product.status != 0:
                return _err(409, "商品已售罄")
            if product.stock < count:
                return _err(409, "库存不足")
            if count > product.limit:
                return _err(409, "超过限购数量")
            total = product.price * count
            purse = user.college_score if product.score_type == 0 else user.school_score
            if purse < total:
                return _err(409, "积分不足")
            product.stock -= count
            if product.stock == 0:
                product.status = 1
            product.version += 1
            order_id = self.world.next_id()
            self.world.orders.insert(0, {
                "id": order_id, "order_code": order_id, "product_id": product.id,
                "product_name": product.name, "count": count, "score": total,
                "code": codec.enc_redeem_code(order_id),
                "state": 0, "created_at": iso(datetime.now(timezone.utc))})
            self.world.add_score(user.id, -total, -1, f"兑换：{product.name}")
            return _ok(self.world.orders[0])

        @r("GET", "/api/v1/store/orderRecords")
        def store_orders(req: Request) -> Response:
            page = max(1, int(req.query.get("page") or 1))
            limit = max(1, int(req.query.get("limit") or 10))
            rows = self.world.orders[(page - 1) * limit: page * limit]
            return _ok(rows)

        @r("GET", "/api/v1/store/scoreRecords")
        def store_score_records(req: Request) -> Response:
            page = max(1, int(req.query.get("page") or 1))
            limit = max(1, int(req.query.get("limit") or 10))
            rows = self.world.score_records.get(self._uid(req), [])
            return _ok(rows[(page - 1) * limit: page * limit])

        @r("GET", "/api/v1/store/score")
        def store_score(req: Request) -> Response:
            user = self._user(req)
            return _ok({"college_score": user.college_score,
                        "school_score": user.school_score})

        # ---- 成绩单 ----
        @r("GET", "/api/v1/transcript/pdf/generate-jobs")
        def transcript_jobs(req: Request) -> Response:
            rows = [j for j in self.world.transcript_jobs if j["user_id"] == self._uid(req)]
            result = {"waiting": [], "active": [], "completed": [], "failed": []}
            for job in rows:
                result[job["state"]].append(job)
            return _ok({"result": result})

        @r("POST", "/api/v1/transcript/pdf/generate")
        def transcript_generate(req: Request) -> Response:
            body = req.json_body if isinstance(req.json_body, dict) else {}
            email = str(body.get("email") or "")
            semesters = body.get("semesters") or []
            types = body.get("types") or []
            if not email or "@" not in email:
                return _err(422, "邮箱不合法")
            if not semesters or not types:
                return _err(422, "学期与类型不能为空")
            job = {"id": self.world.next_id(), "user_id": self._uid(req),
                   "email": email, "semesters": semesters, "types": types,
                   "state": "waiting",
                   "created_at": iso(datetime.now(timezone.utc))}
            self.world.transcript_jobs.insert(0, job)
            return _ok(job)

        # ---- 辅助 ----
        @r("GET", "/config/index.json")
        def misc_config(req: Request) -> Response:
            return Response(200, {
                "notice": "模拟环境",
                "security": {"disableLogout": False},
                "features": {"urls": {"enable": True, "data": []}},
            })

        @r("POST", "/api/v1/event/send")
        def misc_umami(req: Request) -> Response:
            self.world.umami_events.append({"body": req.json_body})
            return _ok(True)

        @r("GET", "/api/v1/image/{imageId}")
        def misc_image(req: Request) -> Response:
            return Response(200, {"data": "<png>"},
                            headers={"content-type": "image/png"})

    # ---------------------------------------------------------------- 工具

    def _now(self) -> datetime:
        return self.now or datetime.now(timezone.utc)
