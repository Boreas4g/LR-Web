export const MAX_MULTI_ROUND_TAKES = 5;

/**
 * Validate the multi-round take/return selection. Returns an error message
 * when invalid, undefined when the selection can be submitted.
 */
export function validateMultiRoundSelection(
  takeBookIds: string[],
  returnBookIds: string[],
): string | undefined {
  if (takeBookIds.length === 0) return '请至少选择一本取书。';
  if (takeBookIds.length > MAX_MULTI_ROUND_TAKES)
    return `最多只能同时取 ${MAX_MULTI_ROUND_TAKES} 本书。`;
  if (returnBookIds.length > takeBookIds.length)
    return '还书数量不能超过取书数量（还书按顺序使用 1、2、3、4、5 号槽位的书）。';
  return undefined;
}
