import { useState, useEffect, useRef } from 'react';
import useGraphStore from '../store/graphStore';
import * as api from '../services/api';
import { positionNewNodes } from '@community-graph/ui-graph-canvas';
import './ChatPanel.css';

/**
 * ChatPanel - Conversational interface for interacting with the graph
 *
 * Features:
 * - Send messages to LLM for graph queries and modifications
 * - Display conversation history with tool results
 * - Handle node proposals with approval/rejection
 * - Document upload and analysis integration
 * - Auto-scroll and loading states
 * - Always visible on left side with fixed width
 */
function ChatPanel() {
  const {
    chatMessages,
    addChatMessage,
    nodes,
    edges,
    addNodesToVisualization,
    updateVisualization,
    clearVisualization,
  } = useGraphStore();

  const [inputValue, setInputValue] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [uploadedFile, setUploadedFile] = useState(null);
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState(null);

  const messagesEndRef = useRef(null);
  const fileInputRef = useRef(null);

  // Auto-scroll to latest message
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [chatMessages]);

  // Clear error after 5 seconds
  useEffect(() => {
    if (error) {
      const timer = setTimeout(() => setError(null), 5000);
      return () => clearTimeout(timer);
    }
  }, [error]);

  /**
   * Filter out Community nodes from results
   */
  const filterCommunityNodes = (nodeList) => {
    return nodeList.filter(n =>
      n.type !== 'Community' && n.data?.type !== 'Community'
    );
  };

  /**
   * Send message to backend
   */
  const handleSend = async () => {
    if ((!inputValue.trim() && !uploadedFile) || isProcessing) return;

    // Build message content with file if present
    let messageContent = inputValue.trim();

    if (uploadedFile) {
      const fileContext = `\n\n[Uppladdad fil: ${uploadedFile.filename}]\n\nInnehåll:\n${uploadedFile.text}`;
      messageContent = messageContent
        ? messageContent + fileContext
        : `Analysera följande dokument:\n${fileContext}`;
    }

    // Add user message to chat
    const userMessage = {
      role: 'user',
      content: messageContent,
      timestamp: new Date(),
      hasFile: !!uploadedFile,
      filename: uploadedFile?.filename,
    };
    addChatMessage(userMessage);
    setInputValue('');
    setUploadedFile(null);
    setIsProcessing(true);
    setError(null);

    try {
      // Build conversation history for API (skip welcome message)
      const conversationMessages = chatMessages
        .filter(m => m.role !== 'system' && m.id !== 'welcome')
        .map(m => ({ role: m.role, content: m.content }));

      conversationMessages.push({ role: 'user', content: messageContent });

      // Call chat API
      const response = await api.sendChatMessage(conversationMessages);

      console.log('[ChatPanel] Response:', response);

      // Handle tool results
      const toolResult = response.toolResult;

      if (toolResult) {
        // Handle save view action
        if (toolResult.action === 'save_view' || toolResult.action === 'save_visualization') {
          const viewName = toolResult.name;
          const currentNodes = useGraphStore.getState().nodes;

          const viewNode = {
            name: viewName,
            type: 'SavedView',
            description: `Sparad vy: ${viewName}`,
            summary: `Innehåller ${currentNodes.length} noder`,
            metadata: {
              node_ids: currentNodes.map(n => n.id),
            },
            communities: [],
          };

          try {
            await api.executeTool('add_nodes', { nodes: [viewNode], edges: [] });
          } catch (err) {
            console.error('[ChatPanel] Failed to save view:', err);
          }
        }
        // Handle clear visualization action
        else if (toolResult.action === 'clear_visualization') {
          clearVisualization();
        }
        // Handle load view action
        else if (toolResult.action === 'load_visualization') {
          if (toolResult.nodes && toolResult.nodes.length > 0) {
            const filteredNodes = filterCommunityNodes(toolResult.nodes);
            updateVisualization(filteredNodes, toolResult.edges || []);
          }
        }
        // Handle "add to visualization" action (user said "lägg till X")
        // This ADDS nodes to the current view instead of replacing
        else if (toolResult.action === 'add_to_visualization') {
          if (toolResult.nodes && toolResult.nodes.length > 0) {
            const filteredNodes = filterCommunityNodes(toolResult.nodes);
            // Position new nodes to avoid overlap with existing nodes
            const currentNodes = useGraphStore.getState().nodes;
            const allEdges = [...edges, ...(toolResult.edges || [])];
            const positionedNodes = positionNewNodes(filteredNodes, currentNodes, allEdges);
            addNodesToVisualization(positionedNodes, toolResult.edges || []);
          }
        }
        // Handle standard node/edge updates (search results, etc.)
        // Use updateVisualization to clear and replace (not add to existing)
        else if (toolResult.nodes && toolResult.nodes.length > 0) {
          const filteredNodes = filterCommunityNodes(toolResult.nodes);
          updateVisualization(filteredNodes, toolResult.edges || []);
        }
      }

      // Add assistant response
      const assistantMessage = {
        role: 'assistant',
        content: response.content,
        timestamp: new Date(),
        toolUsed: response.toolUsed,
        proposal: toolResult?.proposed_node ? {
          node: toolResult.proposed_node,
          similar_nodes: toolResult.similar_nodes || [],
        } : null,
        deleteConfirmation: toolResult?.requires_confirmation ? {
          nodes_to_delete: toolResult.nodes_to_delete,
          affected_edges: toolResult.affected_edges,
          node_ids: toolResult.node_ids,
        } : null,
      };
      addChatMessage(assistantMessage);

    } catch (err) {
      console.error('[ChatPanel] Error:', err);
      addChatMessage({
        role: 'assistant',
        content: `Fel: ${err.message}`,
        timestamp: new Date(),
      });
      setError(err.message);
    } finally {
      setIsProcessing(false);
    }
  };

  /**
   * Handle keyboard events
   */
  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  /**
   * Handle file upload
   */
  const handleFileUpload = async (e) => {
    const file = e.target.files[0];
    if (!file) return;

    setIsUploading(true);
    setError(null);

    try {
      const result = await api.uploadFile(file, false);
      if (result.success && result.text) {
        setUploadedFile({
          filename: result.filename,
          text: result.text,
        });
      } else {
        setError('Kunde inte extrahera text från filen.');
      }
    } catch (err) {
      console.error('[ChatPanel] Upload error:', err);
      setError(`Uppladdning misslyckades: ${err.message}`);
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  };

  /**
   * Remove uploaded file
   */
  const handleRemoveFile = () => {
    setUploadedFile(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  /**
   * Approve a node proposal
   */
  const handleApproveProposal = async (proposal) => {
    const msg = `Ja, lägg till noden "${proposal.node.name}"`;
    addChatMessage({ role: 'user', content: msg, timestamp: new Date() });
    setIsProcessing(true);

    try {
      const conversationMessages = chatMessages
        .filter(m => m.id !== 'welcome')
        .map(m => ({ role: m.role, content: m.content }));
      conversationMessages.push({ role: 'user', content: msg });

      const response = await api.sendChatMessage(conversationMessages);

      if (response.toolResult?.nodes) {
        const filteredNodes = filterCommunityNodes(response.toolResult.nodes);
        // Position new nodes to avoid overlap with existing nodes
        const currentNodes = useGraphStore.getState().nodes;
        const allEdges = [...edges, ...(response.toolResult.edges || [])];
        const positionedNodes = positionNewNodes(filteredNodes, currentNodes, allEdges);
        addNodesToVisualization(positionedNodes, response.toolResult.edges || []);
      }

      addChatMessage({
        role: 'assistant',
        content: response.content,
        timestamp: new Date(),
        toolUsed: response.toolUsed,
      });
    } catch (err) {
      addChatMessage({
        role: 'assistant',
        content: `Fel: ${err.message}`,
        timestamp: new Date(),
      });
    } finally {
      setIsProcessing(false);
    }
  };

  /**
   * Reject a node proposal
   */
  const handleRejectProposal = async (proposal) => {
    const msg = 'Nej, lägg inte till noden.';
    addChatMessage({ role: 'user', content: msg, timestamp: new Date() });
    setIsProcessing(true);

    try {
      const conversationMessages = chatMessages
        .filter(m => m.id !== 'welcome')
        .map(m => ({ role: m.role, content: m.content }));
      conversationMessages.push({ role: 'user', content: msg });

      const response = await api.sendChatMessage(conversationMessages);
      addChatMessage({
        role: 'assistant',
        content: response.content,
        timestamp: new Date(),
      });
    } catch (err) {
      console.error(err);
    } finally {
      setIsProcessing(false);
    }
  };

  /**
   * Confirm node deletion
   */
  const handleConfirmDelete = async (deleteConfirmation) => {
    const msg = 'Ja, ta bort noderna.';
    addChatMessage({ role: 'user', content: msg, timestamp: new Date() });
    setIsProcessing(true);

    try {
      const conversationMessages = chatMessages
        .filter(m => m.id !== 'welcome')
        .map(m => ({ role: m.role, content: m.content }));
      conversationMessages.push({ role: 'user', content: msg });

      const response = await api.sendChatMessage(conversationMessages);

      // Remove deleted nodes from visualization
      if (deleteConfirmation.node_ids) {
        const deletedIds = new Set(deleteConfirmation.node_ids);
        const newNodes = nodes.filter(n => !deletedIds.has(n.id));
        const newEdges = edges.filter(e => !deletedIds.has(e.source) && !deletedIds.has(e.target));
        updateVisualization(newNodes, newEdges);
      }

      addChatMessage({
        role: 'assistant',
        content: response.content,
        timestamp: new Date(),
        toolUsed: response.toolUsed,
      });
    } catch (err) {
      addChatMessage({
        role: 'assistant',
        content: `Fel: ${err.message}`,
        timestamp: new Date(),
      });
    } finally {
      setIsProcessing(false);
    }
  };

  /**
   * Cancel node deletion
   */
  const handleCancelDelete = () => {
    addChatMessage({
      role: 'assistant',
      content: 'Borttagning avbruten.',
      timestamp: new Date(),
    });
  };

  /**
   * Format timestamp
   */
  const formatTime = (timestamp) => {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    return date.toLocaleTimeString('sv-SE', { hour: '2-digit', minute: '2-digit' });
  };

  return (
    <div className="chat-panel">
      <div className="chat-header">
        <h3>Graf-assistent</h3>
      </div>

      <div className="chat-messages">
        {chatMessages.map((msg, idx) => (
          <div key={msg.id || idx} className={`chat-message ${msg.role}`}>
            <div className="message-content">
              {msg.content}

              {/* Loading indicator */}
              {msg.role === 'user' && idx === chatMessages.length - 1 && isProcessing && (
                <div className="message-loading">
                  <div className="loading-dots">
                    <span></span>
                    <span></span>
                    <span></span>
                  </div>
                  <span className="loading-text">Bearbetar...</span>
                </div>
              )}

              {/* Node proposal card */}
              {msg.proposal && (
                <div className="proposal-card">
                  <h4>Föreslaget tillägg:</h4>
                  <div className="proposal-details">
                    <p><strong>Typ:</strong> {msg.proposal.node.type}</p>
                    <p><strong>Namn:</strong> {msg.proposal.node.name}</p>
                    <p><strong>Beskrivning:</strong> {msg.proposal.node.description}</p>
                    {msg.proposal.node.communities?.length > 0 && (
                      <p><strong>Communities:</strong> {msg.proposal.node.communities.join(', ')}</p>
                    )}
                  </div>

                  {msg.proposal.similar_nodes?.length > 0 && (
                    <div className="similar-nodes-warning">
                      <p><strong>Liknande noder hittades:</strong></p>
                      <ul>
                        {msg.proposal.similar_nodes.map((sim, i) => (
                          <li key={i}>
                            {sim.node?.name || sim.name} ({Math.round((sim.similarity_score || sim.score) * 100)}% match)
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}

                  <div className="proposal-actions">
                    <button
                      className="approve-button"
                      onClick={() => handleApproveProposal(msg.proposal)}
                      disabled={isProcessing}
                    >
                      Godkänn
                    </button>
                    <button
                      className="reject-button"
                      onClick={() => handleRejectProposal(msg.proposal)}
                      disabled={isProcessing}
                    >
                      Avvisa
                    </button>
                  </div>
                </div>
              )}

              {/* Delete confirmation card */}
              {msg.deleteConfirmation && (
                <div className="delete-card">
                  <h4>Bekräfta borttagning:</h4>
                  <div className="proposal-details">
                    <p><strong>Noder att ta bort:</strong></p>
                    <ul>
                      {msg.deleteConfirmation.nodes_to_delete?.map((node, i) => (
                        <li key={i}>{node.name} ({node.type})</li>
                      ))}
                    </ul>
                    <p><strong>Påverkade relationer:</strong> {msg.deleteConfirmation.affected_edges?.length || 0}</p>
                  </div>

                  <div className="similar-nodes-warning">
                    <p><strong>Varning:</strong> Denna åtgärd kan inte ångras!</p>
                  </div>

                  <div className="proposal-actions">
                    <button
                      className="reject-button"
                      onClick={() => handleConfirmDelete(msg.deleteConfirmation)}
                      disabled={isProcessing}
                    >
                      Bekräfta borttagning
                    </button>
                    <button
                      className="approve-button"
                      onClick={handleCancelDelete}
                      disabled={isProcessing}
                    >
                      Avbryt
                    </button>
                  </div>
                </div>
              )}
            </div>
            <div className="message-timestamp">
              {formatTime(msg.timestamp)}
            </div>
          </div>
        ))}
        <div ref={messagesEndRef} />
      </div>

      {error && (
        <div className="chat-error">
          {error}
        </div>
      )}

      <div className="chat-input-container">
        {/* File indicator */}
        {uploadedFile && (
          <div className="file-indicator">
            <div className="file-info">
              <span className="file-icon">📄</span>
              <span className="file-name">{uploadedFile.filename}</span>
              <span className="file-size">({Math.round(uploadedFile.text.length / 1024)} KB)</span>
            </div>
            <button
              className="remove-file-button"
              onClick={handleRemoveFile}
              title="Ta bort fil"
            >
              &times;
            </button>
          </div>
        )}

        <textarea
          className="chat-input"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyPress={handleKeyPress}
          placeholder={uploadedFile
            ? "Beskriv vad du vill göra med dokumentet..."
            : "Ställ en fråga eller begär en åtgärd..."}
          rows={3}
          disabled={isProcessing}
        />

        <div className="button-row">
          <input
            type="file"
            ref={fileInputRef}
            onChange={handleFileUpload}
            style={{ display: 'none' }}
            accept=".pdf,.docx,.doc,.txt"
          />
          <button
            className="chat-upload-button"
            onClick={() => fileInputRef.current?.click()}
            disabled={isUploading || isProcessing}
            title="Ladda upp dokument (PDF, Word, Text)"
          >
            {isUploading ? 'Laddar...' : 'Ladda upp'}
          </button>
          <button
            className="chat-send-button"
            onClick={handleSend}
            disabled={(!inputValue.trim() && !uploadedFile) || isProcessing}
          >
            {isProcessing ? 'Bearbetar...' : 'Skicka'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ChatPanel;
