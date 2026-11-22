import { create } from 'zustand';
import { getUserPreferences, saveUserPreferences } from '../services/api';

// The signed-in user's remembered preferences, kept by the server per user and
// never in this browser, so another user of the same device does not inherit
// them. Until loaded, and in an anonymous session, every value is its default.
export const PREFERENCE_DEFAULTS = { hideClassicSurfaces: false };

const useUserPreferencesStore = create((set) => ({
  signedIn: false,
  preferences: PREFERENCE_DEFAULTS,
  saving: false,
  saveFailed: false,

  load: async () => {
    try {
      const { signed_in: signedIn, preferences } = await getUserPreferences();
      set({
        signedIn: !!signedIn,
        preferences: { ...PREFERENCE_DEFAULTS, ...(signedIn ? preferences : {}) },
      });
    } catch {
      set({ signedIn: false, preferences: PREFERENCE_DEFAULTS });
    }
  },

  // Applied only once saved: a failed save keeps the previous value in effect.
  setHideClassicSurfaces: async (value) => {
    set({ saving: true, saveFailed: false });
    try {
      const { preferences } = await saveUserPreferences({ hideClassicSurfaces: value });
      set({ preferences: { ...PREFERENCE_DEFAULTS, ...preferences }, saving: false });
    } catch {
      set({ saving: false, saveFailed: true });
    }
  },
}));

export default useUserPreferencesStore;
