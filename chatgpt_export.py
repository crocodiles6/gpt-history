#!/usr/bin/env python3
"""Export complete Codex-app ChatGPT history snapshots to an Obsidian vault."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from codex_export import atomic_write, filename, json_text, local_time

DEFAULT_SOURCE = Path.home() / 'Documents/AI-Chat-Backup/.chatgpt-source'
DEFAULT_OUTPUT = Path.home() / 'Documents/AI-Chat-Backup/ChatGPT'
SOURCE_KIND = 'codex-app-chatgpt-history'


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', value):
        raise ValueError('Invalid conversation ID')
    return value


def check_truncation(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if 'truncat' in key.lower() and child:
                raise ValueError('History response contains a truncation flag: ' + key)
            check_truncation(child)
    elif isinstance(value, list):
        for child in value:
            check_truncation(child)


def normalize_snapshot(snapshot):
    if snapshot.get('schema_version') != 1 or snapshot.get('source') != SOURCE_KIND:
        raise ValueError('Unsupported ChatGPT snapshot format')
    entry = snapshot['inventory']
    sid = safe_id(entry['id'])
    pages = snapshot.get('pages', [])
    if not pages:
        raise ValueError('Snapshot has no history pages')
    expected_cursor, seen_cursors, turns, attachments = None, set(), [], []
    for number, wrapper in enumerate(pages):
        if wrapper.get('request_cursor') != expected_cursor:
            raise ValueError('History pagination cursor chain is broken')
        response = wrapper['response']
        if response['thread']['id'] != sid or response['thread'].get('kind') != 'chatgpt':
            raise ValueError('History thread identity mismatch')
        check_truncation(response)
        page = response['page']
        if page.get('order') != 'newest_first' or not isinstance(response.get('turns'), list):
            raise ValueError('Unsupported history order or missing turns')
        more = page.get('hasMore')
        expected_cursor = page.get('nextCursor')
        if more is not bool(expected_cursor):
            raise ValueError('Inconsistent pagination completion flags')
        if expected_cursor:
            if expected_cursor in seen_cursors:
                raise ValueError('Repeated pagination cursor')
            seen_cursors.add(expected_cursor)
        if number < len(pages) - 1 and not more:
            raise ValueError('Unexpected page after completed history')
        turns.extend(response['turns'])
        attachments.extend(response.get('attachments', []))
    if expected_cursor:
        raise ValueError('History pagination is incomplete')
    thread = pages[0]['response']['thread']
    chat = {'id': sid, 'title': thread.get('title') or entry.get('title') or sid,
            'created': thread.get('createdAt'), 'updated': thread.get('updatedAt', entry.get('updatedAt')),
            'archived': bool(entry.get('archived')), 'messages': [], 'omitted': Counter(),
            'warnings': [], 'attachments': [], 'turn_count': 0}
    seen_turns, seen_messages = {}, {}
    for turn in reversed(turns):
        tid = turn['id']
        if tid in seen_turns:
            if turn != seen_turns[tid]:
                raise ValueError('Turn changed between history pages: ' + tid)
            continue
        seen_turns[tid] = turn
        chat['turn_count'] += 1
        roles = set()
        for item in turn.get('items', []):
            kind = item.get('type', 'unknown')
            if kind not in ('userMessage', 'agentMessage') or item.get('channel') == 'analysis':
                chat['omitted'][kind] += 1
                continue
            role = 'user' if kind == 'userMessage' else 'assistant'
            parts = []
            if role == 'assistant':
                text = item.get('text', '')
            else:
                for content in item.get('content', []):
                    if content.get('type') == 'text':
                        parts.append(content.get('text', ''))
                    else:
                        parts.append('[非文本消息内容：' + str(content.get('type', 'unknown')) + '，参见附件与来源快照]')
                text = '\n\n'.join(parts)
            mid = str(item.get('id') or tid + '-' + str(len(chat['messages'])))
            identity = (tid, mid)
            if identity in seen_messages:
                if seen_messages[identity] != (role, text):
                    raise ValueError('Conflicting versions of message: ' + mid)
                chat['omitted']['duplicate_message'] += 1
                continue
            seen_messages[identity] = (role, text)
            roles.add(role)
            chat['messages'].append({'id': mid, 'turn_id': tid, 'role': role, 'text': text,
                                     'time': turn.get('startedAt') if role == 'user' else turn.get('completedAt'),
                                     'time_basis': 'turn_started_at' if role == 'user' else 'turn_completed_at',
                                     'phase': item.get('phase'), 'turn_status': turn.get('status')})
        if roles == {'assistant'}:
            chat['warnings'].append({'kind': 'assistant_only_turn', 'turn_id': tid, 'message': '此轮接口仅返回 Assistant 回复，未返回用户提问。'})
        elif roles == {'user'}:
            chat['warnings'].append({'kind': 'user_only_turn', 'turn_id': tid, 'message': '此轮接口仅返回用户提问，未返回 Assistant 回复。'})
    seen_attachments = set()
    for attachment in attachments:
        identity = (attachment.get('name'), attachment.get('path'))
        if identity not in seen_attachments:
            chat['attachments'].append(attachment)
            seen_attachments.add(identity)
    return chat


def render_rich_text(text, notes):
    """Convert UI-only markers outside fenced/inline code; keep original in snapshot."""
    def marker(match):
        kind, payload = match.group(1), match.group(2)
        if kind == 'url':
            parts = payload.split('', 1)
            if len(parts) == 2 and parts[1].startswith(('https://', 'http://')):
                label = parts[0].replace('[', '\\[').replace(']', '\\]')
                return '[' + label + '](' + quote(parts[1], safe=':/?&=#%+@,;~') + ')'
        if kind == 'entity':
            try:
                entity = json.loads(payload)
                if isinstance(entity, list) and len(entity) > 1 and isinstance(entity[1], str):
                    return entity[1]
            except ValueError:
                pass
        descriptions = {'cite': '引用标识，接口未提供对应网址', 'image_group': '图片组，接口未提供图片文件',
                        'genui': '交互组件，数据保留在来源快照', 'entity': '实体卡片', 'url': '无法转换的链接标记'}
        description = descriptions.get(kind, '未渲染的 ChatGPT 组件：' + kind)
        if kind == 'cite':
            description += '：' + payload.replace('', '、')
        elif kind == 'image_group':
            try:
                queries = json.loads(payload).get('query', [])
                description += '；检索词：' + '；'.join(queries)
            except (ValueError, TypeError, AttributeError):
                pass
        notes.append(description)
        prefix = {'image_group': '［图片组］', 'genui': '［交互组件］'}.get(kind, '')
        return prefix + '[^chatgpt-' + str(len(notes)) + ']'

    result, fence = [], None
    for line in text.splitlines(keepends=True):
        boundary = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if boundary:
            delimiter = boundary.group(1)
            if fence is None:
                fence = delimiter
            elif delimiter[0] == fence[0] and len(delimiter) >= len(fence):
                fence = None
            result.append(line)
        elif fence:
            result.append(line)
        else:
            spans = re.split(r'(`+[^`]*`+)', line)
            result.append(''.join(span if span.startswith('`') else re.sub(r'([^]+)(.*?)', marker, span) for span in spans))
    return ''.join(result)


def managed_path(output, name):
    if not isinstance(name, str):
        raise ValueError('Invalid managed output path')
    path = output / name
    if not path.resolve().is_relative_to(output) or path.is_symlink():
        raise ValueError('Managed path escapes output or is a symlink: ' + name)
    return path


def copy_attachments(chat, output, previous):
    results, warnings = [], []
    for attachment in chat['attachments']:
        source = Path(attachment.get('path') or '')
        name = attachment.get('name') or source.name
        if not source.is_file():
            old = next((a for a in previous.get('attachments', []) if a['source'] == str(source)), None)
            if old and managed_path(output, old['file']).is_file():
                results.append(old)
            else:
                warnings.append({'kind': 'missing_attachment', 'message': '附件文件不可读：' + name})
            continue
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        safe_name = re.sub(r'[\x00-\x1f/\\:*?"<>|\[\]#^]', '_', name).encode()[:140].decode(errors='ignore')
        relative = 'attachments/' + chat['id'] + '/' + digest[:16] + '-' + safe_name
        path = managed_path(output, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != data:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
                tmp = Path(f.name)
                f.write(data)
            try:
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        results.append({'name': name, 'source': str(source), 'file': relative, 'sha256': digest})
    return results, warnings


def render_chat(chat, source, attachments, timezone_name):
    front = {'title': chat['title'], 'created': local_time(chat['created'], timezone_name),
             'updated': local_time(chat['updated'], timezone_name), 'session_id': chat['id'],
             'source': str(source), 'url': 'https://chatgpt.com/c/' + chat['id'], 'platform': 'chatgpt',
             'archived': chat['archived'], 'message_count': len(chat['messages']), 'turn_count': chat['turn_count'],
             'history_scope': 'api_visible_history', 'timezone': timezone_name, 'tags': ['chatgpt', 'chat-backup']}
    parts = ['---'] + [k + ': ' + json.dumps(v, ensure_ascii=False) for k, v in front.items()] + ['---', '', '# ' + chat['title'].replace('\n', ' '), '',
             '[返回索引](index.md) · [打开 ChatGPT 原会话](' + front['url'] + ')', '',
             '> 以下为应用历史接口返回的可见消息。接口未提供分支树；时间按轮次开始或完成时间标注。', '']
    for warning in chat['warnings']:
        parts += ['> ' + warning['message'] + (' 轮次 ID：`' + warning['turn_id'] + '`' if warning.get('turn_id') else ''), '']
    notes = []
    for m in chat['messages']:
        role = 'User' if m['role'] == 'user' else 'Assistant'
        basis = '轮次开始' if m['time_basis'] == 'turn_started_at' else '轮次完成'
        parts += ['---', '', '## ' + role + ' · ' + (local_time(m['time'], timezone_name) or '未记录时间') + '（' + basis + '）', '',
                  render_rich_text(m['text'], notes), '',
                  '<!-- message_id: ' + m['id'].replace('--', '—') + ' ; turn_id: ' + m['turn_id'].replace('--', '—') + ' -->', '']
    if attachments:
        parts += ['---', '', '## 附件', '', '> 以下附件由接口随会话返回；接口未标注它们分别属于哪条消息。', '']
        for a in attachments:
            label = a['name'].replace('[', '\\[').replace(']', '\\]')
            prefix = '!' if Path(a['file']).suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp') else ''
            parts += [prefix + '[' + label + '](' + quote(a['file']) + ')', '']
    if notes:
        parts += ['---', '', '## 引用与非文本内容', '']
        parts += ['[^chatgpt-{}]: {}'.format(i, note.replace('\n', ' ')) for i, note in enumerate(notes, 1)]
        parts.append('')
    if chat['omitted']:
        parts += ['## 非对话事件', '', '以下事件未混入正文：', '']
        parts += ['- {}：{}'.format(k, v) for k, v in sorted(chat['omitted'].items())]
    return '\n'.join(parts) + '\n', front, len(notes)


def export_chatgpt(snapshot_dir=DEFAULT_SOURCE, output=DEFAULT_OUTPUT, timezone_name='Asia/Shanghai', only=None):
    source = Path(snapshot_dir).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if source == output or source in output.parents or output in source.parents:
        raise ValueError('Snapshot directory and output must be separate')
    ZoneInfo(timezone_name)
    inventory_path = source / 'inventory.json'
    if not inventory_path.is_file():
        raise ValueError('Complete ChatGPT inventory.json is required')
    inventory = json.loads(inventory_path.read_text())
    if inventory.get('complete') is not True or not isinstance(inventory.get('threads'), list):
        raise ValueError('ChatGPT inventory collection is incomplete')
    entries = inventory['threads']
    ids = [safe_id(x['id']) for x in entries]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate conversation ID in inventory')
    if only:
        if not set(only) <= set(ids):
            raise ValueError('Requested sample is absent from inventory')
        entries = [x for x in entries if x['id'] in only]
    output.mkdir(parents=True, exist_ok=True)
    with managed_path(output, '.export.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return _export_chatgpt(source, output, entries, timezone_name)


def _export_chatgpt(source, output, entries, timezone_name):
    manifest_path = managed_path(output, '.chatgpt-export-manifest.json')
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {'sessions': {}}
    manifest = {'version': 1, 'sessions': dict(old['sessions'])}
    report = {'expected_sessions': len(entries), 'exported_sessions': 0, 'written': 0, 'unchanged': 0,
              'message_count': 0, 'attachment_count': 0, 'errors': [], 'warnings': [],
              'history_scope': 'api_visible_history', 'snapshots': str(source)}
    for entry in entries:
        sid = entry['id']; path = source / (sid + '.json')
        previous = old['sessions'].get(sid, {})
        try:
            raw = path.read_bytes()
            snapshot = json.loads(raw)
            if snapshot['inventory']['id'] != sid:
                raise ValueError('Snapshot identity does not match inventory')
            chat = normalize_snapshot(snapshot)
            # The inventory is authoritative for title/archive changes since the last body fetch.
            chat['title'] = entry.get('title') or chat['title']
            chat['archived'] = bool(entry.get('archived'))
            assets, warnings = copy_attachments(chat, output, previous)
            chat['warnings'].extend(warnings)
            text, front, note_count = render_chat(chat, path, assets, timezone_name)
            name = filename(chat['title'], front['created'], sid)
            target = managed_path(output, name)
            prior_path = managed_path(output, previous['file']) if previous.get('file') else None
            if target.exists() and previous.get('file') != name:
                raise ValueError('Unmanaged file would be overwritten: ' + name)
            if prior_path and prior_path.is_file():
                prior_bytes = prior_path.read_bytes(); digest = hashlib.sha256(prior_bytes).hexdigest()
                if digest != previous.get('output_sha256'):
                    backup = managed_path(output, '.local-edits/' + digest[:12] + '-' + prior_path.name)
                    backup.parent.mkdir(exist_ok=True)
                    if not backup.exists():
                        backup.write_bytes(prior_bytes)
                    report['warnings'].append({'id': sid, 'kind': 'local_edit_backed_up', 'message': '本地编辑已保存至 .local-edits'})
            changed = atomic_write(target, text)
            if prior_path and prior_path != target and prior_path.exists():
                prior_path.unlink()
            manifest['sessions'][sid] = {'file': name, 'title': chat['title'], 'created': front['created'],
                'updated': front['updated'], 'archived': chat['archived'], 'message_count': len(chat['messages']),
                'turn_count': chat['turn_count'], 'source_sha256': hashlib.sha256(raw).hexdigest(),
                'output_sha256': hashlib.sha256(text.encode()).hexdigest(), 'attachments': assets,
                'annotation_count': note_count, 'warnings': chat['warnings']}
            report['written' if changed else 'unchanged'] += 1
            report['exported_sessions'] += 1
            report['message_count'] += len(chat['messages'])
            report['attachment_count'] += len(assets)
            report['warnings'].extend(dict(w, id=sid) for w in chat['warnings'])
        except (OSError, ValueError, KeyError, TypeError) as error:
            report['errors'].append({'id': sid, 'source': str(path), 'error': str(error), 'previous_backup_preserved': bool(previous)})
    index = ['---', 'title: "ChatGPT 历史会话索引"', 'tags: [chatgpt, chat-backup]', '---', '', '# ChatGPT 历史会话', '',
             '共 {} 个会话。时间使用 {}。'.format(len(manifest['sessions']), timezone_name), '',
             '会话由当前应用接口读取；原始响应保存在本机来源快照中。分支树未由接口返回。', '',
             '使用全文搜索查找正文；可通过 archived、created、session_id 等属性管理笔记。个人批注建议另建笔记并链接到会话。', '']
    for archived in (False, True):
        rows = [x for x in manifest['sessions'].values() if x['archived'] == archived]
        index += ['## {}（{}）'.format('已归档会话' if archived else '未归档会话', len(rows)), '']
        for row in sorted(rows, key=lambda x: (x['updated'], x['file']), reverse=True):
            label = re.sub(r'\s+', ' ', row['title']).replace('[', '\\[').replace(']', '\\]')
            index.append('- {} [{}]({}) · {} 条消息{}'.format(row['created'][:10], label, quote(row['file']), row['message_count'], ' · 有内容说明' if row['warnings'] else ''))
        index.append('')
    if report['warnings'] or report['errors']:
        index += ['## 内容说明', '', '单侧消息、不可读附件和导出错误见各会话提示及 `export-report.json`。', '']
    atomic_write(managed_path(output, 'index.md'), '\n'.join(index))
    atomic_write(manifest_path, json_text(manifest))
    atomic_write(managed_path(output, 'export-report.json'), json_text(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot-dir', type=Path, default=DEFAULT_SOURCE)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--timezone', default='Asia/Shanghai')
    parser.add_argument('--only', action='append', help='Export selected IDs to a separate sample output')
    args = parser.parse_args()
    if args.only and args.output == DEFAULT_OUTPUT:
        parser.error('--only requires a separate sample --output directory')
    try:
        result = export_chatgpt(args.snapshot_dir, args.output, args.timezone, args.only)
        print(json_text(result))
        return 1 if result['errors'] else 0
    except (OSError, ValueError) as error:
        print('ChatGPT export failed: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
