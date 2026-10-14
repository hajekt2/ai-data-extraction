#!/usr/bin/env python3
"""Extract Hermes Agent conversations from its SQLite state and legacy sessions."""

import json
import os
import sqlite3
from pathlib import Path

from extract_common import print_summary, write_conversations


def find_hermes_home():
    configured = os.environ.get("HERMES_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def decode_json(value):
    if not isinstance(value, str) or not value:
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def add_if_present(target, key, value):
    if value is not None and value != "":
        target[key] = value


def find_assistant(messages, tool_call_id=None):
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        if tool_call_id:
            for call in message.get("tool_uses", []):
                if call.get("id") == tool_call_id:
                    return message
        else:
            return message
    return None


def normalize_hermes_messages(rows, session_model=None):
    messages = []
    unclaimed_results = []
    for row in rows:
        role = row.get("role")
        if role == "session_meta":
            continue
        if role == "tool":
            result = {
                "tool": row.get("tool_name"),
                "tool_call_id": row.get("tool_call_id"),
                "content": decode_json(row.get("content")),
                "timestamp": row.get("timestamp"),
            }
            assistant = find_assistant(messages, row.get("tool_call_id"))
            if assistant is None:
                unclaimed_results.append(result)
            else:
                assistant.setdefault("tool_results", []).append(result)
            continue
        if role not in ("user", "assistant"):
            continue

        content = row.get("content") or ""
        normalized = {
            "role": role,
            "content": content,
            "timestamp": row.get("timestamp"),
        }
        if role == "assistant":
            add_if_present(normalized, "model", session_model)
            add_if_present(normalized, "finish_reason", row.get("finish_reason"))
            add_if_present(normalized, "reasoning", row.get("reasoning"))
            add_if_present(normalized, "reasoning_content", row.get("reasoning_content"))
            for key in ("reasoning_details", "codex_reasoning_items", "codex_message_items"):
                value = decode_json(row.get(key))
                add_if_present(normalized, key, value)
            tool_calls = decode_json(row.get("tool_calls"))
            if tool_calls:
                normalized["tool_uses"] = tool_calls
        messages.append(normalized)
    return messages, unclaimed_results


def session_conversation(session, rows, source_file):
    messages, unclaimed_results = normalize_hermes_messages(rows, session.get("model"))
    if not messages:
        return None
    conversation = {
        "messages": messages,
        "source": "hermes",
        "session_id": session.get("id"),
        "source_file": str(source_file),
    }
    for source_key, output_key in (
        ("title", "title"),
        ("display_name", "display_name"),
        ("source", "session_source"),
        ("cwd", "project_path"),
        ("session_key", "session_key"),
        ("chat_id", "chat_id"),
        ("chat_type", "chat_type"),
        ("thread_id", "thread_id"),
        ("git_branch", "git_branch"),
        ("git_repo_root", "git_repo_root"),
        ("started_at", "started_at"),
        ("ended_at", "ended_at"),
        ("end_reason", "end_reason"),
    ):
        add_if_present(conversation, output_key, session.get(source_key))
    if session.get("model"):
        conversation["model"] = session["model"]
    if session.get("message_count") is not None:
        conversation["message_count_recorded"] = session["message_count"]
    if session.get("tool_call_count") is not None:
        conversation["tool_call_count_recorded"] = session["tool_call_count"]
    if unclaimed_results:
        conversation["tool_results"] = unclaimed_results
    return conversation


def table_columns(connection, table):
    return [row[1] for row in connection.execute('PRAGMA table_info("{}")'.format(table))]


def extract_state_database(state_file):
    conversations = []
    connection = sqlite3.connect("file:{}?mode=ro".format(state_file), uri=True)
    try:
        session_columns = table_columns(connection, "sessions")
        message_columns = table_columns(connection, "messages")
        if not session_columns or not message_columns:
            return conversations

        session_fields = [
            "id", "source", "model", "title", "display_name", "cwd", "session_key",
            "chat_id", "chat_type", "thread_id", "git_branch", "git_repo_root",
            "started_at", "ended_at", "end_reason", "message_count", "tool_call_count",
        ]
        message_fields = [
            "id", "session_id", "role", "content", "tool_call_id", "tool_calls",
            "tool_name", "timestamp", "finish_reason", "reasoning", "reasoning_content",
            "reasoning_details", "codex_reasoning_items", "codex_message_items",
        ]
        selected_sessions = [field for field in session_fields if field in session_columns]
        selected_messages = [field for field in message_fields if field in message_columns]
        sessions = {}
        for row in connection.execute(
            "SELECT {} FROM sessions ORDER BY id".format(", ".join(selected_sessions))
        ):
            session = dict(zip(selected_sessions, row))
            sessions[session.get("id")] = session

        grouped = {}
        query = "SELECT {} FROM messages ORDER BY id".format(", ".join(selected_messages))
        for row in connection.execute(query):
            message = dict(zip(selected_messages, row))
            grouped.setdefault(message.get("session_id"), []).append(message)

        for session_id, rows in grouped.items():
            session = dict(sessions.get(session_id) or {"id": session_id})
            conversation = session_conversation(session, rows, state_file)
            if conversation:
                conversations.append(conversation)
    finally:
        connection.close()
    return conversations


def extract_legacy_session(session_file):
    try:
        with session_file.open("r", encoding="utf-8") as source:
            data = json.load(source)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return None
    session = {
        "id": data.get("session_id") or session_file.stem,
        "model": data.get("model"),
        "source": data.get("platform"),
        "started_at": data.get("session_start"),
        "ended_at": data.get("last_updated"),
    }
    rows = []
    for index, message in enumerate(data["messages"], start=1):
        if not isinstance(message, dict):
            continue
        row = dict(message)
        row.setdefault("id", index)
        row.setdefault("timestamp", None)
        rows.append(row)
    return session_conversation(session, rows, session_file)


def extract_hermes_data():
    home = find_hermes_home()
    conversations = []
    database_ids = set()
    state_file = home / "state.db"
    if state_file.exists():
        conversations = extract_state_database(state_file)
        database_ids = {item.get("session_id") for item in conversations}

    for session_file in sorted((home / "sessions").glob("session_*.json")):
        conversation = extract_legacy_session(session_file)
        if conversation and conversation.get("session_id") not in database_ids:
            conversations.append(conversation)
    for session_file in sorted((home / "sessions").glob("*.jsonl")):
        if session_file.name == "sessions.json" or session_file.stem in database_ids:
            continue
        rows = []
        try:
            with session_file.open("r", encoding="utf-8") as source:
                for line in source:
                    if line.strip():
                        row = json.loads(line)
                        if isinstance(row, dict):
                            rows.append(row)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        session = {"id": session_file.stem}
        conversation = session_conversation(session, rows, session_file)
        if conversation:
            conversations.append(conversation)
    return conversations


def main():
    print("=" * 80)
    print("HERMES COMPLETE DATA EXTRACTION")
    print("=" * 80)
    print()
    home = find_hermes_home()
    print("Searching Hermes storage: {}".format(home))
    conversations = extract_hermes_data()
    print()
    print("EXTRACTION COMPLETE")
    print("Total conversations: {:,}".format(len(conversations)))
    if not conversations:
        print("No conversations found!")
        return
    print_summary(conversations)
    output_file = write_conversations(conversations, "hermes_conversations")
    print("Saved to: {}".format(output_file))
    print("Format: JSONL (one conversation per line)")


if __name__ == "__main__":
    main()
