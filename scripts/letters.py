"""자기소개서 생성: 이력 파일 + 회사 분석을 근거로 Gemini가 문항별 답변을 쓴다.

server.py가 불러 쓴다. 쓴 자소서는 private/letters.json에 저장되고, private/는 .gitignore로 GitHub에 올라가지 않는다.
"""
import json
import threading
import uuid
from datetime import datetime

STORE_NAME = "letters.json"
TONES = {
    "담백": "과장 없이 담백하고 구체적으로. 형용사보다 사실과 결과로 말한다.",
    "논리": "주장 → 근거(경험) → 배운 점 → 회사에서의 적용 순서가 분명하게 드러나도록 논리적으로.",
    "열정": "지원 의지가 느껴지되 과장되지 않게. 문장은 짧고 힘 있게.",
}
_lock = threading.Lock()


def store_path(root):
    d = root / "private"
    d.mkdir(exist_ok=True)
    return d / STORE_NAME


def load_all(root):
    p = store_path(root)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def save_all(root, rows):
    with _lock:
        store_path(root).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def company_context(c):
    if not c:
        return "지원 회사가 정해지지 않았다. 특정 회사 이름을 지어내지 말고 직무 중심으로 써라."
    lines = [
        f"회사명: {c['name']} ({c.get('market', '')}, {c.get('sector', '')}, 본사 {c.get('region') or '미상'})",
        f"사업: {c.get('business', '')}",
    ]
    if c.get("employees"):
        lines.append(f"직원 수: {c['employees']}명")
    if c.get("reasons"):
        lines.append("이력과 맞닿는 지점(사전 분석): " + " / ".join(c["reasons"]))
    if c.get("caution"):
        lines.append("주의점(사전 분석): " + c["caution"])
    return "\n".join(lines)


SCHEMA = {"type": "OBJECT", "properties": {"answers": {"type": "ARRAY", "items": {
    "type": "OBJECT", "properties": {
        "title": {"type": "STRING"},
        "text": {"type": "STRING"},
        "used": {"type": "ARRAY", "items": {"type": "STRING"}},
    }, "required": ["title", "text", "used"]}}}, "required": ["answers"]}


def build_prompt(profile, company, role, questions, focus, tone, notes, drafts=None, instruction=""):
    qs = "\n".join(f"{i + 1}. {q['q']} (공백 포함 {q['limit']}자 이내, {int(q['limit'] * 0.9)}자 이상)" for i, q in enumerate(questions))
    draft_block = ""
    if drafts:
        draft_block = "\n[현재 초안 — 이것을 고쳐 써라]\n" + "\n\n".join(f"{i + 1}번 초안:\n{d}" for i, d in enumerate(drafts))
    return f"""너는 한국 이공계 신입 채용 자기소개서를 잘 쓰는 코치다. [이력]의 주인공이 직접 쓴 것처럼 자기소개서 답변을 작성하라.

반드시 지킬 것
- 사실은 [이력]에 있는 것만 쓴다. 이력에 없는 경험, 수치(정확도·FPS·사용자 수 등), 역할, 기간을 지어내지 않는다. 필요하면 "~을 비교·분석했다"처럼 이력에 적힌 수준으로만 말한다.
- 회사 정보는 [회사]에 있는 내용과 널리 알려진 사실만 쓴다. 모르는 제품명이나 조직명을 만들어내지 않는다.
- 각 문항은 글자 수 범위(공백 포함)를 지킨다. 범위를 넘기지 마라.
- 1인칭 "저는", 합니다체. 소제목(title)은 [ ] 없이 25자 안팎의 한 줄로, 답변 본문에는 넣지 않는다.
- 문항마다 가장 잘 맞는 경험을 골라 쓰고, 같은 경험을 여러 문항에서 반복하지 않도록 나눠 쓴다.
- 상투어("어릴 때부터", "열정", "최고의 인재가 되겠습니다")를 피하고, 구체적인 행동과 판단을 쓴다.
- 문체: {TONES.get(tone, TONES['담백'])}
- used에는 그 답변에 쓴 이력 항목의 짧은 이름을 적는다 (예: "JDCS 논문", "YogiBot ControlTower").
{f"- 특히 강조할 경험: {', '.join(focus)}" if focus else ""}
{f"- 지원자 메모: {notes}" if notes else ""}
{f"- 수정 요청: {instruction}" if instruction else ""}

[지원 직무]
{role or "지정하지 않음 (이력에 맞는 AI·로봇 엔지니어 직무로 가정)"}

[회사]
{company_context(company)}

[이력]
{profile}

[문항]
{qs}
{draft_block}
문항 순서대로 answers 배열을 채워라."""


def fit_length(gemini, prompt_base, answer, limit):
    """글자 수가 범위를 벗어나면 한 번 고쳐 쓰게 한다."""
    n = len(answer["text"])
    lo = int(limit * 0.85)
    if lo <= n <= limit:
        return answer
    direction = f"{n}자라서 너무 깁니다. {limit}자 이내로 줄이세요" if n > limit else f"{n}자라서 너무 짧습니다. {lo}~{limit}자로 구체적인 내용을 보태세요 (이력에 있는 사실만)"
    out, _ = gemini(prompt_base + f"\n\n[글자 수 조정] 아래 답변이 {direction}. 소제목과 핵심은 유지하라.\n{answer['text']}", SCHEMA, temperature=0.4)
    fixed = out["answers"][0] if out.get("answers") else answer
    if len(fixed["text"]) > limit:  # 그래도 넘치면 문장 단위로 자른다
        cut = fixed["text"][:limit]
        fixed["text"] = cut[:cut.rfind(".") + 1] or cut
    return fixed


def generate(gemini, profile, company, role, questions, focus, tone, notes, drafts=None, instruction=""):
    prompt = build_prompt(profile, company, role, questions, focus, tone, notes, drafts, instruction)
    out, model = gemini(prompt, SCHEMA, temperature=0.6)
    answers = out.get("answers", [])[:len(questions)]
    result = []
    for q, a in zip(questions, answers):
        single = build_prompt(profile, company, role, [q], focus, tone, notes)
        a = fit_length(gemini, single, a, q["limit"])
        result.append({"q": q["q"], "limit": q["limit"], "title": a["title"].strip(), "text": a["text"].strip(), "used": a.get("used", [])})
    return result, model


def new_record(company, role, tone, focus, answers, model):
    return {"id": uuid.uuid4().hex[:10], "created_at": datetime.now().isoformat(timespec="minutes"),
            "company": {k: company.get(k) for k in ("corp_code", "name", "score")} if company else None,
            "role": role, "tone": tone, "focus": focus, "model": model, "answers": answers}
