"""An in-app agent that answers a shopping prompt by calling carousearch's own MCP tools (`carousearch ask`).

- settings.py: AGENT_MODEL, the per-run budgets and the provider API keys
- prompt.py: the system prompt
- result.py: the structured answer (AgentAnswer) and the model's part of it (ModelAnswer)
- budget.py: counts one run's tool calls, photos and listings against the budgets (pure)
- trace.py: the tool trace, built from the run's message history (pure)
- progress.py: the events a run hands an observer as tool calls start and finish (pure)
- run_prompt.py: runs the agent with the MCP server in this process (model and Carousell I/O)
"""
