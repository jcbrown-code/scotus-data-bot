"""Shared pytest fixtures for the test suite.

Each Transform-stage test builds its own temporary staging DB (see test_scope /
test_dedup / test_validate), so no cross-stage fixture lives here yet.
"""
