#!/usr/bin/env python3
"""Assemble editable phone-page sources into the installed Python artifact."""

import argparse
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / 'frontend'
ARTIFACT = ROOT / 'lib' / 'phone_page.py'


def read(name, *, trim=False):
    value = (FRONTEND / name).read_text()
    return value.rstrip('\n') if trim else value


def page():
    scripts = ''.join(read(f'js/{name}') for name in (
        'connection.js', 'audio.js', 'controls.js', 'ui.js'))
    return (read('head.html') + '<style>' + read('styles.css') + '</style>' +
            read('body.html') + read('connection.html') + '<script>' + scripts +
            '</script>' + read('tail.html', trim=True))


def module_text():
    return ('"""Generated browser assets; edit frontend/ and run this builder."""\n\n'
            + f'SW = {read("sw.js").encode()!r}\n'
            + f'PAGE = {page()!r}\n'
            + f'PAIR_PAGE = {read("pairing.html", trim=True)!r}\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='fail if the artifact is stale')
    args = parser.parse_args()
    expected = module_text()
    if args.check:
        if not ARTIFACT.exists() or ARTIFACT.read_text() != expected:
            raise SystemExit('lib/phone_page.py is stale; run scripts/build_phone_page.py')
        ast.parse(expected)
        return
    ARTIFACT.write_text(expected)


if __name__ == '__main__':
    main()
