import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.check_public_tree import publication_files, scan_file


class PublicationTests(unittest.TestCase):
    def test_tracked_ignored_data_still_gets_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            (root / 'session.jsonl').write_text('{}\n')
            subprocess.run(['git', '-C', str(root), 'add', 'session.jsonl'], check=True)
            (root / '.gitignore').write_text('*.jsonl\n')
            (root / 'other.jsonl').write_text('{}\n')
            files = publication_files(root)
            self.assertIn('session.jsonl', files)
            self.assertNotIn('other.jsonl', files)
            self.assertIn('private-artifact', [x[2] for x in scan_file(root, 'session.jsonl')])

    def test_findings_do_not_echo_secret_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = 'ghp_' + 'x' * 36
            note = root / 'note.md'
            note.write_text(secret + '\n' + '/' + 'Users' + '/demo-person/private\n')
            issues = scan_file(root, note.name)
            self.assertEqual({x[2] for x in issues}, {'github-token', 'personal-home-path'})
            self.assertNotIn(secret, repr(issues))

    def test_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'link').symlink_to(root / 'missing-private-target')
            self.assertEqual(scan_file(root, 'link')[0][2], 'symlink-needs-review')
