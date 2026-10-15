import type { Book } from '@/services/libraryRobot/types';

/** 书名搜索：trim + 忽略大小写的包含匹配。 */
export function filterBooksByKeyword(books: Book[], keyword: string): Book[] {
  const query = keyword.trim().toLowerCase();
  if (!query) return books;
  return books.filter((book) => book.name.toLowerCase().includes(query));
}

/** 当前页切片：page 从 1 开始；越界时回退到最后一页（删书后页码收缩）。 */
export function pagedBooks(
  books: Book[],
  page: number,
  pageSize: number,
): Book[] {
  const pageCount = Math.max(1, Math.ceil(books.length / pageSize));
  const current = Math.min(Math.max(1, page), pageCount);
  return books.slice((current - 1) * pageSize, current * pageSize);
}

/** 书名查重（trim + 忽略大小写；编辑时排除自身）。不阻断，仅提示。 */
export function findDuplicateBookName(
  books: Book[],
  name: string,
  excludeId?: string,
): Book | undefined {
  const query = name.trim().toLowerCase();
  if (!query) return undefined;
  return books.find(
    (book) => book.id !== excludeId && book.name.toLowerCase() === query,
  );
}

/** templateId 悬空检测：书脊库清单里没有该书模板（可能已被删除/未入库）。 */
export function missingTemplateIds(
  books: Book[],
  libraryIds: ReadonlySet<string>,
): Set<string> {
  return new Set(
    books
      .map((book) => book.templateId)
      .filter((templateId) => !libraryIds.has(templateId)),
  );
}

/** 更新时间格式化：ISO 原文 → 本地 zh-CN；非法输入原样返回。 */
export function formatBookTime(iso: string): string {
  if (!iso) return '-';
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleString('zh-CN', { hour12: false });
}
