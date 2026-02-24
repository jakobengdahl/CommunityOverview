import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import useGraphStore from '../src/store/graphStore';

describe('graphStore', () => {
  beforeEach(() => {
    // Reset store to initial state
    useGraphStore.setState({
      nodes: [],
      edges: [],
      schema: null,
      presentation: null,
      highlightedNodeIds: [],
      hiddenNodeIds: [],
      selectedNodeId: null,
      editingNode: null,
      contextMenu: null,
      clearGroupsFlag: false,
      searchQuery: '',
      searchResults: null,
      federationDepth: 1,
      stats: null,
      isLoading: false,
      configLoaded: false,
      error: null,
    });
  });

  describe('Schema and Presentation', () => {
    it('initializes with null schema and presentation', () => {
      const { schema, presentation, configLoaded } = useGraphStore.getState();

      expect(schema).toBeNull();
      expect(presentation).toBeNull();
      expect(configLoaded).toBe(false);
    });

    it('sets schema correctly', () => {
      const testSchema = {
        node_types: {
          Actor: { fields: ['name'], color: '#3B82F6', static: false },
          Initiative: { fields: ['name'], color: '#10B981', static: false },
        },
        relationship_types: {
          BELONGS_TO: { description: 'Belongs to' },
        },
      };

      useGraphStore.getState().setSchema(testSchema);

      const { schema } = useGraphStore.getState();
      expect(schema).toEqual(testSchema);
    });

    it('sets presentation and updates welcome message', () => {
      const testPresentation = {
        title: 'Test Graph',
        introduction: 'Welcome to the test graph!',
        colors: { Actor: '#FF0000' },
        prompt_prefix: 'Test prefix',
        prompt_suffix: 'Test suffix',
        default_language: 'en',
      };

      useGraphStore.getState().setPresentation(testPresentation);

      const { presentation, configLoaded, chatMessages } = useGraphStore.getState();

      expect(presentation).toEqual(testPresentation);
      expect(configLoaded).toBe(true);
      // Welcome message should contain introduction
      expect(chatMessages[0].content).toContain('Welcome to the test graph!');
    });

    it('sets both config at once with setConfig', () => {
      const testSchema = {
        node_types: { Actor: { fields: ['name'], color: '#3B82F6', static: false } },
        relationship_types: {},
      };
      const testPresentation = {
        title: 'Test',
        introduction: 'Test intro',
        colors: {},
        prompt_prefix: '',
        prompt_suffix: '',
        default_language: 'sv',
      };

      useGraphStore.getState().setConfig(testSchema, testPresentation);

      const { schema, presentation, configLoaded } = useGraphStore.getState();

      expect(schema).toEqual(testSchema);
      expect(presentation).toEqual(testPresentation);
      expect(configLoaded).toBe(true);
    });
  });

  describe('getNodeColor', () => {
    it('returns color from presentation if available', () => {
      useGraphStore.setState({
        presentation: {
          colors: { Actor: '#FF0000' },
        },
        schema: {
          node_types: {
            Actor: { color: '#3B82F6' },
          },
        },
      });

      const color = useGraphStore.getState().getNodeColor('Actor');
      expect(color).toBe('#FF0000');
    });

    it('falls back to schema color if not in presentation', () => {
      useGraphStore.setState({
        presentation: {
          colors: {},
        },
        schema: {
          node_types: {
            Actor: { color: '#3B82F6' },
          },
        },
      });

      const color = useGraphStore.getState().getNodeColor('Actor');
      expect(color).toBe('#3B82F6');
    });

    it('returns default color for unknown types', () => {
      useGraphStore.setState({
        presentation: { colors: {} },
        schema: { node_types: {} },
      });

      const color = useGraphStore.getState().getNodeColor('UnknownType');
      expect(color).toBe('#9CA3AF');
    });
  });

  describe('getNodeTypes', () => {
    it('returns empty array when schema not loaded', () => {
      const nodeTypes = useGraphStore.getState().getNodeTypes();
      expect(nodeTypes).toEqual([]);
    });

    it('returns array of node types from schema', () => {
      useGraphStore.setState({
        schema: {
          node_types: {
            Actor: { fields: ['name'], description: 'Actors', color: '#3B82F6', static: false },
            Initiative: { fields: ['name', 'summary'], description: 'Initiatives', color: '#10B981', static: false },
          },
        },
      });

      const nodeTypes = useGraphStore.getState().getNodeTypes();

      expect(nodeTypes).toHaveLength(2);
      expect(nodeTypes[0]).toMatchObject({
        type: 'Actor',
        description: 'Actors',
        color: '#3B82F6',
      });
    });
  });

  describe('getRelationshipTypes', () => {
    it('returns empty array when schema not loaded', () => {
      const relTypes = useGraphStore.getState().getRelationshipTypes();
      expect(relTypes).toEqual([]);
    });

    it('returns array of relationship types from schema', () => {
      useGraphStore.setState({
        schema: {
          relationship_types: {
            BELONGS_TO: { description: 'Belongs to' },
            IMPLEMENTS: { description: 'Implements' },
          },
        },
      });

      const relTypes = useGraphStore.getState().getRelationshipTypes();

      expect(relTypes).toHaveLength(2);
      expect(relTypes.map(r => r.type)).toContain('BELONGS_TO');
      expect(relTypes.map(r => r.type)).toContain('IMPLEMENTS');
    });
  });

  describe('getNodeTypeConfig', () => {
    it('returns null when schema not loaded', () => {
      const config = useGraphStore.getState().getNodeTypeConfig('Actor');
      expect(config).toBeNull();
    });

    it('returns null for unknown type', () => {
      useGraphStore.setState({
        schema: {
          node_types: { Actor: { fields: ['name'] } },
        },
      });

      const config = useGraphStore.getState().getNodeTypeConfig('Unknown');
      expect(config).toBeNull();
    });

    it('returns config for known type', () => {
      useGraphStore.setState({
        schema: {
          node_types: {
            Actor: {
              fields: ['name', 'description'],
              description: 'Actors',
              color: '#3B82F6',
              static: false,
            },
          },
        },
      });

      const config = useGraphStore.getState().getNodeTypeConfig('Actor');
      expect(config).toMatchObject({
        fields: ['name', 'description'],
        description: 'Actors',
        color: '#3B82F6',
      });
    });
  });

  describe('clearChatMessages', () => {
    it('resets to welcome message with presentation intro', () => {
      useGraphStore.setState({
        presentation: {
          introduction: 'Custom welcome!',
        },
        chatMessages: [
          { id: 1, role: 'user', content: 'Hello' },
          { id: 2, role: 'assistant', content: 'Hi' },
        ],
      });

      useGraphStore.getState().clearChatMessages();

      const { chatMessages } = useGraphStore.getState();
      expect(chatMessages).toHaveLength(1);
      expect(chatMessages[0].id).toBe('welcome');
      expect(chatMessages[0].content).toContain('Custom welcome!');
    });
  });

  describe('Federation depth persistence', () => {
    it('persists federation depth to localStorage when updated', () => {
      const setItemSpy = vi.spyOn(window.localStorage.__proto__, 'setItem');

      useGraphStore.getState().setFederationDepth(3);

      expect(useGraphStore.getState().federationDepth).toBe(3);
      expect(setItemSpy).toHaveBeenCalledWith('federation_depth', '3');

      setItemSpy.mockRestore();
    });

    it('ignores invalid federation depth values', () => {
      useGraphStore.setState({ federationDepth: 2 });

      useGraphStore.getState().setFederationDepth(0);
      expect(useGraphStore.getState().federationDepth).toBe(2);

      useGraphStore.getState().setFederationDepth('abc');
      expect(useGraphStore.getState().federationDepth).toBe(2);
    });
  });

});
