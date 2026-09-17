"""AI Support Ticket Analyst.

An AI system for querying customer support ticket data in natural language and
flagging operational anomalies within it.

The package is organised so that each module owns exactly one concern:

    config      typed settings, loaded once from the environment
    data        CSV ingestion, SQLite persistence, the AS_OF time anchor
    sql_guard   validation of model-generated SQL before it is executed
    anomalies   deterministic statistical detectors (no LLM involved)
    prompts     construction of the system prompt sent to the model
    llm         tool-calling orchestration against the Groq API
    models      pydantic request and response schemas
    main        the FastAPI application and its endpoints

Exported here rather than in a separate VERSION file because this project is
Python-only: nothing outside the interpreter needs to read the version, and an
import avoids the file I/O and path resolution a plain text file would require.
Surfaced at runtime by the /health endpoint, so the running build can always be
identified.
"""

__version__ = "0.5.0"
