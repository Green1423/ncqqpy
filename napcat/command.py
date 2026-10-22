"""
命令系统 - 衍生自消息事件，自动解析命令、拦截非法输入
输出标准化 CommandPacket，支持子命令、别名、权限检查
内置 Django 风格的参数验证系统
"""
from __future__ import annotations

import re
import shlex
import inspect
from dataclasses import dataclass, field
from typing import Callable, Optional, Any, Type, Union, get_type_hints


# ==================== 参数验证系统 ====================

# 错误类型与反馈策略的唯一业主是 `napcat/errors.py`（分类 / 受众 / 编号 / 节流）。
# 这里只做转出：`napcat.CommandExecutionError` 与
# `napcat.command.CommandExecutionError` 两条历史导入路径都还能用
# （插件里已经这么写了，见 ai_chat）。
from .errors import (                                    # noqa: E402
    CommandExecutionError, ErrorReporter, NapCatError, PluginRuntimeError,
    short_reason,
)
#: `/userperm` 可设置的级别（与 PermissionManager 共用一份，别写两套）
from .permission import ASSIGNABLE_USER_LEVELS          # noqa: E402


class ValidationError(Exception):
    """单个字段验证错误"""


class SchemaError(Exception):
    """Schema 级别的验证错误（包含所有字段错误）"""

    def __init__(self, errors: dict[str, list[str]], message: str = ""):
        self.errors = errors
        self.message = message or self._format_errors()
        super().__init__(self.message)

    def _format_errors(self) -> str:
        lines = []
        for field, msgs in self.errors.items():
            for m in msgs:
                lines.append(f"  {field}: {m}")
        return "\n".join(lines) if lines else "参数验证失败"


class BaseField:
    """参数字段基类 — 类似 Django forms.Field"""

    def __init__(self, *, required: bool = False, default: Any = None,
                 description: str = "", label: str = ""):
        self.required = required
        self.default = default
        self.description = description
        self.label = label

    def validate(self, value: str) -> Any:
        """验证并转换值，返回清洗后的 Python 对象"""
        if value is None or value == "":
            if self.required:
                raise ValidationError("此参数为必填")
            return self.default
        return self._clean(value)

    def _clean(self, value: str) -> Any:
        raise NotImplementedError


