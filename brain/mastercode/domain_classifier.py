"""
Domain Classifier — Pre-inference knowledge domain tagging.

Classifies incoming messages into knowledge domains BEFORE inference fires.
Zero LLM cost — keyword-based with embedding fallback.

Used for:
  1. Codebook selection — load domain-specific codebook weights
  2. Epistemic awareness — flag when confidence is low for the detected domain

Domain taxonomy maps to MMLU categories.

Usage:
    from domain_classifier import classify_domain

    domain = classify_domain("What's the GDP growth formula?")
    # → {"domain": "economics", "confidence": 0.8, "method": "keyword"}
"""

import json
import logging
import re
from typing import Optional

log = logging.getLogger("cseries.domain_classifier")

DOMAIN_KEYWORDS = {
    "biology": [
        "biology", "cell", "dna", "rna", "mrna", "protein", "gene", "genome", "organism",
        "species", "evolution", "ecology", "photosynthesis", "mitosis", "meiosis",
        "enzyme", "bacteria", "virus", "mutation", "chromosome", "anatomy",
        "physiology", "neuroscience", "biochemistry", "vaccine", "cellular",
        "krebs", "mitochondria", "respiration", "genetic", "heredity",
    ],
    "chemistry": [
        "chemistry", "molecule", "atom", "element", "compound", "reaction",
        "bond", "orbital", "catalyst", "acid", "base", "ph", "oxidation",
        "organic chemistry", "inorganic", "periodic table", "molar", "isotope",
    ],
    "physics": [
        "physics", "force", "velocity", "acceleration", "momentum", "energy",
        "quantum", "relativity", "electromagnetic", "thermodynamics", "entropy",
        "wavelength", "frequency", "gravity", "magnetism", "optics", "nuclear",
    ],
    "math": [
        "math", "equation", "calculus", "algebra", "geometry", "statistics",
        "probability", "integral", "derivative", "matrix", "vector", "theorem",
        "proof", "polynomial", "logarithm", "trigonometry", "factorial",
    ],
    "computer science": [
        "algorithm", "data structure", "compiler", "operating system", "database",
        "machine learning", "neural network", "complexity", "recursion", "hash",
        "binary tree", "graph theory", "automata", "turing", "np-hard",
        "distributed system", "cryptography",
    ],
    "economics": [
        "economics", "gdp", "inflation", "supply", "demand", "market",
        "monetary", "fiscal", "trade", "tariff", "microeconomics", "macroeconomics",
        "utility", "equilibrium", "elasticity", "keynesian", "marginal",
    ],
    "law": [
        "law", "legal", "statute", "constitution", "court", "jurisdiction",
        "contract", "tort", "liability", "plaintiff", "defendant", "precedent",
        "amendment", "regulation", "compliance", "litigation", "criminal law",
    ],
    "history": [
        "history", "historical", "century", "civilization", "empire", "revolution",
        "colonial", "medieval", "renaissance", "world war", "ancient", "dynasty",
        "treaty", "independence", "monarchy", "republic",
    ],
    "philosophy": [
        "philosophy", "ethics", "epistemology", "metaphysics", "ontology",
        "existentialism", "utilitarianism", "categorical imperative", "socrates",
        "plato", "aristotle", "consciousness", "free will", "determinism",
        "moral", "virtue",
    ],
    "psychology": [
        "psychology", "cognitive", "behavioral", "therapy", "anxiety",
        "depression", "personality disorder", "attachment", "conditioning",
        "freud", "jung", "piaget", "maslow", "neurotransmitter", "dopamine",
        "serotonin", "schema", "bias",
    ],
    "health": [
        "health", "medical", "disease", "diagnosis", "treatment", "symptom",
        "patient", "clinical", "pharmaceutical", "dosage", "surgery",
        "pathology", "epidemiology", "vaccine", "infection", "chronic",
    ],
    "engineering": [
        "engineering", "circuit", "voltage", "current", "resistance",
        "structural", "mechanical", "thermal", "fluid dynamics", "aerodynamics",
        "materials science", "signal processing", "control system", "robotics",
    ],
    "business": [
        "business", "management", "marketing", "strategy", "startup",
        "revenue", "profit", "roi", "stakeholder", "supply chain",
        "organizational", "leadership", "negotiation", "venture capital",
    ],
    # ---- Coding-task domains (added Phase 3, 2026-05-03) ----
    # These map to the competence codebook scoring scheme
    # for in-orchestrator dispatch. Keyword-only — no inference.
    "coding": [
        "python", "function", "code", "script", "implement", "write a",
        "javascript", "typescript", "java", "rust", "go", "c++", "ruby",
        "class", "method", "variable", "loop", "iterate",
    ],
    "react": [
        "react", "jsx", "tsx", "component", "hook", "usestate", "useeffect",
        "props", "redux", "next.js", "frontend", "ui",
    ],
    "architecture": [
        "architecture", "design pattern", "system design", "microservice",
        "monolith", "modular", "decompose", "abstraction", "scalable",
        "high-level design", "service boundary", "domain-driven",
    ],
    "debugging": [
        "debug", "bug", "fix", "error", "exception", "stack trace",
        "traceback", "broken", "not working", "crash", "regression",
    ],
    "list_ops": [
        "list", "array", "filter", "map", "reduce", "sort", "reverse",
        "slice", "append", "iterate over",
    ],
    "string_parsing": [
        "string", "parse", "regex", "tokenize", "split", "substring",
        "format", "template", "interpolate",
    ],
    "algorithm": [
        "algorithm", "complexity", "big o", "recursion", "dynamic programming",
        "greedy", "search", "sort algorithm", "graph", "tree traversal",
    ],
}

# Pre-compile patterns for speed
_DOMAIN_PATTERNS = {}
for domain, keywords in DOMAIN_KEYWORDS.items():
    patterns = [re.compile(r'\b' + re.escape(kw) + r'\b', re.I) for kw in keywords]
    _DOMAIN_PATTERNS[domain] = patterns


def classify_domain(text: str, min_confidence: float = 0.15) -> dict:
    """
    Classify a message into a knowledge domain.

    Returns:
        {
            "domain": str or None,
            "confidence": float (0.0-1.0),
            "scores": {domain: score, ...},  # top 3
            "method": "keyword",
        }
    """
    if not text or len(text.strip()) < 10:
        return {"domain": None, "confidence": 0.0, "scores": {}, "method": "keyword"}

    lower = text.lower()
    word_count = max(len(lower.split()), 1)

    scores = {}
    for domain, patterns in _DOMAIN_PATTERNS.items():
        hits = sum(1 for p in patterns if p.search(lower))
        if hits > 0:
            score = min(1.0, hits / min(word_count * 0.1, 5))
            scores[domain] = round(score, 4)

    if not scores:
        return {"domain": None, "confidence": 0.0, "scores": {}, "method": "keyword"}

    top_domain = max(scores, key=scores.get)
    top_score = scores[top_domain]

    if top_score < min_confidence:
        return {"domain": None, "confidence": top_score, "scores": dict(sorted(scores.items(), key=lambda x: -x[1])[:3]), "method": "keyword"}

    top_3 = dict(sorted(scores.items(), key=lambda x: -x[1])[:3])

    return {
        "domain": top_domain,
        "confidence": top_score,
        "scores": top_3,
        "method": "keyword",
    }




def get_domain_taxonomy() -> dict:
    """Return the full domain taxonomy with keyword counts."""
    return {
        domain: {
            "keywords": len(keywords),
            "sample": keywords[:5],
        }
        for domain, keywords in DOMAIN_KEYWORDS.items()
    }
