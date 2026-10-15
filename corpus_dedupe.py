#!/usr/bin/env python3
"""
Merge extraction runs into one cumulative corpus without duplicate sessions.

Every extraction run writes its own timestamped files, so the same session appears
once per run. This script keeps one record per (source, session_id) - the largest
one, because a later run of the same session usually holds more messages - and
writes a single deduplicated JSONL corpus.

Usage:
    python3 corpus_dedupe.py extracted_data older_run_dir -o ALL_CONVERSATIONS_DEDUPED.jsonl
"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def collect_inputs(paths):
    """Expand files and directories into corpus JSONL files."""
    files = []
    for raw_path in paths:
        path = Path(raw_path)
        if path.is_dir():
            files.extend(sorted(p for p in path.rglob('*.jsonl') if p.is_file()))
        elif path.is_file():
            files.append(path)
        else:
            raise SystemExit(f'not a file or directory: {path}')
    return files


def _record_key(record, line):
    """Key a record by source plus the session it came from.

    Codex writes one rollout per agent thread and every subagent rollout inherits the
    parent's `session_id`, so the session id alone would collapse distinct subagent
    transcripts into one record. The rollout file name separates them, and it is stable
    across extraction runs, so it is part of the key whenever the extractor recorded it.
    """
    source = record.get('source') or 'unknown'
    session_id = record.get('session_id')
    session_file = record.get('session_file') or record.get('source_file')
    if session_file:
        name = str(session_file).rsplit('/', 1)[-1]
        if name.endswith('.zst'):
            name = name[:-len('.zst')]
        return (source, str(session_id), name)
    if session_id is None:
        session_id = 'sha256:' + hashlib.sha256(line.encode('utf-8')).hexdigest()
    return (source, str(session_id))


def index_records(files):
    """Return the best record per session key as (path, byte offset, size)."""
    best = {}
    total = 0
    for path in files:
        offset = 0
        with path.open('rb') as handle:
            for raw in handle:
                size = len(raw)
                offset += size
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                total += 1
                key = _record_key(record, raw.decode('utf-8', 'replace'))
                current = best.get(key)
                if current is None or size > current[2]:
                    best[key] = (path, offset - size, size)
    return best, total


def write_corpus(files, best, output_file):
    """Stream the winning records into the output corpus."""
    wanted = {}
    for path, offset, _size in best.values():
        wanted.setdefault(path, set()).add(offset)

    output_file.parent.mkdir(parents=True, exist_ok=True)
    written = Counter()
    with output_file.open('w', encoding='utf-8') as destination:
        for path in files:
            offsets = wanted.get(path)
            if not offsets:
                continue
            with path.open('rb') as handle:
                offset = 0
                for raw in handle:
                    if offset in offsets:
                        destination.write(raw.decode('utf-8', 'replace'))
                        record_source = 'unknown'
                        try:
                            record_source = json.loads(raw).get('source') or 'unknown'
                        except json.JSONDecodeError:
                            pass
                        written[record_source] += 1
                    offset += len(raw)
    return written


def main():
    parser = argparse.ArgumentParser(description='Deduplicate extraction corpora into one file.')
    parser.add_argument('inputs', nargs='+', help='corpus files or directories to merge')
    parser.add_argument('-o', '--output', required=True, help='output JSONL path')
    args = parser.parse_args()

    files = collect_inputs(args.inputs)
    if not files:
        raise SystemExit('No input JSONL files found.')

    print(f'Merging {len(files)} file(s):')
    for path in files:
        print(f'   {path} ({path.stat().st_size / 1024 / 1024:.1f} MB)')

    best, total = index_records(files)
    output_file = Path(args.output)
    written = write_corpus(files, best, output_file)

    print()
    print('DEDUPLICATED CORPUS')
    print(f'Records read: {total:,}')
    print(f'Records kept: {sum(written.values()):,}')
    print(f'Duplicates dropped: {total - sum(written.values()):,}')
    print()
    print('By source:')
    for source, count in sorted(written.items(), key=lambda item: -item[1]):
        print(f'   {source:20} {count:6,}')
    print()
    print(f'Saved to: {output_file} ({output_file.stat().st_size / 1024 / 1024:.1f} MB)')


if __name__ == '__main__':
    main()
