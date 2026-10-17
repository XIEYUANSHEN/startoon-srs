# -*- coding: utf-8 -*-
"""HSR Toon — JSON 预设保存/加载。
满足 test_plugin.py 的 API：save_preset(scene, name) 返回路径，load_preset(scene, name) 回写参数。"""
import bpy
import json
import os


# 预设存储目录：插件目录下的 presets/
def _preset_dir():
    d = os.path.join(os.path.dirname(__file__), "presets")
    os.makedirs(d, exist_ok=True)
    return d


def save_preset(scene, name):
    """将 scene.hsr_toon 所有属性序列化为 JSON，存到 presets/{name}.json，返回文件路径。"""
    props = scene.hsr_toon
    data = {}
    for key in dir(props):
        if key.startswith("_"):
            continue
        try:
            val = getattr(props, key)
        except Exception:
            continue
        # 只存可序列化的基本类型
        if isinstance(val, (int, float, bool, str)):
            data[key] = val
        elif hasattr(val, "__len__") and not isinstance(val, str):
            # FloatVectorProperty 等
            try:
                data[key] = list(val)
            except Exception:
                pass

    path = os.path.join(_preset_dir(), name + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def load_preset(scene, name):
    """从 JSON 读取并回写到 scene.hsr_toon。name 可以是文件名或完整路径。"""
    path = name
    if not os.path.isabs(path):
        path = os.path.join(_preset_dir(), name)
    if not path.lower().endswith(".json"):
        path += ".json"
    if not os.path.exists(path):
        raise FileNotFoundError("预设不存在: %s" % path)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    props = scene.hsr_toon
    for key, val in data.items():
        if not hasattr(props, key):
            continue
        try:
            setattr(props, key, val)
        except Exception:
            pass
