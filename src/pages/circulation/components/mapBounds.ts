import type { MapVersion } from '@/services/libraryRobot/types';

export interface MapBounds {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
}

/** ROS map-frame coverage of a map version: origin is the bottom-left corner,
 * the map extends width*resolution / height*resolution metres from it. */
export function mapBounds(
  map: Pick<MapVersion, 'origin' | 'resolution' | 'width' | 'height'>,
): MapBounds {
  return {
    minX: map.origin.x,
    maxX: map.origin.x + map.width * map.resolution,
    minY: map.origin.y,
    maxY: map.origin.y + map.height * map.resolution,
  };
}

/** Whether a hand-entered map-frame target lies inside the map. Without map
 * metadata there is nothing to check against, so the target passes and the
 * vehicle remains the final validator. */
export function targetWithinMap(
  map:
    | Pick<MapVersion, 'origin' | 'resolution' | 'width' | 'height'>
    | undefined,
  x: number,
  y: number,
): boolean {
  if (!map) return true;
  const bounds = mapBounds(map);
  return (
    x >= bounds.minX && x <= bounds.maxX && y >= bounds.minY && y <= bounds.maxY
  );
}

/** Human-readable range text for the out-of-bounds hint. */
export function mapBoundsText(
  map: Pick<MapVersion, 'origin' | 'resolution' | 'width' | 'height'>,
): string {
  const bounds = mapBounds(map);
  return `X ∈ [${bounds.minX.toFixed(2)}, ${bounds.maxX.toFixed(2)}]，Y ∈ [${bounds.minY.toFixed(2)}, ${bounds.maxY.toFixed(2)}]`;
}
