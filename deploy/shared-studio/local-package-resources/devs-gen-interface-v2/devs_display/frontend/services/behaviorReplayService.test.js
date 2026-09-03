import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildBehaviorReplaySteps,
  diffStateProjections,
  groupBehaviorEvents,
  groupIndexForEvent,
  hasOnlyTimeZeroObservations,
  mapBehaviorGroupToGraph,
  parseBehaviorTrace,
  resolveBehaviorEventNode,
} from './behaviorReplayService.js';

const nodes = [
  { id: 'root', name: 'RestaurantSim', className: 'RestaurantSim', parent: null, ports: { inputs: [], outputs: [] } },
  { id: 'root/customer', name: 'customer_gen', className: 'CustomerGenerator', parent: 'root', ports: { inputs: [], outputs: ['out'] } },
  { id: 'root/queue', name: 'queue', className: 'Queue', parent: 'root', ports: { inputs: ['in'], outputs: ['ready'] } },
];

const links = [
  { id: 'customer-to-queue', source: 'root/customer', sourcePort: 'out', target: 'root/queue', targetPort: 'in' },
];

test('parses direction, payload, footer evidence, and groups simultaneous events', () => {
  const trace = parseBehaviorTrace([
    JSON.stringify({ record_type: 'event', sequence: 1, simulation_time: 1, component: 'RestaurantSim.customer_gen', port: 'out', direction: 'output', value: { id: 1 } }),
    JSON.stringify({ record_type: 'event', sequence: 2, simulation_time: 1.0, component: 'RestaurantSim.queue', port: 'in', direction: 'input', value: { id: 1 } }),
    JSON.stringify({ record_type: 'event', sequence: 3, simulation_time: 2, component: 'RestaurantSim.queue', port: 'ready', direction: 'output', value: true }),
    JSON.stringify({ record_type: 'summary', recorded_events: 3, dropped_events: 0, truncated: false }),
  ].join('\n'));

  assert.equal(trace.events.length, 3);
  assert.equal(trace.groups.length, 2);
  assert.equal(trace.groups[0].events.length, 2);
  assert.equal(trace.events[0].direction, 'output');
  assert.deepEqual(trace.events[0].payload, { id: 1 });
  assert.equal(trace.complete, true);
  assert.equal(trace.partial, false);
  assert.equal(groupIndexForEvent(trace.groups, 2), 1);
});

test('parses v2 headers, canonical component ids, and state observations', () => {
  const trace = parseBehaviorTrace([
    JSON.stringify({ record_type: 'header', schema_version: 'devs.event-trace.v2', capabilities: ['transition-control-state'] }),
    JSON.stringify({ record_type: 'event', record_sequence: 1, simulation_time: 1, observation_cycle: 4, component_id: 'root/customer', component: 'OtherRoot.customer_gen', port: 'out', event_kind: 'output', value: 7 }),
    JSON.stringify({ record_type: 'state', record_sequence: 2, simulation_time: 1, observation_cycle: 4, observation: 'post_transition', component_id: 'root/queue', component: 'OtherRoot.queue', phase: 'busy', sigma: 2, sigma_infinite: false, domain_state: { waiting: 3 } }),
    JSON.stringify({ record_type: 'summary', recorded_events: 1, recorded_states: 1, dropped_events: 0, dropped_states: 0, truncated: false }),
  ].join('\n'));

  assert.equal(trace.events.length, 2);
  assert.equal(trace.groups.length, 1);
  assert.equal(trace.events[0].componentId, 'root/customer');
  assert.equal(trace.events[0].direction, 'output');
  assert.equal(trace.events[1].recordKind, 'state');
  assert.equal(trace.events[1].observationCycle, 4);
  assert.equal(trace.events[1].observation, 'post_transition');
  assert.deepEqual(trace.events[1].payload, { phase: 'busy', sigma: 2, sigma_infinite: false, domain_state: { waiting: 3 } });
  assert.equal(trace.recordedStates, 1);
  assert.equal(trace.droppedStates, 0);
});

