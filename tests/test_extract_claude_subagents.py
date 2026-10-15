import json
import tempfile
import unittest
from pathlib import Path

from extract_claude_code import extract_claude_project_conversations


def entry(type_, text, uuid, parent=None):
    return {
        "type": type_,
        "uuid": uuid,
        "parentUuid": parent,
        "timestamp": "2026-10-15T00:00:00.000Z",
        "message": {"role": type_, "content": text},
    }


class ClaudeSubagentTests(unittest.TestCase):
    def build_installation(self, root):
        project = root / "projects" / "-home-haja-work-demo"
        project.mkdir(parents=True)
        session = project / "sess-1.jsonl"
        session.write_text(
            "".join(json.dumps(e) + "\n" for e in (
                entry("user", "main prompt", "u1"),
                entry("assistant", "main answer", "a1", "u1"),
            )),
            encoding="utf-8",
        )

        subagents = project / "sess-1" / "subagents"
        subagents.mkdir(parents=True)
        (subagents / "agent-x1.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in (
                entry("user", "explore the repo", "u2"),
                entry("assistant", "here is what I found", "a2", "u2"),
            )),
            encoding="utf-8",
        )
        return project

    def test_extracts_subagent_transcripts_as_separate_conversations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build_installation(root)

            conversations = extract_claude_project_conversations(root)
            by_id = {c["session_id"]: c for c in conversations}

            self.assertEqual({"sess-1", "agent-x1"}, set(by_id))

            main = by_id["sess-1"]
            self.assertNotIn("subagent", main)
            self.assertEqual(main["project_name"], "-home-haja-work-demo")

            subagent = by_id["agent-x1"]
            self.assertTrue(subagent["subagent"])
            self.assertEqual(subagent["agent_id"], "x1")
            self.assertEqual(subagent["parent_session_id"], "sess-1")
            self.assertEqual(subagent["project_name"], "-home-haja-work-demo")
            self.assertEqual([m["content"] for m in subagent["messages"]],
                             ["explore the repo", "here is what I found"])

    def test_records_first_and_last_entry_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.build_installation(root)
            session = root / "projects" / "-home-haja-work-demo" / "sess-1.jsonl"
            lines = session.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            last = json.loads(lines[-1])
            first["timestamp"] = "2026-10-14T08:00:00.000Z"
            last["timestamp"] = "2026-10-14T09:30:00.000Z"
            session.write_text(
                "".join(json.dumps(o) + "\n" for o in (first, last)),
                encoding="utf-8",
            )

            by_id = {c["session_id"]: c for c in extract_claude_project_conversations(root)}

            self.assertEqual(by_id["sess-1"]["created_at"], "2026-10-14T08:00:00.000Z")
            self.assertEqual(by_id["sess-1"]["updated_at"], "2026-10-14T09:30:00.000Z")


if __name__ == "__main__":
    unittest.main()
