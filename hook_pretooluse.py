#!/usr/bin/env python3
"""PreToolUse hook: Discord Bot に権限リクエストを送信し、ユーザーの判断を待つ。

Claude Code の PreToolUse フックとして呼ばれる。
stdin からツール情報を受け取り、Bot の HTTP サーバーに POST して結果を stdout に返す。

特殊処理:
  - sensitive path (.claude, .git, .env, .ssh 等) への Write/Edit/MultiEdit は
    Claude Code の --dangerously-skip-permissions でもバイパスできない保護がある。
    ここで検出し、Discord ボタンで確認 → 承認されたらフック自身が Python で
    直接書き込み、deny + 完了メッセージを返して Claude に「完了したので次へ」と伝える。

出力フォーマット (Claude Code 仕様):
  許可: {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}
  拒否: {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}
"""

import os
import sys
import json
import urllib.request

# 読み取り専用ツール — 自動許可（ボタン不要）
SAFE_TOOLS = {
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "Agent", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskOutput",
    "TodoWrite", "ToolSearch", "Skill", "Monitor",
}

# Write 系ツール
WRITE_TOOLS = {"Write", "Edit", "MultiEdit"}

# sensitive path 判定用パターン
SENSITIVE_DIR_NAMES = {
    ".claude", ".git", ".ssh", ".aws", ".azure", ".gnupg", ".config/gh",
}
SENSITIVE_BASENAMES = {
    ".env", ".mcp.json", ".bashrc", ".zshrc", ".profile",
    ".bash_profile", ".zprofile", ".npmrc", ".pypirc", ".netrc",
    ".gitconfig", ".git-credentials",
}


def is_sensitive_path(path: str) -> bool:
    if not path:
        return False
    p = path.replace("\\", "/").lower()
    parts = [seg for seg in p.split("/") if seg]
    # いずれかのディレクトリセグメントが sensitive 名
    for seg in parts[:-1]:  # 最後は basename
        if seg in SENSITIVE_DIR_NAMES:
            return True
    # basename が sensitive
    basename = parts[-1] if parts else ""
    if basename in SENSITIVE_BASENAMES:
        return True
    if basename.startswith(".env."):
        return True
    return False


def make_response(decision: str, reason: str = "") -> dict:
    """Claude Code 仕様の PreToolUse レスポンスを生成"""
    result = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        result["hookSpecificOutput"]["permissionDecisionReason"] = reason
    return result


def perform_write(tool_name: str, tool_input: dict) -> str:
    """ファイル操作を直接実行（Claude Code の保護チェックを迂回）"""
    file_path = tool_input.get("file_path", "")
    if not file_path:
        raise ValueError("file_path が空です")

    if tool_name == "Write":
        content = tool_input.get("content", "")
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
                raise ValueError(f"old_string が {count} 箇所に一致（replace_all=false では一意である必要あり）")
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
    """bot の /permission に POST して結果を取得"""
    port = os.environ.get("HOOK_PORT", "8585")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/permission",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_decision(result: dict) -> tuple[str, str]:
    """bot のレスポンスから (decision, reason) を取り出す"""
    hso = result.get("hookSpecificOutput", {}) or {}
    # PreToolUse 形式
    if "permissionDecision" in hso:
        return hso.get("permissionDecision", "deny"), hso.get("permissionDecisionReason", "") or ""
    # PermissionRequest 形式（互換用）
    decision = hso.get("decision", {}) or {}
    if isinstance(decision, dict):
        behavior = decision.get("behavior", "deny")
        message = decision.get("message", "") or ""
        return behavior, message
    return "deny", ""


def main():
    try:
        input_data = json.load(sys.stdin)
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {}) or {}
        file_path = tool_input.get("file_path", "") if isinstance(tool_input, dict) else ""
        thread_id = os.environ.get("DISCORD_THREAD_ID", "")
        session_id = input_data.get("session_id", "")

        # --- 1. sensitive path 書き込み系: 必ず Discord 確認 → 直接書き込み ---
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
            except Exception as e:
                print(json.dumps(make_response(
                    "deny",
                    f"ブリッジへの接続失敗（sensitive path 確認取れず）: {e}",
                )))
                sys.exit(0)

            decision, reason = extract_decision(result)
            if decision == "allow":
                try:
                    msg = perform_write(tool_name, tool_input)
                    print(json.dumps(make_response(
                        "deny",
                        (
                            f"Bridge wrote the file directly (bypassing Claude Code's "
                            f"sensitive-path guard). {msg}. The file is now on disk — "
                            f"do NOT retry this tool call. Continue with the next step."
                        ),
                    )))
                except Exception as e:
                    print(json.dumps(make_response(
                        "deny",
                        f"Bridge 直接書き込み失敗: {e}",
                    )))
            else:
                print(json.dumps(make_response(
                    "deny",
                    reason or "ユーザーが Discord で拒否しました",
                )))
            sys.exit(0)

        # --- 2. 読み取り専用など安全なツール: 自動許可 ---
        if tool_name in SAFE_TOOLS:
            print(json.dumps(make_response("allow")))
            sys.exit(0)

        # --- 3. 通常ツール: SKIP_PERMISSIONS モードでは自動許可 ---
        if os.environ.get("BRIDGE_SKIP_PERMISSIONS", "").lower() in ("true", "1", "yes"):
            print(json.dumps(make_response("allow")))
            sys.exit(0)

        # --- 4. それ以外: Discord ボタンで判断 ---
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
                print(json.dumps(make_response("allow")))
            else:
                print(json.dumps(make_response("deny", reason or "ユーザーが拒否しました")))
        except Exception as e:
            # Bot 接続失敗時は許可（壊れて詰まないように）
            print(f"hook_pretooluse bot unreachable: {e}", file=sys.stderr)
            print(json.dumps(make_response("allow")))

    except Exception as e:
        # 何かあっても許可にフォールバック（フック障害でブロックしない）
        print(f"hook_pretooluse error: {e}", file=sys.stderr)
        print(json.dumps(make_response("allow")))

    sys.exit(0)


if __name__ == "__main__":
    main()
