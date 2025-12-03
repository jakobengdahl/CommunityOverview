import { useState, useEffect, useRef } from 'react';
import useGraphStore from '../store/graphStore';
import './ChatPanel.css';

const WELCOME_MESSAGE = {
  role: 'assistant',
  content: `Välkommen till Community Knowledge Graph!

Du kan ställa frågor som:
• "Vilka initiativ rör NIS2?"
• "Visa alla aktörer i eSam-communityn"
• "Finns det projekt om AI-strategi?"

**OBS:** Hanterar inte personuppgifter i denna tjänst.`,
  timestamp: new Date()
};

function ChatPanel() {
  const { chatMessages, addChatMessage, selectedCommunities } = useGraphStore();
  const [inputValue, setInputValue] = useState('');
  const messagesEndRef = useRef(null);

  // Auto-scroll till senaste meddelandet
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [chatMessages]);

  // Lägg till välkomstmeddelande vid start
  useEffect(() => {
    if (chatMessages.length === 0) {
      addChatMessage(WELCOME_MESSAGE);
    }
  }, []);

  const handleSend = async () => {
    if (!inputValue.trim()) return;

    // Lägg till user message
    const userMessage = {
      role: 'user',
      content: inputValue,
      timestamp: new Date()
    };
    addChatMessage(userMessage);
    setInputValue('');

    // TODO: Skicka till Claude API via MCP
    // För nu: Placeholder response
    setTimeout(() => {
      const assistantMessage = {
        role: 'assistant',
        content: `[Demo mode] Du frågade: "${inputValue}"

MCP-integration kommer implementeras i nästa steg.

Aktiva communities: ${selectedCommunities.join(', ')}`,
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
          placeholder="Ställ en fråga om grafen..."
          rows={3}
        />
        <button
          className="chat-send-button"
          onClick={handleSend}
          disabled={!inputValue.trim()}
        >
          Skicka
        </button>
      </div>
    </div>
  );
}

export default ChatPanel;
