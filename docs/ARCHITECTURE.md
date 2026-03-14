# Architecture

## Design Lineage
- この設計は symphonyn の思想を受け継いでいる
- 対話の場と実行の正本を分離し、操作 UI と scheduler contract を混同しない
- 人間との対話は Discord に置きつつ、実行可否の最終判定は GitHub Issue / Projects v2 に集約する
- そのうえで、このリポジトリでは planning lane と execution lane を分離し、artifact-driven に実装へ接続する形へ再構成している

## Source Of Truth
- GitHub Issues: work item 本体
- GitHub Projects v2: canonical scheduler state
- Discord: conversation UI / planning approval UI / status mirror

## Control Plane
- Python orchestrator が唯一の control plane
- planning lane は Claude Agent SDK に限定する
- execution lane は Codex app-server に統一する
- repo-owned contract は `WORKFLOW.md` / `AGENTS.md` / Skills が担う

## Runtime Model
- workspace key: `{owner}/{repo}#{issue_number}`
- planning artifacts は issue 単位で保持し、execution は `attempt -> candidate` 単位の worktree へ分離する
- bare mirror は issue 単位で共有するが、実装 workspace は `attempt_id` / `candidate_id` ごとに独立させる
- 永続 source は GitHub issue workpad と filesystem artifacts
- DB なしでも再開できる構成を優先する

## State Model
- `Backlog`
- `Ready`
- `In Progress`
- `Human Review`
- `Rework`
- `Merging`
- `Done`
- `Blocked`
- `Cancelled`

## Required Artifacts
- `issue_snapshot.json`
- `requirement_summary.json`
- `plan.json`
- `plan_v2.json`
- `test_plan.json`
- `verification_plan.json`
- `candidate_decision.json`
- `scope_contract.json`
- `attempt_manifest.json`
- `winner_selection.json`
- `final_attempt_summary.json`
- `verification.json`
- `review_summary.json`
- `review_result.json`
- `scope_analysis.json`
- `changed_files.json`
- `proof_result.json`
- `session_checkpoint.json`
- `session_handoff_bundle.json`
- `final_summary.json`
- `run.log`
- `discord_events.jsonl`
- `workpad_updates.jsonl`
- `runner_metadata.json`

## Design Principles
1. GitHub Issue と Projects v2 を唯一の実行正本とする
   - bot の実行可否は GitHub の `State` と `Plan` だけで判定する。
2. Discord は操作 UI であり、正本ではない
   - Discord は requirements intake、plan review、approve/reject、status、abort のために使う。
3. 入力面は複数でも、確定先は 1 つにする
   - Discord から承認しても、最終的な状態確定は GitHub に書き戻す。
4. `Plan` は機械判定、workpad は人間向け記録として使い分ける
   - field は state machine、workpad は監査ログと説明責任のために使う。
5. 通常フローは Discord で要件整理し、approve 時に issue 化する
   - plan 承認後に GitHub Issue を自動作成し、初期状態は `Ready` にする。
6. state machine は既存の Project v2 state をそのまま使う
   - `Backlog / Ready / In Progress / Human Review / Rework / Merging / Done / Blocked / Cancelled` を採用する。
7. planning と implementation の責務を分離する
   - Claude は planning のみ、Codex は implementation のみを担う。
8. Codex は会話履歴ではなく planning artifacts を契約として受け取る
   - 実装時に参照するのは `goal`、`acceptance_criteria`、`constraints`、`out_of_scope`、`plan`、`test_plan`、`WORKFLOW.md` とする。
9. runtime の canonical unit は `attempt` と `candidate` にする
   - issue は scheduler の主語、attempt は試行の正本、candidate は実装探索の単位として管理する。
10. Discord thread は `1 issue : 1 thread` の binding として扱う
   - thread は UI 上の窓口であり、runtime の主語にはしない。
11. issue 化前は `DraftWorkItem` を持つ
   - Discord 上の要件対話は draft として保存し、approve 後に issue work item へ昇格させる。
12. retry は原則 new attempt として扱う
   - 同一 issue の再実行では新しい `attempt_id` を切り、winner 判定と履歴を混ぜない。
13. session resume は crash recovery 専用とする
   - 通常の `Rework` や repair 継続では handoff bundle を書いて new session を開始する。

## State And Plan Semantics
### Execution Gate
- bot が新規実行を開始してよい条件は `State in {Ready, Rework}` かつ `Plan = Approved`
- `In Progress` は新規 dispatch 対象ではなく、既存実行の整合確認対象として扱う

### State Meanings
- `Backlog`: 手動で積まれた未着手 issue。通常の Discord 起点フローはこの state を経由しなくてよい
- `Ready`: plan 承認済みで bot が着手してよい
- `In Progress`: bot 実行中。新しい attempt を重ねて開始してはいけない
- `Human Review`: bot の実装と検証が終わり、人間の確認待ち
- `Rework`: bot が再試行してよい修正待ち
- `Merging`: 人間承認済みで agent が land 中
- `Done`: merge 完了
- `Blocked`: bot 単独では解消できない停止
- `Cancelled`: work item 打ち切り

### State Transitions
- `Backlog -> Ready`
- `Ready -> In Progress`
- `In Progress -> Human Review`
- `In Progress -> Rework`
- `Human Review -> Rework`
- `Human Review -> Merging`
- `Merging -> Done`
- `Any non-terminal -> Blocked`
- `Blocked -> Ready`
- `Any non-terminal -> Cancelled`

