import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

describe('PWA install metadata', () => {
  it('keeps the manifest installable without session-scoped start parameters', async () => {
    const manifestPath = path.join(webRoot, 'public', 'manifest.webmanifest');
    const manifest = JSON.parse(await readFile(manifestPath, 'utf8'));

    expect(manifest).toMatchObject({
      name: 'Community Knowledge Graph',
      short_name: 'Community Graph',
      start_url: '/',
      display: 'standalone',
      background_color: '#1a1a1a',
      theme_color: '#1a1a1a',
    });
    expect(manifest.start_url).not.toMatch(/[?&](session|collect|akc)=/);
    expect(manifest.icons).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          src: '/icon-192.png',
          sizes: '192x192',
          type: 'image/png',
          purpose: 'any',
        }),
        expect.objectContaining({
          src: '/icon-512.png',
          sizes: '512x512',
          type: 'image/png',
          purpose: 'any',
        }),
        expect.objectContaining({
          src: '/icon-maskable-192.png',
          sizes: '192x192',
          type: 'image/png',
          purpose: 'maskable',
        }),
        expect.objectContaining({
          src: '/icon-maskable-512.png',
          sizes: '512x512',
          type: 'image/png',
          purpose: 'maskable',
        }),
      ])
    );
  });

  it('exposes manifest and iOS add-to-home-screen metadata in the app shell', async () => {
    const html = await readFile(path.join(webRoot, 'index.html'), 'utf8');

    expect(html).toContain('<link rel="manifest" href="/manifest.webmanifest" />');
    expect(html).toContain('<meta name="theme-color" content="#1a1a1a" />');
    expect(html).toContain('<link rel="apple-touch-icon" href="/icon-192.png" />');
    expect(html).toContain('<meta name="apple-mobile-web-app-capable" content="yes" />');
    expect(html).toContain(
      '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />'
    );
  });
});
