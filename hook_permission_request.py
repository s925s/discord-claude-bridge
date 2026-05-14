#!/usr/bin/env python3
"""PermissionRequest hook: Claude Code の権限ダイアログを Discord ボタンに転送。

PreToolUse は全ツール実行前に発火するが、PermissionRequest は
Claude Code が実際に権限確認ダイアログを表示しようとした時だけ発火する。

出力 (Claude Code 仕様):
  許可: {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
  拒否: {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "message": "..."}}}
"""

import os
import sys
import json
import socket
import urllib.request
import urllib.error

# hook_pretooluse.py の SAFE_TOOLS と合わせる
SAFE_TOOLS = {
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "Agent", "TaskCreate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TaskUpdate",
    "TodoWrite", "ToolSearch", "Skill", "Monitor", "ScheduleWakeup",
    "ListMcpResourcesTool", "ReadMcpResourceTool",
    "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
}


def make_response(behavior: str, message: str = "", updated_input: dict | None = None) -> dict:
    decision = {"behavior": behavior}
    if behavior == "deny" and message:
        decision["message"] = message
    if behavior == "allow" and updated_input:
        decision["updatedInput"] = updated_input
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": decision,
        }
    }


def emit(response: dict):
    sys.stdout.write(json.dumps(response))
    sys.stdout.flush()


def main():
    try:
        raw = sys.stdin.read()
        input_data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError) as e:
        print(f"hook_permission_request: stdin parse error: {e}", file=sys.stderr)
        emit(make_response("allow"))
        sys.exit(0)

    try:
        tool_name = input_data.get("tool_name", "") or ""
        tool_input = input_data.get("tool_input", {}) or {}
        if not isinstance(tool_input, dict):
            tool_input = {}

        if tool_name in SAFE_TOOLS:
            emit(make_response("allow"))
            sys.exit(0)

        port = os.environ.get("HOOK_PORT", "8585")
        token = os.environ.get("BRIDGE_AUTH_TOKEN", "")
        thread_id = os.environ.get("DISCORD_THREAD_ID", "")

        payload = json.dumps({
            "hook_type": "PermissionRequest",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "thread_id": thread_id,
            "session_id": input_data.get("session_id", ""),
        }).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Bridge-Auth"] = token
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/permission",
            data=payload,
            headers=headers,
        )

        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                result = json.loads(resp.read().decode("utf-8", errors="replace"))
            # bot から返ってきたレスポンスを PermissionRequest 形式に正規化
            hso = result.get("hookSpecificOutput", {}) if isinstance(result, dict) else {}
            if "decision" in hso:
                emit(result)
            elif "permissionDecision" in hso:
                # PreToolUse 形式 → PermissionRequest 形式に変換
                dec = hso.get("permissionDecision", "deny")
                reason = hso.get("permissionDecisionReason", "")
                emit(make_response(dec, reason))
            else:
                emit(make_response("allow"))
        except urllib.error.HTTPError as e:
            print(f"hook_permission_request bot HTTP error: {e.code}", file=sys.stderr)
            # 認証/サーバーエラーは deny に倒して安全側に
            emit(make_response("deny", f"ブリッジ認証/応答エラー (HTTP {e.code})"))
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            print(f"hook_permission_request bot unreachable: {e}", file=sys.stderr)
            emit(make_response("allow"))
        except (json.JSONDecodeError, ValueError) as e:
            print(f"hook_permission_request bot bad response: {e}", file=sys.stderr)
            emit(make_response("allow"))

    except Exception as e:
        print(f"hook_permission_request unexpected error: {e}", file=sys.stderr)
        emit(make_response("allow"))

    sys.exit(0)


if __name__ == "__main__":
    main()
