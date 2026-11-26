import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleDot,
  Pause,
  Play,
  Radio,
  RotateCcw,
  Route,
} from 'lucide-react';

import {
  BehaviorEvent,
  BehaviorEventGroup,
  BehaviorReplaySeekRequest,
  BehaviorStateChange,
  BehaviorTrace,
} from '../behaviorReplayTypes';
import { GraphLink, GraphNode } from '../types';
import {
  buildBehaviorReplaySteps,
  compactSimulationTime,
  groupIndexForEvent,
  hasOnlyTimeZeroObservations,
  mapBehaviorGroupToGraph,
} from '../services/behaviorReplayService.js';
import { GraphVisualizer } from './GraphVisualizer';

export interface BehaviorReplayProps {
  nodes: GraphNode[];
  links: GraphLink[];
  trace: BehaviorTrace;
  className?: string;
  graphClassName?: string;
  selectedNodeId?: string | null;
  seekRequest?: BehaviorReplaySeekRequest | null;
  failureMessage?: string | null;
  active?: boolean;
  onNodeSelect?: (node: GraphNode) => void;
  onStepChange?: (stepIndex: number, group: BehaviorEventGroup | null) => void;
}

const SPEEDS = [0.5, 1, 2, 4] as const;
const NOOP = () => {};

const displayValue = (value: unknown, maximum = 180): string => {
  if (value === undefined) return 'not recorded';
  if (value === null) return 'null';
  let text: string;
  if (typeof value === 'string') text = value;
  else if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint') text = String(value);
  else {
    try { text = JSON.stringify(value); } catch { text = String(value); }
  }
  return text.length <= maximum ? text : `${text.slice(0, maximum)}…`;
};

const controlDescription = (control: BehaviorStateChange['afterControl']): string => {
  const phase = control.phase === undefined ? 'phase not recorded' : `phase ${String(control.phase)}`;
  const sigma = control.sigmaInfinite === true
    ? 'σ = ∞'
    : control.sigma === undefined || control.sigma === null ? 'σ not recorded' : `σ = ${String(control.sigma)}`;
  return `${phase} · ${sigma}`;
};

const eventDescription = (event: BehaviorEvent): string => {
  if (event.recordKind === 'state') {
    const state = event.payload && typeof event.payload === 'object'
      ? event.payload as Record<string, unknown>
      : {};
    return controlDescription({
      phase: state.phase,
      sigma: state.sigma,
      sigmaInfinite: state.sigma_infinite === true,
    });
  }
  return `${event.direction || 'port'} ${event.port}`;
};

const traceNotice = (trace: BehaviorTrace): string | null => {
  if (trace.truncated || trace.hiddenEvents > 0 || (trace.droppedEvents || 0) > 0 || (trace.droppedStates || 0) > 0) {
    return 'This is a bounded replay. Some later event or state evidence may be omitted.';
  }
  if (!trace.complete) return 'The trace has no closing summary, so this replay may stop early.';
  if (trace.malformedLines > 0) return `${trace.malformedLines} malformed trace ${trace.malformedLines === 1 ? 'line was' : 'lines were'} skipped.`;
  if (hasOnlyTimeZeroObservations(trace)) {
    return 'This run recorded only time-zero initialization. The model did not advance to a later simulation time, so there are no later steps to replay.';
  }
  return null;
};

