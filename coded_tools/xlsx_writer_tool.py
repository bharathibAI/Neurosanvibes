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
XlsxWriterTool: a CodedTool that formats SQL result rows into an .xlsx
workbook and writes it to disk, corresponding to the "Xlsx writer step" in
the report-generation architecture.
"""

import os
from datetime import datetime
from logging import Logger
from logging import getLogger
from typing import Any
from typing import Dict
from typing import List
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

_DEFAULT_OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")


class XlsxWriterTool(CodedTool):
    """
    CodedTool that writes tabular query results (columns + rows) out to an
    .xlsx workbook, so the finished report can be attached to/linked from the
    ITIL ticket.
    """

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        Writes the given columns/rows to an .xlsx file.

        :param args: A dictionary with keys:
                "columns": List[str] of column headers.
                "rows": List[List[Any]] of row data.
                "file_name": (optional) base name for the output file
                    (without extension). Defaults to a timestamped name.
                "sheet_name": (optional) worksheet name. Defaults to "Report".
                "output_dir": (optional) directory to write the file into.
                    Defaults to BH_report_agent/reports.

        :param sly_data: A dictionary containing parameters that should be kept out
                of the chat stream. Not used by this tool.

        :return: On success, a dict with key "file_path" pointing to the written
                 workbook. On failure, a dict with key "error" describing the problem.
        """
        tool_name: str = self.__class__.__name__
        logger: Logger = getLogger(self.__class__.__name__)

        logger.info("========== [START] %s ==========", tool_name)

        columns: List[str] = args.get("columns", [])
        rows: List[List[Any]] = args.get("rows", [])
        sheet_name: str = str(args.get("sheet_name", "Report")) or "Report"
        output_dir: str = str(args.get("output_dir", _DEFAULT_OUTPUT_DIR))

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        file_name: str = str(args.get("file_name") or f"report_{timestamp}")
        if not file_name.lower().endswith(".xlsx"):
            file_name = f"{file_name}.xlsx"

        logger.info("[STEP 1] Output target — dir: %s  file: %s  sheet: %s", output_dir, file_name, sheet_name)
        logger.info("[STEP 1] Data received — %d column(s): %s", len(columns), columns)
        logger.info("[STEP 1] Rows received — %d row(s)", len(rows))

        try:
            if not columns:
                raise ValueError("No column headers were provided for the report.")

            logger.info("[STEP 2] Creating output directory (if absent): %s", output_dir)
            os.makedirs(output_dir, exist_ok=True)
            file_path = os.path.join(output_dir, file_name)

            logger.info("[STEP 3] Building and saving workbook: %s", file_path)
            self._write_workbook(file_path, sheet_name, columns, rows, logger, tool_name)

            logger.info("[STEP 4] Workbook written — %d row(s) → %s", len(rows), file_path)
            logger.info("========== [DONE] %s ==========", tool_name)
            return {"file_path": file_path, "row_count": len(rows)}

        except ValueError as validation_error:
            logger.error("[ERROR] %s: validation error — %s", tool_name, validation_error)
            return {"error": str(validation_error)}

        except ImportError as import_error:
            logger.exception("[ERROR] %s: missing dependency (openpyxl not installed)", tool_name)
            return {"error": f"Missing dependency: {import_error}. Install 'openpyxl' to enable Xlsx export."}

        except OSError as os_error:
            logger.exception("[ERROR] %s: file-system error writing workbook", tool_name)
            return {"error": f"Could not write report file: {os_error}"}

        except Exception as unexpected_error:  # pylint: disable=broad-except
            logger.exception("[ERROR] %s: unexpected error", tool_name)
            return {"error": f"Unexpected error while writing report: {unexpected_error}"}

    @staticmethod
    def _write_workbook(
        file_path: str,
        sheet_name: str,
        columns: List[str],
        rows: List[List[Any]],
        logger: Logger,
        tool_name: str,
    ) -> None:
        """
        Creates and saves an .xlsx workbook containing a header row of column
        names followed by the data rows.

        :param file_path: Full path to write the workbook to.
        :param sheet_name: Name to give the worksheet.
        :param columns: Column headers.
        :param rows: Data rows.
        :param logger: Logger to record activity.
        :param tool_name: Name of the calling tool, for log messages.
        """
        # Imported lazily so the rest of the tool (and the agent network) can
        # still be imported/tested even if openpyxl isn't installed yet.
        from openpyxl import Workbook  # pylint: disable=import-outside-toplevel

        logger.info("         %s: building workbook — %d column(s), sheet '%s'", tool_name, len(columns), sheet_name[:31])

        workbook = Workbook()
        worksheet = workbook.active
        worksheet.title = sheet_name[:31]  # Excel sheet name length limit.

        worksheet.append(columns)
        for row in rows:
            worksheet.append(row)

        workbook.save(file_path)
