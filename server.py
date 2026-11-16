# -*- coding: utf-8 -*-
"""
影刀社区自动回答系统 · 本地服务端
零第三方依赖，仅用 Python 标准库。

功能：
  1) 采集影刀社区最新问题（无人回答优先）
  2) 调用外部大模型（OpenAI 兼容接口）阅读问题并生成答案
  3) 把生成的答案再交给模型做质量闸门（含代码层硬规则校验）
  4) 人工确认后发送 / 复制答案手动发布

启动： python server.py
"""

import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data.db")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATIC_DIR = os.path.join(BASE_DIR, "static")
DEFAULT_PORT = 8770

COMMUNITY_API = "https://api.yingdao.com/api/noauth/v1/sns/forum"
QUESTION_PAGE = "https://www.yingdao.com/community/detaildiscuss?id="

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")

DEFAULT_PROMPT_ANSWER = """你是影刀RPA社区里一位经验丰富的技术答主，擅长把报错和卡点拆成可执行的排查步骤。

要求：
1. 先给结论/方向（1-2 句），再给步骤；步骤用 1. 2. 3. 编号，每步说清楚在哪个指令/哪个界面做什么。
2. 涉及影刀指令时必须使用官方指令名（例如「获取已打开的网页对象」「捕获元素」「循环相似元素」），并用反引号包裹。
3. 如果问题信息不足（缺报错截图、缺流程版本、缺元素截图），直接指出需要补充什么，不要凭空编造。
4. 不要写"建议联系官方客服""可以咨询工程师"这类空话；也不要留联系方式。
5. 控制在 400 字以内，不写客套话，不要用"您好"，直接从问题本身切入。"""

DEFAULT_PROMPT_GATE = """你是影刀RPA社区的版主，负责审核 AI 生成的回答是否可以发布。你的审核非常严格。

请从这些角度打分（0-100）：
- 是否真的解决了提问者的问题（最重要）
- 指令名、界面路径是否准确，有没有编造不存在的指令
- 是否只给了空泛建议而没有可执行步骤
- 是否出现硬广、联系方式、求私聊
- 语气是否像真人答主

只输出一个 JSON 对象，不要任何解释文字，格式：
{"score": 82, "verdict": "PASS", "issues": ["问题1", "问题2"], "suggestion": "一句话改进建议"}
verdict 取值：PASS（可直接发）、NEED_FIX（改改能发）、REJECT（不该发）。
score 低于 70 时 verdict 必须是 NEED_FIX 或 REJECT。"""

DEFAULT_CONFIG = {
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.4,
        "timeout": 120,
    },
    "collect": {
        "api_base": "https://api.yingdao.com/api/noauth/v1/sns/forum",
        "list_path": "/question/query",
        "detail_path": "/question/detail",
        "answer_path": "/answer/query",
        "tag": "问答",
        "pages": 2,
        "size": 20,
        "delay": 0.25,
        "user_agent": "",
    },
    "send": {
        "mode": "manual",
        "submit_url": "https://api.yingdao.com/api/v1/sns/forum/answer/create",
        "method": "POST",
        "auth_type": "auto",
        "auth_value": "",
        "headers": {},
        "body_template": "",
        "content_type": "html",
    },
    "prompt": {
        "answer_system": DEFAULT_PROMPT_ANSWER,
        "gate_system": DEFAULT_PROMPT_GATE,
    },
}


# ---------------------------------------------------------------- 基础工具

