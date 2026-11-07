// Poll an export across runs. Task success alone does not mean its ZIP is ready.
export async function pollWorkspaceExport({spaceId, readState, saveState, post, notificationUrl,
  download, pollTimeoutMs, maxTaskAttempts = 1, now = Date.now,
  sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms)), log = console.log}) {
  let pending = readState()?.pending;
  if (pending && (pending.space_id !== spaceId || !pending.task_id ||
      !Number.isFinite(Date.parse(pending.requested_at)))) {
    throw new Error('Saved export request is invalid or belongs to another workspace');
  }
  // Notion download links expire after seven days. Do not silently abandon a
  // request: leave the checkpoint available for investigation/manual recovery.
  if (pending && now() - Date.parse(pending.requested_at) > 6 * 86400000) {
    throw new Error('Pending export is over six days old; inspect it before requesting another export');
  }
  for (let attempt = 1; attempt <= maxTaskAttempts; attempt++) {
    if (!pending) {
      const requestedAt = new Date(now()).toISOString();
      const result = await post('enqueueTask', {task: {eventName: 'exportSpace', request: {
        spaceId, shouldExportComments: false, includePrivatePages: true,
        exportOptions: {exportType: 'markdown', timeZone: 'UTC', locale: 'en', includeContents: 'everything'},
      }}});
      if (!result.taskId) throw new Error('enqueueTask returned no taskId');
      pending = {space_id: spaceId, task_id: result.taskId, requested_at: requestedAt};
      // Persist immediately, before waiting. A later failed job can resume it.
      saveState({pending});
      log(`[workspace-task] requested task=${pending.task_id} at=${requestedAt}`);
    } else {
      log(`[workspace-task] resuming task=${pending.task_id} requested_at=${pending.requested_at}`);
    }
    const deadline = now() + pollTimeoutMs;
    let lastState;
    while (now() < deadline) {
      // Check notifications first: task records can disappear before the link
      // arrives, or the task endpoint can fail independently.
      const url = await notificationUrl(spaceId, Date.parse(pending.requested_at));
      if (url) {
        log(`[workspace-task] download link found; request_age_seconds=${Math.round((now() - Date.parse(pending.requested_at)) / 1000)}`);
        return download(url);
      }
      let data;
      try {
        data = await post('getTasks', {taskIds: [pending.task_id]});
      } catch (err) {
        log(`[workspace-task] task lookup failed; http=${err.httpStatus ?? 'network/unknown'}`);
        await sleep(Math.min(30000, Math.max(0, deadline - now())));
        continue;
      }
      const task = data?.results?.find(t => t.id === pending.task_id);
      const state = task?.state || 'missing';
      if (state !== lastState) {
        log(`[workspace-task] task=${pending.task_id} state=${state}`);
        lastState = state;
      }
      const status = task?.status || {};
      const taskUrl = status.exportURL || status.exportUrl || status.export_url || status.url || status.signedUrl;
      if (state === 'success' && taskUrl) return download(taskUrl);
      if (!['missing', 'success', 'in_progress', 'not_started'].includes(state)) {
        const failedId = pending.task_id;
        saveState({pending: null});
        pending = null;
        if (state === 'retryable_failure' && attempt < maxTaskAttempts) break;
        throw Object.assign(new Error(`workspace export task failed with state=${state}`), {
          stage: 'poll_task', taskId: failedId, taskState: state, taskAttempt: attempt, maxTaskAttempts,
        });
      }
      await sleep(Math.min(30000, Math.max(0, deadline - now())));
    }
    if (pending) {
      // A timeout is not a reason to enqueue again, even if retries are enabled.
      throw Object.assign(new Error(`workspace export still pending after ${Math.round(pollTimeoutMs / 1000)}s; saved for the next run (last_state=${lastState || 'unknown'})`), {
        stage: 'poll_task', taskId: pending.task_id, taskState: lastState || 'unknown',
        taskAttempt: attempt, maxTaskAttempts,
      });
    }
  }
}
