#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本草链 HerbChain — 需求收集后端
问卷:
  POST /api/answers              收集一份固定问卷答卷 -> CouchDB(hc_answers)
  GET  /api/answers?token=XXX     查看全部答卷(凭 view_token)
  GET  /api/stats?token=XXX       选项分布统计
LLM 访谈(凭 chat_access 口令):
  POST /api/chat                  一轮对话(自适应追问)
  POST /api/extract               把该会话整理成结构化需求摘要
  GET  /api/sessions?token=XXX    会话列表(凭 view_token)
  GET  /api/session/<id>?token=XXX 单会话记录
只监听 127.0.0.1:5006，由 nginx 反代 /api。

存储层在 CouchDB(DB 机器 10.0.0.3:5984):库 hc_answers / hc_sessions。
连接配置在 couchdb.env(COUCHDB_URL/COUCHDB_USER/COUCHDB_PASS)。
需求摘要除入库外仍另存一份 requirements/<sid>.md 便于本地查阅。
"""
import json, os, re, sys, base64, secrets, ipaddress, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone
from flask import Flask, request, jsonify, abort

APP_DIR    = os.path.dirname(os.path.abspath(__file__))
REQ_DIR    = "/data/hc-survey/requirements"
TOKEN_FILE = os.path.join(APP_DIR, "view_token")
ACCESS_FILE= os.path.join(APP_DIR, "chat_access")
LLM_ENV    = os.path.join(APP_DIR, "llm.env")
PROMPT_FILE= os.path.join(APP_DIR, "system_prompt.md")

# CouchDB
COUCH_ENV   = os.path.join(APP_DIR, "couchdb.env")
DB_ANSWERS  = "hc_answers"
DB_SESSIONS = "hc_sessions"

MAX_TURNS  = 24          # 单会话最多用户消息条数
LLM_TIMEOUT= 120
LLM_MAXTOK = 4000        # 聊天:glm-5.2 先 thinking 后 text,留足防正文被截断(回复仍按提示词保持简短)
EXTRACT_MAXTOK = 6000    # 生成需求摘要:要把整场访谈整理成结构化Markdown,给足

app = Flask(__name__)
os.makedirs(REQ_DIR, exist_ok=True)


def _read(path, default=""):
    try:
        return open(path, encoding="utf-8").read().strip()
    except OSError:
        return default


def _load_env(path):
    env = {}
    for line in _read(path).splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


# ---------- CouchDB 存储 ----------
_couch_url = None
_couch_auth = None

def _couch():
    """返回 (url, auth_header)。首次调用读 couchdb.env；未配置返回 (None, None)。"""
    global _couch_url, _couch_auth
    if _couch_url is not None:
        return _couch_url, _couch_auth
    env = _load_env(COUCH_ENV)
    url = env.get("COUCHDB_URL", "").rstrip("/")
    if not url:
        _couch_url, _couch_auth = "", ""
        return None, None
    user, pw = env.get("COUCHDB_USER", ""), env.get("COUCHDB_PASS", "")
    _couch_url = url
    _couch_auth = "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    return _couch_url, _couch_auth

def _couch_req(method, path, body=None, params=None, timeout=10):
    url, auth = _couch()
    if url is None:
        return 599, {"error": "CouchDB 未配置"}
    full = url + path
    if params:
        full += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(full, data=data, method=method,
        headers={"Authorization": auth,
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.getcode(), (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try: payload = json.loads(raw) if raw else {}
        except Exception: payload = {"error": raw}
        return e.code, payload
    except Exception as e:
        return 0, {"error": str(e)}

_db_inited = False
def _init_db():
    """幂等建库(201 新建 / 412 已存在 都忽略)。"""
    global _db_inited
    if _db_inited or _couch()[0] is None:
        return
    for db in (DB_ANSWERS, DB_SESSIONS):
        _couch_req("PUT", "/" + db)
    _db_inited = True

def _strip(doc):
    """去掉 CouchDB 内部字段,供 API 输出。"""
    if not isinstance(doc, dict):
        return doc
    doc = dict(doc)
    doc.pop("_id", None)
    doc.pop("_rev", None)
    return doc

def _all_docs(db):
    _init_db()
    code, resp = _couch_req("GET", "/" + db + "/_all_docs",
                            params={"include_docs": "true", "limit": "10000"})
    if code != 200:
        return []
    return [r.get("doc") for r in resp.get("rows", []) if r.get("doc")]


view_token = lambda: _read(TOKEN_FILE)
access_code = lambda: _read(ACCESS_FILE)
check_token = lambda: (request.args.get("token", "") or "") == view_token()

def client_ip():
    xf = request.headers.get("X-Forwarded-For", "")
    return xf.split(",")[0].strip() if xf else (request.remote_addr or "")


# ---------- 问卷 ----------
@app.post("/api/answers")
def submit():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()[:60]
    role = (data.get("role") or "").strip()[:60]
    answers = data.get("answers")
    if not isinstance(answers, dict) or not answers:
        return jsonify(ok=False, error="answers 缺失"), 400
    clean = {}
    for k, v in answers.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            clean[str(k)[:80]] = str(v)[:2000] if isinstance(v, str) else v
        else:
            clean[str(k)[:80]] = json.dumps(v, ensure_ascii=False)[:2000]
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "ip": client_ip(), "name": name, "role": role,
           "answers": clean, "note": (data.get("note") or "").strip()[:4000]}
    _init_db()
    code, resp = _couch_req("POST", "/" + DB_ANSWERS, body=rec)
    if code not in (200, 201):
        return jsonify(ok=False, error="存储失败", detail=resp), 500
    return jsonify(ok=True, saved=True)


@app.get("/api/answers")
def list_answers():
    if not check_token():
        abort(403)
    rows = [_strip(d) for d in _all_docs(DB_ANSWERS)]
    return jsonify(ok=True, count=len(rows), rows=rows)


@app.get("/api/stats")
def stats():
    if not check_token():
        abort(403)
    from collections import Counter
    rows = _all_docs(DB_ANSWERS)
    tally = {}
    for d in rows:
        for q, a in (d.get("answers") or {}).items():
            tally.setdefault(q, Counter())[str(a)] += 1
    out = {q: [{"option": k, "count": v} for k, v in c.most_common()]
           for q, c in tally.items()}
    return jsonify(ok=True, total=len(rows), tally=out)


# ---------- LLM 访谈 ----------
def call_llm(system, messages, max_tokens=LLM_MAXTOK):
    env = _load_env(LLM_ENV)
    base = env.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    tok  = env.get("ANTHROPIC_AUTH_TOKEN", "")
    model= env.get("ANTHROPIC_MODEL", "")
    if not (base and tok and model):
        raise RuntimeError("LLM 配置缺失(llm.env)")
    payload = {"model": model, "max_tokens": max_tokens,
               "system": system, "messages": messages}
    req = urllib.request.Request(
        base + "/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"x-api-key": tok, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
        data = json.loads(r.read().decode("utf-8"))
    # 合并所有 text 块(跳过 thinking)
    parts = []
    for b in data.get("content", []):
        if b.get("type") == "text" and b.get("text"):
            parts.append(b["text"])
    return "\n".join(parts).strip(), data.get("usage", {})


def load_session(sid):
    _init_db()
    code, resp = _couch_req("GET", "/" + DB_SESSIONS + "/" + urllib.parse.quote(sid, safe=""))
    if code != 200:
        return None
    return resp   # 含 _rev,供后续 PUT 更新


def save_session(sess):
    _init_db()
    sid = sess["id"]
    body = dict(sess)
    code, resp = _couch_req("PUT", "/" + DB_SESSIONS + "/" + urllib.parse.quote(sid, safe=""), body=body)
    if code in (200, 201) and isinstance(resp, dict):
        sess["_rev"] = resp.get("rev")   # 记下新 _rev,下次更新用
        return True
    print("hc-survey: save_session FAIL sid=%s code=%s resp=%s" % (sid, code, resp), file=sys.stderr)
    return False


_SID_RE = re.compile(r"[A-Za-z0-9_\-]+")


def new_session_id():
    import secrets
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)


def base_prompt():
    return _read(PROMPT_FILE) or "你是本草链中药材ERP系统的需求分析师，请通过对话收集用户的具体需求。一次只问一个最关键的问题。"


@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    if (data.get("access") or "") != access_code():
        return jsonify(ok=False, error="访问口令错误"), 403
    sid = (data.get("session_id") or "").strip()
    is_opener = bool(data.get("opener"))
    sess = load_session(sid) if sid else None
    if sess is None:
        sid = new_session_id()
        sess = {"id": sid, "token": secrets.token_urlsafe(16),
                "name": (data.get("name") or "").strip()[:60],
                "role": (data.get("role") or "").strip()[:60],
                "ip": client_ip(), "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "msgs": []}
        is_new = True
    else:
        # 会话归属校验:新会话带 token,调用方必须提供匹配 token;老会话(无token)仅凭 access_code
        st = sess.get("token")
        if st and (data.get("session_token") or "") != st:
            return jsonify(ok=False, error="会话令牌不匹配,无权操作该访谈"), 403
        is_new = False
    user_msg = (data.get("message") or "").strip()
    if not user_msg:
        return jsonify(ok=False, error="message 为空"), 400
    if len(sess["msgs"]) // 2 >= MAX_TURNS:
        return jsonify(ok=False, error="本会话已达最大轮数，请新开会话或生成需求摘要",
                       session_id=sid, max=True), 429
    # 角色上下文注入到 system
    role_line = f"\n\n## 当前访谈对象\n称呼：{sess['name'] or '未知'}\n角色：{sess['role'] or '未指定'}\n请基于该角色调整追问方向。"
    system = base_prompt() + role_line
    history = [{"role": m["role"], "content": m["content"]} for m in sess["msgs"]]
    history.append({"role": "user", "content": user_msg})
    try:
        reply, usage = call_llm(system, history)
    except Exception as e:
        # LLM 失败:非opener才存用户消息(opener的message是元指令,不存);存失败也不吞
        if not is_opener:
            sess["msgs"].append({"role": "user", "content": user_msg})
            sess["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            save_session(sess)  # 尽力存,失败已通过 save_session 记日志
        return jsonify(ok=False, error="LLM 调用失败: %s" % e, session_id=sid), 502
    if not reply:
        reply = "（模型未返回正文，请再说一句）"
    # opener 的 message 是"开场元指令",不作为用户消息入库,只存分析师的开场白 -> 记录干净
    if not is_opener:
        sess["msgs"].append({"role": "user", "content": user_msg})
    sess["msgs"].append({"role": "assistant", "content": reply})
    sess["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not save_session(sess):
        return jsonify(ok=False, error="保存失败(CouchDB不可用?),请重试", session_id=sid), 500
    out = {"ok": True, "reply": reply, "session_id": sid, "turn": (len(sess["msgs"]) // 2)}
    if is_new:
        out["session_token"] = sess["token"]   # 创建时下发,后续请求须带回
    return jsonify(out)


@app.post("/api/extract")
def extract():
    data = request.get_json(silent=True) or {}
    if (data.get("access") or "") != access_code():
        return jsonify(ok=False, error="访问口令错误"), 403
    sid = (data.get("session_id") or "").strip()
    sess = load_session(sid)
    if not sess:
        return jsonify(ok=False, error="会话不存在"), 404
    st = sess.get("token")
    if st and (data.get("session_token") or "") != st:
        return jsonify(ok=False, error="会话令牌不匹配,无权操作该访谈"), 403
    if not _SID_RE.fullmatch(sid):
        return jsonify(ok=False, error="会话id非法"), 400
    transcript = "\n".join(f"{'用户' if m['role']=='user' else '分析师'}：{m['content']}"
                           for m in sess["msgs"])
    sys = ("你是本草链中药材ERP系统需求分析师。下面是一段需求访谈记录。"
           "请整理成结构化的需求摘要(Markdown)，包含：访谈对象(称呼/角色)、"
           "关键业务现状、已明确的需求点(按 收购/加工/质量追溯/仓储/销售/发货物流/财务分润/税务合规/部署 等分类，"
           "每一类都要有；没有涉及的写'未涉及')、税务合规风险点与待落实项(单独一节，"
           "覆盖农产品收购发票/进项抵扣/销项开票/三流一致/个税印花税/药监GSP等，按访谈实际涉及到的写)、"
           "待确认的开放问题、对该角色的特别建议。"
           "只输出 Markdown 正文，不要输出 thinking。")
    try:
        md, _ = call_llm(sys, [{"role": "user", "content": transcript}], max_tokens=EXTRACT_MAXTOK)
    except Exception as e:
        return jsonify(ok=False, error="LLM 调用失败: %s" % e), 502
    md = md or "（生成失败，请重试）"
    # 本地另存一份 markdown 便于操作员查阅(requirements 目录不在 webroot,nginx 不直接暴露)
    with open(os.path.join(REQ_DIR, sid + ".md"), "w", encoding="utf-8") as f:
        f.write("# 需求摘要 · " + sid + "\n\n" + md)
    sess["requirements_md"] = md
    save_ok = save_session(sess)
    if not save_ok:
        # 本地md已写,但CouchDB没存 -> 结果页看不到。告知但仍返回摘要正文。
        return jsonify(ok=True, session_id=sid, markdown=md,
                       warning="摘要已生成但未入库(CouchDB不可用),结果页可能不显示,已存本地"), 200
    return jsonify(ok=True, session_id=sid, markdown=md)


@app.get("/api/sessions")
def sessions():
    if not check_token():
        abort(403)
    out = []
    for s in _all_docs(DB_SESSIONS):
        out.append({"id": s.get("id", s.get("_id", "")),
                    "name": s.get("name", ""),
                    "role": s.get("role", ""),
                    "turns": len(s.get("msgs", [])) // 2,
                    "updated": s.get("updated", s.get("created", "")),
                    "has_req": bool(s.get("requirements_md"))})
    out.sort(key=lambda x: x["id"])   # 与原 sorted(os.listdir) 顺序一致(按 sid 升序)
    return jsonify(ok=True, count=len(out), sessions=out)


@app.get("/api/session/<sid>")
def session_detail(sid):
    if not check_token():
        abort(403)
    s = load_session(sid)
    if not s:
        abort(404)
    return jsonify(ok=True, session=_strip(s))


@app.get("/")
def root():
    return jsonify(ok=True, service="hc-survey", version="2.1-couchdb")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5006)