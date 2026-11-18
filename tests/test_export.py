import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

import codex_history as history
from codex_export import export


def event(kind, payload, timestamp='2026-11-17T10:00:00Z'):
    return {'type': kind, 'timestamp': timestamp, 'payload': payload}


def meta(sid='session-1', **kwargs):
    return event('session_meta', {'id': sid, 'timestamp': '2026-11-17T10:00:00Z', **kwargs})


def response(role, text, **kwargs):
    return event('response_item', {'type': 'message', 'role': role, 'content': [{'type': 'input_text', 'text': text}], **kwargs})


def ui(role, text, mid, turn='turn-1'):
    return event('event_msg', {'type': 'item_completed', 'turn_id': turn, 'item': {'type': role, 'id': mid, 'content': [{'type': 'text', 'text': text}]}})


class ParsingTests(unittest.TestCase):
    def test_message_revision_keeps_latest_and_labels_old_version(self):
        s = history.parse_records([meta(), ui('AgentMessage', 'draft', 'a'), ui('AgentMessage', 'revised', 'a')], Path('/f'))
        self.assertEqual([m.text for m in s.messages if not m.variant], ['revised'])
        self.assertEqual([m.text for m in s.messages if m.variant], ['draft'])
    def test_image_projection_and_hidden_app_context(self):
        raw = response('user', '看图片\n\n<image name=[Image #1] path="/tmp/a.png">\n\n[内嵌图片，原始数据保留在来源文件中]')
        shown = ui('UserMessage', '看图片\n\n[图片附件：/tmp/a.png]', 'u')
        s = history.parse_records([meta(), event('event_msg', {'type': 'task_started', 'turn_id': 'turn-1'}), response('user', '<external_codex_apps_open_page>{}</external_codex_apps_open_page>'), raw, shown], Path('/f'))
        self.assertEqual(len(s.messages), 1)
        self.assertEqual(s.messages[0].stream, 'ui')
        self.assertEqual(len(s.contexts), 1)

    def test_ui_projection_deduplicates_but_repeated_turns_survive(self):
        rows = [meta(), event('event_msg', {'type': 'task_started', 'turn_id': 'turn-1'}),
                response('user', '你好'), ui('UserMessage', '你好', 'u1'),
                ui('AgentMessage', '回答', 'a1'), response('assistant', '回答', id='a1'),
                event('event_msg', {'type': 'task_started', 'turn_id': 'turn-2'}),
                response('user', '你好'), ui('UserMessage', '你好', 'u2', 'turn-2')]
        s = history.parse_records(rows, Path('/fake.jsonl'))
        self.assertEqual([(m.role, m.text) for m in s.messages], [('user', '你好'), ('assistant', '回答'), ('user', '你好')])
        self.assertEqual(s.omitted['duplicate_projection'], 3)

    def test_internal_content_is_not_dialogue_and_markdown_is_preserved(self):
        text = '# 标题\n\n- 项目\n\n```python\nprint(1)\n```'
        rows = [meta(), response('user', '# AGENTS.md instructions\nprivate setup'),
                response('developer', 'internal policy'), response('assistant', 'private analysis', channel='analysis'),
                event('response_item', {'type': 'reasoning', 'encrypted_content': 'secret ciphertext'}),
                response('user', '问题'), response('assistant', text),
                event('compacted', {'message': 'summary', 'replacement_history': []})]
        s = history.parse_records(rows, Path('/fake.jsonl'))
        self.assertEqual([m.text for m in s.messages], ['问题', text])
        self.assertEqual(s.omitted['internal_reasoning'], 2)
        self.assertEqual(len(s.contexts), 1)
        self.assertTrue(any('compacted' in n for n in s.notices))

    def test_legacy_events_merge_without_removing_repeated_same_stream_messages(self):
        rows = [meta(), event('event_msg', {'type': 'user_message', 'message': 'same'}), response('user', 'same'),
                event('event_msg', {'type': 'user_message', 'message': 'same'}), response('user', 'same'),
                event('event_msg', {'type': 'agent_message', 'message': 'done'}), response('assistant', 'done')]
        self.assertEqual([m.text for m in history.parse_records(rows, Path('/f')).messages], ['same', 'same', 'done'])

    def test_fork_uses_outer_metadata_and_preserves_shared_history(self):
        s = history.parse_records([meta('child', forked_from_id='parent'), meta('parent'), response('user', 'old'), response('user', 'new')], Path('/f'))
        self.assertEqual(s.id, 'child')
        self.assertEqual(s.metadata['forked_from_id'], 'parent')
        self.assertEqual([m.text for m in s.messages], ['old', 'new'])

    def test_rollback_retains_but_labels_retracted_messages(self):
        rows = [meta(), event('event_msg', {'type': 'task_started', 'turn_id': 't'}), response('user', 'undo'),
                event('event_msg', {'type': 'thread_rolled_back', 'num_turns': 1})]
        s = history.parse_records(rows, Path('/f'))
        self.assertTrue(s.messages[0].retracted)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'source'
        self.out = Path(self.temp.name) / 'output'
        (self.root / 'sessions').mkdir(parents=True)
        self.source = self.root / 'sessions' / 'rollout-test.jsonl'
        self.write([meta(), response('user', '问题'), response('assistant', '回答')])

    def write(self, rows):
        self.source.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))

    def test_incremental_source_integrity_and_missing_output_repair(self):
        before = self.source.read_bytes(), self.source.stat().st_mtime_ns
        first = export([self.root], self.out)
        self.assertEqual(first['written'], 1)
        md = next(p for p in self.out.glob('*.md') if p.name != 'index.md')
        stamp = md.stat().st_mtime_ns
        second = export([self.root], self.out)
        self.assertEqual(second['written'], 0)
        self.assertEqual(md.stat().st_mtime_ns, stamp)
        self.assertEqual((self.source.read_bytes(), self.source.stat().st_mtime_ns), before)
        self.assertIn('session_id:', md.read_text())
        md.unlink()
        self.assertEqual(export([self.root], self.out)['written'], 1)

    def test_zstd_archives_and_unicode_filename(self):
        archived = self.root / 'archived_sessions'
        archived.mkdir()
        dest = archived / 'rollout-test.jsonl.zst'
        subprocess.run(['zstd', '-q', '-o', str(dest), str(self.source)], check=True)
        self.source.unlink()
        (self.root / 'session_index.jsonl').write_text(json.dumps({'id': 'session-1', 'thread_name': '题目 / 含冒号：测试'}) + '\n')
        result = export([self.root], self.out)
        self.assertEqual(result['session_count'], 1)
        md = next(p for p in self.out.glob('*.md') if p.name != 'index.md')
        self.assertIn('题目', md.name)
        self.assertIn('2026-11-17', md.name)
        self.assertIn('archived: true', md.read_text())

    def test_bad_lines_reported_and_valid_messages_retained(self):
        with self.source.open('a') as f: f.write('{broken\n')
        result = export([self.root], self.out)
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(result['session_count'], 1)

    def test_output_inside_source_is_rejected(self):
        with self.assertRaises(ValueError): export([self.root], self.root / 'backup')

    def test_fork_prefix_is_reconstructed(self):
        self.write([dict(meta('parent'), ordinal=0), dict(response('user', 'shared'), ordinal=1), dict(response('assistant', 'later parent only'), ordinal=3)])
        child = self.root / 'sessions' / 'rollout-child.jsonl'
        rows = [dict(meta('child', history_base={'thread_id': 'parent', 'end_ordinal_exclusive': 2}, forked_from_id='parent'), ordinal=2), dict(response('user', 'child only'), ordinal=3)]
        child.write_text(''.join(json.dumps(x) + '\n' for x in rows))
        export([self.root], self.out)
        md = next(self.out.glob('*[[]child].md')).read_text()
        self.assertIn('shared', md)
        self.assertIn('child only', md)
        self.assertNotIn('later parent only', md)
        self.assertIn('继承历史', md)

    def test_title_update_and_local_edits_are_preserved(self):
        export([self.root], self.out)
        old = next(p for p in self.out.glob('*.md') if p.name != 'index.md')
        old.write_text(old.read_text() + '\nmy notes\n')
        (self.root / 'session_index.jsonl').write_text(json.dumps({'id': 'session-1', 'thread_name': 'new title'}) + '\n')
        export([self.root], self.out)
        self.assertFalse(old.exists())
        self.assertTrue(list(self.out.glob('*new title*.md')))
        self.assertIn('my notes', next((self.out / '.local-edits').iterdir()).read_text())

    def test_missing_source_is_explicit_metadata_only_note(self):
        c = sqlite3.connect(self.root / 'state_5.sqlite')
        c.execute('CREATE TABLE threads (id TEXT, name TEXT, rollout_path TEXT, created_at INTEGER, archived INTEGER)')
        c.execute('INSERT INTO threads VALUES (?, ?, ?, ?, ?)', ('missing', 'Missing conversation', str(self.root / 'missing.jsonl'), 1794910255, 1))
        c.commit(); c.close()
        result = export([self.root], self.out)
        self.assertEqual(result['database_missing_ids'], ['missing'])
        md = next(self.out.glob('*[[]missing].md')).read_text()
        self.assertIn('source_missing: true', md)
        self.assertIn('原始历史文件缺失', md)

    def test_disappeared_source_does_not_replace_existing_backup_with_placeholder(self):
        c = sqlite3.connect(self.root / 'state_5.sqlite')
        c.execute('CREATE TABLE threads (id TEXT, name TEXT, rollout_path TEXT)')
        c.execute('INSERT INTO threads VALUES (?, ?, ?)', ('session-1', 'Backup', str(self.source)))
        c.commit(); c.close()
        export([self.root], self.out)
        md = next(p for p in self.out.glob('*.md') if p.name != 'index.md')
        before = md.read_bytes()
        self.source.unlink()
        result = export([self.root], self.out)
        self.assertEqual(result['database_missing_ids'], ['session-1'])
        self.assertEqual(md.read_bytes(), before)

    def test_corrupt_source_does_not_destroy_previous_backup(self):
        export([self.root], self.out)
        md = next(p for p in self.out.glob('*.md') if p.name != 'index.md')
        before = md.read_bytes()
        self.write([meta()])
        with self.source.open('a') as f: f.write('{broken\n')
        result = export([self.root], self.out)
        self.assertTrue(result['errors'])
        self.assertEqual(md.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
