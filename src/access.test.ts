import { describe, expect, it } from 'vitest';
import access from './access';

describe('access', () => {
  it('keeps all access capabilities open for the single operator', () => {
    const initialState = {
      currentUser: {
        userid: '1',
        name: 'Admin User',
        avatar: 'https://example.com/avatar.png',
        access: 'admin',
      },
    };

    const result = access(initialState);

    expect(result.canAdmin).toBe(true);
    expect(result.canDispatch).toBe(true);
    expect(result.canMaintain).toBe(true);
    expect(result.canConfigure).toBe(true);
  });

  it('does not use a role to close debug access', () => {
    const initialState = {
      currentUser: {
        userid: '2',
        name: 'Regular User',
        avatar: 'https://example.com/avatar.png',
        access: 'user',
      },
    };

    const result = access(initialState);

    expect(result.canAdmin).toBe(true);
  });

  it('does not require a legacy access field', () => {
    const initialState = {
      currentUser: {
        userid: '3',
        name: 'Guest User',
        avatar: 'https://example.com/avatar.png',
      },
    };

    const result = access(initialState);

    expect(result.canAdmin).toBe(true);
  });

  it('keeps route capability independent from currentUser shape', () => {
    const initialState = {
      currentUser: undefined,
    };

    const result = access(initialState);

    expect(result.canAdmin).toBe(true);
  });

  it('supports Umi calling access before initial state resolves', () => {
    const result = access(undefined);

    expect(result.canAdmin).toBe(true);
  });
});
