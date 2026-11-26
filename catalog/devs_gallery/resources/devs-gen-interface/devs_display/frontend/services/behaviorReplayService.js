const DEFAULT_MAX_EVENTS = 1000;

const valueFrom = (record, names) => {
  for (const name of names) {
    if (record[name] !== undefined && record[name] !== null) return record[name];
  }
  return undefined;
};

const finiteCount = value => (
  typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : undefined
);

const displayValue = value => {
  if (value === undefined || value === null) return '—';
  if (typeof value === 'string') return value || '—';
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

const normalizeDirection = value => {
  const normalized = String(value || '').trim().toLowerCase();
  if (['out', 'output', 'send', 'sent', 'emit', 'emitted'].includes(normalized)) return 'output';
  if (['in', 'input', 'receive', 'received'].includes(normalized)) return 'input';
  return null;
};

export const compactSimulationTime = value => {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value || '—');
  if (Number.isInteger(numeric)) return String(numeric);
  return numeric.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
};

/**
 * Report the narrow case where a completed trace never advances beyond its
 * initialization instant. This is evidence about the trace, not a claim that
 * every time-zero-only model is invalid; callers use it to explain why a
 * replay has no later behavioral steps.
 */
export const hasOnlyTimeZeroObservations = trace => {
  const observedTimes = (trace?.events || [])
    .map(event => event.numericTime)
    .filter(value => typeof value === 'number' && Number.isFinite(value));
  return Boolean(
    trace?.complete
    && observedTimes.length > 0
    && observedTimes.every(value => value === 0)
  );
};

/**
 * Parse the bounded JSON-lines trace produced by generated simulations.
 * The parser deliberately retains direction and raw payload information so a
 * replay can distinguish observed behavior from a visually inferred route.
 */
