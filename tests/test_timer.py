"""TimerManager 计时精度回归。

历史缺陷（本次修掉）：

1. **周期把任务耗时算进去**：旧实现是「跑完任务再 `sleep(seconds)`」，每轮实际
   周期 = 任务耗时 + interval，误差持续累积（实测 40ms 任务 + 100ms 间隔 →
   141ms/轮，12 轮累计 +493ms；一天就是几十分钟）。现在按**绝对时刻**排程。
2. **落后时补跑**：任务比间隔还久（或进程被挂起）时，旧实现会立刻接着跑下一轮，
   连发消息 / 打满 CPU。现在跳过错过的轮次、对齐到下一个未来时刻。
3. **每日定时在月末崩**：`target.replace(day=now.day + 1)` 在 1 月 31 日变成
   `day=32` → ValueError，每月最后一两天整天不跑。改用 `timedelta(days=1)`。
4. **间隔 <= 0**：旧实现会变成忙等。现在注册时就 `ValueError`。

这些用例用**真实时钟**（不 mock），所以阈值留了调度器抖动的余量；跑在慢机器上也应通过。
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from napcat.events import TimerManager, next_daily_target          # noqa: E402
from napcat.errors import ErrorReporter                            # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  ✅ {name}")
    else:
        FAILED += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))
    return bool(cond)


async def collect(interval: float, job_cost: float, rounds: int) -> list[float]:
    """跑一个定时任务，返回每次**开始执行**的时刻（monotonic）。"""
    tm = TimerManager()
    tm.start()
    stamps: list[float] = []

    @tm.every(interval, name="probe")
    async def _job():
        stamps.append(asyncio.get_running_loop().time())
        if job_cost:
            await asyncio.sleep(job_cost)

    # 首轮固定延迟 1s（框架启动预留），再多等一点余量
    deadline = asyncio.get_running_loop().time() + 1.0 + interval * (rounds + 1)
    while asyncio.get_running_loop().time() < deadline and len(stamps) < rounds + 1:
        await asyncio.sleep(0.005)
    tm.stop()
    await asyncio.sleep(0)
    return stamps


async def test_no_drift_with_slow_job():
    print("\n[1] 周期不含任务耗时（这是原来偏差的来源）")
    interval, cost = 0.06, 0.03            # 任务耗时 = 半个周期
    stamps = await collect(interval, cost, rounds=8)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    check("样本足够", len(gaps) >= 6, f"{len(gaps)} 个间隔")
    if len(gaps) < 6:
        return
    mean = statistics.mean(gaps)
    check(f"平均间隔 ≈ {interval*1000:.0f}ms（不叠加 {cost*1000:.0f}ms 任务耗时）",
          abs(mean - interval) < interval * 0.12,
          f"实测 {mean*1000:.1f}ms")
    drift = (stamps[-1] - stamps[0]) - interval * len(gaps)
    check("多轮累计偏差很小（旧实现这里每轮 +30ms）",
          abs(drift) < 0.05, f"累计 {drift*1000:+.1f}ms / {len(gaps)} 轮")


async def test_no_drift_fast_job():
    print("\n[2] 空任务也不累积偏差")
    stamps = await collect(0.05, 0.0, rounds=10)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    if not check("样本足够", len(gaps) >= 8, f"{len(gaps)} 个间隔"):
        return
    drift = (stamps[-1] - stamps[0]) - 0.05 * len(gaps)
    check("累计偏差 < 40ms", abs(drift) < 0.04, f"{drift*1000:+.1f}ms")


async def test_catch_up_is_skipped():
    print("\n[3] 任务比间隔久时：不补跑，最多按任务速度跑")
    tm = TimerManager()
    tm.start()
    calls: list[float] = []

    @tm.every(0.05, name="slow")
    async def _slow():
        calls.append(asyncio.get_running_loop().time())
        await asyncio.sleep(0.12)          # 比间隔慢 2.4 倍

    await asyncio.sleep(1.0 + 0.6)
    tm.stop()
    await asyncio.sleep(0)
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    check("没有爆发式补跑（相邻执行不短于间隔）",
          all(g >= 0.045 for g in gaps), f"{[round(g*1000) for g in gaps]}")
    check("执行次数按「任务速度」而不是「空转补跑」",
          2 <= len(calls) <= 6, f"{len(calls)} 次（0.6s / 150ms ≈ 4）")


async def test_cancel_and_pause():
    print("\n[4] cancel / pause 之后不再执行")
    tm = TimerManager()
    tm.start()
    n = {"v": 0}

    @tm.every(0.03, name="counted")
    async def _tick():
        n["v"] += 1

    await asyncio.sleep(1.0 + 0.12)
    first = n["v"]
    check("cancel 前在跑", first >= 2, str(first))
    tm.cancel("counted")
    frozen = n["v"]
    await asyncio.sleep(0.15)
    check("cancel 后立刻停（不再增长）", n["v"] == frozen, f"{frozen} → {n['v']}")

    tm2 = TimerManager()
    tm2.start()

    @tm2.every(0.03, name="pausable")
    async def _tick2():
        n["v"] += 1

    await asyncio.sleep(1.0 + 0.1)
    check("pause 成功", tm2.pause_timer("pausable") is True)
    paused_at = n["v"]
    await asyncio.sleep(0.12)
    check("pause 后不再增长", n["v"] == paused_at, f"{paused_at} → {n['v']}")
    check("resume 成功", tm2.resume_timer("pausable") is True)
    await asyncio.sleep(1.0 + 0.1)
    check("resume 后恢复执行", n["v"] > paused_at, f"{paused_at} → {n['v']}")
    tm2.stop()


async def test_bad_interval():
    print("\n[5] 非法间隔在注册时就报错")
    tm = TimerManager()
    for bad in (0, -1, -0.5):
        try:
            tm.every(bad, name=f"bad{bad}")(lambda: None)
            check(f"间隔 {bad} 应报错", False, "没有抛异常")
        except ValueError as e:
            check(f"间隔 {bad} 抛 ValueError", "必须 > 0" in str(e), str(e))


def test_daily_target():
    print("\n[6] 每日定时的下一次触发时刻（含月末/闰年）")
    cases = [
        (datetime(2026, 1, 31, 23, 0), 8, 0, datetime(2026, 2, 1, 8, 0), "1/31 → 2/1"),
        (datetime(2026, 2, 28, 23, 0), 8, 0, datetime(2026, 3, 1, 8, 0), "非闰年 2/28 → 3/1"),
        (datetime(2024, 2, 29, 23, 30), 8, 0, datetime(2024, 3, 1, 8, 0), "闰年 2/29 → 3/1"),
        (datetime(2026, 5, 31, 9, 0), 8, 0, datetime(2026, 6, 1, 8, 0), "5/31 → 6/1"),
        (datetime(2026, 1, 1, 7, 59), 8, 0, datetime(2026, 1, 1, 8, 0), "当天还没到点 → 当天"),
        (datetime(2026, 1, 1, 8, 0), 8, 0, datetime(2026, 1, 2, 8, 0), "正好到点 → 次日（严格大于）"),
    ]
    for now, h, m, want, label in cases:
        got = next_daily_target(now, h, m)
        check(f"{label}：{got}", got == want, f"期望 {want}")


async def test_error_does_not_stop_timer():
    print("\n[7] 单轮抛异常不影响后续轮次（错误走汇报、任务继续）")
    tm = TimerManager()
    reported: list[str] = []

    class _Logger:
        def log(self, level, msg, *args, **kwargs):
            reported.append(str(msg))

        def error(self, *a, **k):
            reported.append(str(a))

    tm.set_error_reporter(ErrorReporter(logger=_Logger(), enabled=False))
    tm.start()
    runs = {"n": 0}

    @tm.every(0.04, name="boom")
    async def _boom():
        runs["n"] += 1
        raise RuntimeError("这一轮失败")

    await asyncio.sleep(1.0 + 0.3)
    tm.stop()
    await asyncio.sleep(0)
    check("继续跑了多轮", runs["n"] >= 3, str(runs["n"]))
    check("错误有汇报", bool(reported), str(reported[:1]))



async def test_operator_pause_command():
    print("\n[8] 运维路径：/pause timer 的暂停与恢复")
    from napcat.command import CommandManager
    from napcat.models import MessageEvent
    from napcat.permission import PermissionManager

    tm = TimerManager()
    tm.start()
    n = {"v": 0}

    @tm.every(0.03, name="op.timer")
    async def _tick():
        n["v"] += 1

    perm = PermissionManager(superusers=[1])
    cmds = CommandManager(prefixes=["/"], perm_manager=perm, timer_ref=tm)

    def ev(text):
        return MessageEvent(message_type="private", user_id=1, message=text)

    await asyncio.sleep(1.0 + 0.1)
    r1 = await cmds.handle(ev("/pause timer op.timer"))
    check("/pause timer → 已暂停", "已暂停" in str(r1), repr(r1))
    frozen = n["v"]
    await asyncio.sleep(0.12)
    check("暂停后不再增长", n["v"] == frozen, f"{frozen} → {n['v']}")
    # 旧代码在这一步 KeyError（_metas 里没有 func），命令只会回一句错误
    r2 = await cmds.handle(ev("/pause timer op.timer"))
    check("/pause timer 第二次 → 已恢复（旧代码 KeyError）", "已恢复" in str(r2), repr(r2))
    await asyncio.sleep(1.0 + 0.15)
    check("恢复后继续增长", n["v"] > frozen, f"{frozen} → {n['v']}")
    tm.stop()
    await asyncio.sleep(0)


async def main() -> int:
    started = time.time()
    await test_no_drift_with_slow_job()
    await test_no_drift_fast_job()
    await test_catch_up_is_skipped()
    await test_cancel_and_pause()
    await test_bad_interval()
    test_daily_target()
    await test_error_does_not_stop_timer()
    await test_operator_pause_command()
    print(f"\n{'=' * 50}\n通过 {PASSED} / 失败 {FAILED}（{time.time() - started:.1f}s）")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