def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def deep_merge(base, patch):
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    return deep_merge(DEFAULT_CONFIG, raw)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS questions (
                uuid TEXT PRIMARY KEY,
                title TEXT,
                content TEXT,
                images TEXT,
                creator TEXT,
                create_time TEXT,
                answer_count INTEGER DEFAULT 0,
                browse_count INTEGER DEFAULT 0,
                url TEXT,
                status TEXT DEFAULT 'new',
                fetched_at TEXT
            );
            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_uuid TEXT,
                text TEXT,
                model TEXT,
                demo INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS gates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_uuid TEXT,
                draft_id INTEGER,
                score INTEGER,
                verdict TEXT,
                issues TEXT,
                suggestion TEXT,
                rules TEXT,
                demo INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS sends (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                question_uuid TEXT,
                final_text TEXT,
                mode TEXT,
                result TEXT,
                sent_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_draft_q ON drafts(question_uuid);
            CREATE INDEX IF NOT EXISTS idx_gate_q ON gates(question_uuid);
            """
        )
        # 兼容早期数据库：补上"是否已解决"字段
        cols = [r[1] for r in conn.execute("PRAGMA table_info(questions)").fetchall()]
        if "is_accept" not in cols:
            conn.execute("ALTER TABLE questions ADD COLUMN is_accept INTEGER DEFAULT 0")


def safe_json(raw, default=None):
    """数据库里的 JSON 字段容错解析，单条脏数据不至于把整个列表打挂。"""
    if raw is None or raw == "":
        return [] if default is None else default
    try:
        return json.loads(raw)
    except Exception:
        return [] if default is None else default


def strip_html(raw):
    if not raw:
        return "", []
    images = re.findall(r'<img[^>]+src="([^"]+)"', raw)
    txt = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    txt = re.sub(r"(?i)</(p|div|li|h\d)>", "\n", txt)
    txt = re.sub(r"(?i)<img[^>]*>", "[图片] ", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = html.unescape(txt)
    txt = re.sub(r"[ \t\u3000]+", " ", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return txt, images


def http_json(url, method="GET", payload=None, headers=None, timeout=25, retries=2):
    last_err = None
    for attempt in range(retries + 1):
        try:
            data = None
            hdrs = {"User-Agent": UA, "Accept": "application/json"}
            if headers:
                hdrs.update(headers)
            if payload is not None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                hdrs["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
            return json.loads(body)
        except urllib.error.HTTPError as e:
            # 401/403/400 这类响应体里通常带明确原因，原样返回给上层判断
            try:
                body = e.read().decode("utf-8", "replace")
                return json.loads(body)
            except Exception:
                last_err = RuntimeError("HTTP %s %s" % (e.code, e.reason))
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt < retries:
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(str(last_err))


# ---------------------------------------------------------------- 社区采集

def collect_cfg():
    cfg = load_config()["collect"]
    base_default = DEFAULT_CONFIG["collect"]
    base = (cfg.get("api_base") or base_default["api_base"]).rstrip("/")
    if not base.startswith("http"):
        base = base_default["api_base"]
    return cfg, base


def endpoint(path_key, default_path):
    cfg, base = collect_cfg()
    path = (cfg.get(path_key) or default_path).strip()
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def ua_headers():
    cfg, _ = collect_cfg()
    ua = (cfg.get("user_agent") or "").strip()
    return {"User-Agent": ua} if ua else None


def fetch_question_page(page, size, tag):
    params = {"page": page, "size": size}
    if tag:
        params["tags"] = tag
    qs = urllib.parse.urlencode(params)
    resp = http_json("%s?%s" % (endpoint("list_path", "/question/query"), qs), headers=ua_headers())
    if not resp.get("success"):
        raise RuntimeError(resp.get("msg") or "社区接口返回失败")
    return resp.get("data") or []


def fetch_question_detail(uuid):
    qs = urllib.parse.urlencode({"forumQuestionUuid": uuid})
    resp = http_json(
        "%s?%s" % (endpoint("detail_path", "/question/detail"), qs), headers=ua_headers()
    )
    return resp.get("data") or {}


def fetch_answers(uuid):
    """返回规范化后的已有回答：[{author, content, time, official}]"""
    qs = urllib.parse.urlencode({"forumQuestionUuid": uuid, "page": 1, "size": 10})
    try:
        resp = http_json(
            "%s?%s" % (endpoint("answer_path", "/answer/query"), qs), headers=ua_headers()
        )
        raw = resp.get("data") or []
    except Exception:
        return []
    return [
        {
            "author": a.get("creatorName") or "",
            "content": strip_html(a.get("content") or "")[0],
            "time": a.get("publishTime") or a.get("createTime") or "",
            "official": bool(a.get("official")),
        }
        for a in raw
    ]


def do_collect():
    cfg = load_config()["collect"]
    pages = max(1, min(5, int(cfg.get("pages", 2))))
    size = max(10, min(50, int(cfg.get("size", 20))))
    tag = (cfg.get("tag") or "").strip()  # 留空 = 不限标签
    try:
        delay = max(0.0, min(5.0, float(cfg.get("delay", 0.25))))
    except (TypeError, ValueError):
        delay = 0.25

    added, refreshed, seen = 0, 0, 0
    with db() as conn:
        for page in range(1, pages + 1):
            try:
                items = fetch_question_page(page, size, tag)
            except Exception as e:  # noqa: BLE001
                log("采集第 %s 页失败: %s" % (page, e))
                break
            if not items:
                break

            for item in items:
                uuid = item.get("uuid")
                if not uuid:
                    continue
                seen += 1
                row = conn.execute(
                    "SELECT uuid, answer_count FROM questions WHERE uuid=?", (uuid,)
                ).fetchone()
                answer_count = int(item.get("answerCount") or 0)
                is_accept = 1 if item.get("isAccept") else 0

                # 已存在且回答数没变：只刷新浏览数和解决状态，不再拉详情（减少请求）
                try:
                    prev_answer = int(row["answer_count"]) if row else -1
                except (TypeError, ValueError):
                    prev_answer = -1  # 脏数据，强制重拉覆盖
                if row and prev_answer == answer_count:
                    conn.execute(
                        "UPDATE questions SET browse_count=?, is_accept=? WHERE uuid=?",
                        (int(item.get("browseCount") or 0), is_accept, uuid),
                    )
                    conn.commit()
                    continue

                content, images = "", []
                try:
                    detail = fetch_question_detail(uuid)
                    content, images = strip_html(detail.get("content") or "")
                except Exception as e:  # noqa: BLE001
                    log("详情获取失败 %s: %s" % (uuid, e))
                time.sleep(delay)

                values = (
                    uuid,
                    item.get("title") or "",
                    content,
                    json.dumps(images, ensure_ascii=False),
                    item.get("creatorName") or "",
                    item.get("createTime") or "",
                    answer_count,
                    int(item.get("browseCount") or 0),
                    QUESTION_PAGE + uuid,
                    now_str(),
                )
                if row:
                    conn.execute(
                        """UPDATE questions SET title=?, content=?, images=?, creator=?,
                           create_time=?, answer_count=?, browse_count=?, url=?, fetched_at=?,
                           is_accept=?
                           WHERE uuid=?""",
                        values[1:10] + (is_accept, uuid),
                    )
                    refreshed += 1
                else:
                    conn.execute(
                        """INSERT INTO questions
                           (uuid,title,content,images,creator,create_time,answer_count,
                            browse_count,url,fetched_at,is_accept,status)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?, 'new')""",
                        values + (is_accept,),
                    )
                    added += 1
                # 单条即时提交，避免长时间持有写锁阻塞页面上的其它操作
                conn.commit()
            time.sleep(0.4)

    log("采集完成：扫描 %s 条，新增 %s 条，更新 %s 条" % (seen, added, refreshed))
    return {"seen": seen, "added": added, "refreshed": refreshed}


# ---------------------------------------------------------------- 问题查询

def list_questions(view="pending", solved="all", sort="new", limit=200):
    where = {
        "pending": "status IN ('new','drafted','gated','needfix')",
        "noanswer": "answer_count = 0 AND status IN ('new','drafted','gated','needfix')",
        "drafted": "status IN ('drafted','gated','needfix')",
        "sent": "status IN ('sent','skipped')",
        "all": "1=1",
    }.get(view, "1=1")

    if solved == "solved":
        where += " AND COALESCE(is_accept, 0) = 1"
    elif solved == "unsolved":
        where += " AND COALESCE(is_accept, 0) = 0"

    order = "create_time ASC" if sort == "old" else "create_time DESC"

    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM questions WHERE %s ORDER BY %s LIMIT ?" % (where, order),
            (limit,),
        ).fetchall()

        out = []
        for r in rows:
            q = dict(r)
            q["images"] = safe_json(q.get("images"))
            d = conn.execute(
                "SELECT * FROM drafts WHERE question_uuid=? ORDER BY id DESC LIMIT 1",
                (q["uuid"],),
            ).fetchone()
            g = conn.execute(
                "SELECT * FROM gates WHERE question_uuid=? ORDER BY id DESC LIMIT 1",
                (q["uuid"],),
            ).fetchone()
            q["draft"] = dict(d) if d else None
            q["gate"] = dict(g) if g else None
            if q["gate"]:
                q["gate"]["issues"] = safe_json(q["gate"].get("issues"))
                q["gate"]["rules"] = safe_json(q["gate"].get("rules"))
            out.append(q)
        return out


def get_stats():
    """实时统计：每次调用都重新查库，保证页眉数字与采集结果一致。"""
    with db() as conn:
        row = conn.execute(
            """SELECT
                 SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) AS new_cnt,
                 SUM(CASE WHEN status IN ('drafted','gated','needfix') THEN 1 ELSE 0 END) AS doing,
                 SUM(CASE WHEN status='sent' THEN 1 ELSE 0 END) AS sent,
                 SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END) AS skipped,
                 COUNT(*) AS total,
                 SUM(CASE WHEN answer_count=0 THEN 1 ELSE 0 END) AS no_answer,
                 SUM(CASE WHEN COALESCE(is_accept,0)=1 THEN 1 ELSE 0 END) AS solved,
                 SUM(CASE WHEN COALESCE(is_accept,0)=0 THEN 1 ELSE 0 END) AS unsolved
               FROM questions"""
        ).fetchone()
    cfg = load_config()
    stats = {
        "new": int(row["new_cnt"] or 0),
        "doing": int(row["doing"] or 0),
        "sent": int(row["sent"] or 0),
        "skipped": int(row["skipped"] or 0),
        "total": int(row["total"] or 0),
        "no_answer": int(row["no_answer"] or 0),
        "solved": int(row["solved"] or 0),
        "unsolved": int(row["unsolved"] or 0),
    }
    stats["pending"] = stats["new"] + stats["doing"]
    return {
        "ok": True,
        "stats": stats,
        "llm_ready": bool((cfg["llm"].get("api_key") or "").strip()),
        "model": cfg["llm"].get("model"),
        "send_mode": cfg["send"].get("mode"),
    }


def get_question(uuid):
    with db() as conn:
        r = conn.execute("SELECT * FROM questions WHERE uuid=?", (uuid,)).fetchone()
        if not r:
            return None
        q = dict(r)
        q["images"] = safe_json(q.get("images"))
        q["answers"] = fetch_answers(uuid)
        return q


# ---------------------------------------------------------------- 大模型

PRESET_MODELS = {
    "deepseek": [
        ("deepseek-chat", "通用对话，速度快，社区答疑首选"),
        ("deepseek-reasoner", "推理模型，质量更高但更慢"),
    ],
    "dashscope": [
        ("qwen-plus", "通义千问均衡版"),
        ("qwen-max", "最强版，贵一些"),
        ("qwen-turbo", "最快最便宜"),
        ("qwen-long", "超长上下文"),
    ],
    "moonshot": [
        ("moonshot-v1-8k", "Kimi 8K 上下文"),
        ("moonshot-v1-32k", "Kimi 32K 上下文"),
        ("moonshot-v1-128k", "Kimi 128K 上下文"),
    ],
    "bigmodel": [
        ("glm-4-plus", "智谱旗舰"),
        ("glm-4-air", "轻量版"),
        ("glm-4-flash", "免费额度版"),
    ],
    "openai": [
        ("gpt-4o-mini", "便宜快速"),
        ("gpt-4o", "质量最好"),
    ],
}


def suggest_models(base_url):
    """拉不到列表时，按服务商给一份常见模型，别让用户卡住。"""
    b = (base_url or "").lower()
    for key, items in PRESET_MODELS.items():
        if key in b:
            return [{"id": i, "note": n} for i, n in items]
    return []


def list_models(base_url=None, api_key=None):
    cfg = load_config()["llm"]
    base = (base_url if base_url is not None else cfg.get("base_url") or "").strip().rstrip("/")
    key = (api_key if api_key is not None else cfg.get("api_key") or "").strip()
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    if not base:
        return {"ok": True, "models": [], "source": "preset", "error": "还没填接口地址"}
    if not key:
        return {
            "ok": True,
            "models": suggest_models(base),
            "source": "preset",
            "error": "还没填 API Key，先列出常见模型",
        }
    try:
        resp = http_json(
            base + "/models",
            headers={"Authorization": "Bearer " + key},
            timeout=20,
            retries=0,
        )
        data = resp.get("data") or []
        models = [
            {"id": m.get("id"), "note": ("属于 " + m["owned_by"]) if m.get("owned_by") else ""}
            for m in data
            if isinstance(m, dict) and m.get("id")
        ]
        if models:
            return {"ok": True, "models": models, "source": "api"}
        err = (resp.get("error") or {}).get("message") or resp.get("msg") or "接口没有返回模型列表"
        return {"ok": True, "models": suggest_models(base), "source": "preset", "error": err}
    except Exception as e:  # noqa: BLE001
        return {"ok": True, "models": suggest_models(base), "source": "preset", "error": str(e)}


def llm_chat(messages, temperature=None, max_tokens=1800, json_mode=False):
    """返回 (content, demo_flag)。未配置 api_key 时进入演示模式。"""
    cfg = load_config()["llm"]
    key = (cfg.get("api_key") or "").strip()
    if not key:
        return None, True

    base = (cfg.get("base_url") or "").rstrip("/")
    if not base.endswith("/chat/completions"):
        base = base + "/chat/completions"
    payload = {
        "model": cfg.get("model") or "deepseek-flash",
        "messages": messages,
        "temperature": cfg.get("temperature", 0.4) if temperature is None else temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        resp = http_json(
            base,
            method="POST",
            payload=payload,
            headers={"Authorization": "Bearer " + key},
            timeout=int(cfg.get("timeout", 120)),
            retries=0,
        )
        return resp["choices"][0]["message"]["content"], False
    except Exception as e:  # noqa: BLE001
        # 有些网关不认 response_format，去掉再试一次
        if json_mode:
            payload.pop("response_format", None)
            try:
                resp = http_json(
                    base,
                    method="POST",
                    payload=payload,
                    headers={"Authorization": "Bearer " + key},
                    timeout=int(cfg.get("timeout", 120)),
                    retries=0,
                )
                return resp["choices"][0]["message"]["content"], False
            except Exception as e2:  # noqa: BLE001
                raise RuntimeError("大模型调用失败：%s" % e2)
        raise RuntimeError("大模型调用失败：%s" % e)


def demo_answer(q):
    imgs = q.get("images") or []
    return (
        "**先定位是元素层面还是风控层面**。如果每次第二次查询才出图片点选，基本可以判定是目标站点对"
        "自动化特征做了拦截，而不是流程指令写错了。\n\n"
        "1. 用「获取已打开的网页对象」拿到当前页面，确认 `web_page` 对象在循环外只获取一次，"
        "不要在循环里反复打开，否则每次都是新的会话指纹。\n"
        "2. 在查询动作前插入「等待」随机 1.5-3 秒，并在「设置浏览器」里关闭无头模式，"
        "让浏览器指纹更接近真人。\n"
        "3. 把同一账号的查询频率降下来，加一个 60 秒以上的间隔；若仍触发，改用「切换浏览器」"
        "配合代理 IP 轮换。\n"
        "4. 最后给元素捕获加兜底：用「IF 元素存在」判断是否出现图片点选，命中就走人工介入或稍后重试。\n\n"
        "补充建议：把报错截图和当前影刀版本号发上来，方便判断是不是新版的指纹策略变化。"
        + ("\n\n（该帖含 %d 张截图，建议结合截图核对元素位置）" % len(imgs) if imgs else "")
    )


def demo_gate(draft):
    score = 68 if len(draft) < 260 else 78
    issues = []
    if "补充" in draft:
        issues.append("结尾要求补充信息的表述偏笼统")
    if "代理 IP" in draft:
        issues.append("涉及代理 IP 的建议，需确认是否符合社区讨论规范")
    return json.dumps(
        {
            "score": score,
            "verdict": "PASS" if score >= 75 else "NEED_FIX",
            "issues": issues,
            "suggestion": "把第 3 步的限频参数写得更具体，并说明在哪个版本的影刀里验证过。",
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------- 生成 / 质检

def build_answer_prompt(q):
    cfg = load_config()["prompt"]
    answers_txt = "（暂无回答）"
    if q.get("answers"):
        answers_txt = "\n".join(
            "- %s：%s" % (a["author"], (a["content"] or "")[:200]) for a in q["answers"][:4]
        )
    user = (
        "【问题标题】%s\n【发帖人】%s\n【发帖时间】%s\n【当前回答数】%s\n"
        "【问题正文】\n%s\n\n【已有回答（供参考，不要照抄，也不要重复别人说过的内容）】\n%s\n\n"
        "请给出一条可以直接发布在社区的回答正文（Markdown）。"
        % (
            q.get("title"),
            q.get("creator"),
            q.get("create_time"),
            q.get("answer_count"),
            (q.get("content") or "（作者只发了标题，没有正文）")[:2500],
            answers_txt,
        )
    )
    return [
        {"role": "system", "content": cfg["answer_system"]},
        {"role": "user", "content": user},
    ]


def rule_check(draft, q):
    """代码层硬规则，不依赖模型。"""
    text = draft or ""
    checks = []

    length = len(re.sub(r"\s", "", text))
    checks.append({
        "name": "篇幅达标",
        "pass": length >= 150,
        "detail": "正文 %d 字（建议 ≥150）" % length,
    })

    has_step = bool(re.search(r"(^|\n)\s*(1[\.、]|一、|步骤)", text))
    checks.append({
        "name": "有可执行步骤",
        "pass": has_step,
        "detail": "检测到编号步骤" if has_step else "没有编号步骤，读起来像泛泛而谈",
    })

    cmds = re.findall(r"[「『]([^」』]{2,20})[」』]|`([^`\n]{2,30})`", text)
    cmd_count = len(cmds)
    checks.append({
        "name": "引用具体指令",
        "pass": cmd_count >= 1,
        "detail": "命中 %d 处指令/代码片段" % cmd_count,
    })

    bad = re.findall(r"(微信号|加微信|私聊|QQ群|付费|代做|联系我)", text)
    checks.append({
        "name": "无推广联系方式",
        "pass": not bad,
        "detail": "未发现" if not bad else "发现：" + "、".join(sorted(set(bad))),
    })

    empty = re.findall(r"(建议联系官方|咨询官方客服|请找工程师)", text)
    checks.append({
        "name": "无空话推诿",
        "pass": not empty,
        "detail": "未发现" if not empty else "发现：" + "、".join(sorted(set(empty))),
    })

    return checks


def parse_gate_json(raw):
    if not raw:
        return None
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def do_draft(uuid):
    with db() as conn:
        r = conn.execute("SELECT * FROM questions WHERE uuid=?", (uuid,)).fetchone()
    if not r:
        raise RuntimeError("问题不存在")
    q = dict(r)
    q["images"] = json.loads(q.get("images") or "[]")
    q["answers"] = fetch_answers(uuid)

    content, demo = llm_chat(build_answer_prompt(q))
    if demo:
        content = demo_answer(q)
        model = "演示模式（未配置 API Key）"
    else:
        model = load_config()["llm"].get("model") or ""

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO drafts (question_uuid,text,model,demo,created_at) VALUES (?,?,?,?,?)",
            (uuid, content, model, 1 if demo else 0, now_str()),
        )
        draft_id = cur.lastrowid
        conn.execute("UPDATE questions SET status='drafted' WHERE uuid=?", (uuid,))
    log("已生成答案：%s（%s 字）" % (uuid, len(content)))
    return {"draft_id": draft_id, "text": content, "model": model, "demo": demo}


def do_gate(uuid, draft_text=None):
    with db() as conn:
        r = conn.execute("SELECT * FROM questions WHERE uuid=?", (uuid,)).fetchone()
        if not r:
            raise RuntimeError("问题不存在")
        q = dict(r)
        if draft_text is None:
            d = conn.execute(
                "SELECT * FROM drafts WHERE question_uuid=? ORDER BY id DESC LIMIT 1", (uuid,)
            ).fetchone()
            if not d:
                raise RuntimeError("还没有生成答案")
            draft_text = d["text"]
            draft_id = d["id"]
        else:
            draft_id = None

    rules = rule_check(draft_text, q)
    rule_fail = [c for c in rules if not c["pass"]]

    cfg = load_config()["prompt"]
    gate_user = (
        "【问题标题】%s\n【问题正文】\n%s\n\n【待审核的回答】\n%s\n\n"
        "请按要求输出 JSON。"
        % (q.get("title"), (q.get("content") or "（无正文）")[:1500], draft_text[:3000])
    )
    msgs = [
        {"role": "system", "content": cfg["gate_system"]},
        {"role": "user", "content": gate_user},
    ]
    raw, demo = llm_chat(msgs, temperature=0.1, max_tokens=800, json_mode=True)
    if demo:
        raw = demo_gate(draft_text)

    parsed = parse_gate_json(raw)
    if not parsed and not demo:
        # 模型偶尔会带上解释文字，用 JSON 模式再要一次，仍失败才按规则兜底
        raw2, _ = llm_chat(msgs, temperature=0, max_tokens=800, json_mode=True)
        parsed = parse_gate_json(raw2)
        if parsed:
            raw = raw2

    parse_failed = not parsed
    if parsed:
        score = int(parsed.get("score") or 70)
        verdict = parsed.get("verdict") or "NEED_FIX"
        issues = parsed.get("issues") or []
        suggestion = parsed.get("suggestion") or ""
    else:
        score = 70 if not rule_fail else 55
        verdict = "NEED_FIX"
        issues = ["模型没有按格式返回审核结果，已按硬规则兜底"]
        suggestion = "点「重新质检」再试一次"

    # 规则层扣分：每条硬规则不通过 -12
    score = max(0, min(100, score - 12 * len(rule_fail)))
    if len(rule_fail) >= 2 and verdict == "PASS":
        verdict = "NEED_FIX"
    if score < 50:
        verdict = "REJECT"

    with db() as conn:
        conn.execute(
            """INSERT INTO gates (question_uuid,draft_id,score,verdict,issues,suggestion,rules,demo,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                uuid,
                draft_id,
                score,
                verdict,
                json.dumps(issues, ensure_ascii=False),
                suggestion,
                json.dumps(rules, ensure_ascii=False),
                1 if demo else 0,
                now_str(),
            ),
        )
        conn.execute(
            "UPDATE questions SET status=? WHERE uuid=?",
            ("gated" if verdict == "PASS" else "needfix", uuid),
        )
    log("质检完成：%s → %s 分 / %s" % (uuid, score, verdict))
    return {
        "score": score,
        "verdict": verdict,
        "issues": issues,
        "suggestion": suggestion,
        "rules": rules,
        "demo": demo,
        "parse_failed": parse_failed,
        "raw": raw if parse_failed else None,
    }


