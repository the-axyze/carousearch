"""An in-app agent that answers a shopping prompt with carousearch's MCP tools and four local tools
(`carousearch ask`, the web app's Ask mode).

- settings.py: the planner and worker models, the per-run budgets, the provider keys and the price table
- prompt.py: the system prompt
- result.py: the structured answer (AgentAnswer) and the model's part of it (ModelAnswer)
- budget.py: counts one run's tool calls, photos and listings against the budgets (pure)
- trace.py: the tool trace, built from the run's message history (pure)
- progress.py: the events a run hands an observer as tool calls start and finish (pure)
- cost.py: token usage per model and the cost estimate (pure)
- tool_results.py: the shorter tool results the planner is sent (pure)
- listings_db.py: the run's listings.db and listings.json (row building pure, the store I/O)
- local_tools.py: run_python, judge_listings, judge_photos, photo_metrics (I/O)
- sandbox.py, sandbox_bootstrap.py: run_python's confined subprocess
- judging.py, judge_cache.py, worker.py: judge prompts and answers (pure), their cache, the worker-model calls
- photo_metrics.py, run_photos.py: photo sharpness and brightness (pure), a run's photo files
- web_search.py: the provider's hosted web search, capped per run
- run_directory.py: data/runs/<time>-<id>/, one folder per run
- run_prompt.py: runs the agent with the MCP server in this process (model and Carousell I/O)
"""
