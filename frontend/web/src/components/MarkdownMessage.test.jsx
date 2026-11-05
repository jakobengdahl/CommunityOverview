// covers: frontend/web/src/components/MarkdownMessage.jsx
import { render } from '@testing-library/react';
import { it, expect } from 'vitest';
import MarkdownMessage from './MarkdownMessage';
import { I18nProvider } from '../i18n';

it('shows image and link targets without fetching resources outside inline images', () => {
  const targets = [
    'https://example.invalid/x',
    '//example.invalid/x',
    '/\\example.invalid/x',
    `${window.location.protocol}//${window.location.hostname}:4321/x`,
    '/x?d=secret',
    '/x',
  ];
  const markdown = [
    '![remote](https://example.invalid/image)',
    '![local](/image?d=secret)',
    '![inline](data:image/png;base64,YQ==)',
    ...targets.map((url) => `[label](${url.replaceAll('\\', '\\\\')})`),
    '$\\href{https://example.invalid/math}{label}$',
    '$\\url{https://example.invalid/url}$',
    '$\\includegraphics{https://example.invalid/image}$',
  ].join('\n\n');
  const { container } = render(
    <I18nProvider>
      <MarkdownMessage>{markdown}</MarkdownMessage>
    </I18nProvider>
  );
  expect([...container.querySelectorAll('[src]')].map((el) => el.getAttribute('src'))).toEqual([
    'data:image/png;base64,YQ==',
  ]);
  const links = [...container.querySelectorAll('a')];
  expect(links).toHaveLength(targets.length);
  for (const link of links) {
    expect(link.textContent).toContain(link.getAttribute('href'));
    expect(link.rel).toBe('noreferrer noopener');
  }
});
