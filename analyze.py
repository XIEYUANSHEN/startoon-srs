# -*- coding: utf-8 -*-
"""HSR Toon — 像素分析 + 评估规则引擎。
Evaluator 是可扩展接口：RuleEvaluator 是纯规则默认实现，未来可替换为模型实现。
每条规则一行阈值判断，简单到小模型都能写。"""
import bpy
import os
import statistics


# ── 可扩展评估器接口 ──────────────────────────────────
class Evaluator:
    """评估器接口。规则引擎是默认实现，未来可替换为模型实现。"""

    def evaluate(self, metrics, config):
        """返回 [(param_name, new_value, reason), ...] 调整建议列表。"""
        raise NotImplementedError


class RuleEvaluator(Evaluator):
    """纯规则实现。每条规则一个阈值判断，不依赖任何 AI 模型。"""

    def evaluate(self, metrics, config):
        suggestions = []

        # 规则1：黑色像素太多 → UV 可能取错层
        if metrics["dark_ratio"] > 0.30:
            suggestions.append((
                "uv_source", "UVMap",
                "黑色区域过多(%d%%)，UV 可能取错层" % int(metrics["dark_ratio"] * 100),
            ))

        # 规则2：最亮值太低 + 黑色多 → 贴图可能没加载
        if metrics["bright_max"] < 0.30 and metrics["dark_ratio"] > 0.50:
            suggestions.append((
                "uv_source", "UVMap",
                "最亮值仅 %.2f，贴图可能未正确采样" % metrics["bright_max"],
            ))

        # 规则3：色彩太低 + AgX 开启 → AgX 压灰了贴图
        if metrics["colorful_ratio"] < 0.03 and config.get("use_agx"):
            suggestions.append((
                "use_agx", False,
                "AgX 压灰了贴图色彩，切回 Standard",
            ))

        return suggestions


# 默认评估器实例（可被外部替换）
_default_evaluator = RuleEvaluator()


def get_evaluator():
    """返回当前评估器。外部可替换为自定义实现（如模型评估器）。"""
    return _default_evaluator


def set_evaluator(evaluator):
    """替换默认评估器。用于未来接入小模型。"""
    global _default_evaluator
    _default_evaluator = evaluator


# ── 像素分析 ──────────────────────────────────────────
def analyze_image(image_path):
    """分析一张渲染图，返回 metrics dict。"""
    img = bpy.data.images.load(image_path)
    try:
        total = len(img.pixels)
        try:
            px = [0.0] * total
            img.pixels.foreach_get(px)   # 批量拷贝(C 层), 远快于 list(img.pixels)
        except Exception:
            px = list(img.pixels)
    finally:
        bpy.data.images.remove(img)

    if not px:
        return {"mean_brightness": 0, "dark_ratio": 1, "bright_max": 0,
                "cf_mean": 0, "cf_max": 0, "colorful_ratio": 0}

    npix = total // 4
    stride = max(1, npix // 8000)   # 采样 ~8000 点, 避免全像素 Python 循环
    bright_vals = []
    cf_vals = []
    dark_count = 0
    colorful_count = 0
    total_s = 0

    for i in range(0, total, 4 * stride):
        r, g, b = px[i], px[i + 1], px[i + 2]
        bright = (r + g + b) / 3
        cf = max(abs(r - g), abs(g - b), abs(r - b))
        bright_vals.append(bright)
        cf_vals.append(cf)
        total_s += 1
        if bright < 0.05:
            dark_count += 1
        if cf > 0.1:
            colorful_count += 1

    return {
        "mean_brightness": statistics.mean(bright_vals) if bright_vals else 0,
        "dark_ratio": dark_count / total_s if total_s else 1,
        "bright_max": max(bright_vals) if bright_vals else 0,
        "cf_mean": statistics.mean(cf_vals) if cf_vals else 0,
        "cf_max": max(cf_vals) if cf_vals else 0,
        "colorful_ratio": colorful_count / total_s if total_s else 0,
    }


def render_and_analyze(scene, resolution=480, filepath=None):
    """渲染一帧低分辨率预览并分析，返回 (metrics, image_path)。
    保存原分辨率设置，渲染后恢复。"""
    # 保存原设置
    orig_res_x = scene.render.resolution_x
    orig_res_y = scene.render.resolution_y
    orig_filepath = scene.render.filepath

    # 设低分辨率
    aspect = orig_res_y / orig_res_x if orig_res_x else 0.75
    scene.render.resolution_x = resolution
    scene.render.resolution_y = int(resolution * aspect)

    if filepath is None:
        import tempfile
        filepath = os.path.join(tempfile.gettempdir(), "hsr_analyze.png")

    scene.render.filepath = filepath
    bpy.ops.render.render(write_still=True)

    # 恢复原设置
    scene.render.resolution_x = orig_res_x
    scene.render.resolution_y = orig_res_y
    scene.render.filepath = orig_filepath

    metrics = analyze_image(filepath)
    return metrics, filepath


def format_metrics(metrics):
    """把 metrics 格式化成 UI 可显示的文本。"""
    lines = []
    lines.append("亮度: %.3f" % metrics["mean_brightness"])
    lines.append("黑色: %d%%" % int(metrics["dark_ratio"] * 100))
    lines.append("最亮: %.2f" % metrics["bright_max"])
    lines.append("色彩: %.3f" % metrics["cf_mean"])
    lines.append("高饱和: %d%%" % int(metrics["colorful_ratio"] * 100))
    return "\n".join(lines)


def format_suggestions(suggestions):
    """把建议列表格式化成 UI 可显示的文本。"""
    if not suggestions:
        return "无需调整，参数已合理。"
    lines = []
    for param, value, reason in suggestions:
        lines.append("%s → %s（%s）" % (param, value, reason))
    return "\n".join(lines)


def apply_suggestions(scene, suggestions):
    """把建议写回 scene.hsr_toon。返回是否改了任何值。"""
    props = scene.hsr_toon
    changed = False
    for param, value, _reason in suggestions:
        if hasattr(props, param):
            setattr(props, param, value)
            changed = True
    return changed