export const parseBehaviorTrace = (content, options = {}) => {
  const maxEvents = Number.isFinite(options.maxEvents)
    ? Math.max(0, Math.floor(options.maxEvents))
    : DEFAULT_MAX_EVENTS;
  const allEvents = [];
  let malformedLines = 0;
  let recordedEvents;
  let droppedEvents;
  let recordedStates;
  let droppedStates;
  let complete = false;
  let truncated = false;

  String(content || '').split(/\r?\n/).forEach((line, lineIndex) => {
    const trimmed = line.trim();
    if (!trimmed) return;

    let parsed;
    try {
      parsed = JSON.parse(trimmed);
    } catch {
      malformedLines += 1;
      return;
    }
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      malformedLines += 1;
      return;
    }

    const record = parsed;
    const recordType = String(valueFrom(record, ['record_type', 'type', 'kind']) || '').toLowerCase();
    const looksLikeSummary = recordType === 'summary'
      || recordType === 'footer'
      || record.truncated !== undefined
      || record.dropped_events !== undefined
      || record.recorded_events !== undefined;

    if (looksLikeSummary && recordType !== 'event') {
      complete = record.complete !== false;
      recordedEvents = finiteCount(valueFrom(record, ['recorded_events', 'event_count', 'count'])) ?? recordedEvents;
      droppedEvents = finiteCount(valueFrom(record, ['dropped_events', 'omitted_events', 'dropped', 'omitted'])) ?? droppedEvents;
      recordedStates = finiteCount(valueFrom(record, ['recorded_states', 'state_count'])) ?? recordedStates;
      droppedStates = finiteCount(valueFrom(record, ['dropped_states', 'omitted_states'])) ?? droppedStates;
      truncated = record.truncated === true
        || (droppedEvents !== undefined && droppedEvents > 0)
        || (droppedStates !== undefined && droppedStates > 0)
        || truncated;
      return;
    }

    // v2 headers describe capabilities but are not behavioral observations.
    if (recordType === 'header' || recordType === 'metadata' || recordType === 'meta') return;

    const recordKind = recordType === 'state' ? 'state' : 'event';

    const simulationTimeValue = valueFrom(record, [
      'simulation_time', 'sim_time', '_sim_time', 'time', 'timestamp'
    ]);
    const simulationTime = displayValue(simulationTimeValue);
    const numericTime = Number(simulationTimeValue);
    const observationCycle = finiteCount(valueFrom(record, [
      'observation_cycle', 'cycle', 'microstep'
    ]));
    const componentIdValue = valueFrom(record, ['component_id', 'model_id']);
    const component = displayValue(valueFrom(record, [
      'component', 'model_path', '_model_path', 'component_path', 'model', 'model_name'
    ]));
    const port = recordKind === 'state' ? '—' : displayValue(valueFrom(record, ['port', 'port_name']));
    const direction = recordKind === 'state'
      ? null
      : normalizeDirection(valueFrom(record, ['direction', 'event_direction', 'port_direction', 'event_kind']));
    let payload = recordKind === 'state'
      ? {
          phase: valueFrom(record, ['phase']),
          sigma: valueFrom(record, ['sigma']),
          sigma_infinite: valueFrom(record, ['sigma_infinite']),
          ...(record.domain_state === undefined ? {} : { domain_state: record.domain_state }),
        }
      : valueFrom(record, ['value', 'event', 'payload', 'data', 'message']);
    if (payload === undefined) {
      const contextualKeys = new Set([
        'schema_version', 'record_type', 'type', 'kind', 'sequence', 'index',
        'simulation_time', 'sim_time', '_sim_time', 'time', 'timestamp',
        'component_id', 'model_id', 'component', 'model_path', '_model_path', 'component_path', 'model', 'model_name',
        'port', 'port_name', 'direction', 'event_direction', 'port_direction', 'event_kind'
      ]);
      const remaining = Object.fromEntries(
        Object.entries(record).filter(([key]) => !contextualKeys.has(key))
      );
      if (Object.keys(remaining).length > 0) payload = remaining;
    }

    const eventIndex = allEvents.length;
    allEvents.push({
      id: `event-${eventIndex}-${lineIndex}`,
      index: eventIndex,
      recordKind,
      sequence: displayValue(valueFrom(record, ['record_sequence', 'sequence', 'index']) ?? eventIndex + 1),
      simulationTime,
      numericTime: Number.isFinite(numericTime) ? numericTime : null,
      observationCycle: observationCycle ?? null,
      observation: recordKind === 'state'
        ? String(valueFrom(record, ['observation']) || '') || null
        : null,
      componentId: componentIdValue === undefined ? null : String(componentIdValue),
      component,
      port,
      direction,
      payload,
      value: displayValue(payload),
      raw: record,
    });
  });

  const events = allEvents.slice(0, maxEvents);
  const groups = groupBehaviorEvents(events);
  const hiddenEvents = allEvents.length - events.length;
  return {
    events,
    groups,
    recordedEvents,
    droppedEvents,
    recordedStates,
    droppedStates,
    complete,
    truncated,
    malformedLines,
    hiddenEvents,
    partial: !complete || truncated || hiddenEvents > 0 || malformedLines > 0,
  };
};

const timeGroupKey = event => (
  event.observationCycle !== null && event.observationCycle !== undefined
    ? `cycle:${event.observationCycle}`
    : event.numericTime !== null
    ? `number:${event.numericTime}`
    : `text:${event.simulationTime}`
);

/**
 * Group one coordinator observation cycle.  Older traces have no cycle id, so
 * they retain the previous consecutive-simulation-time behavior.
 */
export const groupBehaviorEvents = events => {
  const groups = [];
  for (const event of events || []) {
    const key = timeGroupKey(event);
    const current = groups[groups.length - 1];
    if (current && current.key === key) {
      current.events.push(event);
      current.endEventIndex = event.index;
      continue;
    }
    groups.push({
      id: `step-${groups.length}-${event.index}`,
      key,
      simulationTime: event.simulationTime,
      numericTime: event.numericTime,
      observationCycle: event.observationCycle ?? null,
      startEventIndex: event.index,
      endEventIndex: event.index,
      events: [event],
    });
  }
  return groups;
};

const normalizeComponentPath = value => String(value || '')
  .trim()
  .replace(/::/g, '/')
  .replace(/[.\\/]+/g, '/')
  .replace(/^\/+|\/+$/g, '');

const nodeHierarchyPaths = nodes => {
  const nodeById = new Map(nodes.map(node => [String(node.id), node]));
  const paths = new Map();

  const visit = (node, seen = new Set()) => {
    if (paths.has(node.id)) return paths.get(node.id);
    if (seen.has(node.id)) return normalizeComponentPath(node.name || node.id);
    const nextSeen = new Set(seen);
    nextSeen.add(node.id);
    const parent = node.parent ? nodeById.get(String(node.parent)) : null;
    const ownName = normalizeComponentPath(node.name || node.id);
    const path = parent ? `${visit(parent, nextSeen)}/${ownName}` : ownName;
    paths.set(node.id, path);
    return path;
  };

  nodes.forEach(node => visit(node));
  return paths;
};