# ---------------------------------------------------------------- 发送

def md_to_html(text):
    """把 Markdown 答案转成社区富文本能接受的简单 HTML。"""
    blocks = re.split(r"\n{2,}", (text or "").strip())
    out = []
    for b in blocks:
        b = b.strip("\n")
        if not b.strip():
            continue
        if b.startswith("```") and b.rstrip().endswith("```"):
            code = b.strip("`").strip("\n")
            out.append("<pre><code>%s</code></pre>" % html.escape(code, quote=False))
            continue
        s = html.escape(b, quote=False)
        s = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", s)
        s = s.replace("\n", "<br>")
        out.append('<p style="text-align: left;">%s</p>' % s)
    return "".join(out) or html.escape(text or "", quote=False)


def normalize_auth(raw, kind="cookie"):
    """把用户从浏览器复制的东西清洗成可直接用的值。
    支持：纯 Cookie 串、"Cookie: ..." 前缀、curl 的 -H 'Cookie: ...'、多行粘贴。"""
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.search(r"-H\s+['\"](?:Cookie|Authorization)\s*:\s*([^'\"]+)['\"]", s, re.I)
    if m:
        s = m.group(1)
    lines = []
    for ln in s.splitlines():
        ln = re.sub(r"^\s*(?:Cookie|Authorization)\s*:\s*", "", ln, flags=re.I).strip()
        if ln:
            lines.append(ln.rstrip(";").strip())
    if lines:
        s = "; ".join(lines)
    s = re.sub(r"\s*;\s*", "; ", s).strip()
    if kind == "bearer":
        s = re.sub(r"^Bearer\s+", "", s, flags=re.I).strip()
    return s


