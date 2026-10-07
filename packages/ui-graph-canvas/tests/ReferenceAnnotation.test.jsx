import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';
import { AnnotationContext } from '../src/components/AnnotationContext';
import {
  createAnnotation,
  isSafeReferenceUrl,
  normalizeReferenceTargetKind,
  referenceTargetProblem,
  trimReferenceTarget,
  REFERENCE_SAFE_URL_SCHEMES,
  REFERENCE_TARGET_KINDS,
} from '../src/utils/annotationModel';
import { computeAnnotationAriaLabel, overlayToFlowNode } from '../src/utils/annotations';
import urlGate from '../../../docs/fixtures/reference_url_gate.json';

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

// Round 4's mutation pass narrowed this side's whitespace class to only the
// characters some fixture `refuse` case happens to use — 10 of 26 — and BOTH JS
// suites stayed green. On this side that is the DANGEROUS direction: with
// U+202F out of the class, `https://trusted.example<U+202F>@evil.example/`
// passes the gate, and the WHATWG parser folds everything before the `@` into
// USERINFO — so `hostname` is evil.example while the tile's visible target text
// reads trusted.example. A tile that lies about where it goes.
//
// One case per enumerated character, driven off the fixture's own `whitespace`
// array, so a character dropped here fails its own case rather than hiding
// behind a refuse list that never mentioned it.
describe('reference URL gate — the whitespace enumeration itself', () => {
  it.each(urlGate.whitespace)('refuses U+%s inside a path', (code) => {
    const char = String.fromCodePoint(parseInt(code, 16));
    expect(isSafeReferenceUrl(`https://example.org/a${char}b`)).toBe(false);
  });

  it.each(urlGate.whitespace)('handles U+%s at the ends', (code) => {
    const char = String.fromCodePoint(parseInt(code, 16));
    const padded = `${char}https://example.org/x${char}`;
    if (char.codePointAt(0) < 0x20) {
      // C0 controls are refused outright rather than stripped — the same
      // deliberate rule the backend applies, so that the string validated is
      // the string stored.
      expect(isSafeReferenceUrl(padded)).toBe(false);
    } else {
      expect(trimReferenceTarget(padded)).toBe('https://example.org/x');
      expect(isSafeReferenceUrl(padded)).toBe(true);
    }
  });

  it('refuses a target that hides its real host behind userinfo', () => {
    // The concrete harm behind this whole describe block: the visible text and
    // the actual destination must not be able to disagree.
    const spoof = 'https://trusted.example\u202f@evil.example/';
    expect(isSafeReferenceUrl(spoof)).toBe(false);
    expect(referenceTargetProblem({ target_kind: 'url', target: spoof })).toBe('unsafe');
  });
});

