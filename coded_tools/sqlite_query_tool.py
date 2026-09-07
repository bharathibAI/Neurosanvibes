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
SqliteQueryTool: a CodedTool that stands in for the "MCP server" step in the
report-generation architecture. In production this would call out to an MCP
server that exposes a SQLite database as a tool; here it directly executes a
validated, read-only SQL statement against a local SQLite database file and
returns the resulting rows.

The SQL it receives is expected to be composed entirely in terms of the
alias names returned by schema_alias_tool (SchemaAliasTool), never the real
database/table/column names. Before validation and execution, the SQL is
translated from aliases back to real names via schema_alias_map so the LLM-
generated statement can actually run against the underlying SQLite database.
"""

import os
import re
import sqlite3
from logging import Logger
from logging import getLogger
from typing import Any
from typing import Dict
from typing import List
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from .schema_alias_map import DEFAULT_SCHEMA_PATH
from .schema_alias_map import load_schema
from .schema_alias_map import translate_sql_aliases

# Only SELECT statements are allowed through this tool. This is a defense-in-depth
# measure in addition to the upstream "security_and_compliance_agent" checks.
_ALLOWED_STATEMENT_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)

# Statement fragments that should never appear in a report query, even inside a
# SELECT (e.g. attached-database tricks, PRAGMA abuse, multi-statement injection).
_FORBIDDEN_KEYWORDS = (
    "ATTACH",
    "PRAGMA",
    "DROP",
    "DELETE",
    "INSERT",
    "UPDATE",
    "ALTER",
    "CREATE",
    "--",
    ";",
)

_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "company_data.db")
_DEFAULT_ROW_LIMIT = 1000


class SqliteQueryTool(CodedTool):
    """
    CodedTool that executes a read-only SQL query against a SQLite database
    and returns the result rows, simulating an MCP server exposing SQLite
    as a tool.
    """

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        Translates an alias-based SQL statement back to real table/column
        names, validates it, and executes it against the configured SQLite
        database.

        :param args: A dictionary with keys:
                "sql": the SQL SELECT statement to execute, composed using the
                    aliases returned by schema_alias_tool (not real names).
                "db_path": (optional) path to the SQLite database file. Defaults to
                    BH_report_agent/data/company_data.db.
                "schema_path": (optional) path to the schema-alias JSON file used
                    to map aliases back to real names. Defaults to
                    BH_report_agent/data/schema_aliases.json.
                "row_limit": (optional) maximum number of rows to return. Defaults to 1000.

        :param sly_data: A dictionary containing parameters that should be kept out
                of the chat stream. Not used by this tool.

        :return: On success, a dict with keys "columns" (List[str], real column
                 names), "rows" (List[List[Any]]), and "resolved_sql" (the SQL
                 actually executed, after alias translation). On failure, a dict
                 with key "error" describing the problem so the calling agent
                 can react gracefully.
        """
        tool_name: str = self.__class__.__name__
        logger: Logger = getLogger(self.__class__.__name__)

        logger.info("========== [START] %s ==========", tool_name)

        alias_sql: str = str(args.get("sql", "")).strip()
        db_path: str = args.get("db_path") or _DEFAULT_DB_PATH
        schema_path: str = args.get("schema_path") or DEFAULT_SCHEMA_PATH
        row_limit: int = int(args.get("row_limit") or _DEFAULT_ROW_LIMIT)

        logger.info("[STEP 1] Alias SQL received from LLM:")
        logger.info("         %s", alias_sql)
        logger.info("[STEP 1] db_path=%s  schema_path=%s  row_limit=%d", db_path, schema_path, row_limit)

        try:
            if not alias_sql:
                raise ValueError("No SQL statement was provided.")

            logger.info("[STEP 2] Loading schema aliases from: %s", schema_path)
            schema = load_schema(schema_path)
            logger.info("[STEP 2] Schema loaded — %d table(s)", len(schema.get("tables", [])))

            logger.info("[STEP 3] Translating alias SQL → real column/table names")
            resolved_sql, aliases_used = translate_sql_aliases(alias_sql, schema)
            logger.info("[STEP 3] Alias → real mapping (%d): %s", len(aliases_used), aliases_used)
            logger.info("[STEP 3] Resolved SQL:")
            logger.info("         %s", resolved_sql)

            logger.info("[STEP 4] Validating SQL (SELECT-only, no forbidden keywords)")
            self._validate_sql(resolved_sql, logger, tool_name)
            logger.info("[STEP 4] Validation passed")

            if not os.path.exists(db_path):
                message = f"SQLite database not found at path: {db_path}"
                logger.error("[ERROR] %s: %s", tool_name, message)
                return {"error": message}

            logger.info("[STEP 5] Executing query against: %s", db_path)
            columns, rows = self._execute_query(db_path, resolved_sql, row_limit, logger, tool_name)
            logger.info(
                "[STEP 6] Query result — %d row(s), %d column(s): %s",
                len(rows), len(columns), columns,
            )
            logger.info("========== [DONE] %s ==========", tool_name)
            return {"columns": columns, "rows": rows, "resolved_sql": resolved_sql}

        except FileNotFoundError as not_found_error:
            logger.error("[ERROR] %s: schema file not found — %s", tool_name, not_found_error)
            return {"error": str(not_found_error)}

        except ValueError as validation_error:
            logger.error("[ERROR] %s: validation/alias error — %s", tool_name, validation_error)
            return {"error": f"Query rejected: {validation_error}"}

        except sqlite3.Error as db_error:
            logger.exception("[ERROR] %s: SQLite error", tool_name)
            return {"error": f"Database error: {db_error}"}

        except Exception as unexpected_error:  # pylint: disable=broad-except
            logger.exception("[ERROR] %s: unexpected error", tool_name)
            return {"error": f"Unexpected error while querying database: {unexpected_error}"}

    @staticmethod
    def _validate_sql(sql: str, logger: Logger, tool_name: str) -> None:
        """
        Validates that the given SQL is a single, read-only SELECT statement.

        :param sql: The SQL text to validate.
        :param logger: Logger to record validation activity.
        :param tool_name: Name of the calling tool, for log messages.
        :raises ValueError: If the SQL is empty, not a SELECT, or contains
                forbidden keywords/characters.
        """
        if not sql:
            raise ValueError("No SQL statement was provided.")

        if not _ALLOWED_STATEMENT_RE.match(sql):
            raise ValueError("Only SELECT statements are permitted.")

        upper_sql = sql.upper()
        for keyword in _FORBIDDEN_KEYWORDS:
            if keyword in ("--", ";"):
                # Special characters — simple substring check is correct
                if keyword in sql:
                    raise ValueError(f"Statement contains a forbidden keyword/character: '{keyword}'.")
            else:
                # Word-boundary match to avoid false positives on column names like CREATED_AT
                if re.search(r"\b" + re.escape(keyword) + r"\b", upper_sql):
                    raise ValueError(f"Statement contains a forbidden keyword/character: '{keyword}'.")

        logger.info("         %s: SQL passed all validation checks.", tool_name)

    @staticmethod
    def _execute_query(
        db_path: str, sql: str, row_limit: int, logger: Logger, tool_name: str
    ) -> "tuple[List[str], List[List[Any]]]":
        """
        Opens the SQLite database, executes the query, and returns column names
        and row data, always closing the connection afterward.

        :param db_path: Path to the SQLite database file.
        :param sql: The validated SELECT statement to run.
        :param row_limit: Maximum number of rows to fetch.
        :param logger: Logger to record execution activity.
        :param tool_name: Name of the calling tool, for log messages.
        :return: A tuple of (column_names, rows).
        """
        connection = sqlite3.connect(db_path)
        try:
            cursor = connection.cursor()
            logger.info("         %s: executing SQL against DB", tool_name)
            cursor.execute(sql)

            columns = [description[0] for description in cursor.description or []]
            rows = [list(row) for row in cursor.fetchmany(row_limit)]
            return columns, rows
        finally:
            connection.close()