AUTH_KEY_HINT = re.compile(
    r"token|session|sid|jwt|auth|ticket|sso|login|uid|user|passport|credential", re.I
)


def inspect_auth(raw, kind="cookie"):
    """解析用户粘贴的凭证，返回名字层面的统计（不回传任何值）。"""
    s = normalize_auth(raw, kind)
    if not s:
        return {"ok": True, "kind": kind, "count": 0, "keys": [], "likely": [], "length": 0}
    # 用户直接粘了 "Bearer xxx" 时按 token 处理
    if s.lower().startswith("bearer "):
        s = s[7:].strip()
        kind = "bearer"
    if kind == "cookie":
        keys = []
        for part in s.split(";"):
            part = part.strip()
            if not part:
                continue
            k = part.split("=", 1)[0].strip()
            if k:
                keys.append(k)
        likely = [k for k in keys if AUTH_KEY_HINT.search(k)]
        return {
            "ok": True,
            "kind": "cookie",
            "count": len(keys),
            "keys": keys[:30],
            "likely": likely[:10],
            "token_found": bool(extract_token(s)),
            "length": len(s),
        }
    return {
        "ok": True,
        "kind": kind,
        "count": 1,
        "keys": [],
        "likely": [],
        "length": len(s),
    }


TOKEN_COOKIE_KEYS = ("token", "access_token", "accesstoken", "yd_token", "sso_token",
                     "auth_token", "authorization", "jwt")


