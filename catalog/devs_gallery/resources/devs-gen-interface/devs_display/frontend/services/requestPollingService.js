/**
 * Decide whether an activity response means the canonical request details
 * should be refreshed immediately.
 *
 * Activity and request-detail polling are deliberately separate: activity is
 * frequent and incremental, while request details contain the pending guided
 * interaction.  A status transition or interaction event must bridge those
 * streams so the review UI does not wait on (or depend on) the chat poll.
 *
 * @param {{request_id?: string, status?: string} | null} currentRequest
 * @param {{request_status?: string, events?: Array<{request_id?: string, type?: string}>} | null} response
 * @param {string} requestId
 * @returns {boolean}
 */
export const shouldRefreshRequestFromActivity = (
  currentRequest,
  response,
  requestId
) => {
  if (!requestId || !response) return false;
  if (!currentRequest || currentRequest.request_id !== requestId) return true;

  if (
    typeof response.request_status === 'string'
    && response.request_status
    && response.request_status !== currentRequest.status
  ) {
    return true;
  }

  return Array.isArray(response.events) && response.events.some(event => (
    event?.request_id === requestId
    && event.type === 'interaction_required'
  ));
};
