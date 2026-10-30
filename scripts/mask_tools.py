#!/usr/bin/env python3
"""Create inspectable static alpha masks and explicit cumulative/replacement states.

Does not infer objects, track video, or edit a timeline. See mask-spec.example.json.
"""
import argparse
import json
from pathlib import Path
import re
import sys

import cv2
import numpy as np


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', value):
        raise ValueError('Layer/state ids must use letters, digits, hyphens or underscores; no paths.')
    return value


def polygon(points, width, height):
    coords = np.asarray(points, dtype=np.float64)
    if coords.ndim != 2 or coords.shape[1] != 2 or len(coords) < 3:
        raise ValueError('A polygon needs at least 3 x/y points.')
    if not np.isfinite(coords).all() or np.any(coords < 0) or np.any(coords > 1):
        raise ValueError('Polygon coordinates must be finite, normalized to [0, 1].')
    coords = np.rint(coords * np.array([width - 1, height - 1])).astype(np.int32)
    output = np.zeros((height, width), np.uint8)
    cv2.fillPoly(output, [coords], 1)
    return output.astype(bool)


def interval(value, maximum, name):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f'{name} must be [minimum, maximum].')
    lo, hi = value
    if not all(isinstance(x, (int, float)) and np.isfinite(x) for x in value) or not 0 <= lo <= hi <= maximum:
        raise ValueError(f'{name} must be ordered within 0–{maximum}.')
    return lo, hi


def colour_mask(hsv, settings):
    h, s, v = cv2.split(hsv)
    result = np.ones(h.shape, bool)
    if 'hue_bands' in settings:
        hue = np.zeros(h.shape, bool)
        for band in settings['hue_bands']:
            lo, hi = interval(band, 179, 'hue_bands')
            hue |= (h >= lo) & (h <= hi)
        result &= hue
    for key, channel in [('saturation', s), ('value', v)]:
        if key in settings:
            lo, hi = interval(settings[key], 255, key)
            result &= (channel >= lo) & (channel <= hi)
    return result


def clean_mask(mask, settings):
    height, width = mask.shape
    close_radius = settings.get('close_radius_px', 0)
    inset = settings.get('inset_px', 0)
    if not isinstance(close_radius, int) or close_radius < 0 or not isinstance(inset, int) or inset < 0:
        raise ValueError('close_radius_px/inset_px must be nonnegative integer pixels.')
    for key in ['remove_under_area_ratio', 'fill_holes_under_area_ratio']:
        if not 0 <= settings.get(key, 0) <= 1:
            raise ValueError(f'{key} must be 0–1.')
    if close_radius:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * close_radius + 1,) * 2)
        mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel).astype(bool)
    minimum = settings.get('remove_under_area_ratio', 0) * width * height
    if minimum:
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
        keep = np.zeros(count, np.uint8)
        keep[1:] = stats[1:, cv2.CC_STAT_AREA] >= minimum
        mask = keep[labels].astype(bool)
    maximum = settings.get('fill_holes_under_area_ratio', 0) * width * height
    if maximum:
        count, labels, stats, _ = cv2.connectedComponentsWithStats((~mask).astype(np.uint8), 8)
        fill = np.zeros(count, np.uint8)
        # Border-connected background is never a hole, even in a small image.
        border = set(np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]]).tolist())
        for index in range(1, count):
            if index not in border and stats[index, cv2.CC_STAT_AREA] <= maximum:
                fill[index] = 1
        mask |= fill[labels].astype(bool)
    if inset:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * inset + 1,) * 2)
        mask = cv2.erode(mask.astype(np.uint8), kernel).astype(bool)
    feather = float(settings.get('feather_px', 0))
    if not np.isfinite(feather) or feather < 0:
        raise ValueError('feather_px must be finite and nonnegative.')
    alpha = mask.astype(np.float32)
    if feather:
        alpha = cv2.GaussianBlur(alpha, (0, 0), feather)
    return mask, np.clip(alpha, 0, 1)


