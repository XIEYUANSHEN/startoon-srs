# -*- coding: utf-8 -*-
"""仿崩铁风格三渲二 — 参数化材质构建器（虚拟日光驱动明暗）。

核心架构（经实际验证）：
- 虚拟日光：材质内 dot(着色法线, sun_dir) 直接驱动 toon 明暗色带。
  sun_dir = HSR_SunLight 世界朝向, 由 depsgraph 处理器写进 driver_namespace(转灯即实时改),
  用 SCRIPTED 驱动读入 → 旋转 HSR_SunLight 就能调阴影方向, 不受场景其他灯光/环境光干扰。
  (旧版 GN 写 SUN 顶点属性、以及 Diffuse→ShaderToRGB 读真实灯光两条路均已废弃)
- 简化双路：暗路=贴图×暗度系数，明路=贴图×HSV调色（不用灰度预混，避免图集花斑）
- 粉嫩值：总滑块联动饱和度+色相偏移；详细参数可折叠细调
- 亮度：Driver 接灯光 Energy，调灯强度皮肤亮度跟随，保底不黑死
- 材质分类：无 toon 渐变条依赖，透明/叠加层保留原始 MMDShaderDev
"""
import bpy
import math
import os
from bpy.app.handlers import persistent


# ── 材质分类 ──────────────────────────────────────────
CATEGORIES = ("skin", "face", "hair", "eye", "cloth", "skirt", "shoe", "metal", "default")




# 眼透架构: 合成器为唯一模式(旧 alpha 眼透已移除)
def _use_compositor_arch(scene):
    return True


def classify(name):
    """按材质名推断类别(尽量泛用, 覆盖中日英常见命名)。参考昔涟: 宝石/戒/环/金属→metal, 饰/花→cloth。
    皮肤/身体: 肌/skin + 体/體(男主等用"身体") + 肤/膚/body/素体。眼周含 眉/睫(眉眼睫统一走眼)。"""
    n = name.lower()
    if any(k in n for k in ("肌", "skin", "體", "体", "肤", "膚", "body", "素体")):
        return "skin"
    if "顏" in n or "颜" in n or "脸" in n or "face" in n:
        return "face"
    if "髪" in n or "发" in n or "hair" in n:
        return "hair"
    if any(k in n for k in ("目", "眼", "瞳", "眉", "睫", "eye")):
        return "eye"
    if "裙" in n or "skirt" in n:
        return "skirt"
    # 金属类(宝石/戒/环/金属/星环)
    if any(k in n for k in ("宝", "戒", "环", "金屬", "金属", "星环", "耳环", "metal", "gold", "silver")):
        return "metal"
    if "靴" in n or "鞋" in n or "shoe" in n or "boot" in n:
        return "shoe"
    if any(k in n for k in ("衣", "服", "cloth", "dress", "wear", "coat", "jacket", "uniform")):
        return "cloth"
    # 饰品(花/饰/纹/指甲)→cloth(参考爱莉: 全走 clothes 组)
    if any(k in n for k in ("饰", "花", "纹", "指甲", "指")):
        return "cloth"
    return "default"


# ── 分类参数表: category → scene.hsr_toon 上的属性名 ──
CATEGORY_PROP = {
    "skin": "skin",
    "face": "face",
    "hair": "hair",
    "cloth": "cloth",
    "skirt": "cloth",
    "shoe": "cloth",
    "metal": "cloth",
    "eye": "eye",
}


def get_category_config(scene, category):
    """取某个类别的完整参数 dict。"""
    p = scene.hsr_toon
    prop_name = CATEGORY_PROP.get(category, "cloth")
    cat = getattr(p, prop_name, None)
    if cat is None:
        cat = p.cloth
    return {
        "pinkness": cat.pinkness,
        "light_saturation": cat.light_saturation,
        "light_hue_shift": cat.light_hue_shift,
        "light_value": cat.light_value,
        "light_gamma": cat.light_gamma,
        "light_contrast": cat.light_contrast,
        "dark_factor": cat.dark_factor,
        "dark_saturation": cat.dark_saturation,
        "dark_hue_shift": cat.dark_hue_shift,
        "dark_gamma": cat.dark_gamma,
        "dark_contrast": cat.dark_contrast,
        "shadow_boundary": cat.shadow_boundary,
        "shadow_width": cat.shadow_width,
        "shadow_interpolation": cat.shadow_interpolation,
    }


def get_config(scene):
    """全局配置。"""
    p = scene.hsr_toon
    return {
        "ambient_strength": p.ambient_strength,
        "ambient_color": tuple(p.ambient_color),
        "brightness_scale": p.brightness_scale,
        "brightness_min": p.brightness_min,
        "key_energy": p.key_energy,
    }


# 亮度参考默认值: 昔涟/瓦尔特等已调好模型是在这三个默认下验收的, 归一化基准。
# 亮度乘子 = (灯光能量/参考) × (缩放/参考), 保底(参考=1.0 时中性) 兜底不黑死。
# 三滑杆保持默认 → 乘子严格 = 1.0 → 零回归; 抬高能量/缩放→变亮, 调低保底下限→可变暗。
DEF_KEY_ENERGY = 0.87
DEF_BRIGHTNESS_SCALE = 0.15
DEF_BRIGHTNESS_MIN = 0.3
M_BRIGHT_MAX = 4.0   # 亮度乘子上限(防历史遗留的离谱滑杆值把皮肤吹成纯白)


_SHEER_KEYWORDS = ("纱", "紗", "透", "veil", "シアー", "gauze", "sheer")


def _is_sheer(mat_name):
    """半透/纱类材质(薄纱/头纱/透袖/后纱带等): 硬 toon 阴影会把它们压成死黑, 需软化。"""
    n = mat_name or ""
    return any(k in n for k in _SHEER_KEYWORDS)


def _emission_strength(sun_obj):
    """亮度乘子(作用在最终颜色上, 等效自发光强度): 默认滑杆值→恰好 1.0(不改变已调好模型)。
    灯光能量/亮度缩放按相对默认值相乘, 保底(默认 0.3→归一 1.0)作下限; 上限 M_BRIGHT_MAX 兜住
    老文件里为试探失效滑杆而存的离谱值(如 energy108×scale30≈2.5万倍)。"""
    try:
        energy = float(sun_obj.data.energy)
    except Exception:
        energy = DEF_KEY_ENERGY
    bscale = float(sun_obj.get("hsr_brightness_scale", DEF_BRIGHTNESS_SCALE)) or DEF_BRIGHTNESS_SCALE
    bmin = float(sun_obj.get("hsr_brightness_min", DEF_BRIGHTNESS_MIN))
    mult = (energy / DEF_KEY_ENERGY) * (bscale / DEF_BRIGHTNESS_SCALE)
    floor = bmin / DEF_BRIGHTNESS_MIN
    return min(max(mult, floor), M_BRIGHT_MAX)


# ── 粉嫩值 → 实际参数的映射 ───────────────────────────
def pinkness_to_params(pinkness, cat_config):
    """粉嫩值(0~2)映射到 HSV 参数。"""
    base_sat = 1.0 + 0.35 * pinkness
    base_hue = 0.5 + 0.015 * pinkness
    sat = base_sat * cat_config["light_saturation"]
    hue = base_hue + cat_config["light_hue_shift"] - 0.5
    return sat, hue


