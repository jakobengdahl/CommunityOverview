import { useState, useEffect, useRef } from 'react';
import useGraphStore from '../store/graphStore';
import { sendMessageToClaude } from '../services/claudeService';
import { addNodeToDemoData, loadDemoData } from '../services/demoData';
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
  const [showTextExtract, setShowTextExtract] = useState(false);
  const [extractText, setExtractText] = useState('');
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
          // Handle tool results (e.g., search results, updates, deletes)
          if (toolResult.tool_type === 'update' || toolResult.tool_type === 'delete') {
            // For updates and deletes, reload the entire graph to show changes
            loadDemoData(updateVisualization, selectedCommunities);
          } else if (toolResult.nodes && toolResult.nodes.length > 0) {
            // For search/related, update visualization with search results
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
        toolUsed: response.toolUsed,
        proposal: response.toolResult?.proposed_node ? {
          node: response.toolResult.proposed_node,
          similar_nodes: response.toolResult.similar_nodes
        } : null,
        deleteConfirmation: response.toolResult?.requires_confirmation ? {
          nodes_to_delete: response.toolResult.nodes_to_delete,
          affected_edges: response.toolResult.affected_edges,
          node_ids: response.toolResult.nodes_to_delete.map(n => n.id)
        } : null
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

  const handleApproveProposal = (proposal) => {
    // Add the node to demo data
    const result = addNodeToDemoData(proposal.node);

    if (result.success) {
      // Reload the graph to show the new node
      loadDemoData(updateVisualization, selectedCommunities);

      // Add confirmation message
      const confirmationMessage = {
        role: 'assistant',
        content: `✅ Noden "${proposal.node.name}" har lagts till i grafen!`,
        timestamp: new Date()
      };
      addChatMessage(confirmationMessage);
    }
  };

  const handleRejectProposal = (proposal) => {
    // Add rejection message
    const rejectionMessage = {
      role: 'assistant',
      content: `❌ Noden "${proposal.node.name}" lades inte till.`,
      timestamp: new Date()
    };
    addChatMessage(rejectionMessage);
  };

  const handleConfirmDelete = async (deleteConfirmation) => {
    setIsProcessing(true);
    try {
      // Import the executeTool function from claudeService
      const { DEMO_GRAPH_DATA } = await import('../services/demoData');

      // Delete the nodes
      const nodeIds = deleteConfirmation.node_ids;
      DEMO_GRAPH_DATA.nodes = DEMO_GRAPH_DATA.nodes.filter(n => !nodeIds.includes(n.id));
      DEMO_GRAPH_DATA.edges = DEMO_GRAPH_DATA.edges.filter(
        edge => !nodeIds.includes(edge.source) && !nodeIds.includes(edge.target)
      );

      // Reload the graph
      loadDemoData(updateVisualization, selectedCommunities);

      // Add confirmation message
      const confirmationMessage = {
        role: 'assistant',
        content: `✅ ${nodeIds.length} nod(er) och ${deleteConfirmation.affected_edges.length} koppling(ar) har raderats.`,
        timestamp: new Date()
      };
      addChatMessage(confirmationMessage);
    } catch (error) {
      console.error('Error deleting nodes:', error);
      const errorMessage = {
        role: 'assistant',
        content: `❌ Fel vid radering: ${error.message}`,
        timestamp: new Date()
      };
      addChatMessage(errorMessage);
    } finally {
      setIsProcessing(false);
    }
  };

  const handleCancelDelete = () => {
    // Add cancellation message
    const cancellationMessage = {
      role: 'assistant',
      content: `❌ Radering avbruten.`,
      timestamp: new Date()
    };
    addChatMessage(cancellationMessage);
  };

  const handleExtractFromText = async () => {
    if (!extractText.trim()) return;

    setIsProcessing(true);
    setShowTextExtract(false);

    try {
      // Add user message showing they want to extract
      addChatMessage({
        role: 'user',
        content: `Extrahera noder från följande text:\n\n${extractText.substring(0, 200)}${extractText.length > 200 ? '...' : ''}`,
        timestamp: new Date()
      });

      // Build conversation with extraction prompt
      const extractionPrompt = `Analysera följande text och extrahera alla relevanta noder enligt metamodellen (Actor, Initiative, Capability, Resource, Legislation, Theme, Community).

VIKTIGT: Använd find_similar_nodes för VARJE extraherad nod för att kontrollera dubbletter innan du föreslår dem. Föreslå sedan ALLA noder på en gång med propose_new_node för varje nod.

Text att analysera:
${extractText}

Communities att koppla till: ${selectedCommunities.join(', ') || 'Ingen community vald'}`;

      const conversationMessages = [
        ...chatMessages.map(m => ({ role: m.role, content: m.content })),
        { role: 'user', content: extractionPrompt }
      ];

      // Call Claude API
      const response = await sendMessageToClaude(
        conversationMessages,
        apiKey,
        (toolResult) => {
          if (toolResult.tool_type === 'update' || toolResult.tool_type === 'delete') {
            loadDemoData(updateVisualization, selectedCommunities);
          } else if (toolResult.nodes && toolResult.nodes.length > 0) {
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
        toolUsed: response.toolUsed,
        proposal: response.toolResult?.proposed_node ? {
          node: response.toolResult.proposed_node,
          similar_nodes: response.toolResult.similar_nodes
        } : null,
        deleteConfirmation: response.toolResult?.requires_confirmation ? {
          nodes_to_delete: response.toolResult.nodes_to_delete,
          affected_edges: response.toolResult.affected_edges,
          node_ids: response.toolResult.nodes_to_delete.map(n => n.id)
        } : null
      };
      addChatMessage(assistantMessage);

      // Clear the extract text
      setExtractText('');
    } catch (error) {
      console.error('Error extracting from text:', error);
      const errorMessage = {
        role: 'assistant',
        content: `❌ Fel vid extrahering: ${error.message}`,
        timestamp: new Date()
      };
      addChatMessage(errorMessage);
    } finally {
      setIsProcessing(false);
    }
  };

  return (
    <div className="chat-panel">
      <div className="chat-messages">
        {chatMessages.map((msg, idx) => (
          <div key={idx} className={`chat-message ${msg.role}`}>
            <div className="message-content">
              {msg.content}

              {/* Render proposal with approve/reject buttons */}
              {msg.proposal && (
                <div className="proposal-card">
                  <h4>📋 Föreslaget tillägg:</h4>
                  <div className="proposal-details">
                    <p><strong>Typ:</strong> {msg.proposal.node.type}</p>
                    <p><strong>Namn:</strong> {msg.proposal.node.name}</p>
                    <p><strong>Beskrivning:</strong> {msg.proposal.node.description}</p>
                    <p><strong>Communities:</strong> {msg.proposal.node.communities.join(', ')}</p>
                  </div>

                  {msg.proposal.similar_nodes && msg.proposal.similar_nodes.length > 0 && (
                    <div className="similar-nodes-warning">
                      <p><strong>⚠️ Liknande noder hittades:</strong></p>
                      <ul>
                        {msg.proposal.similar_nodes.map((sim, i) => (
                          <li key={i}>
                            {sim.node.name} ({sim.similarity_score * 100}% match)
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  <div className="proposal-actions">
                    <button
                      className="approve-button"
                      onClick={() => handleApproveProposal(msg.proposal)}
                    >
                      ✅ Godkänn
                    </button>
                    <button
                      className="reject-button"
                      onClick={() => handleRejectProposal(msg.proposal)}
                    >
                      ❌ Avslå
                    </button>
                  </div>
                </div>
              )}

              {/* Render delete confirmation with confirm/cancel buttons */}
              {msg.deleteConfirmation && (
                <div className="delete-card">
                  <h4>⚠️ Bekräfta radering:</h4>
                  <div className="proposal-details">
                    <p><strong>Noder som kommer raderas:</strong></p>
                    <ul>
                      {msg.deleteConfirmation.nodes_to_delete.map((node, i) => (
                        <li key={i}>
                          {node.name} ({node.type})
                        </li>
                      ))}
                    </ul>
                    <p><strong>Antal kopplingar som raderas:</strong> {msg.deleteConfirmation.affected_edges.length}</p>
                  </div>

                  <div className="similar-nodes-warning">
                    <p><strong>⚠️ VARNING:</strong> Denna åtgärd kan inte ångras!</p>
                  </div>

                  <div className="proposal-actions">
                    <button
                      className="reject-button"
                      onClick={() => handleConfirmDelete(msg.deleteConfirmation)}
                      disabled={isProcessing}
                    >
                      ⚠️ Bekräfta radering
                    </button>
                    <button
                      className="approve-button"
                      onClick={() => handleCancelDelete()}
                      disabled={isProcessing}
                    >
                      ❌ Avbryt
                    </button>
                  </div>
                </div>
              )}
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
        <div className="button-row">
          <button
            className="chat-extract-button"
            onClick={() => setShowTextExtract(!showTextExtract)}
            disabled={isProcessing}
            title="Extract nodes from document text"
          >
            📄 Extract from Text
          </button>
          <button
            className="chat-send-button"
            onClick={handleSend}
            disabled={!inputValue.trim() || isProcessing}
          >
            {isProcessing ? 'Thinking...' : 'Send'}
          </button>
        </div>
      </div>

      {/* Text Extraction Panel */}
      {showTextExtract && (
        <div className="text-extract-panel">
          <div className="extract-header">
            <h3>📄 Extract Nodes from Text</h3>
            <button
              className="close-button"
              onClick={() => setShowTextExtract(false)}
            >
              ✕
            </button>
          </div>
          <p className="extract-instructions">
            Paste text from a document (strategy document, project description, legislation, etc.).
            Claude will analyze it and extract relevant nodes according to the metamodel.
          </p>
          {selectedCommunities.length > 0 && (
            <p className="extract-communities">
              <strong>Active communities:</strong> {selectedCommunities.join(', ')}
            </p>
          )}
          <textarea
            className="extract-textarea"
            value={extractText}
            onChange={(e) => setExtractText(e.target.value)}
            placeholder="Paste document text here..."
            rows={10}
          />
          <div className="extract-actions">
            <button
              className="approve-button"
              onClick={handleExtractFromText}
              disabled={!extractText.trim() || isProcessing}
            >
              🔍 Extract Nodes
            </button>
            <button
              className="reject-button"
              onClick={() => {
                setShowTextExtract(false);
                setExtractText('');
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default ChatPanel;
