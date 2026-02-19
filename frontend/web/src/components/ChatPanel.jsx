import { useState, useEffect, useRef, useMemo } from 'react';
import { ChatDotsFill, ChevronRight, ChevronLeft, XCircleFill } from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import * as api from '../services/api';
import { positionNewNodes, getNodeColor } from '@community-graph/ui-graph-canvas';
import './ChatPanel.css';

/** Max characters of node context to include with a message to the LLM */
const MAX_SELECTION_CONTEXT_CHARS = 6000;

function ChatPanel() {
  const {
    chatMessages,
    addChatMessage,
    nodes,
    edges,
    addNodesToVisualization,
    updateVisualization,
    clearVisualization,
    chatPanelOpen,
    toggleChatPanel,
    selectedGraphNodes,
    clearSelectedGraphNodes,
  } = useGraphStore();

  const { t, language } = useI18n();

  const [inputValue, setInputValue] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [uploadedFile, setUploadedFile] = useState(null);
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState(null);

  const messagesEndRef = useRef(null);
  const fileInputRef = useRef(null);

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [chatMessages]);

  useEffect(() => {
    if (error) {
      const timer = setTimeout(() => setError(null), 5000);
      return () => clearTimeout(timer);
    }
  }, [error]);

  const filterCommunityNodes = (nodeList) => {
    return nodeList.filter(n =>
      n.type !== 'Community' && n.data?.type !== 'Community'
    );
  };

  const handleSend = async () => {
    if ((!inputValue.trim() && !uploadedFile) || isProcessing) return;

    let messageContent = inputValue.trim();

    if (uploadedFile) {
      const fileContext = t('chat.file_context', { filename: uploadedFile.filename, text: uploadedFile.text });
      messageContent = messageContent
        ? messageContent + fileContext
        : t('chat.analyze_document', { fileContext });
    }

    // Append selected node context for the LLM (not shown in chat bubble)
    const selectionContext = buildSelectionContext();
    const messageForLLM = selectionContext
      ? messageContent + selectionContext
      : messageContent;

    const userMessage = {
      role: 'user',
      content: messageContent, // Show only the user's text in chat
      timestamp: new Date(),
      hasFile: !!uploadedFile,
      filename: uploadedFile?.filename,
      hasSelection: selectedGraphNodes.length > 0,
      selectionCount: selectedGraphNodes.length,
    };
    addChatMessage(userMessage);
    setInputValue('');
    setUploadedFile(null);
    setIsProcessing(true);
    setError(null);

    try {
      const conversationMessages = chatMessages
        .filter(m => m.role !== 'system' && m.id !== 'welcome')
        .map(m => ({ role: m.role, content: m.content }));

      conversationMessages.push({ role: 'user', content: messageForLLM });

      const response = await api.sendChatMessage(conversationMessages);

      console.log('[ChatPanel] Response:', response);

      const toolResult = response.toolResult;

      if (toolResult) {
        if (toolResult.action === 'save_view' || toolResult.action === 'save_visualization') {
          const viewName = toolResult.name;
          const currentNodes = useGraphStore.getState().nodes;

          const viewNode = {
            name: viewName,
            type: 'SavedView',
            description: t('notifications.saved_view_description', { name: viewName }),
            summary: t('notifications.saved_view_summary', { count: currentNodes.length }),
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
        else if (toolResult.action === 'clear_visualization') {
          clearVisualization();
        }
        else if (toolResult.action === 'load_visualization') {
          if (toolResult.nodes && toolResult.nodes.length > 0) {
            const filteredNodes = filterCommunityNodes(toolResult.nodes);
            updateVisualization(filteredNodes, toolResult.edges || []);
          }
        }
        else if (toolResult.action === 'add_to_visualization') {
          if (toolResult.nodes && toolResult.nodes.length > 0) {
            const filteredNodes = filterCommunityNodes(toolResult.nodes);
            const currentNodes = useGraphStore.getState().nodes;
            const allEdges = [...edges, ...(toolResult.edges || [])];
            const positionedNodes = positionNewNodes(filteredNodes, currentNodes, allEdges);
            addNodesToVisualization(positionedNodes, toolResult.edges || []);
          }
        }
        else if (toolResult.action === 'update_in_visualization') {
          if (toolResult.nodes && toolResult.nodes.length > 0) {
            const { nodes: currentNodes, edges: currentEdges, updateVisualization } = useGraphStore.getState();
            const updatedNodeIds = new Set(toolResult.nodes.map(n => n.id));
            const mergedNodes = currentNodes.map(n =>
              updatedNodeIds.has(n.id)
                ? toolResult.nodes.find(un => un.id === n.id)
                : n
            );
            const newNodes = toolResult.nodes.filter(n =>
              !currentNodes.some(cn => cn.id === n.id)
            );
            updateVisualization([...mergedNodes, ...newNodes], currentEdges);
          }
        }
        else if (toolResult.nodes && toolResult.nodes.length > 0) {
          const filteredNodes = filterCommunityNodes(toolResult.nodes);
          updateVisualization(filteredNodes, toolResult.edges || []);
        }
      }

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
        content: t('chat.error_prefix', { message: err.message }),
        timestamp: new Date(),
      });
      setError(err.message);
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
        setError(t('chat.extract_error'));
      }
    } catch (err) {
      console.error('[ChatPanel] Upload error:', err);
      setError(t('chat.upload_error', { message: err.message }));
    } finally {
      setIsUploading(false);
      if (fileInputRef.current) {
        fileInputRef.current.value = '';
      }
    }
  };

  const handleRemoveFile = () => {
    setUploadedFile(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = '';
    }
  };

  const handleApproveProposal = async (proposal) => {
    const msg = t('chat.approve_node', { name: proposal.node.name });
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
        content: t('chat.error_prefix', { message: err.message }),
        timestamp: new Date(),
      });
    } finally {
      setIsProcessing(false);
    }
  };

  const handleRejectProposal = async (proposal) => {
    const msg = t('chat.reject_node');
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

  const handleConfirmDelete = async (deleteConfirmation) => {
    const msg = t('chat.confirm_delete');
    addChatMessage({ role: 'user', content: msg, timestamp: new Date() });
    setIsProcessing(true);

    try {
      const conversationMessages = chatMessages
        .filter(m => m.id !== 'welcome')
        .map(m => ({ role: m.role, content: m.content }));
      conversationMessages.push({ role: 'user', content: msg });

      const response = await api.sendChatMessage(conversationMessages);

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
        content: t('chat.error_prefix', { message: err.message }),
        timestamp: new Date(),
      });
    } finally {
      setIsProcessing(false);
    }
  };

  const handleCancelDelete = () => {
    addChatMessage({
      role: 'assistant',
      content: t('chat.delete_cancelled'),
      timestamp: new Date(),
    });
  };

  // Summarize selected nodes by type for display
  const selectionSummary = useMemo(() => {
    if (!selectedGraphNodes || selectedGraphNodes.length === 0) return null;
    const byType = {};
    for (const node of selectedGraphNodes) {
      const type = node.type || node.nodeType || 'Unknown';
      if (!byType[type]) byType[type] = [];
      byType[type].push(node);
    }
    return {
      total: selectedGraphNodes.length,
      byType,
      types: Object.entries(byType).map(([type, nodes]) => ({
        type,
        count: nodes.length,
        color: getNodeColor(type),
        names: nodes.map(n => n.name || n.label || '?').slice(0, 3),
      })),
    };
  }, [selectedGraphNodes]);

  // Build context string for selected nodes to send to LLM
  const buildSelectionContext = () => {
    if (!selectedGraphNodes || selectedGraphNodes.length === 0) return '';

    let context = '\n\n[Selected nodes in the visualization:]\n';
    let charCount = context.length;

    for (const node of selectedGraphNodes) {
      const type = node.type || node.nodeType || 'Unknown';
      const name = node.name || node.label || '?';
      const id = node.id || '';
      const desc = node.description || '';
      const summary = node.summary || '';
      const tags = (node.tags || []).join(', ');
      const identifier = node.identifier || node.metadata?.identifier || '';

      let nodeStr = `- ${type}: "${name}" (ID: ${id})`;
      if (summary) nodeStr += `\n  Summary: ${summary}`;
      if (desc) nodeStr += `\n  Description: ${desc}`;
      if (tags) nodeStr += `\n  Tags: ${tags}`;
      if (identifier) nodeStr += `\n  Identifier/URL: ${identifier}`;
      nodeStr += '\n';

      if (charCount + nodeStr.length > MAX_SELECTION_CONTEXT_CHARS) {
        const remaining = selectedGraphNodes.length - selectedGraphNodes.indexOf(node);
        context += `\n(... and ${remaining} more selected nodes, truncated for brevity. Use get_node_details with the IDs above to get more information.)\n`;
        break;
      }

      context += nodeStr;
      charCount += nodeStr.length;
    }

    return context;
  };

  const formatTime = (timestamp) => {
    if (!timestamp) return '';
    const date = new Date(timestamp);
    const locale = language === 'sv' ? 'sv-SE' : 'en-US';
    return date.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' });
  };

  // Minimized state
  if (!chatPanelOpen) {
    return (
      <div className="chat-panel-minimized" onClick={toggleChatPanel}>
        <ChatDotsFill size={18} className="chat-panel-minimized-icon" />
        <span className="chat-panel-minimized-text">Graph assistant</span>
      </div>
    );
  }

  // Expanded state
  return (
    <div className="chat-panel-floating">
      <div className="chat-header">
        <div className="chat-header-left" onClick={toggleChatPanel} style={{ cursor: 'pointer' }}>
          <ChatDotsFill size={16} />
          <h3>Graph assistant</h3>
        </div>
        <button className="chat-collapse-button" onClick={toggleChatPanel} title="Minimize">
          <ChevronRight size={18} />
        </button>
      </div>

      <div className="chat-messages">
        {chatMessages.map((msg, idx) => (
          <div key={msg.id || idx} className={`chat-message ${msg.role}`}>
            <div className="message-content">
              {msg.content}

              {msg.role === 'user' && idx === chatMessages.length - 1 && isProcessing && (
                <div className="message-loading">
                  <div className="loading-dots">
                    <span></span>
                    <span></span>
                    <span></span>
                  </div>
                  <span className="loading-text">{t('chat.processing')}</span>
                </div>
              )}

              {msg.proposal && (
                <div className="proposal-card">
                  <h4>{t('proposal.title')}</h4>
                  <div className="proposal-details">
                    <p><strong>{t('proposal.type')}</strong> {msg.proposal.node.type}</p>
                    <p><strong>{t('proposal.name')}</strong> {msg.proposal.node.name}</p>
                    <p><strong>{t('proposal.description')}</strong> {msg.proposal.node.description}</p>
                    {msg.proposal.node.communities?.length > 0 && (
                      <p><strong>Communities:</strong> {msg.proposal.node.communities.join(', ')}</p>
                    )}
                  </div>

                  {msg.proposal.similar_nodes?.length > 0 && (
                    <div className="similar-nodes-warning">
                      <p><strong>{t('proposal.similar_found')}</strong></p>
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
                      {t('proposal.approve')}
                    </button>
                    <button
                      className="reject-button"
                      onClick={() => handleRejectProposal(msg.proposal)}
                      disabled={isProcessing}
                    >
                      {t('proposal.reject')}
                    </button>
                  </div>
                </div>
              )}

              {msg.deleteConfirmation && (
                <div className="delete-card">
                  <h4>{t('delete_confirmation.title')}</h4>
                  <div className="proposal-details">
                    <p><strong>{t('delete_confirmation.nodes_to_delete')}</strong></p>
                    <ul>
                      {msg.deleteConfirmation.nodes_to_delete?.map((node, i) => (
                        <li key={i}>{node.name} ({node.type})</li>
                      ))}
                    </ul>
                    <p><strong>{t('delete_confirmation.affected_edges')}</strong> {msg.deleteConfirmation.affected_edges?.length || 0}</p>
                  </div>

                  <div className="similar-nodes-warning">
                    <p><strong>{t('delete_confirmation.warning')}</strong></p>
                  </div>

                  <div className="proposal-actions">
                    <button
                      className="reject-button"
                      onClick={() => handleConfirmDelete(msg.deleteConfirmation)}
                      disabled={isProcessing}
                    >
                      {t('delete_confirmation.confirm')}
                    </button>
                    <button
                      className="approve-button"
                      onClick={handleCancelDelete}
                      disabled={isProcessing}
                    >
                      {t('delete_confirmation.cancel')}
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
        {selectionSummary && (
          <div className="selection-indicator">
            <div className="selection-indicator-content">
              <span className="selection-indicator-label">
                {t('chat.selected_nodes', { count: selectionSummary.total })}
              </span>
              <div className="selection-indicator-types">
                {selectionSummary.types.map(({ type, count, color, names }) => (
                  <span key={type} className="selection-type-chip" title={names.join(', ')}>
                    <span className="selection-type-dot" style={{ backgroundColor: color }} />
                    {type} ({count})
                  </span>
                ))}
              </div>
            </div>
            <button
              className="selection-clear-button"
              onClick={clearSelectedGraphNodes}
              title={t('chat.clear_selection')}
            >
              <XCircleFill size={14} />
            </button>
          </div>
        )}

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
              title={t('chat.remove_file')}
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
            ? t('chat.placeholder_with_file')
            : selectionSummary
              ? t('chat.placeholder_with_selection')
              : t('chat.placeholder')}
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
            title={t('chat.upload_tooltip')}
          >
            {isUploading ? t('chat.uploading') : t('chat.upload')}
          </button>
          <button
            className="chat-send-button"
            onClick={handleSend}
            disabled={(!inputValue.trim() && !uploadedFile) || isProcessing}
          >
            {isProcessing ? t('chat.processing') : t('chat.send')}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ChatPanel;
