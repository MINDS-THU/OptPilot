import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isCurrentSessionRequest,
  isCurrentSessionScope,
  requestForSession
} from './sessionScopeService.js';

test('rejects a late request response from the session selected before a switch', () => {
  const oldVisit = { sessionId: 'session-old', revision: 4 };
  const currentVisit = { sessionId: 'session-new', revision: 5 };
  const lateOldResponse = { request_id: 'request-old', session_id: 'session-old' };

  assert.equal(isCurrentSessionScope(oldVisit, currentVisit), false);
  assert.equal(isCurrentSessionRequest(lateOldResponse, oldVisit, currentVisit), false);
  assert.equal(requestForSession(lateOldResponse, currentVisit.sessionId), null);
});

test('rejects an earlier visit even after returning to the same session', () => {
  const firstVisit = { sessionId: 'session-a', revision: 2 };
  const secondVisit = { sessionId: 'session-a', revision: 4 };
  const lateFirstResponse = { request_id: 'request-old', session_id: 'session-a' };

  assert.equal(isCurrentSessionRequest(lateFirstResponse, firstVisit, secondVisit), false);
  assert.equal(isCurrentSessionRequest(
    { request_id: 'request-current', session_id: 'session-a' },
    secondVisit,
    secondVisit
  ), true);
});
