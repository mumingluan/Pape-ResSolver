# Pape-ResSolver

`Pape-ResSolver` converts normalized Pape resource dumps into server-friendly,
human-readable configuration data while preserving provenance. It is designed
around the resource manifests rather than fixed package IDs, so the same code
can be reused across resource versions.

The project has three complementary outputs:

- configuration Lua (`LuaCfg.*`) is statically evaluated and normalized with
  its `_k` schema into JSONL and SQLite;
- logic Lua is indexed by resolved source name, dependencies, RPC references,
  configuration references and content hash.
- decoded MessagePack, text, X3 and auxiliary config artifacts are cataloged,
  materialized and consolidated into server-friendly package JSONL files.
- catalog multilingual resource sets are reconstructed as ID-addressable text
  in a separate `languages.sqlite`, independent from the main Res database.

The Lua reader does **not** execute game code. It evaluates the restricted data
construction subset emitted by the existing Lua 5.3 decompiler, preserving
integers and representing unsupported expressions explicitly.

## Quick start

```powershell
python -m pape_res_solver extract `
  D:\path\to\normalized-resources `
  --output .\out\resource-version `
  --export-scripts useful `
  --materialize-tables hardlink
```

Useful focused run while developing:

```powershell
python -m pape_res_solver extract D:\path\to\normalized-resources `
  --output .\out\gacha `
  --table CardBaseInfo --table Item --table GachaAll `
  --table GachaGroup --table GachaRule --table GachaDrop
