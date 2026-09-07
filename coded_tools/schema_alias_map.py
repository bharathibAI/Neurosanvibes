# Copyright © 2025-2026 Cognizant Technology Solutions Corp, www.cognizant.com.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# END COPYRIGHT
"""
schema_alias_map: shared helpers for loading a JSON schema-alias definition
(BH_report_agent/data/schema_aliases.json) and using it to:

1. Build a description of the modules/tables/columns expressed purely in
   terms of *aliases* (plus human-readable explanations), suitable for
   handing to an LLM so it never sees the real underlying database, table,
   or column names.
2. Translate a SQL statement written against those aliases back into the
   real table/column names, so it can actually be executed.

This implements the "Alias mapping (code, not LLM)" step from the report
agent architecture: a *deterministic* rewrite that never involves an LLM.
The preferred rewrite engine is `sqlglot`, which walks the parsed SQL AST
and renames table/column nodes precisely (handling CTEs, subqueries, table
qualifiers and quoted identifiers). If `sqlglot` is not installed, the
module transparently falls back to a whole-word regex rewrite.

Expected schema-alias JSON shape (see data/schema_aliases.json):

    {
        "modules": [
            {"module_name": str, "module_alias": str, "description": str},
            ...
        ],
        "tables": [
            {
                "name": "<real_table_name>",
                "alias": "<table_alias>",
                "module": "<module_alias>",
                "row_count": int,
                "columns": [
                    {
                        "name": "<real_column_name>",
                        "alias": "<column_alias>",
                        "type": "TEXT" | "INTEGER" | ...,
                        "primary_key": bool,
                        "nullable": bool,
                        "foreign_key": "<table>.<column>"   # optional
                    },
                    ...
                ]
            },
            ...
        ]
    }

This module deliberately contains no CodedTool logic itself; it is imported
by both `schema_alias_tool.py` (exposes the alias view to the LLM) and
`sqlite_query_tool.py` (maps aliases back to real names before execution).
"""

import json
import os
import re
from logging import Logger
from logging import getLogger
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple

# Optional sqlglot import -- gracefully degrade to the regex engine when the
# package is not installed, so the tool still works in minimal environments.
try:
    import sqlglot
    import sqlglot.expressions as exp

    _SQLGLOT_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on environment
    _SQLGLOT_AVAILABLE = False

logger: Logger = getLogger(__name__)

DEFAULT_SCHEMA_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "schema_aliases.json")
)


def sqlglot_available() -> bool:
    """Return True if sqlglot is installed and the AST rewrite engine can be used."""
    return _SQLGLOT_AVAILABLE


def load_schema(schema_path: str = DEFAULT_SCHEMA_PATH) -> Dict[str, Any]:
    """
    Loads and validates the schema-alias JSON definition from disk.

    :param schema_path: Path to the schema-alias JSON file.
    :return: The parsed schema dictionary.
    :raises FileNotFoundError: If the schema file does not exist.
    :raises ValueError: If the file is not valid JSON or is missing required keys.
    """
    if not os.path.exists(schema_path):
        raise FileNotFoundError(f"Schema-alias definition not found at path: {schema_path}")

    try:
        with open(schema_path, "r", encoding="utf-8") as schema_file:
            schema: Dict[str, Any] = json.load(schema_file)
    except json.JSONDecodeError as decode_error:
        raise ValueError(f"Schema-alias file at {schema_path} is not valid JSON: {decode_error}") from decode_error

    tables = schema.get("tables")
    if not isinstance(tables, list):
        raise ValueError(
            f"Schema-alias file at {schema_path} must contain a top-level 'tables' list."
        )

    for index, table_def in enumerate(tables):
        if "name" not in table_def or "alias" not in table_def:
            raise ValueError(
                f"Table at index {index} in schema-alias file must define 'name' and 'alias'."
            )
        columns = table_def.get("columns")
        if not isinstance(columns, list):
            raise ValueError(
                f"Table '{table_def.get('name')}' in schema-alias file must define a 'columns' list."
            )
        for column_def in columns:
            if "name" not in column_def or "alias" not in column_def:
                raise ValueError(
                    f"Every column of table '{table_def.get('name')}' must define 'name' and 'alias'."
                )

    return schema


