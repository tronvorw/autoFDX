import json
import shutil
from pathlib import Path

WINDOW_TITLE = "FallenDoll"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_PATH = DATA_DIR / "user_config.json"
ASSETS_DIR = PROJECT_ROOT / "assets"
TEMPLATES_DIR = ASSETS_DIR / "templates"

# ---------------------------------------------------------------------------
# 版本与在线更新（悬浮窗「检查更新」；自动替换依赖 tools/apply_update.py）
# 发布新 tag 时请同步修改 APP_VERSION。
# ---------------------------------------------------------------------------
APP_VERSION = "1.0.0-beta.5"
UPDATE_CHECK_GITHUB_REPO = "tronvorw/autoFDX"
# 当 api.github.com 因「共享出口 IP」匿名限流时，从下列地址拉取清单（与发版 tag 同步维护）。
UPDATE_MANIFEST_FILENAME = "version_manifest.json"
UPDATE_MANIFEST_BRANCHES = ("main", "master")
# 可选：任意 HTTPS 上托管的清单 JSON（格式同 version_manifest.json）。优先于 raw/jsDelivr。
UPDATE_MANIFEST_OVERRIDE_URL = ""

CALIBRATION_ITEMS = [
    ("start", "开始按钮"),
    ("cum2", "高潮按钮"),
    ("cum_single", "单高潮按钮"),
    ("finish", "再来一次按钮"),
    ("experiment_selected_flag", "实验选定标志"),
    ("recover_stamina_button", "恢复体力按钮"),
    # 自动补充体力专用：框1=体力不足时的图标；框2=独立「补充体力」按钮（勿与主流程「恢复体力按钮」混用）。
    ("stamina_insufficient_icon", "体力不足图标"),
    ("stamina_supplement_button", "体力补充按钮（独立）"),
    ("use_gel_confirm", "使用凝胶确认"),
    ("sensitive_progress_bar", "敏感进度条"),
    ("special_action_button", "特殊动作按钮"),
    ("pull_new_experiment_scroll", "拉出新实验滚动"),
    ("experiment_switch", "实验卡片"),
    ("body_part_switch", "身体部位"),
    ("bar_female", "女进度条"),
    ("bar_male", "男进度条"),
    ("scroll_area", "调速区域"),
    ("like1", "用户1"),
    ("like2", "用户2"),
    ("like3", "用户3"),
    ("like4", "点赞用户1"),
    ("like5", "点赞用户2"),
    ("like6", "点赞用户3"),
    # 赞池：圆环（外接矩形标定内接圆），环宽由 like_pool_ring_width_ratio 相对短边调节。
    ("like_pool", "赞池"),
]


def build_default_done_flags():
    return {key: False for key, _ in CALIBRATION_ITEMS}


