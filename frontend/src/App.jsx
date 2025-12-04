import { useState, useEffect } from 'react'
import './App.css'
import Header from './components/Header'
import ChatPanel from './components/ChatPanel'
import VisualizationPanel from './components/VisualizationPanel'
import useGraphStore from './store/graphStore'
import { loadDemoData } from './services/demoData'

function App() {
  const { selectedCommunities, updateVisualization } = useGraphStore();

  // Load communities from URL query on initial load
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const communitiesParam = params.getAll('community');

    if (communitiesParam.length > 0) {
      useGraphStore.getState().setSelectedCommunities(communitiesParam);
    }
  }, []);

  // Load demo data when communities are selected
  useEffect(() => {
    if (selectedCommunities.length > 0) {
      loadDemoData(updateVisualization, selectedCommunities);
    }
  }, [selectedCommunities, updateVisualization]);

  return (
    <div className="app">
      <Header />

      {selectedCommunities.length === 0 ? (
        <div className="no-community-selected">
          <h2>Select at least one community to get started</h2>
          <p>Use the dropdown menu above to select which communities you belong to.</p>
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
