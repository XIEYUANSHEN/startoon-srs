# -*- coding: utf-8 -*-
"""StarToon SRS — 仿崩铁风格 MMD 三渲二插件。

架构:
- 虚拟日光: HSR_SunLight 灯光对象控制阴影方向(旋转即调)。明暗 Fac = dot(着色法线, sun_dir),
  sun_dir 取灯光 world +Z, 经 depsgraph 处理器写入 driver_namespace, 组内 SunDir 节点用
  SCRIPTED 驱动读取(取代旧的 Diffuse→ShaderToRGB, 后者在 EEVEE5.2 不吃方向 → 一片死平)。
- EASE 多级阴影: 多级 EASE 渐变带; 阴影边界越大暗部越多, 阴影宽度越小色带越硬。
- 分类独立参数: 皮肤/面部/头发/衣服/眼部 各一套完整参数(独立默认值)
- 亮度: Driver 接灯光能量, 保底不黑死
"""
import json

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    StringProperty,
    PointerProperty,
)

from . import shader
from . import analyze
from . import preset
from . import pmx_morph

bl_info = {
    "name": "StarToon SRS",
    "author": "AtomCode",
    "version": (1, 7, 2),
    "blender": (4, 2, 0),
    "location": "3D View > N > HSR Toon",
    "description": "仿崩铁风格 MMD 三渲二: 分类独立参数 + EASE 多级阴影 + 反法线描边 + AOV 眼透",
    "category": "Rendering",
}


def _auto_update(self, context):
    """属性变化时自动更新材质节点（实时预览）。"""
    try:
        shader.update_toon_params(context)
    except Exception:
        pass


def _update_eye_shadow(self, context):
    """目影滑块: 实时写目影材质的形态显示值(泛用: 匹配所有含"目影"的材质)。"""
    for mat in bpy.data.materials:
        if "目影" not in mat.name or not mat.node_tree:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "VALUE" and node.label in ("形态显示", "头纱显示"):
                node.outputs["Value"].default_value = self.eye_shadow_alpha


def _update_eye_through(self, context):
    """眼透滑块: factor = 透明 × strength(参考文件=0.5)。拖动即时写合成器"眼透强度"节点。"""
    cng = getattr(context.scene, 'compositing_node_group', None)
    if cng:
        for n in cng.nodes:
            if n.label == "眼透强度" and n.type == 'MATH':
                n.inputs[1].default_value = round(self.eye_through_strength, 3)


def _update_eye_radius(self, context):
    """眼透半径: 刷新合成器 透明 遮罩的模糊节点尺寸(radius×500→像素)。"""
    cng = getattr(context.scene, 'compositing_node_group', None)
    blur_px = max(0, int(round(self.eye_through_radius * 500)))
    if cng:
        found = False
        for n in cng.nodes:
            if n.label == "眼透半径" and n.type == 'BLUR':
                found = True
                try:
                    s = n.inputs["Size"]
                    try:
                        s.default_value = blur_px
                    except Exception:
                        s.default_value = (blur_px, blur_px)
                except Exception:
                    pass
        if not found and blur_px > 0:
            # 旧链没有模糊节点 → 拆了重建一次
            try:
                context.scene.compositing_node_group = None
                shader.setup_eye_through_compositor(context.scene)
            except Exception:
                pass


def _update_eye_gate_angle(self, context):
    """眼透门控角度滑块: threshold = cos(半角) → 写入眼材质 GateThreshold 节点。"""
    try:
        shader.update_eye_gate_angle(context.scene)
    except Exception:
        pass


def _update_glare(self, context):
    """辉光开关/强度变化 → 重建合成器链(建/拆 Glare 节点)。"""
    try:
        scene = context.scene
        if getattr(scene, "compositing_node_group", None) is not None:
            scene.compositing_node_group = None
            shader.setup_eye_through_compositor(scene)
    except Exception:
        pass


def _on_outline_update(self, context):
    """描边粗细变化 → 实时写 mmd Solidify 壳 thickness + 同步颜色(不重建)。"""
    try:
        shader.refresh_mmd_edge_thickness(context.scene)
        shader.refresh_mmd_edge_color(context.scene)
    except Exception:
        pass


def _on_outline_enabled_toggle(self, context):
    """描边开关 → 开: 建 mmd_tools Solidify 壳; 关: 删除壳。"""
    try:
        scene = context.scene
        if getattr(scene.hsr_toon, "outline_enabled", True):
            shader.apply_mmd_edge(scene)
        else:
            shader.clear_mmd_edge(scene)
    except Exception as exc:
        print("[StarToon] 描边开关失败:", exc)


def _on_outline_color_mode(self, context):
    """描边色彩模式(跟随PMX分色/全局单色)变化 → 刷新描边颜色。"""
    try:
        shader.refresh_mmd_edge_color(context.scene)
    except Exception:
        pass


def _on_toon_shadow_cull(self, context):
    """阴影剔除(AutoCel)开关 → 给已转换材质插/摘 mixin, 不重建材质链。"""
    try:
        shader.refresh_toon_shadow_cull(context.scene)
    except Exception:
        pass


def _on_outline_color(self, context):
    """仅描边颜色变化 → 只刷 mmd Solidify 壳材质颜色(轻量, 不重建几何)。"""
    try:
        shader.refresh_mmd_edge_color(context.scene)
    except Exception:
        pass


def _on_outline_region_update(self, context):
    """分段粗细(发/脸/肤/衣倍率)变化 → 防抖重算 mmd_edge_preview 权重(=原生×倍率)。"""
    try:
        shader.schedule_region_refresh(context.scene)
    except Exception:
        pass


