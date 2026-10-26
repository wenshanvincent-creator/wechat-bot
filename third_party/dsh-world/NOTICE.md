# Design reference: dsh-world

Reference project: [chenkezhen480/dsh-world](https://github.com/chenkezhen480/dsh-world),
published under MIT. Reviewed README and the archive/history/runtime modules on
2026-10-25. Original license: <https://github.com/chenkezhen480/dsh-world/blob/main/LICENSE>.

This project independently implements its Python archival and diary code. It
borrows the design of retaining conversation originals, recalling them through
tools, and maintaining daily recollections and a relationship narrative. No
upstream DSH plugin code is copied or installed.

WeChat-specific changes: persistent persona/session SQLite namespaces, per-source
read permissions checked at query time, group speaker attribution, migration
receipts for old originals, uncertain authorship for old account messages without
verified AI receipts, source-line validation, and bounded excerpts in prompts.
The operator's persona and system permissions are not rewritten by diary entries.
