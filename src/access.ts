/**
 * Temporary single-operator access map.  Keep the standard Umi argument and
 * legacy canAdmin key for existing routes while every logged-in user debugs.
 */
export default function access(_initialState?: unknown) {
  return {
    canAdmin: true,
    canDispatch: true,
    canMaintain: true,
    canConfigure: true,
  };
}
