// covers: frontend/web/src/store/userPreferencesStore.js
import { describe, it, expect, vi } from 'vitest';
import useUserPreferencesStore from './userPreferencesStore';
import { getUserPreferences } from '../services/api';

vi.mock('../services/api', () => ({
  getUserPreferences: vi.fn(),
  saveUserPreferences: vi.fn(),
}));

describe('userPreferencesStore.load', () => {
  it.each([
    [
      'a signed-in user with it on',
      { signed_in: true, preferences: { hideClassicSurfaces: true } },
      true,
    ],
    ['a signed-in user without a saved value', { signed_in: true, preferences: {} }, false],
    [
      'an anonymous session',
      { signed_in: false, preferences: { hideClassicSurfaces: true } },
      false,
    ],
    ['a failed load', new Error('offline'), false],
  ])('replaces any previous user value with that of %s', async (_case, answer, expected) => {
    useUserPreferencesStore.setState({
      signedIn: true,
      preferences: { hideClassicSurfaces: true },
    });
    if (answer instanceof Error) getUserPreferences.mockRejectedValueOnce(answer);
    else getUserPreferences.mockResolvedValueOnce(answer);

    await useUserPreferencesStore.getState().load();

    expect(useUserPreferencesStore.getState().preferences.hideClassicSurfaces).toBe(expected);
  });
});
