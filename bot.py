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
from datetime import datetime

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


TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
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
SOFT_TIMEOUT = int(os.getenv("SOFT_TIMEOUT", "600"))   # まだ動いてるよ通知
HARD_TIMEOUT = int(os.getenv("HARD_TIMEOUT", "3600"))  # 強制終了
STREAM_LINE_BUFSIZE = 16 * 1024 * 1024  # 1行あたり最大16MB（画像 base64 を含む長行に対応）

# フォーラムタグ名
TAG_RUNNING = "実行中"
TAG_COMPLETED = "完了"
TAG_ERROR = "エラー"

# 走行中の Claude subprocess を追跡（bot 終了時に確実に kill するため）
_active_procs: "weakref.WeakSet[subprocess.Popen]" = weakref.WeakSet()
_active_procs_lock = threading.Lock()


def _register_proc(proc: subprocess.Popen):
    with _active_procs_lock:
        _active_procs.add(proc)


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
    return web.json_response({"ok": True, "user": str(bot.user) if bot.is_ready() else None})


# AppRunner を保持して bot 終了時に cleanup できるようにする
_hook_runner: web.AppRunner | None = None


async def start_hook_server():
    global _hook_runner
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
        raise SystemExit(
            f"フックサーバー起動失敗 (port {HOOK_PORT}): {e}\n"
            f"既存プロセスがポートを掴んでいるか、HOOK_PORT を別の番号に変えてください"
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


def build_hook_settings() -> str:
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
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return str(settings_path)


# ==============================
# セッション管理
# ==============================

def load_sessions() -> dict:
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
                if len(data) > DISCORD_FILE_LIMIT:
                    log.warning("画像が大きすぎ (%d bytes), スキップ", len(data))
                    continue
                media = src.get("media_type", "image/png")
                ext = media.split("/")[-1] or "png"
                images.append((data, f"image_{len(images)}.{ext}"))
        elif btype == "tool_result":
            _extract_images_from_blocks(b.get("content"), images)


def parse_stream_events(events: list, stderr: str, session_id: str | None) -> tuple[str, str | None, list]:
    new_session_id = session_id
    final_text = ""
    fallback_text_parts: list[str] = []
    images: list[tuple[bytes, str]] = []
    is_error = False
    error_msg = ""
    result_subtype = ""

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

    output = final_text or "\n".join(p for p in fallback_text_parts if p).strip()
    if is_error and not output:
        output = f"エラー: {error_msg or stderr or '不明なエラー'}"
    if not output and stderr:
        # auth エラー等を可視化
        output = f"エラー: {stderr.strip()[:1500]}"
    if not output and not images:
        output = "（Claude Codeからの応答が空でした。再度試してください）"

    return output.strip(), new_session_id, images


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


def _run_claude_subprocess(args: list, env: dict, cwd: str | None):
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

    _register_proc(proc)
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

    events: list = []
    try:
        # readline は LINE_BUFSIZE まで読む。画像 base64 を含む長行に対応
        for raw_line in iter(proc.stdout.readline, b""):
            line = raw_line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line.decode("utf-8", errors="replace")))
            except json.JSONDecodeError:
                continue
    except (OSError, ValueError) as e:
        log.warning("stream-json 読み取りエラー: %s", e)
    finally:
        # cancel() の戻り値: True=キャンセル成功, False=既にfired
        # fired 後の自己cancel と本物の timeout を区別する
        if not timer.cancel():
            # 既に _kill_on_timeout に入っているか実行済み
            pass
        else:
            # キャンセル成功 = タイマー fired していない = 正常終了
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
        # 2回目の wait は OS が回収するまでバックグラウンドで待つ
        threading.Thread(target=lambda: _safe_wait(proc), daemon=True).start()

    # subprocess が完全に終わってれば stderr は EOF なのでブロックしない
    stderr_thread.join(timeout=10)
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")

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

    if MAX_TURNS:
        args.extend(["--max-turns", MAX_TURNS])
    if MAX_BUDGET_USD:
        args.extend(["--max-budget-usd", MAX_BUDGET_USD])
    if session_id:
        args.extend(["--resume", session_id])
    args.append(prompt)
    return args


async def run_claude(
    prompt: str,
    session_id: str | None = None,
    thread: discord.Thread | None = None,
    thread_title: str | None = None,
    cwd: str | None = None,
) -> tuple[str | None, str | None, list]:
    settings_path = build_hook_settings()

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
    if thread:
        async def _soft_timeout_notify():
            nonlocal placeholder
            await asyncio.sleep(SOFT_TIMEOUT)
            placeholder = await safe_send(
                thread, f"まだ処理中です（{SOFT_TIMEOUT // 60}分経過）… 終わったら更新します",
            )
        notify_task = asyncio.create_task(_soft_timeout_notify())

    events, stderr, timed_out = await asyncio.to_thread(_run_claude_subprocess, args, env, cwd)

    if notify_task and not notify_task.done():
        notify_task.cancel()

    if timed_out:
        msg = f"タイムアウトしました（{HARD_TIMEOUT // 60}分超過、強制終了）"
        if placeholder:
            await safe_edit(placeholder, msg)
        return msg, session_id, []

    output, new_session_id, images = parse_stream_events(events, stderr, session_id)

    if placeholder:
        chunks = split_message(output, 2000)
        await safe_edit(placeholder, chunks[0])
        for chunk in chunks[1:]:
            await safe_send(thread, chunk)
        for img_data, filename in images:
            try:
                await safe_send(thread, file=discord.File(io.BytesIO(img_data), filename=filename))
            except (OSError, ValueError) as e:
                log.warning("画像送信準備エラー: %s", e)
        return None, new_session_id, []

    return output, new_session_id, images


