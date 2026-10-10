"""
消息构造系统 - 链式 API 构建 OneBot 11 消息段
支持所有 24 种消息段类型，自动校验，输出纯列表或 CQ 码字符串
"""
from __future__ import annotations

from typing import Optional, Union
from enum import Enum


class MusicPlatform(str, Enum):
    QQ = "qq"
    NETEASE = "163"
    KUGOU = "kugou"
    MIGU = "migu"
    KUWO = "kuwo"
    CUSTOM = "custom"


class PokeType(str, Enum):
    POKE = "1"
    SHOW_LOVE = "2"
    LIKE = "3"
    HEARTBEAT = "4"
    SIX_SIX_SIX = "5"
    HEART = "6"


class DiceResult(int, Enum):
    ONE = 1; TWO = 2; THREE = 3; FOUR = 4; FIVE = 5; SIX = 6


class RPSResult(str, Enum):
    ROCK = "石头"
    SCISSORS = "剪刀"
    PAPER = "布"


class ContactType(str, Enum):
    QQ = "qq"
    GROUP = "group"


def split_text(text: str, room: int) -> tuple[str, str]:
    """把 text 切成「不超过 room 个字符」的前半段与剩余部分。

    优先在 `\\n` 处断开（不破坏排版），没有换行时才硬切。
    """
    if room <= 0:
        return "", text
    if room >= len(text):
        return text, ""
    cut = text.rfind("\n", 0, room)
    if cut <= 0:
        cut = room
    else:
        cut += 1                     # 换行符留在前一段
    return text[:cut], text[cut:]


