import assert from 'node:assert/strict';
import test from 'node:test';

import {
  activityPreviewFileState,
  projectToFollowDuringGeneration,
  projectToOpenAfterGeneration,
  resolveActivityPreviewPath,
  selectedFileAfterProjectRefresh,
  shouldFocusFilesForProjectRefresh
} from './projectSelectionService.js';

const project = (project_id, status, updated_at, version = 1) => ({
  project_id,
  display_name: project_id,
  status,
  updated_at,
  created_at: updated_at,
  version
});

test('follows the newest simulation currently being generated', () => {
  const older = project('older', 'updating', '2026-08-01T10:00:00Z');
  const newer = project('newer', 'updating', '2026-08-01T10:01:00Z');

  assert.equal(
    projectToFollowDuringGeneration([older, newer], null, false)?.project_id,
    'newer'
  );
});

test('does not replace a manual simulation selection', () => {
  const building = project('building', 'updating', '2026-08-01T10:01:00Z');

  assert.equal(
    projectToFollowDuringGeneration([building], 'chosen-by-user', true),
    null
  );
});

test('refreshes a manually selected simulation when that simulation is updating', () => {
  const selected = project('selected', 'updating', '2026-08-01T10:01:00Z');
  const other = project('other', 'updating', '2026-08-01T10:02:00Z');

  assert.equal(
    projectToFollowDuringGeneration([selected, other], 'selected', true)?.project_id,
    'selected'
  );
});

test('keeps refreshing an in-progress simulation that is already selected', () => {
  const building = project('building', 'updating', '2026-08-01T10:01:00Z');

  assert.equal(
    projectToFollowDuringGeneration([building], 'building', false)?.project_id,
    'building'
  );
});

test('completion keeps the selected simulation when that exact simulation changed', () => {
  const selected = project('selected', 'ready', '2026-08-01T10:00:00Z', 2);
  const other = project('other', 'ready', '2026-08-01T10:01:00Z');

  assert.equal(
    projectToOpenAfterGeneration(
      [selected, other],
      ['selected', 'other'],
      'selected',
      true
    )?.project_id,
    'selected'
  );
});

test('completion does not replace an unrelated manual selection', () => {
  const generated = project('generated', 'ready', '2026-08-01T10:01:00Z');

  assert.equal(
    projectToOpenAfterGeneration(
      [generated],
      ['generated'],
      'chosen-by-user',
      true
    ),
    null
  );
});

test('completion opens the newest reported simulation by default', () => {
  const older = project('older', 'ready', '2026-08-01T10:00:00Z');
  const newer = project('newer', 'ready', '2026-08-01T10:01:00Z');

  assert.equal(
    projectToOpenAfterGeneration(
      [older, newer],
      ['older', 'newer'],
      null,
      false
    )?.project_id,
    'newer'
  );
});

test('live refresh preserves a selected file in the same simulation', () => {
  assert.equal(
    selectedFileAfterProjectRefresh(
      'devs_project/models/Server.py',
      'restaurant',
      'restaurant',
      ['README.md', 'devs_project/models/Server.py']
    ),
    'devs_project/models/Server.py'
  );
});

test('live refresh clears a selected file that disappeared', () => {
  assert.equal(
    selectedFileAfterProjectRefresh(
      'devs_project/models/Server.py',
      'restaurant',
      'restaurant',
      ['README.md', 'devs_project/models/Queue.py']
    ),
    null
  );
});

test('live refresh clears selection when switching simulations', () => {
  assert.equal(
    selectedFileAfterProjectRefresh(
      'devs_project/models/Server.py',
      'restaurant-v1',
      'restaurant-v2',
      ['devs_project/models/Server.py']
    ),
    null
  );
});

test('live refresh accepts an equivalent normalized file path', () => {
  assert.equal(
    selectedFileAfterProjectRefresh(
      '.\\devs_project\\models\\Server.py',
      'restaurant',
      'restaurant',
      ['devs_project/models/Server.py']
    ),
    'devs_project/models/Server.py'
  );
});

test('newly followed simulation opens Files but routine refresh keeps the chosen tab', () => {
  assert.equal(shouldFocusFilesForProjectRefresh(null, 'restaurant'), true);
  assert.equal(shouldFocusFilesForProjectRefresh('older', 'restaurant'), true);
  assert.equal(shouldFocusFilesForProjectRefresh('restaurant', 'restaurant'), false);
});

test('validation polling of the selected simulation does not pull Run back to Files', () => {
  // Starting a simulation run temporarily maps validation state to the public
  // project status `updating`. Focus is based on identity, not that status.
  const validating = project('restaurant', 'updating', '2026-08-01T10:02:00Z', 3);

  assert.equal(
    projectToFollowDuringGeneration([validating], 'restaurant', false)?.project_id,
    'restaurant'
  );
  assert.equal(
    shouldFocusFilesForProjectRefresh('restaurant', validating.project_id),
    false
  );
});

test('maps a live activity file to the most specific loaded-tree path', () => {
  assert.equal(
    resolveActivityPreviewPath(
      'restaurant_queue/devs_project/models/Server.py',
      ['Server.py', 'models/Server.py', 'README.md']
    ),
    'models/Server.py'
  );
});

test('keeps the session-relative path before a simulation tree exists', () => {
  assert.equal(
    resolveActivityPreviewPath('restaurant_queue/devs_project/Queue.py', []),
    'restaurant_queue/devs_project/Queue.py'
  );
});

test('loads the project-wide activity snapshot and preserves the clicked file', () => {
  const state = activityPreviewFileState({
    path: 'restaurant_queue/devs_project/models/Server.py',
    content: 'class Server: pass\n',
    root_path: 'restaurant_queue',
    selected_path: 'devs_project/models/Server.py',
    files: {
      'README.md': '# Restaurant queue\n',
      'devs_project/models/Queue.py': 'class Queue: pass\n',
      'devs_project/models/Server.py': 'class Server: pass\n'
    }
  });

  assert.deepEqual(Object.keys(state.files), [
    'README.md',
    'devs_project/models/Queue.py',
    'devs_project/models/Server.py'
  ]);
  assert.equal(state.selectedPath, 'devs_project/models/Server.py');
});
