import { useState } from 'react';
import useGraphStore from '../store/graphStore';
import './Header.css';

// Available communities (hardcoded for now)
const AVAILABLE_COMMUNITIES = [
  'eSam',
  'Myndigheter',
  'Officiell Statistik'
];

function Header() {
  const { selectedCommunities, setSelectedCommunities, apiKey, setApiKey, llmProvider, setLlmProvider } = useGraphStore();
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [tempApiKey, setTempApiKey] = useState(apiKey || '');
  const [tempProvider, setTempProvider] = useState(llmProvider || 'claude');

  const toggleCommunity = (community) => {
    if (selectedCommunities.includes(community)) {
      // Remove
      const updated = selectedCommunities.filter(c => c !== community);
      setSelectedCommunities(updated);
      updateURL(updated);
    } else {
      // Add
      const updated = [...selectedCommunities, community];
      setSelectedCommunities(updated);
      updateURL(updated);
    }
  };

  const updateURL = (communities) => {
    const params = new URLSearchParams();
    communities.forEach(c => params.append('community', c));
    window.history.replaceState({}, '', `?${params.toString()}`);
  };

  const handleSaveApiKey = () => {
    setApiKey(tempApiKey);
    setLlmProvider(tempProvider);
    setIsSettingsOpen(false);
  };

  const handleClearApiKey = () => {
    setApiKey(null);
    setTempApiKey('');
    setLlmProvider(null);
    setTempProvider('claude');
  };

  const handleExportGraph = async () => {
    console.log('[Header] Starting graph export...');
    try {
      // Fetch complete graph from backend instead of just visualization state
      console.log('[Header] Fetching from http://localhost:8000/export_graph');
      const response = await fetch('http://localhost:8000/export_graph');

      console.log('[Header] Response status:', response.status);
      console.log('[Header] Response ok:', response.ok);

      if (!response.ok) {
        const errorText = await response.text();
        console.error('[Header] Export failed with status:', response.status);
        console.error('[Header] Error response:', errorText);
        throw new Error(`Failed to export graph: ${response.status} - ${errorText}`);
      }

      const exportData = await response.json();
      console.log('[Header] Export data received:', {
        nodes: exportData.nodes?.length || 0,
        edges: exportData.edges?.length || 0,
        version: exportData.version,
        exportDate: exportData.exportDate
      });

      const dataStr = JSON.stringify(exportData, null, 2);
      const dataBlob = new Blob([dataStr], { type: 'application/json' });
      const url = URL.createObjectURL(dataBlob);

      const link = document.createElement('a');
      link.href = url;
      link.download = `knowledge-graph-${new Date().toISOString().split('T')[0]}.json`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);

      console.log('[Header] Graph exported successfully');
    } catch (error) {
      console.error('[Header] Error exporting graph:', error);
      console.error('[Header] Error stack:', error.stack);
      alert(`Failed to export graph: ${error.message}`);
    }
  };

  return (
    <header className="app-header">
      <div className="header-left">
        <h1 className="app-title">Community Knowledge Graph</h1>
      </div>

      <div className="header-center">
        <div className="community-selector">
          <button
            className="community-dropdown-toggle"
            onClick={() => setIsDropdownOpen(!isDropdownOpen)}
          >
            {selectedCommunities.length === 0
              ? 'Select Communities'
              : selectedCommunities.join(', ')}
            <span className="dropdown-arrow">▼</span>
          </button>

          {isDropdownOpen && (
            <div className="community-dropdown-menu">
              {AVAILABLE_COMMUNITIES.map(community => (
                <label key={community} className="community-option">
                  <input
                    type="checkbox"
                    checked={selectedCommunities.includes(community)}
                    onChange={() => toggleCommunity(community)}
                  />
                  <span>{community}</span>
                </label>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="header-right">
        <button
          className="export-button"
          onClick={handleExportGraph}
          title="Export Graph"
        >
          💾
        </button>
        <button
          className="settings-button"
          onClick={() => setIsSettingsOpen(!isSettingsOpen)}
          title="Settings"
        >
          ⚙️
        </button>
        {apiKey && <span className="api-key-indicator" title="API Key configured">🔑</span>}
      </div>

      {/* Settings Dialog */}
      {isSettingsOpen && (
        <div className="settings-overlay" onClick={() => setIsSettingsOpen(false)}>
          <div className="settings-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="settings-header">
              <h2>Settings</h2>
              <button
                className="close-button"
                onClick={() => setIsSettingsOpen(false)}
              >
                ✕
              </button>
            </div>
            <div className="settings-body">
              <div className="settings-section">
                <label htmlFor="provider-select">
                  LLM Provider
                </label>
                <p className="settings-description">
                  Select which LLM provider to use. Backend must be configured with the appropriate API key via environment variables, or you can provide your own key below.
                </p>
                <select
                  id="provider-select"
                  className="provider-select"
                  value={tempProvider}
                  onChange={(e) => setTempProvider(e.target.value)}
                >
                  <option value="claude">Claude (Anthropic)</option>
                  <option value="openai">OpenAI (GPT-4)</option>
                </select>
              </div>

              <div className="settings-section">
                <label htmlFor="api-key-input">
                  {tempProvider === 'openai' ? 'OpenAI API Key' : 'Anthropic API Key'} (Optional)
                </label>
                <p className="settings-description">
                  Enter your own API key for temporary use during this session.
                  The key is stored only in memory and will be cleared when you close the app.
                </p>
                <input
                  id="api-key-input"
                  type="password"
                  className="api-key-input"
                  value={tempApiKey}
                  onChange={(e) => setTempApiKey(e.target.value)}
                  placeholder={tempProvider === 'openai' ? 'sk-...' : 'sk-ant-...'}
                />
                <div className="settings-actions">
                  <button
                    className="save-button"
                    onClick={handleSaveApiKey}
                  >
                    Save
                  </button>
                  {apiKey && (
                    <button
                      className="clear-button"
                      onClick={handleClearApiKey}
                    >
                      Clear Key
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </header>
  );
}

export default Header;
