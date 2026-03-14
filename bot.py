import os
import sys
import json
import asyncio
import re
from pathlib import Path
from datetime import datetime

# Windows cp932 で絵文字が encode できない問題を回避
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
FORUM_CHANNEL_ID = int(os.getenv("FORUM_CHANNEL_ID", "0"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
ALLOWED_USERS = set(os.getenv("ALLOWED_USERS", "").split(","))
SKIP_PERMISSIONS = os.getenv("SKIP_PERMISSIONS", "false").lower() in ("true", "1", "yes")

SESSIONS_FILE = Path(__file__).parent / "sessions.json"
SOFT_TIMEOUT = 600   # 10分で「まだやってるよ」メッセージ
HARD_TIMEOUT = 3600  # 1時間で強制終了

# フォーラムタグ名
TAG_RUNNING = "実行中"
TAG_COMPLETED = "完了"
TAG_ERROR = "エラー"


def load_sessions() -> dict:
    if SESSIONS_FILE.exists():
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    return {}


def save_sessions(sessions: dict):
    SESSIONS_FILE.write_text(json.dumps(sessions, indent=2), encoding="utf-8")


def strip_ansi(text: str) -> str:
    return re.sub(r"\x1B\[[0-9;]*[a-zA-Z]", "", text)


def parse_claude_output(raw: str, err: str, session_id: str | None) -> tuple[str, str | None]:
    """Claude CLIの出力をパースして (応答テキスト, セッションID) を返す"""
    print(f"[DEBUG] claude stdout ({len(raw)} chars): {raw[:500]}")
    if err:
        print(f"[DEBUG] claude stderr: {err[:300]}")

    new_session_id = session_id
    output = ""
    try:
        data = json.loads(raw)
        new_session_id = data.get("session_id", session_id)
        result = data.get("result", "")
        if isinstance(result, str):
            output = result
        elif isinstance(result, list):
            texts = [b.get("text", "") for b in result if b.get("type") == "text"]
            output = "\n".join(texts)
        else:
            output = str(result)
        # resultが空でもJSON全体にテキストがあればフォールバック
        if not output:
            output = data.get("text", "") or json.dumps(data, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        output = strip_ansi(raw) if raw else ""

    if not output and err:
        output = f"エラー: {err}"
    if not output:
        output = "（Claude Codeからの応答が空でした。再度試してください）"

    return output.strip(), new_session_id


async def run_claude(prompt: str, session_id: str | None = None, thread: discord.Thread | None = None, thread_title: str | None = None) -> tuple[str, str | None]:
    """Claude Code CLI を実行して (応答テキスト, セッションID) を返す。
    ソフトタイムアウト時はスレッドにメッセージを送り、完了後に編集する。"""
    args = ["claude", "-p", "--output-format", "json"]
    if SKIP_PERMISSIONS:
        args.insert(1, "--dangerously-skip-permissions")
    if session_id:
        args.extend(["--resume", session_id])
    # 新規セッション時はスレッドタイトルをコンテキストとして付加
    if not session_id and thread_title:
        prompt = f"[スレッドタイトル: {thread_title}]\n\n{prompt}"
    args.append(prompt)

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env["PYTHONIOENCODING"] = "utf-8"

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    # まずソフトタイムアウトで待つ
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=SOFT_TIMEOUT)
    except asyncio.TimeoutError:
        # まだ動いてる → メッセージ送って待ち続ける
        elapsed = SOFT_TIMEOUT // 60
        placeholder = None
        if thread:
            placeholder = await thread.send(f"まだ処理中です（{elapsed}分経過）… 終わったら更新します")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=HARD_TIMEOUT - SOFT_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            msg = f"タイムアウトしました（{HARD_TIMEOUT // 60}分超過、強制終了）"
            if placeholder:
                await placeholder.edit(content=msg)
            return msg, session_id

        raw = stdout.decode("utf-8", errors="replace").strip()
        err = stderr.decode("utf-8", errors="replace").strip()
        output, new_session_id = parse_claude_output(raw, err, session_id)

        # プレースホルダーを結果で編集
        if placeholder:
            chunks = split_message(output, 2000)
            await placeholder.edit(content=chunks[0])
            # 2000文字超えてたら残りは追加送信
            for chunk in chunks[1:]:
                await thread.send(chunk)
            return None, new_session_id  # Noneで「もう送信済み」を示す

        return output, new_session_id

    raw = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    return parse_claude_output(raw, err, session_id)


# --- タグ管理 ---

async def get_or_create_tag(forum: discord.ForumChannel, name: str) -> discord.ForumTag:
    """フォーラムタグを取得、なければ作成"""
    for tag in forum.available_tags:
        if tag.name == name:
            return tag
    # タグ作成
    new_tags = list(forum.available_tags) + [discord.ForumTag(name=name)]
    await forum.edit(available_tags=new_tags)
    # 再取得
    forum = await forum.guild.fetch_channel(forum.id)
    for tag in forum.available_tags:
        if tag.name == name:
            return tag
    return None


async def set_thread_tag(thread: discord.Thread, tag_name: str):
    """スレッドのタグを指定のものだけにする"""
    try:
        forum = thread.parent
        if not forum:
            forum = await thread.guild.fetch_channel(thread.parent_id)

        tag = await get_or_create_tag(forum, tag_name)
        if tag:
            # 他のステータスタグを除去して新しいのだけセット
            status_names = {TAG_RUNNING, TAG_COMPLETED, TAG_ERROR}
            keep_tags = [t for t in thread.applied_tags if t.name not in status_names]
            keep_tags.append(tag)
            await thread.edit(applied_tags=keep_tags[:5])  # Discord上限5個
    except Exception as e:
        print(f"タグ設定エラー: {e}")


# --- ログ ---

async def send_log(guild: discord.Guild, user: str, thread_name: str, prompt: str, result: str, status: str):
    """ログチャンネルに記録を送信"""
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
        # 結果は長すぎる場合があるので切り詰め
        if result:
            embed.add_field(name="応答", value=result[:1024], inline=False)
        await log_ch.send(embed=embed)
    except Exception as e:
        print(f"ログ送信エラー: {e}")


# --- メッセージ分割送信 ---

async def send_response(channel: discord.Thread, text: str):
    """Markdown対応で2000文字分割送信"""
    if not text:
        await channel.send("（空の応答）")
        return

    # 2000文字ずつに分割（コードブロックの途中で切れないよう考慮）
    chunks = split_message(text, 2000)
    for chunk in chunks:
        await channel.send(chunk)


def split_message(text: str, limit: int = 2000) -> list[str]:
    """メッセージを制限内で分割。コードブロック内なら閉じて次で開く"""
    if len(text) <= limit:
        return [text]

    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        # 改行位置で切る
        cut = text.rfind("\n", 0, limit)
        if cut == -1 or cut < limit // 2:
            cut = limit

        chunk = text[:cut]
        text = text[cut:].lstrip("\n")

        # コードブロックの開閉チェック
        backtick_count = chunk.count("```")
        if backtick_count % 2 == 1:
            # 開いたまま → 閉じる＆次で開く
            chunk += "\n```"
            text = "```\n" + text

        chunks.append(chunk)

    return chunks


# --- Bot setup ---

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 同時実行制御
queue = asyncio.Queue()
processing = False


async def process_queue():
    global processing
    if processing:
        return
    processing = True
    try:
        while not queue.empty():
            thread, message, prompt, sessions = await queue.get()
            status = TAG_RUNNING
            result = ""
            try:
                # タグ: 実行中
                await set_thread_tag(thread, TAG_RUNNING)
                await send_log(
                    thread.guild, str(message.author),
                    thread.name, prompt, "", TAG_RUNNING,
                )

                async with thread.typing():
                    session_id = sessions.get(str(thread.id))
                    result, new_session_id = await run_claude(prompt, session_id, thread, thread.name)

                    if new_session_id:
                        sessions[str(thread.id)] = new_session_id
                        save_sessions(sessions)

                    # result=None はrun_claude内で送信済み
                    if result is not None:
                        await send_response(thread, result)
                    status = TAG_COMPLETED

            except Exception as e:
                result = str(e)
                status = TAG_ERROR
                await thread.send(f"エラーが発生しました: {e}")
            finally:
                # タグ: 完了 or エラー
                await set_thread_tag(thread, status)
                await send_log(
                    thread.guild, str(message.author),
                    thread.name, prompt, result, status,
                )
                queue.task_done()
    finally:
        processing = False


@bot.event
async def on_ready():
    print(f"Bot起動: {bot.user}")
    print(f"フォーラムチャンネルID: {FORUM_CHANNEL_ID}")
    print(f"ログチャンネルID: {LOG_CHANNEL_ID}")
    print(f"許可ユーザー: {ALLOWED_USERS}")


@bot.event
async def on_message(message: discord.Message):
    """フォーラムスレッド内のメッセージ → Claude Code実行"""
    if message.author.bot:
        return

    if not isinstance(message.channel, discord.Thread):
        return
    if message.channel.parent_id != FORUM_CHANNEL_ID:
        return

    if str(message.author.id) not in ALLOWED_USERS:
        return

    prompt = message.content
    if not prompt or prompt.startswith("!"):
        return

    sessions = load_sessions()
    await queue.put((message.channel, message, prompt, sessions))
    await process_queue()


bot.run(TOKEN)
