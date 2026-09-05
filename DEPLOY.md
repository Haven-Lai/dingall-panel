# 云端部署指南（完整复刻 http://localhost:8899 统一面板）

本目录 `cloud_panel` 是 **http://localhost:8899 的 1:1 云端复刻版**，单进程合并了两套能力：

| 路由 | 功能 | 对应 8899 |
|------|------|-----------|
| `/` | 完整面板（大V发言 + 情绪看板 双 Tab） | 8899 首页 |
| `/api/feed` | 大V发言 JSON（服务端代理 `121.41.9.82:4380`） | 8899 的 `/api/feed` |
| `/api/data` | 东方财富情绪量化数据 | 8899 情绪看板数据源 |
| `/sentiment` | 情绪看板前端（面板 iframe 同源嵌入） | 8899 的 `127.0.0.1:8901` |

纯 Python 标准库，无任何第三方依赖，`render.yaml` 已内置全部部署参数。

---

## 为什么用 Render（而不是 Vercel / Netlify）

- **Vercel / Netlify 出网仅允许 80/443 端口**，无法代理大V发言上游 `121.41.9.82:4380`（非标端口）→ 面板会缺「大V发言」Tab。
- **Render 的 Web Service 出网无端口限制**，能正常代理 4380，因此是唯一能完整复刻 8899 的免费云平台。
- Render 免费档（plan: free）足够个人盘中使用，region 选 `singapore`（离数据源最近，延迟最低）。

---

## 部署步骤（约 3 分钟，一次性）

### 第 1 步：把本仓库推到 GitHub

```bash
# 在 cloud_panel 目录内，把下面 URL 换成你自己的空仓库
git remote add origin https://github.com/<你的用户名>/dingall-panel.git
git branch -M main
git push -u origin main
```

> 没有 GitHub 仓库？去 https://github.com/new 建一个 **空的** 仓库（不要勾 README），拿到地址填上面即可。

### 第 2 步：Render 一键导入

1. 打开 https://dashboard.render.com/  → 用 GitHub 登录（免费）。
2. 右上角 **New** → **Blueprint**。
3. 连接你的 GitHub，选中刚推上去的 `dingall-panel` 仓库。
4. Render 会自动读取 `render.yaml`，无需手动填命令。
5. 点 **Apply** / **Create**。

### 第 3 步：拿到公网地址

创建完成后，Render 会分配一个形如 `https://dingall-panel-xxxx.onrender.com` 的地址。
手机浏览器直接打开它，就是和 8899 一模一样的双 Tab 面板。

---

## 注意事项

- **免费档冷启动**：服务 15 分钟无访问会自动休眠，下次打开需 30~60 秒唤醒并重新拉取数据。交易时段内持续访问则一直在线。
- **数据时效**：`/api/health` 返回 `ok:true` 即数据正常；面板右上角有「数据更新时间」徽章。
- **大V发言源**：`121.41.9.82:4380` 需从云端可达；singapore 节点通常可达，若某源 `ok:false` 面板会显示该源错误但不影响其他 Tab。
- **本地预览**：`python app.py` 后访问 `http://127.0.0.1:10000/`（端口可用 `PORT` 环境变量覆盖）。

---

## 想换区域 / 升级

- `render.yaml` 里把 `region: singapore` 改成 `oregon`（新加坡名额满时）。
- 免费档升级付费档：在 Render 服务页 **Settings → Plan** 切换，命令无需改。
