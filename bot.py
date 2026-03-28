import os
import sys
import json
import asyncio
import re
import subprocess

# Windows: aiodns が SelectorEventLoop を要求する問題を回避
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import uuid
import base64
import io
import tempfile
from pathlib import Path
from datetime import datetime

# Windows cp932 で絵文字が encode できない問題を回避
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import discord
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
FORUM_CHANNEL_ID = int(os.getenv("FORUM_CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
ALLOWED_USERS = set(filter(None, (x.strip() for x in os.getenv("ALLOWED_USERS", "").split(","))))
SKIP_PERMISSIONS = os.getenv("SKIP_PERMISSIONS", "false").lower() in ("true", "1", "yes")
HOOK_PORT = int(os.getenv("HOOK_PORT", "8585"))

SESSIONS_FILE = Path(__file__).parent / "sessions.json"
TEMP_DIR = Path(tempfile.gettempdir()) / "discord-claude-bridge"
TEMP_DIR.mkdir(exist_ok=True)
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
SOFT_TIMEOUT = 600   # 10分で「まだやってるよ」メッセージ
HARD_TIMEOUT = 3600  # 1時間で強制終了

# フォーラムタグ名
TAG_RUNNING = "実行中"
TAG_COMPLETED = "完了"
TAG_ERROR = "エラー"


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
    """ツール情報を Discord 表示用にフォーマット"""
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")
        return f"```bash\n{cmd[:800]}\n```"
    elif tool_name in ("Edit", "Write", "MultiEdit"):
        path = tool_input.get("file_path", "")
        old = tool_input.get("old_string", "")
        new = tool_input.get("new_string", "")
        parts = [f"**ファイル:** `{path}`"]
        if old:
            parts.append(f"```diff\n- {old[:200]}\n+ {new[:200]}\n```")
        return "\n".join(parts)
    elif tool_name == "NotebookEdit":
        path = tool_input.get("notebook_path", tool_input.get("file_path", ""))
        return f"**ノートブック:** `{path}`"
    else:
        # MCP ツール等
        detail = json.dumps(tool_input, ensure_ascii=False, indent=2)
        if len(detail) > 500:
            detail = detail[:500] + "\n..."
        return f"```json\n{detail}\n```"


class PermissionView(discord.ui.View):
    """許可 / 常に許可 / 拒否 ボタン"""

    def __init__(self, request_id: str, tool_name: str, thread_id: str, hook_type: str):
        super().__init__(timeout=600)
        self.request_id = request_id
        self.tool_name = tool_name
        self.thread_id = thread_id
        self.hook_type = hook_type

    def _make_allow(self) -> dict:
        """フックタイプに応じた許可レスポンスを生成"""
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
        """フックタイプに応じた拒否レスポンスを生成"""
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

    def _resolve(self, result: dict):
        permission_results[self.request_id] = result
        ev = permission_events.get(self.request_id)
        if ev:
            ev.set()

    @discord.ui.button(label="許可", style=discord.ButtonStyle.green, emoji="✅")
    async def allow_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._resolve(self._make_allow())
        self.stop()
        await interaction.response.edit_message(
            content=f"✅ `{self.tool_name}` を許可しました", view=None,
        )

    @discord.ui.button(label="常に許可", style=discord.ButtonStyle.blurple, emoji="🔓")
    async def always_allow_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed_tools.setdefault(self.thread_id, set()).add(self.tool_name)
        self._resolve(self._make_allow())
        self.stop()
        await interaction.response.edit_message(
            content=f"🔓 `{self.tool_name}` を常に許可しました（このスレッド内）", view=None,
        )

    @discord.ui.button(label="拒否", style=discord.ButtonStyle.red, emoji="❌")
    async def deny_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._resolve(self._make_deny("Discordユーザーが拒否しました"))
        self.stop()
        await interaction.response.edit_message(
            content=f"❌ `{self.tool_name}` を拒否しました", view=None,
        )

    async def on_timeout(self):
        # タイムアウト時は許可（ブロックしない）
        if self.request_id in permission_events:
            self._resolve(self._make_allow())


def make_quick_allow(hook_type: str) -> dict:
    """即許可レスポンスを生成（常に許可済み/エラー時用）"""
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


async def handle_permission_request(request: web.Request) -> web.Response:
    """フックスクリプトからの HTTP リクエストを処理"""
    data = await request.json()
    hook_type = data.get("hook_type", "PreToolUse")
    tool_name = data.get("tool_name", "unknown")
    tool_input = data.get("tool_input", {})
    thread_id = data.get("thread_id", "")

    # 「常に許可」済みのツールは即応答
    if thread_id in allowed_tools and tool_name in allowed_tools[thread_id]:
        return web.json_response(make_quick_allow(hook_type))

    # Discord にボタン送信
    request_id = str(uuid.uuid4())
    event = asyncio.Event()
    permission_events[request_id] = event

    try:
        thread = bot.get_channel(int(thread_id)) if thread_id else None
        if thread:
            detail = format_tool_detail(tool_name, tool_input)
            view = PermissionView(request_id, tool_name, thread_id, hook_type)
            await thread.send(
                f"🔐 **権限リクエスト: `{tool_name}`**\n{detail}",
                view=view,
            )
    except Exception as e:
        print(f"権限リクエスト送信エラー: {e}")
        # 送信失敗時はクリーンアップして許可
        permission_events.pop(request_id, None)
        permission_results.pop(request_id, None)
        return web.json_response(make_quick_allow(hook_type))

    # ユーザーの応答を待つ（最大10分）
    try:
        await asyncio.wait_for(event.wait(), timeout=600)
    except asyncio.TimeoutError:
        pass

    result = permission_results.pop(request_id, None)
    permission_events.pop(request_id, None)
    # タイムアウト等で結果が無い場合は正しいフォーマットで許可を返す
    if result is None:
        result = make_quick_allow(hook_type)
    return web.json_response(result)


async def start_hook_server():
    """フックからのリクエストを受けるローカル HTTP サーバーを起動"""
    app = web.Application()
    app.router.add_post("/permission", handle_permission_request)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", HOOK_PORT)
    await site.start()
    print(f"Hook サーバー起動: http://127.0.0.1:{HOOK_PORT}")


def build_hook_settings() -> str:
    """Claude Code に渡す hooks 設定 JSON ファイルを生成して返す"""
    base_dir = Path(__file__).parent.resolve()
    pretooluse_script = str(base_dir / "hook_pretooluse.py")
    permission_script = str(base_dir / "hook_permission_request.py")
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
        },
    }
    settings_path = base_dir / ".claude_hook_settings.json"
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return str(settings_path)


