import { describe, expect, it } from 'vitest';

import type { Book } from '@/services/libraryRobot/types';

import {
  filterBooksByKeyword,
  findDuplicateBookName,
  formatBookTime,
  missingTemplateIds,
  pagedBooks,
} from './bookUtils';

function book(id: string, name: string, templateId = `TPL-${id}`): Book {
  return {
    id,
    name,
    templateId,
    heightMm: 550,
    createdAt: '2026-09-01T00:00:00+00:00',
    updatedAt: '2026-09-01T00:00:00+00:00',
  };
}

const BOOKS = [
  book('B1', '深入理解计算机系统'),
  book('B2', '算法导论'),
  book('B3', 'CSAPP 习题解答'),
];

describe('filterBooksByKeyword', () => {
  it('空关键词返回全部', () => {
    expect(filterBooksByKeyword(BOOKS, '  ')).toHaveLength(3);
  });

  it('按子串匹配且忽略大小写', () => {
    expect(filterBooksByKeyword(BOOKS, '算法')).toHaveLength(1);
    expect(filterBooksByKeyword(BOOKS, 'csapp')).toHaveLength(1);
    expect(filterBooksByKeyword(BOOKS, '不存在')).toHaveLength(0);
  });
});

describe('pagedBooks', () => {
  const many = Array.from({ length: 45 }, (_, index) =>
    book(`B${index}`, `书 ${index}`),
  );

  it('按页切片', () => {
    expect(pagedBooks(many, 1, 20)).toHaveLength(20);
    expect(pagedBooks(many, 3, 20)).toHaveLength(5);
  });

  it('页码越界时收缩到最后一页', () => {
    expect(pagedBooks(many, 99, 20)).toHaveLength(5);
    expect(pagedBooks([], 5, 20)).toHaveLength(0);
  });
});

describe('findDuplicateBookName', () => {
  it('命中同名（忽略大小写与首尾空格）', () => {
    expect(findDuplicateBookName(BOOKS, ' csapp 习题解答 ')?.id).toBe('B3');
    expect(findDuplicateBookName(BOOKS, '新书名')).toBeUndefined();
  });

  it('编辑时排除自身', () => {
    expect(findDuplicateBookName(BOOKS, '算法导论', 'B2')).toBeUndefined();
    expect(findDuplicateBookName(BOOKS, '算法导论', 'B1')?.id).toBe('B2');
  });
});

describe('missingTemplateIds', () => {
  it('列出书脊库中不存在的模板', () => {
    const library = new Set(['TPL-B1', 'TPL-B3']);
    expect([...missingTemplateIds(BOOKS, library)]).toEqual(['TPL-B2']);
  });

  it('全部存在时为空', () => {
    const library = new Set(['TPL-B1', 'TPL-B2', 'TPL-B3']);
    expect(missingTemplateIds(BOOKS, library).size).toBe(0);
  });
});

describe('formatBookTime', () => {
  it('格式化 ISO 时间', () => {
    const text = formatBookTime('2026-09-30T08:00:00+00:00');
    expect(text).not.toBe('2026-09-30T08:00:00+00:00');
    expect(text).toContain('2026');
  });

  it('非法输入原样返回，空串显示占位', () => {
    expect(formatBookTime('not-a-date')).toBe('not-a-date');
    expect(formatBookTime('')).toBe('-');
  });
});
