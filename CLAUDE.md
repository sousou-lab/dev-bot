<!-- GENERATED FILE. DO NOT EDIT.
output: CLAUDE.md
sources:
  - docs/policy/core.md
  - docs/policy/claude.overlay.md
checksum: sha256:d9d440bc1ddc170ae43541ee3d532e36139c64e774a9ed4dcebb376bb54a0e66
-->
# Core agent policy

- GitHub Issue と Projects v2 を唯一の実行正本とする。
- Discord は UI であり、実行可否の正本ではない。
- 変更は issue の goal / acceptance criteria / approved plan に厳密に従う。
- out-of-scope を見つけたら workpad に記録し、勝手に拡張しない。
- 既定では single-writer を守る。
- candidate editor は Phase 1 では最大 2 本までとする。
- 検証 artifact は必ず残す。
- secrets / PII / credentials を artifact に含めない。

# Claude overlay

- planning committee は read-only tool だけを使う。
- `setting_sources=["project"]` を前提に project settings を読む。
- structured output を必須とし、JSON schema に従う。