def render_masks(image, spec):
    height, width = image.shape[:2]
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    masks, alphas = {}, {}
    for layer in spec.get('layers', []):
        name = safe_id(layer.get('id'))
        if name in masks:
            raise ValueError(f'Duplicate layer: {name}')
        if not any(k in layer for k in ['polygon', 'hsv', 'union']) and not layer.get('full_frame'):
            raise ValueError(f'{name}: declare polygon, hsv, union or full_frame.')
        has_base = 'polygon' in layer or 'hsv' in layer or layer.get('full_frame')
        mask = np.ones((height, width), bool) if has_base else np.zeros((height, width), bool)
        protect = np.zeros((height, width), bool)
        allowed = np.ones((height, width), bool)
        if 'polygon' in layer:
            mask &= polygon(layer['polygon'], width, height)
        if 'hsv' in layer:
            mask &= colour_mask(hsv, layer['hsv'])
        for operation in ['union', 'intersect', 'subtract']:
            for other in layer.get(operation, []):
                if other not in masks:
                    raise ValueError(f'{name}: {other} must be defined before it is referenced.')
                if operation == 'union':
                    mask |= masks[other]
                elif operation == 'intersect':
                    mask &= masks[other]
                    allowed &= masks[other]
                else:
                    mask &= ~masks[other]
                    protect |= masks[other]
        # Protect explicit physical holes after all Boolean operations and cleanup.
        holes = np.zeros((height, width), bool)
        for points in layer.get('holes', []):
            holes |= polygon(points, width, height)
        mask &= ~holes
        mask, alpha = clean_mask(mask, layer.get('cleanup', {}))
        final_allowed = allowed & ~protect & ~holes
        mask &= final_allowed
        alpha[~final_allowed] = 0
        masks[name], alphas[name] = mask, alpha
    if not masks:
        raise ValueError('No layers.')
    gain = float(spec.get('background_gain', .55))
    if not np.isfinite(gain) or not 0 <= gain <= 1:
        raise ValueError('background_gain must be finite within 0–1.')
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY).astype(np.float32) * gain
    background = np.repeat(gray[..., None], 3, axis=2)

    def composite(alpha):
        a = alpha[..., None]
        return np.rint(image.astype(np.float32) * a + background * (1 - a)).clip(0, 255).astype(np.uint8)

    states = []
    previous = np.zeros((height, width), np.float32)
    seen_states = set()
    for state in spec.get('states', []):
        name = safe_id(state.get('id'))
        if name in seen_states:
            raise ValueError(f'Duplicate state: {name}')
        seen_states.add(name)
        mode = state.get('mode', 'replace')
        if mode not in ('replace', 'accumulate'):
            raise ValueError('State mode must be replace or accumulate.')
        alpha = np.zeros((height, width), np.float32)
        for other in state.get('layers', []):
            if other not in alphas:
                raise ValueError(f'Unknown state layer: {other}')
            alpha = np.maximum(alpha, alphas[other])
        if mode == 'accumulate':
            alpha = np.maximum(previous, alpha)
        states.append((name, mode, alpha.copy(), composite(alpha)))
        previous = alpha
    return masks, alphas, states, composite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--spec', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    image = cv2.imread(str(args.image))
    if image is None:
        parser.error('Cannot read the working image.')
    settings = json.loads(args.spec.read_text(encoding='utf-8'))
    masks, alphas, states, composite = render_masks(image, settings)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    summary = {'width': image.shape[1], 'height': image.shape[0], 'layers': [], 'states': [],
               'semantic_inspection_performed_by_script': False}

    def write(name, data):
        if not cv2.imwrite(str(out / name), data):
            raise OSError(f'Cannot write {name}')

    for name, mask in masks.items():
        write(f'{name}_alpha.png', np.rint(alphas[name] * 255).astype(np.uint8))
        write(f'{name}_composite.png', composite(alphas[name]))
        overlay = image.copy()
        contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(overlay, contours, -1, (0, 0, 255), 1)
        write(f'{name}_boundary.png', overlay)
        summary['layers'].append({'id': name, 'area_ratio': round(float(mask.mean()), 8),
                                  'alpha': f'{name}_alpha.png', 'composite': f'{name}_composite.png',
                                  'boundary': f'{name}_boundary.png'})
    for name, mode, alpha, result in states:
        write(f'state_{name}.png', result)
        write(f'state_{name}_alpha.png', np.rint(alpha * 255).astype(np.uint8))
        summary['states'].append({'id': name, 'mode': mode, 'image': f'state_{name}.png',
                                  'mean_alpha': round(float(alpha.mean()), 8)})
    (out / 'mask_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'layers': len(masks), 'states': len(states), 'output': str(out)}, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        raise SystemExit(1)