def extract_token(cookie_str):
    """从整行 Cookie 里捞出真正的登录 token（影刀社区是名为 token 的字段）。"""
    for part in (cookie_str or "").split(";"):
        k, sep, v = part.strip().partition("=")
        if sep and k.strip().lower() in TOKEN_COOKIE_KEYS and v.strip():
            return v.strip()
    return ""


def build_send_headers(cfg):
    headers = dict(cfg.get("headers") or {})
    kind = (cfg.get("auth_type") or "auto").lower()
    raw = cfg.get("auth_value") or ""

    if kind == "auto":
        norm_cookie = normalize_auth(raw, "cookie")
        if "=" in norm_cookie:
            # 粘贴的是整行 Cookie：原样带上，并从中提取 token 放进 Authorization
            headers["Cookie"] = norm_cookie
            tok = extract_token(norm_cookie)
            if tok:
                headers["Authorization"] = "Bearer " + tok
        else:
            # 粘贴的是裸 token
            headers["Authorization"] = "Bearer " + normalize_auth(raw, "bearer")
    elif kind == "bearer":
        headers["Authorization"] = "Bearer " + normalize_auth(raw, "bearer")
    elif kind == "header":
        for line in normalize_auth(raw, "header").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip()] = v.strip()
    else:
        headers["Cookie"] = normalize_auth(raw, "cookie")
    return headers


