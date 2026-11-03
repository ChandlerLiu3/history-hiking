# 04 交付规范

## 目录

```
routes/
  <l0-slug>/                  # 主线，小写 ASCII，如 changzheng
    <l1-slug>/                # 支线，如 loushanguan
      <NN>-<from>-<to>/       # 日段，如 03-loushanguan-tongzi
        segment.yaml
        card.md
        sources.md
        track.gpx             # 可选：无现成轨迹或需分发时
        track.kml             # 可选：由 gpx 生成
```

`slug` 用 ASCII（避免跨工具路径问题），中文名写在 `name` 字段。

## 编号

`id = <l0>-<l1>-<NN>`，如 `cz-01-03`。编号沿历史行进方向递增。

## segment.yaml 字段

```yaml
id: cz-01-03              # 必填
tier: L1                  # L0 | L1 | L2
name: 娄山关—桐梓
era: "1935-02"            # 历史时段
history:
  event: 二渡赤水后攻娄山关
  sources: ["《红军长征过贵州》p.123"]
  confidence: B           # A|B|C|D
geometry:
  start: {name: 娄山关, lat: 27.9, lon: 106.8}
  end:   {name: 桐梓,   lat: 28.1, lon: 106.8}
  distance_km: 22.4
  ascent_m: 900
route:
  difficulty: 3           # 1–5，见下
  season: "3–5, 9–11"
  permission: 无           # 封山/防火/边检要求
tracks:                   # 核心：成熟轨迹引用
  - platform: 2bulu
    id: "1234567"
    url: https://www.2bulu.com/track/t-xxxx.htm
    author: 张三
    date: 2024-03
    distance_km: 21.8
    ascent_m: 870
    maturity: M2          # M1|M2|M3
    note: 走东沟上山，与史料一致
  - platform: wikiloc
    id: "1672838"
    maturity: M1
    note: 走向不同，绕顶，作对照
safety:
  retreat: [{at_km: 6, to: "G210 公路", distance_m: 300}]
  water: ["K4 山泉（旱季断流）"]
  signal_dead_zone: "K7–K11，约 2 km"
  exit: "桐梓县城，班车至遵义"
uncertainty:
  - "关城位置有 A/B 两说，取 A（《遵义府志》）"
  - "K9–K11 古道被公路覆盖，按公路走"
```

## 难度分级（difficulty 1–5）

| 级 | 判据 |
|---|---|
| 1 | 景区步道，标识齐全，<8 km，<300 m 爬升 |
| 2 | 成熟土路，<12 km，<600 m，有撤退 |
| 3 | 野路/古道，需导航，10–18 km，600–1000 m |
| 4 | 无标识，需找路，>18 km 或 >1000 m |
| 5 | 攀爬、绳索、涉水、探路 |

## 交付物清单

| 文件 | 必需 | 内容 |
|---|---|---|
| `segment.yaml` | 是 | 上述字段，机器可读 |
| `card.md` | 是 | 一页人读卡：怎么去、怎么走、哪里撤、看什么 |
| `sources.md` | 是 | 史料出处 + 轨迹来源 + 检索关键词与日期 |
| `track.gpx` | 否 | 仅当无现成轨迹、或需离线分发时自建，标注 `[自绘]` |
| `track.kml` | 否 | 由 GPX 转换；无导航需求可不生成 |

## GPX / KML 规范（生成时）

- 坐标系 **WGS84**；国内地图底图（高德/腾讯）为 GCJ-02，导入需转换并注明。
- 一个日段一个 `<trk>`，`trkpt` 含 `ele`；抽稀到 ≤1 点/10 m。
- `ele` 为 DEM 高程，误差 ±10 m，标注「非实测」。
- KML 线色按置信度：A 绿 `ff00ff00`｜B 黄 `ff00ffff`｜C 橙 `ff0080ff`｜D 灰 `ff808080`。
- 自绘轨迹必须在 `name` 与 `desc` 里写 `[自绘]` 与依据。

## 命名约定

- 平台 ID 一律加引号存字符串（防前导零丢失）。
- 日期 `YYYY-MM` 或 `YYYY-MM-DD`。
- 撤退点写「到最近公路/村的距离」，不写「附近有路」。

## 文档结构：正文在前，过程在后

读者打开文件是要走路的，不是来看我们怎么整理的。所以：

- **首屏只放正文**：标题 →（可选一句定位）→ 第一节。开头不放修订记录、转换说明、版本表、来源清单、检查结论。
- **过程信息统一进文末附录**，顺序按「先读者后过程」：作废稿 / 原始样貌 → 未处理项 → 来源 → 修订记录。
- **正文不写校订标记**：`〔校订 N〕`、行内注释、「本文件按…重排」这类元信息语句，一律不进正文。《修订记录》用「位置」列定位到章节 / 小节 / 表行，例如「② 青石嘴→白杨城 备注」。
- **不写 front matter**：文档第一行就是 `# 标题`。来源文件名、页数、SHA-256、转换工具、版本号，写进附录《加工经过》或同目录 `sources.md`。
- 长文档（>3 节或 >1000 字）一律按此结构；单页 `card.md` 不强制。
- 例外：确实需要读者先看免责/许可的，把那段放正文最前，但只限一段。
