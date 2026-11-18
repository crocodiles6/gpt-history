#!/usr/bin/env python3
"""Export local Codex conversations to Markdown without modifying Codex data."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from codex_history import Session, Source, default_roots, discover, read_metadata, read_source, resolve_sources

VERSION = 1
DEFAULT_OUTPUT = Path.home() / 'Documents/AI-Chat-Backup/Codex'


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n'


def atomic_write(path, text):
    if path.is_symlink():
        raise ValueError('Refusing to overwrite symlink: ' + str(path))
    data = text.encode('utf-8')
    if path.exists() and path.read_bytes() == data:
        return False
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.export-', delete=False) as f:
        temp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return True


def local_time(value, tz):
    if not value:
        return ''
    try:
        dt = datetime.fromtimestamp(value, timezone.utc) if isinstance(value, (int, float)) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            return str(value)  # Do not invent a timezone for legacy naive timestamps.
        return dt.astimezone(ZoneInfo(tz)).isoformat(timespec='seconds')
    except (ValueError, OverflowError):
        return str(value)


def filename(title, created, sid):
    safe = re.sub(r'[\x00-\x1f/\\:*?"<>|\[\]#^]', '_', title)
    safe = re.sub(r'\s+', ' ', safe).strip(' .') or sid
    # macOS limits a component by UTF-8 bytes, not character count.
    safe = safe.encode('utf-8')[:140].decode('utf-8', errors='ignore').rstrip()
    suffix = re.sub(r'[^A-Za-z0-9_-]', '_', sid)[:64]
    return '{} {} [{}].md'.format(created[:10] or 'undated', safe, suffix)


def render(session, metadata, sources, title, created, tz, links):
    archived = bool(metadata.get('archived', all('archived_sessions' in s.path.parts for s in sources)))
    parent = session.metadata.get('parent_thread_id')
    fork = session.metadata.get('forked_from_id')
    kind = 'subagent' if parent or 'subagent' in str(session.metadata.get('source', '')) else 'conversation'
    front = {'title': title, 'created': created, 'session_id': session.id,
             'source': str(sources[0].path), 'sources': [str(s.path) for s in sources],
             'archived': archived, 'kind': kind, 'timezone': tz,
             'message_count': len(session.messages), 'tags': ['codex', 'chat-backup']}
    if session.metadata.get('source_missing'):
        front['source_missing'] = True
    if session.metadata.get('incomplete'):
        front['incomplete'] = True
    if parent:
        front['parent_session_id'] = parent
    if fork:
        front['forked_from_id'] = fork
    parts = ['---']
    for key, value in front.items():
        # JSON scalars/arrays are valid YAML; quote every string to avoid YAML ambiguity.
        parts.append(key + ': ' + json.dumps(value, ensure_ascii=False))
    parts += ['---', '', '# ' + title.replace('\n', ' '), '', '[返回索引](index.md)', '']
    if kind == 'subagent':
        parts += ['> 此文件是子代理运行记录；其中的 User 表示代理接收的输入，不一定由用户直接发送。', '']
    if session.metadata.get('source_missing'):
        parts += ['> 原始历史文件缺失。此笔记仅保存本机索引中的标题、时间和来源路径，没有可恢复的对话正文。', '']
    if session.metadata.get('incomplete'):
        parts += ['> 来源读取不完整。以下为成功恢复的内容，缺口详见 export-report.json。', '']
    for label, sid in [('父会话', parent), ('分叉来源', fork)]:
        if sid:
            ref = '[{}]({})'.format(sid, quote(links[sid])) if sid in links else '`' + sid + '`'
            parts += ['- ' + label + '：' + ref]
    parts += ['', '> 正文按来源日志顺序排列。工具调用、系统提示和内部推理仅在文末计数；图片保留引用，不复制附件。', '']
    current_group = None
    for i, m in enumerate(session.messages, 1):
        group = '历史版本' if m.variant else ('继承历史' if m.inherited else '当前会话')
        if group != current_group and (group != '当前会话' or current_group is not None):
            parts += ['---', '', '## ' + group, '']
            if m.variant:
                parts += ['> 以下内容来自其他日志版本，未并入当前分支；保留用于追溯。', '']
        current_group = group
        role = {'user': 'User', 'assistant': 'Assistant'}.get(m.role, '语音转写（' + m.role + '）')
        labels = []
        if m.phase in ('commentary', 'final_answer', 'final'):
            labels.append('过程更新' if m.phase == 'commentary' else '最终回复')
        if m.stream == 'voice':
            labels.append('语音转写')
        if m.retracted:
            labels.append('已撤回')
        heading = '## {} · {}'.format(role, local_time(m.timestamp, tz) or '时间未记录')
        if labels:
            heading += ' · ' + ' / '.join(labels)
        parts += ['---', '', heading, '', m.text, '',
                  '<!-- source: {} ; line: {} ; ordinal: {} ; message: {} -->'.format(
                      m.source.replace('--', '—'), m.line, m.ordinal, m.id.replace('--', '—')), '']
    if not session.messages:
        parts += ['> 来源中没有可提取的用户或 Assistant 文本消息。', '']
    if session.contexts:
        parts += ['---', '', '## 注入的上下文', '', '> 下列记录虽使用 user 角色存储，但属于环境或指令上下文，未混入普通对话。', '']
        for m in session.contexts:
            parts += ['<details>', '<summary>上下文 · ' + local_time(m.timestamp, tz) + ' · 来源行 ' + str(m.line) + '</summary>', '', m.text, '', '</details>', '']
    parts += ['---', '', '## 导出记录', '']
    for note in dict.fromkeys(session.notices):
        parts.append('- ' + note)
    parts += ['- 内部推理正文、推理摘要和密文均未导出；工具输出、系统提示与运行事件保留在原始文件中。',
              '- 同一消息的界面事件与响应投影合并；不同轮次的相同文本保留。', '', '| 记录类别 | 数量 |', '| --- | ---: |']
    for category, count in sorted(session.omitted.items()):
        parts.append('| ' + category + ' | ' + str(count) + ' |')
    parts += ['', '### 来源文件', '']
    parts += ['- `' + str(s.path) + '`' for s in sources]
    return '\n'.join(parts) + '\n', front


def export(roots, output, timezone_name='Asia/Shanghai', sample=None):
    roots = list(dict.fromkeys(Path(p).expanduser().resolve() for p in roots))
    output = Path(output).expanduser().resolve()
    for root in roots:
        if output == root or root in output.parents or output in root.parents:
            raise ValueError('Output must be separate from source directories: ' + str(root))
    ZoneInfo(timezone_name)
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.export.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _export(roots, output, timezone_name, sample)


def _export(roots, output, tz, sample):
    paths, errors = discover(roots)
    metadata, metadata_errors = read_metadata(roots)
    errors += metadata_errors
    # DB references can point outside conventional sessions directories.
    for info in metadata.values():
        if info.get('rollout_path'):
            path = Path(info['rollout_path']).expanduser()
            if not path.exists() and Path(str(path) + '.zst').exists():
                path = Path(str(path) + '.zst')
            if path.is_file() and path.resolve() not in paths:
                paths.append(path.resolve())
    paths = sorted(set(paths))
    if sample is not None:
        paths = paths[:sample]
    sources = []
    for path in paths:
        try:
            source = read_source(path)
            sources.append(source)
            errors.extend(source.errors)
        except (OSError, ValueError, RuntimeError) as e:
            errors.append({'source': str(path), 'error': str(e)})
    sessions, resolve_errors = resolve_sources(sources, metadata)
    errors += resolve_errors
    missing = sorted(set(metadata) - set(sessions)) if sample is None else []
    content_session_count = len(sessions)
    for sid in missing:
        info = metadata[sid]
        path = Path(info.get('rollout_path') or 'missing-source-' + sid)
        meta = {'id': sid, 'source_missing': True, 'source': info.get('source', '')}
        sessions[sid] = (Session(sid, meta), [Source(path, meta, [], '', Counter(), [])], [])
    manifest_path = output / '.codex-export-manifest.json'
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {'sessions': {}}
    links, titles, dates = {}, {}, {}
    for sid, (session, chunks, rows) in sessions.items():
        dependencies = {str(s.path) for s in chunks} | {r['_source'] for r in rows if '_source' in r}
        if any(e.get('source') in dependencies for e in errors):
            session.metadata['incomplete'] = True
        info = metadata.get(sid, {})
        title = info.get('name') or info.get('index_title') or info.get('title') or sid
        title = str(title).strip() or sid
        titles[sid] = title
        dates[sid] = local_time(session.metadata.get('timestamp') or info.get('created_at'), tz)
        links[sid] = filename(title, dates[sid], sid)
        previous = old.get('sessions', {}).get(sid, {})
        if (session.metadata.get('source_missing') or session.metadata.get('incomplete')) and previous.get('file') and (output / previous['file']).is_file():
            links[sid] = previous['file']
    manifest = {'version': VERSION, 'roots': [str(r) for r in roots], 'sessions': {}}
    written = 0
    for sid, (session, chunks, rows) in sessions.items():
        previous = old.get('sessions', {}).get(sid, {})
        if (session.metadata.get('source_missing') or session.metadata.get('incomplete')) and previous.get('file') and (output / previous['file']).is_file():
            # A missing source must never destroy the last known good backup.
            manifest['sessions'][sid] = dict(previous, source_missing=bool(session.metadata.get('source_missing')),
                                             incomplete=bool(session.metadata.get('incomplete')))
            links[sid] = previous['file']
            continue
        text, front = render(session, metadata.get(sid, {}), chunks, titles[sid], dates[sid], tz, links)
        name = links[sid]
        target = output / name
        if target.exists() and previous.get('file') != name:
            raise ValueError('Unmanaged output file would be overwritten: ' + str(target))
        if previous.get('file') and (output / previous['file']).is_file():
            prior_path = output / previous['file']
            actual_hash = hashlib.sha256(prior_path.read_bytes()).hexdigest()
            if actual_hash != previous.get('output_sha256'):
                errors.append({'source': str(prior_path), 'error': 'Export edited locally; original export moved to .local-edits before update'})
                edits = output / '.local-edits'
                edits.mkdir(exist_ok=True)
                backup = edits / (actual_hash[:12] + '-' + prior_path.name)
                if not backup.exists():
                    backup.write_bytes(prior_path.read_bytes())
        written += atomic_write(target, text)
        if previous.get('file') and previous['file'] != name:
            prior_path = output / previous['file']
            if prior_path.parent == output and prior_path.is_file() and not prior_path.is_symlink():
                prior_path.unlink()  # Only an earlier exporter-owned output; never source data.
        manifest['sessions'][sid] = {'file': name, 'title': titles[sid], 'created': dates[sid],
                                     'archived': front['archived'], 'kind': front['kind'],
                                     'message_count': len(session.messages), 'context_count': len(session.contexts),
                                     'sources': {str(s.path): s.sha256 for s in chunks},
                                     'source_missing': bool(front.get('source_missing')),
                                     'incomplete': bool(front.get('incomplete')),
                                     'output_sha256': hashlib.sha256(text.encode()).hexdigest()}
    # Keep previously exported sessions if a source disappears. Never silently erase a backup.
    absent = sorted(set(old.get('sessions', {})) - set(sessions))
    for sid in absent:
        manifest['sessions'][sid] = dict(old['sessions'][sid], source_missing=True)
    report = {'source_file_count': len(paths), 'readable_source_count': len(sources),
              'compressed_source_count': sum(p.suffix == '.zst' for p in paths),
              'session_count': len(sessions), 'written': written, 'unchanged': len(sessions) - written,
              'content_session_count': content_session_count,
              'database_session_count': len(metadata), 'database_missing_ids': missing,
              'previously_exported_sources_missing': absent, 'errors': errors,
              'roots': [str(r) for r in roots],
              'message_count': sum(s['message_count'] for s in manifest['sessions'].values())}
    index = ['---', 'title: "Codex 历史会话索引"', 'tags: [codex, chat-backup]', '---', '', '# Codex 历史会话', '',
             '共 {} 个会话；{} 个来源文件。时间使用 {}。'.format(len(manifest['sessions']), len(paths), tz), '',
             '正文保留原始 Markdown；子代理、分叉继承历史和其他版本均单独标记。', '',
             '增量导出会更新会话文件；个人批注请另建笔记，通过链接关联。', '']
    for title, predicate in [('活跃会话', lambda x: x['kind'] != 'subagent' and not x['archived']),
                             ('已归档会话', lambda x: x['kind'] != 'subagent' and x['archived']),
                             ('子代理记录', lambda x: x['kind'] == 'subagent')]:
        entries = [x for x in manifest['sessions'].values() if predicate(x)]
        index += ['## {}（{}）'.format(title, len(entries)), '']
        for entry in sorted(entries, key=lambda x: (x['created'], x['file']), reverse=True):
            label = re.sub(r'\s+', ' ', entry['title']).replace('[', '\\[').replace(']', '\\]')
            index.append('- {} [{}]({}) · {} 条消息{}'.format(entry['created'][:10], label, quote(entry['file']), entry['message_count'], ' · 来源已不在本机' if entry.get('source_missing') else ''))
        index.append('')
    if errors or missing:
        index += ['## 需要核对', '', '- 解析或访问问题：{}；元数据有记录但缺少历史文件的会话：{}。'.format(len(errors), len(missing)),
                  '- 详情见 `export-report.json`。', '']
    atomic_write(output / 'index.md', '\n'.join(index))
    atomic_write(manifest_path, json_text(manifest))
    atomic_write(output / 'export-report.json', json_text(report))
    return report


def main():
    if '--chatgpt' in sys.argv:
        from chatgpt_export import main as chatgpt_main
        sys.argv.remove('--chatgpt')
        return chatgpt_main()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', action='append', type=Path, help='Codex home directory; repeatable; overrides auto-discovery')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--timezone', default='Asia/Shanghai')
    parser.add_argument('--sample', type=int, help='Export only N source files to a separate sample folder')
    parser.add_argument('--discover', action='store_true', help='Only print discovered roots and files')
    args = parser.parse_args()
    roots = args.source or default_roots()
    if args.discover:
        paths, errors = discover(roots)
        print(json_text({'roots': [str(r) for r in roots], 'files': [str(p) for p in paths], 'errors': errors}))
        return 1 if errors else 0
    if args.sample is not None and (args.sample < 1 or args.output == DEFAULT_OUTPUT):
        parser.error('--sample needs a positive count and an explicit separate --output directory')
    try:
        report = export(roots, args.output, args.timezone, args.sample)
    except (OSError, ValueError, RuntimeError) as e:
        print('Export failed: ' + str(e), file=sys.stderr)
        return 1
    print(json_text(report))
    return 1 if report['errors'] or report['database_missing_ids'] else 0


if __name__ == '__main__':
    sys.exit(main())
