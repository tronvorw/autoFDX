from pathlib import Path
from time import monotonic

import cv2
import numpy as np
import pyautogui

from .config import PROJECT_ROOT, TEMPLATES_DIR
from . import log_codes as C
from .run_log import log, log_debug

# 赞池：环形分为若干充能格，用「有蓝的格数/总格数」统计，分隔线不计入填充。
LIKE_POOL_SEGMENT_COUNT = 10
LIKE_POOL_SEGMENT_BLUE_MIN = 0.32
# 首次检测到充能后，从中心红心学习圆心；外接矩形与环宽仍沿用标定。
LIKE_POOL_LEARN_MIN_FILL = 0.06

# 主页判定：下列模板均未匹配时视为「主页」（赞池单独用更严的可见性检测）。
HOME_PAGE_PROBE_TEMPLATES = (
    "start",
    "finish",
    "cum1",
    "cum2",
    "cum_single",
    "experiment_selected_flag",
    "recover_stamina_button",
    "stamina_insufficient_icon",
    "stamina_supplement_button",
    "special_action_button",
    "body_part_switch",
    "sensitive_progress_bar",
)

HOME_PAGE_TEMPLATE_SHORT = {
    "start": "开始",
    "finish": "结束",
    "cum1": "高潮1",
    "cum2": "高潮",
    "cum_single": "单高潮",
    "experiment_selected_flag": "实验标志",
    "recover_stamina_button": "恢复体力",
    "stamina_insufficient_icon": "体力不足",
    "stamina_supplement_button": "补充体力",
    "special_action_button": "特殊动作",
    "body_part_switch": "身体部位",
    "sensitive_progress_bar": "敏感条",
}

# 主页赞池可见性（严于 like_pool 充能统计，降低误判）。
HOME_PAGE_LIKE_POOL_HEART_MIN_PIXELS = 18
HOME_PAGE_LIKE_POOL_RING_STD_MIN = 18.0
HOME_PAGE_LIKE_POOL_RING_MEAN_MIN = 35.0
HOME_PAGE_LIKE_POOL_RING_BLUE_RATIO_MIN = 0.06

# bar_regions 槽位 -> calibration_rects 键名
_BAR_SLOT_CALIB_KEY = {
    "bar1": "bar_female",
    "bar2": "bar_male",
    "sensitive_progress_bar": "sensitive_progress_bar",
}


def like_pool_annulus_radii(ow, oh, rw):
    """
    赞池圆环几何（裁切区坐标，原点为外接矩形左上角）：
    - 外圆：矩形 ow×oh 的内接圆；
    - 环宽：沿径向，宽度 ≈ rw × min(ow,oh)（像素取整后与标定滑块一致）；
    - 内圆：外圆半径减去环宽。
    返回 (cx, cy, r_out, r_in)；无效时返回 None。
    """
    if ow < 4 or oh < 4:
        return None
    rw = max(0.05, min(0.45, float(rw)))
    min_side = float(min(ow, oh))
    cx = float(ow) * 0.5
    cy = float(oh) * 0.5
    r_out = min_side * 0.5
    band = int(round(rw * min_side))
    band = max(1, min(band, max(1, int(np.floor(r_out)) - 1)))
    r_in = float(r_out) - float(band)
    if r_in <= 1e-6 or r_in >= r_out:
        return None
    return cx, cy, float(r_out), r_in


