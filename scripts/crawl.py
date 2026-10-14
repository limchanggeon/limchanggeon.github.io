#!/usr/bin/env python3
"""기업 공식 홈페이지에서 회사 소개 정보를 수집한다 (과제 6단계: 기업 온라인 정보 크롤링).

수집 원칙은 docs/crawling-policy.md에 정리했다. 요약하면
- 대상: DART 기업개황에 회사가 직접 등록한 공식 홈페이지만. 채용 플랫폼·뉴스·리뷰 사이트는 수집하지 않는다.
- robots.txt를 먼저 읽고, 막힌 경로는 가져오지 않는다. robots.txt를 확인할 수 없으면 그 사이트는 건너뛴다.
- 요청마다 수집 주체를 밝히는 User-Agent를 보내고, 같은 사이트에는 1초에 한 번만 요청한다.
- 첫 페이지와 회사 소개·사업·인재상 페이지까지 최대 3쪽, 페이지당 본문 1,500자만 쓴다.
- 결과는 .cache/web/에 7일 동안 저장해 같은 회사를 반복 수집하지 않는다.

단독 실행: python3 scripts/crawl.py 00126380    (DART 고유번호)
           python3 scripts/crawl.py https://www.example.com
"""
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".cache" / "web"
UA = "CGLPortfolioBot/1.0 (+https://limchanggeon.github.io; student portfolio project)"
KST = timezone(timedelta(hours=9))
TTL_DAYS = 7
MAX_PAGES = 3
MAX_BYTES = 1_500_000
PAGE_CHARS = 1500
ABOUT = re.compile(r"(about|company|intro|overview|profile|business|vision|ceo|greeting|talent|"
                   r"회사\s*소개|기업\s*소개|회사\s*개요|기업\s*개요|사업\s*(분야|영역|소개)|인사말|비전|연혁|인재상)", re.I)
SKIP = re.compile(r"(login|logout|join|member|privacy|policy|terms|sitemap|search|board|notice|news|download|"
                  r"\.(pdf|jpg|jpeg|png|gif|zip|hwp|docx?|xlsx?|pptx?)$|^mailto:|^tel:|^javascript:)", re.I)

STATUS = {
    "ok": "수집함",
    "no_homepage": "DART에 등록된 홈페이지 없음",
    "robots_blocked": "robots.txt가 수집을 막음",
    "robots_unknown": "robots.txt를 확인할 수 없어 건너뜀",
    "unreachable": "홈페이지에 접속하지 못함",
    "not_html": "HTML 페이지가 아님",
}

_host_lock = threading.Lock()
_host_last = {}


def _polite_wait(host):
    """같은 사이트에는 1초에 한 번만 요청한다."""
    with _host_lock:
        wait = 1.0 - (time.time() - _host_last.get(host, 0))
        _host_last[host] = time.time() + max(0, wait)
    if wait > 0:
        time.sleep(wait)


def _get(url, accept="text/html"):
    host = urllib.parse.urlsplit(url).netloc
    _polite_wait(host)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept, "Accept-Language": "ko,en;q=0.7"})
    with urllib.request.urlopen(req, timeout=12) as r:
        ctype = r.headers.get("Content-Type", "")
        body = r.read(MAX_BYTES)
        return r.geturl(), ctype, body


def _decode(body, ctype):
    m = re.search(r"charset=([\w-]+)", ctype or "", re.I) or re.search(rb'charset=["\']?([\w-]+)', body[:3000], re.I)
    enc = (m.group(1).decode() if isinstance(m.group(1), bytes) else m.group(1)) if m else "utf-8"
    for e in (enc, "utf-8", "cp949"):
        try:
            return body.decode(e)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "replace")


def normalize_homepage(hm_url):
    u = (hm_url or "").strip().split()[0] if (hm_url or "").strip() else ""
    if not u or "." not in u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u.lstrip("/")
    parts = urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", "", ""))


