# Validation

## Automated checks

Current result: 26 Python tests passed. JavaScript collector checks passed. Publication scan: 19 candidate files, zero findings.

~~~bash
python3 -m unittest discover -s tests -v
node tests/test_collector.js
python3 scripts/check_public_tree.py
~~~

The Python suite uses synthetic records and temporary directories. It covers chronological order, duplicate projections, repeated messages, legacy events, revisions, rollback, inherited history, compressed files, malformed sources, incremental writes, title changes, and local-edit preservation.

ChatGPT checks cover pagination, truncation rejection, omitted internal events, rich-text markers, copied attachments, and protection of previous backups. Collector tests cover portable default paths, explicit destinations, pagination, and collection failure.

## Manual checks

### Public-copy smoke test

After redaction, only the 19 public Git candidates were copied to a separate directory. Tests and README commands ran from that copy. Real histories were read from their original locations; all outputs went to an isolated private directory.

- Environment discovery and the 3-source Codex sample succeeded. Repeating the sample rewrote zero files.
- Full Codex export produced 172 notes: 171 with content and one explicit missing-source placeholder. No parsing errors were reported. Exit code 1 correctly signaled the missing source.
- The public ChatGPT collector fetched 31 conversations through the real app tools. Conversion produced 97 messages and 15 attachments, with no errors.
- Repeating ChatGPT conversion rewrote zero notes. Markdown and image modification times were unchanged.
- All manifest entries, index links, and copied attachment files resolved.

The README now explains partial-success exit codes. This test used macOS and the available Codex desktop tools; Claude Code itself was not launched.

On macOS, exported notes were opened in Obsidian 1.15.1. Index navigation, note properties, full-text search, archive filtering, and image display were observed in the app. The public screenshot covers conversation identifiers.

Exported message text was compared with source records. Re-running an unchanged ChatGPT snapshot produced zero rewritten notes or images.

The Codex app comparison used its read-only history tools because native window automation was unavailable. Source files were never modified by the exporter.

## Limits

Missing source files produce explicit errors or placeholders. Some app responses contain only one side of a conversation. The ChatGPT interface does not expose every branch or renderable widget.

These cases are reported, not silently reconstructed. Actual counts and local source paths belong in private export-report.json files; they are intentionally excluded from this public document.

Tests have been run on macOS. Linux compatibility and native Windows support are not claimed as verified.
