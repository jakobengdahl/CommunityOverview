import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'fs';
import { join, resolve } from 'path';
import { QuestionCircleFill, PersonFill } from 'react-bootstrap-icons';
import { resolveIcon, ICON_REGISTRY, DEFAULT_ICON } from './FloatingToolbar';

// vitest runs with the workspace root (frontend/web) as cwd
const CONFIG_DIR = resolve(process.cwd(), '../../config');

function shippedNodeTypes() {
  const entries = [];
  for (const profile of readdirSync(CONFIG_DIR, { withFileTypes: true })) {
    if (!profile.isDirectory()) continue;
    let raw;
    try {
      raw = readFileSync(join(CONFIG_DIR, profile.name, 'schema_config.json'), 'utf-8');
    } catch {
      continue;
    }
    const nodeTypes = JSON.parse(raw).schema?.node_types ?? {};
    for (const [name, config] of Object.entries(nodeTypes)) {
      entries.push({ profile: profile.name, name, config });
    }
  }
  return entries;
}

describe('resolveIcon', () => {
  it('falls back to a neutral glyph, not a question mark, for an unknown node type', () => {
    expect(resolveIcon('Customer', { node_types: {} })).toBe(DEFAULT_ICON);
    expect(resolveIcon('Customer', { node_types: {} })).not.toBe(QuestionCircleFill);
  });

  it('falls back to a neutral glyph when the schema names an unregistered icon', () => {
    const schema = { node_types: { Customer: { icon: 'HeartPulseFill' } } };
    expect(resolveIcon('Customer', schema)).toBe(DEFAULT_ICON);
    expect(resolveIcon('Customer', schema)).not.toBe(QuestionCircleFill);
  });

  it('treats Object prototype member names as unregistered icons', () => {
    for (const icon of ['toString', 'constructor', 'hasOwnProperty', '__proto__', 'valueOf']) {
      const schema = { node_types: { Customer: { icon } } };
      expect(resolveIcon('Customer', schema)).toBe(DEFAULT_ICON);
    }
  });

  it('keeps the legacy name mapping when the schema declares no icon', () => {
    expect(resolveIcon('Actor', { node_types: { Actor: {} } })).toBe(PersonFill);
    expect(resolveIcon('Actor', null)).toBe(PersonFill);
  });

  it('still honours QuestionCircleFill when a profile selects it deliberately', () => {
    const schema = { node_types: { Unclear: { icon: 'QuestionCircleFill' } } };
    expect(resolveIcon('Unclear', schema)).toBe(QuestionCircleFill);
  });

  it('resolves every icon the shipped profiles declare to its registered component', () => {
    const entries = shippedNodeTypes();
    expect(entries.length).toBeGreaterThan(0);

    const unresolved = entries
      .filter(({ name, config }) => {
        if (!config.icon) return false; // no icon configured: the fallback is the intended result
        return resolveIcon(name, { node_types: { [name]: config } }) !== ICON_REGISTRY[config.icon];
      })
      .map(({ profile, name, config }) => `${profile}/${name} -> ${config.icon}`);

    expect(unresolved).toEqual([]);
  });
});
