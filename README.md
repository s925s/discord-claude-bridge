# Discord Claude Bridge

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![discord.py](https://img.shields.io/badge/discord.py-2.3+-5865F2?logo=discord&logoColor=white)
![Claude Code](https://img.shields.io/badge/Claude_Code-CLI-D97757?logo=anthropic&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)

Discordのフォーラムチャンネルから [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI を呼び出せるブリッジBotです。
フォーラムスレッドがそのままClaude Codeとの会話セッションになります。

## 特徴

- **フォーラムベースのUI** — スレッドごとにClaude Codeセッションを管理。会話の文脈が自動で維持されます
- **セッション永続化** — スレッドIDとClaude CodeのセッションIDを紐付けて保存。Bot再起動後も会話を継続可能
- **タグによるステータス管理** — 実行中 / 完了 / エラー のタグが自動で付与されます
- **ログチャンネル** — 全実行のプロンプト・応答・ステータスをEmbed形式で記録
- **タイムアウト制御** — ソフトタイムアウト（10分）で進捗通知、ハードタイムアウト（1時間）で強制終了
- **メッセージ自動分割** — 2000文字超の応答をコードブロックを壊さず分割送信
- **ユーザー制限** — 許可されたユーザーのみ実行可能

## 必要なもの

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) がインストール済みで `claude` コマンドが使えること
- Discord Bot トークン（Message Content Intent 有効化済み）

## セットアップ

### 1. 依存パッケージのインストール

```bash
pip install -r requirements.txt
```

### 2. 環境変数の設定

`.env.example` をコピーして `.env` を作成し、各値を設定してください。

```bash
cp .env.example .env
```

| 変数名 | 説明 |
|---|---|
| `DISCORD_TOKEN` | Discord Botのトークン |
| `ALLOWED_USERS` | 実行を許可するユーザーID（カンマ区切り） |
| `FORUM_CHANNEL_ID` | プロンプトを受け付けるフォーラムチャンネルのID |
| `LOG_CHANNEL_ID` | 実行ログを送信するチャンネルのID |
| `GUILD_ID` | BotがいるサーバーのID |

### 3. Discord Botの設定

1. [Discord Developer Portal](https://discord.com/developers/applications) でBotを作成
2. **Privileged Gateway Intents** で **Message Content Intent** を有効化
3. Botをサーバーに招待（`Send Messages`, `Manage Threads`, `Read Message History` 権限が必要）
4. サーバーにフォーラムチャンネルとログ用テキストチャンネルを作成

### 4. 起動

```bash
python bot.py
```

## 使い方

1. 設定したフォーラムチャンネルに新しいスレッドを作成
2. スレッド内にメッセージを投稿すると、Claude Code CLIが実行されます
3. スレッドタイトルは新規セッション時のコンテキストとして自動付加されます
4. 同じスレッド内の後続メッセージは、同一セッションとして会話が継続します

## 仕組み

```
Discord フォーラムスレッド
    ↓ メッセージ
Bot (bot.py)
    ↓ claude --dangerously-skip-permissions -p --output-format json
Claude Code CLI
    ↓ JSON応答
Bot → スレッドに返信 + ログチャンネルに記録
```

- セッションIDは `sessions.json` に保存され、`--resume` フラグで会話を継続します
- 同時実行は `asyncio.Queue` で逐次処理されます

## ライセンス

MIT
