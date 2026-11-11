#!/usr/bin/env python3
"""Raw-image input adapter for TenVision's local shape matcher.

Adapted from TenVision, Copyright (c) 2026 Starry-Sky-World.
MIT license: see LICENSE-TenVision in this experiment directory.
No browser, model API, or network calls are used by this module.
"""
import argparse
import itertools
import json
from pathlib import Path

import cv2
import numpy as np


def normalize_mask(binary_mask, canvas_size=48, symbol_size=34):
    ys, xs = np.where(binary_mask > 0)
    if xs.size == 0:
        return None
    crop = binary_mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    height, width = crop.shape
    scale = symbol_size / max(height, width)
    resized = cv2.resize(crop, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_NEAREST)
    canvas = np.zeros((canvas_size, canvas_size), dtype=np.uint8)
    offset_y = (canvas_size - resized.shape[0]) // 2
    offset_x = (canvas_size - resized.shape[1]) // 2
    canvas[offset_y:offset_y + resized.shape[0], offset_x:offset_x + resized.shape[1]] = resized
    return canvas


def rotate_mask(mask, angle):
    height, width = mask.shape
    matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
    return cv2.warpAffine(mask, matrix, (width, height), flags=cv2.INTER_NEAREST, borderValue=0)


def match_cost(query, candidate):
    best = float(np.sum(cv2.absdiff(query, candidate)) / 255.0)
    for angle in (-60, -45, -30, -20, -10, 10, 20, 30, 45, 60, 90, -90):
        rotated = rotate_mask(query, angle)
        score = float(np.sum(cv2.absdiff(rotated, candidate)) / 255.0)
        best = min(best, score)
    return best


def instruction_masks(sprite, raw=False, cells=None):
    gray_cells = ([cv2.cvtColor(cell, cv2.COLOR_BGR2GRAY) for cell in cells] if cells is not None
                  else np.array_split(cv2.cvtColor(sprite, cv2.COLOR_BGR2GRAY), 3, axis=1))
    masks = []
    for cell in gray_cells:
        _, mask = cv2.threshold(cell, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        mask[:2, :] = mask[-2:, :] = 0
        mask[:, :2] = mask[:, -2:] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        normalized = normalize_mask(mask)
        if normalized is None:
            raise ValueError("Empty instruction symbol")
        masks.append(mask if raw else normalized)
    return masks


def crop_foreground(mask):
    ys, xs = np.where(mask > 0)
    if xs.size == 0:
        return None
    return mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def locate_with_template(query_mask, main_mask):
    """TenVision's original scale/rotation template-search fallback."""
    query_crop = crop_foreground(query_mask)
    if query_crop is None or min(query_crop.shape) < 10:
        return -1.0, None
    best_score, best_center = -1.0, None
    height, width = query_crop.shape
    for scale in np.linspace(1.1, 3.4, 16):
        base = cv2.resize(query_crop, (max(8, round(width * scale)), max(8, round(height * scale))), interpolation=cv2.INTER_NEAREST)
        for angle in range(-90, 91, 10):
            rotated = crop_foreground(rotate_mask(base, angle))
            if rotated is None or rotated.shape[0] >= main_mask.shape[0] or rotated.shape[1] >= main_mask.shape[1]:
                continue
            if np.count_nonzero(rotated) < 40:
                continue
            matches = cv2.matchTemplate(main_mask, rotated, cv2.TM_CCOEFF_NORMED)
            _, score, _, location = cv2.minMaxLoc(matches)
            if score > best_score:
                best_score = float(score)
                best_center = [location[0] + rotated.shape[1] // 2, location[1] + rotated.shape[0] // 2]
    return best_score, best_center


def candidates_from_background(background):
    # Keep TenVision's grayscale segmentation: chroma filtering breaks
    # antialiased strokes where symbols blend with a colored photograph.
    gray = cv2.cvtColor(background, cv2.COLOR_BGR2GRAY)
    mask = (gray < 60).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    count, labels, stats, centers = cv2.connectedComponentsWithStats(mask, connectivity=8)
    candidates = []
    for index in range(1, count):
        x, y, width, height, area = (int(value) for value in stats[index])
        if not (100 <= area <= 16000 and 15 <= width <= 170 and 15 <= height <= 170):
            continue
        if not (1 / 3 <= width / height <= 3):
            continue
        component = np.where(labels[y:y + height, x:x + width] == index, 255, 0).astype(np.uint8)
        normalized = normalize_mask(component)
        if normalized is not None:
            candidates.append({"center": [round(float(value)) for value in centers[index]], "box": [x, y, width, height], "norm": normalized})
    return candidates


def solve(sprite_path, background_path, config):
    sprite = cv2.imread(str(sprite_path))
    background = cv2.imread(str(background_path))
    if sprite is None or background is None:
        raise ValueError("Cannot decode challenge images")
    expected = config['bg_elem_cfg']['size_2d']
    if list(background.shape[1::-1]) != expected:
        raise ValueError('Native background dimensions changed')
    instructions = config['ins_elem_cfg']
    if not isinstance(instructions, list) or len(instructions) not in (1, 3):
        raise ValueError('Unsupported instruction layout')
    cells = []
    for item in instructions:
        x, y = item['sprite_pos']
        w, h = item['size_2d']
        if (any(type(n) is not int for n in (x, y, w, h)) or min(x, y) < 0 or min(w, h) < 5
                or x+w > sprite.shape[1] or y+h > sprite.shape[0]):
            raise ValueError('Invalid instruction crop')
        cells.append(sprite[y:y+h, x:x+w])
    if len(cells) == 1:
        cells = list(np.array_split(cells[0], 3, axis=1))
    queries = instruction_masks(sprite, cells=cells)
    candidates = candidates_from_background(background)
    if len(candidates) < 3:
        raise ValueError("Fewer than three symbol candidates")
    costs = [[match_cost(query, candidate["norm"]) for candidate in candidates] for query in queries]
    shortlists = [sorted(range(len(candidates)), key=lambda index: row[index])[:8] for row in costs]
    assignments = (choice for choice in itertools.product(*shortlists) if len(set(choice)) == 3)
    selected = min(assignments, key=lambda choice: sum(costs[index][candidate] for index, candidate in enumerate(choice)))
    points = [candidates[index]["center"] for index in selected]
    base_costs = [costs[query][candidate] for query, candidate in enumerate(selected)]
    raw_queries = instruction_masks(sprite, raw=True, cells=cells)
    main_mask = (cv2.cvtColor(background, cv2.COLOR_BGR2GRAY) < 80).astype(np.uint8) * 255
    template_scores = [None] * 3
    for index, query in enumerate(raw_queries):
        if base_costs[index] < 280:
            continue
        score, point = locate_with_template(query, main_mask)
        template_scores[index] = score
        if point is not None and score >= 0.60 and all(
            other == index or np.linalg.norm(np.array(point) - np.array(existing)) >= 26
            for other, existing in enumerate(points)
        ):
            points[index] = point
    return {
        "points": points,
        "costs": base_costs,
        "template_scores": template_scores,
        "candidate_count": len(candidates),
        "recognizer": "TenVision raw-input shape matcher",
        "browser_started": False,
        "model_api_used": False,
    }


if __name__ == "__main__":
    import sys
    # Worker output is private to the host; never write annotated images/proofs.
    try:
        result = solve(Path('/sprite.png'), Path('/background.png'),
                       json.loads(Path('/layout.json').read_text()))
        print(json.dumps({'points': result['points']}, separators=(',', ':')))
    except Exception:
        sys.exit(1)
