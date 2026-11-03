# 8. Points the bundle does not settle (stated, not guessed)

> 文档索引：[docs/README.md](../../README.md) · 上级：[DSH 内部机制](README.md) · 上一章：[7. Checklist for a Python re-implementation](07-python-checklist.md)


* The exact dates/times injected by `dsh-time-context` / `dsh-tmux-context` were not analyzed; they
  are separate packages and are not part of the core prompt path read here.
* The `surfaceOp` union is printed in `dsh-session/lib/types/types.d.ts:429-433` with `endSeq:
  Sequence` while every implementation treats it as `SessionSeq`
  (`index.js:400-401`, `types.d.ts:439-446`); this is a d.ts rendering artifact.
* The precise pre-v0.1.5 migration transforms live in `dsh-session-format-v1-to-v2` /
  `-v2-to-v3`; only the current version (`SESSION_FORMAT_VERSION = 3`) and the filename/one-JSON-per-line
  layout were verified here.
* No package in this tree documents a numeric "used/limit ratio" surface: the ratio exists only as the
  compaction config, and occupancy is computed by consumers.
