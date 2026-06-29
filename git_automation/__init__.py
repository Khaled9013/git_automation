"""git_automation — GUI git client + GitHub cockpit with an agentic workflow.

Modules:
    core      domain logic (git, gh, sync, models); no UI knowledge
    web       FastAPI backend + browser UI (also hosts the agentic dashboard)
    desktop   pywebview shell + system tray + OS notifications + poller
    skilltree agent-capability graph (model, storage, visualization data)
    agents    Claude Agent SDK orchestration (manager + sub-agents)
"""

__version__ = "0.0.1"
