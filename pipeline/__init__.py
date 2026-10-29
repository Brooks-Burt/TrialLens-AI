"""
pipeline/ -- the five v3 stages: plan, validate, retrieve, organize, summarize.

Stages pass plain dataclasses to each other and never import one another, so
each can be run and tested alone. `common/` is the one shared dependency.
"""
