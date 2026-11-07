import test from 'node:test';
import assert from 'node:assert/strict';
import {pollWorkspaceExport} from '../scripts/workspace_export_task.mjs';

function fixture(initial = null) {
  let time = Date.parse('2026-11-05T00:00:00Z');
  let state = initial;
  const calls = [];
  const options = {
    spaceId: 'space', readState: () => state,
    saveState: value => { state = value; calls.push('saved'); },
    post: async endpoint => {
      calls.push(endpoint);
      return endpoint === 'enqueueTask' ? {taskId: 'task'} : {results: [{id: 'task', state: 'success'}]};
    },
    notificationUrl: async () => null,
    download: async url => { calls.push('download'); return Buffer.from(url); },
    now: () => time, sleep: async ms => { time += ms; }, log: () => {},
    pollTimeoutMs: 60000, maxTaskAttempts: 3,
  };
  return {options, calls, state: () => state};
}

test('success without URL times out but persists one request; next run resumes and downloads', async () => {
  const f = fixture();
  await assert.rejects(pollWorkspaceExport(f.options), err => {
    assert.equal(err.taskId, 'task');
    assert.equal(err.taskState, 'success');
    return /saved for the next run/.test(err.message);
  });
  assert.equal(f.calls.filter(x => x === 'enqueueTask').length, 1);
  assert.ok(f.calls.indexOf('saved') < f.calls.indexOf('getTasks'));
  const original = structuredClone(f.state());
  f.options.notificationUrl = async (space, since) => {
    assert.equal(space, 'space');
    assert.equal(since, Date.parse(original.pending.requested_at));
    return 'zip';
  };
  assert.equal((await pollWorkspaceExport(f.options)).toString(), 'zip');
  assert.equal(f.calls.filter(x => x === 'enqueueTask').length, 1);
  assert.deepEqual(f.state(), original, 'keep request until publication succeeds');
});

test('missing task records do not prevent later notification retrieval', async () => {
  const f = fixture({pending: {space_id: 'space', task_id: 'task', requested_at: '2026-11-04T22:00:00Z'}});
  f.options.post = async () => ({results: []});
  let polls = 0;
  f.options.notificationUrl = async () => ++polls === 2 ? 'zip' : null;
  assert.equal((await pollWorkspaceExport(f.options)).toString(), 'zip');
  assert.ok(f.state().pending);
});

test('download or checkpoint failures retain the pending request', async () => {
  const f = fixture();
  f.options.notificationUrl = async () => 'zip';
  f.options.download = async () => { throw new Error('download failed'); };
  await assert.rejects(pollWorkspaceExport(f.options), /download failed/);
  assert.ok(f.state().pending);
  const g = fixture();
  g.options.saveState = () => { throw new Error('push failed'); };
  await assert.rejects(pollWorkspaceExport(g.options), /push failed/);
  assert.deepEqual(g.calls, ['enqueueTask']);
});

test('terminal failures clear pending state; timeout never consumes task retries', async () => {
  const f = fixture();
  f.options.post = async endpoint => endpoint === 'enqueueTask' ? {taskId:'task'} : {results:[{id:'task',state:'failure'}]};
  await assert.rejects(pollWorkspaceExport(f.options), /state=failure/);
  assert.equal(f.state().pending, null);
});

test('invalid workspace and stale state fail without requesting another export', async () => {
  for (const pending of [
    {space_id:'other',task_id:'task',requested_at:'2026-11-05T00:00:00Z'},
    {space_id:'space',task_id:'task',requested_at:'2026-10-01T00:00:00Z'},
  ]) {
    const f = fixture({pending});
    await assert.rejects(pollWorkspaceExport(f.options));
    assert.deepEqual(f.calls, []);
  }
});
