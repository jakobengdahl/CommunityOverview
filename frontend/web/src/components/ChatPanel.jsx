import { useState, useEffect, useRef, useMemo } from 'react';
import {
  ChatDotsFill,
  ChevronRight,
  ChevronLeft,
  XCircleFill,
  Robot,
  Mortarboard,
} from 'react-bootstrap-icons';
import useGraphStore from '../store/graphStore';
import { useI18n } from '../i18n';
import * as api from '../services/api';
import { positionNewNodes } from '@community-graph/ui-graph-canvas';
import ExpertAgentSelector from './ExpertAgentSelector';
import CollectionForm from './CollectionForm';
import MarkdownMessage from './MarkdownMessage';
import './ChatPanel.css';

/** Extract a present_form spec from a chat response, or null if none. */
const formFromResponse = (response) =>
  response?.toolResult?.action === 'present_form' ? response.toolResult.form : null;

/** Render one answer value for the human-readable summary. */
const formatAnswerValue = (value) => {
  if (Array.isArray(value)) return value.join(', ');
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value ?? '');
};

/** Max characters of node context to include with a message to the LLM */
const MAX_SELECTION_CONTEXT_CHARS = 6000;

/** Returns true when a graph node is of type "Skill".
 *  Checks all known locations for the type string to be resilient against
 *  different node-object shapes (plain data object, raw React-Flow node, etc.).
 */
const isSkillNode = (node) =>
  node.nodeType === 'Skill' ||
  node.type === 'Skill' ||
  node.data?.nodeType === 'Skill' ||
  node.data?.type === 'Skill';