test('keeps nonconsecutive repeated times as separate deterministic steps', () => {
  const events = [
    { index: 0, simulationTime: '1', numericTime: 1 },
    { index: 1, simulationTime: '2', numericTime: 2 },
    { index: 2, simulationTime: '1', numericTime: 1 },
  ];
  const groups = groupBehaviorEvents(events);
  assert.equal(groups.length, 3);
  assert.deepEqual(groups.map(group => group.startEventIndex), [0, 1, 2]);
});

test('keeps zero-delay coordinator cycles separate at the same simulation time', () => {
  const trace = parseBehaviorTrace([
    JSON.stringify({ record_type: 'event', observation_cycle: 1, simulation_time: 0, component: 'Root.source', port: 'out', value: 1 }),
    JSON.stringify({ record_type: 'state', observation_cycle: 1, simulation_time: 0, component: 'Root.source', phase: 'next' }),
    JSON.stringify({ record_type: 'event', observation_cycle: 2, simulation_time: 0, component: 'Root.source', port: 'out', value: 2 }),
    JSON.stringify({ record_type: 'summary', recorded_events: 2, recorded_states: 1 }),
  ].join('\n'));

  assert.equal(trace.groups.length, 2);
  assert.deepEqual(trace.groups.map(group => group.observationCycle), [1, 2]);
  assert.deepEqual(trace.groups.map(group => group.events.length), [2, 1]);
  assert.equal(hasOnlyTimeZeroObservations(trace), true);
});

test('does not describe a replay as initialization-only after time advances', () => {
  const trace = parseBehaviorTrace([
    JSON.stringify({ record_type: 'state', observation_cycle: 0, simulation_time: 0, component: 'Root.source', phase: 'ready' }),
    JSON.stringify({ record_type: 'event', observation_cycle: 1, simulation_time: 0.25, component: 'Root.source', port: 'out', value: 1 }),
    JSON.stringify({ record_type: 'summary', recorded_events: 1, recorded_states: 1 }),
  ].join('\n'));

  assert.equal(hasOnlyTimeZeroObservations(trace), false);
});

test('reports malformed, hidden, and incomplete evidence as partial', () => {
  const trace = parseBehaviorTrace([
    '{bad json',
    JSON.stringify({ simulation_time: 1, component: 'a', port: 'out' }),
    JSON.stringify({ simulation_time: 2, component: 'a', port: 'out' }),
  ].join('\n'), { maxEvents: 1 });

  assert.equal(trace.malformedLines, 1);
  assert.equal(trace.hiddenEvents, 1);
  assert.equal(trace.complete, false);
  assert.equal(trace.partial, true);
});

test('resolves a dotted trace path through graph hierarchy', () => {
  const node = resolveBehaviorEventNode({ component: 'RestaurantSim.customer_gen' }, nodes);
  assert.equal(node?.id, 'root/customer');
});

test('does not guess from a leaf name or class name', () => {
  const node = resolveBehaviorEventNode({ component: 'CustomerGenerator' }, nodes);
  assert.equal(node, null);
});

test('uses an exact canonical component id before the display path', () => {
  const node = resolveBehaviorEventNode({
    componentId: 'root/queue',
    component: 'A.different.display.path',
  }, nodes);
  assert.equal(node?.id, 'root/queue');
});

