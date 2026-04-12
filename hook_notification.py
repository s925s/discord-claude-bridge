#!/usr/bin/env python3
"""Notification hook: Claude Code の通知イベントを Discord に転送する。

permission_prompt / idle_prompt / elicitation_dialog などを受信し、
Discord の該当スレッドにメッセージとして転送する。
ブロッキング呼び出しではないので即座に空レスポンスを返す。
"""
import os
import sys
import json
import urllib.request


def main():
    try:
        input_data = json.load(sys.stdin)

        port = os.environ.get("HOOK_PORT", "8585")
        thread_id = os.environ.get("DISCORD_THREAD_ID", "")

        payload = json.dumps({
            "message": input_data.get("message", ""),
            "title": input_data.get("title", ""),
            "notification_type": input_data.get("notification_type", ""),
            "thread_id": thread_id,
            "session_id": input_data.get("session_id", ""),
        }).encode("utf-8")

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/notification",
            data=payload,
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as e:
            print(f"notification post error: {e}", file=sys.stderr)

        print(json.dumps({}))
    except Exception as e:
        print(f"hook_notification error: {e}", file=sys.stderr)
        print(json.dumps({}))
    sys.exit(0)


if __name__ == "__main__":
    main()
