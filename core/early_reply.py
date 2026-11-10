"""Prioritize a completed final sentence without exposing draft/tool output."""
import re


def final_fragments(output, pacer, maximum, fragmenter):
    text = str(output.text or '')
    if (maximum > 1 and not output.chains and not any(mark in text for mark in ('`', '*', '[CQ:', '<', '{"', 'http://', 'https://', ']('))):
        boundary = re.search(r'[。！？!?][”’」』]?', text)
        paragraph = re.search(r'\n\s*\n', text)
        ends = ([boundary.end()] if boundary else []) + ([paragraph.start()] if paragraph else [])
        end = min(ends) if ends else 0
        if 4 <= end <= 160 and text[end:].strip():
            return [text[:end]] + fragmenter([], text[end:].strip(), pacer, max_fragments=maximum-1)
    return fragmenter(output.chains, text, pacer, max_fragments=maximum)
