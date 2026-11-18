const assert = require('node:assert/strict');
const {collectChatGPTHistory} = require('../collect_chatgpt.js');
const wrap = value => ({content: [{type: 'text', text: JSON.stringify(value)}]});

function fixture() {
  const writes = [], reads = [];
  return {writes, reads, tools: {
    exec_command: async () => ({exit_code: 0, output: '/tmp/demo-home/Documents/AI-Chat-Backup/.chatgpt-source\n'}),
    mcp__codex_app__list_threads: async ({archived}) => wrap({threads: archived ? [] : [{id: 'demo-chat'}], nextCursor: null}),
    mcp__codex_app__read_thread: async args => {
      reads.push(args);
      return wrap({thread: {id: args.threadId, kind: 'chatgpt', preview: 'duplicate'},
        page: {order: 'newest_first', hasMore: !args.cursor, nextCursor: args.cursor ? null : 'older'}, turns: []});
    },
    apply_patch: async patch => {writes.push(patch); return {};}
  }};
}

(async () => {
  const normal = fixture();
  const result = await collectChatGPTHistory(normal.tools);
  assert.equal(result.conversations, 1);
  assert.equal(result.pages, 2);
  assert.match(result.sourceDir, /^\/tmp\/demo-home\//);
  assert.equal(normal.reads[1].cursor, 'older');
  assert.equal(normal.writes.length, 2);
  assert.ok(!normal.writes[0].includes('preview'));
  assert.match(normal.writes[1], /inventory\.json/);

  const explicit = fixture();
  explicit.tools.exec_command = async () => {throw Error('Must not inspect home with explicit destination');};
  await collectChatGPTHistory(explicit.tools, {sourceDir: '/tmp/custom-snapshots'});
  assert.match(explicit.writes[0], /\/tmp\/custom-snapshots\//);

  const failed = fixture();
  failed.tools.mcp__codex_app__read_thread = async () => {throw Error('Unavailable');};
  await assert.rejects(collectChatGPTHistory(failed.tools), /previous snapshots kept/);
  assert.equal(failed.writes.length, 0);

  const badHome = fixture();
  badHome.tools.exec_command = async () => ({exit_code: 1, output: ''});
  await assert.rejects(collectChatGPTHistory(badHome.tools), /Cannot determine snapshot directory/);
  assert.equal(badHome.writes.length, 0);
  console.log('Collector: portable defaults, explicit destination, pagination and failure preservation passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
