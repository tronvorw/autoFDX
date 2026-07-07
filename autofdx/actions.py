import ctypes
import sys
from ctypes import wintypes
from time import monotonic, sleep

import cv2
import keyboard
import numpy as np
import pyautogui

from . import log_codes as C
from .run_log import log, log_debug

# ---------------------------------------------------------------------------
# SendInput 底层鼠标移动（仅 Windows）
# 结构体布局必须与 WinUser.h 一致，否则 SendInput 返回 0。
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    _ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

    _INPUT_MOUSE = 0
    _MOUSEEVENTF_MOVE = 0x0001
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004

    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = (
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        )

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = (
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        )

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = (
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        )

    class _INPUT_UNION(ctypes.Union):
        _fields_ = (
            ("mi", _MOUSEINPUT),
            ("ki", _KEYBDINPUT),
            ("hi", _HARDWAREINPUT),
        )

    class _INPUT(ctypes.Structure):
        _fields_ = (
            ("type", wintypes.DWORD),
            ("u", _INPUT_UNION),
        )

    _INPUT_SIZE = ctypes.sizeof(_INPUT)
    _user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
    _user32.SendInput.restype = wintypes.UINT

    def _send_relative_move(dx: int, dy: int) -> bool:
        """通过 SendInput 注入一次相对鼠标移动事件。"""
        inp = _INPUT()
        inp.type = _INPUT_MOUSE
        inp.u.mi = _MOUSEINPUT(int(dx), int(dy), 0, _MOUSEEVENTF_MOVE, 0, _ULONG_PTR(0))
        sent = _user32.SendInput(1, ctypes.byref(inp), _INPUT_SIZE)
        if sent != 1:
            err = ctypes.get_last_error()
            log(C.SYS003, sent=sent, err=err)
            return False
        return True

    def _send_left_click() -> None:
        """SendInput 左键按下/抬起，比 pyautogui 更适合高频连点。"""
        for flag in (_MOUSEEVENTF_LEFTDOWN, _MOUSEEVENTF_LEFTUP):
            inp = _INPUT()
            inp.type = _INPUT_MOUSE
            inp.u.mi = _MOUSEINPUT(0, 0, 0, flag, 0, _ULONG_PTR(0))
            sent = _user32.SendInput(1, ctypes.byref(inp), _INPUT_SIZE)
            if sent != 1:
                err = ctypes.get_last_error()
                log(C.SYS003, sent=sent, err=err, kind="click")
                return
else:
    def _send_relative_move(dx: int, dy: int) -> bool:
        """非 Windows 回退：用 pyautogui 模拟。"""
        pyautogui.moveRel(dx, dy)
        return True

    def _send_left_click() -> None:
        pyautogui.leftClick()