def build_default_config():
    # 配置集中在一个函数内，便于后续迁移/版本升级。
    return {
        "window_title": WINDOW_TITLE,
        "template_match": {
            "default_threshold": 0.95,
            "scales": [0.8, 0.9, 1.0, 1.1, 1.2],
        },
        "template_thresholds": {
            "start": 0.95,
            "finish": 0.95,
            "cum1": 0.95,
            "cum2": 0.95,
            "cum_single": 0.95,
            "experiment_selected_flag": 0.95,
            "recover_stamina_button": 0.95,
            "stamina_insufficient_icon": 0.95,
            "stamina_supplement_button": 0.95,
            "sensitive_progress_bar": 0.95,
            "special_action_button": 0.95,
            # 身体部位条：用于实验切换时检测「条是否消失」。
            "body_part_switch": 0.95,
        },
        "template_search_margin": 40,
        "template_regions": {},
        "bar_regions": {
            "bar1": [0.07, 0.75, 0.18, 0.79],
            "bar2": [0.07, 0.79, 0.18, 0.83],
        },
        "bar_profiles": {
            "bar1": {"hsv": [0, 200, 200], "tol": [15, 80, 80]},
            "bar2": {"hsv": [0, 200, 200], "tol": [15, 80, 80]},
        },
        "scroll_region": [0.46, 0.42, 0.54, 0.58],
        "safe_move_point": [0.95, 0.92],
        "ui_window_pos": [20, 20],
        "ui_window_size": [780, 200],
        "ui_window_expanded_size": [780, 500],
        "custom_templates": {},
        # “拉出新实验滚动”动作标定结果：
        # - x/y: 归一化鼠标坐标
        # - distance_down: 向下滚动档位（支持小数，1档=10滚轮单位）
        "pull_new_experiment_scroll_action": {
            "x": 0.5,
            "y": 0.5,
            "distance_down": 0.0,
        },
        "like_points": [],
        # 赞池：外接归一化矩形；外圆为矩形内接圆，环宽沿径向 ≈ 比例 × min(宽,高)（圆环区域 = 两圆之间）。
        "like_pool_ring_width_ratio": 0.14,
        # 圆环充能格占比 ≥ 该阈值视为「赞池满」（分段计数+动态圆心，建议 85%~95%，默认 90%）。
        # 主模式：成功点击再来一次后检测一次；单高潮：主循环约每 5 秒检测。
        "like_pool_blue_full_threshold": 0.90,
        # 实验切换网格（3x4，共 12 点）：
        # - experiment_points: 按“从左到右、从上到下”顺序存储 12 个点（归一化坐标）。
        # - current_experiment: 当前实验索引，采用 [行, 列]（1-based）表示。
        "experiment_points": [],
        "current_experiment": [1, 1],
        # 历史字段：六边形实验分类网格已废弃，保留空数据以兼容旧配置。
        "experiment_hex_points": [],
        "current_hex_experiment": [1, 1],
        # 身体部位入口（单行 7 点）：
        # body_part_points 按从左到右保存中心点。
        "body_part_points": [],
        "current_body_part": 1,
        "like_enabled": True,
        "like_force_next": False,
        # 实验切换总开关：
        # False=沿用原有流程；True=运行前先执行“实验切换预处理流程”。
        "experiment_switch_enabled": False,
        # 单高潮模式总开关：
        # False=沿用完整流程；
        # True=仅运行“开始 -> 单高潮按钮 -> 再来一次按钮”三按钮流程。
        "single_cum_mode_enabled": False,
        # 自动补充体力：回合结束、点击「开始」前，若体力显示模板出现则点体力再点凝胶确认。
        "auto_refill_stamina_enabled": False,
        # 女进度条停滞恢复：多次 ESC 仍无法回到开始按钮时，尝试按 J/K 释放游戏卡死态。
        "stall_recovery_rescue_enabled": True,
        "stall_recovery_esc_attempts_before_rescue": 3,
        "stall_recovery_rescue_keys": ["j", "k"],
        "stall_recovery_rescue_mode": "alternate",
        # 特殊动作（主键盘「1」）响应节奏：可在 user_config 中覆盖默认值。
        "special_action_poll_interval_sec": 0.04,
        "special_action_press_delay_sec": 0.06,
        "special_action_key_repeat_count": 3,
        "special_action_key_repeat_interval_sec": 0.08,
        "special_action_trigger_cooldown_sec": 0.55,
        "special_action_sensitive_max": 0.77,
        "special_action_sensitive_rise_min": 0.02,
        "special_action_post_rise_delay_sec": 3.0,
        "special_action_red_threshold": 0.40,
        "special_action_red_blob_min_ratio": 0.22,
        "special_action_sens_ema_alpha": 0.40,
        "special_action_rise_stall_sec": 12.0,
        "special_action_phase_warmup_sec": 1.2,
        "finish_wait_timeout_sec": 20.0,
        "finish_wait_esc_max": 30,
        # 移动视角部署：整圈旋转时长、连点间隔、微步像素、转速倍率。
        "deploy_move_duration_sec": 10.0,
        "deploy_move_click_interval_sec": 0.036,
        "deploy_move_loop_tick_sec": 0.004,
        "deploy_move_max_step_px": 10,
        "deploy_move_poll_interval_sec": 0.08,
        "deploy_move_speed_multiplier": 2.0,
        # 叠加层模式开关（预留）：
        # False=常规悬浮窗；True=用户选择 DX Hook/Overlay 路径（实验项）。
        "overlay_dx_hook_enabled": False,
        # 在线更新通道：release=仅正式版 GitHub Release（及无 Release 时的正式 tag）；beta=含预发布。
        "update_channel": "release",
        "calibration_done": build_default_done_flags(),
        "calibration_rects": {
            "start": [0.35, 0.82, 0.45, 0.89],
            "cum2": [0.45, 0.82, 0.55, 0.89],
            "cum_single": [0.45, 0.82, 0.55, 0.89],
            "finish": [0.55, 0.82, 0.65, 0.89],
            # 实验选定标志：和开始按钮同类，保存模板图与匹配区域。
            "experiment_selected_flag": [0.03, 0.02, 0.10, 0.12],
            # 恢复体力按钮：模板匹配项，保存模板图与匹配区域。
            "recover_stamina_button": [0.82, 0.82, 0.96, 0.94],
            # 体力不足图标：模板匹配，出现时表示需要走自动补充体力流程。
            "stamina_insufficient_icon": [0.82, 0.70, 0.90, 0.78],
            # 独立体力补充按钮：模板匹配，自动补体力时点击此处（非 recover_stamina_button）。
            "stamina_supplement_button": [0.82, 0.62, 0.96, 0.70],
            # 使用凝胶确认：单点（归一化存为零面积矩形中心）。
            "use_gel_confirm": [0.50, 0.50, 0.50, 0.50],
            # 敏感进度条：用于检测红色占比变化。
            "sensitive_progress_bar": [0.40, 0.80, 0.72, 0.90],
            # 特殊动作按钮：用于检测红色占比并执行点击。
            "special_action_button": [0.74, 0.78, 0.92, 0.92],
            # 拉出新实验滚动：以“点位模式”标定，默认放在中心。
            "pull_new_experiment_scroll": [0.50, 0.50, 0.50, 0.50],
            # 实验切换联合标定网的默认包围框（行列均匀分布由 UI 侧计算）。
            "experiment_switch": [0.30, 0.25, 0.70, 0.55],
            # 身体部位标定默认包围框（单行 7 点中心由 UI 侧计算）。
            "body_part_switch": [0.10, 0.10, 0.92, 0.24],
            "bar_female": [0.07, 0.75, 0.18, 0.79],
            "bar_male": [0.07, 0.79, 0.18, 0.83],
            "scroll_area": [0.46, 0.42, 0.54, 0.58],
            "like1": [0.05, 0.15, 0.12, 0.20],
            "like2": [0.05, 0.22, 0.12, 0.27],
            "like3": [0.05, 0.29, 0.12, 0.34],
            "like4": [0.05, 0.36, 0.12, 0.41],
            "like5": [0.05, 0.43, 0.12, 0.48],
            "like6": [0.05, 0.50, 0.12, 0.55],
            "like_pool": [0.40, 0.06, 0.60, 0.20],
        },
    }


