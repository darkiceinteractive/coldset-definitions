#!/usr/bin/env python3
"""Build definitions.json: what Coldset knows about agent tool settings.

Reads the official sources, keeps the plain-English summaries in data/,
writes summaries for anything new or changed, and writes definitions.json.
Stops with an error, and writes nothing, when a source does not look the way
it should: a broken night must never replace a good file.

Sources:
  - Claude Code docs (code.claude.com/docs/en/*.md): environment variables,
    model configuration, settings.
  - The published Claude Code settings schema (SchemaStore, Apache-2.0).
  - Context7, as a fallback for a docs page that cannot be fetched.

Descriptions are kept in our own words in data/. New or reworded entries get
a draft from the summarisation APIs listed in PROVIDERS, tried in order. Keys
come from the environment: DEEPSEEK_API_KEY, OPENROUTER_API_KEY and
CONTEXT7_API_KEY, all optional; with none, new entries are published with
facts and a docs link only.

Usage: python3 build.py [--offline DIR]   (DIR holds env-vars.md,
model-config.md, settings.md and schema.json, for testing without network)
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
OUT = ROOT / "definitions.json"
FORMAT = 1

DOCS = "https://code.claude.com/docs/en/"
PAGES = {
    "env": DOCS + "env-vars.md",
    "models": DOCS + "model-config.md",
    "reference": DOCS + "settings-reference.md",
}
SCHEMA_URL = "https://json.schemastore.org/claude-code-settings.json"
FAMILIES = ("fable", "opus", "sonnet", "haiku")

class SourceError(Exception):
    pass


# ---- fetching ---------------------------------------------------------------------

def fetch(url: str, *, headers: dict | None = None, data: bytes | None = None, timeout: int = 60) -> bytes:
    last: Exception | None = None
    for attempt in range(4):
        try:
            request = urllib.request.Request(url, data=data, headers={"User-Agent": "coldset-definitions", **(headers or {})})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError) as error:
            last = error
            code = getattr(error, "code", None)
            if code is not None and 400 <= code < 500 and code != 429:
                break
            time.sleep(2 ** (attempt + 1))
    raise SourceError(f"{url}: {last}")


def context7_page(topic: str) -> str:
    """The same docs through Context7, when the docs site cannot be reached."""
    key = os.environ.get("CONTEXT7_API_KEY")
    if not key:
        raise SourceError("no CONTEXT7_API_KEY for the fallback")
    query = urllib.parse.urlencode({"libraryId": "/anthropics/claude-code", "query": topic, "type": "txt"})
    return fetch(f"https://context7.com/api/v2/context?{query}", headers={"Authorization": f"Bearer {key}"}).decode()


def load_sources(offline: Path | None) -> dict:
    if offline:
        return {
            "env": (offline / "env-vars.md").read_text(),
            "models": (offline / "model-config.md").read_text(),
            "reference": (offline / "settings-reference.md").read_text(),
            "schema": json.loads((offline / "schema.json").read_text()),
        }
    sources = {}
    for name, url in PAGES.items():
        try:
            sources[name] = fetch(url).decode()
        except SourceError as error:
            print(f"warning: {error}; trying Context7", file=sys.stderr)
            sources[name] = context7_page({"env": "environment variables reference", "models": "model configuration aliases", "reference": "all settings reference"}[name])
    sources["schema"] = json.loads(fetch(SCHEMA_URL))
    return sources


# ---- reading the sources ----------------------------------------------------------

ROW = re.compile(r"^\|\s*`([A-Za-z_$][A-Za-z0-9_$.]*)`\s*\|(.*)\|\s*$")
TICKED = re.compile(r"`([A-Z][A-Z0-9_]{3,})`")


def env_rows(page: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in page.splitlines():
        match = ROW.match(line)
        if match and re.fullmatch(r"[A-Z][A-Z0-9_]+", match.group(1)) and match.group(1) not in rows:
            rows[match.group(1)] = match.group(2).strip()
    if len(rows) < 200:
        raise SourceError(f"env page has only {len(rows)} variables; the page format may have changed")
    return rows


def env_status(name: str, text: str, pages: str) -> dict:
    status: dict = {}
    upper = text.upper()
    replacement = None
    for pattern in (r"in favou?r of\s+`([A-Z0-9_]+)`", r"[Uu]se\s+`([A-Z0-9_]+)`\s+instead", r"[Ss]uperseded by\s+`([A-Z0-9_]+)`", r"[Rr]eplaced by\s+`([A-Z0-9_]+)`"):
        match = re.search(pattern, text)
        if match and match.group(1) != name:
            replacement = match.group(1)
            break
    if not replacement:
        # Elsewhere in the docs: "`NAME` is deprecated in favor of `OTHER`".
        match = re.search(rf"`{name}`[^.\n]{{0,40}}deprecated in favou?r of\s+`([A-Z0-9_]+)`", pages)
        if match:
            replacement = match.group(1)
    if "DEPRECATED" in upper:
        status["deprecated"] = replacement or True
    if re.match(r"\W*Removed\b", text) or re.search(r"\bRemoved in v\d", text) or re.search(r"\bnow a no-op\b", text, re.I):
        status["removed"] = True
    if replacement and "deprecated" not in status and "removed" not in status:
        status["superseded_by"] = replacement
    return status


INDEX_ROW = re.compile(r"^\|\s*\[`([^`]+)`\]\(#[^)]*\)\s*\|(.*?)\|(.*?)\|(.*?)\|\s*$")


def settings_index(page: str) -> dict[str, dict]:
    """Every settings key from the docs' index: its description, topic and
    which files it may go in, in the docs' own order of topics."""
    start = page.find("## Settings index")
    if start < 0:
        raise SourceError("the settings reference has no index; the page format may have changed")
    end = page.find("\n## ", start + 10)
    keys = {}
    for line in page[start:end].splitlines():
        match = INDEX_ROW.match(line)
        if match:
            keys[match.group(1)] = {
                "text": match.group(2).strip(),
                "topic": match.group(3).strip(),
                "scope": match.group(4).strip(),
            }
    if len(keys) < 150:
        raise SourceError(f"the settings index lists only {len(keys)} keys; the page format may have changed")
    return keys


