/**
 * A session view can be selected more than once while older network requests
 * are still in flight. The revision distinguishes those separate visits even
 * when they happen to use the same session id.
 *
 * @typedef {{sessionId: string, revision: number}} SessionScope
 */

/**
 * @param {SessionScope | null | undefined} started
 * @param {SessionScope | null | undefined} current
 * @returns {boolean}
 */
export const isCurrentSessionScope = (started, current) => Boolean(
  started
  && current
  && started.sessionId === current.sessionId
  && started.revision === current.revision
);

/**
 * Accept a request response only when both the async call and the returned
 * request belong to the session view that is still on screen.
 *
 * @param {{session_id?: string} | null | undefined} request
 * @param {SessionScope | null | undefined} started
 * @param {SessionScope | null | undefined} current
 * @returns {boolean}
 */
export const isCurrentSessionRequest = (request, started, current) => Boolean(
  isCurrentSessionScope(started, current)
  && request
  && request.session_id === started.sessionId
);

/**
 * Scope retained request state before deriving review or architecture UI.
 * This is a second line of defense: even if a stale response reached React
 * state, it remains invisible to a different session.
 *
 * @template T
 * @param {(T & {session_id?: string}) | null | undefined} request
 * @param {string} sessionId
 * @returns {(T & {session_id?: string}) | null}
 */
export const requestForSession = (request, sessionId) => (
  request?.session_id === sessionId ? request : null
);