def do_send(uuid, text, force_mode=None):
    cfg = load_config()["send"]
    mode = (force_mode or cfg.get("mode") or "manual").lower()
    submit_url = (cfg.get("submit_url") or "").strip()

    if mode == "http" and not submit_url:
        result = {
            "mode": "http",
            "ok": False,
            "msg": "还没填提交接口地址",
            "hint": "在浏览器抓包拿到发表回答的真实 URL，填到「设置 → 自动发送 → 提交接口」",
        }
    elif mode == "http":
        url = submit_url.replace("{{question_uuid}}", uuid)
        content = md_to_html(text) if (cfg.get("content_type") or "html") == "html" else text
        payload = {"forumQuestionUuid": uuid, "content": content}
        tpl = (cfg.get("body_template") or "").strip()
        if tpl:
            try:
                payload = json.loads(
                    tpl.replace("{{question_uuid}}", uuid).replace(
                        "{{content_json}}", json.dumps(content, ensure_ascii=False)
                    )
                )
            except Exception:
                pass
        headers = build_send_headers(cfg)
        has_auth = bool(
            normalize_auth(cfg.get("auth_value") or "", "cookie") or cfg.get("headers")
        )
        if not has_auth:
            result = {
                "mode": "http",
                "ok": False,
                "msg": "未配置登录凭证",
                "hint": "在设置里粘贴浏览器 Cookie（整行即可，程序会自动取出 token）",
            }
        else:
            try:
                resp = http_json(
                    url,
                    method=(cfg.get("method") or "POST").upper(),
                    payload=payload,
                    headers=headers,
                    retries=0,
                    timeout=30,
                )
                ok = bool(resp.get("success"))
                code = resp.get("code")
                msg = resp.get("msg") or ""
                hint = ""
                if not ok:
                    if code in (401, 403) or "未认证" in msg or "登录" in msg:
                        hint = "登录态被拒绝：token 过期了，重新复制一次整行 Cookie"
                    elif "不存在" in msg:
                        hint = "这条帖子在社区已经找不到了，可能被删除"
                    elif code == 404:
                        hint = "接口地址不对（404）：抓包拿到真实 URL 后填到设置里"
                    elif code == 400:
                        hint = "请求参数被拒绝，可能需要补字段，可在「请求体模板」里自定义"
                result = {
                    "mode": "http",
                    "ok": ok,
                    "code": code,
                    "msg": msg,
                    "hint": hint,
                    "response": resp,
                }
            except Exception as e:  # noqa: BLE001
                result = {
                    "mode": "http",
                    "ok": False,
                    "msg": str(e),
                    "hint": "网络或接口异常",
                    "response": {},
                }
    else:
        result = {"mode": "manual", "ok": True, "url": QUESTION_PAGE + uuid}

    with db() as conn:
        conn.execute(
            "INSERT INTO sends (question_uuid,final_text,mode,result,sent_at) VALUES (?,?,?,?,?)",
            (uuid, text, result.get("mode"), json.dumps(result, ensure_ascii=False), now_str()),
        )
        if result.get("ok"):
            conn.execute(
                "UPDATE questions SET status='sent', answer_count=CAST(answer_count AS INTEGER)+1 WHERE uuid=?",
                (uuid,),
            )
    log("发送 %s → %s %s" % (uuid, result.get("mode"), "成功" if result.get("ok") else "失败:" + str(result.get("msg"))))
    return result