function ChatPanel({ collectionShortName }) {
  const {
    chatMessages,
    addChatMessage,
    updateChatMessage,
    nodes,
    edges,
    addNodesToVisualization,
    updateVisualization,
    clearVisualization,
    chatPanelOpen,
    toggleChatPanel,
    selectedGraphNodes,
    federationDepth,
    stats,
    clearSelectedGraphNodes,
    activeExperts,
    availableExperts,
    showMinimap,
    presentation,
    startGuide,
    guideChatInput,
    clearGuideChatInput,
    getNodeColor,
    requestCloseMenus,
    modelProfiles,
    modelProfileSelectionEnabled,
    selectedModelProfileId,
    setSelectedModelProfileId,
  } = useGraphStore();

  const { t, language } = useI18n();
  const effectiveMaxDepth = Math.max(1, stats?.federation?.max_selectable_depth || 1);

  // A session switch bumps assistantSessionEpoch. An assistant request captures
  // the epoch before awaiting; if it changed by the time the reply arrives, the
  // user switched sessions mid-request and the response belongs to a session
  // that is no longer active — discard it so it can neither append to the new
  // session's chat nor apply its canvas side-effects.
  const isStaleAssistantEpoch = (epoch) => useGraphStore.getState().assistantSessionEpoch !== epoch;

  const [inputValue, setInputValue] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [uploadedFile, setUploadedFile] = useState(null);
  const [isUploading, setIsUploading] = useState(false);
  const [error, setError] = useState(null);

  const messagesEndRef = useRef(null);
  const fileInputRef = useRef(null);
  const handleSendRef = useRef(null);

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

  // Guide: animate typing into chat input
  useEffect(() => {
    if (!guideChatInput) return;
    const { text, animated, auto_send } = guideChatInput;
    clearGuideChatInput();

    if (!animated) {
      setInputValue(text);
      if (auto_send) setTimeout(() => handleSendRef.current?.(), 0);
      return;
    }

    let i = 0;
    setInputValue('');
    const interval = setInterval(() => {
      i++;
      setInputValue(text.slice(0, i));
      if (i >= text.length) {
        clearInterval(interval);
        if (auto_send) setTimeout(() => handleSendRef.current?.(), 300);
      }
    }, 30);
    return () => clearInterval(interval);
  }, [guideChatInput]); // eslint-disable-line react-hooks/exhaustive-deps

  const filterCommunityNodes = (nodeList) => {
    return nodeList.filter((n) => n.type !== 'Community' && n.data?.type !== 'Community');
  };

  // Apply the visualization side-effects a chat response's toolResult implies
  // (save view, clear, add/replace/update nodes, marks, guides). Shared by the
  // typed-message path (handleSend) and the form-submit path (handleSubmitForm)
  // so both entry points behave identically.
  const applyToolResultSideEffects = async (toolResult) => {
    if (!toolResult) return;
    if (toolResult.action === 'save_view' || toolResult.action === 'save_visualization') {
      const viewName = toolResult.name;
      const currentNodes = useGraphStore.getState().nodes;
      const viewNode = {
        name: viewName,
        type: 'SavedView',
        description: t('notifications.saved_view_description', { name: viewName }),
        summary: t('notifications.saved_view_summary', { count: currentNodes.length }),
        metadata: { node_ids: currentNodes.map((n) => n.id) },
        communities: [],
      };
      try {
        await api.addNodes([viewNode], []);
      } catch (err) {
        console.error('[ChatPanel] Failed to save view:', err);
      }
    } else if (toolResult.action === 'clear_visualization') {
      clearVisualization();
    } else if (toolResult.action === 'load_visualization') {
      if (toolResult.nodes && toolResult.nodes.length > 0) {
        const filteredNodes = filterCommunityNodes(toolResult.nodes);
        updateVisualization(filteredNodes, toolResult.edges || []);
      }
    } else if (toolResult.action === 'add_to_visualization') {
      if (toolResult.nodes && toolResult.nodes.length > 0) {
        const filteredNodes = filterCommunityNodes(toolResult.nodes);
        const currentNodes = useGraphStore.getState().nodes;
        const allEdges = [...edges, ...(toolResult.edges || [])];
        const positionedNodes = positionNewNodes(filteredNodes, currentNodes, allEdges);
        addNodesToVisualization(positionedNodes, toolResult.edges || []);
      }
    } else if (toolResult.action === 'update_in_visualization') {
      if (toolResult.nodes && toolResult.nodes.length > 0) {
        const {
          nodes: currentNodes,
          edges: currentEdges,
          updateVisualization: update,
        } = useGraphStore.getState();
        const updatedNodeIds = new Set(toolResult.nodes.map((n) => n.id));
        const mergedNodes = currentNodes.map((n) =>
          updatedNodeIds.has(n.id) ? toolResult.nodes.find((un) => un.id === n.id) : n
        );
        const newNodes = toolResult.nodes.filter((n) => !currentNodes.some((cn) => cn.id === n.id));
        update([...mergedNodes, ...newNodes], currentEdges);
      }
    } else if (toolResult.action === 'mark_nodes') {
      useGraphStore.getState().setNodeMarks(toolResult.marks || []);
    } else if (toolResult.action === 'start_guide') {
      const guideId = toolResult.guide_id;
      const guides = useGraphStore.getState().presentation?.guides || [];
      const guide = guides.find((g) => g.id === guideId);
      if (guide) {
        startGuide(guide);
      } else {
        console.warn(
          `[ChatPanel] start_guide: guide "${guideId}" not found in presentation config`
        );
      }
    } else if (toolResult.nodes && toolResult.nodes.length > 0) {
      const filteredNodes = filterCommunityNodes(toolResult.nodes);
      updateVisualization(filteredNodes, toolResult.edges || []);
    }

    // Execute any pure-action tools that co-occurred with present_form in the same
    // turn.  The backend emits them as extra_actions so they are not dropped when
    // present_form wins the single toolResult.action slot.
    if (toolResult.extra_actions && toolResult.extra_actions.length > 0) {
      for (const extra of toolResult.extra_actions) {
        await applyToolResultSideEffects(extra);
      }
    }
  };

  const handleSend = async () => {
    if ((!inputValue.trim() && !uploadedFile) || isProcessing) return;

    let messageContent = inputValue.trim();

    if (uploadedFile) {
      const fileContext = t('chat.file_context', {
        filename: uploadedFile.filename,
        text: uploadedFile.text,
      });
      messageContent = messageContent
        ? messageContent + fileContext
        : t('chat.analyze_document', { fileContext });
    }

    // Extract Skill-node instructions (sent as system context, not shown in chat)
    const skillsContext = buildSkillsContext();
    // Append regular selected-node context to the user message (not shown in chat bubble)
    const selectionContext = buildSelectionContext();
    const messageForLLM = selectionContext ? messageContent + selectionContext : messageContent;

    const activeSkillNodes = (selectedGraphNodes || []).filter(isSkillNode);
    const userMessage = {
      role: 'user',
      content: messageContent, // Show only the user's text in chat
      timestamp: new Date(),
      hasFile: !!uploadedFile,
      filename: uploadedFile?.filename,
      hasSelection: selectedGraphNodes.length > 0,
      selectionCount: selectedGraphNodes.length,
      activeSkillCount: activeSkillNodes.length,
      activeSkillNames: activeSkillNodes.map((n) => n.name || n.label || '?'),
    };
    addChatMessage(userMessage);
    setInputValue('');
    setUploadedFile(null);
    setIsProcessing(true);
    setError(null);
    const requestEpoch = useGraphStore.getState().assistantSessionEpoch;

    try {
      const conversationMessages = chatMessages
        .filter((m) => m.role !== 'system' && m.id !== 'welcome')
        .map((m) => ({ role: m.role, content: m.llmContent ?? m.content }));

      conversationMessages.push({ role: 'user', content: messageForLLM });

      const response = await api.sendChatMessage(conversationMessages, null, {
        federationDepth,
        modelProfileId: selectedModelProfileId || undefined,
        expertAgentId:
          activeExperts.length > 0 ? activeExperts[activeExperts.length - 1] : undefined,
        skillsContext: skillsContext || undefined,
        collectionShortName,
        visibleNodeIds: nodes.map((n) => n.id),
        selectedNodeIds: selectedGraphNodes.map((n) => n.id),
      });

      if (isStaleAssistantEpoch(requestEpoch)) return;

      console.log('[ChatPanel] Response:', response);

      const toolResult = response.toolResult;

      await applyToolResultSideEffects(toolResult);

      const assistantMessage = {
        role: 'assistant',
        content: response.content,
        timestamp: new Date(),
        toolUsed: response.toolUsed,
        proposal: toolResult?.proposed_node
          ? {
              node: toolResult.proposed_node,
              similar_nodes: toolResult.similar_nodes || [],
            }
          : null,
        deleteConfirmation: toolResult?.requires_confirmation
          ? {
              nodes_to_delete: toolResult.nodes_to_delete,
              affected_edges: toolResult.affected_edges,
              node_ids: toolResult.node_ids,
            }
          : null,
        form: formFromResponse(response),
      };
      addChatMessage(assistantMessage);
    } catch (err) {
      if (isStaleAssistantEpoch(requestEpoch)) return;
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

  // Keep ref current so the guide animation can call the latest handleSend after animation
  // completes, picking up the fully-typed inputValue rather than a stale closure.
  handleSendRef.current = handleSend;

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
      const result = await api.uploadFile(file, false, {
        modelProfileId: selectedModelProfileId || undefined,
      });
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
    const requestEpoch = useGraphStore.getState().assistantSessionEpoch;

    try {
      const conversationMessages = chatMessages
        .filter((m) => m.id !== 'welcome')
        .map((m) => ({ role: m.role, content: m.llmContent ?? m.content }));
      conversationMessages.push({ role: 'user', content: msg });

      const response = await api.sendChatMessage(conversationMessages, null, {
        federationDepth,
        expertAgentId:
          activeExperts.length > 0 ? activeExperts[activeExperts.length - 1] : undefined,
        collectionShortName,
      });

      if (isStaleAssistantEpoch(requestEpoch)) return;

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
      if (isStaleAssistantEpoch(requestEpoch)) return;
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
    const requestEpoch = useGraphStore.getState().assistantSessionEpoch;

    try {
      const conversationMessages = chatMessages
        .filter((m) => m.id !== 'welcome')
        .map((m) => ({ role: m.role, content: m.llmContent ?? m.content }));
      conversationMessages.push({ role: 'user', content: msg });

      const response = await api.sendChatMessage(conversationMessages, null, {
        federationDepth,
        expertAgentId:
          activeExperts.length > 0 ? activeExperts[activeExperts.length - 1] : undefined,
        collectionShortName,
      });
      if (isStaleAssistantEpoch(requestEpoch)) return;
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
    const requestEpoch = useGraphStore.getState().assistantSessionEpoch;

    try {
      const conversationMessages = chatMessages
        .filter((m) => m.id !== 'welcome')
        .map((m) => ({ role: m.role, content: m.llmContent ?? m.content }));
      conversationMessages.push({ role: 'user', content: msg });

      const response = await api.sendChatMessage(conversationMessages, null, {
        federationDepth,
        expertAgentId:
          activeExperts.length > 0 ? activeExperts[activeExperts.length - 1] : undefined,
        collectionShortName,
      });

      if (isStaleAssistantEpoch(requestEpoch)) return;

      if (deleteConfirmation.node_ids) {
        const deletedIds = new Set(deleteConfirmation.node_ids);
        const newNodes = nodes.filter((n) => !deletedIds.has(n.id));
        const newEdges = edges.filter(
          (e) => !deletedIds.has(e.source) && !deletedIds.has(e.target)
        );
        updateVisualization(newNodes, newEdges);
      }

      addChatMessage({
        role: 'assistant',
        content: response.content,
        timestamp: new Date(),
        toolUsed: response.toolUsed,
      });
    } catch (err) {
      if (isStaleAssistantEpoch(requestEpoch)) return;
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

  const handleSubmitForm = async (messageId, answers) => {
    if (isProcessing) return;

    updateChatMessage(messageId, { formSubmitted: true });

    const readable = answers
      .map((a) => `- ${a.label || a.field_id}: ${formatAnswerValue(a.value)}`)
      .join('\n');
    const llmContent =
      `[Form answers submitted]\n${readable}\n\n` +
      `Structured answers to store with save_collection_response: ${JSON.stringify(answers)}`;

    // Persist llmContent on the message so the structured payload (not just the
    // human-readable summary) survives into later turns' history — matching the
    // kiosk, and keeping the answers aggregatable if the save is deferred.
    addChatMessage({ role: 'user', content: readable, llmContent, timestamp: new Date() });
    setIsProcessing(true);
    const requestEpoch = useGraphStore.getState().assistantSessionEpoch;

    try {
      const conversationMessages = chatMessages
        .filter((m) => m.role !== 'system' && m.id !== 'welcome')
        .map((m) => ({ role: m.role, content: m.llmContent ?? m.content }));
      conversationMessages.push({ role: 'user', content: llmContent });

      const response = await api.sendChatMessage(conversationMessages, null, {
        federationDepth,
        expertAgentId:
          activeExperts.length > 0 ? activeExperts[activeExperts.length - 1] : undefined,
        collectionShortName,
      });

      if (isStaleAssistantEpoch(requestEpoch)) return;

      await applyToolResultSideEffects(response.toolResult);

      addChatMessage({
        role: 'assistant',
        content: response.content,
        timestamp: new Date(),
        toolUsed: response.toolUsed,
        form: formFromResponse(response),
      });
    } catch (err) {
      if (isStaleAssistantEpoch(requestEpoch)) return;
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

  // Summarize selected nodes — skill nodes and regular nodes are split so the UI
  // can show skill nodes as active "persona" chips separate from the node-type chips.
  const selectionSummary = useMemo(() => {
    if (!selectedGraphNodes || selectedGraphNodes.length === 0) return null;

    const skillNodes = selectedGraphNodes.filter(isSkillNode);
    const regularNodes = selectedGraphNodes.filter((n) => !isSkillNode(n));

    const byType = {};
    for (const node of regularNodes) {
      const type = node.type || node.nodeType || 'Unknown';
      if (!byType[type]) byType[type] = [];
      byType[type].push(node);
    }

    return {
      total: selectedGraphNodes.length,
      skillNodes: skillNodes.map((n) => ({ id: n.id, name: n.name || n.label || '?' })),
      regularCount: regularNodes.length,
      types: Object.entries(byType).map(([type, nodes]) => ({
        type,
        count: nodes.length,
        color: getNodeColor(type),
        names: nodes.map((n) => n.name || n.label || '?').slice(0, 3),
      })),
    };
  }, [selectedGraphNodes]);

  // Build temporary system context from selected Skill nodes.
  // Injected as a skills_override after the base system prompt so it has
  // recency precedence over the base instructions.
  const buildSkillsContext = () => {
    const skillNodes = (selectedGraphNodes || []).filter(isSkillNode);
    if (skillNodes.length === 0) return null;

    const parts = [
      'ACTIVE SKILL INSTRUCTIONS — YOU MUST APPLY THESE TO THIS RESPONSE:',
      'The user has selected the following skills. These instructions OVERRIDE your default behavior and style for this response. Apply them precisely.',
    ];
    for (const node of skillNodes) {
      const name = node.name || node.label || '?';
      const description = node.description || '';
      const content = node.metadata?.content || '';
      const whenToUse = node.metadata?.when_to_use || '';

      let block = `<skill name="${name}">`;
      if (content) {
        // content is the primary SKILL.md body — use it directly as instructions
        block += `\n${content}`;
      } else {
        // Fall back to description/when_to_use as imperative instructions
        if (description) block += `\nInstruction: ${description}`;
        if (whenToUse) block += `\nApply when: ${whenToUse}`;
      }
      block += '\n</skill>';
      parts.push(block);
    }
    parts.push('END OF SKILL INSTRUCTIONS. Apply the above to your entire response.');
    return parts.join('\n\n');
  };

  // Build context string for selected non-Skill nodes to append to the user message.
  const buildSelectionContext = () => {
    const regularNodes = (selectedGraphNodes || []).filter((n) => !isSkillNode(n));
    if (regularNodes.length === 0) return '';

    let context = '\n\n[Selected nodes in the visualization:]\n';
    let charCount = context.length;

    for (const node of regularNodes) {
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
        const remaining = regularNodes.length - regularNodes.indexOf(node);
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
    <div
      className={`chat-panel-floating${!showMinimap ? ' minimap-hidden' : ''}`}
      id="guide-target-chat"
    >
      <div className="chat-header">
        <div className="chat-header-left" onClick={toggleChatPanel} style={{ cursor: 'pointer' }}>
          <ChatDotsFill size={16} />
          <h3>Graph assistant</h3>
          {effectiveMaxDepth > 1 && (
            <span className="chat-depth-indicator" title={t('federation.depth_indicator_tooltip')}>
              {t('federation.depth_indicator', {
                current: federationDepth,
                max: effectiveMaxDepth,
              })}
            </span>
          )}
        </div>
        <button className="chat-collapse-button" onClick={toggleChatPanel} title="Minimize">
          <ChevronRight size={18} />
        </button>
      </div>

      <div className="chat-messages">
        {chatMessages
          .filter((m) => !m.expertJoinNotification)
          .map((msg, idx) => (
            <div
              key={msg.id || idx}
              className={`chat-message ${msg.role}${msg.role === 'expert' ? ' expert-message' : ''}${msg.isSystemEvent ? ' expert-system-event' : ''}`}
              style={
                msg.role === 'expert'
                  ? { '--expert-color': msg.expertColor || '#9CA3AF' }
                  : undefined
              }
            >
              {msg.role === 'expert' && !msg.isSystemEvent && (
                <div className="expert-message-header">
                  <Robot size={11} style={{ color: msg.expertColor }} />
                  <span className="expert-message-name" style={{ color: msg.expertColor }}>
                    {msg.expertName}
                  </span>
                </div>
              )}
              <div className="message-content">
                {msg.role === 'assistant' || msg.role === 'expert' ? (
                  <MarkdownMessage>{msg.content}</MarkdownMessage>
                ) : (
                  msg.content
                )}

                {msg.role === 'user' && msg.activeSkillCount > 0 && (
                  <div className="message-skill-tag">
                    <Mortarboard size={10} />
                    {msg.activeSkillNames.join(', ')}
                  </div>
                )}

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
                      <p>
                        <strong>{t('proposal.type')}</strong> {msg.proposal.node.type}
                      </p>
                      <p>
                        <strong>{t('proposal.name')}</strong> {msg.proposal.node.name}
                      </p>
                      <p>
                        <strong>{t('proposal.description')}</strong> {msg.proposal.node.description}
                      </p>
                      {msg.proposal.node.communities?.length > 0 && (
                        <p>
                          <strong>Communities:</strong> {msg.proposal.node.communities.join(', ')}
                        </p>
                      )}
                    </div>

                    {msg.proposal.similar_nodes?.length > 0 && (
                      <div className="similar-nodes-warning">
                        <p>
                          <strong>{t('proposal.similar_found')}</strong>
                        </p>
                        <ul>
                          {msg.proposal.similar_nodes.map((sim, i) => (
                            <li key={i}>
                              {sim.node?.name || sim.name} (
                              {Math.round((sim.similarity_score || sim.score) * 100)}% match)
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
                      <p>
                        <strong>{t('delete_confirmation.nodes_to_delete')}</strong>
                      </p>
                      <ul>
                        {msg.deleteConfirmation.nodes_to_delete?.map((node, i) => (
                          <li key={i}>
                            {node.name} ({node.type})
                          </li>
                        ))}
                      </ul>
                      <p>
                        <strong>{t('delete_confirmation.affected_edges')}</strong>{' '}
                        {msg.deleteConfirmation.affected_edges?.length || 0}
                      </p>
                    </div>

                    <div className="similar-nodes-warning">
                      <p>
                        <strong>{t('delete_confirmation.warning')}</strong>
                      </p>
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

                {msg.form && (
                  <CollectionForm
                    form={msg.form}
                    submitted={!!msg.formSubmitted}
                    disabled={isProcessing}
                    onSubmit={(answers) => handleSubmitForm(msg.id, answers)}
                    labels={{
                      submit: t('chat.form_submit'),
                      submitted: t('chat.form_submitted'),
                      requiredHint: t('chat.form_required'),
                    }}
                  />
                )}
              </div>
              <div className="message-timestamp">{formatTime(msg.timestamp)}</div>
            </div>
          ))}
        <div ref={messagesEndRef} />
      </div>

      {error && <div className="chat-error">{error}</div>}

      <div className="chat-input-container">
        {selectionSummary && (
          <div className="selection-indicator">
            <div className="selection-indicator-content">
              {selectionSummary.skillNodes.length > 0 && (
                <div
                  className={`skill-nodes-indicator${selectionSummary.regularCount > 0 ? ' has-regular-nodes' : ''}`}
                >
                  <Mortarboard size={11} className="skill-nodes-icon" />
                  <span className="skill-nodes-label">Skills active:</span>
                  {selectionSummary.skillNodes.map((skill) => (
                    <span key={skill.id} className="skill-node-chip">
                      {skill.name}
                    </span>
                  ))}
                </div>
              )}
              {selectionSummary.regularCount > 0 && (
                <>
                  <span className="selection-indicator-label">
                    {t('chat.selected_nodes', { count: selectionSummary.regularCount })}
                  </span>
                  <div className="selection-indicator-types">
                    {selectionSummary.types.map(({ type, count, color, names }) => (
                      <span key={type} className="selection-type-chip" title={names.join(', ')}>
                        <span className="selection-type-dot" style={{ backgroundColor: color }} />
                        {type} ({count})
                      </span>
                    ))}
                  </div>
                </>
              )}
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
          onFocus={requestCloseMenus}
          placeholder={
            uploadedFile
              ? t('chat.placeholder_with_file')
              : selectionSummary?.skillNodes?.length > 0
                ? `Skill${selectionSummary.skillNodes.length > 1 ? 's' : ''} active: ${selectionSummary.skillNodes.map((s) => s.name).join(', ')} — ask a question...`
                : selectionSummary
                  ? t('chat.placeholder_with_selection')
                  : t('chat.placeholder')
          }
          rows={3}
          disabled={isProcessing}
        />

        {activeExperts.length > 0 && (
          <div className="active-experts-indicator">
            <Robot size={11} className="active-experts-icon" />
            <span className="active-experts-label">
              {activeExperts.map((id) => {
                const agent = availableExperts.find((a) => a.id === id);
                if (!agent) return null;
                const name = language === 'sv' ? agent.name : agent.name_en || agent.name;
                return (
                  <span
                    key={id}
                    className="active-expert-chip"
                    style={{ borderColor: agent.color }}
                  >
                    <span className="active-expert-dot" style={{ backgroundColor: agent.color }} />
                    {name}
                  </span>
                );
              })}
            </span>
          </div>
        )}

        <div className="button-row">
          <ExpertAgentSelector />
          {modelProfiles.length > 0 && (
            <select
              className="model-profile-select"
              aria-label={t('chat.model_profile')}
              value={selectedModelProfileId || ''}
              onChange={(e) => setSelectedModelProfileId(e.target.value || null)}
              disabled={isProcessing || !modelProfileSelectionEnabled}
              title={
                modelProfileSelectionEnabled
                  ? t('chat.model_profile')
                  : t('chat.model_profile_selection_disabled')
              }
            >
              {modelProfiles.map((profile) => (
                <option key={profile.id} value={profile.id}>
                  {profile.name || profile.id}
                  {profile.default ? ' (default)' : ''}
                </option>
              ))}
            </select>
          )}
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