const uniqueMatch = matches => matches.length === 1 ? matches[0] : null;

export const resolveBehaviorEventNode = (event, nodes) => {
  const canonicalId = normalizeComponentPath(event?.componentId);
  if (canonicalId) {
    const exactCanonical = uniqueMatch(nodes.filter(node => normalizeComponentPath(node.id) === canonicalId));
    if (exactCanonical) return exactCanonical;
  }

  const component = normalizeComponentPath(event?.component);
  if (!component || component === '—') return null;
  const hierarchyPaths = nodeHierarchyPaths(nodes);

  const exactPath = uniqueMatch(nodes.filter(node => hierarchyPaths.get(node.id) === component));
  if (exactPath) return exactPath;

  const relativeComponent = component.split('/').slice(1).join('/');
  if (!relativeComponent) return null;
  return uniqueMatch(nodes.filter(node => {
    const path = hierarchyPaths.get(node.id) || '';
    return path.split('/').slice(1).join('/') === relativeComponent;
  }));
};

const endpointId = endpoint => String(endpoint?.id ?? endpoint ?? '');

const inferDirectionFromNode = (event, node) => {
  if (event.direction) return { direction: event.direction, inferred: false };
  const inPort = (node.ports?.inputs || []).includes(event.port);
  const outPort = (node.ports?.outputs || []).includes(event.port);
  if (inPort !== outPort) return { direction: inPort ? 'input' : 'output', inferred: true };
  return { direction: null, inferred: false };
};

const portDirection = (node, portName) => {
  const input = (node?.ports?.inputs || []).includes(portName);
  const output = (node?.ports?.outputs || []).includes(portName);
  if (input === output) return null;
  return input ? 'input' : 'output';
};

const addPort = (ports, nodeId, direction, portName) => {
  if (!nodeId || !direction || !portName || portName === '—') return;
  const key = `${nodeId}\u0000${direction}\u0000${portName}`;
  if (!ports.has(key)) ports.set(key, { nodeId, direction, portName });
};

/**
 * Resolve one simultaneous-time group against actual graph identifiers and
 * couplings. Missing or ambiguous evidence is reported instead of guessed.
 */
