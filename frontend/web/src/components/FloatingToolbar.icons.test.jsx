import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'fs';
import { join, resolve } from 'path';
import { QuestionCircleFill, PersonFill } from 'react-bootstrap-icons';
import { resolveIcon, ICON_REGISTRY, DEFAULT_ICON } from './FloatingToolbar';

// vitest runs with the workspace root (frontend/web) as cwd
const REPO_ROOT = resolve(process.cwd(), '../..');
const CONFIG_DIR = join(REPO_ROOT, 'config');
const CONFIG_LOADER = join(REPO_ROOT, 'backend', 'config', 'config_loader.py');

function shippedProfiles() {
  const profiles = [];
  for (const entry of readdirSync(CONFIG_DIR, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    let raw;
    try {
      raw = readFileSync(join(CONFIG_DIR, entry.name, 'schema_config.json'), 'utf-8');
    } catch {
      continue;
    }
    profiles.push({ name: entry.name, config: JSON.parse(raw) });
  }
  return profiles;
}

function shippedNodeTypes() {
  const entries = [];
  for (const profile of shippedProfiles()) {
    const nodeTypes = profile.config.schema?.node_types ?? {};
    for (const [name, config] of Object.entries(nodeTypes)) {
      entries.push({ profile: profile.name, name, config });
    }
  }
  return entries;
}

function shippedExpertAgents() {
  const entries = [];
  for (const profile of shippedProfiles()) {
    for (const agent of profile.config.presentation?.expert_agents ?? []) {
      entries.push({ profile: profile.name, id: agent.id, icon: agent.icon });
    }
  }
  return entries;
}

// The backend injects system node types and the expert agent icon default from
// Python, and both reach the UI through this registry. Reading the source keeps
// them covered without standing up the backend.
function backendSource() {
  return readFileSync(CONFIG_LOADER, 'utf-8');
}

function backendSystemNodeTypes() {
  const block = backendSource().match(/^SYSTEM_NODE_TYPES = \{$(.*?)^\}$/ms);
  if (!block) throw new Error(`SYSTEM_NODE_TYPES literal not found in ${CONFIG_LOADER}`);

  const typeNames = [];
  const entries = [];
  let current = null;
  for (const line of block[1].split('\n')) {
    const typeName = line.match(/^ {4}"(\w+)": \{$/);
    if (typeName) {
      current = typeName[1];
      typeNames.push(current);
      continue;
    }
    const icon = line.match(/^ {8}"icon": "(\w+)",?$/);
    if (icon && current) entries.push({ name: current, icon: icon[1] });
  }
  return { typeNames, entries };
}

function backendExpertAgentDefaultIcon() {
  // Anchored on the class body (its indented and blank lines): another model in
  // this file declares an `icon` field too.
  const block = backendSource().match(
    /^class ExpertAgentConfig\(BaseModel\):\n((?:[ \t].*\n|\n)*)/m
  );
  if (!block) throw new Error(`ExpertAgentConfig not found in ${CONFIG_LOADER}`);

  const match = block[1].match(/^ {4}icon: str = "(\w+)"$/m);
  if (!match) throw new Error(`ExpertAgentConfig icon default not found in ${CONFIG_LOADER}`);
  return match[1];
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

  it('resolves every icon the backend injects for system node types', () => {
    const { typeNames, entries } = backendSystemNodeTypes();
    // Parse guard: every system type must yield an icon, so neither parser drift
    // nor a system type added without an icon can quietly drop coverage.
    expect(entries.length).toBeGreaterThan(0);
    expect(entries.map(({ name }) => name)).toEqual(typeNames);

    const unresolved = entries
      .filter(
        ({ name, icon }) =>
          resolveIcon(name, { node_types: { [name]: { icon } } }) !== ICON_REGISTRY[icon]
      )
      .map(({ name, icon }) => `${name} -> ${icon}`);

    expect(unresolved).toEqual([]);
  });
});

describe('expert agent icons', () => {
  it('registers every icon the shipped profiles give an expert agent', () => {
    const agents = shippedExpertAgents();
    expect(agents.length).toBeGreaterThan(0);

    const unregistered = agents
      .filter(({ icon }) => icon && !ICON_REGISTRY[icon])
      .map(({ profile, id, icon }) => `${profile}/${id} -> ${icon}`);

    expect(unregistered).toEqual([]);
  });

  it('registers the backend default used when a profile names no icon', () => {
    const icon = backendExpertAgentDefaultIcon();
    expect(ICON_REGISTRY[icon], `${icon} is not a registered icon`).toBeDefined();
  });
});
