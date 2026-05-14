#!/usr/bin/env python3
"""PreToolUse hook: Discord Bot に権限リクエストを送って判断を待つ。

sensitive path (.claude, .git, .env, .ssh, .vscode, .idea, .husky 等) への
Write/Edit/MultiEdit は Claude Code の --dangerously-skip-permissions
でもバイパスできないので、ここで検出 → Discord で承認 →
フック自身が直接書き込み、deny + 完了メッセージで Claude に「完了したので次へ」と伝える。

出力 (Claude Code 仕様):
  許可: {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
  拒否: {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}
"""

import os
import sys
import json
import socket
import urllib.request
import urllib.error

# Read-only ツール: 自動許可
SAFE_TOOLS = {
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "Agent", "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "TodoWrite", "ToolSearch", "Skill", "Monitor", "ScheduleWakeup",
    "ListMcpResourcesTool", "ReadMcpResourceTool",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
}

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Claude Code が --dangerously-skip-permissions でも保護するハードコードパス
SENSITIVE_DIR_NAMES = {
    ".claude", ".git", ".ssh", ".aws", ".azure", ".gnupg",
    ".vscode", ".idea", ".husky",
}
SENSITIVE_BASENAMES = {
    ".env", ".mcp.json", ".bashrc", ".zshrc", ".profile",
    ".bash_profile", ".zprofile", ".npmrc", ".pypirc", ".netrc",
    ".gitconfig", ".git-credentials",
}


def is_sensitive_path(path: str) -> bool:
    if not path:
        return False
    # nullbyte 含むパスは念のため sensitive 扱い（open() でも ValueError になるが先回り）
    if "\x00" in path:
        return True
    # 正規化して `..` や `.` を解消してから判定
    try:
        normalized = os.path.normpath(path)
    except (ValueError, TypeError):
        normalized = path
    p = normalized.replace("\\", "/").lower()
    parts = [seg for seg in p.split("/") if seg]
    for seg in parts[:-1]:
        if seg in SENSITIVE_DIR_NAMES:
            return True
    basename = parts[-1] if parts else ""
    if basename in SENSITIVE_BASENAMES:
        return True
    if basename.startswith(".env."):
        return True
    return False


def make_response(decision: str, reason: str = "") -> dict:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        out["hookSpecificOutput"]["permissionDecisionReason"] = reason
    return out


def perform_write(tool_name: str, tool_input: dict) -> str:
    file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    if not file_path:
        raise ValueError("file_path が空です")
    # シンボリックリンク/相対パスを実体に解決し、sensitive 判定を再確認
    try:
        resolved = os.path.realpath(file_path)
    except (OSError, ValueError):
        resolved = file_path
    if resolved != file_path:
        # 解決後パスが sensitive でも書き込み自体は承認済みなので続行するが、
        # シンボリックリンク先の書き換えはユーザーに見えにくいのでログに出す
        print(f"hook_pretooluse: file_path 解決 {file_path!r} -> {resolved!r}", file=sys.stderr)
    file_path = resolved

    if tool_name == "Write":
        content = tool_input.get("content", "")
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Wrote {len(content)} chars → {file_path}"

    if tool_name == "Edit":
        old_str = tool_input.get("old_string", "")
        new_str = tool_input.get("new_string", "")
        replace_all = bool(tool_input.get("replace_all", False))
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        if replace_all:
            if old_str not in content:
                raise ValueError(f"old_string が見つかりません: {file_path}")
            new_content = content.replace(old_str, new_str)
        else:
            count = content.count(old_str)
            if count == 0:
                raise ValueError(f"old_string が見つかりません: {file_path}")
            if count > 1:
                raise ValueError(f"old_string が {count} 箇所に一致（replace_all=false では一意が必要）")
            new_content = content.replace(old_str, new_str, 1)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        return f"Edited → {file_path}"

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits", []) or []
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        for i, ed in enumerate(edits):
            old_str = ed.get("old_string", "")
            new_str = ed.get("new_string", "")
            replace_all = bool(ed.get("replace_all", False))
            if replace_all:
                if old_str not in content:
                    raise ValueError(f"edit[{i}] の old_string が見つかりません")
                content = content.replace(old_str, new_str)
            else:
                count = content.count(old_str)
                if count == 0:
                    raise ValueError(f"edit[{i}] の old_string が見つかりません")
                if count > 1:
                    raise ValueError(f"edit[{i}] の old_string が複数箇所一致")
                content = content.replace(old_str, new_str, 1)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"MultiEdited ({len(edits)} 編集) → {file_path}"

    raise ValueError(f"非対応ツール: {tool_name}")


def ask_bot(payload: dict, timeout: int = 600) -> dict:
    port = os.environ.get("HOOK_PORT", "8585")
    token = os.environ.get("BRIDGE_AUTH_TOKEN", "")
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Bridge-Auth"] = token
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/permission",
        data=data,
        headers=headers,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


