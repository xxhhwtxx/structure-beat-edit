# 计划与交付约定

## 最小项目记录

按项目复杂度选用这些文件，不强制创建空文档：

- `BRIEF.md`：输入、输出、保留事项与必要假设。
- `reference-analysis.md`：观察到的参考语法，区分推测。
- `sources.json`：原生尺寸、裁切、来源及工作图路径。
- `edit_plan.json`：输出时间线，以下脚本可读取其中的帧数/事件/保持段。
- `masks/`：独立 alpha 与必要的保护层；动画还需跟踪/关键帧数据。
- `review/`：从最终成片提取的接触表、原尺寸帧、测量与复核记录。

媒体与工程内部尽量使用相对路径。授权处理一个项目不等于可以公开发布，也不等于可以修改其他项目配置。

## 计划示例

`assets/edit-plan.example.json` 是 120 帧、两个静态镜头的完整格式示例，用于说明，不可原样套新音乐。其事件包括状态起点和切镜；时间区间全部为半开区间。

```json
{
  "fps": "30/1",
  "frame_count": 120,
  "shots": [{"id":"first","start":0,"end":60},{"id":"second","start":60,"end":120}],
  "events": [{"frame":0,"kind":"shot_start"},{"frame":10,"kind":"reveal","mode":"accumulate"}],
  "holds": [[0,10],[10,20]]
}
```

`holds` 只包含真实静止段，允许为空；整个区间的第一帧允许进入新状态。镜头连续动画期间不要声明静止。`events` 可省略无关动作，不必每个画面变化都写成源镜头切换。

## 静帧遮罩规格

见 `assets/mask-spec.example.json`。

- `polygon`、`holes` 均使用 0–1 归一化坐标，基于已经裁切完成的工作图。
- `hsv.hue_bands` 使用 OpenCV 的 0–179 色相；红色跨零点时给两个区间。`saturation`、`value` 使用 0–255 闭区间。
- 基础几何和 HSV 相交；`union` 加入前面已定义层，`intersect` 和 `subtract` 最后依次约束。仅允许引用前面层，避免隐藏循环。
- `cleanup` 可选闭运算、移除小连通域、填小孔、收边和羽化。面积阈值按全图面积比例，半径以当前工作图像素计，不默认进行破坏细节的清理。
- `states` 使用 `replace` 或 `accumulate`，从所列层生成状态；默认不会自动把每个状态渲染成视频。

## 脚本依赖与命令

Python 3.10+、NumPy、OpenCV；媒体分析另需 FFmpeg/FFprobe 在 PATH。脚本不自动联网安装依赖。

```powershell
python -m pip install numpy opencv-python
python "<skill-dir>/scripts/media_audit.py" "<video.mp4>" --out "work/audit" --audio-onsets
python "<skill-dir>/scripts/media_audit.py" "<final.mp4>" --out "work/review" --all-frames --plan "edit_plan.json" --native-frames "10,19,20,59,60"
python "<skill-dir>/scripts/mask_tools.py" "<image.png>" --spec "mask-spec.json" --out "work/masks"
```

审看原尺寸事件帧可用 `--save-events`；`--native-frames` 是导出视频中的 0 起算帧。`--all-frames` 将全部帧放入接触表，不把每帧都保存为巨大的原尺寸文件。音频默认分析第一条轨，多轨音乐用 `--audio-stream 1` 等选择音轨内的 0 起算索引。

## 交付

通常提供最新 MP4、可继续编辑的工程或渲染入口、相对路径素材、简短复核结果。是否打包所有截图按需要决定，避免无意义的大包。只链接实际存在、已核对的最新文件。无法生成原生工程时交付真实可用的替代并明确格式，不能给脚本改后缀假装剪映工程。
