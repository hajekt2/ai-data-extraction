#!/usr/bin/env python3
"""Extract OpenClaw agent session history from local JSONL files."""

import os
from pathlib import Path

from extract_common import parse_pi_style_session, print_summary, write_conversations


def find_openclaw_home():
    configured = os.environ.get("OPENCLAW_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".openclaw"


def find_openclaw_session_directories():
    root = find_openclaw_home() / "agents"
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob("*/sessions") if path.is_dir())


def find_openclaw_sessions():
    files = []
    for directory in find_openclaw_session_directories():
        # Trajectory files contain derived lifecycle metadata, not conversation messages.
        files.extend(path for path in directory.glob("*.jsonl") if not path.name.endswith(".trajectory.jsonl"))
    return sorted(files)


def extract_openclaw_session(session_file):
    agent_id = session_file.parent.parent.name
    trajectory_file = session_file.with_name(session_file.name.replace(".jsonl", ".trajectory.jsonl"))
    metadata = {
        "session_id": session_file.stem,
        "agent_id": agent_id,
    }
    if trajectory_file.exists():
        metadata["trajectory_file"] = str(trajectory_file)
    return parse_pi_style_session(session_file, "openclaw", metadata)


def main():
    print("=" * 80)
    print("OPENCLAW COMPLETE DATA EXTRACTION")
    print("=" * 80)
    print()
    print("Searching for OpenClaw agent sessions...")
    directories = find_openclaw_session_directories()
    if not directories:
        print("No OpenClaw agent session directories found!")
        return
    for directory in directories:
        print("   - {}".format(directory))

    conversations = []
    for session_file in find_openclaw_sessions():
        conversation = extract_openclaw_session(session_file)
        if conversation:
            conversations.append(conversation)

    print()
    print("EXTRACTION COMPLETE")
    print("Total conversations: {:,}".format(len(conversations)))
    if not conversations:
        print("No conversations found!")
        return
    print_summary(conversations)
    output_file = write_conversations(conversations, "openclaw_conversations")
    print("Saved to: {}".format(output_file))
    print("Format: JSONL (one conversation per line)")


if __name__ == "__main__":
    main()
