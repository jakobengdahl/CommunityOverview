import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

import CreateNodeDialog from '../src/components/CreateNodeDialog';
import useGraphStore from '../src/store/graphStore';
import * as api from '../src/services/api';

vi.mock('../src/services/api', () => ({
  getSubtypes: vi.fn().mockResolvedValue({ subtypes: {} }),
  addNodes: vi.fn(),
}));

function submitName(name) {
  fireEvent.change(screen.getByLabelText('Name *'), { target: { name: 'name', value: name } });
  fireEvent.submit(screen.getByLabelText('Name *').closest('form'));
}

describe('CreateNodeDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    useGraphStore.setState({ schema: { node_types: { Actor: { fields: [] } } } });
  });

  // The dialog hands the node to its owner instead of persisting it itself:
  // only the owner can tell whether the session it was opened in is still the
  // one to draw the created node into.
  it('hands the node to onSave to persist, then closes', async () => {
    const onSave = vi.fn().mockResolvedValue(true);
    const onClose = vi.fn();
    render(<CreateNodeDialog nodeType="Actor" onClose={onClose} onSave={onSave} />);

    submitName('  Acme  ');

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'Acme', type: 'Actor', tags: [], aliases: [] })
    );
    expect(api.addNodes).not.toHaveBeenCalled();
  });

  it('stays open and shows the error when saving fails', async () => {
    const onSave = vi.fn().mockRejectedValue(new Error('Server said no'));
    const onClose = vi.fn();
    render(<CreateNodeDialog nodeType="Actor" onClose={onClose} onSave={onSave} />);

    submitName('Acme');

    expect(await screen.findByText('Server said no')).toBeTruthy();
    expect(onClose).not.toHaveBeenCalled();
  });
});
