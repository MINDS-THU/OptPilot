import assert from 'node:assert/strict';
import test from 'node:test';

import {
  architectureOnlyGraph,
  canRefreshStructure,
  orderedStructureNodes,
  rootStructureNode,
  structureLifecyclePresentation,
  structureNodeRelations
} from './structureLifecycleService.js';

test('proposal graph keeps hierarchy but removes speculative ports and couplings', () => {
  const graph = architectureOnlyGraph({
    root_model: 'System',
    nodes: [
      { id: 'System', parent: null, ports: { inputs: ['in'], outputs: ['out'] } },
      { id: 'Queue', parent: 'System', ports: { inputs: ['arrive'], outputs: ['leave'] } }
    ],
    links: [{ source: 'System', target: 'Queue' }]
  });

  assert.deepEqual(graph.links, []);
  assert.deepEqual(graph.nodes.map(node => node.ports), [
    { inputs: [], outputs: [] },
    { inputs: [], outputs: [] }
  ]);
  assert.equal(graph.nodes[1].parent, 'System');
});

test('lifecycle copy distinguishes review, revision, build, terminal loading, and implemented states', () => {
  assert.equal(structureLifecyclePresentation('awaiting_review', false).label, 'Proposed architecture');
  assert.match(structureLifecyclePresentation('awaiting_review', false).scope, /intentionally not shown/);
  assert.equal(structureLifecyclePresentation('revising', false).label, 'Previous proposal · Revision in progress');
  assert.match(structureLifecyclePresentation('revising', false).scope, /no longer awaiting approval/);
  assert.equal(structureLifecyclePresentation('approved_building', true).label, 'Approved architecture · Building');
  assert.match(structureLifecyclePresentation('approved_building', true).scope, /parsed from source/);
  assert.equal(structureLifecyclePresentation('finalizing', true).label, 'Approved architecture · Loading implemented model');
  assert.match(structureLifecyclePresentation('finalizing', true).scope, /loaded successfully/);
  assert.equal(structureLifecyclePresentation('build_stopped', true).label, 'Approved architecture · Build stopped');
  assert.match(structureLifecyclePresentation('build_stopped', true).scope, /retained for diagnosis/);
  assert.equal(structureLifecyclePresentation(null, true).label, 'Implemented model');
  assert.equal(structureLifecyclePresentation(null, false), null);
});

test('final graph refresh remains available during the terminal handoff', () => {
  assert.equal(canRefreshStructure('finalizing'), true);
  assert.equal(canRefreshStructure('implemented'), true);
  assert.equal(canRefreshStructure('building'), false);
  assert.equal(canRefreshStructure('stopped'), false);
  assert.equal(canRefreshStructure(null), false);
});

test('root selection and component details use explicit parent links', () => {
  const nodes = [
    { id: 'worker', name: 'Worker', parent: 'system' },
    { id: 'system', name: 'System', parent: null },
    { id: 'queue', name: 'Queue', parent: 'system' }
  ];
  assert.equal(rootStructureNode(nodes, 'system').id, 'system');
  assert.deepEqual(orderedStructureNodes(nodes, 'system').map(item => [item.node.id, item.depth]), [
    ['system', 0], ['worker', 1], ['queue', 1]
  ]);
  assert.deepEqual(structureNodeRelations(nodes, 'system').children.map(node => node.id), ['worker', 'queue']);
});
