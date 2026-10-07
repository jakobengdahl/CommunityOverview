import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';
import { AnnotationContext } from '../src/components/AnnotationContext';
import {
  createAnnotation,
  isSafeReferenceUrl,
  normalizeReferenceTargetKind,
  referenceTargetProblem,
  REFERENCE_SAFE_URL_SCHEMES,
  REFERENCE_TARGET_KINDS,
} from '../src/utils/annotationModel';
import { computeAnnotationAriaLabel, overlayToFlowNode } from '../src/utils/annotations';

const hoisted = vi.hoisted(() => ({ resizerProps: [], setNodes: vi.fn(), nodes: [] }));

vi.mock('reactflow', () => ({
  NodeResizer: (props) => {
    hoisted.resizerProps.push(props);
    return <div data-testid="resizer" />;
  },
  useReactFlow: () => ({ setNodes: hoisted.setNodes, getNodes: () => hoisted.nodes }),
}));

// The same unsafe set the backend gate is pinned against
// (backend/core/tests/test_session_annotations_reference.py), so the two sides
// of the stack cannot drift into disagreeing about what a link may be.
const UNSAFE_TARGETS = [
  'javascript:alert(1)',
  'JavaScript:alert(1)',
  '  javascript:alert(1)',
  'data:text/html,<script>alert(1)</script>',
  'DATA:text/html;base64,PHNjcmlwdD4=',
  'file:///etc/passwd',
  'vbscript:msgbox(1)',
  'VBScript:MsgBox(1)',
  '//evil.example/x',
  '/admin/delete-everything',
  'evil.example/x',
  '',
  '   ',
  'java\tscript:alert(1)',
  'java\nscript:alert(1)',
  'java\rscript:alert(1)',
  'java\u0000script:alert(1)',
  '\u0001javascript:alert(1)',
  'http://',
  'https://',
];

const SAFE_TARGETS = [
  'https://example.org/handbook',
  'http://example.org',
  'https://example.org:8443/a/b?c=d#e',
  'HTTPS://example.org/shouty',
  '  https://example.org/padded  ',
];

// Applies the latest setNodes(updater) call to a single-node array and
// returns the updated node — the same helper GenericAnnotationNode.test.jsx
// uses for its live node store.
function applyLatestUpdate(node) {
  const call = hoisted.setNodes.mock.calls.at(-1);
  return call[0]([node])[0];
}

function renderReference(data, context = {}) {
  return render(
    <AnnotationContext.Provider
      value={{
        notifyChange: vi.fn(),
        notifyRemoteLockedAttempt: vi.fn(),
        beginEditing: async (ids) => ({ granted: ids, denied: {} }),
        endEditing: vi.fn(),
        labels: {},
        ...context,
      }}
    >
      <GenericAnnotationNode type="reference" id="r1" data={data} selected={data.selected} />
    </AnnotationContext.Provider>
  );
}

