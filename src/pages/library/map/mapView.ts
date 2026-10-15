import type { MapVersion } from '@/services/libraryRobot/types';

/**
 * CSS transform state of the map preview layer: `translate(x, y) scale(scale)`
 * with transform-origin at the layer's top left, so a layer-local point `p`
 * is displayed at `translate + scale * p`.
 */
export interface MapViewTransform {
  scale: number;
  x: number;
  y: number;
}

export const MIN_MAP_SCALE = 1;
export const MAX_MAP_SCALE = 8;

export const IDENTITY_MAP_TRANSFORM: MapViewTransform = {
  scale: MIN_MAP_SCALE,
  x: 0,
  y: 0,
};

export function clampMapScale(scale: number): number {
  if (!Number.isFinite(scale)) return MIN_MAP_SCALE;
  return Math.min(MAX_MAP_SCALE, Math.max(MIN_MAP_SCALE, scale));
}

/**
 * Zoom to `nextScale` while keeping the layer-local point `center`
 * (viewport point expressed in the layer's untransformed frame) stationary.
 * Zooming all the way out snaps back to the centered, untranslated layout.
 */
export function zoomMapAt(
  current: MapViewTransform,
  nextScale: number,
  center: { x: number; y: number },
): MapViewTransform {
  const scale = clampMapScale(nextScale);
  if (scale === MIN_MAP_SCALE) return IDENTITY_MAP_TRANSFORM;
  const ratio = scale / current.scale;
  return {
    scale,
    x: center.x - ratio * (center.x - current.x),
    y: center.y - ratio * (center.y - current.y),
  };
}

export function panMapBy(
  current: MapViewTransform,
  delta: { x: number; y: number },
): MapViewTransform {
  return { ...current, x: current.x + delta.x, y: current.y + delta.y };
}

/**
 * CSS transform of the map layer itself: `translate(x, y) scale(scale)` with
 * transform-origin at the layer's top left. Point overlays counter this zoom
 * with `markerCenterTransform` to keep a constant on-screen size.
 */
export function mapLayerTransform(transform: MapViewTransform): string {
  return `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`;
}

/**
 * Inverse of the zoom factor, applied to point overlays (annotation/robot
 * markers) so they keep a constant on-screen size at any zoom level.
 */
export function inverseMapScale(scale: number): number {
  return 1 / clampMapScale(scale);
}

/**
 * CSS transform for a point marker anchored at its own center: the
 * translate keeps the marker centered on its anchor, the counter-scale
 * cancels the layer zoom so the marker renders at its 1x size. Marker-local
 * children (e.g. a rotated heading badge) are unaffected in angle because
 * the counter-scale is uniform.
 */
export function markerCenterTransform(scale: number): string {
  return `translate(-50%, -50%) scale(${inverseMapScale(scale)})`;
}

export interface MapPixelPoint {
  x: number;
  y: number;
}

/**
 * Convert a client (viewport) point into a clamped map image pixel. Works at
 * any zoom level because `bounds` is the rendered (already transformed) rect
 * of the map image, e.g. from `getBoundingClientRect()`.
 */
export function mapPixelFromClientPoint(
  client: { x: number; y: number },
  bounds: { left: number; top: number; width: number; height: number },
  map: Pick<MapVersion, 'width' | 'height'>,
): MapPixelPoint {
  return {
    x: Math.min(
      map.width - 1,
      Math.max(
        0,
        Math.floor(((client.x - bounds.left) / bounds.width) * map.width),
      ),
    ),
    y: Math.min(
      map.height - 1,
      Math.max(
        0,
        Math.floor(((client.y - bounds.top) / bounds.height) * map.height),
      ),
    ),
  };
}

export function radiansToDegrees(radians: number): number {
  return (radians * 180) / Math.PI;
}

export function degreesToRadians(degrees: number): number {
  return (degrees * Math.PI) / 180;
}

/** Convert a ROS yaw (radians) to degrees normalized to [-180, 180). */
export function normalizeYawDegrees(radians: number): number {
  return ((radiansToDegrees(radians) + 540) % 360) - 180;
}

/**
 * Convert ROS map coordinates (meters) to a clamped image pixel.
 * Browser pixels start at the top left; ROS map Y grows upward.
 */
export function mapPixelFromMapCoords(
  map: Pick<MapVersion, 'width' | 'height' | 'resolution' | 'origin'>,
  coords: { x: number; y: number },
): MapPixelPoint {
  return {
    x: Math.min(
      map.width - 1,
      Math.max(0, Math.round((coords.x - map.origin.x) / map.resolution)),
    ),
    y: Math.min(
      map.height - 1,
      Math.max(
        0,
        Math.round(map.height - 1 - (coords.y - map.origin.y) / map.resolution),
      ),
    ),
  };
}

/**
 * Convert an image pixel back to ROS map coordinates (meters); inverse of
 * `mapPixelFromMapCoords`. Used by marker drag-to-move to build the PATCH.
 */
export function mapCoordsFromPixel(
  map: Pick<MapVersion, 'width' | 'height' | 'resolution' | 'origin'>,
  pixel: MapPixelPoint,
): { x: number; y: number } {
  return {
    x: map.origin.x + pixel.x * map.resolution,
    y: map.origin.y + (map.height - 1 - pixel.y) * map.resolution,
  };
}

/** Build the PATCH payload for the annotation edit form (yaw is entered in degrees). */
export function buildAnnotationUpdate(values: {
  label: string;
  x: number;
  y: number;
  yawDegrees: number;
}): { label: string; x: number; y: number; yaw: number } {
  return {
    label: values.label.trim(),
    x: values.x,
    y: values.y,
    yaw: degreesToRadians(values.yawDegrees),
  };
}