```

Output layout:

```text
out/<version>/
  catalog.json
  resources.sqlite
  languages.sqlite
  tables/*.jsonl
  schemas/*.json
  scripts/catalog.jsonl
  scripts/**/*.lua
  server_tables/msgpack/*.jsonl
  server_tables/config/*.jsonl
  artifacts/tables/**
  reports/parse_failures.json
  reports/unresolved_values.json
  reports/references.json
  reports/lua_index.json
  reports/artifacts.json
```

Every catalog entry records the source package, entry index, source path,
content hash, row count and schema fingerprint. JSONL rows remain clean config
objects; provenance is available in the catalog and SQLite metadata.

## Extraction model

The source manifest's resolved names are authoritative. Package IDs and entry
numbers are never hard-coded. If the next resource version moves `LuaCfg.Item`
to another package, the output remains `tables/Item.jsonl` and its new source is
recorded in `catalog.json`.

The extractor loads standard Lua 5.3 bytecode in a restricted environment.
Android chunks in the current dump use 32-bit `size_t`; the bytecode converter
rewrites only binary chunk string-size fields for the local 64-bit Lua 5.3
runtime. This preserves Lua integers and corrects `SETLIST` ordering artifacts
that are present in decompiled source. The static source parser remains a
diagnostic fallback.

Unity value constructors such as `Vector2`, `Vector3`, `Quaternion` and `Color`
become tagged JSON objects. No filesystem, network, process, package or debug
APIs are exposed to resource chunks.

## SQLite API

`resources.sqlite` contains stable generic tables suitable for a Go or Python
game server:

- `config_tables` and `config_rows`: named `LuaCfg` tables plus decoded X3
  runtime tables and their JSON rows;
- `config_references`: validated Card/Item/Gacha relationships;
- `lua_scripts` and `lua_dependencies`: source, require, config and RPC index;
- `resource_packages` and `resource_files`: normalized resource inventory;
- `decoded_table_rows`: consolidated MessagePack/X3/auxiliary config rows.

Example:

```sql
select json_extract(data_json, '$.FragmentID')
from config_rows
where table_name = 'CardBaseInfo' and row_key = '121130';
```

The CLI provides the same common lookup without writing SQL:

```powershell
python -m pape_res_solver query .\out\1.7.1546 CardBaseInfo 121130
python -m pape_res_solver find-id .\out\1.7.1546 121130
python -m pape_res_solver text .\out\1.7.1546 377066
python -m pape_res_solver verify .\out\1.7.1546
```

## Multilanguage SQLite

`languages.sqlite` is rebuilt atomically from Get's
`multilanguage/manifest.json`. It does not add rows to `resources.sqlite` or
the BOOI-trimmed database. Its stable runtime tables are:

- `language_resource_sets`: every catalog language resource set and its counts;
- `language_packages`: NX/NXF provenance, key type and dependency packages;
- `localized_text`: `(resource_set_id, text_id) -> UTF-8 text`;
- `language_metadata`: schema and source-version information.

The resource-set ID is the authoritative cross-version language key. This
avoids guessing locale names when a regional client publishes only a subset of
text or voice languages. Servers may attach their own display labels while
keeping lookups stable:

```sql
select text from localized_text
where resource_set_id = 1000000000001 and text_id = 377066;
```

Get downloads the small `Total/XFileZip/*.zip` members already present in its
normal pipeline; it does not need the much larger `Packages/b_*.zip` image and
voice aggregates. Incremental Get runs reuse the ordinary download cache and
regenerate a full resource-set manifest after normalization.

Solver extraction is incremental by default when the output directory already
contains its marker and catalog. Lua configuration rows whose source SHA-256 is
unchanged are reused in place; changed and retired tables are removed before
replacement. The language database compares resource-set package fingerprints
and reuses all text rows when only the game version metadata changed. Use
`--no-incremental` for a deliberate full rebuild, or `--clean` to recreate the
entire output directory.

Config-name recovery scans only Get packages marked as rebuilt. Logic-script
facts are cached by bytecode SHA-256, including dependencies, config/RPC
references and exported source paths. Artifact hardlinks retain their stored
hashes, and the large MessagePack/X3 tree is reused as a unit whenever Get's
per-package table fingerprints are unchanged.

## Compact runtime SQLite

The full database also contains Lua indexes, artifact inventory, and decoded
package research data. Create a smaller server runtime database while keeping
all named configuration tables:

```powershell
python -m pape_res_solver trim `
  .\out\1.7.1546\resources.sqlite `
  .\out\1.7.1546\booi-res.sqlite `
  --resource-version 1.7.1546
```

Use repeatable `--table` arguments to retain only an explicit allowlist. For
example:

```powershell
python -m pape_res_solver trim resources.sqlite booi-res.sqlite `
  --table Task --table Item --table ItemType `
  --table CardBaseInfo --table CardRare `
  --table GachaAll --table GachaGroup --table GachaRule --table GachaDrop
```

The compact database contains `resource_metadata`, `config_tables`,
`config_rows`, and validated `config_references`. Tables use `WITHOUT ROWID`
where appropriate. Analysis-only indexes and decoded artifacts are omitted.
Decoded X3 record containers are promoted with stable `X3<TableName>` names
(for example `X3WeaponLogicConfigs`, `X3WeaponSkinConfigs`, and
`X3ActorCfgs`), so semantic battle configuration survives trimming without
shipping raw MessagePack packages.
The output is built into a temporary file, integrity checked, and atomically
renamed. Existing outputs are preserved unless `--force` is passed.

## Automatic LuaCfg name recovery

The solver does not require compatibility rows such as `Recovered.*` for
stripped or hotfix configuration chunks. Before extraction it scans game Lua
calls for authoritative `LuaCfgMgr` registry names and resolves chunks using:

1. an exact XLua CRC match for ordinary `LuaCfg.<Table>` chunks; or
2. a unique, reciprocal match between fields used by game code and the schema
   returned by an executable configuration chunk when a hotfix uses a physical
   name different from its registry name.

Only evidence-backed unique matches are accepted. Ambiguous candidates remain
unresolved and are listed in `reports/config_name_resolution.json`; names are
never guessed. The recovered canonical table names flow into JSONL, SQLite,
Lua indexing, verification, and compact runtime SQLite outputs automatically.

## Materialization modes

- `hardlink` (default): self-contained output on the same volume without
  duplicating physical table bytes; falls back to copying when unsupported.
- `copy`: ordinary independent copies.
- `none`: catalog and consolidated outputs only.

`--clean` only removes directories containing Pape-ResSolver's output marker.
It refuses to recursively remove an arbitrary unmarked directory.

## Repository hygiene

Recovered resource inputs and generated outputs are intentionally excluded from
Git. Keep them in `resources/`, `source/`, `input/` or `out/`, or outside the
repository entirely. Only the original solver source code, tests and
documentation are intended for publication under MIT; see `NOTICE` for the
third-party data boundary.