### Plan Meanings
- `Not Started`: plan 未生成
- `Drafted`: plan 生成済みで承認待ち
- `Approved`: implementation 開始可能
- `Changes Requested`: plan 差し戻し

### Plan Transitions
- `Not Started -> Drafted`
- `Drafted -> Approved`
- `Drafted -> Changes Requested`
- `Changes Requested -> Drafted`
- 通常の `Rework` では `Plan = Approved` を維持する
- 大きなスコープ変更や方針変更が必要な場合だけ、人間が明示的に `Plan = Changes Requested` に戻す

## Draft Lifecycle
### Draft Work Item States
- `collecting_requirements`
- `planning`
- `awaiting_approval`
- `changes_requested`
- `promotion_failed`
- `promoted`
- `discarded`

### Draft Transitions
- `collecting_requirements -> planning`
- `planning -> awaiting_approval`
- `awaiting_approval -> promoted`
- `awaiting_approval -> changes_requested`
- `changes_requested -> collecting_requirements`
- `changes_requested -> planning`
- `Any non-terminal -> discarded`

### Draft Rules
- `planning` への遷移は人間の `/plan` を起点にする
- `discarded` は明示操作でのみ遷移させる
- approve 時に draft は `promoted` となり、自動作成された GitHub issue に昇格する
- draft は issue 化後も履歴として保持する
- issue 作成後の昇格処理が途中で失敗した場合、draft は `promotion_failed` として保持する

## Identity Model
- pre-issue 段階は `DraftWorkItem` を持つ
- post-issue 段階は `IssueWorkItem` を持つ
- runtime 主キーは `issue_key`
- 各試行は `attempt_id`、実装候補は `candidate_id` で区別する
- Discord thread は `thread_id <-> issue_key` の binding を持つが、runtime 主キーにはしない

### Runtime Rules
- retry は同一 issue に対する新しい試行であり、毎回新しい `attempt_id` を発行する
- `thread/resume` は同一 attempt / session の crash recovery にだけ使う
- `Rework` や差し戻し後の再実行では `thread/resume` を使わず、新しい attempt を開始する
- abort は current attempt を停止し、通常は issue を `Blocked` に遷移させる
- restore は attempt 再開ではなく state 整合処理として扱う
- `In Progress` で process 不在なら `Rework` に落とす
- `Merging` で整合が取れなければ `Blocked` に落とす

## Execution Artifact Model
- planning の正本は `planning/plan_v2.json` と `planning/committee_bundle.json`
- execution の正本は `attempts/{attempt_id}/artifacts/attempt_manifest.json`
- candidate ごとの実装結果は `attempts/{attempt_id}/candidates/{candidate_id}/artifacts/` に保持する
- winner 確定後のみ `views/` と issue latest artifacts を更新する
- repair 継続や compact 前には `session_checkpoint.json` と `session_handoff_bundle.json` を書く

## Scheduler Model
### Core Responsibilities
- `dispatch pass`: `Ready` / `Rework` を新規実行に乗せる
- `reconcile pass`: `In Progress` の process / attempt 整合を確認する
- `merge pass`: `Merging` の完了確認と不整合検出を行う

### Scheduler Rules
- scheduler は GitHub Projects v2 の `State` / `Plan` を見て判断する
- Discord は dispatch の起点ではなく state 変更 UI として扱う
- Discord 操作後は即時 tick のヒントを出してよいが、最終判定は常に GitHub を見る
- 同一 `issue_key` に active attempt は常に 1 つまでとする

## Verification Model
- repo に `WORKFLOW.md` があれば、その `verification.required_checks` を最優先する
- repo に `WORKFLOW.md` がない場合は planning lane が `verification_plan.json` を確定し、execution lane はその plan を workflow fallback として使う
- verification は `hard_checks` と `advisory_checks` に分け、PR gating は `hard_checks` の結果だけで判定する
- `manual_checks` は通常運用の gate に使わない
- profile catalog は `python-basic` / `python-typecheck` / `node-basic` / `node-ts` / `static-web` / `generic-minimal` を初期集合とする
- monorepo や部分変更は profile を増やすのではなく `profile + scope` で扱う
- planner の例外調整は自由な command 生成ではなく `profile_patch` に限定する
- `generic-minimal` は最後の fallback であり、常用 profile にしない

## Failure And Recovery Principles
- Discord 操作の成功条件は GitHub への反映成功とする
- GitHub field 更新に失敗した場合、Discord 側だけで成功扱いしない
- issue 作成成功後は rollback しない
- issue 作成後の後続処理に失敗した場合、draft は `promotion_failed` として保持し、issue URL や `issue_key` を参照できるようにする
- 二重起動防止は多層で行う
  - GitHub の `State` / `Plan` gate
  - active attempt の存在確認
  - `issue_key` 単位のロック
- dispatch の取りこぼしは次回 poll で回収できる設計にする
- restore は賢すぎる推測をしない
- `In Progress` の崩壊は `Rework`
- `Merging` の崩壊は `Blocked`
- 人間が GitHub 上で不正な state / plan 組み合わせを作れてしまうことは許容するが、bot はその組み合わせを見たら保守的に停止する
- GitHub が正本であっても、bot は危険な自動補正を行わない
- `1 issue : 1 thread` を破る binding は hard error として扱う
