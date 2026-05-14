import atexit
import os
import secrets
import signal
import sys
import json
import asyncio
import re
import shutil
import subprocess
import threading
import weakref

# Windows: aiodns が SelectorEventLoop を要求する問題を回避
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import uuid
import base64
import io
import tempfile
import logging
from pathlib import Path
from datetime import datetime, timezone

# Windows cp932 で絵文字が encode できない問題を回避
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

import discord
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bridge")

# ==============================
# 設定読み込み + 起動時検証
# ==============================


def _require_int(name: str, allow_zero_for: list[str] | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        if allow_zero_for and name in allow_zero_for:
            return 0
        raise SystemExit(f"環境変数 {name} が未設定です。.env を確認してください")
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"環境変数 {name} は整数である必要があります（現値: {raw!r}）")


TOKEN = os.getenv("DISCORD_TOKEN", "").strip().lstrip("﻿")  # BOM 付き .env への防御
if not TOKEN:
    raise SystemExit("環境変数 DISCORD_TOKEN が未設定です。.env を確認してください")

FORUM_CHANNEL_ID = _require_int("FORUM_CHANNEL_ID")
LOG_CHANNEL_ID = _require_int("LOG_CHANNEL_ID", allow_zero_for=["LOG_CHANNEL_ID"])
GUILD_ID = _require_int("GUILD_ID", allow_zero_for=["GUILD_ID"])
ALLOWED_USERS = set(filter(None, (x.strip() for x in os.getenv("ALLOWED_USERS", "").split(","))))
if not ALLOWED_USERS:
    log.warning("ALLOWED_USERS が空です。誰もコマンドを実行できません")

SKIP_PERMISSIONS = os.getenv("SKIP_PERMISSIONS", "false").lower() in ("true", "1", "yes")
HOOK_PORT = int(os.getenv("HOOK_PORT", "8585"))
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")

# --permission-mode に渡す値。空なら未指定（claude のデフォルト）
VALID_PERMISSION_MODES = {"", "default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions"}
PERMISSION_MODE = os.getenv("PERMISSION_MODE", "").strip()
if PERMISSION_MODE not in VALID_PERMISSION_MODES:
    raise SystemExit(
        f"PERMISSION_MODE が不正です: {PERMISSION_MODE!r}。"
        f"有効値: {sorted(VALID_PERMISSION_MODES)}"
    )
if SKIP_PERMISSIONS and PERMISSION_MODE:
    log.warning(
        "SKIP_PERMISSIONS=true と PERMISSION_MODE=%s が同時設定されています。"
        "--dangerously-skip-permissions が優先され、PERMISSION_MODE は無視されます",
        PERMISSION_MODE,
    )


def _validate_numeric_env(name: str, value: str, allow_float: bool = False) -> str:
    if not value:
        return ""
    try:
        v = float(value) if allow_float else int(value)
    except ValueError:
        raise SystemExit(f"{name} は数値である必要があります（現値: {value!r}）")
    if v < 0:
        raise SystemExit(f"{name} は 0 以上である必要があります（現値: {value!r}）")
    return value


MAX_TURNS = _validate_numeric_env("MAX_TURNS", os.getenv("MAX_TURNS", "").strip())
MAX_BUDGET_USD = _validate_numeric_env("MAX_BUDGET_USD", os.getenv("MAX_BUDGET_USD", "").strip(), allow_float=True)

# hook → bot HTTP の共有秘密。ローカル他プロセスからの偽リクエスト遮断用
HOOK_AUTH_TOKEN = secrets.token_hex(16)
HOOK_AUTH_HEADER = "X-Bridge-Auth"

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def is_valid_session_id(sid: str) -> bool:
    return bool(sid) and bool(SESSION_ID_RE.match(sid))

SESSIONS_FILE = Path(__file__).parent / "sessions.json"
TEMP_DIR = Path(tempfile.gettempdir()) / "discord-claude-bridge"
TEMP_DIR.mkdir(exist_ok=True)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
DISCORD_FILE_LIMIT = 24 * 1024 * 1024  # 25MB から少し余裕
def _validate_timeout(name: str, default: str) -> int:
    raw = os.getenv(name, default).strip()
    try:
        v = int(raw)
    except ValueError:
        raise SystemExit(f"{name} は整数である必要があります（現値: {raw!r}）")
    if v < 0:
        raise SystemExit(f"{name} は 0 以上である必要があります（現値: {v}）")
    return v


SOFT_TIMEOUT = _validate_timeout("SOFT_TIMEOUT", "600")   # まだ動いてるよ通知 (0 で無効)
HARD_TIMEOUT = _validate_timeout("HARD_TIMEOUT", "3600")  # 強制終了 (0 で無効化したい場合は超大値推奨)
if HARD_TIMEOUT == 0:
    raise SystemExit("HARD_TIMEOUT=0 は許可されていません（必ず正の値を指定してください）")
if SOFT_TIMEOUT > 0 and SOFT_TIMEOUT >= HARD_TIMEOUT:
    log.warning(
        "SOFT_TIMEOUT(%d) >= HARD_TIMEOUT(%d): ソフト通知より先に強制終了するため通知は表示されません",
        SOFT_TIMEOUT, HARD_TIMEOUT,
    )
STREAM_LINE_BUFSIZE = 16 * 1024 * 1024  # 1行あたり最大16MB（画像 base64 を含む長行に対応）

# フォーラムタグ名
TAG_RUNNING = "実行中"
TAG_COMPLETED = "完了"
TAG_ERROR = "エラー"

# 走行中の Claude subprocess を追跡（bot 終了時に確実に kill するため）
_active_procs: "weakref.WeakSet[subprocess.Popen]" = weakref.WeakSet()
_active_procs_lock = threading.Lock()
# スレッドID → 現在実行中の subprocess。/cancel から取り出して kill する
_thread_procs: dict[int, subprocess.Popen] = {}


def _register_proc(proc: subprocess.Popen, thread_id: int | None = None):
    with _active_procs_lock:
        _active_procs.add(proc)
        if thread_id is not None:
            _thread_procs[thread_id] = proc


def _unregister_thread_proc(thread_id: int | None):
    if thread_id is None:
        return
    with _active_procs_lock:
        _thread_procs.pop(thread_id, None)


def get_thread_proc(thread_id: int) -> subprocess.Popen | None:
    with _active_procs_lock:
        return _thread_procs.get(thread_id)


def _kill_all_procs():
    with _active_procs_lock:
        procs = list(_active_procs)
    for p in procs:
        if p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass


atexit.register(_kill_all_procs)

# /cwd で固定された thread → cwd
thread_cwds: dict[int, str] = {}
# /usage 用の累積使用量 (thread → {"input_tokens": N, "output_tokens": N, "cost_usd": float, "turns": N})
usage_stats: dict[int, dict] = {}
# /retry 用の直前 prompt (thread → prompt 本文)
last_prompts: dict[int, str] = {}


# ==============================
# 権限管理
# ==============================

# request_id → asyncio.Event (ボタン押下待ち)
permission_events: dict[str, asyncio.Event] = {}
# request_id → フックに返す結果
permission_results: dict[str, dict] = {}
# thread_id → 「常に許可」されたツール名のセット
allowed_tools: dict[str, set[str]] = {}


def format_tool_detail(tool_name: str, tool_input: dict) -> str:
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"```bash\n{cmd[:800]}\n```"
    if tool_name == "Write":
        path = tool_input.get("file_path", "")
        content = tool_input.get("content", "")
        parts = [f"**ファイル:** `{path}`"]
        if content:
            parts.append(f"```\n{content[:600]}\n```")
        return "\n".join(parts)
    if tool_name in ("Edit", "MultiEdit"):
        path = tool_input.get("file_path", "")
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        parts = [f"**ファイル:** `{path}`"]
        if old:
            parts.append(f"```diff\n- {old[:200]}\n+ {new[:200]}\n```")
        return "\n".join(parts)
    if tool_name == "NotebookEdit":
        path = tool_input.get("notebook_path", tool_input.get("file_path", ""))
        return f"**ノートブック:** `{path}`"
    detail = json.dumps(tool_input, ensure_ascii=False, indent=2)
    if len(detail) > 500:
        detail = detail[:500] + "\n..."
    return f"```json\n{detail}\n```"


async def safe_send(channel, content=None, **kwargs):
    """Discord 送信を NotFound/Forbidden/HTTPException から守る"""
    if channel is None:
        return None
    try:
        return await channel.send(content=content, **kwargs)
    except discord.NotFound:
        log.warning("送信先チャンネル/スレッドが存在しません (削除済?)")
    except discord.Forbidden:
        log.warning("送信権限がありません: %s", getattr(channel, "id", "?"))
    except discord.HTTPException as e:
        log.warning("Discord HTTP エラー: %s", e)
    return None


async def safe_edit(message, content=None, **kwargs):
    if message is None:
        return None
    try:
        return await message.edit(content=content, **kwargs)
    except discord.NotFound:
        log.warning("編集対象メッセージが存在しません")
    except discord.Forbidden:
        log.warning("メッセージ編集権限がありません")
    except discord.HTTPException as e:
        log.warning("Discord HTTP エラー (edit): %s", e)
    return None


