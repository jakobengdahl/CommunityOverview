/**
 * @vitest-environment jsdom
 */
import { describe, it, expect } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useSurfaceManager, SURFACES } from './useSurfaceManager';

describe('useSurfaceManager', () => {
  it('starts with no surface open by default', () => {
    const { result } = renderHook(() => useSurfaceManager());
    expect(result.current.openSurface).toBe('none');
    expect(result.current.isOpen('search')).toBe(false);
  });

  it('accepts a valid initial surface', () => {
    const { result } = renderHook(() => useSurfaceManager('chat'));
    expect(result.current.openSurface).toBe('chat');
  });

  it('falls back to none for an invalid initial surface', () => {
    const { result } = renderHook(() => useSurfaceManager('not-a-real-surface'));
    expect(result.current.openSurface).toBe('none');
  });

  it('open() sets the requested surface as open', () => {
    const { result } = renderHook(() => useSurfaceManager());
    act(() => result.current.open('search'));
    expect(result.current.openSurface).toBe('search');
    expect(result.current.isOpen('search')).toBe(true);
  });

  it('opening a new surface closes whichever surface was previously open (mutual exclusion)', () => {
    const { result } = renderHook(() => useSurfaceManager());

    act(() => result.current.open('search'));
    expect(result.current.isOpen('search')).toBe(true);

    act(() => result.current.open('chat'));
    expect(result.current.isOpen('chat')).toBe(true);
    expect(result.current.isOpen('search')).toBe(false);
    expect(result.current.openSurface).toBe('chat');
  });

  it('never reports more than one surface open at a time, across every surface pair', () => {
    const { result } = renderHook(() => useSurfaceManager());

    for (const surface of SURFACES.filter((s) => s !== 'none')) {
      act(() => result.current.open(surface));
      const openCount = SURFACES.filter((s) => result.current.isOpen(s)).length;
      expect(openCount).toBe(1);
    }
  });

  it('close() returns to none', () => {
    const { result } = renderHook(() => useSurfaceManager());
    act(() => result.current.open('menu'));
    act(() => result.current.close());
    expect(result.current.openSurface).toBe('none');
  });

  it('toggle() opens a closed surface and closes it again on a second call', () => {
    const { result } = renderHook(() => useSurfaceManager());

    act(() => result.current.toggle('detail'));
    expect(result.current.openSurface).toBe('detail');

    act(() => result.current.toggle('detail'));
    expect(result.current.openSurface).toBe('none');
  });

  it('toggle() switches to a different surface rather than closing it when another one is open', () => {
    const { result } = renderHook(() => useSurfaceManager());

    act(() => result.current.open('search'));
    act(() => result.current.toggle('create'));

    expect(result.current.openSurface).toBe('create');
    expect(result.current.isOpen('search')).toBe(false);
  });

  it('open() rejects an unknown surface name', () => {
    const { result } = renderHook(() => useSurfaceManager());
    expect(() => act(() => result.current.open('unknown'))).toThrow(/unknown surface/);
  });

  it('toggle() rejects an unknown surface name', () => {
    const { result } = renderHook(() => useSurfaceManager());
    expect(() => act(() => result.current.toggle('unknown'))).toThrow(/unknown surface/);
  });
});
