import type {
  MapAnnotation,
  MapAnnotationKind,
} from '@/services/libraryRobot/types';

/** 标注类型的中文显示名（下拉/标签统一使用，不再显示英文 kind 原文）。 */
export const ANNOTATION_KIND_LABELS: Record<MapAnnotationKind, string> = {
  shelf: '书架',
  cruise: '巡航',
  dock: '停靠',
  charging: '充电',
  delivery: '交付',
  zone: '区域',
};

export function annotationKindLabel(kind: MapAnnotationKind): string {
  return ANNOTATION_KIND_LABELS[kind] ?? kind;
}

/** 点位下拉筛选项：'all' 表示全部类型。 */
export type AnnotationKindFilter = MapAnnotationKind | 'all';

/** 取书/还书/多轮的导航目标默认只列书架点，操作员可切换到全部或指定类型。 */
export const DEFAULT_ANNOTATION_KIND_FILTER: AnnotationKindFilter = 'shelf';

export function filterAnnotationsByKind(
  annotations: MapAnnotation[],
  filter: AnnotationKindFilter,
): MapAnnotation[] {
  if (filter === 'all') return annotations;
  return annotations.filter((item) => item.kind === filter);
}

/** 按名称模糊过滤（trim + 忽略大小写），供下拉搜索与标注列表搜索共用。 */
export function filterAnnotationsByKeyword(
  annotations: MapAnnotation[],
  keyword: string,
): MapAnnotation[] {
  const query = keyword.trim().toLowerCase();
  if (!query) return annotations;
  return annotations.filter((item) => item.label.toLowerCase().includes(query));
}

/**
 * 创建标注的默认名称：按类型中文名递增查重（“书架 1”“书架 2”…），
 * 跳过地图中已占用的同名片，返回第一个空闲序号。
 */
export function nextAnnotationLabel(
  kind: MapAnnotationKind,
  existing: Pick<MapAnnotation, 'label'>[],
): string {
  const base = ANNOTATION_KIND_LABELS[kind] ?? kind;
  const used = new Set(existing.map((item) => item.label));
  let index = 1;
  while (used.has(`${base} ${index}`)) index += 1;
  return `${base} ${index}`;
}

/** 归一化角度到 [-180, 180)。 */
export function normalizeDegrees180(degrees: number): number {
  if (!Number.isFinite(degrees)) return 0;
  return ((((degrees + 180) % 360) + 360) % 360) - 180;
}

/** 8 方位快捷选项：0°=地图 +X（右），逆时针为正。 */
export const YAW_DIRECTIONS_8: { label: string; degrees: number }[] = [
  { label: '右', degrees: 0 },
  { label: '右上', degrees: 45 },
  { label: '上', degrees: 90 },
  { label: '左上', degrees: 135 },
  { label: '左', degrees: 180 },
  { label: '左下', degrees: -135 },
  { label: '下', degrees: -90 },
  { label: '右下', degrees: -45 },
];

/** 把任意角度吸附到最近的 8 方位，返回其在 YAW_DIRECTIONS_8 中的下标。 */
export function nearestYawDirection8(degrees: number): number {
  const normalized = normalizeDegrees180(degrees);
  let best = 0;
  let bestDistance = Number.POSITIVE_INFINITY;
  YAW_DIRECTIONS_8.forEach((direction, index) => {
    const distance = Math.abs(
      normalizeDegrees180(normalized - direction.degrees),
    );
    if (distance < bestDistance) {
      best = index;
      bestDistance = distance;
    }
  });
  return best;
}

/**
 * 朝向箭头（默认指向正上方 ↑ 的字形/三角）的 CSS 旋转角：
 * ROS 0° 朝右、逆时针为正，屏幕旋转顺时针为正，所以 90°（朝上）不旋转。
 */
export function yawArrowCssDegrees(yawDegrees: number): number {
  return 90 - normalizeDegrees180(yawDegrees);
}
