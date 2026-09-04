"""Local test package marker.

Keeping the repository tests as an explicit package prevents an unrelated
site-packages ``tests`` module from shadowing ``tests.core`` imports when
pytest is run from a clean checkout.
"""
