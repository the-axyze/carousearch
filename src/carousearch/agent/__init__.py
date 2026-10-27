"""An in-app agent that answers a request in two phases, plan then execute, with carousearch's MCP tools and five
local tools (`carousearch ask`, the web app's Ask mode, `carousearch eval`). Nothing in it knows any kind of item.

- settings.py: the planner and worker models, the per-run and planning budgets, the provider keys and the prices
- prompt.py: the planning and execution instructions and user messages (method only)
- plan.py: SearchPlan, the plan review and their checks (pure)
- templates.py: generic method templates the planner may follow (pure)
- knowledge.py: the owner's optional knowledge/ notes, selected by keyword (parsing pure, loading and saving I/O)
- planning.py: the planning phase, plan_prompt (model and Carousell I/O)
- run_prompt.py: the execution phase, execute_plan, and run_prompt for both (model and Carousell I/O)
- toolsets.py: ToolCallGate, the hook every tool call passes through, and the toolsets built on it
- result.py: the structured answer (AgentAnswer) and the model's part of it (ModelAnswer)
- budget.py: counts one phase's tool calls, photos and listings against its budget (pure)
- trace.py: the tool trace, built from the run's message history (pure)
- progress.py: the events a run hands an observer as tool calls start and finish (pure)
- cost.py: token usage per model and the cost estimate (pure)
- tool_results.py: the shorter tool results the planner is sent (pure)
- listings_db.py: the run's listings.db and listings.json (row building pure, the store I/O)
- local_tools.py: run_python, extract_fields, judge_listings, judge_photos, photo_metrics (I/O)
- extraction.py: extract_fields' specs, regexes, typing and worker prompt (pure)
- sandbox.py, sandbox_bootstrap.py: run_python's confined subprocess
- judging.py, judge_cache.py, worker.py: judge prompts and answers (pure), their cache, the worker-model calls
- photo_metrics.py, run_photos.py: photo sharpness and brightness (pure), a run's photo files
- web_search.py: the provider's hosted web search, capped per phase
- run_directory.py: data/runs/<time>-<id>/, one folder per run
- evals.py: the cross-category eval prompts and their plan checks (checks pure, the live runner I/O)
"""
