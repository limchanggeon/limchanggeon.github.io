#!/usr/bin/env python3
"""사이트 파일을 보여 주고, 관심 기업 검색·적합도 분석·자소서 생성 API를 제공하는 서버.

로컬:   python3 scripts/server.py            → http://localhost:8982 (127.0.0.1에만 열림)
Render: render.yaml이 HOST=0.0.0.0, PORT를 넘겨 준다.

환경변수 (로컬은 .env, Render는 대시보드의 Environment)
  DART_API_KEY, GOOGLE_API_KEY   필수
  PROFILE_PATH                   이력 마크다운 경로 (Render는 Secret File: /etc/secrets/profile.md)
  APP_PASSWORD                   있으면 /api/* 전체를 비밀번호로 잠근다 (공개 배포 시 필수)
  HOST, PORT                     기본 127.0.0.1:8982

관심 기업·자소서는 브라우저(localStorage)에 저장한다. 서버 파일은 캐시일 뿐이라
Render 무료 플랜처럼 재시작 때 디스크가 지워져도 사용자 데이터는 남는다.

API
  POST /api/login {password}           비밀번호 확인 후 세션 쿠키 발급
  GET  /api/session                    {auth_required, ok}
  GET  /api/search?q=레인보우          DART 전체(비상장 포함)에서 회사 검색
  GET  /api/fit?corp_code=00123456     적합도 상세 분석 (&refresh=1이면 다시 분석)
  GET  /api/interests                  서버에 남아 있는 분석 결과 (브라우저 목록이 비었을 때 가져오기용)
  POST /api/letter                     자소서 생성 {corp_code?, role, questions:[{q, limit}], focus, tone, notes}
  POST /api/letter/rewrite             한 문항 다시 쓰기 {corp_code?, role, tone, focus, q, limit, text, instruction}
  GET  /api/letters                    예전 버전이 서버에 저장한 자소서 (브라우저로 옮기기용)
"""
import hashlib
import hmac
import importlib.util
import json
import os
import sys
import threading
import traceback
import urllib.parse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mc", HERE / "match_companies.py")
mc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mc)
sys.path.insert(0, str(HERE))
import letters  # noqa: E402

BLOCKED = (".env", ".cache", ".git", "scripts", "private", "render.yaml", ".gitignore", ".env.example", "interests.json")
OPEN_API = ("/api/login", "/api/session")
_lock = threading.Lock()
_state = {}


def load(name):
    p = mc.DATA / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def index_map():
    """전체 상장사 적합도(있으면)를 corp_code → 행으로."""
    rows = load("fit_index.json").get("rows", [])
    return {r["c"]: r for r in rows}, sorted(r["p"] for r in rows)


def percentile(score, sorted_ps):
    if not sorted_ps:
        return None
    return round(100 * sum(1 for p in sorted_ps if p < score) / len(sorted_ps))


def cache_interest(rec, model):
    with _lock:
        cur = load("interests.json")
        rows = [c for c in cur.get("companies", []) if c["corp_code"] != rec["corp_code"]] + [rec]
        rows.sort(key=lambda x: -x["score"])
        mc.write_json("interests.json", mc.meta(model, companies=rows))


def analyze(corp_code, refresh=False):
    if not refresh:
        for name in ("interests.json", "companies.json"):
            for c in load(name).get("companies", []):
                if c["corp_code"] == corp_code and c.get("sub"):
                    return c
    row = next((r for r in mc.all_corp_codes() if r["corp_code"] == corp_code), None)
    if not row:
        raise LookupError("DART에 없는 고유번호입니다")
    rec = mc.company_record(corp_code, row["corp_name"], row["stock_code"])
    if not rec:
        raise LookupError("DART 기업개황을 가져오지 못했습니다")
    recs, model = mc.detailed_analysis(_state["profile"], [rec], {corp_code: mc.financials(corp_code)})
    if not recs:
        raise RuntimeError("Gemini가 결과를 돌려주지 않았습니다. 다시 시도해 주세요")
    rec = {**recs[0], "analyzed_at": mc.datetime.now(mc.KST).isoformat(timespec="minutes"), "model": model}
    cache_interest(rec, model)
    return rec


