#!/usr/bin/env python3
"""PermissionRequest hook: Claude Code の権限ダイアログを Discord ボタンで処理する。

PreToolUse は全ツール実行前に発火するが、PermissionRequest は
Claude Code が実際に権限確認ダイアログを表示しようとした時だけ発火する。

出力フォーマット (Claude Code 仕様):
  許可: {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}
  拒否: {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "deny", "message": "..."}}}
"""

import os
import sys
import json
import urllib.request

# 読み取り専用ツール — 自動許可（ボタン不要）
SAFE_TOOLS = {
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "Agent", "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskOutput",
}


def make_response(behavior: str, message: str = "", updated_input: dict = None) -> dict:
    """Claude Code 仕様の PermissionRequest レスポンスを生成"""
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


def main():
    try:
        input_data = json.load(sys.stdin)
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})

        # 安全なツールは即許可
        if tool_name in SAFE_TOOLS:
            print(json.dumps(make_response("allow")))
            sys.exit(0)

        port = os.environ.get("HOOK_PORT", "8585")
        thread_id = os.environ.get("DISCORD_THREAD_ID", "")

        payload = json.dumps({
            "hook_type": "PermissionRequest",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "thread_id": thread_id,
            "session_id": input_data.get("session_id", ""),
        }).encode("utf-8")

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/permission",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        with urllib.request.urlopen(req, timeout=600) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        print(json.dumps(result))

    except Exception as e:
        # エラー時は許可（フック障害でブロックしない）
        print(f"hook_permission_request error: {e}", file=sys.stderr)
        print(json.dumps(make_response("allow")))

    sys.exit(0)


if __name__ == "__main__":
    main()
