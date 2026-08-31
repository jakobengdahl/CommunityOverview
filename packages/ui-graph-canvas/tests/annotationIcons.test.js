import { describe, it, expect } from 'vitest';
import {
  ANNOTATION_ICONS,
  DEFAULT_ANNOTATION_ICON,
  resolveAnnotationIcon,
} from '../src/utils/annotationIcons';

// Mirrors ICON_REGISTRY's key list in
// frontend/web/src/components/FloatingToolbar.jsx exactly (75 Bootstrap-icon
// names, the vocabulary schema_config.json's `icon` field draws from). Kept
// as a literal list rather than importing that component: FloatingToolbar.jsx
// pulls in react-bootstrap-icons, the graph store and the host app's i18n
// system, none of which this package depends on or should. If the host
// registry's key list changes, update this list to match (see
// docs/ANNOTATION_CONTRACT.md's icon acceptance-matrix cell).
const HOST_REGISTRY_NAMES = [
  'PersonFill',
  'RocketTakeoffFill',
  'LightningFill',
  'FileEarmarkTextFill',
  'ShieldFillCheck',
  'TagsFill',
  'TrophyFill',
  'CalendarEventFill',
  'DatabaseFill',
  'ExclamationTriangleFill',
  'CpuFill',
  'BellFill',
  'BookmarkFill',
  'FolderFill',
  'PinAngleFill',
  'ClipboardDataFill',
  'PeopleFill',
  'Sliders',
  'ListOl',
  'Diagram3Fill',
  'StarFill',
  'QuestionCircleFill',
  'CircleFill',
  'CardChecklist',
  'InputCursorText',
  'ListCheck',
  'LightbulbFill',
  'GearFill',
  'CollectionFill',
  'FunnelFill',
  'GearWideConnected',
  'GlobeEuropeAfricaFill',
  'GeoAltFill',
  'MapFill',
  'BuildingFill',
  'BuildingsFill',
  'Bank',
  'BarChartFill',
  'PieChartFill',
  'GraphUpArrow',
  'FileEarmarkSpreadsheetFill',
  'FileEarmarkBarGraphFill',
  'FileEarmarkCodeFill',
  'JournalBookmarkFill',
  'BookFill',
  'Bookshelf',
  'ClipboardCheckFill',
  'Clipboard2DataFill',
  'ShieldLockFill',
  'ShieldFillExclamation',
  'LockFill',
  'KeyFill',
  'Robot',
  'MotherboardFill',
  'RouterFill',
  'ChatFill',
  'EnvelopeFill',
  'MegaphoneFill',
  'MortarboardFill',
  'AwardFill',
  'Bullseye',
  'FlagFill',
  'Diagram2Fill',
  'KanbanFill',
  'Boxes',
  'LayersFill',
  'GridFill',
  'CompassFill',
  'PuzzleFill',
  'BinocularsFill',
  'EyeFill',
  'Translate',
  'PersonLinesFill',
  'ListNested',
  'TagFill',
];

// The 11 names the set already covered before this fix (docs/ANNOTATION_CONTRACT.md,
// Corp task smallfix-annotation-icon-set-covers-11-of-75). Regression guard: the
// canonical glyph each already drew must not change.
const PREVIOUSLY_COVERED = {
  CircleFill: '●',
  FlagFill: '⚑',
  StarFill: '★',
  ExclamationTriangleFill: '⚠',
  QuestionCircleFill: '❓',
  LightbulbFill: '\u{1F4A1}',
  PinAngleFill: '\u{1F4CC}',
  BellFill: '\u{1F514}',
  Bullseye: '\u{1F3AF}',
  LockFill: '\u{1F512}',
  EyeFill: '\u{1F441}',
};

describe('annotationIcons host-registry coverage', () => {
  it('covers exactly the 75 names the host registry carries today', () => {
    expect(HOST_REGISTRY_NAMES).toHaveLength(75);
  });

  it('resolves every host-registry name to a glyph, not the two-character abbreviation fallback', () => {
    for (const name of HOST_REGISTRY_NAMES) {
      const resolved = resolveAnnotationIcon(name);
      expect(resolved.isGlyph, `${name} should resolve to a configured glyph`).toBe(true);
    }
  });

  it('draws a distinct, non-colliding glyph for every one of the 75 names', () => {
    const glyphs = HOST_REGISTRY_NAMES.map((name) => resolveAnnotationIcon(name).text);
    expect(new Set(glyphs).size).toBe(HOST_REGISTRY_NAMES.length);
  });

  it('does not regress the 11 names already covered before this fix', () => {
    for (const [name, expectedGlyph] of Object.entries(PREVIOUSLY_COVERED)) {
      expect(resolveAnnotationIcon(name)).toEqual({ text: expectedGlyph, isGlyph: true });
    }
  });

  it('still falls back to a two-character abbreviation for a name outside the vocabulary', () => {
    expect(resolveAnnotationIcon('SomeFutureIconName')).toEqual({ text: 'So', isGlyph: false });
  });

  it('still resolves the default icon for an empty/missing name', () => {
    expect(resolveAnnotationIcon(undefined)).toEqual({
      text: ANNOTATION_ICONS[DEFAULT_ANNOTATION_ICON],
      isGlyph: true,
    });
    expect(resolveAnnotationIcon('')).toEqual({
      text: ANNOTATION_ICONS[DEFAULT_ANNOTATION_ICON],
      isGlyph: true,
    });
  });

  it('still resolves everyday synonym aliases to their canonical glyph', () => {
    expect(resolveAnnotationIcon('dot')).toEqual({ text: ANNOTATION_ICONS.circle, isGlyph: true });
    expect(resolveAnnotationIcon('ok')).toEqual({ text: ANNOTATION_ICONS.check, isGlyph: true });
    expect(resolveAnnotationIcon('x')).toEqual({ text: ANNOTATION_ICONS.cross, isGlyph: true });
    expect(resolveAnnotationIcon('alert')).toEqual({
      text: ANNOTATION_ICONS.warning,
      isGlyph: true,
    });
    expect(resolveAnnotationIcon('idea')).toEqual({
      text: ANNOTATION_ICONS.lightbulb,
      isGlyph: true,
    });
  });

  it('resolves a newly-covered name via its snake_case canonical key directly', () => {
    expect(resolveAnnotationIcon('Robot')).toEqual({
      text: ANNOTATION_ICONS.robot,
      isGlyph: true,
    });
    expect(resolveAnnotationIcon('DatabaseFill')).toEqual({
      text: ANNOTATION_ICONS.database_fill,
      isGlyph: true,
    });
  });

  it('resolves every FileEarmark* name to its own distinct mark (the abbreviation collision this fix closes)', () => {
    const fileEarmarkNames = HOST_REGISTRY_NAMES.filter((name) => name.startsWith('FileEarmark'));
    expect(fileEarmarkNames.length).toBeGreaterThan(1);
    const glyphs = fileEarmarkNames.map((name) => resolveAnnotationIcon(name).text);
    expect(new Set(glyphs).size).toBe(fileEarmarkNames.length);
  });
});
