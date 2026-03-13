<!-- GENERATED FILE. DO NOT EDIT.
output: REVIEW.md
sources:
  - docs/policy/core.md
  - docs/policy/review.overlay.md
checksum: sha256:8316bdfa97b9fd15ce220082ff10f5fa7193ec29492a38bcd5c5a3bf6f973320
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

# Review overlay

- correctness を優先する。
- style-only の指摘は `nit` に留める。
- `pre_existing` は severity ではなく origin として扱う。
- GitHub inline 投稿は verifier confirmed かつ confidence threshold 超過のみ。
