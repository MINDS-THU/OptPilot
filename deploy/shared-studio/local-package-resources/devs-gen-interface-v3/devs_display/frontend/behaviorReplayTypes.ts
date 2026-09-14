export type BehaviorDirection = 'input' | 'output' | null;
export type BehaviorRecordKind = 'event' | 'state';

export interface BehaviorEvent {
  id: string;
  index: number;
  recordKind: BehaviorRecordKind;
  sequence: string;
  simulationTime: string;
  numericTime: number | null;
  observationCycle: number | null;
  observation: string | null;
  componentId: string | null;
  component: string;
  port: string;
  direction: BehaviorDirection;
  payload: unknown;
  value: string;
  raw: Record<string, unknown>;
}

export interface BehaviorEventGroup {
  id: string;
  key: string;
  simulationTime: string;
  numericTime: number | null;
  observationCycle: number | null;
  startEventIndex: number;
  endEventIndex: number;
  events: BehaviorEvent[];
}

export interface BehaviorReplaySeekRequest {
  eventIndex: number;
  requestId: number;
}

export interface BehaviorTrace {
  events: BehaviorEvent[];
  groups: BehaviorEventGroup[];
  recordedEvents?: number;
  droppedEvents?: number;
  recordedStates?: number;
  droppedStates?: number;
  complete: boolean;
  truncated: boolean;
  malformedLines: number;
  hiddenEvents: number;
  partial: boolean;
}

export type BehaviorMappingStatus =
  | 'mapped'
  | 'mapped_state'
  | 'observed_no_route'
  | 'unmapped_component'
  | 'unmapped_port'
  | 'unmapped_route';

export interface BehaviorEventMapping {
  eventIndex: number;
  nodeId: string | null;
  linkIds: string[];
  status: BehaviorMappingStatus;
  direction?: BehaviorDirection;
  directionInferred?: boolean;
  activePorts?: Array<{
    nodeId: string;
    direction: 'input' | 'output';
    portName: string;
  }>;
  destinations?: BehaviorRouteDestination[];
}

export interface BehaviorRouteDestination {
  nodeId: string;
  component: string;
  portName: string;
  evidence: 'inferred_from_structure';
}

export interface BehaviorStateFieldChange {
  path: string;
  before?: unknown;
  after?: unknown;
  kind: 'added' | 'removed' | 'changed';
}

export interface BehaviorStateChange {
  eventIndex: number;
  nodeId: string | null;
  component: string;
  observation: string | null;
  initial: boolean;
  projectionAvailable: boolean;
  previousProjectionAvailable: boolean;
  fields: BehaviorStateFieldChange[];
  beforeProjection?: unknown;
  afterProjection?: unknown;
  beforeControl?: { phase?: unknown; sigma?: unknown; sigmaInfinite?: boolean };
  afterControl: { phase?: unknown; sigma?: unknown; sigmaInfinite?: boolean };
}

export interface BehaviorEmission {
  event: BehaviorEvent;
  mapping: BehaviorEventMapping;
  destinations: BehaviorRouteDestination[];
}

export interface BehaviorReplayStep {
  group: BehaviorEventGroup;
  mapping: BehaviorGraphMapping;
  emissions: BehaviorEmission[];
  stateChanges: BehaviorStateChange[];
  recipientNodeIds: string[];
}

export interface BehaviorGraphMapping {
  activeNodeIds: string[];
  sourceNodeIds: string[];
  recipientNodeIds: string[];
  stateNodeIds: string[];
  activeLinkIds: string[];
  activePorts: Array<{
    nodeId: string;
    direction: 'input' | 'output';
    portName: string;
  }>;
  eventMappings: BehaviorEventMapping[];
  mappedEvents: number;
  unmappedEvents: number;
  unmappedRoutes: number;
}
