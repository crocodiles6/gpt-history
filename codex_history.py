"""Read-only discovery and normalization of Codex rollout logs."""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path


META_KEYS = {'id', 'session_id', 'timestamp', 'source', 'thread_source', 'cwd',
             'parent_thread_id', 'forked_from_id', 'subagent_history_start_ordinal',
             'history_base', 'forked_from_ordinal_exclusive', 'agent_path'}
CONTEXT_PREFIXES = ('# AGENTS.md instructions', '<environment_context>', '<permissions instructions>',
                    '<recommended_plugins>', '<app-context>', '<skills_instructions>',
                    '<INSTRUCTIONS>', '<user_instructions>', '<turn_aborted>',
                    '<collaboration_mode>', '<system_reminder>', '<external_codex_',
                    '<external_', '<multi_agent_', '<agent_context>', '<skill>')


@dataclass
class Message:
    role: str
    text: str
    timestamp: str
    id: str = ''
    turn: str = ''
    phase: str = ''
    stream: str = 'response'
    source: str = ''
    line: int = 0
    ordinal: int = 0
    retracted: bool = False
    inherited: bool = False
    variant: bool = False


@dataclass
class Session:
    id: str
    metadata: dict
    messages: list = field(default_factory=list)
    contexts: list = field(default_factory=list)
    omitted: Counter = field(default_factory=Counter)
    notices: list = field(default_factory=list)


@dataclass
class Source:
    path: Path
    metadata: dict
    records: list
    sha256: str
    counts: Counter
    errors: list


def default_roots():
    home = Path.home()
    candidates = [Path(os.environ['CODEX_HOME']).expanduser()] if os.environ.get('CODEX_HOME') else []
    candidates += [home / '.codex', home / '.config/codex',
                   home / 'Library/Application Support/Codex',
                   home / 'Library/Application Support/com.openai.codex',
                   home / 'Library/Application Support/OpenAI']
    candidates += list(home.glob('multica_workspaces*/**/codex-home'))
    return list(dict.fromkeys(p.resolve() for p in candidates if p.is_dir()))


def discover(roots):
    files, errors = set(), []
    skip = {'node_modules', '.git', 'plugins', 'skills', 'browser', 'Cache', 'Caches', 'Code Cache', 'GPUCache'}
    for root in roots:
        if not root.is_dir():
            errors.append({'source': str(root), 'error': 'Directory missing or inaccessible'})
            continue
        for base, dirs, names in os.walk(root, onerror=lambda e: errors.append({'source': str(e.filename), 'error': str(e)})):
            dirs[:] = [d for d in dirs if d not in skip]
            for name in names:
                if name.endswith(('.jsonl', '.jsonl.zst')) and (name.startswith('rollout-') or {'sessions', 'archived_sessions'} & set(Path(base).parts)):
                    files.add((Path(base) / name).resolve())
    return sorted(files), errors


def read_metadata(roots):
    result, errors = {}, []
    for root in roots:
        index = root / 'session_index.jsonl'
        if index.exists():
            try:
                for n, line in enumerate(index.read_text().splitlines(), 1):
                    try:
                        row = json.loads(line)
                        result.setdefault(row['id'], {})['index_title'] = row.get('thread_name', '')
                    except (ValueError, KeyError) as e:
                        errors.append({'source': str(index), 'line': n, 'error': str(e)})
            except OSError as e:
                errors.append({'source': str(index), 'error': str(e)})
        for db in sorted(root.glob('state_*.sqlite')):
            try:
                with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
                    conn.row_factory = sqlite3.Row
                    conn.execute('PRAGMA query_only=ON')
                    cols = {r[1] for r in conn.execute('PRAGMA table_info(threads)')}
                    wanted = ['id', 'name', 'title', 'created_at', 'archived', 'rollout_path', 'source', 'attached_thread_parent_id']
                    if 'id' not in cols:
                        continue
                    for row in conn.execute('SELECT ' + ','.join(k for k in wanted if k in cols) + ' FROM threads'):
                        result.setdefault(row['id'], {}).update(dict(row))
            except (sqlite3.Error, OSError) as e:
                errors.append({'source': str(db), 'error': str(e)})
    return result, errors


def content_text(content):
    if isinstance(content, str):
        return content
    parts = []
    for item in content or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get('type', '')).lower()
        if isinstance(item.get('text'), str):
            parts.append(item['text'])
        elif kind in {'local_image', 'localimage', 'image', 'input_image', 'image_url'}:
            ref = item.get('path') or item.get('image_url') or item.get('url')
            if isinstance(ref, dict):
                ref = ref.get('url')
            if isinstance(ref, str) and not ref.startswith('data:'):
                parts.append('[图片附件：' + ref + ']')
            else:
                parts.append('[内嵌图片，原始数据保留在来源文件中]')
        else:
            parts.append('[非文本内容：' + (kind or 'unknown') + '，参见来源文件]')
    return '\n\n'.join(parts)


