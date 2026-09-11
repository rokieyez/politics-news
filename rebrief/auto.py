"""구독으로 자동 답하기 — 0원 방식의 '붙여넣기' 를 러너가 대신한다 (2026-09-11 사용자 결정).

아침 데일리가 남긴 붙여넣기 묶음(`prompt-pack.md`)을 사람이 claude.ai 에 붙여 넣던 자리에서,
러너 안의 Claude Code 가 묶음을 받아 답(JSON 세 덩이)을 쓴다. 답은 사람이 「답」 이슈에 넣던 것과
똑같이 `pipeline.answer` 를 지나므로 새로 생긴 산출 단계는 없다.

인증은 저장소 Secret `CLAUDE_CODE_OAUTH_TOKEN`(맥에서 `claude setup-token`, 1년짜리)이다. 공식 문서가
"CI pipelines and scripts" 용이라 적었고, 이걸로 돌리면 API 요금이 아니라 **구독 사용량**으로 처리된다
(code.claude.com/docs/en/authentication). 한도·만료로 멈추면 예전처럼 사람이 붙여 넣으면 된다 — 묶음은
그대로 남는다.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile

from .config import Config

log = logging.getLogger(__name__)

TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
PACK_NAME = "prompt-pack.md"
SYSTEM = ("당신은 한국 정치 뉴스 콘텐츠를 만드는 작가입니다. 도구는 쓰지 않습니다. "
          "사용자 메시지의 규칙과 출력 형식을 그대로 따르고, 요청한 JSON 덩이만 답합니다.")


def _conf(cfg: Config) -> dict:
    return cfg.get("auto", {}) or {}


def ready(cfg: Config) -> str:
    """자동으로 답할 수 있으면 빈 문자열, 아니면 못 하는 이유."""
    if not _conf(cfg).get("enabled", False):
        return "꺼져 있음"
    if not os.environ.get(TOKEN_ENV):
        return f"구독 토큰({TOKEN_ENV})이 없음"
    if not shutil.which(str(_conf(cfg).get("claude_bin", "claude") or "claude")):
        return "claude 명령이 없음"
    return ""


def ask_claude(cfg: Config, prompt: str, *, model: str) -> tuple[str, str]:
    """Claude Code 에 한 번 묻는다. (답, 오류) — 둘 중 하나만 채워진다.

    · 도구를 모두 끈다(`--tools ""`) — 글만 쓰면 되는 일이다.
    · 빈 임시 폴더에서 돈다 — 저장소에서 돌리면 긴 CLAUDE.md 를 읽어 답이 흐트러진다.
    · `--bare` 는 쓰지 않는다 — 문서상 그 모드는 CLAUDE_CODE_OAUTH_TOKEN 을 읽지 않는다.
    · ANTHROPIC_API_KEY 를 넘기지 않는다 — 인증 순서상 API 키가 이겨서 **요금이 나간다**.
    """
    conf = _conf(cfg)
    cmd = [str(conf.get("claude_bin", "claude") or "claude"), "-p", "--output-format", "json",
           "--model", model, "--tools", "", "--no-session-persistence", "--system-prompt", SYSTEM]
    env = {**os.environ,
           "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(int(conf.get("max_output_tokens", 64000) or 64000)),
           "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    timeout = int(conf.get("timeout_seconds", 1500) or 1500)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            done = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  cwd=tmp, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return "", f"{timeout // 60}분 안에 답이 오지 않았습니다"
        except OSError as exc:
            return "", f"claude 를 실행하지 못했습니다: {exc}"
    try:
        data = json.loads(done.stdout or "{}")
    except json.JSONDecodeError:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or ["(출력 없음)"]
        return "", f"claude 응답을 읽지 못했습니다: {tail[0][:200]}"
    if data.get("is_error") or done.returncode != 0:
        return "", str(data.get("result") or done.stderr or "알 수 없는 오류")[:300]
    usage = data.get("usage") or {}
    log.info("자동 답하기: %s · 출력 %s토큰 · %.0f초", model, usage.get("output_tokens", "?"),
             (data.get("duration_ms") or 0) / 1000)
    return str(data.get("result") or ""), ""


def answer_today(cfg: Config, date_str: str):
    """묶음을 묻고 답으로 산출물을 만든다. (RunResult | None, 멈춘 이유).

    모델 오류(한도·권한)면 `llm.fallback_model` 로 한 번, 답의 형식이 틀리거나 덩이가 빠지면
    한 번 더 묻는다. 그래도 안 되면 멈춘다 — 묶음은 그대로 남아 사람이 붙여 넣을 수 있다."""
    from .answer import AnswerError, parse_answer
    from .pipeline import answer as answer_pipeline

    out_dir = cfg.output_dir / date_str
    path = out_dir / PACK_NAME
    if not path.exists():
        return None, f"{date_str} 의 붙여넣기 묶음이 없습니다 (이미 답을 받았거나 아침 실행 전)"
    prompt = path.read_text(encoding="utf-8")
    llm = cfg.get("llm", {}) or {}
    model = str(_conf(cfg).get("model") or llm.get("model") or "claude-sonnet-5")
    fallback = str(llm.get("fallback_model", "") or "")
    why = ""
    for attempt in range(2):
        text, err = ask_claude(cfg, prompt, model=model)
        if err and fallback and fallback != model:
            log.warning("자동 답하기 실패(%s) — %s 로 한 번 더", err, fallback)
            text, err = ask_claude(cfg, prompt, model=fallback)
        if err:
            return None, err
        try:
            parsed = parse_answer(text)
        except AnswerError as exc:
            why = f"답 형식이 틀렸습니다: {exc}"
            log.warning("자동 답하기 %d번째 답을 못 읽음: %s", attempt + 1, exc)
            continue
        missing = [k for k in ("blog", "script") if parsed.get(k) is None]
        if missing and attempt == 0:
            why = "답에 빠진 덩이가 있습니다: " + ", ".join(missing)
            log.warning("자동 답하기: %s — 한 번 더 묻습니다", why)
            continue
        return answer_pipeline(cfg, date_str, text), ""
    return None, why or "답을 받지 못했습니다"


def paste_needed_message(cfg: Config, date_str: str, why: str) -> str:
    """자동이 멈춘 날의 알림 — 사람이 예전처럼 붙여 넣으면 된다."""
    site = str(cfg.get("site.url", "") or "").rstrip("/")
    lines = [f"📅 {date_str} 정치 브리핑",
             f"⚠️ 자동 답하기가 멈췄습니다 — {why}",
             "📋 붙여넣기 묶음은 준비돼 있습니다 — 사이트에서 복사해 claude.ai 에 붙여 넣고, 답을 이슈에 붙여 넣으세요"]
    if site:
        lines.append(f"🔗 {site}/latest/")
    return "\n".join(lines)
