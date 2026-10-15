import { Select, Space } from 'antd';
import { useMemo, useState } from 'react';

import type {
  MapAnnotation,
  MapAnnotationKind,
} from '@/services/libraryRobot/types';
import {
  ANNOTATION_KIND_LABELS,
  type AnnotationKindFilter,
  annotationKindLabel,
  DEFAULT_ANNOTATION_KIND_FILTER,
  filterAnnotationsByKind,
} from '@/utils/annotation';

const KIND_FILTER_OPTIONS: { value: AnnotationKindFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  ...Object.entries(ANNOTATION_KIND_LABELS).map(([value, label]) => ({
    value: value as MapAnnotationKind,
    label,
  })),
];

/**
 * 取书/还书/多轮的导航目标点位下拉：可搜索（按名称过滤）、默认只列书架点，
 * 右侧类型筛选器可切换「全部」或指定类型；kind 一律显示中文名。
 */
export default function AnnotationTargetSelect({
  annotations,
  value,
  onChange,
  loading,
  disabled,
}: {
  annotations: MapAnnotation[];
  value?: string;
  onChange: (value: string | undefined) => void;
  loading?: boolean;
  disabled?: boolean;
}) {
  const [kindFilter, setKindFilter] = useState<AnnotationKindFilter>(
    DEFAULT_ANNOTATION_KIND_FILTER,
  );
  const options = useMemo(() => {
    const filtered = filterAnnotationsByKind(annotations, kindFilter);
    // 已选中的点即使被当前类型筛选排除，也保留在选项里，避免下拉回显成原始 ID。
    const selected = annotations.find((item) => item.id === value);
    const list =
      selected && !filtered.some((item) => item.id === selected.id)
        ? [selected, ...filtered]
        : filtered;
    return list.map((item) => ({
      value: item.id,
      label: `${item.label}（${annotationKindLabel(item.kind)}）`,
    }));
  }, [annotations, kindFilter, value]);
  return (
    <Space.Compact>
      <Select
        style={{ minWidth: 240 }}
        placeholder="导航目标点（标注）"
        value={value}
        onChange={onChange}
        loading={loading}
        options={options}
        showSearch={{ optionFilterProp: 'label' }}
        allowClear
        disabled={disabled}
      />
      <Select
        style={{ width: 88 }}
        value={kindFilter}
        onChange={setKindFilter}
        options={KIND_FILTER_OPTIONS}
        disabled={disabled}
      />
    </Space.Compact>
  );
}
