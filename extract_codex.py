#!/usr/bin/env python3
"""
Extract ALL Codex chat data from all projects
Includes: messages, reasoning, commands, file edits, tool output, token usage
Auto-discovers Codex installations on the device

Codex writes one rollout file per session under `sessions/YYYY/MM/DD/rollout-*.jsonl`
and moves older rollouts into `archived_sessions/`, where recent builds store them
zstd-compressed as `rollout-*.jsonl.zst`. Current rollouts describe the conversation
through `event_msg` / `item_completed` items (UserMessage, AgentMessage,
CommandExecution, FileChange, ...); older builds used `event_msg` payloads named
`user_message` and `agent_message`. Both shapes are parsed. A session that exists in
both the live and the archived location is emitted once, from the richer file.
"""

import io
import json
import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# Item types that carry conversation text rather than tool activity.
TEXT_ITEMS = {'UserMessage': 'user', 'AgentMessage': 'assistant'}

# Item types that represent the agent doing something; mapped onto tool calls.
TOOL_ITEM_KEYS = {
    'CommandExecution': ('exec', ('command', 'cwd', 'parsed_cmd', 'source', 'status', 'exit_code')),
    'FileChange': ('edit', ('changes',)),
    'Extension': ('extension', ('kind', 'action', 'query', 'queries')),
    'SubAgentActivity': ('subagent', ('kind', 'agent_path', 'agent_thread_id')),
    'ImageView': ('image_view', ('path',)),
    'WebSearch': ('web_search', ('query', 'queries')),
}


def find_codex_installations():
    """Find all Codex installation directories"""
    system = platform.system()
    home = Path.home()

    locations = []

    # Search patterns for Codex directories
    codex_patterns = [
        'codex', 'codex-local', '.codex', '.codex-local'
    ]

    if system == "Darwin":  # macOS
        base_dirs = [
            home / "Library/Application Support",
            home / ".config",
            home
        ]
    elif system == "Linux":
        base_dirs = [
            home / ".config",
            home / ".local/share",
            home
        ]
    elif system == "Windows":
        base_dirs = [
            Path(os.environ.get('APPDATA', home / 'AppData/Roaming')),
            Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')),
            home
        ]
    else:
        base_dirs = [home / ".config", home]

    for base_dir in base_dirs:
        if not base_dir.exists():
            continue

        for pattern in codex_patterns:
            codex_dir = base_dir / pattern
            if codex_dir.exists():
                locations.append(codex_dir)

    return list(set(locations))