const StateEvidence: React.FC<{ change: BehaviorStateChange | null }> = ({ change }) => {
  if (!change) {
    return <p className="mt-1 text-[11px] text-slate-500">No post-transition state was recorded for this recipient in this step.</p>;
  }
  if (change.initial) {
    return (
      <div className="mt-1 text-[11px] text-slate-600">
        <span className="font-medium text-emerald-700">Initial state recorded.</span> {controlDescription(change.afterControl)}
        {change.projectionAvailable && <div className="mt-1 font-mono text-slate-500">{displayValue(change.afterProjection)}</div>}
      </div>
    );
  }
  if (!change.projectionAvailable) {
    return (
      <div className="mt-1 text-[11px] text-slate-500">
        <div>{controlDescription(change.afterControl)}</div>
        <div className="mt-1">No detailed state projection was recorded for this component.</div>
      </div>
    );
  }
  if (!change.previousProjectionAvailable) {
    return (
      <div className="mt-1 text-[11px] text-slate-500">
        <div>{controlDescription(change.afterControl)}</div>
        <div className="mt-1"><span className="font-medium text-slate-600">Current projection:</span> <span className="font-mono">{displayValue(change.afterProjection)}</span></div>
        <div className="mt-1">An earlier projection was not recorded, so no before/after comparison is shown.</div>
      </div>
    );
  }
  return (
    <div className="mt-1.5">
      <div className="text-[11px] text-slate-500">{controlDescription(change.afterControl)}</div>
      {change.fields.length > 0 ? (
        <dl className="mt-1 grid gap-1">
          {change.fields.map(field => (
            <div key={field.path} className="grid min-w-0 grid-cols-[minmax(0,0.8fr)_minmax(0,1.4fr)] items-baseline gap-2 rounded bg-white px-2 py-1 text-[11px]">
              <dt className="truncate font-medium text-slate-700" title={field.path}>{field.path}</dt>
              <dd className="min-w-0 font-mono text-slate-600" title={`${displayValue(field.before, 1000)} → ${displayValue(field.after, 1000)}`}>
                <span className="break-all">{displayValue(field.before, 72)}</span>
                <ArrowRight size={11} className="mx-1 inline text-slate-400" />
                <span className="break-all font-semibold text-emerald-700">{displayValue(field.after, 72)}</span>
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p className="mt-1 text-[11px] text-slate-500">No projected field changed from the previous recorded state.</p>
      )}
    </div>
  );
};

export const BehaviorReplay: React.FC<BehaviorReplayProps> = ({
  nodes,
  links,
  trace,
  className = '',
  graphClassName = 'h-[360px] sm:h-[430px]',
  selectedNodeId = null,
  seekRequest = null,
  failureMessage = null,
  active = true,
  onNodeSelect,
  onStepChange,
}) => {
  const replayNodes = useMemo(() => nodes.map(node => (
    node.type === 'coupled' && (node.children?.length || 0) > 0
      ? { ...node, expanded: true }
      : node
  )), [nodes]);
  const replaySteps = useMemo(() => buildBehaviorReplaySteps(trace, nodes, links), [links, nodes, trace]);
  const groups = replaySteps.map(step => step.group);
  const [stepIndex, setStepIndex] = useState(0);
  const [selectedEmissionIndex, setSelectedEmissionIndex] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<(typeof SPEEDS)[number]>(1);
  const onStepChangeRef = useRef(onStepChange);
  const lastStepIndex = Math.max(0, groups.length - 1);
  const currentStep = replaySteps[stepIndex] || null;
  const currentGroup = currentStep?.group || null;

  useEffect(() => { onStepChangeRef.current = onStepChange; }, [onStepChange]);
  useEffect(() => { setStepIndex(0); setPlaying(false); }, [trace]);
  useEffect(() => { setSelectedEmissionIndex(null); }, [currentGroup?.id]);
  useEffect(() => {
    if (!seekRequest) return;
    const requestedStep = groupIndexForEvent(groups, seekRequest.eventIndex);
    if (requestedStep >= 0) { setStepIndex(requestedStep); setPlaying(false); }
  }, [groups, seekRequest]);
  useEffect(() => { onStepChangeRef.current?.(stepIndex, currentGroup); }, [currentGroup, stepIndex]);
  useEffect(() => { if (!active) setPlaying(false); }, [active]);
  useEffect(() => {
    if (!playing || groups.length === 0) return;
    if (stepIndex >= lastStepIndex) { setPlaying(false); return; }
    const timer = window.setTimeout(() => {
      setStepIndex(current => Math.min(lastStepIndex, current + 1));
    }, Math.round(1000 / speed));
    return () => window.clearTimeout(timer);
  }, [groups.length, lastStepIndex, playing, speed, stepIndex]);

  const mapping = currentStep?.mapping || mapBehaviorGroupToGraph(null, nodes, links);
  const visibleEmissions = currentStep
    ? selectedEmissionIndex === null
      ? currentStep.emissions
      : currentStep.emissions.filter(item => item.event.index === selectedEmissionIndex)
    : [];
  const visibleRecipientIds = [...new Set(visibleEmissions.flatMap(item => item.destinations.map(destination => destination.nodeId)))];
  const visibleSourceIds = [...new Set(visibleEmissions.map(item => item.mapping.nodeId).filter((id): id is string => Boolean(id)))];
  const visibleStateChanges = (currentStep?.stateChanges || []).filter(change => (
    selectedEmissionIndex === null
    || (change.nodeId !== null && visibleRecipientIds.includes(change.nodeId))
  ));
  const overlayPorts = visibleEmissions.flatMap(item => item.mapping.activePorts || []);
  const overlayLinks = [...new Set(visibleEmissions.flatMap(item => item.mapping.linkIds || []))];
  const stateNodeIds = [...new Set(visibleStateChanges.filter(change => !change.initial && change.nodeId).map(change => change.nodeId as string))];
  const allDestinations = [...new Map(
    visibleEmissions.flatMap(item => item.destinations).map(destination => [`${destination.nodeId}\u0000${destination.portName}`, destination])
  ).values()];
  const otherStateChanges = (currentStep?.stateChanges || []).filter(change => (
    !change.initial && (!change.nodeId || !allDestinations.some(destination => destination.nodeId === change.nodeId))
  ));
  const isLastRecordedFailureStep = Boolean(failureMessage && stepIndex === lastStepIndex);
  const notice = traceNotice(trace);
  const currentTime = currentGroup ? compactSimulationTime(currentGroup.simulationTime) : '—';
  const activityLabel = currentGroup
    ? `Behavior at simulation time ${currentTime}; ${currentGroup.events.length} recorded observations`
    : 'DEVS model structure; no behavior observations are available';

  const setStep = (next: number) => {
    setStepIndex(Math.max(0, Math.min(lastStepIndex, next)));
    setPlaying(false);
  };
  const togglePlaying = () => {
    if (groups.length === 0) return;
    if (!playing && stepIndex >= lastStepIndex) setStepIndex(0);
    setPlaying(value => !value);
  };

  return (
    <section className={`overflow-hidden rounded-xl border border-slate-200 bg-white ${className}`} aria-label="Behavior replay">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2 font-semibold text-slate-800">
            <Route size={17} className="text-purple-600" /> Behavior replay
            {trace.partial && <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">Partial trace</span>}
          </div>
          <p className="mt-0.5 text-xs text-slate-500">Follow recorded emissions, configured recipients, and observed state changes.</p>
        </div>
        <div className="text-right text-xs text-slate-500">
          {groups.length > 0 ? <>Step {stepIndex + 1} of {groups.length} · <span className="font-mono font-semibold text-slate-700">t={currentTime}</span></> : 'No recorded behavior'}
        </div>
      </header>

      {notice && (
        <div className="flex items-start gap-2 border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-800" role="status">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" /> {notice}
        </div>
      )}

      <div className={graphClassName}>
        <GraphVisualizer
          nodes={replayNodes}
          links={links}
          physicsEnabled={false}
          selectedNodeId={selectedNodeId}
          onExpand={NOOP}
          onCollapse={NOOP}
          onToggleFixed={NOOP}
          onNodeMove={NOOP}
          onNodeSelect={onNodeSelect}
          readOnly
          activityOverlay={{
            activeNodeIds: [],
            sourceNodeIds: visibleSourceIds,
            recipientNodeIds: visibleRecipientIds,
            stateNodeIds,
            activeLinkIds: overlayLinks,
            activePorts: overlayPorts,
            dimInactive: Boolean(currentGroup),
            tone: 'active',
            ariaLabel: activityLabel,
          }}
        />
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-slate-200 bg-white px-4 py-2 text-[10px] text-slate-600" aria-label="Behavior replay legend">
        <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full bg-purple-500 ring-2 ring-purple-200" />Observed emitter</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-0.5 w-5 bg-purple-500" />Route inferred from model structure</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full border-2 border-dashed border-blue-600" />Configured recipient</span>
        <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-full bg-emerald-500 ring-2 ring-emerald-200" />State observed after transition</span>
      </div>

      <div className="border-t border-slate-200 bg-slate-50 px-3 py-3">
        <div className="flex flex-wrap items-center gap-2" aria-label="Replay controls">
          <button type="button" onClick={() => setStep(0)} disabled={groups.length === 0 || stepIndex === 0} className="rounded border border-slate-200 bg-white p-2 text-slate-600 hover:bg-slate-100 disabled:opacity-40" aria-label="Restart replay" title="Restart"><RotateCcw size={15} /></button>
          <button type="button" onClick={() => setStep(stepIndex - 1)} disabled={groups.length === 0 || stepIndex === 0} className="rounded border border-slate-200 bg-white p-2 text-slate-600 hover:bg-slate-100 disabled:opacity-40" aria-label="Previous replay step" title="Previous replay step"><ChevronLeft size={16} /></button>
          <button type="button" onClick={togglePlaying} disabled={groups.length === 0} className="flex min-w-24 items-center justify-center gap-1.5 rounded bg-purple-700 px-3 py-2 text-xs font-semibold text-white hover:bg-purple-800 disabled:opacity-40" aria-label={playing ? 'Pause replay' : 'Play replay'}>{playing ? <Pause size={15} /> : <Play size={15} />}{playing ? 'Pause' : 'Play'}</button>
          <button type="button" onClick={() => setStep(stepIndex + 1)} disabled={groups.length === 0 || stepIndex >= lastStepIndex} className="rounded border border-slate-200 bg-white p-2 text-slate-600 hover:bg-slate-100 disabled:opacity-40" aria-label="Next replay step" title="Next replay step"><ChevronRight size={16} /></button>
          <label className="ml-auto flex items-center gap-2 text-xs text-slate-500">Speed
            <select value={speed} onChange={event => setSpeed(Number(event.target.value) as (typeof SPEEDS)[number])} className="rounded border border-slate-200 bg-white px-2 py-1.5 font-medium text-slate-700" aria-label="Replay speed" title="Steps advance at a fixed teaching pace, not wall-clock simulation time">
              {SPEEDS.map(value => <option key={value} value={value}>{value}×</option>)}
            </select>
          </label>
        </div>
        <label className="mt-3 block">
          <span className="sr-only">Replay step</span>
          <input type="range" min={0} max={lastStepIndex} step={1} value={Math.min(stepIndex, lastStepIndex)} onChange={event => setStep(Number(event.target.value))} disabled={groups.length <= 1} className="w-full accent-purple-700 disabled:opacity-40" aria-valuetext={currentGroup ? `Simulation time ${currentTime}, step ${stepIndex + 1} of ${groups.length}` : 'No recorded behavior'} />
        </label>
      </div>

      <div className="border-t border-slate-200 px-4 py-3" aria-live={playing ? 'off' : 'polite'}>
        {currentStep ? (
          <>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="text-xs font-semibold text-slate-700"><span className="font-mono">t={currentTime}</span>{currentGroup?.observationCycle !== null ? ` · replay cycle ${currentGroup?.observationCycle}` : ''}</div>
              {(mapping.unmappedEvents > 0 || mapping.unmappedRoutes > 0) && <span className="text-[11px] text-amber-700">Some recorded behavior could not be matched to the visible structure.</span>}
            </div>

            {currentStep.emissions.length > 0 ? (
              <section className="mt-2" aria-label="Events emitted in this replay step">
                <div className="flex flex-wrap items-center gap-2">
                  <h4 className="text-xs font-semibold text-slate-800">Events emitted</h4>
                  <span className="text-[11px] text-slate-500">Observed from output ports</span>
                  {currentStep.emissions.length > 1 && (
                    <button type="button" onClick={() => setSelectedEmissionIndex(null)} className={`ml-auto rounded px-2 py-1 text-[10px] font-semibold ${selectedEmissionIndex === null ? 'bg-purple-100 text-purple-800' : 'bg-slate-100 text-slate-600'}`}>All events</button>
                  )}
                </div>
                <div className="mt-1.5 grid gap-1.5">
                  {currentStep.emissions.map(({ event, mapping: eventMapping, destinations }) => {
                    const selected = selectedEmissionIndex === event.index;
                    return (
                      <button key={event.id} type="button" onClick={() => setSelectedEmissionIndex(selected ? null : event.index)} className={`min-w-0 rounded border px-3 py-2 text-left transition ${selected ? 'border-purple-400 bg-purple-50' : 'border-slate-200 bg-slate-50 hover:border-purple-200 hover:bg-purple-50/40'}`} aria-pressed={selected}>
                        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs">
                          <Radio size={14} className="shrink-0 text-purple-600" />
                          <span className="truncate font-semibold text-slate-800" title={event.component}>{event.component}</span>
                          <span className="font-mono font-semibold text-purple-700">{event.port}</span>
                          <span className="rounded-full bg-purple-100 px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-purple-700">Observed output</span>
                        </div>
                        <div className="mt-1 break-all font-mono text-[11px] text-slate-600">{displayValue(event.payload)}</div>
                        <div className="mt-1.5 grid gap-1">
                          {destinations.length > 0 ? destinations.map(destination => (
                            <div key={`${destination.nodeId}-${destination.portName}`} className="flex min-w-0 items-center gap-1.5 text-[11px] text-slate-600">
                              <ArrowRight size={12} className="shrink-0 text-purple-500" />
                              <span className="truncate font-medium text-blue-700" title={`${destination.component}.${destination.portName}`}>{destination.component}</span>
                              <span className="font-mono">{destination.portName}</span>
                              <span className="ml-auto shrink-0 rounded-full bg-blue-50 px-1.5 py-0.5 text-[9px] font-medium text-blue-700">Configured route</span>
                            </div>
                          )) : (
                            <div className="text-[11px] text-slate-500">{eventMapping.status === 'observed_no_route' ? 'No internal recipient is configured for this output.' : 'A configured recipient could not be resolved from the visible structure.'}</div>
                          )}
                        </div>
                      </button>
                    );
                  })}
                </div>
                {currentStep.emissions.length > 1 && (
                  <p className="mt-1.5 text-[10px] text-slate-500">Several outputs occurred in this replay cycle. State updates below belong to the whole cycle and are not attributed to one output unless you select its route.</p>
                )}
              </section>
            ) : (
              <div className="mt-2 rounded bg-slate-50 px-3 py-2 text-xs text-slate-600">No output event was recorded in this step; the recorder captured state initialization or an internal transition.</div>
            )}

            <section className="mt-3" aria-label="Configured recipients and observed state changes">
              <div className="flex flex-wrap items-baseline gap-2">
                <h4 className="text-xs font-semibold text-slate-800">Recipients and state</h4>
                <span className="text-[11px] text-slate-500">Routing is inferred from wiring; state is recorded after transition.</span>
              </div>
              {allDestinations.length > 0 ? (
                <div className="mt-1.5 grid gap-1.5 sm:grid-cols-2">
                  {allDestinations.map(destination => {
                    const change = (currentStep.stateChanges || []).find(item => item.nodeId === destination.nodeId) || null;
                    return (
                      <article key={`${destination.nodeId}-${destination.portName}`} className="rounded border border-slate-200 bg-slate-50 px-3 py-2">
                        <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs">
                          {change ? <CheckCircle2 size={14} className="shrink-0 text-emerald-600" /> : <CircleDot size={14} className="shrink-0 text-blue-600" />}
                          <span className="truncate font-semibold text-slate-800" title={destination.component}>{destination.component}</span>
                          <span className="font-mono text-[10px] text-blue-700">{destination.portName}</span>
                          <span className={`ml-auto rounded-full px-1.5 py-0.5 text-[9px] font-semibold ${change ? 'bg-emerald-50 text-emerald-700' : 'bg-blue-50 text-blue-700'}`}>{change ? 'State observed' : 'Configured recipient'}</span>
                        </div>
                        <StateEvidence change={change} />
                      </article>
                    );
                  })}
                </div>
              ) : currentStep.stateChanges.length > 0 ? (
                <div className="mt-1.5 grid gap-1.5 sm:grid-cols-2">
                  {currentStep.stateChanges.map(change => (
                    <article key={change.eventIndex} className="rounded border border-slate-200 bg-slate-50 px-3 py-2">
                      <div className="flex items-center gap-1.5 text-xs"><CheckCircle2 size={14} className="text-emerald-600" /><span className="font-semibold text-slate-800">{change.component}</span></div>
                      <StateEvidence change={change} />
                    </article>
                  ))}
                </div>
              ) : (
                <p className="mt-1.5 rounded bg-slate-50 px-3 py-2 text-xs text-slate-500">No recipient or state evidence was recorded in this step.</p>
              )}
            </section>

            {otherStateChanges.length > 0 && allDestinations.length > 0 && (
              <details className="mt-2 rounded border border-slate-200 bg-white px-3 py-2">
                <summary className="cursor-pointer text-[11px] font-medium text-slate-600">Other components with recorded state updates ({otherStateChanges.length})</summary>
                <div className="mt-2 grid gap-1.5 sm:grid-cols-2">
                  {otherStateChanges.map(change => <div key={change.eventIndex} className="rounded bg-slate-50 px-2 py-1.5 text-xs"><span className="font-semibold text-slate-700">{change.component}</span><StateEvidence change={change} /></div>)}
                </div>
              </details>
            )}

            <details className="mt-2 rounded border border-slate-200 bg-white px-3 py-2">
              <summary className="cursor-pointer text-[11px] font-medium text-slate-600">Recorded evidence ({currentGroup?.events.length || 0})</summary>
              <ol className="mt-2 max-h-36 space-y-1.5 overflow-y-auto" aria-label={`Recorded evidence at simulation time ${currentTime}`}>
                {(currentGroup?.events || []).map(event => (
                  <li key={event.id} className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-2 rounded bg-slate-50 px-2.5 py-2 text-xs">
                    <CircleDot size={14} className="mt-0.5 text-slate-500" />
                    <div className="min-w-0"><div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5"><span className="truncate font-semibold text-slate-800" title={event.component}>{event.component}</span><span className="font-mono text-[11px] text-slate-500">{eventDescription(event)}</span></div><div className="mt-0.5 truncate font-mono text-[11px] text-slate-500" title={event.value}>{event.value}</div></div>
                  </li>
                ))}
              </ol>
            </details>

            {isLastRecordedFailureStep && failureMessage && <div className="mt-2 rounded border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800" role="alert"><span className="font-semibold">Last recorded behavior.</span> The run ended afterward: {failureMessage}</div>}
          </>
        ) : (
          <div className="py-4 text-center text-sm text-slate-500">This run did not record behavior that can be replayed.</div>
        )}
      </div>
    </section>
  );
};
