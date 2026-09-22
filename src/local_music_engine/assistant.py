"""Turn a listener's plain-language feedback into one concrete engine request.

ACE-Step is steered by an English tag caption, the lyrics, optional metas and, for a
repaint, a time range and strength. The listener is not expected to know any of that,
so a plan always spells out which caption tags change and why. Two planners share the
same output contract: offline keyword rules, and an optional loopback LLM.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .local_http import open_local

STRENGTH_NAMES = ("light", "medium", "strong")
ACTIONS = ("repaint", "regenerate")
MAX_CAPTION_CHARS = 1500
MAX_LYRICS_CHARS = 4096


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    example: str
    pattern: str
    add: tuple[str, ...]
    remove: tuple[str, ...] = ()
    local: bool = False
    edge: str | None = None
    bpm_delta: int = 0
    compiled: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "compiled", re.compile(self.pattern))


# Order is the order changes are listed to the listener. Removal patterns match whole
# caption tags case-insensitively; `(?<!fe)male` keeps "female" from matching "male".
RULES: tuple[Rule, ...] = (
    Rule(
        "diction", "발음을 또렷하게", "가사 발음이 뭉개져요",
        r"발음|또렷|뭉개|웅얼|딕션|가사가?\s*(잘\s*)?안\s*들",
        ("clear Korean diction", "articulate vocals"), local=True,
    ),
    Rule(
        "vocal-forward", "보컬을 앞으로", "목소리가 반주에 묻혀요",
        r"(보컬|목소리|노래).{0,6}(작|묻|안\s*들|멀)",
        ("prominent lead vocal", "vocals upfront in the mix"),
    ),
    Rule(
        "vocal-gentle", "보컬을 부드럽게", "목소리가 너무 날카로워요",
        r"(보컬|목소리).{0,6}(날카|쨍|거칠|세다|세요|너무\s*세)",
        ("gentle vocal delivery",), (r"powerful vocal", r"belting", r"raspy"),
    ),
    Rule(
        "female-vocal", "여성 보컬로", "여자 목소리로 바꿔 주세요",
        r"여(자|성)\s*(목소리|보컬)",
        ("female vocal",), (r"(?<!fe)male vocal", r"^(?<!fe)male$"),
    ),
    Rule(
        "male-vocal", "남성 보컬로", "남자 목소리로 바꿔 주세요",
        r"남(자|성)\s*(목소리|보컬)",
        ("male vocal",), (r"female",),
    ),
    Rule(
        "calmer", "더 잔잔하게", "너무 시끄럽고 정신없어요",
        r"시끄|정신\s*없|잔잔|차분|조용하게|부담스러",
        ("calm", "sparse arrangement", "soft dynamics"),
        (r"energetic", r"powerful(?! chorus)", r"aggressive", r"intense", r"heavy", r"driving", r"dense"),
    ),
    Rule(
        "energetic", "더 신나게", "밋밋하고 지루해요",
        r"신나|밋밋|지루|심심|힘이?\s*없|처져|늘어져|에너지",
        ("energetic", "dynamic build", "catchy hook"),
        (r"^calm$", r"mellow", r"sparse", r"laid-back", r"relaxed", r"soft dynamics"),
    ),
    Rule(
        "drums-soft", "드럼을 약하게", "드럼이 너무 세요",
        r"(드럼|비트|타악).{0,6}(세|크|시끄|과|강|쿵)",
        ("light drums",), (r"drum", r"\bbeat\b", r"percussion"),
    ),
    Rule(
        "drums-strong", "드럼을 강하게", "리듬이 약해요",
        r"(드럼|비트|리듬).{0,6}(약|작|없|밋밋|부족)",
        ("punchy drums", "groovy beat"),
        (r"restrained drums", r"light drums", r"soft drums", r"no drums"),
    ),
    Rule(
        "faster", "템포를 빠르게", "너무 느려요",
        r"빠르게|느려|느리다|템포.{0,4}(올|빠르)",
        ("upbeat", "faster tempo"), (r"^slow", r"slow tempo", r"downtempo", r"ballad tempo"),
        bpm_delta=15,
    ),
    Rule(
        "slower", "템포를 느리게", "너무 빨라요",
        r"느리게|빨라|급해|템포.{0,4}(내|느리)",
        ("slower tempo", "relaxed groove"), (r"upbeat", r"uptempo", r"faster tempo", r"^fast"),
        bpm_delta=-15,
    ),
    Rule(
        "brighter", "더 밝게", "분위기가 너무 어두워요",
        r"밝게|어두|우울|칙칙|무거워",
        ("bright", "uplifting", "hopeful"), (r"dark", r"melanchol", r"sad", r"gloomy", r"moody"),
    ),
    Rule(
        "emotional", "더 애절하게", "감정이 잘 안 느껴져요",
        r"애절|슬프게|감성|감정|울림",
        ("emotional", "melancholic", "heartfelt vocal"),
        (r"cheerful", r"happy", r"^bright$", r"uplifting"),
    ),
    Rule(
        "clean", "소리를 깨끗하게", "소리가 지직거리고 탁해요",
        r"잡음|노이즈|지직|찢어|깨져|탁해|먹먹|음질",
        ("clean mix", "polished production", "high fidelity"), (r"lo-fi", r"distorted", r"gritty"),
        local=True,
    ),
    Rule(
        "ending", "끝을 자연스럽게", "끝이 갑자기 끊겨요",
        r"(끝|마지막|엔딩|마무리).{0,8}(끊|갑자기|어색|급|이상)|자연스럽게\s*끝",
        ("natural ending",), local=True, edge="end",
    ),
    Rule(
        "intro", "시작을 자연스럽게", "시작이 어색해요",
        r"(시작|도입|인트로|처음).{0,8}(어색|갑자기|이상|급)",
        ("smooth intro",), local=True, edge="start",
    ),
    Rule(
        "transition", "이음새를 매끄럽게", "중간에 튀는 부분이 있어요",
        r"이음새|연결|튀|중간에.{0,6}끊|갑자기\s*바뀌|어색하게\s*바뀌",
        ("seamless transitions", "consistent tempo and timbre"), local=True,
    ),
    Rule(
        "variety", "덜 반복적으로", "멜로디가 계속 똑같아요",
        r"반복|똑같|단조|변화가?\s*없",
        ("melodic variation",),
    ),
    Rule(
        "chorus", "후렴을 강하게", "후렴이 약해요",
        r"(후렴|코러스|하이라이트).{0,8}(약|밋밋|임팩트|평범|안\s*터)",
        ("powerful chorus", "memorable hook"), local=True,
    ),
    Rule(
        "fewer-instruments", "악기를 줄여서", "악기가 너무 많아요",
        r"악기.{0,6}(많|줄|빼)",
        ("minimal instrumentation",), (r"dense", r"full band", r"orchestral"),
    ),
    Rule(
        "piano", "피아노 더하기", "피아노 소리를 넣어 주세요",
        r"피아노.{0,8}(넣|추가|더|있으면|살려)",
        ("piano",),
    ),
    Rule(
        "guitar", "기타 더하기", "기타 소리를 넣어 주세요",
        r"기타.{0,8}(넣|추가|더|있으면|살려)",
        ("acoustic guitar",),
    ),
    Rule(
        "strings", "현악기 더하기", "현악기를 넣어 주세요",
        r"(스트링|현악|바이올린|첼로).{0,8}(넣|추가|더|있으면|살려)",
        ("strings",),
    ),
)


def catalog() -> list[dict[str, Any]]:
    """What the rule planner understands, for UI suggestion chips."""

    return [
        {"key": rule.key, "label": rule.label, "example": rule.example, "local": rule.local}
        for rule in RULES
    ]


def split_tags(caption: str) -> list[str]:
    return [tag.strip() for tag in caption.split(",") if tag.strip()]


def diff_tags(before: str, after: str) -> list[dict[str, str]]:
    old = split_tags(before)
    new = split_tags(after)
    old_keys = {tag.lower() for tag in old}
    new_keys = {tag.lower() for tag in new}
    changes = [{"op": "remove", "term": tag} for tag in old if tag.lower() not in new_keys]
    changes += [{"op": "add", "term": tag} for tag in new if tag.lower() not in old_keys]
    return changes


def is_prose(caption: str) -> bool:
    """ACE's own drafts are sentences; comma pieces of a sentence are not tags."""

    text = caption.strip()
    return (
        text.endswith(".")
        or ". " in text
        or bool(re.match(r"(a|an|the)\s", text, re.IGNORECASE))
        or any(len(piece.split()) > 6 for piece in split_tags(text))
    )


