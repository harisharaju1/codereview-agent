import operator
from dataclasses import dataclass
from typing import Annotated, Literal, TypedDict

import anthropic
import httpx

from src.config.settings import Settings


# Summary: everything a review graph's nodes USE but the graph does not
# REMEMBER — clients, credentials, and the fixed facts of this one review.
# Passed per run via `graph.ainvoke(..., context=ReviewContext(...))` and
# read inside a node as `runtime.context`.
#
# WHY THIS IS SEPARATE FROM THE STATE BELOW (the most important decision in
# this file): LangGraph state is exactly what a checkpointer writes to
# storage after every step — Postgres, from Day 4 on. An installation
# token in state would mean a live GitHub credential sitting at rest in a
# database table; live client objects in state can't be serialized at all.
# Runtime context is NOT checkpointed (verified before writing the Week 3
# plan: a value passed via `context=` never appeared in a saved
# checkpoint). It's the same instinct as Week 2's executors closing over
# the token the MODEL must never see — applied here to what gets PERSISTED.
# Decided on Day 1, before any checkpointer exists, so that Day 4 is "add a
# checkpointer" rather than "add a checkpointer and discover a credential
# is being written to disk."
#
# frozen=True: these are the fixed facts of one review; no node has any
# business reassigning one mid-run.
@dataclass(frozen=True)
class ReviewContext:
    http_client: httpx.AsyncClient
    anthropic_client: anthropic.AsyncAnthropic
    settings: Settings
    installation_token: str
    owner: str
    repo: str
    head_sha: str
    pr_number: int


# Summary: the loop graph's memory between steps — the conversation so far,
# the iteration counter, running token totals, and how (if at all) the run
# has ended.
#
# HOW EACH KEY MERGES (its "reducer"): a node returns only the keys it
# changes, and LangGraph merges that partial update in. No annotation means
# REPLACE (`iteration`: the new value overwrites the old). `operator.add`
# means APPEND for a list (`messages`: return `[new_message]` and it's added
# to the end) and SUM for an int (`input_tokens`: return this call's tokens
# and they're added to the running total). A node never reads-copies-and-
# returns the whole list; it just says what it contributed.
#
# WHY `operator.add` FOR MESSAGES, NOT LangGraph's BUILT-IN `add_messages` /
# `MessagesState`: those expect LangChain message objects (AIMessage,
# ToolMessage) and merge by message ID. This engine sends raw Anthropic-
# format dicts straight to the Anthropic SDK, so a plain append is both
# correct and simpler. (Day 3's LangChain specialist WILL use add_messages
# — feeling that difference is part of the point.)
#
# WHY EVERY VALUE IS PLAIN JSON-SHAPED DATA (dicts/lists/str/int), NEVER AN
# ANTHROPIC SDK OBJECT: SDK objects (TextBlock, ToolUseBlock) in state
# trigger "Deserializing unregistered type ... will be blocked in a future
# version" once checkpointed (reproduced before writing the plan). Nodes
# convert SDK responses to dicts at the boundary, so nothing in here ever
# depends on a type the checkpointer has to know how to rebuild. `findings`
# is likewise stored as dumped ReviewFinding dicts and re-validated by the
# runner on the way out.
#
# WHY A TypedDict, NOT A PYDANTIC MODEL: Pydantic state would re-validate
# on every single update and adds serialization quirks through checkpoints.
# Validation already happens at the boundaries that matter (SubmitReviewArgs
# on the model's output; ReviewFinding on the way out); state is internal.
class LoopState(TypedDict):
    messages: Annotated[list[dict], operator.add]
    iteration: int
    input_tokens: Annotated[int, operator.add]
    output_tokens: Annotated[int, operator.add]
    stop_reason: str | None
    findings: list[dict] | None
    # None while the run is still going. Set exactly once, by whichever
    # node detects the end — see loop_graph.py on why outcomes are DATA
    # here rather than exceptions raised from inside a node.
    outcome: Literal["submitted", "no_submit", "exceeded"] | None
