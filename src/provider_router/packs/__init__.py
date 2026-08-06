"""Adapter packs — ready-made providers for a domain.

The core router is domain-agnostic; a pack is a concrete set of adapters plus
the request/response contract they share. Packs ship as extras so the core
keeps its zero-dependency install:

    pip install provider-router[flights]
"""
