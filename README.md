# 钉钉大V发言聚合看板（dingall-panel）

> **给接手的人 / AI 助手：请先完整读完这份文件再动任何代码。**
> 这个项目踩过的坑大多不是"写错了"，而是"不知道上游格式长这样"。
> 跳过这份文档直接改，有 90% 概率把某些源的解析搞坏，而且坏得很难发现
> （表现为"某个大V没同步""有漏"，而不是报错）。

---

## 1. 这是什么

把 6 个财经大V 的钉钉发言流（上游只提供 HTML，无 JSON、无 CORS）聚合到一个网页看板，
**实时推送**更新，供用户盘前/盘中快速扫一遍各家观点。

- **线上地址**：https://dingall-panel.onrender.com
- **代码仓库**：https://github.com/Haven-Lai/dingall-panel（本仓库，公开）
- **托管**：Render 免费实例（美国机房）。闲时休眠，首个请求要等冷启动 30~60 秒，属正常现象。
- **上游数据源**：`http://121.41.9.82:4380/dingall?a=<URL编码的群名>&o=1&r=1`
  这是一台小服务器，**别做密集压测**，1.5 秒一轮已经很接近它的舒适上限。

> 历史遗留：`sentiment_core.py`、`/sentiment` 是另一套"市场情绪"页面，跟发言面板无关。
> `/api/health` 里 `ok:false` + `错误:"全市场行情抓取失败"` 就是它报的，**不影响发言面板**，别去追这个 bug。

---

## 2. 文件地图（只有这 3 个文件跟业务有关）

| 文件 | 职责 | 改动频率 |
|---|---|---|
| `dingall_core.py` | **全部业务逻辑**：源配置、HTML 解析、轮询、快照、SSE 增量计算 | 高 |
| `app.py` | 通用 HTTP 壳：路由、gzip、静态文件、`/api/feed`、`/api/stream` | 低 |
| `static/panel.html` | 整个前端（单文件，内联 CSS+JS，无构建步骤） | 中 |

其余：`render.yaml`（部署配置）、`requirements.txt`（零第三方依赖，只有标准库）、
`DEPLOY.md`（早期写的部署笔记，内容已被本文件覆盖，可忽略）、`data/`（运行时快照，已 gitignore）。

**前端没有构建流程**：直接改 `static/panel.html`，刷新页面即生效（线上需重新部署）。

---

## 3. 6 个来源（顺序 = 页面上的色标顺序）

`dingall_core.py` 顶部的 `SOURCES` 列表，顺序即展示顺序：

```python
灯神 → 金牌竞价 → 龙头舵主 → 极致短线2 → 极致短线 → 金甲
```

URL 编码对照表（⚠️ **龙头舵主是「舶」bó，不是「舵」duò**，拼错会返回空）：

| 名称 | 编码 |
|---|---|
| 灯神 | `%E7%81%AF%E7%A5%9E` |
| 金牌竞价 | `%E9%87%91%E7%89%8C%E7%AB%9E%E4%BB%B7` |
| 龙头舵主 | `%E9%BE%99%E5%A4%B4%E8%88%B5%E4%B8%BB` |
| 极致短线2 | `%E6%9E%81%E8%87%B4%E7%9F%AD%E7%BA%BF2` |
| 极致短线 | `%E6%9E%81%E8%87%B4%E7%9F%AD%E7%BA%BF` |
| 金甲 | `%E9%87%91%E7%94%B2` |

已移除：「十倍之路」（上游停更）、「自定义」（用户认为全是二手搬运，2026-11-05 移除）。

**加新源的方法**：往 `SOURCES` 里加一项 → 在 `static/panel.html` 的 `COLORS` 里加一个颜色
→ 先单独跑一遍 `fetch_source` 看解析结果对不对（见第 6 节），**不要看到有帖子就以为成了**，
发言人字段可能是空的。

---

## 4. 架构：为什么这样写（别退回老路）

### 抓取（2026-11-05 重构，这是核心）

```
每个源一条独立线程  →  每 1.5 秒发一次 If-None-Match 条件请求
   ├─ 304（内容没变）：0 字节，直接跳过，不解析、不重建快照
   └─ 200（有变化）：解析 → _publish() → 版本号+1 → 叫醒所有 SSE 订阅者
```

