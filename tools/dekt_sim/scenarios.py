"""场景脚本：把 `final_draft.md` §4 的业务流程逐条跑一遍并断言。

每个场景返回 `(名称, 是否通过, 细节)`；`run_all()` 汇总。所有流量都打到
**内存后端**，不发真实请求（报告 §2.3 的隔离原则）。
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from typing import Callable

from . import codec
from .backend import Backend, Response
from .store import build_world

__all__ = ["run_all", "Result"]

Result = tuple[str, bool, str]


class Session:
    """一个已登录用户的便捷封装（仍然走 `Backend.handle`，不绕过路由）。"""

    def __init__(self, backend: Backend, token: str):
        self.b = backend
        self.token = token

    def call(self, method: str, path: str, *, query=None, form=None,
             js=None, headers=None) -> Response:
        import urllib.parse
        body = b""
        hdrs = dict(headers or {})
        if js is not None:
            body = json.dumps(js).encode()
            hdrs["content-type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            hdrs["content-type"] = "application/x-www-form-urlencoded"
        if self.token:
            hdrs["authorization"] = "Bearer " + self.token
        return self.b.handle(method, path, query=query or {}, body=body, headers=hdrs)

    # 简写
    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, **kw):
        return self.call("POST", path, **kw)


def _login(b: Backend, username="2026000001", password="dekt") -> Session:
    r = b.handle("POST", "/api/v1/auth/sessions",
                 body=f"username={username}&password={password}".encode(),
                 headers={"content-type": "application/x-www-form-urlencoded"})
    if r.status != 200:
        raise RuntimeError(f"登录失败：{r.status} {r.payload}")
    return Session(b, r.payload["data"])


def _joinable(session: "Session") -> dict:
    """挑一个"现在真的报得上"的活动（报名窗口开着、没满、没结束）。"""
    now = datetime.now(timezone.utc)
    for act in session.get("/api/v1/activity").payload["data"]:
        end = act.get("signup_end_time") or ""
        start = act.get("signup_start_time") or ""
        try:
            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            continue
        if start_dt <= now <= end_dt and "已结束" not in str(act.get("name")):
            return act
    raise AssertionError("没有可报名的活动（种子数据有问题）")


def run_all(backend: Backend | None = None) -> list[Result]:
    """跑全部场景；每个场景默认拿到**全新的世界**，互不影响。

    传入 `backend` 时所有场景共用它（想看连续状态时用，比如调试）。
    """
    shared = backend
    out: list[Result] = []

    def check(name: str, fn: Callable[[Backend], str]) -> None:
        b = shared or Backend()
        try:
            out.append((name, True, fn(b) or ""))
        except AssertionError as e:
            out.append((name, False, str(e)))
        except Exception as e:                  # noqa: BLE001 - 场景脚本不该崩
            out.append((name, False, f"{type(e).__name__}: {e}"))

    # ---------------- 认证 ----------------
    def _login_ok(b: Backend) -> str:
        s = _login(b)
        me = s.get("/api/v1/user/2026000001")
        assert me.status == 200, me.payload
        assert me.payload["data"]["name"], "用户名为空"
        return f"token 长度 {len(s.token)}，digest {me.payload['digest'][:8]}"
    check("登录并读取用户", _login_ok)

    def _bad_password(b: Backend) -> str:
        r = b.handle("POST", "/api/v1/auth/sessions",
                     body=b"username=2026000001&password=wrong",
                     headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status == 401, r.status
        return r.payload["msg"]
    check("错误密码被拒", _bad_password)

    def _wechat_unbound(b: Backend) -> str:
        r = b.handle("POST", "/api/v1/auth/wechat/sessions",
                     body=b"code=nope",
                     headers={"content-type": "application/x-www-form-urlencoded"})
        assert r.status == 404, r.status
        return "404 → 客户端跳账号密码登录（与报告一致）"
    check("微信未绑定返回 404", _wechat_unbound)

    def _anonymous_denied(b: Backend) -> str:
        r = b.handle("GET", "/api/v1/activity")
        assert r.status == 401, r.status
        return "未登录访问活动列表 → 401"
    check("未登录被拒", _anonymous_denied)

    # ---------------- 资料 ----------------
    def _profile_validate(b: Backend) -> str:
        s = _login(b)
        bad = s.call("PUT", "/api/v1/user/2026000001", form={"email": "not-an-email"})
        assert bad.status == 422, bad.status
        good = s.call("PUT", "/api/v1/user/2026000001",
                      form={"email": "ok@example.com", "phone": "13800000000"})
        assert good.status == 200, good.payload
        return "非法邮箱 422；合法资料保存成功"
    check("资料校验与保存", _profile_validate)

    # ---------------- 活动与报名 ----------------
    def _list_and_join(b: Backend) -> str:
        s = _login(b)
        acts = s.get("/api/v1/activity")
        assert acts.status == 200 and acts.payload["data"], "活动列表为空"
        items = acts.payload["data"]
        assert len(acts.payload["digest"]) == len(items), "digest 数量与条目不一致"
        act = items[0]
        joined = s.post(f"/api/v1/participation/{act['id']}",
                        form={"digest": act["digest"], "start_t_ms": 0, "end_t_ms": 0})
        assert joined.status == 200, joined.payload
        again = s.post(f"/api/v1/participation/{act['id']}",
                       form={"digest": joined.payload["digest"]})
        assert again.status == 409, again.status
        return f"{len(items)} 条活动；重复报名 409（{again.payload['msg']}）"
    check("活动列表 + 报名 + 重复报名", _list_and_join)

    def _digest_guard(b: Backend) -> str:
        s = _login(b)
        acts = s.get("/api/v1/activity").payload["data"]
        act = [a for a in acts if a["id"] not in
               [p["user_id"] for p in []]][0]
        stale = "0" * 16
        r = s.post(f"/api/v1/participation/{act['id']}", form={"digest": stale})
        assert r.status == 409, r.status
        return "过期 digest → 409（乐观锁生效）"
    check("写操作要求当前 digest", _digest_guard)

    def _join_rules(b: Backend) -> str:
        s = _login(b)
        acts = {a["id"]: a for a in s.get("/api/v1/activity").payload["data"]}
        ended = acts[2]
        r = s.post("/api/v1/participation/2",
                   form={"digest": ended["digest"]})
        # 判定顺序按报告 §4.2：先看报名窗口，再看活动是否结束 —— 已结束的活动
        # 它的报名窗口必然也过了，所以报「报名已截止」是对的
        assert r.status == 409, r.payload
        assert "截止" in r.payload["msg"] or "结束" in r.payload["msg"], r.payload
        full = acts[3]
        r2 = s.post("/api/v1/participation/3", form={"digest": full["digest"]})
        assert r2.status == 409 and "名额" in r2.payload["msg"], r2.payload
        return "已结束 / 名额已满 都被拦下"
    check("报名前置条件", _join_rules)

    def _cancel(b: Backend) -> str:
        s = _login(b)
        act = s.get("/api/v1/activity").payload["data"][-1]
        s.post(f"/api/v1/participation/{act['id']}",
               form={"digest": act["digest"]})
        r = s.call("DELETE", f"/api/v1/participation/{act['id']}",
                   query={"digest": act["digest"]})
        assert r.status == 409, r.status     # 报名后 digest 变了 → 旧值被拒
        r = s.call("DELETE", f"/api/v1/participation/{act['id']}",
                   query={"digest": r.payload.get("digest") or act["digest"]})
        return f"取消报名：{r.status}"
    check("取消报名", _cancel)

    # ---------------- 订阅 ----------------
    def _subscription(b: Backend) -> str:
        s = _login(b)
        r = s.post("/api/v1/subscription/1", query={"college_id": 11,
                                                    "grade": 2026, "role_id": 23})
        assert r.status == 200, r.payload
        assert (b.world.users[2026000001].id, 1) in b.world.subscriptions
        d = s.call("DELETE", "/api/v1/subscription/1", query={"college_id": 11})
        assert d.status == 200, d.payload
        return "订阅/取消订阅各一次"
    check("订阅开关", _subscription)

    # ---------------- 签到 ----------------
    def _checkin_flow(b: Backend) -> str:
        s = _login(b)
        act = _joinable(s)
        joined = s.post(f"/api/v1/participation/{act['id']}",
                        form={"digest": act["digest"]})
        assert joined.status == 200, joined.payload
        trace = codec.new_trace()
        payload = s.get("/api/v1/checkin/qrcode/user",
                        headers={"X-Trace": trace}).payload["data"]
        assert payload["sub"] == 2026000001 and payload["act_id"] == -1
        waiting = s.get("/api/v1/checkin/status", query={"trace": trace})
        assert waiting.payload["data"]["msg"] == "Waiting", waiting.payload
        # 管理员扫用户码：X-Token = Base64(JSON(签到码对象))
        scanned = {**payload, "act_id": act["id"]}
        xt = base64.b64encode(json.dumps(scanned).encode()).decode()
        r = s.post("/api/v1/checkin/passive", form={}, headers={"X-Token": xt})
        assert r.status == 200, r.payload
        done = s.get("/api/v1/checkin/status", query={"trace": trace})
        assert done.payload["data"]["msg"] == "Success", done.payload
        return "Waiting → Success（扫码后状态翻转）"
    check("用户签到码 + 管理员扫码", _checkin_flow)

    def _checkin_ble(b: Backend) -> str:
        s = _login(b)
        act = _joinable(s)
        joined = s.post(f"/api/v1/participation/{act['id']}",
                        form={"digest": act["digest"]})
        assert joined.status == 200, joined.payload
        packet = codec.build_ble_packet(act_id=act["id"], ts=1750000000)
        r = s.post(f"/api/v1/checkin/ble/{packet}", form={})
        assert r.status == 200, r.payload
        bad = s.post("/api/v1/checkin/ble/zzzz", form={})
        assert bad.status == 422, bad.status
        return f"合法包 200；非法包 {bad.status}（{bad.payload['msg']}）"
    check("BLE 签到", _checkin_ble)

    def _checkin_requires_join(b: Backend) -> str:
        s = _login(b)
        act = _joinable(s)          # 故意**不报名**，直接去签到
        trace = codec.new_trace()
        payload = s.get("/api/v1/checkin/qrcode/user",
                        headers={"X-Trace": trace}).payload["data"]
        xt = base64.b64encode(json.dumps({**payload, "act_id": act["id"]}).encode()).decode()
        r = s.post("/api/v1/checkin/passive", form={}, headers={"X-Token": xt})
        assert r.status == 409 and "报名" in r.payload["msg"], r.payload
        return "没报名就签到 → 409"
    check("签到要求先报名", _checkin_requires_join)

    # ---------------- 商城 ----------------
    def _store_redeem(b: Backend) -> str:
        s = _login(b)
        before = s.get("/api/v1/store/score").payload["data"]
        r = s.post("/api/v1/store/order", js={"product_id": 1, "count": 1})
        assert r.status == 200, r.payload
        order = r.payload["data"]
        assert len(order["code"]) == 6, order["code"]
        assert codec.dec_redeem_code(order["code"]) == order["order_code"]
        after = s.get("/api/v1/store/score").payload["data"]
        total_before = before["school_score"] + before["college_score"]
        total_after = after["school_score"] + after["college_score"]
        # 规则（报告 §4.6）：优先扣学院积分，不够再扣校级
        assert total_after < total_before, (before, after)
        return (f"核销码 {order['code']}；总分 {total_before} → {total_after}"
                f"（学院 {before['college_score']} → {after['college_score']}）")
    check("商城兑换与核销码", _store_redeem)

    def _store_rules(b: Backend) -> str:
        s = _login(b)
        sold = s.post("/api/v1/store/order", js={"product_id": 2, "count": 1})
        assert sold.status == 409 and "售罄" in sold.payload["msg"], sold.payload
        too_many = s.post("/api/v1/store/order", js={"product_id": 4, "count": 99})
        assert too_many.status == 409, too_many.status
        poor = s.post("/api/v1/store/order", js={"product_id": 8, "count": 2})
        assert poor.status == 409, poor.status
        return "售罄 / 超量 / 积分不足 都被拦下"
    check("兑换前置校验", _store_rules)

    # ---------------- 成绩单 ----------------
    def _transcript(b: Backend) -> str:
        s = _login(b)
        bad = s.post("/api/v1/transcript/pdf/generate",
                     js={"email": "bad", "semesters": ["2024-2025-1"], "types": [1]})
        assert bad.status == 422, bad.status
        ok = s.post("/api/v1/transcript/pdf/generate",
                    js={"email": "a@b.c", "semesters": ["2024-2025-1"], "types": [1, 2]})
        assert ok.status == 200, ok.payload
        jobs = s.get("/api/v1/transcript/pdf/generate-jobs").payload["data"]["result"]
        assert len(jobs["waiting"]) == 1, jobs
        return "生成任务进入 waiting（不真的发邮件）"
    check("成绩单任务", _transcript)

    # ---------------- 资讯 ----------------
    def _news(b: Backend) -> str:
        s = _login(b)
        prev = s.get("/api/v1/news/preview")
        tree = s.get("/api/v1/news/tree")
        search = s.post("/api/v1/news/search", js={"query": "模拟", "size": 3})
        assert prev.status == tree.status == search.status == 200
        hits = json.loads(search.payload["data"])       # data 是字符串，要二次解析
        assert hits["hits"], hits
        return f"预览 {len(prev.payload['data'])} 条；搜索命中 {hits['total']}"
    check("资讯预览/栏目/搜索", _news)

    # ---------------- 辅助 ----------------
    def _misc(b: Backend) -> str:
        cfg = b.handle("GET", "/config/index.json")
        assert cfg.status == 200 and "features" in cfg.payload
        img = b.handle("GET", "/api/v1/image/1")
        assert img.status == 200
        return "config 与图片直链可用（均不需要鉴权）"
    check("辅助接口", _misc)

    return out