class HSRSkinProps(bpy.types.PropertyGroup):
    pinkness: FloatProperty(name="粉嫩值", default=0.7, update=_auto_update)
    light_saturation: FloatProperty(name="明部饱和度", default=1.2, update=_auto_update)
    light_hue_shift: FloatProperty(name="明部色相", default=0.5, update=_auto_update)
    light_value: FloatProperty(name="明部明度", default=1.1, update=_auto_update)
    light_gamma: FloatProperty(name="明部伽玛", default=0.8, update=_auto_update)
    light_contrast: FloatProperty(name="明部对比度", default=0.2, update=_auto_update)
    dark_factor: FloatProperty(name="暗度系数", default=0.55, update=_auto_update)
    dark_saturation: FloatProperty(name="暗部饱和度", default=1.1, update=_auto_update)
    dark_hue_shift: FloatProperty(name="暗部色相", default=0.5, update=_auto_update)
    dark_gamma: FloatProperty(name="暗部伽玛", default=1.0, update=_auto_update)
    dark_contrast: FloatProperty(name="暗部对比度", default=0.0, update=_auto_update)
    shadow_boundary: FloatProperty(name="阴影分界", default=0.5, min=0.05, max=1.0, step=0.01, update=_auto_update)
    shadow_width: FloatProperty(name="阴影过渡宽度", default=0.05, min=0.0, max=0.5, step=0.01, update=_auto_update)
    shadow_interpolation: EnumProperty(
        name="阴影插值",
        items=[("EASE","缓动",""),("LINEAR","线性",""),("CONSTANT","硬切","")],
        default="EASE", update=_auto_update,
    )


class HSRFaceProps(bpy.types.PropertyGroup):
    pinkness: FloatProperty(name="粉嫩值", default=1.58, update=_auto_update)
    light_saturation: FloatProperty(name="明部饱和度", default=1.2, update=_auto_update)
    light_hue_shift: FloatProperty(name="明部色相", default=0.5, update=_auto_update)
    light_value: FloatProperty(name="明部明度", default=1.1, update=_auto_update)
    light_gamma: FloatProperty(name="明部伽玛", default=0.8, update=_auto_update)
    light_contrast: FloatProperty(name="明部对比度", default=0.2, update=_auto_update)
    dark_factor: FloatProperty(name="暗度系数", default=0.79, update=_auto_update)
    dark_saturation: FloatProperty(name="暗部饱和度", default=1.093, update=_auto_update)
    dark_hue_shift: FloatProperty(name="暗部色相", default=0.48, update=_auto_update)
    dark_gamma: FloatProperty(name="暗部伽玛", default=1.12, update=_auto_update)
    dark_contrast: FloatProperty(name="暗部对比度", default=0.3, update=_auto_update)
    shadow_boundary: FloatProperty(name="阴影分界", default=0.5, min=0.05, max=1.0, step=0.01, update=_auto_update)
    shadow_width: FloatProperty(name="阴影过渡宽度", default=0.05, min=0.0, max=0.5, step=0.01, update=_auto_update)
    shadow_interpolation: EnumProperty(
        name="阴影插值",
        items=[("EASE","缓动",""),("LINEAR","线性",""),("CONSTANT","硬切","")],
        default="LINEAR", update=_auto_update,
    )


class HSRHairProps(bpy.types.PropertyGroup):
    pinkness: FloatProperty(name="粉嫩值", default=0.3, update=_auto_update)
    light_saturation: FloatProperty(name="明部饱和度", default=1.2, update=_auto_update)
    light_hue_shift: FloatProperty(name="明部色相", default=0.5, update=_auto_update)
    light_value: FloatProperty(name="明部明度", default=1.1, update=_auto_update)
    light_gamma: FloatProperty(name="明部伽玛", default=0.8, update=_auto_update)
    light_contrast: FloatProperty(name="明部对比度", default=0.2, update=_auto_update)
    dark_factor: FloatProperty(name="暗度系数", default=0.55, update=_auto_update)
    dark_saturation: FloatProperty(name="暗部饱和度", default=1.1, update=_auto_update)
    dark_hue_shift: FloatProperty(name="暗部色相", default=0.5, update=_auto_update)
    dark_gamma: FloatProperty(name="暗部伽玛", default=1.0, update=_auto_update)
    dark_contrast: FloatProperty(name="暗部对比度", default=0.0, update=_auto_update)
    shadow_boundary: FloatProperty(name="阴影分界", default=0.5, min=0.05, max=1.0, step=0.01, update=_auto_update)
    shadow_width: FloatProperty(name="阴影过渡宽度", default=0.05, min=0.0, max=0.5, step=0.01, update=_auto_update)
    shadow_interpolation: EnumProperty(
        name="阴影插值",
        items=[("EASE","缓动",""),("LINEAR","线性",""),("CONSTANT","硬切","")],
        default="EASE", update=_auto_update,
    )


class HSRClothProps(bpy.types.PropertyGroup):
    pinkness: FloatProperty(name="粉嫩值", default=0.3, update=_auto_update)
    light_saturation: FloatProperty(name="明部饱和度", default=1.2, update=_auto_update)
    light_hue_shift: FloatProperty(name="明部色相", default=0.5, update=_auto_update)
    light_value: FloatProperty(name="明部明度", default=1.1, update=_auto_update)
    light_gamma: FloatProperty(name="明部伽玛", default=0.8, update=_auto_update)
    light_contrast: FloatProperty(name="明部对比度", default=0.2, update=_auto_update)
    dark_factor: FloatProperty(name="暗度系数", default=0.40, update=_auto_update)
    dark_saturation: FloatProperty(name="暗部饱和度", default=1.1, update=_auto_update)
    dark_hue_shift: FloatProperty(name="暗部色相", default=0.5, update=_auto_update)
    dark_gamma: FloatProperty(name="暗部伽玛", default=1.0, update=_auto_update)
    dark_contrast: FloatProperty(name="暗部对比度", default=0.20, update=_auto_update)
    shadow_boundary: FloatProperty(name="阴影分界", default=0.5, min=0.05, max=1.0, step=0.01, update=_auto_update)
    shadow_width: FloatProperty(name="阴影过渡宽度", default=0.05, min=0.0, max=0.5, step=0.01, update=_auto_update)
    shadow_interpolation: EnumProperty(
        name="阴影插值",
        items=[("EASE","缓动",""),("LINEAR","线性",""),("CONSTANT","硬切","")],
        default="EASE", update=_auto_update,
    )


