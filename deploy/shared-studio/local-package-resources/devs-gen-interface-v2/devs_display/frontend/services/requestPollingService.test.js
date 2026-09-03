import assert from 'node:assert/strict';
import test from 'node:test';

import { shouldRefreshRequestFromActivity } from './requestPollingService.js';

const response = (requestStatus, events = []) => ({
  request_status: requestStatus,
  events
});

test('refreshes request details as soon as activity reports a review wait', () => {
  assert.equal(shouldRefreshRequestFromActivity(
    { request_id: 'request-1', status: 'running' },
    response('waiting_for_user'),
    'request-1'
  ), true);
});

test('refreshes on an interaction event even if the status snapshot is stale', () => {
  assert.equal(shouldRefreshRequestFromActivity(
    { request_id: 'request-1', status: 'running' },
    response('running', [{ request_id: 'request-1', type: 'interaction_required' }]),
    'request-1'
  ), true);
});

test('does not refetch an unchanged request for ordinary activity', () => {
  assert.equal(shouldRefreshRequestFromActivity(
    { request_id: 'request-1', status: 'running' },
    response('running', [{ request_id: 'request-1', type: 'activity' }]),
    'request-1'
  ), false);
});

test('ignores an interaction event from a different request', () => {
  assert.equal(shouldRefreshRequestFromActivity(
    { request_id: 'request-1', status: 'running' },
    response('running', [{ request_id: 'request-2', type: 'interaction_required' }]),
    'request-1'
  ), false);
});

test('refreshes when activity starts before local request details are restored', () => {
  assert.equal(shouldRefreshRequestFromActivity(
    null,
    response('waiting_for_user'),
    'request-1'
  ), true);
});