class PermissionView(discord.ui.View):
    """許可 / 常に許可 / 拒否 ボタン"""

    def __init__(self, request_id: str, tool_name: str, thread_id: str, hook_type: str):
        super().__init__(timeout=600)
        self.request_id = request_id
        self.tool_name = tool_name
        self.thread_id = thread_id
        self.hook_type = hook_type
        self._resolved = False

    def _make_allow(self) -> dict:
        if self.hook_type == "PermissionRequest":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow"},
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }

    def _make_deny(self, reason: str) -> dict:
        if self.hook_type == "PermissionRequest":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny", "message": reason},
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    def _resolve(self, result: dict) -> bool:
        if self._resolved:
            return False
        self._resolved = True
        permission_results[self.request_id] = result
        ev = permission_events.get(self.request_id)
        if ev:
            ev.set()
        return True

    async def _safe_edit_response(self, interaction: discord.Interaction, content: str):
        try:
            await interaction.response.edit_message(content=content, view=None)
        except (discord.NotFound, discord.HTTPException) as e:
            # interaction 期限切れや既応答済み
            log.warning("ボタン応答編集失敗: %s", e)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # ALLOWED_USERS 以外のクリックを拒否
        if str(interaction.user.id) not in ALLOWED_USERS:
            try:
                await interaction.response.send_message("権限がありません", ephemeral=True)
            except (discord.NotFound, discord.HTTPException):
                pass
            return False
        return True

    @discord.ui.button(label="許可", style=discord.ButtonStyle.green, emoji="✅")
    async def allow_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._resolve(self._make_allow()):
            await self._safe_edit_response(interaction, "既に処理済みです")
            return
        self.stop()
        await self._safe_edit_response(interaction, f"✅ `{self.tool_name}` を許可しました")

    @discord.ui.button(label="常に許可", style=discord.ButtonStyle.blurple, emoji="🔓")
    async def always_allow_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed_tools.setdefault(self.thread_id, set()).add(self.tool_name)
        if not self._resolve(self._make_allow()):
            await self._safe_edit_response(interaction, "既に処理済みです")
            return
        self.stop()
        await self._safe_edit_response(interaction, f"🔓 `{self.tool_name}` を常に許可しました（このスレッド内）")

    @discord.ui.button(label="拒否", style=discord.ButtonStyle.red, emoji="❌")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._resolve(self._make_deny("Discordユーザーが拒否しました")):
            await self._safe_edit_response(interaction, "既に処理済みです")
            return
        self.stop()
        await self._safe_edit_response(interaction, f"❌ `{self.tool_name}` を拒否しました")

    async def on_timeout(self):
        # タイムアウト時は許可（フック側がタイムアウトで詰まないように）
        if self.request_id in permission_events and not self._resolved:
            self._resolve(self._make_allow())


class QuestionView(discord.ui.View):
    """AskUserQuestion 用: 選択肢ボタンを並べる"""

    def __init__(self, request_id: str, thread_id: str, hook_type: str, options: list):
        super().__init__(timeout=600)
        self.request_id = request_id
        self.thread_id = thread_id
        self.hook_type = hook_type
        self._resolved = False
        for i, label in enumerate(options[:20]):
            btn = discord.ui.Button(
                label=(label or f"選択肢{i+1}")[:80],
                style=discord.ButtonStyle.primary,
                custom_id=f"q_{request_id}_{i}",
            )
            btn.callback = self._make_callback(label)
            self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) not in ALLOWED_USERS:
            try:
                await interaction.response.send_message("権限がありません", ephemeral=True)
            except (discord.NotFound, discord.HTTPException):
                pass
            return False
        return True

    def _build_response(self, answer: str) -> dict:
        reason = f"User answered: {answer}. Treat this as the user's answer and continue."
        if self.hook_type == "PermissionRequest":
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "deny", "message": reason},
                }
            }
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    def _resolve(self, answer: str) -> bool:
        if self._resolved:
            return False
        self._resolved = True
        permission_results[self.request_id] = self._build_response(answer)
        ev = permission_events.get(self.request_id)
        if ev:
            ev.set()
        return True

    def _make_callback(self, answer: str):
        async def cb(interaction: discord.Interaction):
            if not self._resolve(answer):
                try:
                    await interaction.response.edit_message(content="既に回答済みです", view=None)
                except (discord.NotFound, discord.HTTPException):
                    pass
                return
            self.stop()
            try:
                await interaction.response.edit_message(content=f"✅ 回答: {answer[:200]}", view=None)
            except (discord.NotFound, discord.HTTPException) as e:
                log.warning("質問応答編集失敗: %s", e)
        return cb

    async def on_timeout(self):
        if self.request_id in permission_events and not self._resolved:
            self._resolve("（タイムアウト）")


async def _handle_ask_user_question(tool_input: dict, thread_id: str, hook_type: str) -> "web.Response":
    questions = tool_input.get("questions") or tool_input.get("question")
    q_list = []
    if isinstance(questions, list):
        q_list = questions
    elif isinstance(questions, dict):
        q_list = [questions]
    elif isinstance(questions, str):
        q_list = [{"question": questions, "options": []}]

    q = q_list[0] if q_list else {}
    q_text = q.get("question") or q.get("header") or tool_input.get("question") or "Claude からの質問"
    options: list = []
    for opt in (q.get("options") or []):
        if isinstance(opt, dict):
            label = opt.get("label") or opt.get("name") or opt.get("value") or ""
            desc = opt.get("description") or ""
            options.append(f"{label}: {desc}" if desc and label else (label or desc))
        elif isinstance(opt, str):
            options.append(opt)
    if not options:
        options = ["はい", "いいえ"]

    request_id = str(uuid.uuid4())
    event = asyncio.Event()
    permission_events[request_id] = event

    thread = None
    if thread_id:
        try:
            thread = bot.get_channel(int(thread_id))
        except (ValueError, TypeError):
            thread = None

    if thread is None:
        permission_events.pop(request_id, None)
        return web.json_response(make_quick_allow(hook_type))

    try:
        view = QuestionView(request_id, thread_id, hook_type, options)
        await thread.send(f"❓ **Claudeからの質問**\n{str(q_text)[:1500]}", view=view)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.warning("質問送信エラー: %s", e)
        permission_events.pop(request_id, None)
        permission_results.pop(request_id, None)
        return web.json_response(make_quick_allow(hook_type))

    try:
        await asyncio.wait_for(event.wait(), timeout=600)
    except asyncio.TimeoutError:
        pass

    result = permission_results.pop(request_id, None) or make_quick_allow(hook_type)
    permission_events.pop(request_id, None)
    return web.json_response(result)


def make_quick_allow(hook_type: str) -> dict:
    if hook_type == "PermissionRequest":
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        }
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


# Claude Code が現在送る Notification の notification_type
NOTIFICATION_ICONS = {
    "permission_prompt": "🔐",
    "idle_prompt": "💤",
    "elicitation_dialog": "📝",
    "elicitation_complete": "📨",
    "elicitation_response": "📬",
    "auth_success": "🔑",
}


@web.middleware
async def auth_middleware(request: web.Request, handler):
    # /health は認証不要
    if request.path == "/health":
        return await handler(request)
    token = request.headers.get(HOOK_AUTH_HEADER, "")
    if not secrets.compare_digest(token, HOOK_AUTH_TOKEN):
        return web.json_response({"error": "unauthorized"}, status=401)
    return await handler(request)