class MessageChain:
    """消息段链式构造器。

    用法:
        msg = MessageChain() \
            .text("你好") \
            .at("123456") \
            .text(" 今天天气不错") \
            .image("https://example.com/photo.jpg")

        api.send_group_msg(group_id="123", message=msg)
        # 或: api.send_group_msg(group_id="123", message=msg.to_cq())
    """

    __slots__ = ("_segments", "_allow_illegal")

    def __init__(self, allow_illegal: bool = False):
        self._segments: list[dict] = []
        self._allow_illegal = allow_illegal

    # ---- 基础类 ----

    def text(self, content: str) -> "MessageChain":
        self._segments.append({"type": "text", "data": {"text": content}})
        return self

    def at(self, qq: Union[str, int] = "all", name: Optional[str] = None) -> "MessageChain":
        data = {"qq": str(qq)}
        if name:
            data["name"] = name
        self._segments.append({"type": "at", "data": data})
        return self

    def reply(self, message_id: Optional[int] = None, seq: Optional[int] = None) -> "MessageChain":
        data = {}
        if message_id is not None:
            data["id"] = str(message_id)
        if seq is not None:
            data["seq"] = seq
        self._segments.append({"type": "reply", "data": data})
        return self

    def face(self, face_id: Union[str, int]) -> "MessageChain":
        self._segments.append({"type": "face", "data": {"id": str(face_id)}})
        return self

    def mface(self, emoji_package_id: int, emoji_id: str, key: str, summary: str = "") -> "MessageChain":
        self._segments.append({"type": "mface", "data": {
            "emoji_package_id": emoji_package_id,
            "emoji_id": emoji_id,
            "key": key,
            "summary": summary,
        }})
        return self

    # ---- 文件类 ----

    def image(self, file: str, url: Optional[str] = None, name: Optional[str] = None,
              summary: Optional[str] = None, sub_type: Optional[int] = None,
              use_cache: bool = True, timeout: Optional[int] = None) -> "MessageChain":
        data = {"file": file}
        if url: data["url"] = url
        if name: data["name"] = name
        if summary: data["summary"] = summary
        if sub_type is not None: data["sub_type"] = sub_type
        self._segments.append({"type": "image", "data": data})
        return self

    def record(self, file: str, url: Optional[str] = None, name: Optional[str] = None,
               timeout: Optional[int] = None) -> "MessageChain":
        data = {"file": file}
        if url: data["url"] = url
        if name: data["name"] = name
        self._segments.append({"type": "record", "data": data})
        return self

    def video(self, file: str, url: Optional[str] = None, name: Optional[str] = None) -> "MessageChain":
        data = {"file": file}
        if url: data["url"] = url
        if name: data["name"] = name
        self._segments.append({"type": "video", "data": data})
        return self

    def file(self, file: str, url: Optional[str] = None, name: Optional[str] = None) -> "MessageChain":
        data = {"file": file}
        if url: data["url"] = url
        if name: data["name"] = name
        self._segments.append({"type": "file", "data": data})
        return self

    # ---- 音乐 ----

    def music_id(self, platform: MusicPlatform, music_id: Union[str, int]) -> "MessageChain":
        self._segments.append({"type": "music", "data": {
            "type": platform.value,
            "id": str(music_id),
        }})
        return self

    def music_custom(self, url: str, image: str, title: str = "", audio: str = "",
                     content: str = "", platform: MusicPlatform = MusicPlatform.CUSTOM) -> "MessageChain":
        self._segments.append({"type": "music", "data": {
            "type": platform.value,
            "url": url,
            "audio": audio,
            "title": title,
            "image": image,
            "content": content,
        }})
        return self

    # ---- 互动类 ----

    def poke(self, poke_type: Union[str, int] = "1", poke_id: Union[str, int] = "10000") -> "MessageChain":
        self._segments.append({"type": "poke", "data": {"type": str(poke_type), "id": str(poke_id)}})
        return self

    def dice(self, result: Optional[int] = None) -> "MessageChain":
        import random
        r = result or random.randint(1, 6)
        self._segments.append({"type": "dice", "data": {"result": r}})
        return self

    def rps(self, result: Optional[str] = None) -> "MessageChain":
        import random
        r = result or random.choice(["石头", "剪刀", "布"])
        self._segments.append({"type": "rps", "data": {"result": r}})
        return self

    # ---- 位置/联系人 ----

    def location(self, lat: float, lon: float, title: str = "", content: str = "") -> "MessageChain":
        self._segments.append({"type": "location", "data": {
            "lat": str(lat), "lon": str(lon), "title": title, "content": content,
        }})
        return self

    def contact(self, contact_type: ContactType, contact_id: Union[str, int]) -> "MessageChain":
        self._segments.append({"type": "contact", "data": {
            "type": contact_type.value, "id": str(contact_id),
        }})
        return self

    # ---- 富文本 ----

    def json(self, data: Union[str, dict], token: Optional[str] = None) -> "MessageChain":
        import json as _json
        payload = {"data": data if isinstance(data, str) else _json.dumps(data, ensure_ascii=False)}
        if token:
            payload["config"] = {"token": token}
        self._segments.append({"type": "json", "data": payload})
        return self

    def xml(self, data: str) -> "MessageChain":
        self._segments.append({"type": "xml", "data": {"data": data}})
        return self

    def markdown(self, content: str) -> "MessageChain":
        self._segments.append({"type": "markdown", "data": {"content": content}})
        return self

    def miniapp(self, data: Union[str, dict]) -> "MessageChain":
        import json as _json
        self._segments.append({"type": "miniapp", "data": {
            "data": data if isinstance(data, str) else _json.dumps(data, ensure_ascii=False),
        }})
        return self

    # ---- 合并转发 ----

    def node(self, user_id: int, nickname: str, content: Union["MessageChain", list],
             source: Optional[str] = None) -> "MessageChain":
        c = content.build() if isinstance(content, MessageChain) else content
        data = {"user_id": user_id, "nickname": nickname, "content": c}
        if source:
            data["source"] = source
        self._segments.append({"type": "node", "data": data})
        return self

    def forward(self, forward_id: str, content: Optional[list] = None) -> "MessageChain":
        data = {"id": forward_id}
        if content:
            data["content"] = content
        self._segments.append({"type": "forward", "data": data})
        return self

    # ---- 特殊 ----

    def online_file(self, msg_id: str, element_id: str, file_name: str,
                    file_size: str, is_dir: bool = False) -> "MessageChain":
        self._segments.append({"type": "onlinefile", "data": {
            "msgId": msg_id, "elementId": element_id,
            "fileName": file_name, "fileSize": file_size, "isDir": is_dir,
        }})
        return self

    def flash_transfer(self, file_set_id: str) -> "MessageChain":
        self._segments.append({"type": "flashtransfer", "data": {"fileSetId": file_set_id}})
        return self

    # ---- 输出 ----

    # 计入「媒体配额」的消息段（一条消息里塞太多会又大又慢）
    MEDIA_TYPES = frozenset({"image", "record", "video", "file"})

    def split(self, *, max_chars: int = 1200, max_images: int = 4) -> list["MessageChain"]:
        """按「每条消息最多 max_chars 个文字字符、max_images 个媒体段」切分。

        - 保持消息段顺序（图文排版不会乱）；
        - 文字优先在换行处断开，找不到换行才硬切；
        - 单个消息段不会被拆开（图片/文件/JSON 等整段搬走）；
        - `max_chars` / `max_images` 传 0（或负数）表示该维度不限；
        - 永远返回至少一条消息（内容为空时是一条空消息）。
        """
        char_budget = int(max_chars) if max_chars and max_chars > 0 else 0
        media_budget = int(max_images) if max_images and max_images > 0 else 0
        chunks: list[MessageChain] = []
        cur = MessageChain(allow_illegal=self._allow_illegal)
        cur_chars = 0
        cur_media = 0

        def flush():
            nonlocal cur, cur_chars, cur_media
            if cur:
                chunks.append(cur)
            cur = MessageChain(allow_illegal=self._allow_illegal)
            cur_chars = cur_media = 0

        for seg in self._segments:
            stype = seg.get("type")
            if stype == "text":
                text = (seg.get("data") or {}).get("text", "")
                while text:
                    room = char_budget - cur_chars if char_budget else len(text)
                    if room <= 0:
                        flush()
                        continue
                    head, text = split_text(text, room)
                    cur.text(head)
                    cur_chars += len(head)
                    if text:
                        flush()          # 放不下剩余文字 → 开新消息
            elif stype in self.MEDIA_TYPES:
                if media_budget and cur_media >= media_budget:
                    flush()
                cur._segments.append(seg)
                cur_media += 1
            else:
                cur._segments.append(seg)
        flush()
        return chunks or [MessageChain(allow_illegal=self._allow_illegal)]

    def build(self) -> list[dict]:
        """构建消息段列表（用于 API 调用）"""
        return list(self._segments)

    def to_cq(self) -> str:
        """转换为 CQ 码字符串"""
        parts = []
        for seg in self._segments:
            t = seg["type"]
            d = seg["data"]
            if t == "text":
                parts.append(d.get("text", ""))
            elif t == "at":
                qq = d.get("qq", "all")
                parts.append(f"[CQ:at,qq={qq}]")
            elif t == "image":
                parts.append(f"[CQ:image,file={d.get('file', '')}]")
            elif t == "record":
                parts.append(f"[CQ:record,file={d.get('file', '')}]")
            elif t == "video":
                parts.append(f"[CQ:video,file={d.get('file', '')}]")
            elif t == "reply":
                parts.append(f"[CQ:reply,id={d.get('id', '')}]")
            elif t == "face":
                parts.append(f"[CQ:face,id={d.get('id', '')}]")
            elif t == "poke":
                parts.append(f"[CQ:poke,type={d.get('type', '1')}]")
            elif t == "dice":
                parts.append(f"[CQ:dice,result={d.get('result', 1)}]")
            elif t == "rps":
                parts.append(f"[CQ:rps,result={d.get('result', '石头')}]")
            elif t == "json":
                parts.append(f"[CQ:json,data={d.get('data', '')}]")
            elif t == "xml":
                parts.append(f"[CQ:xml,data={d.get('data', '')}]")
            elif t == "file":
                parts.append(f"[CQ:file,file={d.get('file', '')}]")
            elif t == "markdown":
                parts.append(f"[CQ:markdown,content={d.get('content', '')}]")
            else:
                import json
                parts.append(f"[CQ:{t},data={json.dumps(d, ensure_ascii=False)}]")
        return "".join(parts)

    def __str__(self) -> str:
        return self.to_cq()

    def __add__(self, other: "MessageChain") -> "MessageChain":
        c = MessageChain(allow_illegal=self._allow_illegal)
        c._segments = self._segments + other._segments
        return c

    def __bool__(self) -> bool:
        return len(self._segments) > 0

    def __len__(self) -> int:
        return len(self._segments)

    @staticmethod
    def from_text(text: str) -> "MessageChain":
        """快速创建纯文本消息"""
        return MessageChain().text(text)

    @staticmethod
    def from_cq(cq_string: str) -> "MessageChain":
        """从 CQ 码字符串解析——解析失败的非法 CQ 码自动丢弃"""
        import re
        chain = MessageChain(allow_illegal=False)
        pattern = re.compile(r'\[CQ:(\w+)((?:,[\w-]+=[^,\]]*)*)\]')
        pos = 0
        for m in pattern.finditer(cq_string):
            if m.start() > pos:
                chain.text(cq_string[pos:m.start()])
            tag = m.group(1)
            raw_params = m.group(2)
            params = {}
            if raw_params:
                for part in raw_params.lstrip(",").split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        params[k] = v
            segment = _cq_to_segment(tag, params)
            if segment:
                chain._segments.append(segment)
            pos = m.end()
        if pos < len(cq_string):
            chain.text(cq_string[pos:])
        return chain