// The other half of the shared cross-language fixture that
// backend/core/tests/test_session_annotations_reference.py drives. The backend
// gate and this one are separate implementations by design — the backend
// decides what may be STORED, this decides what may be DRAWN AS CLICKABLE, and
// this side must not trust what it is handed. Separate implementations drift,
// and round 1 of the review loop caught drift in the dangerous direction: the
// backend's lenient `urlsplit` accepted hosts with spaces and out-of-range
// ports that this strict WHATWG parser refuses, so a target could be stored
// and then render permanently broken as "Unsafe link — not opened".
describe('reference URL gate — agreement with the backend', () => {
  it.each(urlGate.accept)('accepts the shared case %j', (target) => {
    expect(isSafeReferenceUrl(target)).toBe(true);
  });

  it.each(urlGate.refuse)('refuses the shared case %j', (target) => {
    expect(isSafeReferenceUrl(target)).toBe(false);
  });
});

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

  // Every aria key, not just two of them. Round 1's mutation review hardcoded
  // the broken-state suffix and misspelled the session and url entries of the
  // key table with the suite still green, because only these two keys were
  // ever overridden with a non-English value (S6).
  // A resolvable target per kind, so the name under test is the live one — a
  // bare 'x' is a legitimately broken url target and would append the broken
  // suffix, which is a different assertion (below).
  it.each([
    ['session', '8244-1742-3391-0057', 'ariaKindReferenceSession', 'SESSION-sentinel'],
    ['url', 'https://example.org/x', 'ariaKindReferenceUrl', 'WEBBSIDA-sentinel'],
    ['resource', 'res-1', 'ariaKindReferenceResource', 'UNDERLAG-sentinel'],
  ])('reads the %s target word from its own prop', (targetKind, target, key, sentinel) => {
    const name = computeAnnotationAriaLabel(
      'reference',
      { target_kind: targetKind, target, label: 'L' },
      { ariaKindReference: 'REFERENS-sentinel', [key]: sentinel }
    );
    expect(name).toBe(`REFERENS-sentinel, ${sentinel}, L`);
  });

  // The fourth broken condition in the contract's own list — the one only the
  // host can answer. ReactFlow reads `node.ariaLabel` and it overrides the
  // tile's text, so a host-reported broken tile was being announced as live
  // while drawn dashed with no open control (round 2 of the review loop).
  it('says a host-reported broken target is broken', () => {
    const live = computeAnnotationAriaLabel(
      'reference',
      { target_kind: 'session', target: '8244-1742-3391-0057', label: 'Gone' },
      { ariaKindReferenceBroken: 'TRASIGT-sentinel' }
    );
    expect(live).not.toContain('TRASIGT-sentinel');

    const broken = computeAnnotationAriaLabel(
      'reference',
      { target_kind: 'session', target: '8244-1742-3391-0057', label: 'Gone' },
      { ariaKindReferenceBroken: 'TRASIGT-sentinel' },
      { hostBroken: true }
    );
    expect(broken).toContain('TRASIGT-sentinel');
  });

  it('does not call a structurally fine reference broken on a silent host', () => {
    // `undefined` from the host is "no opinion", not "gone".
    const name = computeAnnotationAriaLabel(
      'reference',
      { target_kind: 'url', target: 'https://example.org/x', label: 'L' },
      { ariaKindReferenceBroken: 'TRASIGT-sentinel' },
      { hostBroken: undefined }
    );
    expect(name).not.toContain('TRASIGT-sentinel');
  });

  it('reads the broken-state word from its own prop', () => {
    const name = computeAnnotationAriaLabel(
      'reference',
      { target_kind: 'url', target: 'javascript:alert(1)', label: 'L' },
      { ariaKindReference: 'REFERENS-sentinel', ariaKindReferenceBroken: 'TRASIGT-sentinel' }
    );
    expect(name).toContain('TRASIGT-sentinel');
    expect(name).not.toContain('broken target');
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
      { openReference, labels: { referenceOpen: 'ÖPPNA-MÅL-sentinel' } }
    );
    const button = screen.getByRole('button', { name: 'ÖPPNA-MÅL-sentinel' });
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
        { labels: { referenceOpen: 'ÖPPNA-MÅL-sentinel' } }
      );
      expect(screen.queryByRole('button', { name: 'ÖPPNA-MÅL-sentinel' })).toBeNull();
    });

    it('says why, preferring the reason over any preview text', () => {
      renderReference(
        {
          target_kind: 'url',
          target: 'javascript:alert(1)',
          label: 'Looks innocent',
          preview: { site: 'example.org' },
        },
        { labels: { referenceUnsafeTarget: 'OSÄKER-LÄNK-sentinel' } }
      );
      expect(screen.getByText('OSÄKER-LÄNK-sentinel')).toBeInTheDocument();
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

    // Round 4: the previous test pads with an ASCII SPACE, which both
    // `.trim()` and the gate's set strip, so it was green whichever this used.
    // U+0085 is the one character in the gate set `.trim()` does not strip,
    // and GraphCanvas's `referenceHostBroken` trims with the gate set — so a
    // bare `.trim()` here made the tile and the accessible name ask the host
    // about DIFFERENT strings for the same tile, which is round 2's own
    // defect (dashed tile announced as live) re-opened one layer down.
    it('asks the host about the gate-trimmed target, not the .trim() one', () => {
      const isReferenceTargetAvailable = vi.fn(() => true);
      renderReference(
        { target_kind: 'session', target: '\u00858244-1742-3391-0057' },
        { isReferenceTargetAvailable }
      );
      expect(isReferenceTargetAvailable).toHaveBeenCalledWith('session', '8244-1742-3391-0057');
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

  // Deliberately NOT the English defaults. A label override identical to the
  // default makes every assertion pass whether the component reads the prop
  // or hardcodes the literal — round 1's mutation review replaced all nine
  // label reads with their English strings and the suite stayed green (S6).
  // Sentinels are the only thing that tells the two apart.
  const LABELS = {
    referenceTarget: 'MÅL-sentinel',
    referenceTargetSession: 'SESSION-sentinel',
    referenceTargetUrl: 'WEBBSIDA-sentinel',
    referenceTargetResource: 'UNDERLAG-sentinel',
    referenceTargetUnknown: 'OKÄNT-sentinel',
    referenceOpen: 'ÖPPNA-sentinel',
    referenceRename: 'BYT-NAMN-sentinel',
    referenceLabel: 'ETIKETT-sentinel',
    referenceBrokenTarget: 'TRASIGT-sentinel',
    editAnnotation: 'REDIGERA-sentinel',
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
    fireEvent.click(within(menu).getByRole('button', { name: LABELS.referenceTarget }));
    return within(menu);
  }

  it('names the target kind and shows the target, read-only', () => {
    const menu = openMenu({ target_kind: 'session', target: '8244-1742', label: 'Overview' });
    expect(menu.getByText(LABELS.referenceTargetSession)).toBeInTheDocument();
    expect(menu.getByText('8244-1742')).toBeInTheDocument();
    // Read-only: repointing goes through the validated MCP/API path in v1, so
    // there is no free-text target field here that would need its own copy of
    // the scheme rule.
    expect(menu.queryByRole('textbox', { name: /target/i })).toBeNull();
  });

  it('activates the target from the menu', () => {
    const openReference = vi.fn();
    const menu = openMenu({ target_kind: 'url', target: 'https://example.org' }, { openReference });
    fireEvent.click(menu.getByRole('button', { name: LABELS.referenceOpen }));
    expect(openReference).toHaveBeenCalledWith(
      expect.objectContaining({ targetKind: 'url', target: 'https://example.org' })
    );
  });

  it('disables opening a broken target and says it is unavailable', () => {
    const menu = openMenu(
      { target_kind: 'session', target: '0000-0000', label: 'Gone' },
      { isReferenceTargetAvailable: () => false }
    );
    expect(menu.getByRole('button', { name: LABELS.referenceOpen })).toBeDisabled();
    expect(menu.getAllByText(LABELS.referenceBrokenTarget).length).toBeGreaterThan(0);
  });

  it('renames the label through an inline editor that writes data.label', async () => {
    // Double-click opens the target, so renaming is reached from the menu
    // instead — and it must write `label`, not the `text` field every other
    // editable kind uses.
    const menu = openMenu({ target_kind: 'resource', target: 'r-1', label: 'Old' });
    fireEvent.click(menu.getByRole('button', { name: LABELS.referenceRename }));

    const input = await screen.findByRole('textbox', { name: LABELS.referenceLabel });
    fireEvent.change(input, { target: { value: 'New name' } });

    const node = applyLatestUpdate({ id: 'r1', data: { label: 'Old' } });
    expect(node.data.label).toBe('New name');
    expect(node.data.text).toBeUndefined();
  });

  it('commits the rename on Enter', async () => {
    const menu = openMenu({ target_kind: 'resource', target: 'r-1', label: 'Old' });
    fireEvent.click(menu.getByRole('button', { name: LABELS.referenceRename }));
    const input = await screen.findByRole('textbox', { name: LABELS.referenceLabel });
    fireEvent.change(input, { target: { value: '  Trimmed  ' } });
    fireEvent.keyDown(input, { key: 'Enter' });

    const node = applyLatestUpdate({ id: 'r1', data: { label: 'Old' } });
    expect(node.data.label).toBe('Trimmed');
  });

  it('names an unrecognised target kind rather than leaving the row blank', () => {
    const menu = openMenu({ target_kind: 'graph_node', target: 'x', label: 'Mystery' });
    expect(menu.getByText(LABELS.referenceTargetUnknown)).toBeInTheDocument();
  });
});
