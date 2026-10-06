"""Format, buffer and optionally deduplicate source-search results."""

from __future__ import annotations

import json
import sys


class RecordDeduplicator:
    """Avoid storing output-sized state unless junction stripping requires it."""

    def __init__(self, enabled):
        self.seen = set() if enabled else None

    def duplicate(self, kind, display, path, line=None):
        if self.seen is None:
            return False
        key = (kind, display, path, line)
        if key in self.seen:
            return True
        self.seen.add(key)
        return False


class LineBuffer:
    def __init__(self, limit: int = 4096):
        self.limit = limit
        self.buf = []
        self.write = sys.stdout.write

    def emit(self, line: str):
        self.buf.append(line)
        self.buf.append("\n")

        if len(self.buf) >= self.limit * 2:
            self.flush()

    def emit_json(self, obj):
        self.emit(json.dumps(obj, ensure_ascii=True))

    def flush(self):
        if self.buf:
            self.write("".join(self.buf))
            self.buf.clear()


class ResultEmitter:
    """Format and deduplicate output while tracking emitted results."""

    def __init__(self, args, out, stats):
        self.args = args
        self.out = out
        self.stats = stats
        self.dedup = RecordDeduplicator(args.strip_junctions)

    def emit_file(self, element_info, rel_path, origin=None):
        display = element_info["recipe"] if self.args.strip_junctions else element_info["label"]

        if self.dedup.duplicate("file", display, rel_path):
            return

        if self.args.json:
            record = {
                "type": "file",
                "element": element_info["label"],
                "recipe": display,
                "path": rel_path,
            }

            if self.args.origin and origin is not None:
                record["origin"] = origin

            if "option_sets" in element_info:
                record["option_sets"] = element_info["option_sets"]

            self.out.emit_json(record)
        else:
            if self.args.origin and origin is not None:
                origin_id = origin.get("id", "?")
                self.out.emit(f"{display}:{origin_id}:{rel_path}")
            else:
                self.out.emit(f"{display}:{rel_path}")

        self.stats["results"] += 1

    def emit_match(self, element_info, rel_path, line_number, text, origin=None):
        display = element_info["recipe"] if self.args.strip_junctions else element_info["label"]

        # Distinct trees can share a stripped recipe name and path but
        # differ in content, so the line text is part of the identity.
        if self.dedup.duplicate("match", display, rel_path, (line_number, hash(text))):
            return

        if self.args.json:
            record = {
                "type": "match",
                "element": element_info["label"],
                "recipe": display,
                "path": rel_path,
                "line": line_number,
                "text": text,
            }

            if self.args.origin and origin is not None:
                record["origin"] = origin

            if "option_sets" in element_info:
                record["option_sets"] = element_info["option_sets"]

            self.out.emit_json(record)
        else:
            if self.args.origin and origin is not None:
                origin_id = origin.get("id", "?")
                prefix = f"{display}:{origin_id}:{rel_path}"
            else:
                prefix = f"{display}:{rel_path}"

            if self.args.line_number:
                self.out.emit(f"{prefix}:{line_number}:{text}")
            else:
                self.out.emit(f"{prefix}:{text}")

        self.stats["results"] += 1