def set_status(uuid, status):
    with db() as conn:
        conn.execute("UPDATE questions SET status=? WHERE uuid=?", (status, uuid))
    return {"ok": True}





# ---------------------------------------------------------------- HTTP 层

class Handler(BaseHTTPRequestHandler):
    server_version = "YingdaoAutoAnswer/1.0"

    def log_message(self, fmt, *args):  # 静默默认访问日志
        return

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, obj)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        try:
            if path.startswith("/api/"):
                return self._api_get(path, qs)
            return self._static(path)
        except Exception as e:  # noqa: BLE001
            log("GET %s 出错: %s" % (path, e))
            return self._json({"ok": False, "error": str(e)}, 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_json()
        try:
            if path == "/api/collect":
                result = do_collect()
                result.update(get_stats())  # 采集完立刻带最新统计，页眉数字即时刷新
                result["ok"] = True
                return self._json(result)
            if path == "/api/models":
                return self._json(
                    list_models(
                        base_url=(body.get("base_url") or "").strip() or None,
                        api_key=(body.get("api_key") or "").strip() or None,
                    )
                )
            if path == "/api/auth/inspect":
                return self._json(
                    inspect_auth(body.get("raw") or "", (body.get("kind") or "cookie").lower())
                )
            if path == "/api/draft":
                return self._json({"ok": True, **do_draft(body.get("uuid"))})
            if path == "/api/gate":
                return self._json({"ok": True, **do_gate(body.get("uuid"), body.get("text"))})
            if path == "/api/send":
                uuid = body.get("uuid")
                text = body.get("text") or ""
                if not text.strip():
                    return self._json({"ok": False, "error": "答案为空，不能发送"}, 400)
                res = do_send(uuid, text, body.get("mode"))
                return self._json({"ok": True, "result": res})
            if path == "/api/status":
                return self._json(set_status(body.get("uuid"), body.get("status") or "new"))
            if path == "/api/settings":
                body = body or {}
                # 前端不回传已保存的 API Key，空值表示"不修改"；登录凭证是明文回填的，空就是清空
                if isinstance(body.get("llm"), dict) and not (body["llm"].get("api_key") or "").strip():
                    body["llm"].pop("api_key", None)
                cfg = deep_merge(load_config(), body)
                save_config(cfg)
                return self._json({"ok": True, "config": public_config(cfg)})
            return self._json({"ok": False, "error": "未知接口"}, 404)
        except Exception as e:  # noqa: BLE001
            log("POST %s 出错: %s" % (path, e))
            return self._json({"ok": False, "error": str(e)}, 500)

    def _api_get(self, path, qs):
        if path == "/api/models":
            base = (qs.get("base_url") or [None])[0]
            return self._json(list_models(base_url=base))
        if path == "/api/probe":
            cfg = load_config()["collect"]
            try:
                items = fetch_question_page(1, 3, cfg.get("tag") or "")
                return self._json({
                    "ok": True,
                    "count": len(items),
                    "url": endpoint("list_path", "/question/query"),
                    "sample": [
                        {"title": i.get("title"), "answerCount": i.get("answerCount")}
                        for i in items[:3]
                    ],
                })
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 400)
        if path == "/api/questions":
            view = (qs.get("view") or ["pending"])[0]
            solved = (qs.get("solved") or ["all"])[0]
            sort = (qs.get("sort") or ["new"])[0]
            return self._json({"ok": True, "items": list_questions(view, solved, sort)})
        if path == "/api/question":
            uuid = (qs.get("uuid") or [""])[0]
            q = get_question(uuid)
            if not q:
                return self._json({"ok": False, "error": "问题不存在"}, 404)
            return self._json({"ok": True, "item": q})
        if path == "/api/stats":
            return self._json(get_stats())
        if path == "/api/settings":
            return self._json({"ok": True, "config": public_config(load_config())})
        return self._json({"ok": False, "error": "未知接口"}, 404)

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/").replace("..", "")
        full = os.path.join(STATIC_DIR, rel)
        if not os.path.isfile(full):
            return self._send(404, "Not Found", "text/plain; charset=utf-8")
        ext = os.path.splitext(full)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)