def _apply_rules(caption: str, rules: list[Rule]) -> tuple[str, list[dict[str, str]]]:
    if is_prose(caption):
        # Only append: removing a comma piece of a sentence would cut it mid-clause.
        additions: list[str] = []
        changes: list[dict[str, str]] = []
        lowered = caption.lower()
        for rule in rules:
            for term in rule.add:
                if term.lower() not in lowered and term not in additions:
                    additions.append(term)
                    changes.append({"op": "add", "term": term, "label": rule.label})
        if not additions:
            return caption, changes
        return f"{caption.rstrip().rstrip('.')}. {', '.join(additions)}", changes
    tags = split_tags(caption)
    changes = []
    for rule in rules:
        additions = {term.lower() for term in rule.add}
        kept = []
        for tag in tags:
            removable = tag.lower() not in additions and any(
                re.search(pattern, tag, re.IGNORECASE) for pattern in rule.remove
            )
            if removable:
                changes.append({"op": "remove", "term": tag, "label": rule.label})
            else:
                kept.append(tag)
        tags = kept
        present = {tag.lower() for tag in tags}
        for term in rule.add:
            if term.lower() not in present:
                tags.append(term)
                present.add(term.lower())
                changes.append({"op": "add", "term": term, "label": rule.label})
    return ", ".join(tags), changes


