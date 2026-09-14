import assert from 'node:assert/strict';
import test from 'node:test';

import {
  groupFeedbackHistory,
  resultRatingDraftMatches
} from './evaluationPresentationService.js';

test('loads the latest result and groups older feedback newest first', () => {
  const evaluations = [
    { evaluation_id: 'result-1', stage: 'final', project_id: 'project-1', request_id: 'request-1', created_at: '2026-01-01T01:00:00Z' },
    { evaluation_id: 'review-old', stage: 'intent', project_id: null, request_id: 'request-1', created_at: '2026-01-01T01:30:00Z' },
    { evaluation_id: 'intent-1', stage: 'intent', project_id: null, request_id: 'request-2', created_at: '2026-01-01T02:00:00Z' },
    { evaluation_id: 'result-other', stage: 'final', project_id: 'project-2', created_at: '2026-01-01T03:00:00Z' },
    { evaluation_id: 'result-2', stage: 'final', project_id: 'project-1', request_id: 'request-2', created_at: '2026-01-01T04:00:00Z' },
    { evaluation_id: 'structure-1', stage: 'structure', project_id: 'project-1', request_id: 'request-2', created_at: '2026-01-01T05:00:00Z' },
    { evaluation_id: 'result-3', stage: 'final', project_id: 'project-1', request_id: 'request-2', created_at: '2026-01-01T06:00:00Z' }
  ];

  const grouped = groupFeedbackHistory(evaluations, 'project-1');

  assert.equal(grouped.latestResult.evaluation_id, 'result-3');
  assert.deepEqual(
    grouped.earlierResultRatings.map(evaluation => evaluation.evaluation_id),
    ['result-2', 'result-1']
  );
  assert.deepEqual(
    grouped.reviewFeedback.map(evaluation => evaluation.evaluation_id),
    ['structure-1', 'intent-1']
  );
});

test('detects unchanged partial result feedback without filling omitted scores', () => {
  const evaluation = {
    scores: { overall: 5 },
    comments: 'Useful result'
  };
  const matchingDraft = {
    overall: '5',
    correctness: '',
    completeness: '',
    runnability: '',
    clarity: ''
  };

  assert.equal(resultRatingDraftMatches(matchingDraft, ' Useful result ', evaluation), true);
  assert.equal(resultRatingDraftMatches({ ...matchingDraft, overall: '4' }, 'Useful result', evaluation), false);
  assert.equal(resultRatingDraftMatches(matchingDraft, 'Different note', evaluation), false);
  assert.equal(resultRatingDraftMatches(matchingDraft, 'Useful result', null), false);
});