def build_llm_schema_view(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Builds an alias-only view of the schema (aliases + descriptions/metadata, no
    real table/column names) suitable for handing to an LLM so it can compose
    SQL using aliases exclusively.

    :param schema: The schema dictionary as returned by load_schema().
    :return: A dictionary of the form:
        {
            "modules": [{"alias": str, "description": str}, ...],
            "tables": [
                {
                    "alias": str,
                    "module": str,
                    "row_count": int,
                    "columns": [
                        {
                            "alias": str,
                            "type": str,
                            "primary_key": bool,
                            "nullable": bool,
                            "foreign_key": str,   # aliased "table_alias.col_alias"
                        },
                        ...
                    ]
                },
                ...
            ]
        }
    """
    modules_view: List[Dict[str, Any]] = [
        {
            "alias": module_def.get("module_alias") or module_def.get("module_name", ""),
            "description": module_def.get("description", ""),
        }
        for module_def in schema.get("modules", [])
    ]

    tables_view: List[Dict[str, Any]] = []
    for table_def in schema.get("tables", []):
        columns_view: List[Dict[str, Any]] = []
        for column_def in table_def.get("columns", []):
            column_view: Dict[str, Any] = {
                "alias": column_def["alias"],
                "type": column_def.get("type", "TEXT"),
                "primary_key": bool(column_def.get("primary_key", False)),
                "nullable": bool(column_def.get("nullable", True)),
            }
            foreign_key = column_def.get("foreign_key")
            if foreign_key:
                column_view["foreign_key"] = _foreign_key_to_alias(foreign_key, schema)
                join_hint = _build_join_hint(foreign_key, schema)
                if join_hint:
                    column_view["join_hint"] = join_hint
            columns_view.append(column_view)

        tables_view.append(
            {
                "alias": table_def["alias"],
                "module": table_def.get("module", ""),
                "row_count": table_def.get("row_count", 0),
                "display_columns": table_def.get("display_columns", []),
                "columns": columns_view,
            }
        )

    return {"modules": modules_view, "tables": tables_view}


def _foreign_key_to_alias(foreign_key: str, schema: Dict[str, Any]) -> str:
    """
    Converts a real "table.column" foreign-key reference into its aliased
    equivalent so the LLM only ever sees aliases.
    """
    if "." not in foreign_key:
        return foreign_key

    real_table, real_column = foreign_key.split(".", 1)
    table_alias = real_table
    column_alias = real_column

    for table_def in schema.get("tables", []):
        if table_def.get("name") == real_table:
            table_alias = table_def.get("alias", real_table)
            for column_def in table_def.get("columns", []):
                if column_def.get("name") == real_column:
                    column_alias = column_def.get("alias", real_column)
                    break
            break

    return f"{table_alias}.{column_alias}"


def _build_join_hint(foreign_key: str, schema: Dict[str, Any]) -> str:
    """
    Returns an actionable JOIN hint string (in alias terms) for an FK column.

    Format:
        "LEFT JOIN <ref_table_alias> ON <this_col> = <ref_table_alias>.<ref_col_alias>
         — fetch: <ref_table_alias>.<disp1>, <ref_table_alias>.<disp2>"

    Returns an empty string when the FK is self-referential (the column points
    back to the PK of its own table, which is a schema artefact, not a real FK).
    """
    if "." not in foreign_key:
        return ""

    real_ref_table, real_ref_col = foreign_key.split(".", 1)

    for table_def in schema.get("tables", []):
        if table_def.get("name") != real_ref_table:
            continue

        ref_table_alias = table_def.get("alias", real_ref_table)
        ref_col_alias = real_ref_col
        for col_def in table_def.get("columns", []):
            if col_def.get("name") == real_ref_col:
                ref_col_alias = col_def.get("alias", real_ref_col)
                break

        display_cols: List[str] = table_def.get("display_columns", [])
        join_clause = f"LEFT JOIN {ref_table_alias} ON <this_col> = {ref_table_alias}.{ref_col_alias}"
        if display_cols:
            fetch_part = ", ".join(f"{ref_table_alias}.{c}" for c in display_cols)
            return f"{join_clause} — fetch: {fetch_part}"
        return join_clause

    return ""


def filter_schema_by_query(schema: Dict[str, Any], user_query: str) -> Dict[str, Any]:
    """
    Returns a copy of *schema* containing only the tables (and their modules)
    that are relevant to *user_query*.

    Relevance is determined by keyword matching:
    - Table alias or real name contains a query keyword → table is included.
    - Module alias or module name contains a query keyword → all tables in that
      module are included.
    - Any column alias contains a query keyword → owning table is included.
    - Any table that is referenced via a foreign key from an already-included
      table is also pulled in so JOIN paths remain intact.

    If no table matches at all the full schema is returned unchanged (safe
    fallback so the LLM is never left without context).
    """
    keywords = {w.lower() for w in re.split(r"\W+", user_query) if len(w) > 2}
    if not keywords:
        return schema

    # Build a set of module aliases that match any keyword.
    matched_modules: set = set()
    for module_def in schema.get("modules", []):
        module_text = " ".join([
            module_def.get("module_alias", ""),
            module_def.get("module_name", ""),
            module_def.get("description", ""),
        ]).lower()
        if any(kw in module_text for kw in keywords):
            matched_modules.add(module_def.get("module_alias", "").upper())

    # First pass: identify directly matched tables.
    matched_aliases: set = set()
    for table_def in schema.get("tables", []):
        if table_def.get("module", "").upper() in matched_modules:
            matched_aliases.add(table_def["alias"])
            continue
        table_text = " ".join([
            table_def.get("alias", ""),
            table_def.get("name", ""),
        ]).lower()
        if any(kw in table_text for kw in keywords):
            matched_aliases.add(table_def["alias"])
            continue
        for column_def in table_def.get("columns", []):
            col_text = column_def.get("alias", "").lower()
            if any(kw in col_text for kw in keywords):
                matched_aliases.add(table_def["alias"])
                break

    if not matched_aliases:
        return schema  # no match — return full schema as fallback

    # Second pass: add FK-referenced tables so JOIN paths stay intact.
    alias_to_real: Dict[str, str] = {t["alias"]: t["name"] for t in schema.get("tables", [])}
    real_to_def: Dict[str, Any] = {t["name"]: t for t in schema.get("tables", [])}
    real_to_alias: Dict[str, str] = {t["name"]: t["alias"] for t in schema.get("tables", [])}

    tables_to_include = set(matched_aliases)
    for table_alias in list(matched_aliases):
        real_name = alias_to_real.get(table_alias)
        if not real_name:
            continue
        for col_def in real_to_def.get(real_name, {}).get("columns", []):
            fk = col_def.get("foreign_key", "")
            if "." in fk:
                fk_table_real = fk.split(".")[0]
                fk_table_alias = real_to_alias.get(fk_table_real)
                if fk_table_alias:
                    tables_to_include.add(fk_table_alias)

    # Build filtered schema.
    filtered_tables = [t for t in schema.get("tables", []) if t["alias"] in tables_to_include]
    included_modules = {t.get("module", "") for t in filtered_tables}
    filtered_modules = [m for m in schema.get("modules", []) if m.get("module_alias", "") in included_modules]

    return {"modules": filtered_modules, "tables": filtered_tables}


def build_table_alias_lookup(schema: Dict[str, Any]) -> Dict[str, str]:
    """Builds a case-insensitive lookup of table alias -> real table name."""
    lookup: Dict[str, str] = {}
    for table_def in schema.get("tables", []):
        alias = table_def["alias"].lower()
        lookup[alias] = table_def["name"]
    return lookup


def build_column_alias_lookup(schema: Dict[str, Any]) -> Dict[Tuple[str, str], str]:
    """
    Builds a lookup of (real_table_name, column_alias_lower) -> real column name,
    so column aliases can be resolved with table context.
    """
    lookup: Dict[Tuple[str, str], str] = {}
    for table_def in schema.get("tables", []):
        real_table = table_def["name"]
        for column_def in table_def.get("columns", []):
            lookup[(real_table, column_def["alias"].lower())] = column_def["name"]
    return lookup


def build_global_column_lookup(schema: Dict[str, Any]) -> Dict[str, str]:
    """
    Builds a flat, case-insensitive lookup of column alias -> real column name
    for unqualified columns (no table prefix). On alias collisions the last
    definition wins, matching the resolution used when a column is unqualified.
    """
    lookup: Dict[str, str] = {}
    for table_def in schema.get("tables", []):
        for column_def in table_def.get("columns", []):
            lookup[column_def["alias"].lower()] = column_def["name"]
    return lookup


def translate_sql_aliases(sql: str, schema: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """
    Translates a SQL statement written entirely in terms of schema aliases
    (table and column aliases) into the equivalent SQL using the real
    underlying table/column names.

    Uses the sqlglot AST engine when available (deterministic, qualifier-aware
    rewrite), otherwise falls back to a whole-word regex rewrite.

    :param sql: SQL statement composed using aliases from build_llm_schema_view().
    :param schema: The schema dictionary as returned by load_schema().
    :return: A tuple of (translated_sql, alias_to_real_map_used) where
             alias_to_real_map_used only contains the aliases actually found
             in the statement (for logging/audit purposes).
    :raises ValueError: If the SQL references no known aliases at all, which
            most likely indicates the LLM used real names instead of aliases,
            or a typo'd alias.
    """
    if _SQLGLOT_AVAILABLE:
        try:
            return _translate_sql_aliases_sqlglot(sql, schema)
        except ValueError:
            raise
        except Exception as sqlglot_error:  # pylint: disable=broad-except
            logger.warning(
                "sqlglot alias rewrite failed (%s) — falling back to regex engine.", sqlglot_error
            )

    return _translate_sql_aliases_regex(sql, schema)


def _translate_sql_aliases_sqlglot(sql: str, schema: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """Deterministic alias -> real-name rewrite using the sqlglot AST engine."""
    table_lookup = build_table_alias_lookup(schema)
    column_lookup = build_column_alias_lookup(schema)
    global_column_lookup = build_global_column_lookup(schema)

    tree = sqlglot.parse_one(sql, dialect="sqlite")
    aliases_used: Dict[str, str] = {}

    # Pass 1: map query-level aliases (e.g. "FROM employees AS e") to real tables.
    query_alias_to_real: Dict[str, str] = {}
    for table_expr in tree.find_all(exp.Table):
        alias_name = table_expr.name
        real_table = table_lookup.get(alias_name.lower())
        sql_alias = table_expr.alias
        if sql_alias and real_table:
            query_alias_to_real[sql_alias.lower()] = real_table

    # Pass 2: rename table nodes alias -> real name.
    for table_expr in tree.find_all(exp.Table):
        alias_name = table_expr.name
        real_table = table_lookup.get(alias_name.lower())
        if real_table and real_table != alias_name:
            aliases_used[alias_name] = real_table
            table_expr.set("this", exp.to_identifier(real_table))

    # Pass 3: rename column nodes alias -> real name (with table context).
    for column_expr in tree.find_all(exp.Column):
        column_alias = column_expr.name
        qualifier = column_expr.table  # table/alias qualifier, may be ""

        real_column = None
        if qualifier:
            if qualifier.lower() in query_alias_to_real:
                real_table = query_alias_to_real[qualifier.lower()]
            else:
                real_table = table_lookup.get(qualifier.lower(), qualifier)
            real_column = column_lookup.get((real_table, column_alias.lower()))

        if real_column is None:
            real_column = global_column_lookup.get(column_alias.lower())

        if real_column and real_column != column_alias:
            aliases_used[column_alias] = real_column
            column_expr.set("this", exp.to_identifier(real_column))

    if not aliases_used:
        raise ValueError(
            "SQL does not reference any known table/field aliases. "
            "The SQL must be composed using the aliases provided by schema_alias_tool, "
            "not the real database/table/column names."
        )

    translated_sql = tree.sql(dialect="sqlite")
    logger.info("Alias translation (sqlglot) — %d alias(es) resolved: %s", len(aliases_used), aliases_used)
    return translated_sql, aliases_used


def _translate_sql_aliases_regex(sql: str, schema: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    """Whole-word regex alias -> real-name rewrite, used when sqlglot is absent."""
    lookup: Dict[str, str] = {}
    lookup.update(build_table_alias_lookup(schema))
    lookup.update(build_global_column_lookup(schema))

    # Replace longer aliases first so a shorter alias that is a prefix of a
    # longer one cannot partially match/shadow it.
    aliases_by_length = sorted(lookup.keys(), key=len, reverse=True)

    translated_sql = sql
    aliases_used: Dict[str, str] = {}

    for alias in aliases_by_length:
        real_name = lookup[alias]
        if alias == real_name.lower():
            # Alias equals the real name -- nothing to rewrite.
            continue
        pattern = re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE)
        if pattern.search(translated_sql):
            aliases_used[alias] = real_name
            translated_sql = pattern.sub(real_name, translated_sql)

    if not aliases_used:
        raise ValueError(
            "SQL does not reference any known table/field aliases. "
            "The SQL must be composed using the aliases provided by schema_alias_tool, "
            "not the real database/table/column names."
        )

    logger.info("Alias translation (regex) — %d alias(es) resolved: %s", len(aliases_used), aliases_used)
    return translated_sql, aliases_used