def _clean_range(value: Any, duration: float) -> dict[str, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        start = max(0.0, float(value["startSeconds"]))
        end = min(duration, float(value["endSeconds"]))
    except (KeyError, TypeError, ValueError):
        return None
    if end - start < 0.5:
        return None
    return {"startSeconds": round(start, 2), "endSeconds": round(end, 2)}


def _edge_range(edge: str, duration: float) -> dict[str, float]:
    span = round(min(10.0, max(3.0, duration * 0.25)), 2)
    if edge == "start":
        return {"startSeconds": 0.0, "endSeconds": min(duration, span)}
    return {"startSeconds": round(max(0.0, duration - span), 2), "endSeconds": round(duration, 2)}


def _time_label(seconds: float) -> str:
    whole = int(round(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def _summary(action: str, edit_range: dict[str, float] | None, labels: list[str], versions: int) -> str:
    """What was understood; the plan's action/range/versions say what will run."""

    if labels:
        return f"알아들은 요청: {', '.join(labels)}"
    if action == "repaint" and edit_range:
        span = f"{_time_label(edit_range['startSeconds'])}–{_time_label(edit_range['endSeconds'])}"
        return f"알아들은 변화가 없어요. {span} 구간을 같은 스타일로 다시 불러 볼 수 있어요."
    return f"알아들은 변화가 없어요. 같은 스타일로 새 버전 {versions}개를 만들 수 있어요."


@dataclass(frozen=True)
class PlanRequest:
    feedback: str
    caption: str
    lyrics: str
    duration_seconds: float
    edit_range: dict[str, float] | None = None
    strength: str = "medium"
    versions: int = 2
    bpm: int | None = None
    key_scale: str | None = None

    def validated(self) -> "PlanRequest":
        if self.strength not in STRENGTH_NAMES:
            raise ValueError(f"strength must be one of: {', '.join(STRENGTH_NAMES)}")
        if not 1 <= self.versions <= 8:
            raise ValueError("versions must be between 1 and 8")
        if self.duration_seconds <= 0:
            raise ValueError("duration must be positive")
        return self


def rule_plan(request: PlanRequest, *, note: str | None = None) -> dict[str, Any]:
    request = request.validated()
    text = request.feedback.strip()
    matched = [rule for rule in RULES if rule.compiled.search(text)] if text else []
    caption, changes = _apply_rules(request.caption, matched)
    edit_range = _clean_range(request.edit_range, request.duration_seconds)
    notes: list[str] = [note] if note else []
    if matched and is_prose(request.caption) and any(rule.remove for rule in matched):
        notes.append("스타일이 문장으로 되어 있어 새 태그만 덧붙였어요. 맞지 않는 표현은 아래에서 직접 지워 주세요.")
    if edit_range is None:
        edges = {rule.edge for rule in matched if rule.edge}
        if len(edges) == 1 and all(rule.local for rule in matched):
            edit_range = _edge_range(edges.pop(), request.duration_seconds)
            notes.append("말씀하신 위치에 맞춰 구간을 골랐어요. 파형에서 직접 바꿀 수 있어요.")
    action = "repaint" if edit_range else "regenerate"
    if action == "repaint" and any(not rule.local for rule in matched):
        notes.append("분위기·템포처럼 곡 전체에 걸친 변화는 구간 수정보다 새 버전이 잘 맞을 수 있어요.")
    bpm = None
    delta = sum(rule.bpm_delta for rule in matched)
    if action == "regenerate" and delta and request.bpm:
        bpm = max(40, min(220, request.bpm + delta))
    if not matched:
        notes.append(
            "이 문장에서 알아들은 변화가 없어요. 아래 스타일 문장을 직접 고치거나, "
            "설정에서 로컬 LLM 도우미를 켜면 자유로운 문장도 해석해요."
        )
    labels = list(dict.fromkeys(rule.label for rule in matched))
    return {
        "action": action,
        "summary": _summary(action, edit_range, labels, request.versions),
        "stylePrompt": caption,
        "baseStylePrompt": request.caption,
        "lyrics": None,
        "bpm": bpm,
        "range": edit_range,
        "strength": request.strength,
        "versions": request.versions,
        "changes": changes,
        "assistant": "rules",
        "assistantModel": None,
        "understood": bool(matched),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# Loopback LLM planner
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the revision planner inside a local music-generation app built on ACE-Step 1.5.
The listener is not a musician. They describe what they heard in plain Korean. Turn that into ONE
concrete change the engine can run.

The engine is steered only by:
- caption: English, comma-separated style tags (genre, mood, instruments, vocal type, production,
  tempo feel). ACE-Step was trained on captions such as
  "Korean indie pop, warm female vocal, gentle electric piano, clean guitar, restrained drums, intimate verse, uplifting chorus".
- lyrics: Korean lyrics with section tags such as [Verse], [Chorus], [Bridge]. "[Instrumental]" means no vocals.
- bpm: optional integer.
- For a repaint only: a time range in seconds and a strength (light | medium | strong).

Actions:
- "repaint": re-create only a time range of the current version and keep the rest. Use for problems
  confined to a passage (a mumbled line, a rough transition, an abrupt ending).
- "regenerate": make new full versions with the revised caption/lyrics. Use for song-wide problems
  (mood, tempo, genre, vocal gender, overall mix).
If the listener selected a range, prefer "repaint" unless the request is clearly song-wide.

Rules:
- Make the smallest caption change that answers the feedback. Copy every tag the listener did not
  complain about verbatim, in its original order; only remove a tag that contradicts the request.
- Keep the caption in English, comma-separated, under 60 words. Describe the desired sound; never
  write words like "fix" or "improve" in the caption.
- Set lyrics to null unless the listener asked for different words. If you change lyrics, return the
  full lyrics with section tags.
- Set bpm to null unless tempo should change and a current bpm is known.
- summary: one short, plain Korean sentence telling the listener what will change.
- labels: for each caption tag you added, {"en": the exact tag, "ko": a 2-6 word Korean label}.
Return JSON only."""

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "summary": {"type": "string"},
        "caption": {"type": "string"},
        "lyrics": {"type": ["string", "null"]},
        "bpm": {"type": ["integer", "null"]},
        "range": {
            "type": ["object", "null"],
            "properties": {
                "startSeconds": {"type": "number"},
                "endSeconds": {"type": "number"},
            },
        },
        "strength": {"type": "string", "enum": list(STRENGTH_NAMES)},
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"en": {"type": "string"}, "ko": {"type": "string"}},
            },
        },
    },
    "required": ["action", "summary", "caption", "strength"],
}


class AssistantError(RuntimeError):
    pass


@dataclass(frozen=True)
class LlmConfig:
    base_url: str
    model: str
    provider: str = "ollama"  # "ollama" native API or "openai" compatible
    timeout_seconds: float = 180.0

    def validated(self) -> "LlmConfig":
        if self.provider not in {"ollama", "openai"}:
            raise ValueError("provider must be ollama or openai")
        if not self.model.strip():
            raise ValueError("choose an LLM model first")
        return LlmConfig(
            _require_loopback(self.base_url), self.model.strip(), self.provider, self.timeout_seconds
        )


def _require_loopback(base_url: str) -> str:
    parsed = urllib.parse.urlparse(base_url.strip())
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("assistant LLM must use an http loopback address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("assistant URL must not include credentials, query, or fragment")
    return base_url.strip().rstrip("/")


def _post_json(url: str, payload: dict[str, Any], timeout: float) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    return _read_json(request, timeout)


def _read_json(request: urllib.request.Request, timeout: float) -> Any:
    try:
        with open_local(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise AssistantError(f"LLM HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise AssistantError(f"LLM unavailable: {error.reason}") from error
    except (TimeoutError, json.JSONDecodeError) as error:
        raise AssistantError(f"LLM response failed: {error}") from error


def _openai_root(base_url: str) -> str:
    return base_url if base_url.endswith("/v1") else f"{base_url}/v1"


def list_models(config: LlmConfig) -> list[str]:
    base = _require_loopback(config.base_url)
    if config.provider == "ollama":
        data = _read_json(urllib.request.Request(f"{base}/api/tags"), 10.0)
        return [str(item["name"]) for item in data.get("models", []) if item.get("name")]
    data = _read_json(urllib.request.Request(f"{_openai_root(base)}/models"), 10.0)
    return [str(item["id"]) for item in data.get("data", []) if item.get("id")]


def _chat(config: LlmConfig, messages: list[dict[str, str]], schema: dict[str, Any] | None = None) -> str:
    if config.provider == "ollama":
        data = _post_json(
            f"{config.base_url}/api/chat",
            {
                "model": config.model,
                "messages": messages,
                "stream": False,
                "format": schema or PLAN_SCHEMA,
                "think": False,
                # The music engine shares unified memory; release the LLM right after planning.
                "keep_alive": 0,
                "options": {"temperature": 0.3},
            },
            config.timeout_seconds,
        )
        content = (data.get("message") or {}).get("content")
    else:
        data = _post_json(
            f"{_openai_root(config.base_url)}/chat/completions",
            {
                "model": config.model,
                "messages": messages,
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
            },
            config.timeout_seconds,
        )
        choices = data.get("choices") or [{}]
        content = (choices[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise AssistantError("LLM returned an empty answer")
    return content


def plan_from_llm_answer(raw: str, request: PlanRequest, *, model: str) -> dict[str, Any]:
    """Validate an LLM answer against what the engine can actually execute."""

    try:
        answer = json.loads(raw)
    except json.JSONDecodeError as error:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise AssistantError("LLM answer is not JSON") from error
        answer = json.loads(match.group(0))
    if not isinstance(answer, dict):
        raise AssistantError("LLM answer is not a JSON object")
    action = answer.get("action")
    if action not in ACTIONS:
        raise AssistantError(f"LLM chose an unknown action: {action!r}")
    caption = " ".join(str(answer.get("caption") or "").split())
    if not caption or len(caption) > MAX_CAPTION_CHARS:
        raise AssistantError("LLM caption is empty or too long")
    strength = answer.get("strength") if answer.get("strength") in STRENGTH_NAMES else request.strength
    edit_range = _clean_range(request.edit_range, request.duration_seconds) or _clean_range(
        answer.get("range"), request.duration_seconds
    )
    notes: list[str] = []
    if action == "repaint" and edit_range is None:
        action = "regenerate"
        notes.append("수정할 구간이 정해지지 않아 새 버전을 만드는 안으로 바꿨어요.")
    lyrics = answer.get("lyrics")
    if lyrics is not None:
        lyrics = str(lyrics).strip()
        if not lyrics or lyrics == request.lyrics.strip():
            lyrics = None
        elif len(lyrics) > MAX_LYRICS_CHARS:
            raise AssistantError("LLM lyrics are too long")
    bpm = answer.get("bpm")
    bpm = int(bpm) if isinstance(bpm, (int, float)) and 40 <= bpm <= 220 else None
    if action == "repaint":
        bpm = None
    labels = {
        str(item.get("en", "")).strip().lower(): str(item.get("ko", "")).strip()
        for item in answer.get("labels") or []
        if isinstance(item, dict)
    }
    changes = diff_tags(request.caption, caption)
    for change in changes:
        label = labels.get(change["term"].lower())
        if label:
            change["label"] = label
    summary = " ".join(str(answer.get("summary") or "").split())[:300]
    return {
        "action": action,
        "summary": summary or _summary(action, edit_range, [], request.versions),
        "stylePrompt": caption,
        "baseStylePrompt": request.caption,
        "lyrics": lyrics,
        "bpm": bpm,
        "range": edit_range if action == "repaint" else None,
        "strength": strength,
        "versions": request.versions,
        "changes": changes,
        "assistant": "llm",
        "assistantModel": model,
        "understood": bool(changes or lyrics or bpm),
        "notes": notes,
    }


def llm_plan(request: PlanRequest, config: LlmConfig) -> dict[str, Any]:
    request = request.validated()
    config = config.validated()
    context = {
        "feedback": request.feedback,
        "selectedRange": _clean_range(request.edit_range, request.duration_seconds),
        "durationSeconds": round(request.duration_seconds, 2),
        "caption": request.caption,
        "lyrics": request.lyrics,
        "bpm": request.bpm,
        "keyScale": request.key_scale,
        "requestedStrength": request.strength,
    }
    raw = _chat(
        config,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ],
    )
    return plan_from_llm_answer(raw, request, model=config.model)


def plan_revision(request: PlanRequest, llm: LlmConfig | None = None) -> dict[str, Any]:
    """Prefer the LLM when configured; fall back to rules and say so instead of failing."""

    if llm is None:
        return rule_plan(request)
    try:
        return llm_plan(request, llm)
    except (AssistantError, ValueError) as error:
        return rule_plan(request, note=f"LLM 도우미를 쓰지 못해 기본 규칙으로 해석했어요 ({error}).")
