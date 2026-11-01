"""사용자 안내문에 싣는 도구 출력에서 자격 증명으로 보이는 값을 `***`로 가린다.

왜: 도구 실패 원인 줄(ToolFailure summary)은 터미널에 그대로 보이고 이슈·채팅에 복사되기 쉽다. 출력에 섞인 토큰·서명·
비밀번호가 함께 퍼지지 않게, URL 사용자 정보(redact_url)에 더해 아래 형태를 가린다. 로그 파일(0600)은 바꾸지 않는다.
"""
from __future__ import annotations

import math
import re
from collections import Counter

MASK = "***"
"""가린 값 자리에 넣는 표시."""

# 자격 증명으로 보이는 이름의 쿼리 값(대소문자 무시). X-Amz-*는 서명 URL의 모든 인자를 가린다.
_CREDENTIAL_QUERY = re.compile(
    r"(?i)([?&;](?:access_token|api_key|apikey|token|key|sig|signature|password|passwd|secret|auth"
    r"|x-amz-[a-z0-9-]+)=)[^&#\s'\"]*")
# `Authorization: <값>` 헤더는 줄 끝까지 가린다(Proxy-Authorization 포함).
_AUTHORIZATION_HEADER = re.compile(r"(?i)\b(authorization\s*[:=]\s*)\S.*")
# `Bearer <토큰>`·`Basic <base64>`. 16자 이상만 가려 "basic configuration" 같은 일반 문장은 두려는 것이다.
_AUTH_SCHEME_VALUE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/-]{16,}=*")
# 무작위 토큰 후보: base64·URL-safe 글자로만 된 32자 이상 연속 문자열.
_TOKEN_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_MIN_TOKEN_LENGTH = 32
_UUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
_HEX = re.compile(r"[0-9A-Fa-f]+")
# 경로 조각으로 보이는 낱말(소문자 낱말이나 첫 글자만 대문자인 낱말을 -, _로 잇거나 붙인 것). 무작위 토큰에는 거의 없다.
# 반복마다 구분자나 대문자로 시작해야 한다: 소문자 연속을 여러 방식으로 나눌 수 없게 해 지수 시간 역추적(ReDoS)을 막는다.
_WORD_SEGMENT = re.compile(r"[A-Z]?[a-z]+(?:[-_][A-Z]?[a-z]+|[A-Z][a-z]+)*")
# 정규식·엔트로피로 살펴보는 조각의 최대 길이와 한 줄의 최대 길이. 더 긴 조각은 길이·글자 규칙만으로 판단한다.
_MAX_PIECE_LENGTH = 256
_MAX_LINE_LENGTH = 4096
_MIN_WORD_LENGTH = 3
# 글자당 Shannon 엔트로피(비트) 기준. 16진은 글자 종류가 16개뿐이라 기준을 따로 둔다(secret 탐지 도구의 관행).
_HEX_ENTROPY = 3.0
_TEXT_ENTROPY = 4.2


def _entropy(text: str) -> float:
    """text의 글자당 Shannon 엔트로피(비트)를 돌려준다."""
    counts, length = Counter(text), len(text)
    return -sum(count / length * math.log2(count / length) for count in counts.values())


def _looks_random(piece: str) -> bool:
    """32자 이상이고 글자와 숫자가 섞였으며 엔트로피가 기준 이상이면 무작위 토큰으로 본다. UUID(기기 식별자)는 뺀다.

    256자를 넘는 조각은 정규식·엔트로피 검사 없이 무작위 토큰 후보로 본다(이미 32자 이상·토큰 글자뿐이라는 규칙을 만족).
    """
    if len(piece) > _MAX_PIECE_LENGTH:
        return True
    if len(piece) < _MIN_TOKEN_LENGTH or _UUID.fullmatch(piece):
        return False
    if not (any(character.isdigit() for character in piece) and any(character.isalpha() for character in piece)):
        return False
    return _entropy(piece) >= (_HEX_ENTROPY if _HEX.fullmatch(piece) else _TEXT_ENTROPY)


def _mask_token_run(match: re.Match) -> str:
    """토큰 후보를 가린다. 낱말 조각이 있는 경로(`builds/<해시>/logs`)는 '/' 조각마다 따로 판단해 해시만 가린다.

    왜: '/'도 base64 글자라 경로 전체가 한 후보가 될 수 있는데, 경로 전체를 가리면 원인 줄이 쓸모없어진다.
    """
    run = match.group(0)
    pieces = run.split("/")
    if len(pieces) > 1 and any(_is_word(piece) for piece in pieces):
        return "/".join(MASK if _looks_random(piece) else piece for piece in pieces)
    return MASK if _looks_random(run) else run


def _is_word(piece: str) -> bool:
    """3~256자이고 _WORD_SEGMENT 낱말이면 True. 길이를 먼저 봐 긴 조각에는 정규식을 돌리지 않는다."""
    return _MIN_WORD_LENGTH <= len(piece) <= _MAX_PIECE_LENGTH and _WORD_SEGMENT.fullmatch(piece) is not None


def mask_secrets(line: str) -> str:
    """한 줄에서 Authorization 헤더 값, Bearer·Basic 값, 자격 증명 이름의 쿼리 값, 긴 무작위 토큰을 `***`로 바꾼다.

    주의: 추정 규칙이라 모든 비밀을 찾는다고 보장하지 않으며, 숫자가 섞인 긴 상대 경로 조각을 가릴 수도 있다(안내문
    한정이라 원문은 로그에 남는다). URL 사용자 정보는 호출자가 redact_url로 따로 가린다.
    4096자를 넘는 줄은 정규식을 돌리기 전에 끝쪽 4096자만 남긴다(안내문은 끝부분만 쓰고, 처리 시간을 묶어 두려는 것).
    """
    line = line[-_MAX_LINE_LENGTH:]
    line = _AUTHORIZATION_HEADER.sub(lambda match: match.group(1) + MASK, line)
    line = _AUTH_SCHEME_VALUE.sub(lambda match: f"{match.group(1)} {MASK}", line)
    line = _CREDENTIAL_QUERY.sub(lambda match: match.group(1) + MASK, line)
    return _TOKEN_RUN.sub(_mask_token_run, line)