test('maps an output event to exact ports, coupling, and target component', () => {
  const group = {
    events: [{ index: 0, component: 'RestaurantSim.customer_gen', port: 'out', direction: 'output' }]
  };
  const mapping = mapBehaviorGroupToGraph(group, nodes, links);

  assert.deepEqual(mapping.activeLinkIds, ['customer-to-queue']);
  assert.deepEqual(mapping.activeNodeIds, ['root/customer']);
  assert.deepEqual(mapping.activePorts, [
    { nodeId: 'root/customer', direction: 'output', portName: 'out' },
    { nodeId: 'root/queue', direction: 'input', portName: 'in' },
  ]);
  assert.equal(mapping.unmappedEvents, 0);
  assert.equal(mapping.unmappedRoutes, 0);
  assert.deepEqual(mapping.recipientNodeIds, ['root/queue']);
  assert.deepEqual(mapping.eventMappings[0].destinations, [
    { nodeId: 'root/queue', component: 'queue', portName: 'in', evidence: 'inferred_from_structure' },
  ]);
});

test('infers direction only when a declared port is unambiguous', () => {
  const mapping = mapBehaviorGroupToGraph({
    events: [{ index: 0, component: 'RestaurantSim.customer_gen', port: 'out', direction: null }]
  }, nodes, links);

  assert.equal(mapping.eventMappings[0].direction, 'output');
  assert.equal(mapping.eventMappings[0].directionInferred, true);
  assert.deepEqual(mapping.activeLinkIds, ['customer-to-queue']);
});

test('treats a declared terminal output as observed without inventing a route', () => {
  const mapping = mapBehaviorGroupToGraph({
    events: [{ index: 0, component: 'RestaurantSim.queue', port: 'ready', direction: 'output' }]
  }, nodes, links);

  assert.deepEqual(mapping.activeNodeIds, ['root/queue']);
  assert.deepEqual(mapping.activeLinkIds, []);
  assert.equal(mapping.unmappedRoutes, 0);
  assert.equal(mapping.eventMappings[0].status, 'observed_no_route');
});

test('maps repeated simultaneous events across the same coupling independently', () => {
  const mapping = mapBehaviorGroupToGraph({ events: [
    { index: 0, recordKind: 'event', componentId: 'root/customer', component: 'RestaurantSim.customer_gen', port: 'out', direction: 'output' },
    { index: 1, recordKind: 'event', componentId: 'root/customer', component: 'RestaurantSim.customer_gen', port: 'out', direction: 'output' },
  ] }, nodes, links);

  assert.deepEqual(mapping.activeLinkIds, ['customer-to-queue']);
  assert.deepEqual(mapping.eventMappings.map(item => item.linkIds), [
    ['customer-to-queue'],
    ['customer-to-queue'],
  ]);
  assert.deepEqual(mapping.eventMappings.map(item => item.status), ['mapped', 'mapped']);
});

test('does not add a missing graph endpoint as an active node', () => {
  const brokenLinks = [
    { id: 'missing-target', source: 'root/customer', sourcePort: 'out', target: 'not-loaded', targetPort: 'in' },
  ];
  const mapping = mapBehaviorGroupToGraph({ events: [{
    index: 0,
    recordKind: 'event',
    componentId: 'root/customer',
    component: 'RestaurantSim.customer_gen',
    port: 'out',
    direction: 'output',
  }] }, nodes, brokenLinks);

  assert.deepEqual(mapping.activeNodeIds, ['root/customer']);
  assert.deepEqual(mapping.activeLinkIds, ['missing-target']);
  assert.equal(mapping.unmappedRoutes, 1);
  assert.equal(mapping.eventMappings[0].status, 'unmapped_route');
});

test('maps state observations to a component without inventing a port route', () => {
  const mapping = mapBehaviorGroupToGraph({
    events: [{ index: 0, recordKind: 'state', componentId: 'root/queue', component: 'RestaurantSim.queue', port: '—', direction: null }]
  }, nodes, links);

  assert.deepEqual(mapping.activeNodeIds, ['root/queue']);
  assert.deepEqual(mapping.activeLinkIds, []);
  assert.equal(mapping.unmappedRoutes, 0);
  assert.equal(mapping.eventMappings[0].status, 'mapped_state');
});