# ── 自发光(候选测量 + 施加) ───────────────────────────
def measure_glow(mat):
    """指标: 材质【基础颜色贴图】的平均亮度(0~1)。贴图越亮(近白/金) = 原作里越'自带亮度/发光'。
    (MMD 无独立自发光通道, toon 多为空、diffuse 恒白 → 用基础贴图亮度作代理, 实测能区分
     髮飾环/发饰叶/衣後紗(≈0.89 该亮) 与 肌/衣(≈0.77 不该亮)。)"""
    nt = mat.node_tree
    if nt is None:
        return 0.0
    base = next((n for n in nt.nodes if n.type == "TEX_IMAGE" and n.image
                 and "toon" not in n.image.name.lower()
                 and "sphere" not in n.image.name.lower()), None)
    img = base.image if base else None
    if img is None or not img.has_data:
        return 0.0
    try:
        px = img.pixels[:]
        step = max(4, (len(px) // 4) // 1500 * 4)
        s = 0.0; c = 0
        for i in range(0, len(px), step):
            s += 0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2]; c += 1
        return round(s / max(1, c), 3)
    except Exception:
        return 0.0


def set_glow(mat, on, strength):
    """给材质施加/取消自发光: 在 surface 输出前插一个 AddShader(原结果 + 贴图色 Emission)。
    幂等: 重复调用会先拆旧的再按新参数重建。"""
    nt = mat.node_tree
    if nt is None:
        return
    out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
    if out is None or not out.inputs["Surface"].links:
        return
    # 定位真实 surface 源(跳过旧的发光叠加)
    ln = out.inputs["Surface"].links[0]
    fn = ln.from_node
    if fn.type == "ADD_SHADER" and getattr(fn, "label", "") == "发光叠加":
        src = fn.inputs[0].links[0].from_socket if fn.inputs[0].links else None
        for n in list(nt.nodes):
            if getattr(n, "label", "") in ("发光叠加", "发光"):
                nt.nodes.remove(n)
    else:
        src = ln.from_socket
    if src is None:
        return
    if not on:
        nt.links.new(src, out.inputs["Surface"])
        return
    base = next((n for n in nt.nodes if n.type == "TEX_IMAGE" and n.image
                 and "toon" not in n.image.name.lower() and "sphere" not in n.image.name.lower()), None)
    em = nt.nodes.new("ShaderNodeEmission"); em.label = "发光"; em.name = "HSR_GlowEm"
    em.inputs["Strength"].default_value = float(strength)
    if base is not None:
        nt.links.new(base.outputs["Color"], em.inputs["Color"])
    else:
        em.inputs["Color"].default_value = (1.0, 1.0, 1.0, 1.0)
    add = nt.nodes.new("ShaderNodeAddShader"); add.label = "发光叠加"; add.name = "HSR_GlowAdd"
    nt.links.new(src, add.inputs[0])
    nt.links.new(em.outputs[0], add.inputs[1])
    nt.links.new(add.outputs[0], out.inputs["Surface"])


# ── 灯光/虚拟日光 ────────────────────────────────────
def ensure_sun_light(scene):
    """确保场景里有 HSR_SunLight（放头顶，好选中旋转）。返回灯光对象。"""
    obj = bpy.data.objects.get("HSR_SunLight")
    if obj is None or obj.type != "LIGHT":
        data = bpy.data.lights.new("HSR_SunLight", "SUN")
        obj = bpy.data.objects.new("HSR_SunLight", data)
        scene.collection.objects.link(obj)
        obj.rotation_euler = (math.radians(35), 0, math.radians(20))
    obj.location = (0, 0, 3.0)
    return obj


def get_sun_light(scene=None):
    """解析当前生效的太阳灯。优先级: convert 时记录的名字 → 标准名 HSR_SunLight → 场景第一盏 SUN。
    修 bug: convert_selected 会复用场景里【已有】的 SUN 灯(如默认场景的 "Sun")且不改名,
    而 update_toon_params / _face_fwd_update 过去只按死名 "HSR_SunLight" 找 → 落空 →
    灯光能量/亮度缩放/保底滑条全部失效、sun_dir 不更新(阴影不跟灯走)。统一走这个解析器。"""
    if scene is None:
        try:
            scene = bpy.context.scene
        except Exception:
            scene = None
    name = scene.get("hsr_sun_name") if scene is not None else None
    if name:
        o = bpy.data.objects.get(name)
        if o is not None and o.type == "LIGHT" and getattr(o.data, "type", None) == "SUN":
            return o
    o = bpy.data.objects.get("HSR_SunLight")
    if o is not None and o.type == "LIGHT":
        return o
    if scene is not None:
        for ob in scene.objects:
            if ob.type == "LIGHT" and getattr(ob.data, "type", None) == "SUN":
                return ob
    return None


def _topmost(obj):
    """沿 parent 链走到最高层祖先(角色根: mmd_tools 的 Root 空物体/Armature)。"""
    seen = set()
    while obj.parent is not None and obj not in seen:
        seen.add(obj)
        obj = obj.parent
    return obj


def _find_object_sun(obj, scene):
    """找【属于该角色】的太阳灯: 在该 obj 所属角色根(topmost)的后代里找第一盏 SUN 灯。
    用户把每角色的灯父级到该角色根 → 各追各的灯。找不到(灯没挂到角色下)→ None(退回全局)。"""
    root = _topmost(obj)
    for o in scene.objects:
        if o.type != "LIGHT" or getattr(o.data, "type", None) != "SUN":
            continue
        p = o.parent
        seen = set()
        while p is not None and p not in seen:
            if p is root:
                return o
            seen.add(p)
            p = p.parent
    return None


def ensure_virtual_sun_gn(mesh_obj, sun_obj):
    """[已废弃] 旧版用几何节点把太阳方向写成 SUN 顶点属性供材质读取。
    现改为材质内 dot(法线, sun_dir) 直接驱动明暗(sun_dir 由 depsgraph 处理器写进
    driver_namespace), 不再需要 GN。此函数仅负责清理历史遗留的 GN 修改器/节点组。"""
    mod = mesh_obj.modifiers.get("HSR_VirtualSun")
    if mod is not None:
        mesh_obj.modifiers.remove(mod)
    tree = bpy.data.node_groups.get("HSR_VirtualSunGN")
    if tree is not None and tree.users == 0:
        try:
            bpy.data.node_groups.remove(tree)
        except Exception:
            pass
    return None


# ── 材质转换判定 ──────────────────────────────────────
def should_toon(mat, shader_group):
    """全部材质都转 toon（含 alpha=0 的隐藏层，以防要用）。
    只跳过无 MMDShaderDev 的。"""
    if shader_group is None:
        return False
    return True


# ── 材质构建 ──────────────────────────────────────────
def _build_ring_material(mat):
    """头环材质: 公配结构 + 菲涅尔边缘发光, 更有神性。"""
    nt = mat.node_tree
    tex_uv = nt.nodes.get("mmd_tex_uv")
    base_tex = nt.nodes.get("mmd_base_tex")
    if not tex_uv or not base_tex:
        return False

    grp = _ensure_group_003()
    if grp is None:
        return False

    for n in list(nt.nodes):
        if n.name not in ("mmd_tex_uv", "mmd_base_tex", "mmd_sphere_tex"):
            nt.nodes.remove(n)

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    grp_node = nt.nodes.new("ShaderNodeGroup")
    grp_node.node_tree = grp
    _ensure_material_sun_vector(mat, nt, grp_node)
    lw = nt.nodes.new("ShaderNodeLayerWeight")
    lw.inputs["Blend"].default_value = 0.3
    cr = nt.nodes.new("ShaderNodeValToRGB")
    cr.color_ramp.elements[0].position = 0.4
    cr.color_ramp.elements[0].color = (0, 0, 0, 1)
    cr.color_ramp.elements[1].position = 0.7
    cr.color_ramp.elements[1].color = (1, 1, 1, 1)
    glow_mix = nt.nodes.new("ShaderNodeMix")
    glow_mix.data_type = "RGBA"
    glow_mix.blend_type = "ADD"
    glow_mix.inputs["Factor"].default_value = 0.6
    tr = nt.nodes.new("ShaderNodeBsdfTransparent")
    ms = nt.nodes.new("ShaderNodeMixShader")

    nt.links.new(base_tex.outputs["Color"], grp_node.inputs["输入"])
    nt.links.new(base_tex.outputs["Color"], grp_node.inputs["明部颜色"])
    nt.links.new(base_tex.outputs["Color"], grp_node.inputs["暗部颜色"])
    nt.links.new(lw.outputs["Facing"], cr.inputs["Fac"])
    nt.links.new(grp_node.outputs["颜色"], glow_mix.inputs["A"])
    nt.links.new(cr.outputs["Color"], glow_mix.inputs["B"])
    nt.links.new(glow_mix.outputs["Result"], ms.inputs[2])
    nt.links.new(tr.outputs[0], ms.inputs[1])
    nt.links.new(base_tex.outputs["Alpha"], ms.inputs["Fac"])
    nt.links.new(ms.outputs[0], out.inputs["Surface"])

    mat.blend_method = "BLEND"
    mat.show_transparent_back = False
    mat.use_backface_culling = True
    mat["hsr_stage"] = "virtual_sun"
    mat["hsr_template"] = "ring"
    return True


def _build_gauze_material(mat):
    """公配版纱类材质: NodeGroup.003 颜色处理 + 透明混合, alpha 直出。"""
    nt = mat.node_tree
    tex_uv = nt.nodes.get("mmd_tex_uv")
    base_tex = nt.nodes.get("mmd_base_tex")
    if not tex_uv or not base_tex:
        return False

    # 确保 NodeGroup.003 存在(资产优先, 程序化兜底)
    grp = _ensure_group_003()
    if grp is None:
        return False

    # 清掉旧链, 保留基础节点
    for n in list(nt.nodes):
        if n.name not in ("mmd_tex_uv", "mmd_base_tex", "mmd_sphere_tex"):
            nt.nodes.remove(n)

    out = nt.nodes.new("ShaderNodeOutputMaterial")
    grp_node = nt.nodes.new("ShaderNodeGroup")
    grp_node.node_tree = grp
    _ensure_material_sun_vector(mat, nt, grp_node)
    tr = nt.nodes.new("ShaderNodeBsdfTransparent")
    ms = nt.nodes.new("ShaderNodeMixShader")

    nt.links.new(base_tex.outputs["Color"], grp_node.inputs["输入"])
    nt.links.new(base_tex.outputs["Color"], grp_node.inputs["明部颜色"])
    nt.links.new(base_tex.outputs["Color"], grp_node.inputs["暗部颜色"])
    nt.links.new(grp_node.outputs["颜色"], ms.inputs[2])
    nt.links.new(tr.outputs[0], ms.inputs[1])
    nt.links.new(base_tex.outputs["Alpha"], ms.inputs["Fac"])
    nt.links.new(ms.outputs[0], out.inputs["Surface"])

    mat.blend_method = "BLEND"
    mat.show_transparent_back = False
    mat.use_backface_culling = True
    mat["hsr_stage"] = "virtual_sun"
    mat["hsr_template"] = "gauze"
    return True

def _build_sun_lambert(nt, lk, sun_vec_out):
    """构建 dot(着色法线, 太阳方向) → max(0) 的朗伯受光项, 返回输出 socket。
    太阳方向由【外部传入的 socket】提供(即组输入 '太阳方向'), 每个材质各自喂自己那盏灯
    → 支持同场景多角色各追各的灯。取代旧版组内硬连 SunDir(全局单灯)。"""
    for n in list(nt):   # 幂等: 重建前清掉旧的受光链 (nt 已是 nodes 集合)
        if n.label in ("着色法线", "N·L", "朗伯"):
            nt.remove(n)
    geo = nt.new("ShaderNodeNewGeometry"); geo.location = (-1500, 500); geo.label = "着色法线"
    dot = nt.new("ShaderNodeVectorMath"); dot.operation = 'DOT_PRODUCT'
    dot.location = (-1300, 400); dot.label = "N·L"
    lk.new(geo.outputs['Normal'], dot.inputs[0])
    lk.new(sun_vec_out, dot.inputs[1])
    pos = nt.new("ShaderNodeMath"); pos.operation = 'MAXIMUM'
    pos.location = (-1120, 400); pos.label = "朗伯"
    pos.inputs[1].default_value = 0.0
    lk.new(dot.outputs['Value'], pos.inputs[0])
    return pos.outputs[0]


_S2RGB_TYPES = ('SHADERTORGB', 'SHADER_TO_RGB')   # ShaderNodeShaderToRGB 的 type 实测是 'SHADERTORGB'


def _migrate_ng003_suninput(tree):
    """把 NodeGroup.003 的明暗受光源统一到【组输入 '太阳方向'】, 供每材质各喂一盏灯。
    覆盖三种历史结构: (a) 最老 Diffuse→ShaderToRGB; (b) v1.5.x 组内硬连 SunDir(全局单灯);
    (c) 已迁移。幂等。"""
    nt = tree.nodes; lk = tree.links
    gi = next((n for n in nt if n.type == 'GROUP_INPUT'), None)
    final_scale = next((n for n in nt if n.label == "缩放后Fac"), None)
    if gi is None or final_scale is None:
        return
    if "太阳方向" not in {s.name for s in tree.interface.items_tree if s.in_out == 'INPUT'}:
        s = tree.interface.new_socket("太阳方向", in_out="INPUT", socket_type="NodeSocketVector")
        s.default_value = (0.0, -0.57, 0.82)
    dot = next((n for n in nt if n.label == "N·L" and n.type == 'VECT_MATH'), None)
    geo = next((n for n in nt if n.label == "着色法线"), None)
    pos = next((n for n in nt if n.label == "朗伯" and n.type == 'MATH'), None)
    need_rebuild = (dot is None or geo is None or pos is None
                    or any(n.type in _S2RGB_TYPES for n in nt))
    if need_rebuild:
        sun_out = _build_sun_lambert(nt, lk, gi.outputs["太阳方向"])
        for l in list(final_scale.inputs[0].links):
            lk.remove(l)
        lk.new(sun_out, final_scale.inputs[0])
    else:
        # 已有 geo/dot/pos: 把 dot 第二操作数从内部 SunDir 改吃组输入, 并把 pos 接到 final_scale
        for l in list(dot.inputs[1].links):
            lk.remove(l)
        lk.new(gi.outputs["太阳方向"], dot.inputs[1])
        for l in list(final_scale.inputs[0].links):
            lk.remove(l)
        lk.new(pos.outputs[0], final_scale.inputs[0])
    # 清理废弃节点(旧 SunDir combine / Diffuse / ShaderToRGB / SeparateColor)
    for n in list(nt):
        if (n.label == "SunDir" or n.type in _S2RGB_TYPES
                or n.type in ('SEPARATE_COLOR', 'SEP_COLOR') or n.type == 'BSDF_DIFFUSE'):
            try:
                nt.remove(n)
            except Exception:
                pass


def _ensure_material_sun_vector(mat, nt, gnode):
    """给材质建/更新一个 CombineXYZ(标签'本材质太阳方向'), 三通道 SCRIPTED 驱动读
    hsr_sun_dirs.get(<本材质绑定灯名>, sun_dir)[i] → 连到组节点 '太阳方向' 输入。
    没绑灯时自动回退全局 sun_dir → 单灯场景与旧版逐像素一致。幂等。"""
    if gnode is None or nt is None:
        return
    tgt = gnode.inputs.get("太阳方向")
    if tgt is None:
        return
    sun = mat.get("hsr_sun_name")
    sun_key = sun if sun is not None else ""
    node = next((n for n in nt.nodes if n.label == "本材质太阳方向"), None)
    # 快路径: 节点已存在、绑的灯没变、且已连到【这个】组节点 → 直接返回(滑条拖动零开销)
    if (node is not None and node.type == 'COMBINE_XYZ'
            and node.get("hsr_sun_key", "\x00") == sun_key
            and tgt.is_linked and tgt.links[0].from_node == node):
        return
    # 建时即把该 key 塞进 hsr_sun_dirs, 避免首帧 KeyError(处理器每帧也会重算覆盖)
    ns = bpy.app.driver_namespace
    dirs = ns.get('hsr_sun_dirs')
    if not isinstance(dirs, dict):
        dirs = {}; ns['hsr_sun_dirs'] = dirs
    if sun not in dirs:
        o = bpy.data.objects.get(sun) if sun else None
        if o is not None and o.type == "LIGHT":
            dd = o.matrix_world.col[2].normalized()
            dirs[sun] = (dd.x, dd.y, dd.z)
        else:
            dirs[sun] = tuple(ns.get('sun_dir', (0.0, -0.57, 0.82)))
    if node is not None and node.type != 'COMBINE_XYZ':
        nt.nodes.remove(node); node = None
    if node is None:
        node = nt.nodes.new("ShaderNodeCombineXYZ")
        node.label = "本材质太阳方向"; node.location = (200, -360)
    node["hsr_sun_key"] = sun_key
    for i in range(3):
        try:
            fc = node.inputs[i].driver_add('default_value')
            fc.driver.type = 'SCRIPTED'
            fc.driver.expression = "hsr_sun_dirs[%r][%d]" % (sun, i)
        except Exception:
            pass
    for l in list(tgt.links):
        nt.links.remove(l)
    nt.links.new(node.outputs[0], tgt)


def _ensure_group_003():
    """确保 NodeGroup.003 存在。优先级: 文件已有 → 从插件资产 append(与参考逐字节一致)
    → 程序化构建(兜底, 明路HS明度1.1)。
    已有树缺 阴影边界/宽度 输入时自动补建(版本迁移)。"""
    tree = bpy.data.node_groups.get("NodeGroup.003")
    if tree is not None and len(tree.nodes) > 5:
        # 版本迁移: 补加阴影输入 + 7 级色带 + Math 节点链
        inp_names = {s.name for s in tree.interface.items_tree if s.in_out == 'INPUT'}
        if "阴影边界" not in inp_names or "阴影宽度" not in inp_names:
            print("[HSR] 迁移 NodeGroup.003: 补加阴影输入")
            _migrate_ng003_shadow(tree)
        # 太阳方向迁移: 统一到组输入 '太阳方向'(兼容最老 S2RGB / v1.5.x 内部 SunDir / 缺输入)
        _inps = {s.name for s in tree.interface.items_tree if s.in_out == 'INPUT'}
        if (any(n.type in _S2RGB_TYPES for n in tree.nodes)
                or any(n.label == "SunDir" for n in tree.nodes)
                or "太阳方向" not in _inps):
            print("[HSR] 迁移 NodeGroup.003: 明暗改由组输入'太阳方向'驱动(每材质各喂灯)")
            _migrate_ng003_suninput(tree)
        _migrate_ng003_brightness(tree)   # 幂等: 补加 '亮度' 输入使明暗滑杆生效
        return tree
    if tree is not None:
        bpy.data.node_groups.remove(tree)
    # 资产 append
    asset = os.path.join(os.path.dirname(__file__), "assets_ng003.blend")
    if os.path.exists(asset):
        try:
            with bpy.data.libraries.load(asset, link=False) as (data_from, data_to):
                if "NodeGroup.003" in data_from.node_groups:
                    data_to.node_groups = ["NodeGroup.003"]
            tree = bpy.data.node_groups.get("NodeGroup.003")
            if tree is not None and len(tree.nodes) > 5:
                tree.use_fake_user = True
                inp_names = {s.name for s in tree.interface.items_tree if s.in_out == 'INPUT'}
                if "阴影边界" not in inp_names or "阴影宽度" not in inp_names:
                    print("[HSR] 迁移 NodeGroup.003 (资产): 补加阴影输入")
                    _migrate_ng003_shadow(tree)
                _inps = {s.name for s in tree.interface.items_tree if s.in_out == 'INPUT'}
                if (any(n.type in _S2RGB_TYPES for n in tree.nodes)
                        or any(n.label == "SunDir" for n in tree.nodes)
                        or "太阳方向" not in _inps):
                    print("[HSR] 迁移 NodeGroup.003 (资产): 明暗改由组输入'太阳方向'驱动")
                    _migrate_ng003_suninput(tree)
                _migrate_ng003_brightness(tree)   # 幂等: 补加 '亮度' 输入使明暗滑杆生效
                return tree
        except Exception:
            pass
    # 程序化兜底
    tree = bpy.data.node_groups.new("NodeGroup.003", "ShaderNodeTree")
    iface = tree.interface
    iface.new_socket("颜色", in_out="OUTPUT", socket_type="NodeSocketShader")
    s_in = iface.new_socket("输入", in_out="INPUT", socket_type="NodeSocketColor")
    s_light_sat = iface.new_socket("明部饱和度", in_out="INPUT", socket_type="NodeSocketFloat")
    s_light_con = iface.new_socket("明部对比度", in_out="INPUT", socket_type="NodeSocketFloat")
    s_dark_sat = iface.new_socket("暗部饱和度", in_out="INPUT", socket_type="NodeSocketFloat")
    s_dark_con = iface.new_socket("暗部对比度", in_out="INPUT", socket_type="NodeSocketFloat")
    s_light_col = iface.new_socket("明部颜色", in_out="INPUT", socket_type="NodeSocketColor")
    s_dark_col = iface.new_socket("暗部颜色", in_out="INPUT", socket_type="NodeSocketColor")
    iface.new_socket("左边缘光", in_out="INPUT", socket_type="NodeSocketColor").default_value = (0.17, 0.17, 1.0, 1.0)
    iface.new_socket("右边缘光", in_out="INPUT", socket_type="NodeSocketColor").default_value = (1.0, 0.69, 0.67, 1.0)
    s_bright = iface.new_socket("Bright", in_out="INPUT", socket_type="NodeSocketFloat")
    # 阴影控制(暴露到组外, 供 update_toon_params 按类别驱动)
    s_shadow_b = iface.new_socket("阴影边界", in_out="INPUT", socket_type="NodeSocketFloat")
    s_shadow_w = iface.new_socket("阴影宽度", in_out="INPUT", socket_type="NodeSocketFloat")
    s_sun_dir = iface.new_socket("太阳方向", in_out="INPUT", socket_type="NodeSocketVector")
    s_sun_dir.default_value = (0.0, -0.57, 0.82)
    s_light_sat.default_value = 1.2
    s_light_con.default_value = 0.2
    s_dark_sat.default_value = 1.0
    s_dark_con.default_value = 0.0
    s_bright.default_value = 0.0
    s_shadow_b.default_value = 0.5
    s_shadow_w.default_value = 0.05

    nt = tree.nodes
    lk = tree.links
    gi = nt.new("NodeGroupInput"); gi.location = (-1400, 0)
    go = nt.new("NodeGroupOutput"); go.location = (600, 0)

    diff = None  # 旧 Diffuse→ShaderToRGB 已废弃, 明暗改由虚拟太阳方向 dot 驱动
    sun_lambert = _build_sun_lambert(nt, lk, gi.outputs["太阳方向"])   # dot(法线, 太阳方向) → clamp[0,1]

    ramp = nt.new("ShaderNodeValToRGB"); ramp.location = (-800, 500)
    ramp.label = "阴影渐变"
    # 7 级谢留渐变(参考昔涟定版) — 位置由 Math 节点缩放输入驱动
    xieliu = [
        (0.000, 0.00), (0.108, 0.05), (0.216, 0.10), (0.271, 0.33),
        (0.325, 0.55), (0.379, 0.78), (0.433, 1.00),
    ]
    ramp.color_ramp.elements[0].position = xieliu[0][0]
    ramp.color_ramp.elements[0].color = (xieliu[0][1], xieliu[0][1], xieliu[0][1], 1.0)
    for pos, val in xieliu[1:]:
        e = ramp.color_ramp.elements.new(pos)
        e.color = (val, val, val, 1.0)
    ramp.color_ramp.interpolation = "EASE"

    # Math 节点链: Diffuse 输出 → 缩放(阴影边界/宽度) → 色带 Fac
    # scale = 0.433 / max(阴影边界, 0.01) * (1 + 阴影宽度 × 2)
    # 1) 0.433 / 阴影边界
    div_b = nt.new("ShaderNodeMath"); div_b.location = (-1200, 500)
    div_b.operation = "DIVIDE"; div_b.inputs[0].default_value = 0.433
    div_b.label = "阴影边界缩放"
    # 2) 1 + 阴影宽度 × 2
    mul_w = nt.new("ShaderNodeMath"); mul_w.location = (-1200, 350)
    mul_w.operation = "MULTIPLY"; mul_w.inputs[0].default_value = 2.0
    mul_w.label = "阴影宽度×2"
    add1 = nt.new("ShaderNodeMath"); add1.location = (-1050, 350)
    add1.operation = "ADD"; add1.inputs[0].default_value = 1.0
    add1.label = "+1"
    # 3) 合并: 边界缩放 × 宽度缩放
    comb = nt.new("ShaderNodeMath"); comb.location = (-900, 425)
    comb.operation = "MULTIPLY"; comb.label = "总缩放"
    # 4) Diffuse 输出 × 总缩放
    final_scale = nt.new("ShaderNodeMath"); final_scale.location = (-750, 500)
    final_scale.operation = "MULTIPLY"; final_scale.label = "缩放后Fac"

    lk.new(sun_lambert, final_scale.inputs[0])
    lk.new(gi.outputs["阴影边界"], div_b.inputs[1])
    lk.new(gi.outputs["阴影宽度"], mul_w.inputs[1])
    lk.new(mul_w.outputs[0], add1.inputs[1])
    lk.new(div_b.outputs[0], comb.inputs[0])
    lk.new(add1.outputs[0], comb.inputs[1])
    lk.new(comb.outputs[0], final_scale.inputs[1])
    lk.new(final_scale.outputs[0], ramp.inputs["Fac"])

    inv = nt.new("ShaderNodeInvert"); inv.location = (-600, 500)
    lk.new(ramp.outputs["Color"], inv.inputs["Color"])

    # 明路: mix(明部颜色, 输入, 0.5) → HS → 伽玛0.8 → BC
    m4 = nt.new("ShaderNodeMix"); m4.data_type = "RGBA"; m4.location = (-1200, 200)
    m4.inputs[0].default_value = 1.0
    gray4 = nt.new("ShaderNodeRGB"); gray4.location = (-1400, 300)
    gray4.outputs[0].default_value = (0.5, 0.5, 0.5, 1.0)
    lk.new(gray4.outputs[0], m4.inputs[6])
    lk.new(gi.outputs["明部颜色"], m4.inputs[7])
    m2 = nt.new("ShaderNodeMix"); m2.data_type = "RGBA"; m2.location = (-1000, 200)
    m2.inputs[0].default_value = 0.5
    lk.new(m4.outputs[2], m2.inputs[6])
    lk.new(gi.outputs["输入"], m2.inputs[7])
    hs1 = nt.new("ShaderNodeHueSaturation"); hs1.location = (-800, 200)
    lk.new(m2.outputs[2], hs1.inputs["Color"])
    lk.new(gi.outputs["明部饱和度"], hs1.inputs["Saturation"])
    hs1.inputs["Value"].default_value = 1.1   # 参考原值: 明路明度1.1
    g1 = nt.new("ShaderNodeGamma"); g1.location = (-620, 200)
    g1.inputs["Gamma"].default_value = 0.8
    lk.new(hs1.outputs["Color"], g1.inputs["Color"])
    bc1 = nt.new("ShaderNodeBrightContrast"); bc1.location = (-450, 200)
    bc1.inputs[1].default_value = 0.04
    lk.new(g1.outputs["Color"], bc1.inputs["Color"])
    lk.new(gi.outputs["明部对比度"], bc1.inputs[2])

    # 暗路: mix(暗部颜色, 输入, 0.5) → HS → 伽玛1.0 → BC
    m5 = nt.new("ShaderNodeMix"); m5.data_type = "RGBA"; m5.location = (-1200, -200)
    m5.inputs[0].default_value = 1.0
    gray5 = nt.new("ShaderNodeRGB"); gray5.location = (-1400, -100)
    gray5.outputs[0].default_value = (0.5, 0.5, 0.5, 1.0)
    lk.new(gray5.outputs[0], m5.inputs[6])
    lk.new(gi.outputs["暗部颜色"], m5.inputs[7])
    m3 = nt.new("ShaderNodeMix"); m3.data_type = "RGBA"; m3.location = (-1000, -200)
    m3.inputs[0].default_value = 0.5
    lk.new(m5.outputs[2], m3.inputs[6])
    lk.new(gi.outputs["输入"], m3.inputs[7])
    hs0 = nt.new("ShaderNodeHueSaturation"); hs0.location = (-800, -200)
    lk.new(m3.outputs[2], hs0.inputs["Color"])
    lk.new(gi.outputs["暗部饱和度"], hs0.inputs["Saturation"])
    g0 = nt.new("ShaderNodeGamma"); g0.location = (-620, -200)
    g0.inputs["Gamma"].default_value = 1.0
    lk.new(hs0.outputs["Color"], g0.inputs["Color"])
    bc0 = nt.new("ShaderNodeBrightContrast"); bc0.location = (-450, -200)
    lk.new(g0.outputs["Color"], bc0.inputs["Color"])
    lk.new(gi.outputs["Bright"], bc0.inputs[1])
    lk.new(gi.outputs["暗部对比度"], bc0.inputs[2])

    # 明暗混合(明=A, 暗=B, Fac=反相主渐变) → 混白0.5 → 输出
    mix = nt.new("ShaderNodeMix"); mix.data_type = "RGBA"; mix.location = (-200, 100)
    lk.new(inv.outputs["Color"], mix.inputs[0])
    lk.new(bc1.outputs["Color"], mix.inputs[6])
    lk.new(bc0.outputs["Color"], mix.inputs[7])
    white = nt.new("ShaderNodeCombineColor"); white.location = (-200, -100)
    white.inputs[0].default_value = 1.0
    white.inputs[1].default_value = 1.0
    white.inputs[2].default_value = 1.0
    mix1 = nt.new("ShaderNodeMix"); mix1.data_type = "RGBA"; mix1.location = (0, 100)
    mix1.inputs[0].default_value = 0.5
    lk.new(mix.outputs[2], mix1.inputs[6])
    lk.new(white.outputs[0], mix1.inputs[7])
    lk.new(mix1.outputs[2], go.inputs["颜色"])
    _migrate_ng003_brightness(tree)   # 程序化兜底也补 '亮度' 输入, 与资产/迁移路径一致
    return tree


def _migrate_ng003_shadow(tree):
    """给已有 NodeGroup.003 补加 阴影边界/宽度 输入 + 7 级色带 + Math 节点链。
    版本迁移: 旧版 2 级色带 + 无输入 → 新版 7 级谢留渐变 + 组外可驱动。"""
    print("[HSR] _migrate_ng003_shadow 开始")
    import mathutils
    nt = tree.nodes
    lk = tree.links
    iface = tree.interface

    # 1) 补加输入(幂等)
    for sname, default in (("阴影边界", 0.5), ("阴影宽度", 0.05)):
        if sname not in {s.name for s in iface.items_tree if s.in_out == 'INPUT'}:
            s = iface.new_socket(sname, in_out="INPUT", socket_type="NodeSocketFloat")
            s.default_value = default

    # 2) 找/建 7 级色带(替换旧 2 级)
    ramp = next((n for n in nt if n.type == 'VALTORGB'), None)
    if ramp is None:
        ramp = nt.new("ShaderNodeValToRGB")
        ramp.location = (-800, 500)
    ramp.label = "阴影渐变"
    xieliu = [
        (0.000, 0.00), (0.108, 0.05), (0.216, 0.10), (0.271, 0.33),
        (0.325, 0.55), (0.379, 0.78), (0.433, 1.00),
    ]
    cr = ramp.color_ramp
    cr.interpolation = "EASE"
    cr.elements[0].position = xieliu[0][0]
    cr.elements[0].color = (xieliu[0][1], xieliu[0][1], xieliu[0][1], 1.0)
    cr.elements[1].position = xieliu[-1][0]
    cr.elements[1].color = (xieliu[-1][1], xieliu[-1][1], xieliu[-1][1], 1.0)
    # 删多余元素
    while len(cr.elements) > 2:
        cr.elements.remove(cr.elements[-1])
    for pos, val in xieliu[1:-1]:
        e = cr.elements.new(pos)
        e.color = (val, val, val, 1.0)

    # 3) 找 GroupInput 节点
    gi = next((n for n in nt if n.type == 'GROUP_INPUT'), None)
    if gi is None:
        return

    # 4) 找 SeparatorColor(Red 输出)
    sep = next((n for n in nt if n.type == 'SEPARATE_COLOR'), None)
    if sep is None:
        return

    # 5) 建 Math 节点链(幂等: 已有同名标签则跳过)
    def _find(label):
        for n in nt:
            if n.label == label and n.type == 'MATH':
                return n
        return None

    div_b = _find("阴影边界缩放")
    if div_b is None:
        div_b = nt.new("ShaderNodeMath")
        div_b.location = (-1200, 500)
    div_b.operation = "DIVIDE"
    div_b.inputs[0].default_value = 0.433
    div_b.label = "阴影边界缩放"

    mul_w = _find("阴影宽度×2")
    if mul_w is None:
        mul_w = nt.new("ShaderNodeMath")
        mul_w.location = (-1200, 350)
    mul_w.operation = "MULTIPLY"
    mul_w.inputs[0].default_value = 2.0
    mul_w.label = "阴影宽度×2"

    add1 = _find("+1")
    if add1 is None:
        add1 = nt.new("ShaderNodeMath")
        add1.location = (-1050, 350)
    add1.operation = "ADD"
    add1.inputs[0].default_value = 1.0
    add1.label = "+1"

    comb = _find("总缩放")
    if comb is None:
        comb = nt.new("ShaderNodeMath")
        comb.location = (-900, 425)
    comb.operation = "MULTIPLY"
    comb.label = "总缩放"

    final_scale = _find("缩放后Fac")
    if final_scale is None:
        final_scale = nt.new("ShaderNodeMath")
        final_scale.location = (-750, 500)
    final_scale.operation = "MULTIPLY"
    final_scale.label = "缩放后Fac"

    # 连线(先清旧连再连)
    for sock in (div_b.inputs[1], mul_w.inputs[1], final_scale.inputs[0], final_scale.inputs[1],
                 div_b.outputs[0], mul_w.outputs[0], add1.outputs[0], comb.outputs[0]):
        for l in list(sock.links):
            lk.remove(l)
    lk.new(gi.outputs["阴影边界"], div_b.inputs[1])
    lk.new(gi.outputs["阴影宽度"], mul_w.inputs[1])
    lk.new(mul_w.outputs[0], add1.inputs[1])
    lk.new(div_b.outputs[0], comb.inputs[0])
    lk.new(add1.outputs[0], comb.inputs[1])
    lk.new(sep.outputs["Red"], final_scale.inputs[0])
    lk.new(comb.outputs[0], final_scale.inputs[1])
    lk.new(final_scale.outputs[0], ramp.inputs["Fac"])


def _migrate_ng003_brightness(tree):
    """给 NodeGroup.003 补加组输入 '亮度'(Float, 默认1.0) + 末端乘子, 使 update_toon_params
    能按材质驱动整体明暗(灯光能量/缩放/保底生效)。幂等: 已有 '亮度' 输入直接返回。
    做法: 找到喂入 输出'颜色' 的那条链路, 在末端插 MixRGB(MULTIPLY, fac=1), B=亮度灰度。"""
    iface = tree.interface
    inp_names = {s.name for s in iface.items_tree if s.in_out == 'INPUT'}
    if "亮度" in inp_names:
        return
    s = iface.new_socket("亮度", in_out="INPUT", socket_type="NodeSocketFloat")
    s.default_value = 1.0
    nt = tree.nodes
    lk = tree.links
    go = next((n for n in nt if n.type == 'GROUP_OUTPUT'), None)
    gi = next((n for n in nt if n.type == 'GROUP_INPUT'), None)
    if go is None or gi is None:
        return
    col_out = go.inputs.get("颜色")
    if col_out is None or not col_out.is_linked:
        return
    src = col_out.links[0].from_socket
    comb = nt.new("ShaderNodeCombineColor"); comb.location = (400, -350); comb.label = "亮度灰度"
    lk.new(gi.outputs["亮度"], comb.inputs[0])
    lk.new(gi.outputs["亮度"], comb.inputs[1])
    lk.new(gi.outputs["亮度"], comb.inputs[2])
    mixb = nt.new("ShaderNodeMix"); mixb.data_type = "RGBA"; mixb.blend_type = "MULTIPLY"
    mixb.location = (450, -150); mixb.label = "亮度乘子"
    mixb.inputs[0].default_value = 1.0
    for l in list(col_out.links):
        lk.remove(l)
    lk.new(src, mixb.inputs[6])
    lk.new(comb.outputs[0], mixb.inputs[7])
    lk.new(mixb.outputs[2], col_out)


def _apply_shadow_cull(nt):
    """给材质输出插 AutoCel 阴影剔除 mixin(幂等, 节点统一 label='阴影剔除'):
    Mix Shader(Fac=Light Path.Is Shadow Ray, 槽1=原表面, 槽2=Transparent) → Material Output。
    影子射线(阴影贴图渲染)时输出透明 → 材质不写阴影贴图 → 角色不自投影/不粘影,
    明暗完全由 NodeGroup.003 色带决定(AutoCel 阴影剔除同款)。相机射线 Fac=0 → 原表面。"""
    out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
    if out is None:
        return False
    surf = out.inputs["Surface"]
    existing = next((n for n in nt.nodes if n.type == "MIX_SHADER" and n.label == "阴影剔除"), None)
    if existing is not None:
        # ⚠ 必须用 == : Blender 5.2 的 link.from_node / nodes[] 每次访问都返回全新 Python
        # 包装对象, `is` 恒为 False(实测) → 误判"输出被改过"→ 走摘除分支 → Surface 被清空!
        if surf.is_linked and surf.links[0].from_node == existing:
            return True
        _remove_shadow_cull(nt)  # 输出被改过 → 摘了重插
    cur = surf.links[0].from_socket if surf.is_linked else None
    if cur is None:
        return False
    for l in list(surf.links):
        nt.links.remove(l)
    lp = nt.nodes.new("ShaderNodeLightPath"); lp.label = "阴影剔除"; lp.location = (1250, 350)
    tr = nt.nodes.new("ShaderNodeBsdfTransparent"); tr.label = "阴影剔除"; tr.location = (1250, 150)
    ms = nt.nodes.new("ShaderNodeMixShader"); ms.label = "阴影剔除"; ms.location = (1450, 0)
    # MixShader: Fac=0→输入1, Fac=1→输入2。Fac=Is Shadow Ray(相机射线=0/阴影射线=1)
    # → 相机看原表面(输入1), 阴影射线看透明(输入2, 不写阴影贴图)。
    nt.links.new(lp.outputs["Is Shadow Ray"], ms.inputs["Fac"])
    nt.links.new(cur, ms.inputs[1])                    # Fac=0 相机射线 → 原表面
    nt.links.new(tr.outputs["BSDF"], ms.inputs[2])     # Fac=1 阴影射线 → 透明
    nt.links.new(ms.outputs["Shader"], surf)
    return True


def _remove_shadow_cull(nt):
    """摘掉阴影剔除 mixin, 原表面接回 Material Output。返回是否摘过。"""
    ms = next((n for n in nt.nodes if n.type == "MIX_SHADER" and n.label == "阴影剔除"), None)
    if ms is None:
        return False
    out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
    src = ms.inputs[1].links[0].from_socket if ms.inputs[1].is_linked else None
    # ⚠ 同 _apply_shadow_cull: from_node 必须用 == 比(is 恒 False → 跳过回接 → 删节点时
    # Surface 链接被连带删除 → 材质表面输出清空变黑)
    if out is not None and out.inputs["Surface"].is_linked \
            and out.inputs["Surface"].links[0].from_node == ms:
        for l in list(out.inputs["Surface"].links):
            nt.links.remove(l)
        if src is not None:
            nt.links.new(src, out.inputs["Surface"])
    for n in list(nt.nodes):
        if n.label == "阴影剔除":
            nt.nodes.remove(n)
    return True


def _sync_shadow_cull(nt, scene):
    """按 scene.hsr_toon.toon_shadow_cull 插/摘阴影剔除(默认关=保持已验收光照)。"""
    props = getattr(scene, "hsr_toon", None)
    if props is not None and bool(getattr(props, "toon_shadow_cull", False)):
        return _apply_shadow_cull(nt)
    return _remove_shadow_cull(nt)


def build_toon_material(mat, scene, sun_obj):
    """给材质构建虚拟日光 toon 节点链。
    参数按材质类别独立读取(scene.hsr_toon.<类别>)。"""
    # 已是参考链成品(曾转换过且保留alpha链) → 跳过, 保证按钮幂等:
    # 在已调好的文件上重复点击不会重建/破坏任何东西
    nt = mat.node_tree
    if mat.get("hsr_stage") == "virtual_sun" and mat.get("hsr_orig_alpha") is not None:
        has_group = any(n.type == 'GROUP' and n.node_tree and n.node_tree.name == "NodeGroup.003"
                        for n in nt.nodes)
        if has_group:
            return True  # 已成品: 计数但不重建(返回False会被打回mmd_original标签)

    # 纱类/环类: 公配专属结构。改为用户可编辑名单(scene.hsr_toon.gauze_mats/ring_mats,
    # 逗号分隔, 默认=昔涟原精确名)——因为半透数据无法区分(昔涟 上衣/纹饰 也半透但不是纱),
    # 只能靠显式名单; 默认值保持原样 → 昔涟零变化, 换模型可自行填入其纱/环材质名。
    def _in_list(_n, _csv):
        return _n in {t.strip() for t in (_csv or "").split(",") if t.strip()}
    _gauze = getattr(getattr(scene, "hsr_toon", None), "gauze_mats",
                     "頭紗,頭紗+,衣後紗2,衣後紗2+")
    _ring = getattr(getattr(scene, "hsr_toon", None), "ring_mats", "髮飾环,髮飾环+")
    if _in_list(mat.name, _gauze):
        return _build_gauze_material(mat)
    if _in_list(mat.name, _ring):
        return _build_ring_material(mat)

    shader_group = None
    sphere_tex = None
    for node in nt.nodes:
        if node.type == "GROUP" and node.node_tree and node.node_tree.name.startswith("MMDShaderDev"):
            shader_group = node
        elif node.type == "TEX_IMAGE" and node.image and "sphere" in node.name.lower():
            sphere_tex = node
    if shader_group is None:
        return False

    color_sock = None
    alpha_sock = None
    for sock in shader_group.outputs:
        if sock.name == "Color":
            color_sock = sock
        elif sock.name == "Alpha":
            alpha_sock = sock
    if color_sock is None:
        return False

    category = classify(mat.name)
    cat = get_category_config(scene, category)
    global_cfg = get_config(scene)

    out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
    if out is None:
        out = nt.nodes.new("ShaderNodeOutputMaterial")
    for link in list(out.inputs["Surface"].links):
        nt.links.remove(link)

    # 清理: 删 toon 贴图节点和残留 Principled BSDF（不使用 toon 渐变条）
    for node in list(nt.nodes):
        if node.type == "BSDF_PRINCIPLED":
            nt.nodes.remove(node)
        elif node.type == "TEX_IMAGE" and node.image and "toon" in node.name.lower():
            nt.nodes.remove(node)

    # 二次转换清理: 颜色链 label 节点删除重建。
    # alpha 链(形态显示/显示alpha/眼透*/可见判定/不透明下限/纱下限等) = 用户验收的
    # 手工链核心 —— 若 Fac 已有连线则整链保留不碰(贴图裸alpha≈0.2, 手工链的
    # 不透明下限负责抬回1, 删了链接=头发79%透明)。只有全新材质才重建 alpha。
    _COLOR_LABELS = ("几何数据", "虚拟日光方向", "N·L", "半兰伯特", "头部中心",
                     "顶点-头中心", "球面法线(归一化)", "阴影渐变",
                     "暗度系数", "暗路: base×暗度", "暗路调色", "暗路伽玛", "暗路对比度",
                     "明路调色(粉嫩)", "明路伽玛", "明路对比度", "明暗混合",
                     "环境光底色", "环境光", "加环境光",
                     "金属边缘权重", "金属边缘光颜色", "金属边缘光",
                     "头纱提亮", "自发光",
                     "伽玛", "亮度/对比度", "色相/饱和度/明度",
                     "色相/饱和度/明度.001", "色相/饱和度/明度.002",
                     "明部颜色调色", "NodeGroup.003")
    _ALPHA_LABELS = ("形态显示", "头纱显示", "透明度下限",
                     "是否正面", "显示alpha", "头纱Alpha", "正面判定几何", "贴图Alpha判定",
                     "纱光泽", "纱相加",
                     "眼透几何", "眼透朝向", "眼透视线", "视角衰减",
                     "眼透锚点L", "眼透锚点R", "眼透距离L", "眼透距离R", "眼透距离",
                     "眼区mask", "眼透boost", "眼透强度", "眼透mask总", "眼透反相", "眼透alpha",
                     "眼透下限", "可见判定", "不透明下限", "纱下限", "眼透alpha输出")

    # 已有 MixShader 且 Fac 已连线 → 保留既有 alpha 链
    old_ms = next((n for n in nt.nodes if n.type == "MIX_SHADER"), None)
    keep_alpha = old_ms is not None and old_ms.inputs["Fac"].is_linked
    for node in list(nt.nodes):
        if node.label in _COLOR_LABELS:
            nt.nodes.remove(node)
    if not keep_alpha:
        for node in list(nt.nodes):
            if node.label in _ALPHA_LABELS:
                nt.nodes.remove(node)
        # 透明/MixShader 只在重建 alpha 时一并重建(防重复构建堆积孤儿)
        for node in list(nt.nodes):
            if node.type in ("MIX_SHADER", "BSDF_TRANSPARENT"):
                nt.nodes.remove(node)
        old_ms = None
    # 阴影剔除 mixin 统一在末尾按 prop 重建 → 先摘旧的(防重复堆积)
    for node in list(nt.nodes):
        if node.label == "阴影剔除":
            nt.nodes.remove(node)

    # ── 1. 参考链: 贴图原色 → (伽玛1.2→BC→HS) → NodeGroup.003 ──
    # 组内光照来自场景真实灯光(漫射BSDF), 输出直连(等效Emission 1.0)
    group_tree = _ensure_group_003()
    gnode = nt.nodes.new("ShaderNodeGroup")
    gnode.node_tree = group_tree
    gnode.name = "NodeGroup.003"
    gnode.label = "NodeGroup.003"
    gnode.width = 250
    gnode.location = (200, 0)
    _ensure_material_sun_vector(mat, nt, gnode)   # 本材质各喂一盏灯(无绑灯→回退全局 sun_dir)

    # 颜色源: 优先贴图原色(参考做法), 无贴图退回 mmd 组 Color
    base_tex_node = None
    for node in nt.nodes:
        if (node.type == "TEX_IMAGE" and node.image
                and "toon" not in node.image.name.lower()
                and "sphere" not in node.image.name.lower()):
            base_tex_node = node
            break
    color_sock = base_tex_node.outputs["Color"] if base_tex_node is not None else color_sock

    # 眼/口类: 目/白目/目光/目影/眉睫 走完整颜色链(独立参数生效),
    # 口/齿/舌 走 simple 直连(口腔内部不需要调色)
    simple = any(k in mat.name for k in ("口", "齿", "齒", "舌", "牙", "歯"))
    light_sat, light_hue = pinkness_to_params(cat["pinkness"], cat)
    if simple:
        nt.links.new(color_sock, gnode.inputs["输入"])
        nt.links.new(color_sock, gnode.inputs["明部颜色"])
        nt.links.new(color_sock, gnode.inputs["暗部颜色"])
    else:
        gm = nt.nodes.new("ShaderNodeGamma")
        gm.location = (-700, 150)
        gm.label = "明路伽玛"
        gm.inputs["Gamma"].default_value = cat["light_gamma"]
        nt.links.new(color_sock, gm.inputs["Color"])

        bc_pre = nt.nodes.new("ShaderNodeBrightContrast")
        bc_pre.location = (-500, 150)
        bc_pre.label = "明路对比度"
        bc_pre.inputs[1].default_value = 0.0
        bc_pre.inputs[2].default_value = cat["light_contrast"]
        nt.links.new(gm.outputs["Color"], bc_pre.inputs["Color"])

        hs_in = nt.nodes.new("ShaderNodeHueSaturation")
        hs_in.location = (-300, 150)
        hs_in.label = "明路调色(粉嫩)"
        hs_in.inputs["Hue"].default_value = light_hue
        hs_in.inputs["Saturation"].default_value = light_sat
        hs_in.inputs["Value"].default_value = cat["light_value"]
        hs_in.inputs["Fac"].default_value = 1.0
        nt.links.new(bc_pre.outputs["Color"], hs_in.inputs["Color"])
        nt.links.new(hs_in.outputs["Color"], gnode.inputs["输入"])

        hs_light = nt.nodes.new("ShaderNodeHueSaturation")
        hs_light.location = (-300, -100)
        hs_light.label = "明部颜色调色"
        hs_light.inputs["Saturation"].default_value = 1.0
        hs_light.inputs["Fac"].default_value = 1.0
        nt.links.new(color_sock, hs_light.inputs["Color"])
        nt.links.new(hs_light.outputs["Color"], gnode.inputs["明部颜色"])

        hs_dark = nt.nodes.new("ShaderNodeHueSaturation")
        hs_dark.location = (-300, -350)
        hs_dark.label = "暗路调色"
        hs_dark.inputs["Hue"].default_value = cat["dark_hue_shift"]
        hs_dark.inputs["Saturation"].default_value = cat["dark_saturation"]
        hs_dark.inputs["Value"].default_value = 1.0
        hs_dark.inputs["Fac"].default_value = 1.0
        nt.links.new(color_sock, hs_dark.inputs["Color"])
        nt.links.new(hs_dark.outputs["Color"], gnode.inputs["暗部颜色"])

    final_shader = gnode.outputs["颜色"]

    # ── 把当前面板值写到组实例输入(避免默认值 vs 面板值不一致导致的光照跳变) ──
    # 旧版组(无迁移)可能缺这些输入, 用 try/except 容错
    for name, val in (
        ("阴影边界", cat["shadow_boundary"]),
        ("阴影宽度", cat["shadow_width"]),
        ("明部饱和度", cat["light_saturation"]),
        ("明部对比度", cat["light_contrast"]),
        ("暗部饱和度", cat["dark_saturation"]),
        ("暗部对比度", cat["dark_contrast"]),
    ):
        try:
            sock = gnode.inputs.get(name)
            if sock is not None:
                sock.default_value = val
        except Exception:
            pass

    # ── 7. Alpha ──
    if keep_alpha:
        # 既有 alpha 链(用户手工/上次构建)原样保留, 只把颜色链接进槽2
        nt.links.new(final_shader, old_ms.inputs[2])
        mat["hsr_orig_alpha"] = float(mat.get("hsr_orig_alpha", 1.0))
        mat["hsr_template"] = category
        mat["hsr_stage"] = "virtual_sun"
        _sync_shadow_cull(nt, scene)
        return True

    orig_alpha = 1.0
    for sock in shader_group.inputs:
        if sock.name == "Alpha" and not sock.is_linked:
            orig_alpha = sock.default_value
            break
    mat["hsr_orig_alpha"] = orig_alpha

    # 显示权重节点: 形态切换/透明层滑块只改这一个值, 不碰任何链接
    show_val = nt.nodes.new("ShaderNodeValue")
    show_val.location = (700, -300)
    show_val.label = "形态显示"
    show_val.outputs["Value"].default_value = orig_alpha

    if alpha_sock:
        base_tex_node = None
        for node in nt.nodes:
            if (node.type == "TEX_IMAGE" and node.image
                    and "toon" not in node.image.name.lower()
                    and "sphere" not in node.image.name.lower()):
                base_tex_node = node
                break

        if orig_alpha < 0.001:
            # 隐藏层: MMD 组内部 Alpha = MIN(MAX(is_front, matAlpha), is_front×texA×toonA×matAlpha),
            # matAlpha=0 → 组输出恒 0, 乘显示权重没用。改用贴图 alpha 当透明源:
            # alpha = 显示值 × 贴图alpha(或贴图alpha判定)
            show_gate = nt.nodes.new("ShaderNodeMath")
            show_gate.location = (800, -450)
            show_gate.label = "透明度下限"
            show_gate.operation = "MULTIPLY"
            nt.links.new(show_val.outputs["Value"], show_gate.inputs[0])

            if base_tex_node is not None:
                # 贴图真半透(纱类): 保留贴图 alpha 梯度, 透感自然;
                # 贴图不透明: 判定 >0.5 → 显示态不透明、隐藏态彻底透明, 切换才可见
                semi = False
                try:
                    img = base_tex_node.image
                    if img is not None and img.pixels and img.size[0] * img.size[1] > 0:
                        n = img.size[0] * img.size[1]
                        step = max(1, n // 20000)
                        vals = img.pixels[3::4][::step]
                        semi = sum(1 for v in vals if v < 0.9) / len(vals) > 0.05
                except Exception:
                    semi = False
                if semi:
                    nt.links.new(base_tex_node.outputs["Alpha"], show_gate.inputs[1])
                else:
                    alpha_clip = nt.nodes.new("ShaderNodeMath")
                    alpha_clip.location = (750, -450)
                    alpha_clip.label = "贴图Alpha判定"
                    alpha_clip.operation = "GREATER_THAN"
                    alpha_clip.inputs[1].default_value = 0.5
                    nt.links.new(base_tex_node.outputs["Alpha"], alpha_clip.inputs[0])
                    nt.links.new(alpha_clip.outputs["Value"], show_gate.inputs[1])
            else:
                show_gate.inputs[1].default_value = 1.0

            # 不加正面遮罩: 纱带是弯曲薄片, 背面段被遮罩会整段隐形, 看起来"断裂"
            alpha_mul = nt.nodes.new("ShaderNodeMath")
            alpha_mul.location = (850, -350)
            alpha_mul.label = "显示alpha"
            alpha_mul.operation = "MULTIPLY"
            alpha_mul.inputs[0].default_value = 1.0
            nt.links.new(show_gate.outputs["Value"], alpha_mul.inputs[1])
            alpha_src = alpha_mul.outputs["Value"]

            # EEVEE 的 HASHED 透明不丢弃 alpha≈0 片段(抖动近似), 隐藏层会隐形失败;
            # 隐藏层用 BLEND, alpha=0 才真正全透明, 形态切换才可见
            mat.blend_method = "BLEND"
        else:
            # 常驻层: 组 Alpha 输出正常, 乘显示权重(默认1)
            show_mul = nt.nodes.new("ShaderNodeMath")
            show_mul.location = (800, -200)
            show_mul.label = "显示alpha"
            show_mul.operation = "MULTIPLY"
            nt.links.new(alpha_sock, show_mul.inputs[0])
            nt.links.new(show_val.outputs["Value"], show_mul.inputs[1])
            alpha_src = show_mul.outputs["Value"]

        if category == "hair" and _use_compositor_arch(scene):
            # 头发直接输出三渲二着色(实心像素), 不走 MixShader+透明BSDF。
            # 眼透完全交给 AOV 合成器, 头发材质保持实心。
            # 仍用 BLEND 通道(不写深度) → 被盖住的眉睫仍能渲染写 AOV。
            surface_src = final_shader
        else:
            tr = nt.nodes.new("ShaderNodeBsdfTransparent")
            tr.location = (750, -200)
            ms = nt.nodes.new("ShaderNodeMixShader")
            ms.location = (900, 0)
            nt.links.new(alpha_src, ms.inputs["Fac"])
            nt.links.new(tr.outputs["BSDF"], ms.inputs[1])
            # 关键: 组输出接槽2(不透明位), 槽1=透明BSDF; 接反=alpha1时显示悬空插座=全黑
            nt.links.new(final_shader, ms.inputs[2])
            surface_src = ms.outputs["Shader"]
    else:
        # 无 alpha 输出的材质: 直接连三渲二着色
        surface_src = final_shader
    nt.links.new(surface_src, out.inputs["Surface"])
    # AutoCel 阴影剔除(默认关=保持已验收光照): 影子射线→透明 → 角色不写阴影贴图
    _sync_shadow_cull(nt, scene)

    mat["hsr_template"] = category
    # 参考文件做法: 开背面剔除, 内层不会以反法线显示, 避免内外双层穿帮
    mat.use_backface_culling = True
    if category == "hair":
        # 合成器眼透架构: 头发 = BLEND 通道 + alpha 锁 1.0
        #   · 走半透明通道 → 不写深度 → 被头发盖住的眉睫仍能渲染并写 AOV → 眼透成立
        #     (OPAQUE 会写深度把眉睫 AOV 剔掉 → 盖眼处没眼透)
        #   · alpha=1(像素不透明) → 不会看到内层发片 → 无方块
        #     (之前 alpha<1 真半透明 → 看到内层 → 方块一团糟)
        if _use_compositor_arch(scene):
            mat.blend_method = "BLEND"
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = "BLENDED"
            mat.show_transparent_back = False
            # 锁 alpha=1: "形态显示"权重设 1.0 → 下游 MixShader Fac=1 → 像素全不透明
            for _n in nt.nodes:
                if _n.type == "VALUE" and _n.label == "形态显示":
                    _n.outputs["Value"].default_value = 1.0
        else:
            # 旧 alpha 眼透架构: 头发 BLEND 半透明
            mat.blend_method = "BLEND"
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = "BLENDED"
            mat.show_transparent_back = True
    mat["hsr_stage"] = "virtual_sun"
    return True


def _set_node_input(nodes, label, socket, value):
    """按 label 找节点并设置输入值；Value 节点设置输出值。
    同 label 节点全部更新(历史残留重复链时, 只改第一个会命中死节点)。"""
    changed = False
    for n in nodes:
        if n.label != label:
            continue
        if n.type == "VALUE" and socket == "Value":
            try:
                n.outputs["Value"].default_value = value
                changed = True
            except Exception:
                pass
            continue
        if socket in n.inputs:
            try:
                n.inputs[socket].default_value = value
                changed = True
            except Exception:
                pass
    return changed


def _label_index(nt):
    """把节点树按 label 建一次性索引 {label: [节点,...]}。
    仅在单次 update_toon_params 内使用(不跨调用缓存) → 无陈旧风险:
    即便用户增删/改名节点, 下次调用会重建索引。"""
    idx = {}
    for n in nt.nodes:
        if n.label:
            idx.setdefault(n.label, []).append(n)
    return idx


def _set_node_input_cached(idx, label, socket, value):
    """_set_node_input 的索引版: 语义完全一致(同名节点全更新), 只是从索引取节点。"""
    changed = False
    for n in idx.get(label, ()):
        if n.type == "VALUE" and socket == "Value":
            try:
                n.outputs["Value"].default_value = value
                changed = True
            except Exception:
                pass
            continue
        if socket in n.inputs:
            try:
                n.inputs[socket].default_value = value
                changed = True
            except Exception:
                pass
    return changed


def _sheer_lit_color_src(nt, grp):
    """稳健取纱的'受光色'socket: 组实例 明部颜色 的连入源 → base 贴图 → 明部颜色默认值RGB → 白。"""
    if grp is not None:
        mc = grp.inputs.get("明部颜色")
        if mc is not None and mc.is_linked:
            return mc.links[0].from_socket
    base = next((n for n in nt.nodes if n.type == "TEX_IMAGE" and n.image
                 and "toon" not in n.image.name.lower()
                 and "sphere" not in n.image.name.lower()), None)
    if base is not None:
        return base.outputs["Color"]
    if grp is not None:
        mc = grp.inputs.get("明部颜色")
        if mc is not None:
            rgb = next((n for n in nt.nodes if n.label == "纱受光色" and n.type == "RGB"), None)
            if rgb is None:
                rgb = nt.nodes.new("ShaderNodeRGB"); rgb.label = "纱受光色"
                rgb.location = (-200, -600)
            try:
                rgb.outputs[0].default_value = tuple(mc.default_value)
            except Exception:
                pass
            return rgb.outputs[0]
    return None


def refresh_sheer(scene, em_strength=1.0):
    """纱/半透材质平光: 把它的表面从'组阴影结果'换成'受光色自发光×纱提亮', 避免薄双面法线
    背光半面被 toon 阴影压成死黑, 也让本就偏暗的纱贴图(星空/深色)在平光下按提亮显示。
    兼容两种结构: (a) 带 alpha MixShader(槽1=透明,槽2=表面) → 改槽2;
    (b) Surface 直接接 NodeGroup.003(无 MixShader) → 直接改 Output.Surface。
    平光条件: sheer_shadow≈0 或 全局'关阴影'诊断开启。全部幂等。
    ⚠ Blender5.2: 节点比较必须用 ==(is 恒 False)。"""
    tp = getattr(scene, "hsr_toon", None)
    try:
        lift = float(getattr(tp, "sheer_lift", 2.0))
    except Exception:
        lift = 2.0
    try:
        no_shadow = bool(getattr(tp, "no_shadow", False))
    except Exception:
        no_shadow = False
    try:
        # 全局关阴影诊断时: 纱不走路过原始暗贴图的自发光旁路(那反而更黑), 而是走被强制
        # 全受光的组输出, 与本体一致。仅常规态(sheer_shadow≈0)才启用纱平光自发光旁路。
        flat = (not no_shadow) and float(getattr(tp, "sheer_shadow", 0.0)) <= 0.02
    except Exception:
        flat = (not no_shadow)
    strength = em_strength * lift
    for m in bpy.data.materials:
        if m.get("hsr_stage") != "virtual_sun" or not m.node_tree:
            continue
        if not _is_sheer(m.name):
            continue
        nt = m.node_tree
        grp = next((n for n in nt.nodes if n.type == "GROUP" and n.node_tree
                    and n.node_tree.name == "NodeGroup.003"), None)
        if grp is None:
            continue
        out = next((n for n in nt.nodes if n.type == "OUTPUT_MATERIAL"), None)
        if out is None:
            continue
        surf = out.inputs["Surface"]
        # 定位"表面目标插座": 优先 alpha MixShader 的不透明槽(slot2), 否则 Surface 直连组
        flat_node = next((n for n in nt.nodes if n.label == "纱平光"), None)
        ms = next((n for n in nt.nodes if n.type == "MIX_SHADER"
                   and n.inputs[1].is_linked
                   and n.inputs[1].links[0].from_node.type == "BSDF_TRANSPARENT"), None)
        if ms is not None:
            target = ms.inputs[2]
        elif surf.is_linked and surf.links[0].from_node in (grp, flat_node):
            # Surface 直连组, 或已连上一次生成的"纱平光"自发光 → 都认作可更新目标
            # (否则重入时 target=None, else 分支会删掉自发光却不还原 → Surface 悬空 → 变黑)
            target = surf
        else:
            target = None   # 结构不认识, 别乱动
        try:
            if target is not None and flat:
                src = _sheer_lit_color_src(nt, grp)
                if src is not None:
                    if flat_node is None or flat_node.type != "EMISSION":
                        if flat_node is not None:
                            nt.nodes.remove(flat_node)
                        em = nt.nodes.new("ShaderNodeEmission")
                        em.label = "纱平光"
                        em.location = (src.node.location.x + 320, src.node.location.y - 320)
                        nt.links.new(src, em.inputs["Color"])
                        flat_node = em
                    flat_node.inputs["Strength"].default_value = strength
                    cur = target.links[0].from_node if target.is_linked else None
                    if cur is None or cur != flat_node:
                        for l in list(target.links):
                            nt.links.remove(l)
                        nt.links.new(flat_node.outputs[0], target)
            else:
                # 非平光: 还原目标回组输出, 删平光节点
                cur = target.links[0].from_node if (target is not None and target.is_linked) else None
                if target is not None and cur is not None and cur.label == "纱平光":
                    for l in list(target.links):
                        nt.links.remove(l)
                    nt.links.new(grp.outputs["颜色"], target)
                if flat_node is not None:
                    nt.nodes.remove(flat_node)
        except Exception:
            pass
        if m.blend_method == "HASHED":
            try:
                m.blend_method = "BLEND"
            except Exception:
                pass


def _set_group_no_shadow(on):
    """全局'关阴影(平光)': 直接改共享的 NodeGroup.003 里的阴影混合节点'混合'
    (mix: A=暗路, B=明路, Factor=反色)。把 Factor 锁到 1.0 → 所有面恒取明路(全受光,
    颜色仍走完整明路调色链, 只是不再有阴影), 比"受光色自发光"更贴近正常本体。
    原 Factor 连线记在组自定义属性供还原。幂等(hsr_ns_on 标记)。"""
    g = bpy.data.node_groups.get("NodeGroup.003")
    if g is None:
        return False
    mix = g.nodes.get("混合")
    if mix is None:
        return False
    # 定位被'反色'驱动的那个 Factor(float) 输入; 找不到就退回第一个名为 Factor 的输入
    fac = None
    for s in mix.inputs:
        if s.name == "Factor" and s.is_linked and s.links and \
                s.links[0].from_node.name == "反色":
            fac = s
            break
    if fac is None:
        fac = next((s for s in mix.inputs if s.name == "Factor"), None)
    if fac is None:
        return False
    cur = bool(g.get("hsr_ns_on"))
    try:
        if on and not cur:
            g["hsr_ns_def"] = float(fac.default_value)
            if fac.is_linked:
                src = fac.links[0].from_socket
                g["hsr_ns_node"] = src.node.name
                g["hsr_ns_sock"] = src.name
                for l in list(fac.links):
                    g.links.remove(l)
            fac.default_value = 1.0
            g["hsr_ns_on"] = True
        elif (not on) and cur:
            nd = g.nodes.get(str(g.get("hsr_ns_node", "")))
            sock = g.get("hsr_ns_sock")
            srcsock = None
            if nd is not None:
                srcsock = nd.outputs.get(sock) if sock else \
                    (nd.outputs[0] if len(nd.outputs) else None)
            for l in list(fac.links):
                g.links.remove(l)
            if srcsock is not None:
                g.links.new(srcsock, fac)
            if "hsr_ns_def" in g:
                fac.default_value = float(g["hsr_ns_def"])
            g["hsr_ns_on"] = False
            for k in ("hsr_ns_node", "hsr_ns_sock", "hsr_ns_def"):
                if k in g:
                    del g[k]
    except Exception:
        return False
    try:
        g.update_tag()
    except Exception:
        pass
    return True


def refresh_flat_light(scene, em_strength=1.0):
    """全局'关阴影(平光)'诊断开关: 开 → 共享阴影混合恒取明路(无阴影, 颜色正常);
    关 → 还原色带阴影。纱/半透材质走各自平光通路, 与此无关。幂等。"""
    tp = getattr(scene, "hsr_toon", None)
    on = bool(getattr(tp, "no_shadow", False)) if tp is not None else False
    try:
        _set_group_no_shadow(on)
    except Exception:
        pass


def update_toon_params(context):
    """实时预览：按材质类别更新节点参数值，不重建节点树。"""
    scene = context.scene
    global_cfg = get_config(scene)

    # 亮度参数写到灯光对象(统一走 get_sun_light, 兼容复用用户已有灯)
    sun_obj = get_sun_light(scene)
    if sun_obj is not None:
        sun_obj["hsr_brightness_scale"] = global_cfg["brightness_scale"]
        sun_obj["hsr_brightness_min"] = global_cfg["brightness_min"]
        if global_cfg["key_energy"] is not None:
            sun_obj.data.energy = global_cfg["key_energy"]
    # 亮度乘子(作用在组 '亮度' 输入上)不走 Driver, 灯光参数变化时直接同步到材质
    em_strength = _emission_strength(sun_obj) if sun_obj is not None else 1.0
    # 老文件(转换时还没有 '亮度' 输入)无需重转即可让明暗滑杆生效: 幂等补加
    try:
        _g003 = bpy.data.node_groups.get("NodeGroup.003")
        if _g003 is not None and len(_g003.nodes) > 5:
            _migrate_ng003_brightness(_g003)
    except Exception:
        pass

    # 按类别缓存参数(5类只读一次)
    cat_configs = {}
    for cat_key in ("skin", "face", "hair", "cloth", "eye"):
        cat_configs[cat_key] = get_category_config(scene, cat_key)

    updated = 0
    for mat in bpy.data.materials:
        if mat.get("hsr_stage") != "virtual_sun" or not mat.node_tree:
            continue
        nt = mat.node_tree
        category = mat.get("hsr_template", "default")
        if category in (None, "", "default"):
            # 通用性: 旧转换里落到 default 的材质, 按当前 classify 实时重判
            # (build_toon_material 对已成品会提前返回、不会重新打标签, 故在此兜底,
            #  让"体/體→皮肤"这类分类改进对已转换模型即时生效, 无需重转)
            category = classify(mat.name)
        prop_key = CATEGORY_PROP.get(category, "cloth")
        cat = cat_configs.get(prop_key, cat_configs["cloth"])
        light_sat, light_hue = pinkness_to_params(cat["pinkness"], cat)
        idx = _label_index(nt)   # 本材质节点树一次性索引, 后续查字典免全表扫
        # 幂等回填: 旧文件(每材质独立太阳方向改造前转换的)补上'太阳方向'喂入, 无绑灯→回退全局
        _gn = next((n for n in nt.nodes if n.type == 'GROUP' and n.node_tree
                    and n.node_tree.name == "NodeGroup.003"), None)
        if _gn is not None:
            _ensure_material_sun_vector(mat, nt, _gn)

        changed = False
        # 旧自发光链标签(已废弃, 找不到节点时静默跳过)
        changed |= _set_node_input_cached(idx, "暗度系数", "Value", cat["dark_factor"])
        changed |= _set_node_input_cached(idx, "暗路调色", "Hue", cat["dark_hue_shift"])
        changed |= _set_node_input_cached(idx, "暗路调色", "Saturation", cat["dark_saturation"])
        changed |= _set_node_input_cached(idx, "明路调色(粉嫩)", "Hue", light_hue)
        changed |= _set_node_input_cached(idx, "明路调色(粉嫩)", "Saturation", light_sat)
        changed |= _set_node_input_cached(idx, "明路调色(粉嫩)", "Value", cat["light_value"])
        changed |= _set_node_input_cached(idx, "明路伽玛", "Gamma", cat["light_gamma"])
        changed |= _set_node_input_cached(idx, "明路对比度", "Contrast", cat["light_contrast"])
        # 参考链: 组实例插座的明/暗部饱和度·对比度
        changed |= _set_node_input_cached(idx, "NodeGroup.003", "明部饱和度", cat["light_saturation"])
        changed |= _set_node_input_cached(idx, "NodeGroup.003", "明部对比度", cat["light_contrast"])
        changed |= _set_node_input_cached(idx, "NodeGroup.003", "暗部饱和度", cat["dark_saturation"])
        changed |= _set_node_input_cached(idx, "NodeGroup.003", "暗部对比度", cat["dark_contrast"])
        changed |= _set_node_input_cached(idx, "加环境光", "Fac", global_cfg["ambient_strength"])
        is_sheer = _is_sheer(mat.name)
        # 亮度乘子: 灯光能量/缩放/保底 → 写组实例 '亮度' 输入(默认为1.0中性)
        #   纱/半透: 再乘 sheer_lift 提亮, 避免暗贴图被压成死黑
        bright = em_strength
        if is_sheer:
            try:
                bright = em_strength * float(getattr(scene.hsr_toon, "sheer_lift", 2.0))
            except Exception:
                pass
        changed |= _set_node_input_cached(idx, "NodeGroup.003", "亮度", bright)
        for n in idx.get("环境光底色", ()):
            if n.type == "RGB":
                n.outputs["Color"].default_value = (*global_cfg["ambient_color"], 1.0)
                changed = True
        # 阴影渐变: 组实例的阴影边界/宽度输入(色带在组内, 由 Math 节点缩放驱动)
        b = cat["shadow_boundary"]
        sw = cat["shadow_width"]
        # 纱/半透材质: 硬 toon 阴影会把薄双面法线朝背光的半面压成死黑 → 边界推到极低(近乎全受光/无阴影)+加宽过渡
        if is_sheer:
            ss = getattr(getattr(scene, "hsr_toon", None), "sheer_shadow", 0.0)
            b = 0.02 + (b - 0.02) * ss
            sw = sw + (1.0 - ss) * 0.5
        changed |= _set_node_input_cached(idx, "NodeGroup.003", "阴影边界", b)
        changed |= _set_node_input_cached(idx, "NodeGroup.003", "阴影宽度", sw)
        if changed:
            updated += 1
        # 目影自动调节(接虚拟日光接口): 环境光越强→越浅; 顶光/侧光(angle_influence)、
        # 强光(energy_influence)→更深; curve 做响应曲线。三项滑杆默认0时=旧环境光公式(零回归)。
        if "目影" in mat.name:
            props = getattr(bpy.context.scene, "hsr_toon", None)
            auto = bool(getattr(props, "eye_shadow_auto", True)) if props else True
            tgt = next((n for n in idx.get("形态显示", ()) if n.type == "VALUE"), None)
            if tgt is not None:
                if auto:
                    ns = bpy.app.driver_namespace
                    sun = ns.get('sun_dir', (0.0, -0.57, 0.82))
                    fwd = ns.get('face_fwd', (0.0, -1.0, 0.0))
                    frontal = max(0.0, min(1.0, sun[0]*fwd[0] + sun[1]*fwd[1] + sun[2]*fwd[2]))
                    overhead = max(0.0, min(1.0, sun[2]))
                    amb = global_cfg["ambient_strength"]
                    energy = float(getattr(sun_obj.data, "energy", 0.87)) if (sun_obj and sun_obj.data) else 0.87
                    ai = getattr(props, "eye_shadow_angle_influence", 0.0)
                    ei = getattr(props, "eye_shadow_energy_influence", 0.0)
                    cu = getattr(props, "eye_shadow_curve", 0.0)
                    fill = max(0.0, 1.0 - amb * 2.5)
                    angle_dark = 0.6 * overhead + 0.4 * (1.0 - frontal)   # 顶光为主(眉骨投影), 兼顾侧光
                    energy_dark = max(0.0, min(1.0, energy / 2.0))
                    dark = fill * 0.35 * (1.0 + ai * angle_dark + ei * energy_dark)
                    if abs(cu) > 1e-3:   # 曲线: cu>0 强光敏感(抬升), cu<0 弱光敏感(压低), 0 线性
                        norm = max(0.0, min(1.0, dark / 0.5))
                        dark = (norm ** (1.0 / (1.0 + cu))) * 0.5
                    tgt.outputs[0].default_value = max(0.0, min(1.0, dark))
                else:
                    # 关自动 → 回填手动 alpha(修"关闭不恢复手动"的 bug)
                    tgt.outputs[0].default_value = getattr(props, "eye_shadow_alpha", 0.3) if props else 0.3
    try:
        refresh_sheer(scene, em_strength)
    except Exception:
        pass
    try:
        refresh_flat_light(scene, em_strength)
    except Exception:
        pass
    return updated


def refresh_toon_shadow_cull(scene):
    """阴影剔除开关实时生效: 给所有已转换材质(hsr_stage=virtual_sun)插/摘 mixin。
    不重建材质链, 只动输出端 → 已调好的 alpha/眼透链不受影响。"""
    for mat in bpy.data.materials:
        if not mat.use_nodes or not mat.node_tree:
            continue
        if mat.get("hsr_stage") != "virtual_sun":
            continue
        try:
            _sync_shadow_cull(mat.node_tree, scene)
        except Exception:
            pass


def _cat_of_material(mat):
    return classify(mat.name) if mat is not None else "default"


def _set_gn_input(mod, ng, name, value):
    """按接口 socket 名设修改器输入值。
    Blender 5.0+ 新 API: modifier.properties.inputs.<identifier>.value
    (旧 mod[identifier]=值 在 5.2 抛 'id properties not supported for this type' →
    静默失败 → 描边宽度停在接口默认 0.0、描边材质 None → 壳不外扩不换材质 = 描边不可见。
    5.2 实测四种类型 float/bool/string/material 新 API 全部可写可读)。"""
    try:
        for it in ng.interface.items_tree:
            if getattr(it, "item_type", "") == "SOCKET" and it.in_out == "INPUT" and it.name == name:
                try:
                    getattr(mod.properties.inputs, it.identifier).value = value
                    return True
                except Exception:
                    pass
                mod[it.identifier] = value  # 旧版 Blender 回退路径
                return True
    except Exception:
        pass
    return False


def _get_gn_input(mod, ng, name):
    """读修改器输入当前值(5.0+ 新 API)。socket 不存在/读失败 → None。"""
    try:
        for it in ng.interface.items_tree:
            if getattr(it, "item_type", "") == "SOCKET" and it.in_out == "INPUT" and it.name == name:
                try:
                    return getattr(mod.properties.inputs, it.identifier).value
                except Exception:
                    return None
    except Exception:
        pass
    return None


def _has_gn_input(ng, name):
    """接口里是否有该输入 socket(v1 旧组没有 v2 新输入)。"""
    try:
        for it in ng.interface.items_tree:
            if getattr(it, "item_type", "") == "SOCKET" and it.in_out == "INPUT" and it.name == name:
                return True
    except Exception:
        pass
    return False


def remove_outline_shell(mesh_obj):
    mod = mesh_obj.modifiers.get("HSR_Outline")
    if mod is not None:
        mesh_obj.modifiers.remove(mod)
    legacy = bpy.data.objects.get(mesh_obj.name + "_Outline")
    if legacy is not None:
        bpy.data.objects.remove(legacy, do_unlink=True)


_EDGE_MAT_PREFIX = "mmd_edge."
_EDGE_MAT_DISABLED = "mmd_edge.disabled"
_EDGE_MOD_NAME = "mmd_edge_preview"
_EDGE_THICK_BASELINE = 4.0  # 面板默认值 → 恰为 mmd_tools 自动算出的厚度(倍率1.0)


def _mmd_edge_roots(scene):
    """场景里所有 MMD 模型根对象。mmd_tools 的 ROOT 是一个 Empty(mmd_type=='ROOT'),
    armature 的 mmd_type 是 'NONE' —— 所以按 mmd_type 找, 不按 type。"""
    roots = []
    for o in scene.objects:
        try:
            if getattr(o, "mmd_type", None) == 'ROOT':
                roots.append(o)
        except Exception:
            pass
    return roots


def _mmd_edge_meshes(root):
    try:
        from mmd_tools.core.model import FnModel
        ms = list(FnModel.iterate_mesh_objects(root))
        if ms:
            return ms
    except Exception:
        pass
    # 回退: 递归收集 root 下所有 MESH 子孙(排除刚体)
    out = []
    def walk(o):
        for c in o.children:
            if c.type == 'MESH' and not c.name.startswith('mmd_tools_rigid'):
                out.append(c)
            walk(c)
    walk(root)
    return out


def _select_for_edge_op(meshes):
    """object 模式 + 选中一个网格为 active, 让 edge_preview_setup 找到 root。"""
    try:
        bpy.ops.object.mode_set(mode='OBJECT')
    except Exception:
        pass
    bpy.ops.object.select_all(action='DESELECT')
    meshes[0].select_set(True)
    try:
        bpy.context.view_layer.objects.active = meshes[0]
    except Exception:
        pass


def apply_mmd_edge(scene):
    """给场景内每个 MMD 模型建 Solidify 描边壳, 再套颜色/粗细。返回是否成功。
    ⚠ 先清后建: 每次重建前无条件 clear_mmd_edge, 彻底消除"上次残留的壳/基准厚度/孤儿描边
    材质"导致 CREATE 被跳过或结果不一致(同插件不同文件有的有描边有的没有)的非确定性。"""
    props = scene.hsr_toon
    roots = _mmd_edge_roots(scene)
    if not roots:
        return False
    try:
        clear_mmd_edge(scene)
    except Exception:
        pass
    created = False
    for root in roots:
        meshes = _mmd_edge_meshes(root)
        if not meshes:
            continue
        _select_for_edge_op(meshes)
        made_ok = False
        for attempt in range(2):
            try:
                bpy.ops.mmd_tools.edge_preview_setup(action='CREATE')
            except Exception as exc:
                print("[StarToon] mmd edge CREATE 异常(第%d次, %s):" % (attempt + 1, root.name), exc)
            # 关键: 验证 Solidify 壳 + 描边材质槽都真建出来了。算子可能只加了修改器
            # 却没建 mmd_edge.* 槽(静默失败 → 有壳无描边), 故两者都要验。
            has_sol = any(o.modifiers.get(_EDGE_MOD_NAME) is not None for o in meshes)
            has_edge_mat = any(
                s.material is not None and s.material.name.startswith(_EDGE_MAT_PREFIX)
                for o in meshes for s in o.material_slots)
            if has_sol and has_edge_mat:
                made_ok = True
                break
            try:
                bpy.context.view_layer.update()
            except Exception:
                pass
            _select_for_edge_op(meshes)
        if made_ok:
            created = True
        else:
            print("[StarToon] mmd edge CREATE 未生成描边壳(%s) → 稍后改用自建模描边" % root.name)
        # 记录 mmd_tools 自动算出的基准厚度(=0.2*empty_display_size), 供面板倍率映射。
        # 存物体(修改器不支持 IDProperties)。
        for o in meshes:
            mod = o.modifiers.get(_EDGE_MOD_NAME)
            if mod is not None and "hsr_edge_base_thick" not in o:
                o["hsr_edge_base_thick"] = float(mod.thickness)
    if not created:
        # 算子彻底失败 → 用纯 Python 自建模描边兜底(不依赖 mmd_tools 算子, 确定性)
        try:
            created = _build_outline_manual(scene)
            if created:
                print("[StarToon] 已用自建模描边兜底生成", flush=True)
        except Exception as exc:
            print("[StarToon] 自建模描边失败:", exc)
    if created:
        # 描边壳渲染模式: 只有头发的壳用 BLEND(不写深度→不挡眼透 AOV);
        # 其余部位回 HASHED(干脆, 不发淡)。全部关背面剔除(show_transparent_back=True +
        # use_backface_culling=False), 之前 show_transparent_back=False 会砍掉背面描边→变淡。
        for m in _iter_edge_materials():
            try:
                src = m.name[len(_EDGE_MAT_PREFIX):] if m.name.startswith(_EDGE_MAT_PREFIX) else m.name
                is_hair = classify(src) == "hair" or any(
                    k in src for k in ("髪", "发", "hair", "Hair"))
                m.blend_method = "BLEND" if is_hair else "HASHED"
                m.show_transparent_back = True
                m.use_backface_culling = False
                if hasattr(m, "surface_render_method"):
                    m.surface_render_method = "BLENDED" if is_hair else "DITHERED"
            except Exception:
                pass
        refresh_mmd_edge_color(scene)
        refresh_mmd_edge_thickness(scene)
        capture_edge_base(scene)
        refresh_mmd_edge_region(scene)
    return created


def clear_mmd_edge(scene):
    """删除 Solidify 描边壳(edge_preview_setup CLEAN) + 彻底清残留(基准厚度/缓存/孤儿描边材质)。"""
    for root in _mmd_edge_roots(scene):
        meshes = _mmd_edge_meshes(root)
        if not meshes:
            continue
        _select_for_edge_op(meshes)
        try:
            bpy.ops.mmd_tools.edge_preview_setup(action='CLEAN')
        except Exception as exc:
            print("[StarToon] mmd edge CLEAN 失败(%s):" % root.name, exc)
        # mmd_tools 只清它自己的 mmd_edge_preview; 再兜底删残留
        for o in meshes:
            # (a) 强制删掉可能残留的 Solidify 描边修改器(算子无操作时, 如无头)
            mod = o.modifiers.get(_EDGE_MOD_NAME)
            if mod is not None:
                try:
                    o.modifiers.remove(mod)
                except Exception:
                    pass
            # (b) 清基准厚度, 逼下次 apply 重新捕获
            if "hsr_edge_base_thick" in o:
                try:
                    del o["hsr_edge_base_thick"]
                except Exception:
                    pass
            vg = o.vertex_groups.get(_EDGE_BASE_VG)
            if vg is not None:
                try:
                    o.vertex_groups.remove(vg)
                except Exception:
                    pass
    # (c) 失效描边分区缓存
    try:
        _edge_bucket_cache.clear()
    except Exception:
        pass
    # (d) 清孤儿描边材质(槽位移除后 users==0)
    try:
        for m in [m for m in bpy.data.materials
                  if m.name.startswith(_EDGE_MAT_PREFIX) and m.users == 0]:
            bpy.data.materials.remove(m)
    except Exception:
        pass


def _iter_edge_materials():
    for m in bpy.data.materials:
        if not m.name.startswith(_EDGE_MAT_PREFIX) or m.name == _EDGE_MAT_DISABLED:
            continue
        if m.use_nodes and m.node_tree:
            yield m


def _hsr_edge_group():
    """描边着色节点组: 优先复用 mmd_tools 的 MMDEdgePreview(已验证正确);
    若不可用(未装/未启用 mmd_tools)则自建等价组: 相机/光泽射线 + 背面 → 彩色背景,
    其余 → 透明。这是 inverted-hull 描边只在轮廓显示黑色的关键。"""
    g = bpy.data.node_groups.get("MMDEdgePreview")
    if g is not None:
        return g
    g = bpy.data.node_groups.get("HSR_EdgePreview")
    if g is not None:
        return g
    g = bpy.data.node_groups.new("HSR_EdgePreview", "ShaderNodeTree")
    try:
        g.interface.new_socket("Color", in_out='INPUT', socket_type='NodeSocketColor')
        g.interface.new_socket("Alpha", in_out='INPUT', socket_type='NodeSocketFloat')
        g.interface.new_socket("Shader", in_out='OUTPUT', socket_type='NodeSocketShader')
    except Exception:
        return g
    n, lk = g.nodes, g.links
    gin = n.new("NodeGroupInput"); gin.location = (-800, 0)
    gout = n.new("NodeGroupOutput"); gout.location = (600, 0)
    lp = n.new("ShaderNodeLightPath"); lp.location = (-800, -250)
    geo = n.new("ShaderNodeNewGeometry"); geo.location = (-800, -450)
    m1 = n.new("ShaderNodeMath"); m1.operation = 'MAXIMUM'; m1.location = (-600, -300)
    m2 = n.new("ShaderNodeMath"); m2.operation = 'GREATER_THAN'; m2.location = (-420, -300)
    m3 = n.new("ShaderNodeMath"); m3.operation = 'MULTIPLY'; m3.location = (-240, -300)
    gam = n.new("ShaderNodeGamma"); gam.location = (-420, 150)
    bg = n.new("ShaderNodeBackground"); bg.location = (-120, 150)
    tr = n.new("ShaderNodeBsdfTransparent"); tr.location = (-120, -50)
    mix = n.new("ShaderNodeMixShader"); mix.location = (180, 0)
    lk.new(gin.outputs["Color"], gam.inputs["Color"])
    lk.new(lp.outputs["Is Camera Ray"], m1.inputs[0])
    lk.new(lp.outputs["Is Glossy Ray"], m1.inputs[1])
    lk.new(m1.outputs[0], m2.inputs[0])
    lk.new(geo.outputs["Backfacing"], m2.inputs[1])
    lk.new(m2.outputs[0], m3.inputs[0])
    lk.new(gin.outputs["Alpha"], m3.inputs[1])
    lk.new(gam.outputs["Color"], bg.inputs["Color"])
    lk.new(m3.outputs[0], mix.inputs["Fac"])
    lk.new(tr.outputs[0], mix.inputs[1])
    lk.new(bg.outputs[0], mix.inputs[2])
    lk.new(mix.outputs[0], gout.inputs["Shader"])
    return g


def _manual_edge_material(grp, base_name, edge_color, props):
    """建/取一个 mmd_edge.<base_name> 描边材质(挂 grp 到 Output.Surface), 组节点命名
    'mmd_edge_preview'(与算子一致, 供 refresh_mmd_edge_color 定位)。edge_color 为该材质
    PMX 原生描边色(alpha=0 即透明 disabled)。"""
    nm = _EDGE_MAT_PREFIX + base_name
    em = bpy.data.materials.get(nm)
    if em is None:
        em = bpy.data.materials.new(nm)
        em.use_nodes = True
        nt = em.node_tree
        nt.nodes.clear()
        gg = nt.nodes.new("ShaderNodeGroup")
        gg.name = "mmd_edge_preview"
        gg.node_tree = grp
        go = nt.nodes.new("ShaderNodeOutputMaterial")
        go.location = (300, 0)
        nt.links.new(gg.outputs["Shader"], go.inputs["Surface"])
    else:
        gg = em.node_tree.nodes.get("mmd_edge_preview")
        if gg is None:
            gg = next((n for n in em.node_tree.nodes if n.type == 'GROUP'), None)
        if gg is not None and gg.node_tree != grp:
            gg.node_tree = grp
    ec = tuple(edge_color) if edge_color and len(edge_color) >= 4 else (0.0, 0.0, 0.0, 1.0)
    if gg is not None:
        ci = gg.inputs.get("Color")
        if ci is not None:
            ci.default_value = ec
        ai = gg.inputs.get("Alpha")
        if ai is not None:
            ai.default_value = ec[3]
    em.blend_method = "HASHED"
    if hasattr(em, "surface_render_method"):
        em.surface_render_method = "DITHERED"
    em.show_transparent_back = True
    em.use_backface_culling = False
    return em


def _manual_edge_weights(o, nreal):
    """复刻算子 __create_edge_preview_group: 逐顶点权重 = mmd_edge_scale 权重 ×
    该顶点所属材质 edge_weight × 0.02。按四舍五入权重分桶批量 add(标量权重)。"""
    vg = o.vertex_groups.get(_EDGE_MOD_NAME)
    if vg is None:
        vg = o.vertex_groups.new(name=_EDGE_MOD_NAME)
    else:
        vg.remove(range(len(o.data.vertices)))
    wmap = {}
    for i, m in enumerate(o.data.materials[:nreal]):
        if m is not None:
            try:
                wmap[i] = float(m.mmd_material.edge_weight)
            except Exception:
                wmap[i] = 1.0
    smap = {}
    vs = o.vertex_groups.get("mmd_edge_scale")
    if vs is not None:
        for v in o.data.vertices:
            for g in v.groups:
                if g.group == vs.index:
                    smap[v.index] = g.weight
    vmat = {}
    for f in reversed(o.data.polygons):   # 与算子一致: reversed, 后写覆盖
        mi = f.material_index
        for vi in f.vertices:
            vmat[vi] = mi
    buckets = {}
    for i, mi in vmat.items():
        w = smap.get(i, 1.0) * wmap.get(mi, 1.0) * 0.02
        buckets.setdefault(round(w, 6), []).append(i)
    for w, idxs in buckets.items():
        vg.add(index=idxs, weight=w, type='REPLACE')


def _build_outline_manual(scene):
    """纯 Python 复刻 mmd_tools EdgePreviewSetup(__create_toon_edge + __create_edge_preview_group),
    不依赖算子(它在部分环境静默失败→描边凭空消失/时灵时不灵)。逐字段对齐算子:
    material_offset=原始材质数, thickness=0.2*root.empty_display_size, thickness_vertex_group=1e-3,
    offset=1, flip, rim off, even_offset off; 启用边的材质配彩色 mmd_edge.<名>, 未启用配透明
    mmd_edge.disabled; 逐顶点权重=edge_scale*edge_weight*0.02(薄壳, 只轮廓可见)。
    幂等, 逐网格异常隔离。"""
    props = getattr(scene, "hsr_toon", None)
    grp = _hsr_edge_group()
    if grp is None:
        return False
    roots = _mmd_edge_roots(scene)
    made = False
    for root in roots:
        try:
            scale = 0.2 * float(getattr(root, "empty_display_size", 0.4))
        except Exception:
            scale = 0.08
        if scale <= 1e-6:
            scale = 0.08
        for o in _mmd_edge_meshes(root):
            try:
                if o.hide_render:
                    continue   # 只处理可见渲染网格(跳过 ~400 个隐藏的按材质分件)
                if o.modifiers.get(_EDGE_MOD_NAME) is not None:
                    continue   # 该网格算子已建壳 → 跳过, 避免重复追加描边槽(多根混合成功)
                mats = o.data.materials
                material_offset = len(mats)
                if material_offset == 0:
                    continue
                # 逐原始材质: 启用边→彩色描边槽; 否则→共享 disabled 透明槽(与算子一致)
                for m in tuple(mats):
                    enabled = False
                    ec = (0.0, 0.0, 0.0, 1.0)
                    if m is not None:
                        try:
                            enabled = bool(m.mmd_material.enabled_toon_edge)
                            ec = tuple(m.mmd_material.edge_color)
                        except Exception:
                            enabled = False
                    if enabled:
                        mats.append(_manual_edge_material(grp, m.name, ec, props))
                    elif material_offset > 1:
                        mats.append(_manual_edge_material(grp, "disabled", (0.0, 0.0, 0.0, 0.0), props))
                if len(mats) <= material_offset:
                    continue   # 没有任何描边槽 → 该网格不建壳
                _manual_edge_weights(o, material_offset)
                sol = o.modifiers.get(_EDGE_MOD_NAME)
                if sol is None:
                    sol = o.modifiers.new(_EDGE_MOD_NAME, 'SOLIDIFY')
                sol.material_offset = material_offset
                sol.thickness_vertex_group = 1e-3
                sol.use_flip_normals = True
                sol.use_rim = False
                sol.offset = 1.0
                sol.use_even_offset = False
                sol.thickness = scale
                sol.vertex_group = _EDGE_MOD_NAME
                if "hsr_edge_base_thick" not in o:
                    o["hsr_edge_base_thick"] = scale
                made = True
            except Exception as exc:
                print("[StarToon] 手动描边失败(%s):" % o.name, exc)
    return made


def refresh_mmd_edge_color(scene):
    """描边颜色: 全局单色 → 覆盖所有 mmd_edge.* 的 Color 输入;
    跟随PMX → 还原各材质自己的 mmd_material.edge_color。
    MMDEdgePreview 着色器内部对 Color 做了 Gamma 2.2, 故这里直接喂 sRGB 语义值即可。"""
    props = scene.hsr_toon
    mode = getattr(props, "outline_color_mode", "GLOBAL")
    col = tuple(props.outline_color)
    for m in _iter_edge_materials():
        gn = m.node_tree.nodes.get("mmd_edge_preview")
        if gn is None:
            continue
        ci = gn.inputs.get("Color")
        if ci is None:
            continue
        if mode == "GLOBAL":
            ci.default_value = (col[0], col[1], col[2], 1.0)
        else:  # PMX
            try:
                ec = tuple(m.mmd_material.edge_color)
                ci.default_value = ec
            except Exception:
                pass


def refresh_mmd_edge_thickness(scene):
    """面板'描边粗细'→ Solidify 修改器 thickness = 基准 × (值/基线)。实时、不重建。"""
    props = scene.hsr_toon
    factor = float(props.outline_thickness) / _EDGE_THICK_BASELINE
    for o in scene.objects:
        if o.type != 'MESH':
            continue
        mod = o.modifiers.get(_EDGE_MOD_NAME)
        if mod is None:
            continue
        base = float(o.get("hsr_edge_base_thick", mod.thickness))
        mod.thickness = base * factor


# ---- 分段描边粗细(方案b: 区域倍率与 PMX 原生 edge_weight 相乘) ----
_EDGE_BASE_VG = "hsr_edge_base_w"  # 存 mmd_tools 原生 mmd_edge_preview 权重(未缩放基准)


def _region_mult(cat, props):
    """classify 类别 → 4 桶倍率。发/脸(face+eye)/肤/衣(其余全部)。"""
    if cat == "hair":
        return float(props.outline_w_hair)
    if cat in ("face", "eye"):
        return float(props.outline_w_face)
    if cat == "skin":
        return float(props.outline_w_skin)
    return float(props.outline_w_cloth)  # cloth/skirt/metal/shoe/default


_edge_bucket_cache = {}   # obj_name -> (sig, buckets); buckets=[(material_index, base_weight, [vidx...])]


def _get_edge_buckets(o):
    """缓存 每网格的 (材质槽, 原生基准权重, 顶点列表) 分桶。
    重活(遍历全部面建 顶点→材质 映射 + 逐顶点读基准权重)只在拓扑/材质变化时做一次;
    拖分段粗细滑杆时只读缓存 → 不再每 tick O(V+F) Python 卡顿。"""
    mats = o.data.materials
    sig = (o.data.name, len(o.data.vertices), len(o.data.polygons),
           tuple((m.name if m is not None else "") for m in mats))
    c = _edge_bucket_cache.get(o.name)
    if c is not None and c[0] == sig:
        return c[1]
    base = o.vertex_groups.get(_EDGE_BASE_VG)
    if base is None:
        _edge_bucket_cache[o.name] = (sig, [])
        return []
    v2mi = {}
    for f in reversed(o.data.polygons):
        mi = f.material_index
        for v in f.vertices:
            v2mi[v] = mi
    base_idx = base.index
    tmp = {}
    for v in o.data.vertices:
        bw = 0.0
        for g in v.groups:
            if g.group == base_idx:
                bw = g.weight
                break
        if bw == 0.0:
            continue
        tmp.setdefault((v2mi.get(v.index, -1), bw), []).append(v.index)
    buckets = [(mi, bw, idxs) for (mi, bw), idxs in tmp.items()]
    _edge_bucket_cache[o.name] = (sig, buckets)
    return buckets


def capture_edge_base(scene):
    """把 mmd_tools 刚建好的 mmd_edge_preview 权重原样备份到 hsr_edge_base_w。
    必须在任何区域缩放之前调用(apply_mmd_edge 里), 每次重建都重新捕获。"""
    for o in scene.objects:
        if o.type != 'MESH':
            continue
        vg = o.vertex_groups.get(_EDGE_MOD_NAME)
        if vg is None:
            continue
        _edge_bucket_cache.pop(o.name, None)   # 基准权重变了 → 失效分桶缓存
        base = o.vertex_groups.get(_EDGE_BASE_VG)
        if base is None:
            base = o.vertex_groups.new(name=_EDGE_BASE_VG)
        else:
            base.remove(range(len(o.data.vertices)))
        src_idx = vg.index
        buckets = {}
        for v in o.data.vertices:
            for g in v.groups:
                if g.group == src_idx:
                    if g.weight != 0.0:
                        buckets.setdefault(g.weight, []).append(v.index)
                    break
        for w, idxs in buckets.items():
            base.add(idxs, w, 'REPLACE')   # 按权重分桶批量, 避免逐顶点 add 的调用开销


def refresh_mmd_edge_region(scene):
    """按区域倍率重写 mmd_edge_preview 权重 = 原生基准 × 区域倍率。
    分桶缓存 → 每次只做 1 次 remove + 每桶 1 次 add, 拖滑杆不卡。"""
    props = scene.hsr_toon
    for o in scene.objects:
        if o.type != 'MESH':
            continue
        edge = o.vertex_groups.get(_EDGE_MOD_NAME)
        if edge is None:
            continue
        buckets = _get_edge_buckets(o)
        if not buckets:
            continue
        mats = o.data.materials
        no_ol = bool(getattr(props, "sheer_no_outline", True))
        edge.remove(range(len(o.data.vertices)))
        for mi, bw, idxs in buckets:
            m = mats[mi] if 0 <= mi < len(mats) and mats[mi] else None
            if no_ol and m is not None and _is_sheer(m.name):
                continue   # 纱/半透: 不给描边权重(黑描边壳会糊死薄纱)
            cat = classify(m.name) if m is not None else "default"
            mult = _region_mult(cat, props)
            edge.add(idxs, bw * mult, 'REPLACE')


_region_timer = None


def schedule_region_refresh(scene):
    """滑杆拖动时防抖: 停下 0.2s 才重算权重, 避免每 tick 全网格 O(V) 卡顿。"""
    global _region_timer
    if _region_timer is not None:
        try:
            bpy.app.timers.unregister(_region_timer)
        except Exception:
            pass
        _region_timer = None

    def _do():
        global _region_timer
        _region_timer = None
        try:
            refresh_mmd_edge_region(bpy.context.scene)
        except Exception as exc:
            print("[StarToon] region refresh err:", exc)
        return None

    _region_timer = bpy.app.timers.register(_do, first_interval=0.2)


def convert_selected(context, config):
    """转换选中物体：确保灯光/GN，然后逐材质构建。返回 (toon数, 保留数)。
    参考链的光照来自场景真实灯光(漫射BSDF)。场景已有 SUN 时直接用用户的灯,
    不再另建 HSR_SunLight 叠加(参考灯 0.87+0.49, 再叠 5.0 会过曝出色块)。"""
    scene = context.scene
    existing_suns = [o for o in scene.objects
                     if o.type == "LIGHT" and o.data.type == "SUN"]
    if existing_suns:
        sun_obj = existing_suns[0]
    else:
        sun_obj = ensure_sun_light(scene)
        # 新链灯光直接进漫射BSDF, 能量以面板 key_energy 为准(默认 0.87 对齐参考主灯),
        # 与 update_toon_params 用同一值 → 拖滑条不会再从 0.87 跳到 5.0
        sun_obj.data.energy = config["key_energy"]
    sun_obj["hsr_brightness_scale"] = config["brightness_scale"]
    sun_obj["hsr_brightness_min"] = config["brightness_min"]
    scene["hsr_sun_name"] = sun_obj.name   # 记录生效灯名, 供 get_sun_light 统一解析(修死名查找 bug)

    toon_count = 0
    keep_count = 0
    for obj in context.selected_objects:
        if obj.type != "MESH":
            continue
        ensure_virtual_sun_gn(obj, sun_obj)
        # 清理旧版几何前推眼透修改器(已废弃, 会致眼睛突出)
        if "HSR_EyeForward" in obj.modifiers:
            obj.modifiers.remove(obj.modifiers["HSR_EyeForward"])
        # 清理旧版内缩副本对象(已废弃, 改用 GN 外扩)
        old_shell = bpy.data.objects.get(obj.name + "_Outline")
        if old_shell is not None:
            bpy.data.objects.remove(old_shell, do_unlink=True)
        seen = set()
        obj_sun = _find_object_sun(obj, scene)   # 该角色根下父级的 SUN 灯(没有→None→用全局)
        obj_arm = _owner_armature(obj)           # 该角色的骨架(眼透头骨按角色独立)
        for slot in obj.material_slots:
            mat = slot.material
            if not mat or not mat.node_tree or mat.name in seen:
                continue
            seen.add(mat.name)
            # 跳过描边材质(HSR_Outline), 它是 GN 外扩壳专用, 不能被 toon 转换污染
            if mat.get("hsr_stage") == "outline":
                continue
            # 每角色各绑一盏灯: 记下本材质归属的太阳灯名(build 时喂进组输入'太阳方向')
            mat["hsr_sun_name"] = obj_sun.name if obj_sun is not None else None
            # 每角色各读自己头骨: 记下本材质归属的骨架名(眼透门控 build 时喂 face_fwd_dirs)
            mat["hsr_head_arm"] = obj_arm.name if obj_arm is not None else None
            # 通用半透判定: 在 build_toon_material 改动 blend_method 之前, 抓取 mmd_tools
            # 导入期按 MMD 材质真实 alpha 设定的 blend_method。半透(纱/叠加层/内侧)→BLEND。
            # 幂等守卫: 只有首次转换(紧跟干净导入)才记录; 再转换保留原值。
            # (头发在 convert 里会被改成 BLEND 走眼透不写深度通道 → 若事后再读会误判头发为半透)
            if "hsr_semi" not in mat:
                mat["hsr_semi"] = (getattr(mat, "blend_method", None) == "BLEND")
            sg = None
            for node in mat.node_tree.nodes:
                if node.type == "GROUP" and node.node_tree and node.node_tree.name.startswith("MMDShaderDev"):
                    sg = node
                    break
            if should_toon(mat, sg):
                if build_toon_material(mat, scene, sun_obj):
                    toon_count += 1
            else:
                mat["hsr_template"] = classify(mat.name) if mat.name else "default"
                mat["hsr_stage"] = "mmd_original"
                keep_count += 1
        # 几何挖孔眼透(定版): 髪面片烙「眼透删除」+ EyeCut GN(描边之前)
        # 描边引擎 v3: 清理遗留的手搓 GN 壳(HSR_Outline), 真正的描边改用 mmd_tools
        # Solidify 壳(edge_preview_setup), 在下面的循环外按整个 MMD 模型统一建一次。
        remove_outline_shell(obj)
    # 眼透 AOV: 循环【外】统一建一次 → 保证每个眼材质都已写好 hsr_head_arm, 各绑各角色头骨
    ensure_lash_aov()
    # 合成器眼透架构: convert 完成后建 AOV+合成器图(参考文件同款)
    if _use_compositor_arch(scene):
        setup_eye_through_compositor(scene)
    # 描边: 复用 mmd_tools Solidify 描边(edge_preview_setup) + 我们的颜色/粗细控制。
    # 按 MMD 模型 root 统一处理(读 PMX 的 enabled_toon_edge 决定哪些面描边)。
    try:
        if getattr(scene.hsr_toon, "outline_enabled", True):
            apply_mmd_edge(scene)
        else:
            clear_mmd_edge(scene)
    except Exception as exc:
        print("[StarToon] 描边(mmd edge)构建失败:", exc)
    return toon_count, keep_count


# ── 描边 (GN 逐面独立挤出 + 焊接 + Flip Faces inverted hull) ──
_OUTLINE_MAT_NAME = "HSR_Outline"
_OUTLINE_GN_NAME = "HSR_OutlineGN"

# 分区粗细: 部位组 → classify 类别 → 面板属性
# 逐面独立挤出, 每面用所属组宽度; 挤出后按距离焊接闭合接缝
_material_overrides = {}


def set_material_param(mat_name, param_name, value):
    """按材质名单独设参数(覆盖分类默认值)。
    agent 可用: set_material_param("鞋+", "dark_factor", 0.3)"""
    if mat_name not in _material_overrides:
        _material_overrides[mat_name] = {}
    _material_overrides[mat_name][param_name] = value


def get_material_param(mat_name, param_name, default=None):
    """读材质级覆盖参数。"""
    return _material_overrides.get(mat_name, {}).get(param_name, default)


def clear_material_override(mat_name=None):
    """清除材质级覆盖。mat_name=None 清除全部。"""
    if mat_name is None:
        _material_overrides.clear()
    else:
        _material_overrides.pop(mat_name, None)


def list_material_overrides():
    """列出所有材质级覆盖(agent 诊断用)。"""
    return dict(_material_overrides)


def apply_material_overrides_to_nodes(context):
    """把材质级覆盖写进对应材质的节点(agent 调参后调这个生效)。"""
    scene = context.scene
    updated = 0
    for mat in bpy.data.materials:
        if mat.get("hsr_stage") != "virtual_sun" or not mat.node_tree:
            continue
        overrides = _material_overrides.get(mat.name)
        if not overrides:
            continue
        nt = mat.node_tree
        changed = False
        for param, value in overrides.items():
            # 参数名 → 节点label + socket 映射
            mapping = {
                "dark_factor": ("暗度系数", "Value", "value"),
                "dark_saturation": ("暗路调色", "Saturation", "input"),
                "dark_hue_shift": ("暗路调色", "Hue", "input"),
                "dark_gamma": ("暗路伽玛", "Gamma", "input"),
                "dark_contrast": ("暗路对比度", "Contrast", "input"),
                "light_saturation": ("明路调色(粉嫩)", "Saturation", "input"),
                "light_hue_shift": ("明路调色(粉嫩)", "Hue", "input"),
                "light_value": ("明路调色(粉嫩)", "Value", "input"),
                "light_gamma": ("明路伽玛", "Gamma", "input"),
                "light_contrast": ("明路对比度", "Contrast", "input"),
                "brightness_scale": (None, None, None),  # 写灯光对象
            }
            info = mapping.get(param)
            if not info or not info[0]:
                continue
            label, socket, kind = info
            if kind == "value":
                changed |= _set_node_input(nt.nodes, label, socket, value)
            else:
                changed |= _set_node_input(nt.nodes, label, socket, value)
        if changed:
            updated += 1
    return updated


# ── 眼透(参考文件架构): 眼周材质写 AOV, 合成器把被头发挡住的眉眼混回, 零运行时成本 ──
# 可配置: 其他模型可通过 set_eye_aov_materials() 自定义
_EYE_AOV_MATERIALS = ("眉睫", "目", "目光")


def set_eye_aov_materials(materials):
    """设置需要写 AOV 的眼周材质名列表(通用化: 适配其他模型)。
    例: set_eye_aov_materials(["eyelash", "eye", "pupil"])"""
    global _EYE_AOV_MATERIALS
    _EYE_AOV_MATERIALS = tuple(materials)


def get_eye_aov_materials():
    """返回当前眼透 AOV 材质名列表。"""
    return _EYE_AOV_MATERIALS


# ── 头骨跟随门控: 驱动 face_fwd() + 角度滑块 ──────────────────
# 可配置: 其他模型可通过 set_head_bone_candidates() 自定义候选名
_HEAD_BONE_CANDIDATES = ('頭', 'head', 'Head', 'HEAD', '首', '头部', 'bip_head')


def set_head_bone_candidates(names):
    """设置头骨候选名列表(通用化: 适配其他模型)。
    例: set_head_bone_candidates(['head', 'Head', 'bip_head'])"""
    global _HEAD_BONE_CANDIDATES
    _HEAD_BONE_CANDIDATES = tuple(names)


def get_head_bone_candidates():
    """返回当前头骨候选名列表。"""
    return _HEAD_BONE_CANDIDATES


_head_cache = (None, None)


def _find_head_bone():
    """自动检测 armature 和头骨(带缓存)。返回 (armature_obj, pose_bone) 或 (None, None)。
    缓存命中即 O(1); 仅当模型/骨架变化(缓存失效)才重扫全物体——避免每 depsgraph 更新全表扫。"""
    global _head_cache
    arm, pb = _head_cache
    if arm is not None and pb is not None:
        try:
            a = bpy.data.objects.get(arm.name)
            if a is not None and a.type == 'ARMATURE' and pb.name in a.pose.bones:
                return a, a.pose.bones[pb.name]
        except (ReferenceError, RuntimeError):
            pass   # 缓存对象已被删除(mmd_tools 导入期会增删物体) → 失效重扫
        _head_cache = (None, None)
    for o in bpy.data.objects:
        if o.type == 'ARMATURE':
            for bn in _HEAD_BONE_CANDIDATES:
                p = o.pose.bones.get(bn)
                if p:
                    _head_cache = (o, p)
                    return o, p
    _head_cache = (None, None)
    return None, None


_sun_names_cache = None
_sun_names_count = -1


def _referenced_sun_names():
    """收集所有 toon 材质绑定的灯名(去重)。按材质数量变化失效, 稳态 O(1) 命中缓存。"""
    global _sun_names_cache, _sun_names_count
    if _sun_names_cache is not None and _sun_names_count == len(bpy.data.materials):
        return _sun_names_cache
    s = set()
    for m in bpy.data.materials:
        sn = m.get("hsr_sun_name")
        if sn:
            s.add(sn)
    _sun_names_cache = s
    _sun_names_count = len(bpy.data.materials)
    return s


def _owner_armature(obj):
    """返回驱动该 mesh 的骨架: Armature 修改器目标 → 父级骨架 → None。"""
    for mod in getattr(obj, "modifiers", ()):
        if mod.type == 'ARMATURE' and mod.object is not None and mod.object.type == 'ARMATURE':
            return mod.object
    p = obj.parent
    if p is not None and p.type == 'ARMATURE':
        return p
    return None


def _head_bone_of(arm):
    """在该骨架上找头骨(按候选名)。返回 pose_bone 或 None。"""
    if arm is None or arm.type != 'ARMATURE':
        return None
    for bn in _HEAD_BONE_CANDIDATES:
        pb = arm.pose.bones.get(bn)
        if pb:
            return pb
    return None


_head_arms_cache = None
_head_arms_count = -1


def _referenced_head_arms():
    """收集所有材质绑定的骨架名(hsr_head_arm, 去重)。按材质数量变化失效。"""
    global _head_arms_cache, _head_arms_count
    if _head_arms_cache is not None and _head_arms_count == len(bpy.data.materials):
        return _head_arms_cache
    s = set()
    for m in bpy.data.materials:
        an = m.get("hsr_head_arm")
        if an:
            s.add(an)
    _head_arms_cache = s
    _head_arms_count = len(bpy.data.materials)
    return s


_last_ns_sig = None
_tagging = False


@persistent
def _face_fwd_update(*_args):
    """在 depsgraph 求值【之后】刷新两个驱动变量:
    - face_fwd: 头骨 local+Z 世界朝向(眼透门控用)
    - sun_dir : HSR_SunLight 世界 +Z(指向光源方向, toon 明暗 dot 用)
    用 post 才能拿到刚评估的 pose_bone/物体矩阵 → 转物体/转头骨/转灯都即时更新。"""
    ns = bpy.app.driver_namespace
    # 虚拟太阳方向: SUN 灯沿局部 -Z 发光 → 指向光源 = 世界 +Z = matrix_world 第3列
    try:
        sun = get_sun_light()
        if sun is not None and sun.type == "LIGHT":
            d = sun.matrix_world.col[2].normalized()
            ns['sun_dir'] = (d.x, d.y, d.z)
    except Exception:
        pass
    # 每盏被引用的灯各算一份方向 → 材质驱动 hsr_sun_dirs[自身灯名] 各追各的; None=全局兜底
    try:
        glob = ns.get('sun_dir', (0.0, -0.57, 0.82))
        dirs = {None: tuple(glob)}
        for sn in _referenced_sun_names():
            o = bpy.data.objects.get(sn)
            if o is not None and o.type == "LIGHT":
                dd = o.matrix_world.col[2].normalized()
                dirs[sn] = (dd.x, dd.y, dd.z)
            else:
                dirs[sn] = tuple(glob)   # 灯被删→退全局, 保证键存在
        ns['hsr_sun_dirs'] = dirs
    except Exception:
        pass
    # 头骨朝向(眼透门控): 全局兜底 face_fwd
    glob_fwd = ns.get('face_fwd', (0.0, -1.0, 0.0))
    arm, pb = _find_head_bone()
    if arm is not None and pb is not None:
        try:
            c = (arm.matrix_world @ pb.matrix).col[2]
            glob_fwd = (c.x, c.y, c.z)
            ns['face_fwd'] = glob_fwd
        except Exception:
            pass
    # 每角色各读自己骨架的头骨 → 材质驱动 face_fwd_dirs[自身骨架名] 各追各的
    try:
        fdirs = {}
        for an in _referenced_head_arms():
            a = bpy.data.objects.get(an)
            hp = _head_bone_of(a)
            if hp is not None:
                cc = (a.matrix_world @ hp.matrix).col[2]
                fdirs[an] = (cc.x, cc.y, cc.z)
            else:
                fdirs[an] = tuple(glob_fwd)   # 骨架删了/无头骨→退全局, 保证键存在
        ns['face_fwd_dirs'] = fdirs
    except Exception:
        pass
    # 关键: driver_namespace 重新赋值不会自动让【材质节点树】的 SCRIPTED 驱动重算,
    # 转灯后阴影会冻在旧方向。检测到方向变化时, 主动 update_tag 材质树逼驱动刷新。
    global _last_ns_sig, _tagging
    if _tagging:
        return
    try:
        sig = (tuple(ns.get('sun_dir', ())),
               tuple(sorted(((repr(k), tuple(v)) for k, v in ns.get('hsr_sun_dirs', {}).items()))),
               tuple(ns.get('face_fwd', ())),
               tuple(sorted(((repr(k), tuple(v)) for k, v in ns.get('face_fwd_dirs', {}).items()))))
    except Exception:
        return
    if sig == _last_ns_sig:
        return
    _last_ns_sig = sig
    _tagging = True
    try:
        for m in bpy.data.materials:
            if m.get("hsr_stage") == "virtual_sun" and m.node_tree:
                m.node_tree.update_tag()
    except Exception:
        pass
    finally:
        _tagging = False


def _register_face_fwd_driver():
    """注册朝向/太阳方向更新: depsgraph_update_post 处理器写 face_fwd/sun_dir 向量,
    驱动只读 face_fwd[i]/sun_dir[i]。⚠ 绝不在驱动表达式里回调 Python 函数访问骨骼——
    5.2.2 求值中重入会崩溃(转身后卡死)。"""
    ns = bpy.app.driver_namespace
    if 'face_fwd' not in ns:
        ns['face_fwd'] = (0.0, -1.0, 0.0)
    if 'sun_dir' not in ns:
        ns['sun_dir'] = (0.0, -0.57, 0.82)   # 对应默认太阳 rot(35°,0,20°)的近似
    if 'hsr_sun_dirs' not in ns or not isinstance(ns.get('hsr_sun_dirs'), dict):
        ns['hsr_sun_dirs'] = {None: ns.get('sun_dir', (0.0, -0.57, 0.82))}
    if 'face_fwd_dirs' not in ns or not isinstance(ns.get('face_fwd_dirs'), dict):
        ns['face_fwd_dirs'] = {}
    h = bpy.app.handlers.depsgraph_update_post
    if _face_fwd_update not in h:
        h.append(_face_fwd_update)
    _face_fwd_update()


def update_eye_gate_angle(scene):
    """角度滑块回调: threshold = cos(半角) → 写入所有眼材质 GateThreshold 节点。"""
    props = getattr(scene, "hsr_toon", None)
    if not props or not hasattr(props, "eye_gate_angle"):
        return
    threshold = math.cos(props.eye_gate_angle)
    for mn in _EYE_AOV_MATERIALS:
        m = bpy.data.materials.get(mn)
        if m and m.node_tree:
            thr = next((n for n in m.node_tree.nodes if n.name == "GateThreshold"), None)
            if thr:
                thr.outputs[0].default_value = threshold


def _add_aov_to_material(m):
    """给单个材质补 透明(头骨跟随门控) + 透视(=贴图色) 两个 AOV 输出。幂等。

    门控公式: gate = clamp((Incoming·FaceForward - thr) / (1 - thr), 0, 1)
    - FaceForward: 头骨 local+Z 在世界空间的方向(SCRIPTED 驱动 face_fwd())
    - thr = cos(半角): 滑块控制, 默认 0.0 = 半角90° = 全范围180°
    - 旧版用 Normal·Incoming 会导致背面漏眼透, 头骨跟随门控修复此问题
    """
    if m is None or not m.use_nodes:
        return False
    nt = m.node_tree
    tex = next((n for n in nt.nodes if n.type == 'TEX_IMAGE' and n.image), None)
    have = {n.aov_name for n in nt.nodes if n.type == 'OUTPUT_AOV'}

    if '透明' not in have:
        _build_eye_gate(nt)
    else:
        # 已有 AOV: 检查是否旧门控(无 FaceForward 节点) → 替换
        has_new = any(n.name == "FaceForward" for n in nt.nodes)
        if not has_new:
            _remove_old_gate(nt)
            _build_eye_gate(nt)

    if '透视' not in have and tex is not None:
        a = nt.nodes.new('ShaderNodeOutputAOV')
        a.aov_name = '透视'
        a.location = (600, -500)
        nt.links.new(tex.outputs['Color'], a.inputs['Color'])
    return True


def _remove_old_gate(nt):
    """移除旧版 Normal·Incoming 门控节点(无 FaceForward 的几何/矢量/钳制)。"""
    for n in list(nt.nodes):
        if n.type == 'OUTPUT_AOV' and n.aov_name == '透明':
            nt.nodes.remove(n)
        elif n.name == "" and n.type in ('ShaderNodeNewGeometry', 'ShaderNodeVectorMath', 'ShaderNodeClamp'):
            # 旧版节点没有命名, 按类型清除孤儿
            nt.nodes.remove(n)


def _build_eye_gate(nt):
    """在材质节点树中构建头骨跟随门控链 → AOV '透明'(Value)。
    每角色读【自己骨架】的头骨(mat['hsr_head_arm']); 无骨架/无头骨→退全局 face_fwd。"""
    _register_face_fwd_driver()
    mat = nt.id_data
    arm_key = mat.get("hsr_head_arm") if hasattr(mat, "get") else None
    arm_obj = bpy.data.objects.get(arm_key) if arm_key else None
    pb = _head_bone_of(arm_obj)
    if arm_obj is None or pb is None:
        arm_obj, gpb = _find_head_bone()
        bone_name = gpb.name if gpb else '頭'
        arm_key = None          # 用全局 face_fwd
    else:
        bone_name = pb.name
        ns = bpy.app.driver_namespace
        fd = ns.get('face_fwd_dirs')
        if not isinstance(fd, dict):
            fd = {}; ns['face_fwd_dirs'] = fd
        if arm_key not in fd:   # 建时补种, 避免首帧 KeyError
            try:
                cc = (arm_obj.matrix_world @ pb.matrix).col[2]
                fd[arm_key] = (cc.x, cc.y, cc.z)
            except Exception:
                fd[arm_key] = tuple(ns.get('face_fwd', (0.0, -1.0, 0.0)))

    # 读取当前角度阈值
    scene = bpy.context.scene
    props = getattr(scene, "hsr_toon", None)
    threshold = math.cos(props.eye_gate_angle) if props and hasattr(props, "eye_gate_angle") else 0.0

    # FaceForward: CombineXYZ, X/Y/Z 各挂 SCRIPTED 驱动(读本材质骨架头骨方向, 无则全局)
    ff = nt.nodes.new('ShaderNodeCombineXYZ')
    ff.name = "FaceForward"
    ff.location = (-20, -280)
    for i, comp in enumerate(('X', 'Y', 'Z')):
        fc = ff.inputs[i].driver_add('default_value')
        drv = fc.driver
        drv.type = 'SCRIPTED'
        drv.expression = ("face_fwd_dirs[%r][%d]" % (arm_key, i)) if arm_key else ('face_fwd[%d]' % i)
        # Transform Channel 变量: 让 depsgraph 追踪【本角色】头骨旋转(变量值未使用, 仅建依赖)
        for axis in ('X', 'Y', 'Z'):
            var = drv.variables.new()
            var.name = 'head_rot_%s' % axis.lower()
            var.type = 'TRANSFORMS'
            tgt = var.targets[0]
            tgt.id = arm_obj
            tgt.bone_target = bone_name
            tgt.transform_type = 'ROT_%s' % axis
            tgt.transform_space = 'WORLD_SPACE'

    # 几何数据.Incoming
    geo = nt.nodes.new('ShaderNodeNewGeometry')
    geo.location = (100, -400)

    # dot = Incoming · FaceForward
    dot = nt.nodes.new('ShaderNodeVectorMath')
    dot.operation = 'DOT_PRODUCT'
    dot.location = (280, -400)
    nt.links.new(geo.outputs['Incoming'], dot.inputs[0])
    nt.links.new(ff.outputs[0], dot.inputs[1])

    # GateThreshold (Value)
    thr = nt.nodes.new('ShaderNodeValue')
    thr.name = "GateThreshold"
    thr.location = (280, -520)
    thr.outputs[0].default_value = threshold

    # GateSub1 = dot - threshold
    sub1 = nt.nodes.new('ShaderNodeMath')
    sub1.operation = 'SUBTRACT'
    sub1.location = (360, -440)
    sub1.name = "GateSub1"
    nt.links.new(dot.outputs['Value'], sub1.inputs[0])
    nt.links.new(thr.outputs[0], sub1.inputs[1])

    # GateSub2 = 1 - threshold
    sub2 = nt.nodes.new('ShaderNodeMath')
    sub2.operation = 'SUBTRACT'
    sub2.location = (360, -560)
    sub2.name = "GateSub2"
    sub2.inputs[0].default_value = 1.0
    nt.links.new(thr.outputs[0], sub2.inputs[1])

    # GateDiv = GateSub1 / GateSub2
    div = nt.nodes.new('ShaderNodeMath')
    div.operation = 'DIVIDE'
    div.location = (440, -480)
    div.name = "GateDiv"
    nt.links.new(sub1.outputs[0], div.inputs[0])
    nt.links.new(sub2.outputs[0], div.inputs[1])

    # Clamp(0, 1)
    clamp = nt.nodes.new('ShaderNodeClamp')
    clamp.location = (460, -400)
    clamp.inputs['Min'].default_value = 0.0
    clamp.inputs['Max'].default_value = 1.0
    nt.links.new(div.outputs[0], clamp.inputs['Value'])

    # 仰角门: 视线竖直分量越大(越接近正上/正下俯视)越关掉,
    # 防止放宽水平角后眼透漏到头顶(侧视与俯视点积都≈0, 只能靠竖直分量区分)
    sepz = nt.nodes.new('ShaderNodeSeparateXYZ')
    sepz.location = (460, -660)
    nt.links.new(geo.outputs['Incoming'], sepz.inputs['Vector'])
    absz = nt.nodes.new('ShaderNodeMath')
    absz.operation = 'ABSOLUTE'; absz.location = (600, -660); absz.name = "GateElevAbs"
    nt.links.new(sepz.outputs['Z'], absz.inputs[0])
    elev = nt.nodes.new('ShaderNodeMapRange')
    elev.location = (740, -660); elev.name = "GateElev"; elev.clamp = True
    elev.inputs['From Min'].default_value = 0.35   # |Z|<0.35(≈<20°仰) 全开
    elev.inputs['From Max'].default_value = 0.75   # |Z|>0.75(≈>49°仰) 全关
    elev.inputs['To Min'].default_value = 1.0
    elev.inputs['To Max'].default_value = 0.0
    nt.links.new(absz.outputs['Value'], elev.inputs['Value'])
    gate_mul = nt.nodes.new('ShaderNodeMath')
    gate_mul.operation = 'MULTIPLY'; gate_mul.location = (520, -400); gate_mul.name = "GateElevMul"
    nt.links.new(clamp.outputs['Result'], gate_mul.inputs[0])
    nt.links.new(elev.outputs['Result'], gate_mul.inputs[1])

    # 兜底: face_fwd 缺失时(重开文件驱动未求值 → CombineXYZ 退化为 (0,0,0)),
    # dot(Incoming,0)=0 会让门控恒定半开(cos(宽角)<0 时更明显) → 脑后漏眼。
    # 用 FaceForward 长度做开关: 长度≈0 → 门控强制归零(关闭), 长度≈1 → 正常。
    flen = nt.nodes.new('ShaderNodeVectorMath')
    flen.operation = 'LENGTH'; flen.location = (520, -260); flen.name = "GateFwdLen"
    nt.links.new(ff.outputs[0], flen.inputs[0])
    fmul = nt.nodes.new('ShaderNodeMath')
    fmul.operation = 'MULTIPLY'; fmul.location = (660, -260); fmul.name = "GateFwdScale"
    fmul.inputs[1].default_value = 10.0
    nt.links.new(flen.outputs['Value'], fmul.inputs[0])
    fmin = nt.nodes.new('ShaderNodeMath')
    fmin.operation = 'MINIMUM'; fmin.location = (800, -260); fmin.name = "GateFwdClamp"
    fmin.inputs[1].default_value = 1.0
    nt.links.new(fmul.outputs['Value'], fmin.inputs[0])
    gate_final = nt.nodes.new('ShaderNodeMath')
    gate_final.operation = 'MULTIPLY'; gate_final.location = (940, -400); gate_final.name = "GateFinal"
    nt.links.new(gate_mul.outputs['Value'], gate_final.inputs[0])
    nt.links.new(fmin.outputs['Value'], gate_final.inputs[1])

    # AOV 输出 '透明'
    a = nt.nodes.new('ShaderNodeOutputAOV')
    a.aov_name = '透明'
    a.location = (1080, -400)
    nt.links.new(gate_final.outputs['Value'], a.inputs['Value'])


def _is_eye_region_material(m):
    """按多语言关键词判定"眼周材质"(眼球/眼白/瞳孔/目光/睫毛/眉), 用于写眼透 AOV。
    排除眼镜/眼罩等佩戴饰品(它们该有描边, 不参与眼透)。不写死具体材质名。"""
    n = m.name
    low = n.lower()
    # 描边壳材质(mmd_edge.*)是 Solidify 外扩壳, 非眼球本体, 不参与眼透
    if n.startswith("mmd_edge."):
        return False
    if any(k in n for k in ("眼镜", "眼罩", "镜框", "墨镜", "面具")):
        return False
    if any(k in n for k in ("目", "眼", "瞳", "睫", "眉")):
        return True
    if any(k in low for k in ("eye", "iris", "pupil", "lash", "sclera", "brow")):
        return True
    return False


def _eye_aov_targets():
    """眼透 AOV 目标材质集合 = 显式配置的 _EYE_AOV_MATERIALS(若存在) ∪ 关键词命中的眼周材质。"""
    explicit = set(_EYE_AOV_MATERIALS)
    mats, seen = [], set()
    for m in bpy.data.materials:
        if m.name in seen or not m.use_nodes or not m.node_tree:
            continue
        if m.name in explicit or _is_eye_region_material(m):
            mats.append(m)
            seen.add(m.name)
    return mats


def ensure_lash_aov():
    """给眼周材质写 透视/透明 AOV, 使整个眼睛都能透过头发被合成器混回。
    泛用版: 按关键词识别, 不再只认昔涟的 眉睫/目/目光(换模型就静默失效)。幂等。"""
    done = False
    for m in _eye_aov_targets():
        if _add_aov_to_material(m):
            done = True
    return done


# ── 眼透架构: 视图层 AOV 注册 + 合成器 MIX/SetAlpha 图(复刻参考文件"合成器节点") ──
_EYE_AOV_COLOR = "透视"
_EYE_AOV_MASK = "透明"
_HAIR_MASK_AOV = "描边掩膜"
_FACE_MASK_AOV = "面部掩膜"


def ensure_eye_through_aov_channels(scene):
    """视图层注册 透视(COLOR) + 透明(VALUE) + 描边掩膜(VALUE) + 面部掩膜(VALUE) AOV 通道。幂等。"""
    vl = scene.view_layers[0]
    for aov_name, aov_type in ((_EYE_AOV_COLOR, 'COLOR'), (_EYE_AOV_MASK, 'VALUE'),
                               (_HAIR_MASK_AOV, 'VALUE'), (_FACE_MASK_AOV, 'VALUE')):
        if not any(a.name == aov_name for a in vl.aovs):
            a = vl.aovs.add()
            a.name = aov_name
            a.type = aov_type


def ensure_face_mask_aov():
    """给面部材质(_cat_of_material=='face')写 AOV 面部掩膜=1.0, 供合成器把深度 Sobel
    描边限制在面部区域(面部走合成器, 其他走壳)。幂等。"""
    for m in bpy.data.materials:
        if not m.use_nodes or not m.node_tree:
            continue
        if _cat_of_material(m) != 'face':
            continue
        nt = m.node_tree
        if any(n.type == 'OUTPUT_AOV' and getattr(n, "aov_name", "") == _FACE_MASK_AOV
               for n in nt.nodes):
            continue
        a = nt.nodes.new('ShaderNodeOutputAOV')
        a.aov_name = _FACE_MASK_AOV
        a.location = (600, -800)
        a.inputs['Value'].default_value = 1.0


def ensure_hair_outline_mask():
    """给头发材质写 AOV 描边掩膜=1.0, 供合成器只对头发剪影做 Sobel 描边。幂等。"""
    for m in bpy.data.materials:
        if not m.use_nodes or not m.node_tree:
            continue
        is_hair = m.get("hsr_template") == "hair" or any(
            k in m.name for k in ("髪", "发", "hair", "Hair", "ツインテール", "三つ編み"))
        if not is_hair:
            continue
        nt = m.node_tree
        if any(n.type == 'OUTPUT_AOV' and getattr(n, "aov_name", "") == _HAIR_MASK_AOV
               for n in nt.nodes):
            continue
        a = nt.nodes.new('ShaderNodeOutputAOV')
        a.aov_name = _HAIR_MASK_AOV
        a.location = (600, -720)
        v = nt.nodes.new('ShaderNodeValue')
        v.outputs[0].default_value = 1.0
        v.location = (400, -720)
        sock = a.inputs['Value'] if 'Value' in a.inputs else a.inputs[0]
        nt.links.new(v.outputs[0], sock)


def setup_eye_through_compositor(scene):
    """合成器后期链(5.2 验证): 眼透 AOV 混合 + 深度边缘描边, 串在同一节点树。

    眼透(参考文件同款): 渲染层.透明×强度 → 混合Factor, 渲染层.Image=A, 透视=B
                        → 设置Alpha(还原原alpha)
    描边(use_compositor_outline, 面部合成器模式): 渲染层.Depth → 归一化 → Filter(Sobel)
                        → × 面部掩膜(只留面部) → 边缘作Factor, 上一步结果=A, 描边色=B → 输出
    """
    ensure_eye_through_aov_channels(scene)
    ensure_lash_aov()
    # 眼透是合成器后期, 必须开渲染的 Compositing 才会生效(默认虽 True, 但场景可能被
    # 关掉/视口预览不走合成器)。这里显式保证, 免得用户以为眼透坏了。
    try:
        scene.render.use_compositing = True
    except Exception:
        pass
    props = getattr(scene, "hsr_toon", None)
    outline_enabled = bool(getattr(props, "outline_enabled", True)) if props else False
    use_comp_outline = bool(getattr(props, "use_compositor_outline", False)) if props else False
    if use_comp_outline:
        ensure_face_mask_aov()
    # 描边模式: face=面部走合成器深度Sobel(其他走壳); None=全身壳描边
    outline_mode = "face" if use_comp_outline else None
    use_outline = outline_mode is not None
    line_w = float(getattr(props, "outline_line_width", 1.0)) if props else 1.0
    face_px = max(1, int(round(line_w * 3.0)))   # 面部描边加粗像素(线宽滑条→厚度)
    # 眼透 factor = 透明 × strength。对齐参考文件的 0.5(之前 ×2 导致 factor≈0.9 冲发白)
    strength = (float(getattr(props, "eye_through_strength", 0.5)) if props else 0.5)
    ocolor = tuple(props.outline_color) if props else (0.02, 0.02, 0.05)
    # 眼透半径: 对 透明 遮罩做柔边/扩散(越大眼透范围越扩散); radius×500→模糊像素
    radius = float(getattr(props, "eye_through_radius", 0.02)) if props else 0.02
    blur_px = max(0, int(round(radius * 500)))

    # 深度描边需要深度通道(面部合成器模式基于 Depth)
    if outline_mode is not None:
        scene.view_layers[0].use_pass_z = True

    if not scene.use_nodes:
        scene.use_nodes = True
    cg = scene.compositing_node_group
    if cg is None:
        cg = bpy.data.node_groups.new("EyeThroughCompositor", 'CompositorNodeTree')
        scene.compositing_node_group = cg
    if not any(s.name == "Image" and s.in_out == 'OUTPUT' for s in cg.interface.items_tree):
        cg.interface.new_socket("Image", in_out='OUTPUT', socket_type='NodeSocketColor')

    # 已建链 → 只刷新参数(眼透强度/描边开关/线宽/颜色), 不重建
    if any(n.label == "眼透MIX" for n in cg.nodes):
        for n in cg.nodes:
            if n.label == "眼透强度" and n.type == 'MATH':
                n.inputs[1].default_value = round(strength, 3)
            elif n.label == "眼透半径" and n.type == 'BLUR':
                try:
                    s = n.inputs["Size"]
                    try:
                        s.default_value = blur_px
                    except Exception:
                        s.default_value = (blur_px, blur_px)
                except Exception:
                    pass
            elif n.label == "描边加粗" and n.type == 'BLUR':
                try:
                    s = n.inputs["Size"]
                    try:
                        s.default_value = (face_px, face_px)
                    except Exception:
                        s.default_value = face_px
                except Exception:
                    pass
            elif n.label == "描边颜色" and n.type == 'RGB':
                n.outputs[0].default_value = (*ocolor, 1.0)
        # 描边开关/模式变化需重建(链结构变: depth↔hair↔none)
        has_outline = any(n.label == "描边Sobel" for n in cg.nodes)
        cur_mode = cg.get("st_outline_mode", "face" if has_outline else None)
        if has_outline != use_outline or cur_mode != outline_mode:
            for n in list(cg.nodes):
                cg.nodes.remove(n)
        else:
            return

    if not cg.nodes:
        for n in list(cg.nodes):
            cg.nodes.remove(n)

        rl = cg.nodes.new("CompositorNodeRLayers")
        rl.location = (-900, 0)
        rl.layer = scene.view_layers[0].name

        # ── 对齐参考"合成器节点"图 ──
        # 分离颜色(取渲染层 Image 的 alpha 给 SetAlpha)
        sep = cg.nodes.new("CompositorNodeSepColor") if hasattr(bpy.types, "CompositorNodeSepColor") else cg.nodes.new("CompositorNodeSeparateColor")
        sep.location = (-500, -300); sep.label = "分离颜色"
        cg.links.new(rl.outputs["Image"], sep.inputs["Image"])

        # 相乘: 透明(可扩散) × 强度 → 混合系数
        math_n = cg.nodes.new("ShaderNodeMath")
        math_n.location = (-500, -550); math_n.operation = 'MULTIPLY'
        math_n.inputs[1].default_value = round(strength, 3); math_n.label = "眼透强度"
        if blur_px > 0:
            blur = cg.nodes.new("CompositorNodeBlur")
            blur.location = (-680, -550); blur.label = "眼透半径"
            try:
                s = blur.inputs["Size"]
                try:
                    s.default_value = blur_px
                except Exception:
                    s.default_value = (blur_px, blur_px)
            except Exception:
                pass
            cg.links.new(rl.outputs[_EYE_AOV_MASK], blur.inputs["Image"])
            cg.links.new(blur.outputs["Image"], math_n.inputs[0])
        else:
            cg.links.new(rl.outputs[_EYE_AOV_MASK], math_n.inputs[0])

        # 混合: A=渲染层图像, B=透视, Fac=透明×0.5
        mix = cg.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"; mix.blend_type = 'MIX'
        mix.location = (-250, 100); mix.label = "眼透MIX"
        cg.links.new(math_n.outputs["Value"], mix.inputs[0])
        cg.links.new(rl.outputs["Image"], mix.inputs[6])
        cg.links.new(rl.outputs[_EYE_AOV_COLOR], mix.inputs[7])

        # 设置 Alpha: 图像=混合结果, Alpha=分离颜色.Alpha
        set_alpha = cg.nodes.new("CompositorNodeSetAlpha")
        set_alpha.location = (0, 100); set_alpha.label = "还原alpha"
        cg.links.new(mix.outputs[2], set_alpha.inputs["Image"])
        cg.links.new(sep.outputs["Alpha"], set_alpha.inputs["Alpha"])
        prev_out = set_alpha.outputs["Image"]

        # ── 描边: 面部合成器模式 = 深度Sobel → 加粗 → ×面部掩膜(只在面部出边) ──
        if use_outline:
            sobel = cg.nodes.new("CompositorNodeFilter")
            sobel.location = (-50, -350); sobel.label = "描边Sobel"
            try:
                sobel.inputs["Type"].default_value = 'Sobel'
                sobel.inputs["Factor"].default_value = 1.0   # 固定边缘检测强度
            except Exception:
                pass
            nrm = cg.nodes.new("CompositorNodeNormalize")
            nrm.location = (-250, -350); nrm.label = "深度归一"
            cg.links.new(rl.outputs["Depth"], nrm.inputs["Value"])
            cg.links.new(nrm.outputs["Value"], sobel.inputs["Image"])
            # 加粗: 模糊把 1~2px 的 Sobel 边扩成 face_px 宽的带, 再 Map Range 硬化成利边
            dil = cg.nodes.new("CompositorNodeBlur")
            dil.location = (20, -420); dil.label = "描边加粗"
            try:
                dil.filter_type = 'FAST'
                s = dil.inputs["Size"]
                try:
                    s.default_value = (face_px, face_px)
                except Exception:
                    s.default_value = face_px
            except Exception:
                pass
            cg.links.new(sobel.outputs["Image"], dil.inputs["Image"])
            hard = cg.nodes.new("ShaderNodeMath")
            hard.operation = 'GREATER_THAN'
            hard.location = (120, -460); hard.label = "描边硬化"
            try:
                hard.inputs[1].default_value = 0.15
            except Exception:
                pass
            cg.links.new(dil.outputs["Image"], hard.inputs[0])
            # 用面部掩膜把边缘限制在面部区域(身体/头发处掩膜=0 → 不叠加合成器边, 避免与壳重边)
            fmask = cg.nodes.new("ShaderNodeMath")
            fmask.operation = 'MULTIPLY'
            fmask.location = (220, -250); fmask.label = "×面部掩膜"
            cg.links.new(hard.outputs['Value'], fmask.inputs[0])
            cg.links.new(rl.outputs[_FACE_MASK_AOV], fmask.inputs[1])
            omix = cg.nodes.new("ShaderNodeMix")
            omix.data_type = "RGBA"; omix.blend_type = 'MIX'
            omix.location = (200, -100); omix.label = "描边MIX"
            orgb = cg.nodes.new("CompositorNodeRGB")
            orgb.location = (0, -350); orgb.label = "描边颜色"
            orgb.outputs[0].default_value = (*ocolor, 1.0)
            cg.links.new(fmask.outputs["Value"], omix.inputs[0])
            cg.links.new(prev_out, omix.inputs[6])
            cg.links.new(orgb.outputs[0], omix.inputs[7])
            prev_out = omix.outputs[2]
            cg["st_outline_mode"] = outline_mode

        # ── 辉光(Glare): 默认关(面板 use_glare 开), 开着时做泛光 ──
        # ⚠ 用 Blender 5.x socket API。旧属性 glare_type/quality/threshold/size/mix 在 5.2
        #   已不存在(赋值抛 AttributeError 被吞 → 节点停在默认 Streaks 且参数全无 = 哑火)。
        #   5.x 里 Type/Quality 的枚举标识是带空格显示串('Fog Glow'/'High'), 实测确认。
        use_glare = bool(getattr(props, "use_glare", False)) if props else False
        if use_glare:
            glare = cg.nodes.new("CompositorNodeGlare")
            glare.location = (400, 0); glare.label = "辉光"
            def _gset(_name, _val):
                try:
                    glare.inputs[_name].default_value = _val
                except Exception:
                    pass
            _gset("Type", getattr(props, "glare_type", 'Fog Glow'))
            _gset("Quality", getattr(props, "glare_quality", 'High'))
            _gset("Threshold", float(getattr(props, "glare_threshold", 0.8)))
            _gset("Size", float(getattr(props, "glare_size", 0.5)))
            _gset("Strength", float(getattr(props, "glare_strength", 1.0)))
            cg.links.new(prev_out, glare.inputs["Image"])
            final_img = glare.outputs["Image"]
        else:
            final_img = prev_out

        viewer = cg.nodes.new("CompositorNodeViewer")
        viewer.location = (650, -260); viewer.label = "合成预览"
        out = cg.nodes.new("NodeGroupOutput")
        out.location = (650, 0)
        cg.links.new(final_img, viewer.inputs["Image"])
        cg.links.new(final_img, out.inputs[0])





