import React, { useEffect, useMemo, useState } from 'react';
import { AlertCircle, CheckCircle2, CircleStop, Clock3, Download, Eye, File, ListTree, Loader2, Play, RotateCcw, RotateCw, Terminal, X } from 'lucide-react';
import { downloadSimulationResultFile, getSimulationResultPreview, getSimulationRun, getSimulationSpec, startSimulationRun, stopSimulationRun } from '../services/agentService';
import {
  hasSuggestedValue,
  initializeScenarioValues,
  isMissingScenarioValue,
  missingRequiredParameters,
  resetSuggestedValues,
  suggestedValuesChanged,
} from '../services/simulationRunFormService.js';
import { SimulationParameter, SimulationResultFile, SimulationResultPreview, SimulationRun, SimulationSpec } from '../types';

interface Props {
  sessionId: string;
  simulationId: string | null;
  simulationName: string | null;
}

const terminalStatuses = new Set(['succeeded', 'completed', 'failed', 'timed_out', 'stopped', 'cancelled']);
const EVENT_TRACE_FILE = 'event_trace.jsonl';
const MAX_RENDERED_TRACE_EVENTS = 1000;

interface EventTraceRow {
  sequence: string;
  simulationTime: string;
  component: string;
  port: string;
  value: string;
}

interface ParsedEventTrace {
  rows: EventTraceRow[];
  recordedEvents?: number;
  droppedEvents?: number;
  complete: boolean;
  truncated: boolean;
  malformedLines: number;
  hiddenRows: number;
}

const recordValue = (record: Record<string, unknown>, names: string[]): unknown => {
  for (const name of names) {
    if (record[name] !== undefined && record[name] !== null) return record[name];
  }
  return undefined;
};

