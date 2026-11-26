import assert from 'node:assert/strict';
import test from 'node:test';

import {
  architectureConnectionLabel,
  architectureStateLabel,
  architectureTerminalHandoff,
  generationModeCaption,
  latestApprovedArchitectureInteraction,
  requestStatusLabel,
  reviewComponentResponsibility,
  reviewPresentation,
  shouldClearArchitectureProjection,
  shouldShowContinueAutomatically,
  shouldShowGenerationActivity
} from './reviewPresentationService.js';

test('presents a waiting request as a review rather than active generation', () => {
  assert.equal(requestStatusLabel('waiting_for_user'), 'Review needed');
  assert.equal(shouldShowGenerationActivity({
    requestId: 'request-1',
    isProcessing: false,
    activityCount: 8,
    isWaitingForReview: true
  }), false);
});

test('continues to show progress before and after a review checkpoint', () => {
  assert.equal(shouldShowGenerationActivity({
    requestId: 'request-1',
    isProcessing: true,
    activityCount: 0,
    isWaitingForReview: false
  }), true);
  assert.equal(shouldShowGenerationActivity({
    requestId: 'request-1',
    isProcessing: false,
    activityCount: 8,
    isWaitingForReview: false
  }), true);
});

test('uses student-facing checkpoint actions and architecture explanation', () => {
  const intent = reviewPresentation('intent_review');
  const structure = reviewPresentation('structure_review');

  assert.equal(intent.primaryAction, 'Continue to architecture');
  assert.equal(structure.primaryAction, 'Approve architecture and generate');
  assert.match(structure.description, /components and nesting/);
  assert.match(structure.description, /interfaces and connections are refined next/);
  assert.equal(shouldShowContinueAutomatically('intent_review'), true);
  assert.equal(shouldShowContinueAutomatically('structure_review'), false);
});

test('explains mode behavior and retained architecture state in plain language', () => {
  assert.equal(generationModeCaption('guided'), 'Pauses for two reviews');
  assert.equal(generationModeCaption('automatic'), 'Runs without review pauses');
  assert.equal(
    architectureStateLabel('approved_building'),
    'Approved architecture · Building'
  );
});

test('recovers the latest approved architecture for either active build mode', () => {
  const older = {
    interaction_id: 'structure-1',
    kind: 'structure_review',
    status: 'resolved',
    resolution: { action: 'confirm' },
    payload: { title: 'First architecture' }
  };
  const automatic = {
    interaction_id: 'structure-2',
    kind: 'structure_review',
    status: 'resolved',
    resolution: { action: 'confirm', automatic: true },
    payload: { title: 'Revised architecture' }
  };

  assert.equal(latestApprovedArchitectureInteraction({
    phase: 'build',
    status: 'queued',
    interactions: [older, automatic]
  }), automatic);
  assert.equal(latestApprovedArchitectureInteraction({
    phase: 'build',
    status: 'running',
    interactions: [older]
  }), older);
});

test('does not restore an architecture after completion or from an unresolved review', () => {
  const resolved = {
    kind: 'structure_review',
    status: 'resolved',
    resolution: { action: 'continue_automatically' },
    payload: { title: 'Architecture' }
  };
  const open = { ...resolved, status: 'open' };

  assert.equal(latestApprovedArchitectureInteraction({
    phase: 'build',
    status: 'completed',
    interactions: [resolved]
  }), null);
  assert.equal(latestApprovedArchitectureInteraction({
    phase: 'plan_structure',
    status: 'running',
    interactions: [resolved]
  }), null);
  assert.equal(latestApprovedArchitectureInteraction({
    phase: 'build',
    status: 'running',
    interactions: [open]
  }), null);
});

test('describes hierarchy-only connections without a misleading zero', () => {
  assert.equal(
    architectureConnectionLabel({
      review_scope: 'component_hierarchy',
      connections_defined: false,
      connections: []
    }),
    'Connections refined after approval'
  );
  assert.equal(
    architectureConnectionLabel({ connections: [{ source: 'a', target: 'b' }] }),
    '1 connection'
  );
});

test('retains architecture when conversation view unmounts for the same active request', () => {
  const owner = { sessionId: 'session-1', requestId: 'request-1' };

  // Conversation visibility is deliberately absent from this lifecycle: Back
  // to history and panel collapse must not affect the result.
  assert.equal(shouldClearArchitectureProjection(owner, {
    sessionId: 'session-1',
    activeRequestId: 'request-1'
  }), false);
});

test('retains architecture across terminal handoff and clears it for another request or session', () => {
  const owner = { sessionId: 'session-1', requestId: 'request-1' };

  assert.equal(shouldClearArchitectureProjection(owner, {
    sessionId: 'session-1',
    activeRequestId: null
  }), false);
  assert.equal(shouldClearArchitectureProjection(owner, {
    sessionId: 'session-1',
    activeRequestId: 'request-2'
  }), true);
  assert.equal(shouldClearArchitectureProjection(owner, {
    sessionId: 'session-2',
    activeRequestId: 'request-2'
  }), true);
});

test('terminal architecture handoff is owned independently of Conversation visibility', () => {
  const owner = { sessionId: 'session-1', requestId: 'request-1' };

  assert.deepEqual(architectureTerminalHandoff(owner, {
    session_id: 'session-1',
    request_id: 'request-1',
    status: 'completed',
    updated_project_ids: ['project-1']
  }), {
    state: 'finalizing',
    updatedProjects: ['project-1']
  });
  assert.deepEqual(architectureTerminalHandoff(owner, {
    session_id: 'session-1',
    request_id: 'request-1',
    status: 'failed'
  }), {
    state: 'build_stopped',
    updatedProjects: []
  });
  assert.deepEqual(architectureTerminalHandoff(owner, {
    session_id: 'session-1',
    request_id: 'request-1',
    status: 'completed',
    updated_project_ids: []
  }), {
    state: 'clear',
    updatedProjects: []
  });
  assert.equal(architectureTerminalHandoff(owner, {
    session_id: 'session-1',
    request_id: 'another-request',
    status: 'completed',
    updated_project_ids: ['project-1']
  }), null);
});

test('preserves a planned component description as its review responsibility', () => {
  assert.equal(reviewComponentResponsibility({
    id: 'DiningArea',
    name: 'Dining Area',
    description: '  Seats arriving parties and coordinates available servers.  '
  }), 'Seats arriving parties and coordinates available servers.');
});
