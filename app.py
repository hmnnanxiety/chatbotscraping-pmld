"""
app.py (Gemini version, hybrid RAG)

FastAPI backend for the DWH chatbot. Exposes a single /chat endpoint that
runs a function-calling loop with Gemini.

What changed vs. the live-fetch version
---------------------------------------
* `get_chart_data` (live Superset call) is DEPRECATED here and replaced by
  `search_dwh_knowledge`, a semantic search over the vector DB that the ETL
  (main_orchestrator.py) keeps fresh. No request ever hits Superset now, so
  the timeouts / 422s stay in the nightly ETL instead of in user requests.
* `list_charts` is kept (unchanged) for "what data do you have?" questions.
* Tools are registered in TOOL_HANDLERS. Adding a real-time tool later
  (e.g. CCTV) is one FunctionDeclaration + one handler entry.
* The loop itself (call model -> run function_calls -> feed responses back,
  capped at MAX_TOOL_ROUNDTRIPS) is unchanged.

Usage:
    pip install fastapi uvicorn google-genai python-dotenv chromadb
    export GEMINI_API_KEY=...
    uvicorn app:app --reload
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from google import genai
from google.genai import types

import vector_store
from chart_service import list_charts
from dashboards import known_dashboards

load_dotenv()

app = FastAPI(title="DWH RAG Chatbot")

genai_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
MAX_TOOL_ROUNDTRIPS = 5
DEFAULT_TOP_K = 5
MAX_TOP_K = 10

SYSTEM_PROMPT = (
    "You are an assistant for the Yogyakarta provincial government's data "
    "warehouse (population, health, land, food prices, staffing, UMKM, and "
    "more). Reply in the language the user writes in.\n\n"
    "Your knowledge comes from a periodically refreshed SNAPSHOT of the "
    "dashboards, not live data. For any question about a specific metric, "
    "call search_dwh_knowledge with a focused query (one metric or topic; "
    "include the region/year if the user gave one). Use the dashboard_name "
    "filter only when the user clearly names a dashboard. If the first "
    "search misses, retry once with different wording.\n\n"
    "Rules:\n"
    "- Answer only from the returned passages, and name the dashboard and "
    "chart you used. Never invent numbers.\n"
    "- Each passage has rows_covered (e.g. '1-20 of 145'). If it covers only "
    "part of a table, do not present totals, rankings, or 'the highest/lowest' "
    "as if they were complete — say the data shown is partial.\n"
    "- Mention data_as_of when freshness matters, and say so if a passage is "
    "possibly_outdated.\n"
    "- If nothing relevant is found, say so instead of guessing.\n"
    "- Use list_charts only when the user asks what data is available."
)

# --------------------------------------------------------------------------- #
# Tool schema
# --------------------------------------------------------------------------- #

LIST_CHARTS_DECL = types.FunctionDeclaration(
    name="list_charts",
    description=(
        "List all charts in the knowledge base as {slice_id, slice_name, "
        "dashboard_name}. Only for questions about what data is available; "
        "it does not return any numbers."
    ),
    parameters=types.Schema(type=types.Type.OBJECT, properties={}, required=[]),
)

_DASHBOARD_NAMES = [d["name"] for d in known_dashboards()]

SEARCH_KNOWLEDGE_DECL = types.FunctionDeclaration(
    name="search_dwh_knowledge",
    description=(
        "Semantic search over a periodically refreshed snapshot of the "
        "government data warehouse. Returns the passages most relevant to the "
        "query, each labelled with its dashboard, chart, the rows of the "
        "source table it covers, and when the data was extracted. Use this "
        "first for any question about a specific metric."
    ),
    parameters=types.Schema(
        type=types.Type.OBJECT,
        properties={
            "query": types.Schema(
                type=types.Type.STRING,
                description=(
                    "What to look for, e.g. 'jumlah penduduk Kabupaten Sleman "
                    "2024'. One metric or topic per call."
                ),
            ),
            "dashboard_name": types.Schema(
                type=types.Type.STRING,
                enum=_DASHBOARD_NAMES or None,
                description="Optional. Restrict the search to one dashboard.",
            ),
            "top_k": types.Schema(
                type=types.Type.INTEGER,
                description=f"Passages to return (1-{MAX_TOP_K}). Default {DEFAULT_TOP_K}.",
            ),
        },
        required=["query"],
    ),
)

TOOLS = [types.Tool(function_declarations=[LIST_CHARTS_DECL, SEARCH_KNOWLEDGE_DECL])]

GENERATE_CONFIG = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    tools=TOOLS,
)

# --------------------------------------------------------------------------- #
# Tool handlers
# --------------------------------------------------------------------------- #

def _tool_list_charts(args: Dict[str, Any]) -> Dict[str, Any]:
    return {"charts": list_charts()}


def _tool_search_dwh_knowledge(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("'query' is required.")

    # Gemini delivers JSON numbers as floats (5.0), so coerce and clamp.
    top_k = max(1, min(MAX_TOP_K, int(args.get("top_k") or DEFAULT_TOP_K)))
    dashboard_name = args.get("dashboard_name") or None

    hits = vector_store.search(query, top_k=top_k, dashboard_name=dashboard_name)
    if not hits:
        return {"results": [], "note": "No relevant passages found in the knowledge base."}

    results = []
    for hit in hits:
        m = hit["metadata"]
        results.append(
            {
                "dashboard": m.get("dashboard_name"),
                "chart": m.get("slice_name"),
                "slice_id": m.get("slice_id"),
                "rows_covered": f"{m.get('row_start')}-{m.get('row_end')} of {m.get('total_rows')}",
                "data_as_of": m.get("extracted_at"),
                "possibly_outdated": bool(m.get("stale")),
                "relevance": round(hit["score"], 3),
                "content": hit["content"],
            }
        )
    return {"results": results}


TOOL_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "list_charts": _tool_list_charts,
    "search_dwh_knowledge": _tool_search_dwh_knowledge,
    # Real-time tools stay live, e.g.: "get_cctv_status": _tool_get_cctv_status,
}


def _execute_tool(name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    return handler(tool_input)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    contents: List[types.Content] = [
        types.Content(role="user", parts=[types.Part.from_text(text=req.message)])
    ]

    for round_num in range(1, MAX_TOOL_ROUNDTRIPS + 1):
        print(f"[chat] Round {round_num}: calling model")
        response = genai_client.models.generate_content(
            model=MODEL,
            contents=contents,
            config=GENERATE_CONFIG,
        )

        # A blocked / empty generation has no candidates or no parts.
        if not response.candidates or not response.candidates[0].content:
            raise HTTPException(status_code=502, detail="The model returned no content.")
        candidate = response.candidates[0]
        parts = candidate.content.parts or []

        function_calls = [p.function_call for p in parts if p.function_call is not None]

        if not function_calls:
            final_text = "".join(p.text for p in parts if p.text)
            print("[chat] Model returned final answer")
            return ChatResponse(reply=final_text)

        contents.append(candidate.content)

        response_parts: List[types.Part] = []
        for fc in function_calls:
            name = fc.name
            args = dict(fc.args or {})

            print(f"[chat] Tool call: {name}({args})")
            try:
                result = _execute_tool(name, args)
                response_parts.append(
                    types.Part.from_function_response(name=name, response={"result": result})
                )
            except Exception as e:
                print(f"[chat] Tool call failed: {e}")
                response_parts.append(
                    types.Part.from_function_response(name=name, response={"error": str(e)})
                )

        contents.append(types.Content(role="user", parts=response_parts))

    raise HTTPException(
        status_code=502,
        detail="Could not resolve the request after multiple tool calls.",
    )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}