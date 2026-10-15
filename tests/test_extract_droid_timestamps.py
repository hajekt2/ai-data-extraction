import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import extract_droid


class DroidTimestampTests(unittest.TestCase):
    def test_records_first_and_last_message_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / "-home-haja-demo" / "sess-1.jsonl"
            session.parent.mkdir(parents=True)
            events = [
                {"type": "session_start", "id": "sess-1", "title": "Demo", "cwd": "/demo"},
                {
                    "type": "message",
                    "timestamp": "2026-04-07T16:02:20.632Z",
                    "message": {"role": "user", "content": [{"type": "text", "text": "start"}]},
                },
                {
                    "type": "message",
                    "timestamp": "2026-04-07T16:05:00.000Z",
                    "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                },
            ]
            session.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )

            with mock.patch.object(extract_droid, "find_droid_sessions", return_value=root):
                conversations = extract_droid.extract_droid_sessions()

            self.assertEqual(len(conversations), 1)
            self.assertEqual(conversations[0]["created_at"], "2026-04-07T16:02:20.632Z")
            self.assertEqual(conversations[0]["updated_at"], "2026-04-07T16:05:00.000Z")


if __name__ == "__main__":
    unittest.main()