class HSREyeProps(bpy.types.PropertyGroup):
    pinkness: FloatProperty(name="粉嫩值", default=0.5, update=_auto_update)
    light_saturation: FloatProperty(name="明部饱和度", default=1.2, update=_auto_update)
    light_hue_shift: FloatProperty(name="明部色相", default=0.5, update=_auto_update)
    light_value: FloatProperty(name="明部明度", default=1.1, update=_auto_update)
    light_gamma: FloatProperty(name="明部伽玛", default=0.8, update=_auto_update)
    light_contrast: FloatProperty(name="明部对比度", default=0.2, update=_auto_update)
    dark_factor: FloatProperty(name="暗度系数", default=0.55, update=_auto_update)
    dark_saturation: FloatProperty(name="暗部饱和度", default=1.1, update=_auto_update)
    dark_hue_shift: FloatProperty(name="暗部色相", default=0.5, update=_auto_update)
    dark_gamma: FloatProperty(name="暗部伽玛", default=1.0, update=_auto_update)
    dark_contrast: FloatProperty(name="暗部对比度", default=0.0, update=_auto_update)
    shadow_boundary: FloatProperty(name="阴影分界", default=0.5, min=0.05, max=1.0, step=0.01, update=_auto_update)
    shadow_width: FloatProperty(name="阴影过渡宽度", default=0.05, min=0.0, max=0.5, step=0.01, update=_auto_update)
    shadow_interpolation: EnumProperty(
        name="阴影插值",
        items=[("EASE","缓动",""),("LINEAR","线性",""),("CONSTANT","硬切","")],
        default="EASE", update=_auto_update,
    )


# 用于 shader.py 的 get_category_config 映射
CATEGORY_CLASSES = {
    "skin": "skin", "face": "face", "hair": "hair",
    "cloth": "cloth", "skirt": "cloth", "shoe": "cloth",
    "metal": "cloth", "eye": "eye", "default": "cloth",
}

# 默认值表(reset 用)
CATEGORY_DEFAULTS = {
    "skin":  {"pinkness":0.7,  "dark_factor":0.55, "dark_saturation":1.1, "dark_hue_shift":0.5, "dark_gamma":1.0, "dark_contrast":0.0, "shadow_boundary":0.5, "shadow_width":0.05, "shadow_interpolation":"EASE"},
    "face":  {"pinkness":1.58, "dark_factor":0.79, "dark_saturation":1.093, "dark_hue_shift":0.48, "dark_gamma":1.12, "dark_contrast":0.3, "shadow_boundary":0.5, "shadow_width":0.05, "shadow_interpolation":"LINEAR"},
    "hair":  {"pinkness":0.3,  "dark_factor":0.55, "dark_saturation":1.1, "dark_hue_shift":0.5, "dark_gamma":1.0, "dark_contrast":0.0, "shadow_boundary":0.5, "shadow_width":0.05, "shadow_interpolation":"EASE"},
    "cloth": {"pinkness":0.3,  "dark_factor":0.40, "dark_saturation":1.1, "dark_hue_shift":0.5, "dark_gamma":1.0, "dark_contrast":0.20, "shadow_boundary":0.5, "shadow_width":0.05, "shadow_interpolation":"EASE"},
    "eye":   {"pinkness":0.5,  "dark_factor":0.55, "dark_saturation":1.1, "dark_hue_shift":0.5, "dark_gamma":1.0, "dark_contrast":0.0, "shadow_boundary":0.5, "shadow_width":0.05, "shadow_interpolation":"EASE"},
}
# 通用参数默认值(明部固定)
COMMON_DEFAULTS = {
    "light_saturation":1.2, "light_hue_shift":0.5, "light_value":1.1,
    "light_gamma":0.8, "light_contrast":0.2,
}


import re as _re


def _glow_group_key(name):
    """分组键: 去掉尾部 .NNN 和 +, 让 发饰叶 / 发饰叶+ / 发饰叶.001 归为一条(正反/morph 变体共用)。"""
    n = _re.sub(r'\.\d+$', '', name)
    n = _re.sub(r'\++$', '', n)
    return n


def _glow_update(self, context):
    """发光列表项勾选/强度变化 → 对该分组下所有材质施加/取消自发光。"""
    try:
        for m in bpy.data.materials:
            if m.get("hsr_stage") != "virtual_sun":
                continue
            if _glow_group_key(m.name) == self.base:
                shader.set_glow(m, self.on, self.strength)
    except Exception:
        pass


class HSRGlowItem(bpy.types.PropertyGroup):
    base: StringProperty()               # 分组显示名
    on: BoolProperty(name="亮", default=False, update=_glow_update)
    strength: FloatProperty(name="强度", default=0.5, min=0.0, max=8.0,
                            soft_min=0.0, soft_max=4.0, update=_glow_update)
    has_sphere: BoolProperty(name="球")   # 该组是否带 sphere(matcap 已自动处理, 仅标记)


def rebuild_glow_list(scene):
    """列出所有已转换材质(按分组), 不采样像素 → 快。重转时保留已勾选/强度状态。"""
    props = getattr(scene, "hsr_toon", None)
    if props is None:
        return
    # 快照旧状态(按分组名), 重建后回填 → 连点转换不丢勾选
    prev = {it.base: (it.on, it.strength) for it in props.glow_items}
    props.glow_items.clear()
    groups = {}
    for m in bpy.data.materials:
        if m.get("hsr_stage") != "virtual_sun" or not m.node_tree:
            continue
        k = _glow_group_key(m.name)
        hs = m.node_tree.nodes.get("mmd_sphere_tex") is not None
        groups[k] = groups.get(k, False) or hs
    for k in sorted(groups):
        it = props.glow_items.add()
        it.base = k
        it.has_sphere = groups[k]
        if k in prev:
            it.on, it.strength = prev[k]        # 保留(不触发重施加, 材质节点本就在)
        else:
            it.on = False
            it.strength = 0.5


