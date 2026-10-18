from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from djedai.contracts.agent import AgentState, Block, Entities, Understanding


def load_ctx(state: AgentState) -> dict:
    """Loads the catalog and context for the specific dataset, and resets per-turn state."""
    # Stub: Real implementation will fetch from DB based on state["dataset_id"]
    return {
        "catalog_prompt": f"Catalog mapped for {state.get('dataset_id')}",
        "plan": [],
        "results": [],
        "charts": [],
        "answer_blocks": [],
        "retries": 0,
    }


def understand(state: AgentState) -> dict:
    """Invokes the model to understand intent."""
    # Stub: Minimal logic for graph framework testing
    last_msg = state.get("messages", [])[-1].content.lower() if state.get("messages") else ""

    intent = "ambiguous" if "ambiguous" in last_msg else "data_question"

    return {
        "understanding": Understanding(intent=intent, entities=Entities()),
    }


def plan(state: AgentState) -> dict:
    return {"plan": []}


def execute(state: AgentState) -> dict:
    # Later this will use the tools from djedai.analytics.registry
    # and return actual djedai.contracts.tool_result.ToolResult objects
    return {"results": []}


def visualize(state: AgentState) -> dict:
    return {"charts": []}


def compose(state: AgentState) -> dict:
    return {"answer_blocks": [Block(type="text", markdown="Calculated response")]}


def verify(state: AgentState) -> dict:
    # Final check of numbers in text vs data
    return {}


def clarify(state: AgentState) -> dict:
    return {"answer_blocks": [Block(type="clarification", markdown="Please clarify your request.")]}


def route_after_understand(state: AgentState) -> str:
    """Conditional edge: Routes off_topic/invalid/ambiguous to clarify, else plans."""
    intent = state["understanding"].intent
    if intent in ["ambiguous", "invalid", "off_topic"]:
        return "clarify"
    return "plan"


def build_agent_graph() -> StateGraph:
    workflow = StateGraph(AgentState)

    workflow.add_node("load_ctx", load_ctx)
    workflow.add_node("understand", understand)
    workflow.add_node("plan", plan)
    workflow.add_node("execute", execute)
    workflow.add_node("visualize", visualize)
    workflow.add_node("compose", compose)
    workflow.add_node("verify", verify)
    workflow.add_node("clarify", clarify)

    workflow.add_edge(START, "load_ctx")
    workflow.add_edge("load_ctx", "understand")

    workflow.add_conditional_edges(
        "understand", route_after_understand, {"plan": "plan", "clarify": "clarify"}
    )

    workflow.add_edge("plan", "execute")
    workflow.add_edge("execute", "visualize")
    workflow.add_edge("visualize", "compose")
    workflow.add_edge("compose", "verify")
    workflow.add_edge("verify", END)

    workflow.add_edge("clarify", END)

    return workflow.compile(checkpointer=MemorySaver())
