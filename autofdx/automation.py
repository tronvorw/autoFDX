import random
import threading
from time import monotonic, sleep, time

import keyboard
import pyautogui

from . import log_codes as C
from .run_log import log, log_debug

# 女进度条停滞：建立基线后，若连续该秒数内女条（b1）无有效增长则触发停滞（秒）。
FEMALE_BAR_STALL_NO_INCREASE_SECONDS = 5.0
# 女条填充率至少上升该比例才视为「有效增长」（过滤识别抖动）。
FEMALE_BAR_STALL_GROWTH_EPSILON = 0.012
# 武装停滞检测后、或 F1 恢复后：该秒内不因“未增长”判停滞（开局/恢复后女条常短暂不动，易误判）。
FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC = 3.5
# 按「1」后：暂停女条停滞判定的固定时长（秒）；不再无限等待按钮再出现。
FEMALE_BAR_STALL_SUSPEND_AFTER_ONE_SEC = 3.5
# 主键盘按「1」后：至少经过该秒数才允许再次判红触发；女条停滞恢复亦在此后判定。
SPECIAL_ACTION_REAPPEAR_DELAY_AFTER_ONE_SEC = 0.7
# 特殊动作监测轮询间隔（秒）：越小响应越快，但截图/匹配更频繁。
SPECIAL_ACTION_POLL_INTERVAL_SEC = 0.04
# 判红满足后、发送「1」前的短延时（秒）；保留 abort 二次校验，防误触。
SPECIAL_ACTION_PRESS_DELAY_SEC = 0.06
# 连续两次触发「1」的最小间隔（秒）；防按键洪泛，不影响首次触发。
SPECIAL_ACTION_TRIGGER_COOLDOWN_SEC = 0.55
# 按「1」后：敏感条相对触发基线至少上升该比例，才允许下一次触发（见 详细流程逻辑.md §3）。
SPECIAL_ACTION_SENSITIVE_RISE_MIN = 0.02
# 敏感条达标上升后，额外等待该秒数再允许下一次触发。
SPECIAL_ACTION_POST_RISE_DELAY_SEC = 3.0
# 敏感条填充 ≥ 该比例时不再触发特殊动作（略低于 80% 以匹配视觉满条读数）。
SPECIAL_ACTION_SENSITIVE_MAX = 0.77
SPECIAL_ACTION_SENS_EMA_ALPHA = 0.40
SPECIAL_ACTION_RISE_STALL_SEC = 12.0
SPECIAL_ACTION_PHASE_WARMUP_SEC = 1.2
# 单高潮模式：赞池蓝色占比检测最小间隔（秒），与「随时随地」轮询并存，避免高频截图。
LIKE_POOL_POLL_INTERVAL_SINGLE_CUM_SEC = 5.0
# 实验切换模式：开始按钮连续点击未确认（模板未消失）达此次数后，ESC 退出并重新部署当前实验。
START_CLICK_FAIL_RECOVERY_THRESHOLD = 10
# 高潮后等待「再来一次/结束」按钮：超过该秒数仍不出现则 ESC 直到看到开始或结束按钮。
FINISH_WAIT_TIMEOUT_SEC = 20.0
FINISH_WAIT_ESC_MAX = 30


def _sleep_interruptible(total_sec, state, step_sec=0.05):
    """
    可中断睡眠：在关闭程序时尽快跳出，避免长时间 sleep 阻塞 run_forever 退出。
    """
    total = max(0.0, float(total_sec))
    step = max(0.01, float(step_sec))
    deadline = time() + total
    while time() < deadline:
        if state.stop_requested:
            return
        remain = deadline - time()
        if remain <= 0:
            break
        sleep(min(step, remain))