class HSRToonProperties(bpy.types.PropertyGroup):
    """全局参数 + 分类参数集合。"""

    key_energy: FloatProperty(name="灯光能量", default=0.87, min=0.0, update=_auto_update)
    brightness_scale: FloatProperty(name="亮度缩放", default=0.15, update=_auto_update)
    brightness_min: FloatProperty(name="亮度保底", default=0.3, update=_auto_update)
    ambient_strength: FloatProperty(name="环境光强度", default=0.25, update=_auto_update)
    ambient_color: FloatVectorProperty(name="环境光颜色", subtype="COLOR", default=(0.35,0.33,0.36), update=_auto_update)

    eye_shadow_alpha: FloatProperty(
        name="目影",
        description="眼部阴影浓度(目影材质 alpha)。0=完全隐藏, 1=最深。",
        default=0.3,
        min=0.0,
        max=1.0,
        update=_update_eye_shadow,
    )
    eye_shadow_auto: BoolProperty(
        name="目影自动",
        description="根据环境光+灯光角度+灯光强度综合调节目影(仿崩铁三渲二)。"
                    "环境光越强→目影越浅, 侧光/强光→目影更深。开启后滑块值被自动覆盖, 关闭则恢复手动。",
        default=True,
        update=_auto_update,
    )
    eye_shadow_curve: FloatProperty(
        name="目影曲线",
        description="目影响应曲线: 0=线性(等同旧版仅环境光), 1=强光敏感(目影更浓), -1=弱光敏感(更淡)。",
        default=0.0,
        min=-1.0,
        max=1.0,
        subtype="FACTOR",
        update=_auto_update,
    )
    eye_shadow_angle_influence: FloatProperty(
        name="角度影响",
        description="虚拟日光角度对目影的影响: 0=无影响(等同旧版), 越大则顶光/侧光时目影越深(崩铁风格推荐0.3-0.5)。",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_auto_update,
    )
    eye_shadow_energy_influence: FloatProperty(
        name="强度影响",
        description="虚拟日光强度对目影的影响: 0=无影响(等同旧版), 越大则强光时目影越深(崩铁风格推荐0.2-0.4)。",
        default=0.0,
        min=0.0,
        max=1.0,
        subtype="FACTOR",
        update=_auto_update,
    )

    eye_through_strength: FloatProperty(
        name="眼透强度",
        description="合成器眼透: 把被头发挡住的眉眼贴图混回来的比例(参考文件=0.5)。0=关闭。",
        default=0.5,
        min=0.0,
        max=1.0,
        update=_update_eye_through,
    )
    eye_through_radius: FloatProperty(
        name="眼透半径",
        description="眼透遮罩柔边/扩散半径(越大眼透范围越向外扩散)。0=硬边。",
        default=0.01,
        min=0.0,
        max=0.06,
        update=_update_eye_radius,
    )
    eye_gate_angle: FloatProperty(
        name="水平可见半角",
        description="眼透门控: 相机与人脸朝向夹角在此范围内显示眼透(带渐变过渡)。90°=正面半球, 可调到180°放宽到侧面。",
        default=2.2340214,
        min=0.0174533,
        max=3.14159265,
        subtype='ANGLE',
        update=_update_eye_gate_angle,
    )

    use_glare: BoolProperty(
        name="辉光",
        description="合成器泛光(Glare)。默认关; 开启给画面加柔和辉光(会整体偏亮)",
        default=False,
        update=_update_glare,
    )
    glare_type: EnumProperty(
        name="泛光类型",
        description="崩铁观感最接近的是雾泛光; 星芒类会出放射状光条",
        items=[('Fog Glow', "雾泛光 Fog Glow", "柔和雾状光晕, 最接近崩铁(推荐)"),
               ('Bloom', "泛光 Bloom", "经典多阶 bloom"),
               ('Ghosts', "幽灵光 Ghosts", "镜头鬼影"),
               ('Simple Star', "星芒 Simple Star", "简单星芒"),
               ('Streaks', "星芒条纹 Streaks", "放射状光条")],
        default='Fog Glow', update=_update_glare,
    )
    glare_threshold: FloatProperty(
        name="阈值",
        description="线性亮度超过此值的像素才泛光。本管线合成器入口 max≈1.5, 故默认0.8(≥1.5 全场无像素可泛)",
        default=0.8, min=0.0, max=8.0, soft_max=2.0, update=_update_glare,
    )
    glare_size: FloatProperty(
        name="光晕大小", default=0.5, min=0.0, soft_max=1.5, update=_update_glare,
    )
    glare_strength: FloatProperty(
        name="强度", default=1.0, min=0.0, soft_max=2.0, update=_update_glare,
    )
    glare_quality: EnumProperty(
        name="质量",
        items=[('High', "高", ""), ('Medium', "中", ""), ('Low', "低", "")],
        default='High', update=_update_glare,
    )

    outline_enabled: BoolProperty(
        name="描边",
        description="开启仿崩铁描边(复用 mmd_tools 的 Solidify 外扩壳: 描边与否/颜色/粗细读 PMX 每材质数据, 泛用不挑命名)",
        default=True,
        update=_on_outline_enabled_toggle,
    )
    outline_color_mode: EnumProperty(
        name="描边色彩",
        description="跟随PMX分色=用模型每个材质自带的描边色(游戏原生观感); 全局单色=所有描边统一用下方颜色",
        items=[
            ("PMX", "跟随PMX分色", "使用 PMX 每个材质的 edge_color(游戏原生按部位分色)"),
            ("GLOBAL", "全局单色", "把所有描边材质颜色覆盖为下方'描边颜色'"),
        ],
        default="GLOBAL",
        update=_on_outline_color_mode,
    )
    outline_color: FloatVectorProperty(
        name="描边颜色",
        subtype="COLOR",
        default=(0.05, 0.035, 0.03),
        min=0.0,
        max=1.0,
        update=_on_outline_color,
    )
    outline_thickness: FloatProperty(
        name="描边粗细",
        description="描边粗细倍率: 以 mmd_tools 自动算出的原生厚度为基准(默认值4.0=原生厚度), 拖动实时改 Solidify 厚度不重建",
        default=4.0,
        min=0.5,
        max=100.0,
        precision=1,
        update=_on_outline_update,
    )
    outline_w_hair: FloatProperty(
        name="发",
        description="头发描边粗细倍率(与模型原生 edge_weight 相乘, 1.0=原生)。松手后重算",
        default=1.0, min=0.0, max=3.0, precision=2, step=10.0,
        update=_on_outline_region_update,
    )
    outline_w_face: FloatProperty(
        name="脸",
        description="面部/眼部描边粗细倍率(与原生相乘, 1.0=原生)。松手后重算",
        default=1.0, min=0.0, max=3.0, precision=2, step=10.0,
        update=_on_outline_region_update,
    )
    outline_w_skin: FloatProperty(
        name="肤",
        description="皮肤描边粗细倍率(与原生相乘, 1.0=原生)。松手后重算",
        default=1.0, min=0.0, max=3.0, precision=2, step=10.0,
        update=_on_outline_region_update,
    )
    outline_w_cloth: FloatProperty(
        name="衣",
        description="衣服/裙/金属/鞋/饰品等其余部位描边粗细倍率(与原生相乘, 1.0=原生)。松手后重算",
        default=1.0, min=0.0, max=3.0, precision=2, step=10.0,
        update=_on_outline_region_update,
    )
    toon_shadow_cull: BoolProperty(
        name="角色不投影(阴影剔除)",
        description="AutoCel 阴影剔除: 影子射线时材质输出透明 → 角色不写阴影贴图, 不自投影/不粘阴影, 明暗完全由色带决定。默认关=保持你验收过的光照(角色会自投影); 开=纯色带光影",
        default=False,
        update=_on_toon_shadow_cull,
    )
    no_shadow: BoolProperty(
        name="关阴影(平光测试)",
        description="临时诊断开关: 开=所有普通三渲二材质按受光色平光显示, 彻底屏蔽 toon 阴影, 便于检查模型本体是否正常(阴影即将重做)。纱/半透材质走各自通路不受影响。默认关",
        default=False,
        update=_auto_update,
    )
    sheer_shadow: FloatProperty(
        name="纱/半透阴影",
        description="纱/头纱/透袖/后纱带等半透材质的 toon 阴影强度: 0=完全平光(受光均匀, 不压成死黑), 1=与普通材质相同的硬阴影。默认 0(平光)",
        default=0.0, min=0.0, max=1.0, step=1.0, precision=2,
        update=_auto_update,
    )
    sheer_alpha: FloatProperty(
        name="纱透光",
        description="纱/头纱/飘带/透袖等半透材质的不透明度倍率(乘在原alpha上): 1=不透明, 越小越透光(看得到背后)。默认 0.5",
        default=0.5, min=0.05, max=1.0, step=1.0, precision=2,
        update=_auto_update,
    )
    sheer_lift: FloatProperty(
        name="纱提亮",
        description="纱/半透材质平光自发光的强度倍率: 让薄纱按受光色平光显示而不是被阴影压黑。1=原受光色, 越大越亮。默认 1.5",
        default=1.5, min=0.5, max=4.0, step=1.0, precision=2,
        update=_auto_update,
    )
    sheer_no_outline: BoolProperty(
        name="纱不描边",
        description="纱/半透材质不给描边权重: 黑色描边壳会糊死薄纱飘带, 关掉它们的描边让透光纱显示出来。默认开",
        default=True,
        update=_on_outline_region_update,
    )

    skin:  PointerProperty(type=HSRSkinProps)
    face:  PointerProperty(type=HSRFaceProps)
    hair:  PointerProperty(type=HSRHairProps)
    cloth: PointerProperty(type=HSRClothProps)
    eye:   PointerProperty(type=HSREyeProps)

    active_tab: EnumProperty(
        name="类别",
        items=[("skin","皮肤","",0),("face","面部","",1),("hair","头发","",2),("cloth","衣服","",3),("eye","眼部","",4)],
        default="skin",
    )
    analysis_result: StringProperty(name="分析结果", default="尚未分析")

    # 公配专属材质名单(逗号分隔, 可编辑): 半透数据无法自动区分, 只能显式列名。
    # 默认=昔涟原精确名 → 昔涟零变化; 换模型时把它的对应材质名填进来即可套用同款处理。
    gauze_mats: StringProperty(
        name="纱层材质", default="頭紗,頭紗+,衣後紗2,衣後紗2+",
        description="走公配透明纱结构的材质名(逗号分隔)。换模型时填入其头纱/后纱等半透纱层名")
    ring_mats: StringProperty(
        name="环饰材质", default="髮飾环,髮飾环+",
        description="走头环菲涅尔发光结构的材质名(逗号分隔)")
    blush_mats: StringProperty(
        name="脸红材质", default="顏+,顏2+",
        description="脸红叠加层: 直出贴图自发光不过色变链(避免脸红偏黄)。逗号分隔")

    # 自定义发光: 扫描出的候选材质列表(可勾选 + 每块独立强度)
    glow_items: CollectionProperty(type=HSRGlowItem)
    glow_index: IntProperty(default=0)
    glow_threshold: FloatProperty(
        name="候选阈值", default=0.85, min=0.0, max=1.0,
        description="扫描时 基础贴图亮度≥此值自动勾选为发光候选")


