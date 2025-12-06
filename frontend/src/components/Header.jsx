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
  const { selectedCommunities, setSelectedCommunities, apiKey, setApiKey } = useGraphStore();
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);
  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [tempApiKey, setTempApiKey] = useState(apiKey || '');

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
    setIsSettingsOpen(false);
  };

  const handleClearApiKey = () => {
    setApiKey(null);
    setTempApiKey('');
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
                <label htmlFor="api-key-input">
                  Anthropic API Key (Optional)
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
                  placeholder="sk-ant-..."
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
