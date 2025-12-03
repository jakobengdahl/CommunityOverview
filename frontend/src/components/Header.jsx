import { useState } from 'react';
import useGraphStore from '../store/graphStore';
import './Header.css';

// Tillgängliga communities (hårdkodade för nu)
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
      // Ta bort
      const updated = selectedCommunities.filter(c => c !== community);
      setSelectedCommunities(updated);
      updateURL(updated);
    } else {
      // Lägg till
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
              ? 'Välj Communities'
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
        {/* Logo eller annan info kan läggas här */}
      </div>
    </header>
  );
}

export default Header;
