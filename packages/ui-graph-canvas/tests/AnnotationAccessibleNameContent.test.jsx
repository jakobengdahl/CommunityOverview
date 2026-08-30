import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/react';
import { computeAccessibleName } from 'dom-accessibility-api';
import NoteNode from '../src/components/NoteNode';
import LabelNode from '../src/components/LabelNode';
import ArrowNode from '../src/components/ArrowNode';
import GroupNode from '../src/components/GroupNode';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';
import FreehandAnnotationNode from '../src/components/FreehandAnnotationNode';
import { resolveAnnotationIcon } from '../src/utils/annotationIcons';

// task-annotation-accessibility-controls-audit: what accname content each
// annotation kind actually renders, isolated from the jsdom-only artifact
// AnnotationAccessibleNameContent.test.jsx's sibling file
// (AnnotationAccessibilityAudit.test.jsx) hit when driving the real
// `reactflow` package — jsdom does no layout, so ReactFlow can never mark a
// node `initialized`/visible there, and an accname computation correctly
// treats hidden content as unnamed regardless of what it contains. This file
// mocks `reactflow` (this suite's usual pattern — see GenericAnnotationNode.
// test.jsx) so each kind's OWN rendered markup, wrapped in a plain
// `role="button"` container standing in for ReactFlow's real (but here
// artificially hidden) node wrapper, can be measured directly: exactly what
// text/title content the DOM offers an accessible-name computation once the
// visibility problem above is taken out of the picture.
//
// The decision's own bar (dec-annotation-v1-accessibility-and-touch) is "role
// + accessible name per annotation, e.g. 'sticky note, Budget Q3'" — a name
// that SAYS WHAT THE THING IS. What these tests find is only ever an
// incidental fallback (an annotation's own visible text, or nothing at all)
// — never a designed name, because no annotation kind's node object ever
// sets ReactFlow's `ariaLabel` field (grepped across
// packages/ui-graph-canvas/src: zero matches outside this test suite and
// ToolSlotPicker/ContextMenus/AnnotationToolbox's own unrelated `ariaLabel`
// prop). `computeAccessibleName` is the same accname implementation
// @testing-library's `getByRole(..., {name})` uses — real, spec-following
// computation — but still one library's implementation running without a
// live screen reader; treat this as strong evidence of DOM content, not a
// substitute for a real NVDA/VoiceOver/TalkBack pass
// (task-annotation-manual-accessibility-touch-acceptance).

const hoisted = vi.hoisted(() => ({ setNodes: vi.fn(), nodes: [] }));

vi.mock('reactflow', () => ({
  NodeResizer: () => null,
  useReactFlow: () => ({
    setNodes: hoisted.setNodes,
    getNodes: () => hoisted.nodes,
    screenToFlowPosition: ({ x, y }) => ({ x, y }),
  }),
}));

beforeEach(() => {
  hoisted.setNodes.mockClear();
  hoisted.nodes = [];
});

// Renders `element`, then measures the accessible name of a synthetic
// `role="button"` wrapper around exactly what it rendered — standing in for
// ReactFlow's real node wrapper (which also carries `role="button"` and no
// `aria-label`, per GraphCanvas.jsx never setting `node.ariaLabel` — see
// AnnotationAccessibilityAudit.test.jsx's role/tabIndex/aria-label assertions
// against the real, unmocked package for that half of the same claim).
function accessibleNameOf(element) {
  const { container } = render(element);
  container.setAttribute('role', 'button');
  container.setAttribute('tabindex', '0');
  return computeAccessibleName(container);
}

describe('per-kind accessible-name content (what a role="button" wrapper around this markup would compute)', () => {
  it('note: falls back to its own text content - readable by accident, not a designed "sticky note, X" name', () => {
    expect(accessibleNameOf(<NoteNode id="note-1" data={{ text: 'Budget Q3' }} />)).toBe(
      'Budget Q3'
    );
  });

  it('note with no text yet: falls back to the placeholder word "Note" only - never distinguishes one empty note from another', () => {
    expect(accessibleNameOf(<NoteNode id="note-1" data={{}} />)).toBe('Note');
  });

  it('label: same incidental text-content fallback as note', () => {
    expect(accessibleNameOf(<LabelNode id="label-1" data={{ text: 'Q3 launch' }} />)).toBe(
      'Q3 launch'
    );
  });

  it('text kind (GenericAnnotationNode): same incidental fallback; empty when there is no text yet', () => {
    expect(
      accessibleNameOf(<GenericAnnotationNode id="text-1" type="text" data={{ text: 'Ship it' }} />)
    ).toBe('Ship it');
    expect(accessibleNameOf(<GenericAnnotationNode id="text-2" type="text" data={{}} />)).toBe('');
  });

  it('shape kind with a caption: the caption text is the only fallback; a captionless shape has no name at all', () => {
    expect(
      accessibleNameOf(
        <GenericAnnotationNode
          id="shape-1"
          type="shape"
          data={{ shape: 'rectangle', text: 'Phase 1' }}
        />
      )
    ).toBe('Phase 1');
    expect(
      accessibleNameOf(
        <GenericAnnotationNode id="shape-2" type="shape" data={{ shape: 'circle' }} />
      )
    ).toBe('');
  });

  it('icon: falls back to the rendered glyph CHARACTER, not the icon\'s name - "★", not "star"; the `title={data.icon}` attribute (GenericAnnotationNode.jsx) is never reached because the glyph text content wins first', () => {
    const glyph = resolveAnnotationIcon('star').text;
    expect(
      accessibleNameOf(<GenericAnnotationNode id="icon-1" type="icon" data={{ icon: 'star' }} />)
    ).toBe(glyph);
  });

  it('vote_dot: no text, no title anywhere in its markup - accessible name is EMPTY', () => {
    expect(accessibleNameOf(<GenericAnnotationNode id="vd-1" type="vote_dot" data={{}} />)).toBe(
      ''
    );
  });

  it('image: falls back to `alt` when set, empty otherwise - never says "image annotation"', () => {
    expect(
      accessibleNameOf(
        <GenericAnnotationNode
          id="img-1"
          type="image"
          data={{ image: { url: 'https://example.test/x.png' }, alt: 'Q3 chart' }}
        />
      )
    ).toBe('Q3 chart');
    expect(accessibleNameOf(<GenericAnnotationNode id="img-2" type="image" data={{}} />)).toBe('');
  });

  it('arrow: no text and no title anywhere in its SVG markup - accessible name is EMPTY, whatever the arrow connects', () => {
    expect(accessibleNameOf(<ArrowNode id="arrow-1" data={{}} />)).toBe('');
  });

  it('freehand: no text and no title anywhere in its SVG markup - accessible name is EMPTY, however long or colourful the stroke', () => {
    expect(
      accessibleNameOf(
        <FreehandAnnotationNode
          id="fh-1"
          data={{
            points: [
              { x: 0, y: 0 },
              { x: 10, y: 10 },
            ],
          }}
        />
      )
    ).toBe('');
  });

  it('group: falls back to its header text (folder glyph + label) when named, empty when not - never says "group"', () => {
    expect(accessibleNameOf(<GroupNode id="group-1" data={{ label: 'Pilot phase' }} />)).toBe(
      '📁 Pilot phase'
    );
    expect(accessibleNameOf(<GroupNode id="group-2" data={{}} />)).toBe('📁 Group');
  });
});
