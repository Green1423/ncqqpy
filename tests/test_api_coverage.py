"""API 覆盖检查：`napcat/api.py` 与 NapCat 官方文档（Apifox）对齐。

官方文档：<https://napcat.apifox.cn/llms.txt>（每个接口页内含 OpenAPI 片段）。

测试内容：

1. 官方 178 个 action 全部有对应的 `ApiClient` 方法（无遗漏、无自造 action）；
2. 字段名与官方一致（`set` / `longNick` / `fileset_id` / `file_path` / `character` 等易错项）；
3. `send_forward_msg` 使用官方字段 `message`（兼容旧别名 `messages`）；
4. 群公告不发送官方已删除的 `title` 字段。
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from napcat.api import ApiClient                            # noqa: E402

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


#: 官方文档列出的全部 action（2026-10 抓取；新增/删除接口时同步更新）
OFFICIAL_ACTIONS = (
    ".handle_quick_operation",
    ".ocr_image",
    "ArkShareGroup",
    "ArkSharePeer",
    "_del_group_notice",
    "_get_group_notice",
    "_get_model_show",
    "_mark_all_as_read",
    "_send_group_notice",
    "_set_model_show",
    "add_custom_face",
    "bot_exit",
    "can_send_image",
    "can_send_record",
    "cancel_group_album_media_like",
    "cancel_group_todo",
    "cancel_online_file",
    "check_url_safely",
    "clean_cache",
    "clean_stream_temp_file",
    "click_inline_keyboard_button",
    "complete_group_todo",
    "create_collection",
    "create_flash_task",
    "create_group_file_folder",
    "del_group_album_media",
    "delete_custom_face",
    "delete_essence_msg",
    "delete_friend",
    "delete_group_file",
    "delete_group_folder",
    "delete_msg",
    "delete_qzone_msg",
    "do_group_album_comment",
    "download_file",
    "download_file_image_stream",
    "download_file_record_stream",
    "download_file_stream",
    "download_fileset",
    "fetch_custom_face",
    "fetch_custom_face_detail",
    "fetch_emoji_like",
    "fetch_ptt_text",
    "forward_friend_single_msg",
    "forward_group_single_msg",
    "friend_poke",
    "get_ai_characters",
    "get_ai_record",
    "get_clientkey",
    "get_collection_list",
    "get_cookies",
    "get_credentials",
    "get_csrf_token",
    "get_doubt_friends_add_request",
    "get_emoji_likes",
    "get_essence_msg_list",
    "get_file",
    "get_fileset_id",
    "get_fileset_info",
    "get_flash_file_list",
    "get_flash_file_url",
    "get_forward_msg",
    "get_friend_list",
    "get_friend_msg_history",
    "get_friends_with_category",
    "get_group_album_media_list",
    "get_group_at_all_remain",
    "get_group_detail_info",
    "get_group_file_system_info",
    "get_group_file_url",
    "get_group_files_by_folder",
    "get_group_honor_info",
    "get_group_ignore_add_request",
    "get_group_ignored_notifies",
    "get_group_info",
    "get_group_info_ex",
    "get_group_list",
    "get_group_member_info",
    "get_group_member_list",
    "get_group_msg_history",
    "get_group_root_files",
    "get_group_share_link",
    "get_group_shut_list",
    "get_group_signed_list",
    "get_group_system_msg",
    "get_guild_list",
    "get_guild_service_profile",
    "get_image",
    "get_login_info",
    "get_mini_app_ark",
    "get_msg",
    "get_online_clients",
    "get_online_file_msg",
    "get_private_file_url",
    "get_profile_like",
    "get_qun_album_list",
    "get_recent_contact",
    "get_record",
    "get_rkey",
    "get_rkey_server",
    "get_robot_uin_range",
    "get_share_link",
    "get_status",
    "get_stranger_info",
    "get_unidirectional_friend_list",
    "get_version_info",
    "group_poke",
    "mark_group_msg_as_read",
    "mark_msg_as_read",
    "mark_private_msg_as_read",
    "move_group_file",
    "nc_get_packet_status",
    "nc_get_rkey",
    "nc_get_user_status",
    "ocr_image",
    "receive_online_file",
    "refuse_online_file",
    "rename_group_file",
    "send_ark_share",
    "send_flash_msg",
    "send_forward_msg",
    "send_group_ai_record",
    "send_group_ark_share",
    "send_group_forward_msg",
    "send_group_msg",
    "send_group_sign",
    "send_like",
    "send_msg",
    "send_online_file",
    "send_online_folder",
    "send_packet",
    "send_poke",
    "send_private_forward_msg",
    "send_private_msg",
    "send_qzone_msg",
    "set_custom_face_desc",
    "set_diy_online_status",
    "set_doubt_friends_add_request",
    "set_essence_msg",
    "set_friend_add_request",
    "set_friend_remark",
    "set_group_add_option",
    "set_group_add_request",
    "set_group_admin",
    "set_group_album_media_like",
    "set_group_ban",
    "set_group_card",
    "set_group_kick",
    "set_group_kick_members",
    "set_group_leave",
    "set_group_member_invite_policy",
    "set_group_member_permissions",
    "set_group_name",
    "set_group_new_member_history_visibility",
    "set_group_portrait",
    "set_group_remark",
    "set_group_robot_add_option",
    "set_group_search",
    "set_group_sign",
    "set_group_special_title",
    "set_group_todo",
    "set_group_whole_ban",
    "set_input_status",
    "set_msg_emoji_like",
    "set_online_status",
    "set_qq_avatar",
    "set_qq_profile",
    "set_restart",
    "set_self_longnick",
    "test_download_stream",
    "trans_group_file",
    "translate_en2zh",
    "upload_file_stream",
    "upload_group_file",
    "upload_image_to_qun_album",
    "upload_images_to_qun_album",
    "upload_private_file",
    "upload_video_to_qun_album",
)


def api_actions() -> dict[str, str]:
    """解析 api.py：action → 方法名。"""
    tree = ast.parse((ROOT / "napcat" / "api.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ApiClient")
    out: dict[str, str] = {}
    for fn in cls.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name.startswith("_"):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "_call" and node.args \
                    and isinstance(node.args[0], ast.Constant):
                out.setdefault(str(node.args[0].value), fn.name)
    return out


def test_coverage():
    print("\n[1] 官方 action 全覆盖")
    got = api_actions()
    missing = sorted(set(OFFICIAL_ACTIONS) - set(got))
    extra = sorted(set(got) - set(OFFICIAL_ACTIONS))
    check(f"官方 {len(OFFICIAL_ACTIONS)} 个 action 全覆盖", not missing, "; ".join(missing[:8]))
    check("没有官方清单外的自造 action", not extra, "; ".join(extra[:8]))
    check("action 与方法一一对应", len(got) == len(set(got.values())), str(len(got)))


class _Result:
    ok = True
    retcode = 0
    message = ""
    wording = ""
    def __init__(self, data=None):
        self.data = {} if data is None else data


class _FakeWs:
    def __init__(self):
        self.calls = []
    async def request(self, payload):
        self.calls.append(payload)
        return _Result()


async def test_params():
    print("\n[2] 易错字段名")
    ws = _FakeWs()
    api = ApiClient(ws)

    async def call(coro):
        try:
            await coro
        except Exception:  # noqa: BLE001 - 返回值建模失败不影响字段断言
            pass
        return ws.calls[-1]

    payload = await call(api.set_msg_emoji_like(1, "2"))
    check("set_msg_emoji_like 用官方字段 set",
          payload["params"].get("set") is True and "is_add" not in payload["params"],
          str(payload))
    payload = await call(api.set_msg_emoji_like(1, "2", is_add=False))
    check("is_add 旧别名映射到 set=false", payload["params"].get("set") is False, str(payload))

    payload = await call(api.set_self_longnick("签名"))
    check("set_self_longnick 用 official 的 longNick",
          payload["params"].get("longNick") == "签名", str(payload))

    payload = await call(api.send_online_file("10001", "/tmp/a.txt", "a.txt"))
    check("send_online_file 字段 user_id/file_path/file_name",
          payload["params"] == {"user_id": "10001", "file_path": "/tmp/a.txt",
                                  "file_name": "a.txt"}, str(payload))

    payload = await call(api.send_flash_msg("set_1", user_id="10001"))
    check("send_flash_msg 用 fileset_id",
          payload["params"].get("fileset_id") == "set_1"
          and "fileSetId" not in payload["params"], str(payload))

    payload = await call(api.get_ai_record("c1", "100002", "你好"))
    check("get_ai_record 字段 character/group_id/text",
          payload["params"] == {"character": "c1", "group_id": "100002",
                                  "text": "你好"}, str(payload))

    payload = await call(api.get_ai_characters("100002"))
    check("get_ai_characters 带 chat_type",
          payload["params"].get("chat_type") == 1, str(payload))

    payload = await call(api.get_record(file="f"))
    check("get_record 带官方必填 out_format",
          payload["params"].get("out_format") == "mp3", str(payload))

    payload = await call(api.get_credentials("qun.qq.com"))
    check("get_credentials 带 domain",
          payload["params"].get("domain") == "qun.qq.com", str(payload))

    payload = await call(api.send_forward_msg(message=[{"type": "node"}]))
    check("send_forward_msg 用官方字段 message",
          "message" in payload["params"] and "messages" not in payload["params"],
          str(payload))
    payload = await call(api.send_forward_msg(messages=[{"type": "node"}]))
    check("messages 旧别名仍可用", "message" in payload["params"], str(payload))

    payload = await call(api.mark_msg_as_read(7))
    check("mark_msg_as_read 带 message_id",
          payload["params"].get("message_id") == 7, str(payload))

    payload = await call(api.delete_essence_msg(7, group_id="100002", msg_random=1))
    check("delete_essence_msg 带 group_id/msg_random",
          payload["params"].get("group_id") == "100002"
          and payload["params"].get("msg_random") == 1, str(payload))

    payload = await call(api.get_group_info_ex("100002"))
    check("新增 action 可调用（get_group_info_ex）",
          payload["action"] == "get_group_info_ex", str(payload))

    payload = await call(api.handle_quick_operation({}, {}))
    check("handle_quick_operation 带前导点",
          payload["action"] == ".handle_quick_operation", str(payload))

    payload = await call(api.ark_share_group("100002"))
    check("ArkShareGroup 保持官方大小写",
          payload["action"] == "ArkShareGroup", str(payload))

    payload = await call(api.ocr_image_internal("x"))
    check(".ocr_image 内部 action 可调用",
          payload["action"] == ".ocr_image", str(payload))

    payload = await call(api.send_group_notice("100002", "正文"))
    check("send_group_notice 不发送已删除的 title",
          "title" not in payload["params"]
          and payload["params"].get("content") == "正文", str(payload))

    payload = await call(api.upload_group_file("100002", "/f", "n"))
    check("upload_group_file 默认 upload_file=True",
          payload["params"].get("upload_file") is True, str(payload))

    payload = await call(api.set_group_add_request("flag", approve=True))
    check("set_group_add_request 不再强塞 sub_type",
          "sub_type" not in payload["params"], str(payload))


async def main() -> int:
    test_coverage()
    await test_params()
    print(f"\n{'=' * 52}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
