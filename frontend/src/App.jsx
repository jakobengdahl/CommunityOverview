import { useState, useEffect } from 'react'
import './App.css'
import Header from './components/Header'
import ChatPanel from './components/ChatPanel'
import VisualizationPanel from './components/VisualizationPanel'
import useGraphStore from './store/graphStore'

function App() {
  const { selectedCommunities } = useGraphStore();

  // Hämta communities från URL-query vid initial load
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const communitiesParam = params.getAll('community');

    if (communitiesParam.length > 0) {
      useGraphStore.getState().setSelectedCommunities(communitiesParam);
    }
  }, []);

  return (
    <div className="app">
      <Header />

      {selectedCommunities.length === 0 ? (
        <div className="no-community-selected">
          <h2>Välj minst en community för att komma igång</h2>
          <p>Använd dropdown-menyn ovan för att välja vilka communities du tillhör.</p>
        </div>
      ) : (
        <div className="main-content">
          <ChatPanel />
          <VisualizationPanel />
        </div>
      )}
    </div>
  )
}

export default App
