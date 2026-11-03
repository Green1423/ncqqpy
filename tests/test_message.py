"""MessageChain.split 的切分契约。

覆盖两类真实踩过的坑：

1. **靠前的换行把整条预算浪费掉**：`split_text` 原来只要在 room 内找到换行就按它切，
   于是「21 字的标题行 + 一大段没有换行的正文」会被切成「21 + 1200 + 1200 + …」
   —— 第一条只装了标题，剩下的预算白扔。
2. **短段落被硬切**：剩余空间放不下一个短段落（典型是 `🔗 原文（含附件）：https://…`）
   时，旧实现为了填满当前条会把它拦腰切断 —— 上一条以 `…/notice/` 结尾、下一条以
   `2026/…开头`，两边都点不开（bupt_news 长通知分段时实测踩到）。

现在：换行点不足一半时不按换行切；整段放不进当前条剩余空间、但能放进新的一条时，
先换条再整段放。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from napcat.message import MessageChain
from napcat.message import split_text               # noqa: E402

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


def texts(chunks: list[MessageChain]) -> list[str]:
    return ["".join(s["data"]["text"] for s in c.build() if s["type"] == "text")
            for c in chunks]


def test_budget_and_lossless():
    print("\n[1] 预算与内容无损")
    long_text = "\n".join(f"第{i}段" + "字" * 40 for i in range(1, 11))
    parts = MessageChain().text(long_text).split(max_chars=120, max_images=0)
    tt = texts(parts)
    check("切成多块", len(parts) >= 4, str(len(tt)))
    check("拼回去与原消息一致", "".join(tt) == long_text)
    check("每块不超预算", all(len(t) <= 120 for t in tt), str([len(t) for t in tt]))


def test_newline_preference():
    print("\n[2] 换行优先（不把一行劈开）")
    text = "\n".join(f"第{i}行" + "字" * 41 for i in range(1, 9)) + "\n"
    tt = texts(MessageChain().text(text).split(max_chars=120, max_images=0))
    check("每块正文都以换行结尾（最后一块除外）",
          all(t.endswith("\n") for t in tt[:-1]), str([t[-2:] for t in tt[:-1]]))
    check("拼回去一致", "".join(tt) == text)


def test_early_newline_not_wasteful():
    print("\n[3] 靠前的换行不能浪费预算")
    text = "📢 #1 标题\n" + "字" * 3000
    head, rest = split_text(text, 1200)
    check("第一条装到预算上限（不是只装标题）", len(head) == 1200, str(len(head)))
    check("剩余部分接着来", len(rest) == len(text) - 1200, str(len(rest)))
    parts = texts(MessageChain().text(text).split(max_chars=1200, max_images=0))
    check("分条长度合理（没有 21 字那种空条）",
          all(len(t) > 100 for t in parts[:-1]), str([len(t) for t in parts]))


def test_short_segment_moves_not_cut():
    print("\n[4] 短段落放不下时整段换条（原文链接不被切断）")
    url = "https://example.edu.cn/notice/2026/1234567890abcdef"
    footer = f"\n🔗 原文（含附件）：{url}"
    body = "正" * (1200 - 30)                  # 当前条只剩 30 字余量，footer 有 55+
    parts = texts(MessageChain().text(body).text(footer).split(max_chars=1200, max_images=0))
    check("被切成两条", len(parts) == 2, str([len(p) for p in parts]))
    check("链接完整地只在其中一条里", sum(url in p for p in parts) == 1, str(parts))
    check("没有半截链接", not any("notice/2026" in p and url not in p for p in parts))
    check("footer 那条只有 footer（没有把正文尾巴塞进来）",
          parts[1] == footer, repr(parts[1]))


def test_bupt_news_shape():
    print("\n[5] 复现 bupt_news 的真实形态：标题 + 长正文 + 链接 footer")
    url = "https://example.edu.cn/notice/2026/1234567890abcdef"
    head = "📢 #1 标题\n────────────\n"
    budget = 1200
    body_len = (budget - len(head)) + budget * 2 - 20
    chain = (MessageChain()
             .text(head + "正" * body_len)
             .text(f"\n🔗 原文（含附件）：{url}"))
    parts = texts(chain.split(max_chars=budget, max_images=4))
    check("链接完整", any(url in p for p in parts), str([len(p) for p in parts]))
    check("每条都接近预算（只有 footer 那条例外）",
          all(len(p) >= budget * 0.95 for p in parts if url not in p),
          str([len(p) for p in parts]))
    check("条数没有因为空条而虚增", len(parts) == 4, str(len(parts)))


def test_media_and_limits():
    print("\n[6] 媒体配额 / 不限 / 边界")
    chain = MessageChain()
    for i in range(1, 8):
        chain.text(f"\n段落{i}").image(file=f"file:///tmp/{i}.jpg")
    mixed = chain.split(max_chars=1000, max_images=3)
    check("每块 ≤3 张图",
          all(len([s for s in p.build() if s["type"] == "image"]) <= 3 for p in mixed))
    check("图文顺序不变",
          [s["data"]["file"].rsplit("/", 1)[-1]
           for p in mixed for s in p.build() if s["type"] == "image"]
          == [f"{i}.jpg" for i in range(1, 8)])
    check("max_chars=0 / max_images=0 = 不限",
          len(chain.split(max_chars=0, max_images=0)) == 1)
    check("空消息也返回一条", len(MessageChain().split()) == 1)
    check("超长无换行硬切",
          [len(t) for t in texts(MessageChain().text("y" * 250).split(max_chars=100,
                                                                     max_images=0))]
          == [100, 100, 50])
    check("整段远长于预算也能收敛（不死循环）",
          len(texts(MessageChain().text("z" * 2500).split(max_chars=1200, max_images=0))) == 3)
    many = MessageChain()
    for i in range(12):
        many.text(f"{i:02d}abcde")             # 每段 7 字
    packed = texts(many.split(max_chars=100, max_images=0))
    check("多段短文本按条打包（不是一段一条）", len(packed) <= 2,
          str([len(p) for p in packed]))


def main() -> int:
    test_budget_and_lossless()
    test_newline_preference()
    test_early_newline_not_wasteful()
    test_short_segment_moves_not_cut()
    test_bupt_news_shape()
    test_media_and_limits()
    print(f"\n{'=' * 50}\n通过 {PASSED} / 失败 {FAILED}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