def extract_decision(result: dict) -> tuple[str, str]:
    hso = (result or {}).get("hookSpecificOutput", {}) or {}
    if "permissionDecision" in hso:
        return hso.get("permissionDecision", "deny"), hso.get("permissionDecisionReason", "") or ""
    decision = hso.get("decision", {}) or {}
    if isinstance(decision, dict):
        return decision.get("behavior", "deny"), decision.get("message", "") or ""
    return "deny", ""


def emit(response: dict):
    """stdout に1回だけ書き出す。重複出力でClaude側パース失敗するのを防ぐ"""
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()


def main():
    try:
        raw = sys.stdin.read()
        input_data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError) as e:
        # 入力が壊れている = フックを介さず Claude を直接動かしたいユーザー意図に合わせ allow
        print(f"hook_pretooluse: stdin parse error: {e}", file=sys.stderr)
        emit(make_response("allow"))
        sys.exit(0)

    try:
        tool_name = input_data.get("tool_name", "") or ""
        tool_input = input_data.get("tool_input", {}) or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        thread_id = os.environ.get("DISCORD_THREAD_ID", "")
        session_id = input_data.get("session_id", "")

        # 1. sensitive path への書き込み: 必ず Discord 確認 → 直接書き込み
        if tool_name in WRITE_TOOLS and is_sensitive_path(file_path):
            try:
                result = ask_bot({
                    "hook_type": "PreToolUse",
                    "tool_name": tool_name,
                    "tool_input": tool_input,
                    "thread_id": thread_id,
                    "session_id": session_id,
                    "sensitive": True,
                })
            except urllib.error.HTTPError as e:
                # 401/5xx: bot は生きてるが認証失敗/サーバーエラー → sensitive path は安全側に倒し deny
                emit(make_response("deny", f"ブリッジ認証/応答エラー (HTTP {e.code}): sensitive path 書き込みを拒否"))
                sys.exit(0)
            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                emit(make_response("deny", f"ブリッジへの接続失敗（sensitive path 確認取れず）: {e}"))
                sys.exit(0)
            except (json.JSONDecodeError, ValueError) as e:
                emit(make_response("deny", f"ブリッジ応答が不正: {e}"))
                sys.exit(0)

            decision, reason = extract_decision(result)
            if decision == "allow":
                try:
                    msg = perform_write(tool_name, tool_input)
                    emit(make_response(
                        "deny",
                        (
                            f"Bridge wrote the file directly (bypassing Claude Code's "
                            f"sensitive-path guard). {msg}. The file is now on disk — "
                            f"do NOT retry this tool call. Continue with the next step."
                        ),
                    ))
                except (OSError, ValueError, TypeError) as e:
                    # TypeError は content が None 等で f.write(None) になったケース
                    emit(make_response("deny", f"Bridge 直接書き込み失敗: {e}"))
            else:
                emit(make_response("deny", reason or "ユーザーが Discord で拒否しました"))
            sys.exit(0)

        # 2. 安全ツール: 自動許可
        if tool_name in SAFE_TOOLS:
            emit(make_response("allow"))
            sys.exit(0)

        # 3. AskUserQuestion は SKIP_PERMISSIONS でも必ず Discord に出す
        #    （print モードでは Claude 側に質問UIが無いので、bot で選択肢ボタンを出さないと詰む）
        skip = os.environ.get("BRIDGE_SKIP_PERMISSIONS", "").lower() in ("true", "1", "yes")
        if skip and tool_name != "AskUserQuestion":
            emit(make_response("allow"))
            sys.exit(0)

        # 4. Discord ボタンで判断
        try:
            result = ask_bot({
                "hook_type": "PreToolUse",
                "tool_name": tool_name,
                "tool_input": tool_input,
                "thread_id": thread_id,
                "session_id": session_id,
            })
            decision, reason = extract_decision(result)
            if decision == "allow":
                emit(make_response("allow"))
            else:
                emit(make_response("deny", reason or "ユーザーが拒否しました"))
        except urllib.error.HTTPError as e:
            # 401/5xx: 認証失敗/サーバーエラーは deny に倒す（allow フォールバックは抜け穴になる）
            print(f"hook_pretooluse bot HTTP error: {e.code}", file=sys.stderr)
            emit(make_response("deny", f"ブリッジ認証/応答エラー (HTTP {e.code})"))
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            # bot 未起動・落ちている: 詰まないように許可で抜ける
            print(f"hook_pretooluse bot unreachable: {e}", file=sys.stderr)
            emit(make_response("allow"))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"hook_pretooluse bot bad response: {e}", file=sys.stderr)
            emit(make_response("allow"))

    except Exception as e:
        # 想定外: 詰まないように allow
        print(f"hook_pretooluse unexpected error: {e}", file=sys.stderr)
        emit(make_response("allow"))

    sys.exit(0)


if __name__ == "__main__":
    main()
