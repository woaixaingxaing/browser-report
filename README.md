# 浏览报告（Chrome）

把 Chrome 的历史记录和书签，生成一份离线单文件 HTML 报告。历史浏览仿 Better History：
左边月历、右边时间线，按天翻、按关键词搜、按分类和站点筛；另外提供日报、周报、月报和年报。

代码公开，数据不出本机：脚本只读取当前电脑上的 Chrome 数据，生成过程不联网，也不会上传任何浏览记录。

## 快速开始

需要 Python 3.10+，只用标准库，不用装依赖。

```bash
git clone https://github.com/woaixaingxaing/browser-report.git
cd browser-report
python gen_report.py --open
```

生成的报告在 `dist/browser-report.html`，双击就能离线看。也可以直接打开：

```bash
python gen_report.py                 # 日报 + 本周 + 本月 + 今年
python gen_report.py --open          # 生成后自动打开
python gen_report.py --year 2026     # 指定年度
python gen_report.py --anchor 2026-10-24   # 以某天为基准
```

Windows 上如果 `python` 不在 PATH，可以试 `py -3 gen_report.py`。

## 能做什么

**历史（默认页）**

- 左侧月历按访问量分四档着色，0 次留白，点任意日期跳到当天；支持「上一天 / 下一天 / 今天 / 回到最近一天」。
- 月历用固定访问量分档：**1–49 / 50–149 / 150–399 / 400+ 次/天**，图例直接标出界限，换月份颜色含义不变。
- 右侧按天分组的时间线：时间、停留时长、标题、站点、分类、完整 URL，点标题或箭头直接打开原网页。
- 搜索标题、URL、站点，命中处高亮；结果按天分组，最多展示 600 条。
- 分类和站点两个筛选器，可与搜索叠加。
- 显示设置：主题（跟随系统 / 浅色 / 深色）、24/12 小时制、密度（宽松 / 紧凑）、是否显示 URL、是否显示停留时长。设置存在浏览器本机，只影响这份报告。

**日报**

- 当天访问量、有效阅读、站点数、连续浏览次数，附前一日变化。
- 第一次 / 最后一次访问时间、最活跃时段、内容类型、最长会话、单页停留最久。
- 全天时间轴、24 小时分布、站点 Top 15、内容类型、读得最久的页面。
- 逐次明细按小时折叠，点开就能看到该小时的全部访问和可点击标题。

**周报 / 月报 / 年报**

- 总量、有效阅读时长、会话数、日均、环比等指标卡。
- 每天一根的访问柱、分类占比、站点排行、时段分布、会话明细。
- 所有页面标题和排行条目都可点击，直达原网页。
- 年度活跃日历是 GitHub 贡献图样式，悬停看当天明细；图例同样使用固定的 1–49 / 50–149 / 150–399 / 400+ 次/天。

## 数据从哪来

| 数据 | 来源 | 精度 |
|---|---|---|
| 访问记录 | Chrome `User Data/<Profile>/History`（SQLite） | 微秒，展示到秒 |
| 停留时长 | `visits.visit_duration` | 微秒 |
| 书签 | `Bookmarks`（JSON，`date_added`） | 微秒 |

默认数据目录：

| 系统 | 路径 |
|---|---|
| Windows | `%LOCALAPPDATA%\Google\Chrome\User Data` |
| macOS | `~/Library/Application Support/Google/Chrome` |
| Linux | `~/.config/google-chrome`（找不到时退回 `~/.config/chromium`） |

默认读 `Default` 配置；多用户或工作资料可以指定，例如：

```bash
python gen_report.py --profile "Profile 1"
python gen_report.py --user-data "D:/ChromeUserData"
```

脚本先把数据库**快照**到临时目录再读，不碰原库、不需要关 Chrome。

## 两个口径（重要）

1. **有效阅读** = min(记录时长, 到下一次访问的间隔, 15 分钟)。
   Chrome 的 `visit_duration` 会把「标签页挂着不管」的时间也算进去，
   不设上限的话一天能算出 27 小时。取到下一次访问的间隔，能自动压掉挂机和自动刷新。
2. **会话**：两次访问间隔超过 20 分钟，算新的一次会话。

## 一个绕不开的限制

Chrome 默认只保留约 **90 天**历史，所以「年度」视图实际只覆盖 Chrome 留存的这段时间。
报告顶部的「历史覆盖」和年度视图里的灰色格子都在说明这件事。
想要完整的年度数据，得靠定期归档（比如每周跑一次，把 HTML 或导出的 JSON 存下来）。

## 自动刷新（可选）

`auto_report.py` 是给定时或事件触发用的包装器：

```bash
python auto_report.py --trigger event --quiet
```

它运行 `gen_report.py`，把每次结果追加到 `logs/auto-refresh.log`，
记录时间、触发来源、成功/失败、耗时、文件大小和本期汇总。
日志超过 1 MB 自动轮转，保留 3 份备份；`logs/` 已在 `.gitignore` 里，不会提交。

配合 Quicker 的「设备闲置超过 300 秒」事件触发，就能在电脑空下来时自动记一次：

- 新建一个 C# 脚本动作，调用 `python -X utf8 auto_report.py --trigger event --quiet`；
- 动作需要能写入项目目录，超时上限建议不低于 150 秒；
- 事件参数：空闲时长 `300` 秒，重复间隔 `0`，最大重复次数 `1`；
- 事件触发时不要打开报告、不要弹通知，结果只进本地日志。

手动运行时去掉 `--quiet`，就能看到生成器的汇总输出。

## 文件

```
gen_report.py            生成器（Python 3.10+，只用标准库）
auto_report.py           自动刷新包装器 + 本地日志
report.css               报告样式（生成时内联进 HTML）
dist/browser-report.html 生成结果（单文件、离线可看，不入库）
logs/                    自动刷新日志（本地，不入库）
```

样式改动只需改 `report.css`，重新跑一次生成器即可。

## 隐私

报告包含完整的个人浏览记录，仓库里**只放生成器代码**，不包含 `dist/` 下的成品、
`logs/` 日志、Chrome 原始 `History` 数据库或 `Bookmarks`。
每个人运行后读到的都是自己电脑上的数据；想分享报告前，请先确认内容可以公开。

## 版本

- 当前 `v1.2`，变更见 [CHANGELOG.md](CHANGELOG.md)。
- 版本用 Git tag 与 GitHub Release 标记，升级时递增次版本号。