def public_config(cfg):
    """返回给前端。登录凭证按用户要求明文返回，方便核对。"""
    out = json.loads(json.dumps(cfg))
    key = (out["llm"].get("api_key") or "").strip()
    if key:
        out["llm"]["api_key_masked"] = key[:4] + "****" + key[-4:] if len(key) > 8 else "****"
        out["llm"]["api_key"] = ""
        out["llm"]["has_key"] = True
    else:
        out["llm"]["has_key"] = False

    auth = (out["send"].get("auth_value") or "").strip()
    out["send"]["has_auth"] = bool(auth)
    out["send"]["token_found"] = bool(extract_token(normalize_auth(auth, "cookie")))
    out["send"]["auth_value"] = auth
    url_ok = bool((out["send"].get("submit_url") or "").strip())
    out["send"]["auto_ready"] = bool(url_ok and auth)
    return out


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    init_db()
    load_config()

    port = DEFAULT_PORT
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        port = int(sys.argv[1])

    for attempt in range(5):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
            httpd = None
            if attempt == 4:
                raise
    log("影刀社区自动回答系统已启动： http://127.0.0.1:%d" % port)
    log("数据文件：%s" % DB_PATH)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("正在退出…")
        httpd.shutdown()


if __name__ == "__main__":
    main()
