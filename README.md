# NeuroSanVibes — Report Generation Agent

A [neuro-san](https://github.com/cognizant-ai-lab/neuro-san) agent network that turns a natural-language ITIL service request into a formatted `.xlsx` report backed by a SQLite database.

---

## Architecture

```
User query (natural language)
        │
 schema_alias_tool       ← strips real DB names; LLM only ever sees aliases
        │
 sql_generator_agent     ← drafts a SELECT using aliases only
        │
 sql_validator_agent     ← cross-checks the SQL (loops back up to 3×)
        │
 security_and_compliance_agent  ← checks for injection / sensitive-data access
        │
 sqlite_query_tool       ← deterministic sqlglot alias→real-name rewrite + execute
        │
 xlsx_writer_tool        ← formats rows into an .xlsx workbook
        │
 Report delivered  (file path + row count reported to user)
```

The real database/table/column names are **never** shown to the LLM. All SQL is generated in terms of aliases defined in `data/schema_aliases.json`; `sqlite_query_tool` performs a code-only (sqlglot AST) rewrite back to real names before execution.

---

## Folder structure

```
neurosanvibes_ReportGen/
├── streamlit_app.py          # Streamlit UI — main entry point
├── requirements.txt          # Extra Python dependencies
├── coded_tools/
│   ├── schema_alias_tool.py  # Loads alias schema; filters to relevant tables
│   ├── schema_alias_map.py   # sqlglot-based alias→real-name SQL rewrite
│   ├── sqlite_query_tool.py  # MCP-stand-in: rewrites + executes SQL
│   └── xlsx_writer_tool.py   # Writes query results to .xlsx
├── data/
│   ├── schema_aliases.json   # Alias definitions (committed)
│   └── company_data.db       # SQLite database (excluded from git)
├── registries/
│   ├── manifest.hocon        # Points neuro-san at the agent definition
│   ├── report_agent.hocon    # Agent network definition (excluded — has AWS keys)
│   └── report_agent.hocon.example  # Template — copy and fill in credentials
├── logs/                     # Rotating log files (excluded from git)
└── reports/                  # Generated .xlsx reports (excluded from git)
```

---

## Prerequisites

- Python 3.11+
- [neuro-san](https://github.com/cognizant-ai-lab/neuro-san) installed and on `PYTHONPATH`
- AWS credentials with access to Amazon Bedrock (Claude Sonnet via `anthropic-bedrock`)

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

The extra dependencies (`openpyxl`, `sqlglot`, `pandas`) are in addition to the core neuro-san requirements.

### 2. Configure AWS credentials

Copy the example config and fill in your credentials:

```bash
cp registries/report_agent.hocon.example registries/report_agent.hocon
# Edit report_agent.hocon and replace the placeholder values
```

> **Never commit `report_agent.hocon`** — it is listed in `.gitignore`.
> If you use AWS STS temporary credentials, refresh the session token before each run.

### 3. Run the Streamlit UI

From the **repository root** (one level above this folder):

```bash
streamlit run neurosanvibes_ReportGen/streamlit_app.py
```

The app opens at `http://localhost:8501`.

---

## Usage

1. Enter a natural-language report request in the **Report Query** box, or pick one of the built-in examples.
2. Optionally upload a custom `schema_aliases.json` via the sidebar.
3. Click **Generate Report**.
4. Switch between the **Agent Answer**, **Data Table**, and **Download** tabs to view and save the result.

### Example queries

- List all employees in the Finance department with their email addresses.
- Show the top 10 highest paid employees with their designation and department.
- Report all active projects with their completion percentage.
- List employees hired in the last 12 months with their hire date and department.
- Show total hours logged per project across all time entries.
- List all project tasks that are overdue with the assigned employee names.

---

## Schema aliases

`data/schema_aliases.json` maps real database names to human-friendly aliases the LLM uses:

| Module | Alias | Description |
|--------|-------|-------------|
| HR Management | HRM | Employee and department data |
| Project Management | PJM | Projects, tasks, assignments |
| Finance | FIN | Salary and financial tracking |

To adapt the agent to a different database, update `schema_aliases.json` and replace `data/company_data.db`.

---

## Coded tools

| Tool | Role |
|------|------|
| `SchemaAliasTool` | Loads alias schema; filters to tables relevant to the query |
| `SqliteQueryTool` | Alias→real-name rewrite (sqlglot) + SQLite execution |
| `XlsxWriterTool` | Formats rows into a styled `.xlsx` workbook |

`SqliteQueryTool` is a CodedTool stand-in for an MCP server. To connect a real MCP server, replace the CodedTool call while keeping the alias-translation step.

---

## License

Copyright © 2025-2026 Cognizant Technology Solutions Corp.  
Licensed under the [Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0).