def read_source(path):
    """Stream decompression; retain only text/structure, never tool data or reasoning."""
    digest, records, counts, errors = hashlib.sha256(), [], Counter(), []
    metadata = None
    process = None
    if path.suffix == '.zst':
        zstd = shutil.which('zstd')
        if not zstd:
            raise RuntimeError('Compressed history requires zstd (brew install zstd)')
        process = subprocess.Popen([zstd, '-dc', '--', str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stream = process.stdout
    else:
        stream = path.open('rb')
    try:
        for line_no, raw in enumerate(stream, 1):
            digest.update(raw)
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
                if not isinstance(row, dict) or not isinstance(row.get('payload', {}), dict):
                    raise ValueError('JSONL record/payload must be an object')
            except (ValueError, UnicodeError) as e:
                errors.append({'source': str(path), 'line': line_no, 'error': str(e)})
                continue
            typ, payload = row.get('type'), row.get('payload', {})
            subtype = payload.get('type', '')
            counts[str(typ) + ('.' + str(subtype) if subtype else '')] += 1
            keep = False
            if typ == 'session_meta':
                payload = {k: v for k, v in payload.items() if k in META_KEYS}
                if metadata is None:
                    metadata = payload
                keep = True
            elif typ == 'response_item' and subtype == 'message':
                if payload.get('role') in ('user', 'assistant') and payload.get('channel') != 'analysis':
                    payload = dict(payload, content=content_text(payload.get('content')))
                    keep = True
                else:
                    counts['internal_reasoning' if payload.get('channel') == 'analysis' else 'system_developer'] += 1
            elif typ == 'response_item' and subtype == 'reasoning':
                counts['internal_reasoning'] += 1
            elif typ == 'event_msg' and subtype == 'item_completed':
                item = payload.get('item', {})
                counts['ui.' + str(item.get('type'))] += 1
                if str(item.get('type', '')).lower() in ('usermessage', 'agentmessage'):
                    item = dict(item, content=content_text(item.get('content', item.get('text', ''))))
                    payload = dict(payload, item=item)
                    keep = True
            elif typ == 'event_msg' and subtype in ('task_started', 'user_message', 'agent_message', 'thread_rolled_back', 'turn_aborted'):
                keep = True
            elif typ == 'turn_context':
                payload = {k: payload[k] for k in ('turn_id',) if k in payload}
                keep = True
            elif typ == 'compacted':
                payload = {}
                keep = True
            elif typ == 'realtime_item' and subtype == 'transcript_segment':
                keep = True
            if keep:
                records.append({'type': typ, 'payload': payload, 'timestamp': row.get('timestamp', ''),
                                'ordinal': row.get('ordinal', line_no - 1), '_source': str(path), '_line': line_no})
    finally:
        stream.close()
        if process:
            stderr = process.stderr.read().decode(errors='replace')
            process.stderr.close()
            if process.wait() != 0:
                errors.append({'source': str(path), 'error': 'zstd decompression failed: ' + stderr.strip()})
    if not metadata or not (metadata.get('id') or metadata.get('session_id')):
        raise ValueError('No session_meta ID in ' + str(path))
    return Source(path, metadata, records, digest.hexdigest(), counts, errors)


def parse_records(records, path, metadata=None):
    if metadata is None:
        metadata = next((r['payload'] for r in records if r.get('type') == 'session_meta'), {})
    session = Session(metadata.get('id') or metadata.get('session_id') or path.stem, metadata)
    candidates, turn, active_turns = [], '', []
    for i, row in enumerate(records):
        p = row.get('payload', {})
        typ, subtype = row.get('type'), p.get('type')
        ordinal = row.get('ordinal', i)
        if typ == 'turn_context' or (typ == 'event_msg' and subtype == 'task_started'):
            turn = p.get('turn_id', turn)
            if turn and turn not in active_turns:
                active_turns.append(turn)
        if typ == 'compacted':
            session.notices.append('compacted：上下文压缩摘要未作为新对话重放。')
        if typ == 'event_msg' and subtype == 'turn_aborted':
            session.notices.append('turn_aborted：存在中断的轮次 ' + p.get('turn_id', turn))
        if typ == 'event_msg' and subtype == 'thread_rolled_back':
            n = p.get('num_turns', 0)
            if isinstance(n, int) and n > 0:
                removed = set(active_turns[-n:])
                for m in candidates:
                    if m.turn in removed:
                        m.retracted = True
                active_turns = active_turns[:-n]
            session.notices.append('thread_rolled_back：撤回内容保留并单独标记，回滚轮数 ' + str(n))
        data, role, stream = None, '', ''
        if typ == 'response_item' and subtype == 'message':
            role = p.get('role')
            if p.get('channel') == 'analysis':
                session.omitted['internal_reasoning'] += 1
                continue
            if role not in ('user', 'assistant'):
                session.omitted['system_developer'] += 1
                continue
            data, stream = p, 'response'
        elif typ == 'response_item' and subtype == 'reasoning':
            session.omitted['internal_reasoning'] += 1
        elif typ == 'event_msg' and subtype == 'item_completed':
            item = p.get('item', {})
            role = {'usermessage': 'user', 'agentmessage': 'assistant'}.get(str(item.get('type', '')).lower())
            if role:
                data, stream = item, 'ui'
        elif typ == 'event_msg' and subtype in ('user_message', 'agent_message'):
            role = 'user' if subtype == 'user_message' else 'assistant'
            data, stream = {'content': p.get('message', ''), 'id': p.get('id', '')}, 'legacy'
        elif typ == 'realtime_item' and subtype == 'transcript_segment':
            # Transcript schema differs between versions; unknown records stay explicit.
            data = {'content': p.get('text', p.get('transcript', '')), 'id': p.get('id', '')}
            role, stream = p.get('role', p.get('speaker', 'voice')), 'voice'
        if data is None:
            continue
        text = content_text(data.get('content', data.get('text', '')))
        if not text:
            continue
        m = Message(role, text, row.get('timestamp', ''), data.get('id') or '',
                    p.get('turn_id') or p.get('internal_chat_message_metadata_passthrough', {}).get('turn_id') or turn,
                    data.get('phase') or data.get('channel') or '', stream,
                    row.get('_source', str(path)), row.get('_line', i + 1), ordinal,
                    inherited=bool(row.get('_inherited')) or bool(metadata.get('subagent_history_start_ordinal') and ordinal < metadata['subagent_history_start_ordinal']))
        if role == 'user' and stream == 'response' and text.lstrip().startswith(CONTEXT_PREFIXES):
            session.contexts.append(m)
        else:
            candidates.append(m)

    # Match projections one-to-one, scoped to a turn. Never globally deduplicate text.
    priority = {'ui': 3, 'legacy': 2, 'response': 1, 'voice': 3}
    groups, by_id, by_text, revisions = [], {}, defaultdict(list), []
    for pos, m in enumerate(candidates):
        projection_text = re.sub(r'</?image\b[^>]*>|\[(?:图片附件：[^\n]*|内嵌图片，原始数据保留在来源文件中)\]', '', m.text).strip()
        text_key = (m.turn, m.role, projection_text)
        target = by_id.get((m.turn, m.role, m.id)) if m.id else None
        if target is None:
            for idx in by_text[text_key]:
                if m.stream not in groups[idx][2]:
                    target = idx
                    break
        if target is None:
            target = len(groups)
            groups.append([pos, m, {m.stream}])
            by_text[text_key].append(target)
        else:
            if m.stream in groups[target][2] and m.text != groups[target][1].text:
                revisions.append(replace(groups[target][1], variant=True))
                groups[target][1] = m
                session.omitted['message_revision'] += 1
                continue
            groups[target][2].add(m.stream)
            if priority[m.stream] > priority[groups[target][1].stream]:
                groups[target][1] = m
            session.omitted['duplicate_projection'] += 1
        if m.id:
            by_id[(m.turn, m.role, m.id)] = target
    session.messages = [g[1] for g in groups] + revisions
    return session


def resolve_sources(sources, db_metadata):
    """Resolve history_base prefixes and retain non-current branch tails separately."""
    grouped = defaultdict(list)
    for source in sources:
        grouped[source.metadata.get('id') or source.metadata['session_id']].append(source)
    selected, cache, issues = {}, {}, []
    for sid, chunks in grouped.items():
        expected = db_metadata.get(sid, {}).get('rollout_path', '')
        selected[sid] = max(chunks, key=lambda s: (str(s.path).removesuffix('.zst') == expected, s.records[0].get('ordinal', 0), s.path.name))

    def expand(source, trail=()):
        key = str(source.path)
        if key in cache:
            return cache[key]
        if key in trail:
            raise ValueError('Cyclic history_base: ' + key)
        rows = source.records
        base = source.metadata.get('history_base')
        if base:
            sid = base.get('thread_id')
            cutoff = base.get('end_ordinal_exclusive')
            current_id = source.metadata.get('id') or source.metadata.get('session_id')
            options = [s for s in grouped.get(sid, []) if s.path != source.path and s.records[0].get('ordinal', 0) < cutoff]
            if options and isinstance(cutoff, int):
                prior = max(options, key=lambda s: (s.records[0].get('ordinal', 0), s.path.name))
                prefix = [dict(r, _inherited=bool(r.get('_inherited')) or sid != current_id)
                          for r in expand(prior, trail + (key,)) if r.get('ordinal', 0) < cutoff]
                rows = prefix + rows
            else:
                issues.append({'source': key, 'error': 'Missing history_base prefix', 'base': base})
        cache[key] = rows
        return rows

    result = {}
    for sid, main in selected.items():
        rows = expand(main)
        session = parse_records(rows, main.path, main.metadata)
        seen = {(m.turn, m.role, m.id or (m.timestamp, m.text)) for m in session.messages}
        for alt in grouped[sid]:
            if alt is main:
                continue
            for m in parse_records(expand(alt), alt.path, alt.metadata).messages:
                key = (m.turn, m.role, m.id or (m.timestamp, m.text))
                if key not in seen:
                    m.variant = True
                    session.messages.append(m)
                    seen.add(key)
        for source in grouped[sid]:
            session.omitted.update(source.counts)
        result[sid] = (session, grouped[sid], rows)
    return result, issues
