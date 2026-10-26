"""Bounded, exact-position unified diff proposals; never invokes patch or Git."""
from __future__ import annotations

import re
from .core import require
from .execution.wire import canonical, safe_transfer_path

_HUNK = re.compile(r'@@ -([0-9]{1,7})(?:,([0-9]{1,7}))? \+([0-9]{1,7})(?:,([0-9]{1,7}))? @@(?: [^\r\n]*)?\n')
_INDEX = re.compile(r'index [0-9a-f]{7,64}\.\.[0-9a-f]{7,64}(?: 100644)?\n')


def exact_edits(diff, sources):
    """Validate every file before returning existing exact-replacement edits.

    Supports modifications of existing, nonempty, LF-terminated UTF-8 text.
    File creation/deletion, metadata changes and no-newline markers are rejected.
    The caller separately binds the full immutable source snapshot digest.
    """
    require(type(diff) is str and 0 < len(diff.encode('utf-8')) <= 256 * 1024
            and '\r' not in diff and '\0' not in diff and diff.endswith('\n'), 'Invalid unified diff')
    require(type(sources) is dict and sources, 'Invalid source packet')
    lines = diff.splitlines(keepends=True)
    # Only LF divides lines. Python splitlines also recognizes Unicode separators.
    require(lines == [line + '\n' for line in diff[:-1].split('\n')], 'Invalid line delimiter')
    i = 0
    edits = []
    seen = set()
    while i < len(lines):
        git_path = None
        if lines[i].startswith('diff --git '):
            header = lines[i][11:-1]
            # No quoting, timestamps, escaped paths, or arbitrary Git metadata.
            require(header.startswith('a/') and ' b/' in header, 'Invalid diff header')
            left, right = header.split(' b/', 1)
            require(left[2:] == right, 'Rename is not supported')
            git_path = right
            i += 1
            if i < len(lines) and lines[i].startswith('index '):
                require(_INDEX.fullmatch(lines[i]) is not None, 'Invalid index header')
                i += 1
        require(i + 1 < len(lines) and lines[i].startswith('--- a/')
                and lines[i + 1].startswith('+++ b/'), 'Expected existing-file headers')
        path = lines[i][6:-1]
        require(path == lines[i + 1][6:-1] and (git_path is None or git_path == path), 'Path mismatch')
        safe_transfer_path(path)
        require(path not in seen and path in sources and len(seen) < 64, 'Undeclared or duplicate path')
        seen.add(path)
        before = sources[path]
        require(type(before) is str and before and before.endswith('\n') and '\r' not in before
                and '\0' not in before and len(before.encode('utf-8')) <= 128 * 1024, 'Unsupported source text')
        original = [line + '\n' for line in before[:-1].split('\n')]
        output = []
        cursor = 0
        previous_end = -1
        hunks = 0
        i += 2
        while i < len(lines) and lines[i].startswith('@@ '):
            match = _HUNK.fullmatch(lines[i])
            require(match is not None, 'Invalid hunk')
            old_start, old_count, new_start, new_count = match.groups()
            old_start, new_start = int(old_start), int(new_start)
            old_count = 1 if old_count is None else int(old_count)
            new_count = 1 if new_count is None else int(new_count)
            require(old_count or new_count, 'Empty hunk')
            start = old_start - 1 if old_count else old_start
            require(0 <= start <= len(original) and start >= cursor and start > previous_end,
                    'Overlapping or unordered hunks')
            require(old_count == 0 or old_start > 0, 'Invalid old position')
            output.extend(original[cursor:start])
            expected_new = len(output) + 1 if new_count else len(output)
            require(new_start == expected_new, 'New position mismatch')
            i += 1
            old_seen = new_seen = 0
            while old_seen < old_count or new_seen < new_count:
                require(i < len(lines) and lines[i][:1] in {' ', '+', '-'}, 'Truncated hunk')
                mark, content = lines[i][0], lines[i][1:]
                if mark in {' ', '-'}:
                    require(old_seen < old_count and start + old_seen < len(original)
                            and original[start + old_seen] == content, 'Original context mismatch')
                    old_seen += 1
                if mark in {' ', '+'}:
                    require(new_seen < new_count, 'Hunk count mismatch')
                    output.append(content)
                    new_seen += 1
                i += 1
            previous_end = start + old_count - 1
            # Two insertions at the same boundary are ambiguous even with zero old lines.
            if old_count == 0:
                previous_end = start
            cursor = start + old_count
            hunks += 1
            require(hunks <= 4096, 'Too many hunks')
        require(hunks > 0, 'Missing hunk')
        output.extend(original[cursor:])
        after = ''.join(output)
        require(after and after != before and len(after.encode('utf-8')) <= 128 * 1024,
                'Empty, no-op or oversized candidate')
        edits.append({'path': path, 'old': before, 'new': after})
    require(edits and len(canonical(edits)) <= 256 * 1024, 'Proposal too large')
    return edits
