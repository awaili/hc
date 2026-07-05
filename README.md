# 本草链 HerbChain · 中药材大货贸易协作系统

面向中药材大货贸易的产-加-销协作系统。当前仓库包含**需求采集站点**（品牌官网 + 固定问卷 + LLM 自适应访谈 + 结果看板）的源码与部署配置。ERP 业务功能将在此基础上迭代。

线上地址：<https://hc.quickpapa.com>

## 仓库结构

```
backend/      Flask 后端(app.py) + 访谈系统提示词 + systemd unit + 凭据示例
  app.py                问卷 + LLM访谈 + 摘要生成 + 会话管理( CouchDB 存储)
  system_prompt.md      访谈AI人设/规则(含财税合规与合规节税红线)
  hc-survey.service     systemd 单元(监听 127.0.0.1:5006)
  llm.env.example       LLM 网关凭据模板
  couchdb.env.example   CouchDB 凭据模板
frontend/     静态前端(由 nginx 直接serve)
  index.html            品牌官网
  questions.html        固定6题问卷
  chat.html             LLM 自适应访谈(口令门 + 路线图引导)
  results.html          结果看板(问卷统计 + 访谈记录 + 摘要渲染)
  marked.min.js         本地markdown渲染库(离线)
nginx/        hc.quickpapa.com.conf(80→443 + /api 反代)
systemd/      hc-survey.service
docs/         tax-checklist.md 税务合规清单 / decisions.md 关键决策日志
```

## 凭据(不入库,在部署机本地填写)

部署到 `/opt/hc-survey/`：
- `llm.env` — 由 `llm.env.example` 复制并填写（LLM 网关地址/token/模型）
- `couchdb.env` — 由 `couchdb.env.example` 复制并填写（CouchDB URL/账号/密码）
- `view_token` — 查看结果用的令牌（自拟随机串）
- `chat_access` — 发起访谈用的口令（自拟随机串）

均已在 `.gitignore` 中忽略。

## 部署概要

1. 后端：`cp backend/app.py backend/system_prompt.md /opt/hc-survey/`，填写上述凭据文件，`cp systemd/hc-survey.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now hc-survey`
2. 前端：`cp frontend/* /data/website/hc.quickpapa.com/`
3. nginx：`cp nginx/hc.quickpapa.com.conf /etc/nginx/conf.d/`，证书由 `/opt/sslrenew/sslrenew.sh issue hc.quickpapa.com` 签发
4. 数据库：CouchDB 库 `hc_answers` / `hc_sessions`（后端启动时幂等创建）

## 关键设计点

- **批次追溯 + 财税合规**贯穿系统设计；访谈与摘要强制覆盖税务合规章节。
- glm-5.2 为 reasoning 模型，先输出 thinking 再输出 text，`max_tokens` 需给足（聊天 4000 / 摘要 6000）。
- 会话归属：新会话下发 `session_token`，后续操作必须带回，防止跨人劫持。
- 部署方式：**SaaS**（已定）。

详见 `docs/`。