def topics_in_order(page: str) -> list[str]:
    return [heading for heading in re.findall(r"^## (.+)$", page, re.M) if heading not in ("Settings index", "See also")]


def sections_for(index: dict[str, dict], order: list[str]) -> list[dict]:
    topics = order + sorted({entry["topic"] for entry in index.values()} - set(order))
    return [
        {"title": topic, "keys": sorted(key for key, entry in index.items() if entry["topic"] == topic)}
        for topic in topics
        if any(entry["topic"] == topic for entry in index.values())
    ]


def version(model_id: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", model_id.split("-", 2)[2])[:2])


def models(page: str, aliases: dict) -> dict:
    ids = set(re.findall(r"\bclaude-(?:fable|opus|sonnet|haiku)-\d+-\d+(?:-\d{8})?\b", page))
    # The provider table names what each alias means on the Anthropic API.
    row = re.search(r"^\|\s*Anthropic API\s*\|(.*)$", page, re.M)
    if row:
        for family, major, minor in re.findall(r"\b(Opus|Sonnet|Haiku|Fable)\s+(\d+)(?:\.(\d+))?", row.group(1)):
            ids.add(f"claude-{family.lower()}-{major}-{minor or 0}" if minor else f"claude-{family.lower()}-{major}")
    for family, major, minor in re.findall(r"\b(Fable)\s+(\d+)\.(\d+)\s+everywhere else", page):
        ids.add(f"claude-{family.lower()}-{major}-{minor}")
    latest = {}
    for family in FAMILIES:
        found = [i for i in ids if i.startswith(f"claude-{family}-") and re.fullmatch(rf"claude-{family}-\d+(-\d+)?", i)]
        if found:
            latest[family] = max(found, key=version)
    if set(latest) != set(FAMILIES):
        raise SourceError(f"model page names only {sorted(latest)}; expected {list(FAMILIES)}")
    return {
        "latest": latest,
        "known": sorted(ids),
        "aliases": [{"name": name, "summary": summary} for name, summary in aliases.items()],
    }


# ---- summaries --------------------------------------------------------------------

def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", re.sub(r"`[^`]*`", " ", text.lower()))


def longest_shared_run(a: str, b: str) -> int:
    x, y = words(a), words(b)
    best = 0
    for i in range(len(x)):
        for j in range(len(y)):
            k = 0
            while i + k < len(x) and j + k < len(y) and x[i + k] == y[j + k]:
                k += 1
            best = max(best, k)
    return best


PROMPT = """Write one-line hover text for a code editor. For each item below, say what it does in your own words.
Rules: at most 30 words; one or two plain sentences; Australian English; no em or en dashes; keep names, numbers and values exact;
do not copy more than four words in a row from the description you are given; no Markdown links.
Answer with only a JSON object mapping each name to {{"summary": "...", "kind": "..."}} where kind is one of
model, bool, number, duration, path, url, string, list (for environment variables) or omitted for settings keys.

{items}"""

PROVIDERS = [
    ("DEEPSEEK_API_KEY", "https://api.deepseek.com/chat/completions", "deepseek-flash"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions", "deepseek/deepseek-v4.1-flash"),
    ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions", "qwen/qwen3.8-flash"),
]


def ask_model(prompt: str) -> dict:
    errors = []
    for key_name, url, model in PROVIDERS:
        key = os.environ.get(key_name)
        if not key:
            continue
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }).encode()
        try:
            raw = fetch(url, data=body, timeout=120, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
            content = json.loads(raw)["choices"][0]["message"]["content"]
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
            return json.loads(content)
        except (SourceError, KeyError, IndexError, json.JSONDecodeError) as error:
            errors.append(f"{model}: {error}")
    raise SourceError("no model could write summaries: " + "; ".join(errors or ["no API keys set"]))


def plain(text: str) -> str:
    """Docs text with links reduced to their words."""
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)


def summarise(items: dict[str, str], *, env: bool) -> dict[str, dict]:
    """Own-words summaries for `items` (name -> official description).
    One that comes back too close to the source is asked for again, up to
    three rounds, and then left out rather than published."""
    done: dict[str, dict] = {}
    remaining = sorted(items)
    for _round in range(3):
        failed = []
        for start in range(0, len(remaining), 30):
            batch = remaining[start:start + 30]
            listing = "\n".join(f"- {name}: {plain(items[name])}" for name in batch)
            try:
                answer = ask_model(PROMPT.format(items=listing))
            except SourceError as error:
                print(f"warning: {error}", file=sys.stderr)
                return done
            for name in batch:
                entry = answer.get(name) if isinstance(answer, dict) else None
                if not isinstance(entry, dict):
                    failed.append(name)
                    continue
                summary = re.sub(r"[\u2013\u2014]", ",", str(entry.get("summary", "")).strip())
                if not summary or len(summary.split()) > 40 or longest_shared_run(summary, plain(items[name])) > 5:
                    failed.append(name)
                    continue
                record = {"summary": summary}
                if env:
                    kind = entry.get("kind")
                    record["kind"] = kind if kind in {"model", "bool", "number", "duration", "path", "url", "string", "list"} else "string"
                done[name] = record
        remaining = failed
        if not remaining:
            break
    for name in remaining:
        print(f"warning: no usable summary for {name}; published with facts and a link only", file=sys.stderr)
    return done


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---- main -------------------------------------------------------------------------

def load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", type=Path)
    args = parser.parse_args()

    sources = load_sources(args.offline)
    schema = sources["schema"]
    if not isinstance(schema.get("properties"), dict) or len(schema["properties"]) < 80:
        raise SourceError("the settings schema has too few properties; not publishing")
    schema_keys = set(schema["properties"])

    rows = env_rows(sources["env"])
    all_pages = sources["env"] + sources["models"] + sources["reference"]
    env_summaries = load_json(DATA / "env-summaries.json", {})
    key_summaries = load_json(DATA / "key-summaries.json", {})
    hashes = load_json(DATA / "source-hashes.json", {})
    aliases = load_json(DATA / "aliases.json", {})

    # New or reworded entries get new summaries.
    stale_env = {n: t for n, t in rows.items() if n not in env_summaries or (n in hashes and hashes[n] != digest(t))}
    index = settings_index(sources["reference"])
    key_rows = {key: entry["text"] for key, entry in index.items()}
    stale_keys = {
        n: t for n, t in key_rows.items()
        if n not in schema_keys and (n not in key_summaries or (("key:" + n) in hashes and hashes["key:" + n] != digest(t)))
    }
    if stale_env:
        print(f"summarising {len(stale_env)} environment variables", file=sys.stderr)
        for name, record in summarise(stale_env, env=True).items():
            env_summaries[name] = record
            hashes[name] = digest(rows[name])
    if stale_keys:
        print(f"summarising {len(stale_keys)} settings keys", file=sys.stderr)
        for name, record in summarise(stale_keys, env=False).items():
            key_summaries[name] = record
            hashes["key:" + name] = digest(key_rows[name])
    for name, text in rows.items():
        hashes.setdefault(name, digest(text))

    env = []
    for name, text in rows.items():
        entry = {"name": name, **env_summaries.get(name, {"kind": "string"}), **env_status(name, text, all_pages)}
        entry.setdefault("summary", None)
        env.append(entry)
    env.sort(key=lambda entry: entry["name"])

    definitions = {
        "format": FORMAT,
        "tools": {
            "claude-code": {
                "name": "Claude Code",
                "docs": {
                    "settings": DOCS + "settings",
                    "env": DOCS + "env-vars",
                    "models": DOCS + "model-config",
                },
                "settings": {
                    "schema": schema,
                    "keys": {
                        key: {
                            "topic": entry["topic"],
                            "scope": entry["scope"],
                            **({"summary": key_summaries[key]["summary"]} if key in key_summaries else {}),
                        }
                        for key, entry in index.items()
                    },
                    "sections": sections_for(index, topics_in_order(sources["reference"])),
                },
                "env": env,
                "models": models(sources["models"], aliases),
            }
        },
    }
    body = json.dumps(definitions, indent=1, ensure_ascii=False, sort_keys=True)
    previous = load_json(OUT, None)
    if previous is not None:
        previous_body = json.dumps({k: v for k, v in previous.items() if k != "generated"}, indent=1, ensure_ascii=False, sort_keys=True)
        if previous_body == body:
            print("no changes")
            return 0
    definitions["generated"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT.write_text(json.dumps(definitions, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    DATA.mkdir(exist_ok=True)
    (DATA / "env-summaries.json").write_text(json.dumps(env_summaries, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    (DATA / "key-summaries.json").write_text(json.dumps(key_summaries, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
    (DATA / "source-hashes.json").write_text(json.dumps(hashes, indent=1, sort_keys=True) + "\n")
    print(f"wrote definitions: {len(env)} environment variables, {len(index)} documented keys ({len(set(index) - schema_keys)} not in the schema), {len(schema_keys)} schema keys")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SourceError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