- 环境变量 `DINGALL_POLL_SEC` 可调轮询间隔（默认 1.5，1.0 未压测过，别乱降）。
- **源之间互不拖累**：哪个源先变就立刻可见，不用等最慢的那个（龙头舵主偶尔要 6 秒）。
- ⚠️ **绝对不要改回「每 N 秒把 6 个源全量重抓一遍」**。老实现每轮搬 1.2MB，
  线上 `/api/feed` 实测 8~52 秒。改回去等于把用户最在意的"慢"重新装回来。

### 推送

- `/api/stream`（SSE）推送 `{ver, total, updated, added[], full, sources[]}`，约 500~700 字节。
- `_publish()` 顺手算出"相对上一版新增了哪些帖子"塞进 `added`，前端 `applyDelta()`
  就地插入 + 重排 + 高亮，**零往返**，不用再跨境拉 19KB 全量。
- 新增 > 12 条、或前端没有可信基线时推 `full:true`，前端老实拉全量。
- 前端连上 SSE 就停掉 5 秒轮询；断线自动退回轮询兜底。20 秒心跳，10 分钟主动断开重连。

### 快照与冷启动

- 内存快照 + 落盘 `data/feed_snapshot.json.gz`（**脏标记 + 独立线程每 20 秒写**）。
- ⚠️ 不要在 `_publish()` 里按时间节流直接写盘：会正好写在"只有第一个源抓完"的时刻，
  落盘结果是残缺的（实测只存下 309 条，实际 465 条）。
- 冷启动：有磁盘快照 → 首屏 0ms 顶旧数据；没有 → `build_feed()` **只等不抢**，
  等各源线程首次抓完（约 1.5 秒）。⚠️ 别再让它自己再并发抓一轮，12 个请求会压垮上游，首屏反而变 10 秒。

### 每日定时源

`DAILY_SOURCES = {"金甲": {"window": ("09:00", "09:40")}}` —— 金甲每天 09:27 左右只发一张图：

- 窗口内：1.5 秒高频轮询，确保当天那张图刚发出就抓到
- 窗口外且**当天已成功拉到过**：降频到 30 秒，少打扰上游
- 窗口外但当天还没拉到（服务刚重启）：仍会拉一次，否则整天看不到图
- 灯神 / 龙头舵主 / 金牌竞价这类白天持续发言的源**不要**加进来

### 发言人白名单（屏蔽群里的其他人）

`SPEAKER_WHITELIST = {"金牌竞价": {"嘉兴可爱范"}}` —— 「金牌竞价」实质是个群，几十人聊天，
群里只有群主有价值。白名单里的源**只保留指定发言人**，其余帖子在 `parse_posts()` 之后立刻丢弃：
不占快照、不进 SSE 增量、来源计数和前端 chip 一并变干净。网页和小程序同时生效
（两边读同一份 `/api/feed`，所以不需要改 `dingall-mini`）。

**为什么不用用户 ID 锚定**：上游 dingall 接口只吐纯文本昵称，整个响应里没有
`userId` / `staffId` / 头像 / 用户主页链接（602 个 `<a>` 全是同一个「只看Ta」，
指向的是整个来源而非某个人）。**这个接口上不存在可锚定的 ID**，所以只能靠结构信号锚定。

**改名自动锚定**（`_wl_learn`，别再手改名字）：

- 上游一次吐 **42 小时左右**的发言，所以「旧昵称彻底消失」要等一天多——不能拿它当判据
- 真正的判据是：**白名单里的人沉默超过 30 分钟（群里还在聊）+ 冒出一个从没见过的昵称 +
  它第一次开口正好在他销声之后**。改名在时间轴上就是这个形状
- 命中后先挂「待确认」90 秒（`WL_CONFIRM_SEC`），仍成立才写入别名表
  `data/speaker_whitelist.json`，新旧昵称**都保留**（改回来也认，历史发言不断档）
- 有人正常入群时，群主照样在说话，走不到这段分支，不会误伤
- 候选必须占近期发言 ≥25%（`WL_MIN_SHARE`），否则新群友刷几句就会把群主顶掉
- 实在认不出来（他真的整天没说话）：**临时显示全部并标 `unresolved`**，
  网页状态条会提示「昵称待识别」，绝不把该源清空成 0 条
- 排查入口：`/api/health` → `feed.whitelist`，能看到 `configured / aliases / active / mode / pending`
- 回归测试：`python tests/test_whitelist_anchor.py`（合成数据、不联网，验改名/入群/沉默/重启四种情形）