class HSR_OT_Convert(bpy.types.Operator):
    bl_idname = "hsr.convert"
    bl_label = "一键三渲二"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        global _FORM_MORPH_MAP
        config = shader.get_config(context.scene)
        toon, keep = shader.convert_selected(context, config)
        # 从 mmd_tools 的导入记录找 PMX 路径; 反推失败(空表)时保留旧映射, 防止形态表被清空
        new_map = _build_form_map_from_pmx()
        if new_map:
            _FORM_MORPH_MAP = new_map
        _tag_form_materials()
        _save_form_map()
        # 确保纱层隐藏形状键存在, 形态按钮可用
        for obj in context.scene.objects:
            if obj.type == "MESH" and "mesh" in obj.name.lower():
                pmx_morph.ensure_veil_hide_shapekeys(obj)
                break
        # 纱类材质的剔除和形态值恢复为确认的正确态(convert 会强制全开/全透明)
        for mn in pmx_morph._get_morph_material_names():
            mat = bpy.data.materials.get(mn)
            if not mat or not mat.node_tree:
                continue
            # 剔除: 只有衣後紗/衣後紗+ 开, 其余关
            mat.use_backface_culling = (mn in ("衣後紗", "衣後紗+"))
            # 形态值=1 (隐藏靠形状键, 不靠 alpha)
            for node in mat.node_tree.nodes:
                if node.type == "VALUE" and node.label in ("形态显示", "头纱显示"):
                    node.outputs["Value"].default_value = 1.0
        # 颜赤.tga 直出自发光, 不经过色变链(脸红变黄) — 名单可编辑(scene.hsr_toon.blush_mats)
        for mn in [t.strip() for t in context.scene.hsr_toon.blush_mats.split(",") if t.strip()]:
            mat = bpy.data.materials.get(mn)
            if mat and mat.node_tree:
                em = next((n for n in mat.node_tree.nodes if n.type == "EMISSION"), None)
                base = mat.node_tree.nodes.get("mmd_base_tex")
                if em and base:
                    for l in list(em.inputs["Color"].links):
                        mat.node_tree.links.remove(l)
                    mat.node_tree.links.new(base.outputs["Color"], em.inputs["Color"])
        form_count = len(_get_filtered_forms())
        rebuild_glow_list(context.scene)   # 转换后自动列出全部材质到发光面板
        self.report({"INFO"}, "转换 %d, 保留 %d, 形态 %d" % (toon, keep, form_count))
        return {"FINISHED"}


