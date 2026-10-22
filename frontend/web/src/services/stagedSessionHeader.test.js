import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { isSessionAware, needsSessionHeader, pathBelowRoot } from './stagedSessionHeader';

// The frontend's list must name every route backend/api_host/session_binding.py
// stages or composes on: a route missing here would read or write a staged
// session's graph without the session.
function backendAwareRoutes() {
  const source = readFileSync(
    // Tests run from frontend/web (the workspace root vitest is started in).
    resolve(process.cwd(), '../../backend/api_host/session_binding.py'),
    'utf8'
  );
  const block = (name) => {
    const at = source.indexOf(`${name}:`);
    return source.slice(at, source.indexOf('\n)\n', at));
  };
  const entries = (text, prefix) =>
    [...text.matchAll(/\("(\*|[A-Z]+)",\s*r"([^"]+)"\)/g)].map(([, method, path]) => [
      method,
      prefix + path.replace(/\[\^\/\]\+/g, 'x').replace(/\(\/\.\*\)\?/g, ''),
    ]);
  return [
    ...entries(block('_API_SESSION_AWARE'), '/api'),
    ...entries(block('_HOST_SESSION_AWARE'), ''),
  ];
}

describe('session-aware routes', () => {
  it('match every route the backend stages or composes on', () => {
    const routes = backendAwareRoutes().filter(([method]) => method !== '*');
    expect(routes.length).toBeGreaterThan(20);
    for (const [method, path] of routes) {
      expect([method, path, isSessionAware(method, path)]).toEqual([method, path, true]);
    }
  });

  it('treat every graph-changing verb as needing the header', () => {
    for (const verb of ['POST', 'PUT', 'PATCH', 'DELETE']) {
      expect(needsSessionHeader(verb, '/api/something/new')).toBe(true);
    }
  });

  it('read the path below a deployment root', () => {
    expect(pathBelowRoot('/app/api/nodes?x=1', '/app')).toBe('/api/nodes');
    expect(needsSessionHeader('POST', pathBelowRoot('/app/api/nodes', '/app'))).toBe(true);
  });
});
