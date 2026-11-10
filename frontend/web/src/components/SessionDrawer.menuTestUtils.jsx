import { vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act, cleanup } from '@testing-library/react';
import SessionDrawer from './SessionDrawer';

if (typeof window.PointerEvent === 'undefined') {
  window.PointerEvent = class PointerEvent extends window.Event {
    constructor(type, init = {}) {
      super(type, init);
      this.pointerId = init.pointerId || 0;
      this.pointerType = init.pointerType || '';
      this.isPrimary = init.isPrimary || false;
      this.clientX = init.clientX || 0;
      this.clientY = init.clientY || 0;
      this.screenX = init.screenX || 0;
      this.screenY = init.screenY || 0;
    }
  };
}

// jsdom has no layout; positioning tests use explicit geometry.
const _nativeGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect;

beforeEach(() => {
  setMobile(false);
  vi.stubGlobal('innerWidth', 1024);
  vi.stubGlobal('innerHeight', 768);
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
    if (this.getAttribute('role') === 'menu') {
      return { x: 0, y: 0, left: 0, top: 0, right: 200, bottom: 100, width: 200, height: 100 };
    }
    return _nativeGetBoundingClientRect.call(this);
  });
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockReturnValue(200);
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(100);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
});

function makeMql(matches) {
  return {
    matches,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
}

export function setMobile(isMobile) {
  window.matchMedia = vi.fn((query) => makeMql(query === '(max-width: 768px)' ? isMobile : false));
}

export function dispatchPointer(el, type, opts = {}) {
  act(() =>
    el.dispatchEvent(
      new window.PointerEvent(type, {
        bubbles: true,
        cancelable: true,
        pointerType: opts.pointerType || '',
        clientX: opts.clientX ?? 0,
        clientY: opts.clientY ?? 0,
      })
    )
  );
}

export function renderDrawer(props = {}) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  return render(
    <SessionDrawer
      open
      onClose={vi.fn()}
      sessions={[{ id: 'abc-123', name: 'Test session' }]}
      currentSessionId="abc-123"
      onNewSession={vi.fn()}
      onConnectSession={vi.fn()}
      onSelectSession={vi.fn()}
      onRenameSession={vi.fn()}
      onDeleteSession={vi.fn()}
      onCopySessionLink={vi.fn()}
      onCopyTriggerUrl={vi.fn()}
      onOpenSettings={vi.fn()}
      {...props}
    />,
    { container }
  );
}

export function renderSessionRow(props = {}) {
  const view = renderDrawer(props);
  return { ...view, sessionName: screen.getByRole('button', { name: 'Test session abc-123' }) };
}
