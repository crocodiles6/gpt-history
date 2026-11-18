import copy
import json
import tempfile
import unittest
from pathlib import Path

from chatgpt_export import normalize_snapshot, export_chatgpt, render_rich_text


def turn(tid, text, started):
    return {'id': tid, 'startedAt': started, 'completedAt': started + 1, 'status': 'completed',
            'items': [{'type': 'userMessage', 'id': tid + '-u', 'content': [{'type': 'text', 'text': text}]},
                      {'type': 'agentMessage', 'id': tid + '-a', 'text': '# 回答\n\n```python\nprint(1)\n```'}]}


def snapshot():
    entry = {'id': 'chat-1', 'title': '测试会话', 'archived': False, 'updatedAt': 1794930290}
    thread = {'id': 'chat-1', 'kind': 'chatgpt', 'title': '测试会话', 'createdAt': 1794910000, 'updatedAt': 1794930290}
    page = {'thread': thread, 'page': {'order': 'newest_first', 'hasMore': False, 'nextCursor': None},
            'turns': [turn('t2', '重复问题', 1794910010), turn('t1', '重复问题', 1794910000)], 'attachments': []}
    return {'schema_version': 1, 'source': 'codex-app-chatgpt-history', 'inventory': entry,
            'pages': [{'request_cursor': None, 'response': page}]}


class ChatGPTTests(unittest.TestCase):
    def test_turn_order_and_repeated_text(self):
        chat = normalize_snapshot(snapshot())
        self.assertEqual([m['id'] for m in chat['messages']], ['t1-u', 't1-a', 't2-u', 't2-a'])
        self.assertEqual(chat['messages'][0]['time_basis'], 'turn_started_at')
        self.assertIn('```python\nprint(1)\n```', chat['messages'][1]['text'])

    def test_cursor_chain_requires_complete_pages(self):
        s = snapshot(); first = s['pages'][0]['response']
        first['page'].update(hasMore=True, nextCursor='older')
        with self.assertRaises(ValueError): normalize_snapshot(s)
        second = copy.deepcopy(first); second['turns'] = [turn('t0', '最早', 1794900000)]
        second['page'].update(hasMore=False, nextCursor=None)
        s['pages'].append({'request_cursor': 'older', 'response': second})
        self.assertEqual(normalize_snapshot(s)['messages'][0]['text'], '最早')
        s['pages'][1]['request_cursor'] = 'wrong'
        with self.assertRaises(ValueError): normalize_snapshot(s)

    def test_truncated_messages_rejected(self):
        s = snapshot(); s['pages'][0]['response']['turns'][0]['items'][1]['truncated'] = True
        with self.assertRaises(ValueError): normalize_snapshot(s)

    def test_hidden_reasoning_not_rendered(self):
        s = snapshot(); s['pages'][0]['response']['turns'][0]['items'].append({'type': 'reasoning', 'text': 'private'})
        chat = normalize_snapshot(s)
        self.assertNotIn('private', '\n'.join(m['text'] for m in chat['messages']))
        self.assertEqual(chat['omitted']['reasoning'], 1)

    def test_rich_links_and_unresolved_references_and_code(self):
        raw = 'url官网https://example.com entity["software","Obsidian"] citeturn1search2\n```text\nciteliteral\n```'
        notes = []
        text = render_rich_text(raw, notes)
        self.assertIn('[官网](https://example.com)', text)
        self.assertIn('Obsidian', text)
        self.assertIn('[^chatgpt-1]', text)
        self.assertIn('turn1search2', notes[0])
        self.assertIn('```text\nciteliteral\n```', text)

    def test_incremental_export_attachments_and_incomplete_preservation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'source'; root.mkdir(); out = Path(td) / 'output'
            image = Path(td) / 'img.png'; image.write_bytes(b'png-fixture')
            s = snapshot(); s['pages'][0]['response']['attachments'] = [{'name': '图片.png', 'path': str(image)}]
            source = root / 'chat-1.json'; source.write_text(json.dumps(s))
            (root / 'inventory.json').write_text(json.dumps({'complete': True, 'threads': [s['inventory']]}))
            r = export_chatgpt(root, out)
            self.assertEqual(r['written'], 1)
            md = next(p for p in out.glob('*.md') if p.name != 'index.md')
            original = md.read_bytes(); stamp = md.stat().st_mtime_ns
            self.assertIn('platform: "chatgpt"', md.read_text())
            self.assertEqual(len(list((out / 'attachments').rglob('*.png'))), 1)
            self.assertEqual(export_chatgpt(root, out)['written'], 0)
            self.assertEqual(md.stat().st_mtime_ns, stamp)
            s['pages'][0]['response']['page']['hasMore'] = True
            source.write_text(json.dumps(s))
            self.assertTrue(export_chatgpt(root, out)['errors'])
            self.assertEqual(md.read_bytes(), original)

    def test_empty_or_incomplete_inventory_is_not_success(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / 'source'; root.mkdir()
            with self.assertRaises(ValueError): export_chatgpt(root, Path(td) / 'out')


if __name__ == '__main__':
    unittest.main()
