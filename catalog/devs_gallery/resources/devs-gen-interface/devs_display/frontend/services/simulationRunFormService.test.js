import assert from 'node:assert/strict';
import test from 'node:test';

import {
  initializeScenarioValues,
  missingRequiredParameters,
  resetSuggestedValues,
  suggestedValuesChanged
} from './simulationRunFormService.js';

const parameters = [
  { name: 'horizon', type: 'integer', default: 30 },
  { name: 'seed', type: 'integer', default: 7 },
  { name: 'event_file', type: 'string', required: true }
];

test('uses only declared runner defaults and leaves required user input empty', () => {
  assert.deepEqual(initializeScenarioValues(parameters), {
    horizon: 30,
    seed: 7,
    event_file: ''
  });
});

test('preserves edits when refreshed runner metadata describes the same fields', () => {
  assert.deepEqual(initializeScenarioValues(parameters, {
    horizon: '90',
    seed: 11,
    event_file: 'arrivals.csv'
  }), {
    horizon: '90',
    seed: 11,
    event_file: 'arrivals.csv'
  });
});

test('reset restores suggestions without erasing required user-owned values', () => {
  const edited = { horizon: '90', seed: 11, event_file: 'arrivals.csv' };

  assert.equal(suggestedValuesChanged(parameters, edited), true);
  assert.deepEqual(resetSuggestedValues(parameters, edited), {
    horizon: 30,
    seed: 7,
    event_file: 'arrivals.csv'
  });
  assert.equal(suggestedValuesChanged(parameters, resetSuggestedValues(parameters, edited)), false);
});

test('does not mark a numeric suggestion changed only because an input stores text', () => {
  assert.equal(suggestedValuesChanged(parameters, {
    horizon: '30',
    seed: '7',
    event_file: ''
  }), false);
});

test('identifies only required fields that still need student input', () => {
  assert.deepEqual(
    missingRequiredParameters(parameters, initializeScenarioValues(parameters)).map(parameter => parameter.name),
    ['event_file']
  );
  assert.deepEqual(missingRequiredParameters(parameters, {
    horizon: 30,
    seed: 7,
    event_file: 'arrivals.csv'
  }), []);
});
