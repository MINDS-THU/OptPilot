const REVIEW_PRESENTATION = {
  intent_review: {
    title: 'Review the simulation brief',
    description: 'Confirm how the generator understood your request before it designs the model.',
    summaryLabel: 'What I understood',
    primaryAction: 'Continue to architecture',
    secondaryAction: 'Change the brief'
  },
  structure_review: {
    title: 'Review the model architecture',
    description: 'No model code has been generated. Confirm the components and nesting; interfaces and connections are refined next.',
    summaryLabel: 'Proposed architecture',
    primaryAction: 'Approve architecture and generate',
    secondaryAction: 'Request a change'
  }
};

/**
 * Translate request protocol states into concise labels for students.
 * Protocol values remain unchanged on the wire.
 *
 * @param {string | null | undefined} status
 * @returns {string}
 */
export const requestStatusLabel = status => ({
  waiting_for_user: 'Review needed',
  queued: 'Waiting to start',
  running: 'Generating',
  cancelling: 'Stopping',
  completed: 'Complete',
  failed: 'Needs attention',
  cancelled: 'Stopped'
}[status || ''] || 'Ready');

/**
 * A review checkpoint is a stable pause, not active generation. The dedicated
 * review card is the single source of truth while the request waits.
 *
 * @param {{requestId?: string | null, isProcessing: boolean, activityCount: number, isWaitingForReview: boolean}} state
 * @returns {boolean}
 */
export const shouldShowGenerationActivity = state => Boolean(
  state.requestId
  && !state.isWaitingForReview
  && (state.isProcessing || state.activityCount > 0)
);

/**
 * @param {'intent_review' | 'structure_review' | string | null | undefined} kind
 */
export const reviewPresentation = kind => (
  REVIEW_PRESENTATION[kind] || REVIEW_PRESENTATION.intent_review
);

/**
 * Skipping later reviews is meaningful at the first checkpoint only. At the
 * architecture checkpoint it would be a confusing duplicate of approval.
 *
 * @param {string | null | undefined} kind
 */
export const shouldShowContinueAutomatically = kind => kind === 'intent_review';

/** @param {'guided' | 'automatic' | string} mode */
export const generationModeCaption = mode => (
  mode === 'automatic' ? 'Runs without review pauses' : 'Pauses for two reviews'
);

/**
 * @param {'awaiting_review' | 'approved_building' | null | undefined} state
 */
export const architectureStateLabel = state => (
  state === 'approved_building'
    ? 'Approved architecture · Building'
    : 'Proposed architecture · awaiting review'
);

/**
 * Recover the architecture approved for the active build from durable request
 * history. Automatic generation resolves both checkpoints without ever
 * exposing pending_interaction, so pending state alone is insufficient.
 *
 * Deliberately return nothing outside an active build. In particular, a
 * completed request must not reopen the architecture projection over a
 * simulation the student has selected from the completed project list.
 *
 * @param {{phase?: string, status?: string, interactions?: unknown[]} | null | undefined} request
 * @returns {Record<string, unknown> | null}
 */
export const latestApprovedArchitectureInteraction = request => {
  if (
    !request
    || request.phase !== 'build'
    || (request.status !== 'queued' && request.status !== 'running')
    || !Array.isArray(request.interactions)
  ) return null;

  for (let index = request.interactions.length - 1; index >= 0; index -= 1) {
    const candidate = request.interactions[index];
    if (!candidate || typeof candidate !== 'object') continue;
    const interaction = /** @type {Record<string, any>} */ (candidate);
    const action = interaction.resolution?.action;
    if (
      interaction.kind === 'structure_review'
      && interaction.status === 'resolved'
      && (action === 'confirm' || action === 'continue_automatically')
      && interaction.payload
      && typeof interaction.payload === 'object'
    ) return interaction;
  }
  return null;
};

/**
 * A hierarchy-only checkpoint intentionally has no final coupling count yet.
 * Avoid presenting that as a misleading zero.
 *
 * @param {Record<string, unknown> | null | undefined} payload
 * @returns {string}
 */
export const architectureConnectionLabel = payload => {
  if (
    payload?.connections_defined === false
    || payload?.review_scope === 'component_hierarchy'
  ) return 'Connections refined after approval';

  const connections = Array.isArray(payload?.connections) ? payload.connections : [];
  return `${connections.length} connection${connections.length === 1 ? '' : 's'}`;
};

/**
 * Architecture projection lifetime is tied to its session/request, not to
 * whether the Conversation panel happens to be mounted or visible.
 *
 * @param {{sessionId: string, requestId: string} | null | undefined} owner
 * @param {{sessionId: string, activeRequestId: string | null | undefined}} current
 * @returns {boolean}
 */
export const shouldClearArchitectureProjection = (owner, current) => Boolean(
  owner
  && (
    owner.sessionId !== current.sessionId
    || (
      current.activeRequestId
      && owner.requestId !== current.activeRequestId
    )
  )
);

/**
 * Describe the one terminal side effect owned by the persistent application
 * shell. Conversation may be collapsed or unmounted, so it must not own the
 * completed-build handoff.
 *
 * @param {{sessionId: string, requestId: string} | null | undefined} owner
 * @param {{session_id?: string, request_id?: string, status?: string, updated_project_ids?: unknown[], updated_project_names?: unknown[]} | null | undefined} request
 * @returns {{state: 'finalizing' | 'build_stopped' | 'clear', updatedProjects: string[]} | null}
 */
export const architectureTerminalHandoff = (owner, request) => {
  if (
    !owner
    || !request
    || request.session_id !== owner.sessionId
    || request.request_id !== owner.requestId
  ) return null;

  if (request.status === 'failed' || request.status === 'cancelled') {
    return { state: 'build_stopped', updatedProjects: [] };
  }
  if (request.status !== 'completed') return null;

  const ids = Array.isArray(request.updated_project_ids)
    ? request.updated_project_ids.filter(value => typeof value === 'string')
    : [];
  const names = Array.isArray(request.updated_project_names)
    ? request.updated_project_names.filter(value => typeof value === 'string')
    : [];
  const updatedProjects = ids.length > 0 ? ids : names;
  return {
    state: updatedProjects.length > 0 ? 'finalizing' : 'clear',
    updatedProjects
  };
};

/**
 * Preserve the responsibility already declared for a planned component so the
 * architecture review explains what the component is for, not only its name.
 *
 * @param {Record<string, unknown> | null | undefined} component
 * @returns {string}
 */
export const reviewComponentResponsibility = component => {
  const value = component?.description ?? component?.responsibility;
  if (typeof value !== 'string') return '';
  return value.replace(/\s+/g, ' ').trim().slice(0, 600);
};
