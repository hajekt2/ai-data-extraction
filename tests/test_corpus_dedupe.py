import json
import tempfile
import unittest
from pathlib import Path

from corpus_dedupe import collect_inputs, index_records, write_corpus


def conversation(source, session_id, text):
    return {"source": source, "session_id": session_id, "messages": [{"role": "user", "content": text}]}


def write_jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


class CorpusDedupeTests(unittest.TestCase):
    def test_keeps_the_largest_record_per_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = write_jsonl(root / "run1.jsonl", [
                conversation("codex", "s1", "short"),
                conversation("pi", "p1", "only once"),
            ])
            second = write_jsonl(root / "run2.jsonl", [
                conversation("codex", "s1", "short but longer record with more content"),
            ])

            files = collect_inputs([root])
            best, total = index_records(files)
            output = root / "merged.jsonl"
            written = write_corpus(files, best, output)

            self.assertEqual(total, 3)
            self.assertEqual(dict(written), {"codex": 1, "pi": 1})

            lines = output.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            kept = [json.loads(line) for line in lines]
            codex = [record for record in kept if record["source"] == "codex"][0]
            self.assertIn("more content", codex["messages"][0]["content"])

    def test_records_without_session_ids_stay_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = write_jsonl(root / "run1.jsonl", [{"source": "cursor", "messages": ["a"]}])
            second = write_jsonl(root / "run2.jsonl", [{"source": "cursor", "messages": ["b"]}])

            files = collect_inputs([first, second])
            best, total = index_records(files)

            self.assertEqual(total, 2)
            self.assertEqual(len(best), 2)

    def test_codex_subagent_rollouts_share_a_session_id_but_stay_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run1 = write_jsonl(root / "run1.jsonl", [
                {"source": "codex", "session_id": "parent-1", "session_file": "/x/rollout-a.jsonl",
                 "messages": ["parent thread"]},
                {"source": "codex", "session_id": "parent-1", "session_file": "/x/rollout-b.jsonl",
                 "messages": ["subagent one"]},
            ])
            run2 = write_jsonl(root / "run2.jsonl", [
                {"source": "codex", "session_id": "parent-1", "session_file": "/x/rollout-a.jsonl.zst",
                 "messages": ["parent thread", "with more messages"]},
            ])

            files = collect_inputs([run1, run2])
            best, total = index_records(files)

            self.assertEqual(total, 3)
            self.assertEqual(len(best), 2)

            output = root / "merged.jsonl"
            written = write_corpus(files, best, output)
            self.assertEqual(sum(written.values()), 2)
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            parent = [r for r in records if r["session_file"].endswith("rollout-a.jsonl.zst")][0]
            self.assertEqual(len(parent["messages"]), 2)


if __name__ == "__main__":
    unittest.main()
