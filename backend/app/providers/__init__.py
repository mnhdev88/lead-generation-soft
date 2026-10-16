"""Paid / external data providers used to enrich leads (e.g. Hunter.io for emails).

Each provider module offers available() -> bool (is it configured?) and counts every paid
request with usage.consume(...) so the monthly free allowance is never exceeded.
"""
