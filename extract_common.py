#!/usr/bin/env python3
"""Shared helpers for the local agent conversation extractors."""

import json
from datetime import datetime
from pathlib import Path


def _content_parts(content):
    """Return visible text, thinking text, tool calls, and non-text parts."""
    if isinstance(content, str):
        return content, "", [], []

    if not isinstance(content, list):
        if content is None:
            return "", "", [], []
        return str(content), "", [], []

    text_parts = []
    thinking_parts = []
    tool_calls = []
    other_parts = []

    for item in content:
        if not isinstance(item, dict):
            if item is not None:
                other_parts.append(item)
            continue

        item_type = item.get("type")
        if item_type == "text":
            text = item.get("text", "")
            if text:
                text_parts.append(text)
        elif item_type == "thinking":
            thinking = item.get("thinking", "")
            if thinking:
                thinking_parts.append(thinking)
        elif item_type == "toolCall":
            tool_calls.append(item)
        else:
            other_parts.append(item)

    return "\n".join(text_parts), "\n".join(thinking_parts), tool_calls, other_parts


def _add_optional(message, key, value):
    if value is not None and value != "":
        message[key] = value


def _make_tool_result(message):
    text, _, _, other_parts = _content_parts(message.get("content"))
    result = {
        "tool": message.get("toolName"),
        "tool_call_id": message.get("toolCallId"),
        "content": text,
    }
    if other_parts:
        result["content_parts"] = other_parts
    if message.get("isError") is not None:
        result["is_error"] = message.get("isError")
    _add_optional(result, "error_message", message.get("errorMessage"))
    _add_optional(result, "details", message.get("details"))
    _add_optional(result, "timestamp", message.get("timestamp"))
    return result


def _attach_tool_result(messages, result, unclaimed):
    """Attach a tool result to its assistant call, preserving unpaired results."""
    call_id = result.get("tool_call_id")
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        calls = message.get("tool_uses", [])
        if call_id and any(call.get("id") == call_id for call in calls):
            message.setdefault("tool_results", []).append(result)
            return

    for message in reversed(messages):
        if message.get("role") == "assistant":
            message.setdefault("tool_results", []).append(result)
            return

    unclaimed.append(result)


def parse_pi_style_session(session_file, source, metadata=None):
    """Parse Pi/OpenClaw JSONL session events into one normalized conversation."""
    metadata = dict(metadata or {})
    messages = []
    unclaimed_tool_results = []

    try:
        with session_file.open("r", encoding="utf-8") as source_file:
            for raw_line in source_file:
                if not raw_line.strip():
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue

                event_type = event.get("type")
                if event_type == "session":
                    _add_optional(metadata, "project_path", event.get("cwd"))
                    _add_optional(metadata, "session_version", event.get("version"))
                    _add_optional(metadata, "parent_session_id", event.get("parentSession"))
                    continue

                if event_type == "session.started":
                    data = event.get("data") or {}
                    _add_optional(metadata, "project_path", event.get("workspaceDir"))
                    _add_optional(metadata, "project_path", data.get("workspaceDir"))
                    _add_optional(metadata, "agent_id", data.get("agentId"))
                    _add_optional(metadata, "trigger", data.get("trigger"))
                    _add_optional(metadata, "session_key", event.get("sessionKey"))
                    _add_optional(metadata, "model", event.get("modelId"))
                    _add_optional(metadata, "provider", event.get("provider"))
                    _add_optional(metadata, "started_at", event.get("ts"))
                    continue

                if event_type == "session.ended":
                    data = event.get("data") or {}
                    _add_optional(metadata, "ended_at", event.get("ts"))
                    _add_optional(metadata, "end_status", data.get("status"))
                    continue

                message = event.get("message")
                if not isinstance(message, dict):
                    continue

                role = message.get("role")
                timestamp = message.get("timestamp") or event.get("timestamp")
                if role == "toolResult":
                    result = _make_tool_result(message)
                    _add_optional(result, "timestamp", timestamp)
                    _attach_tool_result(messages, result, unclaimed_tool_results)
                    continue
                if role not in ("user", "assistant"):
                    continue

                text, thinking, tool_calls, other_parts = _content_parts(
                    message.get("content")
                )
                if not text and not thinking and not tool_calls and not other_parts:
                    continue

                normalized = {
                    "role": role,
                    "content": text,
                    "timestamp": timestamp,
                }
                if role == "assistant":
                    _add_optional(normalized, "model", message.get("model"))
                    _add_optional(normalized, "provider", message.get("provider"))
                    _add_optional(normalized, "thinking", thinking)
                    if tool_calls:
                        normalized["tool_uses"] = tool_calls
                if other_parts:
                    normalized["content_parts"] = other_parts
                _add_optional(normalized, "details", message.get("details"))
                _add_optional(normalized, "stop_reason", message.get("stopReason"))
                messages.append(normalized)

    except (OSError, UnicodeDecodeError):
        return None

    if not messages:
        return None

    conversation = {
        "messages": messages,
        "source": source,
        "session_id": metadata.pop("session_id", session_file.stem),
        "source_file": str(session_file),
    }
    for key, value in metadata.items():
        if value is not None and value != "":
            conversation[key] = value
    # Session-level time lets a corpus answer "what period does this source cover?"
    # without re-reading every message.
    stamps = [m["timestamp"] for m in messages if m.get("timestamp")]
    if stamps:
        conversation.setdefault("created_at", stamps[0])
        conversation.setdefault("updated_at", stamps[-1])
    if unclaimed_tool_results:
        conversation["tool_results"] = unclaimed_tool_results
    return conversation


def write_conversations(conversations, prefix):
    """Write normalized conversations and return the output path."""
    output_dir = Path("extracted_data")
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / (prefix + "_" + timestamp + ".jsonl")
    with output_file.open("w", encoding="utf-8") as destination:
        for conversation in conversations:
            destination.write(json.dumps(conversation, ensure_ascii=False) + "\n")
    return output_file


def print_summary(conversations):
    """Print the common extraction statistics used by extract_all.sh."""
    total_messages = sum(len(item["messages"]) for item in conversations)
    with_tools = sum(
        1
        for item in conversations
        if "tool_results" in item
        or any("tool_uses" in message or "tool_results" in message for message in item["messages"])
    )
    complete = sum(
        1 for item in conversations if any(message["role"] == "assistant" for message in item["messages"])
    )
    print("Complete conversations: {:,}".format(complete))
    print("Total messages: {:,}".format(total_messages))
    print("With tool use/diffs: {:,}".format(with_tools))
