#!/usr/bin/env python3
"""PreToolUse hook: Discord Bot に権限リクエストを送信し、ユーザーの判断を待つ。

Claude Code の PreToolUse フックとして呼ばれる。
stdin からツール情報を受け取り、Bot の HTTP サーバーに POST して結果を stdout に返す。

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
}


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


def main():
    try:
        input_data = json.load(sys.stdin)
        tool_name = input_data.get("tool_name", "")

        # 安全なツールは即許可
        if tool_name in SAFE_TOOLS:
            print(json.dumps(make_response("allow")))
            sys.exit(0)

        port = os.environ.get("HOOK_PORT", "8585")
        thread_id = os.environ.get("DISCORD_THREAD_ID", "")

        payload = json.dumps({
            "hook_type": "PreToolUse",
            "tool_name": tool_name,
            "tool_input": input_data.get("tool_input", {}),
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
        print(json.dumps(make_response("allow")))

    sys.exit(0)


if __name__ == "__main__":
    main()
