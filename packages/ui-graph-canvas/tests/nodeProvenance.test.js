import { describe, it, expect } from 'vitest';
import { originGraphId, isFederatedNode, originGraphName } from '../src/utils/nodeProvenance';

describe('originGraphId', () => {
  it('reads the origin graph off a raw API node', () => {
    expect(originGraphId({ metadata: { origin_graph_id: 'esam-main' } })).toBe('esam-main');
  });

  it('reads the origin graph off a canvas node wrapper', () => {
    expect(originGraphId({ data: { metadata: { origin_graph_id: 'esam-main' } } })).toBe(
      'esam-main'
    );
  });

  it('prefers the direct metadata when both shapes are present', () => {
    expect(
      originGraphId({
        metadata: { origin_graph_id: 'direct' },
        data: { metadata: { origin_graph_id: 'wrapped' } },
      })
    ).toBe('direct');
  });

  it('returns empty for a local node', () => {
    expect(originGraphId({ metadata: {} })).toBe('');
    expect(originGraphId({ metadata: { origin_graph_id: '' } })).toBe('');
  });

  it('treats a whitespace-only origin graph as local', () => {
    expect(originGraphId({ metadata: { origin_graph_id: '   ' } })).toBe('');
  });

  it('survives a node with no metadata at all', () => {
    expect(originGraphId({})).toBe('');
    expect(originGraphId(null)).toBe('');
    expect(originGraphId(undefined)).toBe('');
  });

  it('coerces a non-string origin graph the way the backend does', () => {
    // access.node_graph_id / teleport._normalize do `str(value or "").strip()`,
    // so the two readers must agree on a value that is not a string: truthy
    // coerces, falsy is local.
    expect(originGraphId({ metadata: { origin_graph_id: 7 } })).toBe('7');
    expect(originGraphId({ metadata: { origin_graph_id: 0 } })).toBe('');
    expect(originGraphId({ metadata: { origin_graph_id: false } })).toBe('');
    expect(originGraphId({ metadata: { origin_graph_id: null } })).toBe('');
  });
});

describe('isFederatedNode', () => {
  it('is true only when another graph owns the node', () => {
    expect(isFederatedNode({ metadata: { origin_graph_id: 'esam-main' } })).toBe(true);
    expect(isFederatedNode({ data: { metadata: { origin_graph_id: 'esam-main' } } })).toBe(true);
  });

  it('is false for a local node', () => {
    expect(isFederatedNode({ metadata: {} })).toBe(false);
    expect(isFederatedNode(null)).toBe(false);
  });

  it('keys off the origin graph rather than the is_federated marker', () => {
    // origin_graph_id is the field the backend narrows graph visibility on, so
    // it is the one that decides here; a stale marker must not override it.
    expect(isFederatedNode({ metadata: { is_federated: true } })).toBe(false);
    expect(
      isFederatedNode({ metadata: { origin_graph_id: 'esam-main', is_federated: false } })
    ).toBe(true);
  });

  it('does not treat a federated-looking id as provenance on its own', () => {
    expect(isFederatedNode({ id: 'federated::esam-main::1', metadata: {} })).toBe(false);
  });
});

describe('originGraphName', () => {
  it('prefers the name stamped on the node', () => {
    expect(
      originGraphName({ metadata: { origin_graph_id: 'esam-main', origin_graph_name: 'eSam' } })
    ).toBe('eSam');
  });

  it('falls back to the display-name map', () => {
    expect(
      originGraphName(
        { metadata: { origin_graph_id: 'esam-main' } },
        { graphDisplayNames: { 'esam-main': 'eSam' } }
      )
    ).toBe('eSam');
  });

  it('falls back to the raw graph id when nothing names it', () => {
    expect(originGraphName({ metadata: { origin_graph_id: 'esam-main' } })).toBe('esam-main');
  });

  it('uses the local display name for a local node', () => {
    expect(originGraphName({ metadata: {} }, { graphDisplayNames: { local: 'Local Graph' } })).toBe(
      'Local Graph'
    );
  });

  it('uses the caller-supplied local label when the map has none', () => {
    expect(originGraphName({ metadata: {} }, { localLabel: 'This graph' })).toBe('This graph');
  });

  it('returns empty for a local node with no label available', () => {
    expect(originGraphName({ metadata: {} })).toBe('');
  });
});
