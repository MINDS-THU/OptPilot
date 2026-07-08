import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, Bot, Clock, Loader2, Send, Sparkles, User, X, Undo2 } from 'lucide-react';
import { BackendMessage, ChatMessage, ChatRequestInfo } from '../types';
import {
  cancelQueuedRequest,
  getSessionMessages,
  getSessionRequest,
  submitSessionChat
} from '../services/agentService';

interface Props {
  sessionId: string;
  activeRequestId?: string | null;
  activeProjectId: string | null;
  currentProjectName: string | null;
  onProjectsUpdated: (updatedProjectIdsOrNames: string[]) => void;
  apiKey?: string;
  provider?: string;
  isOpen: boolean;
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
const TERMINAL_REQUEST_STATUSES = ['completed', 'failed', 'cancelled'];

const elapsedSecondsFromRequest = (request: ChatRequestInfo | null): number => {
  if (!request?.started_at) return 0;
  const startedAt = Date.parse(request.started_at);
  if (!Number.isFinite(startedAt)) return 0;
  return Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
};

export const ChatInterface: React.FC<Props> = ({
  sessionId,
  activeRequestId,
  activeProjectId,
  currentProjectName,
  onProjectsUpdated,
  isOpen,
  onClose
}) => {
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [activeRequest, setActiveRequest] = useState<ChatRequestInfo | null>(null);
  const [isProcessing, setIsProcessing] = useState(false);
  const [timerSeconds, setTimerSeconds] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [includeProjectContext, setIncludeProjectContext] = useState(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const completedRequestsRef = useRef<Set<string>>(new Set());
  const pollingInFlightRef = useRef(false);
  const quickPollTimeoutRef = useRef<number | null>(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  const refreshMessages = async () => {
    if (!sessionId) return;
    const backendMessages = await getSessionMessages(sessionId, 30);
    setMessages(backendMessages.map(toChatMessage));
  };

  useEffect(() => {
    if (!isOpen || !sessionId) return;
    setError(null);
    completedRequestsRef.current.clear();
    refreshMessages().catch(err => setError(err.message || 'Failed to load messages.'));
  }, [sessionId, isOpen]);

  useEffect(() => {
    if (!isOpen || !sessionId) {
      setActiveRequest(null);
      setIsProcessing(false);
      return;
    }

    if (!activeRequestId) {
      setActiveRequest(null);
      setIsProcessing(false);
      return;
    }

    getSessionRequest(sessionId, activeRequestId)
      .then(request => {
        setActiveRequest(request);
        setIsProcessing(!TERMINAL_REQUEST_STATUSES.includes(request.status));
      })
      .catch(err => setError(err.message || 'Failed to restore active request.'));
  }, [activeRequestId, sessionId, isOpen]);

  useEffect(() => {
    scrollToBottom();
  }, [messages, isOpen, activeRequest]);

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
    setTimerSeconds(0);
  }, [isProcessing, activeRequest?.request_id, activeRequest?.started_at]);

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
      const newHeight = Math.min(textareaRef.current.scrollHeight, 200);
      textareaRef.current.style.height = `${newHeight}px`;
    }
  }, [input]);

  const pollChatOnce = useCallback(async (requestToPoll: ChatRequestInfo | null = activeRequest) => {
    if (!isOpen || !sessionId || pollingInFlightRef.current) return;
    pollingInFlightRef.current = true;
    try {
      await refreshMessages();
      if (requestToPoll) {
        const request = await getSessionRequest(sessionId, requestToPoll.request_id);
        setActiveRequest(request);
        const terminal = TERMINAL_REQUEST_STATUSES.includes(request.status);
        setIsProcessing(!terminal);
        if (terminal && !completedRequestsRef.current.has(request.request_id)) {
          completedRequestsRef.current.add(request.request_id);
          const updated = request.updated_project_ids?.length
            ? request.updated_project_ids
            : (request.updated_project_names || []);
          if (updated.length > 0) onProjectsUpdated(updated);
        }
      }
    } catch (err: any) {
      setError(err.message || 'Failed to refresh chat.');
    } finally {
      pollingInFlightRef.current = false;
    }
  }, [activeRequest, isOpen, sessionId, onProjectsUpdated]);

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

  const handleSend = async () => {
    if (!sessionId) {
      setError('Create or select a session before sending a chat message.');
      return;
    }
    if (!input.trim() || isProcessing) return;

    const content = input.trim();
    setInput('');
    setError(null);
    setIsProcessing(true);

    try {
      const result = await submitSessionChat(sessionId, content, activeProjectId, includeProjectContext && !!activeProjectId);
      setActiveRequest(result.request);
      await refreshMessages();
      if (quickPollTimeoutRef.current !== null) {
        window.clearTimeout(quickPollTimeoutRef.current);
      }
      quickPollTimeoutRef.current = window.setTimeout(() => {
        pollChatOnce(result.request);
      }, 1000);
    } catch (err: any) {
      setIsProcessing(false);
      setError(err.message || 'Failed to submit chat request.');
    }
  };

  const handleWithdraw = async () => {
    if (!activeRequest || activeRequest.status !== 'queued') return;
    try {
      const result = await cancelQueuedRequest(sessionId, activeRequest.request_id);
      setActiveRequest(result.request);
      setIsProcessing(false);
      await refreshMessages();
    } catch (err: any) {
      setError(err.message || 'Failed to withdraw queued request.');
    }
  };

  if (!isOpen) return null;

  const displayMessages = messages.length > 0
    ? messages
    : [{
      id: 'init',
      role: 'assistant' as const,
      content: sessionId
        ? 'Hello! I can help you modify this session workspace or generate new projects.'
        : 'Create or select a session to start chatting.',
      timestamp: Date.now()
    }];

  return (
    <div className="flex h-full w-full flex-col bg-white">
      <div className="flex items-center justify-between p-4 border-b border-slate-100 bg-slate-50">
        <div className="flex items-center gap-2 text-slate-700 font-semibold">
          <Sparkles size={18} className="text-purple-600" />
          <span>Session Chat</span>
        </div>
        {onClose && (
          <button onClick={onClose} className="text-slate-400 hover:text-slate-600">
            <X size={18} />
          </button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-4 space-y-4 bg-slate-50/50">
        {displayMessages.map((msg) => (
          <div key={msg.id} className={`flex gap-3 ${msg.role === 'user' ? 'flex-row-reverse' : ''}`}>
            <div className={`w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 ${
              msg.role === 'user' ? 'bg-blue-100 text-blue-600' :
              msg.role === 'system' ? 'bg-amber-100 text-amber-600' : 'bg-purple-100 text-purple-600'
            }`}>
              {msg.role === 'user' ? <User size={14} /> : msg.role === 'system' ? <AlertTriangle size={14} /> : <Bot size={14} />}
            </div>
            <div className={`flex flex-col max-w-[85%] ${msg.role === 'user' ? 'items-end' : 'items-start'}`}>
              <div className={`px-4 py-2 rounded-lg text-sm whitespace-pre-wrap ${
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

        {isProcessing && (
          <div className="bg-white border border-slate-200 text-slate-600 rounded-lg px-4 py-3 text-sm shadow-sm">
            <div className="flex items-center justify-between gap-3">
              <div className="flex items-center gap-2 font-medium">
                <Loader2 size={14} className="animate-spin" />
                {activeRequest?.status === 'queued' ? 'Queued...' : 'Agent is working...'}
              </div>
              {activeRequest?.status === 'queued' && (
                <button
                  onClick={handleWithdraw}
                  className="flex items-center gap-1 text-xs text-amber-700 hover:text-amber-900"
                >
                  <Undo2 size={12} /> Withdraw
                </button>
              )}
            </div>
            <div className="text-xs text-slate-400 flex items-center gap-1 mt-2">
              <Clock size={12} /> {timerSeconds}s elapsed
            </div>
            <div className="text-[10px] text-slate-400 italic mt-1">
              Running requests cannot be force-stopped in this MVP.
            </div>
          </div>
        )}

        {error && (
          <div className="bg-amber-50 border border-amber-200 text-amber-800 text-xs rounded-lg px-3 py-2">
            {error}
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      <div className="p-4 bg-white border-t border-slate-100">
        <label className="mb-2 flex items-center gap-2 text-xs text-slate-500">
          <input
            type="checkbox"
            checked={includeProjectContext && !!activeProjectId}
            onChange={(e) => setIncludeProjectContext(e.target.checked)}
            disabled={!activeProjectId || isProcessing}
            className="h-3.5 w-3.5 rounded border-slate-300 text-purple-600 focus:ring-purple-500"
          />
          <span>{activeProjectId ? `Attach selected project: ${currentProjectName}` : 'No selected project to attach'}</span>
        </label>
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
            placeholder={sessionId ? 'Ask the agent to modify this session workspace...' : 'Select or create a session first...'}
            className="w-full pl-4 pr-12 py-3 text-sm bg-transparent border-none focus:ring-0 outline-none resize-none overflow-y-auto disabled:opacity-50"
            style={{ minHeight: '50px', maxHeight: '200px' }}
            rows={1}
            disabled={!sessionId || isProcessing}
          />
          <div className="absolute right-2 bottom-2">
            <button
              onClick={handleSend}
              disabled={!sessionId || !input.trim() || isProcessing}
              className="p-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {isProcessing ? <Loader2 size={16} className="animate-spin" /> : <Send size={16} />}
            </button>
          </div>
        </div>
        <div className="text-[10px] text-slate-400 mt-2 text-center flex justify-between px-2">
          <span>{currentProjectName ? `Selected for visualizer: ${currentProjectName}` : 'Whole Session Workspace'}</span>
          {isProcessing && <span>{activeRequest?.status || 'processing'}</span>}
        </div>
      </div>
    </div>
  );
};