class HSR_OT_ResetCategory(bpy.types.Operator):
    """重置当前标签页参数为初始值"""
    bl_idname = "hsr.reset_category"
    bl_label = "回归初始"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.hsr_toon
        tab = props.active_tab
        cat = getattr(props, tab)
        defaults = dict(CATEGORY_DEFAULTS.get(tab, CATEGORY_DEFAULTS["cloth"]))
        defaults.update(COMMON_DEFAULTS)
        for key, val in defaults.items():
            if hasattr(cat, key):
                setattr(cat, key, val)
        shader.update_toon_params(context)
        self.report({"INFO"}, "%s 已重置" % tab)
        return {"FINISHED"}


# ── 形态切换 ──────────────────────────────────────────
# 从 PMX 自动读取的形态映射表（运行时填充）
# 格式: {morph名: [(材质名关键词, "show"/"hide"), ...]}
_FORM_MORPH_MAP = {}


def _build_form_map_from_pmx():
    """从用户选中的模型反推 PMX, 读取形态 morph; 无选中时扫全场景网格。"""
    try:
        objs = list(bpy.context.selected_objects) if bpy.context.selected_objects else [
            o for o in bpy.data.objects if o.type == "MESH"]
        if not objs:
            return {}

        class _Ctx:
            pass

        c = _Ctx()
        c.selected_objects = objs
        return pmx_morph.read_pmx_form_morphs(c)
    except Exception:
        return {}


# 排除面部表情类 morph（照れ/脸红/目影等不该作为形态切换）
def _get_filtered_forms():
    """获取形态列表（排除面部表情，颜赤.tga 着色暂未调好）。"""
    forms = {}
    for name, ops in _FORM_MORPH_MAP.items():
        if any(kw in name for kw in ("照れ", "目影")):
            continue
        forms[name] = ops
    return forms


_FORM_MAP_BLOCK = "hsr_form_morph_map"


def _save_form_map():
    """把形态映射存进 blend 文本块, 重开文件不转换也能用形态切换。"""
    try:
        txt = bpy.data.texts.get(_FORM_MAP_BLOCK)
        if txt is None:
            txt = bpy.data.texts.new(_FORM_MAP_BLOCK)
        txt.clear()
        txt.write(json.dumps(_FORM_MORPH_MAP, ensure_ascii=False))
    except Exception:
        pass


def _load_form_map():
    global _FORM_MORPH_MAP
    try:
        txt = bpy.data.texts.get(_FORM_MAP_BLOCK)
        if txt is not None and txt.as_string().strip():
            data = json.loads(txt.as_string())
            # 空表({})不算有效数据, 继续尝试 legacy, 避免历史损坏文件顶掉可用映射
            if isinstance(data, dict) and data:
                _FORM_MORPH_MAP = data
                return
        # 兼容旧版 blend: 形态表曾存在 scene 自定义属性里
        legacy = bpy.context.scene.get("hsr_form_morph_map") if bpy.context.scene else None
        if legacy:
            data = json.loads(legacy) if isinstance(legacy, str) else legacy
            if isinstance(data, dict):
                _FORM_MORPH_MAP = data
    except Exception:
        pass


def _tag_form_materials():
    """给被形态 morph 控制的材质打标记, 供透明层滑块隔离(泛用, 不依赖节点标签)。"""
    count = 0
    for name, ops in _FORM_MORPH_MAP.items():
        for kw, op in ops:
            for mat in bpy.data.materials:
                if mat.get("hsr_stage") != "virtual_sun" or not mat.node_tree:
                    continue
                if kw not in mat.name:
                    continue
                owned = set(mat.get("hsr_form_morphs", []))
                owned.add(name)
                mat["hsr_form_morphs"] = sorted(owned)
                count += 1
    return count


def _show_nodes(mat):
    """该材质的显示权重节点: 优先"形态显示"(shader.py 标准),
    兼容旧 blend 的"头纱显示"遗留标签(迁移时改回)。"""
    nodes = []
    for node in mat.node_tree.nodes:
        if node.type == "VALUE" and node.label in ("形态显示", "头纱显示"):
            nodes.append(node)
    return nodes


