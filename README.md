<div align="center">

**日本語** | [English](README_en.md) | [中文](README_zh.md)

# Discord Claude Bridge

### Discordフォーラム × Claude Code CLI

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![discord.py](https://img.shields.io/badge/discord.py-2.3+-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discordpy.readthedocs.io/)
[![Claude Code](https://img.shields.io/badge/Claude_Code-CLI-D97757?style=for-the-badge&logo=anthropic&logoColor=white)](https://docs.anthropic.com/en/docs/claude-code)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)
[![Platform](https://img.shields.io/badge/Windows-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://www.microsoft.com/windows)

**Discordのフォーラムスレッドがそのまま Claude Code の会話セッションに。**

---

</div>

## Overview

Discord のフォーラムチャンネルに投稿するだけで、サーバー上の [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI が実行されるブリッジBotです。スレッドごとにセッションが管理され、会話の文脈を維持したまま継続的にやり取りできます。

```mermaid
graph LR
    A["Discord フォーラム"] -->|メッセージ| B["Bridge Bot"]
    B -->|"claude -p --output-format json"| C["Claude Code CLI"]
    C -->|JSON応答| B
    B -->|返信| A
    B -->|Embed| D["ログチャンネル"]

    style A fill:#5865F2,color:#fff,stroke:none
    style B fill:#2b2d31,color:#fff,stroke:#5865F2
    style C fill:#D97757,color:#fff,stroke:none
    style D fill:#2b2d31,color:#e7e9ea,stroke:#5865F2
```

## Features

| 機能 | 説明 |
|:---:|---|
| **セッション管理** | スレッドごとにClaude Codeセッションを自動管理。`--resume` で会話を継続 |
| **Discord権限承認** | Claude Codeのツール実行前にDiscordボタンで許可/拒否を選択可能 |
| **タグ自動更新** | `実行中` / `完了` / `エラー` タグがリアルタイムで切り替わる |
| **実行ログ** | 全実行のプロンプト・応答・ステータスをEmbed形式で別チャンネルに記録 |
| **タイムアウト制御** | 10分で進捗通知、1時間で強制終了。長時間タスクも安心 |
| **メッセージ分割** | 2000文字超の応答をコードブロックを壊さずに自動分割 |
| **アクセス制御** | 許可されたユーザーIDのみ実行可能 |

## Requirements

- **Python 3.11+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** — `claude` コマンドがPATHに通っていること
- **Discord Bot** — Message Content Intent が有効なBotトークン

## Quick Start

### 1. インストール

```bash
git clone https://github.com/cUDGk/discord-claude-bridge.git
cd discord-claude-bridge
pip install -r requirements.txt
```

### 2. 設定

```bash
cp .env.example .env
```

`.env` を編集して以下を設定：

| 変数名 | 説明 |
|---|---|
| `DISCORD_TOKEN` | Discord Botのトークン |
| `ALLOWED_USERS` | 実行を許可するユーザーID（カンマ区切り） |
| `FORUM_CHANNEL_ID` | プロンプトを受け付けるフォーラムチャンネルのID |
| `LOG_CHANNEL_ID` | 実行ログを送信するチャンネルのID |
| `GUILD_ID` | BotがいるサーバーのID |
| `SKIP_PERMISSIONS` | `true` で全操作を自動許可（デフォルト: `false`） |
| `HOOK_PORT` | 権限リクエスト用の内部ポート（デフォルト: `8585`） |

### 3. Discord Botの準備

1. [Discord Developer Portal](https://discord.com/developers/applications) でBotを作成
2. **Privileged Gateway Intents** → **Message Content Intent** を有効化
3. 必要な権限でBotをサーバーに招待：
   - `Send Messages` / `Manage Threads` / `Read Message History` / `Embed Links`
4. フォーラムチャンネルとログ用テキストチャンネルを作成

### 4. 起動

```bash
python bot.py
```

## Usage

```
1. フォーラムチャンネルにスレッドを作成
2. スレッド内にメッセージを投稿
3. Bot が Claude Code を実行して返信
4. 同じスレッド内で会話を継続
```

> スレッドタイトルは新規セッション時のコンテキストとして自動的に付加されます。

## Permission Mode

`SKIP_PERMISSIONS=false`（デフォルト）の場合、Claude Code がファイル編集やコマンド実行などのツールを使おうとすると、Discordスレッドにボタンが表示されます。

```mermaid
sequenceDiagram
    participant CC as Claude Code
    participant Hook as Hook Script
    participant Bot as Bridge Bot (HTTP :8585)
    participant D as Discord

    CC->>Hook: ツール実行 or 権限ダイアログ
    Hook->>Bot: HTTP POST /permission
    Bot->>D: ボタン送信（許可/常に許可/拒否）
    D->>Bot: ユーザーがクリック
    Bot->>Hook: 判定結果を返却
    Hook->>CC: 許可 or ブロック
```

2つのフックで全ての確認をカバーします：

| フック | 発火タイミング |
|:---:|---|
| **PreToolUse** | 全ツール実行前（読み取り専用ツールは自動許可） |
| **PermissionRequest** | Claude Codeの権限確認ダイアログ表示時 |

| ボタン | 動作 |
|:---:|---|
| **許可** | 今回のツール実行のみ許可 |
| **常に許可** | そのスレッド内で同じツールを以後自動許可 |
| **拒否** | ツール実行をブロック |

> 読み取り専用ツール（`Read`, `Glob`, `Grep` 等）は自動的に許可されます。
> ポートは `HOOK_PORT` 環境変数で変更可能です（デフォルト: `8585`）。

## Security

> **Warning**
> `SKIP_PERMISSIONS=true` を設定すると、全操作が**確認なしで**実行されます。
>
> - 必ず `ALLOWED_USERS` を信頼できるユーザーのみに限定してください
> - Botを動かすマシン上で実行されるため、そのマシンへのアクセス権と同等のリスクがあります
> - デフォルトの `false` では、Discord上でツールごとに許可/拒否を選択できます

## License

MIT
