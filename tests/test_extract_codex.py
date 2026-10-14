import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from extract_codex import dedupe_sessions, extract_codex_session, open_session_text


def event(type_, payload, timestamp="2026-10-14T10:00:00.000Z"):
    return {"timestamp": timestamp, "type": type_, "payload": payload}


CURRENT_ROLLOUT = [
    {"timestamp": "2026-10-14T09:59:00.000Z", "type": "session_meta",
     "payload": {"id": "sess-current", "cwd": "/work/project", "cli_version": "0.155.1",
                 "originator": "Codex Desktop", "model_provider": "openai"}},
    event("event_msg", {"type": "item_completed", "completed_at_ms": 1792000000000,
                        "item": {"type": "UserMessage", "id": "u1",
                                 "content": [{"type": "text", "text": "Fix the writer"}]}}),
    event("event_msg", {"type": "item_completed",
                        "item": {"type": "Reasoning", "id": "r1",
                                 "summary_text": ["Inspect the writer first."]}}),
    event("event_msg", {"type": "item_completed",
                        "item": {"type": "AgentMessage", "id": "a1", "phase": "commentary",
                                 "content": [{"type": "text", "text": "I will inspect it."}]}}),
    event("event_msg", {"type": "item_completed",
                        "item": {"type": "CommandExecution", "id": "exec-1",
                                 "command": ["/bin/zsh", "-lc", "pwd"], "cwd": "file:///work/project",
                                 "status": "completed", "exit_code": 0, "stdout": "/work/project"}}),
    event("event_msg", {"type": "item_completed",
                        "item": {"type": "FileChange", "id": "fc-1",
                                 "changes": {"/work/project/writer.py": {"type": "update",
                                                                         "content": "def write(): ..."}}}}),
    event("event_msg", {"type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 5,
                                                       "total_tokens": 15}}}),
]

LEGACY_ROLLOUT = [
    {"timestamp": "2026-02-16T22:53:42.000Z", "type": "session_meta",
     "payload": {"id": "sess-legacy", "cwd": "/work/legacy", "cli_version": "0.100.0"}},
    event("event_msg", {"type": "user_message", "message": "Old shape question"}),
    event("event_msg", {"type": "agent_message", "message": "Old shape answer", "model": "gpt-5"}),
    event("response_item", {"type": "custom_tool_call", "call_id": "call-9", "name": "exec",
                            "input": "tools.exec_command({cmd:'ls'})"}),
    event("response_item", {"type": "custom_tool_call_output", "call_id": "call-9",
                            "output": [{"type": "input_text", "text": "README.md"}]}),
]


def write_rollout(directory, name, events):
    path = Path(directory) / name
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return path


class CurrentCodexRolloutTests(unittest.TestCase):
    def test_parses_item_completed_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            conversation = extract_codex_session(write_rollout(directory, "rollout-a.jsonl", CURRENT_ROLLOUT))

            self.assertEqual(conversation["session_id"], "sess-current")
            self.assertEqual(conversation["cwd"], "/work/project")
            self.assertEqual(conversation["cli_version"], "0.155.1")
            self.assertEqual(conversation["token_usage"]["total_tokens"], 15)

            roles = [m["role"] for m in conversation["messages"]]
            self.assertEqual(roles, ["user", "assistant", "assistant", "tool", "assistant"])

            user, assistant, command, command_result, edit = conversation["messages"]
            self.assertEqual(user["content"], "Fix the writer")
            self.assertEqual(assistant["reasoning"], "Inspect the writer first.")
            self.assertEqual(assistant["content"], "I will inspect it.")
            self.assertEqual(assistant["phase"], "commentary")

            call = command["tool_calls"][0]["function"]
            self.assertEqual(call["name"], "exec")
            self.assertIn("pwd", call["arguments"])
            self.assertEqual(command_result["content"], "/work/project")
            self.assertEqual(command_result["tool_call_id"], "exec-1")
            self.assertEqual(edit["tool_calls"][0]["function"]["name"], "edit")
            self.assertIn("writer.py", edit["tool_calls"][0]["function"]["arguments"])


class LegacyCodexRolloutTests(unittest.TestCase):
    def test_parses_user_and_agent_message_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            conversation = extract_codex_session(write_rollout(directory, "rollout-b.jsonl", LEGACY_ROLLOUT))

            self.assertEqual(conversation["session_id"], "sess-legacy")
            user, assistant, tool_call, tool_result = conversation["messages"]
            self.assertEqual((user["role"], user["content"]), ("user", "Old shape question"))
            self.assertEqual((assistant["role"], assistant["content"]), ("assistant", "Old shape answer"))
            self.assertEqual(tool_call["tool_calls"][0]["function"]["name"], "exec")
            self.assertEqual(tool_result["content"], "README.md")


class ArchivedRolloutTests(unittest.TestCase):
    def test_reads_zstd_archives(self):
        zstd = shutil.which("zstd")
        if not zstd:
            self.skipTest("zstd CLI is not installed")
        with tempfile.TemporaryDirectory() as directory:
            plain = write_rollout(directory, "rollout-c.jsonl", CURRENT_ROLLOUT)
            archive = Path(directory) / "rollout-c.jsonl.zst"
            subprocess.run([zstd, "-q", "-o", str(archive), str(plain)], check=True)

            with open_session_text(archive) as handle:
                self.assertIn("UserMessage", handle.read())

            conversation = extract_codex_session(archive)
            self.assertEqual(conversation["session_id"], "sess-current")

    def test_keeps_one_copy_per_session(self):
        with tempfile.TemporaryDirectory() as directory:
            live = write_rollout(directory, "rollout-2026-10-14T10-00-00-abc.jsonl", CURRENT_ROLLOUT)
            archived = write_rollout(directory, "rollout-2026-10-14T10-00-00-abc.jsonl.zst",
                                     CURRENT_ROLLOUT + [event("event_msg", {"type": "task_complete"})])

            self.assertEqual(dedupe_sessions([live, archived]), [archived])


if __name__ == "__main__":
    unittest.main()
