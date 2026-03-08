# AGENTS.md

## Claude Agent SDK Only Policy

このリポジトリで Claude を使ったエージェント機能を実装・拡張・修正する場合、必ず `claude-agent-sdk` のみを使用すること。

### Mandatory Rules
- Python では `claude_agent_sdk` を唯一のエージェント SDK として扱うこと。
- 単発タスク、自律実行、構造化出力、hooks、権限制御は、すべて `claude-agent-sdk` の公式 API で実装すること。
- Claude を呼び出す処理を追加・変更するときは、既存の SDK ラッパーや設定経路を拡張すること。
- 親エージェントが issue 解決まで同一コンテキストを維持する必要がある場合は、`ClaudeSDKClient` を使用してよい。
- Python では、単発処理は `query()`、継続セッションが必要な親エージェントは `ClaudeSDKClient` を使い分けること。
- hooks は `ClaudeAgentOptions(hooks=...)` で構成すること。
- Claude Code CLI を SDK の代替として直接制御する実装にしないこと。Claude の実行は `claude-agent-sdk` を通すこと。

### Required Patterns
- 単発の問い合わせは `query()` を使うこと。
- issue 解決まで継続的に文脈を保持する親エージェントは `ClaudeSDKClient` を使うこと。
- JSON を期待する場合は `ClaudeAgentOptions(output_format={"type": "json_schema", "schema": ...})` を優先すること。
- ツール制御は `allowed_tools`、`disallowed_tools`、`permission_mode` で行うこと。
- プロジェクト固有設定や `CLAUDE.md` を読む必要がある場合は `setting_sources=["project"]` などを明示すること。
- hooks を使う場合は `PreToolUse`、`PostToolUse`、`PostToolUseFailure`、`Notification` などの公式 hook event を使うこと。
- hooks の副作用は軽量に保ち、ログ送信や通知のような副作用中心の処理は必要に応じて `async_` を使って非同期化すること。

### Progress And Observability
- エージェントの進捗可視化が必要な場合、進捗の取得元は `claude-agent-sdk` の hooks に限定すること。
- ツール使用状況の記録は `PreToolUse` / `PostToolUse` hooks で取得すること。
- Discord など外部通知が必要な場合も、hooks で取得した情報を整形して送ること。
- `Read` / `Grep` のような高頻度イベントはログ中心、`Bash` / `Write` / `Edit` / `Task` は通知対象、のようにノイズ制御を行うこと。
- 将来的な `/status` の情報源も、hooks 由来の状態ファイルや履歴ファイルに統一すること。

### Prohibited Approaches
- Claude の応答を自前の while ループで回し、独自に tool_use を解釈・再送する実装。
- `claude` CLI を直接叩き、その出力を独自解析して SDK の代わりに使う実装。
- hooks が使える箇所で、hooks 以外の監視経路を主経路にする実装。
- OpenAI Agents SDK、LangChain agents、Autogen、CrewAI、smolagents など、`claude-agent-sdk` 以外のエージェント基盤の導入。
- 他社 SDK や別モデル SDK を混在させたエージェント層の新設。

### Decision Rule
- 「`claude-agent-sdk` の公式 API で実現できるか」を最優先の判断基準にすること。
- まず `query()`、`ClaudeAgentOptions`、`hooks`、`output_format`、`permission_mode`、`setting_sources` の組み合わせで解決を試みること。
- `claude-agent-sdk` で実現できないと確認できるまでは、別の SDK や独自エージェントループへ逃げないこと。
- 実現不能な場合も、理由を明示し、勝手に別 SDK へ移行しないこと。

### Default Implementation Baseline
- 基本 import は `from claude_agent_sdk import query, ClaudeSDKClient, ClaudeAgentOptions, HookMatcher` とすること。
- 標準形は次を基準にすること。
  - `query(prompt=..., options=ClaudeAgentOptions(...))`
  - `async with ClaudeSDKClient(options=...) as client: ...`
  - `hooks={"PreToolUse": [...], "PostToolUse": [...], "Notification": [...]}`
  - `output_format={"type": "json_schema", "schema": ...}`
- 既存コードに SDK ラッパーがある場合、新機能もまずそのラッパー拡張で対応すること。

### Change Management
- 既存の `claude-agent-sdk` 利用方針を維持しながら改善すること。
- 新機能追加時も、まず既存の SDK ラッパー、状態保存、進捗通知経路を拡張すること。
- この方針に反する提案をする場合は、実装前に理由と代替案を明示すること。
