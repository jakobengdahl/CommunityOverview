// The collapse behaviour the compact property bar is built on
// (task-annotation-compact-property-bar). It had no coverage of its own, and
// the one behaviour it adds beyond relocating markup — focus handling — is
// exactly where a bug shipped green: switching groups pulled focus back to the
// group you had just left.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/react';
import { useState } from 'react';
import { AnnotationMenuGroup } from '../src/components/AnnotationMenuGroup';

// Two sibling groups sharing one open-at-a-time state, which is how
// ContextMenuPortal drives them.
function TwoGroups() {
  const [openGroup, setOpenGroup] = useState(null);
  return (
    <div>
      <AnnotationMenuGroup
        groupKey="fill"
        label="Fill"
        glyph="A"
        swatch="#ef4444"
        open={openGroup === 'fill'}
        onToggle={setOpenGroup}
      >
        <button type="button">fill-option</button>
      </AnnotationMenuGroup>
      <AnnotationMenuGroup
        groupKey="border"
        label="Border"
        glyph="B"
        open={openGroup === 'border'}
        onToggle={setOpenGroup}
      >
        <button type="button">border-option</button>
      </AnnotationMenuGroup>
    </div>
  );
}

describe('AnnotationMenuGroup', () => {
  afterEach(() => cleanup());

  it('keeps its panel closed until the trigger is activated', () => {
    render(<TwoGroups />);
    expect(screen.queryByText('fill-option')).toBeNull();
    expect(screen.getByRole('button', { name: 'Fill' })).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(screen.getByRole('button', { name: 'Fill' }));

    expect(screen.getByText('fill-option')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Fill' })).toHaveAttribute('aria-expanded', 'true');
  });

  it('shows only one panel at a time', () => {
    // Two open panels would overlap on a pointer-positioned menu, and the bar
    // exists to keep the surface small.
    render(<TwoGroups />);
    fireEvent.click(screen.getByRole('button', { name: 'Fill' }));
    fireEvent.click(screen.getByRole('button', { name: 'Border' }));

    expect(screen.queryByText('fill-option')).toBeNull();
    expect(screen.getByText('border-option')).toBeInTheDocument();
  });

  it('does not drag focus back to the previous group when switching groups', () => {
    // The regression: groups are siblings, and switching closes A in the same
    // commit that opens B. React flushes sibling effects in tree order, so an
    // unguarded focus-restore in A's close ran AFTER B's trigger had taken
    // focus — leaving B's panel open with focus on A's trigger.
    render(<TwoGroups />);
    const fill = screen.getByRole('button', { name: 'Fill' });
    const border = screen.getByRole('button', { name: 'Border' });

    fireEvent.click(fill);
    // Focus has to actually go INTO the open panel first, or the restore this
    // guards is never armed and the test passes with the guard deleted —
    // `fireEvent.click` does not move focus in jsdom.
    screen.getByText('fill-option').focus();
    border.focus();
    fireEvent.click(border);

    expect(document.activeElement).toBe(border);
    expect(screen.getByText('border-option')).toBeInTheDocument();
  });

  it('returns focus to its own trigger when its panel closes under it', () => {
    render(<TwoGroups />);
    const fill = screen.getByRole('button', { name: 'Fill' });
    fireEvent.click(fill);
    // Focus something inside the panel, then close the group from the trigger.
    screen.getByText('fill-option').focus();
    fireEvent.click(fill);

    expect(screen.queryByText('fill-option')).toBeNull();
    expect(document.activeElement).toBe(fill);
  });

  it('previews the group current value on the trigger', () => {
    // The bar has no captions, so the swatch is how it answers "what is this
    // set to?" without being opened.
    render(<TwoGroups />);
    const swatch = screen
      .getByRole('button', { name: 'Fill' })
      .querySelector('.annotation-menu-group-swatch');
    expect(swatch.style.backgroundColor).toBe('rgb(239, 68, 68)');
    // A group with no current value renders no swatch at all.
    expect(
      screen.getByRole('button', { name: 'Border' }).querySelector('.annotation-menu-group-swatch')
    ).toBeNull();
  });

  it('renders a transparent value as a checkerboard, not as a dark fill', () => {
    render(
      <AnnotationMenuGroup
        groupKey="fill"
        label="Fill"
        glyph="A"
        swatch="transparent"
        open={false}
        onToggle={vi.fn()}
      >
        <span />
      </AnnotationMenuGroup>
    );
    const swatch = document.querySelector('.annotation-menu-group-swatch');
    expect(swatch.className).toContain('annotation-menu-group-swatch--transparent');
    // No inline background-color: the checkerboard comes from the class, and
    // painting `transparent` inline would read as "unset" instead.
    expect(swatch.style.backgroundColor).toBe('');
  });

  it('is a disclosure, not a menu', () => {
    // `aria-haspopup="true"` is a synonym for "menu" and would promise a
    // menu-role popup; the panel is a `group`.
    render(<TwoGroups />);
    const fill = screen.getByRole('button', { name: 'Fill' });
    expect(fill).not.toHaveAttribute('aria-haspopup');
    fireEvent.click(fill);
    expect(screen.getByRole('group', { name: 'Fill' })).toBeInTheDocument();
  });
});
