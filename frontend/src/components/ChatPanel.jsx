import { useState, useEffect, useRef } from 'react';
import useGraphStore from '../store/graphStore';
import { sendMessageToBackend, uploadFileToBackend, executeTool, downloadDocumentFromUrl } from '../services/api';
// import { addNodeToDemoData, loadDemoData } from '../services/demoData'; // REMOVED: Using backend now
import './ChatPanel.css';

function ChatPanel() {
  const {
    chatMessages,
    addChatMessage,
    selectedCommunities,
    updateVisualization,
    setLoading,
    setError,
    clearError,
    nodes, // Need existing nodes to capture state
    hiddenNodeIds,
    setHiddenNodeIds
  } = useGraphStore();
  const [inputValue, setInputValue] = useState('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [uploadedFile, setUploadedFile] = useState(null); // { filename, text }
  const [isUploading, setIsUploading] = useState(false);
  const messagesEndRef = useRef(null);
  const fileInputRef = useRef(null);

  // Auto-scroll to latest message
  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    scrollToBottom();
  }, [chatMessages]);

  // Helper function to detect URLs in text
  const extractUrls = (text) => {
    const urlRegex = /(https?:\/\/[^\s]+)/g;
    return text.match(urlRegex) || [];
  };

  const handleSend = async () => {
    if ((!inputValue.trim() && !uploadedFile) || isProcessing) return;

    // Check if message contains URLs to documents
    const urls = extractUrls(inputValue);
    const documentUrls = urls.filter(url =>
      url.match(/\.(pdf|docx?|txt)(\?|$)/i) ||
      url.includes('/document/') ||
      url.includes('/file/')
    );

    // If document URL detected and no file uploaded yet, try to download it
    if (documentUrls.length > 0 && !uploadedFile) {
      setIsProcessing(true);
      clearError();

      try {
        // Try to download the first document URL
        const result = await downloadDocumentFromUrl(documentUrls[0]);
        if (result.success && result.text) {
          setUploadedFile({
            filename: result.filename,
            text: result.text
          });

          // Add system message
          addChatMessage({
            role: 'system',
            content: `📥 Dokument laddat från URL: ${result.filename}`,
            timestamp: new Date()
          });

          setIsProcessing(false);
          // Don't send yet, let user add their message or just send
          return;
        }
      } catch (err) {
        console.error("URL download error:", err);
        // Continue with normal send if download fails
        addChatMessage({
          role: 'system',
          content: `⚠️ Kunde inte ladda dokument från URL. Fortsätter utan dokument.`,
          timestamp: new Date()
        });
      }
      setIsProcessing(false);
    }

    // Build message content with file if present
    let messageContent = inputValue.trim();

    if (uploadedFile) {
      // Include file information and content in the message
      const fileContext = `\n\n[Uppladdad fil: ${uploadedFile.filename}]\n\nInnehåll:\n${uploadedFile.text}`;
      messageContent = messageContent ? messageContent + fileContext : `Analysera följande dokument:\n${fileContext}`;
    }

    // Add user message
    const userMessage = {
      role: 'user',
      content: messageContent,
      timestamp: new Date(),
      hasFile: !!uploadedFile,
      filename: uploadedFile?.filename
    };
    addChatMessage(userMessage);
    setInputValue('');
    setUploadedFile(null); // Clear uploaded file after sending
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
        content: messageContent
      });

      // Call Backend API
      const response = await sendMessageToBackend(conversationMessages);

      console.log('[ChatPanel] ========== BACKEND RESPONSE ==========');
      console.log('[ChatPanel] Full response:', JSON.stringify(response, null, 2));
      console.log('[ChatPanel] Response keys:', Object.keys(response));
      console.log('[ChatPanel] toolResult exists:', !!response.toolResult);

      if (response.toolResult) {
        console.log('[ChatPanel] toolResult keys:', Object.keys(response.toolResult));
        console.log('[ChatPanel] toolResult.nodes exists:', !!response.toolResult.nodes);
        console.log('[ChatPanel] toolResult.nodes length:', response.toolResult.nodes?.length);
        console.log('[ChatPanel] toolResult.edges exists:', !!response.toolResult.edges);
        console.log('[ChatPanel] toolResult.edges length:', response.toolResult.edges?.length);
      }
      console.log('[ChatPanel] ==========================================');

      // Handle tool results from response
      const toolResult = response.toolResult;

      if (toolResult) {
        // 1. Handle "Save Visualization" signal from backend
        if (toolResult.action === 'save_visualization') {
             const viewName = toolResult.name;
             // Capture state from store
             // Note: store positions might be outdated if we haven't synced.
             // But we added sync logic in VisualizationPanel.
             const currentNodes = useGraphStore.getState().nodes;
             const currentHidden = useGraphStore.getState().hiddenNodeIds;

             const viewData = {
                nodes: currentNodes.map(n => ({ id: n.id, position: n.position })),
                hidden_nodes: currentHidden
             };

             const viewNode = {
                name: viewName,
                type: 'VisualizationView',
                description: `Saved view: ${viewName}`,
                metadata: { view_data: viewData },
                communities: []
             };

             // Execute actual save
             try {
                await executeTool('add_nodes', { nodes: [viewNode], edges: [] });
                // We could add a system message here, but Claude usually replies "Ready to save..."
             } catch (err) {
                console.error("Failed to save view via chat:", err);
                setError(`Failed to save view: ${err.message}`);
             }
        }

        // 2. Handle "Load Visualization" signal
        else if (toolResult.action === 'load_visualization') {
            const viewData = toolResult.view.metadata.view_data;
            if (viewData) {
                if (viewData.hidden_nodes) {
                    setHiddenNodeIds(viewData.hidden_nodes);
                }

                // We need to apply positions.
                // updateVisualization expects full nodes.
                // We should probably just update positions of existing nodes?
                // Or if the view contains specific nodes (filtering?), we might want to filter?
                // The prompt said "which nodes ... are present".
                // If the view implies filtering, we should handle that.
                // But for now, let's assume it just restores positions and hidden state.

                // Note: If the graph currently loaded doesn't have these nodes, we can't show them.
                // We assume the user has searched/loaded the graph or the view contains enough info to load them?
                // The view only stores IDs.
                // So this only works if the nodes are already loaded.
                // Ideally, `load_visualization` should probably return the full node objects too?
                // But `get_visualization` returns the view node itself.

                // TODO: In a real implementation, we might need to fetch the nodes listed in the view
                // if they are not currently in the store.
                // For now, let's update positions for nodes we DO have.
                if (viewData.nodes) {
                   useGraphStore.getState().updateNodePositions(viewData.nodes);
                }
            }
        }

        // 3. Handle standard updates
        else if (toolResult.nodes && toolResult.nodes.length > 0) {
           console.log('[ChatPanel] Calling updateVisualization with:');
           console.log('[ChatPanel]   - Nodes count:', toolResult.nodes.length);
           console.log('[ChatPanel]   - Edges count:', toolResult.edges?.length || 0);
           console.log('[ChatPanel]   - First node:', toolResult.nodes[0]);
           const edges = toolResult.edges || [];
           updateVisualization(toolResult.nodes, edges);
           console.log('[ChatPanel] updateVisualization called successfully');
        } else {
           console.log('[ChatPanel] NOT calling updateVisualization because:');
           console.log('[ChatPanel]   - toolResult.nodes exists:', !!toolResult.nodes);
           console.log('[ChatPanel]   - toolResult.nodes.length:', toolResult.nodes?.length);
        }
      }

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
      console.error('Error communicating with Backend:', error);
      const errorMessage = {
        role: 'assistant',
        content: `❌ Error: ${error.message}`,
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

  const handleApproveProposal = async (proposal) => {
    // Send approval message to backend
    const msg = `Ja, lägg till noden "${proposal.node.name}"`;
    setInputValue(msg);
    // Trigger send automatically would require refactoring, so just setting it for now
    // or we can call handleSend logic directly

    // Simulating user typing "Yes"
    addChatMessage({
        role: 'user',
        content: msg,
        timestamp: new Date()
    });

    setIsProcessing(true);

    try {
        const conversationMessages = chatMessages.map(m => ({ role: m.role, content: m.content }));
        conversationMessages.push({ role: 'user', content: msg });

        const response = await sendMessageToBackend(conversationMessages);

        // Handle tool results
        const toolResult = response.toolResult;
        if (toolResult && toolResult.added_node_ids) {
            // Success!
            // We might want to query the new node to show it
            // For now, let's rely on Claude's confirmation text
        }

         // Add Claude's response
        const assistantMessage = {
            role: 'assistant',
            content: response.content,
            timestamp: new Date(),
            toolUsed: response.toolUsed
        };
        addChatMessage(assistantMessage);

    } catch (error) {
        console.error("Error approving:", error);
        addChatMessage({
            role: 'assistant',
            content: `❌ Error: ${error.message}`
        });
    } finally {
        setIsProcessing(false);
    }
  };

  const handleRejectProposal = (proposal) => {
    // Send rejection message
    const msg = "Nej, lägg inte till noden.";
    setInputValue(msg); // Optional: just puts it in input

    // But better to just send it
    addChatMessage({
        role: 'user',
        content: msg,
        timestamp: new Date()
    });

     // We should also send this to backend so context is maintained?
     // Yes, otherwise Claude thinks we are still waiting
     // ... implementing similar to handleApproveProposal ...
     // For brevity, just calling the backend:

     (async () => {
         setIsProcessing(true);
         try {
             const conversationMessages = chatMessages.map(m => ({ role: m.role, content: m.content }));
             conversationMessages.push({ role: 'user', content: msg });
             const response = await sendMessageToBackend(conversationMessages);
             addChatMessage({
                 role: 'assistant',
                 content: response.content,
                 timestamp: new Date()
             });
         } catch(e) {
             console.error(e);
         } finally {
             setIsProcessing(false);
         }
     })();
  };

  const handleConfirmDelete = async (deleteConfirmation) => {
    setIsProcessing(true);

    const msg = "Ja, radera noderna.";
    addChatMessage({
        role: 'user',
        content: msg,
        timestamp: new Date()
    });

    try {
      const conversationMessages = chatMessages.map(m => ({ role: m.role, content: m.content }));
      conversationMessages.push({ role: 'user', content: msg });

      const response = await sendMessageToBackend(conversationMessages);

      addChatMessage({
          role: 'assistant',
          content: response.content,
          timestamp: new Date(),
          toolUsed: response.toolUsed
      });

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

  const handleFileUpload = async (e) => {
    const file = e.target.files[0];
    if (!file) return;

    setIsUploading(true);
    clearError();

    try {
      const result = await uploadFileToBackend(file);
      if (result.success && result.text) {
        setUploadedFile({
          filename: result.filename,
          text: result.text
        });
      } else {
        setError("Kunde inte extrahera text från filen.");
      }
    } catch (err) {
      console.error("Upload error:", err);
      setError(`Fel vid uppladdning: ${err.message}`);
    } finally {
      setIsUploading(false);
      // Reset input
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
        {/* File indicator - shown when a file is uploaded */}
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
              ✕
            </button>
          </div>
        )}

        <textarea
          className="chat-input"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyPress={handleKeyPress}
          placeholder={uploadedFile ? "Beskriv vad du vill göra med dokumentet..." : "Ställ en fråga om grafen eller ladda upp ett dokument..."}
          rows={3}
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
            {isUploading ? '📤 Laddar upp...' : '📤 Ladda upp'}
          </button>
          <button
            className="chat-send-button"
            onClick={handleSend}
            disabled={(!inputValue.trim() && !uploadedFile) || isProcessing}
          >
            {isProcessing ? 'Thinking...' : 'Send'}
          </button>
        </div>
      </div>
    </div>
  );
}

export default ChatPanel;
