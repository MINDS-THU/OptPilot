import assert from 'node:assert/strict';
import test from 'node:test';

import { deriveActivityTimelineStates } from './activityStateService.js';

const activity = (activityKey, state, title = activityKey) => ({
  activityKey,
  state,
  title
});

test('checks an earlier implicit step after later activity begins', () => {
  const result = deriveActivityTimelineStates([
    activity('understand_request', 'started', 'Understanding your request'),
    activity('agent_inspect_files', 'progress', 'Inspecting simulation files'),
    activity('plan_structure', 'started', 'Planning the model structure')
  ]);

  assert.equal(result[0].state, 'completed');
  assert.equal(result[1].state, 'completed');
  assert.equal(result[2].state, 'started');
});

test('keeps a lifecycle stage active across nested activity', () => {
  const result = deriveActivityTimelineStates([
    activity('build_simulation', 'started'),
    activity('plan_structure', 'started')
  ]);

  assert.equal(result[0].state, 'started');
  assert.equal(result[1].state, 'started');
});

test('checks an older update when the same stage advances', () => {
  const result = deriveActivityTimelineStates([
    activity('generate_components', 'started'),
    activity('generate_components', 'progress')
  ]);

  assert.equal(result[0].state, 'completed');
  assert.equal(result[1].state, 'progress');
});

test('preserves explicit completed and failed states', () => {
  const result = deriveActivityTimelineStates([
    activity('plan_structure', 'completed'),
    activity('verify_model', 'failed'),
    activity('package_simulation', 'started')
  ]);

  assert.equal(result[0].state, 'completed');
  assert.equal(result[1].state, 'failed');
  assert.equal(result[2].state, 'started');
});
