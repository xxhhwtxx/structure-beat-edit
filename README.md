# 结构卡点剪辑 · Structure Beat Edit

一个用于 Codex 的参考驱动视频剪辑 skill。适合校园、建筑、风景以及具有重复结构的实拍素材。

从参考片中分析局部显色、黑底抽离、窗格阵列、台阶或楼体拼合、反相闪切和光带扫描，再根据新素材编排效果、修整遮罩并逐帧复核。

## 包含内容

- `SKILL.md`：完整工作流与使用边界。
- `references/`：参考拆解、音乐节奏、结构效果、遮罩边界、逐帧检查、项目约定和验证记录。
- `scripts/media_audit.py`：视频参数、真实 PTS、逐帧统计、接触表、原尺寸帧、音频瞬态候选与计划检查。
- `scripts/mask_tools.py`：静帧多边形、孔洞、颜色约束、保护层、累计/替换上色预览。
- `assets/`：编辑计划和遮罩规格示例。
- `agents/openai.yaml`：Codex 界面元数据。

## 安装到 Codex

下载仓库，将包含 `SKILL.md` 的整个目录放到 Codex 的技能目录，文件夹名使用 `structure-beat-edit`：

```text
$CODEX_HOME/skills/structure-beat-edit/
```

未设置 `CODEX_HOME` 时使用 `~/.codex/skills/structure-beat-edit/`。

调用示例：

```text
使用 $structure-beat-edit 分析这个参考，并根据我的素材完成结构卡点剪辑和逐帧检查。
```

## 辅助脚本

需要 Python 3.10+、NumPy、OpenCV。媒体分析另需 FFmpeg 和 FFprobe 在 PATH。

```bash
python -m pip install numpy opencv-python
python scripts/media_audit.py reference.mp4 --out work/reference-audit --audio-onsets
python scripts/media_audit.py final.mp4 --out work/final-audit --all-frames --plan edit_plan.json --save-events
python scripts/mask_tools.py working-image.png --spec mask-spec.json --out work/mask-review
```

示例规格需要针对实际图像重新绘制。`mask_tools.py` 不识别语义、不跟踪移动画面；`media_audit.py` 生成证据，不代替看图，也不把差异峰自动当成切镜或将音频瞬态直接当作最终节拍。

## 验证范围

已在 421 帧参考片、386 帧校园成片、合成门体遮罩、CFR/VFR 与旋转元数据样例上验证相关辅助行为。详见 [验证记录](references/validation.md)。

本仓库不包含原用户视频、照片、音频或私人绝对路径。实际时间线由当前项目的编辑器或渲染工具完成；此 skill 不伪造剪映等软件的原生工程。
