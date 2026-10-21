import { describe, it, expect, vi, afterEach } from 'vitest';
import { getBuildInfo } from './buildInfo';

describe('getBuildInfo', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('returns empty strings instead of throwing when the build globals are undefined', () => {
    expect(getBuildInfo()).toEqual({ version: '', sha: '' });
  });

  it('returns the injected build values', () => {
    vi.stubGlobal('__APP_VERSION__', '2.0.1');
    vi.stubGlobal('__APP_BUILD_SHA__', 'abc1234');
    expect(getBuildInfo()).toEqual({ version: '2.0.1', sha: 'abc1234' });
  });
});
