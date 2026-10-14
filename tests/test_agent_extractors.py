import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from extract_common import parse_pi_style_session
from extract_hermes import extract_state_database


class PiStyleExtractorTests(unittest.TestCase):
    def test_normalizes_messages_tool_calls_and_tool_results(self):
        with tempfile.TemporaryDirectory() as directory:
            session_file = Path(directory) / "session.jsonl"
            events = [
                {"type": "session", "cwd": "/workspace", "version": 1},
                {
                    "type": "message",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Inspect this"}],
                        "timestamp": "2026-08-21T10:00:00Z",
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "I will inspect it."},
                            {
                                "type": "toolCall",
                                "id": "call-1",
                                "name": "read",
                                "arguments": {"path": "README.md"},
                            },
                        ],
                        "model": "test-model",
                        "timestamp": "2026-08-21T10:00:01Z",
                    },
                },
                {
                    "type": "message",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "call-1",
                        "toolName": "read",
                        "content": [{"type": "text", "text": "contents"}],
                        "timestamp": "2026-08-21T10:00:02Z",
                    },
                },
            ]
            session_file.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )

            conversation = parse_pi_style_session(session_file, "test", {})

            self.assertEqual(conversation["project_path"], "/workspace")
            self.assertEqual(len(conversation["messages"]), 2)
            assistant = conversation["messages"][1]
            self.assertEqual(assistant["tool_uses"][0]["name"], "read")
            self.assertEqual(assistant["tool_results"][0]["content"], "contents")


class HermesExtractorTests(unittest.TestCase):
    def test_reads_state_database_without_system_prompt_table(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, source TEXT, model TEXT, title TEXT,
                    display_name TEXT, cwd TEXT, session_key TEXT, chat_id TEXT,
                    chat_type TEXT, thread_id TEXT, git_branch TEXT,
                    git_repo_root TEXT, started_at TEXT, ended_at TEXT,
                    end_reason TEXT, message_count INTEGER, tool_call_count INTEGER
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, tool_call_id TEXT, tool_calls TEXT,
                    tool_name TEXT, timestamp TEXT, finish_reason TEXT,
                    reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
                    codex_reasoning_items TEXT, codex_message_items TEXT
                );
                INSERT INTO sessions VALUES
                    ('hermes-1', 'local', 'test-model', 'Test', NULL,
                     '/workspace', NULL, NULL, NULL, NULL, NULL, NULL,
                     '2026-08-21T10:00:00Z', NULL, NULL, 3, 1);
                INSERT INTO messages VALUES
                    (1, 'hermes-1', 'user', 'Run the tool', NULL, NULL, NULL,
                     '2026-08-21T10:00:00Z', NULL, NULL, NULL, NULL, NULL, NULL),
                    (2, 'hermes-1', 'assistant', '', NULL,
                     '[{"id":"call-1","name":"bash","arguments":{"command":"pwd"}}]',
                     NULL, '2026-08-21T10:00:01Z', 'tool_use', 'thinking',
                     NULL, NULL, NULL, NULL),
                    (3, 'hermes-1', 'tool', '{"output":"/workspace"}',
                     'call-1', NULL, 'bash', '2026-08-21T10:00:02Z', NULL,
                     NULL, NULL, NULL, NULL, NULL);
                """
            )
            connection.commit()
            connection.close()

            conversations = extract_state_database(database)

            self.assertEqual(len(conversations), 1)
            self.assertEqual(conversations[0]["project_path"], "/workspace")
            assistant = conversations[0]["messages"][1]
            self.assertEqual(assistant["tool_uses"][0]["name"], "bash")
            self.assertEqual(assistant["tool_results"][0]["content"]["output"], "/workspace")

    def test_reads_legacy_jsonl_session_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            session_file = Path(directory) / "20260821_legacy.jsonl"
            session_file.write_text(
                "".join(
                    json.dumps(item) + "\n"
                    for item in (
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi"},
                    )
                ),
                encoding="utf-8",
            )
            rows = []
            for line in session_file.read_text(encoding="utf-8").splitlines():
                rows.append(json.loads(line))

            from extract_hermes import session_conversation

            conversation = session_conversation(
                {"id": session_file.stem}, rows, session_file
            )

            self.assertEqual(conversation["session_id"], "20260821_legacy")
            self.assertEqual(len(conversation["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
