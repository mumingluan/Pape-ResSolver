# Pape-ResSolver

`Pape-ResSolver` 将规范化后的 Pape 资源转换为适合服务端使用、可读且保留来源信息的配置数据。程序依赖资源清单，不把包 ID 写死，因此可以跨资源版本复用。

它生成四类产物：

- 对 `LuaCfg.*` 配置 Lua 做静态求值，依据 `_k` 字段表导出 JSONL 和 SQLite；
- 为逻辑 Lua 建立来源名、依赖、RPC、配置引用和内容哈希索引；
- 整理 MessagePack、文本、X3 和其他配置资源，生成服务端可读取的 JSONL；
- 根据 ResGet 的多语言资源清单生成独立的 `languages.sqlite`。

Lua 读取器不会执行游戏代码，只处理现有 Lua 5.3 反编译器输出的受限数据构造语法。无法静态表示的表达式会被明确记录，不会被猜测成普通数值。

## 环境与快速开始

需要 Python 3.11 及以上版本。部分 Lua 反编译流程还需要 Java 17 及以上版本。

```powershell
python -m pape_res_solver extract `
  D:\path\to\normalized-resources `
  --output .\out\resource-version `
  --export-scripts useful `
  --materialize-tables hardlink
```

开发时可以只提取指定表：

```powershell
python -m pape_res_solver extract D:\path\to\normalized-resources `
  --output .\out\gacha `
  --table CardBaseInfo --table Item --table GachaAll `
  --table GachaGroup --table GachaRule --table GachaDrop
```

典型输出结构：

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

每条配置目录记录来源包、条目序号、源码路径、内容哈希、行数和 schema 指纹。JSONL 保持为干净的配置对象，来源信息保存在 catalog 和 SQLite 元数据中。

## 提取规则

资源清单中的正式名称是唯一依据，程序不会根据包 ID 或条目序号猜表名。如果下一版本把 `LuaCfg.Item` 移到其他包，输出仍然是 `tables/Item.jsonl`，新的来源会写入 `catalog.json`。

当前 Android Lua 字节码使用 32 位 `size_t`。转换器只调整二进制 chunk 的字符串长度字段，以便本地 64 位 Lua 5.3 运行时读取，同时保留 Lua 整数和 `SETLIST` 顺序。`Vector2`、`Vector3`、`Quaternion`、`Color` 等 Unity 构造器会转换为带标签的 JSON 对象。配置 chunk 无权访问文件、网络、进程、包或 debug API。

## SQLite 数据库

`resources.sqlite` 包含 `config_tables`、`config_rows`、`config_references`、`lua_scripts`、`lua_dependencies`、`resource_packages`、`resource_files` 和 `decoded_table_rows` 等稳定表。

```sql
select json_extract(data_json, '$.FragmentID')
from config_rows
where table_name = 'CardBaseInfo' and row_key = '121130';
```

也可以直接查询：

```powershell
python -m pape_res_solver query .\out\1.7.1546 CardBaseInfo 121130
python -m pape_res_solver find-id .\out\1.7.1546 121130
python -m pape_res_solver text .\out\1.7.1546 377066
python -m pape_res_solver verify .\out\1.7.1546
```

## 多语言数据库

`languages.sqlite` 根据 ResGet 的 `multilanguage/manifest.json` 原子重建，不向 `resources.sqlite` 或 BOOI 紧凑库写入文本镜像。主要表包括 `language_resource_sets`、`language_packages`、`localized_text` 和 `language_metadata`。

资源集 ID 是跨版本语言查找的稳定键：

```sql
select text from localized_text
where resource_set_id = 1000000000001 and text_id = 377066;
```

Solver 默认增量提取。未变化的 Lua、MessagePack、X3 和文本输出会复用；删除的包会同步清理。需要完整重建时使用 `--no-incremental` 或 `--clean`。

## BOOI 紧凑 SQLite

使用维护好的 `booi` 预设可以生成 BOOI 运行时数据库：

```powershell
python -m pape_res_solver trim `
  .\out\1.7.1546\resources.sqlite `
  .\out\1.7.1546\booi-res.sqlite `
  --preset booi `
  --resource-version 1.7.1546
```

也可以显式指定表：

```powershell
python -m pape_res_solver trim resources.sqlite booi-res.sqlite `
  --table Task --table Item --table ItemType `
  --table CardBaseInfo --table CardRare `
  --table GachaAll --table GachaGroup --table GachaRule --table GachaDrop
```

输出会先写入临时文件，完成完整性检查后再原子替换。使用 `--force` 才会覆盖已有输出。X3 战斗配置会提升为稳定的 `X3<TableName>`，例如 `X3WeaponLogicConfigs`、`X3WeaponSkinConfigs` 和 `X3ActorCfgs`。

解析阶段还会处理两个已知客户端二进制资源：

- `config/DBCfg/DirtyWords.db` 会还原为 `server_tables/config/DirtyWords.sqlite`，并导出 `DirtyWords.jsonl`；
- `config/XFileZip/210201614.bin` 会导出为 `XFileZip_210201614.jsonl`。

两个解析器都会校验记录数量，并把结果写入 `reports/artifacts.json`。尚未识别的 `MultiLanguagePackageManiFest.bin` 会继续列在未解析报告中。

## 客户端 AppKey 修补

```powershell
python -m pape_res_solver patch-app-key `
  D:\path\to\XFileZip\2530387745.zip `
  .\patched\2530387745.zip `
  --old-app-key old-app-key-here `
  --new-app-key new-app-key-here `
  --nx-output .\patched\2530387745.nx
```

新旧 key 必须拥有相同的 ASCII 字节长度。程序默认要求恰好匹配一次，会保留未修改内容，并在输出后重新读取验证。不同资源版本的包 ID 可能不同，也可以直接传入 XFileZip 目录或规范化资源根目录让程序按内容查找。

## LuaCfg 表名恢复

支持 `_k` 通过中间变量赋值的反编译结果。对 `_useBinary` 主表，按客户端 CfgHelper 的 `LuaCfg.LuaSplitFils.{table}.cfg_{table}_{key}` 规则查找片段并按主表 schema 合并；缺片、哈希冲突和 ID 不一致会报告提取失败。目录记录片段数量及依赖指纹，分片变化会使主表增量缓存失效。

Solver 会从 Lua 的 `LuaCfgMgr` 调用点提取正式注册名，并使用精确的 XLua CRC，或游戏字段访问与配置 schema 的唯一互证，恢复被裁剪或热修表的名称。

无法形成唯一证据的候选会保留为未解析项，写入 `reports/config_name_resolution.json`，不会生成 `Recovered.*` 猜测表。

## 物化方式

- `hardlink`（默认）：同卷时使用硬链接，失败后复制；
- `copy`：生成独立副本；
- `none`：只生成 catalog 和汇总产物。

`--clean` 只删除带有 Pape-ResSolver 输出标记的目录，不会递归删除任意未标记目录。

## 仓库边界

资源输入和生成目录不会提交到 Git。它们应放在 `resources/`、`source/`、`input/`、`out/` 或仓库外。仓库只发布工具源码、测试和文档，遵循 MIT License；第三方组件和数据边界见 `NOTICE`。
