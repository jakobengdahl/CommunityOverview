import { useState, useEffect, useRef, useCallback } from 'react';
import { FunnelFill, SendFill, ArrowRightCircleFill } from 'react-bootstrap-icons';
import * as api from '../services/api';
import CollectionForm from './CollectionForm';
import './CollectKioskView.css';

/** Extract a present_form spec from a chat response, or null if none. */
function formFromResponse(response) {
  return response?.toolResult?.action === 'present_form' ? response.toolResult.form : null;
}

/** Render one answer value for the human-readable summary. */
function formatAnswerValue(value) {
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value ?? '');
}

/** Build the visible summary and the LLM-facing content for a submitted form. */
function buildFormSubmission(answers) {
  const readable = answers
    .map((a) => `- ${a.label || a.field_id}: ${formatAnswerValue(a.value)}`)
    .join('\n');
  const display = readable;
  const llmContent =
    `[Form answers submitted]\n${readable}\n\n` +
    `Structured answers to store with save_collection_response: ${JSON.stringify(answers)}`;
  return { display, llmContent };
}

/**
 * CollectKioskView — Full-screen AI-assistant kiosk for data collection.
 *
 * Activated when the URL contains ?collect=<shortName>.
 * Fetches the ActiveKnowledgeCollection config, shows an intro overlay,
 * then presents a focused full-screen chat UI (no graph, no toolbar).
 *
 * The AI assistant receives the collection's `prompt` as a system prompt
 * prefix, along with a summary of the permitted operations derived from
 * `node_type_permissions`.
 */
