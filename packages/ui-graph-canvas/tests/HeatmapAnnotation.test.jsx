import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import GenericAnnotationNode from '../src/components/GenericAnnotationNode';
import { AnnotationContext } from '../src/components/AnnotationContext';
import {
  createAnnotation,
  normalizeHeatmapIntensity,
  HEATMAP_DEFAULT_DIAMETER,
  HEATMAP_DEFAULT_INTENSITY,
} from '../src/utils/annotationModel';
import {
  computeAnnotationAriaLabel,
  flowNodeToOverlay,
  heatmapFillStyle,
  heatmapPeakAlpha,
  overlayToFlowNode,
  HEATMAP_PEAK_ALPHA,
} from '../src/utils/annotations';

const hoisted = vi.hoisted(() => ({ resizerProps: [], setNodes: vi.fn() }));

vi.mock('reactflow', () => ({
  NodeResizer: (props) => {
    hoisted.resizerProps.push(props);
    return <div data-testid="resizer" />;
  },
  useReactFlow: () => ({ setNodes: hoisted.setNodes, getNodes: () => [] }),
}));

function renderHeatmap(data, { selected = false, notifyChange = vi.fn() } = {}) {
  const utils = render(
    <AnnotationContext.Provider value={{ notifyChange, labels: {} }}>
      <GenericAnnotationNode id="h1" type="heatmap" data={data} selected={selected} />
    </AnnotationContext.Provider>
  );
  return { ...utils, notifyChange, circle: utils.container.querySelector('.kind-heatmap') };
}

describe('heat-map intensity model', () => {
  it('keeps whole levels 0-10 as they are', () => {
    for (let level = 0; level <= 10; level += 1) {
      expect(normalizeHeatmapIntensity(level)).toBe(level);
    }
  });

  it('clamps, rounds and defaults anything else to a whole level', () => {
    expect(normalizeHeatmapIntensity(-3)).toBe(0);
    expect(normalizeHeatmapIntensity(42)).toBe(10);
    expect(normalizeHeatmapIntensity(6.6)).toBe(7);
    expect(normalizeHeatmapIntensity('4')).toBe(4);
    expect(normalizeHeatmapIntensity(undefined)).toBe(HEATMAP_DEFAULT_INTENSITY);
    expect(normalizeHeatmapIntensity(null)).toBe(HEATMAP_DEFAULT_INTENSITY);
    expect(normalizeHeatmapIntensity('hot')).toBe(HEATMAP_DEFAULT_INTENSITY);
    expect(normalizeHeatmapIntensity(Number.NaN)).toBe(HEATMAP_DEFAULT_INTENSITY);
  });

  it('keeps an explicit 0 rather than treating it as unset', () => {
    expect(createAnnotation({ type: 'heatmap', intensity: 0 }).intensity).toBe(0);
  });

  it('gives a heat-map with no stored size a circle of the default diameter', () => {
    const annotation = createAnnotation({ type: 'heatmap', position: { x: 1, y: 2 } });
    expect(annotation.geometry.w).toBe(HEATMAP_DEFAULT_DIAMETER);
    expect(annotation.geometry.h).toBe(HEATMAP_DEFAULT_DIAMETER);
    expect(annotation.intensity).toBe(HEATMAP_DEFAULT_INTENSITY);
  });
});

describe('heat-map rendering semantics', () => {
  it('is fully transparent at level 0 and strongest at level 10', () => {
    expect(heatmapPeakAlpha(0)).toBe(0);
    expect(heatmapPeakAlpha(10)).toBe(HEATMAP_PEAK_ALPHA);
    expect(heatmapFillStyle(0).backgroundImage).not.toMatch(/rgba\([^)]*, 0\.\d+\)|, 1\)/);
  });

  it('never paints an opaque disk, even at level 10', () => {
    expect(HEATMAP_PEAK_ALPHA).toBeLessThan(1);
    // The rim is always fully transparent, which is what makes neighbouring
    // circles merge into one soft field instead of meeting at a hard edge.
    expect(heatmapFillStyle(10).backgroundImage).toMatch(/rgba\(220, 38, 38, 0\) 100%\)$/);
  });

  it('gets strictly denser with every level', () => {
    for (let level = 1; level <= 10; level += 1) {
      expect(heatmapPeakAlpha(level)).toBeGreaterThan(heatmapPeakAlpha(level - 1));
    }
  });

  it('draws a circle even in a box that is not square', () => {
    expect(heatmapFillStyle(5).backgroundImage).toMatch(/^radial-gradient\(circle closest-side,/);
  });

  it('uses one colour for every level, so overlapping circles blend the same in any z order', () => {
    const colours = new Set();
    for (let level = 1; level <= 10; level += 1) {
      for (const match of heatmapFillStyle(level).backgroundImage.matchAll(
        /rgba\((\d+, \d+, \d+),/g
      )) {
        colours.add(match[1]);
      }
    }
    expect([...colours]).toEqual(['220, 38, 38']);
  });
});