# ==============================
# タグ管理
# ==============================

async def get_or_create_tag(forum: discord.ForumChannel, name: str) -> discord.ForumTag | None:
    for tag in forum.available_tags:
        if tag.name == name:
            return tag
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
            timestamp=datetime.now(),
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
            chunk += "\n```"
            text = "```\n" + text
        chunks.append(chunk)

    return chunks


# ==============================
# Bot setup
# ==============================

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

MAX_CONCURRENT_RUNS = int(os.getenv("MAX_CONCURRENT_RUNS", "5"))

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
    message: discord.Message,
    prompt: str,
    cwd: str | None = None,
    image_paths: list[Path] | None = None,
):
    tid = thread.id
    async with _worker_lock:
        q = thread_queues.setdefault(tid, asyncio.Queue())
        q.put_nowait((thread, message, prompt, cwd, image_paths or []))
        worker = thread_workers.get(tid)
        if worker is None or worker.done():
            thread_workers[tid] = asyncio.create_task(process_thread(tid))


async def process_thread(tid: int):
    q = thread_queues[tid]
    while True:
        while True:
            try:
                thread, message, prompt, cwd, image_paths = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            async with concurrency_sem:
                try:
                    await _run_one(thread, message, prompt, cwd, image_paths)
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
    message: discord.Message,
    prompt: str,
    cwd: str | None,
    image_paths: list[Path] | None = None,
):
    status = TAG_RUNNING
    result = ""
    try:
        await set_thread_tag(thread, TAG_RUNNING)
        await send_log(
            thread.guild, str(message.author),
            thread.name, prompt, "", TAG_RUNNING,
        )

        try:
            typing_ctx = thread.typing()
        except discord.Forbidden:
            typing_ctx = None

        async def _do_work():
            thread_key = str(thread.id)
            session_id = await get_session_id(thread_key)
            # session_id が不正形式または .jsonl が存在しなければ resume を諦めて新規開始
            if session_id and not is_valid_session_id(session_id):
                log.warning("不正形式の session_id を破棄: %s", session_id)
                session_id = None
            run_cwd = cwd or (find_session_cwd(session_id) if session_id else None) or str(Path.home())
            result_inner, new_session_id, images = await run_claude(
                prompt, session_id, thread, thread.name, cwd=run_cwd,
            )
            if new_session_id:
                await set_session_id(thread_key, new_session_id)
                if not session_id:
                    await safe_send(thread, f"🆔 Session: `{new_session_id}`")
            if result_inner is not None or images:
                await send_response(thread, result_inner or "", images)
            return result_inner

        if typing_ctx is not None:
            async with typing_ctx:
                result = await _do_work() or ""
        else:
            result = await _do_work() or ""
        status = TAG_COMPLETED

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
            thread.guild, str(message.author),
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


@bot.event
async def on_ready():
    await start_hook_server()
    try:
        synced = await bot.tree.sync()
    except discord.HTTPException as e:
        log.warning("スラッシュコマンド同期失敗: %s", e)
        synced = []
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
    for att in message.attachments:
        # API直叩きで `../` 等が来てもパストラバーサルを許さない
        safe_name = Path(att.filename).name
        ext = Path(safe_name).suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            continue
        save_path = TEMP_DIR / f"{message.id}_{safe_name}"
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
    例: 'C--Users-user-discord-claude-bridge' → 'C:\\Users\\user\\discord-claude-bridge'
    """
    if not encoded or "--" not in encoded:
        return None
    parts = encoded.split("--", 1)
    if len(parts) != 2:
        return None
    drive = parts[0] + ":"
    rest = parts[1]
    segments = rest.split("-")

    def resolve(idx: int, current: str) -> str | None:
        if idx >= len(segments):
            return current if os.path.isdir(current) else None
        for end in range(len(segments), idx, -1):
            segment = "-".join(segments[idx:end])
            candidate = os.path.join(current, segment)
            if os.path.isdir(candidate):
                result = resolve(end, candidate)
                if result:
                    return result
        return None

    return resolve(0, drive + os.sep)


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


@bot.tree.command(name="sessions", description="PCのClaude Codeセッション一覧を表示する（最新10件）")
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


@bot.tree.command(name="resume-latest", description="PCの最新セッションを引き継いでフォーラムスレッドを作成する")
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


@bot.tree.command(name="help", description="使えるコマンド一覧を表示する")
async def show_help(interaction: discord.Interaction):
    embed = discord.Embed(title="Discord Claude Bridge - コマンド一覧", color=discord.Color.blue())
    embed.add_field(name="/help", value="このヘルプを表示する", inline=False)
    embed.add_field(name="/sessions [件数]", value="PCのClaude Codeセッション一覧（最大20件）", inline=False)
    embed.add_field(
        name="/resume <session_id> [title] [prompt]",
        value="セッションIDを指定してフォーラムスレッドを作成し、PCのセッションを引き継ぐ",
        inline=False,
    )
    embed.add_field(name="/resume-latest [title] [prompt]", value="PCの最新セッションをワンクリックで引き継ぐ", inline=False)
    embed.add_field(
        name="フォーラムに投稿",
        value="フォーラムにスレッドを立てるか、既存スレッドにメッセージを送ると、Claude Codeが応答します",
        inline=False,
    )
    embed.add_field(name="!sync", value="スラッシュコマンドをDiscordに同期する（コマンド追加・変更時に1回）", inline=False)
    embed.set_footer(text="画像添付にも対応しています")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="resume", description="セッションIDを指定してPCのClaude Codeセッションを引き継ぐ")
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