export const mapBehaviorGroupToGraph = (group, nodes, links) => {
  const nodeById = new Map((nodes || []).map(node => [String(node.id), node]));
  const activeNodeIds = new Set();
  const activeLinkIds = new Set();
  const activePorts = new Map();
  const sourceNodeIds = new Set();
  const recipientNodeIds = new Set();
  const stateNodeIds = new Set();
  const eventMappings = [];
  let unmappedEvents = 0;
  let unmappedRoutes = 0;

  for (const event of group?.events || []) {
    const node = resolveBehaviorEventNode(event, nodes || []);
    if (!node) {
      unmappedEvents += 1;
      eventMappings.push({ eventIndex: event.index, nodeId: null, linkIds: [], status: 'unmapped_component' });
      continue;
    }

    activeNodeIds.add(node.id);
    if (event.recordKind === 'state') {
      stateNodeIds.add(node.id);
      eventMappings.push({
        eventIndex: event.index,
        nodeId: node.id,
        linkIds: [],
        status: 'mapped_state',
        direction: null,
        directionInferred: false,
      });
      continue;
    }

    sourceNodeIds.add(node.id);

    const { direction, inferred } = inferDirectionFromNode(event, node);
    const declaredPorts = direction === 'input' ? node.ports?.inputs || [] : direction === 'output' ? node.ports?.outputs || [] : [];
    const portDeclared = Boolean(direction && declaredPorts.includes(event.port));
    if (portDeclared) addPort(activePorts, node.id, direction, event.port);

    const matchingLinks = [];
    const eventLinkIds = new Set();
    const eventPorts = new Map();
    const destinations = new Map();
    if (portDeclared) addPort(eventPorts, node.id, direction, event.port);
    let routeIncomplete = false;
    if (portDeclared) {
      const queue = [{ nodeId: String(node.id), direction, portName: event.port }];
      const visited = new Set();
      while (queue.length > 0) {
        const endpoint = queue.shift();
        const endpointKey = `${endpoint.nodeId}\u0000${endpoint.direction}\u0000${endpoint.portName}`;
        if (visited.has(endpointKey)) continue;
        visited.add(endpointKey);

        for (const link of links || []) {
          const sourceId = endpointId(link.source);
          const sourceNode = nodeById.get(sourceId);
          const sourceDirection = portDirection(sourceNode, link.sourcePort);
          if (
            sourceId !== endpoint.nodeId
            || link.sourcePort !== endpoint.portName
            || sourceDirection !== endpoint.direction
          ) continue;

          if (!eventLinkIds.has(link.id)) matchingLinks.push(link);
          eventLinkIds.add(link.id);
          activeLinkIds.add(link.id);
          const targetId = endpointId(link.target);
          const targetNode = nodeById.get(targetId);
          const targetDirection = portDirection(targetNode, link.targetPort);
          addPort(activePorts, sourceId, sourceDirection, link.sourcePort);
          addPort(eventPorts, sourceId, sourceDirection, link.sourcePort);
          if (targetNode) {
            addPort(activePorts, targetId, targetDirection, link.targetPort);
            addPort(eventPorts, targetId, targetDirection, link.targetPort);
          }
          if (!targetNode || !targetDirection) routeIncomplete = true;

          // Only a coupled-model boundary forwards an observed event through
          // another declared coupling. Atomic input handling is internal state,
          // not another graph edge that the trace proves occurred.
          if (targetNode?.type === 'coupled' && targetDirection) {
            queue.push({ nodeId: targetId, direction: targetDirection, portName: link.targetPort });
          } else if (targetNode && targetDirection === 'input') {
            const destinationKey = `${targetId}\u0000${link.targetPort}`;
            destinations.set(destinationKey, {
              nodeId: targetId,
              component: targetNode.name || targetId,
              portName: link.targetPort,
              evidence: 'inferred_from_structure',
            });
            recipientNodeIds.add(targetId);
          }
        }
      }
    }

    if (!portDeclared || routeIncomplete) unmappedRoutes += 1;
    const mappingStatus = !portDeclared
      ? 'unmapped_port'
      : routeIncomplete
        ? 'unmapped_route'
        : matchingLinks.length === 0
          ? 'observed_no_route'
          : 'mapped';

    eventMappings.push({
      eventIndex: event.index,
      nodeId: node.id,
      linkIds: matchingLinks.map(link => link.id),
      status: mappingStatus,
      direction,
      directionInferred: inferred,
      activePorts: [...eventPorts.values()],
      destinations: [...destinations.values()],
    });
  }

  return {
    activeNodeIds: [...activeNodeIds],
    sourceNodeIds: [...sourceNodeIds],
    recipientNodeIds: [...recipientNodeIds],
    stateNodeIds: [...stateNodeIds],
    activeLinkIds: [...activeLinkIds],
    activePorts: [...activePorts.values()],
    eventMappings,
    mappedEvents: eventMappings.length - unmappedEvents,
    unmappedEvents,
    unmappedRoutes,
  };
};

const hasOwn = (value, key) => (
  value !== null
  && typeof value === 'object'
  && Object.prototype.hasOwnProperty.call(value, key)
);

const projectionAvailable = value => (
  value !== undefined
  && !(value && typeof value === 'object' && value.unavailable === true)
);

const stableValue = value => {
  if (Array.isArray(value)) return value.map(stableValue);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.keys(value).sort().map(key => [key, stableValue(value[key])])
    );
  }
  return value;
};

const valuesEqual = (left, right) => {
  if (Object.is(left, right)) return true;
  try {
    return JSON.stringify(stableValue(left)) === JSON.stringify(stableValue(right));
  } catch {
    return false;
  }
};

const flattenProjection = (value, prefix = '', depth = 0, fields = new Map()) => {
  const plainObject = value && typeof value === 'object' && !Array.isArray(value);
  if (plainObject && depth < 2) {
    const entries = Object.entries(value);
    if (entries.length > 0) {
      entries.slice(0, 32).forEach(([key, child]) => {
        flattenProjection(child, prefix ? `${prefix}.${key}` : key, depth + 1, fields);
      });
      return fields;
    }
  }
  fields.set(prefix || 'value', value);
  return fields;
};

