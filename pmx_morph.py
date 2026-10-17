import os
import bpy


def _pmx_module():
    """惰性解析 mmd_tools 的 PMX 读取模块并返回。
    ⚠ 绝不在模块顶层 import mmd_tools/pmx, 也不写死 sys.path —— 否则只要 mmd_tools
    没先启用, `from . import pmx_morph` 会在插件加载期抛 ImportError 直接拖垮整个插件
    (换机器/换 Blender 版本尤其致命)。
    Blender 4.5+/5.x 里 mmd_tools 是扩展, 真实包名是 bl_ext.<repo>.mmd_tools, 直接
    `import mmd_tools` 反而失败(旧代码那句写死 sys.path 就是在硬凑这个)。这里按候选包名
    逐个尝试, 不依赖任何绝对路径; 全都失败才抛清晰 RuntimeError(由调用方兜底)。"""
    import importlib
    last = None
    for base in ("bl_ext.blender_org.mmd_tools",
                 "bl_ext.user_default.mmd_tools",
                 "mmd_tools"):
        try:
            return importlib.import_module(base + ".core.pmx")
        except ImportError as exc:
            last = exc
    raise RuntimeError(
        "读取 PMX 形态需要 mmd_tools 扩展: 请先在『偏好设置 → 插件』启用 mmd_tools 后重试。(原始错误: %s)" % last)


def _find_pmx_and_model(context):
    """从用户选中的网格对象的贴图路径反推 PMX, 返回 (path, 已加载model)。
    贴图目录下有多个 PMX 时, 用材质名匹配确定是哪个。⚠ 打分时加载的 model 直接返回复用,
    不再二次 pmx.load(修 🟠: 选中文件被整文件解析两遍)。只有一个候选时直接用它。"""
    tex_dirs = set()
    for obj in context.selected_objects:
        if obj.type != "MESH":
            continue
        for mat in obj.data.materials:
            if not mat or not mat.node_tree:
                continue
            for node in mat.node_tree.nodes:
                if node.type == "TEX_IMAGE" and node.image:
                    fp = bpy.path.abspath(node.image.filepath)
                    if fp:
                        tex_dirs.add(os.path.dirname(fp))

    # 在贴图目录和上级目录找所有 PMX
    candidates = []
    for d in tex_dirs:
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.lower().endswith(".pmx"):
                    candidates.append(os.path.join(d, f))
        parent = os.path.dirname(d)
        if parent and os.path.isdir(parent):
            for f in os.listdir(parent):
                if f.lower().endswith(".pmx"):
                    p = os.path.join(parent, f)
                    if p not in candidates:
                        candidates.append(p)

    if not candidates:
        return None, None

    pmx = _pmx_module()
    # 只有一个候选 → 无需打分, 直接加载一次
    if len(candidates) == 1:
        try:
            return candidates[0], pmx.load(candidates[0])
        except Exception:
            return None, None

    # 收集用户选中模型的材质名
    scene_mat_names = set()
    for obj in context.selected_objects:
        if obj.type == "MESH":
            for slot in obj.material_slots:
                if slot.material:
                    scene_mat_names.add(slot.material.name)

    # 逐个 PMX 试读, 匹配材质名最多的就是正确文件; 保留其 model 复用
    best_path = None
    best_model = None
    best_score = 0
    for path in candidates:
        try:
            model = pmx.load(path)
            pmx_mat_names = set(m.name for m in model.materials)
            score = len(scene_mat_names & pmx_mat_names)
            if score > best_score:
                best_score = score
                best_path = path
                best_model = model
        except Exception:
            continue

    return best_path, best_model