class HSR_OT_ToggleForm(bpy.types.Operator):
    """一键切换形态（用隐藏形状键塌缩/展开对应纱层）"""
    bl_idname = "hsr.toggle_form"
    bl_label = "切换形态"
    bl_options = {"REGISTER", "UNDO"}

    form_name: StringProperty(
        name="形态名",
        description="要切换的形态",
        default="",
    )

    @staticmethod
    def _mesh_obj(context):
        for o in context.scene.objects:
            if o.type == "MESH" and "mesh" in o.name.lower():
                return o
        for o in bpy.data.objects:
            if o.type == "MESH" and len(o.material_slots) > 20:
                return o
        return None

    def execute(self, context):
        global _FORM_MORPH_MAP
        forms = _get_filtered_forms()
        morph_ops = forms.get(self.form_name, [])
        if not morph_ops:
            self.report({"WARNING"}, "未知形态: %s" % self.form_name)
            return {"CANCELLED"}

        mesh_obj = self._mesh_obj(context)
        if mesh_obj is None:
            self.report({"WARNING"}, "未找到角色网格")
            return {"CANCELLED"}

        # 确保隐藏形状键存在
        pmx_morph.ensure_veil_hide_shapekeys(mesh_obj)
        sk = mesh_obj.data.shape_keys
        if sk is None:
            self.report({"WARNING"}, "无形状键")
            return {"CANCELLED"}

        # 判断当前 morph 是否处于"已应用态": 按形状键/Value 的实际值反推,
        # 兼容 hide 类(眼镜消失)与 show 类(头纱出现)——旧逻辑只认 show, 导致 hide-only 单向。
        applied = None
        for kw, op in morph_ops:
            kb = sk.key_blocks.get(pmx_morph._hide_key_name(kw)) if sk else None
            if kb is not None:
                # hide-op: value>0.5=已塌缩=已应用; show-op: value<0.5=已展开=已应用
                applied = (kb.value > 0.5) if op == "hide" else (kb.value < 0.5)
                break
            # 退化为读 Value 节点(alpha 层)
            for mat in bpy.data.materials:
                if mat.get("hsr_stage") == "virtual_sun" and mat.node_tree and kw in mat.name:
                    nodes = _show_nodes(mat)
                    if nodes:
                        v = nodes[0].outputs["Value"].default_value
                        # show-op: 显示(>0.1)=已应用; hide-op: 隐藏(<0.1)=已应用
                        applied = (v > 0.1) if op == "show" else (v < 0.1)
                    break
            if applied is not None:
                break
        if applied is None:
            applied = False
        new = not applied   # 本次点击后要进入的态

        count = 0
        for kw, op in morph_ops:
            kb = sk.key_blocks.get(pmx_morph._hide_key_name(kw)) if sk else None
            if kb is not None:
                # 形状键: 0=展开, 1=塌缩。应用态下 hide→塌缩(1), show→展开(0)
                kb.value = 1.0 if ((op == "hide") == new) else 0.0
                count += 1
                continue
            # 退化为 alpha 切换(表情层等共享顶点的材质)
            orig = None
            for mat in bpy.data.materials:
                if mat.get("hsr_stage") != "virtual_sun" or not mat.node_tree:
                    continue
                if kw not in mat.name:
                    continue
                if orig is None:
                    orig = float(mat.get("hsr_orig_alpha", 1.0))
                for node in _show_nodes(mat):
                    if op == "show":
                        node.outputs["Value"].default_value = orig if new else 0.0
                    else:  # hide
                        node.outputs["Value"].default_value = 0.0 if new else orig
                    count += 1

        action = "切换" if new else "恢复"
        self.report({"INFO"}, "%s: %s (%d 形状键)" % (self.form_name, action, count))
        return {"FINISHED"}


class HSR_OT_Evaluate(bpy.types.Operator):
    bl_idname = "hsr.evaluate"
    bl_label = "分析预览"

    def execute(self, context):
        try:
            metrics, _path = analyze.render_and_analyze(context.scene, resolution=320)
            text = analyze.format_metrics(metrics)
            context.scene.hsr_toon.analysis_result = text
            self.report({"INFO"}, "分析完成")
        except Exception as exc:
            context.scene.hsr_toon.analysis_result = "失败: %s" % exc
            return {"CANCELLED"}
        return {"FINISHED"}


class HSR_OT_ViewportCompositor(bpy.types.Operator):
    """把所有 3D 视口的合成器预览打开(总是), 这样视口渲染预览能实时看到合成器眼透"""
    bl_idname = "hsr.viewport_compositor"
    bl_label = "视口显示合成器预览"

    def execute(self, context):
        count = 0
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D' and hasattr(space.shading, "use_compositor"):
                            space.shading.use_compositor = 'ALWAYS'
                            count += 1
        self.report({"INFO"}, "已开启 %d 个视口的合成器预览(总是)" % count)
        return {"FINISHED"}


class HSR_PT_Panel(bpy.types.Panel):
    bl_label = "StarToon SRS v" + ".".join(map(str, bl_info["version"]))
    bl_idname = "HSR_PT_Main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "StarToon"

    def draw(self, context):
        layout = self.layout
        props = context.scene.hsr_toon

        col = layout.column()
        col.scale_y = 1.4
        col.operator("hsr.convert", icon="SHADERFX")
        layout.operator("hsr.evaluate", icon="VIEWZOOM")
        layout.separator()

        box = layout.box()
        row = box.row(align=True)
        row.prop(props, "active_tab", expand=True)
        cat = getattr(props, props.active_tab)

        col = box.column(align=True)
        col.label(text="粉嫩", icon="MOD_HUE_SATURATION")
        col.prop(cat, "pinkness")
        col.separator()
        col.label(text="明部(亮区)", icon="LIGHT_SUN")
        col.prop(cat, "light_saturation")
        col.prop(cat, "light_hue_shift")
        col.prop(cat, "light_value")
        col.prop(cat, "light_gamma")
        col.prop(cat, "light_contrast")
        col.separator()
        col.label(text="暗部(暗区)", icon="MODIFIER")
        col.prop(cat, "dark_saturation")
        col.prop(cat, "dark_hue_shift")
        col.prop(cat, "dark_contrast")
        col.separator()
        col.label(text="阴影过渡", icon="SHADING_RENDERED")
        col.prop(cat, "shadow_boundary")
        col.prop(cat, "shadow_width")

        box.operator("hsr.reset_category", text="回归初始", icon="FILE_REFRESH")

        box = layout.box()
        box.label(text="亮度(全局)", icon="LIGHT_SUN")
        box.prop(props, "key_energy")
        box.prop(props, "brightness_scale")
        box.prop(props, "brightness_min")
        _sun = shader.get_sun_light(context.scene)
        box.label(text=("旋转 %s 调阴影方向" % _sun.name) if _sun else "旋转太阳灯调阴影方向",
                  icon="LIGHT")

        box = layout.box()
        box.label(text="环境光(全局)", icon="WORLD")
        box.prop(props, "ambient_strength")
        box.prop(props, "ambient_color")

        box = layout.box()
        box.label(text="眼透(仿崩铁)", icon="HIDE_OFF")
        box.prop(props, "eye_through_strength", slider=True)
        box.prop(props, "eye_through_radius", slider=True)
        box.prop(props, "eye_gate_angle", slider=True)
        box.operator("hsr.viewport_compositor", text="视口显示合成器预览", icon="VIEWZOOM")

        box = layout.box()
        box.label(text="后期(全局)", icon="RENDER_STILL")
        box.prop(props, "use_glare", text="辉光")
        if props.use_glare:
            box.prop(props, "glare_type", text="类型")
            box.prop(props, "glare_threshold", slider=True)
            box.prop(props, "glare_size", slider=True)
            box.prop(props, "glare_strength", slider=True)
            box.prop(props, "glare_quality", text="质量")

        box = layout.box()
        box.label(text="描边(仿崩铁)", icon="MOD_SOLIDIFY")
        box.prop(props, "outline_enabled")
        if props.outline_enabled:
            # 描边引擎: mmd_tools Solidify 外扩壳。描边与否/原生色/粗细读 PMX 每材质数据。
            box.prop(props, "outline_color_mode")
            if props.outline_color_mode == "GLOBAL":
                box.prop(props, "outline_color")
            box.prop(props, "outline_thickness", slider=True)
            sub = box.box()
            sub.label(text="分段粗细(倍率·与原生相乘)")
            col = sub.column(align=True)
            col.prop(props, "outline_w_hair", slider=True)
            col.prop(props, "outline_w_face", slider=True)
            col.prop(props, "outline_w_skin", slider=True)
            col.prop(props, "outline_w_cloth", slider=True)
            box.prop(props, "sheer_no_outline")

        box = layout.box()
        box.label(text="阴影(AutoCel)", icon="SHADING_RENDERED")
        box.prop(props, "no_shadow")
        box.prop(props, "toon_shadow_cull")
        box.prop(props, "sheer_shadow", slider=True)
        box.prop(props, "sheer_lift", slider=True)

        box = layout.box()
        box.label(text="形态切换", icon="OUTLINER_OB_GROUP_INSTANCE")
        forms = _get_filtered_forms()
        if not forms:
            box.label(text="(转换后自动读取)", icon="INFO")
        else:
            for form_name in forms:
                op = box.operator("hsr.toggle_form", text=form_name)
                op.form_name = form_name

            box.separator()
            box.label(text="目影(仿崩铁)", icon="HIDE_OFF")
            box.prop(props, "eye_shadow_auto", toggle=True)
            if props.eye_shadow_auto:
                box.prop(props, "eye_shadow_curve", slider=True)
                box.prop(props, "eye_shadow_angle_influence", slider=True)
                box.prop(props, "eye_shadow_energy_influence", slider=True)
            else:
                box.prop(props, "eye_shadow_alpha", slider=True)

        box = layout.box()
        box.label(text="分析", icon="VIEWZOOM")
        for line in props.analysis_result.splitlines()[:6]:
            box.label(text=line[:120])

        box = layout.box()
        box.label(text="专属材质名单(泛用)", icon="TEXT")
        box.label(text="逗号分隔; 换模型填入其对应材质名", icon="INFO")
        box.prop(props, "gauze_mats", text="纱层")
        box.prop(props, "ring_mats", text="环饰")
        box.prop(props, "blush_mats", text="脸红")


