# 六只脚导出（收藏 + 自有行程）

把六只脚（foooooot.com）账号里的**收藏**和**自己记录的行程**批量导出成 GPX，落到 `data/liuzhijiao/`。

设计前提：**登录这一步交给你**。脚本只负责开一个真浏览器、等你自己登进去，然后接管翻页和下载。账号密码不经过脚本，也不写盘。

## 站点路径（2026-11 实测）

| 用途 | 地址 |
|---|---|
| 我的行程 | `/home/trip/my_trip/` |
| 我的收藏 | `/home/trip/fav_trip/` |
| 两者也挂在 | `/home/trip/` 的 tab 上 `#my_trip-1` / `#fav_trip-1` |
| 轨迹下载页 | `/trip/<id>/downloads/` |
| **GPX 文件** | `/trip/<id>/gps/file/?format=gpx` |
| KML / 原始航迹 | `/trip/<id>/download/?fileformat=kml` · `?fileformat=orig` |

老的个人主页 `/userindex/<uid>/trip/` 上是空的，别用。

## 装依赖

```powershell
pip install playwright
playwright install chromium
```

## 跑

```powershell
python tools/liuzhijiao_export.py
```

1. 弹出 Chromium 窗口，停在登录页
2. 你自己输账号密码（验证码也是你自己点）
3. 登录成功后不用管，脚本自动翻列表、逐条下载
4. 登录态存在 `.cache/liuzhijiao-profile/`，下次不用重登

先探 5 条看格式对不对：`python tools/liuzhijiao_export.py --limit 5`

## 输出

```
logs/
├── run-20261101-141530.log    每次运行一份，含崩溃 traceback
├── diagnose.json              诊断结论（--diagnose）
└── 03-my.png / .html          列表页实际长什么样

data/liuzhijiao/
├── my/          自己记录的行程  <id>_<标题>.gpx
├── fav/         收藏的行程      <id>_<标题>.gpx
├── raw/         trackjson 原始数据（走了兜底路径的才留）
└── manifest.csv id,标题,来源,文件,点数,状态
```

`status`：`ok`（官方 GPX）、`ok(trackjson)`（自己拼的）、`skip(exists)`、`fail`。

## 退出码

| 码 | 含义 | 怎么办 |
|---|---|---|
| 0 | 正常结束 | — |
| 2 | 缺 playwright | `pip install playwright && playwright install chromium` |
| 4 | 一条行程都没收集到 | 页面已存到 `logs/99-empty.*`，看那张截图 |

## 常用参数

| 参数 | 用途 |
|---|---|
| `--diagnose` | 只诊断不下载，存截图和 HTML |
| `--only my` / `--only fav` | 只导一边 |
| `--limit 5` | 先下 5 条试水 |
| `--my-url` / `--fav-url` | 手动指定列表页地址 |
| `--force` | 覆盖已存在的文件 |

## 已知的坑

- **收藏里可能混进自己的行程**：两个 tab 的卡片有时同时在 DOM 里。脚本会按行程编号去重，自己记录的优先归到 `my/`。跑完翻一下 `manifest.csv` 的 `source` 列就能确认。
- **翻页可能不全**：先试「下一页」链接，再试 tab 上的 `-2`/`-3` 序号锚点。如果 `manifest.csv` 条数明显少于 App 里看到的，把 `logs/run-*.log` 发我。
- **某条 fail**：多半是私有行程或已删除。清单里标了，不影响其它条。

## 边界

- 只读：不点赞、不评论、不修改任何东西。
- 每条之间默认隔 2 秒，别调太小。
- 导出的轨迹是实测记录，不是史料。按 `preset/02-sources.md` 的规矩，引用要写平台、行程 ID、作者、日期；它证明「这条路今天能走」，不替代史料定位。