/** Return bounded top-level/one-level-deep changes between two projections. */
export const diffStateProjections = (before, after, maxFields = 8) => {
  if (!projectionAvailable(before) || !projectionAvailable(after)) return [];
  const beforeFields = flattenProjection(before);
  const afterFields = flattenProjection(after);
  const paths = [...new Set([...beforeFields.keys(), ...afterFields.keys()])].sort();
  const changes = [];
  for (const path of paths) {
    const hasBefore = beforeFields.has(path);
    const hasAfter = afterFields.has(path);
    const beforeValue = beforeFields.get(path);
    const afterValue = afterFields.get(path);
    if (hasBefore && hasAfter && valuesEqual(beforeValue, afterValue)) continue;
    changes.push({
      path,
      ...(hasBefore ? { before: beforeValue } : {}),
      ...(hasAfter ? { after: afterValue } : {}),
      kind: !hasBefore ? 'added' : !hasAfter ? 'removed' : 'changed',
    });
    if (changes.length >= Math.max(0, maxFields)) break;
  }
  return changes;
};

const stateSnapshot = event => {
  const payload = event?.payload && typeof event.payload === 'object'
    ? event.payload
    : {};
  return {
    control: {
      phase: payload.phase,
      sigma: payload.sigma,
      sigmaInfinite: payload.sigma_infinite === true,
    },
    domainState: hasOwn(payload, 'domain_state') ? payload.domain_state : undefined,
  };
};

const stateIdentity = (event, node) => (
  node?.id
  || normalizeComponentPath(event?.componentId)
  || normalizeComponentPath(event?.component)
  || `event:${event?.index}`
);

/**
 * Build the evidence-oriented replay model once for all steps.  Before/after
 * values are derived from recorded snapshots; routes remain explicitly
 * labelled as inferred from the implemented coupling structure.
 */
export const buildBehaviorReplaySteps = (trace, nodes, links) => {
  const previousStates = new Map();
  return (trace?.groups || []).map(group => {
    const mapping = mapBehaviorGroupToGraph(group, nodes || [], links || []);
    const mappingByEvent = new Map(mapping.eventMappings.map(item => [item.eventIndex, item]));
    const emissions = (group.events || [])
      .filter(event => event.recordKind !== 'state')
      .map(event => {
        const eventMapping = mappingByEvent.get(event.index) || {
          eventIndex: event.index,
          nodeId: null,
          linkIds: [],
          status: 'unmapped_component',
          destinations: [],
          activePorts: [],
        };
        return {
          event,
          mapping: eventMapping,
          destinations: eventMapping.destinations || [],
        };
      });

    const stateChanges = [];
    for (const event of (group.events || []).filter(item => item.recordKind === 'state')) {
      const node = resolveBehaviorEventNode(event, nodes || []);
      const identity = stateIdentity(event, node);
      const current = stateSnapshot(event);
      const previous = previousStates.get(identity);
      const afterAvailable = projectionAvailable(current.domainState);
      const beforeAvailable = Boolean(previous && projectionAvailable(previous.domainState));
      stateChanges.push({
        eventIndex: event.index,
        nodeId: node?.id || null,
        component: node?.name || event.component,
        observation: event.observation,
        initial: event.observation === 'initialized',
        projectionAvailable: afterAvailable,
        previousProjectionAvailable: beforeAvailable,
        fields: beforeAvailable && afterAvailable
          ? diffStateProjections(previous.domainState, current.domainState)
          : [],
        ...(beforeAvailable ? { beforeProjection: previous.domainState } : {}),
        ...(afterAvailable ? { afterProjection: current.domainState } : {}),
        ...(previous ? { beforeControl: previous.control } : {}),
        afterControl: current.control,
      });
      previousStates.set(identity, current);
    }

    return {
      group,
      mapping,
      emissions,
      stateChanges,
      recipientNodeIds: [...new Set(emissions.flatMap(item => item.destinations.map(destination => destination.nodeId)))],
    };
  });
};

export const groupIndexForEvent = (groups, eventIndex) => (
  (groups || []).findIndex(group => (
    eventIndex >= group.startEventIndex && eventIndex <= group.endEventIndex
  ))
);
