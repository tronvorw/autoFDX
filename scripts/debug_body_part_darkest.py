"""一次性脚本：对 custom_body_part_switch.png 按配置推算七格亮度并打印最暗索引。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autofdx.body_part_hex import analyze_template_png_with_config  # noqa: E402


def main():
    cfg_path = ROOT / "data" / "user_config.json"
    png = ROOT / "assets" / "templates" / "custom_body_part_switch.png"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    pts = cfg.get("body_part_points", [])
    r = float(cfg.get("body_part_hex_radius_px", 20))
    tr = cfg.get("template_regions", {}).get("body_part_switch")
    gw, gh = 1920, 1080
    if not tr or len(pts) != 7 or not png.exists():
        print("需要 data/user_config.json 中 body_part_points×7、template_regions.body_part_switch 及模板 PNG")
        return 1
    idx, lumas = analyze_template_png_with_config(
        png, pts, r, tr, game_w=gw, game_h=gh, min_spread=5.0
    )
    print("最暗格(1~7):", idx)
    print("各格平均亮度:", [round(x, 1) for x in lumas])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