def session_token():
    pw = os.environ.get("APP_PASSWORD", "")
    return hmac.new(pw.encode(), b"cgl-session-v1", hashlib.sha256).hexdigest() if pw else ""


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if "/api/" in str(args[0] if args else ""):
            sys.stderr.write("[api] " + (fmt % args) + "\n")

    def send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(min(n, 200_000)) or b"{}") if n else {}

    def authed(self):
        token = session_token()
        if not token:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cgl_auth" and hmac.compare_digest(v, token):
                return True
        return False

    def route(self, method):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path == "/api/session":
                return self.send_json({"auth_required": bool(session_token()), "ok": self.authed()})
            if method == "POST" and url.path == "/api/login":
                token = session_token()
                if not token:
                    return self.send_json({"ok": True})
                given = str(self.body().get("password", ""))
                if not hmac.compare_digest(given.encode(), os.environ["APP_PASSWORD"].encode()):
                    return self.send_json({"error": "비밀번호가 맞지 않아요"}, 401)
                secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
                cookie = f"cgl_auth={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000{secure}"
                return self.send_json({"ok": True}, headers={"Set-Cookie": cookie})
            if url.path not in OPEN_API and not self.authed():
                return self.send_json({"error": "비밀번호가 필요해요", "auth": True}, 401)

            if url.path.startswith("/api/letter"):
                return self.route_letters(method, url.path)

            if method == "GET" and url.path == "/api/search":
                term = q.get("q", "").strip()
                if not term:
                    return self.send_json({"results": []})
                idx, _ = index_map()
                hits = [r for r in mc.all_corp_codes() if term.lower() in r["corp_name"].lower()]
                hits.sort(key=lambda r: (not r["stock_code"], r["corp_name"] != term, len(r["corp_name"]), r["corp_name"]))
                out = [{"corp_code": r["corp_code"], "name": r["corp_name"], "stock_code": r["stock_code"],
                        "listed": bool(r["stock_code"]), "p": idx.get(r["corp_code"], {}).get("p")} for r in hits[:25]]
                return self.send_json({"results": out, "total": len(hits)})

            if method == "GET" and url.path == "/api/fit":
                code = q.get("corp_code", "")
                if not (code.isdigit() and len(code) == 8):
                    return self.send_json({"error": "corp_code는 8자리 숫자여야 합니다"}, 400)
                rec = analyze(code, refresh=q.get("refresh") == "1")
                _, ps = index_map()
                return self.send_json({"company": rec, "percentile": percentile(rec["score"], ps) if rec.get("stock_code") else None,
                                       "index_size": len(ps)})

            if method == "GET" and url.path == "/api/interests":
                return self.send_json({"companies": load("interests.json").get("companies", [])})

            return self.send_json({"error": "없는 API입니다"}, 404)
        except LookupError as e:
            return self.send_json({"error": str(e)}, 404)
        except Exception as e:
            traceback.print_exc()
            return self.send_json({"error": f"처리 중 오류가 났습니다: {type(e).__name__}"}, 500)

    def route_letters(self, method, path):
        if method == "GET" and path == "/api/letters":
            return self.send_json({"letters": letters.load_all(mc.ROOT) if (mc.ROOT / "private").exists() else []})

        b = self.body() if method == "POST" else {}
        role = str(b.get("role", "")).strip()[:100]
        tone = b.get("tone", "담백") if b.get("tone") in letters.TONES else "담백"
        focus = [str(f)[:80] for f in b.get("focus", [])][:8]
        code = str(b.get("corp_code") or "")
        company = analyze(code) if code.isdigit() and len(code) == 8 else None

        if method == "POST" and path == "/api/letter":
            questions = [{"q": str(x.get("q", "")).strip()[:500], "limit": max(100, min(3000, int(x.get("limit") or 700)))}
                         for x in b.get("questions", []) if str(x.get("q", "")).strip()][:8]
            if not questions:
                return self.send_json({"error": "문항을 하나 이상 입력해 주세요"}, 400)
            answers, model = letters.generate(mc.gemini, _state["profile"], company, role, questions, focus, tone,
                                              str(b.get("notes", "")).strip()[:500])
            return self.send_json({"letter": letters.new_record(company, role, tone, focus, answers, model)})

        if method == "POST" and path == "/api/letter/rewrite":
            q = {"q": str(b.get("q", "")).strip()[:500], "limit": max(100, min(3000, int(b.get("limit") or 700)))}
            if not q["q"]:
                return self.send_json({"error": "다시 쓸 문항이 비어 있어요"}, 400)
            new, model = letters.generate(mc.gemini, _state["profile"], company, role, [q], focus, tone, "",
                                          drafts=[str(b.get("text", ""))[:6000]],
                                          instruction=str(b.get("instruction", "")).strip()[:300] or "더 구체적이고 자연스럽게 다듬어라")
            if not new:
                return self.send_json({"error": "Gemini가 결과를 돌려주지 않았습니다"}, 500)
            return self.send_json({"answer": new[0], "model": model})

        return self.send_json({"error": "없는 API입니다"}, 404)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            return self.route("GET")
        if any(part in BLOCKED for part in Path(path).parts):
            return self.send_error(404)
        return super().do_GET()

    def do_POST(self):
        return self.route("POST")


def main():
    mc.load_env()
    for sub in ("company", "fin"):
        (mc.CACHE / sub).mkdir(parents=True, exist_ok=True)
    profile_path = (mc.ROOT / os.environ.get("PROFILE_PATH", "")).resolve()
    if not profile_path.is_file():
        sys.exit(f"이력 파일을 찾지 못했습니다: {profile_path} (PROFILE_PATH 확인)")
    _state["profile"] = profile_path.read_text(encoding="utf-8")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8982"))
    if host != "127.0.0.1" and not session_token():
        print("경고: APP_PASSWORD 없이 외부에 열려 있습니다. 누구나 API 키를 쓸 수 있어요.", flush=True)
    mc.all_corp_codes()  # 검색용 목록을 미리 받아 둔다
    server = ThreadingHTTPServer((host, port), partial(Handler, directory=str(mc.ROOT)))
    print(f"http://{'localhost' if host == '127.0.0.1' else host}:{port} 에서 실행 중", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