- 想恢复看全部：把这个源从字典里删掉即可
- 磁盘快照读取时也会再筛一遍（`_load_snapshot_from_disk`），
  否则旧快照会让别人发的垃圾先冒出来几秒

---

## 5. 解析：5 种上游格式与它们各自的陷阱

上游同一个接口有至少 5 种排版，全靠 `parse_block()` 兼容。改解析前必须知道这些：

1. **时间戳双份，且内层是 12 小时制**（最坑）
   每块有两个时间：外层块头 `YYYY-MM-DD HH:MM:SS`（24 小时制，**正确**），
   内层行 `2026-9-23 02:31:11`（同一时刻的 **12 小时制渲染**，14:30 → 02:31）。
   **一律以外层为准**，已加通用兜底：内外层偏差 > 6 小时就回退外层。
   用错内层的后果：发言在时间线里沉 12 小时，用户会报"某某没同步/有漏"。

2. **龙头舵主**：发言人固定是「老师」，正文开头的 `老师:` / `老师 ` 要剥掉。

3. **灯神（群标签格式）**：内联标签 `【计划群 · 10-21 09:58 · 灯湖】`
   含 `·` 时**只取最后一段**作为发言人。不是人名（`∴`、`10-21 09:58`）就留空，
   **绝不回退去取前面的群名**——否则「闲聊群」「系统复盘」会变成"发言人"，
   用户点它只会捞出一堆无关发言。另有 `NON_PERSON_RE` 排除"群/计划/复盘/机器人/公告/通知/助手"结尾的词。

4. **金甲（纯图片源）**：每天 09:27 发一张图，602 条几乎全是图，无正文、无发言人、无内层时间戳，
   24 小时窗口里通常只有 1~2 条。原文形如 `![]( \n\n ]( <a href="...media/xxx.png?bizType=im">详情</a>`，
   剥完会在正文残留一截 `](` → `clean_text()` 需要额外 `re.sub(r"\]\(\s*", "", s)`。

5. **金牌竞价**：块内**首行是内层时间戳**，真正的 `发言人 YYYY-MM-DD HH:MM:SS` 在**下一行**。
   `HEADER_RE` 必须能吃 4 位年份的日期，且首行是时间戳时把表头行**下移一格**再匹配。
   （2026-11-05 修过一次：修之前 602 条发言人全空，前端连名字都不渲染，用户以为"点人名没用"。）

其他通用陷阱：
- 页面把内容**渲染两遍**（块数 ×2），靠 `(source, speaker, time, text)` 去重，不是 bug。
- 钉钉媒体链可能没有后缀（`static.dingtalk.com/media/...`），`IMG_RE` 要单独分支，否则裸链泄进正文。
- 第一个非空行不一定是 `lines[0]`，别硬编码 `lines[1:]`。

---

## 6. 改完怎么验证（**别跳过这步**）

```bash
# 1) 单独看某个源的解析结果，重点看 speaker 是不是空的
python -c "
import collections, dingall_core as d
src = [s for s in d.SOURCES if s['name']=='金牌竞价'][0]
r = d.fetch_source(src)
c = collections.Counter(p['speaker'] for p in r['posts'])
print(r['count'], c.most_common(8))
"
```

健康标准：**每个源都有若干非空发言人**。某个源全是空字符串 = 解析坏了。

```bash
# 2) 本地起服务，看全链路
PORT=18899 python app.py
curl -s --noproxy '*' "http://127.0.0.1:18899/api/feed?hours=24" | python -m json.tool | head -40
curl -sN --noproxy '*' -m 8 "http://127.0.0.1:18899/api/stream"   # 看 SSE 首帧
```

- `/api/feed` 应 3~4ms 返回（不是几秒）。如果是秒级，说明有人把快照机制改坏了。
- `/api/health` 的 `feed.sources` 里，每个源的 `fetched` 应每 1.5 秒跳一次，
  `etag` 为 `true` 表示条件请求生效。**排查"为什么某源不更新"先看这里。**

---

## 7. 部署（唯一需要注意权限的地方）

### 正常流程

```bash
git add -A && git commit -m "..." && git push
python deploy_render.py        # 或双击 deploy_render.bat
```

