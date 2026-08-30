#!/usr/bin/env python3
"""
phpcheck.py - structural validation of PHP files without an interpreter.

Not a parser: it tokenises well enough to ignore strings, comments and heredocs,
then checks the things that actually break a deployment - unbalanced brackets,
a stray close tag, calls to functions that exist nowhere, and syntax newer than
the target PHP version.
"""
import re
import sys

# Everything the two files legitimately call from the standard library.
BUILTINS = {
    'array', 'array_key_exists', 'count', 'date_default_timezone_set', 'echo',
    'explode', 'file_get_contents', 'header', 'htmlspecialchars', 'http_response_code',
    'implode', 'in_array', 'ini_get', 'intdiv', 'is_array', 'is_link', 'is_readable',
    'isset', 'json_decode', 'json_encode', 'max', 'min', 'number_format',
    'preg_match', 'preg_replace', 'rawurlencode', 'readlink', 'require_once',
    'set_time_limit', 'sprintf', 'str_repeat', 'stream_context_create', 'strcasecmp',
    'strlen', 'substr', 'timezone_identifiers_list', 'trim', 'unset', 'empty',
    'list', 'exit', 'die', 'print', 'return', 'declare', 'strict_types',
}
KEYWORDS = {
    'if', 'else', 'elseif', 'for', 'foreach', 'while', 'do', 'switch', 'case',
    'default', 'break', 'continue', 'function', 'fn', 'return', 'try', 'catch',
    'finally', 'throw', 'new', 'class', 'const', 'match', 'and', 'or', 'xor',
    'as', 'endif', 'endforeach', 'endwhile', 'static', 'use', 'global',
}
# Syntax introduced after PHP 8.0, which AlmaLinux 9 ships.
TOO_NEW = [
    (r'\breadonly\s+\w', 'readonly properties are PHP 8.1+'),
    (r'\benum\s+\w+\s*[:{]', 'enums are PHP 8.1+'),
    (r'\bnever\b\s*\{', 'never return type is PHP 8.1+'),
    (r'\.\.\.\s*\$\w+\s*\)\s*:\s*static', 'PHP 8.1+ syntax'),
    (r'\bjson_validate\s*\(', 'json_validate() is PHP 8.3+'),
    (r'\bstr_contains\s*\(', 'str_contains() is PHP 8.0+, fine'),
]


def php_blocks(src):
    """Yield (offset, code) for each <?php / <?= region."""
    out, i = [], 0
    while True:
        start = src.find('<?', i)
        if start < 0:
            break
        if src.startswith('<?php', start):
            body_start = start + 5
        elif src.startswith('<?=', start):
            body_start = start + 3
        else:
            i = start + 2
            continue
        end = find_close_tag(src, body_start)
        out.append((body_start, src[body_start:end]))
        i = end + 2 if end < len(src) else len(src)
    return out


def find_close_tag(src, start):
    """Find '?>' that is not inside a string or comment."""
    i, n = start, len(src)
    while i < n:
        c = src[i]
        if c in ("'", '"'):
            i = skip_string(src, i)
            continue
        if src.startswith('//', i) or c == '#':
            nl = src.find('\n', i)
            i = n if nl < 0 else nl
            continue
        if src.startswith('/*', i):
            close = src.find('*/', i + 2)
            i = n if close < 0 else close + 2
            continue
        if src.startswith('<<<', i):
            i = skip_heredoc(src, i)
            continue
        if src.startswith('?>', i):
            return i
        i += 1
    return n


def skip_string(src, i):
    quote = src[i]
    i += 1
    while i < len(src):
        if src[i] == '\\':
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    return i


def skip_heredoc(src, i):
    match = re.match(r"<<<[ \t]*(['\"]?)(\w+)\1\r?\n", src[i:])
    if not match:
        return i + 3
    label = match.group(2)
    body = i + match.end()
    closer = re.search(r'^\s*' + label + r'\b', src[body:], re.M)
    return body + (closer.end() if closer else len(src) - body)


def analyse(path):
    src = open(path).read()
    problems = []

    code = []
    for _, block in php_blocks(src):
        i, n = 0, len(block)
        while i < n:
            c = block[i]
            if c in ("'", '"'):
                j = skip_string(block, i)
                code.append(' ' * (j - i))
                i = j
                continue
            if block.startswith('//', i) or c == '#':
                nl = block.find('\n', i)
                j = n if nl < 0 else nl
                code.append(' ' * (j - i))
                i = j
                continue
            if block.startswith('/*', i):
                close = block.find('*/', i + 2)
                j = n if close < 0 else close + 2
                code.append(' ' * (j - i))
                i = j
                continue
            if block.startswith('<<<', i):
                j = skip_heredoc(block, i)
                code.append(' ' * (j - i))
                i = j
                continue
            code.append(c)
            i += 1
    code = ''.join(code)

    # brackets
    stack, line = [], 1
    pairs = {')': '(', '}': '{', ']': '['}
    for ch in code:
        if ch == '\n':
            line += 1
        elif ch in '({[':
            stack.append((ch, line))
        elif ch in ')}]':
            if not stack:
                problems.append('line %d: stray %s' % (line, ch))
            elif stack[-1][0] != pairs[ch]:
                problems.append('line %d: %s closes %s opened on line %d'
                                % (line, ch, stack[-1][0], stack[-1][1]))
                stack.pop()
            else:
                stack.pop()
    for ch, opened in stack:
        problems.append('line %d: %s never closed' % (opened, ch))

    # functions
    defined = set(re.findall(r'function\s+(\w+)\s*\(', code))
    called = set(re.findall(r'(?<![\w$>:\\])([a-z_]\w*)\s*\(', code))
    unknown = sorted(called - defined - BUILTINS - KEYWORDS)
    if unknown:
        problems.append('unknown calls: ' + ', '.join(unknown))

    # version
    for pattern, message in TOO_NEW:
        if 'fine' in message:
            continue
        if re.search(pattern, code):
            problems.append(message)

    # a trailing ?> before output is a classic source of stray whitespace
    if src.rstrip().endswith('?>'):
        problems.append('file ends with ?> - drop it to avoid stray output')

    return defined, problems


if __name__ == '__main__':
    # Definitions are shared across every file given, since they include each
    # other with require_once - checking them in isolation reports false alarms.
    everything = set()
    for path in sys.argv[1:]:
        everything |= analyse(path)[0]
    BUILTINS |= everything

    failed = False
    for path in sys.argv[1:]:
        defined, problems = analyse(path)
        print('%s  (%d functions defined)' % (path, len(defined)))
        if problems:
            failed = True
            for p in problems:
                print('   PROBLEM: %s' % p)
        else:
            print('   structure OK')
    sys.exit(1 if failed else 0)