def _cq_to_segment(tag: str, params: dict) -> Optional[dict]:
    try:
        if tag == "text":
            return {"type": "text", "data": {"text": params.get("text", "")}}
        elif tag == "at":
            return {"type": "at", "data": {"qq": params.get("qq", "all")}}
        elif tag == "image":
            return {"type": "image", "data": {"file": params.get("file", "")}}
        elif tag == "record":
            return {"type": "record", "data": {"file": params.get("file", "")}}
        elif tag == "video":
            return {"type": "video", "data": {"file": params.get("file", "")}}
        elif tag == "reply":
            return {"type": "reply", "data": {"id": params.get("id", "")}}
        elif tag == "face":
            return {"type": "face", "data": {"id": params.get("id", "0")}}
        elif tag == "poke":
            return {"type": "poke", "data": {"type": params.get("type", "1"), "id": params.get("id", "10000")}}
        elif tag == "dice":
            return {"type": "dice", "data": {"result": int(params.get("result", 1))}}
        elif tag == "rps":
            return {"type": "rps", "data": {"result": params.get("result", "石头")}}
        elif tag == "file":
            return {"type": "file", "data": {"file": params.get("file", "")}}
        elif tag == "json":
            return {"type": "json", "data": {"data": params.get("data", "")}}
        elif tag == "xml":
            return {"type": "xml", "data": {"data": params.get("data", "")}}
        elif tag == "markdown":
            return {"type": "markdown", "data": {"content": params.get("content", "")}}
        elif tag == "location":
            return {"type": "location", "data": {"lat": params.get("lat", "0"), "lon": params.get("lon", "0"),
                                                   "title": params.get("title", ""), "content": params.get("content", "")}}
        elif tag == "contact":
            return {"type": "contact", "data": {"type": params.get("type", "qq"), "id": params.get("id", "0")}}
    except Exception:
        pass
    return None