# ==============================
# セッション管理
# ==============================

def load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        try:
            return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            print(f"警告: {SESSIONS_FILE} が破損しています。空のセッションで開始します")
    return {}


def save_sessions(sessions: dict):
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2), encoding="utf-8")


# ==============================
# Claude Code 実行
# ==============================

def strip_ansi(text: str) -> str:
    return re.sub(r"\x1B\[[0-9;]*[a-zA-Z]", "", text)


def parse_claude_output(raw: str, err: str, session_id: str | None) -> tuple[str, str | None, list]:
    """Claude CLIの出力をパースして (応答テキスト, セッションID, 画像リスト) を返す"""
    print(f"[DEBUG] claude stdout ({len(raw)} chars): {raw[:500]}")
    if err:
        print(f"[DEBUG] claude stderr: {err[:300]}")

    new_session_id = session_id
    output = ""
    images: list[tuple[bytes, str]] = []  # (data, filename)
    try:
        data = json.loads(raw)
        new_session_id = data.get("session_id", session_id)
        result = data.get("result", "")
        if isinstance(result, str):
            output = result
        elif isinstance(result, list):
            texts = []
            for i, block in enumerate(result):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "image":
                    source = block.get("source", {})
                    if source.get("type") == "base64":
                        try:
                            img_data = base64.b64decode(source["data"])
                            ext = source.get("media_type", "image/png").split("/")[-1]
                            images.append((img_data, f"image_{i}.{ext}"))
                        except Exception as e:
                            print(f"画像デコードエラー: {e}")
            output = "\n".join(texts)
        else:
            output = str(result)
        if not output:
            output = data.get("text", "") or json.dumps(data, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        output = strip_ansi(raw) if raw else ""

    if not output and err:
        output = f"エラー: {err}"
    if not output and not images:
        output = "（Claude Codeからの応答が空でした。再度試してください）"

    return output.strip(), new_session_id, images


async def run_claude(
    prompt: str,
    session_id: str | None = None,
    thread: discord.Thread | None = None,
    thread_title: str | None = None,
    cwd: str | None = None,
) -> tuple[str, str | None, list]:
    """Claude Code CLI を実行して (応答テキスト, セッションID) を返す。"""
    args = ["claude", "-p", "--output-format", "json"]

    if SKIP_PERMISSIONS:
        # 全権限スキップ（フックなし）
        args.insert(1, "--dangerously-skip-permissions")
    else:
        # フックで権限管理（built-in プロンプトは無効化）
        args.insert(1, "--dangerously-skip-permissions")
        settings_path = build_hook_settings()
        args.extend(["--settings", settings_path])

    if session_id:
        args.extend(["--resume", session_id])

    if not session_id and thread_title:
        prompt = f"[スレッドタイトル: {thread_title}]\n\n{prompt}"
    args.append(prompt)

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["HOOK_PORT"] = str(HOOK_PORT)
    if thread:
        env["DISCORD_THREAD_ID"] = str(thread.id)

    def _run_subprocess():
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        try:
            stdout, stderr = proc.communicate(timeout=HARD_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            return None, None, True  # timed out
        return stdout, stderr, False

    # ソフトタイムアウト通知用タスク
    placeholder = None
    if thread:
        async def _soft_timeout_notify():
            nonlocal placeholder
            await asyncio.sleep(SOFT_TIMEOUT)
            placeholder = await thread.send(f"まだ処理中です（{SOFT_TIMEOUT // 60}分経過）… 終わったら更新します")
        notify_task = asyncio.create_task(_soft_timeout_notify())
    else:
        notify_task = None

    stdout, stderr, timed_out = await asyncio.to_thread(_run_subprocess)

    if notify_task and not notify_task.done():
        notify_task.cancel()

    if timed_out:
        msg = f"タイムアウトしました（{HARD_TIMEOUT // 60}分超過、強制終了）"
        if placeholder:
            await placeholder.edit(content=msg)
        return msg, session_id, []

    raw = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    output, new_session_id, images = parse_claude_output(raw, err, session_id)

    if placeholder:
        chunks = split_message(output, 2000)
        await placeholder.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await thread.send(chunk)
        if images:
            for img_data, filename in images:
                file = discord.File(io.BytesIO(img_data), filename=filename)
                await thread.send(file=file)
        return None, new_session_id, []

    return output, new_session_id, images


# ==============================
# タグ管理
# ==============================

async def get_or_create_tag(forum: discord.ForumChannel, name: str) -> discord.ForumTag:
    for tag in forum.available_tags:
        if tag.name == name:
            return tag
    new_tags = list(forum.available_tags) + [discord.ForumTag(name=name)]
    await forum.edit(available_tags=new_tags)
    forum = await forum.guild.fetch_channel(forum.id)
    for tag in forum.available_tags:
        if tag.name == name:
            return tag
    return None


async def set_thread_tag(thread: discord.Thread, tag_name: str):
    try:
        forum = thread.parent
        if not forum:
            forum = await thread.guild.fetch_channel(thread.parent_id)
        tag = await get_or_create_tag(forum, tag_name)
        if tag:
            status_names = {TAG_RUNNING, TAG_COMPLETED, TAG_ERROR}
            keep_tags = [t for t in thread.applied_tags if t.name not in status_names]
            keep_tags.append(tag)
            await thread.edit(applied_tags=keep_tags[:5])
    except Exception as e:
        print(f"タグ設定エラー: {e}")


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
        embed.add_field(name="プロンプト", value=prompt[:1024], inline=False)
        if result:
            embed.add_field(name="応答", value=result[:1024], inline=False)
        await log_ch.send(embed=embed)
    except Exception as e:
        print(f"ログ送信エラー: {e}")


# ==============================
# メッセージ分割送信
# ==============================

async def send_response(channel: discord.Thread, text: str, images: list[tuple[bytes, str]] | None = None):
    if not text and not images:
        await channel.send("（空の応答）")
        return
    if text:
        chunks = split_message(text, 2000)
        for chunk in chunks:
            await channel.send(chunk)
    if images:
        for img_data, filename in images:
            file = discord.File(io.BytesIO(img_data), filename=filename)
            await channel.send(file=file)


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

        backtick_count = chunk.count("```")
        if backtick_count % 2 == 1:
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

queue = asyncio.Queue()
processing = False


async def process_queue():
    global processing
    if processing:
        return
    processing = True
    try:
        while not queue.empty():
            thread, message, prompt, sessions, *extra = await queue.get()
            cwd = extra[0] if extra else None
            status = TAG_RUNNING
            result = ""
            try:
                await set_thread_tag(thread, TAG_RUNNING)
                await send_log(
                    thread.guild, str(message.author),
                    thread.name, prompt, "", TAG_RUNNING,
                )

                async with thread.typing():
                    session_id = sessions.get(str(thread.id))
                    # cwd未指定でresumeの場合、セッションのプロジェクトパスを自動解決
                    run_cwd = cwd
                    if not run_cwd and session_id:
                        run_cwd = find_session_cwd(session_id)
                    if not run_cwd:
                        run_cwd = str(Path.home())
                    result, new_session_id, images = await run_claude(prompt, session_id, thread, thread.name, cwd=run_cwd)

                    if new_session_id:
                        sessions[str(thread.id)] = new_session_id
                        save_sessions(sessions)
                        if not session_id:
                            await thread.send(f"🆔 Session: `{new_session_id}`")

                    if result is not None or images:
                        await send_response(thread, result or "", images)
                    status = TAG_COMPLETED

            except Exception as e:
                import traceback
                traceback.print_exc()
                result = str(e)
                status = TAG_ERROR
                await thread.send(f"エラーが発生しました: {e}")
            finally:
                await set_thread_tag(thread, status)
                await send_log(
                    thread.guild, str(message.author),
                    thread.name, prompt, result, status,
                )
                queue.task_done()
    finally:
        processing = False


@bot.command(name="sync")
async def sync_commands(ctx: commands.Context):
    """スラッシュコマンドを手動同期（!sync）"""
    if str(ctx.author.id) not in ALLOWED_USERS:
        return
    synced = await bot.tree.sync()
    await ctx.send(f"✅ {len(synced)}個のコマンドを同期しました")


@bot.event
async def on_ready():
    if not SKIP_PERMISSIONS:
        await start_hook_server()
    synced = await bot.tree.sync()
    print(f"Bot起動: {bot.user} ({len(synced)}個のコマンドを同期)")
    print(f"フォーラムチャンネルID: {FORUM_CHANNEL_ID}")
    print(f"ログチャンネルID: {LOG_CHANNEL_ID}")
    print(f"許可ユーザー: {ALLOWED_USERS}")
    print(f"権限モード: {'全スキップ' if SKIP_PERMISSIONS else f'Discord承認 (port {HOOK_PORT})'}")


async def download_attachments(message: discord.Message) -> list[Path]:
    """メッセージの画像添付をダウンロードしてパスのリストを返す"""
    downloaded = []
    for att in message.attachments:
        ext = Path(att.filename).suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            continue
        save_path = TEMP_DIR / f"{message.id}_{att.filename}"
        try:
            await att.save(save_path)
            downloaded.append(save_path)
            print(f"画像ダウンロード: {save_path}")
        except Exception as e:
            print(f"画像ダウンロードエラー ({att.filename}): {e}")
    return downloaded


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
        # 長い方から試す（ハイフン入りディレクトリ名に対応）
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
    """セッションIDからそのセッションが属するプロジェクトのパスを返す"""
    proj_dir = Path.home() / ".claude" / "projects"
    if not proj_dir.exists():
        return None
    for d in proj_dir.iterdir():
        if not d.is_dir():
            continue
        session_file = d / f"{session_id}.jsonl"
        if session_file.exists():
            resolved = decode_project_path(d.name)
            if resolved:
                return resolved
    return None


def get_recent_sessions(limit: int = 10, exclude_discord: bool = False) -> list[dict]:
    """PCのClaude Codeセッションファイルを読み取り、最新順で返す"""
    # Discordで既に使用中のセッションIDを除外リストに
    used_sids = set()
    if exclude_discord:
        try:
            used_sids = set(load_sessions().values())
        except Exception:
            pass

    proj_dir = Path.home() / ".claude" / "projects"
    results = []
    for d in proj_dir.iterdir():
        if not d.is_dir():
            continue
        for fp in d.glob("*.jsonl"):
            if "subagents" in str(fp):
                continue
            sid = fp.stem
            if exclude_discord and sid in used_sids:
                continue
            mtime = fp.stat().st_mtime
            dt = datetime.fromtimestamp(mtime).strftime("%m/%d %H:%M")
            project = d.name.replace("C--Users-user-", "").replace("C--Users-user", "(home)")

            first_msg = ""
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    for line in f:
                        rec = json.loads(line)
                        if rec.get("type") == "user":
                            msg = rec.get("message", "")
                            if isinstance(msg, dict):
                                content = msg.get("content", "")
                                if isinstance(content, str):
                                    first_msg = content
                                elif isinstance(content, list):
                                    for b in content:
                                        if isinstance(b, dict) and b.get("type") == "text":
                                            first_msg = b["text"]
                                            break
                            elif isinstance(msg, str):
                                first_msg = msg
                            if first_msg:
                                break
            except Exception:
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
@discord.app_commands.describe(
    件数="表示するセッション数（デフォルト10、最大20）",
)
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
    # 2000文字超えたら分割
    chunks = split_message(text, 2000)
    await interaction.followup.send(chunks[0], ephemeral=True)
    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


@bot.tree.command(name="resume-latest", description="PCの最新セッションを引き継いでフォーラムスレッドを作成する")
@discord.app_commands.describe(
    title="スレッドのタイトル（省略時は自動生成）",
    prompt="最初に送るメッセージ（省略時はセッション要約をリクエスト）",
)
async def resume_latest(
    interaction: discord.Interaction,
    title: str = "",
    prompt: str = "",
):
    if str(interaction.user.id) not in ALLOWED_USERS:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    # フォーラムチャンネル取得
    forum = interaction.guild.get_channel(FORUM_CHANNEL_ID)
    if not forum:
        try:
            forum = await interaction.guild.fetch_channel(FORUM_CHANNEL_ID)
        except Exception:
            await interaction.followup.send("フォーラムチャンネルが見つかりません", ephemeral=True)
            return

    if not isinstance(forum, discord.ForumChannel):
        await interaction.followup.send("指定されたチャンネルはフォーラムではありません", ephemeral=True)
        return

    # セッション取得（Discord既使用分は除外）
    recent = get_recent_sessions(1, exclude_discord=True)
    if not recent:
        await interaction.followup.send("セッションが見つかりませんでした", ephemeral=True)
        return

    latest = recent[0]
    session_id = latest["session_id"]

    if not title:
        msg_preview = latest["first_msg"][:30] if latest["first_msg"] else session_id[:8]
        title = f"PC引継ぎ: {msg_preview}"

    try:
        initial_prompt = prompt or "これはPCのClaude Codeセッションからの引き継ぎです。これまでの会話の内容を簡潔に要約してください。"
        thread_with_message = await forum.create_thread(
            name=title,
            content=f"🔗 **PCセッション引き継ぎ（最新）**\nセッションID: `{session_id}`\n元の会話: {latest['first_msg'] or '（不明）'}\n\n{initial_prompt}",
        )
        thread = thread_with_message.thread
        message = thread_with_message.message
    except Exception as e:
        await interaction.followup.send(f"スレッド作成エラー: {e}", ephemeral=True)
        return

    sessions_data = load_sessions()
    sessions_data[str(thread.id)] = session_id
    save_sessions(sessions_data)

    await interaction.followup.send(
        f"✅ 最新セッションを引き継ぎ: {thread.mention}\n"
        f"セッション: `{session_id}`\n"
        f"元の会話: {latest['first_msg'] or '（不明）'}"
    )

    await queue.put((thread, message, initial_prompt, sessions_data))
    await process_queue()


@bot.tree.command(name="help", description="使えるコマンド一覧を表示する")
async def show_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Discord Claude Bridge - コマンド一覧",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="/help",
        value="このヘルプを表示する",
        inline=False,
    )
    embed.add_field(
        name="/sessions [件数]",
        value="PCのClaude Codeセッション一覧を表示する（デフォルト10件、最大20件）",
        inline=False,
    )
    embed.add_field(
        name="/resume <session_id> [title] [prompt]",
        value="セッションIDを指定してフォーラムスレッドを作成し、PCのセッションを引き継ぐ",
        inline=False,
    )
    embed.add_field(
        name="/resume-latest [title] [prompt]",
        value="PCの最新セッションをワンクリックで引き継ぐ",
        inline=False,
    )
    embed.add_field(
        name="フォーラムに投稿",
        value="フォーラムにスレッドを立てるか、既存スレッドにメッセージを送ると、Claude Codeが応答します",
        inline=False,
    )
    embed.add_field(
        name="!sync",
        value="スラッシュコマンドをDiscordに同期する（コマンド追加・変更時に1回実行）",
        inline=False,
    )
    embed.set_footer(text="画像添付にも対応しています")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="resume", description="セッションIDを指定してPCのClaude Codeセッションを引き継ぐ")
