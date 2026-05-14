#!/usr/bin/env python3
"""Notification hook: Claude Code の通知イベントを Discord に転送する。

permission_prompt / idle_prompt / elicitation_dialog /
elicitation_complete / elicitation_response / auth_success などを受信し、
Discord の該当スレッドにメッセージとして転送する。
"""
import os
import sys
import json
import socket
import urllib.request
import urllib.error


def main():
    try:
        raw = sys.stdin.read()
        input_data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError) as e:
        print(f"hook_notification: stdin parse error: {e}", file=sys.stderr)
        print("{}")
        sys.exit(0)

    try:
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
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            # bot 落ちてても通知転送だけだから黙って続行
            print(f"hook_notification: post error: {e}", file=sys.stderr)

        print("{}")
    except Exception as e:
        print(f"hook_notification unexpected error: {e}", file=sys.stderr)
        print("{}")
    sys.exit(0)


if __name__ == "__main__":
    main()