class VisionService:
    """负责模板匹配与进度条识别。"""

    def __init__(self, config_store, runtime_state, window_service):
        self.config_store = config_store
        self.runtime_state = runtime_state
        self.window_service = window_service
        # 模板缓存：避免高频 match 时重复磁盘读取/解码导致卡顿。
        # 结构: {abs_path: (mtime, gray_image)}
        self._template_cache = {}
        # 赞池动态圆心（窗口归一化 [nx, ny]）；外接框尺寸与环宽比例仍沿用标定。
        self._learned_like_pool_center_norm = None
        self._last_progress_diag_log_ts = {}

    def reset_learned_progress_bar_regions(self):
        """切换实验或重新部署后清空赞池动态圆心（进度条始终用标定框）。"""
        self._learned_like_pool_center_norm = None

    def _progress_diag_due(self, key, interval_sec=1.0):
        now = monotonic()
        last = self._last_progress_diag_log_ts.get(key, 0.0)
        if now - last < interval_sec:
            return False
        self._last_progress_diag_log_ts[key] = now
        return True

    @property
    def config(self):
        return self.config_store.data

    def get_template_path(self, template_name):
        custom_name = self.config.get("custom_templates", {}).get(template_name)
        if custom_name:
            custom_path = PROJECT_ROOT / custom_name
            if custom_path.exists():
                return str(custom_path)
            # 兼容旧配置: custom_templates 仅存文件名
            legacy_custom = TEMPLATES_DIR / Path(custom_name).name
            if legacy_custom.exists():
                return str(legacy_custom)
        default_path = TEMPLATES_DIR / f"{template_name}.png"
        if default_path.exists():
            return str(default_path)
        # 兜底兼容旧目录
        return str(PROJECT_ROOT / f"{template_name}.png")

    def _match_with_template(self, img_gray, template, ac, offset_x=0, offset_y=0):
        """
        对给定模板执行多尺度匹配，返回中心点和最大匹配值。
        该函数只负责“计算”，不负责模板来源和回退策略。
        """
        best_val = -1.0
        best_center = None
        left, top, _, _ = self.window_service.get_window_region()

        for scale in self.config.get("template_match", {}).get("scales", [1.0]):
            x, y = template.shape[0:2]
            scaled = cv2.resize(template, (max(1, int(y * scale)), max(1, int(x * scale))))
            if scaled.shape[0] > img_gray.shape[0] or scaled.shape[1] > img_gray.shape[1]:
                continue
            res = cv2.matchTemplate(img_gray, scaled, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_val:
                w, h = scaled.shape[::-1]
                best_val = max_val
                best_center = (left + offset_x + max_loc[0] + w // 2, top + offset_y + max_loc[1] + h // 2)

        if best_val < ac:
            return None, best_val
        return best_center, best_val

    def _load_template_gray_cached(self, template_path):
        """
        按文件修改时间缓存模板灰度图：
        - 路径未命中：首次读取并缓存
        - 文件被重新标定覆盖后：mtime 变化，自动失效重载
        这样可以在不牺牲“热更新模板”的前提下，显著减少 IO 与解码开销。
        """
        p = Path(template_path)
        if not p.exists():
            return None

        mtime = p.stat().st_mtime
        cached = self._template_cache.get(str(p))
        if cached and cached[0] == mtime:
            return cached[1]

        img = cv2.imread(str(p), 0)
        if img is None:
            return None
        self._template_cache[str(p)] = (mtime, img)
        return img

    def match(self, template_name, ac=None, search_norm_rect=None, search_margin=None):
        left, top, width, height = self.window_service.get_window_region()
        template_path = self.get_template_path(template_name)
        template = self._load_template_gray_cached(template_path)
        if template is None:
            raise FileNotFoundError(f"模板不存在：{template_name}")

        if ac is None:
            ac = self.config.get("template_thresholds", {}).get(
                template_name, self.config.get("template_match", {}).get("default_threshold", 0.95)
            )

        # 如果用户已标定该模板区域（或调用方指定 search_norm_rect），则仅在“区域 + 冗余边距”做局部截图匹配。
        template_regions = self.config.get("template_regions", {})
        region_norm = search_norm_rect
        if region_norm is None and template_name in template_regions:
            region_norm = template_regions[template_name]
        if region_norm is not None:
            nx1, ny1, nx2, ny2 = region_norm
            margin = int(
                self.config.get("template_search_margin", 40)
                if search_margin is None
                else search_margin
            )
            x1 = int(nx1 * width)
            y1 = int(ny1 * height)
            x2 = int(nx2 * width)
            y2 = int(ny2 * height)
            x1, x2 = sorted((max(0, x1), min(width, x2)))
            y1, y2 = sorted((max(0, y1), min(height, y2)))
            sx1 = max(0, x1 - margin)
            sy1 = max(0, y1 - margin)
            sx2 = min(width, x2 + margin)
            sy2 = min(height, y2 + margin)
            if sx2 > sx1 and sy2 > sy1:
                local_img = pyautogui.screenshot(region=(left + sx1, top + sy1, sx2 - sx1, sy2 - sy1))
                local_gray = cv2.cvtColor(np.array(local_img), cv2.COLOR_RGB2GRAY)
                best_center, best_val = self._match_with_template(local_gray, template, ac, sx1, sy1)
                if best_center is not None:
                    if self.runtime_state.debug:
                        log_debug(True, C.TM001, tpl=template_name, phase="p1", val=best_val, at=best_center)
                    return best_center

                # 区域模式下仍保留“默认模板兜底”，但同样限定在局部截图内，不回退全屏。
                custom_map = self.config.get("custom_templates", {})
                if template_name in custom_map:
                    default_template_path = TEMPLATES_DIR / f"{template_name}.png"
                    if default_template_path.exists():
                        fallback_template = self._load_template_gray_cached(str(default_template_path))
                        if fallback_template is not None:
                            fallback_center, fallback_val = self._match_with_template(
                                local_gray,
                                fallback_template,
                                max(0.72, ac * 0.9),
                                sx1,
                                sy1,
                            )
                            if fallback_center is not None:
                                if self.runtime_state.debug:
                                    log_debug(True, C.TM001, tpl=template_name, phase="p2fb", val=fallback_val, at=fallback_center)
                                return fallback_center

                if self.runtime_state.debug:
                    log_debug(True, C.TM001, tpl=template_name, phase="reg_fail", val=best_val)
                return None

        # 未标定区域时，才回退到全窗口截图匹配（兼容首次使用）。
        img = pyautogui.screenshot(region=(left, top, width, height))
        img_gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
        global_center, global_val = self._match_with_template(img_gray, template, max(0.75, ac * 0.95), 0, 0)
        if global_center is not None:
            if self.runtime_state.debug:
                log_debug(True, C.TM001, tpl=template_name, phase="global", val=global_val, at=global_center)
            return global_center

        # 如果用了 custom_ 模板仍失败，再用原始模板兜底（未标定区域场景）。
        custom_map = self.config.get("custom_templates", {})
        if template_name in custom_map:
            default_template_path = TEMPLATES_DIR / f"{template_name}.png"
            if default_template_path.exists():
                fallback_template = self._load_template_gray_cached(str(default_template_path))
                if fallback_template is not None:
                    fallback_center, fallback_val = self._match_with_template(
                        img_gray,
                        fallback_template,
                        max(0.72, ac * 0.9),
                        0,
                        0,
                    )
                    if fallback_center is not None:
                        if self.runtime_state.debug:
                            log_debug(True, C.TM001, tpl=template_name, phase="fb_def", val=fallback_val, at=fallback_center)
                        return fallback_center

        if self.runtime_state.debug:
            log_debug(True, C.TM001, tpl=template_name, phase="fail", val=global_val)
        return None

    def capture_screen(self):
        return cv2.cvtColor(
            np.array(pyautogui.screenshot(region=self.window_service.get_window_region())),
            cv2.COLOR_RGB2BGR,
        )

    def _build_ui_blue_mask_bgr(self, bgr_img):
        """
        游戏 UI 常见高亮蓝/青填充：宽松 HSV，用于赞池圆环内「蓝色占比」统计。
        """
        if bgr_img is None or bgr_img.size == 0:
            return None
        hsv = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2HSV)
        return cv2.inRange(hsv, np.array([85, 45, 40]), np.array([135, 255, 255]))

    def _build_red_mask_bgr(self, bgr_img):
        """
        与 GameActions._build_red_mask 同口径：红色跨 H=0/179，双区间合并。
        女/男快感条在游戏内均为「红色填充」，用固定红掩膜比「整图 HSV 中值 + 容差」更稳：
        标定截图里若混入灰底、描边或邻近 UI，sample_hsv_profile 的中值易偏离真红，导致两掩膜系统性偏一侧。
        """
        if bgr_img is None or bgr_img.size == 0:
            return None
        hsv = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2HSV)
        mask1 = cv2.inRange(hsv, np.array([0, 55, 50]), np.array([12, 255, 255]))
        mask2 = cv2.inRange(hsv, np.array([168, 55, 50]), np.array([179, 255, 255]))
        return cv2.bitwise_or(mask1, mask2)

    def _like_pool_capture_crop(self):
        """截取赞池标定外接矩形；返回 (crop_bgr, ox, oy, ow, oh)。"""
        rects = self.config.get("calibration_rects", {})
        outer = rects.get("like_pool")
        if not isinstance(outer, (list, tuple)) or len(outer) != 4:
            return None
        left, top, w, h = self.window_service.get_window_region()
        nx1, ny1, nx2, ny2 = outer
        ox1 = int(min(nx1, nx2) * w)
        oy1 = int(min(ny1, ny2) * h)
        ox2 = int(max(nx1, nx2) * w)
        oy2 = int(max(ny1, ny2) * h)
        ox1, ox2 = max(0, ox1), min(w, ox2)
        oy1, oy2 = max(0, oy1), min(h, oy2)
        ow, oh = ox2 - ox1, oy2 - oy1
        if ow < 4 or oh < 4:
            return None
        try:
            shot = pyautogui.screenshot(region=(int(left + ox1), int(top + oy1), int(ow), int(oh)))
        except Exception:
            return None
        crop = cv2.cvtColor(np.asarray(shot), cv2.COLOR_RGB2BGR)
        if crop.size == 0:
            return None
        return crop, ox1, oy1, ow, oh

    def _like_pool_center_from_heart(self, crop, cx, cy, r_in):
        """从内圆区域的红色心形估计圆心（crop 内坐标）。"""
        oh, ow = crop.shape[:2]
        yy, xx = np.ogrid[0:oh, 0:ow]
        dist2 = (xx.astype(np.float64) - cx) ** 2 + (yy.astype(np.float64) - cy) ** 2
        inner = dist2 <= (float(r_in) * 0.92) ** 2
        red = self._build_red_mask_bgr(crop)
        if red is None:
            return None
        mask = (red > 0) & inner
        if int(np.count_nonzero(mask)) < 8:
            return None
        ys, xs = np.where(mask)
        return float(np.mean(xs)), float(np.mean(ys))

    def _like_pool_resolve_center(self, crop, ox, oy, ow, oh, r_in):
        """
        解析赞池圆心（crop 内坐标）：
        - 已学习则使用窗口归一化圆心；
        - 否则尝试从红心定位并学习；失败则退回外接矩形中心。
        """
        _, _, win_w, win_h = self.window_service.get_window_region()
        if self._learned_like_pool_center_norm is not None:
            nx, ny = self._learned_like_pool_center_norm
            return float(nx * win_w - ox), float(ny * win_h - oy)
        heart = self._like_pool_center_from_heart(crop, ow * 0.5, oh * 0.5, r_in)
        if heart is not None:
            hcx, hcy = heart
            self._learned_like_pool_center_norm = [
                (ox + hcx) / float(win_w),
                (oy + hcy) / float(win_h),
            ]
            log(C.LP001, kind="center", center=self._learned_like_pool_center_norm)
            return hcx, hcy
        return float(ow) * 0.5, float(oh) * 0.5

    def _like_pool_segment_fill_ratio(self, crop, cx, cy, r_in, r_out, n_segments=LIKE_POOL_SEGMENT_COUNT):
        """
        按充能格计数：每格为圆环上的一段扇区，格内蓝色占比超过阈值则计为已充能。
        分隔线为深色，不会误判为蓝色；总占比 = 已充能格数 / 总格数。
        """
        oh, ow = crop.shape[:2]
        yy, xx = np.ogrid[0:oh, 0:ow]
        dx = xx.astype(np.float64) - cx
        dy = yy.astype(np.float64) - cy
        dist2 = dx * dx + dy * dy
        ring = (dist2 > float(r_in) * float(r_in)) & (dist2 <= float(r_out) * float(r_out) + 1e-9)
        blue = self._build_ui_blue_mask_bgr(crop)
        if blue is None:
            return 0.0
        blue_on = blue > 0
        # 0 在顶部，顺时针递增（与常见环形充能 UI 一致）。
        angle = (np.arctan2(dx, -dy) + 2.0 * np.pi) % (2.0 * np.pi)
        seg = max(4, int(n_segments))
        seg_angle = 2.0 * np.pi / float(seg)
        filled = 0
        for i in range(seg):
            a0 = i * seg_angle
            a1 = (i + 1) * seg_angle
            sector = ring & (angle >= a0) & (angle < a1)
            area = int(np.count_nonzero(sector))
            if area <= 0:
                continue
            blue_cnt = int(np.count_nonzero(blue_on & sector))
            if (blue_cnt / float(area)) >= LIKE_POOL_SEGMENT_BLUE_MIN:
                filled += 1
        return float(filled) / float(seg)

    def like_pool_blue_fill_ratio(self):
        """
        赞池圆环充能占比（约 0~1）：
        - 外接矩形与环宽比例沿用标定；
        - 圆心可在首次识别到红心或充能后动态学习；
        - 按充能格计数（默认 10 格），分隔线不影响统计。
        """
        if not bool(self.config.get("calibration_done", {}).get("like_pool", False)):
            return None
        captured = self._like_pool_capture_crop()
        if captured is None:
            return None
        crop, ox, oy, ow, oh = captured
        rw = float(self.config.get("like_pool_ring_width_ratio", 0.14))
        rw = max(0.05, min(0.45, rw))
        geo = like_pool_annulus_radii(ow, oh, rw)
        if geo is None:
            return None
        _cx0, _cy0, r_out, r_in = geo
        cx, cy = self._like_pool_resolve_center(crop, ox, oy, ow, oh, r_in)
        ratio = self._like_pool_segment_fill_ratio(crop, cx, cy, r_in, r_out)
        if self.runtime_state.debug:
            log_debug(
                True,
                C.LP001,
                kind="ratio",
                ratio=ratio,
                seg=LIKE_POOL_SEGMENT_COUNT,
                learned=self._learned_like_pool_center_norm is not None,
            )
        return ratio

    def _progress_bar_calibrated(self, bar_slot):
        calib_key = _BAR_SLOT_CALIB_KEY.get(bar_slot, bar_slot)
        return bool(self.config.get("calibration_done", {}).get(calib_key, False))

    def _default_progress_bar_norm(self, bar_slot):
        calib_key = _BAR_SLOT_CALIB_KEY.get(bar_slot, bar_slot)
        cr = self.config.get("calibration_rects", {})
        norm = cr.get(calib_key)
        if isinstance(norm, list) and len(norm) == 4:
            return norm
        br = self.config.get("bar_regions", {})
        norm = br.get(bar_slot)
        if isinstance(norm, list) and len(norm) == 4:
            return norm
        return None

    def _get_progress_bar_window_rect(self, bar_slot):
        """返回窗口内像素矩形 (x1,y1,x2,y2)；始终使用用户标定区域。"""
        norm = self._default_progress_bar_norm(bar_slot)
        if norm is None:
            return None
        return self.window_service.denormalize_region(norm)

    def _build_blue_mask_bgr(self, bgr_img):
        """敏感进度条：蓝色填充掩膜（略放宽 HSV，覆盖抗锯齿与浅青边）。"""
        if bgr_img is None or bgr_img.size == 0:
            return None
        hsv = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2HSV)
        return cv2.inRange(hsv, np.array([90, 45, 45]), np.array([140, 255, 255]))

    def _fill_mask_clean_binary(self, bgr_img, color):
        if color == "blue":
            mask = self._build_blue_mask_bgr(bgr_img)
        else:
            mask = self._build_red_mask_bgr(bgr_img)
        if mask is None:
            return None
        bin_mask = (mask > 0).astype(np.uint8)
        if bin_mask.max() == 0:
            return None
        h, w = bin_mask.shape[:2]
        # 薄条带：仅闭运算连接断裂，避免 3×3 开运算吃掉有效像素。
        if h <= 12:
            kernel = np.ones((1, 3), np.uint8)
            clean = cv2.morphologyEx(bin_mask, cv2.MORPH_CLOSE, kernel)
        else:
            kernel = np.ones((3, 3), np.uint8)
            clean = cv2.morphologyEx(bin_mask, cv2.MORPH_OPEN, kernel)
            clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, kernel)
        if clean.max() == 0:
            clean = bin_mask
        return clean

    def _bar_fill_score_from_bgr(self, bgr_img, color="red"):
        clean = self._fill_mask_clean_binary(bgr_img, color)
        if clean is None:
            return 0.0
        return self._bar_fill_score(clean)

    def _crop_progress_bar_from_image(self, image, bar_slot):
        """从整窗 BGR 图裁切进度条；返回 (crop, origin_x, origin_y)。"""
        rect = self._get_progress_bar_window_rect(bar_slot)
        if rect is None:
            return None, 0, 0
        x1, y1, x2, y2 = rect
        if y2 <= y1 or x2 <= x1:
            return None, x1, y1
        if image is None or image.size == 0:
            return None, x1, y1
        ih, iw = image.shape[:2]
        cx1, cy1 = max(0, x1), max(0, y1)
        cx2, cy2 = min(iw, x2), min(ih, y2)
        if cy2 <= cy1 or cx2 <= cx1:
            return None, cx1, cy1
        return image[cy1:cy2, cx1:cx2], cx1, cy1

    def _capture_progress_bar_bgr(self, bar_slot):
        """直接截取游戏窗口内的进度条区域。"""
        rect = self._get_progress_bar_window_rect(bar_slot)
        if rect is None:
            return None, 0, 0
        x1, y1, x2, y2 = rect
        if x2 <= x1 or y2 <= y1:
            return None, x1, y1
        left, top, _, _ = self.window_service.get_window_region()
        shot = pyautogui.screenshot(region=(left + x1, top + y1, x2 - x1, y2 - y1))
        return cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR), x1, y1

    def get_sensitive_progress_bar_ratio(self):
        """敏感进度条蓝色填充占比；未标定返回 None。"""
        if not self._progress_bar_calibrated("sensitive_progress_bar"):
            if self._progress_diag_due("sens_missing"):
                log(C.PB005, slot="s", reason="uncal")
            return None
        rect = self._get_progress_bar_window_rect("sensitive_progress_bar")
        crop, x, y = self._capture_progress_bar_bgr("sensitive_progress_bar")
        if crop is None or crop.size == 0:
            if self._progress_diag_due("sens_empty"):
                log(C.PB005, slot="s", reason="empty", x=x, y=y)
            return None
        score = self._bar_fill_score_from_bgr(crop, color="blue")
        if self._progress_diag_due("sens"):
            h, w = crop.shape[:2]
            log(C.PB005, slot="s", f=score, x=x, y=y, w=w, h=h, rect=rect)
        if self.runtime_state.debug:
            log_debug(True, C.PB004, slot="sensitive", fill=score)
        return score

    def detect_bars(self, image):
        """从整窗截图裁切女(bar1)、男(bar2)标定区域，估计红色填充进度。"""
        crop1, ox1, oy1 = self._crop_progress_bar_from_image(image, "bar1")
        crop2, ox2, oy2 = self._crop_progress_bar_from_image(image, "bar2")
        if crop1 is None or crop1.size == 0 or crop2 is None or crop2.size == 0:
            if self._progress_diag_due("fm_empty"):
                log(C.PB005, slot="fm", reason="empty", fy=oy1, my=oy2)
            return 0, 0
        s1 = self._bar_fill_score_from_bgr(crop1, color="red")
        s2 = self._bar_fill_score_from_bgr(crop2, color="red")
        if self._progress_diag_due("fm"):
            h1, w1 = crop1.shape[:2]
            h2, w2 = crop2.shape[:2]
            log(C.PB005, slot="fm", f=s1, m=s2, fx=ox1, fy=oy1, mx=ox2, my=oy2, w=w1, h=h1, mh=h2)
        if self.runtime_state.debug:
            log_debug(True, C.PB004, f=s1, m=s2)
        return s1, s2

    def _bar_fill_score(self, mask):
        """
        进度条填充估计：
        - 长度：取最右有效列（忽略条上三角标记等造成的中间断点）；
        - 分母：从首次出现填充色的列到标定右缘（跳过左侧无填充区）。
        """
        if mask.size == 0:
            return 0.0

        area_ratio = float(np.count_nonzero(mask)) / float(mask.size)
        bin_mask = (mask > 0).astype(np.uint8)
        h, w = bin_mask.shape[:2]
        if h <= 12:
            clean = bin_mask
        else:
            kernel = np.ones((3, 3), np.uint8)
            clean = cv2.morphologyEx(bin_mask, cv2.MORPH_OPEN, kernel)

        col_thr = 0.18 if h <= 12 else 0.22
        col_ratio = np.mean(clean, axis=0)
        active_cols = np.where(col_ratio > col_thr)[0]
        if active_cols.size == 0:
            return float(np.clip(area_ratio * 0.5, 0.0, 1.0))

        leftmost = int(active_cols.min())
        rightmost = int(active_cols.max())
        track_span = max(1, w - leftmost)
        length_ratio = float(rightmost - leftmost + 1) / float(track_span)
        score = 0.72 * length_ratio + 0.28 * area_ratio
        return float(np.clip(score, 0.0, 1.0))

    def _template_file_exists(self, template_name):
        try:
            return Path(self.get_template_path(template_name)).exists()
        except Exception:
            return False

    def _home_probe_template_enabled(self, template_name):
        """已存在模板文件，或该项已完成标定（含实验选定标志等 custom 模板）。"""
        if self._template_file_exists(template_name):
            return True
        return bool(self.config.get("calibration_done", {}).get(template_name, False))

    def probe_flow_templates(self, template_names=None):
        """
        探测流程相关模板是否出现在当前画面。
        赞池不参与模板探测（主页上单独判可见性）。
        返回已匹配到的 template_name 列表。
        """
        names = template_names or HOME_PAGE_PROBE_TEMPLATES
        matched = []
        for name in names:
            if not self._home_probe_template_enabled(name):
                continue
            try:
                if self.match(name) is not None:
                    matched.append(name)
            except FileNotFoundError:
                continue
            except Exception:
                continue
        return matched

    def _like_pool_heart_red_pixels(self, crop, cx, cy, r_in):
        oh, ow = crop.shape[:2]
        yy, xx = np.ogrid[0:oh, 0:ow]
        dist2 = (xx.astype(np.float64) - cx) ** 2 + (yy.astype(np.float64) - cy) ** 2
        inner = dist2 <= (float(r_in) * 0.92) ** 2
        red = self._build_red_mask_bgr(crop)
        if red is None:
            return 0
        return int(np.count_nonzero((red > 0) & inner))

    def is_like_pool_present(self):
        """
        赞池 UI 是否出现在当前画面（主页必备，判定严于充能统计）：
        须检测到足够强的红心，且圆环结构或环内蓝色占比达标。
        """
        if not bool(self.config.get("calibration_done", {}).get("like_pool", False)):
            return False
        captured = self._like_pool_capture_crop()
        if captured is None:
            return False
        crop, _ox, _oy, ow, oh = captured
        rw = float(self.config.get("like_pool_ring_width_ratio", 0.14))
        rw = max(0.05, min(0.45, rw))
        geo = like_pool_annulus_radii(ow, oh, rw)
        if geo is None:
            return False
        _cx0, _cy0, r_out, r_in = geo
        cx, cy = float(ow) * 0.5, float(oh) * 0.5
        heart_px = self._like_pool_heart_red_pixels(crop, cx, cy, r_in)
        has_heart = heart_px >= HOME_PAGE_LIKE_POOL_HEART_MIN_PIXELS

        yy, xx = np.ogrid[0:oh, 0:ow]
        dist2 = (xx.astype(np.float64) - cx) ** 2 + (yy.astype(np.float64) - cy) ** 2
        ring = (dist2 > float(r_in) * float(r_in)) & (dist2 <= float(r_out) * float(r_out) + 1e-9)
        if not np.any(ring):
            return False
        ring_bgr = crop[ring]
        ring_area = int(np.count_nonzero(ring))
        if ring_area <= 0 or ring_bgr.size == 0:
            return False
        ring_std = float(np.std(ring_bgr))
        ring_mean = float(np.mean(ring_bgr))
        has_ring = ring_std >= HOME_PAGE_LIKE_POOL_RING_STD_MIN and ring_mean >= HOME_PAGE_LIKE_POOL_RING_MEAN_MIN

        blue = self._build_ui_blue_mask_bgr(crop)
        blue_ratio = 0.0
        if blue is not None:
            blue_ratio = float(np.count_nonzero((blue > 0) & ring)) / float(ring_area)
        has_blue = blue_ratio >= HOME_PAGE_LIKE_POOL_RING_BLUE_RATIO_MIN

        return has_heart and (has_ring or has_blue)

    def detect_home_page(self):
        """
        主页判定（先决条件优先）：
        1) 必须先 positive 检测到赞池 UI；未检测到则一律非主页，不再探测其它模板；
        2) 赞池可见后，再确认除赞池外所有流程模板均未识别 → 主页。
        返回 (is_home, matched_templates, blockers)。
        """
        if not self.is_like_pool_present():
            return False, [], ["未检测到赞池"]

        matched = self.probe_flow_templates()
        if matched:
            blockers = [HOME_PAGE_TEMPLATE_SHORT.get(k, k) for k in matched]
            return False, matched, blockers

        return True, [], []

    @staticmethod
    def format_home_page_scene_label(is_home, matched, blockers=None):
        """供 UI 展示的页面状态文案。"""
        if is_home:
            return "主页"
        blockers = list(blockers or [])
        if blockers:
            if len(blockers) <= 3:
                return f"非主页({','.join(blockers)})"
            return f"非主页({','.join(blockers[:3])}等{len(blockers)}项)"
        labels = [HOME_PAGE_TEMPLATE_SHORT.get(k, k) for k in (matched or [])]
        if len(labels) <= 3:
            return f"非主页({','.join(labels)})"
        return f"非主页({','.join(labels[:3])}等{len(labels)}项)"


def sample_hsv_profile(crop_bgr):
    """校准区域颜色采样：取 HSV 中值，增强抗噪声能力。"""
    hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV).reshape(-1, 3)
    valid = hsv[(hsv[:, 1] > 20) & (hsv[:, 2] > 20)]
    if valid.size == 0:
        valid = hsv
    center = np.median(valid, axis=0).astype(int).tolist()
    return {"hsv": center, "tol": [15, 80, 80]}