@discord.app_commands.describe(
    session_id="Claude CodeのセッションID（claude --resume で使うやつ）",
    title="スレッドのタイトル（省略時は自動生成）",
    prompt="最初に送るメッセージ（省略時はセッション要約をリクエスト）",
)
async def resume_session(
    interaction: discord.Interaction,
    session_id: str,
    title: str = "",
    prompt: str = "",
):
    # 権限チェック
    if str(interaction.user.id) not in ALLOWED_USERS:
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)

    # フォーラムチャンネル取得
    forum = interaction.guild.get_channel(FORUM_CHANNEL_ID)
    if not forum:
        try:
            forum = await interaction.guild.fetch_channel(FORUM_CHANNEL_ID)
        except Exception:
            await interaction.followup.send("フォーラムチャンネルが見つかりません", ephemeral=True)
            return

    if not isinstance(forum, discord.ForumChannel):
        await interaction.followup.send("指定されたチャンネルはフォーラムではありません", ephemeral=True)
        return

    # タイトル生成（セッションの最初のメッセージをプレビュー）
    if not title:
        sessions_list = get_recent_sessions(100)
        matched = [s for s in sessions_list if s["session_id"] == session_id]
        if matched and matched[0]["first_msg"]:
            title = f"PC引継ぎ: {matched[0]['first_msg'][:30]}"
        else:
            title = f"PC引継ぎ: {session_id[:8]}..."

    # フォーラムスレッド作成
    try:
        initial_prompt = prompt or "これはPCのClaude Codeセッションからの引き継ぎです。これまでの会話の内容を簡潔に要約してください。"
        thread_with_message = await forum.create_thread(
            name=title,
            content=f"🔗 **PCセッション引き継ぎ**\nセッションID: `{session_id}`\n\n{initial_prompt}",
        )
        thread = thread_with_message.thread
        message = thread_with_message.message
    except Exception as e:
        await interaction.followup.send(f"スレッド作成エラー: {e}", ephemeral=True)
        return

    # セッションID紐付け
    sessions = load_sessions()
    sessions[str(thread.id)] = session_id
    save_sessions(sessions)

    await interaction.followup.send(f"✅ スレッド作成完了: {thread.mention}\nセッション `{session_id}` を引き継ぎます")

    # Claude Code実行（セッション引き継ぎ）
    await queue.put((thread, message, initial_prompt, sessions))
    await process_queue()


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # !コマンド（!sync等）を処理
    await bot.process_commands(message)

    if not isinstance(message.channel, discord.Thread):
        return
    if message.channel.parent_id != FORUM_CHANNEL_ID:
        return
    if str(message.author.id) not in ALLOWED_USERS:
        return

    prompt = message.content or ""

    # 画像添付をダウンロードしてプロンプトに追加
    image_paths = await download_attachments(message)
    if image_paths:
        paths_str = "\n".join(f"  - {p}" for p in image_paths)
        prompt += f"\n\n[添付画像（Readツールで閲覧可能）]\n{paths_str}"

    if not prompt.strip() or prompt.startswith("!"):
        return

    sessions = load_sessions()
    await queue.put((message.channel, message, prompt, sessions))
    await process_queue()


bot.run(TOKEN)