describe('heat-map accessible name', () => {
  it('names the kind and its level', () => {
    expect(computeAnnotationAriaLabel('heatmap', { intensity: 7 })).toBe(
      'Heat-map circle, intensity 7 of 10'
    );
    expect(computeAnnotationAriaLabel('heatmap', { intensity: 0 })).toBe(
      'Heat-map circle, intensity 0 of 10'
    );
  });

  it.each([
    [7.6, 8],
    [15, 10],
    [-2, 0],
    ['x', 5],
    [undefined, 5],
  ])('names a stored %s as the level it draws, %i', (stored, level) => {
    expect(computeAnnotationAriaLabel('heatmap', { intensity: stored })).toBe(
      `Heat-map circle, intensity ${level} of 10`
    );
  });

  it('fills a host-translated template', () => {
    expect(
      computeAnnotationAriaLabel(
        'heatmap',
        { intensity: 3 },
        { ariaKindHeatmap: 'Värmekartcirkel, intensitet {level} av {max}' }
      )
    ).toBe('Värmekartcirkel, intensitet 3 av 10');
  });
});

describe('heat-map overlay round trip', () => {
  it('carries intensity and size through overlay -> flow node -> overlay', () => {
    const overlay = {
      id: 'h1',
      kind: 'heatmap',
      position: { x: 10, y: 20 },
      intensity: 0,
      size: { w: 220, h: 220 },
      z: 0,
      locked: false,
      rotation: 0,
    };
    const node = overlayToFlowNode(overlay);
    expect(node.type).toBe('heatmap');
    expect(node.style).toEqual({ width: 220, height: 220 });
    expect(node.data.intensity).toBe(0);
    const back = flowNodeToOverlay(node);
    expect(back.intensity).toBe(0);
    expect(back.size).toEqual({ w: 220, h: 220 });
  });

  it('leaves the other sized kinds on their own 160x96 default', () => {
    expect(overlayToFlowNode({ id: 's1', kind: 'shape', position: { x: 0, y: 0 } }).style).toEqual({
      width: 160,
      height: 96,
    });
    for (const type of ['shape', 'image', 'note']) {
      const { geometry } = createAnnotation({ type, position: { x: 0, y: 0 } });
      expect([geometry.w, geometry.h]).toEqual([160, 96]);
    }
  });

  it('gives an unsized heat-map overlay a square default box', () => {
    const node = overlayToFlowNode({ id: 'h2', kind: 'heatmap', position: { x: 0, y: 0 } });
    expect(node.style).toEqual({
      width: HEATMAP_DEFAULT_DIAMETER,
      height: HEATMAP_DEFAULT_DIAMETER,
    });
  });
});