def _text(content):
    """Join the text of a string or of a list of content parts."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    parts = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get('text'), str):
            parts.append(item['text'])
    return '\n'.join(parts)


def _iso(ts):
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')
    return ts


def _tool_call(call_id, name, arguments):
    return {
        'id': call_id,
        'type': 'function',
        'function': {
            'name': name,
            'arguments': arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
        },
    }


def _tool_output(item):
    """Return the printable output of a tool result payload."""
    output = item.get('output')
    if isinstance(output, list):
        return _text(output)
    if isinstance(output, str):
        return output
    if output is None:
        return ''
    return json.dumps(output, ensure_ascii=False)


def open_session_text(session_file):
    """Open a rollout file as text, transparently decompressing .zst archives."""
    path = Path(session_file)
    if path.name.endswith('.zst'):
        try:
            from compression import zstd as stdlib_zstd  # Python 3.14+
            return io.StringIO(stdlib_zstd.decompress(path.read_bytes()).decode('utf-8', 'replace'))
        except ImportError:
            pass
        try:
            import zstandard
            dctx = zstandard.ZstdDecompressor()
            with path.open('rb') as handle:
                return io.StringIO(dctx.stream_reader(handle).read().decode('utf-8', 'replace'))
        except ImportError:
            pass
        zstd = shutil.which('zstd') or shutil.which('pzstd')
        if not zstd:
            raise RuntimeError('zstd support is unavailable: install zstd, or use Python 3.14+')
        process = subprocess.Popen([zstd, '-dc', str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return io.TextIOWrapper(process.stdout, encoding='utf-8', errors='replace')
    return path.open('r', encoding='utf-8', errors='replace')


def _iter_events(session_file):
    with open_session_text(session_file) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def _from_items(events):
    """Parse the current `item_completed` shape into normalized messages."""
    messages = []
    pending_reasoning = []
    saw_items = False

    for obj in events:
        event_type = obj.get('type')
        payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
        ts = _iso(obj.get('timestamp'))

        if event_type == 'event_msg' and payload.get('type') == 'item_completed':
            saw_items = True
            item = payload.get('item') if isinstance(payload.get('item'), dict) else {}
            item_type = item.get('type')
            item_ts = _iso(payload.get('completed_at_ms')) or ts

            if item_type in TEXT_ITEMS:
                content = _text(item.get('content'))
                if not content:
                    continue
                message = {'role': TEXT_ITEMS[item_type], 'content': content, 'timestamp': item_ts}
                if item_type == 'AgentMessage':
                    if pending_reasoning:
                        message['reasoning'] = '\n'.join(pending_reasoning)
                        pending_reasoning = []
                    if item.get('phase'):
                        message['phase'] = item['phase']
                messages.append(message)

            elif item_type == 'Reasoning':
                for text in item.get('summary_text') or []:
                    if isinstance(text, str) and text:
                        pending_reasoning.append(text)
                for text in item.get('raw_content') or []:
                    if isinstance(text, str) and text:
                        pending_reasoning.append(text)

            elif item_type in TOOL_ITEM_KEYS:
                name, fields = TOOL_ITEM_KEYS[item_type]
                arguments = {field: item[field] for field in fields if field in item}
                call_id = item.get('id')
                messages.append({
                    'role': 'assistant', 'content': '', 'timestamp': item_ts,
                    'tool_calls': [_tool_call(call_id, name, arguments)],
                })
                output = item.get('stdout')
                if output is None:
                    output = item.get('result')
                if output is not None:
                    tool_message = {
                        'role': 'tool', 'tool_call_id': call_id, 'name': name,
                        'content': output if isinstance(output, str) else json.dumps(output, ensure_ascii=False),
                        'timestamp': item_ts,
                    }
                    if item.get('exit_code') is not None:
                        tool_message['exit_code'] = item['exit_code']
                    if item.get('stderr'):
                        tool_message['stderr'] = item['stderr']
                    if item.get('status'):
                        tool_message['status'] = item['status']
                    messages.append(tool_message)

            else:
                # Unknown item: keep it as a tool call so nothing is silently dropped.
                call_id = item.get('id')
                arguments = {k: v for k, v in item.items() if k not in ('type', 'id')}
                if arguments:
                    messages.append({
                        'role': 'assistant', 'content': '', 'timestamp': item_ts,
                        'tool_calls': [_tool_call(call_id, (item_type or 'unknown').lower(), arguments)],
                    })

        elif event_type == 'event_msg' and payload.get('type') == 'task_complete':
            if pending_reasoning:
                messages.append({'role': 'assistant', 'content': '', 'reasoning': '\n'.join(pending_reasoning),
                                 'timestamp': ts})
                pending_reasoning = []

    if pending_reasoning:
        messages.append({'role': 'assistant', 'content': '', 'reasoning': '\n'.join(pending_reasoning)})

    return messages, saw_items


def _from_legacy_events(events):
    """Parse older rollouts: `user_message`, `agent_message`, and response_item messages."""
    messages = []
    tool_results = []

    for obj in events:
        event_type = obj.get('type')
        payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
        ts = _iso(obj.get('timestamp'))
        payload_type = payload.get('type')

        if event_type == 'event_msg':
            if payload_type == 'user_message':
                content = _text(payload.get('message')) or (payload.get('message') or '')
                if str(content).strip():
                    messages.append({'role': 'user', 'content': str(content).strip(), 'timestamp': ts})
            elif payload_type == 'agent_message':
                content = _text(payload.get('message')) or (payload.get('message') or '')
                if str(content).strip():
                    message = {'role': 'assistant', 'content': str(content).strip(), 'timestamp': ts}
                    if payload.get('model'):
                        message['model'] = payload['model']
                    messages.append(message)
            elif payload_type in ('tool_use', 'tool_result', 'diff'):
                tool_results.append({'type': payload_type, 'tool': payload.get('tool'),
                                     'input': payload.get('input'), 'output': payload.get('output'),
                                     'file': payload.get('file'), 'diff': payload.get('diff'),
                                     'timestamp': ts})

        elif event_type == 'response_item':
            if payload_type == 'message':
                role = payload.get('role')
                content = _text(payload.get('content'))
                if role in ('user', 'assistant') and content.strip():
                    messages.append({'role': role, 'content': content, 'timestamp': ts})
            elif payload_type == 'custom_tool_call':
                messages.append({
                    'role': 'assistant', 'content': '', 'timestamp': ts,
                    'tool_calls': [_tool_call(payload.get('call_id'), payload.get('name'), payload.get('input'))],
                })
            elif payload_type == 'custom_tool_call_output':
                messages.append({'role': 'tool', 'tool_call_id': payload.get('call_id'),
                                 'content': _tool_output(payload), 'timestamp': ts})
            elif payload_type == 'reasoning':
                summary = payload.get('summary')
                if isinstance(summary, list) and summary:
                    messages.append({'role': 'assistant', 'content': '', 'reasoning': _text(summary),
                                     'timestamp': ts})

    return messages, tool_results


def extract_codex_session(session_file):
    """Extract one Codex rollout with full context."""
    events = list(_iter_events(session_file))

    session_meta = {}
    usage = {}
    for obj in events:
        event_type = obj.get('type')
        payload = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
        if event_type == 'session_meta' and payload:
            session_meta = payload
        elif event_type == 'event_msg' and payload.get('type') == 'token_count':
            info = payload.get('info') if isinstance(payload.get('info'), dict) else {}
            if info.get('total_token_usage'):
                usage = info['total_token_usage']

    messages, saw_items = _from_items(events)
    tool_results = []
    if not saw_items:
        messages, tool_results = _from_legacy_events(events)

    if not messages:
        return None

    conv = {
        'messages': messages,
        'session_id': session_meta.get('id') or session_meta.get('session_id') or Path(session_file).stem,
        'cwd': session_meta.get('cwd'),
        'source': 'codex',
        'session_file': str(session_file),
        'timestamp': _iso(session_meta.get('timestamp')),
    }
    for key, value in (
        ('cli_version', session_meta.get('cli_version')),
        ('originator', session_meta.get('originator')),
        ('model_provider', session_meta.get('model_provider')),
        ('git_branch', (session_meta.get('git') or {}).get('branch') if isinstance(session_meta.get('git'), dict) else None),
        ('token_usage', usage or None),
    ):
        if value:
            conv[key] = value

    if tool_results:
        conv['tool_results'] = tool_results

    return conv


def find_all_codex_sessions(installation):
    """Find all Codex rollout files in an installation, live and archived."""
    session_files = []

    for subdirectory in ('sessions', 'archived_sessions'):
        candidate = installation / subdirectory
        if candidate.exists():
            session_files.extend(sorted(candidate.rglob('rollout-*.jsonl')))
            session_files.extend(sorted(candidate.rglob('rollout-*.jsonl.zst')))

    # Also check for project-based structure
    projects_dir = installation / 'projects'
    if projects_dir.exists():
        session_files.extend(list(projects_dir.rglob('*.jsonl')))

    return session_files


def dedupe_sessions(session_files):
    """Keep one rollout per session id, preferring the larger file."""
    by_id = {}
    for path in session_files:
        key = Path(path).name.replace('.zst', '')
        for prefix in ('rollout-',):
            if key.startswith(prefix):
                key = key[len(prefix):]
        try:
            size = Path(path).stat().st_size
        except OSError:
            size = 0
        existing = by_id.get(key)
        if existing is None or size > existing[0]:
            by_id[key] = (size, Path(path))
    return [path for _, path in sorted(by_id.values(), key=lambda entry: str(entry[1]))]


def main():
    print("=" * 80)
    print("CODEX COMPLETE DATA EXTRACTION")
    print("=" * 80)
    print()

    print("🔍 Searching for Codex installations...")
    installations = find_codex_installations()

    if not installations:
        print("❌ No Codex installations found!")
        return

    print(f"✅ Found {len(installations)} installation(s):")
    for inst in installations:
        print(f"   - {inst}")
    print()

    all_conversations = []
    installation_stats = {}

    for installation in installations:
        print(f"📂 Processing: {installation}")

        session_files = dedupe_sessions(find_all_codex_sessions(installation))
        print(f"   Found {len(session_files)} session files")

        conversations = []
        for session_file in session_files:
            try:
                conv = extract_codex_session(session_file)
            except RuntimeError as error:
                print(f"   ⚠️  Skipping {Path(session_file).name}: {error}")
                continue
            if conv:
                conv['installation'] = str(installation)
                conversations.append(conv)

        if conversations:
            all_conversations.extend(conversations)
            installation_stats[str(installation)] = len(conversations)
            print(f"   ✅ {len(conversations)} conversations")
        else:
            print("   ⚠️  No conversations found")

    print()
    print("=" * 80)
    print("EXTRACTION COMPLETE")
    print("=" * 80)
    print(f"Total conversations: {len(all_conversations):,}")

    if not all_conversations:
        print("No conversations found!")
        return

    # Statistics
    total_messages = sum(len(c['messages']) for c in all_conversations)
    with_tools = sum(1 for c in all_conversations
                     if 'tool_results' in c
                     or any(m.get('tool_calls') or m.get('role') == 'tool' for m in c['messages']))
    complete = sum(1 for c in all_conversations
                   if any(m['role'] == 'assistant' for m in c['messages']))

    print(f"Complete conversations: {complete:,}")
    print(f"Total messages: {total_messages:,}")
    print(f"With tool use/diffs: {with_tools:,}")
    print()

    print("Breakdown by installation:")
    for inst, count in sorted(installation_stats.items(), key=lambda x: -x[1]):
        print(f"  {Path(inst).name:20} {count:5,} conversations")
    print()

    # Save to organized JSONL
    output_dir = Path('extracted_data')
    output_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    output_file = output_dir / f'codex_conversations_{timestamp}.jsonl'

    with open(output_file, 'w', encoding='utf-8') as f:
        for conv in all_conversations:
            f.write(json.dumps(conv, ensure_ascii=False) + '\n')

    file_size = output_file.stat().st_size / 1024 / 1024
    print(f"✅ Saved to: {output_file}")
    print(f"   Size: {file_size:.2f} MB")
    print(f"   Format: JSONL (one conversation per line)")


if __name__ == '__main__':
    main()
