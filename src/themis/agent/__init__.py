"""An agent over THEMIS: a model that chooses which facts to fetch, never one that makes them.

Everything a review establishes — grain, lineage, findings, measured changes, conventions —
is exposed as a typed, read-only tool. A local model decides which to call to answer a
question, and every sentence of its answer has to quote a tool result verbatim or the
answer is refused. The tools are the same ones an MCP client gets, so an external agent and
the built-in one see exactly the same evidence, through exactly the same checks.
"""
