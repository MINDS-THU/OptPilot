import React, { useState } from 'react';
import { Check, Pencil, Plus, RefreshCw, Rows3, Trash2, X } from 'lucide-react';
import { SessionInfo } from '../types';

interface Props {
  sessions: SessionInfo[];
  currentSessionId: string;
  onCreateSession: () => void;
  onRefreshSessions: () => void;
  onSelectSessionId: (sessionId: string) => void;
  onRenameSession: (sessionId: string, title: string) => Promise<void> | void;
  onDeleteSession: (session: SessionInfo) => Promise<void> | void;
}

export const SessionSelectorPanel: React.FC<Props> = ({
  sessions,
  currentSessionId,
  onCreateSession,
  onRefreshSessions,
  onSelectSessionId,
  onRenameSession,
  onDeleteSession
}) => {
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const activeStatuses: SessionInfo['status'][] = ['queued', 'running', 'waiting_for_user', 'cancelling'];
  const statusLabels: Record<SessionInfo['status'], string> = {
    idle: 'Ready',
    queued: 'Waiting',
    running: 'Building',
    waiting_for_user: 'Review needed',
    cancelling: 'Stopping',
    failed: 'Needs attention'
  };

  const formatTime = (value: string) => {
    const timestamp = Date.parse(value);
    if (!Number.isFinite(timestamp)) return 'unknown';
    return new Date(timestamp).toLocaleString([], {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit'
    });
  };

  const startEditing = (session: SessionInfo) => {
    setEditingSessionId(session.session_id);
    setDraftTitle(session.title || session.session_id);
  };

  const cancelEditing = () => {
    setEditingSessionId(null);
    setDraftTitle('');
  };

  const submitRename = async () => {
    const title = draftTitle.trim();
    if (!editingSessionId || !title) return;
    await onRenameSession(editingSessionId, title);
    cancelEditing();
  };

  return (
    <aside className="flex h-full w-full flex-col border-r border-slate-200 bg-white">
      <div className="border-b border-slate-100 px-4 py-4 pr-10">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-sm font-semibold text-slate-800">
            <Rows3 size={16} />
            History
          </div>
          <button
            onClick={onRefreshSessions}
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-blue-600"
            title="Refresh history"
          >
            <RefreshCw size={14} />
          </button>
        </div>
      </div>

      <div className="flex flex-1 flex-col gap-3 overflow-y-auto p-4">
        <button
          onClick={onCreateSession}
          className="flex w-full items-center justify-center gap-2 rounded border border-blue-200 bg-blue-50 px-3 py-2 text-sm font-medium text-blue-700 hover:bg-blue-100"
          title="Start a new simulation design"
        >
          <Plus size={15} />
          New simulation
        </button>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold uppercase text-slate-500">Design history</span>
            <span className="text-[10px] text-slate-400">Recent first</span>
          </div>
          {sessions.length > 0 ? sessions.map(session => {
            const selected = session.session_id === currentSessionId;
            const editing = editingSessionId === session.session_id;
            return (
              <div
                key={session.session_id}
                onClick={() => {
                  if (!editing) onSelectSessionId(session.session_id);
                }}
                className={`w-full rounded border px-3 py-3 text-left text-xs transition-colors ${
                  selected
                    ? 'cursor-default border-blue-300 bg-blue-50 shadow-sm'
                    : 'cursor-pointer border-slate-200 bg-slate-50 hover:border-slate-300 hover:bg-white'
                }`}
              >
                <div className="flex items-start justify-between gap-2">
                  {editing ? (
                    <input
                      value={draftTitle}
                      onChange={event => setDraftTitle(event.target.value)}
                      onClick={event => event.stopPropagation()}
                      onKeyDown={event => {
                        if (event.key === 'Enter') submitRename();
                        if (event.key === 'Escape') cancelEditing();
                      }}
                      autoFocus
                      className="min-w-0 flex-1 rounded border border-blue-300 bg-white px-2 py-1 text-xs font-medium text-slate-800 outline-none focus:ring-2 focus:ring-blue-200"
                    />
                  ) : (
                    <button
                      onClick={event => {
                        event.stopPropagation();
                        onSelectSessionId(session.session_id);
                      }}
                      className={`min-w-0 flex-1 truncate text-left font-medium ${selected ? 'text-blue-900' : 'text-slate-800'}`}
                      title={session.title || session.session_id}
                    >
                      {session.title || session.session_id}
                    </button>
                  )}

                  <div className="flex flex-shrink-0 items-center gap-1">
                    {editing ? (
                      <>
                        <button
                          onClick={event => {
                            event.stopPropagation();
                            submitRename();
                          }}
                          className="rounded p-1 text-slate-500 hover:bg-white hover:text-blue-600"
                          title="Save design name"
                        >
                          <Check size={13} />
                        </button>
                        <button
                          onClick={event => {
                            event.stopPropagation();
                            cancelEditing();
                          }}
                          className="rounded p-1 text-slate-500 hover:bg-white hover:text-slate-700"
                          title="Cancel Rename"
                        >
                          <X size={13} />
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          onClick={event => {
                            event.stopPropagation();
                            startEditing(session);
                          }}
                          className="rounded p-1 text-slate-400 hover:bg-white hover:text-blue-600"
                          title="Rename design"
                        >
                          <Pencil size={12} />
                        </button>
                        <button
                          onClick={event => {
                            event.stopPropagation();
                            onDeleteSession(session);
                          }}
                          disabled={activeStatuses.includes(session.status)}
                          className="rounded p-1 text-slate-400 hover:bg-white hover:text-red-600 disabled:cursor-not-allowed disabled:opacity-30"
                          title={activeStatuses.includes(session.status) ? 'Cannot delete an active design' : 'Delete design'}
                        >
                          <Trash2 size={12} />
                        </button>
                      </>
                    )}
                    <span className={`rounded px-1.5 py-0.5 text-[10px] ${
                      session.status === 'idle'
                        ? 'bg-slate-200 text-slate-600'
                        : session.status === 'failed'
                          ? 'bg-red-100 text-red-700'
                        : 'bg-amber-100 text-amber-700'
                    }`}>
                      {statusLabels[session.status]}
                    </span>
                  </div>
                </div>
                <div className="mt-2 grid grid-cols-2 gap-2 text-slate-500">
                  <div>Simulations: {session.project_count}</div>
                  <div>Updated: {formatTime(session.updated_at)}</div>
                </div>
              </div>
            );
          }) : (
            <div className="rounded border border-slate-200 bg-slate-50 px-3 py-3 text-xs text-slate-600">
              Start a new simulation, then describe what you want the agent to build.
            </div>
          )}
        </div>
      </div>
    </aside>
  );
};
