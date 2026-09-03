const RESULT_RATING_KEYS = [
  'overall',
  'correctness',
  'completeness',
  'runnability',
  'clarity'
];

const normalizedScore = value => {
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  if (typeof value === 'string' && /^[1-5]$/.test(value)) return value;
  return '';
};

const newestFirst = evaluations => [...evaluations].sort((left, right) => (
  (Date.parse(right?.created_at || '') || 0) - (Date.parse(left?.created_at || '') || 0)
));

/**
 * Keep the latest result submission in the editable form. Older result
 * submissions and checkpoint feedback remain available as two clear groups.
 *
 * @param {Array<Record<string, any>>} evaluations
 * @param {string | null | undefined} projectId
 */
export const groupFeedbackHistory = (evaluations, projectId) => {
  const resultRatings = newestFirst((evaluations || []).filter(evaluation => (
    evaluation?.stage === 'final' && evaluation?.project_id === projectId
  )));
  const latestResult = resultRatings[0] || null;
  const reviewFeedback = newestFirst((evaluations || []).filter(evaluation => (
    (evaluation?.stage === 'intent' || evaluation?.stage === 'structure')
    && (!evaluation?.project_id || evaluation.project_id === projectId)
    && (!latestResult?.request_id || evaluation.request_id === latestResult.request_id)
  )));

  return {
    latestResult,
    reviewFeedback,
    earlierResultRatings: resultRatings.slice(1)
  };
};

/**
 * @param {Record<string, unknown>} scores
 * @param {string} comments
 * @param {Record<string, any> | null | undefined} evaluation
 */
export const resultRatingDraftMatches = (scores, comments, evaluation) => Boolean(
  evaluation
  && RESULT_RATING_KEYS.every(key => (
    normalizedScore(scores?.[key]) === normalizedScore(evaluation.scores?.[key])
  ))
  && String(comments || '').trim() === String(evaluation.comments || '').trim()
);
