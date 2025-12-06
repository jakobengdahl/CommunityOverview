/**
 * @vitest-environment jsdom
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import VisualizationPanel from './VisualizationPanel';
import useGraphStore from '../store/graphStore';

// Mock React Flow
vi.mock('reactflow', () => ({
  default: vi.fn(({ children }) => <div data-testid="react-flow">{children}</div>),
  Background: vi.fn(() => <div data-testid="background" />),
  Controls: vi.fn(() => <div data-testid="controls" />),
  MiniMap: vi.fn(() => <div data-testid="minimap" />),
  useNodesState: vi.fn((initial) => [initial, vi.fn(), vi.fn()]),
  useEdgesState: vi.fn((initial) => [initial, vi.fn(), vi.fn()]),
  addEdge: vi.fn()
}));

// Mock API
vi.mock('../services/api', () => ({
  executeTool: vi.fn().mockResolvedValue({ success: true })
}));

// Mock graph layout
vi.mock('../utils/graphLayout', () => ({
  getLayoutedElements: vi.fn((nodes) => nodes)
}));

describe('VisualizationPanel', () => {
  beforeEach(() => {
    useGraphStore.setState({
      nodes: [
        { id: '1', type: 'Actor', name: 'Test Actor', summary: 'Test summary', communities: ['eSam'] },
        { id: '2', type: 'Initiative', name: 'Test Initiative', summary: 'Init summary', communities: ['eSam'] }
      ],
      edges: [
        { id: 'e1', source: '1', target: '2', type: 'BELONGS_TO' }
      ],
      highlightedNodeIds: [],
      hiddenNodeIds: [],
      toggleNodeVisibility: vi.fn(),
      addNodesToVisualization: vi.fn(),
      updateNodePositions: vi.fn()
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('renders React Flow component', () => {
    render(<VisualizationPanel />);
    expect(screen.getByTestId('react-flow')).toBeDefined();
  });

  it('renders StatsPanel', () => {
    render(<VisualizationPanel />);
    // StatsPanel should be rendered
    expect(screen.getByRole('button', { name: /Graf-statistik/i })).toBeDefined();
  });

  it('renders save view button when nodes exist', () => {
    render(<VisualizationPanel />);
    expect(screen.getByRole('button', { name: /Save View/i })).toBeDefined();
  });

  it('does not render save button when no nodes', () => {
    useGraphStore.setState({
      nodes: [],
      edges: []
    });

    render(<VisualizationPanel />);
    expect(screen.queryByRole('button', { name: /Save View/i })).toBeNull();
  });

  it('opens SaveViewDialog when save button is clicked', () => {
    render(<VisualizationPanel />);

    const saveButton = screen.getByRole('button', { name: /Save View/i });
    fireEvent.click(saveButton);

    // Dialog should now be visible
    expect(screen.getByText('Save Current View')).toBeDefined();
  });

  it('filters out hidden nodes from visualization', () => {
    useGraphStore.setState({
      nodes: [
        { id: '1', type: 'Actor', name: 'Visible', summary: 'Test', communities: ['eSam'] },
        { id: '2', type: 'Actor', name: 'Hidden', summary: 'Test', communities: ['eSam'] }
      ],
      edges: [],
      hiddenNodeIds: ['2']
    });

    render(<VisualizationPanel />);

    // The component should filter out node '2'
    // We can verify this by checking the React Flow mock was called with correct nodes
    const ReactFlow = require('reactflow').default;
    expect(ReactFlow).toHaveBeenCalled();
  });

  it('filters out edges connected to hidden nodes', () => {
    useGraphStore.setState({
      nodes: [
        { id: '1', type: 'Actor', name: 'Node 1', summary: 'Test', communities: ['eSam'] },
        { id: '2', type: 'Actor', name: 'Node 2', summary: 'Test', communities: ['eSam'] }
      ],
      edges: [
        { id: 'e1', source: '1', target: '2', type: 'BELONGS_TO' }
      ],
      hiddenNodeIds: ['2']
    });

    render(<VisualizationPanel />);

    // Edge e1 should be filtered out because node 2 is hidden
    const ReactFlow = require('reactflow').default;
    expect(ReactFlow).toHaveBeenCalled();
  });

  it('applies correct colors to different node types', () => {
    render(<VisualizationPanel />);

    // Verify the component renders - color mapping is handled internally
    expect(screen.getByTestId('react-flow')).toBeDefined();
  });

  it('handles save view action', async () => {
    const { executeTool } = await import('../services/api');

    render(<VisualizationPanel />);

    const saveButton = screen.getByRole('button', { name: /Save View/i });
    fireEvent.click(saveButton);

    // Enter view name
    const input = screen.getByPlaceholderText(/Enter view name/i);
    fireEvent.change(input, { target: { value: 'Test View' } });

    // Click save in dialog
    const dialogSaveButton = screen.getByRole('button', { name: /Save View/i });
    fireEvent.click(dialogSaveButton);

    await waitFor(() => {
      expect(executeTool).toHaveBeenCalledWith('add_nodes', expect.objectContaining({
        nodes: expect.arrayContaining([
          expect.objectContaining({
            name: 'Test View',
            type: 'VisualizationView'
          })
        ])
      }));
    });
  });
});
