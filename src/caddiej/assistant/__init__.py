"""The boundary an outside assistant reaches CaddieJ through.

OpenJarvis, a ChatGPT-style agent, or any other MCP client integrates from the
outside, through a small set of named capabilities. Nothing in this package
knows what any of them are, and nothing here names a product: the surface is the
same whoever is asking, and there is no dependency on any particular assistant's
repository, protocol version or conventions.

The layering, and why there are three pieces rather than one:

* `contract.py` is the published API — hand-written wire models and a version.
  It imports nothing from CaddieJ, which is what lets the out-of-process MCP
  server share it without carrying a database driver.
* `reads.py` and `commands.py` are the façades. They call CaddieJ's own service
  APIs and project the results into the contract. They are the allowlist: a
  capability exists because there is a method here, and for no other reason.
* `registry.py` is the literal list of tools, which both transports read.

Two transports sit above this, and neither holds any logic:
`caddiej.adapters.api` serves the façades over loopback HTTP, and
`caddiej.mcp` is a separate process that speaks MCP to an assistant and HTTP to
CaddieJ. Either could be replaced without an assistant noticing the difference,
which is the test of whether this layer is in the right place.

CaddieJ runs identically with none of it switched on.

**This module deliberately re-exports nothing.** Every other package in CaddieJ
gathers its public names here, and this one must not: `contract` and `registry`
are imported by a separate process that has no business loading a database
driver or a coaching provider, and a convenience import in this file would pull
all of it in behind them. Importing `caddiej.assistant.contract` has to cost
exactly that module. A test in `test_assistant_contract.py` checks it in a fresh
interpreter, because inside a test session everything is already loaded and the
question is unanswerable.
"""