function CollectKioskView({ shortName }) {
  const [config, setConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [introShown, setIntroShown] = useState(false);

  // Chat state (local, not in Zustand — this is a standalone view)
  const [messages, setMessages] = useState([]);
  const [inputValue, setInputValue] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [chatError, setChatError] = useState(null);

  const messagesEndRef = useRef(null);
  const textareaRef = useRef(null);
  const kickstartFiredRef = useRef(false);

  // Scroll to bottom when messages change
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // Fetch config on mount
  useEffect(() => {
    async function fetchConfig() {
      try {
        setLoading(true);
        const data = await api.getCollectConfig(shortName);
        setConfig(data);
      } catch (err) {
        setError(err.message || 'Collection not found');
      } finally {
        setLoading(false);
      }
    }
    fetchConfig();
  }, [shortName]);

  // Auto-trigger AI opening message when the intro overlay is dismissed.
  // kickstartFiredRef prevents the effect from running twice under React StrictMode.
  useEffect(() => {
    if (!introShown || !config || kickstartFiredRef.current) return;
    kickstartFiredRef.current = true;

    const kickstartMsg = { role: 'user', content: '[COLLECTION_START]' };
    setMessages([
      {
        id: crypto.randomUUID(),
        role: 'user',
        content: '[COLLECTION_START]',
        timestamp: new Date(),
        hidden: true,
      },
    ]);
    setIsProcessing(true);

    api
      .sendChatMessage([kickstartMsg], null, { collectionShortName: shortName })
      .then((response) => {
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: 'assistant',
            content: response.content || '(no response)',
            timestamp: new Date(),
            toolUsed: response.toolUsed,
            form: formFromResponse(response),
          },
        ]);
      })
      .catch((err) => {
        console.error('[CollectKioskView] Kickstart error:', err);
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: 'assistant',
            content: 'Error starting the collection session. Please type a message to begin.',
            timestamp: new Date(),
          },
        ]);
      })
      .finally(() => {
        setIsProcessing(false);
        setTimeout(() => textareaRef.current?.focus(), 100);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [introShown]);

  const handleSend = useCallback(async () => {
    if (!inputValue.trim() || isProcessing) return;

    const userText = inputValue.trim();

    // Capture history synchronously before state updates to avoid stale-closure issues.
    // Hidden messages (e.g. the [COLLECTION_START] kickstart) must stay in the API history
    // so the conversation starts with a user turn as required by the Anthropic API.
    // A form submission carries its structured payload in llmContent (not shown in chat).
    const conversationHistory = [
      ...messages.map((m) => ({ role: m.role, content: m.llmContent ?? m.content })),
      { role: 'user', content: userText },
    ];

    setMessages((prev) => [
      ...prev,
      {
        id: crypto.randomUUID(),
        role: 'user',
        content: userText,
        timestamp: new Date(),
      },
    ]);
    setInputValue('');
    setIsProcessing(true);
    setChatError(null);

    try {
      const response = await api.sendChatMessage(conversationHistory, null, {
        collectionShortName: shortName,
      });

      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: 'assistant',
          content: response.content || '(no response)',
          timestamp: new Date(),
          toolUsed: response.toolUsed,
          form: formFromResponse(response),
        },
      ]);
    } catch (err) {
      console.error('[CollectKioskView] Chat error:', err);
      setChatError(err.message);
      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: 'assistant',
          content: `Error: ${err.message}`,
          timestamp: new Date(),
        },
      ]);
    } finally {
      setIsProcessing(false);
      setTimeout(() => textareaRef.current?.focus(), 100);
    }
  }, [inputValue, isProcessing, messages, shortName]);

  const handleFormSubmit = useCallback(
    async (messageId, answers) => {
      if (isProcessing) return;

      const { display, llmContent } = buildFormSubmission(answers);

      // Lock the submitted form so it can't be resubmitted.
      setMessages((prev) =>
        prev.map((m) => (m.id === messageId ? { ...m, formSubmitted: true } : m))
      );

      const conversationHistory = [
        ...messages.map((m) => ({ role: m.role, content: m.llmContent ?? m.content })),
        { role: 'user', content: llmContent },
      ];

      setMessages((prev) => [
        ...prev,
        {
          id: crypto.randomUUID(),
          role: 'user',
          content: display,
          llmContent,
          timestamp: new Date(),
        },
      ]);
      setIsProcessing(true);
      setChatError(null);

      try {
        const response = await api.sendChatMessage(conversationHistory, null, {
          collectionShortName: shortName,
        });
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: 'assistant',
            content: response.content || '(no response)',
            timestamp: new Date(),
            toolUsed: response.toolUsed,
            form: formFromResponse(response),
          },
        ]);
      } catch (err) {
        console.error('[CollectKioskView] Form submit error:', err);
        setChatError(err.message);
        setMessages((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: 'assistant',
            content: `Error: ${err.message}`,
            timestamp: new Date(),
          },
        ]);
      } finally {
        setIsProcessing(false);
        setTimeout(() => textareaRef.current?.focus(), 100);
      }
    },
    [isProcessing, messages, shortName]
  );

  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const formatTime = (timestamp) => {
    if (!timestamp) return '';
    return new Date(timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  };

  // ── Loading state ──────────────────────────────────────────────
  if (loading) {
    return (
      <div className="kiosk-root">
        <div className="kiosk-loading">
          <div className="kiosk-spinner" />
          <p>Loading collection…</p>
        </div>
      </div>
    );
  }

  // ── Error state ────────────────────────────────────────────────
  if (error || !config) {
    return (
      <div className="kiosk-root">
        <div className="kiosk-error-card">
          <FunnelFill size={36} style={{ color: '#F59E0B', marginBottom: '1rem' }} />
          <h2>Collection not found</h2>
          <p style={{ color: '#888' }}>
            The collection <strong style={{ color: '#ccc' }}>{shortName}</strong> does not exist or
            has been removed.
          </p>
          {error && (
            <p style={{ color: '#666', fontSize: '0.85rem', marginTop: '0.5rem' }}>{error}</p>
          )}
        </div>
      </div>
    );
  }

  // ── Intro overlay ──────────────────────────────────────────────
  if (!introShown) {
    return (
      <div className="kiosk-root">
        <div className="kiosk-intro-overlay">
          <div className="kiosk-intro-card">
            <div className="kiosk-intro-icon">
              <FunnelFill size={28} style={{ color: '#F59E0B' }} />
            </div>
            <h1 className="kiosk-intro-title">{config.name || 'Knowledge Collection'}</h1>

            {config.introduction_text ? (
              <div className="kiosk-intro-text">
                {config.introduction_text.split('\n').map((line, i) => (
                  <p key={i} style={{ margin: '0 0 0.6rem 0' }}>
                    {line}
                  </p>
                ))}
              </div>
            ) : (
              <p className="kiosk-intro-text" style={{ color: '#888' }}>
                You are about to start a guided data collection session. An AI assistant will help
                you enter the relevant information.
              </p>
            )}

            <button className="kiosk-start-button" onClick={() => setIntroShown(true)}>
              Start
              <ArrowRightCircleFill size={18} style={{ marginLeft: '0.5rem' }} />
            </button>
          </div>
        </div>
      </div>
    );
  }

  // ── Main chat UI ───────────────────────────────────────────────
  return (
    <div className="kiosk-root">
      {/* Header */}
      <div className="kiosk-header">
        <div className="kiosk-header-left">
          <FunnelFill size={18} style={{ color: '#F59E0B' }} />
          <span className="kiosk-header-title">{config.name || 'Knowledge Collection'}</span>
        </div>
        <div className="kiosk-header-right">
          <span className="kiosk-header-badge">Collection Mode</span>
        </div>
      </div>

      {/* Messages area */}
      <div className="kiosk-messages">
        {messages
          .filter((m) => !m.hidden)
          .map((msg) => (
            <div key={msg.id} className={`kiosk-message kiosk-message-${msg.role}`}>
              <div className="kiosk-message-bubble">
                <div className="kiosk-message-content">{msg.content}</div>
                {msg.form && (
                  <CollectionForm
                    form={msg.form}
                    submitted={!!msg.formSubmitted}
                    disabled={isProcessing}
                    onSubmit={(answers) => handleFormSubmit(msg.id, answers)}
                  />
                )}
                <div className="kiosk-message-meta">
                  {msg.role === 'assistant' ? 'Assistant' : 'You'}
                  {msg.timestamp && (
                    <span style={{ marginLeft: '0.4rem', opacity: 0.6 }}>
                      · {formatTime(msg.timestamp)}
                    </span>
                  )}
                  {msg.toolUsed && <span className="kiosk-tool-badge">tool: {msg.toolUsed}</span>}
                </div>
              </div>
            </div>
          ))}

        {/* Processing indicator */}
        {isProcessing && (
          <div className="kiosk-message kiosk-message-assistant">
            <div className="kiosk-message-bubble kiosk-thinking">
              <div className="kiosk-dots">
                <span />
                <span />
                <span />
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* Error banner */}
      {chatError && <div className="kiosk-chat-error">{chatError}</div>}

      {/* Input area */}
      <div className="kiosk-input-area">
        <textarea
          ref={textareaRef}
          className="kiosk-input"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyDown={handleKeyPress}
          placeholder="Type your message… (Enter to send, Shift+Enter for new line)"
          rows={3}
          disabled={isProcessing}
          autoFocus
        />
        <button
          className="kiosk-send-button"
          onClick={handleSend}
          disabled={!inputValue.trim() || isProcessing}
          title="Send message"
        >
          <SendFill size={20} />
        </button>
      </div>
    </div>
  );
}

export default CollectKioskView;