class ConfigStore:
    """配置读写与默认值补齐。"""

    def __init__(self):
        self.data = {}

    def load(self):
        self._ensure_asset_dirs()
        # 兼容历史路径：若根目录存在旧 user_config.json，则迁移到 data 目录。
        legacy_config = PROJECT_ROOT / "user_config.json"
        if (not CONFIG_PATH.exists()) and legacy_config.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_config), str(CONFIG_PATH))
        default = build_default_config()
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.data = self._merge(default, loaded)
        else:
            self.data = default
            self.save()
        self._ensure_calibration_keys()
        self._migrate_legacy_assets()
        self.save()
        return self.data

    def save(self):
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def _merge(self, base, override):
        merged = json.loads(json.dumps(base))
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    def _ensure_calibration_keys(self):
        self.data.setdefault("calibration_done", {})
        self.data.setdefault("calibration_rects", {})
        defaults = build_default_config()

        # 兼容旧配置：若历史存在 bar_area，则自动拆为女/男两个进度条区域。
        old_bar_area = self.data["calibration_rects"].get("bar_area")
        if old_bar_area and ("bar_female" not in self.data["calibration_rects"] or "bar_male" not in self.data["calibration_rects"]):
            x1, y1, x2, y2 = old_bar_area
            mid = (y1 + y2) / 2.0
            self.data["calibration_rects"]["bar_female"] = [x1, y1, x2, mid]
            self.data["calibration_rects"]["bar_male"] = [x1, mid, x2, y2]
            old_done = bool(self.data["calibration_done"].get("bar_area", False))
            self.data["calibration_done"]["bar_female"] = old_done
            self.data["calibration_done"]["bar_male"] = old_done

        # 旧版单一「stamina_display」拆成「体力不足图标」+「独立体力补充按钮」；迁移矩形/完成标记/模板路径。
        if "stamina_display" in self.data["calibration_rects"]:
            legacy_rect = self.data["calibration_rects"].pop("stamina_display")
            self.data["calibration_rects"].setdefault("stamina_insufficient_icon", legacy_rect)
        if "stamina_display" in self.data.get("calibration_done", {}):
            legacy_done = bool(self.data["calibration_done"].pop("stamina_display"))
            self.data["calibration_done"].setdefault("stamina_insufficient_icon", legacy_done)
        ct = self.data.setdefault("custom_templates", {})
        if "stamina_display" in ct:
            ct.setdefault("stamina_insufficient_icon", ct.pop("stamina_display"))
        tr = self.data.setdefault("template_regions", {})
        if "stamina_display" in tr:
            tr.setdefault("stamina_insufficient_icon", tr.pop("stamina_display"))
        th = self.data.setdefault("template_thresholds", {})
        if "stamina_display" in th:
            th.setdefault("stamina_insufficient_icon", th.pop("stamina_display"))
        th.setdefault("body_part_switch", 0.95)

        for key, _ in CALIBRATION_ITEMS:
            self.data["calibration_done"].setdefault(key, False)
            self.data["calibration_rects"].setdefault(key, defaults["calibration_rects"][key])

        # 女/男进度条：vision.detect_bars 读 bar_regions；与 calibration_rects 中 bar_female/bar_male 必须一致，避免手工改配置后不同步。
        cr = self.data["calibration_rects"]
        br = self.data.setdefault("bar_regions", defaults["bar_regions"])
        if isinstance(cr.get("bar_female"), list) and len(cr["bar_female"]) == 4:
            br["bar1"] = list(cr["bar_female"])
        if isinstance(cr.get("bar_male"), list) and len(cr["bar_male"]) == 4:
            br["bar2"] = list(cr["bar_male"])

        # 清理已废弃的旧标定项，避免后续维护混淆。
        self.data["calibration_done"].pop("bar_area", None)
        self.data["calibration_rects"].pop("bar_area", None)
        # 实验分类（六边形网格）标定已移除。
        self.data["calibration_done"].pop("experiment_hex_switch", None)
        self.data["calibration_rects"].pop("experiment_hex_switch", None)
        # 清理已废弃点赞重复配置：点赞流程已改为固定节奏，不再使用该字段。
        self.data.pop("like_click_repeat", None)
        # 点赞开关与“下一次立即点赞”标记补默认值。
        self.data.setdefault("like_enabled", True)
        self.data.setdefault("like_force_next", False)
        ui_size = self.data.get("ui_window_size", defaults["ui_window_size"])
        if not (isinstance(ui_size, list) and len(ui_size) == 2):
            ui_size = defaults["ui_window_size"]
        try:
            self.data["ui_window_size"] = [max(460, int(ui_size[0])), max(180, int(ui_size[1]))]
        except Exception:
            self.data["ui_window_size"] = list(defaults["ui_window_size"])
        ui_expanded_size = self.data.get("ui_window_expanded_size", defaults["ui_window_expanded_size"])
        if not (isinstance(ui_expanded_size, list) and len(ui_expanded_size) == 2):
            ui_expanded_size = defaults["ui_window_expanded_size"]
        try:
            self.data["ui_window_expanded_size"] = [
                max(460, int(ui_expanded_size[0])),
                max(320, int(ui_expanded_size[1])),
            ]
        except Exception:
            self.data["ui_window_expanded_size"] = list(defaults["ui_window_expanded_size"])
        # 实验切换总开关补默认值。
        self.data.setdefault("experiment_switch_enabled", False)
        # 单高潮模式开关补默认值。
        self.data.setdefault("single_cum_mode_enabled", False)
        self.data.setdefault("auto_refill_stamina_enabled", False)
        self.data.setdefault("stall_recovery_rescue_enabled", True)
        try:
            self.data["stall_recovery_esc_attempts_before_rescue"] = max(
                1, int(self.data.get("stall_recovery_esc_attempts_before_rescue", 3))
            )
        except Exception:
            self.data["stall_recovery_esc_attempts_before_rescue"] = 3
        rescue_keys = self.data.get("stall_recovery_rescue_keys", ["j", "k"])
        if not isinstance(rescue_keys, list):
            rescue_keys = ["j", "k"]
        rescue_keys = [str(k).strip().lower() for k in rescue_keys if str(k).strip()]
        if not rescue_keys:
            rescue_keys = ["j", "k"]
        self.data["stall_recovery_rescue_keys"] = rescue_keys
        rescue_mode = str(self.data.get("stall_recovery_rescue_mode", "alternate")).strip().lower()
        if rescue_mode not in ("first", "alternate", "random"):
            rescue_mode = "alternate"
        self.data["stall_recovery_rescue_mode"] = rescue_mode
        self.data.setdefault("special_action_poll_interval_sec", 0.04)
        self.data.setdefault("special_action_press_delay_sec", 0.06)
        self.data.setdefault("special_action_key_repeat_count", 3)
        self.data.setdefault("special_action_key_repeat_interval_sec", 0.08)
        self.data.setdefault("special_action_trigger_cooldown_sec", 0.55)
        self.data.setdefault("special_action_sensitive_max", 0.77)
        self.data.setdefault("special_action_sensitive_rise_min", 0.02)
        self.data.setdefault("special_action_post_rise_delay_sec", 3.0)
        self.data.setdefault("special_action_red_threshold", 0.40)
        self.data.setdefault("special_action_red_blob_min_ratio", 0.22)
        self.data.setdefault("special_action_sens_ema_alpha", 0.40)
        self.data.setdefault("special_action_rise_stall_sec", 12.0)
        self.data.setdefault("special_action_phase_warmup_sec", 1.2)
        self.data.setdefault("finish_wait_timeout_sec", 20.0)
        self.data.setdefault("finish_wait_esc_max", 30)
        self.data.setdefault("deploy_move_duration_sec", 10.0)
        self.data.setdefault("deploy_move_click_interval_sec", 0.036)
        self.data.setdefault("deploy_move_loop_tick_sec", 0.004)
        self.data.setdefault("deploy_move_max_step_px", 10)
        self.data.setdefault("deploy_move_poll_interval_sec", 0.08)
        self.data.setdefault("deploy_move_speed_multiplier", 2.0)
        self.data.setdefault("like_pool_ring_width_ratio", 0.14)
        self.data.setdefault("like_pool_blue_full_threshold", 0.90)
        # 旧版默认 80% 在分段计数+圆心学习下偏易误触，自动抬升到 90%。
        try:
            if abs(float(self.data.get("like_pool_blue_full_threshold", 0.90)) - 0.80) < 1e-6:
                self.data["like_pool_blue_full_threshold"] = 0.90
        except Exception:
            self.data["like_pool_blue_full_threshold"] = 0.90
        # 实验切换：补齐当前实验索引与 12 点网格数据。
        self.data.setdefault("current_experiment", [1, 1])
        self.data.setdefault("experiment_points", [])
        # 六边形实验切换：补齐当前索引与中心点网格数据。
        self.data.setdefault("current_hex_experiment", [1, 1])
        self.data.setdefault("experiment_hex_points", [])
        # 身体部位：补齐当前索引与中心点网格数据。
        self.data.setdefault("current_body_part", 1)
        self.data.setdefault("body_part_points", [])
        # 拉出新实验滚动动作：补齐默认动作结构。
        self.data.setdefault(
            "pull_new_experiment_scroll_action",
            {"x": 0.5, "y": 0.5, "distance_down": 0.0},
        )
        # 兼容旧版本字段：direction/distance -> distance_down。
        action = self.data.get("pull_new_experiment_scroll_action", {})
        if isinstance(action, dict):
            if "distance_down" not in action:
                try:
                    old_distance = float(action.get("distance", 0))
                except Exception:
                    old_distance = 0.0
                action["distance_down"] = max(0.0, old_distance)
            else:
                try:
                    action["distance_down"] = max(0.0, float(action.get("distance_down", 0)))
                except Exception:
                    action["distance_down"] = 0.0
            action.pop("direction", None)
            action.pop("distance", None)
            self.data["pull_new_experiment_scroll_action"] = action
        # DX Hook/Overlay 选择项补默认值。
        self.data.setdefault("overlay_dx_hook_enabled", False)
        # 检查更新：Release / Beta 通道。
        ch = self.data.get("update_channel", "release")
        self.data["update_channel"] = ch if ch in ("release", "beta") else "release"

    def _ensure_asset_dirs(self):
        # 固定数据/素材目录，避免散落在项目根目录。
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

    def _migrate_legacy_assets(self):
        # 自动迁移历史素材文件到 assets/templates。
        for name in ("start.png", "cum2.png", "finish.png"):
            src = PROJECT_ROOT / name
            dst = TEMPLATES_DIR / name
            self._move_if_needed(src, dst)

        for src in PROJECT_ROOT.glob("custom_*.png"):
            dst = TEMPLATES_DIR / src.name
            self._move_if_needed(src, dst)

        # 兼容旧配置：custom_templates 里只存文件名时，自动改成素材目录相对路径。
        custom_map = self.data.get("custom_templates", {})
        for key, rel in list(custom_map.items()):
            p = Path(rel)
            if p.parent == Path("."):
                custom_map[key] = str(Path("assets") / "templates" / p.name)
        self.data["custom_templates"] = custom_map

    def _move_if_needed(self, src, dst):
        if not src.exists():
            return
        if dst.exists():
            src.unlink(missing_ok=True)
            return
        shutil.move(str(src), str(dst))
