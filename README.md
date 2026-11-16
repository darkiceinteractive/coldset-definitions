# Coldset definitions

What the [Coldset](https://github.com/darkiceinteractive) editor knows about agent tool settings: every documented Claude Code settings key and environment variable, what each does in a sentence, which are deprecated or removed and what replaces them, which model IDs are current, and how the docs group the settings.

Coldset ships with a copy and checks this repository for a newer one at most once a day. The file is data only.

## The file

`definitions.json`, format 1:

| Path | What it holds |
| --- | --- |
| `tools.claude-code.settings.schema` | The published settings schema: types, allowed values, limits |
| `tools.claude-code.settings.keys` | Every documented key: its topic, which files it may go in, a summary when the schema has none |
| `tools.claude-code.settings.sections` | The docs' topics, in order, with their keys |
| `tools.claude-code.env` | Every documented environment variable: summary, kind of value, `deprecated` (with its replacement), `removed`, `superseded_by` |
| `tools.claude-code.models` | The newest model in each family, the model IDs the docs name, and the aliases |
| `tools.claude-code.docs` | Links to the pages each part comes from |

## How it is kept up to date

Every night `build.py` reads the official docs pages and the published schema, and writes `definitions.json` again. A source that does not look the way it should stops the run, so a broken night never replaces a good file. When nothing changed, nothing is committed.

Descriptions are short and in our own words, kept in `data/`. Entries that are new or whose official wording changed get a new description drafted through a summarisation API (keys in the repository's secrets); a draft that repeats the docs' wording is refused, and the entry is published with its facts and a link until a good one is written.

To run it by hand:

```bash
python3 build.py
```

## Licence

The scripts and the descriptions in `data/` are MIT licensed. The settings schema inside `definitions.json` comes from [SchemaStore](https://github.com/SchemaStore/schemastore) and is under the Apache License 2.0; see `NOTICE`.
