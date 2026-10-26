"""Insertion-only integration for a deliberately restricted Groovy lexical subset.

Not a Groovy interpreter or general parser. Unsupported quoting/metaprogramming
forms fail closed; the original bytes are never rewritten or executed here.
"""
from .core import require

_ERROR = 'Unsupported Groovy build syntax; use simple plugins blocks without script application or composite builds'
_MAX_BYTES = 4 * 1024 * 1024
_FORBIDDEN = {'apply', 'includeBuild', 'import', 'class', 'interface', 'trait', 'def',
              'evaluate', 'metaClass', 'getMetaClass', 'setMetaClass'}


def _tokens(source):
    require(type(source) is str, _ERROR)
    try:
        size = len(source.encode('utf-8'))
    except UnicodeError:
        require(False, _ERROR)
    require(size <= _MAX_BYTES
            and '\x00' not in source and '\r' not in source and '\\u' not in source, _ERROR)
    result, stack = [], []
    i = 0
    while i < len(source):
        ch = source[i]
        if ch in ' \t\n':
            i += 1
            continue
        if source.startswith('//', i):
            end = source.find('\n', i + 2)
            i = len(source) if end < 0 else end + 1
            continue
        if source.startswith('/*', i):
            end = source.find('*/', i + 2)
            require(end >= 0 and '/*' not in source[i + 2:end], _ERROR)
            i = end + 2
            continue
        start = i
        if ch in "'\"":
            require(not source.startswith(ch * 3, i), _ERROR)
            i += 1
            while i < len(source) and source[i] != ch:
                require(source[i] not in '\n\r$' and ord(source[i]) >= 32, _ERROR)
                if source[i] == '\\':
                    i += 1
                    require(i < len(source) and source[i] in "\\'\"", _ERROR)
                i += 1
            require(i < len(source), _ERROR)
            i += 1
            value = source[start:i]
            require('io.reproof.instrumentation' not in value and 'reproof-build-logic' not in value
                    and value[1:-1] not in _FORBIDDEN
                    and not (result and result[-1][1] == '.'), _ERROR)
            result.append(('string', value, start, i, len(stack)))
        elif ch.isascii() and (ch.isalpha() or ch == '_'):
            i += 1
            while i < len(source) and source[i].isascii() and (source[i].isalnum() or source[i] == '_'):
                i += 1
            value = source[start:i]
            require(value not in _FORBIDDEN, _ERROR)
            result.append(('name', value, start, i, len(stack)))
        elif ch in '({[':
            require(len(stack) < 128, _ERROR)
            result.append(('open', ch, start, start + 1, len(stack)))
            stack.append(ch)
            i += 1
        elif ch in ')}]':
            require(stack and stack.pop() == {')': '(', '}': '{', ']': '['}[ch], _ERROR)
            result.append(('close', ch, start, start + 1, len(stack)))
            i += 1
        else:
            # No slashy/dollar-slashy strings, annotations, escapes, backticks,
            # operators involving division, or non-ASCII identifier syntax.
            require(ch in '0123456789.,:=+-*!?&|<>%;', _ERROR)
            result.append(('punct', ch, start, start + 1, len(stack)))
            i += 1
        require(len(result) <= 200_000, _ERROR)
    require(not stack, _ERROR)
    return result


def _block(source, name, *, required):
    tokens = _tokens(source)
    matches = [i for i, token in enumerate(tokens) if token[0:2] == ('name', name) and token[4] == 0]
    require(len(matches) <= 1 and (bool(matches) or not required), _ERROR)
    if not matches:
        return None
    i = matches[0]
    # The selected block must be the first statement, not x.plugins, a named
    # argument, a function invocation, or a duplicate conditional block.
    require(i == 0 and i + 1 < len(tokens) and tokens[i + 1][0:2] == ('open', '{')
            and tokens[i + 1][4] == 0, _ERROR)
    return tokens[i + 1][3]


def configure_groovy(settings, module):
    """Return exactly two insertion-only copies, never rewrite original bodies."""
    settings_at = _block(settings, 'pluginManagement', required=False)
    module_at = _block(module, 'plugins', required=True)
    settings_insert = "\n    includeBuild('reproof-build-logic')\n"
    module_insert = "\n    id 'io.reproof.instrumentation'\n"
    settings_result = ("pluginManagement {" + settings_insert + "}\n" + settings if settings_at is None
                       else settings[:settings_at] + settings_insert + settings[settings_at:])
    return {'settings.gradle': settings_result,
            'module.gradle': module[:module_at] + module_insert + module[module_at:]}