describe('reference annotation — model', () => {
  it('is one of the v1 annotation types and has exactly three target kinds', () => {
    expect(new Set(REFERENCE_TARGET_KINDS)).toEqual(new Set(['session', 'url', 'resource']));
    expect(new Set(REFERENCE_SAFE_URL_SCHEMES)).toEqual(new Set(['http:', 'https:']));
  });

  it.each(UNSAFE_TARGETS)('refuses %j as a renderable link', (target) => {
    expect(isSafeReferenceUrl(target)).toBe(false);
  });

  it.each(SAFE_TARGETS)('accepts %j as a renderable link', (target) => {
    expect(isSafeReferenceUrl(target)).toBe(true);
  });

  it('refuses a scheme nobody named rather than only the known-bad ones', () => {
    for (const scheme of ['about', 'blob', 'chrome', 'intent', 'jar', 'ws', 'ftp', 'tel']) {
      expect(isSafeReferenceUrl(`${scheme}://example.org/x`)).toBe(false);
      expect(isSafeReferenceUrl(`${scheme}:example.org/x`)).toBe(false);
    }
  });

  it('refuses a non-string target', () => {
    for (const value of [null, undefined, 7, [], {}, true]) {
      expect(isSafeReferenceUrl(value)).toBe(false);
    }
  });

  it('normalizes only the three known target kinds', () => {
    expect(normalizeReferenceTargetKind('session')).toBe('session');
    expect(normalizeReferenceTargetKind('url')).toBe('url');
    expect(normalizeReferenceTargetKind('resource')).toBe('resource');
    for (const bad of ['URL', 'graph_node', '', null, undefined, 7]) {
      expect(normalizeReferenceTargetKind(bad)).toBe(null);
    }
  });

  describe('referenceTargetProblem', () => {
    it('reports a missing target kind or target', () => {
      expect(referenceTargetProblem({})).toBe('missing');
      expect(referenceTargetProblem({ target_kind: 'url' })).toBe('missing');
      expect(referenceTargetProblem({ target: 'https://example.org' })).toBe('missing');
      expect(referenceTargetProblem({ target_kind: 'url', target: '   ' })).toBe('missing');
      expect(referenceTargetProblem({ target_kind: 'nope', target: 'x' })).toBe('missing');
    });

    it('reports an unsafe url target as unsafe, not merely missing', () => {
      expect(referenceTargetProblem({ target_kind: 'url', target: 'javascript:alert(1)' })).toBe(
        'unsafe'
      );
    });

    it('reports no problem for a well-formed reference of each kind', () => {
      expect(referenceTargetProblem({ target_kind: 'url', target: 'https://example.org' })).toBe(
        null
      );
      expect(referenceTargetProblem({ target_kind: 'session', target: '8244-1742' })).toBe(null);
      expect(referenceTargetProblem({ target_kind: 'resource', target: 'r-1' })).toBe(null);
    });

    it('does not hold a session or resource target to the url rule', () => {
      // A session id is not a URL and must not be called unsafe for it.
      expect(referenceTargetProblem({ target_kind: 'session', target: 'not-a-url' })).toBe(null);
    });
  });

  it('carries an unsafe or unrecognised payload through verbatim rather than rewriting it', () => {
    // Normalising either away would make a broken reference look live, and
    // rewriting a target would point a tile somewhere its author never chose.
    const annotation = createAnnotation({
      id: 'r1',
      type: 'reference',
      target_kind: 'bogus',
      target: 'javascript:alert(1)',
      geometry: { x: 0, y: 0, w: 220, h: 72 },
    });
    expect(annotation.target_kind).toBe('bogus');
    expect(annotation.target).toBe('javascript:alert(1)');
    expect(referenceTargetProblem(annotation)).toBe('missing');
  });

  it('gives a reference a default box so it draws something', () => {
    const annotation = createAnnotation({
      id: 'r1',
      type: 'reference',
      target_kind: 'resource',
      target: 'r-1',
    });
    expect(annotation.geometry.w).toBe(220);
    expect(annotation.geometry.h).toBe(72);
  });

  it('keeps an optional preview and drops it when absent', () => {
    const withPreview = createAnnotation({
      id: 'r1',
      type: 'reference',
      target_kind: 'url',
      target: 'https://example.org',
      preview: { title: 'T', site: 'S' },
    });
    expect(withPreview.preview).toEqual({ title: 'T', site: 'S' });
    const without = createAnnotation({
      id: 'r2',
      type: 'reference',
      target_kind: 'url',
      target: 'https://example.org',
    });
    expect(without.preview).toBeUndefined();
  });

  it('projects a reference overlay onto a flow node carrying its whole payload', () => {
    // The translator is data-driven off GENERIC_OVERLAY_FIELDS; a field
    // missing from that list is silently dropped on the browser's
    // hydrate -> autosave round trip.
    const node = overlayToFlowNode({
      id: 'r1',
      kind: 'reference',
      position: { x: 1, y: 2 },
      size: { w: 240, h: 80 },
      target_kind: 'session',
      target: '8244-1742',
      label: 'Overview',
      icon: 'flag',
      preview: { title: 'Overview' },
    });
    expect(node.type).toBe('reference');
    expect(node.data.target_kind).toBe('session');
    expect(node.data.target).toBe('8244-1742');
    expect(node.data.label).toBe('Overview');
    expect(node.data.icon).toBe('flag');
    expect(node.data.preview).toEqual({ title: 'Overview' });
    expect(node.style).toEqual({ width: 240, height: 80 });
  });
});

