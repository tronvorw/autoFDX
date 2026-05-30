import random
import threading
from time import monotonic, sleep, time

import keyboard
import pyautogui

# 女进度条停滞：建立基线后，若连续该秒数内女条（b1）无有效增长则触发停滞（秒）。
FEMALE_BAR_STALL_NO_INCREASE_SECONDS = 5.0
# 武装停滞检测后、或 F1 恢复后：该秒内不因“未增长”判停滞（开局/恢复后女条常短暂不动，易误判）。
FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC = 6.0
# 主键盘按「1」触发特殊动作后：至少经过该秒数，才用「模板匹配」判定重新出现。
SPECIAL_ACTION_REAPPEAR_DELAY_AFTER_ONE_SEC = 1.0
# 单高潮模式：赞池蓝色占比检测最小间隔（秒），与「随时随地」轮询并存，避免高频截图。
LIKE_POOL_POLL_INTERVAL_SINGLE_CUM_SEC = 5.0


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
        # 女进度条停滞检测“可恢复最早时间戳”（秒）：保留字段，当前与「等模板再出现」组合使用。
        self._female_bar_stall_suspend_until = 0.0
        # 实验切换：首次部署后 / 每轮点击「开始」后，须先模板匹配到特殊动作按钮存在，再启动女条停滞检测。
        self._female_bar_stall_wait_special_visible_after_start = False
        # 按下主键盘「1」后：延迟 SPECIAL_ACTION_REAPPEAR_DELAY_AFTER_ONE_SEC 秒，再任意一次模板匹配即视为重新出现并启动女条停滞检测。
        self._female_bar_stall_wait_special_reappear = False
        # 按「1」成功时刻起算，在此之前不接受「模板=重新出现」判定（与 press 内 0.2s 延迟区分）。
        self._female_bar_stall_reappear_earliest_ts = 0.0
        # 早于该时刻不判女条停滞（宽限期结束时刻）；用于开局、按1后、F1恢复后防误判。
        self._female_bar_stall_grace_until = 0.0
        # 用于检测「刚从暂停恢复」，恢复时给一段宽限期。
        self._female_bar_monitor_was_paused = False
        # 特殊动作状态机（按新规则）：
        # - 当“敏感进度条<80% 且 特殊动作按钮红色>60%”时，延迟 200ms 触发主键盘“1”；
        # - 每次触发后暂停女条停滞检测，延迟后再用模板匹配判定重新出现。
        # 连续触发节流时间戳：避免条件持续满足时每帧都触发按键。
        self._special_action_last_trigger_ts = 0.0
        self._special_action_monitor_active = False
        self._special_action_monitor_stop = threading.Event()
        self._special_action_monitor_thread = None
        # 特殊动作阶段令牌：主线程进入/离开「开始~高潮」区间时更新，防止子线程在
        # press 的 0.2s 延迟后仍发送「1」（阶段已结束或已暂停时偶发误触）。
        self._special_action_phase_token = 0
        self._special_action_expected_token = 0
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
        extra = ""
        if not bool(self.config_store.data.get("experiment_switch_enabled", False)):
            extra = "（「5回合」计数需开启实验切换）"
        print(
            f"\n[统计] 自程序启动：高潮成功 {self._runtime_total_cum_successes} 次；"
            f"完成「5回合」实验 {self._runtime_total_five_round_experiments} 个（以该组第 5 次成功高潮为准）{extra}"
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
            print(f"\n[赞池] {msg}")
            self.state.log(msg)
            return

        if bool(self.config_store.data.get("like_force_next", False)):
            self.state.log("主模式：like_force_next 强制点赞")
            self.state.set_status("主模式：立即点赞（一次性）")
            print("\n[点赞] 主模式：like_force_next 触发 give()")
            self.actions.give()
            self.config_store.data["like_force_next"] = False
            self.config_store.save()
            return

        if not bool(self.config_store.data.get("calibration_done", {}).get("like_pool")):
            msg = "主模式跳过：赞池未标定"
            print(f"\n[赞池] {msg}")
            self.state.log(msg)
            return
        pts = self.config_store.data.get("like_points", [])
        if not isinstance(pts, list) or len(pts) < 6:
            count = len(pts) if isinstance(pts, list) else 0
            msg = f"主模式跳过：点赞点位不完整（{count}/6）"
            print(f"\n[赞池] {msg}")
            self.state.log(msg)
            return

        ratio = self.vision_service.like_pool_blue_fill_ratio()
        if ratio is None:
            msg = "主模式检测失败：圆环区域无效或截图失败"
            print(f"\n[赞池] {msg}")
            self.state.log(msg)
            return
        th = float(self.config_store.data.get("like_pool_blue_full_threshold", 0.90))
        msg = f"主模式检测：圆环蓝占比 {ratio:.1%}，阈值 {th:.1%}"
        print(f"\n[赞池] {msg}")
        self.state.log(msg)
        if ratio < th:
            return

        msg = f"赞池已满：蓝占比 {ratio:.1%} ≥ 阈值 {th:.1%}，执行点赞（主模式·再来一次后）"
        print(f"\n[赞池] {msg}")
        self.state.log(msg)
        self.state.set_status(f"赞池已满(蓝占比约{ratio:.0%})：执行点赞")
        self.actions.give()

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
        th = float(self.config_store.data.get("like_pool_blue_full_threshold", 0.90))
        if ratio < th:
            self._like_pool_armed_single_cum = True
            return
        if not self._like_pool_armed_single_cum:
            return
        self._like_pool_armed_single_cum = False

        msg = f"赞池已满：蓝占比 {ratio:.1%} ≥ 阈值 {th:.1%}，执行点赞"
        print(f"\n[赞池] {msg}")
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
        print(f"\n[实验切换] 缺少标定项：{', '.join(missing)}，已自动暂停。")
        return False

    def _retry_experiment_panel_and_click_same_card(self, card_index_1based):
        """
        当「点击实验卡片后身体部位条仍未消失」时，不一定是卡槽用尽：
        常见为界面未就绪、动画偏慢、模板瞬时未匹配。
        本函数：ESC 关闭 → 再按 E 打开面板 → 稍等后再次点击同一张卡片。
        返回 True 表示再次点击已执行（不保证身体部位条已消失）。
        """
        pyautogui.press("esc")
        sleep(1.0)
        self.actions.press_experiment_switch_hotkey()
        sleep(1.2)
        return bool(self.actions.click_experiment_card(card_index_1based))

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
                    print(
                        f"\n[实验已用尽] 12张卡片已全部尝试，"
                        f"切换身体部位 {self._current_body_part_index}号→5号，重置实验点位为1。"
                    )
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
                print(
                    "\n[实验已用尽] 身体部位5号上 12 张卡片仍无法确认「身体部位条已消失」。"
                    "若仍有可用实验，请检查「身体部位」模板标定或重试。"
                )
                return False

            self._save_card_index_to_config(self._experiment_card_index)
            # 流程.md 第3条：进入【尝试选定实验】后延时 1s 再点击实验卡片。
            sleep(1.0)
            print(f"\n[实验切换] 尝试选定实验：准备点击实验卡片索引={self._experiment_card_index}。")
            clicked = self.actions.click_experiment_card(self._experiment_card_index)
            if not clicked:
                self.state.manual_pause = True
                self.state.set_status("实验切换失败: 实验卡片点位不可用")
                print(
                    f"\n[实验切换] 已暂停：实验卡片点位不可用，当前索引={self._experiment_card_index}。"
                )
                return False

            # 流程.md 第3条：点击后检测「身体部位条是否消失」（实验选定后该条通常收起/不可见）。
            # 原 2s 偏紧；拉长超时并允许重开面板再点同卡一次。
            sel_confirm_timeout_sec = 3.5
            body_part_hidden = self.actions.wait_until_body_part_switch_hidden(timeout_sec=sel_confirm_timeout_sec)
            if not body_part_hidden:
                print(
                    "\n[实验切换] 首次未在超时内检测到身体部位条消失，"
                    "将重开实验面板并再次点击同一张卡片（避免界面延迟/模板瞬时未匹配误判为已用尽）。"
                )
                if not self._retry_experiment_panel_and_click_same_card(self._experiment_card_index):
                    self.state.manual_pause = True
                    self.state.set_status("实验切换失败: 实验卡片点位不可用")
                    print(
                        f"\n[实验切换] 已暂停：重试时实验卡片点位不可用，当前索引={self._experiment_card_index}。"
                    )
                    return False
                body_part_hidden = self.actions.wait_until_body_part_switch_hidden(timeout_sec=sel_confirm_timeout_sec)

            if not body_part_hidden:
                # 两次尝试后身体部位条仍在：多数为当前卡槽不可用，或「身体部位」模板/阈值需检查。
                if self._current_body_part_index != 5:
                    self.state.set_status("实验已用尽：切换到身体部位5号")
                    print(
                        f"\n[实验已用尽] 卡片{self._experiment_card_index}号经两次检测身体部位条仍未消失，"
                        f"将假定当前身体部位该槽不可用，切换身体部位 {self._current_body_part_index}号→5号，重置实验点位为1。"
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
                print(
                    "\n[实验已用尽] 在身体部位5号上仍无法确认身体部位条消失。"
                    "若你确认仍有可用实验，请检查「身体部位」模板标定与匹配阈值，或适当提高游戏帧率/关闭遮挡。"
                )
                return False

            # ── 流程.md 第4条：【尝试部署实验】──
            self.state.set_status("尝试部署实验")
            # 流程第1步：进入部署阶段后先延时1秒，再点击鼠标左键。
            sleep(1.0)
            start_seen, both_ready = self.actions.deploy_and_check_start_recover(timeout_sec=2.0)
            if both_ready:
                # 部署成功后先等待 1s，再进入正常运行阶段。
                sleep(1.0)
                self._experiment_switch_bootstrapped = True
                self._experiment_cycle_count = 0
                self.state.set_status("实验切换完成")
                print(f"\n[实验切换] 部署成功：实验卡片索引={self._experiment_card_index}。")
                return True
            if start_seen:
                # 开始按钮出现后，必须再满足“恢复体力按钮存在”才算部署成功。
                # 规则：若“恢复体力按钮”不存在，直接判定部署失败并切换下一实验，不做移动视角重试。
                print(
                    f"\n[实验切换] 部署失败：索引={self._experiment_card_index}，"
                    "原因=恢复体力按钮不存在，直接进入【切换下一实验】。"
                )
                self._experiment_card_index += 1
                pyautogui.press("esc")
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
                print("\n[移动视角部署] 开始：5 秒内连续移动视角并左键...")
                self.state.set_status("移动视角部署（5秒连续）")
                mv_start_seen, mv_both = self.actions.move_camera_burst_deploy_check(duration_sec=5.0)
                if mv_both:
                    print("\n[移动视角部署] 成功：开始按钮与恢复体力按钮均通过。")
                    sleep(1.0)
                    self._experiment_switch_bootstrapped = True
                    self._experiment_cycle_count = 0
                    self.state.set_status("实验切换完成")
                    print(f"\n[实验切换] 部署成功：实验卡片索引={self._experiment_card_index}。")
                    return True
                if mv_start_seen:
                    # 规则：恢复体力按钮不存在 -> 直接切换下一实验。
                    print(
                        "\n[移动视角部署] 失败：开始按钮已出现但恢复体力按钮不存在，"
                        "直接进入【切换下一实验】。"
                    )
                    self._experiment_card_index += 1
                    pyautogui.press("esc")
                    sleep(1.0)
                    self.actions.press_experiment_switch_hotkey()
                    sleep(1.0)
                    continue
                # 5 秒内始终未出现开始按钮（或未达到双条件）→ 【切换下一实验】。
                print(
                    f"\n[实验切换] 部署全部失败：索引={self._experiment_card_index}，"
                    "顺延到下一卡片并进入【切换下一实验】。"
                )
                self._experiment_card_index += 1
                pyautogui.press("esc")
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
            print("\n[F1] 当前处于标定模式，忽略恢复请求。")
            return

        self.state.manual_pause = not self.state.manual_pause
        if self.state.manual_pause:
            self.state.set_status("F1紧急暂停")
            print("\n[F1] 已暂停自动流程。")
        else:
            # 主线程里收起展开的子页面，避免遮挡游戏区域、影响模板匹配。
            self.state.collapse_subpanels_request = True
            # 若此前由 F2 声明了“恢复后立即切换下一实验”，
            # 则恢复状态文案要明确提示“下一步会先切换”，避免用户误判脚本会先继续当前实验。
            if self._f2_pending_switch_after_resume:
                self.state.set_status("F1恢复运行，准备切换下一实验")
                print("\n[F1] 已恢复自动流程，将立即切换下一实验。")
            else:
                self.state.set_status("F1恢复运行")
                print("\n[F1] 已恢复自动流程。")

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
            print("\n[F2] 已暂停；当前未开启实验切换，恢复后不会执行切换。")
            return
        self._f2_pending_switch_after_resume = True
        self.state.set_status("F2已暂停，恢复后切换下一实验")
        print("\n[F2] 已暂停，恢复后将立即切换下一实验。")

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
            print("\n[F3] 已忽略：当前未开启点赞功能。")
            return

        next_value = not bool(self.config_store.data.get("like_force_next", False))
        self.config_store.data["like_force_next"] = next_value
        self.config_store.save()
        if next_value:
            self.state.set_status("F3已开启：结束后强制点赞")
            print("\n[F3] 已开启：再来一次成功后强制点赞（一次性）。")
        else:
            self.state.set_status("F3已关闭：结束后强制点赞")
            print("\n[F3] 已关闭：再来一次成功后强制点赞。")

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
            print("\n[F12] 请选择要显示的标定项。")
            return
        if phase == "await_selection":
            print("\n[F12] 等待完成标定项选择。")
            return
        if phase == "ready_to_show":
            selected = list(getattr(self.state, "calibration_overlay_selected_keys", []))
            if not selected:
                # 防御：无选择时回到第一步
                self.state.open_calibration_overlay_selector = True
                self.state.calibration_overlay_phase = "await_selection"
                print("\n[F12] 未选择任何标定项，请先选择。")
                return
            self.state.show_all_calibration_overlay = True
            self.state.calibration_overlay_phase = "showing"
            print("\n[F12] 已显示选定标定叠加层。")
            return
        if phase == "showing":
            self.state.show_all_calibration_overlay = False
            self.state.calibration_overlay_phase = "idle"
            self.state.calibration_overlay_selected_keys = []
            print("\n[F12] 已收起标定叠加层。")
            return
        # 异常状态兜底
        self.state.show_all_calibration_overlay = False
        self.state.open_calibration_overlay_selector = False
        self.state.calibration_overlay_phase = "idle"
        self.state.calibration_overlay_selected_keys = []
        print("\n[F12] 调试状态已重置。")

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
                    print("\n[F2切换] 「恢复体力按钮」已消失，视为已回到大厅侧。")
                return
            if round_idx == 0:
                print(
                    "\n[F2切换] 首次 ESC 后仍可见「恢复体力按钮」，将间歇按 ESC 直至其消失。"
                )
            self.state.set_status("F2切换：ESC 返回中（恢复体力按钮仍可见）")
            pyautogui.press("esc")
            sleep(0.38)

        print(
            f"\n[F2切换] 警告：已额外 ESC {max_extra_esc} 次后「恢复体力按钮」仍可见，继续后续切换。"
        )

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
        self.state.set_status("F2恢复后：切换下一实验")
        pyautogui.press("esc")
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
        pyautogui.press("esc")
        sleep(1.0)
        self.actions.press_experiment_switch_hotkey()
        sleep(1.0)

    def _female_bar_stall_monitor_loop(self):
        """
        流程.md 第8条：正常运行时独立线程监测女进度条是否停滞。
        每 ~0.3s 采样一次 b1；若连续 FEMALE_BAR_STALL_NO_INCREASE_SECONDS 秒内无有效增长则置位停滞标志。
        """
        # 放宽“有增长”的判定门槛：更小的增长也视为有效增长，降低误判停滞概率。
        epsilon = 0.003
        baseline_b1 = None
        baseline_time = None
        while not self._female_bar_monitor_stop.is_set():
            if not self._female_bar_monitor_active:
                baseline_b1 = None
                baseline_time = None
                sleep(0.1)
                continue
            if self.state.manual_pause:
                # 暂停中记下标记，恢复后给宽限期，避免 F1 恢复后立刻误判停滞。
                self._female_bar_monitor_was_paused = True
                baseline_b1 = None
                baseline_time = None
                sleep(0.1)
                continue
            if self._female_bar_monitor_was_paused:
                self._female_bar_monitor_was_paused = False
                self._female_bar_stall_grace_until = time() + FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC
            # 额外条件：特殊动作触发后暂停停滞判断；
            # 直到“特殊动作按键重新出现”后立即恢复。
            now = time()
            if now < self._female_bar_stall_suspend_until or self._female_bar_stall_wait_special_reappear:
                baseline_b1 = None
                baseline_time = None
                sleep(0.1)
                continue
            # 开局：开始按钮已点后，须先模板匹配到特殊动作按钮，再启动停滞计时（存在≠可触发红态）。
            if self._female_bar_stall_wait_special_visible_after_start:
                try:
                    if self.actions.is_special_action_button_present():
                        self._female_bar_stall_wait_special_visible_after_start = False
                        self._female_bar_stall_grace_until = time() + FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC
                        print(
                            f"\n[女进度条监测] 已检测到特殊动作按钮（模板），启动女进度条停滞检测；"
                            f"宽限 {FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC:.0f}s 内不因未增长判停滞。"
                        )
                    else:
                        baseline_b1 = None
                        baseline_time = None
                        sleep(0.1)
                        continue
                except Exception:
                    sleep(0.1)
                    continue
            try:
                screen = self.vision_service.capture_screen()
                b1, _ = self.vision_service.detect_bars(screen)
            except Exception:
                sleep(0.3)
                continue
            if baseline_b1 is None:
                baseline_b1 = b1
                baseline_time = now
            elif b1 > baseline_b1 + epsilon:
                # 有增长：刷新基线
                baseline_b1 = b1
                baseline_time = now
            elif now < self._female_bar_stall_grace_until:
                # 宽限期内：不判停滞；滑动刷新基线，避免宽限刚结束就因旧计时触发。
                baseline_b1 = b1
                baseline_time = now
            elif now - baseline_time >= FEMALE_BAR_STALL_NO_INCREASE_SECONDS:
                print(
                    f"\n[女进度条监测] {FEMALE_BAR_STALL_NO_INCREASE_SECONDS:.0f}s 内未增加（b1={b1:.4f}），触发停滞切换。"
                )
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

    def _special_action_monitor_loop(self):
        """
        特殊动作线程：
        仅在“点击开始后~点击高潮前”激活，持续循环判断并触发主键盘“1”。
        """
        while not self._special_action_monitor_stop.is_set():
            if (not self._special_action_monitor_active) or self.state.manual_pause:
                sleep(0.1)
                continue

            # 按「1」后：延迟 1s 起算，任意一次模板匹配即视为重新出现（与敏感条采样无关）。
            if self._female_bar_stall_wait_special_reappear:
                if self._special_action_should_abort():
                    sleep(0.1)
                    continue
                if time() < self._female_bar_stall_reappear_earliest_ts:
                    sleep(0.1)
                    continue
                if self.actions.is_special_action_button_present():
                    self._female_bar_stall_wait_special_reappear = False
                    self._female_bar_stall_suspend_until = 0.0
                    self._female_bar_stall_grace_until = time() + FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC
                    print(
                        f"\n[特殊动作] 延迟 {SPECIAL_ACTION_REAPPEAR_DELAY_AFTER_ONE_SEC:.0f}s 后检测到特殊按钮（模板），"
                        f"启动女进度条停滞检测；宽限 {FEMALE_BAR_STALL_GRACE_AFTER_ARMING_SEC:.0f}s 内不因未增长判停滞。"
                    )
                sleep(0.1)
                continue

            sensitive_ratio = self.actions.get_sensitive_progress_bar_ratio()
            if sensitive_ratio is None:
                sleep(0.1)
                continue

            # 条件：敏感进度条<80% 且 特殊动作按钮红色占比>60%。
            # 与点击逻辑保持“循环判断”，条件持续满足时可重复触发“1”。
            if sensitive_ratio < 0.80 and self.actions.is_special_action_button_red(threshold=0.60):
                if self._special_action_should_abort():
                    sleep(0.1)
                    continue
                # 触发节流：最多约每 0.8s 触发一次，避免按键洪泛。
                now_ts = time()
                if (now_ts - self._special_action_last_trigger_ts) >= 0.8:
                    if self.actions.press_main_keyboard_one_after_delay(
                        delay_sec=0.2, abort_check=self._special_action_should_abort
                    ):
                        self._special_action_last_trigger_ts = now_ts
                        # 每次触发“1”后，先暂停停滞检测并等待“特殊动作按键重新出现”；
                        # 重新出现后立即恢复女进度条停滞检测。
                        self._female_bar_stall_flag = False
                        self._female_bar_stall_suspend_until = 0.0
                        self._female_bar_stall_wait_special_reappear = True
                        self._female_bar_stall_reappear_earliest_ts = time() + SPECIAL_ACTION_REAPPEAR_DELAY_AFTER_ONE_SEC
                        print(
                            f"\n[特殊动作] 已触发“1”：敏感进度条={sensitive_ratio:.3f}，"
                            f"暂停女进度条停滞检测；{SPECIAL_ACTION_REAPPEAR_DELAY_AFTER_ONE_SEC:.0f}s 后任意模板匹配即恢复。"
                        )
            sleep(0.1)

    def _start_special_action_monitor(self):
        """启动特殊动作线程（守护线程，生命周期跟随主循环）。"""
        if self._special_action_monitor_thread is None or (not self._special_action_monitor_thread.is_alive()):
            self._special_action_monitor_stop.clear()
            self._special_action_monitor_active = False
            self._special_action_last_trigger_ts = 0.0
            self._female_bar_stall_suspend_until = 0.0
            self._female_bar_stall_wait_special_reappear = False
            self._female_bar_stall_reappear_earliest_ts = 0.0
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
            print(f"\n[女进度条停滞] ESC恢复尝试 {attempt}/{esc_attempts}，等待开始/结束按钮。")
            pyautogui.press("esc")
            surface = self._wait_stall_recovery_surface(timeout_sec=3.0)
            if surface in ("start", "finish", "interrupted"):
                return surface

        if not bool(self.config_store.data.get("stall_recovery_rescue_enabled", True)):
            return None

        keys = self._stall_recovery_rescue_keys()
        for rescue_attempt in range(1, len(keys) + 1):
            key = self._next_stall_recovery_rescue_key()
            self.state.set_status(f"女进度条停滞：ESC失败，尝试按 {key.upper()}")
            print(
                f"\n[女进度条停滞] {esc_attempts} 次 ESC 后仍未出现开始/结束按钮，"
                f"尝试按 {key.upper()} 释放卡死态（{rescue_attempt}/{len(keys)}）。"
            )
            pyautogui.press(key)
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
        print("\n[女进度条停滞] 执行恢复：多次 ESC → 必要时 J/K 释放 → 等待开始按钮 → 双条近似相等后点击开始。")
        self.state.set_status("女进度条停滞：恢复中")
        # 停滞恢复场景单独放宽判定：按需求固定使用 20% 容差。
        near_equal_tolerance = max(float(bar_balance_tolerance), 0.20)

        surface = self._escape_or_rescue_stall_recovery_surface()
        if surface == "interrupted":
            return False
        if surface is None:
            self.state.manual_pause = True
            self.state.set_status("女进度条停滞：恢复失败，已暂停")
            print("\n[女进度条停滞] 恢复失败：多次 ESC 与 J/K 后仍未出现开始/结束按钮，已暂停。")
            return False
        if surface == "finish":
            self.state.set_status("女进度条停滞：点击结束后等待开始")
            if not self._click_finish_until_start_after_stall_rescue():
                self.state.manual_pause = True
                self.state.set_status("女进度条停滞：结束后未出现开始，已暂停")
                print("\n[女进度条停滞] 已尝试点击结束，但未等到开始按钮，已暂停。")
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
                b1, b2 = self.vision_service.detect_bars(self.vision_service.capture_screen())
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
        while self.actions.ready_to_start():
            if self._wait_if_paused_or_interrupted():
                return False
            clicked = self.actions.start()
            if not clicked:
                # 去抖点击未达成“按钮稳定消失”时，不推进后续动作，避免误进入下一阶段。
                self.state.log("停滞恢复：开始按钮点击未确认，重试")
                sleep(0.12)
                continue
            self.state.log("停滞恢复：点击开始")
            x, y = self.window_service.denormalize_point(self.config_store.data.get("safe_move_point", [0.95, 0.92]))
            pyautogui.moveTo(x, y)
            sleep(0.2)

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
                    print("\n[实验切换] 点位号=12，执行【当页实验全部完成】：ESC → E → 拉出新实验滚动 → 点位置9。")
                    pyautogui.press("esc")
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
                    print(f"\n[实验切换] 点位号={self._experiment_card_index}，执行【切换下一实验】。")
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

        while self.actions.ready_to_start():
            if self._wait_if_paused_or_interrupted():
                return
            clicked = self.actions.start()
            if not clicked:
                # 关键：若本轮未确认成功（按钮未稳定消失），留在当前阶段继续重试。
                # 这样可抑制“模板瞬时抖动 -> 误判已点击 -> 流程跳转”的问题。
                self.state.log("开始按钮点击未确认，重试")
                sleep(0.12)
                continue
            self.state.log("点击开始")
            x, y = self.window_service.denormalize_point(self.config_store.data.get("safe_move_point", [0.95, 0.92]))
            pyautogui.moveTo(x, y)
            sleep(0.2)

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
            self._female_bar_stall_wait_special_reappear = False
            self._female_bar_stall_reappear_earliest_ts = 0.0
            # 新一轮「开始~高潮」阶段：刷新令牌，使旧线程中待发送的「1」全部失效。
            self._special_action_phase_token += 1
            self._special_action_expected_token = self._special_action_phase_token
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
                        b1, b2 = self.vision_service.detect_bars(self.vision_service.capture_screen())
                        # EMA：用平滑后的填充率算 diff，抑制单帧跳变导致的纠偏方向抖动。
                        if bar_fill_ema_b1 is None:
                            bar_fill_ema_b1, bar_fill_ema_b2 = b1, b2
                        else:
                            a = bar_fill_ema_alpha
                            bar_fill_ema_b1 = a * b1 + (1.0 - a) * bar_fill_ema_b1
                            bar_fill_ema_b2 = a * b2 + (1.0 - a) * bar_fill_ema_b2
                        diff = bar_fill_ema_b1 - bar_fill_ema_b2
                        if self.state.debug:
                            print(
                                f"\n[bar] raw f={b1:.4f} m={b2:.4f} | "
                                f"ema f={bar_fill_ema_b1:.4f} m={bar_fill_ema_b2:.4f} diff={diff:.4f}"
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
                                    print(f"[bar] action=scroll_up sign=+ (female ahead) count={scroll_count}")
                                self._set_scroll_command(+360, scroll_count)
                            else:
                                # 女进度条 < 男进度条：原方向取反后使用 -360。
                                if self.state.debug:
                                    print(f"[bar] action=scroll_down sign=- (female behind) count={scroll_count}")
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

        while not self.actions.ready_to_finish():
            if self._wait_if_paused_or_interrupted():
                return
            self.state.log("等待结束")
            self.actions.wait(0.2)

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
                    print(f"\n发生异常：{exc}")
                    _sleep_interruptible(1.0, self.state)
        finally:
            print(
                f"\n[统计] 本次运行结束汇总：高潮成功共 {self._runtime_total_cum_successes} 次；"
                f"完成「5回合」实验 {self._runtime_total_five_round_experiments} 个（以各组第 5 次成功高潮计）。"
            )
            self._scroll_enabled = False
            self._clear_scroll_command()
            self._stop_scroll_worker()
            self._stop_female_bar_monitor()
            self._stop_special_action_monitor()
            self._unregister_hotkeys()