async def handle_notification(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({}, status=400)

    message = data.get("message", "") or ""
    title = data.get("title", "") or ""
    ntype = data.get("notification_type", "") or ""
    thread_id = data.get("thread_id", "")

    thread = None
    if thread_id:
        try:
            thread = bot.get_channel(int(thread_id))
        except (ValueError, TypeError):
            thread = None
    if thread is None:
        return web.json_response({})

    icon = NOTIFICATION_ICONS.get(ntype, "🔔")
    header = f"{icon} **{title or '通知'}**"
    if ntype:
        header += f"  `{ntype}`"
    parts = [header]
    if message:
        parts.append(str(message)[:1800])
    await safe_send(thread, "\n".join(parts))

    return web.json_response({})


async def handle_permission_request(request: web.Request) -> web.Response:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response(make_quick_allow("PreToolUse"), status=400)

    hook_type = data.get("hook_type", "PreToolUse")
    tool_name = data.get("tool_name", "unknown")
    tool_input = data.get("tool_input", {}) or {}
    thread_id = data.get("thread_id", "")
    sensitive = bool(data.get("sensitive", False))

    # AskUserQuestion は選択肢ボタンとして扱う（「常に許可」の対象外）
    if tool_name == "AskUserQuestion":
        return await _handle_ask_user_question(tool_input, thread_id, hook_type)

    # sensitive path は「常に許可」を無視（毎回確認）
    if not sensitive and thread_id in allowed_tools and tool_name in allowed_tools[thread_id]:
        return web.json_response(make_quick_allow(hook_type))

    request_id = str(uuid.uuid4())
    event = asyncio.Event()
    permission_events[request_id] = event

    thread = None
    if thread_id:
        try:
            thread = bot.get_channel(int(thread_id))
        except (ValueError, TypeError):
            thread = None

    if thread is None:
        # スレッド消失/未指定 → 詰まないように許可
        permission_events.pop(request_id, None)
        return web.json_response(make_quick_allow(hook_type))

    try:
        detail = format_tool_detail(tool_name, tool_input)
        view = PermissionView(request_id, tool_name, thread_id, hook_type)
        if sensitive:
            header = (
                f"⚠️ **センシティブパスへの書き込み要求: `{tool_name}`**\n"
                f"（`--dangerously-skip-permissions` でも保護されるパス。"
                f"承認するとブリッジが直接書き込みます）"
            )
        else:
            header = f"🔐 **権限リクエスト: `{tool_name}`**"
        await thread.send(f"{header}\n{detail}", view=view)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.warning("権限リクエスト送信エラー: %s", e)
        permission_events.pop(request_id, None)
        permission_results.pop(request_id, None)
        return web.json_response(make_quick_allow(hook_type))

    try:
        await asyncio.wait_for(event.wait(), timeout=600)
    except asyncio.TimeoutError:
        pass

    result = permission_results.pop(request_id, None) or make_quick_allow(hook_type)
    permission_events.pop(request_id, None)
    return web.json_response(result)


async def handle_health(_request: web.Request) -> web.Response:
    # bot.user 等は出さない（認証無しエンドポイントなので情報漏洩を最小化）
    return web.json_response({"ok": True, "ready": bot.is_ready()})


# AppRunner を保持して bot 終了時に cleanup できるようにする
_hook_runner: web.AppRunner | None = None


async def start_hook_server():
    """フックサーバーを起動。on_ready の再発火（gateway 再接続）で二重起動しないようガードする。"""
    global _hook_runner
    if _hook_runner is not None:
        # 既に起動済み
        return
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_post("/permission", handle_permission_request)
    app.router.add_post("/notification", handle_notification)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", HOOK_PORT)
        await site.start()
    except OSError as e:
        await runner.cleanup()
        raise SystemExit(
            f"フックサーバー起動失敗 (port {HOOK_PORT}): {e}\n"
            f"既存プロセスがポートを掴んでいるか、HOOK_PORT を別の番号に変えてください"
            + (f"\nLinux で HOOK_PORT={HOOK_PORT} は特権ポートです。1024 以上を使ってください"
               if HOOK_PORT < 1024 else "")
        )
    _hook_runner = runner
    log.info("Hook サーバー起動: http://127.0.0.1:%d", HOOK_PORT)


async def stop_hook_server():
    global _hook_runner
    if _hook_runner is not None:
        try:
            await _hook_runner.cleanup()
        except Exception as e:
            log.warning("フックサーバー cleanup 失敗: %s", e)
        _hook_runner = None


_HOOK_SETTINGS_PATH: str | None = None


def build_hook_settings() -> str:
    """フック設定 JSON を生成して書き込む。並行書き込みを避けるため atomic rename。"""
    base_dir = Path(__file__).parent.resolve()
    pretooluse_script = str(base_dir / "hook_pretooluse.py")
    permission_script = str(base_dir / "hook_permission_request.py")
    notification_script = str(base_dir / "hook_notification.py")
    settings = {
        "hooks": {
            "PreToolUse": [{
                "hooks": [{
                    "type": "command",
                    "command": f'"{sys.executable}" "{pretooluse_script}"',
                    "timeout": 600,
                }],
            }],
            "PermissionRequest": [{
                "hooks": [{
                    "type": "command",
                    "command": f'"{sys.executable}" "{permission_script}"',
                    "timeout": 600,
                }],
            }],
            "Notification": [{
                "hooks": [{
                    "type": "command",
                    "command": f'"{sys.executable}" "{notification_script}"',
                    "timeout": 30,
                }],
            }],
        },
    }
    settings_path = base_dir / ".claude_hook_settings.json"
    tmp_path = settings_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        os.replace(tmp_path, settings_path)
    except OSError as e:
        raise SystemExit(
            f"フック設定ファイルの書き込みに失敗しました ({settings_path}): {e}\n"
            f"ディスク容量とディレクトリの書き込み権限を確認してください"
        )
    return str(settings_path)


def _ensure_hook_settings_path() -> str:
    """起動時に1回だけ生成。並行 run_claude による race を回避する。"""
    global _HOOK_SETTINGS_PATH
    if _HOOK_SETTINGS_PATH is None:
        _HOOK_SETTINGS_PATH = build_hook_settings()
    return _HOOK_SETTINGS_PATH


# ==============================
# セッション管理
# ==============================

def load_sessions() -> dict:
    # 前回 atomic rename が完了せず .tmp が残っているケースを救済
    tmp = SESSIONS_FILE.with_suffix(".json.tmp")
    if tmp.exists():
        try:
            if not SESSIONS_FILE.exists() or tmp.stat().st_mtime > SESSIONS_FILE.stat().st_mtime:
                log.warning("%s が残存。本体より新しいので復旧します", tmp)
                os.replace(tmp, SESSIONS_FILE)
            else:
                tmp.unlink(missing_ok=True)
        except OSError as e:
            log.warning("sessions.json.tmp 処理失敗: %s", e)

    if not SESSIONS_FILE.exists():
        return {}
    try:
        data = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            log.warning("%s が辞書形式ではありません。リセットします", SESSIONS_FILE)
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        log.warning("%s 読み込み失敗 (%s)。空のセッションで開始", SESSIONS_FILE, e)
        return {}


def save_sessions(sessions: dict):
    # 中断時に壊れないよう一旦 .tmp に書いて rename
    tmp = SESSIONS_FILE.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(sessions, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, SESSIONS_FILE)
    except OSError as e:
        log.error("セッション保存失敗: %s", e)


# ==============================
# Claude Code 実行
# ==============================

ANSI_RE = re.compile(r"\x1B\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def _extract_images_from_blocks(blocks, images: list):
    if not isinstance(blocks, list):
        return
    for b in blocks:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "image":
            src = b.get("source", {}) or {}
            if src.get("type") == "base64":
                try:
                    data = base64.b64decode(src.get("data", ""))
                except (ValueError, TypeError) as e:
                    log.warning("画像デコードエラー: %s", e)
                    continue
                if not data:
                    log.warning("0バイト画像をスキップ")
                    continue
                if len(data) > DISCORD_FILE_LIMIT:
                    log.warning("画像が大きすぎ (%d bytes), スキップ", len(data))
                    continue
                media = src.get("media_type", "image/png")
                ext = media.split("/")[-1] or "png"
                images.append((data, f"image_{len(images)}.{ext}"))
        elif btype == "tool_result":
            _extract_images_from_blocks(b.get("content"), images)


# --resume が失敗したことを示唆する stderr パターン
_RESUME_FAIL_PATTERNS = (
    "session not found",
    "no such session",
    "could not find session",
    "session does not exist",
    "session id not found",
)


def _stderr_indicates_resume_fail(stderr: str) -> bool:
    if not stderr:
        return False
    s = stderr.lower()
    return any(p in s for p in _RESUME_FAIL_PATTERNS)


def parse_stream_events(events: list, stderr: str, session_id: str | None) -> tuple[str, str | None, list, bool]:
    """戻り値: (output, new_session_id, images, is_error)"""
    new_session_id = session_id
    final_text = ""
    fallback_text_parts: list[str] = []
    images: list[tuple[bytes, str]] = []
    is_error = False
    error_msg = ""
    result_subtype = ""
    saw_result = False

    for ev in events:
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")

        if etype == "system":
            # init 以外の system サブタイプ (api_retry/compact_boundary 等) でも session_id を更新
            new_session_id = ev.get("session_id") or new_session_id

        elif etype == "assistant":
            msg = ev.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        fallback_text_parts.append(b.get("text", "") or "")
                    elif b.get("type") == "image":
                        _extract_images_from_blocks([b], images)
            elif isinstance(content, str):
                # SDK 仕様で content が string になる場合があるので拾う
                fallback_text_parts.append(content)

        elif etype == "user":
            msg = ev.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            _extract_images_from_blocks(content, images)

        elif etype == "result":
            saw_result = True
            new_session_id = ev.get("session_id") or new_session_id
            is_error = bool(ev.get("is_error"))
            result_subtype = ev.get("subtype", "") or ""
            r = ev.get("result", "")
            if isinstance(r, str):
                final_text = r
            error_msg = ev.get("error", "") or ""

        # tool_use / tool_result / partial_message / hook_event / その他未知タイプは無視

    # subtype が error_* なら成功でも実質失敗として扱う
    if result_subtype.startswith("error_"):
        is_error = True
        if not error_msg:
            error_msg = f"Claude Code: {result_subtype}"

    # result イベントすら来なかった = subprocess が起動失敗 or auth エラー等
    if not saw_result and stderr:
        is_error = True

    output = final_text or "\n".join(p for p in fallback_text_parts if p).strip()
    if is_error and not output:
        output = f"エラー: {error_msg or stderr or '不明なエラー'}"
    elif is_error and stderr:
        # 正常 output がある場合でも stderr を補足で添える (1500→2500字)
        output = f"{output}\n\n---\nstderr (抜粋): {stderr.strip()[:1500]}"
    if not output and stderr:
        # auth エラー等を可視化（2500字まで）
        output = f"エラー: {stderr.strip()[:2500]}"
    if not output and not images:
        output = "（Claude Codeからの応答が空でした。再度試してください）"

    return output.strip(), new_session_id, images, is_error


def _drain_stderr(stream, sink: list):
    """stderr を別スレッドで読み、stdout 待機中の deadlock を防ぐ"""
    try:
        for line in iter(stream.readline, b""):
            if not line:
                break
            sink.append(line)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _run_claude_subprocess(
    args: list,
    env: dict,
    cwd: str | None,
    thread_id: int | None = None,
    event_queue: "asyncio.Queue | None" = None,
    main_loop: "asyncio.AbstractEventLoop | None" = None,
):
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            bufsize=STREAM_LINE_BUFSIZE,
        )
    except FileNotFoundError:
        return [], (
            f"claude CLI が見つかりません ({args[0]!r})。"
            f"インストール状況と PATH、または環境変数 CLAUDE_BIN を確認してください"
        ), False
    except OSError as e:
        return [], f"claude 起動失敗: {e}", False

    _register_proc(proc, thread_id)
    timed_out_flag = [False]

    def _kill_on_timeout():
        timed_out_flag[0] = True
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(HARD_TIMEOUT, _kill_on_timeout)
    timer.start()

    stderr_chunks: list = []
    stderr_thread = threading.Thread(
        target=_drain_stderr, args=(proc.stderr, stderr_chunks), daemon=True,
    )
    stderr_thread.start()

    def _push_event(ev):
        if event_queue is None or main_loop is None:
            return
        try:
            main_loop.call_soon_threadsafe(event_queue.put_nowait, ev)
        except RuntimeError:
            # loop closed
            pass

    events: list = []
    try:
        # readline は LINE_BUFSIZE まで読む。画像 base64 を含む長行に対応
        for raw_line in iter(proc.stdout.readline, b""):
            line = raw_line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            events.append(ev)
            _push_event(ev)
    except (OSError, ValueError) as e:
        log.warning("stream-json 読み取りエラー: %s", e)
    finally:
        # cancel() の戻り値: True=キャンセル成功, False=既にfired
        if not timer.cancel():
            pass
        else:
            timed_out_flag[0] = False
        try:
            proc.stdout.close()
        except Exception:
            pass

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        threading.Thread(target=lambda: _safe_wait(proc), daemon=True).start()

    stderr_thread.join(timeout=10)
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    _unregister_thread_proc(thread_id)

    return events, stderr, timed_out_flag[0]


def _safe_wait(proc: subprocess.Popen):
    try:
        proc.wait()
    except Exception:
        pass


def _build_claude_args(prompt: str, session_id: str | None, settings_path: str) -> list[str]:
    args = [
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json",
        "--verbose",
    ]
    if SKIP_PERMISSIONS:
        args.append("--dangerously-skip-permissions")
    elif PERMISSION_MODE:
        args.extend(["--permission-mode", PERMISSION_MODE])

    args.extend(["--settings", settings_path])

    # MAX_TURNS は "0" が「無制限」を意図する誤設定なので除外。1以上のみ渡す。
    if MAX_TURNS and MAX_TURNS != "0":
        args.extend(["--max-turns", MAX_TURNS])
    if MAX_BUDGET_USD and MAX_BUDGET_USD not in ("0", "0.0"):
        args.extend(["--max-budget-usd", MAX_BUDGET_USD])
    if session_id:
        args.extend(["--resume", session_id])
    # prompt が `--` で始まる場合に CLI パーサが引数と誤認しないよう区切る
    args.append("--")
    args.append(prompt)
    return args


# ツール名 → 進捗表示用絵文字
_TOOL_EMOJI = {
    "Read": "📖", "Glob": "🔍", "Grep": "🔎",
    "Bash": "⚡", "BashOutput": "📤", "KillShell": "🛑",
    "Write": "📝", "Edit": "✏️", "MultiEdit": "✏️", "NotebookEdit": "📓",
    "WebFetch": "🌐", "WebSearch": "🌐",
    "Task": "🤖", "Agent": "🤖",
    "TaskCreate": "📋", "TaskUpdate": "📋", "TaskList": "📋", "TaskGet": "📋",
    "TaskOutput": "📋", "TaskStop": "📋",
    "Skill": "🛠️", "ToolSearch": "🔧", "Monitor": "👀",
    "AskUserQuestion": "❓", "TodoWrite": "✅",
    "EnterPlanMode": "🗺️", "ExitPlanMode": "🗺️",
    "EnterWorktree": "🌿", "ExitWorktree": "🌿",
    "ScheduleWakeup": "⏰",
}


def _format_tool_use_line(name: str, inp: dict) -> str:
    emoji = _TOOL_EMOJI.get(name, "🔧")
    if not isinstance(inp, dict):
        return f"{emoji} `{name}`"
    if name == "Bash":
        cmd = (inp.get("command") or "").replace("\n", " ")
        return f"{emoji} `{name}` `{cmd[:100]}`"
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        # ホーム配下を ~ に省略
        try:
            h = str(Path.home())
            if path.startswith(h):
                path = "~" + path[len(h):]
        except Exception:
            pass
        return f"{emoji} `{name}` `{path[:100]}`"
    if name == "Glob":
        return f"{emoji} `{name}` `{(inp.get('pattern') or '')[:100]}`"
    if name == "Grep":
        pat = (inp.get("pattern") or "")[:60]
        path = inp.get("path") or inp.get("glob") or ""
        return f"{emoji} `{name}` `{pat}`" + (f" in `{path}`" if path else "")
    if name in ("WebFetch",):
        return f"{emoji} `{name}` `{(inp.get('url') or '')[:100]}`"
    if name in ("WebSearch",):
        return f"{emoji} `{name}` `{(inp.get('query') or '')[:100]}`"
    if name in ("Task", "Agent"):
        desc = inp.get("description") or inp.get("prompt") or ""
        return f"{emoji} `{name}` {desc[:100]}"
    if name == "TaskCreate":
        return f"{emoji} `{name}` {(inp.get('subject') or inp.get('description') or '')[:100]}"
    if name == "Skill":
        return f"{emoji} `{name}` `{(inp.get('skill') or '')[:60]}`"
    if name == "AskUserQuestion":
        qs = inp.get("questions") or []
        if isinstance(qs, list) and qs:
            return f"{emoji} `{name}` {str(qs[0].get('question', ''))[:100]}"
        return f"{emoji} `{name}`"
    return f"{emoji} `{name}`"


def _format_event_for_progress(ev: dict) -> str | None:
    """stream-json イベントから進捗 1 行を生成。None は表示しない。"""
    etype = ev.get("type")
    if etype == "assistant":
        msg = ev.get("message") or {}
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    return _format_tool_use_line(b.get("name", ""), b.get("input") or {})
    elif etype == "system" and ev.get("subtype") == "compact_boundary":
        return "🗜️ `compact` (会話圧縮)"
    return None


async def _progress_consumer(thread, event_queue: asyncio.Queue, header: str = "🔧 **進捗**"):
    """イベントキューから進捗を消費してメッセージ編集"""
    progress_msg = None
    lines: list[str] = []
    last_edit = 0.0
    MIN_INTERVAL = 1.5  # Discord rate-limit 対策

    async def _flush(final: bool = False):
        nonlocal progress_msg
        if not lines:
            return
        body = header + "\n" + "\n".join(lines[-20:])
        if len(body) > 1900:
            body = body[:1900] + "…"
        if progress_msg is None:
            progress_msg = await safe_send(thread, body)
        else:
            edited = await safe_edit(progress_msg, body)
            if edited is None and final:
                # 編集失敗 (削除等) → 新規送信
                progress_msg = await safe_send(thread, body)

    try:
        while True:
            ev = await event_queue.get()
            if ev is None:
                break
            line = _format_event_for_progress(ev)
            if not line:
                continue
            lines.append(line)
            now = asyncio.get_event_loop().time()
            if now - last_edit >= MIN_INTERVAL:
                last_edit = now
                await _flush()
        await _flush(final=True)
    except asyncio.CancelledError:
        await _flush(final=True)
        raise
    return progress_msg


def _accumulate_usage(thread_id: int, events: list):
    """result イベントから usage 情報を抽出して累積"""
    stats = usage_stats.setdefault(thread_id, {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_tokens": 0, "cache_read_tokens": 0,
        "cost_usd": 0.0, "turns": 0,
    })
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("type") != "result":
            continue
        stats["turns"] += 1
        usage = ev.get("usage") or {}
        if isinstance(usage, dict):
            stats["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
            stats["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
            stats["cache_creation_tokens"] += int(usage.get("cache_creation_input_tokens", 0) or 0)
            stats["cache_read_tokens"] += int(usage.get("cache_read_input_tokens", 0) or 0)
        cost = ev.get("total_cost_usd") or ev.get("cost_usd") or 0
        try:
            stats["cost_usd"] += float(cost)
        except (TypeError, ValueError):
            pass


async def run_claude(
    prompt: str,
    session_id: str | None = None,
    thread: discord.Thread | None = None,
    thread_title: str | None = None,
    cwd: str | None = None,
) -> tuple[str | None, str | None, list, bool]:
    """戻り値: (response_text_or_None, new_session_id, images, is_error)
    response_text が None のときは placeholder で送信済み。"""
    settings_path = _ensure_hook_settings_path()

    if not session_id and thread_title:
        prompt = f"[スレッドタイトル: {thread_title}]\n\n{prompt}"
    args = _build_claude_args(prompt, session_id, settings_path)

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["HOOK_PORT"] = str(HOOK_PORT)
    env["BRIDGE_SKIP_PERMISSIONS"] = "true" if SKIP_PERMISSIONS else "false"
    env["BRIDGE_AUTH_TOKEN"] = HOOK_AUTH_TOKEN
    if thread:
        env["DISCORD_THREAD_ID"] = str(thread.id)

    placeholder = None
    notify_task = None
    if thread and SOFT_TIMEOUT > 0:
        async def _soft_timeout_notify():
            nonlocal placeholder
            await asyncio.sleep(SOFT_TIMEOUT)
            placeholder = await safe_send(
                thread, f"まだ処理中です（{SOFT_TIMEOUT // 60}分経過）… 終わったら更新します",
            )
        notify_task = asyncio.create_task(_soft_timeout_notify())

    # ツール使用ストリーミング
    event_queue: asyncio.Queue | None = None
    main_loop: asyncio.AbstractEventLoop | None = None
    progress_task: asyncio.Task | None = None
    if thread:
        event_queue = asyncio.Queue()
        main_loop = asyncio.get_running_loop()
        progress_task = asyncio.create_task(_progress_consumer(thread, event_queue))

    thread_id = thread.id if thread else None
    events, stderr, timed_out = await asyncio.to_thread(
        _run_claude_subprocess, args, env, cwd, thread_id, event_queue, main_loop,
    )

    # 進捗ストリーマー停止
    if event_queue is not None:
        await event_queue.put(None)
    if progress_task is not None:
        try:
            await asyncio.wait_for(progress_task, timeout=10)
        except asyncio.TimeoutError:
            progress_task.cancel()
        except asyncio.CancelledError:
            pass

    # usage 累積（thread コンテキストありのみ）
    if thread_id is not None:
        _accumulate_usage(thread_id, events)

    if notify_task and not notify_task.done():
        notify_task.cancel()

    if timed_out:
        msg = f"タイムアウトしました（{HARD_TIMEOUT // 60}分超過、強制終了）"
        if placeholder:
            await safe_edit(placeholder, msg)
            return None, session_id, [], True
        return msg, session_id, [], True

    output, new_session_id, images, is_error = parse_stream_events(events, stderr, session_id)

    # --resume が「セッション無し」で失敗した場合は new_session_id を None にして呼び出し側で破棄させる
    if session_id and is_error and _stderr_indicates_resume_fail(stderr):
        log.warning("--resume 失敗を検知。session_id を破棄: %s", session_id)
        new_session_id = None

    if placeholder:
        chunks = split_message(output, 2000)
        edited = await safe_edit(placeholder, chunks[0])
        if edited is None:
            # placeholder が削除済 等 → chunks[0] も新規送信
            await safe_send(thread, chunks[0])
        for chunk in chunks[1:]:
            await safe_send(thread, chunk)
        for img_data, filename in images:
            try:
                await safe_send(thread, file=discord.File(io.BytesIO(img_data), filename=filename))
            except (OSError, ValueError) as e:
                log.warning("画像送信準備エラー: %s", e)
        return None, new_session_id, [], is_error

    return output, new_session_id, images, is_error


# ==============================
# タグ管理
# ==============================

async def get_or_create_tag(forum: discord.ForumChannel, name: str) -> discord.ForumTag | None:
    for tag in forum.available_tags:
        if tag.name == name:
            return tag
    if len(forum.available_tags) >= 20:
        log.error(
            "フォーラムタグが 20 個上限に達しているため `%s` を作成できません。"
            "不要なタグを Discord 側で削除してください",
            name,
        )
        return None
    new_tags = list(forum.available_tags) + [discord.ForumTag(name=name)]
    try:
        await forum.edit(available_tags=new_tags)
    except (discord.Forbidden, discord.HTTPException) as e:
        log.warning("フォーラムタグ追加失敗: %s", e)
        return None
    try:
        forum = await forum.guild.fetch_channel(forum.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.warning("フォーラム再取得失敗: %s", e)
        return None
    for tag in forum.available_tags:
        if tag.name == name:
            return tag
    return None


async def set_thread_tag(thread: discord.Thread, tag_name: str):
    try:
        forum = thread.parent
        if not forum:
            try:
                forum = await thread.guild.fetch_channel(thread.parent_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning("親フォーラム取得失敗: %s", e)
                return
        tag = await get_or_create_tag(forum, tag_name)
        if not tag:
            return
        status_names = {TAG_RUNNING, TAG_COMPLETED, TAG_ERROR}
        keep_tags = [t for t in thread.applied_tags if t.name not in status_names]
        keep_tags.append(tag)
        try:
            await thread.edit(applied_tags=keep_tags[:5])
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("タグ適用失敗: %s", e)
    except Exception as e:
        log.warning("タグ設定エラー: %s", e)


# ==============================
# ログ
# ==============================

async def send_log(guild: discord.Guild, user: str, thread_name: str, prompt: str, result: str, status: str):
    if not LOG_CHANNEL_ID:
        return
    try:
        log_ch = guild.get_channel(LOG_CHANNEL_ID)
        if not log_ch:
            log_ch = await guild.fetch_channel(LOG_CHANNEL_ID)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        embed = discord.Embed(
            title=f"[{status}] {thread_name}",
            color={
                TAG_RUNNING: discord.Color.yellow(),
                TAG_COMPLETED: discord.Color.green(),
                TAG_ERROR: discord.Color.red(),
            }.get(status, discord.Color.greyple()),
            # Discord は UTC naive を UTC と解釈する。aware にして渡し、TZ ずれを防ぐ
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="ユーザー", value=user, inline=True)
        embed.add_field(name="時刻", value=now, inline=True)
        embed.add_field(name="プロンプト", value=(prompt or "（空）")[:1024], inline=False)
        if result:
            embed.add_field(name="応答", value=result[:1024], inline=False)
        await log_ch.send(embed=embed)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.warning("ログ送信エラー: %s", e)


# ==============================
# メッセージ分割送信
# ==============================

async def send_response(channel: discord.Thread, text: str, images: list[tuple[bytes, str]] | None = None):
    if not text and not images:
        await safe_send(channel, "（空の応答）")
        return
    if text:
        for chunk in split_message(text, 2000):
            await safe_send(channel, chunk)
    if images:
        for img_data, filename in images:
            try:
                await safe_send(channel, file=discord.File(io.BytesIO(img_data), filename=filename))
            except (OSError, ValueError) as e:
                log.warning("画像送信準備エラー: %s", e)


_CODE_FENCE_OPEN_RE = re.compile(r"```([A-Za-z0-9_+\-]*)", re.MULTILINE)


def split_message(text: str, limit: int = 2000) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut == -1 or cut < limit // 2:
            cut = limit
        chunk = text[:cut]
        text = text[cut:].lstrip("\n")
        if chunk.count("```") % 2 == 1:
            # コードブロックを跨いだ。最後の開きフェンスの言語タグを次チャンクに引き継ぐ
            fences = _CODE_FENCE_OPEN_RE.findall(chunk)
            lang = fences[-1] if fences and len(fences) % 2 == 1 else ""
            chunk += "\n```"
            text = f"```{lang}\n" + text
        chunks.append(chunk)

    return chunks


# ==============================
# Bot setup
# ==============================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

try:
    MAX_CONCURRENT_RUNS = int(os.getenv("MAX_CONCURRENT_RUNS", "5"))
except ValueError:
    raise SystemExit(f"MAX_CONCURRENT_RUNS は整数である必要があります: {os.getenv('MAX_CONCURRENT_RUNS')!r}")
if MAX_CONCURRENT_RUNS < 1:
    raise SystemExit(f"MAX_CONCURRENT_RUNS は 1 以上必要 (現値: {MAX_CONCURRENT_RUNS})。0/負は全ワーカーが詰みます")

thread_queues: dict[int, asyncio.Queue] = {}
thread_workers: dict[int, asyncio.Task] = {}
concurrency_sem = asyncio.Semaphore(MAX_CONCURRENT_RUNS)
sessions_lock = asyncio.Lock()
_worker_lock = asyncio.Lock()


async def get_session_id(thread_key: str) -> str | None:
    async with sessions_lock:
        return load_sessions().get(thread_key)


async def set_session_id(thread_key: str, session_id: str):
    async with sessions_lock:
        sessions = load_sessions()
        sessions[thread_key] = session_id
        save_sessions(sessions)


async def enqueue_for_thread(
    thread: discord.Thread,
    author: "discord.Message | discord.Interaction | str",
    prompt: str,
    cwd: str | None = None,
    image_paths: list[Path] | None = None,
):
    # author: Message / Interaction / str いずれも許可
    if isinstance(author, discord.Message):
        author_name = str(author.author)
    elif isinstance(author, discord.Interaction):
        author_name = str(author.user)
    else:
        author_name = str(author)
    tid = thread.id
    last_prompts[tid] = prompt
    async with _worker_lock:
        q = thread_queues.setdefault(tid, asyncio.Queue())
        q.put_nowait((thread, author_name, prompt, cwd, image_paths or []))
        worker = thread_workers.get(tid)
        if worker is None or worker.done():
            thread_workers[tid] = asyncio.create_task(process_thread(tid))


async def process_thread(tid: int):
    q = thread_queues[tid]
    while True:
        while True:
            try:
                thread, author_name, prompt, cwd, image_paths = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            async with concurrency_sem:
                try:
                    await _run_one(thread, author_name, prompt, cwd, image_paths)
                except Exception:
                    log.exception("ワーカー実行中に想定外エラー")
                finally:
                    q.task_done()
        async with _worker_lock:
            if q.empty():
                thread_queues.pop(tid, None)
                thread_workers.pop(tid, None)
                # 長期運用で per-thread の allowed_tools / 並列キューが残らないようクリア
                allowed_tools.pop(str(tid), None)
                return


async def _run_one(
    thread: discord.Thread,
    author_name: str,
    prompt: str,
    cwd: str | None,
    image_paths: list[Path] | None = None,
):
    status = TAG_RUNNING
    result = ""
    try:
        await set_thread_tag(thread, TAG_RUNNING)
        await send_log(
            thread.guild, author_name,
            thread.name, prompt, "", TAG_RUNNING,
        )

        async def _do_work() -> tuple[str, bool]:
            thread_key = str(thread.id)
            session_id = await get_session_id(thread_key)
            # session_id が不正形式または .jsonl が存在しなければ resume を諦めて新規開始
            if session_id and not is_valid_session_id(session_id):
                log.warning("不正形式の session_id を破棄: %s", session_id)
                session_id = None
            # /bridge-cwd で固定された cwd を最優先
            fixed_cwd = thread_cwds.get(thread.id)
            run_cwd = fixed_cwd or cwd or (find_session_cwd(session_id) if session_id else None) or str(Path.home())
            result_inner, new_session_id, images, is_error = await run_claude(
                prompt, session_id, thread, thread.name, cwd=run_cwd,
            )
            # --resume 失敗時は session を破棄して次回新規開始
            if session_id and new_session_id is None and is_error:
                async with sessions_lock:
                    sessions = load_sessions()
                    if sessions.pop(thread_key, None) is not None:
                        save_sessions(sessions)
                log.info("壊れた session_id を sessions.json から削除: %s", session_id)
            elif new_session_id and new_session_id != session_id:
                await set_session_id(thread_key, new_session_id)
                if not session_id:
                    await safe_send(thread, f"🆔 Session: `{new_session_id}`")
            if result_inner is not None or images:
                await send_response(thread, result_inner or "", images)
            return result_inner or "", is_error

        # typing() の Forbidden 等は __aenter__ で発生する。タイピング表示無しで継続。
        try:
            async with thread.typing():
                result, is_err = await _do_work()
        except (discord.Forbidden, discord.HTTPException):
            result, is_err = await _do_work()
        status = TAG_ERROR if is_err else TAG_COMPLETED

    except Exception as e:
        log.exception("_run_one エラー")
        result = str(e)
        status = TAG_ERROR
        await safe_send(thread, f"エラーが発生しました: {e}")
    finally:
        if image_paths:
            cleanup_attachments(image_paths)
        await set_thread_tag(thread, status)
        await send_log(
            thread.guild, author_name,
            thread.name, prompt, result, status,
        )


@bot.command(name="sync")
async def sync_commands(ctx: commands.Context):
    if str(ctx.author.id) not in ALLOWED_USERS:
        return
    synced = await bot.tree.sync()
    await safe_send(ctx.channel, f"✅ {len(synced)}個のコマンドを同期しました")


async def _validate_startup_targets():
    """フォーラム/ログチャンネルが見えるかを起動時に確認"""
    try:
        ch = bot.get_channel(FORUM_CHANNEL_ID) or await bot.fetch_channel(FORUM_CHANNEL_ID)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        log.error("FORUM_CHANNEL_ID=%s にアクセスできません: %s", FORUM_CHANNEL_ID, e)
        return
    if not isinstance(ch, discord.ForumChannel):
        log.error("FORUM_CHANNEL_ID=%s はフォーラムではありません (type=%s)", FORUM_CHANNEL_ID, type(ch).__name__)

    if LOG_CHANNEL_ID:
        try:
            await bot.fetch_channel(LOG_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.warning("LOG_CHANNEL_ID=%s にアクセスできません: %s", LOG_CHANNEL_ID, e)

    if shutil.which(CLAUDE_BIN) is None and not Path(CLAUDE_BIN).exists():
        log.warning(
            "%s が PATH 上に見つかりません。実行時に FileNotFoundError になる可能性があります",
            CLAUDE_BIN,
        )


_synced_once = False


@bot.event
async def on_ready():
    global _synced_once
    # 起動時に hook settings を一度だけ生成（並行 run_claude の race を防ぐ）
    _ensure_hook_settings_path()
    # start_hook_server は二重起動ガード内蔵
    await start_hook_server()
    # tree.sync() は global で 1h あたり 2 回までしか許されない。再接続で連発しないよう初回だけ
    synced = []
    if not _synced_once:
        try:
            synced = await bot.tree.sync()
            _synced_once = True
        except discord.HTTPException as e:
            log.warning("スラッシュコマンド同期失敗: %s", e)
    log.info("Bot起動: %s (%d個のコマンドを同期)", bot.user, len(synced))
    log.info("フォーラムチャンネルID: %s", FORUM_CHANNEL_ID)
    log.info("ログチャンネルID: %s", LOG_CHANNEL_ID)
    log.info("許可ユーザー: %s", ALLOWED_USERS)
    log.info(
        "権限モード: %s",
        "全スキップ" if SKIP_PERMISSIONS else f"Discord承認 (port {HOOK_PORT})"
        + (f" / --permission-mode {PERMISSION_MODE}" if PERMISSION_MODE else ""),
    )
    await _validate_startup_targets()


async def download_attachments(message: discord.Message) -> list[Path]:
    downloaded = []
    for i, att in enumerate(message.attachments):
        # API直叩きで `../` 等が来てもパストラバーサルを許さない
        safe_name = Path(att.filename).name
        ext = Path(safe_name).suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            continue
        # 同名添付ファイル衝突回避のため index 付与
        save_path = TEMP_DIR / f"{message.id}_{i}_{safe_name}"
        # 念のため save_path が TEMP_DIR 配下に収まっているか再検証
        try:
            save_path.resolve().relative_to(TEMP_DIR.resolve())
        except ValueError:
            log.warning("save_path が TEMP_DIR 外: %s", save_path)
            continue
        try:
            await att.save(save_path)
            downloaded.append(save_path)
            log.info("画像ダウンロード: %s", save_path)
        except (discord.HTTPException, OSError) as e:
            log.warning("画像ダウンロードエラー (%s): %s", att.filename, e)
    return downloaded


def cleanup_attachments(paths: list[Path]):
    for p in paths:
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            log.warning("一時画像削除失敗 (%s): %s", p, e)


def decode_project_path(encoded: str) -> str | None:
    """プロジェクトディレクトリ名からファイルシステムパスを復元する。
    Windows: 'C--Users-user-discord-claude-bridge' → 'C:\\Users\\user\\discord-claude-bridge'
    POSIX  : '-home-user-myproject' (元 `/home/user/myproject`) → '/home/user/myproject'
    """
    if not encoded:
        return None

    def resolve(segments: list[str], idx: int, current: str) -> str | None:
        if idx >= len(segments):
            return current if os.path.isdir(current) else None
        for end in range(len(segments), idx, -1):
            segment = "-".join(segments[idx:end])
            candidate = os.path.join(current, segment)
            if os.path.isdir(candidate):
                result = resolve(segments, end, candidate)
                if result:
                    return result
        return None

    # Windows 形式: drive--rest
    if "--" in encoded:
        parts = encoded.split("--", 1)
        if len(parts) == 2 and len(parts[0]) == 1:
            drive = parts[0] + ":"
            segments = parts[1].split("-")
            return resolve(segments, 0, drive + os.sep)

    # POSIX 形式: 先頭ハイフン or 単にハイフン区切り → / から復号
    if sys.platform != "win32":
        s = encoded.lstrip("-")
        segments = s.split("-")
        return resolve(segments, 0, "/")

    return None


def find_session_cwd(session_id: str) -> str | None:
    if not session_id or not is_valid_session_id(session_id):
        return None
    proj_dir = Path.home() / ".claude" / "projects"
    if not proj_dir.exists():
        return None
    try:
        for d in proj_dir.iterdir():
            if not d.is_dir():
                continue
            session_file = d / f"{session_id}.jsonl"
            if session_file.exists():
                resolved = decode_project_path(d.name)
                if resolved:
                    return resolved
    except OSError as e:
        log.warning("プロジェクトディレクトリ走査失敗: %s", e)
    return None


def _encode_home_path() -> str:
    """`Path.home()` を Claude Code のプロジェクトディレクトリ命名規則でエンコードした文字列を返す。
    例: C:\\Users\\user → C--Users-user, /home/user → -home-user
    """
    home = str(Path.home())
    if sys.platform == "win32" and len(home) > 1 and home[1] == ":":
        drive, rest = home[0], home[2:]
        encoded = drive + "--" + rest.replace("\\", "-").replace("/", "-")
    else:
        encoded = home.replace("/", "-")
    return encoded.lstrip("-")  # 先頭ハイフン除去


def get_recent_sessions(limit: int = 10, exclude_discord: bool = False) -> list[dict]:
    used_sids = set()
    if exclude_discord:
        try:
            used_sids = set(load_sessions().values())
        except Exception:
            pass

    proj_dir = Path.home() / ".claude" / "projects"
    if not proj_dir.exists():
        return []
    results = []
    try:
        proj_iter = list(proj_dir.iterdir())
    except OSError as e:
        log.warning("プロジェクトディレクトリ列挙失敗: %s", e)
        return []

    home_encoded = _encode_home_path()
    home_prefix_with_dash = home_encoded + "-"

    for d in proj_iter:
        if not d.is_dir():
            continue
        try:
            session_files = list(d.glob("*.jsonl"))
        except OSError:
            continue
        for fp in session_files:
            if "subagents" in str(fp):
                continue
            sid = fp.stem
            if exclude_discord and sid in used_sids:
                continue
            try:
                mtime = fp.stat().st_mtime
            except OSError:
                continue
            dt = datetime.fromtimestamp(mtime).strftime("%m/%d %H:%M")
            # ホーム配下のプロジェクトはホーム部分を省略表示
            if d.name == home_encoded:
                project = "(home)"
            elif d.name.startswith(home_prefix_with_dash):
                project = d.name[len(home_prefix_with_dash):]
            else:
                project = d.name

            first_msg = ""
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if rec.get("type") == "user":
                            msg = rec.get("message", "")
                            if isinstance(msg, dict):
                                content = msg.get("content", "")
                                if isinstance(content, str):
                                    first_msg = content
                                elif isinstance(content, list):
                                    for b in content:
                                        if isinstance(b, dict) and b.get("type") == "text":
                                            first_msg = b.get("text", "")
                                            break
                            elif isinstance(msg, str):
                                first_msg = msg
                            if first_msg:
                                break
            except OSError:
                pass

            first_msg = first_msg.replace("\n", " ").strip()[:50]
            results.append({
                "session_id": sid,
                "mtime": mtime,
                "date": dt,
                "project": project,
                "first_msg": first_msg,
            })

    results.sort(key=lambda x: x["mtime"], reverse=True)
    return results[:limit]


@bot.tree.command(name="bridge-sessions", description="PCのClaude Codeセッション一覧を表示する（最新10件）")
@discord.app_commands.describe(件数="表示するセッション数（デフォルト10、最大20）")
async def list_sessions(interaction: discord.Interaction, 件数: int = 10):
    if str(interaction.user.id) not in ALLOWED_USERS:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    count = max(1, min(件数, 20))
    sessions = get_recent_sessions(count)
    if not sessions:
        await interaction.followup.send("セッションが見つかりませんでした", ephemeral=True)
        return

    lines = []
    for i, s in enumerate(sessions, 1):
        lines.append(
            f"**{i}.** `{s['date']}` [{s['project']}]\n"
            f"   `{s['session_id']}`\n"
            f"   {s['first_msg'] or '（メッセージなし）'}"
        )
    text = "**PCのClaude Codeセッション一覧**\n\n" + "\n\n".join(lines)
    for chunk in split_message(text, 2000):
        await interaction.followup.send(chunk, ephemeral=True)


async def _create_forum_thread(
    interaction: discord.Interaction,
    title: str,
    initial_content: str,
) -> tuple[discord.Thread, discord.Message] | None:
    forum = interaction.guild.get_channel(FORUM_CHANNEL_ID)
    if not forum:
        try:
            forum = await interaction.guild.fetch_channel(FORUM_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await interaction.followup.send("フォーラムチャンネルが見つかりません", ephemeral=True)
            return None
    if not isinstance(forum, discord.ForumChannel):
        await interaction.followup.send("指定されたチャンネルはフォーラムではありません", ephemeral=True)
        return None

    try:
        thread_with_message = await forum.create_thread(name=title[:100] or "スレッド", content=initial_content)
    except (discord.Forbidden, discord.HTTPException) as e:
        await interaction.followup.send(f"スレッド作成エラー: {e}", ephemeral=True)
        return None
    return thread_with_message.thread, thread_with_message.message


@bot.tree.command(name="bridge-resume-latest", description="PCの最新セッションを引き継いでフォーラムスレッドを作成する")
@discord.app_commands.describe(
    title="スレッドのタイトル（省略時は自動生成）",
    prompt="最初に送るメッセージ（省略時はセッション要約をリクエスト）",
)
async def resume_latest(interaction: discord.Interaction, title: str = "", prompt: str = ""):
    if str(interaction.user.id) not in ALLOWED_USERS:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)

    recent = get_recent_sessions(1, exclude_discord=True)
    if not recent:
        await interaction.followup.send("セッションが見つかりませんでした", ephemeral=True)
        return

    latest = recent[0]
    session_id = latest["session_id"]
    if not title:
        msg_preview = latest["first_msg"][:30] if latest["first_msg"] else session_id[:8]
        title = f"PC引継ぎ: {msg_preview}"

    initial_prompt = prompt or "これはPCのClaude Codeセッションからの引き継ぎです。これまでの会話の内容を簡潔に要約してください。"
    created = await _create_forum_thread(
        interaction, title,
        f"🔗 **PCセッション引き継ぎ（最新）**\nセッションID: `{session_id}`\n元の会話: {latest['first_msg'] or '（不明）'}\n\n{initial_prompt}",
    )
    if not created:
        return
    thread, message = created
    await set_session_id(str(thread.id), session_id)
    await interaction.followup.send(
        f"✅ 最新セッションを引き継ぎ: {thread.mention}\n"
        f"セッション: `{session_id}`\n"
        f"元の会話: {latest['first_msg'] or '（不明）'}"
    )
    await enqueue_for_thread(thread, message, initial_prompt)


@bot.tree.command(name="bridge-help", description="bridge-* コマンド一覧を表示する")
async def show_help(interaction: discord.Interaction):
    embed = discord.Embed(title="Discord Claude Bridge - コマンド一覧", color=discord.Color.blue())
    embed.add_field(
        name="📚 セッション操作",
        value=(
            "`/bridge-help` — このヘルプ\n"
            "`/bridge-sessions [件数]` — PC側 Claude Code セッション一覧（最大20）\n"
            "`/bridge-resume <id> [title] [prompt]` — 指定セッションをDiscordへ引継ぎ\n"
            "`/bridge-resume-latest [title] [prompt]` — 最新セッションをワンクリック引継ぎ"
        ),
        inline=False,
    )
    embed.add_field(
        name="🧵 スレッド単位の操作 (スレッド内で実行)",
        value=(
            "`/bridge-info` — このスレッドのセッションID・cwd・許可ツール表示\n"
            "`/bridge-forget` — このスレッドのセッションを破棄して次から新規開始\n"
            "`/bridge-cancel` — 走行中の claude を kill\n"
            "`/bridge-retry` — 直前のメッセージを再実行\n"
            "`/bridge-cwd [path]` — このスレッドの作業ディレクトリを固定/解除\n"
            "`/bridge-reset-perms` — このスレッドで「常に許可」したツールを全クリア\n"
            "`/bridge-usage` — このスレッドの累積トークン/コスト\n"
            "`/bridge-archive` — このスレッドをアーカイブ"
        ),
        inline=False,
    )
    embed.add_field(
        name="💬 フォーラムに投稿",
        value=(
            "フォーラムにスレッドを立てるか、既存スレッドにメッセージを送ると Claude Code が応答します。\n"
            "`/init` `/clear` `/compact` 等の Claude Code スラッシュコマンドはメッセージ本文として送ればそのまま動きます。"
        ),
        inline=False,
    )
    embed.add_field(name="🔄 同期", value="`!sync` — スラッシュコマンドを Discord に同期（コマンド変更時に1回）", inline=False)
    embed.set_footer(text="画像添付にも対応。途中経過のツール使用も表示されます")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# =========================================================
# /bridge-* スレッド操作系コマンド
# =========================================================

def _check_thread_command(interaction: discord.Interaction) -> tuple[bool, discord.Thread | None]:
    """スレッド内 + ALLOWED_USERS + フォーラムスレッドであることを確認。
    OK なら (True, thread)、NG なら (False, None) を返す。
    """
    if str(interaction.user.id) not in ALLOWED_USERS:
        return False, None
    ch = interaction.channel
    if not isinstance(ch, discord.Thread) or ch.parent_id != FORUM_CHANNEL_ID:
        return False, None
    return True, ch


async def _reject_non_thread(interaction: discord.Interaction, ephemeral: bool = True):
    if str(interaction.user.id) not in ALLOWED_USERS:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    await interaction.response.send_message(
        "このコマンドは bridge フォーラムのスレッド内で実行してください",
        ephemeral=ephemeral,
    )


@bot.tree.command(name="bridge-info", description="このスレッドのセッション情報を表示する")
async def cmd_info(interaction: discord.Interaction):
    ok, thread = _check_thread_command(interaction)
    if not ok:
        await _reject_non_thread(interaction)
        return
    sid = await get_session_id(str(thread.id))
    cwd_fixed = thread_cwds.get(thread.id)
    derived_cwd = find_session_cwd(sid) if sid else None
    cwd = cwd_fixed or derived_cwd or str(Path.home())
    allowed = sorted(allowed_tools.get(str(thread.id), set()))
    proc = get_thread_proc(thread.id)
    running = "走行中" if proc and proc.poll() is None else "アイドル"
    stats = usage_stats.get(thread.id) or {}
    embed = discord.Embed(title="スレッド情報", color=discord.Color.blurple())
    embed.add_field(name="セッションID", value=f"`{sid}`" if sid else "（無し）", inline=False)
    embed.add_field(name="cwd", value=f"`{cwd}`" + (" (固定)" if cwd_fixed else ""), inline=False)
    embed.add_field(name="状態", value=running, inline=True)
    embed.add_field(name="ターン数", value=str(stats.get("turns", 0)), inline=True)
    embed.add_field(name="常に許可済", value=", ".join(allowed) if allowed else "（無し）", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="bridge-forget", description="このスレッドのセッションを破棄して次から新規開始する")
async def cmd_forget(interaction: discord.Interaction):
    ok, thread = _check_thread_command(interaction)
    if not ok:
        await _reject_non_thread(interaction)
        return
    thread_key = str(thread.id)
    async with sessions_lock:
        sessions = load_sessions()
        old = sessions.pop(thread_key, None)
        if old:
            save_sessions(sessions)
    if old:
        await interaction.response.send_message(f"✅ セッション `{old}` を破棄しました。次のメッセージから新規開始します", ephemeral=True)
    else:
        await interaction.response.send_message("（このスレッドにはセッションがありません）", ephemeral=True)


@bot.tree.command(name="bridge-cancel", description="このスレッドで走行中の claude を kill する")
async def cmd_cancel(interaction: discord.Interaction):
    ok, thread = _check_thread_command(interaction)
    if not ok:
        await _reject_non_thread(interaction)
        return
    proc = get_thread_proc(thread.id)
    if proc is None or proc.poll() is not None:
        await interaction.response.send_message("（走行中の claude はありません）", ephemeral=True)
        return
    try:
        proc.kill()
        await interaction.response.send_message("🛑 走行中の claude を kill しました", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"kill 失敗: {e}", ephemeral=True)


@bot.tree.command(name="bridge-cwd", description="このスレッドの作業ディレクトリを固定する（空で固定解除）")
@discord.app_commands.describe(path="絶対パス。空文字でクリア（自動推定に戻す）")
async def cmd_cwd(interaction: discord.Interaction, path: str = ""):
    ok, thread = _check_thread_command(interaction)
    if not ok:
        await _reject_non_thread(interaction)
        return
    p = path.strip()
    if not p:
        thread_cwds.pop(thread.id, None)
        await interaction.response.send_message("🌿 cwd 固定を解除しました（自動推定に戻ります）", ephemeral=True)
        return
    if not os.path.isabs(p):
        await interaction.response.send_message(f"❌ 絶対パスを指定してください: `{p}`", ephemeral=True)
        return
    if not os.path.isdir(p):
        await interaction.response.send_message(f"❌ ディレクトリが存在しません: `{p}`", ephemeral=True)
        return
    thread_cwds[thread.id] = p
    await interaction.response.send_message(f"📌 cwd を固定: `{p}`", ephemeral=True)


@bot.tree.command(name="bridge-reset-perms", description="このスレッドで「常に許可」したツールを全クリアする")
async def cmd_reset_perms(interaction: discord.Interaction):
    ok, thread = _check_thread_command(interaction)
    if not ok:
        await _reject_non_thread(interaction)
        return
    removed = allowed_tools.pop(str(thread.id), set())
    if removed:
        await interaction.response.send_message(
            f"🔒 クリアした「常に許可」: {', '.join(sorted(removed))}",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message("（このスレッドに「常に許可」されたツールはありません）", ephemeral=True)


@bot.tree.command(name="bridge-retry", description="直前のメッセージを再実行する")
async def cmd_retry(interaction: discord.Interaction):
    ok, thread = _check_thread_command(interaction)
    if not ok:
        await _reject_non_thread(interaction)
        return
    last = last_prompts.get(thread.id)
    if not last:
        await interaction.response.send_message("（このスレッドに直前メッセージの記録がありません）", ephemeral=True)
        return
    await interaction.response.send_message(f"🔁 再実行: {last[:200]}", ephemeral=True)
    # 直前 prompt をそのままエンキュー
    await enqueue_for_thread(thread, interaction, last)


@bot.tree.command(name="bridge-usage", description="このスレッドの累積トークン/コストを表示する")
async def cmd_usage(interaction: discord.Interaction):
    ok, thread = _check_thread_command(interaction)
    if not ok:
        await _reject_non_thread(interaction)
        return
    s = usage_stats.get(thread.id)
    if not s:
        await interaction.response.send_message("（使用量データなし）", ephemeral=True)
        return
    embed = discord.Embed(title="スレッド使用量", color=discord.Color.gold())
    embed.add_field(name="ターン数", value=str(s.get("turns", 0)), inline=True)
    embed.add_field(name="累計コスト", value=f"${s.get('cost_usd', 0.0):.4f}", inline=True)
    embed.add_field(name="入力トークン", value=f"{s.get('input_tokens', 0):,}", inline=True)
    embed.add_field(name="出力トークン", value=f"{s.get('output_tokens', 0):,}", inline=True)
    embed.add_field(name="cache_creation", value=f"{s.get('cache_creation_tokens', 0):,}", inline=True)
    embed.add_field(name="cache_read", value=f"{s.get('cache_read_tokens', 0):,}", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="bridge-archive", description="このスレッドをアーカイブする")
async def cmd_archive(interaction: discord.Interaction):
    ok, thread = _check_thread_command(interaction)
    if not ok:
        await _reject_non_thread(interaction)
        return
    try:
        await interaction.response.send_message("📦 アーカイブします", ephemeral=True)
        await thread.edit(archived=True)
    except (discord.Forbidden, discord.HTTPException) as e:
        await interaction.followup.send(f"アーカイブ失敗: {e}", ephemeral=True)


@bot.tree.command(name="bridge-resume", description="セッションIDを指定してPCのClaude Codeセッションを引き継ぐ")
@discord.app_commands.describe(
    session_id="Claude CodeのセッションID（claude --resume で使うやつ）",
    title="スレッドのタイトル（省略時は自動生成）",
    prompt="最初に送るメッセージ（省略時はセッション要約をリクエスト）",
)
async def resume_session(interaction: discord.Interaction, session_id: str, title: str = "", prompt: str = ""):
    if str(interaction.user.id) not in ALLOWED_USERS:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)

    session_id = session_id.strip()
    if not is_valid_session_id(session_id):
        await interaction.followup.send(
            f"❌ session_id の形式が不正です: `{session_id}`",
            ephemeral=True,
        )
        return
    if not find_session_cwd(session_id):
        await interaction.followup.send(
            f"⚠️ セッション `{session_id}` がローカルに見つかりません。"
            f"このまま実行すると --resume で claude が失敗する可能性があります。",
            ephemeral=True,
        )

    if not title:
        sessions_list = get_recent_sessions(100)
        matched = [s for s in sessions_list if s["session_id"] == session_id]
        if matched and matched[0]["first_msg"]:
            title = f"PC引継ぎ: {matched[0]['first_msg'][:30]}"
        else:
            title = f"PC引継ぎ: {session_id[:8]}..."

    initial_prompt = prompt or "これはPCのClaude Codeセッションからの引き継ぎです。これまでの会話の内容を簡潔に要約してください。"
    created = await _create_forum_thread(
        interaction, title,
        f"🔗 **PCセッション引き継ぎ**\nセッションID: `{session_id}`\n\n{initial_prompt}",
    )
    if not created:
        return
    thread, message = created
    await set_session_id(str(thread.id), session_id)
    await interaction.followup.send(f"✅ スレッド作成完了: {thread.mention}\nセッション `{session_id}` を引き継ぎます")
    await enqueue_for_thread(thread, message, initial_prompt)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    if not isinstance(message.channel, discord.Thread):
        return
    if message.channel.parent_id != FORUM_CHANNEL_ID:
        return
    if str(message.author.id) not in ALLOWED_USERS:
        return

    prompt = message.content or ""

    image_paths = await download_attachments(message)
    if image_paths:
        paths_str = "\n".join(f"  - {p}" for p in image_paths)
        prompt += f"\n\n[添付画像（Readツールで閲覧可能）]\n{paths_str}"

    if not prompt.strip() or prompt.startswith("!"):
        # 画像だけ送って空テキスト/コマンド先頭メッセージは処理しない → 一時画像を即削除
        cleanup_attachments(image_paths)
        return

    await enqueue_for_thread(message.channel, message, prompt, image_paths=image_paths)


@bot.event
async def on_error(event_method: str, *args, **kwargs):
    log.exception("on_error in %s", event_method)


if __name__ == "__main__":
    try:
        bot.run(TOKEN, log_handler=None)
    except discord.LoginFailure:
        raise SystemExit("DISCORD_TOKEN が無効です。Bot トークンを確認してください")
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "MESSAGE CONTENT INTENT が無効です。"
            "Discord Developer Portal > Bot > Privileged Gateway Intents で有効化してください"
        )