const displayTraceValue = (value: unknown): string => {
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

const compactSimulationTime = (value: string): string => {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return value;
  if (Number.isInteger(numeric)) return String(numeric);
  return numeric.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
};

const finiteCount = (value: unknown): number | undefined => (
  typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : undefined
);

const parseEventTrace = (content: string): ParsedEventTrace => {
  const rows: EventTraceRow[] = [];
  let malformedLines = 0;
  let recordedEvents: number | undefined;
  let droppedEvents: number | undefined;
  let complete = false;
  let truncated = false;
  let eventIndex = 0;

  content.split(/\r?\n/).forEach((line) => {
    const trimmed = line.trim();
    if (!trimmed) return;

    let parsed: unknown;
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

    const record = parsed as Record<string, unknown>;
    const recordType = String(recordValue(record, ['record_type', 'type', 'kind']) || '').toLowerCase();
    const looksLikeSummary = recordType === 'summary'
      || recordType === 'footer'
      || recordType === 'metadata'
      || recordType === 'meta'
      || record.truncated !== undefined
      || record.dropped_events !== undefined
      || record.recorded_events !== undefined;

    if (looksLikeSummary && recordType !== 'event') {
      complete = true;
      recordedEvents = finiteCount(recordValue(record, ['recorded_events', 'event_count', 'count'])) ?? recordedEvents;
      droppedEvents = finiteCount(recordValue(record, ['dropped_events', 'omitted_events', 'dropped', 'omitted'])) ?? droppedEvents;
      truncated = record.truncated === true || (droppedEvents !== undefined && droppedEvents > 0) || truncated;
      return;
    }

    const simulationTime = recordValue(record, [
      'simulation_time', 'sim_time', '_sim_time', 'time', 'timestamp'
    ]);
    const component = recordValue(record, [
      'component', 'model_path', '_model_path', 'component_path', 'model', 'model_name'
    ]);
    const port = recordValue(record, ['port', 'port_name']);
    let value = recordValue(record, ['value', 'event', 'payload', 'data', 'message']);
    if (value === undefined) {
      const contextualKeys = new Set([
        'schema_version', 'record_type', 'type', 'kind', 'sequence', 'index',
        'simulation_time', 'sim_time', '_sim_time', 'time', 'timestamp',
        'component', 'model_path', '_model_path', 'component_path', 'model', 'model_name',
        'port', 'port_name', 'direction'
      ]);
      const remaining = Object.fromEntries(
        Object.entries(record).filter(([key]) => !contextualKeys.has(key))
      );
      if (Object.keys(remaining).length > 0) value = remaining;
    }

    eventIndex += 1;
    rows.push({
      sequence: displayTraceValue(recordValue(record, ['sequence', 'index']) ?? eventIndex),
      simulationTime: displayTraceValue(simulationTime),
      component: displayTraceValue(component),
      port: displayTraceValue(port),
      value: displayTraceValue(value),
    });
  });

  const visibleRows = rows.slice(0, MAX_RENDERED_TRACE_EVENTS);
  return {
    rows: visibleRows,
    recordedEvents,
    droppedEvents,
    complete,
    truncated,
    malformedLines,
    hiddenRows: rows.length - visibleRows.length,
  };
};

const choiceValue = (
  parameter: SimulationParameter,
  value: string
): string | number | boolean => {
  if (value === '') return '';
  if (parameter.type === 'integer') return Number.parseInt(value, 10);
  if (parameter.type === 'number') return Number.parseFloat(value);
  if (parameter.type === 'boolean') return value === 'true';
  return value;
};

const statusTone = (status?: string) => {
  if (status === 'succeeded' || status === 'completed' || status === 'ready') return 'bg-emerald-50 text-emerald-700 border-emerald-200';
  if (status === 'failed' || status === 'timed_out') return 'bg-red-50 text-red-700 border-red-200';
  if (status === 'running' || status === 'queued' || status === 'validating' || status === 'finalizing' || status === 'stopping') return 'bg-blue-50 text-blue-700 border-blue-200';
  return 'bg-slate-50 text-slate-600 border-slate-200';
};

const validationStatusLabel = (status?: SimulationSpec['validation_status']): string => {
  if (status === 'ready') return 'Ready to save';
  if (status === 'validating') return 'Finishing check';
  if (status === 'failed') return 'Run failed';
  if (status === 'unverified' || status === 'stale') return 'Run needed';
  return status || '';
};

const formatFileSize = (size?: number): string => {
  if (size === undefined) return 'Ready';
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
};

const formattedPreview = (preview: SimulationResultPreview): string => {
  if (preview.media_type.includes('json') || preview.path.toLowerCase().endsWith('.json')) {
    try {
      return JSON.stringify(JSON.parse(preview.content), null, 2);
    } catch {
      // Show the original content when a text file has a JSON-like name.
    }
  }
  return preview.content;
};

export const SimulationRunPanel: React.FC<Props> = ({ sessionId, simulationId, simulationName }) => {
  const [spec, setSpec] = useState<SimulationSpec | null>(null);
  const [values, setValues] = useState<Record<string, string | number | boolean>>({});
  const [run, setRun] = useState<SimulationRun | null>(null);
  const [loadingSpec, setLoadingSpec] = useState(false);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [validationRunId, setValidationRunId] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resultPreview, setResultPreview] = useState<SimulationResultPreview | null>(null);
  const [activeResultPath, setActiveResultPath] = useState<string | null>(null);
  const [resultFileError, setResultFileError] = useState<string | null>(null);
  const [eventTrace, setEventTrace] = useState<ParsedEventTrace | null>(null);
  const [eventTraceStatus, setEventTraceStatus] = useState<'idle' | 'loading' | 'loaded' | 'failed'>('idle');
  const [eventTraceError, setEventTraceError] = useState<string | null>(null);

  const loadSpec = async () => {
    if (!sessionId || !simulationId || simulationId.startsWith('local-')) {
      setUnavailable(Boolean(simulationId));
      return;
    }
    setLoadingSpec(true);
    setUnavailable(false);
    setError(null);
    try {
      const nextSpec = await getSimulationSpec(sessionId, simulationId);
      setSpec(nextSpec);
      setValues(previous => initializeScenarioValues(nextSpec.parameters, previous));
    } catch (reason: any) {
      const missing = reason?.status === 404 || reason?.status === 405 || reason?.status === 501;
      setUnavailable(missing);
      setError(missing ? null : (reason?.message || 'Could not load the simulation runner.'));
      setSpec(null);
    } finally {
      setLoadingSpec(false);
    }
  };

  useEffect(() => {
    setRun(null);
    setValues({});
    setSpec(null);
    setUnavailable(false);
    setError(null);
    setResultPreview(null);
    setActiveResultPath(null);
    setResultFileError(null);
    setEventTrace(null);
    setEventTraceStatus('idle');
    setEventTraceError(null);
    setValidationRunId(null);
    loadSpec();
  }, [sessionId, simulationId]);

  useEffect(() => {
    if (!run || terminalStatuses.has(run.status) || !sessionId || !simulationId) return;
    const timer = window.setInterval(async () => {
      try {
        const next = await getSimulationRun(sessionId, simulationId, run.run_id);
        setRun(next);
        if (next.error && ['failed', 'timed_out'].includes(next.status)) {
          setError(next.error);
        }
        if (terminalStatuses.has(next.status) && next.run_id === validationRunId) {
          setValidationRunId(null);
          void loadSpec();
        }
      } catch (reason: any) {
        setError(reason?.message || 'Lost contact with the simulation run.');
      }
    }, 1000);
    return () => window.clearInterval(timer);
  }, [run?.run_id, run?.status, sessionId, simulationId, validationRunId]);

  const eventTraceFile = useMemo(
    () => run?.result_files?.find(file => file.path.toLowerCase() === EVENT_TRACE_FILE) || null,
    [run?.result_files]
  );
  const otherResultFiles = useMemo(
    () => (run?.result_files || []).filter(file => file.path.toLowerCase() !== EVENT_TRACE_FILE),
    [run?.result_files]
  );

  useEffect(() => {
    if (!run || !terminalStatuses.has(run.status) || !sessionId || !simulationId) return;

    setEventTrace(null);
    setEventTraceError(null);
    if (!eventTraceFile) {
      setEventTraceStatus('idle');
      return;
    }
    if (!eventTraceFile.previewable) {
      setEventTraceStatus('failed');
      setEventTraceError('This event trace is too large or is not a text file. Download the raw result to inspect it.');
      return;
    }

    let cancelled = false;
    setEventTraceStatus('loading');
    void getSimulationResultPreview(
      sessionId,
      simulationId,
      run.run_id,
      eventTraceFile.path
    ).then((preview) => {
      if (cancelled) return;
      setEventTrace(parseEventTrace(preview.content));
      setEventTraceStatus('loaded');
    }).catch((reason: any) => {
      if (cancelled) return;
      setEventTraceStatus('failed');
      setEventTraceError(reason?.message || 'The event trace could not be loaded.');
    });

    return () => {
      cancelled = true;
    };
  }, [
    eventTraceFile?.path,
    eventTraceFile?.previewable,
    eventTraceFile?.sha256,
    run?.run_id,
    run?.status,
    sessionId,
    simulationId,
  ]);

  const isRunning = Boolean(run && !terminalStatuses.has(run.status));
  const canStop = Boolean(run && ['queued', 'running', 'stopping'].includes(run.status));
  const validationBlocksRun = spec?.validation_status === 'validating';
  const missingRequired = spec ? missingRequiredParameters(spec.parameters, values) : [];
  const suggestedParameterCount = spec?.parameters.filter(hasSuggestedValue).length || 0;
  const suggestionsChanged = Boolean(spec && suggestedValuesChanged(spec.parameters, values));
  const inputsValid = Boolean(spec && spec.parameters.every(parameter => {
    const value = values[parameter.name];
    if (isMissingScenarioValue(value)) return !parameter.required;
    if (parameter.type === 'integer' || parameter.type === 'number') {
      return Number.isFinite(Number(value));
    }
    return true;
  }));
  const canRun = Boolean(spec?.available && sessionId && simulationId && !isRunning && !validationBlocksRun && inputsValid);

  const resetSuggestions = () => {
    if (!spec) return;
    setValues(previous => resetSuggestedValues(spec.parameters, previous));
  };

  const parsedValues = useMemo(() => {
    if (!spec) return values;
    return Object.fromEntries(spec.parameters.flatMap(parameter => {
      const value = values[parameter.name];
      if (value === '' || value === undefined) return [];
      if (parameter.type === 'integer') return [[parameter.name, Number.parseInt(String(value), 10)] as const];
      if (parameter.type === 'number') return [[parameter.name, Number.parseFloat(String(value))] as const];
      return [[parameter.name, value] as const];
    }));
  }, [spec, values]);

  const start = async () => {
    if (!simulationId || !canRun) return;
    setStarting(true);
    setError(null);
    setResultPreview(null);
    setActiveResultPath(null);
    setResultFileError(null);
    setEventTrace(null);
    setEventTraceStatus('idle');
    setEventTraceError(null);
    try {
      const next = await startSimulationRun(
        sessionId,
        simulationId,
        parsedValues
      );
      setValidationRunId(next.run_id);
      setRun(next);
      setSpec(previous => previous ? {
        ...previous,
        validation_status: 'validating',
        validation_message: 'Running this scenario against the exact current simulation files.'
      } : previous);
    } catch (reason: any) {
      setError(reason?.message || 'The simulation could not be started.');
    } finally {
      setStarting(false);
    }
  };

  const stop = async () => {
    if (!simulationId || !run) return;
    setStopping(true);
    try {
      setRun(await stopSimulationRun(sessionId, simulationId, run.run_id));
    } catch (reason: any) {
      setError(reason?.message || 'The simulation could not be stopped.');
    } finally {
      setStopping(false);
    }
  };

  const downloadResult = async (file: SimulationResultFile) => {
    if (!simulationId || !run || file.downloadable === false) return;
    setActiveResultPath(file.path);
    setResultFileError(null);
    try {
      const blob = await downloadSimulationResultFile(
        sessionId,
        simulationId,
        run.run_id,
        file.path
      );
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = file.path.split('/').pop() || 'simulation-result';
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    } catch (reason: any) {
      setResultFileError(reason?.message || 'The result file could not be downloaded.');
    } finally {
      setActiveResultPath(null);
    }
  };

  const openResult = async (file: SimulationResultFile) => {
    if (!simulationId || !run) return;
    if (!file.previewable) {
      await downloadResult(file);
      return;
    }
    setActiveResultPath(file.path);
    setResultFileError(null);
    try {
      setResultPreview(await getSimulationResultPreview(
        sessionId,
        simulationId,
        run.run_id,
        file.path
      ));
    } catch (reason: any) {
      setResultFileError(reason?.message || 'The result file could not be previewed.');
    } finally {
      setActiveResultPath(null);
    }
  };

  if (!simulationId) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="max-w-md rounded-xl border border-dashed border-slate-300 bg-white p-8 text-center">
          <Play className="mx-auto mb-3 text-slate-400" size={28} />
          <h2 className="font-semibold text-slate-800">Choose a simulation to run</h2>
          <p className="mt-2 text-sm text-slate-500">Select one from the simulation menu above, or ask the agent to create one.</p>
        </div>
      </div>
    );
  }

  if (loadingSpec) {
    return <div className="flex h-full items-center justify-center gap-2 text-sm text-slate-500"><Loader2 size={16} className="animate-spin" /> Preparing runner…</div>;
  }

  if (unavailable) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="max-w-lg rounded-xl border border-amber-200 bg-amber-50 p-6">
          <h2 className="flex items-center gap-2 font-semibold text-amber-900"><Clock3 size={18} /> Runner is being prepared</h2>
          <p className="mt-2 text-sm leading-6 text-amber-800">You can inspect this simulation now. Running it will become available after the execution service is enabled.</p>
          <button onClick={loadSpec} className="mt-4 flex items-center gap-2 rounded border border-amber-300 bg-white px-3 py-2 text-sm font-medium text-amber-900 hover:bg-amber-100"><RotateCw size={14} /> Check again</button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto p-4 sm:p-6">
      <div className="mx-auto grid max-w-6xl gap-4 lg:grid-cols-[minmax(260px,360px)_minmax(0,1fr)]">
        <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <h2 className="truncate font-semibold text-slate-900" title={`Run ${simulationName || 'simulation'}`}>Run {simulationName || 'simulation'}</h2>
              <p className="mt-1 text-xs leading-5 text-slate-500">Use a small scenario to observe how the generated model behaves. A successful run verifies this exact version so it can be saved.</p>
            </div>
            {spec?.validation_status && <span className={`shrink-0 whitespace-nowrap rounded-full border px-2 py-1 text-[10px] font-semibold ${statusTone(spec.validation_status)}`}>{validationStatusLabel(spec.validation_status)}</span>}
          </div>

          {spec?.description && <p className="mt-4 text-sm leading-6 text-slate-600">{spec.description}</p>}
          {spec && !spec.available && <p className="mt-3 rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">This simulation does not declare a runnable entry point yet.</p>}
          {spec?.validation_message && <p className="mt-3 rounded bg-slate-50 px-3 py-2 text-xs text-slate-600">{spec.validation_message}</p>}

          {suggestedParameterCount > 0 && (
            <div className="mt-4 flex items-center justify-between gap-3 rounded-lg border border-blue-100 bg-blue-50 px-3 py-2">
              <div className="min-w-0">
                <div className="text-xs font-semibold text-blue-900">Suggested scenario</div>
                <div className="mt-0.5 text-[11px] leading-4 text-blue-700">Run these starting values as shown, or adjust them to explore the model.</div>
              </div>
              {suggestionsChanged && (
                <button
                  type="button"
                  onClick={resetSuggestions}
                  className="flex shrink-0 items-center gap-1.5 rounded border border-blue-200 bg-white px-2.5 py-1.5 text-[11px] font-medium text-blue-800 hover:bg-blue-100"
                >
                  <RotateCcw size={13} /> Reset to suggested
                </button>
              )}
            </div>
          )}

          {missingRequired.length > 0 && (
            <div className="mt-3 flex gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-900">
              <AlertCircle size={15} className="mt-0.5 shrink-0" />
              <span>
                <strong>More information is required.</strong>{' '}
                The generator could not choose a safe starting value for {missingRequired.map(parameter => parameter.label || parameter.name).join(', ')}. Enter {missingRequired.length === 1 ? 'it' : 'them'} before running.
              </span>
            </div>
          )}

          <div className="mt-5 space-y-4">
            {(spec?.parameters || []).map(parameter => {
              const hasSuggestion = hasSuggestedValue(parameter);
              const requiresUserValue = Boolean(parameter.required && !hasSuggestion);
              const missing = Boolean(parameter.required && isMissingScenarioValue(values[parameter.name]));
              return (
              <label key={parameter.name} className="block">
                <span className="mb-1 flex items-center gap-2 text-xs font-semibold text-slate-700">
                  <span>{parameter.label || parameter.name}</span>
                  {requiresUserValue && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-amber-800">Required</span>}
                </span>
                {parameter.description && <span className="mb-1.5 block text-[11px] leading-4 text-slate-500">{parameter.description}</span>}
                {parameter.type === 'boolean' && hasSuggestion ? (
                  <input type="checkbox" checked={Boolean(values[parameter.name])} onChange={event => setValues(previous => ({ ...previous, [parameter.name]: event.target.checked }))} className="h-4 w-4 rounded border-slate-300 text-blue-600" />
                ) : parameter.type === 'boolean' ? (
                  <select
                    value={String(values[parameter.name] ?? '')}
                    onChange={event => setValues(previous => ({ ...previous, [parameter.name]: choiceValue(parameter, event.target.value) }))}
                    className={`h-9 w-full rounded border bg-white px-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100 ${missing ? 'border-amber-400' : 'border-slate-300'}`}
                  >
                    <option value="">{parameter.required ? 'Choose true or false…' : 'Leave unspecified'}</option>
                    <option value="true">True</option>
                    <option value="false">False</option>
                  </select>
                ) : parameter.type === 'choice' || Boolean(parameter.choices?.length) ? (
                  <select value={String(values[parameter.name] ?? '')} onChange={event => setValues(previous => ({ ...previous, [parameter.name]: choiceValue(parameter, event.target.value) }))} className={`h-9 w-full rounded border bg-white px-2 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100 ${missing ? 'border-amber-400' : 'border-slate-300'}`}>
                    {!hasSuggestion && <option value="">{parameter.required ? 'Choose a value…' : 'Leave unspecified'}</option>}
                    {(parameter.choices || []).map(choice => <option key={String(choice)} value={String(choice)}>{choice}</option>)}
                  </select>
                ) : (
                  <input
                    type={parameter.type === 'integer' || parameter.type === 'number' ? 'number' : 'text'}
                    min={parameter.minimum}
                    max={parameter.maximum}
                    required={parameter.required}
                    step={parameter.type === 'integer' ? 1 : 'any'}
                    value={String(values[parameter.name] ?? '')}
                    onChange={event => setValues(previous => ({ ...previous, [parameter.name]: event.target.value }))}
                    placeholder={parameter.required && !hasSuggestion ? 'Enter a value to run' : undefined}
                    className={`h-9 w-full rounded border px-3 text-sm outline-none focus:border-blue-500 focus:ring-2 focus:ring-blue-100 ${missing ? 'border-amber-400' : 'border-slate-300'}`}
                  />
                )}
                {requiresUserValue && (
                  <span className={`mt-1.5 block text-[10px] leading-4 ${missing ? 'text-amber-700' : 'text-slate-500'}`}>
                    No suggested value is available for this input.
                  </span>
                )}
              </label>
              );
            })}
            {spec?.parameters.length === 0 && <div className="rounded bg-slate-50 px-3 py-3 text-xs text-slate-500">This simulation uses its default scenario.</div>}
          </div>

          <div className="mt-5 flex gap-2">
            <button onClick={start} disabled={!canRun || starting} className="flex flex-1 items-center justify-center gap-2 rounded bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-45">
              {starting ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />} Run simulation
            </button>
            {canStop && <button onClick={stop} disabled={stopping || run?.status === 'stopping'} className="flex items-center gap-2 rounded border border-red-200 px-3 py-2 text-sm font-medium text-red-700 hover:bg-red-50 disabled:opacity-50"><CircleStop size={16} /> {run?.status === 'stopping' ? 'Stopping…' : 'Stop'}</button>}
          </div>
          {missingRequired.length > 0 && <p className="mt-2 text-[11px] text-amber-700">Run is available after the required {missingRequired.length === 1 ? 'value is' : 'values are'} supplied.</p>}
          {error && <div className="mt-3 flex gap-2 rounded border border-red-200 bg-red-50 p-3 text-xs leading-5 text-red-700"><AlertCircle size={15} className="mt-0.5 flex-shrink-0" /> {error}</div>}
        </section>

        <section className="min-h-[340px] overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
          <div className="flex items-center justify-between border-b border-slate-200 px-4 py-3">
            <div className="flex items-center gap-2 font-semibold text-slate-800"><Terminal size={16} /> Results</div>
            {run && <span className={`rounded-full border px-2 py-1 text-[10px] font-semibold ${statusTone(run.status)}`}>{run.status}</span>}
          </div>
          {!run ? (
            <div className="flex min-h-[290px] items-center justify-center px-6 text-center text-sm text-slate-500">Run the simulation to see metrics and output here.</div>
          ) : (
            <div className="space-y-4 p-4">
              {run.status === 'succeeded' && <div className="flex items-center gap-2 rounded bg-emerald-50 px-3 py-2 text-sm text-emerald-700"><CheckCircle2 size={16} /> Simulation completed successfully.</div>}
              {run.status === 'finalizing' && <div className="flex items-center gap-2 rounded bg-blue-50 px-3 py-2 text-sm text-blue-700"><Loader2 size={16} className="animate-spin" /> Simulation completed. Preparing this exact version so it can be saved…</div>}
              {run.status === 'stopped' && <div className="flex items-center gap-2 rounded bg-slate-50 px-3 py-2 text-sm text-slate-600"><CircleStop size={16} /> Run stopped. You can change the scenario and run this version again.</div>}
              {run.metrics && Object.keys(run.metrics).length > 0 && (
                <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                  {Object.entries(run.metrics).map(([name, value]) => <div key={name} className="rounded border border-slate-200 bg-slate-50 p-3"><div className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">{name}</div><div className="mt-1 break-all text-lg font-semibold text-slate-900">{String(value ?? '—')}</div></div>)}
                </div>
              )}
              {terminalStatuses.has(run.status) && (
                <div className="overflow-hidden rounded-lg border border-slate-200">
                  <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 bg-slate-50 px-3 py-2">
                    <div className="flex min-w-0 items-center gap-2">
                      <ListTree size={15} className="shrink-0 text-blue-600" />
                      <span className="text-xs font-semibold text-slate-800">Event trace</span>
                      {eventTrace && (
                        <span className="text-[10px] text-slate-500">
                          {eventTrace.recordedEvents ?? (eventTrace.rows.length + eventTrace.hiddenRows)} recorded
                        </span>
                      )}
                    </div>
                    {eventTraceFile && (eventTraceFile.previewable || eventTraceFile.downloadable !== false) && (
                      <button
                        type="button"
                        onClick={() => void openResult(eventTraceFile)}
                        disabled={activeResultPath === eventTraceFile.path}
                        className="flex items-center gap-1 rounded px-2 py-1 text-[10px] font-medium text-blue-700 hover:bg-blue-50 disabled:opacity-50"
                      >
                        {activeResultPath === eventTraceFile.path
                          ? <Loader2 size={12} className="animate-spin" />
                          : eventTraceFile.previewable ? <Eye size={12} /> : <Download size={12} />}
                        {eventTraceFile.previewable ? 'View raw' : 'Download trace'}
                      </button>
                    )}
                  </div>

                  {eventTraceStatus === 'loading' && (
                    <div className="flex items-center gap-2 px-3 py-4 text-xs text-slate-500">
                      <Loader2 size={14} className="animate-spin text-blue-500" /> Loading the recorded events…
                    </div>
                  )}

                  {!eventTraceFile && eventTraceStatus === 'idle' && (
                    <div className="px-3 py-3 text-xs leading-5 text-slate-600">
                      No event trace was recorded for this run. Older generated simulations may only provide their summary and console output, and a failed run may end before tracing starts. Regenerate or revise the simulation to add event tracing.
                    </div>
                  )}

                  {eventTraceStatus === 'failed' && (
                    <div className="flex gap-2 px-3 py-3 text-xs leading-5 text-amber-800">
                      <AlertCircle size={14} className="mt-0.5 shrink-0" />
                      <span>{eventTraceError || 'The event trace could not be displayed. You can still open or download the raw result file.'}</span>
                    </div>
                  )}

                  {eventTraceStatus === 'loaded' && eventTrace && eventTrace.rows.length === 0 && (
                    <div className="px-3 py-3 text-xs leading-5 text-slate-600">
                      {eventTrace.malformedLines > 0
                        ? `The trace file contains ${eventTrace.malformedLines} line${eventTrace.malformedLines === 1 ? '' : 's'} that could not be read as events. Open the raw file to inspect it.`
                        : eventTrace.complete
                          ? 'The trace was recorded, but this scenario produced no port events.'
                          : 'The run ended before any complete event rows or a trace summary were retained.'}
                    </div>
                  )}

                  {eventTraceStatus === 'loaded' && eventTrace && eventTrace.rows.length > 0 && (
                    <>
                      <div className="max-h-80 overflow-auto">
                        <table className="w-full min-w-[760px] table-fixed text-left text-[11px]">
                          <colgroup>
                            <col className="w-14" />
                            <col className="w-32" />
                            <col className="w-56" />
                            <col className="w-40" />
                            <col />
                          </colgroup>
                          <thead className="sticky top-0 z-10 bg-white text-[10px] uppercase tracking-wide text-slate-500 shadow-[0_1px_0_0_rgb(226,232,240)]">
                            <tr>
                              <th className="px-3 py-2 font-semibold">#</th>
                              <th className="whitespace-nowrap px-3 py-2 font-semibold">Time</th>
                              <th className="whitespace-nowrap px-3 py-2 font-semibold">Component</th>
                              <th className="whitespace-nowrap px-3 py-2 font-semibold">Port</th>
                              <th className="px-3 py-2 font-semibold">Value</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-slate-100">
                            {eventTrace.rows.map((row, index) => (
                              <tr key={`${row.sequence}-${index}`} className="align-top hover:bg-blue-50/40">
                                <td className="px-3 py-2 font-mono text-slate-400">{row.sequence}</td>
                                <td className="overflow-hidden text-ellipsis whitespace-nowrap px-3 py-2 font-mono text-slate-700" title={row.simulationTime}>{compactSimulationTime(row.simulationTime)}</td>
                                <td className="overflow-hidden text-ellipsis whitespace-nowrap px-3 py-2 font-medium text-slate-700" title={row.component}>{row.component}</td>
                                <td className="truncate px-3 py-2 font-mono text-slate-600" title={row.port}>{row.port}</td>
                                <td className="truncate px-3 py-2 font-mono text-slate-600" title={row.value}>{row.value}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                      {(!eventTrace.complete || eventTrace.truncated || eventTrace.hiddenRows > 0 || eventTrace.malformedLines > 0) && (
                        <div className="border-t border-amber-100 bg-amber-50 px-3 py-2 text-[10px] leading-4 text-amber-800">
                          {!eventTrace.complete && <span>The run ended before the trace summary was written; these are the events retained up to that point.</span>}
                          {eventTrace.truncated && (
                            <span>
                              The recorder reached its trace limit
                              {eventTrace.droppedEvents !== undefined ? ` and omitted ${eventTrace.droppedEvents} event${eventTrace.droppedEvents === 1 ? '' : 's'}` : ''}.
                            </span>
                          )}
                          {eventTrace.hiddenRows > 0 && <span> Showing the first {eventTrace.rows.length} events here; use the raw file for the rest.</span>}
                          {eventTrace.malformedLines > 0 && <span> Skipped {eventTrace.malformedLines} malformed line{eventTrace.malformedLines === 1 ? '' : 's'}.</span>}
                        </div>
                      )}
                    </>
                  )}
                </div>
              )}
              {otherResultFiles.length > 0 && (
                <div>
                  <div className="mb-2 text-xs font-semibold text-slate-700">Result files</div>
                  <div className="divide-y divide-slate-100 overflow-hidden rounded border border-slate-200">
                    {otherResultFiles.map(file => {
                      const available = file.previewable || file.downloadable !== false;
                      const busy = activeResultPath === file.path;
                      return (
                        <button
                          key={file.path}
                          type="button"
                          onClick={() => void openResult(file)}
                          disabled={!available || busy}
                          className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
                          title={file.previewable ? `Preview ${file.path}` : `Download ${file.path}`}
                        >
                          {busy ? <Loader2 size={14} className="flex-shrink-0 animate-spin text-blue-500" /> : file.previewable ? <Eye size={14} className="flex-shrink-0 text-slate-400" /> : <File size={14} className="flex-shrink-0 text-slate-400" />}
                          <span className="min-w-0 flex-1 truncate font-mono text-slate-700">{file.path}</span>
                          <span className="flex-shrink-0 text-slate-400">{formatFileSize(file.size)}</span>
                          <span className="flex-shrink-0 font-medium text-blue-600">{file.previewable ? 'View' : file.downloadable === false ? 'Too large' : 'Download'}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
              {resultFileError && <div className="flex gap-2 rounded border border-red-200 bg-red-50 p-2 text-xs text-red-700"><AlertCircle size={14} className="mt-0.5 flex-shrink-0" /> {resultFileError}</div>}
              {resultPreview && (
                <div className="overflow-hidden rounded border border-slate-700 bg-slate-950">
                  <div className="flex items-center justify-between gap-3 border-b border-slate-700 bg-slate-900 px-3 py-2">
                    <div className="min-w-0 truncate font-mono text-xs text-slate-200" title={resultPreview.path}>{resultPreview.path}</div>
                    <div className="flex flex-shrink-0 items-center gap-1">
                      {resultPreview.downloadable !== false && (
                        <button type="button" onClick={() => void downloadResult(resultPreview)} className="rounded p-1.5 text-slate-300 hover:bg-slate-700 hover:text-white" title="Download file"><Download size={14} /></button>
                      )}
                      <button type="button" onClick={() => setResultPreview(null)} className="rounded p-1.5 text-slate-300 hover:bg-slate-700 hover:text-white" title="Close preview"><X size={14} /></button>
                    </div>
                  </div>
                  <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-xs leading-5 text-slate-100">{formattedPreview(resultPreview)}</pre>
                </div>
              )}
              {(run.stdout || run.stderr) && (
                <div className="overflow-hidden rounded border border-slate-800 bg-slate-950">
                  {run.stdout && <pre className="max-h-72 overflow-auto whitespace-pre-wrap p-3 font-mono text-xs leading-5 text-slate-100">{run.stdout}{run.stdout_truncated ? '\n… output truncated …' : ''}</pre>}
                  {run.stderr && <pre className="max-h-48 overflow-auto whitespace-pre-wrap border-t border-slate-800 p-3 font-mono text-xs leading-5 text-red-300">{run.stderr}{run.stderr_truncated ? '\n… output truncated …' : ''}</pre>}
                </div>
              )}
              {!run.stdout && !run.stderr && !run.metrics && <div className="flex items-center gap-2 text-sm text-slate-500">{isRunning && <Loader2 size={15} className="animate-spin" />} Waiting for results…</div>}
            </div>
          )}
        </section>
      </div>
    </div>
  );
};
