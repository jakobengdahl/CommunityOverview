import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import SettingsDialog from './SettingsDialog';
import { getBuildInfo } from '../buildInfo';

vi.mock('../buildInfo', () => ({ getBuildInfo: vi.fn() }));
vi.mock('../i18n', () => ({
  useI18n: () => ({
    t: (k) => ({ 'settings.version': 'Version', 'settings.version_unknown': 'unknown' })[k] || k,
    language: 'en',
    setLanguage: () => {},
    languageSwitchingEnabled: false,
  }),
}));

describe('SettingsDialog version line', () => {
  beforeEach(() => vi.clearAllMocks());

  it('shows the build version with the short SHA', () => {
    getBuildInfo.mockReturnValue({ version: '1.2.3', sha: 'abc1234' });
    render(<SettingsDialog stats={null} onClose={() => {}} />);
    expect(screen.getByText('Version: 1.2.3 (abc1234)')).toBeInTheDocument();
  });

  it('shows the version alone when no SHA is available', () => {
    getBuildInfo.mockReturnValue({ version: '1.2.3', sha: '' });
    render(<SettingsDialog stats={null} onClose={() => {}} />);
    expect(screen.getByText('Version: 1.2.3')).toBeInTheDocument();
  });

  it('falls back to "unknown" when no version resolves', () => {
    getBuildInfo.mockReturnValue({ version: '', sha: '' });
    render(<SettingsDialog stats={null} onClose={() => {}} />);
    expect(screen.getByText('Version: unknown')).toBeInTheDocument();
  });
});