describe('GenericAnnotationNode — heatmap', () => {
  beforeEach(() => {
    hoisted.resizerProps.length = 0;
    hoisted.setNodes.mockClear();
  });

  it('paints the level as a radial gradient', () => {
    const { circle } = renderHeatmap({ intensity: 8 });
    expect(circle).toBeTruthy();
    expect(circle.querySelector('.graph-heatmap-circle').style.backgroundImage).toContain(
      'radial-gradient'
    );
    expect(circle.getAttribute('data-intensity')).toBe('8');
    expect(circle.classList.contains('is-empty')).toBe(false);
  });

  it('marks a level-0 circle so it stays findable although it paints nothing', () => {
    const { circle } = renderHeatmap({ intensity: 0 });
    expect(circle.classList.contains('is-empty')).toBe(true);
    expect(heatmapPeakAlpha(0)).toBe(0);
  });

  it('draws the circle as its own element inside the box', () => {
    const { circle } = renderHeatmap({ intensity: 3 });
    expect(circle.style.backgroundImage).toBe('');
    expect(circle.querySelectorAll('.graph-heatmap-circle')).toHaveLength(1);
  });

  it('shows the level as a number only while selected', () => {
    renderHeatmap({ intensity: 6 });
    expect(screen.queryByText('6')).toBeNull();
    renderHeatmap({ intensity: 6 }, { selected: true });
    expect(screen.getByText('6')).toBeInTheDocument();
  });

  it('is resizable with a locked, square aspect', () => {
    renderHeatmap({ intensity: 5 }, { selected: true });
    const props = hoisted.resizerProps.at(-1);
    expect(props.keepAspectRatio).toBe(true);
    expect(props.isVisible).toBe(true);
  });

  it('hides the resize handles while locked', () => {
    renderHeatmap({ intensity: 5, locked: true }, { selected: true });
    expect(hoisted.resizerProps.at(-1).isVisible).toBe(false);
  });

  it('offers one button per level 0-10, marking the current one', () => {
    const { circle } = renderHeatmap({ intensity: 5 });
    fireEvent.contextMenu(circle);
    fireEvent.click(screen.getByRole('button', { name: 'Intensity' }));
    const levels = screen.getAllByRole('button', { name: /^Intensity \d+$/ });
    expect(levels.map((b) => b.textContent)).toEqual([
      '0',
      '1',
      '2',
      '3',
      '4',
      '5',
      '6',
      '7',
      '8',
      '9',
      '10',
    ]);
    expect(screen.getByRole('button', { name: 'Intensity 5' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(screen.getByRole('button', { name: 'Intensity 4' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
  });

  it('sets the intensity with one change per choice, reported as a style change', () => {
    const { circle, notifyChange } = renderHeatmap({ intensity: 5 });
    fireEvent.contextMenu(circle);
    fireEvent.click(screen.getByRole('button', { name: 'Intensity' }));
    fireEvent.click(screen.getByRole('button', { name: 'Intensity 0' }));
    expect(hoisted.setNodes).toHaveBeenCalledTimes(1);
    const updater = hoisted.setNodes.mock.calls.at(-1)[0];
    const [updated] = updater([{ id: 'h1', type: 'heatmap', data: { intensity: 5 } }]);
    expect(updated.data.intensity).toBe(0);
    expect(notifyChange).toHaveBeenCalledTimes(1);
    expect(notifyChange).toHaveBeenCalledWith('style');
  });

  it('publishes nothing when the current level is chosen again', () => {
    const { circle, notifyChange } = renderHeatmap({ intensity: 5 });
    fireEvent.contextMenu(circle);
    fireEvent.click(screen.getByRole('button', { name: 'Intensity' }));
    fireEvent.click(screen.getByRole('button', { name: 'Intensity 5' }));
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('refuses an intensity change when another client takes the lease while the menu is open', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    const notifyChange = vi.fn();
    const ui = (data) => (
      <AnnotationContext.Provider value={{ notifyChange, notifyRemoteLockedAttempt, labels: {} }}>
        <GenericAnnotationNode id="h1" type="heatmap" data={data} />
      </AnnotationContext.Provider>
    );
    const { container, rerender } = render(ui({ intensity: 5 }));
    fireEvent.contextMenu(container.querySelector('.kind-heatmap'));
    fireEvent.click(screen.getByRole('button', { name: 'Intensity' }));
    rerender(ui({ intensity: 5, remoteLease: { clientId: 'other', displayName: 'Other' } }));
    fireEvent.click(screen.getByRole('button', { name: 'Intensity 9' }));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
    expect(notifyChange).not.toHaveBeenCalled();
  });

  it('offers no rotation or opacity control — intensity is its only transparency', () => {
    const { circle } = renderHeatmap({ intensity: 5 });
    fireEvent.contextMenu(circle);
    expect(screen.queryByRole('button', { name: 'Rotation' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Opacity' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Size' })).toBeInTheDocument();
  });

  it('refuses an intensity change while another client holds the edit lease', () => {
    const notifyRemoteLockedAttempt = vi.fn();
    const { container } = render(
      <AnnotationContext.Provider
        value={{ notifyChange: vi.fn(), notifyRemoteLockedAttempt, labels: {} }}
      >
        <GenericAnnotationNode
          id="h1"
          type="heatmap"
          data={{ intensity: 5, remoteLease: { clientId: 'other', displayName: 'Other' } }}
        />
      </AnnotationContext.Provider>
    );
    fireEvent.contextMenu(container.querySelector('.kind-heatmap'));
    expect(notifyRemoteLockedAttempt).toHaveBeenCalled();
    expect(hoisted.setNodes).not.toHaveBeenCalled();
  });
});