class HSR_OT_RefreshGlow(bpy.types.Operator):
    """刷新发光列表: 列出所有已转换材质(分组), 不采样像素。"""
    bl_idname = "hsr.refresh_glow"
    bl_label = "刷新发光列表"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        rebuild_glow_list(context.scene)
        self.report({"INFO"}, "已列出 %d 组材质" % len(context.scene.hsr_toon.glow_items))
        return {"FINISHED"}


class HSR_PT_Glow(bpy.types.Panel):
    """自定义发光: 列出全部材质, 勾选该亮的 + 每块独立强度。sphere 材质已自动 matcap。"""
    bl_label = "自定义发光"
    bl_parent_id = "HSR_PT_Main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        props = context.scene.hsr_toon
        lay = self.layout
        lay.operator("hsr.refresh_glow", icon="FILE_REFRESH")
        if not props.glow_items:
            lay.label(text="(转换后自动列出, 或点刷新)", icon="INFO")
            return
        for it in props.glow_items:
            r = lay.row(align=True)
            r.prop(it, "on", text=(("◈ " if it.has_sphere else "") + it.base))
            r.prop(it, "strength", text="", slider=True)


classes = (HSRSkinProps, HSRFaceProps, HSRHairProps, HSRClothProps, HSREyeProps,
           HSRGlowItem,
           HSRToonProperties, HSR_OT_Convert, HSR_OT_ResetCategory,
           HSR_OT_ToggleForm, HSR_OT_Evaluate, HSR_OT_ViewportCompositor,
           HSR_OT_RefreshGlow, HSR_PT_Panel, HSR_PT_Glow)


@persistent
def _deferred_face_fwd_refresh():
    """载入后延迟重算一次 sun_dir/face_fwd: 此时 depsgraph 已求值物体矩阵,
    修正 _on_load 里过早读到单位矩阵导致的 sun_dir=(0,0,1)/face_fwd 卡死。"""
    try:
        shader._face_fwd_update()
    except Exception:
        pass
    return None   # 一次性, 自动注销


@persistent
def _on_load(_dummy=None):
    """重开 blend 不转换也能用形态切换: 从文本块恢复映射并补打标记。
    ⚠ 打开 .blend 会清空 bpy.app.handlers 与 driver_namespace(均不随文件保存),
    导致眼透门控驱动 face_fwd 缺失 → FaceForward 退化 (0,0,0) → 门控恒定半开 → 脑后漏眼。
    这里在 load 后重新注册 depsgraph 处理器并补写 face_fwd/sun_dir。"""
    _load_form_map()
    _tag_form_materials()
    try:
        shader._register_face_fwd_driver()
    except Exception:
        pass
    # 多角色眼透自愈: 把每个眼材质门控重绑到它自己角色的头骨(老文件 hsr_head_arm 缺失时)
    try:
        shader.ensure_lash_aov()
    except Exception:
        pass
    try:
        bpy.app.timers.register(_deferred_face_fwd_refresh, first_interval=0.1)
        bpy.app.timers.register(_deferred_face_fwd_refresh, first_interval=0.5)
    except Exception:
        pass


def register():
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass
    bpy.types.Scene.hsr_toon = PointerProperty(type=HSRToonProperties)
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)
    # 注册眼透门控驱动 face_fwd()
    try:
        shader._register_face_fwd_driver()
    except Exception:
        pass


def unregister():
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    if hasattr(bpy.types.Scene, "hsr_toon"):
        del bpy.types.Scene.hsr_toon
    # 清理眼透门控驱动 + 处理器
    try:
        h = bpy.app.handlers.depsgraph_update_post
        if getattr(shader, "_face_fwd_update", None) in h:
            h.remove(shader._face_fwd_update)
        if 'face_fwd' in bpy.app.driver_namespace:
            del bpy.app.driver_namespace['face_fwd']
    except Exception:
        pass
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()