def _extract_morphs(model):
    """从【已加载】的 pmx model 提取 material morph + group morph → {name: [(mat, op),...]}。
    纯内存, 不读文件(修 🟠: 避免对选中 PMX 二次整文件解析)。"""
    material_names = [m.name for m in model.materials]
    morph_names = [getattr(m, 'name', '?').strip() for m in model.morphs]

    material_morphs = {}
    group_morphs = {}

    for morph in model.morphs:
        name = getattr(morph, 'name', '?').strip()
        data = getattr(morph, 'data', getattr(morph, 'offsets', []))
        if not data:
            continue
        first = data[0]
        if hasattr(first, 'diffuse_offset') or hasattr(first, 'offset_type'):
            ops = []
            for d in data:
                mi = getattr(d, 'index', -1)
                ot = getattr(d, 'offset_type', getattr(d, 'type', -1))
                df = getattr(d, 'diffuse_offset', getattr(d, 'diffuse', None))
                if mi < 0 or mi >= len(material_names) or df is None:
                    continue
                alpha_offset = df[3] if len(df) > 3 else 0.0
                mat_name = material_names[mi]
                if ot == 1 and alpha_offset > 0.5:
                    ops.append((mat_name, "show"))
                elif ot == 0 and alpha_offset < 0.5:
                    ops.append((mat_name, "hide"))
            if ops:
                material_morphs[name] = ops
        elif hasattr(first, 'factor') and hasattr(first, 'morph'):
            refs = []
            for d in data:
                mi = getattr(d, 'morph', getattr(d, 'morph_index', -1))
                factor = getattr(d, 'factor', getattr(d, 'value', 0))
                if 0 <= mi and factor > 0.01:
                    refs.append(mi)
            if refs:
                group_morphs[name] = refs

    result = {}
    for name, ops in material_morphs.items():
        if any(op in ("show", "hide") for _, op in ops):
            result[name] = ops
    for name, refs in group_morphs.items():
        merged = []
        for ref_idx in refs:
            if 0 <= ref_idx < len(morph_names):
                ref_name = morph_names[ref_idx]
                if ref_name in result:
                    merged.extend(result[ref_name])
        if merged:
            result[name] = merged
    return result


def _read_morphs_from_pmx(pmx_path):
    """读 PMX 的 material morph + group morph, 返回 {name: [(mat, op), ...]}"""
    pmx = _pmx_module()
    return _extract_morphs(pmx.load(pmx_path))


def read_pmx_form_morphs(context=None):
    """从用户选中的模型反推 PMX, 读取形态 morph。打分阶段已加载的 model 直接复用。"""
    if context is None:
        return {}
    _path, model = _find_pmx_and_model(context)
    if model is None:
        return {}
    return _extract_morphs(model)


# 可被形态按钮隐藏的材质名(从 PMX morph 表动态收集, 换模型自动适配)
# 排除面部表情层(顶点与身体共享, 塌缩会扯脸; 它们走 alpha 切换)
_EXCLUDE_MORPH_KW = ("照れ", "脸红", "目影", "顏+", "顏2+", "颜+", "颜2+")


def _get_morph_material_names():
    """从 blend 文本块读取 morph 映射表, 提取所有被引用的材质名。
    排除面部表情类 morph。表不存在时退回硬编码列表(昔涟模型)。"""
    try:
        import json
        txt = bpy.data.texts.get("hsr_form_morph_map")
        if txt and txt.as_string().strip():
            mmap = json.loads(txt.as_string())
            names = set()
            for morph_name, ops in mmap.items():
                if any(kw in morph_name for kw in _EXCLUDE_MORPH_KW):
                    continue
                for kw, _op in ops:
                    names.add(kw)
            if names:
                return sorted(names)
    except Exception:
        pass
    return ["衣後紗", "衣後紗+", "衣後紗2", "衣後紗2+", "頭紗", "頭紗+", "髮飾环", "髮飾环+"]


def _hide_key_name(mat_name):
    """纱层隐藏形状键名。"""
    return "HSR_hide_" + mat_name