class AutomationEngine:
    """自动流程主循环。"""

    def __init__(self, config_store, state, window_service, vision_service, actions):
        self.config_store = config_store
        self.state = state
        self.window_service = window_service
        self.vision_service = vision_service
        self.actions = actions
        self._f1_hotkey_handle = None
        self._f2_hotkey_handle = None
        self._f3_hotkey_handle = None
        self._f11_hotkey_handle = None
        self._f12_hotkey_handle = None
        # 滚轮副线程控制状态：
        # 主线程负责识别与点击；副线程仅根据主线程下发的滚轮指令持续滚动。
        self._scroll_stop_event = threading.Event()
        self._scroll_lock = threading.Lock()
        self._scroll_enabled = False
        self._scroll_amount = 0
        self._scroll_batch = 0
        self._scroll_thread = None
        # “实验切换”流程状态：
        # - _experiment_switch_bootstrapped: 当前实验是否已“选定+部署完成”，可直接跑主流程。
        # - _experiment_first_stage_done: 是否已完成“首次启动运行”（E/身体部位）阶段。
        # - _experiment_card_index: 当前准备点击的实验卡片索引（1-based，1~12）。
        self._experiment_switch_bootstrapped = False
        self._experiment_card_index = self._read_card_index_from_config()
        self._experiment_first_stage_done = False
        # 当前使用的身体部位点位号（仅取 2 或 5）；首次启动固定使用 2 号。
        self._current_body_part_index = 2
        # 实验切换模式下的“当前实验已运行次数”计数器：
        # 每满 5 次自动顺延到下一个实验卡片，并重新进入“尝试选定实验”。
        self._experiment_cycle_count = 0
        # 满 5 次后的延迟切换请求：
        # 置位后在“开始按钮再次出现”时，先等待 2s 再切换下一实验。
        self._switch_after_five_on_start_pending = False
        # 标记“本轮已由其他流程提前按过 E 打开面板”，
        # 供 _run_experiment_switch_bootstrap 的重入路径去重使用，避免重复按 E。
        self._experiment_panel_preopened = False
        # F2 手动切换意图：
        # True 表示“用户已按 F2，且当前已暂停；当用户恢复后，应立即调起下一实验切换”。
        # 该标记只负责“恢复后立即切换”的一次性触发，执行后会立刻清零。
        self._f2_pending_switch_after_resume = False
        # 女进度条停滞监测（流程.md 第8条）：
        # 独立线程在正常运行期间持续采样 b1，如超时窗口内始终不增加则置位标志。
        self._female_bar_stall_flag = False
        self._female_bar_monitor_active = False
        self._female_bar_monitor_stop = threading.Event()
        self._female_bar_monitor_thread = None
        # 按「1」后暂停女条停滞判定的截止时间戳（秒）。
        self._female_bar_stall_suspend_until = 0.0
        # 是否处于按「1」后的固定暂停窗口（用于暂停结束时的 SA008 与宽限期）。
        self._female_bar_stall_in_suspend = False
        # 实验切换：首次部署后 / 每轮点击「开始」后，须先模板匹配到特殊动作按钮存在，再启动女条停滞检测。
        self._female_bar_stall_wait_special_visible_after_start = False
        # 早于该时刻不判女条停滞（宽限期结束时刻）；用于开局、按1后、F1恢复后防误判。
        self._female_bar_stall_grace_until = 0.0
        # 用于检测「刚从暂停恢复」，恢复时给一段宽限期。
        self._female_bar_monitor_was_paused = False
        # 特殊动作状态机（按新规则）：
        # - 当“敏感进度条<阈值 且 特殊动作按钮红色>50%”时，短延时后连按主键盘「1」；
        # - 每次触发后暂停女条停滞检测；须等敏感条相对基线上升后再允许下一次触发。
        self._special_action_last_trigger_ts = 0.0
        self._special_action_wait_sensitive_rise = False
        self._special_action_sensitive_baseline = None
        self._special_action_sensitive_trough = None
        self._special_action_sens_ema = None
        self._special_action_rise_wait_since = 0.0
        self._special_action_phase_started_at = 0.0
        self._special_action_post_rise_until = 0.0
        self._special_action_monitor_active = False
        self._special_action_monitor_stop = threading.Event()
        self._special_action_monitor_thread = None
        # 特殊动作阶段令牌：主线程进入/离开「开始~高潮」区间时更新，防止子线程在
        # press 的 0.2s 延迟后仍发送「1」（阶段已结束或已暂停时偶发误触）。
        self._special_action_phase_token = 0
        self._special_action_expected_token = 0
        self._special_action_diag_last_ts = {}
        self._scene_poll_last_mono = 0.0
        # 降低 pyautogui 全局动作间隔，避免滚轮动作被库默认节流。
        # 调整为 0：进一步提升连续滚轮吞吐，解决“滚动距离/次数偏小”的问题。
        pyautogui.PAUSE = 0
        # 自本进程启动起的运行统计（仅内存，不落盘）：用于日志汇总。
        # - 高潮成功：每次 cum() 点击确认成功 +1。
        # - 「5回合」实验单元：实验切换开启时，当前卡已跑满 4 个完整回合后的第 5 次成功高潮 +1（与换卡逻辑一致）。
        self._runtime_total_cum_successes = 0
        self._runtime_total_five_round_experiments = 0
        # PyAutoGUI 默认 failsafe：光标停在屏幕四角时，下一次任意操作会抛异常以防失控。
        # 本脚本会把鼠标移到窗口边角安全位（如 safe_move_point）、全屏/无边框下也易贴近物理屏幕角，
        # 与用户手操或特殊动作恢复流程叠加时易误触，导致主循环异常中断（例如特殊动作日志刚打印后崩溃）。
        # 自动化场景关闭 failsafe；紧急停止仍依赖 F1 暂停与悬浮窗「退出脚本」。
        pyautogui.FAILSAFE = False
        # 单高潮·赞池轮询：上次检测时刻（monotonic）；None 表示尚未检测过。
        self._like_pool_poll_last_mono = None
        # 边沿：蓝占比先低于阈值后再涨满才触发下一轮 give，避免长时间满池时每 5 秒重复点赞。
        self._like_pool_armed_single_cum = True
        # 女条停滞恢复兜底：多次 ESC 失败后按 J/K 时，alternate 模式从这里轮换。
        self._stall_recovery_rescue_next_index = 0
        # 开始按钮点击失败恢复（仅实验切换模式）：
        # - 连续未确认次数；重新部署后首次仍失败则切下一实验。
        self._start_click_consecutive_failures = 0
        self._awaiting_first_start_after_redeploy = False
        self._start_click_recovery_marked = False
        self._runtime_start_click_recovery_count = 0

    def _inp(self):
        return self.actions._inp()

    def _press_key(self, key):
        inp = self._inp()
        if inp is not None:
            inp.press(key)
        else:
            pyautogui.press(key)

    def _get_bar_ratios(self):
        screen = self.vision_service.capture_screen()
        return self.vision_service.detect_bars(screen)

    def _poll_home_page_scene(self):
        """周期性探测是否处于主页，并写入 state.scene_label 供悬浮窗展示。"""
        now = monotonic()
        if now - self._scene_poll_last_mono < 1.0:
            return
        self._scene_poll_last_mono = now
        try:
            is_home, matched, blockers = self.vision_service.detect_home_page()
            self.state.scene_label = self.vision_service.format_home_page_scene_label(
                is_home, matched, blockers
            )
            self.state.scene_matched_templates = list(matched)
        except Exception:
            self.state.scene_label = "页面未知"

    def _special_action_should_abort(self):
        """
        若应放弃本次「按 1」，返回 True（用于延迟等待期间与按键前二次校验）。
        覆盖：已暂停、已请求停止/标定、阶段已结束、高潮按钮已出现（主循环即将退出）。
        """
        if not self._special_action_monitor_active:
            return True
        if self._special_action_phase_token != self._special_action_expected_token:
            return True
        if self.state.manual_pause:
            return True
        if self.state.stop_requested or self.state.pending_calibration is not None:
            return True
        try:
            if self.actions.ready_to_cum():
                return True
        except Exception:
            return True
        return False

    def _print_runtime_experiment_stats(self):
        """在控制台输出自程序启动以来的高潮次数与「5回合」完成个数。"""
        log(
            C.SYS002,
            cum=self._runtime_total_cum_successes,
            five=self._runtime_total_five_round_experiments,
            sr=self._runtime_start_click_recovery_count,
            exp_sw=int(bool(self.config_store.data.get("experiment_switch_enabled", False))),
        )

    def _single_cum_mode_enabled(self):
        """
        单高潮模式开关：
        True 时为无阶段顺序的模板轮询：常规高潮与单高潮同屏时优先常规高潮，再单高潮，再开始、再来一次；
        不含条带纠偏/特殊动作/女条停滞。
        """
        return bool(self.config_store.data.get("single_cum_mode_enabled", False))

    def _maybe_like_after_finish_main(self):
        """
        【主模式】loop_once：每次成功点击「再来一次」后调用一次（单高潮早退不会进入）。
        - 若 like_force_next：整轮 give() 并清除标记（F3/UI「结束后强制点赞」）；
        - 否则若赞池已标定：检测圆环蓝占比一次，≥ 阈值则 give()。
        """
        if self._single_cum_mode_enabled():
            return
        if not bool(self.config_store.data.get("like_enabled", True)):
            msg = "主模式跳过：点赞功能未开启"
            log(C.LP001, kind="skip", reason="disabled")
            self.state.log(msg)
            return

        if bool(self.config_store.data.get("like_force_next", False)):
            self.state.log("主模式：like_force_next 强制点赞")
            self.state.set_status("主模式：立即点赞（一次性）")
            log(C.LP001, kind="force", action="give")
            self.actions.give()
            self.config_store.data["like_force_next"] = False
            self.config_store.save()
            return

        if not bool(self.config_store.data.get("calibration_done", {}).get("like_pool")):
            msg = "主模式跳过：赞池未标定"
            log(C.LP001, kind="skip", reason="no_cal")
            self.state.log(msg)
            return
        pts = self.config_store.data.get("like_points", [])
        if not isinstance(pts, list) or len(pts) < 6:
            count = len(pts) if isinstance(pts, list) else 0
            msg = f"主模式跳过：点赞点位不完整（{count}/6）"
            log(C.LP001, kind="skip", reason="pts", count=count)
            self.state.log(msg)
            return

        ratio = self.vision_service.like_pool_blue_fill_ratio()
        if ratio is None:
            msg = "主模式检测失败：圆环区域无效或截图失败"
            log(C.LP001, kind="fail", reason="ratio")
            self.state.log(msg)
            return
        th = self._like_pool_full_threshold()
        msg = f"主模式检测：圆环蓝占比 {ratio:.1%}，阈值 {th:.1%}"
        log(C.LP001, kind="check", ratio=ratio, th=th)
        self.state.log(msg)
        if ratio < th:
            return

        msg = f"赞池已满：蓝占比 {ratio:.1%} ≥ 阈值 {th:.1%}，执行点赞（主模式·再来一次后）"
        log(C.LP001, kind="give", ratio=ratio, th=th)
        self.state.log(msg)
        self.state.set_status(f"赞池已满(蓝占比约{ratio:.0%})：执行点赞")
        self.actions.give()

    def _like_pool_full_threshold(self):
        """赞池满判定阈值；分段计数下建议 85%~95%，超出范围自动钳制。"""
        try:
            th = float(self.config_store.data.get("like_pool_blue_full_threshold", 0.90))
        except Exception:
            th = 0.90
        return max(0.85, min(0.95, th))

    def _poll_like_pool_single_cum(self):
        """
        单高潮专用：主循环内「随时」轮询赞池（与 finish 时序无关），两次截图检测至少相隔
        LIKE_POOL_POLL_INTERVAL_SINGLE_CUM_SEC 秒。蓝占比 ≥ 阈值时调起 give()。

        边沿：仅当「先低于阈值再涨满」时给一轮赞，避免 UI 长期满池时每 5 秒重复执行整套 give()。
        主流程 loop_once 不调用。
        """
        if not self._single_cum_mode_enabled():
            return
        if not bool(self.config_store.data.get("like_enabled", True)):
            return
        if not bool(self.config_store.data.get("calibration_done", {}).get("like_pool")):
            return
        pts = self.config_store.data.get("like_points", [])
        if not isinstance(pts, list) or len(pts) < 6:
            return

        now = monotonic()
        if self._like_pool_poll_last_mono is not None:
            if now - self._like_pool_poll_last_mono < LIKE_POOL_POLL_INTERVAL_SINGLE_CUM_SEC:
                return
        self._like_pool_poll_last_mono = now

        ratio = self.vision_service.like_pool_blue_fill_ratio()
        if ratio is None:
            return
        th = self._like_pool_full_threshold()
        if ratio < th:
            self._like_pool_armed_single_cum = True
            return
        if not self._like_pool_armed_single_cum:
            return
        self._like_pool_armed_single_cum = False

        msg = f"赞池已满：蓝占比 {ratio:.1%} ≥ 阈值 {th:.1%}，执行点赞"
        log(C.LP001, kind="give", ratio=ratio, th=th)
        self.state.log(msg)
        self.state.set_status(f"赞池已满(蓝占比约{ratio:.0%})：执行点赞")
        self.actions.give()

    def _run_single_cum_mode_once(self):
        """
        单高潮模式（轻量、无固定阶段顺序）：
        - 同一轮内循环检测可点模板；若常规高潮与单高潮同时匹配，**优先点击常规高潮**（cum），再考虑单高潮。
        - 其次「开始」、最后「再来一次」（点中再来一次则结束本轮）；点赞由主循环内赞池轮询触发，无计次点赞。
        - 不做进度条纠偏、特殊动作、女条停滞。
        - 首次检测到「开始」出现时执行一次自动补体力（与主流程相同），避免每帧重复触发。
        """
        self._scroll_enabled = False
        self._clear_scroll_command()
        self._female_bar_monitor_active = False
        self._special_action_monitor_active = False

        # 仅在本轮第一次匹配到「开始」时做补体力 + 等「开始」再出现，避免在轮询里反复执行。
        did_prefill_before_start = False
        self._like_pool_poll_last_mono = None
        self._like_pool_armed_single_cum = True

        while not self.state.stop_requested:
            if self._wait_if_paused_or_interrupted():
                return

            # 赞池：任意阶段均可检测（5 秒节流），不依赖 finish。
            self._poll_like_pool_single_cum()

            if not did_prefill_before_start and self.actions.ready_to_start():
                self._maybe_auto_refill_stamina_before_start()
                if self._wait_until_start_visible_again("单高潮模式：等待开始"):
                    return
                did_prefill_before_start = True

            if self._wait_if_paused_or_interrupted():
                return

            # 同屏多种按钮可见时的优先级：
            # 1) 常规高潮（cum2 等）与单高潮同时出现时，必须先点常规高潮（仅用 if/elif，不会先走 cum_single）；
            # 2) 再「开始」；3) 最后「再来一次」（减少误点结束）。
            if self.actions.ready_to_cum():
                clicked = self.actions.cum()
                if clicked:
                    self.state.log("单高潮模式：点击高潮")
                    self._runtime_total_cum_successes += 1
                    self._print_runtime_experiment_stats()
                else:
                    self.state.log("单高潮模式：高潮按钮点击未确认，重试")
                    sleep(0.12)
                sleep(0.1)
                continue

            if self.actions.ready_to_cum_single():
                clicked = self.actions.cum_single()
                if clicked:
                    self.state.log("单高潮模式：点击单高潮")
                    self._runtime_total_cum_successes += 1
                    self._print_runtime_experiment_stats()
                else:
                    self.state.log("单高潮模式：单高潮按钮点击未确认，重试")
                    sleep(0.12)
                sleep(0.1)
                continue

            if self.actions.ready_to_start():
                clicked = self.actions.start()
                if clicked:
                    self.state.log("单高潮模式：点击开始")
                    x, y = self.window_service.denormalize_point(self.config_store.data.get("safe_move_point", [0.95, 0.92]))
                    pyautogui.moveTo(x, y)
                else:
                    self.state.log("单高潮模式：开始按钮点击未确认，重试")
                    sleep(0.12)
                sleep(0.2)
                continue

            if self.actions.ready_to_finish():
                clicked = self.actions.finish()
                if clicked:
                    self.state.log("单高潮模式：点击再来一次")
                    return
                self.state.log("单高潮模式：再来一次按钮点击未确认，重试")
                sleep(0.12)
                continue

            self.state.log("单高潮模式：等待可匹配按钮")
            sleep(0.2)

    def _read_card_index_from_config(self):
        """
        从 current_experiment([行,列])推导实验卡片一维索引（1~12）。
        非法配置自动回退到 1。
        """
        cur = self.config_store.data.get("current_experiment", [1, 1])
        if (not isinstance(cur, list)) or len(cur) != 2:
            return 1
        row, col = cur[0], cur[1]
        if (not isinstance(row, int)) or (not isinstance(col, int)):
            return 1
        if row < 1 or row > 3 or col < 1 or col > 4:
            return 1
        return (row - 1) * 4 + col

    def _save_card_index_to_config(self, idx_1based):
        """
        将实验卡片一维索引写回 current_experiment([行,列])，便于 UI 与配置同步展示。
        """
        idx = max(1, min(12, int(idx_1based)))
        row = (idx - 1) // 4 + 1
        col = (idx - 1) % 4 + 1
        cur = self.config_store.data.get("current_experiment", [1, 1])
        if cur != [row, col]:
            self.config_store.data["current_experiment"] = [row, col]
            self.config_store.save()

    def _missing_experiment_switch_calibrations(self):
        """
        返回“实验切换流程”缺失的标定项 key 列表。
        除 calibration_done 外，也会校验网格点数量是否完整。
        """
        done_map = self.config_store.data.get("calibration_done", {})
        missing = []
        # 实验是否选定：运行时以「身体部位条是否消失」判定，不再依赖 experiment_selected_flag 模板。
        required_flags = (
            "recover_stamina_button",
            "experiment_switch",
            "body_part_switch",
        )
        for key in required_flags:
            if not bool(done_map.get(key, False)):
                missing.append(key)

        if len(self.config_store.data.get("experiment_points", [])) != 12 and "experiment_switch" not in missing:
            missing.append("experiment_switch")
        if len(self.config_store.data.get("body_part_points", [])) != 7 and "body_part_switch" not in missing:
            missing.append("body_part_switch")
        return missing

    def _ensure_experiment_switch_ready(self):
        """
        实验切换前置校验：
        开关开启时，必须先完成实验相关标定，否则强制保持暂停。
        """
        if not bool(self.config_store.data.get("experiment_switch_enabled", False)):
            return True

        missing = self._missing_experiment_switch_calibrations()
        if not missing:
            return True

        self.state.manual_pause = True
        self.state.set_status("实验切换缺少标定")
        log(C.EX001, missing=",".join(missing))
        return False

    def _retry_experiment_panel_and_click_same_card(self, card_index_1based):
        """
        当「点击实验卡片后身体部位条仍未消失」时，不一定是卡槽用尽：
        常见为界面未就绪、动画偏慢、模板瞬时未匹配。
        本函数：ESC 关闭 → 再按 E 打开面板 → 稍等后再次点击同一张卡片。
        返回 True 表示再次点击已执行（不保证身体部位条已消失）。
        """
        self._press_key("esc")
        sleep(1.0)
        self.actions.press_experiment_switch_hotkey()
        sleep(1.2)
        return bool(self.actions.click_experiment_card(card_index_1based))

    def _deploy_current_experiment_card(self):
        """
        在已选定实验卡片的前提下尝试部署（左键 + 可选移动视角）。
        返回 True 表示开始按钮与恢复体力按钮均已就绪。
        """
        sleep(1.0)
        start_seen, both_ready = self.actions.deploy_and_check_start_recover(timeout_sec=2.0)
        if both_ready:
            sleep(1.0)
            return True
        if start_seen:
            return False
        if self._wait_if_paused_or_interrupted():
            return False
        log(C.EX007)
        mv_start_seen, mv_both = self.actions.move_camera_burst_deploy_check()
        if mv_both:
            sleep(1.0)
            return True
        if mv_start_seen:
            return False
        return False

    def _redeploy_same_experiment_after_start_fail(self):
        """
        开始按钮连续点击失败后：ESC 退出 → 重开面板 → 再次选定当前卡片并部署。
        不改变 _experiment_cycle_count 与 _experiment_card_index。
        """
        card_index = self._experiment_card_index
        cycle_count = self._experiment_cycle_count
        log(C.SR001, n=START_CLICK_FAIL_RECOVERY_THRESHOLD, idx=card_index, marked=cycle_count)
        self.state.set_status("开始按钮失败：重新部署当前实验")
        self._press_key("esc")
        sleep(1.0)
        self.actions.press_experiment_switch_hotkey()
        sleep(1.0)
        sleep(1.0)
        if not self.actions.click_experiment_card(card_index):
            log(C.SR003, reason="card_click", idx=card_index)
            return False
        sel_confirm_timeout_sec = 3.5
        body_part_hidden = self.actions.wait_until_body_part_switch_hidden(timeout_sec=sel_confirm_timeout_sec)
        if not body_part_hidden:
            if not self._retry_experiment_panel_and_click_same_card(card_index):
                return False
            body_part_hidden = self.actions.wait_until_body_part_switch_hidden(timeout_sec=sel_confirm_timeout_sec)
        if not body_part_hidden:
            log(C.SR003, reason="body_part", idx=card_index)
            return False
        if not self._deploy_current_experiment_card():
            log(C.SR003, reason="buttons", idx=card_index)
            return False
        log(C.SR002, idx=card_index)
        return True

    def _switch_next_experiment_after_start_click_fail(self):
        """重新部署后首次点击仍失败：ESC 退出并顺延到下一实验卡片。"""
        log(C.EX005, reason="start_fail", idx=self._experiment_card_index + 1)
        self.state.set_status("开始按钮失败：切换下一实验")
        self._experiment_card_index += 1
        self._save_card_index_to_config(self._experiment_card_index)
        self._experiment_switch_bootstrapped = False
        self._awaiting_first_start_after_redeploy = False
        self._start_click_consecutive_failures = 0
        self._start_click_recovery_marked = False
        self.actions.reset_dynamic_learned_regions()
        self._press_key("esc")
        sleep(1.0)
        self.actions.press_experiment_switch_hotkey()
        sleep(1.0)
        self._experiment_panel_preopened = True

    def _move_mouse_to_safe_point_after_start(self):
        x, y = self.window_service.denormalize_point(self.config_store.data.get("safe_move_point", [0.95, 0.92]))
        pyautogui.moveTo(x, y)
        sleep(0.2)

    def _click_start_until_confirmed(self, experiment_switch_enabled=False, log_prefix=""):
        """
        循环点击开始按钮直至模板消失确认成功。

        实验切换模式下：
        - 连续 START_CLICK_FAIL_RECOVERY_THRESHOLD 次未确认 → ESC 并重部署当前实验（回合计数不变），记录失败标记；
        - 重部署后首次点击仍失败 → ESC 并切换下一实验。

        返回 True=成功；False=中断；\"rebootstrap\"=已切换下一实验，上层应 return 以重走 bootstrap。
        """
        while self.actions.ready_to_start():
            if self._wait_if_paused_or_interrupted():
                return False
            clicked = self.actions.start()
            if clicked:
                self._start_click_consecutive_failures = 0
                self._awaiting_first_start_after_redeploy = False
                self._start_click_recovery_marked = False
                label = f"{log_prefix}点击开始" if log_prefix else "点击开始"
                self.state.log(label.strip())
                self._move_mouse_to_safe_point_after_start()
                return True

            self._start_click_consecutive_failures += 1
            fail_label = f"{log_prefix}开始按钮点击未确认，重试" if log_prefix else "开始按钮点击未确认，重试"
            self.state.log(fail_label.strip())

            if experiment_switch_enabled:
                if self._awaiting_first_start_after_redeploy:
                    self._switch_next_experiment_after_start_click_fail()
                    return "rebootstrap"
                if self._start_click_consecutive_failures >= START_CLICK_FAIL_RECOVERY_THRESHOLD:
                    self._runtime_start_click_recovery_count += 1
                    self._start_click_recovery_marked = True
                    if not self._redeploy_same_experiment_after_start_fail():
                        log(C.SR003, reason="redeploy_fail_switch")
                        self._switch_next_experiment_after_start_click_fail()
                        return "rebootstrap"
                    self._start_click_consecutive_failures = 0
                    self._awaiting_first_start_after_redeploy = True
                    while not self.actions.ready_to_start():
                        if self._wait_if_paused_or_interrupted():
                            return False
                        sleep(0.2)
                    continue

            sleep(0.12)
        return True

    def _run_experiment_switch_bootstrap(self):
        """
        实验切换开启后的首次运行流程：
        1) 按 E -> 点身体部位 2 号（不再点击实验分类）；
        2) 尝试选定实验卡片（从当前索引开始）；
        3) 选定后尝试部署；
        4) 部署失败则切换下一实验并回到步骤 2。
        """
        if self._experiment_switch_bootstrapped:
            return True

        if not self._ensure_experiment_switch_ready():
            return False

        # “首次启动运行”阶段只执行一次；
        # 后续自动切换实验时，直接回到“尝试选定实验”阶段，不重复按 E/点身体部位。
        if not self._experiment_first_stage_done:
            self.state.set_status("首次启动运行")
            # 按需求：首次进入时从实验卡片 1 号点开始。
            self._experiment_card_index = 1
            self._save_card_index_to_config(self._experiment_card_index)
            self.actions.press_experiment_switch_hotkey()
            # 按下 E 后延时 1s，再点击身体部位（流程.md 第2条：延时1秒）。
            sleep(1.0)
            # 身体部位仅使用 2 号与 5 号；首次启动固定先点 2 号。
            if not self.actions.click_body_part(2):
                self.state.manual_pause = True
                self.state.set_status("实验切换失败: 身体部位点位不可用")
                return False
            self._experiment_first_stage_done = True
        else:
            # 非首次重入“尝试选定实验”：
            # - 若前序流程已提前按过 E（例如：5 回合切换/部署失败回退），这里不重复按；
            # - 否则按一次 E 打开实验面板。
            if self._experiment_panel_preopened:
                self._experiment_panel_preopened = False
            else:
                self.state.set_status("重新打开实验面板")
                self.actions.press_experiment_switch_hotkey()
                # 流程.md 第5条：打开实验面板后等待 1s 再进入尝试选定实验阶段。
                sleep(1.0)

        while (not self.state.stop_requested) and (self.state.pending_calibration is None):
            if self._wait_if_paused_or_interrupted():
                return False
            self.state.set_status("尝试选定实验")
            if self._experiment_card_index > 12:
                # 【实验已用尽、12张卡片已全部尝试完毕。
                if self._current_body_part_index != 5:
                    self.state.set_status("实验已用尽：切换到身体部位5号")
                    log(C.EX005, reason="exhausted_body", from_idx=self._current_body_part_index, to=5)
                    self.actions.click_body_part(2)
                    sleep(0.5)
                    self.actions.click_body_part(5)
                    self._current_body_part_index = 5
                    self._experiment_card_index = 1
                    self._save_card_index_to_config(self._experiment_card_index)
                    continue
                # 5号身体部位：12 张卡仍全部无法确认身体部位条消失，视为本流程结束。
                self.state.manual_pause = True
                self.state.set_status("全部实验已完成，程序暂停")
                log(C.EX004, reason="all_done", ws=5)
                return False

            self._save_card_index_to_config(self._experiment_card_index)
            # 流程.md 第3条：进入【尝试选定实验】后延时 1s 再点击实验卡片。
            sleep(1.0)
            log(C.EX002, idx=self._experiment_card_index)
            clicked = self.actions.click_experiment_card(self._experiment_card_index)
            if not clicked:
                self.state.manual_pause = True
                self.state.set_status("实验切换失败: 实验卡片点位不可用")
                log(C.EX002, reason="no_point", idx=self._experiment_card_index)
                return False

            # 流程.md 第3条：点击后检测「身体部位条是否消失」（实验选定后该条通常收起/不可见）。
            # 原 2s 偏紧；拉长超时并允许重开面板再点同卡一次。
            sel_confirm_timeout_sec = 3.5
            body_part_hidden = self.actions.wait_until_body_part_switch_hidden(timeout_sec=sel_confirm_timeout_sec)
            if not body_part_hidden:
                log(C.EX002, reason="retry_panel", idx=self._experiment_card_index)
                if not self._retry_experiment_panel_and_click_same_card(self._experiment_card_index):
                    self.state.manual_pause = True
                    self.state.set_status("实验切换失败: 实验卡片点位不可用")
                    log(C.EX002, reason="retry_fail", idx=self._experiment_card_index)
                    return False
                body_part_hidden = self.actions.wait_until_body_part_switch_hidden(timeout_sec=sel_confirm_timeout_sec)

            if not body_part_hidden:
                # 两次尝试后身体部位条仍在：多数为当前卡槽不可用，或「身体部位」模板/阈值需检查。
                if self._current_body_part_index != 5:
                    self.state.set_status("实验已用尽：切换到身体部位5号")
                    log(
                        C.EX005,
                        reason="card_body",
                        idx=self._experiment_card_index,
                        from_idx=self._current_body_part_index,
                        to=5,
                    )
                    self.actions.click_body_part(2)
                    sleep(0.5)
                    self.actions.click_body_part(5)
                    self._current_body_part_index = 5
                    self._experiment_card_index = 1
                    self._save_card_index_to_config(self._experiment_card_index)
                    continue
                # 5号身体部位仍失败：可能真已无可用实验，也可能是身体部位模板未稳定匹配。
                self.state.manual_pause = True
                self.state.set_status("全部实验已完成，程序暂停")
                log(C.EX004, reason="body5_fail")
                return False

            # ── 流程.md 第4条：【尝试部署实验】──
            self.state.set_status("尝试部署实验")
            # 流程第1步：进入部署阶段后先延时1秒，再点击鼠标左键。
            sleep(1.0)
            start_seen, both_ready = self.actions.deploy_and_check_start_recover(timeout_sec=2.0)
            if both_ready:
                # 部署成功后先等待 1s，再进入正常运行阶段。
                sleep(1.0)
                self.actions.reset_dynamic_learned_regions()
                self._experiment_switch_bootstrapped = True
                self._experiment_cycle_count = 0
                self.state.set_status("实验切换完成")
                log(C.EX003, idx=self._experiment_card_index)
                return True
            if start_seen:
                log(C.EX004, reason="no_recover", idx=self._experiment_card_index)
                self._experiment_card_index += 1
                self.actions.reset_dynamic_learned_regions()
                self._press_key("esc")
                sleep(1.0)
                self.actions.press_experiment_switch_hotkey()
                sleep(1.0)
                continue

            # ── 流程.md 第5条：开始按钮未出现 →【移动视角部署】5 秒内连续移动视角并左键 ──
            if not start_seen:
                if self.state.stop_requested or self.state.pending_calibration is not None:
                    return False
                if self._wait_if_paused_or_interrupted():
                    return False
                log(C.EX007)
                self.state.set_status("移动视角部署（平滑转圈）")
                mv_start_seen, mv_both = self.actions.move_camera_burst_deploy_check()
                if mv_both:
                    log(C.EX008, reason="ok")
                    sleep(1.0)
                    self.actions.reset_dynamic_learned_regions()
                    self._experiment_switch_bootstrapped = True
                    self._experiment_cycle_count = 0
                    self.state.set_status("实验切换完成")
                    log(C.EX003, idx=self._experiment_card_index)
                    return True
                if mv_start_seen:
                    log(C.EX008, reason="no_recover")
                    self._experiment_card_index += 1
                    self._press_key("esc")
                    sleep(1.0)
                    self.actions.press_experiment_switch_hotkey()
                    sleep(1.0)
                    continue
                # 5 秒内始终未出现开始按钮（或未达到双条件）→ 【切换下一实验】。
                log(C.EX004, reason="all_fail", idx=self._experiment_card_index)
                self._experiment_card_index += 1
                self._press_key("esc")
                sleep(1.0)
                self.actions.press_experiment_switch_hotkey()
                sleep(1.0)
                continue

        return False

    def _scroll_worker_loop(self):
        """
        滚轮副线程：
        - 不做识别、不做点击，只消费主线程下发的滚轮参数；
        - 任何时刻主线程都可通过 _scroll_enabled 快速启停。
        """
        while (not self.state.stop_requested) and (not self._scroll_stop_event.is_set()):
            if (not self._scroll_enabled) or self.state.manual_pause:
                sleep(0.01)
                continue

            with self._scroll_lock:
                amount = int(self._scroll_amount)
                batch = int(self._scroll_batch)

            if amount == 0 or batch <= 0:
                sleep(0.005)
                continue

            for _ in range(batch):
                if (not self._scroll_enabled) or self.state.manual_pause or self._scroll_stop_event.is_set():
                    break
                inp = self.actions._inp()
                if inp is not None:
                    inp.scroll(amount)
                else:
                    pyautogui.scroll(amount)

    def _start_scroll_worker(self):
        if self._scroll_thread is None or (not self._scroll_thread.is_alive()):
            self._scroll_stop_event.clear()
            self._scroll_thread = threading.Thread(target=self._scroll_worker_loop, daemon=True)
            self._scroll_thread.start()

    def _stop_scroll_worker(self):
        self._scroll_stop_event.set()
        if self._scroll_thread is not None:
            self._scroll_thread.join(timeout=1.0)
        self._scroll_thread = None

    def _set_scroll_command(self, amount, batch):
        with self._scroll_lock:
            self._scroll_amount = int(amount)
            self._scroll_batch = int(batch)

    def _clear_scroll_command(self):
        with self._scroll_lock:
            self._scroll_amount = 0
            self._scroll_batch = 0

    def _toggle_pause_by_f1(self):
        """
        F1 紧急开关：
        - 按一次暂停自动流程
        - 再按一次恢复流程
        """
        # 标定层期间保持暂停，避免误恢复导致鼠标继续被脚本接管。
        if str(self.state.current_status).startswith("标定中"):
            log(C.HK001, reason="calibration")
            return

        self.state.manual_pause = not self.state.manual_pause
        if self.state.manual_pause:
            self.state.set_status("F1紧急暂停")
            log(C.HK001, action="pause")
        else:
            # 主线程里收起展开的子页面，避免遮挡游戏区域、影响模板匹配。
            self.state.collapse_subpanels_request = True
            # 若此前由 F2 声明了“恢复后立即切换下一实验”，
            # 则恢复状态文案要明确提示“下一步会先切换”，避免用户误判脚本会先继续当前实验。
            if self._f2_pending_switch_after_resume:
                self.state.set_status("F1恢复运行，准备切换下一实验")
                log(C.HK002, action="resume_switch")
            else:
                self.state.set_status("F1恢复运行")
                log(C.HK002, action="resume")

    def _wait_if_paused_or_interrupted(self):
        """
        在流程执行中统一处理“暂停/中断”：
        - stop_requested 或进入标定（pending_calibration）时，返回 True 让上层中断当前流程；
        - manual_pause 时阻塞等待，直到用户恢复；
        - 若恢复后存在 F2 的“立即切换下一实验”请求，返回 True 终止当前阶段，
          让主循环马上进入“切换下一实验”分支。
        """
        if self.state.stop_requested or self.state.pending_calibration is not None:
            return True

        while self.state.manual_pause:
            if self.state.stop_requested or self.state.pending_calibration is not None:
                return True
            sleep(0.1)
        # 关键点：F2 触发后，恢复时立即打断当前阶段，避免继续执行旧流程，
        # 从而保证“恢复后马上调起切换下一实验”。
        if self._f2_pending_switch_after_resume:
            return True
        return False

    def _maybe_auto_refill_stamina_before_start(self):
        """
        自动补充体力：在「开始」按钮已出现、尚未点击前执行。
        1) 若「体力不足图标」可匹配，表示需要补充；
        2) 点击「体力补充按钮（独立）」模板中心（勿用主流程的 recover_stamina_button）；
        3) 延时 1s 后点击「使用凝胶确认」标定点。
        依赖标定：stamina_insufficient_icon、stamina_supplement_button（均为模板）、use_gel_confirm（单点）。
        """
        if not bool(self.config_store.data.get("auto_refill_stamina_enabled", False)):
            return
        if self._wait_if_paused_or_interrupted():
            return
        done = self.config_store.data.get("calibration_done", {})
        if not (
            bool(done.get("stamina_insufficient_icon"))
            and bool(done.get("stamina_supplement_button"))
            and bool(done.get("use_gel_confirm"))
        ):
            return
        try:
            if self.vision_service.match("stamina_insufficient_icon") is None:
                return
        except FileNotFoundError:
            return
        try:
            supplement_pos = self.vision_service.match("stamina_supplement_button")
        except FileNotFoundError:
            return
        if supplement_pos is None:
            self.state.set_status("自动补充体力：已检测到体力不足，但未匹配到独立补充按钮")
            return
        self.state.set_status("自动补充体力：点击独立补充按钮并确认凝胶")
        inp = self._inp()
        if inp is not None:
            inp.move_to(supplement_pos[0], supplement_pos[1])
            sleep(0.12)
            inp.left_click()
        else:
            pyautogui.moveTo(supplement_pos[0], supplement_pos[1])
            sleep(0.12)
            pyautogui.leftClick()
        t_end = time() + 1.0
        while time() < t_end:
            if self._wait_if_paused_or_interrupted():
                return
            sleep(min(0.08, t_end - time()))
        rect = self.config_store.data.get("calibration_rects", {}).get("use_gel_confirm")
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            return
        nx = (float(rect[0]) + float(rect[2])) / 2.0
        ny = (float(rect[1]) + float(rect[3])) / 2.0
        x, y = self.window_service.denormalize_point([nx, ny])
        if inp is not None:
            inp.move_to(x, y)
            sleep(0.08)
            inp.left_click()
        else:
            pyautogui.moveTo(x, y)
            sleep(0.08)
            pyautogui.leftClick()

    def _wait_until_start_visible_again(self, log_message="等待开始"):
        """
        自动补体力、凝胶确认等操作后，「开始」按钮常会短暂不匹配或不在画面上；
        若紧接着进入「while ready_to_start 点击开始」，可能因当前帧已无开始模板而整段循环不执行。
        因此补体力后必须重新轮询，直到「开始」再次出现再点。
        """
        while not self.actions.ready_to_start():
            if self._wait_if_paused_or_interrupted():
                return True
            self.state.log(log_message)
            sleep(0.2)
        return False

    def _pause_and_switch_next_experiment_by_f2(self):
        """
        F2 快捷操作（新规则）：
        - 立即进入暂停；
        - 打上“恢复后立即切换下一实验”的一次性标记；
        - 用户恢复（通常按 F1）后，主循环会优先执行切换分支。
        """
        experiment_switch_enabled = bool(self.config_store.data.get("experiment_switch_enabled", False))
        self.state.manual_pause = True
        if not experiment_switch_enabled:
            # 未开启实验切换时不保留待切换标记，避免恢复后触发无意义分支。
            self._f2_pending_switch_after_resume = False
            self.state.set_status("F2已暂停（实验切换未开启）")
            log(C.HK003, reason="no_exp_switch")
            return
        self._f2_pending_switch_after_resume = True
        self.state.set_status("F2已暂停，恢复后切换下一实验")
        log(C.HK003, action="pause_switch")

    def _toggle_like_force_next_by_f3(self):
        """
        F3 快捷操作：
        - 切换「结束后强制整轮点赞（一次性）」——主模式在成功点击再来一次后消费；
        - 与 UI 勾选保持同一配置项（like_force_next）。
        """
        like_enabled = bool(self.config_store.data.get("like_enabled", True))
        if not like_enabled:
            # 点赞功能关闭时，不允许置位强制点赞，避免出现看不见的无效状态。
            if bool(self.config_store.data.get("like_force_next", False)):
                self.config_store.data["like_force_next"] = False
                self.config_store.save()
            self.state.set_status("F3忽略：点赞功能未开启")
            log(C.HK004, reason="disabled")
            return

        next_value = not bool(self.config_store.data.get("like_force_next", False))
        self.config_store.data["like_force_next"] = next_value
        self.config_store.save()
        if next_value:
            self.state.set_status("F3已开启：结束后强制点赞")
            log(C.HK004, action="on")
        else:
            self.state.set_status("F3已关闭：结束后强制点赞")
            log(C.HK004, action="off")

    def _toggle_all_calibration_overlay_by_f12(self):
        """
        F12 调试三段式流程：
        1) 首次按下：弹出标定项选择窗口；
        2) 选择完成后再按一次：显示所选标定叠加；
        3) 再按一次：收起叠加并结束本轮。
        """
        phase = str(getattr(self.state, "calibration_overlay_phase", "idle"))
        if phase == "idle":
            self.state.show_all_calibration_overlay = False
            self.state.open_calibration_overlay_selector = True
            self.state.calibration_overlay_phase = "await_selection"
            log(C.HK005, action="select")
            return
        if phase == "await_selection":
            log(C.HK005, action="await")
            return
        if phase == "ready_to_show":
            selected = list(getattr(self.state, "calibration_overlay_selected_keys", []))
            if not selected:
                # 防御：无选择时回到第一步
                self.state.open_calibration_overlay_selector = True
                self.state.calibration_overlay_phase = "await_selection"
                log(C.HK005, reason="empty")
                return
            self.state.show_all_calibration_overlay = True
            self.state.calibration_overlay_phase = "showing"
            log(C.HK005, action="show")
            return
        if phase == "showing":
            self.state.show_all_calibration_overlay = False
            self.state.calibration_overlay_phase = "idle"
            self.state.calibration_overlay_selected_keys = []
            log(C.HK005, action="hide")
            return
        # 异常状态兜底
        self.state.show_all_calibration_overlay = False
        self.state.open_calibration_overlay_selector = False
        self.state.calibration_overlay_phase = "idle"
        self.state.calibration_overlay_selected_keys = []
        log(C.HK005, action="reset")

    def _replay_pull_new_experiment_scroll_by_f11(self):
        """F11 调试：随时重播“拉出新实验滚动”标定动作。"""
        self.actions.replay_pull_new_experiment_scroll_action(delay_sec=1.0)

    def _esc_until_recover_stamina_button_hidden(self):
        """
        F2→F1 时首次 ESC 已用于「退出当前实验」之后调用。

        若画面上仍能模板匹配到主流程的「恢复体力按钮」（recover_stamina_button），
        说明仍停留在需继续按 ESC 逐层返回的界面；先短暂延迟再判图，然后循环 ESC，
        直至该按钮不再出现——视为已回到脚本认知中的「大厅/列表」侧，再继续后续按 E 等步骤。

        注意：此处不使用「体力补充按钮（独立）」stamina_supplement_button，与自动补体力那条链路区分。

        未标定该模板或缺少模板文件时静默跳过，不阻塞切换流程。
        """
        done = self.config_store.data.get("calibration_done", {})
        if not bool(done.get("recover_stamina_button")):
            return
        # 给界面动画/叠层一拍时间，避免首帧误判。
        sleep(0.45)
        max_extra_esc = 60
        for round_idx in range(max_extra_esc):
            if self.state.stop_requested or self.state.pending_calibration is not None:
                return
            if self.state.manual_pause:
                return
            try:
                still = self.vision_service.match("recover_stamina_button") is not None
            except FileNotFoundError:
                return
            if not still:
                if round_idx > 0:
                    log(C.HK003, reason="recover_hidden")
                return
            if round_idx == 0:
                log(C.HK003, reason="esc_loop")
            self.state.set_status("F2切换：ESC 返回中（恢复体力按钮仍可见）")
            self._press_key("esc")
            sleep(0.38)

        log(C.HK003, reason="esc_max", n=max_extra_esc)

    def _switch_next_experiment_after_f2_resume(self):
        """
        仅用于 F2 场景下“恢复后立即切换下一实验”：
        - 实验卡片顺延一次；
        - 实验计数清零；
        - 不等待 2s，直接执行【切换下一实验】定义动作：
          ESC ->（若恢复体力按钮仍可见则间歇 ESC 直至消失）-> 延迟 1000ms -> E -> 等待 1s；
        - 标记面板已预开，避免 bootstrap 重复按 E。
        """
        self._experiment_cycle_count = 0
        self._experiment_card_index += 1
        self._experiment_switch_bootstrapped = False
        self.actions.reset_dynamic_learned_regions()
        self.state.set_status("F2恢复后：切换下一实验")
        self._press_key("esc")
        # 首次 ESC 后若仍卡在带「恢复体力按钮」的界面，继续 ESC 直到回到大厅认知态。
        self._esc_until_recover_stamina_button_hidden()
        sleep(1.0)
        self.actions.press_experiment_switch_hotkey()
        sleep(1.0)
        self._experiment_panel_preopened = True

    def _reopen_experiment_panel_with_esc(self):
        """
        仅用于“正常 5 回合切换实验”前的动作（流程.md 的6、5条）：
        按 ESC 退出当前实验；
        延迟 1s 后按 E 打开实验面板；
        最后再等待 1s，交由后续流程进入“尝试选定实验”阶段。
        """
        self._press_key("esc")
        sleep(1.0)
        self.actions.press_experiment_switch_hotkey()
        sleep(1.0)

    def _special_action_visible_for_stall(self):
        """特殊动作按钮是否可见（模板或变红），用于女条停滞武装/恢复。"""
        if self.actions.is_special_action_button_present():
            return True
        return self.actions.is_special_action_button_red(threshold=0.45)

    def _female_bar_stall_monitor_loop(self):
        """
        流程.md 第8条：正常运行时独立线程监测女进度条是否停滞。
        每 ~0.3s 采样一次 b1；若连续 FEMALE_BAR_STALL_NO_INCREASE_SECONDS 秒内无有效增长则置位停滞标志。
        """
        epsilon = FEMALE_BAR_STALL_GROWTH_EPSILON
        peak_b1 = None
        stall_since = None
        while not self._female_bar_monitor_stop.is_set():
            if not self._female_bar_monitor_active:
                peak_b1 = None
                stall_since = None
                sleep(0.1)
                continue
            if self.state.manual_pause:
                # 暂停中记下标记，恢复后给宽限期，避免 F1 恢复后立刻误判停滞。
                self._female_bar_monitor_was_paused = True
                peak_b1 = None
                stall_since = None
                sleep(0.1)
                continue
            if self._female_bar_monitor_was_paused:
                self._female_bar_monitor_was_paused = False
                self._female_bar_stall_grace_until = time() + FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC
            now = time()
            if now < self._female_bar_stall_suspend_until:
                self._female_bar_stall_in_suspend = True
                peak_b1 = None
                stall_since = None
                sleep(0.1)
                continue
            if self._female_bar_stall_in_suspend:
                self._female_bar_stall_in_suspend = False
                self._female_bar_stall_grace_until = now + FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC
                log(C.SA008, reason="suspend_done", grace=FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC)
            # 开局：须检测到特殊动作按钮（模板或变红）后再启动停滞计时。
            if self._female_bar_stall_wait_special_visible_after_start:
                try:
                    if self._special_action_visible_for_stall():
                        self._female_bar_stall_wait_special_visible_after_start = False
                        self._female_bar_stall_grace_until = time() + FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC
                        log(C.FB001, grace=FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC)
                    else:
                        peak_b1 = None
                        stall_since = None
                        sleep(0.1)
                        continue
                except Exception:
                    sleep(0.1)
                    continue
            try:
                b1, _ = self._get_bar_ratios()
            except Exception:
                sleep(0.3)
                continue
            if peak_b1 is None:
                peak_b1 = b1
                stall_since = now
            elif b1 > peak_b1 + epsilon:
                peak_b1 = b1
                stall_since = now
            elif now < self._female_bar_stall_grace_until:
                stall_since = now
            elif stall_since is not None and now - stall_since >= FEMALE_BAR_STALL_NO_INCREASE_SECONDS:
                log(C.FB002, b1=b1, peak=peak_b1, sec=FEMALE_BAR_STALL_NO_INCREASE_SECONDS)
                self._female_bar_stall_flag = True
                self._female_bar_monitor_active = False
            sleep(0.3)

    def _start_female_bar_monitor(self):
        """启动女进度条停滞监测线程（守护线程，生命周期跟随主循环）。"""
        if self._female_bar_monitor_thread is None or not self._female_bar_monitor_thread.is_alive():
            self._female_bar_monitor_stop.clear()
            self._female_bar_stall_flag = False
            self._female_bar_monitor_active = False
            self._female_bar_monitor_thread = threading.Thread(
                target=self._female_bar_stall_monitor_loop, daemon=True
            )
            self._female_bar_monitor_thread.start()

    def _stop_female_bar_monitor(self):
        """停止女进度条停滞监测线程。"""
        self._female_bar_monitor_active = False
        self._female_bar_monitor_stop.set()
        if self._female_bar_monitor_thread is not None:
            self._female_bar_monitor_thread.join(timeout=1.0)
        self._female_bar_monitor_thread = None

    def _reset_special_action_rise_gate(self):
        """清空「等敏感条上升」门控，允许新一轮判红触发。"""
        self._special_action_wait_sensitive_rise = False
        self._special_action_sensitive_baseline = None
        self._special_action_sensitive_trough = None
        self._special_action_sens_ema = None
        self._special_action_rise_wait_since = 0.0
        self._special_action_post_rise_until = 0.0

    def _special_action_sens_ema_alpha(self):
        try:
            return float(
                self.config_store.data.get(
                    "special_action_sens_ema_alpha", SPECIAL_ACTION_SENS_EMA_ALPHA
                )
            )
        except Exception:
            return SPECIAL_ACTION_SENS_EMA_ALPHA

    def _special_action_rise_stall_sec(self):
        try:
            return float(
                self.config_store.data.get(
                    "special_action_rise_stall_sec", SPECIAL_ACTION_RISE_STALL_SEC
                )
            )
        except Exception:
            return SPECIAL_ACTION_RISE_STALL_SEC

    def _special_action_phase_warmup_sec(self):
        try:
            return float(
                self.config_store.data.get(
                    "special_action_phase_warmup_sec", SPECIAL_ACTION_PHASE_WARMUP_SEC
                )
            )
        except Exception:
            return SPECIAL_ACTION_PHASE_WARMUP_SEC

    def _update_special_action_sens_ema(self, ratio_raw):
        """敏感条 EMA，抑制单帧跳变误触发升幅门控。"""
        if ratio_raw is None:
            return self._special_action_sens_ema
        alpha = self._special_action_sens_ema_alpha()
        if self._special_action_sens_ema is None:
            self._special_action_sens_ema = ratio_raw
        else:
            self._special_action_sens_ema = (
                alpha * ratio_raw + (1.0 - alpha) * self._special_action_sens_ema
            )
        return self._special_action_sens_ema

    def _log_special_action_diag(self, code, reason, interval_sec=1.0, **fields):
        now = time()
        key = (code, reason)
        last = self._special_action_diag_last_ts.get(key, 0.0)
        if now - last < interval_sec:
            return
        self._special_action_diag_last_ts[key] = now
        log(code, reason=reason, **fields)

    def _special_action_sensitive_max(self):
        """敏感条达到该填充率时不再触发特殊动作（config 可覆盖）。"""
        try:
            return float(
                self.config_store.data.get("special_action_sensitive_max", SPECIAL_ACTION_SENSITIVE_MAX)
            )
        except Exception:
            return SPECIAL_ACTION_SENSITIVE_MAX

    def _special_action_rise_min(self):
        try:
            return float(
                self.config_store.data.get(
                    "special_action_sensitive_rise_min", SPECIAL_ACTION_SENSITIVE_RISE_MIN
                )
            )
        except Exception:
            return SPECIAL_ACTION_SENSITIVE_RISE_MIN

    def _special_action_post_rise_delay(self):
        try:
            return float(
                self.config_store.data.get(
                    "special_action_post_rise_delay_sec", SPECIAL_ACTION_POST_RISE_DELAY_SEC
                )
            )
        except Exception:
            return SPECIAL_ACTION_POST_RISE_DELAY_SEC

    def _special_action_rise_gate_allows_trigger(self):
        """
        按「1」后的上升门控（详细流程逻辑 §3）：
        - 未在等待上升：允许进入触发判定；
        - 等待中：EMA 相对「触发后谷底」上升 + post-rise 延时（不要求按钮仍红）；
        - 长时间无上升：超时重置门控，避免 sens 持平导致永久漏触发；
        - 敏感条已满：重置门控（满则不再触发）；
        - 门控解除后由 evaluate_special_action_trigger 再判红触发。
        """
        if not self._special_action_wait_sensitive_rise:
            return True

        sensitive_max = self._special_action_sensitive_max()
        ratio_raw = self.actions.get_sensitive_progress_bar_ratio()
        ratio = self._update_special_action_sens_ema(ratio_raw)
        baseline = self._special_action_sensitive_baseline
        rise_min = self._special_action_rise_min()
        now_ts = time()

        if ratio is not None and ratio >= sensitive_max:
            self._reset_special_action_rise_gate()
            self._log_special_action_diag(C.SA010, "gate_sens_high", sens=ratio, max=sensitive_max)
            return False

        # 已检测到上升：仅等待 post-rise 倒计时（按钮变灰不阻断）。
        if self._special_action_post_rise_until > 0.0:
            if now_ts >= self._special_action_post_rise_until:
                self._reset_special_action_rise_gate()
                return True
            self._log_special_action_diag(
                C.SA010,
                "gate_post_wait",
                sens=ratio,
                base=baseline,
                trough=self._special_action_sensitive_trough,
                left=max(0.0, self._special_action_post_rise_until - now_ts),
            )
            return False

        if ratio is not None:
            if self._special_action_sensitive_trough is None:
                self._special_action_sensitive_trough = ratio
            else:
                self._special_action_sensitive_trough = min(
                    self._special_action_sensitive_trough, ratio
                )

        trough = self._special_action_sensitive_trough
        stall_sec = self._special_action_rise_stall_sec()
        if (
            self._special_action_rise_wait_since > 0.0
            and stall_sec > 0.0
            and (now_ts - self._special_action_rise_wait_since) >= stall_sec
        ):
            self._reset_special_action_rise_gate()
            self._log_special_action_diag(
                C.SA010,
                "gate_stall_reset",
                sens=ratio,
                base=baseline,
                trough=trough,
                stall=stall_sec,
            )
            return True

        # 等待敏感条相对触发后谷底上升（EMA，不要求按钮仍红）。
        if ratio is not None and trough is not None and ratio >= trough + rise_min:
            self._special_action_post_rise_until = now_ts + self._special_action_post_rise_delay()
            self._log_special_action_diag(
                C.SA010,
                "gate_rise_seen",
                sens=ratio,
                base=baseline,
                trough=trough,
                rise=rise_min,
            )
            return False

        self._log_special_action_diag(
            C.SA010,
            "gate_wait_rise",
            sens=ratio,
            base=baseline,
            trough=trough,
            rise=rise_min,
        )
        return False

    def _special_action_timing(self):
        """读取特殊动作线程的轮询/按键/节流参数（config 可覆盖模块默认值）。"""
        cfg = self.config_store.data
        try:
            poll = float(cfg.get("special_action_poll_interval_sec", SPECIAL_ACTION_POLL_INTERVAL_SEC))
        except Exception:
            poll = SPECIAL_ACTION_POLL_INTERVAL_SEC
        try:
            press_delay = float(cfg.get("special_action_press_delay_sec", SPECIAL_ACTION_PRESS_DELAY_SEC))
        except Exception:
            press_delay = SPECIAL_ACTION_PRESS_DELAY_SEC
        try:
            cooldown = float(cfg.get("special_action_trigger_cooldown_sec", SPECIAL_ACTION_TRIGGER_COOLDOWN_SEC))
        except Exception:
            cooldown = SPECIAL_ACTION_TRIGGER_COOLDOWN_SEC
        return (
            max(0.02, poll),
            max(0.0, press_delay),
            max(0.15, cooldown),
        )

    def _special_action_monitor_loop(self):
        """
        特殊动作线程：
        仅在“点击开始后~点击高潮前”激活，持续循环判断并触发主键盘“1”。
        """
        poll_sec, press_delay_sec, cooldown_sec = self._special_action_timing()
        idle_sleep = poll_sec
        while not self._special_action_monitor_stop.is_set():
            if (not self._special_action_monitor_active) or self.state.manual_pause:
                sleep(idle_sleep)
                continue

            now_ts = time()
            # 刚按完「1」的短冷却内不再重复触发，避免连按。
            if (now_ts - self._special_action_last_trigger_ts) < SPECIAL_ACTION_REAPPEAR_DELAY_AFTER_ONE_SEC:
                self._log_special_action_diag(
                    C.SA009,
                    "reappear_wait",
                    left=SPECIAL_ACTION_REAPPEAR_DELAY_AFTER_ONE_SEC
                    - (now_ts - self._special_action_last_trigger_ts),
                )
                sleep(idle_sleep)
                continue

            if not self._special_action_rise_gate_allows_trigger():
                sleep(idle_sleep)
                continue

            warmup_sec = self._special_action_phase_warmup_sec()
            if (
                self._special_action_phase_started_at > 0.0
                and warmup_sec > 0.0
                and (now_ts - self._special_action_phase_started_at) < warmup_sec
            ):
                sens_probe = self.actions.get_sensitive_progress_bar_ratio()
                if sens_probe is None or sens_probe < 0.06:
                    self._log_special_action_diag(
                        C.SA009,
                        "phase_warmup",
                        left=warmup_sec - (now_ts - self._special_action_phase_started_at),
                        sens=sens_probe,
                    )
                    sleep(idle_sleep)
                    continue

            sensitive_max = self._special_action_sensitive_max()
            ready, sensitive_ratio, detail = self.actions.evaluate_special_action_trigger(
                sensitive_max=sensitive_max,
                detail=True,
            )
            if not ready:
                detail = detail or {}
                reason = detail.get("reason", "not_ready")
                self._log_special_action_diag(
                    C.SA009,
                    reason,
                    sens=sensitive_ratio,
                    max=detail.get("max", sensitive_max),
                    red=detail.get("red"),
                    th=detail.get("th"),
                    mode=detail.get("mode"),
                    x=detail.get("x"),
                    y=detail.get("y"),
                    w=detail.get("w"),
                    h=detail.get("h"),
                )
                sleep(idle_sleep)
                continue
            if self._special_action_should_abort():
                self._log_special_action_diag(C.SA009, "abort", sens=sensitive_ratio)
                sleep(idle_sleep)
                continue
            if (now_ts - self._special_action_last_trigger_ts) < cooldown_sec:
                self._log_special_action_diag(
                    C.SA009,
                    "cooldown",
                    sens=sensitive_ratio,
                    left=cooldown_sec - (now_ts - self._special_action_last_trigger_ts),
                )
                sleep(idle_sleep)
                continue
            if self.actions.press_main_keyboard_one_after_delay(
                delay_sec=press_delay_sec, abort_check=self._special_action_should_abort
            ):
                self._special_action_last_trigger_ts = now_ts
                self._special_action_wait_sensitive_rise = True
                self._special_action_sensitive_baseline = sensitive_ratio
                self._special_action_sensitive_trough = sensitive_ratio
                self._special_action_sens_ema = sensitive_ratio
                self._special_action_rise_wait_since = now_ts
                self._special_action_post_rise_until = 0.0
                self._female_bar_stall_flag = False
                self._female_bar_stall_suspend_until = now_ts + FEMALE_BAR_STALL_SUSPEND_AFTER_ONE_SEC
                # 按 1 即说明特殊按钮已可用，确保女条停滞检测已武装。
                if self._female_bar_stall_wait_special_visible_after_start:
                    self._female_bar_stall_wait_special_visible_after_start = False
                    self._female_bar_stall_grace_until = time() + FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC
                    log(C.FB001, grace=FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC, via="sa")
                rise_min = self._special_action_rise_min()
                post_delay = self._special_action_post_rise_delay()
                log(C.SA007, sens=sensitive_ratio, rise=rise_min, post=post_delay)
            sleep(idle_sleep)

    def _start_special_action_monitor(self):
        """启动特殊动作线程（守护线程，生命周期跟随主循环）。"""
        if self._special_action_monitor_thread is None or (not self._special_action_monitor_thread.is_alive()):
            self._special_action_monitor_stop.clear()
            self._special_action_monitor_active = False
            self._special_action_last_trigger_ts = 0.0
            self._reset_special_action_rise_gate()
            self._female_bar_stall_suspend_until = 0.0
            self._female_bar_stall_in_suspend = False
            self._special_action_monitor_thread = threading.Thread(
                target=self._special_action_monitor_loop, daemon=True
            )
            self._special_action_monitor_thread.start()

    def _stop_special_action_monitor(self):
        """停止特殊动作线程。"""
        self._special_action_monitor_active = False
        self._special_action_monitor_stop.set()
        if self._special_action_monitor_thread is not None:
            self._special_action_monitor_thread.join(timeout=1.0)
        self._special_action_monitor_thread = None

    def _stall_recovery_rescue_keys(self):
        keys = self.config_store.data.get("stall_recovery_rescue_keys", ["j", "k"])
        if not isinstance(keys, list):
            return ["j", "k"]
        cleaned = [str(key).strip().lower() for key in keys if str(key).strip()]
        return cleaned or ["j", "k"]

    def _next_stall_recovery_rescue_key(self):
        keys = self._stall_recovery_rescue_keys()
        mode = str(self.config_store.data.get("stall_recovery_rescue_mode", "alternate")).strip().lower()
        if mode == "random":
            return random.choice(keys)
        if mode == "first":
            return keys[0]
        key = keys[self._stall_recovery_rescue_next_index % len(keys)]
        self._stall_recovery_rescue_next_index += 1
        return key

    def _finish_wait_timeout_sec(self):
        try:
            return float(self.config_store.data.get("finish_wait_timeout_sec", FINISH_WAIT_TIMEOUT_SEC))
        except Exception:
            return FINISH_WAIT_TIMEOUT_SEC

    def _finish_wait_esc_max(self):
        try:
            return max(1, int(self.config_store.data.get("finish_wait_esc_max", FINISH_WAIT_ESC_MAX)))
        except Exception:
            return FINISH_WAIT_ESC_MAX

    def _esc_until_start_or_finish(self, status_msg, log_context="finish_wait"):
        """循环 ESC 直到出现开始或结束按钮。"""
        max_esc = self._finish_wait_esc_max()
        for attempt in range(1, max_esc + 1):
            if self._wait_if_paused_or_interrupted():
                return "interrupted"
            if self.actions.ready_to_start():
                log(C.FN001, ctx=log_context, reason="start", n=attempt)
                return "start"
            if self.actions.ready_to_finish():
                log(C.FN001, ctx=log_context, reason="finish", n=attempt)
                return "finish"
            self.state.set_status(f"{status_msg}（ESC {attempt}/{max_esc}）")
            log(C.FN001, ctx=log_context, reason="esc", n=attempt, m=max_esc)
            self._press_key("esc")
            sleep(0.35)
            poll_until = monotonic() + 2.0
            while monotonic() < poll_until:
                if self._wait_if_paused_or_interrupted():
                    return "interrupted"
                if self.actions.ready_to_start():
                    log(C.FN001, ctx=log_context, reason="start", n=attempt)
                    return "start"
                if self.actions.ready_to_finish():
                    log(C.FN001, ctx=log_context, reason="finish", n=attempt)
                    return "finish"
                sleep(0.08)
        log(C.FN001, ctx=log_context, reason="esc_max", m=max_esc)
        return None

    def _wait_for_finish_button_with_esc_recovery(self):
        """
        等待「再来一次/结束」按钮出现；超时则 ESC 直到看到开始或结束按钮。
        返回 finish / start / interrupted；esc_max 后仍无界面则继续等待下一轮超时。
        """
        timeout = self._finish_wait_timeout_sec()
        deadline = monotonic() + timeout
        while not self.actions.ready_to_finish():
            if self._wait_if_paused_or_interrupted():
                return "interrupted"
            if monotonic() >= deadline:
                log(C.FN001, ctx="finish_wait", reason="timeout", sec=timeout)
                self.state.log("等待结束超时，ESC 返回")
                result = self._esc_until_start_or_finish(
                    status_msg="等待结束超时",
                    log_context="finish_wait",
                )
                if result in ("interrupted", "start", "finish"):
                    return result
                deadline = monotonic() + timeout
                continue
            self.state.log("等待结束")
            self.actions.wait(0.2)
        return "finish"

    def _wait_stall_recovery_surface(self, timeout_sec=3.0, poll_interval_sec=0.10):
        deadline = monotonic() + max(0.2, float(timeout_sec))
        while monotonic() < deadline:
            if self._wait_if_paused_or_interrupted():
                return "interrupted"
            if self.actions.ready_to_start():
                return "start"
            if self.actions.ready_to_finish():
                return "finish"
            sleep(max(0.03, float(poll_interval_sec)))
        return None

    def _escape_or_rescue_stall_recovery_surface(self):
        try:
            esc_attempts = max(1, int(self.config_store.data.get("stall_recovery_esc_attempts_before_rescue", 3)))
        except Exception:
            esc_attempts = 3

        for attempt in range(1, esc_attempts + 1):
            self.state.set_status(f"女进度条停滞：ESC恢复尝试 {attempt}/{esc_attempts}")
            log(C.FB003, n=attempt, m=esc_attempts)
            self._press_key("esc")
            surface = self._wait_stall_recovery_surface(timeout_sec=3.0)
            if surface in ("start", "finish", "interrupted"):
                return surface

        if not bool(self.config_store.data.get("stall_recovery_rescue_enabled", True)):
            return None

        keys = self._stall_recovery_rescue_keys()
        for rescue_attempt in range(1, len(keys) + 1):
            key = self._next_stall_recovery_rescue_key()
            self.state.set_status(f"女进度条停滞：ESC失败，尝试按 {key.upper()}")
            log(C.FB003, n=rescue_attempt, m=len(keys), key=key)
            self._press_key(key)
            surface = self._wait_stall_recovery_surface(timeout_sec=3.0)
            if surface in ("start", "finish", "interrupted"):
                return surface

        return None

    def _click_finish_until_start_after_stall_rescue(self):
        deadline = monotonic() + 8.0
        while monotonic() < deadline:
            if self._wait_if_paused_or_interrupted():
                return False
            if self.actions.ready_to_start():
                return True
            if self.actions.ready_to_finish():
                clicked = self.actions.finish()
                if clicked:
                    self.state.log("停滞恢复：点击结束")
                else:
                    self.state.log("停滞恢复：结束按钮点击未确认，重试")
                sleep(0.2)
                continue
            sleep(0.2)
        return False

    def _recover_after_female_bar_stall(self, bar_balance_tolerance):
        """
        按“女进度条停滞恢复流程”执行恢复并重启当前实验：
        1) 多次 ESC，等待开始按钮或结束按钮；
        2) 若 ESC 仍失效，按 J/K 释放游戏卡死态；
        3) 若出现结束按钮，先点击结束并等待开始按钮；
        4) 开始按钮出现后等待 2 秒；
        5) 持续检测女/男进度条占比，满足以下任一条件后循环点击开始按钮：
           - 两者差值 <= 20%（近似相等）；
           - 两者占比都为 0（视为相等）；
           - 女进度条 > 男进度条 且 女进度条 < 60%（允许继续运行）。
        """
        log(C.FB004)
        self.state.set_status("女进度条停滞：恢复中")
        # 停滞恢复场景单独放宽判定：按需求固定使用 20% 容差。
        near_equal_tolerance = max(float(bar_balance_tolerance), 0.20)

        surface = self._escape_or_rescue_stall_recovery_surface()
        if surface == "interrupted":
            return False
        if surface is None:
            self.state.manual_pause = True
            self.state.set_status("女进度条停滞：恢复失败，已暂停")
            log(C.FB005, reason="no_surface")
            return False
        if surface == "finish":
            self.state.set_status("女进度条停滞：点击结束后等待开始")
            if not self._click_finish_until_start_after_stall_rescue():
                self.state.manual_pause = True
                self.state.set_status("女进度条停滞：结束后未出现开始，已暂停")
                log(C.FB005, reason="no_start_after_finish")
                return False

        # 保险等待：即便 3 秒内已出现，也统一进入“开始按钮稳定后再操作”节奏。
        self.state.set_status("女进度条停滞：开始按钮已出现，等待2秒")
        while not self.actions.ready_to_start():
            if self._wait_if_paused_or_interrupted():
                return False
            sleep(0.2)
        sleep(2.0)

        # 按需求：在开始按钮可见阶段，循环等待“双条近似相等/可放行”。
        self.state.set_status("女进度条停滞：等待双进度条近似相等")
        while True:
            if self._wait_if_paused_or_interrupted():
                return False
            if not self.actions.ready_to_start():
                # 若过程中开始按钮短暂消失，回到等待，避免误触发。
                sleep(0.2)
                continue
            try:
                b1, b2 = self._get_bar_ratios()
            except Exception:
                sleep(0.15)
                continue
            # 条件1：常规“近似相等”判定（差值 <= 20%）。
            near_equal = abs(b1 - b2) <= near_equal_tolerance
            # 条件2：两者都为 0 视为相等；用极小阈值兼容浮点噪声。
            both_zero = (b1 <= 0.001) and (b2 <= 0.001)
            # 条件3：开始按钮出现 2 秒后，若女条略高但女条本身 <60%，也允许继续。
            female_ahead_but_low = (b1 > b2) and (b1 < 0.60)
            if near_equal or both_zero or female_ahead_but_low:
                break
            sleep(0.12)

        # “循环点击开始按钮”：按钮还在就持续点击，直到进入正常运行阶段。
        start_result = self._click_start_until_confirmed(
            experiment_switch_enabled=bool(self.config_store.data.get("experiment_switch_enabled", False)),
            log_prefix="停滞恢复：",
        )
        if start_result is False:
            return False
        if start_result == "rebootstrap":
            return False

        return True

    def _register_hotkeys(self):
        # suppress=False 保持 F1 原生行为不被拦截，仅增加脚本暂停能力。
        if self._f1_hotkey_handle is None:
            self._f1_hotkey_handle = keyboard.add_hotkey("f1", self._toggle_pause_by_f1, suppress=False)
        if self._f2_hotkey_handle is None:
            self._f2_hotkey_handle = keyboard.add_hotkey("f2", self._pause_and_switch_next_experiment_by_f2, suppress=False)
        if self._f3_hotkey_handle is None:
            self._f3_hotkey_handle = keyboard.add_hotkey("f3", self._toggle_like_force_next_by_f3, suppress=False)
        if self._f11_hotkey_handle is None:
            self._f11_hotkey_handle = keyboard.add_hotkey("f11", self._replay_pull_new_experiment_scroll_by_f11, suppress=False)
        if self._f12_hotkey_handle is None:
            self._f12_hotkey_handle = keyboard.add_hotkey("f12", self._toggle_all_calibration_overlay_by_f12, suppress=False)

    def _unregister_hotkeys(self):
        if self._f1_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f1_hotkey_handle)
            self._f1_hotkey_handle = None
        if self._f2_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f2_hotkey_handle)
            self._f2_hotkey_handle = None
        if self._f3_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f3_hotkey_handle)
            self._f3_hotkey_handle = None
        if self._f11_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f11_hotkey_handle)
            self._f11_hotkey_handle = None
        if self._f12_hotkey_handle is not None:
            keyboard.remove_hotkey(self._f12_hotkey_handle)
            self._f12_hotkey_handle = None
        # 兜底：移除本进程注册的其余钩子，避免关闭后热键仍驻留导致“像没退出”。
        try:
            keyboard.unhook_all()
        except Exception:
            pass

    def loop_once(self):
        # 单高潮模式优先级最高：开启后仅保留“开始/单高潮/再来一次”三按钮流程（赞池轮询仅在其中生效）。
        if self._single_cum_mode_enabled():
            self._run_single_cum_mode_once()
            return

        # —— 以下为【主模式】loop_once：点赞在成功点击「再来一次」后由 _maybe_like_after_finish_main() 处理。 ——

        experiment_switch_enabled = bool(self.config_store.data.get("experiment_switch_enabled", False))
        # F2 一次性“恢复后切换”入口：
        # 该分支优先级最高，确保恢复后先切换，再决定是否进入主流程。
        if self._f2_pending_switch_after_resume:
            if not experiment_switch_enabled:
                # 若恢复前用户关闭了开关，则取消这次待切换请求，避免误动作。
                self._f2_pending_switch_after_resume = False
                self.state.set_status("F2待切换取消：实验切换未开启")
                sleep(0.2)
                return
            if not self._ensure_experiment_switch_ready():
                sleep(0.2)
                return
            self._switch_next_experiment_after_f2_resume()
            self._f2_pending_switch_after_resume = False
            return

        if not experiment_switch_enabled:
            # 开关关闭时重置“首次启动运行”状态；下次再开启会重新走首次流程。
            self._experiment_switch_bootstrapped = False
            self._experiment_card_index = self._read_card_index_from_config()
            self._experiment_first_stage_done = False
            self._experiment_cycle_count = 0
            self._current_body_part_index = 2
            self._switch_after_five_on_start_pending = False
        else:
            if not self._ensure_experiment_switch_ready():
                sleep(0.2)
                return
            if not self._run_experiment_switch_bootstrap():
                sleep(0.2)
                return
            # 满 5 次后不立刻切换，等"开始按钮出现"再等待 2s，然后按点位号分支处理。
            if self._switch_after_five_on_start_pending:
                self.state.set_status("5次完成：等待开始按钮后切换")
                while not self.actions.ready_to_start():
                    if self._wait_if_paused_or_interrupted():
                        return
                    sleep(0.2)
                sleep(2.0)
                self._switch_after_five_on_start_pending = False
                self._experiment_switch_bootstrapped = False

                if self._experiment_card_index == 12:
                    # 流程.md 【当页实验全部完成】：
                    # 当前实验是本页最后一个（点位号=12），需先退出实验面板，
                    # 拉出新实验滚动，再将点位号置为9，重新进入【尝试选定实验】。
                    self.state.set_status("当页实验全部完成：拉出新实验")
                    log(C.EX006)
                    self._press_key("esc")
                    sleep(1.0)
                    self.actions.press_experiment_switch_hotkey()
                    sleep(1.0)
                    # 拉出新实验滚动，将下一页实验列表推入视野。
                    self.actions.replay_pull_new_experiment_scroll_action(delay_sec=0.0)
                    # 下次从第9个实验卡片开始选定。
                    self._experiment_card_index = 9
                    self._experiment_cycle_count = 0
                    self._save_card_index_to_config(self._experiment_card_index)
                    # 面板已在上方按 E 打开，告知 bootstrap 不要重复按 E。
                    self._experiment_panel_preopened = True
                else:
                    # 流程.md 【切换下一实验】（点位号≠12）：
                    # 点赞已在每轮回合结束（再来一次后）处理；此处直接执行切换。
                    self.state.set_status("实验5次完成，执行切换实验")
                    log(C.EX005, idx=self._experiment_card_index + 1)
                    self._experiment_card_index += 1
                    self._experiment_cycle_count = 0
                    self._reopen_experiment_panel_with_esc()
                    # 标记本轮已按过 E，避免下次进入 bootstrap 重复按键。
                    self._experiment_panel_preopened = True
                return

        # 进度条平衡容差（作用于平滑后的 diff，见下方 EMA）：
        # 调整为更细更及时：减小死区、提高采样频率。
        bar_balance_tolerance = 0.010
        balance_check_interval_sec = 0.30
        bar_fill_ema_alpha = 0.52
        # 强约束：默认关闭滚轮，仅在“点击开始后~点击高潮前”临时开启。
        self._scroll_enabled = False
        self._clear_scroll_command()

        while not self.actions.ready_to_start():
            if self._wait_if_paused_or_interrupted():
                return
            self.state.log("等待开始")
            sleep(0.2)

        # 回合结束后再进关：此时「开始」已出现；若开启自动补体力且检测到体力提示，则先处理再点「开始」。
        self._maybe_auto_refill_stamina_before_start()
        if self._wait_until_start_visible_again():
            return

        start_result = self._click_start_until_confirmed(
            experiment_switch_enabled=experiment_switch_enabled,
        )
        if start_result is False:
            return
        if start_result == "rebootstrap":
            return

        # “点击开始后~点击高潮前”阶段：
        # 若发生女进度条停滞，则按新规则执行恢复，并在恢复后继续留在当前实验。
        while True:
            # 每轮“等待高潮”前重置平滑状态，避免停滞恢复后沿用旧轮次数据。
            next_balance_check_at = time()
            bar_fill_ema_b1 = None
            bar_fill_ema_b2 = None
            # 流程.md 第8条：女进度条停滞监测（仅实验切换模式）。
            # 首次运行 / 切换实验后 / 每轮点击「开始」后：须先模板匹配到特殊动作按钮，再启动停滞检测（超时见 FEMALE_BAR_STALL_NO_INCREASE_SECONDS）。
            if experiment_switch_enabled:
                self._female_bar_stall_flag = False
                self._female_bar_monitor_active = True
                self._female_bar_stall_suspend_until = 0.0
                self._female_bar_stall_wait_special_visible_after_start = True
            # 新一轮「开始~高潮」阶段：刷新令牌，使旧线程中待发送的「1」全部失效。
            self._special_action_phase_token += 1
            self._special_action_expected_token = self._special_action_phase_token
            self._special_action_last_trigger_ts = 0.0
            self._reset_special_action_rise_gate()
            self._special_action_phase_started_at = time()
            self._special_action_monitor_active = True

            # 仅在“点击开始后~点击高潮前”阶段启用滚轮纠偏。
            self._scroll_enabled = True
            stall_detected = False
            try:
                while not self.actions.ready_to_cum():
                    if self._wait_if_paused_or_interrupted():
                        return
                    # 流程.md 第8条：检查停滞标志
                    if experiment_switch_enabled and self._female_bar_stall_flag:
                        stall_detected = True
                        break
                    self.state.log("等待高潮")
                    now = time()
                    # 高频闭环：约每 0.4 秒做一次纠偏，避免进度条差距扩散过快。
                    # 约定固定为：
                    # - b1 = 女进度条（原上方进度条）
                    # - b2 = 男进度条（原下方进度条）
                    if now >= next_balance_check_at:
                        b1, b2 = self._get_bar_ratios()
                        # EMA：用平滑后的填充率算 diff，抑制单帧跳变导致的纠偏方向抖动。
                        if bar_fill_ema_b1 is None:
                            bar_fill_ema_b1, bar_fill_ema_b2 = b1, b2
                        else:
                            a = bar_fill_ema_alpha
                            bar_fill_ema_b1 = a * b1 + (1.0 - a) * bar_fill_ema_b1
                            bar_fill_ema_b2 = a * b2 + (1.0 - a) * bar_fill_ema_b2
                        diff = bar_fill_ema_b1 - bar_fill_ema_b2
                        if self.state.debug:
                            log_debug(
                                self.state.debug,
                                C.BR001,
                                raw_f=b1,
                                raw_m=b2,
                                ema_f=bar_fill_ema_b1,
                                ema_m=bar_fill_ema_b2,
                                diff=diff,
                            )

                        # 仅当平滑后的 |diff| 超出死区才纠偏；力度随 |diff| 分档，并整体压低批次避免过冲。
                        if abs(diff) > bar_balance_tolerance:
                            self.actions.move_to_scroll_region_center()

                            ad = abs(diff)
                            # 更细分档：在中小差值区间提供更细腻纠偏。
                            if ad > 0.18:
                                scroll_count = 24
                            elif ad > 0.12:
                                scroll_count = 20
                            elif ad > 0.08:
                                scroll_count = 16
                            elif ad > 0.05:
                                scroll_count = 12
                            elif ad > 0.025:
                                scroll_count = 9
                            else:
                                scroll_count = 6

                            # 临近满条时略加大纠偏，但增量减半以减轻末端抖动。
                            if max(bar_fill_ema_b1, bar_fill_ema_b2) > 0.85 and abs(diff) > bar_balance_tolerance:
                                scroll_count += 4

                            scroll_count = max(4, min(28, scroll_count))

                            # 速度纠偏：滚轮方向相对上一版整体取反（pyautogui.scroll 正数为常见“向上滚”语义）。
                            if diff > 0:
                                # 女进度条 > 男进度条：原方向取反后使用 +360。
                                if self.state.debug:
                                    log_debug(self.state.debug, C.BR001, dir="up", count=scroll_count)
                                self._set_scroll_command(+360, scroll_count)
                            else:
                                # 女进度条 < 男进度条：原方向取反后使用 -360。
                                if self.state.debug:
                                    log_debug(self.state.debug, C.BR001, dir="down", count=scroll_count)
                                self._set_scroll_command(-360, scroll_count)
                        else:
                            # 差值已在容差内：暂停副线程滚轮输出，避免多余扰动。
                            self._clear_scroll_command()
                        next_balance_check_at = now + balance_check_interval_sec
                    # 主线程优先响应按钮检测，轮询频率高于滚轮参数刷新频率。
                    sleep(0.06)
            finally:
                # 无论正常进入高潮、手动中断或异常，都立即停滚轮并停用停滞监测。
                self._scroll_enabled = False
                self._clear_scroll_command()
                self._female_bar_monitor_active = False
                self._special_action_monitor_active = False
                # 离开阶段：递增令牌，使特殊动作线程内任何未完成的延迟按键被取消。
                self._special_action_phase_token += 1

            if not stall_detected:
                break

            if not self._recover_after_female_bar_stall(bar_balance_tolerance=bar_balance_tolerance):
                return

        while self.actions.ready_to_cum():
            if self._wait_if_paused_or_interrupted():
                return
            clicked = self.actions.cum()
            if not clicked:
                self.state.log("高潮按钮点击未确认，重试")
                self.actions.wait(0.12)
                continue
            self.state.log("点击高潮")
            # 累计统计：每次确认成功的高潮点击；第 5 次成功高潮（且已开启实验切换、本卡已跑完前 4 回合）计为一个「5回合」实验单元。
            self._runtime_total_cum_successes += 1
            if experiment_switch_enabled and self._experiment_cycle_count == 4:
                self._runtime_total_five_round_experiments += 1
            self._print_runtime_experiment_stats()
            # 高潮阶段点击优先速度，缩短间隔。
            self.actions.wait(0.1)

        finish_wait = self._wait_for_finish_button_with_esc_recovery()
        if finish_wait == "interrupted":
            return
        if finish_wait == "start":
            self.state.log("结束等待超时：已看到开始按钮")
            return

        finish_missing_checks = 0
        while True:
            if self._wait_if_paused_or_interrupted():
                return
            if not self.actions.ready_to_finish():
                # 进入结束阶段后，不能因单帧模板 miss 就直接推进到下一轮；
                # 只有开始按钮出现，才说明“再来一次/结束”阶段已经真正结束。
                if self.actions.ready_to_start():
                    self.state.log("结束按钮已消失，等待开始")
                    return
                finish_missing_checks += 1
                if finish_missing_checks == 1 or finish_missing_checks % 5 == 0:
                    self.state.log("结束按钮短暂未匹配，继续确认")
                self.actions.wait(0.2)
                continue
            finish_missing_checks = 0
            clicked = self.actions.finish()
            if not clicked:
                self.state.log("结束按钮点击未确认，重试")
                self.actions.wait(0.12)
                continue
            self.state.log("点击结束")
            self.actions.wait(0.2)
            # 主模式：再来一次成功后检测赞池一次（或消费 like_force_next）。
            self._maybe_like_after_finish_main()

            # 新规则（按流程文档）：
            # 在实验切换模式下，正常运行满 5 回合后，
            # 先处理点赞，再等待“开始按钮出现后 2s”执行切换。
            if experiment_switch_enabled:
                self._experiment_cycle_count += 1
                if self._experiment_cycle_count >= 5:
                    self._experiment_cycle_count = 0
                    self._switch_after_five_on_start_pending = True
                    self.state.set_status("实验5次完成，等待开始按钮后切换")
                    return
            return

    def run_forever(self):
        self._register_hotkeys()
        self._start_scroll_worker()
        self._start_female_bar_monitor()
        self._start_special_action_monitor()
        # 原固定 sleep(2) 会在用户立刻关闭窗口时仍阻塞 2 秒，延迟释放热键与退出。
        _sleep_interruptible(2.0, self.state)
        if not self.state.stop_requested:
            self.state.set_status("初始化完成")
        try:
            while not self.state.stop_requested:
                self._poll_home_page_scene()
                if self.state.manual_pause:
                    if self.state.current_status not in (
                        "F1紧急暂停",
                        "标定中",
                        "取消标定",
                    ) and not self.state.current_status.startswith("已应用标定"):
                        self.state.set_status("手动暂停")
                    # 分段睡眠，便于 stop_requested 后尽快结束循环
                    _sleep_interruptible(0.2, self.state)
                    continue

                try:
                    self.loop_once()
                except Exception as exc:
                    self.state.set_status(f"异常: {exc}")
                    log(C.SYS001, err=exc)
                    _sleep_interruptible(1.0, self.state)
        finally:
            log(
                C.SYS002,
                cum=self._runtime_total_cum_successes,
                five=self._runtime_total_five_round_experiments,
                sr=self._runtime_start_click_recovery_count,
                final=1,
            )
            self._scroll_enabled = False
            self._clear_scroll_command()
            self._stop_scroll_worker()
            self._stop_female_bar_monitor()
            self._stop_special_action_monitor()
            self._unregister_hotkeys()
