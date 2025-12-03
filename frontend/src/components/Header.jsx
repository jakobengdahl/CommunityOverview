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
  const { selectedCommunities, setSelectedCommunities } = useGraphStore();
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);

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
        {/* Logo or other info can be added here */}
      </div>
    </header>
  );
}

export default Header;
