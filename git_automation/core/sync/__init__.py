"""Polling/sync engine.

Periodically queries ``core.gh``, diffs against stored last-seen state, and
emits typed events (IssueAssigned, Mentioned, ReviewRequested, ...). Polls as
fast as GitHub allows via conditional requests + the ``X-Poll-Interval`` header,
falling back to a fixed 5-minute interval. Depends on ``core.gh`` + a local
state store.
"""
