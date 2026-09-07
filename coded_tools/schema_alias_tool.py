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
SchemaAliasTool: the "alias context builder" CodedTool. It loads the JSON
schema-alias definition (BH_report_agent/data/schema_aliases.json) and returns
an alias-only view (alias + metadata for the modules, tables, and columns) for
the SQL generator agent to compose SQL against. The LLM never sees the real
database/table/column names -- only aliases -- which are later mapped back to
the real names by sqlite_query_tool (a deterministic sqlglot rewrite) before
execution.
"""

from logging import Logger
from logging import getLogger
from typing import Any
from typing import Dict
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from .schema_alias_map import DEFAULT_SCHEMA_PATH
from .schema_alias_map import build_llm_schema_view
from .schema_alias_map import filter_schema_by_query
from .schema_alias_map import load_schema


class SchemaAliasTool(CodedTool):
    """
    CodedTool that returns the alias + description view of the reporting
    database schema, so downstream agents can compose SQL using aliases
    only.
    """

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        Loads the schema-alias JSON definition and returns the alias-only view.

        :param args: A dictionary with keys:
                "schema_path": (optional) path to the schema-alias JSON file.
                    Defaults to BH_report_agent/data/schema_aliases.json.
                "user_query": (optional) natural-language request from the user.
                    When provided, only tables relevant to the query are included
                    in the returned view (plus any FK-referenced tables required
                    for JOINs). Falls back to the full schema when omitted or
                    when no tables match.

        :param sly_data: A dictionary containing parameters that should be kept out
                of the chat stream. Not used by this tool.

        :return: On success, a dict describing the database/tables/fields purely
                 in terms of aliases and descriptions (see
                 schema_alias_map.build_llm_schema_view). On failure, a dict with
                 key "error" describing the problem.
        """
        tool_name: str = self.__class__.__name__
        logger: Logger = getLogger(self.__class__.__name__)

        logger.info("========== [START] %s ==========", tool_name)

        schema_path: str = args.get("schema_path") or DEFAULT_SCHEMA_PATH
        user_query: str = args.get("user_query", "")
        logger.info("[STEP 1] Loading schema from: %s", schema_path)

        try:
            schema = load_schema(schema_path)
            all_table_aliases = [t.get("alias", t.get("name", "?")) for t in schema.get("tables", [])]
            module_aliases = [m.get("module_alias", m.get("module_name", "?")) for m in schema.get("modules", [])]
            logger.info(
                "[STEP 2] Schema loaded — %d table(s): %s | %d module(s): %s",
                len(all_table_aliases), all_table_aliases,
                len(module_aliases), module_aliases,
            )

            if user_query:
                logger.info("[STEP 2b] Filtering tables relevant to query: %r", user_query)
                schema = filter_schema_by_query(schema, user_query)
                filtered_aliases = [t.get("alias", "?") for t in schema.get("tables", [])]
                logger.info(
                    "[STEP 2b] Filtered to %d table(s): %s",
                    len(filtered_aliases), filtered_aliases,
                )

            logger.info("[STEP 3] Building alias-only LLM view (real names stripped)")
            schema_view = build_llm_schema_view(schema)
            col_count = sum(len(t.get("columns", [])) for t in schema_view.get("tables", []))
            logger.info(
                "[STEP 4] LLM view ready — %d table(s), %d column(s) total",
                len(schema_view.get("tables", [])), col_count,
            )
            logger.info("========== [DONE] %s ==========", tool_name)
            return schema_view

        except FileNotFoundError as not_found_error:
            logger.error("[ERROR] %s: schema file not found — %s", tool_name, not_found_error)
            return {"error": str(not_found_error)}

        except ValueError as validation_error:
            logger.error("[ERROR] %s: invalid schema definition — %s", tool_name, validation_error)
            return {"error": f"Invalid schema-alias definition: {validation_error}"}

        except Exception as unexpected_error:  # pylint: disable=broad-except
            logger.exception("[ERROR] %s: unexpected error", tool_name)
            return {"error": f"Unexpected error while loading schema aliases: {unexpected_error}"}
