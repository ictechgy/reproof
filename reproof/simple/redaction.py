"""사용자 안내문에 싣는 도구 출력에서 자격 증명으로 보이는 값을 `***`로 가린다.

왜: 도구 실패 원인 줄(ToolFailure summary)은 터미널에 그대로 보이고 이슈·채팅에 복사되기 쉽다. 출력에 섞인 토큰·서명·
비밀번호가 함께 퍼지지 않게, URL 사용자 정보(redact_url)에 더해 아래 형태를 가린다. 로그 파일(0600)은 바꾸지 않는다.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

MASK = "***"
"""가린 값 자리에 넣는 표시."""

# 쿼리 인자 하나(구분자, 이름, 값). 이름이 자격 증명처럼 보이면 값을 가린다(_is_credential_key). '#' 뒤 조각도 같은 형식이다.
_QUERY_PARAMETER = re.compile(r"([?&;#])([A-Za-z0-9_.~-]{1,64})=([^&#\s'\"]*)")
# 이름이 정확히 이것이면 자격 증명으로 본다(소문자 비교). 접미·부분 일치 규칙은 _is_credential_key에 있다.
_CREDENTIAL_KEYS = frozenset({"key", "apikey", "sig", "signature", "auth", "token"})
_CREDENTIAL_KEY_SUFFIXES = ("token", "_key", "-key")
_CREDENTIAL_KEY_PARTS = ("secret", "password", "passwd")
# 점 세 조각 JWT. 조각은 base64url이고 '.'은 글자에 없어 정규식이 되돌아가지 않는다(선형 시간).
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
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


# 터미널 색·커서·제목을 바꾸는 ANSI escape 순서(CSI와 2바이트 escape).
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-_]")


def strip_control_characters(text: str) -> str:
    """ANSI escape 순서와 제어 문자(유니코드 C 분류: 줄바꿈·탭·ESC·BEL·형식 문자 등)를 지운다.

    왜: 도구 출력에서 뽑은 조각(저장소 URL 등)을 안내문에 넣을 때, 상대가 심은 escape가 터미널 화면을 조작하거나
    붙여 넣을 명령에 숨은 글자를 넣지 못하게 하려는 것이다.
    """
    return "".join(character for character in _ANSI_ESCAPE.sub("", text)
                   if not unicodedata.category(character).startswith("C"))


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


def _is_credential_key(key: str) -> bool:
    """쿼리 이름이 자격 증명처럼 보이는지 본다: 정확한 이름, `*token`·`*_key`·`*-key` 접미, `secret`·`password`·
    `passwd` 포함, `x-amz-*`(서명 URL 인자). 대소문자는 무시한다."""
    lowered = key.lower()
    return (lowered in _CREDENTIAL_KEYS or lowered.startswith("x-amz-") or lowered.endswith(_CREDENTIAL_KEY_SUFFIXES)
            or any(part in lowered for part in _CREDENTIAL_KEY_PARTS))


def _mask_query_parameter(match: re.Match) -> str:
    """자격 증명 이름의 쿼리 값만 `***`로 바꾸고 다른 인자는 그대로 둔다."""
    separator, key, _ = match.groups()
    return f"{separator}{key}={MASK}" if _is_credential_key(key) else match.group(0)


def _is_path_like(run: str, pieces: list[str]) -> bool:
    """'/'로 나눈 run이 경로처럼 보이는지 본다: '+'나 끝 '='(base64 표시)가 없고 낱말 조각이 절반 이상이다.

    왜: 경로(`builds/<해시>/logs`)는 조각마다 판단해야 원인 줄이 쓸모 있지만, '/'가 섞인 base64 비밀은 우연히 낱말 같은
    조각(info, abc)이 하나 끼었다고 조각마다 판단하면 32자보다 짧은 조각들이 그대로 남는다.
    """
    if "+" in run or run.endswith("="):
        return False
    return sum(1 for piece in pieces if _is_word(piece)) * 2 >= len(pieces)


def _mask_path_pieces(pieces: list[str]) -> str:
    """경로 조각을 가린다. 낱말이 아닌 조각을 이어 붙인 것이 무작위로 보이면 그 조각을 모두, 아니면 무작위 조각만 가린다."""
    others = "".join(piece for piece in pieces if not _is_word(piece))
    if _looks_random(others):
        return "/".join(piece if _is_word(piece) else MASK for piece in pieces)
    return "/".join(MASK if _looks_random(piece) else piece for piece in pieces)


def _mask_token_run(match: re.Match) -> str:
    """토큰 후보를 가린다. 경로처럼 보이면(_is_path_like) 조각 단위로, 아니면 run 전체로 판단한다.

    왜: '/'도 base64 글자라 경로 전체가 한 후보가 될 수 있는데, 경로 전체를 가리면 원인 줄이 쓸모없어진다.
    """
    run = match.group(0)
    pieces = run.split("/")
    if len(pieces) > 1 and _is_path_like(run, pieces):
        return _mask_path_pieces(pieces)
    return MASK if _looks_random(run) else run


def _is_word(piece: str) -> bool:
    """3~256자이고 _WORD_SEGMENT 낱말이면 True. 길이를 먼저 봐 긴 조각에는 정규식을 돌리지 않는다."""
    return _MIN_WORD_LENGTH <= len(piece) <= _MAX_PIECE_LENGTH and _WORD_SEGMENT.fullmatch(piece) is not None


def _tail_window(line: str) -> str:
    """4096자를 넘는 줄은 끝쪽 4096자만 남기되, 창 시작에서 잘린 첫 조각(첫 공백까지)은 버린다. 공백이 없으면 전부 가린다.

    왜: 잘린 조각은 앞쪽의 단서(`access_token=`, `https://`)를 잃어 비밀이어도 알아볼 수 없다. 처리 시간을 묶어 두려고
    창은 그대로 두고, 판단할 수 없는 조각만 버린다.
    """
    if len(line) <= _MAX_LINE_LENGTH:
        return line
    window = line[-_MAX_LINE_LENGTH:]
    _, separator, rest = window.partition(" ")
    return "… " + rest if separator else MASK


def mask_secrets(line: str) -> str:
    """한 줄에서 Authorization 헤더 값, Bearer·Basic 값, JWT, 자격 증명 이름의 쿼리 값, 긴 무작위 토큰을 `***`로 바꾼다.

    주의: 추정 규칙이라 모든 비밀을 찾는다고 보장하지 않으며, 숫자가 섞인 긴 상대 경로 조각을 가릴 수도 있다(안내문
    한정이라 원문은 로그에 남는다). URL 사용자 정보는 호출자가 redact_url로 따로 가린다.
    4096자를 넘는 줄은 정규식을 돌리기 전에 끝쪽만 남긴다(_tail_window: 잘린 첫 조각은 버린다).
    """
    line = _tail_window(line)
    line = _AUTHORIZATION_HEADER.sub(lambda match: match.group(1) + MASK, line)
    line = _AUTH_SCHEME_VALUE.sub(lambda match: f"{match.group(1)} {MASK}", line)
    line = _JWT.sub(MASK, line)
    line = _QUERY_PARAMETER.sub(_mask_query_parameter, line)
    return _TOKEN_RUN.sub(_mask_token_run, line)
