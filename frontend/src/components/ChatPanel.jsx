import { useState, useEffect, useRef } from 'react';
import useGraphStore from '../store/graphStore';
import './ChatPanel.css';

function ChatPanel() {
  const { chatMessages, addChatMessage, selectedCommunities } = useGraphStore();
  const [inputValue, setInputValue] = useState('');
  const messagesEndRef = useRef(null);

  // Auto-scroll to latest message
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [chatMessages]);

  const handleSend = async () => {
    if (!inputValue.trim()) return;

    // Add user message
    const userMessage = {
      role: 'user',
      content: inputValue,
      timestamp: new Date()
    };
    addChatMessage(userMessage);
    setInputValue('');

    // TODO: Send to Claude API via MCP
    // For now: Placeholder response
    setTimeout(() => {
      const assistantMessage = {
        role: 'assistant',
        content: `[Demo mode] You asked: "${inputValue}"

MCP integration will be implemented in the next step.

Active communities: ${selectedCommunities.join(', ')}`,
        timestamp: new Date()
      };
      addChatMessage(assistantMessage);
    }, 500);
  };

  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="chat-panel">
      <div className="chat-messages">
        {chatMessages.map((msg, idx) => (
          <div key={idx} className={`chat-message ${msg.role}`}>
            <div className="message-content">
              {msg.content}
            </div>
            <div className="message-timestamp">
              {msg.timestamp?.toLocaleTimeString('sv-SE', {
                hour: '2-digit',
                minute: '2-digit'
              })}
            </div>
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      <div className="chat-input-container">
        <textarea
          className="chat-input"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyPress={handleKeyPress}
          placeholder="Ask a question about the graph..."
          rows={3}
        />
        <button
          className="chat-send-button"
          onClick={handleSend}
          disabled={!inputValue.trim()}
        >
          Send
        </button>
      </div>
    </div>
  );
}

export default ChatPanel;
