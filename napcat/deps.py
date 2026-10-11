"""插件依赖声明与解析（必需依赖 + 可选依赖）。

插件用两个类属性声明依赖（都是 `PluginBase` 的类属性，不需要写 `__init__`）：

```python
class ReportPlugin(PluginBase):
    name = "report"
    version = "1.0.0"
    requires = ["store"]                       # 必需：没有它本插件不加载
    optional_requires = ["ai_chat"]            # 可选：有就用，没有也照常跑

class StorePlugin(PluginBase):
    name = "store"
    version = "1.2.0"
    provides = ["store"]                       # 可选：显式声明自己对外提供的名字
```

几个约定（都有理由，不是随手定的）：

- **必需依赖没装/没启用 → 本插件不加载**，并把原因私聊超管（`audience="operator"`）。
  半死不活地加载（命令能调、依赖是 None）比干脆不加载难查得多；
- **可选依赖缺失永远不算失败**：`self.deps.get("ai_chat")` 拿到 `None`，
  插件自己决定降级行为。写进列表只是为了「装/卸时能提示」；
- **依赖可以带版本要求**：`requires = ["store>=1.2"]`（`>=` / `>` / `<=` / `<` /
  `==` / `!=`，版本号按数字段比较，`1.10 > 1.9`）。版本号取的是插件自己声明的
  `spec = provided_spec(instance)` —— 没声明 `provides`/`optional_requires` 的
  插件，用同名字段兜底：装它的插件时写的 `optional_requires = {"store": ">=1.2"}`
  只作**提示**用，不会因为版本不符就拒绝加载（可选依赖不该造成硬失败）；
- **不能依赖自己**，也不能有环（`a → b → a`）—— 两种都在加载期明确报错，
  而不是留到运行期变成「命令莫名其妙不响应」；
- **`name` 只允许 `[A-Za-z_][A-Za-z0-9_]*`**：它要能与变量名、命令名一起出现在
  报错文案里，含奇怪字符的名字（空格、`/`）一律拒绝。

这个模块只负责**纯计算**（解析声明、检查满足情况、拓扑排序），不认识文件系统、
不加载模块 —— 加载顺序与生命周期由 `PluginManager` 负责。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional

#: 合法的插件名（同时也是解析后的依赖名判据）
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
#: `名字` 或 `名字<op>版本`，例如 `store`、`store>=1.2`、`ai_chat==1.0.0`
SPEC_RE = re.compile(
    r"^(?P<name>[A-Za-z_][A-Za-z0-9_]{0,63})\s*"
    r"(?:(?P<op>>=|<=|==|!=|>|<)\s*(?P<version>[0-9A-Za-z_.\-+]{0,32}))?$")
#: 版本号里的数字段（用于比较：1.10 > 1.9）
_NUM_RE = re.compile(r"\d+")


class DependencyError(Exception):
    """依赖声明本身有问题（名字非法、依赖自己、成环、字段类型不对）。

    加载期就会被发现并报给超管；**不是**运行期错误。
    """


@dataclass(frozen=True)
class RequirementSpec:
    """一条依赖声明（`"store>=1.2"` → name=store, op=">=", version="1.2"）。"""

    name: str
    op: str = ""
    version: str = ""
    optional: bool = False

    def __str__(self) -> str:                     # 文案里直接用
        return f"{self.name}{self.op}{self.version}" if self.op else self.name

    @property
    def has_version_requirement(self) -> bool:
        return bool(self.op and self.version)


def parse_spec(raw: Any, *, optional: bool = False) -> RequirementSpec:
    """把一条声明解析成 `RequirementSpec`，非法就抛 `DependencyError`。"""
    if isinstance(raw, RequirementSpec):
        return raw
    if isinstance(raw, dict):
        # `{"name": "store", "version": ">=1.2", "optional": True}` 这种写法
        name = str(raw.get("name") or raw.get("plugin") or "").strip()
        op_version = str(raw.get("version") or "").strip()
        optional = bool(raw.get("optional", optional))
        raw = f"{name}{op_version}" if name else ""
    if not isinstance(raw, str):
        raise DependencyError(f"依赖声明必须是字符串，收到 {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise DependencyError("依赖声明是空字符串")
    found = SPEC_RE.match(text)
    if not found:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*\s*[<>=!]", text):
            raise DependencyError(
                f"依赖声明 {text!r} 的版本号不合法（只允许字母数字与 _ . - +）")
        raise DependencyError(
            f"依赖声明 {text!r} 不合法（应为 插件名 或 插件名[<>=!]=版本，"
            "插件名只允许字母数字与下划线）")
    op = found.group("op") or ""
    version = (found.group("version") or "").strip()
    if op and not version:
        raise DependencyError(f"依赖声明 {text!r} 里 {op} 后面没有版本号")
    if version and not op:
        raise DependencyError(
            f"依赖声明 {text!r} 缺少比较符（写成 插件名>=版本）")
    return RequirementSpec(name=found.group("name"), op=op, version=version,
                           optional=optional)


def parse_specs(raw: Any, *, optional: bool = False) -> list[RequirementSpec]:
    """解析一组依赖声明（list / tuple / set / 单个字符串都收）。

    去重按**名字**（同一个插件写两遍只算一条），保留首次出现的版本要求。
    """
    if raw is None:
        return []
    if isinstance(raw, (str, dict, RequirementSpec)):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise DependencyError(
            f"依赖声明必须是列表（或单个字符串），收到 {type(raw).__name__}")
    out: list[RequirementSpec] = []
    index: dict[str, int] = {}
    for item in raw:
        spec = parse_spec(item, optional=optional)
        if spec.name in index:
            # 同名写了两遍：**带版本要求的那条赢**（与书写顺序无关）。
            # 顺手把版本要求放宽到「更严」的那条：`>=1.2` + `>=1.5` 取 >=1.5。
            kept = out[index[spec.name]]
            if spec.has_version_requirement and not kept.has_version_requirement:
                out[index[spec.name]] = spec
            elif spec.has_version_requirement and kept.has_version_requirement \
                    and spec.op == kept.op == ">=":
                if version_key(spec.version) > version_key(kept.version):
                    out[index[spec.name]] = spec
            continue
        index[spec.name] = len(out)
        out.append(spec)
    return out


def version_key(version: str) -> tuple:
    """版本号 → 可比较的元组（数字段按数值比，其余按字符串）。

    `"1.10" > "1.9"` 且 `"1.0.0" == "1.0"`：比纯字符串比较靠谱，
    又不需要引入 `packaging` 依赖。
    """
    text = (version or "").strip().lstrip("vV")
    parts: list[tuple[int, Any]] = []
    for chunk in re.split(r"[.\-+]", text):
        if chunk.isdigit():
            parts.append((1, int(chunk)))
        elif chunk:
            parts.append((0, chunk.lower()))
    # 去掉末尾的数字 0：1.0 == 1.0.0
    while parts and parts[-1] == (1, 0):
        parts.pop()
    return tuple(parts)


def version_matches(actual: str, op: str, wanted: str) -> bool:
    """`actual` 是否满足 `op wanted`（actual 为空 = 无法判断 → False）。"""
    if not op:
        return True
    if not actual:
        return False
    left, right = version_key(actual), version_key(wanted)
    if op == ">=":
        return left >= right
    if op == ">":
        return left > right
    if op == "<=":
        return left <= right
    if op == "<":
        return left < right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    return False


def dependency_names(plugin: Any) -> tuple[list[str], list[str]]:
    """读插件声明的依赖名：(必需, 可选)。

    取类属性，因此**构造前**（拿到类对象时）就能读到；`PluginBase` 的默认值
    是空列表，没声明依赖的插件什么都不用管。
    """
    required = _as_specs_or_empty(getattr(plugin, "requires", None), optional=False)
    optional = _as_specs_or_empty(getattr(plugin, "optional_requires", None),
                                  optional=True)
    optional_names = {spec.name for spec in optional}
    return ([str(s) for s in required if s.name not in optional_names],
            [str(s) for s in optional])


def _as_specs_or_empty(raw: Any, *, optional: bool) -> list[RequirementSpec]:
    """声明写坏了（类型不对）时抛 `DependencyError`；解析细节见 `parse_specs`。"""
    return parse_specs(raw, optional=optional)


def validate_declaration(owner: str, required: Iterable[Any],
                         optional: Iterable[Any] = ()) -> tuple[
                             list[RequirementSpec], list[RequirementSpec]]:
    """校验一份依赖声明，返回 (必需, 可选)。

    在这里一次性挡掉三类会在运行期变成「玄学故障」的写法：

    - 名字非法（含空格、`/`、以数字开头）—— 报错文案里要能直接指出是哪一条；
    - **依赖自己** —— `a` 依赖 `a` 永远无法满足，只会让人以为「加载器坏了」；
    - 同一个插件**同时**出现在必需与可选里 —— 语义矛盾，按必需算会在
      「可选缺失不该失败」和「必需缺失必须失败」之间反复横跳。
    """
    req = parse_specs(list(required), optional=False)
    opt = parse_specs(list(optional), optional=True)
    for spec in req:
        if spec.name == owner:
            raise DependencyError(f"{owner} 依赖了自己（requires 里有 {spec.name}）")
    for spec in opt:
        if spec.name == owner:
            raise DependencyError(
                f"{owner} 依赖了自己（optional_requires 里有 {spec.name}）")
    req_names = {s.name for s in req}
    for spec in opt:
        if spec.name in req_names:
            raise DependencyError(
                f"{owner} 的 {spec.name} 同时写在 requires 与 optional_requires 里"
                "（要么必需、要么可选）")
    # 同一插件既要求 >=1.2 又要求 >=1.5 时保留更严的那条：按第一次出现的算，
    # 其余的只在名字上去重（见 parse_specs），所以这里不再合并版本。
    return req, opt


@dataclass
class DependencyCheck:
    """一份依赖声明的满足情况（加载期与 `/plugins` 共用同一份判定）。"""

    owner: str
    missing: list[RequirementSpec] = field(default_factory=list)
    """必需依赖：根本没加载/不存在。"""
    disabled: list[RequirementSpec] = field(default_factory=list)
    """必需依赖：加载了但没启用（插件被禁用/启用失败）。"""
    version_mismatch: list[tuple[RequirementSpec, str]] = field(default_factory=list)
    """必需依赖：在，但版本不满足（附实际版本）。"""
    optional_missing: list[RequirementSpec] = field(default_factory=list)
    """可选依赖：不在（**不算失败**，只是提示）。"""
    satisfied: list[RequirementSpec] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """必需依赖是否全部满足。"""
        return not (self.missing or self.disabled or self.version_mismatch)

    def reason(self) -> str:
        """一句话说明为什么不行（空串 = 都满足）。"""
        if self.ok:
            return ""
        parts = []
        if self.missing:
            parts.append("缺少 " + "、".join(str(s) for s in self.missing))
        if self.disabled:
            parts.append("未启用 " + "、".join(str(s) for s in self.disabled))
        for spec, actual in self.version_mismatch:
            parts.append(f"{spec.name} 版本不满足（需要 {spec.op}{spec.version}，"
                         f"实际 {actual or '未知'}）")
        return "；".join(parts)

    def hint(self) -> str:
        """给超管的处理建议（与 `reason()` 分开：这里说的是「怎么办」）。"""
        names = [s.name for s in self.missing] + [s.name for s in self.disabled]
        if not names:
            return ""
        return ("装/启用它们后本插件会自动加载："
                + "、".join(f"/plugin install {n}" for n in names)
                + "（或写好文件等热重载）")


def check_dependencies(required: Iterable[RequirementSpec],
                       optional: Iterable[RequirementSpec] = (),
                       *,
                       resolve: Callable[[str], Any],
                       version_of: Optional[Callable[[Any], str]] = None,
                       owner: str = "",
                       check_enabled: bool = True) -> DependencyCheck:
    """逐个检查依赖是否满足。`resolve(name)` 返回插件实例或 None。

    `version_of(instance)` 取该插件对外声明的版本；缺省按 `version` 属性取。
    必需依赖的版本要求不满足 → 记进 `version_mismatch`（= 加载失败）；
    可选依赖只检查「在不在」，版本不符只进 `optional_missing`（= 提示）。

    ⚠️ `check_enabled` 要按**时机**选：加载期（`load_all()` 里大家都没启用）
    必须传 False，否则「依赖已加载、只是还没启用」会被判成不满足，出现
    「依赖后加载的插件永远进不来」；启用期才要求依赖确实启用着。
    判「被运维显式禁用」靠 resolve() 返回一个 `enabled=False` 的包装对象，
    与这里的开关无关。
    """
    get_version = version_of or (lambda inst: str(
        getattr(inst, "version", "") or ""))
    result = DependencyCheck(owner=owner)
    for spec in required:
        instance = resolve(spec.name)
        if instance is None:
            result.missing.append(spec)
            continue
        if check_enabled and not getattr(instance, "enabled", True):
            result.disabled.append(spec)
            continue
        if spec.has_version_requirement and not version_matches(
                get_version(instance), spec.op, spec.version):
            result.version_mismatch.append((spec, get_version(instance)))
            continue
        result.satisfied.append(spec)
    for spec in optional:
        if resolve(spec.name) is None:
            result.optional_missing.append(spec)
        else:
            result.satisfied.append(spec)
    return result


# ==================== 拓扑排序 ====================


def _edges(owner: str, required: Iterable[str],
           optional: Iterable[str] = ()) -> list[tuple[str, str]]:
    """(依赖 → 使用者) 的边；可选依赖**也参与排序**（有就先加载，没有不阻塞）。"""
    edges: dict[str, set[str]] = {}
    for name in required:
        if name != owner:
            edges.setdefault(name, set()).add(owner)
    for name in optional:
        if name != owner:
            edges.setdefault(name, set()).add(owner)
    return edges


def graph(plugins: Iterable[str],
          deps_of: Callable[[str], tuple[Iterable[str], Iterable[str]]]
          ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """构建依赖图，返回 (requires 边, optional 边)，键是「依赖」，值是使用者集合。

    `deps_of(name)` 返回该插件的 (必需依赖名, 可选依赖名)；不在 `plugins`
    里的依赖名**不出现在图里**（缺依赖由 `check_dependencies` 负责报告，
    排序时忽略即可）。
    """
    known = list(plugins)
    known_set = set(known)
    requires_edges: dict[str, set[str]] = {}
    optional_edges: dict[str, set[str]] = {}
    for name in known:
        required, optional = deps_of(name)
        for dep in required:
            if dep in known_set and dep != name:
                requires_edges.setdefault(dep, set()).add(name)
        for dep in optional:
            if dep in known_set and dep != name:
                optional_edges.setdefault(dep, set()).add(name)
    return requires_edges, optional_edges


def _build_edges(requires_edges: Mapping[str, set[str]],
                 optional_edges: Mapping[str, set[str]],
                 nodes: set[str]) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {k: set(v) for k, v in requires_edges.items()}
    for dep, users in optional_edges.items():
        edges.setdefault(dep, set()).update(users)
    for dep in list(edges):
        if dep not in nodes:
            edges.pop(dep)
            continue
        edges[dep] = {u for u in edges[dep] if u in nodes}
    for node in nodes:
        edges.setdefault(node, set())
    return edges


def _topo(requires_edges: Mapping[str, set[str]],
          optional_edges: Mapping[str, set[str]],
          known_set: set[str], *, reverse: bool) -> list[str]:
    """Kahn 拓扑排序，字典序稳定；`reverse=True` 时输出「使用者在前」。

    - **必需依赖成环 → 抛 `DependencyError`**（这种环永远解不开，必须让人去改声明）；
    - **可选依赖成环 → 摘掉一条可选边**（可选依赖本来就不是硬约束，
      为它让整批插件加载不了，是把软约束当硬约束用）；
    - 缺失的依赖（不在 `known_set` 里）不参与排序，由 `check_dependencies` 报告。
    """
    nodes = set(known_set)
    edges = _build_edges(requires_edges, optional_edges, nodes)

    def indegree_of(edge_map: Mapping[str, set[str]]) -> dict[str, int]:
        # 入度：使用者 ← 依赖它的那些边（依赖先出）
        degree = {node: 0 for node in nodes}
        for dep, users in edge_map.items():
            for user in users:
                degree[user] = degree.get(user, 0) + 1
        return degree

    # 环检测只看必需依赖（可选边可以由使用者摘掉）
    if _has_cycle(requires_edges, nodes):
        raise DependencyError("插件必需依赖成环："
                              + _describe_cycle(requires_edges, nodes))

    # 可选边成环：一条条摘掉（只摘可选边，必需边不能动）
    guard = 0
    while _has_cycle(edges, nodes):
        guard += 1
        if guard > len(nodes) + 5:
            raise DependencyError("依赖图无法排序（可能是隐性环）："
                                  + "、".join(sorted(nodes)))
        broken = False
        for dep in sorted(edges):
            for user in sorted(edges[dep]):
                is_required = user in requires_edges.get(dep, ())
                if not is_required:
                    edges[dep].discard(user)
                    broken = True
                    break
            if broken:
                break
        if not broken:
            raise DependencyError("依赖图无法排序：" + "、".join(sorted(nodes)))

    indegree = indegree_of(edges)
    ready = sorted(node for node, deg in indegree.items() if deg == 0)
    order: list[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for user in sorted(edges.get(node, ())):
            indegree[user] -= 1
            if indegree[user] == 0:
                ready.append(user)
        ready.sort()
    if len(order) != len(nodes):
        # 理论上到不了（环已挡掉），留个兜底别让剩余节点悄悄消失
        remaining = sorted(nodes - set(order))
        raise DependencyError("依赖图无法排序（可能成环）：" + "、".join(remaining))
    if reverse:
        return list(reversed(order))
    return order


def _has_cycle(edges: Mapping[str, set[str]], nodes: set[str]) -> bool:
    """有向图是否有环（DFS 三色标记）。`edges[dep] = {使用者}`。"""
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {node: WHITE for node in nodes}
    # 反向索引：使用者 → 依赖（按「依赖先出」的方向走）
    reverse: dict[str, set[str]] = {node: set() for node in nodes}
    for dep, users in edges.items():
        if dep not in nodes:
            continue
        for user in users:
            if user in nodes:
                reverse[user].add(dep)

    def visit(node: str) -> bool:
        color[node] = GREY
        for nxt in sorted(reverse.get(node, ())):
            if color.get(nxt) == GREY:
                return True
            if color.get(nxt) == WHITE and visit(nxt):
                return True
        color[node] = BLACK
        return False

    return any(color[node] == WHITE and visit(node) for node in sorted(nodes))


def _describe_cycle(edges: Mapping[str, set[str]], nodes: set[str]) -> str:
    """找一条具体的环，用在报错里（只说「成环了」没法排查）。"""
    reverse: dict[str, set[str]] = {node: set() for node in nodes}
    for dep, users in edges.items():
        if dep not in nodes:
            continue
        for user in users:
            if user in nodes:
                reverse[user].add(dep)
    path: list[str] = []
    seen: set[str] = set()

    def walk(node: str) -> bool:
        if node in path:
            start = path.index(node)
            path.append(node)
            del path[:start]
            return True
        if node in seen:
            return False
        seen.add(node)
        path.append(node)
        for nxt in sorted(reverse.get(node, ())):
            if walk(nxt):
                return True
        path.pop()
        return False

    for node in sorted(nodes):
        if walk(node):
            return " → ".join(reversed(path))
    return "（无法定位具体环路）"


def load_order(plugins: Iterable[str],
               deps_of: Callable[[str], tuple[Iterable[str], Iterable[str]]]
               ) -> list[str]:
    """加载顺序：依赖在前（同名同层按字典序，结果稳定可测）。"""
    known = list(plugins)
    requires_edges, optional_edges = graph(known, deps_of)
    return _topo(requires_edges, optional_edges, set(known), reverse=False)


def unload_order(plugins: Iterable[str],
                 deps_of: Callable[[str], tuple[Iterable[str], Iterable[str]]]
                 ) -> list[str]:
    """卸载顺序：使用者在前（先撤依赖别人的，再撤被依赖的）。"""
    known = list(plugins)
    requires_edges, optional_edges = graph(known, deps_of)
    return _topo(requires_edges, optional_edges, set(known), reverse=True)