test('traverses nested EOC boundary couplings with endpoint-specific directions', () => {
  const nestedNodes = [
    { id: 'root', name: 'Root', className: 'Root', type: 'coupled', parent: null, ports: { inputs: [], outputs: ['external'] } },
    { id: 'root/group', name: 'group', className: 'Group', type: 'coupled', parent: 'root', ports: { inputs: [], outputs: ['out'] } },
    { id: 'root/group/source', name: 'source', className: 'Source', type: 'atomic', parent: 'root/group', ports: { inputs: [], outputs: ['emit'] } },
  ];
  const nestedLinks = [
    { id: 'inner-eoc', source: 'root/group/source', sourcePort: 'emit', target: 'root/group', targetPort: 'out', couplingType: 'EOC' },
    { id: 'outer-eoc', source: 'root/group', sourcePort: 'out', target: 'root', targetPort: 'external', couplingType: 'EOC' },
  ];
  const mapping = mapBehaviorGroupToGraph({ events: [{
    index: 0,
    recordKind: 'event',
    componentId: 'root/group/source',
    component: 'Root.group.source',
    port: 'emit',
    direction: 'output',
  }] }, nestedNodes, nestedLinks);

  assert.deepEqual(mapping.activeLinkIds, ['inner-eoc', 'outer-eoc']);
  assert.deepEqual(mapping.activePorts, [
    { nodeId: 'root/group/source', direction: 'output', portName: 'emit' },
    { nodeId: 'root/group', direction: 'output', portName: 'out' },
    { nodeId: 'root', direction: 'output', portName: 'external' },
  ]);
});

test('traverses a coupled input boundary but stops at atomic inputs', () => {
  const nestedNodes = [
    { id: 'root', name: 'Root', className: 'Root', type: 'coupled', parent: null, ports: { inputs: [], outputs: [] } },
    { id: 'root/source', name: 'source', className: 'Source', type: 'atomic', parent: 'root', ports: { inputs: [], outputs: ['out'] } },
    { id: 'root/group', name: 'group', className: 'Group', type: 'coupled', parent: 'root', ports: { inputs: ['in'], outputs: [] } },
    { id: 'root/group/sink', name: 'sink', className: 'Sink', type: 'atomic', parent: 'root/group', ports: { inputs: ['accept'], outputs: [] } },
  ];
  const nestedLinks = [
    { id: 'to-boundary', source: 'root/source', sourcePort: 'out', target: 'root/group', targetPort: 'in', couplingType: 'IC' },
    { id: 'inner-eic', source: 'root/group', sourcePort: 'in', target: 'root/group/sink', targetPort: 'accept', couplingType: 'EIC' },
  ];
  const mapping = mapBehaviorGroupToGraph({ events: [{
    index: 0,
    recordKind: 'event',
    componentId: 'root/source',
    component: 'Root.source',
    port: 'out',
    direction: 'output',
  }] }, nestedNodes, nestedLinks);

  assert.deepEqual(mapping.activeLinkIds, ['to-boundary', 'inner-eic']);
  assert.deepEqual(mapping.activePorts, [
    { nodeId: 'root/source', direction: 'output', portName: 'out' },
    { nodeId: 'root/group', direction: 'input', portName: 'in' },
    { nodeId: 'root/group/sink', direction: 'input', portName: 'accept' },
  ]);
});