`deploy_render.py` 会读取 **Deploy Hook URL** 并 POST 给 Render 触发构建。

> ⚠️ **Deploy Hook URL 不在本仓库里，也不应该放进来。**
> 本仓库是**公开**的，Hook 相当于部署密码，泄露别人就能随意触发构建、刷爆免费额度。
> 原位置：用户本机 `.workbuddy/render_deploy_hook.txt`（工作区根目录下，仓库之外）。
>
> **接手时如果找不到这个文件**：去 Render Dashboard → 选服务 `dingall-panel`
> → Settings → Deploy Hook → 复制 URL，存到仓库外的一个文件里，**不要提交**。
> 或者干脆跳过脚本，在 Dashboard 点 "Manual Deploy → Deploy latest commit"。

### ⚠️ push 完 ≠ 已上线

Render 的 GitHub webhook 会**静默失败**（不报错、不通知）。实测推了 3 个 commit、40 分钟毫无动静，
而 Auto-Deploy 明明设的是 `On Commit`。所以推送后必须验证：

1. 等 2~3 分钟
2. 抓线上首页 `curl https://dingall-panel.onrender.com/ | grep <你刚加的关键字>`
3. 看 `/api/health` 的 `feed.version` 是否归零重算（说明是新进程）

对不上就手动跑一次 `python deploy_render.py`，仍不动就去 Dashboard 点 Manual Deploy。

### Auto-Deploy 三档（Dashboard → 服务 → Settings → Auto-Deploy）

- `On Commit`（推荐）：推送即构建
- `After CI Checks Pass`：**本仓库没有 CI 检查**，Render 视为 0 个检查 → **永不部署且不报错**，别选
- `Off`：只能手动或 Hook

另注：做 Rollback、或选 "Deploy a specific commit"，会**静默把 Auto-Deploy 关掉**，之后要手动重开。
`render.yaml` 里的字段已改名为 `autoDeployTrigger: commit|checksPass|off`（旧 `autoDeploy` 已废弃）。

---

## 8. 用户在意什么（做技术取舍时按这个来）

1. **快**。他对"比原网页慢"极度敏感，这是他推动整个重构的原因。
   任何让端到端延迟变大的改动，都要先说服他。当前上限：发帖 → 页面可见 ≤ 2 秒。
2. **不要本地服务**。以前跑过 `localhost:8899` 的本地版，2026-11-05 按他要求**全部删掉**了
   （本地服务、开机自启、桌面快捷方式）。**不要再提议"跑本地版"**，他只要线上。
3. **点人名只看这个人**。这是他最常用的功能，全靠 `speaker` 字段解析正确。
   解析出空 speaker 或把群名当人名，等于把这个功能废掉。
4. 超短线交易者，盘前盘中看，**宁可少一个功能，不要慢一秒**。

---

## 9. 环境注意事项

- **零第三方依赖**，纯 Python 标准库。`requirements.txt` 是空的占位，别往里加东西。
- 本机测试必须 `curl --noproxy '*'`：系统代理（127.0.0.1:7897）会拦截 localhost。
- 推送若报 502，是环境代理的问题，绕过它：
  ```bash
  env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
      git -c http.proxy=http://127.0.0.1:7897 push
  ```
- 云容器默认 UTC，`app.py` 里已按需设时区；Windows 本地跑时 `TZ=Asia/Shanghai` 会被 CRT 解析成 UTC
  （差 8 小时），所以只在非 Windows 设置。

---

## 10. 快速自检清单（接手后 5 分钟确认项目还健康）

```bash
curl -s --noproxy '*' "https://dingall-panel.onrender.com/api/feed?hours=24" | python -c "
import sys, json, collections
d = json.load(sys.stdin)
print('总数', d['total'], '版本', d.get('version'))
for s in d['sources']:
    print(' ', s['name'], 'ok=', s['ok'], '条数=', s['count'])
for name in ('灯神', '金牌竞价'):
    ps = [p for p in d['posts'] if p['source'] == name]
    c = collections.Counter(p['speaker'] for p in ps)
    print(name, len(ps), '条 /', len(c), '个发言人 →', c.most_common(3))
"
```

期望：6 个源全 `ok=True`，灯神和金牌竞价都有多个非空发言人。
若某源 `ok=False`，多半是上游 `121.41.9.82:4380` 临时抽风，等几分钟重试，别急着改代码。
