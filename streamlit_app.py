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
Streamlit UI for the BH_report_agent report-generation agent network.

Accepts a natural-language query and an optional JSON schema-alias file,
invokes the neuro-san `report_agent` network in-process, and presents
the agent answer, a data-table preview, and an xlsx download.

Run from the repository root:
    streamlit run BH_report_agent/streamlit_app.py
"""

import glob
import json
import logging
import logging.handlers
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import streamlit as st

# ---------------------------------------------------------------------------
# Paths & environment setup (must happen BEFORE importing neuro_san)
# ---------------------------------------------------------------------------
AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parent
MANIFEST_FILE = AGENT_DIR / "registries" / "manifest.hocon"
TOOL_PATH = AGENT_DIR / "coded_tools"
DATA_DIR = AGENT_DIR / "data"
REPORTS_DIR = AGENT_DIR / "reports"
DEFAULT_SCHEMA = DATA_DIR / "schema_aliases.json"
DEFAULT_DB = DATA_DIR / "company_data.db"

os.environ["AGENT_MANIFEST_FILE"] = str(MANIFEST_FILE)
os.environ["AGENT_TOOL_PATH"] = str(TOOL_PATH)

# PYTHONPATH must include AGENT_DIR so neuro-san can convert the absolute
# AGENT_TOOL_PATH into a dot-separated module path ("coded_tools") by
# stripping the AGENT_DIR prefix. Without this, activation_factory strips
# the full path from itself, gets an empty string, and fails to resolve tools.
_path_sep = os.pathsep
_existing_parts = os.environ["PYTHONPATH"].split(_path_sep) if os.environ.get("PYTHONPATH") else []
_extra_paths = [str(AGENT_DIR), str(REPO_ROOT)]
for _p in reversed(_extra_paths):  # reversed so AGENT_DIR ends up at index 0
    if _p not in _existing_parts:
        _existing_parts.insert(0, _p)
os.environ["PYTHONPATH"] = _path_sep.join(_existing_parts)

for _p in _extra_paths:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---------------------------------------------------------------------------
# File logging setup
# ---------------------------------------------------------------------------
_TOOL_LOGGERS = (
    "SchemaAliasTool",
    "SqliteQueryTool",
    "XlsxWriterTool",
    "coded_tools.schema_alias_map",
)


def _setup_logging() -> None:
    """Configure rotating-file logging for BH report agent tools.

    Called once at module load so every Streamlit rerun reuses the same
    handlers (duplicate-handler guard prevents stacking on hot-reload).
    Log file: BH_report_agent/logs/bh_report_agent.log
    """
    logs_dir = AGENT_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / "bh_report_agent.log"

    plain_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        str(log_file), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(plain_fmt)

    for name in _TOOL_LOGGERS:
        tool_log = logging.getLogger(name)
        # Guard against duplicate handlers on Streamlit hot-reload.
        if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in tool_log.handlers):
            tool_log.addHandler(file_handler)
        tool_log.setLevel(logging.DEBUG)


_setup_logging()

# ---------------------------------------------------------------------------
# Page configuration & styling
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Report Generation Agent",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
        .main-header {
            font-size: 2.4rem; font-weight: 700; color: #1E88E5; margin-bottom: .2rem;
        }
        .sub-header {
            font-size: 1.05rem; color: #667; margin-bottom: 1.5rem;
        }
        .stButton > button {
            background-color: #1E88E5; color: white;
            font-weight: 600; padding: .6rem 2rem; border-radius: .5rem;
        }
        .stButton > button:hover { background-color: #1565C0; }
        .schema-badge {
            display: inline-block; background: #E3F2FD; color: #1565C0;
            border-radius: 4px; padding: 2px 8px; font-size: 0.82rem; margin: 2px;
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="main-header">📊NeuroSanVibes - Report Generation Agent</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub-header">'
       "</div>",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def newest_report(before: float) -> Path | None:
    """Return the newest .xlsx in REPORTS_DIR modified at or after `before`."""
    if not REPORTS_DIR.exists():
        return None
    candidates = [
        Path(p) for p in glob.glob(str(REPORTS_DIR / "*.xlsx"))
        if os.path.getmtime(p) >= before - 1
    ]
    return max(candidates, key=os.path.getmtime) if candidates else None


def run_agent(request_text: str) -> str:
    """Invoke the report_agent network in-process and return the answer text."""
    from neuro_san.client.simple_one_shot import SimpleOneShot  # noqa: WPS433
    one_shot = SimpleOneShot(agent="report_agent", connection_type="direct")
    return one_shot.get_answer_for(request_text)


def read_xlsx_as_df(path: Path):
    """Read an xlsx file into a pandas DataFrame. Returns None if pandas unavailable."""
    try:
        import pandas as pd
        return pd.read_excel(path, engine="openpyxl")
    except ImportError:
        return None


def extract_table_names(schema: dict) -> list[str]:
    """Return a list of display names from a schema dict."""
    tables = schema.get("tables", [])
    if isinstance(tables, dict):
        return list(tables.keys())
    return [t.get("alias") or t.get("name") or "?" for t in tables]


# ---------------------------------------------------------------------------
# Sidebar: configuration & schema upload
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("⚙️ Configuration")
    st.text_input("Agent network", value="report_agent", disabled=True)
    st.text_input("Database", value=str(DEFAULT_DB.relative_to(REPO_ROOT)), disabled=True)

    st.divider()
    st.subheader("📄 Schema JSON")
    st.caption(
        "Upload a JSON schema-alias file to use for this run instead of the default. "
        "Must contain a top-level `tables` key."
    )

    uploaded_schema = st.file_uploader(
        "Upload schema-alias JSON",
        type=["json"],
        help="Provide the alias→real-name mapping JSON for your data.",
    )

    schema_path_for_run = str(DEFAULT_SCHEMA)

    if uploaded_schema is not None:
        try:
            parsed = json.load(uploaded_schema)
            if "tables" not in parsed:
                st.error("Invalid schema: missing top-level `tables` key.")
            else:
                tmp = Path(tempfile.gettempdir()) / f"bh_schema_{os.getpid()}.json"
                tmp.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
                schema_path_for_run = str(tmp)

                table_names = extract_table_names(parsed)
                st.success(f"Loaded {len(table_names)} table(s)")

                with st.expander("Tables in uploaded schema", expanded=True):
                    badges = "".join(
                        f'<span class="schema-badge">{n}</span>' for n in table_names[:20]
                    )
                    if len(table_names) > 20:
                        badges += f'<span class="schema-badge">+{len(table_names)-20} more</span>'
                    st.markdown(badges, unsafe_allow_html=True)
        except json.JSONDecodeError as err:
            st.error(f"JSON parse error: {err}")
    else:
        if DEFAULT_SCHEMA.exists():
            try:
                default_data = json.loads(DEFAULT_SCHEMA.read_text(encoding="utf-8"))
                default_tables = extract_table_names(default_data)
                st.info(f"Using default schema — {len(default_tables)} table(s)")
                with st.expander("Default schema tables"):
                    badges = "".join(
                        f'<span class="schema-badge">{n}</span>' for n in default_tables
                    )
                    st.markdown(badges, unsafe_allow_html=True)
            except Exception:
                pass

    st.divider()
    st.caption(f"Manifest: `{MANIFEST_FILE.relative_to(REPO_ROOT)}`")
    st.caption(f"Tools: `{TOOL_PATH.relative_to(REPO_ROOT)}`")
    st.caption(f"Schema: `{'uploaded' if schema_path_for_run != str(DEFAULT_SCHEMA) else 'default'}`")

    st.divider()
    with st.expander("🔍 Debug: Resolved Paths", expanded=False):
        st.write("**AGENT_DIR**", str(AGENT_DIR))
        st.write("**REPO_ROOT**", str(REPO_ROOT))
        st.write("**MANIFEST_FILE**", str(MANIFEST_FILE), "— exists:", MANIFEST_FILE.exists())
        st.write("**TOOL_PATH**", str(TOOL_PATH), "— exists:", TOOL_PATH.exists())
        st.write("**DATA_DIR**", str(DATA_DIR), "— exists:", DATA_DIR.exists())
        st.write("**REPORTS_DIR**", str(REPORTS_DIR), "— exists:", REPORTS_DIR.exists())
        st.write("**DEFAULT_SCHEMA**", str(DEFAULT_SCHEMA), "— exists:", DEFAULT_SCHEMA.exists())
        st.write("**DEFAULT_DB**", str(DEFAULT_DB), "— exists:", DEFAULT_DB.exists())
        st.write("**AGENT_MANIFEST_FILE env**", os.environ.get("AGENT_MANIFEST_FILE", "(not set)"))
        st.write("**AGENT_TOOL_PATH env**", os.environ.get("AGENT_TOOL_PATH", "(not set)"))


# ---------------------------------------------------------------------------
# Main area: query input
# ---------------------------------------------------------------------------
st.subheader("📝 Report Query")

EXAMPLE_QUERIES = [
    "List all employees in the Finance department with their email addresses.",
    "Show the top 10 highest paid employees with their designation and department.",
    "Report all active projects with their completion percentage.",
    "List employees hired in the last 12 months with their hire date and department.",
    "Show total hours logged per project across all time entries.",
    "List all project tasks that are overdue with the assigned employee names.",
]

col_query, col_example = st.columns([3, 1])
with col_example:
    example = st.selectbox("Example queries", ["— select —"] + EXAMPLE_QUERIES, index=0)

with col_query:
    prefill = "" if example == "— select —" else example
    query = st.text_area(
        "Describe the report you need",
        value=prefill,
        height=130,
        placeholder="e.g. List all employees in the Finance department with their salaries.",
    )

run_clicked = st.button("🚀 Generate Report", type="primary")

if "history" not in st.session_state:
    st.session_state.history = []


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------
if run_clicked:
    if not query.strip():
        st.warning("Please enter a query describing the report you need.")
    else:
        request_text = query.strip()

        # Tell the agent to use the uploaded schema when one was provided.
        if schema_path_for_run != str(DEFAULT_SCHEMA):
            request_text += (
                f"\n\n(Use schema_path='{schema_path_for_run}' when calling "
                "schema_alias_tool and sqlite_query_tool.)"
            )

        start_time = datetime.now().timestamp()

        with st.spinner("Running the report-generation pipeline…"):
            try:
                answer = run_agent(request_text)
                report_file = newest_report(start_time)

                st.success("✅ Report generation complete.")

                # ── Tabbed results ────────────────────────────────────────
                tab_answer, tab_table, tab_download = st.tabs(
                    ["💬 Agent Answer", "📋 Data Table", "📥 Download"]
                )

                with tab_answer:
                    st.markdown(answer or "_(no textual answer returned)_")

                with tab_table:
                    if report_file and report_file.exists():
                        df = read_xlsx_as_df(report_file)
                        if df is not None:
                            st.caption(
                                f"{len(df):,} rows × {len(df.columns)} columns  "
                                f"— `{report_file.name}`"
                            )
                            st.dataframe(df, use_container_width=True)
                        else:
                            st.info(
                                "Install `pandas` to preview the table here. "
                                "You can still download the xlsx file."
                            )
                    else:
                        st.info("No xlsx report was generated for this query.")

                with tab_download:
                    if report_file and report_file.exists():
                        with open(report_file, "rb") as fh:
                            st.download_button(
                                label=f"⬇️  Download  {report_file.name}",
                                data=fh.read(),
                                file_name=report_file.name,
                                mime=(
                                    "application/vnd.openxmlformats-officedocument"
                                    ".spreadsheetml.sheet"
                                ),
                            )
                        st.caption(f"Saved to `{report_file}`")
                    else:
                        st.info("No .xlsx report file was detected in the reports folder.")

                # ── History ───────────────────────────────────────────────
                st.session_state.history.insert(
                    0,
                    {
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "query": query.strip(),
                        "answer": answer,
                        "report": str(report_file) if report_file else None,
                    },
                )

            except ModuleNotFoundError as err:
                st.error(
                    f"neuro-san runtime could not be imported ({err}). "
                    "Install the project dependencies with `pip install -r requirements.txt` "
                    "and try again."
                )
            except Exception as err:  # pylint: disable=broad-except
                st.error(f"Report generation failed: {err}")
                st.exception(err)


# ---------------------------------------------------------------------------
# Query history
# ---------------------------------------------------------------------------
if st.session_state.history:
    st.divider()
    st.subheader("🕑 Query History")
    for item in st.session_state.history[:10]:
        label = f"[{item['time']}]  {item['query'][:80]}"
        with st.expander(label):
            st.markdown(item["answer"] or "_(no answer)_")
            if item.get("report"):
                st.caption(f"Report file: `{item['report']}`")