class GameActions:
    """负责点击与滚动行为，不关心识别细节。"""

    def __init__(
        self,
        config_store,
        state,
        window_service,
        vision_service,
    ):
        self.config_store = config_store
        self.state = state
        self.window_service = window_service
        self.vision_service = vision_service
        self._last_special_action_red_info = {}

    def _inp(self):
        return None

    def _move_rel(self, dx: int, dy: int) -> bool:
        return _send_relative_move(dx, dy)

    def _left_click_sendinput(self) -> None:
        _send_left_click()

    def reset_dynamic_learned_regions(self):
        """清空赞池圆心等动态区域（特殊动作判红始终用用户标定框）。"""
        self.vision_service.reset_learned_progress_bar_regions()

    @property
    def config(self):
        return self.config_store.data

    def wait(self, sec):
        sleep(sec / 2)

    def _wait_template_disappear(
        self,
        template_name,
        timeout_sec=0.55,
        poll_interval_sec=0.05,
        stable_miss_required=3,
        settle_delay_sec=0.06,
    ):
        """
        轮询等待模板消失（去抖成功判定）：
        - 点击后不立即认定成功，必须观察到按钮模板“消失”才算本次点击有效；
        - 为避免“单帧丢匹配”导致误判，会要求连续多次检测不到模板才判定成功；
        - 若在超时窗口内仍能匹配到模板，则视为本次点击失败（需要重试）。
        """
        # 点击后先给 UI 一个最小稳定时间，再做“消失”判断，降低过早采样误判。
        sleep(max(0.0, float(settle_delay_sec)))
        deadline = monotonic() + max(0.1, float(timeout_sec))
        miss_count = 0
        miss_need = max(1, int(stable_miss_required))
        while monotonic() < deadline:
            cleared = self.vision_service.match(template_name) is None
            if cleared:
                miss_count += 1
                if miss_count >= miss_need:
                    return True
            else:
                miss_count = 0
            sleep(max(0.01, float(poll_interval_sec)))
        return False

    def _click_with_disappear_retry(
        self,
        template_name,
        click_fn,
        max_retry=4,
        retry_interval_sec=0.16,
        disappear_timeout_sec=0.45,
    ):
        """
        通用“点击 + 去抖重试”流程：
        1) 执行一次点击动作；
        2) 仅当按钮模板在短时间内消失，才认定点击成功；
        3) 若未消失，则等待一小段时间后重试。
        """
        retries = max(1, int(max_retry))
        for _ in range(retries):
            click_fn()
            if self._wait_template_disappear(template_name, timeout_sec=disappear_timeout_sec):
                return True
            # 未消失则判定为点击未生效，短暂等待后再次尝试。
            sleep(max(0.05, float(retry_interval_sec)))
        return False

    def ready_to_start(self):
        return self.vision_service.match("start")

    def ready_to_cum(self):
        key = f"cum{self.state.cum_mode}"
        return self.vision_service.match(key)

    def ready_to_cum_single(self):
        """
        单高潮模式专用：检测“单高潮按钮（cum_single）”是否出现。
        """
        return self.vision_service.match("cum_single")

    def ready_to_finish(self):
        return self.vision_service.match("finish")

    def _move_mouse_right_after_click(self, key):
        """
        点击后避让鼠标，避免鼠标覆盖按钮区域影响下一帧模板匹配。
        规则：向右移动至少“标定按钮宽度的 3 倍”，并设置更大的最小位移下限。
        同时在移动前后加入短延时，降低连续动作抖动导致的误触发概率。
        """
        left, top, width, _height = self.window_service.get_window_region()
        regions = self.config.get("template_regions", {})
        region = regions.get(key)

        # cum1 通常未单独标定，兼容复用 cum2 的区域宽度估算。
        if region is None and key == "cum1":
            region = regions.get("cum2")

        if region is not None:
            template_w_px = max(1, int((region[2] - region[0]) * width))
            # 右移距离进一步加大：
            # - 至少为模板宽度 3 倍；
            # - 同时设定更高最小值，避免模板较小时位移仍偏小。
            move_x = max(220, template_w_px * 3)
        else:
            # 没有区域信息时退化为固定安全位移。
            move_x = 220

        # 去抖：移动前稍等，给上一轮点击留出稳定时间窗口。
        sleep(0.12)
        # 右移采用“分段 + 带时长”方式，避免一次瞬移过快导致游戏内视角变化不明显。
        # 这里分两段完成，同步增强可见性与稳定性。
        first_step = int(move_x * 0.65)
        second_step = max(1, move_x - first_step)
        inp = self._inp()
        if inp is not None:
            inp.move_rel(first_step, 0, duration=0.14)
            sleep(0.06)
            inp.move_rel(second_step, 0, duration=0.14)
        else:
            pyautogui.moveRel(first_step, 0, duration=0.14)
            sleep(0.06)
            pyautogui.moveRel(second_step, 0, duration=0.14)
        # 去抖：移动后再稍等，避免“刚移动就点击”造成落点不稳定。
        sleep(0.15)

    def _click_at_match(self, template_name):
        pos = self.vision_service.match(template_name)
        if not pos:
            return None
        inp = self._inp()
        if inp is not None:
            inp.move_to(pos[0], pos[1])
        else:
            pyautogui.moveTo(pos[0], pos[1])
        return pos

    def start(self):
        def _click_once():
            if self._click_at_match("start") is None:
                return
            inp = self._inp()
            if inp is not None:
                self.wait(0.12)
                inp.left_click()
                self.wait(0.14)
                inp.left_click()
            else:
                self.wait(0.12)
                pyautogui.leftClick()
                self.wait(0.14)
                pyautogui.leftClick()
            self.wait(0.08)
            self._move_mouse_right_after_click("start")

        return self._click_with_disappear_retry("start", _click_once)

    def cum(self):
        key = f"cum{self.state.cum_mode}"

        def _click_once():
            if self._click_at_match(key) is None:
                return
            inp = self._inp()
            if inp is not None:
                self.wait(0.12)
                inp.click(clicks=2, interval=0.08)
            else:
                self.wait(0.12)
                pyautogui.click(clicks=2, interval=0.08)
            self.wait(0.08)
            self._move_mouse_right_after_click(key)

        return self._click_with_disappear_retry(key, _click_once)

    def cum_single(self):
        """
        单高潮模式专用点击：
        - 仅使用 cum_single 模板；
        - 与常规高潮点击保持同样的双击节奏与去抖重试策略。
        """
        key = "cum_single"

        def _click_once():
            if self._click_at_match(key) is None:
                return
            inp = self._inp()
            if inp is not None:
                self.wait(0.12)
                inp.click(clicks=2, interval=0.08)
            else:
                self.wait(0.12)
                pyautogui.click(clicks=2, interval=0.08)
            self.wait(0.08)
            self._move_mouse_right_after_click(key)

        return self._click_with_disappear_retry(key, _click_once)

    def finish(self):
        def _click_once():
            if self._click_at_match("finish") is None:
                return
            inp = self._inp()
            if inp is not None:
                self.wait(0.12)
                inp.left_click()
                self.wait(0.14)
                inp.left_click()
            else:
                self.wait(0.12)
                pyautogui.leftClick()
                self.wait(0.14)
                pyautogui.leftClick()
            self.wait(0.08)
            self._move_mouse_right_after_click("finish")

        return self._click_with_disappear_retry("finish", _click_once)

    def move_to_scroll_region_center(self):
        r = self.config["scroll_region"]
        left, top, width, height = self.window_service.get_window_region()
        x = int(left + (r[0] + r[2]) * width / 2)
        y = int(top + (r[1] + r[3]) * height / 2)
        inp = self._inp()
        if inp is not None:
            inp.move_to(x, y)
        else:
            pyautogui.moveTo(x, y)

    def _point_by_1based_index(self, points, index_1based):
        """
        将“1-based 点位索引”转换为屏幕绝对坐标。
        返回 None 表示点位不存在（例如未标定或索引越界）。
        """
        if not isinstance(points, list):
            return None
        if (not isinstance(index_1based, int)) or index_1based < 1 or index_1based > len(points):
            return None
        point = points[index_1based - 1]
        if (not isinstance(point, list)) or len(point) != 2:
            return None
        return self.window_service.denormalize_point(point)

    def _click_point(self, abs_point):
        """
        对绝对坐标执行一次稳定左键点击。
        统一加入极短停顿，降低“移动与点击竞争”导致的漏触发。
        """
        if abs_point is None:
            return False
        inp = self._inp()
        if inp is not None:
            inp.move_to(abs_point[0], abs_point[1])
            sleep(0.08)
            inp.left_click()
        else:
            pyautogui.moveTo(abs_point[0], abs_point[1])
            sleep(0.08)
            pyautogui.leftClick()
        sleep(0.08)
        return True

    def press_experiment_switch_hotkey(self):
        """实验切换入口热键：按下 E。"""
        inp = self._inp()
        if inp is not None:
            inp.press("e")
        else:
            pyautogui.press("e")
        sleep(0.12)

    def click_body_part(self, index_1based):
        """
        点击“身体部位”网格点（单行 7 点）。
        index_1based 为 1-based 索引。
        """
        point = self._point_by_1based_index(self.config.get("body_part_points", []), index_1based)
        return self._click_point(point)

    def click_experiment_card(self, index_1based):
        """
        点击“实验卡片”网格点（3x4，共 12 点）。
        index_1based 为 1-based 索引。
        """
        point = self._point_by_1based_index(self.config.get("experiment_points", []), index_1based)
        return self._click_point(point)

    def estimate_selected_body_part_index_1based(self, min_spread=10.0):
        """
        根据当前游戏窗口截图：对 7 个身体部位六边形内部求平均灰度，
        最暗的一格通常对应 UI「当前选中」。
        """
        from .body_part_hex import detect_selected_body_part_by_darkest_hex

        parts = self.config.get("body_part_points", [])
        if len(parts) != 7:
            return None
        try:
            r = float(self.config.get("body_part_hex_radius_px", 20.0))
        except Exception:
            r = 20.0
        try:
            screen = self.vision_service.capture_screen()
        except Exception:
            return None
        if screen is None or screen.size == 0:
            return None
        h, w = screen.shape[:2]
        centers = []
        for p in parts:
            if isinstance(p, (list, tuple)) and len(p) == 2:
                centers.append((float(p[0]) * w, float(p[1]) * h))
        if len(centers) != 7:
            return None
        return detect_selected_body_part_by_darkest_hex(screen, centers, r, min_spread=float(min_spread))

    def has_experiment_selected_flag(self):
        """
        检测「实验选定标志」模板是否出现（遗留项，实验切换/bootstrap 已改用语义见 has_body_part_switch_visible）。
        """
        return self.vision_service.match("experiment_selected_flag") is not None

    def wait_experiment_selected_flag(self, timeout_sec=0.5, poll_interval_sec=0.06):
        """遗留：轮询实验选定标志是否出现。新流程请用 wait_until_body_part_switch_hidden。"""
        deadline = monotonic() + max(0.0, float(timeout_sec))
        while monotonic() < deadline:
            if self.has_experiment_selected_flag():
                return True
            sleep(max(0.01, float(poll_interval_sec)))
        return False

    def has_body_part_switch_visible(self):
        """
        身体部位条是否仍可见（依赖「身体部位」标定矩形内的模板匹配）。
        实验面板打开时通常为可见；选定实验并关闭/收起该条后视为不可见。
        无模板文件时返回 True，避免误判为「已消失」导致假阳性（最终会超时由上层处理）。
        """
        try:
            return self.vision_service.match("body_part_switch") is not None
        except FileNotFoundError:
            return True

    def wait_until_body_part_switch_hidden(self, timeout_sec=3.5, poll_interval_sec=0.06):
        """
        在给定超时内轮询，直至身体部位条不再匹配到模板（视为实验已选定/界面已切换）。
        返回 True 表示在超时前已消失；False 表示超时仍未消失。
        """
        deadline = monotonic() + max(0.0, float(timeout_sec))
        while monotonic() < deadline:
            if not self.has_body_part_switch_visible():
                return True
            sleep(max(0.01, float(poll_interval_sec)))
        return False

    def wait_start_button(self, timeout_sec=1.0, poll_interval_sec=0.08):
        """
        在给定超时时间内轮询“开始按钮”是否出现。
        返回 True 表示出现；False 表示超时未出现。
        """
        # 最短等待窗口不低于 1s；实验部署阶段可传入更长超时（由上层决定）。
        timeout = max(1.0, float(timeout_sec))
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            if self.ready_to_start():
                return True
            sleep(max(0.01, float(poll_interval_sec)))
        return False

    def move_camera_right_sendinput(self):
        """
        流程.md 第5条【移动视角部署】：
        使用 SendInput 底层输入让鼠标向右移动屏幕横向分辨率 1/10 的距离。
        分多小步执行以提高游戏对输入事件的识别率。
        """
        screen_w, _ = pyautogui.size()
        total_dx = screen_w // 10
        step_px = 80
        steps = total_dx // step_px
        remainder = total_dx % step_px
        for _ in range(steps):
            self._move_rel(step_px, 0)
            sleep(0.02)
        if remainder > 0:
            self._move_rel(remainder, 0)
        # 移动完成后短暂稳定，避免游戏来不及响应
        sleep(0.1)
        log(C.MV001, dx=total_dx, screen_w=screen_w)

    def _rotation_target_dx_360(self):
        """
        估算「约一整圈水平视角」所需的累积相对位移（SendInput 像素，与 pyautogui 逻辑坐标一致）。

        说明：
        - 游戏内灵敏度未知，这里用「逻辑屏宽」与「系统 DPI」做下限标定，使不同 Windows 显示缩放下
          总行程接近「在 96 DPI 设计下拖过一整屏宽度」的量级，避免高分屏/缩放只移了半圈。
        - 公式：max(屏宽, 屏宽 × DPI/96)，高 DPI 时逻辑宽度往往变小，乘系数补足。
        """
        screen_w, _ = pyautogui.size()
        dpi = 96
        if sys.platform == "win32":
            try:
                dpi = int(ctypes.windll.user32.GetDpiForSystem())
            except Exception:
                dpi = 96
        dpi = max(72, min(384, dpi))
        return max(int(screen_w), int(screen_w * (dpi / 96.0)))

    def _deploy_move_settings(self, duration_sec=None):
        cfg = self.config
        if duration_sec is None:
            try:
                duration_sec = float(cfg.get("deploy_move_duration_sec", 10.0))
            except Exception:
                duration_sec = 10.0
        try:
            click_iv = float(cfg.get("deploy_move_click_interval_sec", 0.036))
        except Exception:
            click_iv = 0.036
        try:
            loop_tick = float(cfg.get("deploy_move_loop_tick_sec", 0.004))
        except Exception:
            loop_tick = 0.004
        try:
            max_step = int(cfg.get("deploy_move_max_step_px", 10))
        except Exception:
            max_step = 10
        try:
            poll_iv = float(cfg.get("deploy_move_poll_interval_sec", 0.08))
        except Exception:
            poll_iv = 0.08
        try:
            speed_mul = float(cfg.get("deploy_move_speed_multiplier", 2.0))
        except Exception:
            speed_mul = 2.0
        return {
            "duration_sec": max(1.0, float(duration_sec)),
            "click_interval_sec": max(0.02, click_iv),
            "loop_tick_sec": max(0.002, loop_tick),
            "max_step_px": max(1, min(32, max_step)),
            "poll_interval_sec": max(0.04, poll_iv),
            "speed_multiplier": max(1.0, min(4.0, speed_mul)),
        }

    def move_camera_burst_deploy_check(self, duration_sec=None, poll_interval_sec=None):
        """
        【移动视角部署】在整段窗口内**同时**进行：
        - 鼠标相对屏幕匀速、**微步连续**向右移动（按时间积分，每拍最多移动 max_step_px，观感接近 360° 平滑转圈）；
        - 高频 SendInput 左键连点尝试部署。

        返回 (start_seen, both_ready)，口径与 deploy_and_check_start_recover 一致。
        """
        move_cfg = self._deploy_move_settings(duration_sec)
        ws = move_cfg["duration_sec"]
        if poll_interval_sec is not None:
            poll_interval_sec = max(0.04, float(poll_interval_sec))
        else:
            poll_interval_sec = move_cfg["poll_interval_sec"]
        loop_tick_sec = move_cfg["loop_tick_sec"]
        click_interval_sec = move_cfg["click_interval_sec"]
        max_step_px = move_cfg["max_step_px"]
        speed_multiplier = move_cfg["speed_multiplier"]

        screen_w, _ = pyautogui.size()
        rotation_target_dx = self._rotation_target_dx_360()
        velocity_px_per_sec = (float(rotation_target_dx) / ws) * speed_multiplier

        outer_deadline = monotonic() + ws
        loop_start = monotonic()
        cumulative_dx = 0
        start_seen = False
        dpi_show = 96
        if sys.platform == "win32":
            try:
                dpi_show = int(ctypes.windll.user32.GetDpiForSystem())
            except Exception:
                dpi_show = 96
        log(
            C.MV002,
            ws=ws,
            target_dx=rotation_target_dx,
            v=velocity_px_per_sec,
            screen_w=screen_w,
            dpi=dpi_show,
            step=max_step_px,
            click_iv=click_interval_sec,
            speed=speed_multiplier,
        )

        def _send_dx_micro(dx: int) -> None:
            nonlocal cumulative_dx
            left = max(0, int(dx))
            while left > 0:
                chunk = min(max_step_px, left)
                self._move_rel(chunk, 0)
                cumulative_dx += chunk
                left -= chunk

        last_click_t = loop_start - click_interval_sec
        last_vision_t = loop_start - poll_interval_sec

        while monotonic() < outer_deadline:
            if self.state.stop_requested:
                log(C.MV002, reason="stop_requested")
                return start_seen, False

            now = monotonic()
            elapsed = max(0.0, now - loop_start)
            ideal_cumulative = min(float(rotation_target_dx), velocity_px_per_sec * elapsed)
            target_int = min(rotation_target_dx, int(ideal_cumulative))
            delta = target_int - cumulative_dx
            if delta > 0:
                # 模板检测会阻塞主循环；落后时连续微步追平，避免 10s 内走不满一整圈。
                chase_budget = max(max_step_px * 8, int(velocity_px_per_sec * 0.05))
                while delta > 0 and chase_budget > 0:
                    step = min(max_step_px, delta, chase_budget)
                    _send_dx_micro(step)
                    chase_budget -= step
                    delta = target_int - cumulative_dx

            if now - last_click_t >= click_interval_sec:
                self._left_click_sendinput()
                last_click_t = now

            if now - last_vision_t >= poll_interval_sec:
                last_vision_t = now
                has_start = bool(self.ready_to_start())
                try:
                    has_recover = self.vision_service.match("recover_stamina_button") is not None
                except Exception:
                    has_recover = False
                if has_start:
                    start_seen = True
                if has_start and has_recover:
                    log(C.MV002, reason="ok_mid_try")
                    return True, True

            sleep(loop_tick_sec)

        rem = rotation_target_dx - cumulative_dx
        if rem > 0:
            _send_dx_micro(rem)
        if cumulative_dx < rotation_target_dx:
            log(C.MV002, reason="under_rotate", ws=ws, dx=cumulative_dx, target=rotation_target_dx)
        if start_seen:
            log(C.MV002, reason="start_no_recover", ws=ws)
            return True, False
        log(C.MV002, reason="no_start", ws=ws)
        return False, False

    def has_recover_stamina_button(self, timeout_sec=2.0, poll_interval_sec=0.06):
        """
        检测“恢复体力按钮”是否出现（模板匹配）：
        - 与 start/finish 同类，依赖标定后的模板图与匹配区域；
        - 在 timeout_sec 内轮询，任意一次命中即返回 True。
        """
        deadline = monotonic() + max(0.1, float(timeout_sec))
        while monotonic() < deadline:
            hit = self.vision_service.match("recover_stamina_button") is not None
            if hit:
                log(C.RS001, ok=1)
                return True
            sleep(max(0.01, float(poll_interval_sec)))
        log(C.RS001, ok=0)
        return False

    def deploy_and_check_start_recover(self, timeout_sec=2.0, poll_interval_sec=0.06):
        """
        尝试部署并在同一时间窗口内同时检测“开始按钮 + 恢复体力按钮”：
        - 先左键尝试部署；
        - 在 timeout_sec 内循环判断：
          1) 若两者同时存在 -> (start_seen=True, both_ready=True)
          2) 若只出现过开始按钮 -> (start_seen=True, both_ready=False)
          3) 若开始按钮始终未出现 -> (start_seen=False, both_ready=False)
        """
        ws = max(1.0, float(timeout_sec))
        log(C.DP001, ws=ws, mode="start_recover")
        pyautogui.leftClick()
        deadline = monotonic() + ws
        start_seen = False
        while monotonic() < deadline:
            has_start = bool(self.ready_to_start())
            has_recover = self.vision_service.match("recover_stamina_button") is not None
            if has_start:
                start_seen = True
            if has_start and has_recover:
                log(C.DP002, mode="start_recover")
                return True, True
            sleep(max(0.01, float(poll_interval_sec)))

        if start_seen:
            log(C.DP003, reason="no_recover", ws=ws)
            return True, False
        log(C.DP003, reason="no_start", ws=ws)
        return False, False

    def _capture_calibration_region_bgr(self, key):
        """
        读取某标定项区域截图（BGR）：
        - 只使用 calibration_rects 中的归一化坐标；
        - 未标定或区域非法时返回 None，调用方需做降级处理。
        """
        done_map = self.config.get("calibration_done", {})
        if not bool(done_map.get(key, False)):
            return None
        rect_map = self.config.get("calibration_rects", {})
        norm = rect_map.get(key)
        if (not isinstance(norm, list)) or len(norm) != 4:
            return None
        left, top, _, _ = self.window_service.get_window_region()
        x1, y1, x2, y2 = self.window_service.denormalize_region(norm)
        if x2 <= x1 or y2 <= y1:
            return None
        shot = pyautogui.screenshot(region=(left + x1, top + y1, x2 - x1, y2 - y1))
        return cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)

    def _capture_window_rect_bgr(self, rx1, ry1, rx2, ry2):
        """按窗口内像素矩形截屏，返回 BGR；非法区域返回 None。"""
        left, top, width, height = self.window_service.get_window_region()
        rx1 = max(0, min(width, int(rx1)))
        ry1 = max(0, min(height, int(ry1)))
        rx2 = max(0, min(width, int(rx2)))
        ry2 = max(0, min(height, int(ry2)))
        if rx2 <= rx1 or ry2 <= ry1:
            return None
        shot = pyautogui.screenshot(region=(left + rx1, top + ry1, rx2 - rx1, ry2 - ry1))
        return cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)

    def _special_action_calibration_rect(self):
        """特殊动作按钮：始终使用用户标定外接框（尺寸≈按钮），返回 (rx1, ry1, rx2, ry2)。"""
        key = "special_action_button"
        if not bool(self.config.get("calibration_done", {}).get(key, False)):
            return None
        norm = self.config.get("calibration_rects", {}).get(key)
        if (not isinstance(norm, list)) or len(norm) != 4:
            return None
        x1, y1, x2, y2 = self.window_service.denormalize_region(norm)
        if x2 <= x1 or y2 <= y1:
            return None
        return x1, y1, x2, y2

    def _special_action_largest_red_bbox(self, clean, min_area=16):
        """在掩膜中取面积最大的红色连通域包围盒 (x1,y1,x2,y2)，忽略零散噪点。"""
        if clean is None or clean.max() == 0:
            return None
        n, _labels, stats, _centroids = cv2.connectedComponentsWithStats(clean, connectivity=8)
        best = None
        best_area = 0
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < min_area or area <= best_area:
                continue
            best_area = area
            x = int(stats[i, cv2.CC_STAT_LEFT])
            y = int(stats[i, cv2.CC_STAT_TOP])
            w = int(stats[i, cv2.CC_STAT_WIDTH])
            h = int(stats[i, cv2.CC_STAT_HEIGHT])
            best = (x, y, x + w - 1, y + h - 1)
        return best

    def _special_action_red_blob_large_enough(self, bbox, cal_w, cal_h, min_ratio=0.28):
        """红块宽高须达到标定框一定比例，过滤进度条边缘等细条误检。"""
        if bbox is None:
            return False
        x1, y1, x2, y2 = bbox
        bw = max(1, x2 - x1 + 1)
        bh = max(1, y2 - y1 + 1)
        return bw >= cal_w * min_ratio and bh >= cal_h * min_ratio

    def _red_mask_clean_binary(self, bgr_img):
        """红色掩膜经形态学去噪后的二值图；无红色时返回 None。"""
        mask = self._build_red_mask(bgr_img)
        if mask is None:
            return None
        bin_mask = (mask > 0).astype(np.uint8)
        if bin_mask.max() == 0:
            return None
        kernel = np.ones((3, 3), np.uint8)
        clean = cv2.morphologyEx(bin_mask, cv2.MORPH_OPEN, kernel)
        clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, kernel)
        if clean.max() == 0:
            clean = bin_mask
        return clean

    def _special_action_red_component_score(self, bgr_img):
        """
        在标定框 crop 内取「最大红色连通块」的填充占比（块内红像素/块面积）。
        """
        clean = self._red_mask_clean_binary(bgr_img)
        bbox = self._special_action_largest_red_bbox(clean)
        if bbox is None:
            return 0.0, None
        x1, y1, x2, y2 = bbox
        core = clean[y1 : y2 + 1, x1 : x2 + 1]
        if core.size == 0:
            return 0.0, bbox
        return float(np.count_nonzero(core)) / float(core.size), bbox

    def _build_red_mask(self, bgr_img):
        """
        构建红色掩码（HSV 双区间）：
        红色跨越色相 0/179，需要合并两个区间。
        """
        if bgr_img is None or bgr_img.size == 0:
            return None
        hsv = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2HSV)
        # 适当放宽 S/V 下限，提升对游戏内半透明与抗锯齿边缘的覆盖。
        mask1 = cv2.inRange(hsv, np.array([0, 55, 50]), np.array([12, 255, 255]))
        mask2 = cv2.inRange(hsv, np.array([168, 55, 50]), np.array([179, 255, 255]))
        return cv2.bitwise_or(mask1, mask2)

    def _red_ratio(self, bgr_img):
        """
        计算图像中“红色像素占比”：
        - 使用 HSV 双区间（低 H + 高 H）覆盖红色环绕；
        - 返回 [0,1] 比例，便于做阈值判断。
        """
        mask = self._build_red_mask(bgr_img)
        if mask is None:
            return 0.0
        return float(np.count_nonzero(mask)) / float(mask.size)

    def _red_fill_ratio(self, bgr_img):
        """
        估计“红色填充占比”（比纯面积占比更稳）：
        - 先做开闭运算去噪并连接断裂；
        - 结合 area_ratio 与 length_ratio（从左到右的活跃列长度）得到分数。
        """
        mask = self._build_red_mask(bgr_img)
        if mask is None:
            return 0.0
        area_ratio = float(np.count_nonzero(mask)) / float(mask.size)
        bin_mask = (mask > 0).astype(np.uint8)
        kernel = np.ones((3, 3), np.uint8)
        clean = cv2.morphologyEx(bin_mask, cv2.MORPH_OPEN, kernel)
        clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, kernel)
        col_ratio = np.mean(clean, axis=0)
        active_cols = np.where(col_ratio > 0.22)[0]
        if active_cols.size == 0:
            return float(np.clip(area_ratio * 0.5, 0.0, 1.0))
        rightmost = int(active_cols.max())
        length_ratio = float(rightmost + 1) / float(clean.shape[1])
        score = 0.72 * length_ratio + 0.28 * area_ratio
        return float(np.clip(score, 0.0, 1.0))

    def _build_blue_mask(self, bgr_img):
        """
        构建蓝色掩码（HSV）：
        用于“敏感进度条”为蓝色时的占比检测。
        """
        if bgr_img is None or bgr_img.size == 0:
            return None
        hsv = cv2.cvtColor(bgr_img, cv2.COLOR_BGR2HSV)
        # 蓝色大致在 H=[95,135]，S/V 放宽以覆盖游戏内抗锯齿和半透明边缘。
        return cv2.inRange(hsv, np.array([90, 45, 45]), np.array([140, 255, 255]))

    def _blue_fill_ratio(self, bgr_img):
        """
        估计“蓝色填充占比”（与红色同口径）：
        - 开闭运算去噪并连接断裂；
        - 融合面积占比与从左到右长度占比。
        """
        mask = self._build_blue_mask(bgr_img)
        if mask is None:
            return 0.0
        area_ratio = float(np.count_nonzero(mask)) / float(mask.size)
        bin_mask = (mask > 0).astype(np.uint8)
        kernel = np.ones((3, 3), np.uint8)
        clean = cv2.morphologyEx(bin_mask, cv2.MORPH_OPEN, kernel)
        clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, kernel)
        col_ratio = np.mean(clean, axis=0)
        active_cols = np.where(col_ratio > 0.22)[0]
        if active_cols.size == 0:
            return float(np.clip(area_ratio * 0.5, 0.0, 1.0))
        rightmost = int(active_cols.max())
        length_ratio = float(rightmost + 1) / float(clean.shape[1])
        score = 0.72 * length_ratio + 0.28 * area_ratio
        return float(np.clip(score, 0.0, 1.0))

    def get_sensitive_progress_bar_ratio(self):
        """返回“敏感进度条”填充占比；未标定时返回 None。"""
        return self.vision_service.get_sensitive_progress_bar_ratio()

    def is_special_action_button_present(self):
        """
        是否存在特殊动作按钮（仅模板匹配，不判颜色）：
        - 始终使用标定模板区域匹配；
        - 与 is_special_action_button_red（可触发态）区分。
        """
        try:
            return self.vision_service.match("special_action_button") is not None
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def _special_action_red_threshold(self):
        try:
            return float(self.config.get("special_action_red_threshold", 0.40))
        except Exception:
            return 0.40

    def _special_action_red_blob_min_ratio(self):
        try:
            return float(self.config.get("special_action_red_blob_min_ratio", 0.22))
        except Exception:
            return 0.22

    def is_special_action_button_red(self, threshold=None):
        """
        判断特殊动作按钮是否处于「可触发」的红色态。
        始终在用户标定框内判红：最大红色连通块填充率 ≥ 阈值，且块尺寸须接近按钮大小。
        """
        if threshold is None:
            threshold = self._special_action_red_threshold()
        min_ratio = self._special_action_red_blob_min_ratio()
        cal = self._special_action_calibration_rect()
        if cal is None:
            self._last_special_action_red_info = {"reason": "no_cal"}
            return False
        cal_x1, cal_y1, cal_x2, cal_y2 = cal
        cal_w = max(1, cal_x2 - cal_x1)
        cal_h = max(1, cal_y2 - cal_y1)
        crop = self._capture_window_rect_bgr(cal_x1, cal_y1, cal_x2, cal_y2)
        if crop is None:
            self._last_special_action_red_info = {"reason": "no_crop", "mode": "calibration_rect"}
            return False
        score, bbox = self._special_action_red_component_score(crop)
        fill_score = self._red_fill_ratio(crop)
        info = {
            "mode": "calibration_rect",
            "red": score,
            "fill": fill_score,
            "th": float(threshold),
            "x": cal_x1,
            "y": cal_y1,
            "w": cal_w,
            "h": cal_h,
        }
        large_enough = bbox is not None and self._special_action_red_blob_large_enough(
            bbox, cal_w, cal_h, min_ratio=min_ratio
        )
        if bbox is not None:
            x1, y1, x2, y2 = bbox
            info["bw"] = max(1, x2 - x1 + 1)
            info["bh"] = max(1, y2 - y1 + 1)

        if large_enough:
            effective = max(score, fill_score * 0.92)
            info["red"] = effective
            is_red = effective >= float(threshold)
            info["reason"] = None if is_red else "red_low"
            self._last_special_action_red_info = info
            if self.state.debug:
                log_debug(
                    self.state.debug,
                    C.SA006,
                    mode="calibration_rect",
                    fill=effective,
                    th=threshold,
                )
            return is_red

        soft_w = cal_w * min_ratio * 0.55
        soft_h = cal_h * min_ratio * 0.55
        if (
            bbox is not None
            and fill_score >= float(threshold) * 0.92
            and info.get("bw", 0) >= soft_w
            and info.get("bh", 0) >= soft_h
        ):
            info["red"] = fill_score
            info["reason"] = "red_fill"
            self._last_special_action_red_info = info
            return True

        info["reason"] = "red_small"
        info["red"] = score if bbox is not None else fill_score
        self._last_special_action_red_info = info
        return False

    def evaluate_special_action_trigger(self, sensitive_max=0.80, red_threshold=None, detail=False):
        """
        特殊动作触发条件：敏感条 < sensitive_max 且按钮判红。
        敏感条不满足时跳过判红截图，减少无效开销。
        返回 (是否触发, 敏感条占比或 None)。
        """
        sensitive_ratio = self.get_sensitive_progress_bar_ratio()
        if sensitive_ratio is None:
            if detail:
                return False, None, {"reason": "no_sens"}
            return False, None
        if sensitive_ratio >= float(sensitive_max):
            if detail:
                return False, sensitive_ratio, {"reason": "sens_high", "max": float(sensitive_max)}
            return False, sensitive_ratio
        if not self.is_special_action_button_red(threshold=red_threshold):
            if detail:
                info = dict(self._last_special_action_red_info or {})
                info.setdefault("reason", "no_red")
                return False, sensitive_ratio, info
            return False, sensitive_ratio
        if detail:
            return True, sensitive_ratio, dict(self._last_special_action_red_info or {})
        return True, sensitive_ratio

    def _special_action_key_repeat_settings(self):
        """主键盘「1」连按次数与间隔（config 可覆盖）。"""
        try:
            count = int(self.config.get("special_action_key_repeat_count", 3))
        except Exception:
            count = 3
        try:
            interval = float(self.config.get("special_action_key_repeat_interval_sec", 0.08))
        except Exception:
            interval = 0.08
        return max(1, count), max(0.04, interval)

    def press_main_keyboard_one_after_delay(self, delay_sec=0.5, abort_check=None):
        """
        延迟后连按主键盘「1」（非小键盘），默认 3 次、间隔极短去抖。
        - abort_check：返回 True 时取消；延迟与各次按键之间均会二次校验。
        """
        total = max(0.0, float(delay_sec))
        deadline = monotonic() + total
        while monotonic() < deadline:
            if abort_check and abort_check():
                return False
            sleep(0.02)
        if abort_check and abort_check():
            return False

        repeat_count, repeat_interval = self._special_action_key_repeat_settings()
        inp = self._inp()
        try:
            if inp is not None:
                return inp.press_after_delay(
                    "1",
                    0.0,
                    abort_check=abort_check,
                    repeat=repeat_count,
                    interval=repeat_interval,
                )
            for i in range(repeat_count):
                if abort_check and abort_check():
                    return False
                keyboard.press_and_release("1")
                if i < repeat_count - 1:
                    sleep(repeat_interval)
            log(C.SA004, n=repeat_count, interval=repeat_interval)
            return True
        except Exception as exc:
            log(C.SA005, err=exc)
            return False

    def replay_pull_new_experiment_scroll_action(self, delay_sec=1.0):
        """
        重播“拉出新实验滚动”标定动作：
        - 延迟 delay_sec 后执行；
        - 在标定记录的坐标处，向下滚动记录距离。
        """
        action = self.config.get("pull_new_experiment_scroll_action", {})
        ax = action.get("x", 0.5)
        ay = action.get("y", 0.5)
        # 兼容旧数据：若无 distance_down，则回退读取旧 distance。
        raw_distance = action.get("distance_down", action.get("distance", 0))
        try:
            distance = max(0.0, float(raw_distance))
        except Exception:
            distance = 0.0
        if distance <= 0.0:
            log(C.SC001, reason="distance_zero")
            return False
        try:
            sleep(max(0.0, float(delay_sec)))
            x, y = self.window_service.denormalize_point([ax, ay])
            inp = self._inp()
            if inp is not None:
                inp.move_to(x, y)
            else:
                pyautogui.moveTo(x, y)
            total_units = max(0, int(round(distance * 10.0)))
            full_steps = total_units // 10
            remain_units = total_units % 10
            for _ in range(full_steps):
                if inp is not None:
                    inp.scroll(-10, x, y)
                else:
                    pyautogui.scroll(-10)
                sleep(0.005)
            if remain_units > 0:
                if inp is not None:
                    inp.scroll(-remain_units, x, y)
                else:
                    pyautogui.scroll(-remain_units)
            log(C.SC001, x=ax, y=ay, down=distance)
            return True
        except Exception as exc:
            log(C.SC001, ok=0, err=exc)
            return False

    def deploy_experiment_with_retry(self, wait_start_sec=2.0):
        """
        尝试部署实验（对齐流程.md 第4条）：
        左键点击一次，在 wait_start_sec 内出现开始按钮 → 成功；否则由上层切换下一实验。
        女进度条存在检查由上层（automation.py）负责。
        """
        ws = max(1.0, float(wait_start_sec))
        log(C.DP001, ws=ws, mode="start_only")
        inp = self._inp()
        if inp is not None:
            inp.left_click()
        else:
            pyautogui.leftClick()
        if self.wait_start_button(timeout_sec=ws):
            log(C.DP002, mode="start_only")
            return True
        log(C.DP003, reason="no_start_switch", ws=ws)
        return False

    def _click_with_interval(self, x, y, count, interval_sec):
        """
        在固定坐标执行重复点击，并在两次点击之间保持给定间隔。
        该方法用于“点赞用户按钮”的节奏控制，避免点击过快导致漏触发。
        """
        inp = self._inp()
        if inp is not None:
            inp.move_to(x, y)
            sleep(0.08)
            for i in range(count):
                inp.left_click()
                if i < count - 1:
                    sleep(interval_sec)
        else:
            pyautogui.moveTo(x, y)
            sleep(0.08)
            for i in range(count):
                pyautogui.leftClick()
                if i < count - 1:
                    sleep(interval_sec)

    def give(self):
        points = self.config.get("like_points", [])
        # 点位顺序约定：
        # [0..2] = 用户1/2/3
        # [3..5] = 点赞用户1/2/3
        # 若点位未完整标定，直接跳过，避免点错位置。
        if len(points) < 6:
            return

        # 按业务固定流程执行三组：
        # 用户1 -> 点赞用户1，用户2 -> 点赞用户2，用户3 -> 点赞用户3
        for idx in range(3):
            user_x, user_y = self.window_service.denormalize_point(points[idx])
            like_x, like_y = self.window_service.denormalize_point(points[idx + 3])

            # 第一步：点击“用户N”一次（按你的要求去掉去抖双击）。
            self._click_with_interval(user_x, user_y, count=1, interval_sec=0.12)
            sleep(0.15)

            # 第二步：点击“点赞用户N”三次（略增到 120ms 间隔）。
            self._click_with_interval(like_x, like_y, count=3, interval_sec=0.12)
            sleep(0.15)

            # 第三步：继续点击“点赞用户N”三次（略增到 220ms 间隔）。
            self._click_with_interval(like_x, like_y, count=3, interval_sec=0.22)
            sleep(0.18)
