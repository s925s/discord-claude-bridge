<div align="center">

[日本語](README.md) | **English** | [中文](README_zh.md)

# Discord Claude Bridge

### Discord Forum × Claude Code CLI

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![discord.py](https://img.shields.io/badge/discord.py-2.3+-5865F2?style=for-the-badge&logo=discord&logoColor=white)](https://discordpy.readthedocs.io/)
[![Claude Code](https://img.shields.io/badge/Claude_Code-CLI-D97757?style=for-the-badge&logo=anthropic&logoColor=white)](https://docs.anthropic.com/en/docs/claude-code)
[![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)](LICENSE)
[![Platform](https://img.shields.io/badge/Windows-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://www.microsoft.com/windows)

**Turn Discord forum threads into Claude Code conversation sessions.**

---

</div>

## Overview

A bridge bot that executes [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI simply by posting in a Discord forum channel. Sessions are managed per thread, maintaining conversation context for continuous interaction.

```mermaid
graph LR
    A["Discord Forum"] -->|Message| B["Bridge Bot"]
    B -->|"claude -p --output-format json"| C["Claude Code CLI"]
    C -->|JSON Response| B
    B -->|Reply| A
    B -->|Embed| D["Log Channel"]

    style A fill:#5865F2,color:#fff,stroke:none
    style B fill:#2b2d31,color:#fff,stroke:#5865F2
    style C fill:#D97757,color:#fff,stroke:none
    style D fill:#2b2d31,color:#e7e9ea,stroke:#5865F2
```

## Features

| Feature | Description |
|:---:|---|
| **Session Management** | Automatic Claude Code session management per thread. Continues conversations with `--resume` |
| **Discord Permission Approval** | Approve/deny tool executions via Discord buttons before Claude Code runs them |
| **Auto Tag Updates** | `Running` / `Completed` / `Error` tags update in real-time |
| **Execution Logs** | All prompts, responses, and statuses recorded as Embeds in a separate channel |
| **Timeout Control** | Progress notification at 10 min, force kill at 1 hour |
| **Message Splitting** | Auto-splits responses over 2000 chars without breaking code blocks |
| **Access Control** | Only allowed user IDs can execute |

## Requirements

- **Python 3.11+**
- **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** — `claude` command available in PATH
- **Discord Bot** — Bot token with Message Content Intent enabled

## Quick Start

### 1. Installation

```bash
git clone https://github.com/cUDGk/discord-claude-bridge.git
cd discord-claude-bridge
pip install -r requirements.txt
```

### 2. Configuration

```bash
cp .env.example .env
```

Edit `.env` with the following:

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Discord bot token |
| `ALLOWED_USERS` | Allowed user IDs (comma-separated) |
| `FORUM_CHANNEL_ID` | Forum channel ID for receiving prompts |
| `LOG_CHANNEL_ID` | Channel ID for execution logs |
| `GUILD_ID` | Server (guild) ID |
| `SKIP_PERMISSIONS` | Set `true` to auto-allow all operations (default: `false`) |
| `HOOK_PORT` | Internal port for permission requests (default: `8585`) |

### 3. Discord Bot Setup

1. Create a bot at [Discord Developer Portal](https://discord.com/developers/applications)
2. Enable **Message Content Intent** under **Privileged Gateway Intents**
3. Invite the bot with required permissions:
   - `Send Messages` / `Manage Threads` / `Read Message History`
4. Create a forum channel and a text channel for logs

### 4. Start

```bash
python bot.py
```

## Usage

```
1. Create a thread in the forum channel
2. Post a message in the thread
3. The bot executes Claude Code and replies
4. Continue the conversation in the same thread
```

> Thread titles are automatically included as context for new sessions.

## Permission Mode

When `SKIP_PERMISSIONS=false` (default), Discord buttons appear whenever Claude Code attempts to use tools like file editing or command execution.

```mermaid
sequenceDiagram
    participant CC as Claude Code
    participant Hook as Hook Script
    participant Bot as Bridge Bot (HTTP :8585)
    participant D as Discord

    CC->>Hook: Tool execution or permission dialog
    Hook->>Bot: HTTP POST /permission
    Bot->>D: Send buttons (Allow / Always Allow / Deny)
    D->>Bot: User clicks
    Bot->>Hook: Return decision
    Hook->>CC: Allow or Block
```

Two hooks cover all permission checks:

| Hook | Trigger |
|:---:|---|
| **PreToolUse** | Before every tool execution (read-only tools are auto-allowed) |
| **PermissionRequest** | When Claude Code's permission dialog would appear |

| Button | Action |
|:---:|---|
| **Allow** | Allow this tool execution only |
| **Always Allow** | Auto-allow this tool for the rest of the thread |
| **Deny** | Block the tool execution |

> Read-only tools (`Read`, `Glob`, `Grep`, etc.) are automatically allowed.
> The port can be changed with the `HOOK_PORT` environment variable (default: `8585`).

## Security

> **Warning**
> Setting `SKIP_PERMISSIONS=true` will execute all operations **without confirmation**.
>
> - Always limit `ALLOWED_USERS` to trusted users only
> - The bot runs on the host machine, so it has equivalent access rights
> - The default `false` lets you approve/deny each tool via Discord

## License

MIT
