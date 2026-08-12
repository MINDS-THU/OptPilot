import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Activity, AlertTriangle, ArrowLeft, Bot, CheckCircle2, Circle, Clock, FastForward, FileCode2, Loader2, Network, Send, Sparkles, User, X, XCircle, Undo2 } from 'lucide-react';
import { BackendMessage, ChatMessage, ChatRequestInfo, GenerationMode, PendingInteraction, ProgressActivityState, ProgressEvent, ProgressFileChange, ReviewQuestion } from '../types';
import {
  cancelQueuedRequest,
  getSessionEvents,
  getSessionMessages,
  getSessionRequest,
  resolveGenerationInteraction,
  submitSessionChat
} from '../services/agentService';
import { deriveActivityTimelineStates } from '../services/activityStateService.js';
import { shouldRefreshRequestFromActivity } from '../services/requestPollingService.js';
import {
  isCurrentSessionRequest,
  isCurrentSessionScope,
  requestForSession
} from '../services/sessionScopeService.js';
import {
  architectureConnectionLabel,
  generationModeCaption,
  latestApprovedArchitectureInteraction,
  requestStatusLabel,
  reviewComponentResponsibility,
  reviewPresentation,
  shouldShowContinueAutomatically,
  shouldShowGenerationActivity
} from '../services/reviewPresentationService.js';
import { focusWithoutDocumentScroll, scrollContainerToBottom } from '../services/scrollContainerService.js';

interface Props {
  sessionId: string;
  activeRequestId?: string | null;
  activeProjectId: string | null;
  currentProjectName: string | null;
  currentSessionTitle?: string;
  onActivityFileSelect?: (requestId: string, filePath: string) => void | Promise<void>;
  onPendingStructureChange?: (
    interaction: PendingInteraction | null,
    state?: 'awaiting_review' | 'revising' | 'approved_building' | 'finalizing' | 'build_stopped' | 'clear',
    owner?: { sessionId: string; requestId: string }
  ) => void;
  onReviewStructure?: () => void;
  defaultGenerationMode?: GenerationMode;
  apiKey?: string;
  provider?: string;
  isOpen: boolean;
  onBack?: () => void;
  onClose?: () => void;
}

const toChatMessage = (msg: BackendMessage): ChatMessage => ({
  id: msg.message_id,
  role: msg.role,
  content: msg.status === 'withdrawn' ? '(withdrawn)' : msg.content,
  timestamp: Date.parse(msg.created_at) || Date.now(),
  status: msg.status
});

const CHAT_POLL_INTERVAL_MS = 5000;
const ACTIVITY_POLL_INTERVAL_MS = 2000;
const TERMINAL_REQUEST_STATUSES = ['completed', 'failed', 'cancelled'];
const BUSY_REQUEST_STATUSES = ['queued', 'running', 'cancelling'];
const GENERATION_MODE_STORAGE_PREFIX = 'devs_generator_mode:';

interface DisplayActivity {
  id: number;
  activityKey: string;
  state: ProgressActivityState;
  title: string;
  detail?: string;
  current?: number;
  total?: number;
  technicalName?: string;
  fileChanges: ProgressFileChange[];
  requestId: string;
  createdAt: string;
}

const LEGACY_ACTIVITY: Record<string, {
  state: ProgressActivityState;
  title: string;
  detail?: string;
}> = {
  request_started: { state: 'completed', title: 'Request received' },
  request_recovered: { state: 'progress', title: 'Request restored', detail: 'Generation resumed after the service restarted.' },
  agent_started: { state: 'completed', title: 'Generator started' },
  agent_log: { state: 'progress', title: 'Working on the simulation' },
  tool_started: { state: 'progress', title: 'Running a build step' },
  tool_finished: { state: 'completed', title: 'Build step completed' },
  files_changed: { state: 'completed', title: 'Simulation files updated' },
  assistant_message: { state: 'progress', title: 'Preparing the result' },
  simulation_repair_started: { state: 'progress', title: 'Repairing an execution problem' },
  simulation_repair_completed: { state: 'completed', title: 'Simulation repair checked' },
  interaction_required: { state: 'completed', title: 'Review ready' },
  interaction_resolved: { state: 'completed', title: 'Review confirmed' },
  request_cancel_requested: { state: 'progress', title: 'Stopping after the current step' },
  request_cancelled: { state: 'failed', title: 'Request withdrawn' },
  request_failed: { state: 'failed', title: 'Generation stopped' },
  request_completed: { state: 'completed', title: 'Simulation ready' }
};

const boundedText = (value: unknown, maxLength: number): string | undefined => {
  if (typeof value !== 'string') return undefined;
  const compact = value.replace(/\s+/g, ' ').trim();
  if (!compact) return undefined;
  return compact.length > maxLength ? `${compact.slice(0, maxLength - 1)}…` : compact;
};

const normalizeProgressEvent = (event: ProgressEvent): DisplayActivity => {
  const legacy = LEGACY_ACTIVITY[event.type];
  const structured = event.type === 'activity';
  const title = structured
    ? boundedText(event.title || event.content, 160) || 'Working on the simulation'
    : legacy?.title || 'Working on the simulation';
  const state = event.activity_state || legacy?.state || 'progress';
  const current = typeof event.current === 'number' && Number.isFinite(event.current)
    ? event.current
    : undefined;
  const total = typeof event.total === 'number' && Number.isFinite(event.total)
    ? event.total
    : undefined;

  return {
    id: event.event_id,
    activityKey: event.activity_key || event.type,
    state,
    title,
    // Only the structured activity contract is allowed to expose event detail.
    // Legacy agent logs may contain prompts, source code, or internal paths.
    detail: structured ? boundedText(event.detail, 360) : legacy?.detail,
    current,
    total,
    technicalName: structured ? boundedText(event.technical_name, 100) : undefined,
    fileChanges: structured && Array.isArray(event.file_changes)
      ? event.file_changes.filter(change => (
          change
          && typeof change.path === 'string'
          && (change.change === 'added' || change.change === 'modified')
        ))
      : [],
    requestId: event.request_id,
    createdAt: event.created_at
  };
};

