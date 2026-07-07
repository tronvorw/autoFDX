import threading
from time import time

from .run_log import log_status


class RuntimeState:
    """运行态：流程状态、暂停/停止控制、循环计数。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.i = 0
        self.debug = False
        self.info = "init"
        self.mark = "+"
        self.cum_mode = 2
        self.op_time = time()
        self.current_status = "init"
        # 画面页面推断（主页 / 非主页），由 automation 线程周期性更新。
        self.scene_label = "—"
        self.scene_matched_templates = []
        # 启动默认暂停，避免脚本一打开就接管鼠标。
        self.manual_pause = True
        self.stop_requested = False
        self.pending_calibration = None
        # 标记“刚完成标定，等待用户点击继续运行”。
        self.calibration_updated = False
        # F1 恢复运行时在后台线程置位，由悬浮窗 refresh 在主线程收起子页面（避免与 Tk 线程冲突）。
        self.collapse_subpanels_request = False
        # F12 调试开关：绘制所有标定窗口叠加层。
        self.show_all_calibration_overlay = False
        # F12 调试流程状态机：
        # idle -> await_selection -> ready_to_show -> showing -> idle
        self.calibration_overlay_phase = "idle"
        # UI 侧收到该标记后弹出“选择要显示的标定项”窗口。
        self.open_calibration_overlay_selector = False
        # F12 选中的标定项 key 列表，仅用于调试显示。
        self.calibration_overlay_selected_keys = []

    def set_status(self, text):
        self.current_status = text

    def should_interrupt(self):
        return self.manual_pause or self.pending_calibration is not None or self.stop_requested

    def switch_mark(self):
        self.mark = "-" if self.mark == "+" else "+"

    def log(self, buf):
        # 复用原打印逻辑，避免用户使用习惯变化。
        self.set_status(buf)
        # 注意：禁止在此处注入任何真实按键（例如 pyautogui.press("1")）。
        # 旧版曾用「长时间重复同一条日志则按 1」做调试，会导致未进入主流程、
        # 甚至未点击「继续」时仍偶发按键，与业务完全无关。
        if buf != self.info:
            self.op_time = time()
            self.info = buf
            log_status(buf)
        else:
            print(f"\r{self.mark}", end="")
            self.switch_mark()
