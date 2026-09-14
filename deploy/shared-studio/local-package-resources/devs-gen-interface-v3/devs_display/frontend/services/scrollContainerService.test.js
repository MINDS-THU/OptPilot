import assert from 'node:assert/strict';
import test from 'node:test';

import {
  focusWithoutDocumentScroll,
  scrollContainerToBottom
} from './scrollContainerService.js';

test('conversation autoscroll targets only its owning pane', () => {
  const calls = [];
  const pane = {
    scrollHeight: 842,
    scrollTo(options) {
      calls.push(options);
    }
  };

  assert.equal(scrollContainerToBottom(pane), true);
  assert.deepEqual(calls, [{ top: 842, behavior: 'smooth' }]);
  assert.equal(scrollContainerToBottom(null), false);
});

test('review controls receive focus without scrolling their ancestors', () => {
  const calls = [];
  const control = {
    focus(options) {
      calls.push(options);
    }
  };

  assert.equal(focusWithoutDocumentScroll(control), true);
  assert.deepEqual(calls, [{ preventScroll: true }]);
  assert.equal(focusWithoutDocumentScroll(undefined), false);
});