const ActivityFileLinks: React.FC<{
  activity: DisplayActivity;
  onSelect?: (requestId: string, filePath: string) => void | Promise<void>;
}> = ({ activity, onSelect }) => {
  if (activity.fileChanges.length === 0) return null;
  return (
    <div className="mt-1.5 space-y-1" aria-label="Files changed in this step">
      {activity.fileChanges.map(change => (
        <button
          key={`${change.change}:${change.path}`}
          type="button"
          onClick={() => onSelect?.(activity.requestId, change.path)}
          disabled={!onSelect}
          title={`Open ${change.path}`}
          className="flex w-full min-w-0 items-center gap-1.5 rounded border border-slate-200 bg-white px-2 py-1 text-left hover:border-blue-200 hover:bg-blue-50 disabled:cursor-default disabled:hover:border-slate-200 disabled:hover:bg-white"
        >
          <FileCode2 size={11} aria-hidden="true" className="shrink-0 text-slate-400" />
          <span className="min-w-0 flex-1 truncate text-[10px] font-medium text-slate-600">{change.path}</span>
          <span className={`shrink-0 rounded px-1 py-0.5 text-[8px] font-semibold uppercase tracking-wide ${change.change === 'added' ? 'bg-emerald-50 text-emerald-700' : 'bg-blue-50 text-blue-700'}`}>
            {change.change}
          </span>
        </button>
      ))}
    </div>
  );
};

const mergeProgressEvents = (existing: ProgressEvent[], incoming: ProgressEvent[]): ProgressEvent[] => {
  const byId = new Map<number, ProgressEvent>();
  existing.forEach(event => byId.set(event.event_id, event));
  incoming.forEach(event => byId.set(event.event_id, event));
  return Array.from(byId.values()).sort((left, right) => left.event_id - right.event_id);
};

const formatElapsed = (seconds: number): string => {
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return minutes > 0 ? `${minutes}m ${remainder}s` : `${remainder}s`;
};

