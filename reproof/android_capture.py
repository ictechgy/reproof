"""Finite operator-selected Android capture limits, not application state proof."""
from .core import ContractError

CAPTURE_WIDTHS = (480, 720, 960, 1280, 1440, 1920)
DEFAULT_CAPTURE_WIDTH = 960
PIXEL_LIMIT = 8294400
FRAME_BYTES = 3145728


def capture_policy(max_width=DEFAULT_CAPTURE_WIDTH):
    if type(max_width) is not int or max_width not in CAPTURE_WIDTHS:
        raise ContractError('Android capture width is unsupported')
    return {'schemaVersion':1,'maxWidth':max_width,
            'pixelLimit':PIXEL_LIMIT,'frameBytes':FRAME_BYTES}


def require_capture_receipt(value, max_width):
    expected=capture_policy(max_width)
    if type(value) is not dict or set(value)!=set(expected) or any(
            type(value[key]) is not int or value[key]!=wanted for key,wanted in expected.items()):
        raise ContractError('Android capture policy acknowledgement differs')
    return expected