describe('reference annotation — accessible name', () => {
  it('names the kind of thing it points at', () => {
    expect(
      computeAnnotationAriaLabel('reference', {
        target_kind: 'session',
        target: '8244-1742',
        label: 'Overview',
      })
    ).toBe('Reference, session, Overview');
  });

  it('falls back to the target when there is no label', () => {
    expect(
      computeAnnotationAriaLabel('reference', {
        target_kind: 'url',
        target: 'https://example.org',
      })
    ).toBe('Reference, web page, https://example.org');
  });

  it('says a broken reference is broken, so it is not only a colour', () => {
    const name = computeAnnotationAriaLabel('reference', {
      target_kind: 'url',
      target: 'javascript:alert(1)',
      label: 'Looks innocent',
    });
    expect(name).toContain('broken target');
  });

  it('uses host-supplied words rather than English literals', () => {
    const name = computeAnnotationAriaLabel(
      'reference',
      { target_kind: 'resource', target: 'r-1', label: 'Metodguide' },
      {
        ariaKindReference: 'Referens',
        ariaKindReferenceResource: 'underlag',
      }
    );
    expect(name).toBe('Referens, underlag, Metodguide');
  });
});

describe('reference annotation — rendering and activation', () => {
  beforeEach(() => {
    hoisted.resizerProps.length = 0;
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  it('renders as its own kind, not as a graph node or another annotation kind', () => {
    const { container } = renderReference({
      target_kind: 'session',
      target: '8244-1742',
      label: 'Overview',
    });
    expect(container.querySelector('.kind-reference')).toBeTruthy();
    // Not a graph node (CustomNode's class) and not any other annotation kind.
    expect(container.querySelector('.graph-custom-node')).toBeNull();
    for (const other of ['kind-text', 'kind-shape', 'kind-icon', 'kind-image', 'kind-heatmap']) {
      expect(container.querySelector(`.${other}`)).toBeNull();
    }
  });

  it('badges each target kind distinguishably', () => {
    const badges = new Set();
    for (const kind of REFERENCE_TARGET_KINDS) {
      const { container, unmount } = renderReference({ target_kind: kind, target: 'x' });
      const el = container.querySelector('.kind-reference');
      expect(el.dataset.targetKind).toBe(kind);
      badges.add(container.querySelector('.graph-reference-badge').textContent);
      unmount();
    }
    // Three kinds, three distinct glyphs — a shared glyph would make a tile
    // that says nothing about where it goes.
    expect(badges.size).toBe(REFERENCE_TARGET_KINDS.length);
  });

  it('shows the label, and the target when there is no label', () => {
    const withLabel = renderReference({
      target_kind: 'url',
      target: 'https://example.org/a',
      label: 'Handbook',
    });
    expect(screen.getByText('Handbook')).toBeInTheDocument();
    withLabel.unmount();

    renderReference({ target_kind: 'url', target: 'https://example.org/a' });
    // Once, not twice: when the title falls back to the target, the detail
    // line must not repeat it.
    expect(screen.getAllByText('https://example.org/a')).toHaveLength(1);
  });

  it('shows a preview line when one is stored', () => {
    renderReference({
      target_kind: 'url',
      target: 'https://example.org/a',
      label: 'Handbook',
      preview: { site: 'example.org' },
    });
    expect(screen.getByText('example.org')).toBeInTheDocument();
  });

  it('never renders the target into an href, for any target kind', () => {
    // An anchor would give the canvas a middle-click/context-menu "open"
    // path that bypasses both the broken-target guard and the host.
    for (const kind of REFERENCE_TARGET_KINDS) {
      const { container, unmount } = renderReference({
        target_kind: kind,
        target: kind === 'url' ? 'https://example.org' : 'x',
      });
      expect(container.querySelector('a[href]')).toBeNull();
      unmount();
    }
  });

  it('hands a double-click to the host with the target kind and target', () => {
    const openReference = vi.fn();
    const { container } = renderReference(
      { target_kind: 'session', target: '8244-1742', label: 'Overview' },
      { openReference }
    );
    fireEvent.doubleClick(container.querySelector('.kind-reference'));
    expect(openReference).toHaveBeenCalledWith({
      annotationId: 'r1',
      targetKind: 'session',
      target: '8244-1742',
      label: 'Overview',
    });
  });

  it('trims the target it reports', () => {
    const openReference = vi.fn();
    const { container } = renderReference(
      { target_kind: 'url', target: '  https://example.org  ' },
      { openReference }
    );
    fireEvent.doubleClick(container.querySelector('.kind-reference'));
    expect(openReference.mock.calls[0][0].target).toBe('https://example.org');
  });

  it('offers a focusable control that activates the same target while selected', () => {
    const openReference = vi.fn();
    renderReference(
      { target_kind: 'resource', target: 'r-1', selected: true },
      { openReference, labels: { referenceOpen: 'Open target' } }
    );
    const button = screen.getByRole('button', { name: 'Open target' });
    fireEvent.click(button);
    expect(openReference).toHaveBeenCalledWith(
      expect.objectContaining({ targetKind: 'resource', target: 'r-1' })
    );
  });

  it('is resizable, like the other kinds that own an explicit box', () => {
    renderReference({ target_kind: 'resource', target: 'r-1', selected: true });
    expect(screen.getByTestId('resizer')).toBeInTheDocument();
  });

  describe('broken targets', () => {
    it.each(UNSAFE_TARGETS)('renders %j as broken and refuses to open it', (target) => {
      const openReference = vi.fn();
      const { container } = renderReference(
        { target_kind: 'url', target, selected: true },
        { openReference }
      );
      expect(container.querySelector('.kind-reference.is-broken')).toBeTruthy();
      fireEvent.doubleClick(container.querySelector('.kind-reference'));
      expect(openReference).not.toHaveBeenCalled();
    });

    it('renders a reference with no target at all as broken', () => {
      const { container } = renderReference({});
      expect(container.querySelector('.kind-reference.is-broken')).toBeTruthy();
    });

    it('renders a reference with an unrecognised target kind as broken', () => {
      const { container } = renderReference({ target_kind: 'graph_node', target: 'x' });
      expect(container.querySelector('.kind-reference.is-broken')).toBeTruthy();
    });

    it('hides the open control on a broken reference', () => {
      renderReference(
        { target_kind: 'url', target: 'javascript:alert(1)', selected: true },
        { labels: { referenceOpen: 'Open target' } }
      );
      expect(screen.queryByRole('button', { name: 'Open target' })).toBeNull();
    });

    it('says why, preferring the reason over any preview text', () => {
      renderReference(
        {
          target_kind: 'url',
          target: 'javascript:alert(1)',
          label: 'Looks innocent',
          preview: { site: 'example.org' },
        },
        { labels: { referenceUnsafeTarget: 'Unsafe link — not opened' } }
      );
      expect(screen.getByText('Unsafe link — not opened')).toBeInTheDocument();
      expect(screen.queryByText('example.org')).toBeNull();
    });

    it('renders as broken when only the host reports the target unresolvable', () => {
      const openReference = vi.fn();
      const { container } = renderReference(
        { target_kind: 'session', target: '0000-0000', selected: true },
        { openReference, isReferenceTargetAvailable: () => false }
      );
      expect(container.querySelector('.kind-reference.is-broken')).toBeTruthy();
      fireEvent.doubleClick(container.querySelector('.kind-reference'));
      expect(openReference).not.toHaveBeenCalled();
    });

    it('treats a host that cannot judge the target as no opinion, not as broken', () => {
      const openReference = vi.fn();
      const { container } = renderReference(
        { target_kind: 'session', target: '8244-1742' },
        { openReference, isReferenceTargetAvailable: () => undefined }
      );
      expect(container.querySelector('.kind-reference.is-broken')).toBeNull();
      fireEvent.doubleClick(container.querySelector('.kind-reference'));
      expect(openReference).toHaveBeenCalled();
    });

    it('stays live when the host confirms the target', () => {
      const { container } = renderReference(
        { target_kind: 'session', target: '8244-1742' },
        { isReferenceTargetAvailable: () => true }
      );
      expect(container.querySelector('.kind-reference.is-broken')).toBeNull();
    });

    it('asks the host about the target it actually holds', () => {
      const isReferenceTargetAvailable = vi.fn(() => true);
      renderReference(
        { target_kind: 'session', target: '  8244-1742  ' },
        { isReferenceTargetAvailable }
      );
      expect(isReferenceTargetAvailable).toHaveBeenCalledWith('session', '8244-1742');
    });

    it('does not navigate at all when no host handler is wired', () => {
      // A reference on a host that wired nothing must not fall back to a
      // guess about the host's routing.
      const { container } = renderReference({
        target_kind: 'url',
        target: 'https://example.org',
      });
      expect(() => fireEvent.doubleClick(container.querySelector('.kind-reference'))).not.toThrow();
    });
  });
});

describe('reference annotation — property editor', () => {
  beforeEach(() => {
    hoisted.resizerProps.length = 0;
    hoisted.setNodes.mockClear();
    hoisted.nodes = [];
  });

  const LABELS = {
    referenceTarget: 'Target',
    referenceTargetSession: 'Session',
    referenceTargetUrl: 'Web page',
    referenceTargetResource: 'Supporting material',
    referenceTargetUnknown: 'Unknown target',
    referenceOpen: 'Open target',
    referenceRename: 'Rename',
    referenceLabel: 'Label',
    referenceBrokenTarget: 'Target not available',
    editAnnotation: 'Edit',
  };

  // Returns a scope limited to the open menu. The tile itself carries the
  // same words (its title can be the target, and its selected-state control
  // is also named "Open target"), so an unscoped query is ambiguous — and an
  // ambiguous query here would not tell the menu's control apart from the
  // tile's.
  function openMenu(data, context = {}) {
    const rendered = renderReference({ ...data, selected: true }, { labels: LABELS, ...context });
    fireEvent.contextMenu(rendered.container.querySelector('.kind-reference'));
    const menu = document.querySelector('.graph-annotation-context-menu--bar');
    expect(menu).toBeTruthy();
    fireEvent.click(within(menu).getByRole('button', { name: 'Target' }));
    return within(menu);
  }

  it('names the target kind and shows the target, read-only', () => {
    const menu = openMenu({ target_kind: 'session', target: '8244-1742', label: 'Overview' });
    expect(menu.getByText('Session')).toBeInTheDocument();
    expect(menu.getByText('8244-1742')).toBeInTheDocument();
    // Read-only: repointing goes through the validated MCP/API path in v1, so
    // there is no free-text target field here that would need its own copy of
    // the scheme rule.
    expect(menu.queryByRole('textbox', { name: /target/i })).toBeNull();
  });

  it('activates the target from the menu', () => {
    const openReference = vi.fn();
    const menu = openMenu({ target_kind: 'url', target: 'https://example.org' }, { openReference });
    fireEvent.click(menu.getByRole('button', { name: 'Open target' }));
    expect(openReference).toHaveBeenCalledWith(
      expect.objectContaining({ targetKind: 'url', target: 'https://example.org' })
    );
  });

  it('disables opening a broken target and says it is unavailable', () => {
    const menu = openMenu(
      { target_kind: 'session', target: '0000-0000', label: 'Gone' },
      { isReferenceTargetAvailable: () => false }
    );
    expect(menu.getByRole('button', { name: 'Open target' })).toBeDisabled();
    expect(menu.getAllByText('Target not available').length).toBeGreaterThan(0);
  });

  it('renames the label through an inline editor that writes data.label', async () => {
    // Double-click opens the target, so renaming is reached from the menu
    // instead — and it must write `label`, not the `text` field every other
    // editable kind uses.
    const menu = openMenu({ target_kind: 'resource', target: 'r-1', label: 'Old' });
    fireEvent.click(menu.getByRole('button', { name: 'Rename' }));

    const input = await screen.findByRole('textbox', { name: 'Label' });
    fireEvent.change(input, { target: { value: 'New name' } });

    const node = applyLatestUpdate({ id: 'r1', data: { label: 'Old' } });
    expect(node.data.label).toBe('New name');
    expect(node.data.text).toBeUndefined();
  });

  it('commits the rename on Enter', async () => {
    const menu = openMenu({ target_kind: 'resource', target: 'r-1', label: 'Old' });
    fireEvent.click(menu.getByRole('button', { name: 'Rename' }));
    const input = await screen.findByRole('textbox', { name: 'Label' });
    fireEvent.change(input, { target: { value: '  Trimmed  ' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    const node = applyLatestUpdate({ id: 'r1', data: { label: 'Old' } });
    expect(node.data.label).toBe('Trimmed');
  });

  it('names an unrecognised target kind rather than leaving the row blank', () => {
    const menu = openMenu({ target_kind: 'graph_node', target: 'x', label: 'Mystery' });
    expect(menu.getByText('Unknown target')).toBeInTheDocument();
  });
});
