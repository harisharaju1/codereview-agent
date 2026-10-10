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


# Summary: the workflow graph's runtime context — the same idea as
# ReviewContext (used, never checkpointed), minus `head_sha`.
#
# WHY A SEPARATE TYPE RATHER THAN ReviewContext WITH head_sha OPTIONAL:
# runtime context is fixed when a run STARTS, but the workflow only
# discovers the PR's head commit DURING the run (its fetch_context node).
# So head_sha lives in WorkflowState instead (a public commit hash, not a
# secret — fine to checkpoint), and each specialist gets a complete
# ReviewContext, head_sha included, built at the moment it starts. Two
# precise types beat one type with a field that's "None until it isn't".
@dataclass(frozen=True)
class WorkflowContext:
    http_client: httpx.AsyncClient
    anthropic_client: anthropic.AsyncAnthropic
    settings: Settings
    installation_token: str
    owner: str
    repo: str
    pr_number: int


# Summary: what ONE specialist run receives — the payload of one
# Send("run_specialist", ...) from the routing step. Each parallel
# specialist gets its own copy, with only ITS files.
class SpecialistInput(TypedDict):
    specialist: str
    pr_title: str
    head_sha: str
    inventory: str
    files: list[dict]  # FileChange dumps, this specialist's scope only


# Summary: the workflow graph's memory: the PR's facts, triage's per-file
# result, and one result entry per specialist, merged at the end.
#
# The two reducers that make parallelism safe: `specialist_results` and
# the token counts use operator.add, so when several specialists finish
# in the SAME super-step, LangGraph concatenates their result lists and
# sums their token counts. Without a reducer on a key that parallel nodes
# both write, LangGraph raises InvalidUpdateError instead of silently
# letting the last writer win — a race condition becomes a loud error.
#
# Note what is NOT here: any specialist's message history. Each
# specialist's conversation (its tool calls, file contents, search
# results) lives inside its own subgraph run and is discarded when it
# finishes; only findings and token counts flow back up. That's this
# week's answer to "context management across files": compact shared
# GLOBAL context (the inventory) + isolated detailed LOCAL context per
# specialist, instead of one ever-growing shared transcript.
class WorkflowState(TypedDict):
    pr_title: str
    head_sha: str
    # The raw diff exists only between fetch_context and triage; triage
    # replaces it with "" once `files` holds the per-file slices, so the
    # full diff isn't carried (or, from Day 4, checkpointed) for the rest
    # of the run.
    diff_text: str
    files: list[dict]
    inventory: str
    specialist_results: Annotated[list[dict], operator.add]
    input_tokens: Annotated[int, operator.add]
    output_tokens: Annotated[int, operator.add]
    findings: list[dict]
    incomplete_specialists: list[str]
    dropped_findings: int
    outcome: Literal["submitted", "all_failed"] | None