def _collapse_point(mesh_obj):
    """塌缩点: 骨架髋部中心(身体内部), 找不到骨架时退化为网格世界包围盒中心。"""
    try:
        arm = None
        for o in bpy.data.objects:
            if o.type == "ARMATURE":
                arm = o
                break
        if arm is not None:
            pts = []
            _root_kw = ("下半身", "腰", "骨盤", "センター", "center", "hips",
                        "pelvis", "waist", "root")
            for bone in arm.data.bones:
                bn = bone.name
                if bn in ("下半身", "腰", "センター", "center", "Center") or \
                   any(k in bn for k in _root_kw):
                    pts.append(arm.matrix_world @ bone.head_local)
            if not pts:
                # 用所有骨骼 head 的加权中心作为身体内部近似
                for bone in arm.data.bones:
                    pts.append(arm.matrix_world @ bone.head_local)
            if pts:
                import mathutils
                c = mathutils.Vector((0.0, 0.0, 0.0))
                for p in pts:
                    c += p
                return c / len(pts)
    except Exception:
        pass
    # 退化: 网格世界包围盒中心
    import mathutils
    coords = [mesh_obj.matrix_world @ v.co for v in mesh_obj.data.vertices]
    mn = mathutils.Vector((min(c.x for c in coords), min(c.y for c in coords), min(c.z for c in coords)))
    mx = mathutils.Vector((max(c.x for c in coords), max(c.y for c in coords), max(c.z for c in coords)))
    return (mn + mx) / 2


def _mat_base(name):
    """去掉 Blender 自动加的 .NNN 后缀(重名时会产生), 用于把场景材质名映射回 PMX 形态名。"""
    import re
    return re.sub(r'\.\d{3}$', '', name)


def _material_vertex_indices(mesh_obj):
    """返回 {形态材质名(kw): 该材质槽覆盖的顶点 index 集合}。
    按【去后缀名】匹配(修 🟠: 多模型时 衣後紗.001 精确匹配失败→隐藏形状键空→纱层不塌)。
    单模型(昔涟)名字本就精确, 去后缀=原名, 行为不变。"""
    me = mesh_obj.data
    targets = set(_get_morph_material_names())
    result = {kw: set() for kw in targets}
    if not targets:
        return result
    # 槽索引 → kw(去后缀匹配)
    slot_kw = {}
    for i, slot in enumerate(mesh_obj.material_slots):
        if slot.material and _mat_base(slot.material.name) in result:
            slot_kw[i] = _mat_base(slot.material.name)
    if not slot_kw:
        return {k: v for k, v in result.items() if v}
    for poly in me.polygons:
        kw = slot_kw.get(poly.material_index)
        if kw is not None:
            result[kw].update(poly.vertices)
    return {k: v for k, v in result.items() if v}


def ensure_veil_hide_shapekeys(mesh_obj):
    """为 6 个纱层各建一个隐藏形状键(幂等):
    key 的 offset = 塌缩点 - basis 坐标, 仅作用于该层顶点, 其余为 0。
    value=1 时该层顶点收拢到塌缩点(面积→0, 不可见); value=0 恢复。
    返回创建的 key 名列表。"""
    import mathutils
    me = mesh_obj.data
    if me.shape_keys is None:
        mesh_obj.shape_key_add(name="Basis", from_mix=False)
    basis = me.shape_keys.key_blocks[0]

    basis_co = [0.0] * (len(me.vertices) * 3)
    basis.data.foreach_get("co", basis_co)

    collapse = _collapse_point(mesh_obj)
    local_collapse = mesh_obj.matrix_world.inverted() @ collapse

    verts_by_mat = _material_vertex_indices(mesh_obj)

    created = []
    for mat_name in _get_morph_material_names():
        key_name = _hide_key_name(mat_name)
        kb = me.shape_keys.key_blocks.get(key_name)
        is_new = False
        if kb is None:
            kb = mesh_obj.shape_key_add(name=key_name, from_mix=False)
            kb.value = 0.0
            created.append(key_name)
            is_new = True
        verts = verts_by_mat.get(mat_name, set())
        co = list(basis_co)  # 复制 basis
        for vi in verts:
            co[vi * 3 + 0] = local_collapse.x
            co[vi * 3 + 1] = local_collapse.y
            co[vi * 3 + 2] = local_collapse.z
        kb.data.foreach_set("co", co)
        # 新建时按 PMX 默认 alpha 设初始态: 默认隐藏层(orig_alpha<0.001)→塌缩(1)
        if is_new:
            mat = bpy.data.materials.get(mat_name)
            default_alpha = float(mat.get("hsr_orig_alpha", 1.0)) if mat else 1.0
            kb.value = 1.0 if default_alpha < 0.001 else 0.0
    return created
