/* Run in Codex functions.exec with its provided tools object, not a browser console.
 * Entry: collectChatGPTHistory(tools, {sourceDir, report}). No credentials are read.
 */
async function collectChatGPTHistory(tools, options = {}) {
  let sourceDir = options.sourceDir;
  if (!sourceDir) {
    const home = await tools.exec_command({
      cmd: 'python3 -c \'from pathlib import Path; print(Path.home() / "Documents/AI-Chat-Backup/.chatgpt-source")\'',
      max_output_tokens: 200
    });
    if (home.exit_code !== 0 || home.session_id || home.original_token_count > 200) {
      throw Error('Cannot determine snapshot directory; pass options.sourceDir explicitly');
    }
    sourceDir = home.output.trim();
  }
  const report = options.report || (() => {});
  if (!sourceDir.startsWith('/') || /[\r\n]/.test(sourceDir)) throw Error('Use an absolute snapshot directory');
  const unwrap = result => {
    if (result.isError) throw Error(result.content.filter(x => x.type === 'text').map(x => x.text).join('\n'));
    return JSON.parse(result.content.find(x => x.type === 'text').text);
  };
  const checkTruncation = value => {
    if (!value || typeof value !== 'object') return;
    for (const [key, child] of Object.entries(value)) {
      if (/truncat/i.test(key) && child) throw Error('Truncated history: ' + key);
      if (typeof child === 'object') checkTruncation(child);
    }
  };
  const inventory = [];
  // Active and archived histories are separate namespaces with separate cursors.
  for (const archived of [false, true]) {
    let cursor;
    const seen = new Set();
    do {
      const page = unwrap(await tools.mcp__codex_app__list_threads({
        source: 'chatgpt', mode: 'history', archived, limit: 50, ...(cursor ? {cursor} : {})
      }));
      inventory.push(...page.threads.map(thread => ({...thread, archived})));
      cursor = page.nextCursor;
      if (cursor && seen.has(cursor)) throw Error('Repeated inventory cursor');
      if (cursor) seen.add(cursor);
    } while (cursor);
  }
  const ids = new Set();
  for (const entry of inventory) {
    if (!/^[A-Za-z0-9-]{1,100}$/.test(entry.id) || ids.has(entry.id)) throw Error('Invalid or duplicate conversation ID');
    ids.add(entry.id);
  }
  const snapshots = [], failures = [];
  for (let start = 0; start < inventory.length; start += 4) {
    const batch = inventory.slice(start, start + 4);
    const results = await Promise.allSettled(batch.map(async entry => {
      const pages = [], seen = new Set();
      let cursor;
      do {
        const response = unwrap(await tools.mcp__codex_app__read_thread({
          threadId: entry.id, turnLimit: 10, includeOutputs: true,
          maxOutputCharsPerItem: 20000, ...(cursor ? {cursor} : {})
        }));
        if (response.thread.id !== entry.id || response.thread.kind !== 'chatgpt') throw Error('Mismatched response identity');
        checkTruncation(response);
        if (response.page.hasMore !== Boolean(response.page.nextCursor)) throw Error('Incomplete pagination metadata');
        // This duplicate preview is not an independent message.
        delete response.thread.preview;
        pages.push({request_cursor: cursor || null, response});
        cursor = response.page.nextCursor;
        if (cursor && seen.has(cursor)) throw Error('Repeated history cursor');
        if (cursor) seen.add(cursor);
      } while (cursor);
      return {schema_version: 1, source: 'codex-app-chatgpt-history', inventory: entry, pages};
    }));
    for (let i = 0; i < results.length; i++) {
      const result = results[i];
      if (result.status === 'fulfilled') snapshots.push(result.value);
      else failures.push({id: batch[i].id, error: String(result.reason)});
    }
    report({fetched: Math.min(start + batch.length, inventory.length), total: inventory.length});
  }
  // Collect first; a failed request does not replace any previous source snapshot.
  if (failures.length) throw Error('Collection failed; previous snapshots kept: ' + JSON.stringify(failures));
  const save = async (name, value) => {
    const content = JSON.stringify(value, null, 2);
    const result = await tools.apply_patch('*** Begin Patch\n*** Add File: ' + sourceDir + '/' + name +
      '\n+' + content.split('\n').join('\n+') + '\n*** End Patch');
    if (result && result.isError) throw Error('Snapshot write failed: ' + name);
  };
  for (const snapshot of snapshots) await save(snapshot.inventory.id + '.json', snapshot);
  await save('inventory.json', {schema_version: 1, complete: true, threads: inventory});
  return {conversations: inventory.length, pages: snapshots.reduce((n, s) => n + s.pages.length, 0), sourceDir};
}

if (typeof module !== 'undefined') module.exports = {collectChatGPTHistory};
