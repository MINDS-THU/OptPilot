import assert from 'node:assert/strict';
import test from 'node:test';

import { hierarchyDepthById } from './graphHierarchyService.js';

test('derives nested depth from parent links for unqualified component ids', () => {
  const depths = hierarchyDepthById([
    { id: 'Restaurant', parent: null },
    { id: 'DiningArea', parent: 'Restaurant' },
    { id: 'Server', parent: 'DiningArea' },
    { id: 'Queue', parent: 'DiningArea' }
  ]);

  assert.equal(depths.get('Restaurant'), 0);
  assert.equal(depths.get('DiningArea'), 1);
  assert.equal(depths.get('Server'), 2);
  assert.equal(depths.get('Queue'), 2);
});

test('does not infer hierarchy from slashes embedded in opaque ids', () => {
  const depths = hierarchyDepthById([
    { id: 'course/project/root', parent: null },
    { id: 'atomic', parent: 'course/project/root' }
  ]);

  assert.equal(depths.get('course/project/root'), 0);
  assert.equal(depths.get('atomic'), 1);
});

test('keeps malformed missing-parent and cyclic graphs bounded', () => {
  const depths = hierarchyDepthById([
    { id: 'Orphan', parent: 'Missing' },
    { id: 'A', parent: 'B' },
    { id: 'B', parent: 'A' }
  ]);

  assert.equal(depths.get('Orphan'), 0);
  assert.ok(Number.isFinite(depths.get('A')));
  assert.ok(Number.isFinite(depths.get('B')));
});
