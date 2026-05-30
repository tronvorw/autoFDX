"""
身体部位条：7 个尖顶正六边形区域的几何与亮度分析。
游戏中「选中」格往往整体更暗（图标与底对比更低），可用各六边形内平均亮度找最暗一格。
几何与 ui.py 标定一致：外接圆半径 R、顶角朝上。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import cv2
import numpy as np


def pointy_top_hex_vertices(cx: float, cy: float, radius_px: float) -> np.ndarray:
    """尖顶正六边形 6 顶点，shape (6, 2) 浮点。"""
    r = float(radius_px)
    pts = []
    for k in range(6):
        ang = math.radians(-90.0 + k * 60.0)
        pts.append([cx + r * math.cos(ang), cy + r * math.sin(ang)])
    return np.array(pts, dtype=np.float32)


def mean_luma_inside_hex(bgr: np.ndarray, cx: float, cy: float, radius_px: float) -> float:
    """
    计算单帧 BGR 图像中，尖顶六边形内部的平均灰度亮度 [0,255]。
    对掩膜做一次轻微腐蚀，减轻六边形边界抗锯齿对邻格对比的干扰。
    """
    if bgr is None or bgr.size == 0:
        return float("nan")
    h, w = bgr.shape[:2]
    poly = pointy_top_hex_vertices(cx, cy, radius_px)
    mask = np.zeros((h, w), dtype=np.uint8)
    cnt = np.array([[int(round(p[0])), int(round(p[1]))] for p in poly], dtype=np.int32)
    cv2.fillConvexPoly(mask, cnt, 255)
    if float(radius_px) > 6.0:
        mask = cv2.erode(mask, np.ones((3, 3), np.uint8), iterations=1)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    sel = gray[mask > 0]
    if sel.size == 0:
        return float("nan")
    return float(np.mean(sel))


def detect_selected_body_part_by_darkest_hex(
    bgr: np.ndarray,
    centers_xy: Sequence[Tuple[float, float]],
    radius_px: float,
    *,
    min_spread: float = 10.0,
) -> Optional[int]:
    """
    在整幅 bgr 上，对 7 个六边形中心分别求内部平均亮度，返回最暗一格的 1-based 索引。
    - min_spread：最亮与最暗平均亮度至少相差该值（0~255）才认为「选中对比足够明显」，
      否则返回 None，避免全场亮度接近时误判。

    centers_xy：与 bgr 同一像素坐标系下的 7 个 (cx, cy)。
    """
    if len(centers_xy) != 7:
        return None
    lumas: List[float] = []
    for cx, cy in centers_xy:
        lumas.append(mean_luma_inside_hex(bgr, float(cx), float(cy), float(radius_px)))
    if any(math.isnan(x) for x in lumas):
        return None
    lo = min(lumas)
    hi = max(lumas)
    if hi - lo < float(min_spread):
        return None
    return int(np.argmin(lumas)) + 1


def game_norm_centers_to_template_image_pixels(
    body_part_points_norm: Sequence[Sequence[float]],
    game_w: int,
    game_h: int,
    template_region_norm: Sequence[float],
    actual_template_wh: Tuple[int, int],
) -> List[Tuple[float, float]]:
    """
    将配置里 body_part_points（相对游戏窗口归一化）换算到「已裁切的模板图」像素坐标。
    若模板 PNG 实际宽高与归一化推算不一致（取整误差），按宽高比例缩放对齐。
    """
    nx1, ny1, nx2, ny2 = [float(x) for x in template_region_norm]
    bx1 = nx1 * game_w
    by1 = ny1 * game_h
    bx2 = nx2 * game_w
    by2 = ny2 * game_h
    tw_ref = max(1.0, bx2 - bx1)
    th_ref = max(1.0, by2 - by1)
    tw_act, th_act = int(actual_template_wh[0]), int(actual_template_wh[1])
    sx = tw_act / tw_ref
    sy = th_act / th_ref
    out: List[Tuple[float, float]] = []
    for p in body_part_points_norm:
        gx = float(p[0]) * game_w
        gy = float(p[1]) * game_h
        lx = (gx - bx1) * sx
        ly = (gy - by1) * sy
        out.append((lx, ly))
    return out


def analyze_template_png_with_config(
    png_path: Path,
    body_part_points_norm: Sequence[Sequence[float]],
    radius_px: float,
    template_region_norm: Sequence[float],
    *,
    game_w: int = 1920,
    game_h: int = 1080,
    min_spread: float = 10.0,
) -> Tuple[Optional[int], List[float]]:
    """
    离线：对保存的 custom_body_part_switch.png 与当前配置推算中心在图内坐标，返回
    (最暗格 1~7 或 None, 七格平均亮度列表)。
    game_w/game_h 应与标定时游戏客户区一致；若未知可用默认 1920x1080 或从窗口服务读取。
    """
    bgr = cv2.imread(str(png_path))
    if bgr is None:
        return None, []
    th, tw = bgr.shape[:2]
    centers = game_norm_centers_to_template_image_pixels(
        body_part_points_norm, game_w, game_h, template_region_norm, (tw, th)
    )
    # 模板图上的等效半径：随模板相对游戏裁切框缩放（横向为主，与 ui 保存时一致）
    nx1, ny1, nx2, ny2 = [float(x) for x in template_region_norm]
    tw_ref = max(1e-6, (nx2 - nx1) * game_w)
    r_on_template = float(radius_px) * (tw / tw_ref)
    lumas = [mean_luma_inside_hex(bgr, cx, cy, r_on_template) for cx, cy in centers]
    if len(lumas) != 7 or any(math.isnan(x) for x in lumas):
        return None, lumas
    lo, hi = min(lumas), max(lumas)
    if hi - lo < min_spread:
        return None, lumas
    return int(np.argmin(lumas)) + 1, lumas
