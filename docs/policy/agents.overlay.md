# OpenAI / Codex overlay

- 実装前に `AGENTS.md` と repo-local skills を確認する。
- Codex の `thread/resume` は same `run_id` crash recovery にのみ使う。
- 既定で network off とし、明示許可がなければ外部アクセスしない。
- draft PR summary と verification artifact を更新してから終了する。