test('highlights exact fan-out couplings and all declared endpoint ports', () => {
  const fanoutNodes = [
    { id: 'root', name: 'Root', className: 'Root', type: 'coupled', parent: null, ports: { inputs: [], outputs: [] } },
    { id: 'root/source', name: 'source', className: 'Source', type: 'atomic', parent: 'root', ports: { inputs: [], outputs: ['out'] } },
    { id: 'root/a', name: 'a', className: 'SinkA', type: 'atomic', parent: 'root', ports: { inputs: ['in'], outputs: [] } },
    { id: 'root/b', name: 'b', className: 'SinkB', type: 'atomic', parent: 'root', ports: { inputs: ['receive'], outputs: [] } },
  ];
  const fanoutLinks = [
    { id: 'to-a', source: 'root/source', sourcePort: 'out', target: 'root/a', targetPort: 'in' },
    { id: 'to-b', source: 'root/source', sourcePort: 'out', target: 'root/b', targetPort: 'receive' },
  ];
  const mapping = mapBehaviorGroupToGraph({ events: [{
    index: 0,
    recordKind: 'event',
    componentId: 'root/source',
    component: 'Root.source',
    port: 'out',
    direction: 'output',
  }] }, fanoutNodes, fanoutLinks);

  assert.deepEqual(mapping.activeLinkIds, ['to-a', 'to-b']);
  assert.deepEqual(mapping.activeNodeIds, ['root/source']);
  assert.deepEqual(mapping.recipientNodeIds, ['root/a', 'root/b']);
  assert.equal(mapping.unmappedRoutes, 0);
});

test('derives configured recipients and before/after projected state without inventing input events', () => {
  const trace = parseBehaviorTrace([
    JSON.stringify({ record_type: 'state', observation_cycle: 0, observation: 'initialized', simulation_time: 0, component_id: 'root/queue', component: 'RestaurantSim.queue', phase: 'idle', sigma_infinite: true, domain_state: { waiting: 0, served: 0 } }),
    JSON.stringify({ record_type: 'event', observation_cycle: 1, simulation_time: 1, component_id: 'root/customer', component: 'RestaurantSim.customer_gen', port: 'out', event_kind: 'output', value: { id: 7 } }),
    JSON.stringify({ record_type: 'state', observation_cycle: 1, observation: 'post_transition', simulation_time: 1, component_id: 'root/queue', component: 'RestaurantSim.queue', phase: 'busy', sigma: 2, sigma_infinite: false, domain_state: { waiting: 1, served: 0 } }),
    JSON.stringify({ record_type: 'summary', recorded_events: 1, recorded_states: 2 }),
  ].join('\n'));

  const steps = buildBehaviorReplaySteps(trace, nodes, links);
  assert.equal(steps.length, 2);
  assert.deepEqual(steps[1].emissions[0].destinations, [
    { nodeId: 'root/queue', component: 'queue', portName: 'in', evidence: 'inferred_from_structure' },
  ]);
  assert.equal(steps[1].stateChanges[0].component, 'queue');
  assert.deepEqual(steps[1].stateChanges[0].fields, [
    { path: 'waiting', before: 0, after: 1, kind: 'changed' },
  ]);
});

test('state projection diffs preserve falsey values and bound nested changes', () => {
  assert.deepEqual(diffStateProjections(
    { busy: false, count: 0, current: null, nested: { queue: 2 } },
    { busy: true, count: 0, current: 'A', nested: { queue: 1 } },
  ), [
    { path: 'busy', before: false, after: true, kind: 'changed' },
    { path: 'current', before: null, after: 'A', kind: 'changed' },
    { path: 'nested.queue', before: 2, after: 1, kind: 'changed' },
  ]);
});

test('missing projections remain explicit instead of fabricating a comparison', () => {
  const trace = parseBehaviorTrace([
    JSON.stringify({ record_type: 'state', observation_cycle: 0, observation: 'initialized', simulation_time: 0, component_id: 'root/queue', component: 'RestaurantSim.queue', phase: 'idle', sigma_infinite: true }),
    JSON.stringify({ record_type: 'state', observation_cycle: 1, observation: 'post_transition', simulation_time: 1, component_id: 'root/queue', component: 'RestaurantSim.queue', phase: 'busy', sigma: 2 }),
    JSON.stringify({ record_type: 'summary', recorded_events: 0, recorded_states: 2 }),
  ].join('\n'));
  const change = buildBehaviorReplaySteps(trace, nodes, links)[1].stateChanges[0];
  assert.equal(change.projectionAvailable, false);
  assert.equal(change.previousProjectionAvailable, false);
  assert.deepEqual(change.fields, []);
});
