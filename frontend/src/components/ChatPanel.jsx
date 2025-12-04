import { useState, useEffect, useRef } from 'react';
import useGraphStore from '../store/graphStore';
import { sendMessageToClaude } from '../services/claudeService';
import './ChatPanel.css';

function ChatPanel() {
  const {
    chatMessages,
    addChatMessage,
    selectedCommunities,
    updateVisualization,
    setLoading,
    setError,
    clearError
  } = useGraphStore();
  const [inputValue, setInputValue] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const messagesEndRef = useRef(null);

  // Get API key from environment
  const apiKey = import.meta.env.VITE_ANTHROPIC_API_KEY;

  // Auto-scroll to latest message
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [chatMessages]);

  const handleSend = async () => {
    if (!inputValue.trim() || isProcessing) return;

    // Check if API key is available
    if (!apiKey) {
      const errorMessage = {
        role: 'assistant',
        content: '⚠️ API key not configured. Please add VITE_ANTHROPIC_API_KEY to your .env file.\n\nSee frontend/.env.example for details.',
        timestamp: new Date()
      };
      addChatMessage(errorMessage);
      return;
    }

    // Add user message
    const userMessage = {
      role: 'user',
      content: inputValue,
      timestamp: new Date()
    };
    addChatMessage(userMessage);
    setInputValue('');
    setIsProcessing(true);
    clearError();

    try {
      // Get conversation history for Claude
      const conversationMessages = chatMessages
        .filter(m => m.role !== 'system') // Filter out system messages
        .map(m => ({
          role: m.role,
          content: m.content
        }));

      // Add the new user message
      conversationMessages.push({
        role: 'user',
        content: inputValue
      });

      // Call Claude API with tool support
      const response = await sendMessageToClaude(
        conversationMessages,
        apiKey,
        (toolResult) => {
          // Handle tool results (e.g., search results)
          if (toolResult.nodes && toolResult.nodes.length > 0) {
            // Update visualization with search results
            const edges = toolResult.edges || [];
            updateVisualization(toolResult.nodes, edges);
          }
        }
      );

      // Add Claude's response
      const assistantMessage = {
        role: 'assistant',
        content: response.content,
        timestamp: new Date(),
        toolUsed: response.toolUsed
      };
      addChatMessage(assistantMessage);

    } catch (error) {
      console.error('Error communicating with Claude:', error);
      const errorMessage = {
        role: 'assistant',
        content: `❌ Error: ${error.message}\n\nPlease check your API key and try again.`,
        timestamp: new Date()
      };
      addChatMessage(errorMessage);
      setError(error.message);
    } finally {
      setIsProcessing(false);
    }
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
          disabled={!inputValue.trim() || isProcessing}
        >
          {isProcessing ? 'Thinking...' : 'Send'}
        </button>
      </div>
    </div>
  );
}

export default ChatPanel;
