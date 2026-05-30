import ctypes
import sys
from ctypes import wintypes
from time import monotonic, sleep

import cv2
import keyboard
import numpy as np
import pyautogui

# ---------------------------------------------------------------------------
# SendInput 底层鼠标移动（仅 Windows）
# 结构体布局必须与 WinUser.h 一致，否则 SendInput 返回 0。
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    _ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

    _INPUT_MOUSE = 0
    _MOUSEEVENTF_MOVE = 0x0001

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
            print(f"[SendInput] 失败 sent={sent}, GetLastError={err}")
            return False
        return True
else:
    def _send_relative_move(dx: int, dy: int) -> bool:
        """非 Windows 回退：用 pyautogui 模拟。"""
        pyautogui.moveRel(dx, dy)
        return True


class GameActions:
    """负责点击与滚动行为，不关心识别细节。"""

    def __init__(self, config_store, state, window_service, vision_service):
        self.config_store = config_store
        self.state = state
        self.window_service = window_service
        self.vision_service = vision_service

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
            # 必须“连续 miss”达到阈值，才算成功消失。
            if self.vision_service.match(template_name) is None:
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
        return self.vision_service.match(f"cum{self.state.cum_mode}")

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
        pyautogui.moveRel(first_step, 0, duration=0.14)
        sleep(0.06)
        pyautogui.moveRel(second_step, 0, duration=0.14)
        # 去抖：移动后再稍等，避免“刚移动就点击”造成落点不稳定。
        sleep(0.15)

    def start(self):
        def _click_once():
            # 每次重试都重新取当前模板中心，避免首次坐标过期导致“越点越偏”。
            pos = self.vision_service.match("start")
            if not pos:
                return
            pyautogui.moveTo(pos[0], pos[1])
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
            # 每次重试动态取点，兼容按钮动画抖动/轻微位移。
            pos = self.vision_service.match(key)
            if not pos:
                return
            pyautogui.moveTo(pos[0], pos[1])
            self.wait(0.12)
            # 略微拉开双击间隔，降低与游戏输入竞争导致的漏触发。
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
            # 每次重试动态取点，兼容按钮动画抖动/轻微位移。
            pos = self.vision_service.match(key)
            if not pos:
                return
            pyautogui.moveTo(pos[0], pos[1])
            self.wait(0.12)
            # 略微拉开双击间隔，降低与游戏输入竞争导致的漏触发。
            pyautogui.click(clicks=2, interval=0.08)
            self.wait(0.08)
            self._move_mouse_right_after_click(key)

        return self._click_with_disappear_retry(key, _click_once)

    def finish(self):
        def _click_once():
            # 每次重试动态取点，避免使用陈旧坐标。
            pos = self.vision_service.match("finish")
            if not pos:
                return
            pyautogui.moveTo(pos[0], pos[1])
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
        pyautogui.moveTo(int(left + (r[0] + r[2]) * width / 2), int(top + (r[1] + r[3]) * height / 2))

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
        pyautogui.moveTo(abs_point[0], abs_point[1])
        sleep(0.08)
        pyautogui.leftClick()
        sleep(0.08)
        return True

    def press_experiment_switch_hotkey(self):
        """实验切换入口热键：按下 E。"""
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
        根据当前游戏窗口截图：对 7 个身体部位六边形（与标定一致）内部求平均灰度，
        最暗的一格通常对应 UI「当前选中」。
        返回 1~7；若七格亮度差小于 min_spread（0~255）或未标定齐 7 点，返回 None。
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
            _send_relative_move(step_px, 0)
            sleep(0.02)
        if remainder > 0:
            _send_relative_move(remainder, 0)
        # 移动完成后短暂稳定，避免游戏来不及响应
        sleep(0.1)
        print(f"\n[移动视角] SendInput 向右移动 {total_dx}px（屏幕宽 {screen_w} 的 1/10）。")

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

    def move_camera_burst_deploy_check(self, duration_sec=5.0, poll_interval_sec=0.06):
        """
        【移动视角部署】在整段 duration_sec 内**同时**进行：
        - 鼠标相对屏幕**匀速、连续向右移动**（按时间积分位移，观感平滑；总行程≈_rotation_target_dx_360()，约一整圈+DPI 适配）；
        - **不间断**左键点击尝试部署（固定间隔，避免过快被系统/游戏吞掉）。

        主循环高频刷新位移（loop_tick_sec），模板检测按 poll_interval_sec 节流。
        口径与 deploy_and_check_start_recover 一致。

        返回 (start_seen, both_ready)：
        - both_ready=True：已可同时检测到开始+恢复体力；
        - start_seen=True 且 both_ready=False：曾出现开始但未同时满足恢复体力；
        - start_seen=False：整个窗口内未稳定出现开始按钮。
        """
        ws = max(0.5, float(duration_sec))
        screen_w, _ = pyautogui.size()
        rotation_target_dx = self._rotation_target_dx_360()
        # 匀速：整段窗口内平均角速度，使结束时累积位移≈一整圈标定值。
        velocity_px_per_sec = float(rotation_target_dx) / ws
        # 单次 SendInput 位移上限，拆段注入，避免单帧过大。
        max_chunk = 512
        # 主循环节拍：位移按「已过时间」追赶目标，节拍越细观感越平滑。
        loop_tick_sec = 0.006
        # 左键间隔：略大于 0 即视为「不停点」，过密可能被合并为一次。
        click_interval_sec = 0.065

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
        print(
            f"\n[移动视角部署] {ws:.0f}s 平滑匀速右移 + 连续左键：目标总位移≈{rotation_target_dx}px"
            f"（≈{velocity_px_per_sec:.1f}px/s，屏宽={screen_w}px，DPI≈{dpi_show}）。"
        )

        def _send_dx_total(dx: int) -> None:
            """将 dx 拆成多段注入并累加到 cumulative_dx。"""
            nonlocal cumulative_dx
            left = int(dx)
            while left > 0:
                chunk = min(max_chunk, left)
                _send_relative_move(chunk, 0)
                cumulative_dx += chunk
                left -= chunk

        # 首拍即可点一次；之后按 click_interval_sec 重复。
        last_click_t = loop_start - click_interval_sec
        last_vision_t = loop_start - float(poll_interval_sec)

        while monotonic() < outer_deadline:
            if self.state.stop_requested:
                print("\n[移动视角部署] 已请求停止，中断连续尝试。")
                return start_seen, False

            now = monotonic()
            elapsed = max(0.0, now - loop_start)
            # 平滑：当前时刻「应已移动」的整数像素，不超过整圈目标。
            ideal_cumulative = min(float(rotation_target_dx), velocity_px_per_sec * elapsed)
            target_int = min(rotation_target_dx, int(ideal_cumulative))
            delta = target_int - cumulative_dx
            if delta > 0:
                _send_dx_total(delta)

            if now - last_click_t >= click_interval_sec:
                pyautogui.leftClick()
                last_click_t = now

            if now - last_vision_t >= float(poll_interval_sec):
                last_vision_t = now
                has_start = bool(self.ready_to_start())
                try:
                    has_recover = self.vision_service.match("recover_stamina_button") is not None
                except Exception:
                    has_recover = False
                if has_start:
                    start_seen = True
                if has_start and has_recover:
                    print("\n[移动视角部署] 连续尝试中已成功：开始按钮与恢复体力按钮均检测到。")
                    return True, True

            sleep(loop_tick_sec)

        # 舍入误差收尾，尽量凑满整圈标定（不打断上面的提前成功返回）。
        rem = rotation_target_dx - cumulative_dx
        if rem > 0:
            _send_dx_total(rem)
        if cumulative_dx < rotation_target_dx:
            print(
                f"\n[移动视角部署] 警告：{ws:.0f}s 结束时累积位移 {cumulative_dx}px < 目标 {rotation_target_dx}px，"
                "若视角仍不足一圈可适当提高游戏鼠标灵敏度或延长 duration_sec。"
            )
        if start_seen:
            print(f"\n[移动视角部署] {ws:.0f}s 内出现过开始按钮，但恢复体力按钮未同时满足。")
            return True, False
        print(f"\n[移动视角部署] {ws:.0f}s 连续尝试结束：未检测到开始按钮。")
        return False, False

    def has_recover_stamina_button(self, timeout_sec=2.0, poll_interval_sec=0.06):
        """
        检测“恢复体力按钮”是否出现（模板匹配）：
        - 与 start/finish 同类，依赖标定后的模板图与匹配区域；
        - 在 timeout_sec 内轮询，任意一次命中即返回 True。
        """
        deadline = monotonic() + max(0.1, float(timeout_sec))
        while monotonic() < deadline:
            if self.vision_service.match("recover_stamina_button") is not None:
                print("\n[恢复体力按钮检测] 检测到恢复体力按钮。")
                return True
            sleep(max(0.01, float(poll_interval_sec)))
        print("\n[恢复体力按钮检测] 超时未检测到恢复体力按钮。")
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
        print(f"\n[部署实验] 左键尝试部署，并在 {ws:.1f}s 内同时检测开始按钮+恢复体力按钮...")
        pyautogui.leftClick()
        deadline = monotonic() + ws
        start_seen = False
        while monotonic() < deadline:
            has_start = bool(self.ready_to_start())
            has_recover = self.vision_service.match("recover_stamina_button") is not None
            if has_start:
                start_seen = True
            if has_start and has_recover:
                print("\n[部署实验] 成功：开始按钮与恢复体力按钮均检测到。")
                return True, True
            sleep(max(0.01, float(poll_interval_sec)))

        if start_seen:
            print(f"\n[部署实验] 失败：{ws:.1f}s 内出现过开始按钮，但恢复体力按钮未出现。")
            return True, False
        print(f"\n[部署实验] 失败：{ws:.1f}s 内未出现开始按钮。")
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
        return cv2.inRange(hsv, np.array([95, 55, 50]), np.array([135, 255, 255]))

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
        crop = self._capture_calibration_region_bgr("sensitive_progress_bar")
        if crop is None:
            return None
        # 按最新规则：敏感进度条颜色为蓝色。
        return self._blue_fill_ratio(crop)

    def is_special_action_button_present(self):
        """
        是否存在特殊动作按钮（仅模板匹配，不判颜色）：
        - 用于「按钮已出现在画面上」类逻辑（开局等待、消失/再出现语义）；
        - 与 is_special_action_button_red（可触发态）区分。
        """
        try:
            return self.vision_service.match("special_action_button") is not None
        except FileNotFoundError:
            return False
        except Exception:
            return False

    def is_special_action_button_red(self, threshold=0.60):
        """
        判断特殊动作按钮是否处于「可触发」的红色态（区域内红色占比 >= threshold）。
        仅用于触发主键盘「1」的条件，不用于判断按钮是否在画面上存在。
        """
        crop = self._capture_calibration_region_bgr("special_action_button")
        if crop is None:
            return False
        ratio = self._red_ratio(crop)
        return ratio >= float(threshold)

    def press_main_keyboard_one_after_delay(self, delay_sec=0.5, abort_check=None):
        """
        延迟后触发主键盘“1”（非小键盘）：
        - 使用 keyboard.press_and_release("1")；
        - 用于替代“点击特殊动作按钮中心”的旧逻辑。
        - abort_check：可选，返回 True 表示应取消本次按键（阶段已结束/已暂停等）。
          延迟期间分段睡眠并轮询，避免固定 sleep 结束后再退出导致误触。
        """
        total = max(0.0, float(delay_sec))
        deadline = monotonic() + total
        while monotonic() < deadline:
            if abort_check and abort_check():
                return False
            sleep(0.02)
        if abort_check and abort_check():
            return False
        try:
            keyboard.press_and_release("1")
            print("\n[特殊动作] 已触发主键盘“1”。")
            return True
        except Exception as exc:
            print(f"\n[特殊动作] 触发主键盘“1”失败：{exc}")
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
            print("\n[拉出新实验滚动] 重播跳过：向下滚动距离为0，请先完成该标定。")
            return False
        try:
            sleep(max(0.0, float(delay_sec)))
            x, y = self.window_service.denormalize_point([ax, ay])
            pyautogui.moveTo(x, y)
            # 按需求固定“向下滚动”。
            # 新比例：1档=10滚轮单位，支持小数档位（如 8.5 -> 85 单位）。
            total_units = max(0, int(round(distance * 10.0)))
            full_steps = total_units // 10
            remain_units = total_units % 10
            for _ in range(full_steps):
                pyautogui.scroll(-10)
                # 提速 2 倍：每步滚动间隔由 10ms 降到 5ms。
                sleep(0.005)
            if remain_units > 0:
                pyautogui.scroll(-remain_units)
            print(
                f"\n[拉出新实验滚动] 已重播：x={ax:.3f}, y={ay:.3f}, "
                f"direction=down, distance_down={distance:g}"
            )
            return True
        except Exception as exc:
            print(f"\n[拉出新实验滚动] 重播失败：{exc}")
            return False

    def deploy_experiment_with_retry(self, wait_start_sec=2.0):
        """
        尝试部署实验（对齐流程.md 第4条）：
        左键点击一次，在 wait_start_sec 内出现开始按钮 → 成功；否则由上层切换下一实验。
        女进度条存在检查由上层（automation.py）负责。
        """
        ws = max(1.0, float(wait_start_sec))
        print(f"\n[部署实验] 左键尝试部署，等待开始按钮（最长 {ws:.1f}s）...")
        pyautogui.leftClick()
        if self.wait_start_button(timeout_sec=ws):
            print("\n[部署实验] 成功：检测到开始按钮。")
            return True
        print(f"\n[部署实验] 失败：{ws:.1f}s 内未出现开始按钮，切换下一个实验。")
        return False

    def _click_with_interval(self, x, y, count, interval_sec):
        """
        在固定坐标执行重复点击，并在两次点击之间保持给定间隔。
        该方法用于“点赞用户按钮”的节奏控制，避免点击过快导致漏触发。
        """
        pyautogui.moveTo(x, y)
        # 光标落点后稍等一拍，再点击，降低“移动与点击竞争”导致的漏点。
        sleep(0.08)
        for i in range(count):
            pyautogui.leftClick()
            # 末次点击后不需要再等待，避免引入额外尾部延迟。
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