const formatLastUpdate = (createdAt: string | undefined): string => {
  if (!createdAt) return 'Waiting for the first update';
  const timestamp = Date.parse(createdAt);
  if (!Number.isFinite(timestamp)) return 'Updated recently';
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 5) return 'Updated just now';
  if (seconds < 60) return `Updated ${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `Updated ${minutes}m ago`;
  return `Updated ${new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`;
};

const ActivityStateIcon: React.FC<{ state: ProgressActivityState; active?: boolean }> = ({ state, active }) => {
  const label = state === 'completed'
    ? 'Completed'
    : state === 'failed'
      ? 'Failed'
      : active
        ? 'In progress'
        : 'Pending';
  const icon = state === 'completed'
    ? <CheckCircle2 aria-hidden="true" size={13} className="shrink-0 text-emerald-600" />
    : state === 'failed'
      ? <XCircle aria-hidden="true" size={13} className="shrink-0 text-red-500" />
      : active
        ? <Loader2 aria-hidden="true" size={13} className="shrink-0 animate-spin text-purple-600" />
        : <Circle aria-hidden="true" size={11} className="shrink-0 text-slate-400" />;
  return <><span className="sr-only">{label}: </span>{icon}</>;
};

const elapsedSecondsFromRequest = (request: ChatRequestInfo | null): number => {
  if (!request?.started_at) return 0;
  const startedAt = Date.parse(request.started_at);
  if (!Number.isFinite(startedAt)) return 0;
  const completedAt = request.completed_at ? Date.parse(request.completed_at) : Number.NaN;
  const end = Number.isFinite(completedAt) ? completedAt : Date.now();
  return Math.max(0, Math.floor((end - startedAt) / 1000));
};

const stringValue = (value: unknown): string => typeof value === 'string' ? value.trim() : '';

const stringList = (value: unknown): string[] => {
  if (Array.isArray(value)) return value.map(stringValue).filter(Boolean);
  const scalar = stringValue(value);
  return scalar ? [scalar] : [];
};

const reviewQuestionsFrom = (
  interaction: PendingInteraction | null,
  payload: Record<string, unknown>
): ReviewQuestion[] => {
  const raw = Array.isArray(payload.questions)
    ? payload.questions
    : (Array.isArray(interaction?.questions) ? interaction.questions : []);
  return raw.filter((question): question is ReviewQuestion => Boolean(
    question
    && typeof question === 'object'
    && typeof (question as ReviewQuestion).question_id === 'string'
    && typeof (question as ReviewQuestion).prompt === 'string'
  ));
};

export const ChatInterface: React.FC<Props> = ({
  sessionId,
  activeRequestId,
  activeProjectId,
  currentProjectName,
  currentSessionTitle,
  onActivityFileSelect,
  onPendingStructureChange,
  onReviewStructure,
  defaultGenerationMode = 'guided',
  isOpen,
  onBack,
  onClose
}) => {
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [storedActiveRequest, setActiveRequest] = useState<ChatRequestInfo | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);
  const [timerSeconds, setTimerSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [activityError, setActivityError] = useState<string | null>(null);
  const [progressEvents, setProgressEvents] = useState<ProgressEvent[]>([]);
  const [includeProjectContext, setIncludeProjectContext] = useState(false);
  const [generationMode, setGenerationMode] = useState<GenerationMode>('guided');
  const [reviewAnswers, setReviewAnswers] = useState<Record<string, string>>({});
  const [isResolvingReview, setIsResolvingReview] = useState(false);
  const [latestRequestRef, setLatestRequestRef] = useState<{
    sessionId: string;
    requestId: string;
  } | null>(null);

  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const shouldAutoScrollRef = useRef(true);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const pollingInFlightRef = useRef(false);
  const quickPollTimeoutRef = useRef<number | null>(null);
  const activityCursorRef = useRef(0);
  const activityScopeRef = useRef('');
  const activityPollingInFlightRef = useRef(false);
  const reviewDecisionRef = useRef<{ fingerprint: string; idempotencyKey: string } | null>(null);
  const chatSubmissionRef = useRef<{ fingerprint: string; idempotencyKey: string } | null>(null);
  const retainedStructureReviewRef = useRef<{
    sessionId: string;
    requestId: string;
    interaction: PendingInteraction;
  } | null>(null);
  const currentSessionScopeRef = useRef({ sessionId, revision: 0 });
  if (currentSessionScopeRef.current.sessionId !== sessionId) {
    currentSessionScopeRef.current = {
      sessionId,
      revision: currentSessionScopeRef.current.revision + 1
    };
  }

  // A response from a previously selected session may still reach React. Keep
  // it ineligible for every review/projection/render decision even before the
  // async caller's stale-response guard runs.
  const activeRequest = requestForSession(storedActiveRequest, sessionId) as ChatRequestInfo | null;

  const latestRequestId = latestRequestRef?.sessionId === sessionId
    ? latestRequestRef.requestId
    : null;
  const progressRequestId = activeRequest?.session_id === sessionId
    ? activeRequest.request_id
    : (activeRequestId || latestRequestId);
  const pendingInteraction = activeRequest?.status === 'waiting_for_user'
    ? activeRequest.pending_interaction || null
    : null;
  const isWaitingForReview = Boolean(pendingInteraction);
  const approvedArchitectureInteraction = latestApprovedArchitectureInteraction(activeRequest) as PendingInteraction | null;

  const scrollToBottom = () => {
    scrollContainerToBottom(scrollContainerRef.current, 'smooth');
  };

  const refreshMessages = async (scope = currentSessionScopeRef.current): Promise<boolean> => {
    if (!scope.sessionId) return false;
    const backendMessages = await getSessionMessages(scope.sessionId, 30);
    if (!isCurrentSessionScope(scope, currentSessionScopeRef.current)) return false;
    setMessages(backendMessages.map(toChatMessage));
    const mostRecentRequest = [...backendMessages]
      .reverse()
      .find(message => Boolean(message.request_id));
    setLatestRequestRef(previous => {
      if (!mostRecentRequest) return null;
      if (
        previous?.sessionId === scope.sessionId
        && previous.requestId === mostRecentRequest.request_id
      ) return previous;
      return { sessionId: scope.sessionId, requestId: mostRecentRequest.request_id };
    });
    return true;
  };

  useEffect(() => {
    if (!isOpen || !sessionId) return;
    const scope = currentSessionScopeRef.current;
    setError(null);
    setActiveRequest(null);
    setIsProcessing(false);
    setIsResolvingReview(false);
    chatSubmissionRef.current = null;
    reviewDecisionRef.current = null;
    pollingInFlightRef.current = false;
    if (quickPollTimeoutRef.current !== null) {
      window.clearTimeout(quickPollTimeoutRef.current);
      quickPollTimeoutRef.current = null;
    }
    shouldAutoScrollRef.current = true;
    refreshMessages(scope).catch(err => {
      if (isCurrentSessionScope(scope, currentSessionScopeRef.current)) {
        setError(err.message || 'Failed to load messages.');
      }
    });
  }, [sessionId, isOpen]);

  useEffect(() => {
    if (!sessionId || typeof window === 'undefined') return;
    const saved = window.localStorage.getItem(`${GENERATION_MODE_STORAGE_PREFIX}${sessionId}`);
    setGenerationMode(
      saved === 'automatic' || saved === 'guided'
        ? saved
        : defaultGenerationMode
    );
  }, [sessionId, defaultGenerationMode]);

  useEffect(() => {
    if (!sessionId || typeof window === 'undefined') return;
    window.localStorage.setItem(`${GENERATION_MODE_STORAGE_PREFIX}${sessionId}`, generationMode);
  }, [sessionId, generationMode]);

  useEffect(() => {
    setReviewAnswers({});
    if (pendingInteraction?.kind === 'structure_review' && activeRequest) {
      retainedStructureReviewRef.current = {
        sessionId,
        requestId: activeRequest.request_id,
        interaction: pendingInteraction
      };
      onPendingStructureChange?.(pendingInteraction, 'awaiting_review', {
        sessionId,
        requestId: activeRequest.request_id
      });
      return;
    }

    if (approvedArchitectureInteraction && activeRequest) {
      retainedStructureReviewRef.current = {
        sessionId,
        requestId: activeRequest.request_id,
        interaction: approvedArchitectureInteraction
      };
      onPendingStructureChange?.(approvedArchitectureInteraction, 'approved_building', {
        sessionId,
        requestId: activeRequest.request_id
      });
      return;
    }

    const retained = retainedStructureReviewRef.current;
    if (!retained) return;
    if (
      retained.sessionId !== sessionId
      || !activeRequest
      || activeRequest.request_id !== retained.requestId
    ) {
      retainedStructureReviewRef.current = null;
      onPendingStructureChange?.(null, 'clear', {
        sessionId: retained.sessionId,
        requestId: retained.requestId
      });
      return;
    }
    if (activeRequest.status === 'failed' || activeRequest.status === 'cancelled') {
      onPendingStructureChange?.(retained.interaction, 'build_stopped', {
        sessionId: retained.sessionId,
        requestId: retained.requestId
      });
      return;
    }
    if (activeRequest.status === 'completed') {
      // App replaces this retained plan only after the final project graph has
      // loaded. Avoid flashing a partial generated hierarchy in between.
      onPendingStructureChange?.(retained.interaction, 'finalizing', {
        sessionId: retained.sessionId,
        requestId: retained.requestId
      });
      return;
    }
    if (
      activeRequest.phase === 'build'
      && (activeRequest.status === 'queued' || activeRequest.status === 'running')
    ) {
      // Keep the exact approved whole-system plan visible during the build.
      // Completion clears this projection before App presents the generated
      // project, so it cannot displace a student's actual project selection.
      onPendingStructureChange?.(retained.interaction, 'approved_building', {
        sessionId: retained.sessionId,
        requestId: retained.requestId
      });
    }
  }, [
    pendingInteraction?.interaction_id,
    pendingInteraction?.revision,
    pendingInteraction?.kind,
    approvedArchitectureInteraction?.interaction_id,
    approvedArchitectureInteraction?.revision,
    activeRequest?.request_id,
    activeRequest?.status,
    activeRequest?.phase,
    sessionId,
    onPendingStructureChange
  ]);

  useEffect(() => {
    if (!isOpen || !sessionId) {
      setActiveRequest(null);
      setIsProcessing(false);
      return;
    }

    const requestId = activeRequestId || latestRequestId;
    if (!requestId) {
      setActiveRequest(null);
      setIsProcessing(false);
      return;
    }

    const scope = currentSessionScopeRef.current;
    let cancelled = false;
    getSessionRequest(scope.sessionId, requestId)
      .then(request => {
        if (cancelled || !isCurrentSessionRequest(request, scope, currentSessionScopeRef.current)) return;
        setActiveRequest(request);
        setIsProcessing(BUSY_REQUEST_STATUSES.includes(request.status));
      })
      .catch(err => {
        if (!cancelled && isCurrentSessionScope(scope, currentSessionScopeRef.current)) {
          setError(err.message || 'Failed to restore active request.');
        }
      });
    return () => { cancelled = true; };
  }, [activeRequestId, latestRequestId, sessionId, isOpen]);

  useEffect(() => {
    const scope = isOpen && sessionId && progressRequestId
      ? `${sessionId}:${progressRequestId}`
      : '';
    activityScopeRef.current = scope;
    activityCursorRef.current = 0;
    activityPollingInFlightRef.current = false;
    setProgressEvents([]);
    setActivityError(null);
  }, [isOpen, sessionId, progressRequestId]);

  useEffect(() => {
    if (!isOpen || !sessionId || !progressRequestId) return;

    const scope = `${sessionId}:${progressRequestId}`;
    let cancelled = false;

    const pollActivity = async () => {
      if (activityPollingInFlightRef.current || activityScopeRef.current !== scope) return;
      activityPollingInFlightRef.current = true;
      try {
        const response = await getSessionEvents(
          sessionId,
          progressRequestId,
          activityCursorRef.current
        );
        if (cancelled || activityScopeRef.current !== scope) return;
        const requestEvents = (response.events || []).filter(
          event => event.request_id === progressRequestId
        );
        if (requestEvents.length > 0) {
          setProgressEvents(existing => mergeProgressEvents(existing, requestEvents));
        }
        activityCursorRef.current = Math.max(
          activityCursorRef.current,
          Number.isFinite(response.next_after) ? response.next_after : activityCursorRef.current
        );

        // The activity endpoint is the fastest source of request lifecycle
        // transitions.  Guided checkpoints store their review artifact on the
        // request itself, so fetch that canonical request immediately instead
        // of waiting for the slower chat poll to notice the transition.
        if (shouldRefreshRequestFromActivity(activeRequest, response, progressRequestId)) {
          const request = await getSessionRequest(sessionId, progressRequestId);
          if (
            cancelled
            || activityScopeRef.current !== scope
            || request.session_id !== sessionId
          ) return;
          setActiveRequest(request);
          setIsProcessing(BUSY_REQUEST_STATUSES.includes(request.status));
        }
        setActivityError(null);
      } catch {
        if (!cancelled && activityScopeRef.current === scope) {
          setActivityError('Live activity is temporarily unavailable. Generation is still running.');
        }
      } finally {
        if (activityScopeRef.current === scope) {
          activityPollingInFlightRef.current = false;
        }
      }
    };

    pollActivity();
    if (!isProcessing) return () => { cancelled = true; };

    const interval = window.setInterval(pollActivity, ACTIVITY_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [
    isOpen,
    sessionId,
    progressRequestId,
    isProcessing,
    activeRequest?.request_id,
    activeRequest?.status
  ]);

  useEffect(() => {
    if (shouldAutoScrollRef.current) scrollToBottom();
  }, [messages, isOpen]);

  useEffect(() => {
    if (isProcessing) {
      setTimerSeconds(elapsedSecondsFromRequest(activeRequest));
      const timer = setInterval(() => {
        setTimerSeconds(prev => {
          const elapsed = elapsedSecondsFromRequest(activeRequest);
          return elapsed > 0 ? elapsed : prev + 1;
        });
      }, 1000);
      return () => clearInterval(timer);
    }
    setTimerSeconds(elapsedSecondsFromRequest(activeRequest));
  }, [isProcessing, activeRequest?.request_id, activeRequest?.started_at]);

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      const newHeight = Math.min(textareaRef.current.scrollHeight, 140);
      textareaRef.current.style.height = `${newHeight}px`;
    }
  }, [input]);

  const pollChatOnce = useCallback(async (requestToPoll: ChatRequestInfo | null = activeRequest) => {
    const scope = currentSessionScopeRef.current;
    if (
      !isOpen
      || !scope.sessionId
      || pollingInFlightRef.current
      || (requestToPoll && requestToPoll.session_id !== scope.sessionId)
    ) return;
    pollingInFlightRef.current = true;
    try {
      const messagesRefreshed = await refreshMessages(scope);
      if (!messagesRefreshed) return;
      if (requestToPoll) {
        const request = await getSessionRequest(scope.sessionId, requestToPoll.request_id);
        if (!isCurrentSessionRequest(request, scope, currentSessionScopeRef.current)) return;
        setActiveRequest(request);
        setIsProcessing(BUSY_REQUEST_STATUSES.includes(request.status));
      }
    } catch (err: any) {
      if (isCurrentSessionScope(scope, currentSessionScopeRef.current)) {
        setError(err.message || 'Failed to refresh chat.');
      }
    } finally {
      pollingInFlightRef.current = false;
    }
  }, [activeRequest, isOpen, sessionId]);

  useEffect(() => {
    if (!isOpen || !sessionId) return;

    const interval = setInterval(() => {
      pollChatOnce();
    }, CHAT_POLL_INTERVAL_MS);

    return () => clearInterval(interval);
  }, [sessionId, isOpen, pollChatOnce]);

  useEffect(() => {
    return () => {
      if (quickPollTimeoutRef.current !== null) {
        window.clearTimeout(quickPollTimeoutRef.current);
      }
    };
  }, []);

  const handleResolveReview = async (
    action: 'confirm' | 'revise' | 'continue_automatically' | 'cancel',
    options: { feedback?: string | null; editedIntent?: Record<string, unknown> | null } = {}
  ) => {
    if (!activeRequest || !pendingInteraction || isResolvingReview) return;
    const scope = currentSessionScopeRef.current;
    const decision = {
      interactionId: pendingInteraction.interaction_id,
      action,
      artifactDigest: pendingInteraction.artifact_digest,
      answers: reviewAnswers,
      feedback: options.feedback ?? null,
      editedIntent: options.editedIntent ?? null
    };
    const fingerprint = JSON.stringify(decision);
    if (reviewDecisionRef.current?.fingerprint !== fingerprint) {
      reviewDecisionRef.current = {
        fingerprint,
        idempotencyKey: `resolve-${pendingInteraction.interaction_id}-${Date.now()}-${Math.random().toString(16).slice(2)}`
      };
    }
    const idempotencyKey = reviewDecisionRef.current.idempotencyKey;
    setIsResolvingReview(true);
    setError(null);
    try {
      const result = await resolveGenerationInteraction(
        scope.sessionId,
        activeRequest.request_id,
        pendingInteraction.interaction_id,
        {
          action,
          artifact_digest: pendingInteraction.artifact_digest,
          answers: reviewAnswers,
          feedback: options.feedback ?? null,
          edited_intent: options.editedIntent ?? null
        },
        idempotencyKey
      );
      if (!isCurrentSessionRequest(result.request, scope, currentSessionScopeRef.current)) return;
      reviewDecisionRef.current = null;
      setActiveRequest(result.request);
      setIsProcessing(BUSY_REQUEST_STATUSES.includes(result.request.status));
      setReviewAnswers({});
      if (pendingInteraction.kind === 'structure_review' && action === 'revise') {
        retainedStructureReviewRef.current = {
          sessionId: scope.sessionId,
          requestId: activeRequest.request_id,
          interaction: pendingInteraction
        };
        onPendingStructureChange?.(pendingInteraction, 'revising', {
          sessionId: scope.sessionId,
          requestId: activeRequest.request_id
        });
      } else if (pendingInteraction.kind === 'structure_review' && action === 'cancel') {
        retainedStructureReviewRef.current = null;
        onPendingStructureChange?.(null, 'clear', {
          sessionId: scope.sessionId,
          requestId: activeRequest.request_id
        });
      }
      if (action !== 'cancel') {
        setInput('');
      }
      await refreshMessages(scope);
      if (!isCurrentSessionScope(scope, currentSessionScopeRef.current)) return;
      if (quickPollTimeoutRef.current !== null) {
        window.clearTimeout(quickPollTimeoutRef.current);
      }
      quickPollTimeoutRef.current = window.setTimeout(() => {
        pollChatOnce(result.request);
      }, 500);
    } catch (err: any) {
      if (isCurrentSessionScope(scope, currentSessionScopeRef.current)) {
        setError(err.message || 'Failed to continue generation.');
      }
    } finally {
      if (isCurrentSessionScope(scope, currentSessionScopeRef.current)) {
        setIsResolvingReview(false);
      }
    }
  };

  const handleSend = async () => {
    if (!sessionId) {
      setError('Start or select a design before sending a message.');
      return;
    }
    if (!input.trim() || isProcessing || isResolvingReview) return;

    const scope = currentSessionScopeRef.current;
    const content = input.trim();
    if (isWaitingForReview) {
      await handleResolveReview('revise', { feedback: content });
      return;
    }
    const submission = {
      sessionId,
      content,
      activeProjectId,
      includeProjectContext: includeProjectContext && !!activeProjectId,
      generationMode
    };
    const fingerprint = JSON.stringify(submission);
    if (chatSubmissionRef.current?.fingerprint !== fingerprint) {
      chatSubmissionRef.current = {
        fingerprint,
        idempotencyKey: `chat-${sessionId}-${Date.now()}-${Math.random().toString(16).slice(2)}`
      };
    }
    const idempotencyKey = chatSubmissionRef.current.idempotencyKey;
    setError(null);
    setIsProcessing(true);

    try {
      const result = await submitSessionChat(
        scope.sessionId,
        content,
        activeProjectId,
        includeProjectContext && !!activeProjectId,
        generationMode,
        idempotencyKey
      );
      if (!isCurrentSessionRequest(result.request, scope, currentSessionScopeRef.current)) return;
      chatSubmissionRef.current = null;
      setInput('');
      setActiveRequest(result.request);
      setLatestRequestRef({ sessionId: scope.sessionId, requestId: result.request.request_id });
      await refreshMessages(scope);
      if (!isCurrentSessionScope(scope, currentSessionScopeRef.current)) return;
      if (quickPollTimeoutRef.current !== null) {
        window.clearTimeout(quickPollTimeoutRef.current);
      }
      quickPollTimeoutRef.current = window.setTimeout(() => {
        pollChatOnce(result.request);
      }, 1000);
    } catch (err: any) {
      if (isCurrentSessionScope(scope, currentSessionScopeRef.current)) {
        setIsProcessing(false);
        // Keep both the draft and idempotency key so Retry cannot create a
        // duplicate request after a transient response failure.
        setInput(content);
        setError(err.message || 'Failed to submit chat request.');
      }
    }
  };

  const handleWithdraw = async () => {
    if (!activeRequest || activeRequest.status !== 'queued') return;
    const scope = currentSessionScopeRef.current;
    try {
      const result = await cancelQueuedRequest(scope.sessionId, activeRequest.request_id);
      if (!isCurrentSessionRequest(result.request, scope, currentSessionScopeRef.current)) return;
      setActiveRequest(result.request);
      setIsProcessing(false);
      await refreshMessages(scope);
    } catch (err: any) {
      if (isCurrentSessionScope(scope, currentSessionScopeRef.current)) {
        setError(err.message || 'Failed to withdraw queued request.');
      }
    }
  };

  const displayActivities = useMemo<DisplayActivity[]>(
    () => deriveActivityTimelineStates(progressEvents.map(normalizeProgressEvent)),
    [progressEvents]
  );
  const latestActivity: DisplayActivity = displayActivities[displayActivities.length - 1] || {
    id: 0,
    activityKey: 'starting_generation',
    state: 'progress',
    title: activeRequest?.status === 'waiting_for_user'
      ? 'Review needed'
      : activeRequest?.status === 'queued'
        ? 'Waiting to start'
        : 'Starting generation',
    fileChanges: [],
    requestId: progressRequestId || '',
    createdAt: activeRequest?.started_at || ''
  };
  const earlierActivities = displayActivities.slice(0, -1);
  const showGenerationActivity = shouldShowGenerationActivity({
    requestId: progressRequestId,
    isProcessing,
    activityCount: displayActivities.length,
    isWaitingForReview
  });
  const generationActivityLabel = isProcessing
      ? 'Generation progress'
    : latestActivity.state === 'failed'
      ? 'Generation needs attention'
      : activeRequest?.status === 'completed'
        ? 'Generation complete'
        : activeRequest?.status === 'failed'
          ? 'Generation stopped'
          : 'Generation activity';
  const displayedRequestStatus = requestStatusLabel(activeRequest?.status);
  const reviewPayload = (pendingInteraction?.payload || pendingInteraction?.artifact || {}) as Record<string, unknown>;
  const reviewQuestions = reviewQuestionsFrom(pendingInteraction, reviewPayload);
  const missingRequiredAnswer = reviewQuestions.some(question => (
    question.required && !reviewAnswers[question.question_id]
  ));
  const reviewSummary = stringValue(reviewPayload.summary)
    || stringValue(reviewPayload.goal)
    || pendingInteraction?.description
    || pendingInteraction?.prompt
    || '';
  const reviewAssumptions = stringList(reviewPayload.assumptions);
  const reviewEntities = stringList(reviewPayload.entities);
  const reviewEventFlow = stringList(reviewPayload.event_flow);
  const reviewParameters = stringList(reviewPayload.parameters);
  const reviewMetrics = stringList(reviewPayload.metrics);
  const reviewComponents = Array.isArray(reviewPayload.components)
    ? reviewPayload.components.filter(component => component && typeof component === 'object') as Array<Record<string, unknown>>
    : [];
  const omittedConnectionCount = Math.max(
    0,
    Number(reviewPayload.omitted_connection_count)
      || Number(reviewPayload.omitted_coupling_count)
      || 0
  );
  const truncatedComponentCount = Math.max(0, Number(reviewPayload.truncated_component_count) || 0);
  const structureReviewIncomplete = pendingInteraction?.kind === 'structure_review'
    && (reviewPayload.is_complete === false || omittedConnectionCount > 0 || truncatedComponentCount > 0);
  const priorRevisionFeedback = [...(activeRequest?.interactions || [])]
    .reverse()
    .find(interaction => (
      interaction.kind === pendingInteraction?.kind
      && interaction.resolution?.action === 'revise'
      && typeof interaction.resolution?.feedback === 'string'
      && interaction.resolution.feedback.trim()
    ))?.resolution?.feedback || '';
  const pendingReviewPresentation = reviewPresentation(pendingInteraction?.kind);

  if (!isOpen) return null;

  const displayMessages = messages.length > 0
    ? messages
    : [{
      id: 'init',
      role: 'assistant' as const,
      content: sessionId
        ? 'Describe the simulation you want to build. I can generate it, explain its structure, and help revise it.'
        : 'Start a new simulation design to begin.',
      timestamp: Date.now()
    }];

  return (
    <div className="flex h-full w-full flex-col bg-white">
      <div className="flex min-h-[52px] items-center justify-between gap-2 border-b border-slate-100 bg-white px-3 py-2 pr-10">
        <div className="flex min-w-0 items-center gap-2">
          {onBack && <button onClick={onBack} className="rounded p-1 text-slate-500 hover:bg-slate-100" title="Back to history"><ArrowLeft size={16} /></button>}
          <div className="flex h-7 w-7 items-center justify-center rounded bg-purple-50 text-purple-600"><Sparkles size={15} /></div>
          <div className="min-w-0">
            <div className="truncate text-sm font-semibold text-slate-800">{currentSessionTitle || 'Conversation'}</div>
            <div className="truncate text-[11px] text-slate-500">{displayedRequestStatus}</div>
          </div>
        </div>
        {onClose && (
          <button aria-label="Close conversation" onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X size={18} />
          </button>
        )}
      </div>

      <div
        ref={scrollContainerRef}
        onScroll={() => {
          const container = scrollContainerRef.current;
          if (!container) return;
          shouldAutoScrollRef.current = (
            container.scrollHeight - container.scrollTop - container.clientHeight < 80
          );
        }}
        className="flex-1 overflow-y-auto p-3 space-y-3 bg-slate-50/70"
      >
        {displayMessages.map((msg) => (
          <div key={msg.id} className={`flex gap-2 ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}>
            <div className={`w-6 h-6 rounded flex items-center justify-center flex-shrink-0 ${
              msg.role === 'user' ? 'bg-blue-100 text-blue-600' :
              msg.role === 'system' ? 'bg-amber-100 text-amber-600' : 'bg-purple-100 text-purple-600'
            }`}>
              {msg.role === 'user' ? <User size={14} /> : msg.role === 'system' ? <AlertTriangle size={14} /> : <Bot size={14} />}
            </div>
            <div className={`flex flex-col max-w-[85%] ${msg.role === 'user' ? 'items-end' : 'items-start'}`}>
              <div className={`px-3 py-2 rounded text-xs leading-5 whitespace-pre-wrap ${
                msg.role === 'user' ? 'bg-blue-600 text-white rounded-tr-none' :
                msg.role === 'system' ? 'bg-amber-50 border border-amber-200 text-amber-800 text-xs' :
                'bg-white border border-slate-200 text-slate-700 rounded-tl-none shadow-sm'
              } ${msg.status === 'withdrawn' ? 'opacity-60 italic' : ''}`}>
                {msg.content}
              </div>
              <span className="text-[10px] text-slate-400 mt-1">
                {new Date(msg.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
              </span>
            </div>
          </div>
        ))}

        {showGenerationActivity && (
          <div className="rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-slate-700 shadow-sm">
            <div className="flex items-center justify-between gap-2">
              <div className="flex min-w-0 items-center gap-2">
                <Activity size={14} className="shrink-0 text-purple-600" />
                <span className="text-xs font-semibold text-slate-800">{generationActivityLabel}</span>
                <span className="flex shrink-0 items-center gap-1 text-[10px] text-slate-400">
                  <Clock size={11} /> {formatElapsed(timerSeconds)}
                </span>
              </div>
              {activeRequest?.status === 'queued' && (
                <button
                  onClick={handleWithdraw}
                  className="flex shrink-0 items-center gap-1 rounded px-1.5 py-1 text-[11px] font-medium text-amber-700 hover:bg-amber-50 hover:text-amber-900"
                >
                  <Undo2 size={11} /> Withdraw
                </button>
              )}
            </div>

            <div role="status" aria-live="polite" className="mt-2 flex items-start gap-2 rounded bg-purple-50/70 px-2.5 py-2">
              <ActivityStateIcon state={latestActivity.state} active={latestActivity.state === 'started' || latestActivity.state === 'progress'} />
              <div className="min-w-0 flex-1">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="truncate text-xs font-medium text-slate-800">{latestActivity.title}</span>
                  {latestActivity.current !== undefined && latestActivity.total !== undefined && (
                    <span className="shrink-0 text-[10px] font-medium text-purple-700">
                      {latestActivity.current}/{latestActivity.total}
                    </span>
                  )}
                </div>
                {latestActivity.detail && (
                  <div className="mt-0.5 line-clamp-2 text-[11px] leading-4 text-slate-500">{latestActivity.detail}</div>
                )}
                <ActivityFileLinks activity={latestActivity} onSelect={onActivityFileSelect} />
                <div className="mt-0.5 text-[10px] text-slate-400">{formatLastUpdate(latestActivity.createdAt)}</div>
              </div>
            </div>

            {earlierActivities.length > 0 && (
              <details className="group mt-2 border-t border-slate-100 pt-1.5">
                <summary className="cursor-pointer select-none text-[11px] font-medium text-slate-500 hover:text-slate-700">
                  <span className="group-open:hidden">Show earlier steps ({earlierActivities.length})</span>
                  <span className="hidden group-open:inline">Hide earlier steps</span>
                </summary>
                <div className="mt-1.5 max-h-52 space-y-1.5 overflow-y-auto pr-1">
                  {earlierActivities.map((activity) => (
                    <div key={activity.id} className="flex gap-2 rounded bg-slate-50 px-2 py-1.5">
                      <ActivityStateIcon
                        state={activity.state}
                        active={activity.state === 'started' || activity.state === 'progress'}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-baseline justify-between gap-2">
                          <span className="text-[11px] font-medium leading-4 text-slate-700">{activity.title}</span>
                          <span className="shrink-0 text-[9px] text-slate-400">
                            {Number.isFinite(Date.parse(activity.createdAt))
                              ? new Date(activity.createdAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
                              : ''}
                          </span>
                        </div>
                        {activity.detail && <div className="text-[10px] leading-4 text-slate-500">{activity.detail}</div>}
                        <ActivityFileLinks activity={activity} onSelect={onActivityFileSelect} />
                        <div className="flex flex-wrap items-center gap-x-2 text-[9px] text-slate-400">
                          {activity.current !== undefined && activity.total !== undefined && (
                            <span>{activity.current} of {activity.total}</span>
                          )}
                          {activity.technicalName && <span>Technical step: {activity.technicalName}</span>}
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </details>
            )}

            {activityError && <div className="mt-1.5 text-[10px] text-amber-700">{activityError}</div>}
            {isProcessing && (
              <div className="mt-1.5 text-[10px] text-slate-400">Generation continues if you leave this page.</div>
            )}
          </div>
        )}

        {pendingInteraction && (
          <section className="rounded-lg border border-purple-200 bg-white shadow-sm" aria-label="Generation review">
            <div className="flex items-start gap-2 border-b border-purple-100 bg-purple-50/70 px-3 py-2.5">
              <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded bg-white text-purple-700 shadow-sm">
                {pendingInteraction.kind === 'structure_review' ? <Network size={14} /> : <Sparkles size={14} />}
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center justify-between gap-1.5">
                  <h2 className="text-xs font-semibold text-slate-900">
                    {pendingReviewPresentation.title}
                  </h2>
                  <span className="rounded-full bg-white px-2 py-0.5 text-[9px] font-semibold uppercase tracking-wide text-purple-700">
                    {pendingInteraction.kind === 'structure_review' ? 'Step 2 of 2' : 'Step 1 of 2'}
                  </span>
                </div>
                <p className="mt-0.5 text-[10px] leading-4 text-slate-500">
                  {pendingReviewPresentation.description}
                </p>
              </div>
            </div>

            <div className="space-y-2.5 px-3 py-3">
              {priorRevisionFeedback && (
                <div className="rounded border border-blue-100 bg-blue-50 px-2 py-1.5 text-[10px] leading-4 text-blue-800">
                  Updated after your note: “{priorRevisionFeedback}”
                </div>
              )}
              {reviewSummary && (
                <div>
                  <div className="text-[9px] font-semibold uppercase tracking-wide text-slate-400">
                    {pendingReviewPresentation.summaryLabel}
                  </div>
                  <p className="mt-1 text-[11px] leading-4 text-slate-700">{reviewSummary}</p>
                </div>
              )}

              {pendingInteraction.kind === 'intent_review' && (
                <>
                  {(stringValue(reviewPayload.root_model_name) || stringValue(reviewPayload.project_folder)) && (
                    <div className="grid grid-cols-2 gap-2">
                      {stringValue(reviewPayload.root_model_name) && (
                        <div className="rounded bg-slate-50 px-2 py-1.5">
                          <div className="text-[8px] font-semibold uppercase tracking-wide text-slate-400">Root model</div>
                          <div className="mt-0.5 truncate text-[10px] font-medium text-slate-700">{stringValue(reviewPayload.root_model_name)}</div>
                        </div>
                      )}
                      {stringValue(reviewPayload.project_folder) && (
                        <div className="rounded bg-slate-50 px-2 py-1.5">
                          <div className="text-[8px] font-semibold uppercase tracking-wide text-slate-400">Simulation folder</div>
                          <div className="mt-0.5 truncate text-[10px] font-medium text-slate-700">{stringValue(reviewPayload.project_folder)}</div>
                        </div>
                      )}
                    </div>
                  )}
                  {reviewEventFlow.length > 0 && (
                    <div>
                      <div className="text-[9px] font-semibold uppercase tracking-wide text-slate-400">Expected event flow</div>
                      <div className="mt-1 flex flex-wrap items-center gap-1 text-[9px] text-slate-600">
                        {reviewEventFlow.map((step, index) => (
                          <React.Fragment key={`${index}:${step}`}>
                            <span className="rounded bg-blue-50 px-2 py-1 text-blue-800">{step}</span>
                            {index < reviewEventFlow.length - 1 && <span aria-hidden="true" className="text-slate-300">→</span>}
                          </React.Fragment>
                        ))}
                      </div>
                    </div>
                  )}
                  {(reviewEntities.length > 0 || reviewParameters.length > 0 || reviewMetrics.length > 0) && (
                    <div className="grid gap-1.5 sm:grid-cols-3">
                      {([
                        ['Entities', reviewEntities],
                        ['Parameters', reviewParameters],
                        ['Measures', reviewMetrics]
                      ] as const).map(([label, values]) => values.length > 0 && (
                        <div key={label} className="rounded bg-slate-50 px-2 py-1.5">
                          <div className="text-[8px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
                          <div className="mt-0.5 line-clamp-3 text-[9px] leading-3.5 text-slate-600">{values.join(' · ')}</div>
                        </div>
                      ))}
                    </div>
                  )}
                  {reviewAssumptions.length > 0 && (
                    <details className="rounded border border-slate-100 bg-slate-50 px-2 py-1.5">
                      <summary className="cursor-pointer text-[10px] font-medium text-slate-600">Assumptions ({reviewAssumptions.length})</summary>
                      <ul className="mt-1 list-disc space-y-0.5 pl-4 text-[10px] leading-4 text-slate-500">
                        {reviewAssumptions.map((assumption, index) => <li key={`${index}:${assumption}`}>{assumption}</li>)}
                      </ul>
                    </details>
                  )}
                  {reviewQuestions.map(question => {
                    const choices = question.options || question.choices || [];
                    return (
                      <fieldset key={question.question_id} className="rounded border border-slate-200 p-2">
                        <legend className="px-1 text-[10px] font-medium text-slate-700">
                          {question.prompt}{question.required ? ' *' : ''}
                        </legend>
                        {choices.length > 0 ? (
                          <div className="mt-1 flex flex-wrap gap-1.5">
                            {choices.map(choice => {
                              const selected = reviewAnswers[question.question_id] === choice.value;
                              const recommended = choice.recommended || question.recommended_value === choice.value;
                              return (
                                <button
                                  key={choice.value}
                                  type="button"
                                  onClick={() => setReviewAnswers(existing => ({ ...existing, [question.question_id]: choice.value }))}
                                  title={choice.description}
                                  className={`rounded-full border px-2 py-1 text-[9px] font-medium ${selected ? 'border-purple-500 bg-purple-50 text-purple-800' : 'border-slate-200 bg-white text-slate-600 hover:border-purple-300'}`}
                                >
                                  {choice.label}{recommended ? ' · Recommended' : ''}
                                </button>
                              );
                            })}
                          </div>
                        ) : (
                          <input
                            value={reviewAnswers[question.question_id] || ''}
                            onChange={event => setReviewAnswers(existing => ({ ...existing, [question.question_id]: event.target.value }))}
                            className="mt-1 w-full rounded border border-slate-200 px-2 py-1.5 text-[10px] outline-none focus:border-purple-400"
                            placeholder="Your answer"
                          />
                        )}
                      </fieldset>
                    );
                  })}
                </>
              )}

              {pendingInteraction.kind === 'structure_review' && (
                <>
                  <div className="flex flex-wrap gap-1.5">
                    {reviewComponents.slice(0, 8).map((component, index) => (
                      <span
                        key={`${stringValue(component.id) || index}`}
                        title={reviewComponentResponsibility(component) || undefined}
                        className="rounded-full bg-slate-100 px-2 py-1 text-[9px] text-slate-700"
                      >
                        {stringValue(component.name) || stringValue(component.id) || `Component ${index + 1}`}
                        {stringValue(component.model_type) ? ` · ${stringValue(component.model_type)}` : ''}
                      </span>
                    ))}
                    {reviewComponents.length > 8 && <span className="px-1 py-1 text-[9px] text-slate-400">+{reviewComponents.length - 8} more</span>}
                  </div>
                  <div className="flex items-center justify-between rounded bg-slate-50 px-2 py-1.5 text-[10px] text-slate-600">
                    <span>{reviewComponents.length || Number(reviewPayload.component_count) || 0} components</span>
                    <span>{architectureConnectionLabel(reviewPayload)}</span>
                  </div>
                  {structureReviewIncomplete && (
                    <div className="flex gap-1.5 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-[9px] leading-4 text-amber-800">
                      <AlertTriangle size={12} className="mt-0.5 shrink-0" />
                      <span>
                        This preview is incomplete
                        {omittedConnectionCount > 0 ? `: ${omittedConnectionCount} planned connection${omittedConnectionCount === 1 ? '' : 's'} could not be drawn` : ''}
                        {truncatedComponentCount > 0 ? ` and ${truncatedComponentCount} component${truncatedComponentCount === 1 ? '' : 's'} could not be shown` : ''}.
                        {' '}Request a revision before approving this architecture.
                      </span>
                    </div>
                  )}
                  {onReviewStructure && (
                    <button
                      type="button"
                      onClick={onReviewStructure}
                      className="flex w-full items-center justify-center gap-1.5 rounded border border-blue-200 bg-blue-50 px-2 py-1.5 text-[10px] font-semibold text-blue-700 hover:bg-blue-100"
                    >
                      <Network size={12} /> Review components and responsibilities
                    </button>
                  )}
                </>
              )}

              <div className="flex flex-wrap gap-1.5 border-t border-slate-100 pt-2.5">
                <button
                  type="button"
                  onClick={() => handleResolveReview('confirm')}
                  disabled={isResolvingReview || missingRequiredAnswer || structureReviewIncomplete}
                  className="rounded bg-purple-700 px-2.5 py-1.5 text-[10px] font-semibold text-white hover:bg-purple-800 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {isResolvingReview ? 'Continuing…' : pendingReviewPresentation.primaryAction}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    focusWithoutDocumentScroll(textareaRef.current);
                  }}
                  disabled={isResolvingReview}
                  className="rounded border border-slate-200 px-2.5 py-1.5 text-[10px] font-medium text-slate-600 hover:bg-slate-50"
                >
                  {pendingReviewPresentation.secondaryAction}
                </button>
                {shouldShowContinueAutomatically(pendingInteraction.kind) && (
                  <button
                    type="button"
                    onClick={() => handleResolveReview('continue_automatically')}
                    disabled={isResolvingReview || missingRequiredAnswer}
                    className="ml-auto flex items-center gap-1 rounded px-2 py-1.5 text-[9px] font-medium text-slate-500 hover:bg-slate-50 hover:text-slate-700 disabled:opacity-50"
                    title="Accept this brief and skip the architecture review for this request"
                  >
                    <FastForward size={11} /> Continue automatically
                  </button>
                )}
              </div>
              {missingRequiredAnswer && <p className="text-[9px] text-amber-700">Answer the required question before continuing.</p>}
              {structureReviewIncomplete && <p className="text-[9px] text-amber-700">This architecture cannot be approved until every planned component can be shown.</p>}
            </div>
          </section>
        )}

        {error && (
          <div className="bg-amber-50 border border-amber-200 text-amber-800 text-xs rounded-lg px-3 py-2">
            {error}
          </div>
        )}
      </div>

      <div className="p-3 bg-white border-t border-slate-100">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <label className="flex min-w-0 items-center gap-2 text-xs text-slate-500">
            <input
              type="checkbox"
              checked={includeProjectContext && !!activeProjectId}
              onChange={(e) => setIncludeProjectContext(e.target.checked)}
              disabled={!activeProjectId || isProcessing || isWaitingForReview}
              className="h-3.5 w-3.5 rounded border-slate-300 text-purple-600 focus:ring-purple-500"
            />
            <span className="truncate">{activeProjectId ? `Include selected simulation: ${currentProjectName}` : 'No simulation selected'}</span>
          </label>
          <div className="flex shrink-0 flex-col items-end gap-0.5">
            <div className="flex items-center rounded-md border border-slate-200 bg-slate-50 p-0.5" aria-label="Generation mode">
              {(['guided', 'automatic'] as GenerationMode[]).map(mode => (
                <button
                  key={mode}
                  type="button"
                  onClick={() => setGenerationMode(mode)}
                  disabled={Boolean(activeRequest && !TERMINAL_REQUEST_STATUSES.includes(activeRequest.status))}
                  title={mode === 'guided'
                    ? 'Pause to review the simulation brief and proposed model architecture'
                    : 'Generate without review pauses'}
                  className={`rounded px-2 py-1 text-[9px] font-semibold capitalize ${generationMode === mode ? 'bg-white text-purple-700 shadow-sm' : 'text-slate-500 hover:text-slate-700'} disabled:cursor-not-allowed`}
                >
                  {mode === 'guided' ? 'Interactive' : 'Automatic'}
                </button>
              ))}
            </div>
            <span className="text-[9px] leading-none text-slate-400">{generationModeCaption(generationMode)}</span>
          </div>
        </div>
        <div className="relative bg-white border border-slate-200 rounded-xl shadow-sm focus-within:ring-2 focus-within:ring-purple-100 focus-within:border-purple-300 transition-all">
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                handleSend();
              }
            }}
            placeholder={!sessionId
              ? 'Start a new design first…'
              : isWaitingForReview
                ? 'Describe what should change in this proposal…'
                : 'Describe or revise a simulation…'}
            className="w-full pl-3 pr-12 py-2.5 text-xs bg-transparent border-none focus:ring-0 outline-none resize-none overflow-y-auto disabled:opacity-50"
            style={{ minHeight: '42px', maxHeight: '140px' }}
            rows={1}
            disabled={!sessionId || isProcessing || isResolvingReview}
          />
          <div className="absolute right-2 bottom-2">
            <button
              onClick={handleSend}
              disabled={!sessionId || !input.trim() || isProcessing || isResolvingReview}
              className="p-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {isProcessing || isResolvingReview ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
            </button>
          </div>
        </div>
        <div className="text-[10px] text-slate-400 mt-2 text-center flex justify-between px-2">
          <span>{currentProjectName ? `Selected simulation: ${currentProjectName}` : 'No simulation selected'}</span>
          {(isProcessing || isWaitingForReview) && <span>{displayedRequestStatus}</span>}
        </div>
      </div>
    </div>
  );
};