class StrField(BaseField):
    """字符串参数"""

    def __init__(self, *, min_length: int = 0, max_length: int = 4096,
                 regex: str = None, choices: list[str] = None,
                 strip: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.min_length = min_length
        self.max_length = max_length
        self.regex = regex and re.compile(regex)
        self.choices = choices
        self.strip = strip

    def _clean(self, value: str) -> str:
        if self.strip:
            value = value.strip()
        if len(value) < self.min_length:
            raise ValidationError(f"长度不能少于 {self.min_length} 个字符")
        if len(value) > self.max_length:
            raise ValidationError(f"长度不能超过 {self.max_length} 个字符")
        if self.regex and not self.regex.match(value):
            raise ValidationError(f"格式不正确（需要匹配: {self.regex.pattern}）")
        if self.choices and value not in self.choices:
            raise ValidationError(f"无效值，可选: {', '.join(self.choices)}")
        return value


class IntField(BaseField):
    """整数参数"""

    def __init__(self, *, min: int = None, max: int = None,
                 choices: list[int] = None, **kwargs):
        super().__init__(**kwargs)
        self.min = min
        self.max = max
        self.choices = choices

    def _clean(self, value: str) -> int:
        try:
            v = int(value)
        except (ValueError, TypeError):
            raise ValidationError("必须为整数")
        if self.min is not None and v < self.min:
            raise ValidationError(f"不能小于 {self.min}")
        if self.max is not None and v > self.max:
            raise ValidationError(f"不能大于 {self.max}")
        if self.choices and v not in self.choices:
            raise ValidationError(f"无效值，可选: {', '.join(map(str, self.choices))}")
        return v


class FloatField(BaseField):
    """浮点数参数"""

    def __init__(self, *, min: float = None, max: float = None, **kwargs):
        super().__init__(**kwargs)
        self.min = min
        self.max = max

    def _clean(self, value: str) -> float:
        try:
            v = float(value)
        except (ValueError, TypeError):
            raise ValidationError("必须为数值")
        if self.min is not None and v < self.min:
            raise ValidationError(f"不能小于 {self.min}")
        if self.max is not None and v > self.max:
            raise ValidationError(f"不能大于 {self.max}")
        return v


class BoolField(BaseField):
    """布尔参数 — 支持 true/false/1/0/yes/no，以及单字母简写（`-y`）。

    `short` 的取值：
    - 不传（`None`）：自动用**字段名首字母**（仅 ASCII 字母；同字母有多个布尔
      字段时不猜，校验时提示改用 `--字段名`）；
    - 传一个字母（`short="f"`）：显式指定；
    - 传空串（`short=""`）：关闭简写。
    """

    TRUTHY = {"true", "1", "yes", "on", "y", "是", "开"}
    FALSEY = {"false", "0", "no", "off", "n", "否", "关"}

    def __init__(self, *, short: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        if short is not None:
            cleaned = str(short).strip().lstrip("-").lower()
            if cleaned and (len(cleaned) != 1 or not cleaned.isascii()
                            or not cleaned.isalpha()):
                raise ValueError(
                    f"BoolField.short 必须是单个 ASCII 字母或空串：{short!r}")
            short = cleaned
        self.short = short

    def _clean(self, value: str) -> bool:
        v = value.strip().lower()
        if v in self.TRUTHY:
            return True
        if v in self.FALSEY:
            return False
        raise ValidationError(f"必须为布尔值（true/false, 1/0, yes/no）")


class QQField(IntField):
    """QQ 号参数 — 自动校验范围"""

    def __init__(self, *, allow_all: bool = False, **kwargs):
        kwargs.setdefault("min", 10000 if not allow_all else 0)
        kwargs.setdefault("max", 99999999999)
        kwargs.setdefault("description", "QQ号")
        super().__init__(**kwargs)

    def _clean(self, value: str) -> int:
        # 支持 @mention 格式: [@123456] 或纯数字
        m = re.match(r'^\[@(\d+)\]$', value.strip())
        if m:
            value = m.group(1)
        v = super()._clean(value)
        if v < 10000 and v != 0:
            raise ValidationError("无效的 QQ 号")
        return v


class DurationField(IntField):
    """时长参数 — 支持 "1h30m" "600" "1d" 等人类可读格式"""

    UNITS = {"s": 1, "sec": 1, "秒": 1,
             "m": 60, "min": 60, "分钟": 60,
             "h": 3600, "hour": 3600, "小时": 3600,
             "d": 86400, "day": 86400, "天": 86400}

    def __init__(self, *, min: int = 0, max: int = 2592000, **kwargs):
        super().__init__(min=min, max=max, **kwargs)

    def _clean(self, value: str) -> int:
        value = value.strip().lower()
        # 纯数字
        if value.isdigit():
            return super()._clean(value)
        # 带单位: 1h30m
        total = 0
        pattern = re.compile(r'(\d+)\s*(s|sec|秒|m|min|分钟|h|hour|小时|d|day|天)?')
        for m in pattern.finditer(value):
            num = int(m.group(1))
            unit = m.group(2) or "s"
            total += num * self.UNITS.get(unit, 1)
        if total == 0:
            raise ValidationError("无法解析时长，支持的格式: 600 / 10m / 1h30m / 1d")
        if self.min is not None and total < self.min:
            raise ValidationError(f"不能小于 {self.min} 秒")
        if self.max is not None and total > self.max:
            raise ValidationError(f"不能超过 {self.max} 秒")
        return total


class ChoiceField(BaseField):
    """选项参数 — 枚举类型"""

    def __init__(self, choices: list[str], *, case_insensitive: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.choices = choices
        self.case_insensitive = case_insensitive
        if case_insensitive:
            self._lookup = {c.lower(): c for c in choices}
        else:
            self._lookup = {c: c for c in choices}

    def _clean(self, value: str) -> str:
        v = value.strip().lower() if self.case_insensitive else value.strip()
        if v not in self._lookup:
            raise ValidationError(f"无效选项，可选: {', '.join(self.choices)}")
        return self._lookup[v]


class VariadicField(BaseField):
    """可变参数 — 接收剩余所有位置参数，不做类型校验"""

    def _clean(self, value: str) -> str:
        return value


# ==================== 验证 Schema ====================

class CommandSchemaMeta(type):
    """元类 — 收集类属性中定义的 Field 实例"""

    def __new__(mcs, name, bases, namespace):
        fields = {}
        for base in bases:
            if hasattr(base, "_fields"):
                fields.update(base._fields)
        for key, val in namespace.items():
            if isinstance(val, BaseField):
                fields[key] = val
        namespace["_fields"] = fields
        cls = super().__new__(mcs, name, bases, namespace)
        return cls


class CommandSchema(metaclass=CommandSchemaMeta):
    """命令参数 Schema — 类似 Django forms.Form。

    用法:
        class BanSchema(CommandSchema):
            user_id = QQField(required=True, description="目标QQ")
            duration = DurationField(default=600, description="禁言时长")
            reason = StrField(default="", description="原因")

        # 在命令中使用
        @cmds.register("ban", schema=BanSchema)
        async def cmd_ban(params: BanParams, event):
            await api.set_group_ban(str(event.group_id), str(params.user_id), params.duration)
    """

    def __init__(self):
        self._cleaned: dict[str, Any] = {}
        # 注入 __init__ 的签名，让 IDE 能补全
        # 通过 __init_subclass__ 或元类生成 dataclass-like __init__

    @classmethod
    def get_fields(cls) -> dict[str, BaseField]:
        return dict(getattr(cls, "_fields", {}))

    @classmethod
    def short_flag_map(cls) -> tuple[dict[str, str], dict[str, list[str]]]:
        """布尔字段的单字母简写：`({字母: 字段名}, {字母: [歧义字段…]})`。

        显式 `short=` 优先；否则用字段名首字母（仅 ASCII 字母）且要求唯一。
        歧义**不猜** —— 由 `validate()` 报错让用户写 `--字段名`。
        """
        explicit: dict[str, list[str]] = {}
        auto: dict[str, list[str]] = {}
        for name, field in cls.get_fields().items():
            if not isinstance(field, BoolField):
                continue
            if field.short == "":
                continue
            if field.short:
                explicit.setdefault(field.short, []).append(name)
                continue
            first = name[:1].lower()
            if first and first.isascii() and first.isalpha():
                auto.setdefault(first, []).append(name)

        mapping: dict[str, str] = {}
        ambiguous: dict[str, list[str]] = {}
        for letter, names in explicit.items():
            if len(names) > 1:
                ambiguous[letter] = names
            else:
                mapping[letter] = names[0]
        for letter, names in auto.items():
            if letter in mapping or letter in ambiguous:
                continue          # 显式简写优先
            if len(names) > 1:
                ambiguous[letter] = names
            else:
                mapping[letter] = names[0]
        return mapping, ambiguous

    def validate(self, args: list[str], kwargs: dict[str, str]) -> "ValidatedParams":
        """验证 args/kwargs 对 Schema 的匹配，返回 ValidatedParams"""
        fields = self.get_fields()
        errors: dict[str, list[str]] = {}
        cleaned: dict[str, Any] = {}

        # 单字母简写：解析器把 `-y` 收进 kwargs["y"]="true"，这里把它映射到
        # 对应的布尔字段（字段名精确匹配优先；歧义时明确报错，不猜）。
        kwargs = dict(kwargs or {})
        short_map, short_ambiguous = self.short_flag_map()
        for key in [k for k in kwargs if k not in fields]:
            letter = key.lower()
            if letter in short_ambiguous:
                errors[f"-{letter}"] = [
                    f"短选项 -{letter} 有歧义：{' / '.join(short_ambiguous[letter])}"
                    "（请用 --字段名=值）"]
                continue
            target = short_map.get(letter)
            if target is None:
                continue
            value = kwargs.pop(key)
            if target not in kwargs:
                kwargs[target] = value

        # 按位置映射 args → field（跳过 variadic 之前的字段）
        field_names = list(fields.keys())
        # 分离 variadic 字段
        variadic_name = None
        pos_fields = []
        for name in field_names:
            f = fields[name]
            if isinstance(f, VariadicField):
                variadic_name = name
                break
            pos_fields.append(name)

        # 映射位置参数
        # ⚠️ 显式 `--名称=值` 优先于位置位：它能让调用者**只**改后面某个可选参数
        # （例如 `/group_config greeting 内容 true 60` 想跳过 recall 直接给延迟）。
        # 旧实现里带位置位的字段一律先被位置参数填满，`--键=值` 就被静默丢弃，
        # 于是帮助里宣传的键值写法对这些字段完全不生效。
        for i, name in enumerate(pos_fields):
            field = fields[name]
            raw = kwargs[name] if name in kwargs else (args[i] if i < len(args) else None)
            try:
                cleaned[name] = field.validate(raw)
            except ValidationError as e:
                errors[name] = errors.get(name, []) + [str(e)]

        # 处理 variadic
        if variadic_name:
            extra = args[len(pos_fields):]
            cleaned[variadic_name] = extra  # 可变参数保留为字符串列表

        # 其余字段（位置位之外的）：取关键字参数，否则用默认值
        handled = set(pos_fields)     # 位置位循环已经处理过（含校验失败的）字段
        for key, field in fields.items():
            # ⚠️ 位置位字段必须在这里跳过：`pos_fields` 循环对「显式 --键=值」也生效，
            # 所以校验失败的字段不在 cleaned 里 —— 只按 cleaned 判断会让同一个字段
            # 的同一个错误被记两次，用户看到的报错就是同一行重复两遍。
            if key in handled or key in cleaned:
                continue
            if key in kwargs:
                try:
                    cleaned[key] = field.validate(kwargs[key])
                except ValidationError as e:
                    errors[key] = errors.get(key, []) + [str(e)]
            elif field.required and key != variadic_name:
                if field.default is None:
                    errors[key] = errors.get(key, []) + ["此参数为必填"]
                else:
                    cleaned[key] = field.default
            else:
                cleaned[key] = field.default

        if errors:
            raise SchemaError(errors)

        return ValidatedParams(cleaned, schema=self)


class ValidatedParams:
    """验证通过的参数容器 — 支持 .attr 访问和 [key] 访问"""

    def __init__(self, data: dict[str, Any], schema: CommandSchema = None):
        object.__setattr__(self, "_data", data)
        object.__setattr__(self, "_schema", schema)
        object.__setattr__(self, "_schema_class", type(schema) if schema else None)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        data = object.__getattribute__(self, "_data")
        if name in data:
            return data[name]
        # 尝试从 schema 获取默认值
        schema_cls = object.__getattribute__(self, "_schema_class")
        if schema_cls:
            fields = schema_cls.get_fields()
            if name in fields:
                return fields[name].default
        raise AttributeError(f"'{type(self).__name__}' 没有参数 '{name}'")

    def __getitem__(self, key: str) -> Any:
        return object.__getattribute__(self, "_data")[key]

    def __contains__(self, key: str) -> bool:
        return key in object.__getattribute__(self, "_data")

    def __repr__(self) -> str:
        return f"ValidatedParams({self._data})"

    def dict(self) -> dict[str, Any]:
        return dict(object.__getattribute__(self, "_data"))

    def get(self, key: str, default: Any = None) -> Any:
        return object.__getattribute__(self, "_data").get(key, default)

    @property
    def schema(self) -> Optional[CommandSchema]:
        return object.__getattribute__(self, "_schema")


# 导出 schema 快捷别名
Params = ValidatedParams


# ==================== 命令解析器 ====================

@dataclass
class CommandPacket:
    """命令解析结果数据包"""
    raw: str
    prefix: str
    command: str
    subcommand: Optional[str] = None
    args: list[str] = field(default_factory=list)
    kwargs: dict[str, str] = field(default_factory=dict)
    event: Optional["MessageEvent"] = None
    illegal: bool = False
    illegal_reason: str = ""
    #: 非法输入的**类型**（机器判据，别拿 `illegal_reason` 文案去判）：
    #: `"empty"` 前缀后面没内容 / `"name"` 命令名不是合法标识符 /
    #: `"too_long"` 命令名过长 / `"too_many_args"` 参数超过上限 / `"parse"` 解析失败。
    #: 框架据此决定「静默丢弃」还是「回一句」：`#include<bits/stdc++.h>` 这类
    #: 以命令前缀开头的正文必须安静（kind="name"），而参数写到 21 个的用户
    #: 是真的在打命令，静默就是「有问不答」。
    illegal_kind: str = ""

    def arg(self, index: int, default: str = "") -> str:
        return self.args[index] if index < len(self.args) else default

    def opt(self, key: str, default: str = "") -> str:
        return self.kwargs.get(key, default)

    def __getitem__(self, index: int) -> str:
        return self.arg(index)


_CMD_LOGGER = None


def _cmd_logger():
    """命令系统的默认 logger（延迟创建，避免 import 时就配置 logging）。"""
    global _CMD_LOGGER
    if _CMD_LOGGER is None:
        import logging
        lg = logging.getLogger("napcat.command")
        if not lg.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter(
                "[%(asctime)s] [%(levelname)s] %(message)s"))
            lg.addHandler(h)
            lg.setLevel(logging.INFO)
        _CMD_LOGGER = lg
    return _CMD_LOGGER


def normalize_text(text: str) -> str:
    """模块级便捷入口：归一化命令文本（首尾空白 + 前导 @机器人）。

    插件若要在命令系统之外自行判断文本（例如
    `if normalize_text(event.plain_text).startswith("/hello")`），必须走这里，
    否则会因用户输入带着首尾空白而漏判。
    """
    return CommandParser().normalize(text)


#: 命令名 / 子命令名 / 别名的唯一规则：**Unicode 字母开头**，后续是字母、数字、
#: 下划线或连字符。用 `[^\W\d_]` 而不是 `[A-Za-z]` —— 中文、日文、西里尔字母都是
#: 字母，`/签到`、别名 `/签` 因此可用；而 `//`、`/签到！`、`/2签到` 这类「前缀后
#: 不是名字」的输入仍被挡住（`\w` 不匹配标点）。
_COMMAND_NAME_RE = re.compile(r"[^\W\d_][\w-]*")


def valid_command_name(name: str, max_len: int = 64) -> bool:
    """名字是否合法（命令名、子命令名、别名共用这一条规则）。"""
    name = (name or "").strip()
    return bool(name) and len(name) <= max_len and bool(
        _COMMAND_NAME_RE.fullmatch(name))


class CommandParser:
    """命令解析器 — 分词、前缀匹配、参数分离"""

    def __init__(self, prefixes: list[str] = None, max_args: int = 20, max_cmd_len: int = 64):
        self.prefixes = prefixes or ["/", "!", "。", "#"]
        self.max_args = max_args
        self.max_cmd_len = max_cmd_len

    _AT_PATTERN = re.compile(r'^\[@\d+\]\s*|^\[@all\]\s*')
    # 合法命令名：字母开头，跟字母/数字/下划线/连字符。用来把「前缀后跟分隔符」
    # 的输入（`//`、`/ /`）判成非法而不是一个叫 `/` 的命令。
    _NAME_PATTERN = _COMMAND_NAME_RE
    _CJK_QUOTES = {
        "“": "\"", "”": "\"",
        "‘": "'", "’": "'",
        "「": "\"", "」": "\"",
        "『": "'", "』": "'",
        "＂": "\"", "＇": "'",
    }

    def _normalize_quotes(self, text: str) -> str:
        result = []
        for ch in text:
            result.append(self._CJK_QUOTES.get(ch, ch))
        return "".join(result)

    _ESCAPE_MAP = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'"}

    # 受保护的 \'：POSIX shlex 在单引号内不处理反斜杠，'don\'t' 会被判成引号没配对；
    # 先用哨兵顶替，分词后再还原，于是单引号里的单引号也能转义。
    _SQ_MARK = "\x02"

    @staticmethod
    def _unescape(s: str) -> str:
        """处理 \n \t \r \\ \" \' 转义序列。"""
        result = []
        i = 0
        while i < len(s):
            if s[i] == "\\" and i + 1 < len(s):
                nxt = s[i + 1]
                if nxt in CommandParser._ESCAPE_MAP:
                    result.append(CommandParser._ESCAPE_MAP[nxt])
                    i += 2
                    continue
            result.append(s[i])
            i += 1
        return "".join(result)

    @staticmethod
    def _protect_single_quote(text: str) -> str:
        """把 `\\'` 换成哨兵，避免被 shlex 的单引号规则当成引号结束。

        `\\'`（转义的反斜杠 + 引号）保持原样，交给 shlex 与 `_unescape` 按原语义处理。
        """
        if "\\'" not in text:
            return text
        out: list[str] = []
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "\\" and i + 1 < n:
                nxt = text[i + 1]
                if nxt == "'":
                    out.append(CommandParser._SQ_MARK)
                else:
                    out.append(ch)
                    out.append(nxt)
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    def normalize(self, text: str) -> str:
        """命令文本归一化 —— **全框架唯一**的首尾空白 / 前导 @ 处理位置。

        处理掉：首尾空白（空格、制表符、换行、CR、全角空格、NBSP 等 `.strip()`
        认得的全部字符）与前导 `@机器人` mention。命令体内部的连续空白由
        `shlex` 在分词时折叠，不需要在这里动。

        为什么要单独拎出来：`MessageEvent.plain_text` 是**原样**拼接的，
        用户发 `"  /buptnews get 451\\n"` 时它带着首尾空白。插件若直接写
        `event.plain_text.startswith("/xxx")` 就会漏判（框架文档里曾这么示范，
        那是个坑）。插件需要自行判断时请用 `napcat.command.normalize_text()`。
        """
        return self._AT_PATTERN.sub("", (text or "").strip()).strip()

    def strip_prefix(self, text: str) -> Optional[tuple[str, str]]:
        """拆出 `(prefix, rest)`；不是命令前缀开头（或后面没内容）则返回 None。

        ⚠️ 只认「前缀 + 至少一个非空白字符」。旧写法直接 `text[len(p):]`，
        会把**纯前缀**认成命令：`//` 被剥成 `/`、`/` 被剥成 `""`，于是它们
        变成「名为 `/` 的命令」被回 `未知命令: //` —— 而 `//` 恰恰是用户常用的
        「结束符」写法。空命令应当走 `illegal` 分支（静默丢弃）。
        """
        candidate = (text or "").lstrip()      # 与 parse() 的口径一致：先归一化空白
        for p in sorted(self.prefixes, key=len, reverse=True):
            if not p or not candidate.startswith(p):
                continue
            rest = candidate[len(p):]
            if not rest.strip():
                return None
            return p, rest
        return None

    def skip_prefix(self, text: str) -> Optional[str]:
        """命令名解析用的片段（已剥前缀与空白）；不是命令则 None。"""
        got = self.strip_prefix(text)
        return got[1].strip() if got else None

    def parse(self, text: str) -> Optional[CommandPacket]:
        text = self.normalize(text)
        if not text:
            return None
        # 标准化 CJK 引号 → ASCII 引号（shlex 仅识别 ASCII 引号）
        text = self._normalize_quotes(text)
        # 哨兵是内部占位符，输入里不该出现（出现就丢掉，避免被当成转义引号）
        text = text.replace(self._SQ_MARK, "")
        # 保护 \' ，让它在单引号里也能转义
        text = self._protect_single_quote(text)
        got = self.strip_prefix(text)
        if got is None:
            # 纯前缀（`/`、`//`、`。`）不是命令 —— 返回 None 让调用方当普通消息处理
            return None
        prefix, text = got
        text = text.strip()
        try:
            parts = shlex.split(text)
        except ValueError:
            # 引号没配对（例如没加引号的英文撇号）→ 宽松回退：按空白切
            parts = text.split()
        # 先还原受保护的 \' ，再统一处理 \n \t \\ \" \' 等转义序列
        parts = [self._unescape(p.replace(self._SQ_MARK, "'")) for p in parts]
        if not parts:
            return CommandPacket(raw=text, prefix=prefix, command="", illegal=True,
                                 illegal_kind="empty", illegal_reason="解析失败")
        command = parts[0].lower()
        if len(command) > self.max_cmd_len:
            return CommandPacket(raw=text, prefix=prefix, command=command, illegal=True,
                                 illegal_kind="too_long",
                                 illegal_reason=f"命令名过长（>{self.max_cmd_len}）")
        if not self._NAME_PATTERN.fullmatch(command):
            # 前缀后跟着的是分隔符而不是命令名（典型：`// `）→ 不是命令。
            # 让它落到 `illegal` 分支静默丢弃，而不是回一句
            # `未知命令: //`（用户只是想发个结束符，不该被指责打错命令）。
            return CommandPacket(raw=text, prefix=prefix, command=command,
                                 illegal=True, illegal_kind="name",
                                 illegal_reason="命令名不是合法标识符")
        subcommand = None
        idx = 1
        # 仅当第二个 token 是合法名字时才视为子命令（与命令名同一规则，
        # 所以中文子命令 `/点歌 列表` 也能路由）
        if len(parts) > 1 and valid_command_name(parts[1], self.max_cmd_len):
            subcommand = parts[1].lower()
            idx = 2
        args = []
        kwargs = {}
        for part in parts[idx:]:
            if part.startswith("--") and "=" in part:
                k, v = part[2:].split("=", 1)
                kwargs[k] = v
            elif part.startswith("-") and len(part) == 2:
                kwargs[part[1:]] = "true"
            else:
                args.append(part)
        if len(args) > self.max_args:
            return CommandPacket(raw=text, prefix=prefix, command=command, subcommand=subcommand,
                                 args=args, kwargs=kwargs, illegal=True,
                                 illegal_kind="too_many_args",
                                 illegal_reason=f"参数过多（>{self.max_args}）")
        return CommandPacket(raw=text, prefix=prefix, command=command,
                             subcommand=subcommand, args=args, kwargs=kwargs)


# ==================== 命令处理器 ====================

# 群聊里命令必须先 @机器人（框架入口 command_dispatcher 的规则）：
# 帮助输出要显式说明，否则用户会以为「复制命令就能用」。
GROUP_AT_HINT = "⚠️ 群里需先 @机器人再发命令（直接复制命令不触发）"

# ---- 占位符记号（全框架唯一约定）----
# `<名称>` = 必填，`[名称]` = 可选。所有帮助文本（命令列表 / 单命令详情 / 参数表 /
# 手写的用法行）都必须只用这两对符号，并由这里生成，避免各处符号漂移。
# 用户不知道这是占位符时会照抄 `<编号>` 去发命令，所以 `PLACEHOLDER_LEGEND`
# 要出现在真正需要传参的地方（/help 首屏、单命令详情、以及携带参数的命令）。
REQUIRED_MARK = ("<", ">")
OPTIONAL_MARK = ("[", "]")

PLACEHOLDER_LEGEND = "ℹ️ <名称> 必填，[名称] 可选；尖括号/方括号不用输入"


def required_placeholder(name: str) -> str:
    """必填占位符：`<名称>` —— 唯一允许的必填写法。"""
    return f"{REQUIRED_MARK[0]}{name}{REQUIRED_MARK[1]}"


def optional_placeholder(name: str) -> str:
    """可选占位符：`[名称]` —— 唯一允许的可选写法。"""
    return f"{OPTIONAL_MARK[0]}{name}{OPTIONAL_MARK[1]}"


def format_field_usage(name: str, field: "BaseField") -> str:
    """Schema 字段 → 用法片段。

    - 必填（无默认值）      → `<名称>`
    - 选填（带默认值）      → `[名称=默认值]`
    - 必填但可留空的语义    → `<名称>`
    - 可变参数（吃剩余参数）→ `[名称 ...]`
    """
    if isinstance(field, VariadicField):
        return optional_placeholder(f"{name} ...")
    if field.required and field.default is None:
        return required_placeholder(name)
    if field.default not in (None, ""):
        return optional_placeholder(f"{name}={field.default}")
    return optional_placeholder(name)


def build_usage(name: str, schema_cls: "Type[CommandSchema]") -> str:
    """由 Schema 生成 `usage`：必填用 `<>`、可选用 `[]`。"""
    parts = [name]
    for fname, field in schema_cls.get_fields().items():
        parts.append(format_field_usage(fname, field))
    return " ".join(parts)


def describe_fields(fields: dict, short_map: dict[str, str] = None) -> list[str]:
    """参数表正文行 —— 字段名同样按 `<>` / `[]` 标注，与用法行口径一致。

    可选且**无默认值**的字段额外标注「保持原值」：这类字段省略时不会被覆盖，
    与「带默认值、省略即回落默认值」的字段语义不同，帮助里必须写清楚。
    """
    lines: list[str] = []
    for fname, field in fields.items():
        req = "必填" if field.required else "可选"
        desc = f" — {field.description}" if field.description else ""
        short = (short_map or {}).get(fname)
        if short:
            desc += (f"（简写：-{short}）" if desc else f" — 简写：-{short}")
        if field.default not in (None, ""):
            default = f" [默认: {field.default}]"
        elif not field.required and not isinstance(field, VariadicField):
            default = " [不带则保持原值]"
        else:
            default = ""
        marker = (required_placeholder(fname) if field.required
                  else optional_placeholder(fname))
        lines.append(f"\n    {marker} {type(field).__name__} {req}{default}{desc}")
    return lines


# 帮助文本里 scope/permission 的措辞 —— 与 CommandManager.handle() 的校验口径一一对应，
# 避免帮助显示与实际拦截不一致。
SCOPE_LABELS = {"private": "仅私聊", "group": "仅群聊"}

# 分组/标记用图标，扫一眼就知道是哪一档权限
PERMISSION_ICONS = {"superuser": "👑", "owner": "⭐", "admin": "🛡️",
                    "member": "👤", "": "🔓"}


def _parent_help(name: str, manager: "CommandManager"):
    """构造「占位父命令」的处理函数。

    只在「子命令先注册、父命令本体还没注册」时临时顶替。裸调父命令时列出子命令，
    而不是返回 None —— 后者在用户侧就是「发了命令什么都没发生」。
    """
    def _show(**kwargs):           # 吞掉框架注入的 packet / event / api
        handler = manager._commands.get(name)
        subs = sorted(getattr(handler, "subcommands", {}) or {})
        if not subs:
            return f"用法：/{name}"
        return (f"用法：/{name} <子命令>\n"
                + "\n".join(f"  /{name} {sub}" for sub in subs))
    return _show


def permission_group_label(permission: str) -> str:
    """权限分组标题，如 `[🛡️ admin]` / `[🔓 所有人可用]` / `[🔑 vip]`。"""
    if not permission:
        return "[🔓 所有人可用]"
    return f"[{PERMISSION_ICONS.get(permission, '🔑')} {permission}]"


def describe_requirements(permission: str, scope: str) -> str:
    """完整的需求描述 —— **权限 + 作用域**都要写出来，如 `权限：superuser · 作用域：仅私聊`。

    三种帮助入口都靠它（单命令详情、命令组的子命令行、子命令详情）：只写权限不写作用域时，
    「仅私聊的命令在群里能不能用」这种问题在帮助里根本没有答案。
    完全公开且不限场景的也要显式写 `权限：公开 · 作用域：所有场景`，不留空白。
    """
    perm = f"权限：{permission}" if permission else "权限：公开"
    return f"{perm} · 作用域：{SCOPE_LABELS.get(scope) or '所有场景'}"


def requirement_tag(permission: str, scope: str) -> str:
    """命令组页里挂在子命令名后的紧凑标注：与详情行**同一句话**，只是加方括号。

    只用 `describe_requirements()` 这一处措辞，帮助页与执行口径不会各说各话。
    """
    return f" [{describe_requirements(permission, scope)}]"


# ==================== 系统命令的 Schema ====================
#
# 系统命令组（/perm、/userperm、/alias、/stop、/pause）里凡是**带参数**的子命令，
# 一律声明 Schema，而不是手写 usage 字符串：
#   · 帮助里的「位置传参 / 键值传参 / 参数表」由 Schema 生成（不会和校验漂移）；
#   · 参数缺失/类型不对时，框架统一回「⚠️ 用法不正确 + /help <组> <子命令>」，
#     不用每个 handler 自己写 usage 兜底。

class _PermTargetSchema(CommandSchema):
    target = StrField(required=True, description="命令名（支持 `父.子` 与别名）")


class _PermSetSchema(CommandSchema):
    target = StrField(required=True, description="命令名（支持 `父.子` 与别名）")
    permission = StrField(
        required=True,
        description="权限节点：superuser / owner / admin / member / public / 自定义节点")


class _PermResetSchema(CommandSchema):
    target = StrField(required=True, description="命令名，或 all（清空全部覆盖）")


class _UserpermScopeSchema(CommandSchema):
    group = IntField(min=1, default=0, description="群号；0/不给 = 全局")


class _UserpermGetSchema(CommandSchema):
    user = QQField(min=1, required=True, description="目标 QQ")
    group = IntField(min=1, default=0, description="群号；0/不给 = 全局")


class _UserpermSetSchema(CommandSchema):
    user = QQField(min=1, required=True, description="目标 QQ")
    level = ChoiceField(list(ASSIGNABLE_USER_LEVELS), required=True,
                        description="权限级别：owner / admin / member / everyone")
    group = IntField(min=1, default=0, description="群号；0/不给 = 全局")


class _UserpermClearSchema(CommandSchema):
    user = QQField(min=1, required=True, description="目标 QQ")
    group = IntField(min=1, default=0, description="群号；0/不给 = 全局")


class _AliasListSchema(CommandSchema):
    command = StrField(description="只看这条命令的别名")


class _AliasAddSchema(CommandSchema):
    alias = StrField(required=True,
                     description="新别名（字母开头，字母/数字/_/-）")
    command = StrField(required=True, description="目标命令或命令组")


class _AliasDelSchema(CommandSchema):
    alias = StrField(required=True, description="要删除的别名")


class _AliasSetSchema(CommandSchema):
    command = StrField(required=True, description="目标命令或命令组")
    aliases = VariadicField(description="别名列表（空格或逗号分隔）；不给 = 清空")


class _AliasMoveSchema(CommandSchema):
    alias = StrField(required=True, description="要改指向的别名")
    command = StrField(required=True, description="新的目标命令或命令组")


class _EventTargetSchema(CommandSchema):
    target = StrField(required=True, description="事件监听器 hid")


class _CommandTargetSchema(CommandSchema):
    target = StrField(required=True, description="命令名")


class _TimerTargetSchema(CommandSchema):
    target = StrField(required=True, description="定时任务名")


class CommandHandler:
    """单个命令处理器"""

    def __init__(self, name: str, func: Callable, *, subcommands: dict = None,
                 aliases: list[str] = None, description: str = "", usage: str = "",
                 permission: str = "", schema: Type[CommandSchema] = None,
                 scope: str = "all",
                 permission_manager: "PermissionManager" = None,
                 plugin: str = ""):
        self.name = name
        #: 注册它的插件名（`PluginBase.register_command` 自动打标，空 = 框架内置）。
        #: 用途是精确归属：卸载/加载失败时按它回收命令，出错反馈时按它定位插件 ——
        #: 都比「拿命令名去猜插件名前缀」可靠（`essential` 注册的是 `/ban`、`/kick`）。
        self.plugin = plugin
        self.func = func
        self.subcommands = subcommands or {}
        self.aliases = aliases or []
        self.description = description
        # ⚠️ 不能写成 `usage or self._build_usage(schema) if schema else ""`：
        # Python 把它解析成 `(usage or ...) if schema else ""`，于是**没有 schema 的命令
        # 显式传入的 usage= 会被静默丢掉**（子命令正是先以 dummy 身份注册、schema=None，
        # 所以 /help 里子命令从来不显示参数）。显式 usage 优先，其次由 schema 推导。
        if usage:
            self.usage = usage
        elif schema:
            self.usage = self._build_usage(schema)
        else:
            self.usage = ""
        self.parent_name = ""  # 子命令的父命令名
        # permission 是**生效值**（可被 config.json 的 command_permissions 覆盖）；
        # declared_permission 是代码里的声明值，永不被改写 —— /perm reset 靠它恢复。
        self.declared_permission = permission
        self.permission = permission
        self.scope = scope
        self.disabled = False
        self.schema = schema() if schema else None
        self._perm_manager = permission_manager

    def _build_usage(self, schema_cls: Type[CommandSchema]) -> str:
        return build_usage(self.name, schema_cls)

    @property
    def permission_overridden(self) -> bool:
        """生效值是否与声明值不同（帮助文本据此标注覆盖来源）。

        只比较取值，不查覆盖表：覆盖成与声明相同的值时行为完全一致，
        没必要在帮助里制造噪声（覆盖表本身由 `/perm list` 呈现）。
        """
        return self.permission != self.declared_permission

    async def check_permission(self, user_id: int, group_id: int = None,
                               is_group: bool = False) -> bool:
        if not self.permission or not self._perm_manager:
            return True
        return await self._perm_manager.check(
            user_id, self.permission, group_id=group_id, is_group=is_group)

    def allows_context(self, is_group: bool) -> bool:
        """scope 是否允许当前上下文 —— handle() 与 /help 共用同一判定"""
        if self.scope == "private" and is_group:
            return False
        if self.scope == "group" and not is_group:
            return False
        return True

    @property
    def help_text(self) -> str:
        """生成帮助文本（不过滤子命令；/help 会按调用者权限用 render_help 过滤）"""
        return self.render_help()

    def render_help(self, *, visible: Optional[Callable[["CommandHandler"], bool]] = None) -> str:
        """生成帮助文本。

        权限/范围提示一律取自该命令自身的声明；`visible` 用于过滤子命令
        （`/help` 传入「调用者有权访问」的判定，避免列出用不了的命令）。
        """
        prefix = f"{self.parent_name} " if self.parent_name else ""
        header_usage = self.usage or self.name
        used_placeholder = bool(self.usage) and ("<" in self.usage or "[" in self.usage)
        parts = [f"📖 /{prefix}{header_usage}"]
        if self.description:
            parts.append(f"\n  {self.description}")
        if self.subcommands:
            # 命令组本体：**不校验权限**（父命令上的 permission 只是子命令的默认值），
            # 但 `scope` 对本体生效（如 group_config 仅群聊）。两者都要写出来。
            bits = ["📦 命令组 · 本体无需权限",
                    f"作用域：{SCOPE_LABELS.get(self.scope) or '所有场景'}",
                    "各子命令的权限/作用域见下行标注"]
            parts.append("\n  " + " · ".join(bits))
        else:
            # 单命令详情：权限 + 作用域**总是**写出来（公开且不限场景也写「公开 · 所有场景」），
            # 否则用户没法从帮助里判断这条命令在自己这儿能不能用。
            req = describe_requirements(self.permission, self.scope)
            if self.permission_overridden:
                # 覆盖来源必须能查到：否则「帮助说 admin、代码里写的是 superuser」
                # 会变成一桩无头案（/perm list 是总账，这里是单条线索）。
                req += f"（已覆盖，声明值：{self.declared_permission or '公开'}）"
            parts.append(f"\n  {req}")
        if self.aliases:
            parts.append(f"\n  别名：{', '.join(self.aliases)}")
        if self.schema:
            fields = self.schema.get_fields()
            if fields:
                used_placeholder = True
                # 生成传参示例（位置/键值两种写法都用同一套占位符记号）
                pos_args = []
                kv_args = []
                short_map, _short_ambiguous = self.schema.short_flag_map()
                # short_flag_map 是「字母 → 字段名」，帮助里按字段查简写
                letters = {fname: letter for letter, fname in short_map.items()}
                for fname, field in fields.items():
                    pos_args.append(format_field_usage(fname, field))
                    short = letters.get(fname)
                    if field.required:
                        long_form = f"--{fname}=<值>"
                    elif field.default not in (None, ""):
                        long_form = f"--{fname}={field.default}"
                    else:
                        long_form = f"--{fname}=<值>"
                    if isinstance(field, BoolField) and short:
                        # 布尔字段同时给出简写与长写法：`-y` = `--yes=true`
                        token = f"-{short}|{long_form}"
                        kv_args.append(token if field.required else f"[{token}]")
                        continue
                    kv_args.append(long_form if field.required else f"[{long_form}]")
                parts.append(f"\n  位置传参：/{prefix}{self.name} {' '.join(pos_args)}")
                parts.append(f"\n  键值传参：/{prefix}{self.name} {' '.join(kv_args)}")
                parts.append("\n  参数")
                parts.extend(describe_fields(fields, short_map=letters))
        if self.subcommands:
            # 子命令清单：名字 + **权限/作用域** + 介绍。
            # ⚠️ 不带参数与用法（那属于第三级 `/help <命令组> <子命令>`）；但**权限与
            # 作用域必须留在这一行** —— 命令组页是「这个组里有哪些子命令、各自要什么
            # 权限、在哪儿能用」的唯一一页，去掉标注用户只能靠试。标注与详情行同一句话
            # （requirement_tag → describe_requirements），不会两处口径不一致。
            shown = [(sname, sub) for sname, sub in self.subcommands.items()
                     if visible is None or visible(sub)]
            parts.append("\n  子命令")
            if not shown:
                parts.append("\n    （没有你可用的子命令）")
            for sname, sub in shown:
                desc = f" — {sub.description}" if sub.description else ""
                # 标注取自该子命令**自身**的生效声明（可与父命令不同）
                tag = requirement_tag(sub.permission, sub.scope)
                parts.append(f"\n    /{self.name} {sname}{tag}{desc}")
        text = "".join(parts)
        # 占位符图例：只要这段帮助里真的渲染了 `<>` / `[]` 占位符就补一句说明。
        # 覆盖两类渲染：头部用法行（命令/子命令详情）与参数表。
        # （子命令清单不再列参数，故不会因它产生图例。）
        # ⚠️ 用显式标记判断，不要事后在文本里搜 `<`：图例自身就含 `<名称>`，
        # 那样会把「本行没有参数」的命令也误判成需要图例。
        if used_placeholder:
            text += f"\n  {PLACEHOLDER_LEGEND}"
        return text

    async def execute(self, packet: CommandPacket):
        if packet.subcommand and packet.subcommand in self.subcommands:
            sc_name = packet.subcommand
            packet.subcommand = None  # 已路由，清除避免子命令回退处理
            return await self.subcommands[sc_name].execute(packet)
        # 子命令未注册 → 回退为位置参数
        if packet.subcommand and not self.subcommands:
            packet.args.insert(0, packet.subcommand)
            packet.subcommand = None

        # Schema 验证
        validated = None
        validation_error = ""
        if self.schema:
            try:
                validated = self.schema.validate(packet.args, packet.kwargs)
            except SchemaError as e:
                validation_error = str(e)
                packet.illegal = True
                packet.illegal_reason = validation_error
                # 返回特殊标记，由 CommandManager.handle() 分发到 on_schema_error
                return ("_schema_error", validation_error)

        try:
            sig = inspect.signature(self.func)
            kwargs = {}
            for name, param in sig.parameters.items():
                if name == "packet":
                    kwargs[name] = packet
                elif name == "event" and packet.event:
                    kwargs[name] = packet.event
                elif name == "api":
                    kwargs[name] = getattr(packet.event, "_api", None) if packet.event else None
                elif name == "params" and validated is not None:
                    kwargs[name] = validated
                elif validated is not None and name in validated:
                    kwargs[name] = validated[name]
            if not kwargs:
                pass  # 函数没有匹配的参数 → 无参调用
            result = self.func(**kwargs)
            if inspect.isawaitable(result):
                return await result
            return result
        except NapCatError:
            # 插件已经用错误类型说明白了自己是哪一类失败（超时/限流/配置/无权…）：
            # **原样向上抛**，由 `CommandManager.handle()` 交给 ErrorReporter 渲染
            # —— 文案、编号、受众、节流都在那一个业主里，这里不自己拼字符串。
            raise
        except Exception as e:
            # 未分类的异常 = 插件里的 bug（含「命令函数签名写错」这类 TypeError）。
            # ⚠️ 这里**不能**再吞成 None：吞掉就是用户侧「有问不答」——
            # 他分不清是自己打错了、还是机器人死了。包成 PluginRuntimeError
            # 向上抛，handle() 会记完整堆栈（含命令名）并回一句带编号的提示。
            raise PluginRuntimeError(
                short_reason(e), plugin=self.plugin, cause=e) from e


# ==================== 命令管理器 ====================

class CommandManager:
    """命令管理器 — 注册、解析、路由、权限校验、Schema 验证。

    用法:
        from napcat.command import CommandSchema, QQField, DurationField, StrField

        class BanSchema(CommandSchema):
            user_id = QQField(required=True, description="目标QQ")
            duration = DurationField(default=600, min=0, max=2592000)
            reason = StrField(default="", max_length=200)

        @cmds.register("ban", schema=BanSchema, permission="admin")
        async def cmd_ban(params, event):
            await api.set_group_ban(str(event.group_id), str(params.user_id), params.duration)
    """

    def __init__(self, prefixes: list[str] = None, perm_manager: "PermissionManager" = None,
                 max_args: int = 20, max_cmd_len: int = 64,
                 groups_ref=None, plugins_ref=None, cfg_ref=None,
                 event_bus_ref=None, timer_ref=None):
        self.parser = CommandParser(prefixes=prefixes, max_args=max_args, max_cmd_len=max_cmd_len)
        self._commands: dict[str, CommandHandler] = {}
        self._perm_manager = perm_manager
        self._on_illegal: Optional[Callable] = None
        self._on_denied: Optional[Callable] = None
        # 错误反馈（分类/文案/受众/节流）的唯一业主；客户端启动时注入带投递通道的
        # 实例。默认这个没有投递通道 —— 独立使用 CommandManager（测试、脚本）时
        # 仍会把失败渲染成一句话回给调用方，而不是静默返回 None。
        self._error_reporter: Optional[ErrorReporter] = ErrorReporter()
        # 用法错误（Schema 校验失败）的回复由框架统一负责（重定向到该命令的帮助页），
        # 不再有插件回调：那个钩子只有一个槽位，两个插件注册时后者覆盖前者，
        # 用户看到哪种报错取决于插件加载顺序。
        # 「服务范围」之类的可见性过滤器：签名 (handler, event) -> bool。
        # /help 会用它过滤，保证「帮助里显示什么」与「实际能不能用」口径一致。
        self._visibility_filters: list[Callable] = []
        self._groups_ref = groups_ref
        self._plugins_ref = plugins_ref
        self._cfg_ref = cfg_ref
        self._event_bus_ref = event_bus_ref
        self._timer_ref = timer_ref
        # 运行时权限覆盖：{命令规范名: 权限节点}，来源是 config.json 的
        # command_permissions。必须在 _register_builtins() **之前**载入 ——
        # 内置命令同样可被覆盖，而构造期/register() 就是唯一的重放点。
        self._perm_overrides: dict[str, str] = {}
        self._load_perm_overrides()
        # 运行时命令别名：{命令规范名: [别名…]}，来源 config.json 的
        # `command_aliases`。与 /perm 同一套纪律：**重放点在登记处**
        # （`_install_handler`），所以插件热重载/重新注册后别名不会丢；
        # 「配置里有这个命令」表示**接管**该命令的全部别名（空列表 = 清空，
        # 连插件在 register(aliases=…) 里声明的也压掉）。
        self._alias_overrides: dict[str, list[str]] = {}
        self._load_alias_overrides()
        self._register_builtins()

    # ==================== 运行时权限覆盖 ====================
    #
    # 覆盖值优先于 register(permission=...) 的声明值，持久化在 config.json 的
    # command_permissions。**重放点只有命令登记处（_install_handler / register）**——
    # 启动时的内置/插件命令、以及插件热重载后重新注册的 handler 都走它，
    # 所以 /reload 不会让覆盖悄悄回退。设计见
    # docs/plans/2026-09-30-command-permission-override.md §1。

    def _load_perm_overrides(self) -> None:
        """从配置读覆盖表；非字符串值只警告不采纳（手改配置的容错）。"""
        raw = self._cfg_ref.get("command_permissions", {}) if self._cfg_ref else {}
        table: dict[str, str] = {}
        if isinstance(raw, dict):
            for name, value in raw.items():
                if isinstance(value, str):
                    table[str(name)] = value
                else:
                    _cmd_logger().warning(
                        "command_permissions.%s 不是字符串（%r），已忽略", name, value)
        elif raw:
            _cmd_logger().warning("command_permissions 不是对象（%r），已忽略", raw)
        self._perm_overrides = table

    def _save_perm_overrides(self) -> bool:
        """整表写回配置；空表删键（别在 config.json 里留一个空对象）。

        返回 False 表示没有配置管理器 → 覆盖只在内存生效，调用方要如实告知用户。
        """
        if self._cfg_ref is None:
            return False
        if self._perm_overrides:
            self._cfg_ref.set("command_permissions", dict(self._perm_overrides))
        else:
            self._cfg_ref.delete("command_permissions")
        return True

    # ==================== 运行时命令别名 ====================
    #
    # 别名不是「另一条命令」，而是 `_commands` 里指向同一个 handler 的另一个键：
    # 这样权限、schema、作用域、归属标签天然共用一份，卸载时也一起回收。

    def _load_alias_overrides(self) -> None:
        """从配置读别名表；结构不对的条目只告警不采纳。"""
        raw = self._cfg_ref.get("command_aliases", {}) if self._cfg_ref else {}
        table: dict[str, list[str]] = {}
        if isinstance(raw, dict):
            for name, values in raw.items():
                if not isinstance(values, list) or not all(
                        isinstance(v, str) for v in values):
                    _cmd_logger().warning(
                        "command_aliases.%s 不是字符串数组（%r），已忽略", name, values)
                    continue
                table[str(name)] = [str(v).strip() for v in values if str(v).strip()]
        elif raw:
            _cmd_logger().warning("command_aliases 不是对象（%r），已忽略", raw)
        self._alias_overrides = table

    def _save_alias_overrides(self) -> bool:
        """整表写回配置；空表删键（别留空对象）。返回 False = 未持久化。"""
        if self._cfg_ref is None:
            return False
        if self._alias_overrides:
            self._cfg_ref.set("command_aliases",
                              {k: list(v) for k, v in self._alias_overrides.items()})
        else:
            self._cfg_ref.delete("command_aliases")
        return True

    def _drop_alias_entries(self, handler: "CommandHandler",
                            keep: Iterable[str] = ()) -> None:
        """删掉所有「指向这个 handler」的键（保留规范名与 `keep` 里的别名）。"""
        canonical = handler.name
        keep = set(keep)
        for key in [k for k, h in self._commands.items()
                    if h is handler and k != canonical and k not in keep]:
            self._commands.pop(key, None)

    def _apply_alias_override(self, name: str, handler: "CommandHandler") -> None:
        """把「配置里的别名」套到刚登记的命令上（所有登记路径都会调它）。

        配置存在 → 以配置为**全集**（可以清空插件代码里声明的别名）；
        不存在 → 用 `register(aliases=…)` 的声明值。
        """
        declared = list(handler.aliases or [])
        configured = self._alias_overrides.get(name)
        effective = list(configured) if configured is not None else declared
        handler.aliases = effective
        self._drop_alias_entries(handler, keep=effective)
        for alias in effective:
            self._commands[alias] = handler

    @staticmethod
    def _valid_alias_name(alias: str, max_len: int) -> str:
        """别名合法性：非空、长度受限、字母开头（与命令名同一条规则）。"""
        alias = str(alias or "").strip()
        if not alias:
            return "别名不能为空"
        if len(alias) > max_len:
            return f"别名过长（最多 {max_len} 字符）"
        if not valid_command_name(alias, max_len):
            return (f"别名 {alias!r} 非法：字母开头（支持中文），"
                    "只能含字母/数字/下划线/连字符")
        return ""

    def _resolve_alias_target(self, raw: str) -> tuple[str, "CommandHandler"] | tuple[None, str]:
        """解析别名要指向的命令 / 命令组；失败返回 `(None, 原因)`。"""
        target = (raw or "").strip().lstrip("/")
        if not target:
            return None, "缺少目标命令"
        resolved = self.find_command(target)
        if resolved is None:
            return None, f"未知命令: /{target}"
        canonical, handler = resolved
        if "." in canonical:
            return None, (f"/{canonical} 是子命令；别名只支持顶层命令"
                          "（可给父命令起别名，或用 /help 查子命令）")
        if handler.subcommands:
            # 命令组**可以**起别名：别名在 `_commands` 里指向同一个父 handler，
            # `/别名 子命令` 照常路由到子命令，`/别名` 照常显示命令组帮助页。
            # （子命令 `父.子` 本身暂不支持别名 —— 那需要另一套“别名→子命令”的
            # 归属与回收规则，等有真实需求再做。）
            return canonical, handler
        return canonical, handler

    def alias_overrides(self) -> dict[str, list[str]]:
        """别名表副本（诊断/测试用；改动请走 /alias 的 add/del/set/move）。"""
        return {k: list(v) for k, v in self._alias_overrides.items()}

    def add_alias(self, alias: str, target: str) -> tuple[bool, str]:
        """给命令加一个别名（校验 → 写入 → 落盘）。"""
        reason = self._valid_alias_name(alias, self.parser.max_cmd_len)
        if reason:
            return False, reason
        resolved, handler = self._resolve_alias_target(target)
        if resolved is None:
            return False, handler
        canonical = resolved
        if alias == canonical:
            return False, f"别名与命令名相同（/{canonical}），无需设置"
        occupied = self._commands.get(alias)
        if occupied is not None:
            if occupied is handler:
                return False, f"/{canonical} 已经有别名 /{alias}"
            other = occupied.name or alias
            return False, (f"/{alias} 已被 /{other} 占用（命令名或别的别名先占），"
                           "换一个别名，或先删掉它")
        effective = list(handler.aliases or [])
        effective.append(alias)
        self._alias_overrides[canonical] = effective
        self._apply_alias_override(canonical, handler)
        saved = self._save_alias_overrides()
        _cmd_logger().info("命令别名：/%s 新增 /%s", canonical, alias)
        tail = "" if saved else "（⚠️ 未持久化：配置未初始化，重启即失效）"
        return True, f"✅ 已给 /{canonical} 添加别名 /{alias}{tail}"

    def remove_alias(self, alias: str) -> tuple[bool, str]:
        """删除一个别名（不能删命令名；插件代码声明的别名也会被压制）。"""
        alias = str(alias or "").strip().lstrip("/")
        handler = self._commands.get(alias)
        if handler is None:
            return False, f"没有这个命令或别名: /{alias}"
        canonical = handler.name or alias
        if alias == canonical:
            return False, (f"/{alias} 是命令名不是别名；要禁用命令用 "
                           "/pause command <名>")
        effective = [a for a in (handler.aliases or []) if a != alias]
        self._alias_overrides[canonical] = effective
        self._apply_alias_override(canonical, handler)
        saved = self._save_alias_overrides()
        _cmd_logger().info("命令别名：/%s 删除 /%s", canonical, alias)
        tail = "" if saved else "（⚠️ 未持久化：配置未初始化）"
        return True, f"✅ 已删除 /{canonical} 的别名 /{alias}{tail}"

    def set_aliases(self, target: str, aliases: list[str]) -> tuple[bool, str]:
        """整体替换某个命令的别名集合（空列表 = 清空，含代码声明值）。"""
        resolved, handler = self._resolve_alias_target(target)
        if resolved is None:
            return False, handler
        canonical = resolved
        cleaned: list[str] = []
        for alias in aliases:
            reason = self._valid_alias_name(alias, self.parser.max_cmd_len)
            if reason:
                return False, reason
            if alias == canonical:
                return False, f"别名不能等于命令名 /{canonical}"
            occupied = self._commands.get(alias)
            if occupied is not None and occupied is not handler:
                return False, (f"/{alias} 已被 /{occupied.name or alias} 占用")
            if alias not in cleaned:
                cleaned.append(alias)
        self._alias_overrides[canonical] = cleaned
        self._apply_alias_override(canonical, handler)
        saved = self._save_alias_overrides()
        _cmd_logger().info("命令别名：/%s = %s", canonical, cleaned)
        tail = "" if saved else "（⚠️ 未持久化：配置未初始化）"
        if not cleaned:
            return True, f"✅ 已清空 /{canonical} 的全部别名{tail}"
        return True, (f"✅ /{canonical} 的别名已设为："
                      + "、".join(f"/{a}" for a in cleaned) + tail)

    def move_alias(self, alias: str, target: str) -> tuple[bool, str]:
        """把已有别名改指向另一条命令。"""
        alias = str(alias or "").strip().lstrip("/")
        old_handler = self._commands.get(alias)
        if old_handler is None:
            return False, f"没有这个别名: /{alias}"
        old_canonical = old_handler.name or alias
        if alias == old_canonical:
            return False, f"/{alias} 是命令名不是别名"
        resolved, _handler = self._resolve_alias_target(target)
        if resolved is None:
            return False, str(_handler)
        # 先从旧命令名下摘掉，否则 add_alias 的「已被占用」检查会挡住自己
        self._commands.pop(alias, None)
        ok, message = self.add_alias(alias, target)
        if not ok:
            self._commands[alias] = old_handler       # 回滚
            return False, message
        old_effective = [a for a in (old_handler.aliases or []) if a != alias]
        self._alias_overrides[old_canonical] = old_effective
        self._apply_alias_override(old_canonical, old_handler)
        self._save_alias_overrides()
        return True, (f"✅ 别名 /{alias} 已从 /{old_canonical} 改指向 /{resolved}")

    def _apply_perm_override(self, name: str, handler: "CommandHandler") -> None:
        """把覆盖表里该命令的值套到 handler 上（所有登记路径都会调它）。"""
        if name not in self._perm_overrides:
            return
        value = self._perm_overrides[name]
        handler.permission = value
        if handler.subcommands:
            # 命令组本体不参与权限校验（handle() 的 is_namespace_body 分支），
            # 套上去也不生效 —— 配置里手写的这类条目要留痕，别让人以为设成功了。
            _cmd_logger().warning(
                "命令组本体 %s 的权限覆盖不生效（权限属于各子命令）", name)
        if value and self._perm_manager and not self._perm_manager.has_node(value):
            _cmd_logger().warning(
                "命令 %s 的覆盖指向未注册权限节点 %r：非超级用户将无法使用它",
                name, value)

    def _install_handler(self, name: str, handler: "CommandHandler") -> None:
        """登记命令并套用覆盖 —— **所有**命令进入 `_commands` 的唯一入口。

        ⚠️ 内置命令是在 `_register_builtins()` 里直接 new 出 handler 的，**不走**
        `register()`；如果只在 `register()` 里重放覆盖，`/perm set ping ...` 会写进
        配置却对运行中的内置命令无效（重启后才"生效"）。所以登记动作统一收敛到这里。
        """
        self._commands[name] = handler
        self._apply_perm_override(name, handler)
        self._apply_alias_override(name, handler)

    @property
    def perm_overrides(self) -> dict[str, str]:
        """覆盖表副本（诊断/测试用；改动请走 /perm 的 set/reset 路径）。"""
        return dict(self._perm_overrides)

    def iter_handlers(self) -> list[tuple[str, "CommandHandler"]]:
        """(规范名, handler) 列表 —— 含子命令，别名不重复列出。"""
        out: list[tuple[str, CommandHandler]] = []
        seen: set[int] = set()
        for name, handler in self._commands.items():
            if id(handler) in seen:
                continue
            seen.add(id(handler))
            base = handler.name or name
            out.append((base, handler))
            for sname, sub in handler.subcommands.items():
                out.append((f"{base}.{sname}", sub))
        return out

    def find_command(self, target: str) -> Optional[tuple[str, "CommandHandler"]]:
        """按名字解析命令 → (规范名, handler)；支持顶层名/别名与 `父.子`。

        规范名是覆盖表的键：别名解析到同一个 handler，但只存规范名一份 ——
        否则同一条命令会有两条覆盖，谁生效取决于注册顺序。
        """
        target = (target or "").strip()
        if not target:
            return None
        handler = self._commands.get(target)
        if handler is not None:
            return (handler.name or target), handler
        if "." in target:
            parent, child = target.split(".", 1)
            parent_h = self._commands.get(parent)
            if parent_h is not None and child in parent_h.subcommands:
                base = parent_h.name or parent
                return f"{base}.{child}", parent_h.subcommands[child]
        return None

    def resolve_perm_node(self, raw: str) -> Optional[str]:
        """把用户输入解析为规范权限节点；None = 非法（空或未注册）。

        公开类写法（`public` / `everyone` / `公开` / `所有人` / `-`）统一归一化为空串：
        `PermissionManager.check()` 里 `everyone` 与 `""` 完全等价（都无条件通过），
        归一化只是为了让 /help 不出现两个同义分组。
        """
        value = (raw or "").strip()
        if value in ("-", "公开", "所有人") or value.lower() in ("public", "everyone"):
            return ""
        if not value:
            return None
        if self._perm_manager is None:
            return value
        for node in self._perm_manager.known_nodes():
            if node.lower() == value.lower():
                return node
        return None

    def perm_node_choices(self) -> str:
        """可用取值的用户可读清单（`everyone` 归一到 `public`，故不单列）。"""
        if self._perm_manager is None:
            return "public（公开）"
        roles = [r for r in self._perm_manager.ROLE_HIERARCHY if r != "everyone"]
        return "、".join(roles + ["public（公开）"] + self._perm_manager.custom_nodes())

    def _register_builtins(self):
        """注册内置命令: help, bind, unbind, groups, ping, plugins"""

        async def _cmd_help(packet: CommandPacket):
            evt = packet.event
            is_group = evt.message_type == "group" if evt else False

            # /help <主命令> [子命令] → 单命令详情（三级入口的后两级）
            #   /help demo        → 命令组清单（子命令名字 + 介绍）
            #   /help demo alpha  → 该子命令的介绍 + 用法（等价于 /help demo.alpha）
            # 空格写法在 args 里是 ["demo", "alpha"]：解析器把第二个 token 当
            # subcommand 剥走，execute() 又塞回 args（help 自己没有子命令，必然走
            # 这条回填路径）。此前只读 args[0]，于是 `/help demo alpha` 静默退化成
            # `/help demo` —— 用户查的是子命令，拿到的却是父命令清单。
            target = packet.arg(0, "")
            wanted_sub = packet.arg(1, "") if target else ""
            if target:
                # 名字解析统一交给 find_command()（/perm 用的同一份）：此前 help
                # 自己维护了一套等价的点号查找，两条解析路径迟早分叉。
                found = self.find_command(f"{target}.{wanted_sub}" if wanted_sub else target)
                if found is None and wanted_sub:
                    # 第二个 token 不是子命令时按主命令处理：`/help perm set` 这类
                    # 「用法里带取值」的写法不该被判成未知命令。
                    found = self.find_command(target)
                handler = found[1] if found is not None else None
                if handler and handler.func is not _cmd_help:
                    # 没权限的命令按「不存在」处理，与 handle() 的隐藏口径一致
                    if not await self.command_allowed(handler, evt):
                        return f"未知命令: /{target}"
                    # 与「裸调命令组」共用同一渲染，两处不会漂移
                    return await self.help_page(handler, evt)
                return f"未知命令: /{target}"

            # /help → 命令清单（三级入口的第一级）
            # 两段：`[📦 命令组]`（带子命令的命令，介绍取本体 description）与
            # `[📖 命令]`（没有子命令的命令）。两段的行**排成同一个样子**：
            # `/{名字} — {介绍}` —— 与第二级的子命令清单同一版式。
            # ⚠️ 不按权限分类：分组标题（`[🛡️ admin]` 之类）会把同一种命令拆成几段，
            # 而且「看得出需要什么权限」对用不了它的用户没有意义 —— 用不了的命令
            # 根本不会出现在这里（下面的 scope/permission 过滤照旧）。
            # ⚠️ 子命令**不进这一级**：它们由 `/help <命令组>` 逐条列出。此前这里
            # 把每个子命令也塞进列表，几十条子命令把命令组本身淹掉（列表首屏全是
            # `/aichat xxx`），且同一份子命令清单在「第一级」和「第二级」有两处
            # 渲染口径 —— 与 /help <命令组> / /help <命令组> <子命令> 的分层重复。
            namespaces: list[str] = []
            leaves: list[str] = []
            seen: set[int] = set()

            for name, handler in sorted(self._commands.items()):
                if handler.func is _cmd_help:
                    continue
                if id(handler) in seen:
                    continue
                seen.add(id(handler))
                if handler.parent_name:
                    continue
                # 第一级只给「介绍」（命令名 + description）：用法属于第二级
                # （`/help <命令名>`），列表才不会又长又挤。
                line = f"  /{name} — {handler.description or '无描述'}"
                # 可用性判据与 handle() 共用同一份（command_allowed）：
                # 命令组本体不设权限、但插件过滤器要问一遍本体，否则「整块服务未开通」
                # 的命名空间仍会把名字和描述列出来。
                if await self.command_allowed(handler, evt):
                    (namespaces if handler.subcommands else leaves).append(line)
            count = len(namespaces) + len(leaves)
            lines = [f"📖 可用命令（{count}）"]
            if is_group:
                lines.append(GROUP_AT_HINT)
            if namespaces:
                lines.append("\n[📦 命令组]")
                lines.extend(namespaces)
            if leaves:
                lines.append("\n[📖 命令]")
                lines.extend(leaves)
            if count == 0:
                lines.append("\n（当前身份没有可用的命令）")
            lines.append("\n" + PLACEHOLDER_LEGEND)
            lines.append("💡 /help <命令名> 查看子命令或用法")
            return "\n".join(lines)

        self._install_handler("help", CommandHandler(
            "help", _cmd_help, description="显示帮助信息", usage="help [命令名]",
            permission_manager=self._perm_manager))

        # 内置私聊 superuser 命令
        # 内置私聊 superuser 命令
        def _cmd_ping(packet=None):
            return "pong!"

        def _cmd_groups(packet=None):
            gr = self._groups_ref
            if gr is None:
                return "群绑定未初始化"
            if gr.all_groups:
                return "当前为全群模式（监听所有群）"
            bound = gr.list_bound()
            if not bound:
                return "未绑定任何群"
            return f"已绑定 {len(bound)} 个群: {', '.join(map(str, bound))}"

        def _cmd_bind(packet: CommandPacket):
            gr = self._groups_ref
            if gr is None:
                return "群绑定未初始化"
            evt = packet.event
            gid = None
            for a in packet.args:
                if a.lower() == "all":
                    gr.bind_all()
                    return "已切换为全群模式（监听所有群）"
                if a.isdigit():
                    gid = int(a)
                    break
            if not gid and evt and evt.group_id:
                gid = evt.group_id
            if not gid:
                return "请指定群号: /bind <群号> 或 /bind all"
            ok = gr.bind(gid)
            if ok and self._cfg_ref:
                self._cfg_ref.set("bound_groups", list(gr._bound))
                self._cfg_ref.set("all_groups", False)
            return f"群 {gid} {'已绑定' if ok else '绑定失败（可能已存在）'}"

        def _cmd_unbind(packet: CommandPacket):
            gr = self._groups_ref
            if gr is None:
                return "群绑定未初始化"
            evt = packet.event
            gid = None
            for a in packet.args:
                if a.isdigit():
                    gid = int(a)
                    break
            if not gid and evt and evt.group_id:
                gid = evt.group_id
            if not gid:
                return "请指定群号: /unbind <群号>"
            ok = gr.unbind(gid)
            if ok and self._cfg_ref:
                self._cfg_ref.set("bound_groups", list(gr._bound))
            return f"群 {gid} {'已解绑' if ok else '解绑失败（可能未绑定）'}"

        def _cmd_plugins(packet=None):
            pm = self._plugins_ref
            if pm is None:
                return "插件管理器未初始化"
            plist = pm.plugins
            blocked = getattr(pm, "_dep_blocked", {}) or {}
            if not plist and not blocked:
                return "没有加载的插件"
            lines = ["已加载插件:"]
            # 依赖顺序展示：被依赖的排前面，一眼能看出谁在谁之上
            try:
                order = pm.load_order()
            except Exception:  # noqa: BLE001 - 依赖声明成环时退回字典序
                order = sorted(set(plist) | set(blocked))
            rank = {name: i for i, name in enumerate(order)}
            for name in sorted(set(plist) | set(blocked),
                               key=lambda n: (rank.get(n, len(order)), n)):
                pinst = plist.get(name)
                if pinst is None:
                    # 显示**实时**判定：依赖可能刚装上（缓存里的原因会显得自相矛盾）
                    verdict = None
                    try:
                        verdict = pm.dependency_verdict(name)
                    except Exception:  # noqa: BLE001
                        verdict = None
                    reason = (verdict.reason() if verdict and not verdict.ok
                              else blocked.get(name, "")) or "依赖不满足"
                    lines.append(f"  [⛔] {name} —— {reason}")
                    continue
                status = "✓" if pinst.enabled else "✗"
                try:
                    required, optional = pinst.dependency_names()
                except Exception:  # noqa: BLE001
                    required, optional = [], []
                tags = ""
                if required or optional:
                    parts = []
                    if required:
                        parts.append("依赖 " + "、".join(required))
                    if optional:
                        parts.append("可选 " + "、".join(optional))
                    tags = "（" + " · ".join(parts) + "）"
                lines.append(f"  [{status}] {name} v{pinst.version}{tags}")
            return "\n".join(lines)

        async def _cmd_reload(packet: CommandPacket):
            pm = self._plugins_ref
            if pm is None:
                return "插件管理器未初始化"
            target = packet.arg(0, "")
            if target:
                ok = await pm.reload(target)
                return f"插件 {target} {'重载成功' if ok else '重载失败（检查插件名）'}"
            # 无参数 → 重新加载全部（含新插件）
            import asyncio
            await pm.load_all()
            await pm.enable_all()
            count = len(pm.plugins)
            return f"已重新加载全部插件，共 {count} 个"

        def _cmd_events(packet=None):
            eb = self._event_bus_ref
            if eb is None:
                return "事件总线未初始化"
            metas = eb.list_all()
            if not metas:
                return "没有注册的事件监听器"
            lines = ["已注册事件监听器:"]
            for m in sorted(metas, key=lambda x: x.id):
                status = "⏸ 暂停" if m.paused else "▶ 运行"
                evt_name = m.event_type.__name__
                desc = f" — {m.description}" if m.description else ""
                lines.append(f"  [{status}] {m.id} ({evt_name}){desc}")
            lines.append(f"\n共 {len(metas)} 个监听器")
            return "\n".join(lines)

        def _cmd_commands(packet=None):
            """已注册命令清单（按权限分组，含子命令的自身权限/范围）。"""
            seen: set[int] = set()
            namespaces: list[str] = []
            groups: dict[str, list[str]] = {}
            total = 0

            def add_leaf(h: CommandHandler, path: str) -> None:
                scope = SCOPE_LABELS.get(h.scope, "所有场景")
                desc = h.description or "-"
                groups.setdefault(h.permission or "", []).append(
                    f"  /{path} · {scope} — {desc}")

            for name, handler in sorted(self._commands.items()):
                if id(handler) in seen:
                    continue
                seen.add(id(handler))
                total += 1
                if handler.subcommands:
                    # 这是 `/commands`（superuser 诊断）的列表：它是**运维视角**，
                    # 只列「注册了什么」，不按插件过滤器裁剪 —— 否则一个服务未开通
                    # 的命名空间会在诊断里凭空消失，运维查不到自己其实注册了它。
                    namespaces.append(
                        f"  /{name} · {SCOPE_LABELS.get(handler.scope, '所有场景')}"
                        f" — {handler.description or '-'}")
                    for sname, sub in sorted(handler.subcommands.items()):
                        seen.add(id(sub))
                        total += 1
                        add_leaf(sub, f"{name} {sname}")
                    continue
                add_leaf(handler, handler.usage or name)

            lines = [f"🧩 已注册命令（{total}）"]
            if namespaces:
                lines.append("\n[📦 命令组]（本体无需权限）")
                lines.extend(namespaces)
            known = ["", "member", "admin", "owner", "superuser"]
            for perm_label in known + sorted(k for k in groups if k not in known):
                items = groups.get(perm_label, [])
                if not items:
                    continue
                lines.append("\n" + permission_group_label(perm_label))
                lines.extend(items)
            return "\n".join(lines)

        def _cmd_timers(packet=None):
            tm = self._timer_ref
            if tm is None:
                return "定时器未初始化"
            metas = tm.list_all()
            if not metas:
                return "没有注册的定时任务"
            lines = ["已注册定时任务:"]
            for m in sorted(metas, key=lambda x: x["id"]):
                status = "▶" if m["running"] else "⏸"
                desc = f" — {m['description']}" if m["description"] else ""
                lines.append(f"  [{status}] {m['id']} ({m['type']}:{m['value']}){desc}")
            lines.append(f"\n共 {len(metas)} 个定时任务")
            return "\n".join(lines)


        # ---- /stop：命令组（event / command / timer）----
        def _cmd_stop_event(packet: CommandPacket, target: str):
            eb = self._event_bus_ref
            if eb and eb.stop(target):
                return f"事件监听器 {target} 已移除"
            return f"未找到事件监听器: {target}"

        def _cmd_stop_command(packet: CommandPacket, target: str):
            removed = self._commands.pop(target, None)
            if removed is not None:
                # 别名与命令共用 handler：不一起删会留下「命令没了但别名还能用」
                self._drop_alias_entries(removed)
                return f"命令 /{target} 已移除"
            return f"未找到命令: /{target}"

        def _cmd_stop_timer(packet: CommandPacket, target: str):
            tm = self._timer_ref
            if tm:
                tm.cancel(target)
                return f"定时任务 {target} 已取消"
            return "定时器未初始化"

        # ---- /pause：命令组（event / command / timer）----
        def _cmd_pause_event(packet: CommandPacket, target: str):
            eb = self._event_bus_ref
            if eb is None:
                return "事件总线未初始化"
            metas = eb.list_all()
            current = next((m for m in metas if m.id == target), None)
            if current is None:
                return f"未找到事件监听器: {target}"
            if current.paused:
                eb.resume(target)
                return f"事件监听器 {target} 已恢复"
            eb.pause(target)
            return f"事件监听器 {target} 已暂停"

        def _cmd_pause_command(packet: CommandPacket, target: str):
            h = self._commands.get(target)
            if h is None:
                return f"未找到命令: /{target}"
            h.disabled = not h.disabled
            return f"命令 /{target} 已{'禁用' if h.disabled else '启用'}"

        def _cmd_pause_timer(packet: CommandPacket, target: str):
            tm = self._timer_ref
            if tm is None:
                return "定时器未初始化"
            metas = tm.list_all()
            current = next((m for m in metas if m["id"] == target), None)
            if current is None:
                return f"未找到定时任务: {target}"
            if current["running"]:
                tm.pause_timer(target)
                return f"定时任务 {target} 已暂停"
            tm.resume_timer(target)
            return f"定时任务 {target} 已恢复"

        # ---- /perm：运行时权限覆盖（superuser · 私聊）----

        def _perm_entry_note(key: str) -> str:
            """覆盖条目的补充说明：未注册 / 命令组本体 / 声明值。"""
            resolved = self.find_command(key)
            if resolved is None:
                return "（未注册，可用 /perm reset 清除）"
            handler = resolved[1]
            if handler.subcommands:
                return "（命令组本体，不生效）"
            return f"（声明值：{handler.declared_permission or '公开'}）"

        def _perm_list() -> str:
            if not self._perm_overrides:
                return "当前没有权限覆盖（全部使用声明值）"
            lines = [f"🔐 权限覆盖（{len(self._perm_overrides)}）"]
            for key in sorted(self._perm_overrides):
                lines.append(f"  /{key} → {self._perm_overrides[key] or '公开'} "
                             f"{_perm_entry_note(key)}")
            lines.append("\n💡 /perm get <命令> 看单条 · /perm reset <命令|all> 恢复声明值")
            return "\n".join(lines)

        def _perm_get(target: str) -> str:
            resolved = self.find_command(target)
            if resolved is None:
                # 命令已卸载但配置里还有覆盖：要能查到、也要能被 reset 清掉
                if target in self._perm_overrides:
                    return (f"/{target} 未注册，但配置里有覆盖："
                            f"{self._perm_overrides[target] or '公开'}"
                            f"（/perm reset {target} 可清除）")
                return f"未知命令: /{target}"
            key, handler = resolved
            if handler.subcommands:
                subs = "、".join(f"{key}.{s}" for s in sorted(handler.subcommands))
                return f"/{key} 是命令组，本体不校验权限；请查子命令：{subs}"
            current = handler.permission or "公开"
            if key in self._perm_overrides:
                return (f"/{key}：{current}（覆盖值，声明值："
                        f"{handler.declared_permission or '公开'}）")
            return f"/{key}：{current}（声明值，无覆盖）"

        def _perm_set(target: str, raw_value: str) -> str:
            resolved = self.find_command(target)
            if resolved is None:
                return f"未知命令: /{target}"
            key, handler = resolved
            if handler.subcommands:
                subs = "、".join(f"{key}.{s}" for s in sorted(handler.subcommands))
                return (f"/{key} 是命令组，本体不校验权限。\n"
                        f"请逐条设置子命令：{subs}")
            node = self.resolve_perm_node(raw_value)
            if node is None:
                return (f"无效的权限值：{raw_value}\n"
                        f"可用值：{self.perm_node_choices()}")
            handler.permission = node
            self._perm_overrides[key] = node
            saved = self._save_perm_overrides()
            _cmd_logger().info("权限覆盖：/%s → %s（声明值 %s）",
                               key, node or "公开",
                               handler.declared_permission or "公开")
            tail = "" if saved else "（⚠️ 未持久化：配置未初始化，重启即失效）"
            return (f"/{key} 权限已设为 {node or '公开'}"
                    f"（原声明值：{handler.declared_permission or '公开'}）{tail}")

        def _perm_reset(target: str) -> str:
            if target.lower() == "all":
                count = len(self._perm_overrides)
                if not count:
                    return "当前没有任何权限覆盖"
                self._perm_overrides.clear()
                for _key, handler in self.iter_handlers():
                    handler.permission = handler.declared_permission
                saved = self._save_perm_overrides()
                _cmd_logger().info("权限覆盖：已清除全部 %d 条", count)
                tail = "" if saved else "（⚠️ 未持久化：配置未初始化）"
                return f"已清除全部 {count} 条权限覆盖{tail}"
            resolved = self.find_command(target)
            key = resolved[0] if resolved else target
            if key not in self._perm_overrides:
                return f"/{key} 没有权限覆盖（无需恢复）"
            self._perm_overrides.pop(key)
            if resolved is not None:
                resolved[1].permission = resolved[1].declared_permission
            saved = self._save_perm_overrides()
            _cmd_logger().info("权限覆盖：清除 /%s", key)
            tail = "" if saved else "（⚠️ 未持久化：配置未初始化）"
            if resolved is None:
                return f"已清除残留覆盖 /{key}{tail}"
            return (f"/{key} 已恢复声明值："
                    f"{resolved[1].declared_permission or '公开'}{tail}")

        def _sub_perm_list(packet: CommandPacket = None):
            return _perm_list()

        def _sub_perm_get(packet: CommandPacket, target: str):
            return _perm_get(target)

        def _sub_perm_set(packet: CommandPacket, target: str, permission: str):
            return _perm_set(target, permission)

        def _sub_perm_reset(packet: CommandPacket, target: str):
            return _perm_reset(target)

        # ---- /userperm：用户级权限覆盖（superuser · 私聊）----
        #
        # 与 /perm 的分工：
        #   /perm     = 改**命令**要求的权限（谁有资格用某条命令）；
        #   /userperm = 改**用户**的权限级别（这个人在全局/某个群里算几级）。
        # 优先级：群内覆盖 > 全局覆盖 > 群角色（群主/管理员默认 admin）> 默认成员。

        def _userperm_qq(raw: str) -> int:
            text = str(raw or "").strip().lstrip("@")
            if text.lower().startswith("qq="):
                text = text[3:]
            if not text.isdigit() or int(text) <= 0:
                raise ValueError(f"非法 QQ：{raw!r}")
            return int(text)

        def _userperm_levels() -> str:
            return " / ".join(ASSIGNABLE_USER_LEVELS)

        def _userperm_where(group_id: int) -> str:
            return f"群 {group_id} 内" if group_id else "全局"

        def _userperm_effective(uid: int, group_id: int) -> str:
            perm = self._perm_manager
            level = perm.user_level_override(uid, group_id=group_id or None)[0]
            source = perm.user_level_source(uid, group_id=group_id or None,
                                            is_group=bool(group_id))
            score = perm.effective_user_level(uid, group_id=group_id or None,
                                              is_group=bool(group_id))
            name = next((n for n, v in perm.ROLE_HIERARCHY.items() if v == score),
                        str(score))
            return f"{name}（来源：{source}）"

        def _userperm_list(group_id: Optional[int] = None) -> str:
            perm = self._perm_manager
            data = perm.export_user_levels()
            users = data.get("users", {})
            groups = data.get("groups", {})
            if group_id:
                table = groups.get(str(group_id), {})
                if not table:
                    return (f"群 {group_id} 内没有用户级覆盖"
                            f"（群主/管理员默认 admin，其他成员默认 member）")
                lines = [f"🔐 群 {group_id} 内的权限覆盖（{len(table)}）"]
                for uid in sorted(table, key=int):
                    lines.append(f"  QQ {uid} → {table[uid]}")
            else:
                if not users and not groups:
                    return ("当前没有用户级覆盖"
                            "（群主/管理员默认 admin，其他成员默认 member）")
                total = len(users) + sum(len(t) for t in groups.values())
                lines = [f"🔐 用户权限覆盖（{total} 条）"]
                if users:
                    lines.append("  全局：")
                    for uid in sorted(users, key=int):
                        lines.append(f"    QQ {uid} → {users[uid]}")
                for gid in sorted(groups, key=int):
                    lines.append(f"  群 {gid}：")
                    for uid in sorted(groups[gid], key=int):
                        lines.append(f"    QQ {uid} → {groups[gid][uid]}")
            lines.append("\n💡 /userperm set <QQ> <级别> [群号] · "
                         "/userperm get <QQ> [--group=群号] · "
                         "/userperm clear <QQ> [--group=群号]")
            return "\n".join(lines)

        def _userperm_get(uid: int, group_id: int) -> str:
            perm = self._perm_manager
            global_level = perm.user_level_override(uid)[0]
            group_level = (perm.user_level_override(uid, group_id=group_id)[0]
                           if group_id else "")
            lines = [f"🔐 QQ {uid}"]
            if perm.is_superuser(uid):
                lines.append("  该用户是**超管**（config.json 的 superusers），"
                             "不受用户级覆盖影响")
                return "\n".join(lines)
            lines.append(f"  当前生效：{_userperm_effective(uid, group_id)}")
            lines.append(f"  全局覆盖：{global_level or '无'}")
            if group_id:
                lines.append(f"  群 {group_id} 覆盖：{group_level or '无'}")
                role = perm.get_group_role(group_id, uid)
                lines.append(f"  该群角色：{role}（群主/管理员默认 admin）")
            lines.append(f"  可设置级别：{_userperm_levels()}（不含 superuser）")
            return "\n".join(lines)

        def _save_user_levels() -> bool:
            if self._cfg_ref is None:
                return False
            data = self._perm_manager.export_user_levels()
            if data:
                self._cfg_ref.set("user_permissions", data)
            else:
                self._cfg_ref.delete("user_permissions")
            return True

        def _userperm_set(uid: int, level: str, group_id: int) -> str:
            perm = self._perm_manager
            if perm.is_superuser(uid):
                return (f"QQ {uid} 是超管，无需也不允许用用户级覆盖"
                        "（超管只能在 config.json 的 superusers 里配）")
            level = level.strip().lower()
            if level not in ASSIGNABLE_USER_LEVELS:
                return (f"无效的级别：{level or '（空）'}\n"
                        f"可用级别：{_userperm_levels()}"
                        "（superuser 只能用 config.json 的 superusers）")
            before = _userperm_effective(uid, group_id)
            perm.set_user_level(uid, level, group_id=group_id or None)
            saved = _save_user_levels()
            tail = "" if saved else "（⚠️ 未持久化：配置未初始化，重启即失效）"
            _cmd_logger().info("用户权限覆盖：QQ %s → %s（%s）",
                               uid, level, _userperm_where(group_id))
            return (f"✅ 已将 QQ {uid} 在{_userperm_where(group_id)}的权限设为 "
                    f"{level}{tail}\n"
                    f"  原来：{before}\n  现在：{_userperm_effective(uid, group_id)}")

        def _userperm_clear(uid: int, group_id: int) -> str:
            perm = self._perm_manager
            existed = perm.clear_user_level(uid, group_id=group_id or None)
            if not existed:
                return (f"QQ {uid} 在{_userperm_where(group_id)}没有覆盖"
                        "（本来就是按群角色/默认值算）")
            saved = _save_user_levels()
            tail = "" if saved else "（⚠️ 未持久化：配置未初始化）"
            _cmd_logger().info("用户权限覆盖：清除 QQ %s（%s）",
                               uid, _userperm_where(group_id))
            return (f"✅ 已清除 QQ {uid} 在{_userperm_where(group_id)}的覆盖{tail}\n"
                    f"  现在：{_userperm_effective(uid, group_id)}")

        def _sub_userperm_list(packet: CommandPacket, group: int = 0):
            if self._perm_manager is None:
                return "权限管理器未初始化"
            return _userperm_list(group or None)

        def _sub_userperm_get(packet: CommandPacket, user: int, group: int = 0):
            if self._perm_manager is None:
                return "权限管理器未初始化"
            return _userperm_get(user, group or 0)

        def _sub_userperm_set(packet: CommandPacket, user: int, level: str,
                              group: int = 0):
            if self._perm_manager is None:
                return "权限管理器未初始化"
            return _userperm_set(user, level, group or 0)

        def _sub_userperm_clear(packet: CommandPacket, user: int, group: int = 0):
            if self._perm_manager is None:
                return "权限管理器未初始化"
            return _userperm_clear(user, group or 0)

        # ---- /alias：命令别名（superuser · 私聊）----
        #
        # 别名不是「另一条命令」：它在 `_commands` 里指向同一个 handler，
        # 所以权限/schema/作用域/归属标签天然共用，卸载时也一起回收。
        # 配置（config.json 的 `command_aliases`）一旦出现该命令，就是**全集**：
        # 空列表表示清空，连插件 register(aliases=…) 里声明的也会被压掉。

        def _alias_list(target: str = "") -> str:
            if target:
                resolved = self.find_command(target)
                if resolved is None:
                    configured = self._alias_overrides.get(target)
                    if configured is None:
                        return f"未知命令: /{target}"
                    return (f"/{target} 未注册，但配置里有别名："
                            + "、".join(f"/{a}" for a in configured)
                            + "（用 /alias set 重设，或 /alias del 逐个清）")
                canonical, handler = resolved
                aliases = list(handler.aliases or [])
                if not aliases:
                    return f"/{canonical} 没有别名"
                return f"/{canonical} 的别名：" + "、".join(f"/{a}" for a in aliases)

            rows: list[str] = []
            for canonical, handler in self.iter_handlers():
                aliases = list(handler.aliases or [])
                if aliases:
                    rows.append(f"  /{canonical} → " + "、".join(f"/{a}" for a in aliases))
            leftover = [name for name in self._alias_overrides
                        if self._commands.get(name) is None]
            if not rows and not leftover:
                return "当前没有命令别名"
            lines = [f"🔗 命令别名（{len(rows)} 条命令）"]
            lines += rows
            if leftover:
                lines.append("  ⚠️ 配置里有未注册命令的别名（插件卸载/缺失，重装后自动生效）：")
                for name in leftover:
                    lines.append(f"    /{name} → "
                                 + "、".join(f"/{a}" for a in self._alias_overrides[name]))
            lines.append("\n💡 /alias add <别名> <命令> · del <别名> · "
                         "set <命令> [别名…] · move <别名> <命令>")
            return "\n".join(lines)

        def _sub_alias_list(packet: CommandPacket, command: str = None):
            return _alias_list((command or "").lstrip("/"))

        def _sub_alias_add(packet: CommandPacket, alias: str, command: str):
            return self.add_alias(alias, command)[1]

        def _sub_alias_del(packet: CommandPacket, alias: str):
            return self.remove_alias(alias)[1]

        def _sub_alias_set(packet: CommandPacket, command: str,
                           aliases: list[str] = None):
            names = [part.strip() for arg in (aliases or [])
                     for part in arg.split(",") if part.strip()]
            return self.set_aliases(command, names)[1]

        def _sub_alias_move(packet: CommandPacket, alias: str, command: str):
            return self.move_alias(alias, command)[1]

        def _register_group(parent: str, description: str,
                            subs: list[tuple[str, Callable, str, type]]) -> None:
            """把系统命令注册成**真正的命令组**（子命令各有帮助/权限/作用域）。

            这些命令（/perm、/stop、/pause、/userperm、/alias）第一个位置参数
            本来就是「动作」，但以前注册成「无子命令的命令」：`/help perm` 只有一行
            usage、`/help perm get` 查不到、裸调父命令也不会走命令组帮助页。
            现在每个动作是独立子命令；**带参数的子命令声明 Schema**，帮助里的
            参数表由 Schema 生成，缺参时框架自动重定向到
            `/help <父> <子命令>`。
            """
            def _parent_body(packet: CommandPacket = None):
                verbs = "|".join(child for child, _f, _d, _s in subs)
                return (f"用法：/{parent} <{verbs}> …\n"
                        f"发 /help {parent} 查看各子命令的用法与权限")

            head = CommandHandler(parent, _parent_body, description=description,
                                  scope="private", permission="superuser",
                                  permission_manager=self._perm_manager)
            for child, func, desc, schema in subs:
                # 参数文档一律由 Schema 生成；有参数的子命令缺参时框架自动
                # 「⚠️ 用法不正确 + /help <父> <子命令>」，不再手写 usage 兜底。
                sub = CommandHandler(child, func, description=desc, schema=schema,
                                     scope="private", permission="superuser",
                                     permission_manager=self._perm_manager)
                sub.parent_name = parent
                # 子命令也可以被 /perm 单独覆盖
                self._apply_perm_override(f"{parent}.{child}", sub)
                head.subcommands[child] = sub
            self._install_handler(parent, head)

        # superuser 专属（绑定管理 + 重载）
        for cmd_name, func, desc in [
            ("bind", _cmd_bind, "绑定群"),
            ("unbind", _cmd_unbind, "解绑群"),
            ("groups", _cmd_groups, "查看已绑定群"),
            ("reload", _cmd_reload, "重载插件（无参=全部, 有参=指定）"),
            ("events", _cmd_events, "查看已注册事件监听器"),
            ("commands", _cmd_commands, "查看已注册命令"),
            ("timers", _cmd_timers, "查看已注册定时任务"),
        ]:
            self._install_handler(cmd_name, CommandHandler(
                cmd_name, func, description=desc, scope="private",
                permission="superuser",
                permission_manager=self._perm_manager))

        # ---- 系统命令组：动作是子命令，参数文档来自 Schema ----
        _register_group("stop", "移除监听器/命令/定时任务", [
            ("event", _cmd_stop_event, "移除事件监听器（按 hid）", _EventTargetSchema),
            ("command", _cmd_stop_command, "移除命令（含别名）", _CommandTargetSchema),
            ("timer", _cmd_stop_timer, "取消定时任务", _TimerTargetSchema),
        ])
        _register_group("pause", "暂停/恢复监听器/命令/定时任务", [
            ("event", _cmd_pause_event, "暂停/恢复事件监听器", _EventTargetSchema),
            ("command", _cmd_pause_command, "启用/禁用命令", _CommandTargetSchema),
            ("timer", _cmd_pause_timer, "暂停/恢复定时任务", _TimerTargetSchema),
        ])
        _register_group("perm", "查看/设置命令权限覆盖", [
            ("list", _sub_perm_list, "列出全部权限覆盖", None),
            ("get", _sub_perm_get, "查看某条命令的权限", _PermTargetSchema),
            ("set", _sub_perm_set, "设置覆盖", _PermSetSchema),
            ("reset", _sub_perm_reset, "恢复声明值 / 清空全部", _PermResetSchema),
        ])
        _register_group("userperm", "查看/设置用户权限（全局或指定群）", [
            ("list", _sub_userperm_list, "列出用户级覆盖（可指定群）",
             _UserpermScopeSchema),
            ("get", _sub_userperm_get, "查看某人当前生效级别与来源",
             _UserpermGetSchema),
            ("set", _sub_userperm_set, "设置覆盖（不给群号=全局）",
             _UserpermSetSchema),
            ("clear", _sub_userperm_clear, "清除覆盖", _UserpermClearSchema),
        ])
        _register_group("alias", "查看/增删改命令别名", [
            ("list", _sub_alias_list, "列出全部别名（可指定命令）", _AliasListSchema),
            ("add", _sub_alias_add, "新增别名", _AliasAddSchema),
            ("del", _sub_alias_del, "删除别名", _AliasDelSchema),
            ("set", _sub_alias_set, "整体替换（不给别名=清空）", _AliasSetSchema),
            ("move", _sub_alias_move, "把别名改指向另一条命令", _AliasMoveSchema),
        ])

        # 所有人可见（查询/测试）
        for cmd_name, func, desc in [
            ("ping", _cmd_ping, "延迟测试"),
            ("plugins", _cmd_plugins, "查看已加载插件"),
        ]:
            self._install_handler(cmd_name, CommandHandler(
                cmd_name, func, description=desc, scope="private",
                permission_manager=self._perm_manager))

    def register(self, name: str, *, schema: Type[CommandSchema] = None,
                 subcommands: dict = None, aliases: list[str] = None,
                 description: str = "", usage: str = "", permission: str = None,
                 scope: str = None, plugin: str = "") -> Callable:
        if name.count(".") > 1:
            # 子命令只支持一级（`父.子`）。旧实现 `parts[0], parts[1]` 会把
            # "a.b.c" 静默截断成 "a.b"：注册"成功"但名字不是你要的，按名解析的
            # 入口（/help、/perm）也就无从对准。宁可加载期显式报错。
            raise ValueError(
                f"命令名 {name!r} 层级超过两级：子命令只支持 `父.子` 形式")
        for _part in name.split("."):
            if not valid_command_name(_part, self.parser.max_cmd_len):
                # 注册进来却解析不到 = 命令永远收不到消息（也无从 /help 查到）。
                raise ValueError(
                    f"命令名 {name!r} 非法（{_part!r}）：字母开头（支持中文），"
                    "只能含字母/数字/下划线/连字符")
        # permission/scope 用 None 表示"未指定"，这样子命令才能按文档所述
        # 「继承父命令的 scope 和 permission 作为默认值」；显式传入（含空串 /
        # "all"）则视为对该子命令的覆盖。
        def decorator(func: Callable) -> Callable:
            if "." in name:
                parts = name.split(".")
                parent, child = parts[0], parts[1]
                if parent in self._commands:
                    parent_h = self._commands[parent]
                    h = CommandHandler(
                        child, func, schema=schema, usage=usage,
                        permission=permission if permission is not None else parent_h.permission,
                        scope=scope if scope is not None else parent_h.scope,
                        permission_manager=self._perm_manager, description=description,
                        plugin=plugin)
                    h.parent_name = parent
                    self._commands[parent].subcommands[child] = h
                    self._apply_perm_override(f"{parent}.{child}", h)
                else:
                    # 占位本体：子命令先注册、父体还没注册时用它顶着。
                    # 裸调父命令（`/aichat`）会走到这里 —— 给出子命令清单，
                    # 比返回 None（用户看到的是一片空白）有用得多。
                    # 签名用 **kwargs 兜住框架的参数名注入（event / api / packet），
                    # 免得任何一条路径把 TypeError 吞成「命令没反应」。
                    h = CommandHandler(parent, _parent_help(parent, self),
                                       permission_manager=self._perm_manager,
                                       plugin=plugin)
                    sub = CommandHandler(
                        child, func, schema=schema, usage=usage,
                        permission=permission if permission is not None else "",
                        scope=scope if scope is not None else "all",
                        permission_manager=self._perm_manager, description=description,
                        plugin=plugin)
                    sub.parent_name = parent
                    h.subcommands[child] = sub
                    self._apply_perm_override(f"{parent}.{child}", sub)
                    self._install_handler(parent, h)
            else:
                handler = CommandHandler(
                    name, func, schema=schema, subcommands=subcommands,
                    aliases=aliases, description=description, usage=usage,
                    permission=permission if permission is not None else "",
                    scope=scope if scope is not None else "all",
                    permission_manager=self._perm_manager, plugin=plugin)
                # 若已有 dummy（子命令先于父命令注册），继承其子命令
                existing = self._commands.get(name)
                if existing and existing.subcommands:
                    handler.subcommands.update(existing.subcommands)
                # 别名由 `_install_handler` → `_apply_alias_override` 统一登记：
                # 配置（command_aliases）存在时以配置为全集，所以这里**不能**再
                # 直接把声明值塞进 _commands（会把「已清空」的别名又加回来）。
                self._install_handler(name, handler)
            return func
        return decorator

    def set_error_reporter(self, reporter: "ErrorReporter") -> None:
        """注入错误反馈业主（客户端启动时调用）。"""
        self._error_reporter = reporter

    def unregister_plugin(self, plugin: str) -> int:
        """注销某插件注册过的全部命令（含子命令与别名），返回条数。

        这是插件卸载/加载失败时回收命令的**唯一**入口：归属来自
        `CommandHandler.plugin`（注册时打的标签），不是「拿插件名猜命令名前缀」
        —— `essential` 注册的是 `/ban`、`/kick`，前缀猜法一个也回收不了。
        """
        if not plugin:
            return 0
        removed = 0
        for name in list(self._commands):
            handler = self._commands[name]
            if handler.plugin == plugin:
                self._commands.pop(name, None)
                removed += 1
                continue
            for sub_name in list(handler.subcommands):
                if handler.subcommands[sub_name].plugin == plugin:
                    handler.subcommands.pop(sub_name, None)
                    removed += 1
        return removed

    def on_illegal(self, func: Callable):
        self._on_illegal = func
        return func

    def on_denied(self, func: Callable):
        self._on_denied = func
        return func

    def add_visibility_filter(self, func: Callable) -> Callable:
        """注册「命令可见性」过滤器 —— 签名 `(handler, event) -> bool`（可为协程）。

        `/help` 在列举与详情两处都会问它。存在的意义：框架自带的可见性判据是
        「scope 匹配 + permission 通过」，而插件可能还有**别的**可用性条件
        （如「本群是否开通了这项服务」）。没有这个钩子，插件只能自己再注册一个
        help 命令，于是同一份命令清单有两条渲染路径、两套口径，迟早不一致。

        过滤器只应回答「这条命令对当前调用者是否可用」；返回 False 的命令在
        `/help` 里按「不存在」处理（与 `handle()` 的隐藏口径一致）。
        """
        self._visibility_filters.append(func)
        return func

    def remove_visibility_filter(self, func: Callable) -> bool:
        try:
            self._visibility_filters.remove(func)
            return True
        except ValueError:
            return False

    async def is_visible(self, handler: "CommandHandler", event) -> bool:
        """所有过滤器一致通过才可见（任一否决即隐藏）。"""
        for flt in list(self._visibility_filters):
            try:
                ok = flt(handler, event)
                if inspect.isawaitable(ok):
                    ok = await ok
            except Exception:  # noqa: BLE001 - 过滤器自身出错不该炸掉 /help
                ok = False
            if not ok:
                return False
        return True

    async def command_allowed(self, handler: "CommandHandler", event) -> bool:
        """命令对当前调用者是否「可见且可用」—— `/help`、执行、帮助重定向共用同一判据。

        判据 = `scope` 匹配 + 插件可见性过滤器 + 权限校验。命令组本体不设权限
        （与 `handle()` 同口径：父命令上的 `permission` 只是子命令的默认值），
        所以带子命令的 handler 到这里直接放行，由各子命令分别判定。
        """
        is_group = bool(event) and event.message_type == "group"
        if not handler.allows_context(is_group):
            return False
        if not await self.is_visible(handler, event):
            return False
        if handler.subcommands:
            # 命令组本体不校验权限 —— 但「一个子命令都用不了」时整组应当不可见，
            # 否则超管专属组（/perm、/alias …）会出现在普通用户的 /help 里
            # （以前它们是单命令、有 permission，列表里天然被过滤掉）。
            for _name, sub in handler.subcommands.items():
                if await self.command_allowed(sub, event):
                    return True
            return False
        return await handler.check_permission(
            getattr(event, "user_id", 0) if event else 0,
            getattr(event, "group_id", None), is_group=is_group)

    async def help_page(self, handler: "CommandHandler", event) -> str:
        """单条命令的帮助页 —— `/help <命令>`、裸调命令组、用法错误重定向共用同一渲染。

        子命令清单按调用者过滤（列出来的都是能用的）；群里补一句「需先 @机器人」。
        """
        sub_visible: set[int] = set()
        for _name, sub in handler.subcommands.items():
            if await self.command_allowed(sub, event):
                sub_visible.add(id(sub))
        text = handler.render_help(visible=lambda h: id(h) in sub_visible)
        is_group = bool(event) and event.message_type == "group"
        # 只在「这条命令在群里本来就能用」时提示 @（仅私聊的命令已有范围说明）
        if is_group and handler.allows_context(True):
            text += "\n\n" + GROUP_AT_HINT
        return text

    async def handle(self, event: "MessageEvent") -> Optional[Any]:
        from .models import MessageEvent
        packet = self.parser.parse(event.plain_text)
        if packet is None:
            return None
        packet.event = event
        setattr(event, "_cmd_packet", packet)

        if packet.illegal:
            if self._on_illegal:
                result = self._on_illegal(packet, event)
                if inspect.isawaitable(result):
                    await result
                return None
            # 静默丢弃的两种非法输入（`//` 结束符、`#include<...>` 这类以命令前缀
            # 开头的正文）保持静默 —— 判据是**类型**（`illegal_kind`），不是文案。
            # 但「参数写到 21 个」的用户是真的在打命令：静默就是「有问不答」。
            if packet.illegal_kind == "too_many_args":
                return (f"⚠️ 参数过多：最多支持 {self.parser.max_args} 个位置参数"
                        f"（收到 {len(packet.args)} 个）。发 /help <命令> 看用法。")
            return None

        handler = self._commands.get(packet.command)
        if not handler:
            return ("_not_found", packet.command)

        if handler.disabled:
            return ("_not_found", packet.command)

        # 解析"实际会执行"的 handler：子命令优先。
        # 此前只校验父命令的 scope/permission，CommandHandler.execute() 路由到
        # 子命令后不再校验，导致子命令上声明的 permission/scope 完全不生效
        # （权限绕过：父命令设为 admin 时，群管理员可执行标着 superuser 的子命令）。
        is_group = event.message_type == "group"
        effective = handler
        if handler.subcommands:
            # 带子命令的命令是**命名空间**。
            # ⚠️ 两种输入必须区分开：
            #   `/x`        —— 裸调父命令 → 重定向到 `/help x`（见下）；
            #   `/x nosuch` —— 子命令名写错 = 用法不正确 → 同样回 `/help x`
            #                 （那里列着可用的子命令名，正是写错的人需要的）。
            # 曾经第二种会回落到父命令本体：若父体是注册子命令时自建的**占位**
            # handler，它的 `lambda p: None` 与框架注入的实参不匹配 → TypeError
            # → 被吞成「没反应」；就算签名对得上，也会执行一个与输入无关的动作。
            if packet.subcommand and packet.subcommand not in handler.subcommands:
                # 组本身在该上下文用不了（范围不符/插件过滤器否决）时仍按「未知命令」
                # 处理，不把一个用不了的命名空间泄露出来。
                if not handler.allows_context(is_group) \
                        or not await self.is_visible(handler, event):
                    return ("_not_found", packet.command)
                return await self.help_page(handler, event)
        if packet.subcommand and packet.subcommand in handler.subcommands:
            effective = handler.subcommands[packet.subcommand]

        # scope 检查：越界访问返回 _not_found（与命令不存在的回应一致）
        if not effective.allows_context(is_group):
            return ("_not_found", packet.command)

        # permission 检查：不满足时返回 _not_found（隐藏命令存在）
        # 例外：带子命令的命令是「命令组 / 命名空间」，本体不设权限 —— 权限属于
        # 各子命令；执行到子命令时上面已把 effective 解析为该子命令并按其声明校验。
        is_namespace_body = bool(effective.subcommands) and effective is handler
        if not is_namespace_body and not await effective.check_permission(
                event.user_id, event.group_id, is_group=is_group):
            return ("_not_found", packet.command)

        # 裸调命令组（`/a`）→ 重定向到 `/help a`：命令组没有「本体动作」，
        # 直接把它的帮助页回给用户（插件手写的用法行、框架占位 handler 的名字
        # 清单都不如这一页完整：有本体描述 + 你有权使用的子命令）。
        if is_namespace_body:
            if not await self.is_visible(handler, event):
                return None     # 用不了：静默（与插件服务范围守卫的口径一致）
            # 一个子命令都用不了 → 与「没权限」同一口径：**不暴露命令组的存在**。
            # 否则超管专属组（/perm、/alias、/stop …）会对普通用户露出
            # 「📖 /perm … （没有你可用的子命令）」，等于告诉他机器上有这个命令。
            usable = False
            for _name, sub in effective.subcommands.items():
                if await self.command_allowed(sub, event):
                    usable = True
                    break
            if not usable:
                return ("_not_found", packet.command)
            return await self.help_page(handler, event)

        try:
            result = await handler.execute(packet)
        except NapCatError as e:
            # 插件自报的失败（超时/限流/配置/无权…）与框架包出来的运行错误：
            # 都交给同一条反馈出口，用户拿到「分类 + 短原因 + 编号」。
            return await self.report_failure(e, effective, event)
        except Exception as e:  # noqa: BLE001 - 兜底：任何失败都不许变成「没反应」
            return await self.report_failure(
                PluginRuntimeError(short_reason(e), plugin=effective.plugin, cause=e),
                effective, event)
        # 用法不正确（Schema 校验失败）→ 重定向到**对应**命令的帮助页：
        # 只说「参数不对」用户还是不知道该怎么写，帮助页里有用法行、参数表与默认值。
        if isinstance(result, tuple) and len(result) == 2 and result[0] == "_schema_error":
            return (f"⚠️ 用法不正确：{str(result[1]).strip()}\n\n"
                    + await self.help_page(effective, event))
        return result

    async def report_failure(self, exc: BaseException, handler: "CommandHandler",
                             event=None) -> str:
        """命令失败的统一出口：记完整日志 + 给触发者一句话，**永远不返回 None**。

        「命令没反应」是这套系统历史上最贵的症状：用户分不清是自己打错了、
        命令不存在、还是内部炸了，于是反复重试或者干脆不用了。所以这里保证
        任何失败路径都有一句带编号的回应（超管侧另有细节，见 ErrorReporter）。
        """
        reporter = self._error_reporter
        what = "执行 /%s%s 时" % (
            (handler.parent_name + " ") if handler.parent_name else "", handler.name)
        if reporter is None:
            reporter = ErrorReporter()
        text = await reporter.report(exc, event=event, plugin=handler.plugin, what=what)
        if text is None and reporter.enabled:
            # 有反馈意愿但没有投递通道（独立使用 CommandManager，如测试/脚本）：
            # 至少把渲染好的句子交回调用方，不静默。
            return reporter.render(exc, plugin=handler.plugin, what=what)
        return text

    def unregister(self, name: str) -> bool:
        """注销命令（含它名下的别名条目）。"""
        handler = self._commands.pop(name, None)
        if handler is None:
            return False
        self._drop_alias_entries(handler)
        return True

    def list_commands(self) -> dict[str, CommandHandler]:
        return dict(self._commands)