class _Robots:
    """사이트별 robots.txt 판정. 401/403이면 전부 막힌 것으로, 404면 전부 허용으로 본다 (표준 해석)."""

    def __init__(self, base):
        self.rp = urllib.robotparser.RobotFileParser()
        self.ok = True
        try:
            _, _, body = _get(base + "/robots.txt", accept="text/plain")
            self.rp.parse(_decode(body, "").splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self.rp.disallow_all = True
            elif 400 <= e.code < 500:
                self.rp.allow_all = True
            else:
                self.ok = False
        except Exception:
            self.ok = False

    def allowed(self, url):
        return self.ok and self.rp.can_fetch(UA, url)


class _Page(HTMLParser):
    HIDE = {"script", "style", "noscript", "svg", "template", "iframe", "nav", "footer", "header", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.desc, self.text, self.links = "", "", [], []
        self._hide = 0
        self._in_title = False
        self._a = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.HIDE:
            self._hide += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (a.get("name") or a.get("property") or "").lower()
            if name in ("description", "og:description") and a.get("content") and len(a["content"]) > len(self.desc):
                self.desc = a["content"].strip()
        elif tag == "a" and a.get("href"):
            self._a = [a["href"], ""]

    def handle_endtag(self, tag):
        if tag in self.HIDE and self._hide:
            self._hide -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._a:
            self.links.append(tuple(self._a))
            self._a = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._a is not None:
            self._a[1] += data
        if not self._hide:
            t = data.strip()
            if len(t) > 1:
                self.text.append(t)


def _clean(lines, seen):
    """메뉴·버튼 같은 짧은 조각과, 같은 사이트의 앞 페이지에 이미 나온 문구(공통 메뉴·푸터)를 뺀다."""
    out = []
    for t in lines:
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) < 12 or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return " ".join(out)


def _fetch_page(url, robots, seen):
    if not robots.allowed(url):
        return None, "robots_blocked"
    try:
        final, ctype, body = _get(url)
    except Exception:
        return None, "unreachable"
    if "html" not in ctype.lower():
        return None, "not_html"
    p = _Page()
    try:
        p.feed(_decode(body, ctype))
    except Exception:
        pass
    return {"url": final, "title": re.sub(r"\s+", " ", p.title).strip()[:120], "description": p.desc[:300],
            "text": _clean(p.text, seen)[:PAGE_CHARS], "links": p.links}, "ok"


def collect(corp_code, homepage_raw, force=False):
    """한 회사의 공식 홈페이지를 수집해 {status, status_text, homepage, fetched_at, pages} 를 돌려준다."""
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{corp_code}.json"
    if path.exists() and not force:
        cached = json.loads(path.read_text(encoding="utf-8"))
        age = datetime.now(KST) - datetime.fromisoformat(cached["fetched_at"])
        if age < timedelta(days=TTL_DAYS):
            return cached

    home = normalize_homepage(homepage_raw)
    result = {"homepage": home, "fetched_at": datetime.now(KST).isoformat(timespec="minutes"), "pages": []}
    if not home:
        result["status"] = "no_homepage"
    else:
        parts = urllib.parse.urlsplit(home)
        robots = _Robots(f"{parts.scheme}://{parts.netloc}")
        if not robots.ok and parts.scheme == "https":  # https가 안 열리는 옛 사이트는 http로 한 번 더
            home = urllib.parse.urlunsplit(("http", parts.netloc, parts.path, "", ""))
            robots = _Robots(f"http://{parts.netloc}")
            result["homepage"] = home
        if not robots.ok:
            result["status"] = "robots_unknown"
        else:
            seen = set()
            first, status = _fetch_page(home, robots, seen)
            result["status"] = status
            if first:
                result["pages"].append(first)
                host = urllib.parse.urlsplit(first["url"]).netloc.removeprefix("www.")
                picked = []
                for href, label in first.pop("links"):
                    url = urllib.parse.urljoin(first["url"], href.strip()).split("#")[0]
                    u = urllib.parse.urlsplit(url)
                    if u.scheme not in ("http", "https") or not u.netloc.removeprefix("www.").endswith(host):
                        continue
                    if SKIP.search(url) or url in picked or url.rstrip("/") == first["url"].rstrip("/"):
                        continue
                    if ABOUT.search(url) or ABOUT.search(label or ""):
                        picked.append(url)
                    if len(picked) >= MAX_PAGES - 1:
                        break
                for url in picked:
                    page, _ = _fetch_page(url, robots, seen)
                    if page and page["text"]:
                        page.pop("links", None)
                        result["pages"].append(page)
    for p in result["pages"]:
        p.pop("links", None)
    result["status_text"] = STATUS.get(result["status"], result["status"])
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def prompt_block(web, limit=2400):
    """Gemini 프롬프트에 넣을 수집 내용 요약. 수집하지 못했으면 그 사유만."""
    if not web or web.get("status") != "ok" or not web.get("pages"):
        return f"(홈페이지 수집 없음: {(web or {}).get('status_text', '수집 안 함')})"
    chunks, used = [], 0
    for p in web["pages"]:
        piece = f"[{p['url']}] {p['title']} — {p['description']} {p['text']}".strip()
        piece = piece[: max(0, limit - used)]
        if not piece:
            break
        chunks.append(piece)
        used += len(piece)
    return "\n".join(chunks)


def source_list(web):
    """화면 표시용: 상태와 출처 주소만 (본문은 화면에 다시 싣지 않는다)."""
    if not web:
        return None
    return {"status": web.get("status"), "status_text": web.get("status_text"), "homepage": web.get("homepage"),
            "fetched_at": web.get("fetched_at"), "sources": [{"url": p["url"], "title": p["title"]} for p in web.get("pages", [])]}


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if re.fullmatch(r"\d{8}", arg):
        info = json.loads((ROOT / ".cache" / "company" / f"{arg}.json").read_text(encoding="utf-8"))
        res = collect(arg, info.get("hm_url"), force=True)
    else:
        res = collect("manual-" + re.sub(r"\W", "_", arg)[:40], arg, force=True)
    print(json.dumps({k: v for k, v in res.items() if k != "pages"}, ensure_ascii=False, indent=1))
    for p in res["pages"]:
        print(f"- {p['url']} | {p['title']} | {len(p['text'])}자 | {p['text'][:120]}")
